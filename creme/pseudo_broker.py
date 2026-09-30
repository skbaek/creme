"""Protocol-independent plumbing shared by Creme's brokered pseudo-subagents.

A brokered pseudo-subagent (Luna reserve over ``codex app-server``, Muse over
``muse serve``) keeps a guarded model session alive between one-shot CLI
calls. Everything here is independent of the wire protocol the broker speaks
to its model server: private files, the per-session event feed, the broker's
Unix socket (server loop and client), stale-broker replacement, and the
bounded client commands (``wait``, ``events``, ``list``, ``detail``,
``shutdown``) that read the on-disk registry. Each capability supplies a
:class:`ClientSpec` naming its state directory, its exit codes, and how a
session record maps to an exit code and a few printed lines.

Layout under a capability's state directory (all private to the user)::

    broker/broker.sock    0600 socket in a 0700 directory, peer uid checked
    broker/broker.json    pid, instance token, socket, start time, code digest
    broker/broker.log     broker stdout/stderr
    sessions/<id>/        session.json (registry record), events.jsonl, ...
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


DETAIL_LEVELS = ("silent", "summary", "live")
DEFAULT_DETAIL = "silent"
_EVENT_RANK = {"attention": 0, "summary": 1, "live": 2}
_DETAIL_RANK = {"silent": 0, "summary": 1, "live": 2}

MAX_REQUEST_BYTES = 256 * 1024
MAX_SOCKET_PATH_BYTES = 100
LINE_WIDTH = 200
STOP_WAIT_SECONDS = 60.0

EXIT_USAGE = 2
EXIT_ATTENTION = 20
EXIT_INTERRUPTED = 21
EXIT_TIMEOUT = 124


class BrokerError(RuntimeError):
    """A broker or client condition with a user-facing reason."""


# ---------------------------------------------------------------------------
# Files and permissions


def broker_dir(state: Path) -> Path:
    return state / "broker"


def sessions_dir(state: Path) -> Path:
    return state / "sessions"


def socket_path(state: Path) -> Path:
    return broker_dir(state) / "broker.sock"


def info_path(state: Path) -> Path:
    return broker_dir(state) / "broker.json"


def private_dir(path: Path) -> Path:
    """Create or verify a directory only this user can enter (0700, owned, no symlink)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BrokerError(f"{path} is not a plain directory")
    if info.st_uid != os.getuid():
        raise BrokerError(f"{path} is not owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def write_private_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def read_json(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def one_line(text: Any, width: int = LINE_WIDTH) -> str:
    flat = " | ".join(part.strip() for part in str(text).splitlines() if part.strip())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_digest(module_root: Path, relatives: Iterable[str]) -> str:
    """Digest of the files a broker runs, so a client never drives a broker on other code."""
    digest = hashlib.sha256()
    for relative in relatives:
        try:
            digest.update((module_root / relative).read_bytes())
        except OSError:
            digest.update(b"missing:" + relative.encode())
    return digest.hexdigest()[:16]


LIBRARY_FIRST_RELATIVE = Path("templates/shared/library-first.md")


def library_first_text(module_root: Path) -> str:
    """The canonical shared-library rule every pseudo-subagent contract carries."""
    return (module_root / LIBRARY_FIRST_RELATIVE).read_text(encoding="utf-8").strip()


def render_library_first(template: str, module_root: Path) -> str:
    """Fill a contract template's ``{library_first}`` placeholder from the canonical file."""
    return template.replace("{library_first}", library_first_text(module_root))


def peer_uid(connection: socket.socket) -> Optional[int]:
    """The connecting process's uid, or None when the platform cannot say."""
    try:
        if sys.platform == "darwin":
            # getsockopt(SOL_LOCAL=0, LOCAL_PEERCRED=1) -> struct xucred
            raw = connection.getsockopt(0, 1, 76)
            _version, uid = struct.unpack_from("=II", raw)
            return uid
        if hasattr(socket, "SO_PEERCRED"):
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Registry reads (no broker needed)


def load_record(state: Path, session_id: str, pattern: "re.Pattern[str]") -> Optional[dict]:
    if not pattern.match(session_id or ""):
        return None
    return read_json(sessions_dir(state) / session_id / "session.json")


def all_records(state: Path, prefix: str) -> list[dict]:
    root = sessions_dir(state)
    if not root.is_dir():
        return []
    records = [read_json(path) for path in sorted(root.glob(f"{prefix}*/session.json"))]
    return sorted((record for record in records if record), key=lambda record: record.get("id", ""))


def read_events(state: Path, session_id: str, offset: int = 0) -> tuple[list[dict], int]:
    path = sessions_dir(state) / session_id / "events.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset
    events = []
    consumed = 0
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break
        consumed += len(raw)
        try:
            events.append(json.loads(raw))
        except ValueError:
            continue
    return events, offset + consumed


def visible(event: dict, detail: str) -> bool:
    return _EVENT_RANK.get(event.get("level"), 2) <= _DETAIL_RANK.get(detail, 0)


def format_event(event: dict) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", 0)))
    return one_line(f"[{event.get('seq')}] {stamp} {event.get('kind')}: {event.get('text')}")


class SessionRecord:
    """Record and event-feed methods of a broker-side session.

    The subclass sets ``dir``, ``record``, ``data_lock`` (an ``RLock``), and
    ``seq`` (the last event number) before it calls these.
    """

    dir: Path
    record: dict
    data_lock: Any
    seq: int

    def persist(self) -> None:
        with self.data_lock:
            self.record["updated"] = now_iso()
            write_private_json(self.dir / "session.json", self.record)
            if self.record.get("model_fit"):
                from .model_fit_adapters import broker_capture
                from .model_fit_episodes import EpisodeError
                captured_fields = {name: self.record.get(name) for name in
                                   ("id", "model", "effort", "model_fit", "turns")}
                fingerprint = hashlib.sha256(json.dumps(captured_fields, sort_keys=True).encode()).hexdigest()
                # In-memory only: a restarted broker always replays durable
                # source evidence. Routine log/state persistence need not reopen
                # SQLite or reconcile the identical turn history every time.
                if getattr(self, "_model_fit_capture_fingerprint", None) == fingerprint:
                    return
                try:
                    health = broker_capture(self.record)
                    self.record["model_fit_capture"] = {"status": "captured", "inbox_pending": health["inbox_pending"]}
                    if not health["inbox_pending"]:
                        self._model_fit_capture_fingerprint = fingerprint
                except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, EpisodeError) as exc:
                    # Run control remains intact; the durable source and explicit
                    # error survive for replay. Never report absent usage as zero.
                    self.record["model_fit_capture"] = {"status": "gap", "error": str(exc)}
                write_private_json(self.dir / "session.json", self.record)

    def set_state(self, value: str, note: Optional[str] = None) -> None:
        with self.data_lock:
            self.record["state"] = value
            if note is not None:
                self.record["note"] = note
            self.persist()

    def emit(self, level: str, kind: str, text: str) -> None:
        with self.data_lock:
            self.seq += 1
            event = {"seq": self.seq, "t": round(time.time(), 3), "level": level, "kind": kind,
                     "text": one_line(text, 400)}
            descriptor = os.open(self.dir / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            self.record["last_event"] = {"seq": self.seq, "kind": kind, "text": one_line(text, 120)}
            if level == "attention":
                self.record["last_attention_seq"] = self.seq
            self.persist()

    @property
    def state(self) -> str:
        return self.record.get("state", "")


# ---------------------------------------------------------------------------
# Broker socket server


def listen(state: Path, state_env: str) -> socket.socket:
    """Bind the broker socket (0600) after verifying the private directories."""
    os.umask(0o077)
    private_dir(state)
    private_dir(broker_dir(state))
    private_dir(sessions_dir(state))
    path = socket_path(state)
    if len(os.fsencode(str(path))) > MAX_SOCKET_PATH_BYTES:
        raise BrokerError(f"socket path is too long for AF_UNIX: {path}; set {state_env} to a shorter path")
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise BrokerError(f"{path} exists and is not a socket")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(16)
    listener.settimeout(1.0)
    return listener


def serve_loop(listener: socket.socket, stopping: threading.Event, on_idle: Callable[[], None],
               handle: Callable[[socket.socket], None]) -> None:
    """Accept connections until ``stopping`` is set; each is handled on its own thread."""
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            on_idle()
            continue
        except OSError:
            break
        threading.Thread(target=handle, args=(connection,), daemon=True).start()


def remove_own_socket(state: Path, instance: str) -> None:
    """Remove the socket and info file only if they still belong to ``instance``."""
    info = read_json(info_path(state)) or {}
    if info.get("instance") == instance:
        for leftover in (socket_path(state), info_path(state)):
            try:
                leftover.unlink()
            except OSError:
                pass


def reply(connection: socket.socket, value: dict) -> None:
    try:
        connection.sendall((json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        pass


def handle_connection(connection: socket.socket, uid_of: Callable[[socket.socket], Optional[int]],
                      dispatch: Callable[[dict], dict], on_request: Callable[[], None],
                      failure: Callable[[Exception], dict]) -> None:
    """Read one JSON request line, check the peer uid, dispatch, and answer.

    ``failure`` maps an exception raised by ``dispatch`` to the reply; the
    broker must answer, not die.
    """
    with connection:
        connection.settimeout(30)
        uid = uid_of(connection)
        if uid is not None and uid != os.getuid():
            reply(connection, {"ok": False, "code": EXIT_USAGE, "error": "peer uid refused"})
            return
        data = b""
        try:
            while not data.endswith(b"\n") and len(data) <= MAX_REQUEST_BYTES:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data)
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
        except (OSError, ValueError) as exc:
            reply(connection, {"ok": False, "code": EXIT_USAGE, "error": f"bad request: {exc}"})
            return
        on_request()
        connection.settimeout(None)
        try:
            result = dispatch(request)
        except Exception as exc:  # the broker must answer, not die
            result = failure(exc)
        on_request()
        reply(connection, result)


# ---------------------------------------------------------------------------
# Client side of the socket


_SPAWNED: list[subprocess.Popen] = []


def call(state: Path, op: str, timeout: float = 180.0, **arguments: Any) -> dict:
    path = socket_path(state)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
        connection.sendall((json.dumps({"op": op, **arguments}) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        connection.close()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("broker reply is not an object")
    return value


def probe_broker(state: Path) -> Optional[dict]:
    """A live broker whose ping matches its recorded pid and instance, or None."""
    info = read_json(info_path(state))
    if info is None:
        return None
    try:
        answer = call(state, "ping", timeout=3)
    except (OSError, ValueError):
        return None
    if answer.get("instance") != info.get("instance") or answer.get("pid") != info.get("pid") \
            or answer.get("uid") != os.getuid():
        return None
    return answer


def process_command(pid: int) -> str:
    try:
        completed = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                   check=False)
    except OSError:
        return ""
    return completed.stdout.strip()


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        completed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                                   check=False)
    except OSError:
        return True
    status = completed.stdout.strip()
    return bool(status) and not status.startswith("Z")  # an exited, unreaped child is not alive


def replace_stale_broker(state: Path, info: Optional[dict]) -> list[str]:
    """Stop a recorded broker that no longer answers, only if its command line carries its instance token."""
    notes = []
    if info and pid_alive(info.get("pid")):
        pid = info["pid"]
        if info.get("instance") and f"--instance {info['instance']}" in process_command(pid):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if pid_alive(pid) and f"--instance {info['instance']}" in process_command(pid):
                os.kill(pid, signal.SIGKILL)
            notes.append(f"stopped unresponsive broker pid {pid}")
        else:
            notes.append(f"recorded broker pid {pid} is another process; not signalled")
    path = socket_path(state)
    if path.is_symlink() or path.exists():
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise BrokerError(f"{path} exists and is not a socket")
        path.unlink()
        notes.append("removed stale socket")
    try:
        info_path(state).unlink()
    except OSError:
        pass
    return notes


def ensure_broker(state: Path, module_root: Path, environ: dict, digest: str,
                  command: Callable[[str], list[str]], start_timeout: float = 20.0) -> dict:
    """Return a verified live broker, replacing a stale one and starting a new one if needed.

    ``command(instance)`` is the argv of a new broker; it must carry
    ``--instance INSTANCE`` so a stale broker can be recognised safely.
    """
    private_dir(state)
    directory = private_dir(broker_dir(state))
    private_dir(sessions_dir(state))
    descriptor = os.open(directory / "broker.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        answer = probe_broker(state)
        notes: list[str] = []
        if answer is not None:
            if answer.get("module_root") == str(module_root) and answer.get("code_digest") == digest:
                return answer
            if answer.get("live"):
                raise BrokerError(
                    f"the running broker (pid {answer.get('pid')}) runs other code ({answer.get('module_root')}) "
                    f"and holds open sessions; stop them or run shutdown first")
            call(state, "shutdown", timeout=STOP_WAIT_SECONDS)
            deadline = time.monotonic() + 10
            while pid_alive(answer.get("pid")) and time.monotonic() < deadline:
                time.sleep(0.1)
            notes.append(f"replaced idle broker pid {answer.get('pid')} running other code")
        notes += replace_stale_broker(state, read_json(info_path(state)))
        instance = secrets.token_hex(8)
        env = dict(environ)
        env["PYTHONPATH"] = str(module_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        log = os.open(directory / "broker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            process = subprocess.Popen(
                command(instance), cwd=str(module_root), env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=log, start_new_session=True,
            )
        finally:
            os.close(log)
        _SPAWNED.append(process)  # keep the handle so a long-lived caller can reap it
        deadline = time.monotonic() + start_timeout
        while time.monotonic() < deadline:
            answer = probe_broker(state)
            if answer is not None and answer.get("instance") == instance:
                answer["notes"] = notes + ["started broker"]
                return answer
            if process.poll() is not None:
                break
            time.sleep(0.1)
        tail = ""
        try:
            tail = (directory / "broker.log").read_text(encoding="utf-8")[-600:]
        except OSError:
            pass
        raise BrokerError(f"broker did not start: {one_line(tail, 300)}")


# ---------------------------------------------------------------------------
# Bounded client commands: each returns (exit code, bounded lines, JSON record)


@dataclass
class ClientSpec:
    """What the shared client commands need to know about one capability."""

    command: str                                   # CLI name, e.g. "luna-reserve" or "muse"
    state_root: Callable[[Path, dict], Path]
    session_pattern: "re.Pattern[str]"
    session_prefix: str                            # registry directory prefix, e.g. "lr-"
    verdict_code: Callable[[dict], int]
    session_lines: Callable[[dict], list[str]]
    exit_ok: int = 0
    exit_refused: int = 10
    exit_failed: int = 11
    server_label: str = "server"                   # prefix of a server error line
    display: str = ""                              # human name, e.g. "Luna reserve"
    resume_hint: str = "resume it in a new session"
    list_header: Callable[[Path], str] = field(default=lambda state: "")
    open_states: tuple = ("starting", "idle", "running", "stopping")
    terminal_states: tuple = ("stopped", "refused", "failed", "tripped", "lost", "unclean")


def refusal_lines(spec: ClientSpec, answer: dict) -> list[str]:
    lines = [f"verdict={answer.get('verdict') or 'ERROR'} exit={answer.get('code')}"]
    if answer.get("error"):
        lines.append(f"error: {one_line(answer['error'])}")
    lines.extend(f"refused: {one_line(item)}" for item in (answer.get("refusals") or [])[:8])
    lines.extend(f"failure: {one_line(item)}" for item in (answer.get("failures") or [])[:8])
    lines.extend(f"{spec.server_label}: {one_line(item)}" for item in (answer.get("errors") or [])[:4])
    if answer.get("message"):
        lines.append(answer["message"])
    return lines


def broker_call(spec: ClientSpec, module_root: Path, environ: dict, op: str, autostart: bool,
                ensure: Callable[[Path, dict], dict], **arguments: Any) -> dict:
    state = spec.state_root(module_root, environ)
    try:
        if autostart:
            ensure(module_root, environ)
        elif probe_broker(state) is None:
            session = arguments.get("session")
            record = load_record(state, str(session), spec.session_pattern) if session else None
            detail = f"; session {session} is {record.get('state')}" if record else ""
            return {"ok": False, "code": spec.exit_failed,
                    "error": f"no {spec.display or spec.command} broker is running{detail}; {spec.resume_hint}"}
        return call(state, op, **arguments)
    except (OSError, ValueError, BrokerError) as exc:
        return {"ok": False, "code": spec.exit_failed, "error": f"broker unavailable: {exc}"}


def simple_output(spec: ClientSpec, answer: dict) -> tuple[int, list[str], dict]:
    code = int(answer.get("code", spec.exit_failed))
    if not answer.get("session"):
        return code, refusal_lines(spec, answer), answer
    head = f"session={answer['session']} state={answer.get('state')} {str(answer.get('verdict', '')).lower()}"
    if answer.get("turn"):
        head += f" turn={answer['turn']}"
    if answer.get("approval"):
        head += f" {answer['approval']}={answer.get('decision')}"
    if answer.get("detail"):
        head += f" detail={answer['detail']}"
    if answer.get("stop_audit"):
        head += f" stop_audit={answer['stop_audit']}"
    if answer.get("wind_down"):
        head += f" wind_down={answer['wind_down']}"
    lines = [head]
    if code != 0:
        lines.extend(refusal_lines(spec, answer)[1:])
    return code, lines, answer


def broker_alive_for(state: Path, record: dict) -> bool:
    answer = probe_broker(state)
    return answer is not None and answer.get("instance") == record.get("broker_instance")


def cmd_wait(spec: ClientSpec, module_root: Path, environ: dict, session: str, timeout: float,
             poll_seconds: float = 0.5) -> tuple[int, list[str], dict]:
    state = spec.state_root(module_root, environ)
    deadline = time.monotonic() + timeout
    next_liveness = 0.0
    while True:
        record = load_record(state, session, spec.session_pattern)
        if record is None:
            return EXIT_USAGE, [f"no session {session}"], {}
        current = record.get("state")
        if current in spec.terminal_states or current == "idle" or record.get("pending_approvals"):
            return spec.verdict_code(record), spec.session_lines(record), record
        now = time.monotonic()
        if now >= next_liveness:
            next_liveness = now + 5
            if not broker_alive_for(state, record):
                lines = spec.session_lines(record)
                lines.append(f"broker is gone; the session is lost; {spec.resume_hint}")
                return spec.exit_failed, lines, record
        if now > deadline:
            return EXIT_TIMEOUT, spec.session_lines(record) + [f"timeout after {timeout:g}s; still {current}"], record
        time.sleep(poll_seconds)


def cmd_events(spec: ClientSpec, module_root: Path, environ: dict, session: str, follow: bool, last: int,
               since: int, emit: Callable[[str], None], poll_seconds: float = 0.5,
               timeout: Optional[float] = None) -> int:
    state = spec.state_root(module_root, environ)
    record = load_record(state, session, spec.session_pattern)
    if record is None:
        emit(f"no session {session}")
        return EXIT_USAGE
    if not follow:
        events, _ = read_events(state, session)
        shown = [event for event in events if event.get("seq", 0) > since and visible(event, record.get("detail"))]
        for event in shown[-max(1, min(last, 200)):]:
            emit(format_event(event))
        return spec.exit_ok
    offset = 0
    deadline = time.monotonic() + timeout if timeout else None
    next_liveness = time.monotonic() + 5
    while True:
        record = load_record(state, session, spec.session_pattern) or record
        events, offset = read_events(state, session, offset)
        for event in events:
            if event.get("seq", 0) > since and visible(event, record.get("detail")):
                emit(format_event(event))
        if record.get("state") in spec.terminal_states:
            emit(f"session {session} ended: {record.get('state')}")
            return spec.verdict_code(record)
        now = time.monotonic()
        if now >= next_liveness:
            next_liveness = now + 5
            if not broker_alive_for(state, record):
                emit(f"session {session} lost: the broker is gone")
                return spec.exit_failed
        if deadline is not None and now > deadline:
            return EXIT_TIMEOUT
        time.sleep(poll_seconds)


def cmd_list(spec: ClientSpec, module_root: Path, environ: dict, limit: int,
             describe: Callable[[dict], str]) -> tuple[int, list[str], dict]:
    state = spec.state_root(module_root, environ)
    answer = probe_broker(state)
    live = set((answer or {}).get("live") or [])
    records = all_records(state, spec.session_prefix)
    shown = records[-max(1, min(limit, 50)):]
    header = f"broker={'pid ' + str(answer['pid']) if answer else 'not running'} sessions={len(records)}"
    extra = spec.list_header(state)
    lines = [header + (f" {extra}" if extra else "")]
    for record in shown:
        current = record.get("state")
        if current in spec.open_states and record.get("id") not in live:
            current = "lost"
        last = (record.get("last_event") or {}).get("text") or ""
        lines.append(one_line(f"{record.get('id')} {current} {describe(record)} "
                              f"turns={len(record.get('turns') or [])} "
                              f"pending={len(record.get('pending_approvals') or [])} "
                              f"target={record.get('target')} last={last}", 240))
    return spec.exit_ok, lines, {"broker": answer, "sessions": shown}


def cmd_detail(spec: ClientSpec, module_root: Path, environ: dict, session: str, level: str,
               ensure: Callable[[Path, dict], dict]) -> tuple[int, list[str], dict]:
    state = spec.state_root(module_root, environ)
    if level not in DETAIL_LEVELS:
        return EXIT_USAGE, [f"detail must be one of {', '.join(DETAIL_LEVELS)}"], {}
    record = load_record(state, session, spec.session_pattern)
    if record is None:
        return EXIT_USAGE, [f"no session {session}"], {}
    if record.get("state") in spec.open_states and probe_broker(state) is not None:
        answer = broker_call(spec, module_root, environ, "detail", False, ensure, session=session, level=level)
        return simple_output(spec, answer)
    record["detail"] = level
    write_private_json(sessions_dir(state) / session / "session.json", record)
    return spec.exit_ok, [f"session={session} state={record.get('state')} detail={level}"], record


def cmd_shutdown(spec: ClientSpec, module_root: Path, environ: dict) -> tuple[int, list[str], dict]:
    state = spec.state_root(module_root, environ)
    if probe_broker(state) is None:
        info = read_json(info_path(state))
        notes = replace_stale_broker(state, info) if info else []
        return spec.exit_ok, ["no broker is running"] + notes, {}
    info = read_json(info_path(state)) or {}
    answer = call(state, "shutdown", timeout=STOP_WAIT_SECONDS * 3)
    deadline = time.monotonic() + 10
    while pid_alive(info.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.1)
    return spec.exit_ok, [f"broker pid {info.get('pid')} shut down; sessions stopped={answer.get('stopped')}"], answer
