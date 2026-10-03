"""Loopback OTLP/HTTP-JSON receiver for Claude Code telemetry.

Claude Code subagent transcripts lack final output usage for most responses
(measured 2026-10-04), so model-fit cannot close a Claude Code episode from
them. Claude Code's own OpenTelemetry export reports every API request with
final counts. This receiver accepts that export on 127.0.0.1 only and appends
each request body, unmodified, to a daily JSONL file in the goal store's
ignored model-fit runtime directory. It interprets nothing; the model-fit
adapter reads the files.

Its purview is Claude Code work under a master, so it lives with the master
lease: `master start` (Claude client) starts it detached and
`semaphore master-release` stops it. It is never a login item.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 4318
SIGNALS = {"/v1/logs": "logs", "/v1/traces": "traces", "/v1/metrics": "metrics"}
MAX_BODY = 64 * 1024 * 1024
_LOCK = threading.Lock()


def output_directory(model_fit_dir: Path) -> Path:
    return Path(model_fit_dir) / "runtime" / "claude-otel"


def _append(directory: Path, signal: str, body: bytes) -> None:
    now = datetime.now(timezone.utc)
    record = {"received": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "signal": signal,
              "body": json.loads(body)}
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    path = directory / f"otlp-{now.strftime('%Y%m%d')}.jsonl"
    with _LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)


def make_handler(directory: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            signal = SIGNALS.get(self.path.split("?", 1)[0])
            length = int(self.headers.get("Content-Length") or 0)
            if signal is None or length <= 0 or length > MAX_BODY:
                self.send_response(404 if signal is None else 413)
                self.end_headers()
                return
            if "json" not in (self.headers.get("Content-Type") or ""):
                # Only http/json is configured; a protobuf body would be stored unreadable.
                self.send_response(415)
                self.end_headers()
                return
            body = self.rfile.read(length)
            try:
                _append(directory, signal, body)
            except (OSError, ValueError) as exc:
                print(f"claude-telemetry: dropped {signal} body: {exc}", file=sys.stderr, flush=True)
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args) -> None:
            pass

    return Handler


class LoopbackHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that binds without a reverse name lookup.

    `HTTPServer.server_bind` calls `socket.getfqdn(host)` between bind and
    listen. On hosts with slow reverse DNS (GitHub's macOS runners take about
    35 s for 127.0.0.1) the port stays closed that long, so `start` reports a
    failed receiver that is in fact still coming up. The name is unused here.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def serve(model_fit_dir: Path, port: int = DEFAULT_PORT) -> None:
    directory = output_directory(model_fit_dir)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    server = LoopbackHTTPServer(("127.0.0.1", port), make_handler(directory))
    print(f"claude-telemetry: listening on 127.0.0.1:{port}, writing {directory}", flush=True)
    server.serve_forever()


RUNTIME = Path(__file__).resolve().parents[1] / ".creme"
PID_FILE = RUNTIME / "claude-telemetry.pid"
LOG_FILE = RUNTIME / "claude-telemetry.log"
SERVE_MARKER = "claude-telemetry serve"


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _recorded_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, check=False).stdout
    except OSError:
        return None
    # A recycled pid running something else is never ours to signal.
    return pid if SERVE_MARKER in command else None


def status(port: int = DEFAULT_PORT) -> dict:
    return {"listening": _listening(port), "pid": _recorded_pid(), "port": port}


def start(model_fit_dir: Path, port: int = DEFAULT_PORT) -> dict:
    """Start the receiver detached unless something already listens on the port."""
    if _listening(port):
        return {"status": "running", **status(port)}
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "creme", "claude-telemetry", "serve", "--dir", str(model_fit_dir),
             "--port", str(port)],
            cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
    PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
    for _ in range(50):
        if _listening(port):
            return {"status": "started", **status(port)}
        if process.poll() is not None:
            break
        threading.Event().wait(0.1)
    return {"status": "failed", "detail": f"receiver did not listen; see {LOG_FILE}", **status(port)}


def stop(port: int = DEFAULT_PORT) -> dict:
    """Stop the recorded receiver; never signal a process that is not it."""
    pid = _recorded_pid()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        return {"status": "not-running" if not _listening(port) else "foreign-listener", **status(port)}
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if _recorded_pid() is None:
            break
        threading.Event().wait(0.1)
    PID_FILE.unlink(missing_ok=True)
    return {"status": "stopped", **status(port)}
