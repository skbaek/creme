"""Loopback OTLP/HTTP-JSON receiver for Claude Code telemetry.

Claude Code subagent transcripts lack final output usage for most responses
(measured 2026-10-04), so model-fit cannot close a Claude Code episode from
them. Claude Code's own OpenTelemetry export reports every API request with
final counts. This receiver accepts that export on 127.0.0.1 only and appends
each request body, unmodified, to a daily JSONL file in the goal store's
ignored model-fit runtime directory. It interprets nothing; the model-fit
adapter reads the files.
"""

from __future__ import annotations

import json
import os
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


def serve(model_fit_dir: Path, port: int = DEFAULT_PORT) -> None:
    directory = output_directory(model_fit_dir)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(directory))
    print(f"claude-telemetry: listening on 127.0.0.1:{port}, writing {directory}", flush=True)
    server.serve_forever()
