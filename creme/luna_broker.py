"""Long-lived broker for Luna reserve pseudo-subagent sessions.

The broker keeps guarded ``codex app-server`` sessions alive between one-shot
CLI calls, so a master in any client can start a task, steer it, give
follow-up orders, interrupt or stop it, answer its approval requests, and
watch a filtered event feed. It adds no guard logic of its own: every session
is prepared by ``luna_reserve.ReserveServer`` (probe, admission, isolation
proof), every request goes through ``GuardedSession`` pins, every rate-limit
update is attributed live, each turn ends with ``luna_reserve.audit_turn``,
and any failure records the shared ``ATTRIBUTION_FAILURE`` tripwire and stops
every session.

Layout under the Luna reserve state directory (all private to the user)::

    broker/broker.sock    0600 socket in a 0700 directory, peer uid checked
    broker/broker.json    pid, instance token, socket, start time
    broker/broker.log     broker stdout/stderr
    sessions/<id>/        session.json (registry record), events.jsonl,
                          transcript.jsonl, preflight.json, turns/<n>/...

Everything a successor needs is on disk: ``list`` reads the registry,
``events``, ``wait``, and ``read`` read files, and ``resume`` attaches a new
session to a recorded thread. One app-server runs per open session; each is a
child of the broker and exits when the broker's pipe closes.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import luna_lean
from . import luna_reserve as L
from .codex_app_server import APPROVAL_METHODS, AppServerError, PinViolation, decline_server_requests


DETAIL_LEVELS = ("silent", "summary", "live")
DEFAULT_DETAIL = "silent"
_EVENT_RANK = {"attention": 0, "summary": 1, "live": 2}
_DETAIL_RANK = {"silent": 0, "summary": 1, "live": 2}

# ``unclean``: a Lean session whose wind-down did not report OK when it stopped.
TERMINAL_STATES = ("stopped", "refused", "failed", "tripped", "lost", "unclean")
OPEN_STATES = ("starting", "idle", "running", "stopping")
DECISIONS = ("accept", "decline", "cancel")

BROKER_IDLE_SECONDS = 600
SESSION_IDLE_SECONDS = 1800
IDLE_ENV = "CREME_LUNA_RESERVE_BROKER_IDLE_SECONDS"
SESSION_IDLE_ENV = "CREME_LUNA_RESERVE_SESSION_IDLE_SECONDS"
SETTLE_ENV = "CREME_LUNA_RESERVE_SETTLE_SECONDS"
ROLLOUT_WAIT_ENV = "CREME_LUNA_RESERVE_ROLLOUT_WAIT_SECONDS"
MAX_LEAN_SESSIONS = 2
MAX_LEAN_SESSIONS_ENV = "CREME_LUNA_MAX_LEAN_SESSIONS"

MAX_REQUEST_BYTES = 256 * 1024
MAX_SOCKET_PATH_BYTES = 100
LINE_WIDTH = 200
STOP_WAIT_SECONDS = 60.0

EXIT_USAGE = 2
EXIT_ATTENTION = 20
EXIT_INTERRUPTED = 21
EXIT_TIMEOUT = 124

_SESSION_ID = re.compile(r"^lr-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_APPROVAL_ID = re.compile(r"^a[0-9]{1,6}$")

# ``approve-builds`` deliberately recognizes only the command shape that the
# broker's Lean build recipe emits.  The shell wrapper is part of the rule;
# accepting a tokenized or differently quoted equivalent would make the rule
# depend on a caller's shell rather than on the approval the master sees.
_BUILD_COMMAND = re.compile(r"^/bin/zsh -lc '([^']*)'$")
_MODULE_NAME = r"[A-Za-z0-9_.]+"
_DECLARATION = re.compile(
    rb"^(?:@\[[^\]\r\n]*\][ \t]*)?"
    rb"(?:(?:private|protected|noncomputable)[ \t]+)*"
    rb"(?:theorem|lemma|def|opaque|structure|class|abbrev|inductive|instance)"
    rb"[ \t]+([^\s:({]+)", re.MULTILINE,
)
_APPROVAL_SUMMARY = re.compile(r"^command `([^`]*)` in (.*?)(?: reason: .*)?$")


_SPAWNED: list[subprocess.Popen] = []


def _max_lean_sessions(environ: dict) -> int:
    """Return the bounded Lean-session cap, failing safe on bad configuration."""
    try:
        value = int(environ.get(MAX_LEAN_SESSIONS_ENV, MAX_LEAN_SESSIONS))
    except (TypeError, ValueError):
        return MAX_LEAN_SESSIONS
    return value if 1 <= value <= 4 else MAX_LEAN_SESSIONS


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


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def code_digest(module_root: Path) -> str:
    """Digest of the modules a broker runs, so a client never drives a broker on other code."""
    import hashlib

    digest = hashlib.sha256()
    for relative in ("creme/luna_broker.py", "creme/luna_reserve.py", "creme/codex_app_server.py",
                     "creme/luna_lean.py", "templates/luna-reserve/preamble.md",
                     "templates/luna-reserve/lean-preamble.md"):
        try:
            digest.update((module_root / relative).read_bytes())
        except OSError:
            digest.update(b"missing:" + relative.encode())
    return digest.hexdigest()[:16]


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


def load_record(state: Path, session_id: str) -> Optional[dict]:
    if not _SESSION_ID.match(session_id or ""):
        return None
    return read_json(sessions_dir(state) / session_id / "session.json")


def all_records(state: Path) -> list[dict]:
    root = sessions_dir(state)
    if not root.is_dir():
        return []
    records = [read_json(path) for path in sorted(root.glob("lr-*/session.json"))]
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


def _declaration_headers(source: bytes) -> dict[str, bytes]:
    """Extract conservative line-start declaration headers from UTF-8 bytes.

    This is intentionally the small, documented shape used by the session
    prototype: optional attributes/modifiers followed by a named theorem,
    lemma, definition, structure, class, abbreviation, inductive, opaque, or
    instance.  A definition ends immediately before ``:=``; a structure,
    class, or inductive ends immediately before ``where``.  Unknown syntax is
    not guessed, which keeps this check fail-closed for files it cannot read
    while avoiding a pretend Lean parser.
    """
    found = list(_DECLARATION.finditer(source))
    headers: dict[str, bytes] = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(source)
        body = source[match.start():end]
        prefix = match.group(0)
        marker: Optional[int] = None
        if re.search(rb"\b(?:structure|class|inductive)\b", prefix):
            where = re.search(rb"[ \t\r\n]+where\b", body)
            marker = where.start() if where else None
        else:
            assignment = body.find(b":=")
            marker = assignment if assignment >= 0 else None
        headers[match.group(1).decode("utf-8", errors="replace")] = body[:marker] if marker is not None else body
    return headers


def _read_header_at_ref(target: Path, ref: str, filename: str) -> tuple[Optional[bytes], Optional[str]]:
    """Read one tracked file, returning an explicit failure instead of guessing."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(target), "show", f"{ref}:{filename}"],
            capture_output=True, check=False,
        )
    except OSError as exc:
        return None, f"cannot read header file {filename!r} at {ref!r}: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return None, f"cannot read header file {filename!r} at {ref!r}" + (f": {detail}" if detail else "")
    return completed.stdout, None


def header_rule_failures(target: Path, ref: Optional[str], filenames: list[str], allowed: set[str]) -> list[str]:
    """Check tracked declaration headers, failing closed on path/read errors."""
    if ref is None:
        return []
    if not filenames:
        return ["--header-base requires at least one --header-file"]
    failures: list[str] = []
    root = target.resolve()
    for filename in filenames:
        relative = Path(filename)
        if relative.is_absolute():
            failures.append(f"header file {filename!r} is not relative to the session target")
            continue
        working = (root / relative).resolve()
        try:
            git_name = working.relative_to(root).as_posix()
        except ValueError:
            failures.append(f"header file {filename!r} is outside the session target")
            continue
        old, error = _read_header_at_ref(root, ref, git_name)
        if error is not None:
            failures.append(error)
            continue
        try:
            current = working.read_bytes()
        except OSError as exc:
            failures.append(f"cannot read working-tree header file {filename!r}: {exc}")
            continue
        old_headers = _declaration_headers(old or b"")
        current_headers = _declaration_headers(current)
        for name, header in old_headers.items():
            if name not in current_headers:
                if name not in allowed:
                    failures.append(f"header removed: {filename}:{name}")
            elif current_headers[name] != header:
                failures.append(f"header changed: {filename}:{name}")
    return failures


def _approval_command_and_cwd(approval: dict) -> tuple[Any, Any]:
    """Return retained approval fields, with compatibility for old records."""
    command, cwd = approval.get("command"), approval.get("cwd")
    if command is None or cwd is None:
        match = _APPROVAL_SUMMARY.match(str(approval.get("summary") or ""))
        if match:
            command, cwd = match.group(1), match.group(2)
    return command, cwd


def _presented_command(value: Any) -> Optional[str]:
    """Render both approval protocol command shapes as the broker presents them."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if len(value) == 3 and value[0] == "/bin/zsh" and value[1] == "-lc":
            return f"/bin/zsh -lc '{value[2]}'"
        return " ".join(str(part) for part in value)
    return None


def build_approval_failure(record: dict, approval: dict, header_base: Optional[str],
                           header_files: list[str], allowed: set[str]) -> Optional[str]:
    """Return the first failed opt-in build-approval rule, or ``None``."""
    lean = record.get("lean") or {}
    if not lean.get("goal"):
        return "session is not a Lean session"
    if record.get("state") != "running":
        return "session has no current running turn"
    method = approval.get("method")
    if method not in luna_lean.COMMAND_APPROVAL_METHODS:
        return "approval is not a command-execution request"
    command_value, cwd = _approval_command_and_cwd(approval)
    command = _presented_command(command_value)
    goal = str(lean["goal"])
    if command is None:
        return "approval has no command"
    wrapped = _BUILD_COMMAND.fullmatch(command)
    if wrapped is None:
        return "command is not the exact /bin/zsh -lc build form"
    inner = wrapped.group(1)
    # The model sometimes spells the launcher with the expanded home directory.
    launcher = f"(?:~/creme/scripts/creme|{re.escape(str(Path.home() / 'creme/scripts/creme'))})"
    pattern = re.compile(
        rf"^{launcher} lake-build {re.escape(goal)}"
        rf"(?: --wait ([0-9]+))? -- ({_MODULE_NAME}(?: {_MODULE_NAME})*)$"
    )
    parsed = pattern.fullmatch(inner)
    if parsed is None:
        return "command is not an allowed lake-build (goal, wait, or module rule failed)"
    if parsed.group(1) is not None and not 1 <= int(parsed.group(1)) <= 900:
        return "--wait must be an integer from 1 through 900"
    if cwd is None:
        return "approval has no working directory"
    try:
        if Path(str(cwd)).resolve() != Path(str(record.get("target"))).resolve():
            return "working directory is not the session target"
    except (OSError, RuntimeError):
        return "working directory cannot be resolved"
    failures = header_rule_failures(Path(str(record["target"])), header_base, header_files, allowed)
    return failures[0] if failures else None


def visible(event: dict, detail: str) -> bool:
    return _EVENT_RANK.get(event.get("level"), 2) <= _DETAIL_RANK.get(detail, 0)


def format_event(event: dict) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", 0)))
    return one_line(f"[{event.get('seq')}] {stamp} {event.get('kind')}: {event.get('text')}")


# ---------------------------------------------------------------------------
# Broker-side session


class BrokerSession:
    """One guarded app-server and thread held by the broker."""

    def __init__(self, broker: "Broker", session_id: str, record: dict) -> None:
        self.broker = broker
        self.id = session_id
        self.dir = sessions_dir(broker.state) / session_id
        self.record = record
        self.lock = threading.RLock()        # serializes operations; may be held across requests
        self.data_lock = threading.RLock()   # record, events, approvals; never held across requests
        self.turn_ended = threading.Event()
        self.turn_ended.set()
        self.server: Optional[L.ReserveServer] = None
        self.guard = None
        self.pending: dict[str, dict] = {}
        self.approval_counter = 0
        self.seq = 0
        self.closed = False
        self.guard_tripped = False
        self.interrupted_turn: Optional[str] = None
        self.alarmed = False
        self.timed_out = False
        self.turn_deadline: Optional[float] = None
        self.last_activity = time.monotonic()
        self.pump_thread: Optional[threading.Thread] = None

    # -- record and events ----------------------------------------------
    def persist(self) -> None:
        with self.data_lock:
            self.record["updated"] = _now_iso()
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

    def is_open(self) -> bool:
        return not self.closed and self.server is not None

    @property
    def state(self) -> str:
        return self.record.get("state", "")

    # -- opening --------------------------------------------------------
    def open(self, brief: Optional[str], resume_thread: Optional[str]) -> tuple[int, dict]:
        broker = self.broker
        record = self.record
        workdir = Path(record["target"])
        policy = L.Policy(**record["policy"])
        lean = luna_lean.LeanMode(record["lean"]["goal"], workdir, tracked=broker.tracked_definition) \
            if record.get("lean") else None
        server = L.ReserveServer(
            broker.module_root, broker.root, self.dir, workdir, record["mode"] == "write", record["effort"],
            policy, broker.environ, record["approval_policy"], lean=lean,
        )
        refusals = server.probe()
        if refusals:
            return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": refusals}
        private_dir(self.dir)
        record["target"] = str(server.workdir)
        self.set_state("starting")
        self.server = server
        try:
            refusals = server.open(self.on_server_request)
            record["app_server_pid"] = server.process.pid if server.process else None
            if refusals:
                return self._refuse_open(server, refusals)
            if lean is not None:
                instructions = luna_lean.developer_instructions(broker.module_root, broker.root, lean)
            else:
                instructions = L.developer_instructions(
                    broker.module_root, L.RunRequest(brief="", workdir=server.workdir, write=server.write),
                    broker.root,
                )
            (self.dir / "developer-instructions.md").write_text(instructions, encoding="utf-8")
            self.guard = server.session(instructions)
            if resume_thread:
                record["rollout_base"] = self._rollout_length(record.get("rollout"))
                self.guard.resume_thread(resume_thread, broker.settle_seconds)
            else:
                record["rollout_base"] = 0
                self.guard.start_thread(broker.settle_seconds)
            record.update({
                "thread_id": self.guard.thread_id,
                "rollout": self.guard.rollout_path,
                "instruction_sources": self.guard.instruction_sources,
            })
            if resume_thread and record["rollout_base"] == 0:
                record["rollout_base"] = self._rollout_length(self.guard.rollout_path)
            self.persist()
            if self.guard.guard_failures:
                failures = list(self.guard.guard_failures)
                self.guard_tripped = True
                broker.trip_all(self, failures)
                return L.EXIT_ATTRIBUTION_FAILED, {"verdict": "ATTRIBUTION_FAILED", "failures": failures}
            if lean is not None:
                refusals = self._verify_lean_server()
                if refusals:
                    return self._refuse_open(server, refusals)
        except PinViolation as exc:
            self.guard_tripped = True
            broker.trip_all(self, [f"pin violation: {exc}"])
            return L.EXIT_ATTRIBUTION_FAILED, {"verdict": "ATTRIBUTION_FAILED", "failures": [str(exc)]}
        except AppServerError as exc:
            self.closed = True
            server.close()
            self.lean_wind_down("open failed")
            self.set_state("failed", str(exc)[:400])
            self.emit("attention", "failed", f"codex app-server failed while opening: {exc}")
            return L.EXIT_CODEX_FAILED, {"verdict": "CODEX_FAILED", "errors": [str(exc)]}
        self.set_state("idle")
        self.emit("attention" if not brief else "live", "opened",
                  f"thread {self.guard.thread_id} {'resumed' if resume_thread else 'started'}")
        self.pump_thread = threading.Thread(target=self.pump, name=f"pump-{self.id}", daemon=True)
        self.pump_thread.start()
        if brief:
            return self.begin_turn(brief, reference=server.before)
        return L.EXIT_OK, {"verdict": "OPEN"}

    def _refuse_open(self, server: L.ReserveServer, refusals: list[str]) -> tuple[int, dict]:
        self.closed = True
        server.close()
        self.lean_wind_down("refused at open")
        self.set_state("refused", "; ".join(refusals)[:400])
        self.emit("attention", "refused", "; ".join(refusals))
        return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": refusals}

    def _verify_lean_server(self) -> list[str]:
        """Before any turn: the one kept Lean server is ready, offers no forbidden tool, and nothing else started."""
        deadline = time.monotonic() + self.broker.mcp_ready_seconds
        name = luna_lean.LEAN_MCP_SERVER
        while self.guard.mcp_startup.get(name) not in ("ready", "failed", "cancelled") \
                and time.monotonic() < deadline and not self.guard.guard_failures:
            message = self.server.process.next_notification(min(1.0, max(0.0, deadline - time.monotonic())))
            if message is not None and message.get("method") != "creme/transport/closed":
                self.guard.observe(message)
            elif message is not None:
                break
        refusals = list(self.guard.guard_failures)
        status = self.guard.mcp_startup.get(name)
        if status != "ready":
            refusals.append(f"{name} did not become ready (startup status {status!r})")
        others = sorted(set(self.guard.mcp_startup) - {name})
        if others:
            refusals.append(f"other MCP servers reported startup: {others}")
        try:
            listing = self.guard.request("mcpServerStatus/list", {"threadId": self.guard.thread_id}, timeout=30)
        except AppServerError as exc:
            listing = None
            refusals.append(f"MCP status could not be read: {exc}")
        if listing is not None:
            refusals += luna_lean.tool_failures(listing)
            entries = [entry for entry in (listing or {}).get("data") or [] if isinstance(entry, dict)]
            with self.data_lock:
                self.record["lean"]["mcp"] = {
                    "startup": dict(self.guard.mcp_startup),
                    "servers": {entry.get("name"): entry.get("runtimeStatus") for entry in entries},
                    "tools": sorted(next((entry.get("tools") or {} for entry in entries
                                          if entry.get("name") == name), {}).keys()),
                }
                self.persist()
        return refusals

    def _rollout_length(self, rollout: Optional[str]) -> int:
        if not rollout or not Path(rollout).is_file():
            return 0
        try:
            return len(L.load_rollout(Path(rollout)))
        except L.LunaReserveError:
            return 0

    # -- turns ----------------------------------------------------------
    def begin_turn(self, text: str, reference: Optional[dict] = None) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            if L.tripwire_path(self.broker.state).exists():
                return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [
                    f"attribution failure tripwire present: {L.tripwire_path(self.broker.state)}"]}
            if self.state != "idle" or not self.is_open():
                return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [
                    f"session is {self.state}; a new turn needs an idle session"]}
            refusals = L.early_refusals(self.broker.state, self.record["effort"], (), text)
            if refusals:
                return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": refusals}
            if reference is None:
                try:
                    reference = self.server.admission_read()
                except AppServerError as exc:
                    self.emit("attention", "error", f"admission read failed: {exc}")
                    return L.EXIT_CODEX_FAILED, {"verdict": "CODEX_FAILED", "errors": [str(exc)]}
                if not reference["admitted"]:
                    self.emit("attention", "refused", "admission refused: " + "; ".join(reference["refusals"]))
                    return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": reference["refusals"]}
            number = len(self.record["turns"]) + 1
            turn_dir = private_dir(self.dir / "turns" / str(number))
            (turn_dir / "brief.md").write_text(text, encoding="utf-8")
            L._write_json(turn_dir / "preflight.json", {"admission": reference})
            turn = {
                "n": number, "started": _now_iso(), "turn_id": None, "status": None, "verdict": None,
                "rollout_start": self._rollout_length(self.record.get("rollout")),
                "reference": {"reserve": reference["reserve"], "regular": reference["regular"]},
                "warnings": list(reference.get("warnings") or []),
            }
            self.guard.attribution = L.attribution_for(reference, self.record["policy"]["jitter_seconds"])
            self.timed_out = False
            try:
                outcome = self.guard.begin_turn(text.strip())
            except PinViolation as exc:
                self.guard_tripped = True
                self.broker.trip_all(self, [f"pin violation: {exc}"])
                return L.EXIT_ATTRIBUTION_FAILED, {"verdict": "ATTRIBUTION_FAILED", "failures": [str(exc)]}
            except AppServerError as exc:
                self.emit("attention", "error", f"turn/start failed: {exc}")
                return L.EXIT_CODEX_FAILED, {"verdict": "CODEX_FAILED", "errors": [str(exc)]}
            turn["turn_id"] = outcome.turn_id
            with self.data_lock:
                self.record["turns"].append(turn)
                self.record["state"] = "running"
                self.turn_ended.clear()
                self.turn_deadline = time.monotonic() + float(self.record["turn_timeout_seconds"])
                self.persist()
            self.emit("live", "turn", f"turn {number} started ({outcome.turn_id})")
            return L.EXIT_OK, {"verdict": "STARTED", "turn": number}

    def steer(self, text: str) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            if self.state != "running" or self.guard is None or not self.guard.active_turn:
                return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [
                    f"session is {self.state}; only a running turn can be steered"]}
            refusals = L.early_refusals(self.broker.state, self.record["effort"], (), text)
            if refusals:
                return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": refusals}
            number = len(self.record["turns"])
            try:
                self.guard.turn_steer(text.strip())
            except PinViolation as exc:
                return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [f"pin: {exc}"]}
            except AppServerError as exc:
                return L.EXIT_CODEX_FAILED, {"verdict": "CODEX_FAILED", "errors": [str(exc)]}
            with self.data_lock:
                steers = self.record["turns"][-1].setdefault("steers", 0) + 1
                self.record["turns"][-1]["steers"] = steers
                path = self.dir / "turns" / str(number) / f"steer-{steers}.md"
                path.write_text(text, encoding="utf-8")
                self.persist()
            self.emit("live", "steer", f"turn {number} steered: {one_line(text, 100)}")
            return L.EXIT_OK, {"verdict": "STEERED", "turn": number}

    def send(self, text: str) -> tuple[int, dict]:
        """Steer or start after one atomic state check.

        A completion can change the state to idle after a caller has observed
        running; keeping the choice under this lock makes that immediate
        follow-up a new turn instead of a stale steer refusal.
        """
        with self.lock:
            if self.state == "running" and self.guard is not None and self.guard.active_turn:
                return self.steer(text)
            return self.begin_turn(text)

    def request_interrupt(self) -> None:
        """Interrupt the active turn once; repeated requests for the same turn are not resent."""
        turn = self.guard.active_turn if self.guard is not None else None
        if not turn or self.interrupted_turn == turn:
            return
        self.interrupted_turn = turn
        self.guard.turn_interrupt()

    def interrupt(self) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            if self.state not in ("running", "stopping") or self.guard is None or not self.guard.active_turn:
                return L.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [
                    f"session is {self.state}; no active turn to interrupt"]}
            try:
                self.request_interrupt()
            except (AppServerError, PinViolation) as exc:
                return L.EXIT_CODEX_FAILED, {"verdict": "CODEX_FAILED", "errors": [str(exc)]}
            self.emit("live", "interrupt", f"interrupt requested for turn {len(self.record['turns'])}")
            return L.EXIT_OK, {"verdict": "INTERRUPT_REQUESTED"}

    def end_turn(self) -> None:
        with self.lock:
            outcome = self.guard.finish_turn()
            if outcome is None:
                return
            self.turn_deadline = None
            turn = self.record["turns"][-1]
            number = turn["n"]
            turn_dir = self.dir / "turns" / str(number)
            failures = list(outcome.guard_failures)
            errors = list(outcome.errors)
            reference = turn["reference"]
            try:
                limits, after, delta = self.server.postflight(reference)
                L._write_json(turn_dir / "postflight.json", {"limits": limits, "admission": after})
                failures.extend(delta)
                turn["reserve_after"] = after.get("reserve")
            except AppServerError as exc:
                errors.append(f"postflight read failed: {exc}")
            try:
                items = self.guard.request("thread/items/list", {"threadId": self.guard.thread_id, "limit": 200})
                L._write_json(turn_dir / "items.json", items)
            except (AppServerError, PinViolation) as exc:
                errors.append(f"transcript read failed: {exc}")
            rollout = Path(self.record["rollout"]) if self.record.get("rollout") else None
            try:
                records = L.rollout_records(rollout, self.broker.rollout_wait_seconds)
            except L.LunaReserveError as exc:
                records = None
                errors.append(str(exc))
            audit, audit_failures = L.audit_turn(
                records, turn["rollout_start"], reference, self.record["policy"]["jitter_seconds"],
                outcome.status, outcome.token_usage, self.guard.thread_id,
            )
            if audit is not None:
                L._write_json(turn_dir / "audit.json", audit)
            failures.extend(audit_failures)
            last_message = turn_dir / "last-message.md"
            if outcome.final_message is not None:
                last_message.write_text(outcome.final_message.rstrip("\n") + "\n", encoding="utf-8")
            if failures:
                verdict = "ATTRIBUTION_FAILED"
            elif self.timed_out or errors or outcome.status not in ("completed", "interrupted"):
                verdict = "CODEX_FAILED"
            elif outcome.status == "interrupted":
                verdict = "INTERRUPTED"
            else:
                verdict = "PASS"
            usage = outcome.token_usage or (audit or {}).get("token_usage") or {}
            with self.data_lock:
                turn.update({
                    "status": outcome.status, "verdict": verdict, "completed": _now_iso(),
                    "timed_out": self.timed_out, "failures": failures, "errors": errors,
                    "live_snapshots": f"{outcome.attributed_snapshots}/{outcome.snapshots}",
                    "tokens": {
                        "input": usage.get("inputTokens", usage.get("input_tokens")),
                        "cached": usage.get("cachedInputTokens", usage.get("cached_input_tokens")),
                        "output": usage.get("outputTokens", usage.get("output_tokens")),
                    },
                    "rollout_audit": (audit or {}).get("verdict"),
                    "last_message": str(last_message) if last_message.exists() else None,
                })
                self.pending.clear()
                self.record["pending_approvals"] = []
                if self.state == "running":
                    self.record["state"] = "idle"
                self.persist()
            tokens = turn["tokens"]
            self.emit("attention", "turn", (
                f"turn {number} {outcome.status} verdict={verdict} live={turn['live_snapshots']} "
                f"thread_tokens in={tokens['input']} out={tokens['output']}"
                + (f" failures={one_line('; '.join(failures + errors), 160)}" if failures or errors else "")
            ))
            if outcome.final_message is not None:
                self.emit("summary", "final", f"{last_message} ({len(outcome.final_message.splitlines())} lines): "
                          f"{one_line(outcome.final_message, 140)}")
            self.last_activity = time.monotonic()
            self.turn_ended.set()
        if failures:
            self.guard_tripped = True
            self.broker.trip_all(self, failures)

    # -- approvals ------------------------------------------------------
    def on_server_request(self, method: str, params: Any, request_id: Any) -> Optional[dict]:
        """Reader-thread handler: queue approvals for the master; never answer them here."""
        params = params if isinstance(params, dict) else {}
        elicitation = method == "mcpServer/elicitation/request" and self.record.get("lean") \
            and params.get("serverName") == luna_lean.LEAN_MCP_SERVER and params.get("mode") == "form"
        if (method in APPROVAL_METHODS and self.record.get("approval_policy") == "on-request") or elicitation:
            if self.record.get("lean") and method in luna_lean.COMMAND_APPROVAL_METHODS:
                reason = luna_lean.forbidden_command(params.get("command"))
                if reason is not None:
                    return self.refuse_approval(method, params, request_id, reason)
            with self.data_lock:
                self.approval_counter += 1
                key = f"a{self.approval_counter}"
                summary = describe_approval(method, params)
                offered = params.get("availableDecisions")
                # Only plain decisions are answerable here; amendments and session-wide
                # approvals are never offered to the master.
                available = [item for item in DECISIONS if not isinstance(offered, list) or item in offered]
                if elicitation and ((params.get("requestedSchema") or {}).get("required")):
                    # An accept would have to invent form content; only decline or cancel are offered.
                    available = ["decline", "cancel"]
                self.pending[key] = {"request_id": request_id, "method": method, "available": available}
                pending_record = {
                    "id": key, "method": method, "summary": summary, "received": _now_iso(),
                    "decisions": available,
                }
                if method in luna_lean.COMMAND_APPROVAL_METHODS:
                    # Keep the fields needed by the opt-in approver.  The
                    # summary remains the human-facing source of truth, but
                    # these avoid reparsing a bounded one-line rendering.
                    pending_record.update({"command": params.get("command"), "cwd": params.get("cwd")})
                self.record.setdefault("pending_approvals", []).append(pending_record)
                self.persist()
            self.emit("attention", "approval",
                      f"{key} [{'|'.join(available)}] {summary} -> approve {self.id} {key} DECISION")
            return None
        if method == "mcpServer/elicitation/request":
            reply = {"result": {"action": "decline", "content": None}}
        else:
            reply = decline_server_requests(method, params, request_id)
        self.emit("attention", "server-request", f"{method} answered by policy: {json.dumps(reply)[:120]}")
        return reply

    def refuse_approval(self, method: str, params: dict, request_id: Any, reason: str) -> dict:
        """Decline a command a Lean session may never run, without asking the master.

        The refusal takes the next approval id, so a successor reading the
        records sees both that it happened and where it sits among the
        approvals the master did answer.
        """
        with self.data_lock:
            self.approval_counter += 1
            key = f"a{self.approval_counter}"
            summary = describe_approval(method, params)
            self.record.setdefault("refused_approvals", []).append({
                "id": key, "method": method, "summary": summary, "reason": reason,
                "refused": _now_iso(), "by": "broker-lean-guard",
            })
            self.persist()
        self.emit("attention", "approval-refused",
                  f"{key} declined by the broker, not offered to the master: {summary} -- {reason}")
        return decline_server_requests(method, params, request_id)

    def approve(self, key: str, decision: str) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            if decision not in DECISIONS:
                return EXIT_USAGE, {"verdict": "REFUSED", "refusals": [f"decision must be one of {DECISIONS}"]}
            with self.data_lock:
                entry = self.pending.get(key)
                if entry is None:
                    return EXIT_USAGE, {"verdict": "REFUSED", "refusals": [f"no pending approval {key}"]}
                if decision not in entry.get("available", DECISIONS):
                    return EXIT_USAGE, {"verdict": "REFUSED", "refusals": [
                        f"{key} offers only {', '.join(entry.get('available') or [])}; {decision} is not offered"]}
                self.pending.pop(key)
                self.record["pending_approvals"] = [
                    item for item in self.record.get("pending_approvals") or [] if item.get("id") != key]
                self.persist()
            if entry["method"] in ("applyPatchApproval", "execCommandApproval"):
                result = {"decision": {"accept": "approved", "decline": "denied", "cancel": "abort"}[decision]}
            elif entry["method"] == "mcpServer/elicitation/request":
                result = {"action": decision, "content": {} if decision == "accept" else None}
            else:
                result = {"decision": decision}
            try:
                self.server.process.respond(entry["request_id"], {"result": result})
            except AppServerError as exc:
                return L.EXIT_CODEX_FAILED, {"verdict": "CODEX_FAILED", "errors": [str(exc)]}
            self.emit("live", "approval", f"{key} answered {decision}")
            return L.EXIT_OK, {"verdict": "ANSWERED", "approval": key, "decision": decision}

    # -- pump -----------------------------------------------------------
    def pump(self) -> None:
        process = self.server.process
        next_check = 0.0
        while not self.closed:
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 1.0
                self.periodic(now)
            message = process.next_notification(0.5)
            if message is None or self.closed:
                continue
            if message.get("method") == "creme/transport/closed":
                self.on_lost()
                return
            # Holding the operation lock means a turn/completed that races the
            # turn/start reply is observed only after the turn id is known.
            with self.lock:
                failures = self.guard.observe(message)
                self.describe(message)
                if failures:
                    self.on_guard_failure(failures)
                if message.get("method") == "turn/completed":
                    turn = (message.get("params") or {}).get("turn") or {}
                    outcome = self.guard.outcome
                    if outcome is not None and turn.get("id") == outcome.turn_id:
                        self.end_turn()

    def periodic(self, now: float) -> None:
        broker = self.broker
        if L.tripwire_path(broker.state).exists() and not broker.tripped:
            broker.trip_all(None, ["an attribution failure tripwire was recorded by another run"])
            return
        if self.state == "running" and self.turn_deadline is not None and now > self.turn_deadline \
                and not self.timed_out:
            self.timed_out = True
            self.emit("attention", "timeout", "turn timed out; interrupting")
            try:
                self.request_interrupt()
            except (AppServerError, PinViolation) as exc:
                self.emit("attention", "error", f"interrupt failed: {exc}")
        if self.state == "idle" and not self.pending and now - self.last_activity > broker.session_idle_seconds:
            threading.Thread(target=self.stop, kwargs={"reason": "idle"}, daemon=True).start()
            self.last_activity = now

    def describe(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        kind = item.get("type")
        if method == "item/started" and kind == "commandExecution":
            self.emit("live", "command", f"start: {one_line(item.get('command'), 150)}")
        elif method == "item/completed" and kind == "commandExecution":
            self.emit("live", "command",
                      f"exit={item.get('exitCode')} {item.get('status')}: {one_line(item.get('command'), 100)}")
        elif method == "item/completed" and kind == "fileChange":
            paths = [change.get("path") for change in item.get("changes") or [] if isinstance(change, dict)]
            self.emit("live", "file-change", f"{item.get('status')}: {', '.join(str(p) for p in paths)}")
        elif method == "item/completed" and kind == "agentMessage":
            self.emit("live", "message", f"({item.get('phase')}) {one_line(item.get('text'), 150)}")
        elif method == "error":
            self.emit("live", "codex-error", json.dumps(params)[:200])

    def on_guard_failure(self, failures: list[str]) -> None:
        self.guard_tripped = True
        try:
            self.request_interrupt()
        except (AppServerError, PinViolation):
            pass
        self.broker.trip_all(self, failures)

    def on_lost(self) -> None:
        if self.closed:
            return
        if self.state == "running":
            failures = ["the server failed during a turn; attribution cannot be shown"]
            with self.data_lock:
                turn = self.record["turns"][-1]
                turn.update({"status": "lost", "verdict": "ATTRIBUTION_FAILED", "failures": failures})
                self.persist()
            self.guard.finish_turn()
            self.turn_ended.set()
            self.guard_tripped = True
            self.closed = True
            self.server.close()
            self.lean_wind_down("app-server lost")
            # The tripwire is recorded before the terminal state is visible, so
            # a reader that sees "tripped" also sees the tripwire.
            self.broker.trip_all(self, failures)
            self.set_state("tripped", failures[0])
        else:
            self.closed = True
            self.server.close()
            self.lean_wind_down("app-server lost")
            self.set_state("failed", "codex app-server exited")
            self.emit("attention", "failed", "codex app-server exited; resume the thread in a new session")

    # -- stopping -------------------------------------------------------
    def stop(self, reason: str = "requested") -> tuple[int, dict]:
        with self.lock:
            if self.closed or self.state in TERMINAL_STATES or self.server is None:
                return L.EXIT_OK, {"verdict": "ALREADY_CLOSED", "state": self.state}
            self.set_state("stopping", f"stop: {reason}")
            with self.data_lock:
                pending = list(self.pending.items())
                self.pending.clear()
                self.record["pending_approvals"] = []
                self.persist()
            for key, entry in pending:
                cancel = {"action": "cancel", "content": None} \
                    if entry["method"] == "mcpServer/elicitation/request" else {"decision": "cancel"}
                try:
                    self.server.process.respond(entry["request_id"], {"result": cancel})
                except AppServerError:
                    pass
                self.emit("live", "approval", f"{key} cancelled by stop")
            active = self.guard is not None and self.guard.active_turn
            if active:
                try:
                    self.request_interrupt()
                except (AppServerError, PinViolation):
                    pass
        if active and threading.current_thread() is not self.pump_thread:
            self.turn_ended.wait(STOP_WAIT_SECONDS)
        with self.lock:
            if self.guard is not None and self.guard.outcome is not None:
                with self.data_lock:
                    self.record["turns"][-1].update({"status": "abandoned", "verdict": "CODEX_FAILED",
                                                     "errors": ["no completion after interrupt at stop"]})
                self.guard.finish_turn()
            stop_audit = self.stop_audit()
            self.closed = True
            self.server.close()
            if self.pump_thread is not None and threading.current_thread() is not self.pump_thread:
                self.pump_thread.join(5)
            # Lean mode: the app-server (and the MCP tree under it) is closed first,
            # then the goal-scoped wind-down runs and its verdict is recorded.
            wind_down = self.lean_wind_down(f"stop: {reason}")
            failed = stop_audit["verdict"] != "PASS"
            unclean = wind_down is not None and wind_down.get("verdict") != "OK"
            final = "tripped" if (self.guard_tripped or failed) else "unclean" if unclean else "stopped"
            if failed:
                # Recorded while still "stopping", before the terminal state.
                self.guard_tripped = True
                self.broker.trip_all(self, stop_audit["failures"])
            with self.data_lock:
                self.record["stop_audit"] = {"verdict": stop_audit["verdict"],
                                             "failures": stop_audit["failures"][:10]}
                self.record["state"] = final
                self.record["note"] = f"stop: {reason}"
                self.persist()
            self.emit("attention", "stopped", f"{final} ({reason}) stop_audit={stop_audit['verdict']}"
                      + (f" wind_down={wind_down.get('verdict')}" if wind_down is not None else ""))
        code = L.EXIT_ATTRIBUTION_FAILED if final == "tripped" else L.EXIT_CODEX_FAILED if final == "unclean" \
            else L.EXIT_OK
        result = {"verdict": final.upper(), "state": final, "stop_audit": stop_audit["verdict"]}
        if wind_down is not None:
            result["wind_down"] = wind_down.get("verdict")
        return code, result

    def lean_wind_down(self, reason: str) -> Optional[dict]:
        """Run and record the goal-scoped wind-down of a Lean session; ``None`` outside Lean mode."""
        lean = self.record.get("lean")
        if not lean:
            return None
        try:
            result = self.broker.wind_down_function(lean["goal"], Path(self.record["target"]))
        except Exception as exc:  # a wind-down that cannot run is not OK
            result = {"verdict": "NOT_OK", "status": "ERROR", "detail": f"wind-down raised: {exc!r}", "residual": []}
        entry = {
            "verdict": result.get("verdict"), "status": result.get("status"),
            "detail": one_line(result.get("detail") or "", 300), "residual": (result.get("residual") or [])[:10],
            "reason": reason, "at": _now_iso(), "exit": result.get("exit"),
        }
        with self.data_lock:
            lean["wind_down"] = entry
            if self.dir.is_dir():
                L._write_json(self.dir / "wind-down.json", result)
            self.persist()
        self.emit("attention", "wind-down",
                  f"goal {lean['goal']} wind_down={entry['verdict']} status={entry['status']} "
                  f"residual={len(entry['residual'])} ({reason})")
        return entry

    def stop_audit(self) -> dict:
        """Re-audit every turn's slice of the rollout against its own reference read."""
        turns = [turn for turn in self.record["turns"] if turn.get("reference")]
        result = {"verdict": "PASS", "failures": [], "turns": []}
        if not turns:
            L._write_json(self.dir / "stop-audit.json", result)
            return result
        rollout = Path(self.record["rollout"]) if self.record.get("rollout") else None
        try:
            records = L.rollout_records(rollout, self.broker.rollout_wait_seconds)
        except L.LunaReserveError as exc:
            records = None
            result["failures"].append(str(exc))
        for index, turn in enumerate(turns):
            start = self.record.get("rollout_base", 0) if index == 0 else turn["rollout_start"]
            end = turns[index + 1]["rollout_start"] if index + 1 < len(turns) else None
            sliced = None if records is None else records[:end] if end is not None else records
            usage = (turn.get("tokens") or {}).get("input")
            audit, failures = L.audit_turn(
                sliced, start, turn["reference"], self.record["policy"]["jitter_seconds"],
                turn.get("status"), usage, self.record.get("thread_id"),
            )
            result["turns"].append({"n": turn["n"], "verdict": (audit or {}).get("verdict"), "failures": failures})
            result["failures"].extend(f"turn {turn['n']}: {failure}" for failure in failures)
        result["verdict"] = "PASS" if not result["failures"] else "FAIL"
        L._write_json(self.dir / "stop-audit.json", result)
        return result


def describe_approval(method: str, params: dict) -> str:
    if method == "mcpServer/elicitation/request":
        text = f"MCP {params.get('serverName')} asks: {params.get('message')}"
    elif method == "item/commandExecution/requestApproval":
        text = f"command `{params.get('command')}` in {params.get('cwd')}"
    elif method == "item/fileChange/requestApproval":
        text = f"file change {params.get('itemId')} grantRoot={params.get('grantRoot')}"
    elif method == "execCommandApproval":
        text = f"command `{' '.join(str(part) for part in params.get('command') or [])}` in {params.get('cwd')}"
    else:
        text = f"patch touching {', '.join(sorted((params.get('fileChanges') or {}).keys()))}"
    if params.get("reason"):
        text += f" reason: {params.get('reason')}"
    return one_line(text, 300)


# ---------------------------------------------------------------------------
# Broker process


class Broker:
    def __init__(self, module_root: Path, state: Path, instance: str, environ: dict,
                 idle_seconds: float = BROKER_IDLE_SECONDS, session_idle_seconds: float = SESSION_IDLE_SECONDS,
                 settle_seconds: float = 2.0, rollout_wait_seconds: float = 10.0,
                 peer_uid_function: Callable[[socket.socket], Optional[int]] = peer_uid) -> None:
        self.module_root = module_root
        self.state = state
        self.instance = instance
        self.environ = dict(environ)
        self.root = L.launch_root(module_root).resolve()
        self.idle_seconds = idle_seconds
        self.session_idle_seconds = session_idle_seconds
        self.settle_seconds = settle_seconds
        self.rollout_wait_seconds = rollout_wait_seconds
        self.peer_uid_function = peer_uid_function
        self.sessions: dict[str, BrokerSession] = {}
        self.lock = threading.RLock()
        self.tripped = False
        self.stopping = threading.Event()
        self.last_request = time.monotonic()
        self.listener: Optional[socket.socket] = None
        self.code = code_digest(module_root)
        # Lean-mode host hooks; tests replace them with fakes.
        self.lean_repositories: Callable[[], tuple[Path, ...]] = \
            lambda: luna_lean.repositories(L.launch_root(self.module_root))
        self.host_probe: Callable[[str], tuple[list[str], dict]] = luna_lean.host_observation
        self.residual_scan: Callable[[Path], list] = luna_lean.residual_processes
        self.wind_down_function: Callable[[str, Path], dict] = lambda goal, target: luna_lean.run_wind_down(
            self.module_root, self.environ, goal, target, self.residual_scan)
        self.tracked_definition: Callable[[Path], dict] = luna_lean.tracked_definition
        self.mcp_ready_seconds = luna_lean.MCP_READY_SECONDS
        self.reconciling: set[str] = set()

    # -- lifecycle ------------------------------------------------------
    def serve(self) -> int:
        os.umask(0o077)
        private_dir(self.state)
        private_dir(broker_dir(self.state))
        private_dir(sessions_dir(self.state))
        path = socket_path(self.state)
        if len(os.fsencode(str(path))) > MAX_SOCKET_PATH_BYTES:
            raise BrokerError(f"socket path is too long for AF_UNIX: {path}; set {L.STATE_ENV} to a shorter path")
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise BrokerError(f"{path} exists and is not a socket")
            path.unlink()
        self.reconcile_registry()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(16)
        listener.settimeout(1.0)
        self.listener = listener
        write_private_json(info_path(self.state), {
            "pid": os.getpid(), "instance": self.instance, "socket": str(path), "started": _now_iso(),
            "uid": os.getuid(), "module_root": str(self.module_root), "code": self.code,
        })
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        try:
            while not self.stopping.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    self.check_idle()
                    continue
                except OSError:
                    break
                threading.Thread(target=self.handle, args=(connection,), daemon=True).start()
        finally:
            self.stop_all("broker shutdown")
            listener.close()
            info = read_json(info_path(self.state)) or {}
            if info.get("instance") == self.instance:
                for leftover in (path, info_path(self.state)):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
        return 0

    def check_idle(self) -> None:
        with self.lock:
            live = [session for session in self.sessions.values() if session.is_open()]
        if not live and time.monotonic() - self.last_request > self.idle_seconds:
            self.stopping.set()

    def reconcile_registry(self) -> None:
        """Mark sessions of a dead broker lost, and stop their orphaned app-servers."""
        pending = []
        for record in all_records(self.state):
            if record.get("state") not in OPEN_STATES or record.get("broker_instance") == self.instance:
                continue
            reaped = reap_orphan_app_server(record.get("app_server_pid"))
            record["state"] = "lost"
            record["note"] = "broker exited while the session was open" + ("; orphaned app-server stopped" if reaped else "")
            if record.get("lean"):
                record["lean"]["wind_down"] = {"verdict": "PENDING", "reason": "crash recovery", "at": _now_iso()}
                pending.append(record)
                self.reconciling.add(record["lean"]["goal"])
            write_private_json(sessions_dir(self.state) / record["id"] / "session.json", record)
        if pending:
            # A wind-down can take longer than a client waits for the broker to
            # answer, so it runs beside the listener; a Lean session for the same
            # goal is refused until it has finished.
            threading.Thread(target=self._reconcile_lean, args=(pending,), daemon=True).start()

    def _reconcile_lean(self, records: list[dict]) -> None:
        for record in records:
            goal = record["lean"]["goal"]
            try:
                result = self.wind_down_function(goal, Path(record["target"]))
            except Exception as exc:
                result = {"verdict": "NOT_OK", "status": "ERROR", "detail": f"wind-down raised: {exc!r}"}
            record["lean"]["wind_down"] = {
                "verdict": result.get("verdict"), "status": result.get("status"),
                "detail": one_line(result.get("detail") or "", 300), "residual": (result.get("residual") or [])[:10],
                "reason": "crash recovery", "at": _now_iso(), "exit": result.get("exit"),
            }
            write_private_json(sessions_dir(self.state) / record["id"] / "session.json", record)
            with self.lock:
                self.reconciling.discard(goal)

    def stop_all(self, reason: str) -> None:
        with self.lock:
            sessions = [session for session in self.sessions.values() if session.is_open()]
        threads = [threading.Thread(target=session.stop, kwargs={"reason": reason}, daemon=True)
                   for session in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(STOP_WAIT_SECONDS + 15)

    def trip_all(self, offender: Optional[BrokerSession], failures: list[str]) -> None:
        with self.lock:
            first = not self.tripped
            self.tripped = True
            sessions = [session for session in self.sessions.values() if session.is_open()]
        if offender is not None and not offender.alarmed:
            offender.alarmed = True
            with offender.data_lock:
                if offender.record.get("turns") and offender.guard is not None and offender.guard.outcome is not None:
                    offender.record["turns"][-1].setdefault("guard_failures", []).extend(failures)
                offender.persist()
            offender.emit("attention", "billing-alarm", f"{L.STOP_MESSAGE} Failures: {'; '.join(failures)}")
            L.record_tripwire(self.state, offender.id, offender.record.get("thread_id"), failures)
        if not first:
            return
        for session in sessions:
            if session is not offender:
                session.emit("attention", "billing-alarm", "stopping: the attribution failure tripwire is recorded")
            threading.Thread(target=session.stop, kwargs={"reason": "tripwire"}, daemon=True).start()
        # An offender this call is stopping, or one already inside its own
        # stop, reaches "tripped" only when that stop has recorded its
        # wind-down; marking it here would publish a terminal state first.
        if offender is not None and offender not in sessions \
                and offender.state not in TERMINAL_STATES + ("stopping",):
            offender.set_state("tripped")

    # -- requests -------------------------------------------------------
    def handle(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(30)
            uid = self.peer_uid_function(connection)
            if uid is not None and uid != os.getuid():
                self._reply(connection, {"ok": False, "code": EXIT_USAGE, "error": "peer uid refused"})
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
                self._reply(connection, {"ok": False, "code": EXIT_USAGE, "error": f"bad request: {exc}"})
                return
            self.last_request = time.monotonic()
            connection.settimeout(None)
            try:
                reply = self.dispatch(request)
            except (BrokerError, L.LunaReserveError) as exc:
                reply = {"ok": False, "code": L.EXIT_PREFLIGHT_REFUSED, "error": str(exc)}
            except Exception as exc:  # the broker must answer, not die
                reply = {"ok": False, "code": L.EXIT_CODEX_FAILED, "error": f"broker error: {exc!r}"}
            self.last_request = time.monotonic()
            self._reply(connection, reply)

    @staticmethod
    def _reply(connection: socket.socket, reply: dict) -> None:
        try:
            connection.sendall((json.dumps(reply, sort_keys=True) + "\n").encode("utf-8"))
        except OSError:
            pass

    def session(self, session_id: Any) -> BrokerSession:
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            record = load_record(self.state, str(session_id))
            if record is None:
                raise BrokerError(f"no session {session_id}")
            raise BrokerError(f"session {session_id} is {record.get('state')} and not held by this broker; "
                              f"resume thread {record.get('thread_id')} in a new session")
        return session

    def dispatch(self, request: dict) -> dict:
        op = request.get("op")
        if op == "ping":
            with self.lock:
                live = sorted(sid for sid, session in self.sessions.items() if session.is_open())
            return {"ok": True, "code": 0, "instance": self.instance, "pid": os.getpid(), "uid": os.getuid(),
                    "live": live, "tripped": self.tripped, "module_root": str(self.module_root),
                    "code_digest": self.code}
        if op in ("start", "resume"):
            return self.open_session(request, op)
        if op == "shutdown":
            with self.lock:
                count = sum(1 for session in self.sessions.values() if session.is_open())
            self.stop_all("shutdown")
            self.stopping.set()
            return {"ok": True, "code": 0, "stopped": count}
        session = self.session(request.get("session"))
        if op == "send":
            text = str(request.get("text") or "")
            if request.get("steer"):
                code, result = session.steer(text)
            else:
                code, result = session.send(text)
        elif op == "interrupt":
            code, result = session.interrupt()
        elif op == "approve":
            code, result = session.approve(str(request.get("approval")), str(request.get("decision")))
        elif op == "detail":
            level = request.get("level")
            if level not in DETAIL_LEVELS:
                return {"ok": False, "code": EXIT_USAGE, "error": f"detail must be one of {DETAIL_LEVELS}"}
            with session.data_lock:
                session.record["detail"] = level
                session.persist()
            code, result = 0, {"verdict": "UPDATED", "detail": level}
        elif op == "items":
            limit = max(1, min(int(request.get("limit") or 10), 50))
            if not session.is_open():
                raise BrokerError(f"session {session.id} is {session.state}")
            items = session.guard.request(
                "thread/items/list", {"threadId": session.guard.thread_id, "limit": limit}, timeout=30)
            L._write_json(session.dir / "items-latest.json", items)
            code, result = 0, {"verdict": "ITEMS", "path": str(session.dir / "items-latest.json")}
        elif op == "stop":
            code, result = session.stop("requested")
        else:
            return {"ok": False, "code": EXIT_USAGE, "error": f"unknown op {op!r}"}
        return {"ok": code == 0, "code": code, "session": session.id, "state": session.state, **result}

    def open_session(self, request: dict, op: str) -> dict:
        overrides = list(request.get("overrides") or [])
        effort = request.get("effort") or L.DEFAULT_EFFORT
        detail = request.get("detail") or DEFAULT_DETAIL
        if detail not in DETAIL_LEVELS:
            return {"ok": False, "code": EXIT_USAGE, "error": f"detail must be one of {DETAIL_LEVELS}"}
        brief = request.get("brief") if op == "start" else None
        refusals = L.early_refusals(self.state, effort, overrides, brief if op == "start" else None)
        if op == "start" and brief is None:
            refusals.append("start needs a brief")
        resume_thread = None
        prior: Optional[dict] = None
        target = request.get("target")
        write = bool(request.get("write"))
        lean_goal = request.get("lean")
        if op == "resume":
            resume_thread = str(request.get("thread") or "")
            if not L._THREAD_ID.match(resume_thread):
                return {"ok": False, "code": EXIT_USAGE, "error": f"not a thread id: {resume_thread!r}"}
            priors = [record for record in all_records(self.state) if record.get("thread_id") == resume_thread]
            prior = priors[-1] if priors else None
            with self.lock:
                if any(session.is_open() and session.record.get("thread_id") == resume_thread
                       for session in self.sessions.values()):
                    refusals.append(f"thread {resume_thread} is already open in this broker")
            if target is None and prior is not None:
                target, write = prior.get("target"), prior.get("mode") == "write"
                effort = request.get("effort") or prior.get("effort") or effort
                if lean_goal is None and prior.get("lean"):
                    lean_goal = prior["lean"].get("goal")
            if target is None:
                refusals.append("no session record names this thread; pass --target (and --write if needed)")
        if lean_goal is not None:
            write = True
            if target is not None:
                try:
                    refusals += luna_lean.target_refusals(str(lean_goal), Path(str(target)), self.lean_repositories())
                except luna_lean.LeanModeError as exc:
                    refusals.append(str(exc))
        if refusals:
            return {"ok": False, "code": L.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED", "refusals": refusals}
        policy = dict(request.get("policy") or {})
        allowed_policy = set(L.Policy().__dict__)
        policy = {key: value for key, value in policy.items() if key in allowed_policy}
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        session_id = f"lr-{stamp}-{secrets.token_hex(3)}"
        record = {
            "id": session_id, "created": _now_iso(), "state": "starting", "thread_id": None, "rollout": None,
            "target": str(Path(str(target)).expanduser().resolve()), "mode": "write" if write else "read-only",
            "effort": effort, "detail": detail, "approval_policy": L.approval_policy_for(write, broker=True),
            "policy": {**L.Policy().__dict__, **policy}, "broker_instance": self.instance,
            "turns": [], "pending_approvals": [], "refused_approvals": [],
            "last_event": None, "last_attention_seq": 0,
            "resumed_from": (prior or {}).get("id"), "turn_timeout_seconds": int(
                request.get("turn_timeout_seconds") or L.DEFAULT_TIMEOUT_SECONDS),
            "launch_root": str(self.root), "model": L.RESERVE_MODEL,
        }
        if lean_goal is not None:
            record["lean"] = {"goal": str(lean_goal), "mcp_server": luna_lean.LEAN_MCP_SERVER,
                              "disabled_tools": list(luna_lean.OPEN_WORLD_TOOLS)}
        session = BrokerSession(self, session_id, record)
        if op == "resume":
            record["rollout"] = (prior or {}).get("rollout")
            orphan_failures = self.audit_orphans(resume_thread, session_id)
            if orphan_failures:
                L.record_tripwire(self.state, session_id, resume_thread, orphan_failures)
                return {"ok": False, "code": L.EXIT_ATTRIBUTION_FAILED, "verdict": "ATTRIBUTION_FAILED",
                        "failures": orphan_failures, "message": L.STOP_MESSAGE}
        with self.lock:
            if lean_goal is not None:
                refusals = self.lean_admission(str(lean_goal), Path(record["target"]), record)
                if refusals:
                    return {"ok": False, "code": L.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED",
                            "refusals": refusals}
            # Registered under the same lock as the Lean check, so concurrent
            # starts cannot both pass the Lean-session cap.
            self.sessions[session_id] = session
        code, result = session.open(brief, resume_thread)
        if code == L.EXIT_PREFLIGHT_REFUSED and not session.dir.is_dir():
            with self.lock:
                self.sessions.pop(session_id, None)
            return {"ok": False, "code": code, **result}
        return {"ok": code == 0, "code": code, "session": session_id, "state": session.state,
                "thread": record.get("thread_id"), "mode": record["mode"], "effort": effort, "detail": detail,
                "resumed_from": record.get("resumed_from"), "lean": (record.get("lean") or {}).get("goal"),
                **result}

    def lean_admission(self, goal: str, target: Path, record: dict) -> list[str]:
        """Called with the broker lock held: the Lean cap and goal rule, host admits, target is quiet."""
        live = [session.id for session in self.sessions.values()
                if session.record.get("lean") and session.state not in TERMINAL_STATES]
        limit = _max_lean_sessions(self.environ)
        refusals: list[str] = []
        if len(live) >= limit:
            refusals.append(f"already {len(live)} Lean sessions are live in this broker ({', '.join(live)}); "
                            f"at most {limit} Lean sessions may be live")
        same_goal = [session.id for session in self.sessions.values()
                     if (session.record.get("lean") or {}).get("goal") == goal
                     and session.state not in TERMINAL_STATES]
        if same_goal:
            refusals.append(f"a Lean session for the same goal label {goal} is already live in this broker "
                            f"({', '.join(same_goal)}); goal worktree wind-down scopes overlap")
        if refusals:
            return refusals
        if self.reconciling:
            return [f"crash-recovery wind-down is still running for {sorted(self.reconciling)}"]
        refusals, observed = self.host_probe(goal)
        residual = self.residual_scan(target)
        if residual:
            refusals.append(f"{len(residual)} Lean/Lake process(es) already run in {target}; wind down first")
        record["lean"]["host_admission"] = {"observed": observed, "refusals": refusals,
                                            "residual": residual[:10], "at": _now_iso()}
        return refusals

    def audit_orphans(self, thread_id: str, new_session: str) -> list[str]:
        """Audit turns of this thread that ended with their broker, before any new work on it."""
        failures: list[str] = []
        for record in all_records(self.state):
            if record.get("thread_id") != thread_id or record.get("state") in OPEN_STATES:
                continue
            turns = [turn for turn in record.get("turns") or [] if turn.get("reference")]
            orphaned = [index for index, turn in enumerate(turns) if turn.get("verdict") is None]
            if not orphaned:
                continue
            rollout = Path(record["rollout"]) if record.get("rollout") else None
            try:
                records = L.rollout_records(rollout, 0)
            except L.LunaReserveError as exc:
                records = None
                failures.append(str(exc))
            for index in orphaned:
                turn = turns[index]
                end = turns[index + 1]["rollout_start"] if index + 1 < len(turns) else None
                sliced = None if records is None else (records[:end] if end is not None else records)
                audit, turn_failures = L.audit_turn(
                    sliced, turn["rollout_start"], turn["reference"], record["policy"]["jitter_seconds"],
                    None, None, thread_id,
                )
                turn.update({"verdict": "AUDITED_AFTER_LOSS" if not turn_failures else "ATTRIBUTION_FAILED",
                             "status": turn.get("status") or "lost", "failures": turn_failures,
                             "rollout_audit": (audit or {}).get("verdict")})
                failures.extend(f"{record['id']} turn {turn['n']}: {failure}" for failure in turn_failures)
            record["note"] = f"orphaned turns audited before resume as {new_session}"
            write_private_json(sessions_dir(self.state) / record["id"] / "session.json", record)
        return failures


def reap_orphan_app_server(pid: Any) -> bool:
    """SIGTERM a recorded app-server only if it is orphaned and carries this capability's pins."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        completed = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                                   capture_output=True, text=True, check=False)
    except OSError:
        return False
    line = completed.stdout.strip()
    if not line:
        return False
    parent, _, command = line.partition(" ")
    command = command.strip()
    if parent.strip() != "1" or "app-server" not in command:
        return False
    if f'review_model="{L.RESERVE_MODEL}"' not in command or 'approvals_reviewer="user"' not in command:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Client


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
    reply = json.loads(data)
    if not isinstance(reply, dict):
        raise ValueError("broker reply is not an object")
    return reply


def probe_broker(state: Path) -> Optional[dict]:
    """A live broker whose ping matches its recorded pid and instance, or None."""
    info = read_json(info_path(state))
    if info is None:
        return None
    try:
        reply = call(state, "ping", timeout=3)
    except (OSError, ValueError):
        return None
    if reply.get("instance") != info.get("instance") or reply.get("pid") != info.get("pid") \
            or reply.get("uid") != os.getuid():
        return None
    return reply


def _process_command(pid: int) -> str:
    try:
        completed = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                   check=False)
    except OSError:
        return ""
    return completed.stdout.strip()


def _pid_alive(pid: Any) -> bool:
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
    if info and _pid_alive(info.get("pid")):
        pid = info["pid"]
        if info.get("instance") and f"--instance {info['instance']}" in _process_command(pid):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _pid_alive(pid) and f"--instance {info['instance']}" in _process_command(pid):
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


def ensure_broker(module_root: Path, environ: dict, start_timeout: float = 20.0) -> dict:
    """Return a verified live broker, replacing a stale one and starting a new one if needed."""
    state = L.state_root(module_root, environ)
    private_dir(state)
    directory = private_dir(broker_dir(state))
    private_dir(sessions_dir(state))
    descriptor = os.open(directory / "broker.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reply = probe_broker(state)
        notes: list[str] = []
        if reply is not None:
            if reply.get("module_root") == str(module_root) and reply.get("code_digest") == code_digest(module_root):
                return reply
            if reply.get("live"):
                raise BrokerError(
                    f"the running broker (pid {reply.get('pid')}) runs other code ({reply.get('module_root')}) and "
                    f"holds open sessions; stop them or run shutdown first")
            call(state, "shutdown", timeout=STOP_WAIT_SECONDS)
            deadline = time.monotonic() + 10
            while _pid_alive(reply.get("pid")) and time.monotonic() < deadline:
                time.sleep(0.1)
            notes.append(f"replaced idle broker pid {reply.get('pid')} running other code")
        notes += replace_stale_broker(state, read_json(info_path(state)))
        instance = secrets.token_hex(8)
        env = dict(environ)
        env["PYTHONPATH"] = str(module_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        log = os.open(directory / "broker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "creme", "luna-reserve", "broker-serve", "--instance", instance],
                cwd=str(module_root), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True,
            )
        finally:
            os.close(log)
        _SPAWNED.append(process)  # keep the handle so a long-lived caller can reap it
        deadline = time.monotonic() + start_timeout
        while time.monotonic() < deadline:
            reply = probe_broker(state)
            if reply is not None and reply.get("instance") == instance:
                reply["notes"] = notes + ["started broker"]
                return reply
            if process.poll() is not None:
                break
            time.sleep(0.1)
        tail = ""
        try:
            tail = (directory / "broker.log").read_text(encoding="utf-8")[-600:]
        except OSError:
            pass
        raise BrokerError(f"broker did not start: {one_line(tail, 300)}")


def serve_main(module_root: Path, instance: str, environ: Optional[dict] = None) -> int:
    environ = dict(os.environ if environ is None else environ)

    def number(name: str, default: float) -> float:
        try:
            return float(environ.get(name, default))
        except ValueError:
            return default

    broker = Broker(
        module_root, L.state_root(module_root, environ), instance, environ,
        idle_seconds=number(IDLE_ENV, BROKER_IDLE_SECONDS),
        session_idle_seconds=number(SESSION_IDLE_ENV, SESSION_IDLE_SECONDS),
        settle_seconds=number(SETTLE_ENV, 2.0),
        rollout_wait_seconds=number(ROLLOUT_WAIT_ENV, 10.0),
    )
    return broker.serve()


# ---------------------------------------------------------------------------
# Client commands: each returns (exit code, bounded lines, JSON record)


def _refusal_lines(reply: dict) -> list[str]:
    lines = [f"verdict={reply.get('verdict') or 'ERROR'} exit={reply.get('code')}"]
    if reply.get("error"):
        lines.append(f"error: {one_line(reply['error'])}")
    lines.extend(f"refused: {one_line(item)}" for item in (reply.get("refusals") or [])[:8])
    lines.extend(f"failure: {one_line(item)}" for item in (reply.get("failures") or [])[:8])
    lines.extend(f"codex: {one_line(item)}" for item in (reply.get("errors") or [])[:4])
    if reply.get("message"):
        lines.append(reply["message"])
    return lines


def _broker_call(module_root: Path, environ: dict, op: str, autostart: bool, **arguments: Any) -> dict:
    state = L.state_root(module_root, environ)
    try:
        if autostart:
            ensure_broker(module_root, environ)
        elif probe_broker(state) is None:
            session = arguments.get("session")
            record = load_record(state, str(session)) if session else None
            detail = f"; session {session} is {record.get('state')}" if record else ""
            return {"ok": False, "code": L.EXIT_CODEX_FAILED,
                    "error": f"no Luna reserve broker is running{detail}; resume its thread in a new session"}
        return call(state, op, **arguments)
    except (OSError, ValueError, BrokerError) as exc:
        return {"ok": False, "code": L.EXIT_CODEX_FAILED, "error": f"broker unavailable: {exc}"}


def _lean_client_refusals(module_root: Path, lean: Optional[str], target: Optional[str]) -> list[str]:
    """Zero-process target check, repeated authoritatively by the broker."""
    if lean is None or target is None:
        return []
    try:
        return luna_lean.target_refusals(lean, Path(target), luna_lean.repositories(L.launch_root(module_root)))
    except luna_lean.LeanModeError as exc:
        return [str(exc)]


def cmd_start(module_root: Path, environ: dict, brief: str, target: str, write: bool, effort: str,
              detail: str, policy: dict, overrides: list[str], turn_timeout_seconds: int,
              lean: Optional[str] = None) -> tuple[int, list[str], dict]:
    state = L.state_root(module_root, environ)
    refusals = L.early_refusals(state, effort, overrides, brief) or _lean_client_refusals(module_root, lean, target)
    if refusals:
        reply = {"ok": False, "code": L.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED", "refusals": refusals}
        return reply["code"], _refusal_lines(reply), reply
    arguments = {"lean": lean} if lean is not None else {}
    reply = _broker_call(module_root, environ, "start", True, brief=brief, target=target,
                         write=write or lean is not None, effort=effort, detail=detail, policy=policy,
                         overrides=overrides, turn_timeout_seconds=turn_timeout_seconds, **arguments)
    return _open_output(reply)


def cmd_resume(module_root: Path, environ: dict, thread: str, target: Optional[str], write: bool,
               effort: Optional[str], detail: str, policy: dict, overrides: list[str],
               turn_timeout_seconds: int, lean: Optional[str] = None) -> tuple[int, list[str], dict]:
    state = L.state_root(module_root, environ)
    refusals = L.early_refusals(state, effort or L.DEFAULT_EFFORT, overrides) or \
        _lean_client_refusals(module_root, lean, target)
    if refusals:
        reply = {"ok": False, "code": L.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED", "refusals": refusals}
        return reply["code"], _refusal_lines(reply), reply
    arguments = {"lean": lean} if lean is not None else {}
    reply = _broker_call(module_root, environ, "resume", True, thread=thread, target=target,
                         write=write or lean is not None, effort=effort, detail=detail, policy=policy,
                         overrides=overrides, turn_timeout_seconds=turn_timeout_seconds, **arguments)
    return _open_output(reply)


def _open_output(reply: dict) -> tuple[int, list[str], dict]:
    code = int(reply.get("code", L.EXIT_CODEX_FAILED))
    if not reply.get("session"):
        return code, _refusal_lines(reply), reply
    lines = [
        f"session={reply['session']} state={reply.get('state')} thread={reply.get('thread')} "
        f"mode={reply.get('mode')} effort={reply.get('effort')} detail={reply.get('detail')}"
        + (f" lean={reply['lean']}" if reply.get("lean") else "")
        + (f" resumed_from={reply['resumed_from']}" if reply.get("resumed_from") else "")
    ]
    if code == 0:
        lines.append(f"next: luna-reserve wait {reply['session']}  |  luna-reserve events {reply['session']} --follow")
    else:
        lines.extend(_refusal_lines(reply))
    return code, lines, reply


def cmd_simple(module_root: Path, environ: dict, op: str, session: str, **arguments: Any) -> tuple[int, list[str], dict]:
    reply = _broker_call(module_root, environ, op, False, session=session, **arguments)
    code = int(reply.get("code", L.EXIT_CODEX_FAILED))
    if not reply.get("session"):
        return code, _refusal_lines(reply), reply
    head = f"session={reply['session']} state={reply.get('state')} {str(reply.get('verdict', '')).lower()}"
    if reply.get("turn"):
        head += f" turn={reply['turn']}"
    if reply.get("approval"):
        head += f" {reply['approval']}={reply.get('decision')}"
    if reply.get("detail"):
        head += f" detail={reply['detail']}"
    if reply.get("stop_audit"):
        head += f" stop_audit={reply['stop_audit']}"
    if reply.get("wind_down"):
        head += f" wind_down={reply['wind_down']}"
    lines = [head]
    if code != 0:
        lines.extend(_refusal_lines(reply)[1:])
    return code, lines, reply


def _verdict_code(record: dict) -> int:
    state = record.get("state")
    if state == "tripped":
        return L.EXIT_ATTRIBUTION_FAILED
    if state == "refused":
        return L.EXIT_PREFLIGHT_REFUSED
    if state == "unclean":
        return L.EXIT_CODEX_FAILED
    if state == "stopped":
        return L.EXIT_OK if (record.get("stop_audit") or {}).get("verdict", "PASS") == "PASS" \
            else L.EXIT_ATTRIBUTION_FAILED
    if record.get("pending_approvals"):
        return EXIT_ATTENTION
    turns = record.get("turns") or []
    verdict = turns[-1].get("verdict") if turns else None
    if verdict == "ATTRIBUTION_FAILED":
        return L.EXIT_ATTRIBUTION_FAILED
    if verdict == "CODEX_FAILED" or state in ("failed", "lost"):
        return L.EXIT_CODEX_FAILED
    if verdict == "INTERRUPTED":
        return EXIT_INTERRUPTED
    return L.EXIT_OK


def session_lines(record: dict, excerpt_lines: int = 3) -> list[str]:
    turns = record.get("turns") or []
    last = turns[-1] if turns else {}
    tokens = last.get("tokens") or {}
    lines = [
        f"session={record.get('id')} state={record.get('state')} thread={record.get('thread_id')} "
        f"turn={last.get('n')} status={last.get('status')} verdict={last.get('verdict')} "
        f"thread_tokens in={tokens.get('input')} out={tokens.get('output')}"
    ]
    if record.get("stop_audit"):
        lines[0] += f" stop_audit={record['stop_audit'].get('verdict')}"
    wind_down = (record.get("lean") or {}).get("wind_down")
    if wind_down:
        lines[0] += f" wind_down={wind_down.get('verdict')}"
        if wind_down.get("verdict") != "OK":
            lines.append(one_line(f"wind-down {wind_down.get('status')}: {wind_down.get('detail')} "
                                  f"residual={len(wind_down.get('residual') or [])}"))
    for approval in record.get("pending_approvals") or []:
        lines.append(one_line(f"approval {approval.get('id')} [{'|'.join(approval.get('decisions') or DECISIONS)}]: "
                              f"{approval.get('summary')}"))
    for refusal in (record.get("refused_approvals") or [])[-1:]:
        lines.append(one_line(f"refused {refusal.get('id')} by the broker: {refusal.get('summary')} "
                              f"-- {refusal.get('reason')}"))
    for failure in (last.get("failures") or [])[:3] + (last.get("errors") or [])[:2]:
        lines.append(f"failure: {one_line(failure)}")
    if last.get("last_message"):
        try:
            text = Path(last["last_message"]).read_text(encoding="utf-8").splitlines()
        except OSError:
            text = []
        lines.append(f"final={last['last_message']} ({len(text)} lines)")
        lines.extend("  " + one_line(line, 160) for line in text[:excerpt_lines])
    if record.get("state") == "tripped" or last.get("verdict") == "ATTRIBUTION_FAILED":
        lines.append(L.STOP_MESSAGE)
    return lines


def _broker_alive_for(state: Path, record: dict) -> bool:
    reply = probe_broker(state)
    return reply is not None and reply.get("instance") == record.get("broker_instance")


def cmd_wait(module_root: Path, environ: dict, session: str, timeout: float,
             poll_seconds: float = 0.5) -> tuple[int, list[str], dict]:
    state = L.state_root(module_root, environ)
    deadline = time.monotonic() + timeout
    next_liveness = 0.0
    while True:
        record = load_record(state, session)
        if record is None:
            return EXIT_USAGE, [f"no session {session}"], {}
        current = record.get("state")
        if current in TERMINAL_STATES or current == "idle" or record.get("pending_approvals"):
            return _verdict_code(record), session_lines(record), record
        now = time.monotonic()
        if now >= next_liveness:
            next_liveness = now + 5
            if not _broker_alive_for(state, record):
                lines = session_lines(record)
                lines.append("broker is gone; the session is lost; resume its thread in a new session")
                return L.EXIT_CODEX_FAILED, lines, record
        if now > deadline:
            return EXIT_TIMEOUT, session_lines(record) + [f"timeout after {timeout:g}s; still {current}"], record
        time.sleep(poll_seconds)


def cmd_approve_builds(module_root: Path, environ: dict, session: str, header_base: Optional[str],
                       header_files: list[str], allowed: list[str], timeout: float,
                       poll_seconds: float = 0.5) -> tuple[int, list[str], dict]:
    """Opt-in, fail-closed approval loop for the narrow Lean build rule."""
    state = L.state_root(module_root, environ)
    initial = load_record(state, session)
    if initial is None:
        return EXIT_USAGE, [f"no session {session}"], {}
    if header_base is None and (header_files or allowed):
        return EXIT_USAGE, ["--header-file/--allow-removed require --header-base"], initial
    deadline = time.monotonic() + timeout
    approved: list[str] = []
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return EXIT_TIMEOUT, approved + [f"timeout after {timeout:g}s"], load_record(state, session) or initial
        code, waited, record = cmd_wait(module_root, environ, session, remaining, poll_seconds=poll_seconds)
        if code == EXIT_TIMEOUT:
            return code, approved + waited, record
        pending = list(record.get("pending_approvals") or [])
        if not pending:
            return code, approved + waited, record

        failures = []
        for approval in pending:
            reason = build_approval_failure(record, approval, header_base, header_files, set(allowed))
            if reason is not None:
                failures.append((approval, reason))
        if failures:
            lines = list(approved)
            for approval, reason in failures:
                lines.append(one_line(f"approval {approval.get('id')}: {approval.get('summary')}"))
                lines.append(f"rule failed: {reason}")
            return EXIT_ATTENTION, lines, record

        for approval in pending:
            approve_code, lines, reply = cmd_simple(
                module_root, environ, "approve", session,
                approval=approval.get("id"), decision="accept",
            )
            if approve_code != L.EXIT_OK:
                return approve_code, approved + lines, reply
            approved.append(f"approved {approval.get('id')}: {approval.get('summary')}")
        if time.monotonic() >= deadline:
            return EXIT_TIMEOUT, approved + [f"timeout after {timeout:g}s"], record


def cmd_events(module_root: Path, environ: dict, session: str, follow: bool, last: int, since: int,
               emit: Callable[[str], None], poll_seconds: float = 0.5, timeout: Optional[float] = None) -> int:
    state = L.state_root(module_root, environ)
    record = load_record(state, session)
    if record is None:
        emit(f"no session {session}")
        return EXIT_USAGE
    if not follow:
        events, _ = read_events(state, session)
        shown = [event for event in events if event.get("seq", 0) > since and visible(event, record.get("detail"))]
        for event in shown[-max(1, min(last, 200)):]:
            emit(format_event(event))
        return L.EXIT_OK
    offset = 0
    deadline = time.monotonic() + timeout if timeout else None
    next_liveness = time.monotonic() + 5
    while True:
        record = load_record(state, session) or record
        events, offset = read_events(state, session, offset)
        for event in events:
            if event.get("seq", 0) > since and visible(event, record.get("detail")):
                emit(format_event(event))
        if record.get("state") in TERMINAL_STATES:
            emit(f"session {session} ended: {record.get('state')}")
            return _verdict_code(record)
        now = time.monotonic()
        if now >= next_liveness:
            next_liveness = now + 5
            if not _broker_alive_for(state, record):
                emit(f"session {session} lost: the broker is gone")
                return L.EXIT_CODEX_FAILED
        if deadline is not None and now > deadline:
            return EXIT_TIMEOUT
        time.sleep(poll_seconds)


def cmd_read(module_root: Path, environ: dict, session: str, items: int, lines: int) -> tuple[int, list[str], dict]:
    state = L.state_root(module_root, environ)
    record = load_record(state, session)
    if record is None:
        return EXIT_USAGE, [f"no session {session}"], {}
    lines = max(1, min(lines, 60))
    if items:
        path = sessions_dir(state) / session / "items-latest.json"
        reply = {}
        if probe_broker(state) is not None and record.get("state") in OPEN_STATES:
            reply = call(state, "items", session=session, limit=items)
        if not reply.get("ok"):
            turns = record.get("turns") or []
            path = sessions_dir(state) / session / "turns" / str(turns[-1]["n"]) / "items.json" if turns else path
        data = read_json(path) or {}
        entries = (data.get("data") or [])[-max(1, min(items, 50)):]
        output = [f"session={session} items={len(entries)} source={path}"]
        for entry in entries:
            item = entry.get("item") if isinstance(entry, dict) and "item" in entry else entry
            item = item if isinstance(item, dict) else {}
            text = item.get("text") or item.get("command") or ""
            if not text and item.get("content"):
                text = " ".join(str(part.get("text", "")) for part in item["content"] if isinstance(part, dict))
            output.append(one_line(f"{item.get('type')}: {text}", 160))
        return L.EXIT_OK, output, data
    turns = record.get("turns") or []
    message = next((turn.get("last_message") for turn in reversed(turns) if turn.get("last_message")), None)
    if not message:
        return L.EXIT_OK, [f"session={session} has no final message yet (state={record.get('state')})"], record
    text = Path(message).read_text(encoding="utf-8").splitlines()
    output = [f"final={message} ({len(text)} lines, showing {min(lines, len(text))})"]
    output.extend(line[:LINE_WIDTH] for line in text[:lines])
    return L.EXIT_OK, output, record


def cmd_list(module_root: Path, environ: dict, limit: int) -> tuple[int, list[str], dict]:
    state = L.state_root(module_root, environ)
    reply = probe_broker(state)
    live = set((reply or {}).get("live") or [])
    records = all_records(state)[-max(1, min(limit, 50)):]
    lines = [f"broker={'pid ' + str(reply['pid']) if reply else 'not running'} sessions={len(all_records(state))} "
             f"tripwire={'PRESENT' if L.tripwire_path(state).exists() else 'absent'}"]
    for record in records:
        current = record.get("state")
        if current in OPEN_STATES and record.get("id") not in live:
            current = "lost"
        last = (record.get("last_event") or {}).get("text") or ""
        lines.append(one_line(
            f"{record.get('id')} {current} thread={record.get('thread_id')} {record.get('mode')} "
            f"turns={len(record.get('turns') or [])} pending={len(record.get('pending_approvals') or [])} "
            f"target={record.get('target')} last={last}", 240))
    return L.EXIT_OK, lines, {"broker": reply, "sessions": records}


def cmd_detail(module_root: Path, environ: dict, session: str, level: str) -> tuple[int, list[str], dict]:
    state = L.state_root(module_root, environ)
    if level not in DETAIL_LEVELS:
        return EXIT_USAGE, [f"detail must be one of {', '.join(DETAIL_LEVELS)}"], {}
    record = load_record(state, session)
    if record is None:
        return EXIT_USAGE, [f"no session {session}"], {}
    if record.get("state") in OPEN_STATES and probe_broker(state) is not None:
        return cmd_simple(module_root, environ, "detail", session, level=level)
    record["detail"] = level
    write_private_json(sessions_dir(state) / session / "session.json", record)
    return L.EXIT_OK, [f"session={session} state={record.get('state')} detail={level}"], record


def cmd_shutdown(module_root: Path, environ: dict) -> tuple[int, list[str], dict]:
    state = L.state_root(module_root, environ)
    if probe_broker(state) is None:
        info = read_json(info_path(state))
        notes = replace_stale_broker(state, info) if info else []
        return L.EXIT_OK, ["no broker is running"] + notes, {}
    info = read_json(info_path(state)) or {}
    reply = call(state, "shutdown", timeout=STOP_WAIT_SECONDS * 3)
    deadline = time.monotonic() + 10
    while _pid_alive(info.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.1)
    return L.EXIT_OK, [f"broker pid {info.get('pid')} shut down; sessions stopped={reply.get('stopped')}"], reply
