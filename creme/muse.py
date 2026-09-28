"""Muse pseudo-subagents: bounded, model-pinned Muse sessions a Creme master drives.

``python3 -m creme muse`` hands a brief to Muse (``muse-spark-1.3``, never a
``contributor`` variant) and keeps the run bounded. ``status`` and ``run`` live
here; the brokered, steerable sessions (``start``/``send``/``steer``/...) live
in ``creme.muse_broker`` and use the same host and guard.

How a session is built (all measured on the first host, Muse 1.4.0):

1. **Bootstrap.** ``muse serve`` composes a new session's permission profile
   from the user's saved ``permissions.default_profile``; ``:auto-review`` (the
   user's choice for interactive sessions) cannot be composed on a serve host,
   and a Creme-owned config root has no credentials. So a session is created by
   ``muse exec --provider echo`` (no model call, zero tokens) with an explicit
   ``--permission-profile`` (``:read-only`` or ``:ask-me``),
   ``--no-foreign-personal-context``, ``--disable-web-tools``, a Creme-minted
   ``--session-id``, and ``--workspace TARGET``. The durable permission
   bootstrap is reused on resume. Nothing in the user's Muse configuration is
   read for writing, copied, or changed.
2. **Attach.** A ``muse serve`` host whose sandbox posture is fixed per mode
   ``session/resume``s it; the guard then selects the pinned model
   (``session/setModel``), approval mode ``promptUnmatched``, and the effort,
   and reads them back (``model/list`` active row, ``session/read``) before any
   turn.
3. **Turns.** Every approval reaches the broker's allowlist
   (``muse_client.decide_approval``); every notification is checked by the
   guard; each turn ends with an audit of the durable session log.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import luna_lean
from . import pseudo_broker as PB
from .codex_app_server import AppServerError, PinViolation
from .muse_log import DurableLog
from .muse_client import (
    EFFORTS, PINNED_MODEL, ApprovalDecision, MuseGuard, MuseServeProcess, TurnOutcome, audit_session_log,
    decide_approval, describe_approval, log_length, model_failures, receipt_handler, uuid7,
)

BINARY_ENV = "CREME_MUSE_BIN"
STATE_ENV = "CREME_MUSE_STATE"
STATE_RELATIVE = Path(".creme/muse")
DEFAULT_BINARY = Path("~/.local/bin/muse")
PREAMBLE_RELATIVE = Path("templates/muse/preamble.md")
LEAN_PREAMBLE_RELATIVE = Path("templates/muse/lean-preamble.md")
TRIPWIRE_NAME = "MODEL_PIN_FAILURE"
STOP_MESSAGE = ("MODEL PIN FAILURE: Muse served or selected a model other than muse-spark-1.3. "
                "Stop using Muse pseudo-subagents and tell the user.")

EXIT_OK = 0
EXIT_PREFLIGHT_REFUSED = 10
EXIT_MUSE_FAILED = 11
EXIT_PIN_FAILED = 12

DEFAULT_EFFORT = "medium"
DEFAULT_TIMEOUT_SECONDS = 1800
STREAM_IDLE_ENV = "CREME_MUSE_STREAM_IDLE_TIMEOUT_SECS"
DEFAULT_STREAM_IDLE_TIMEOUT_SECS = 900
STREAM_IDLE_CHILD_ENV = "TBH_STREAM_IDLE_TIMEOUT_SECS"
USAGE_REFUSE_PERCENT = 99
BOOTSTRAP_TIMEOUT_SECONDS = 120
INTERRUPT_WAIT_SECONDS = 60
BOOTSTRAP_TEXT = "Creme pseudo-subagent session bootstrap (echo provider; no model call)."

MODES = ("read-only", "write", "lean")
# Permission profile chosen at bootstrap, and the fixed sandbox posture of the
# serve host, per mode. See docs/guides/muse.md "Sandbox and approvals".
PROFILES = {"read-only": ":read-only", "write": ":ask-me", "lean": ":ask-me"}
SERVE_FLAGS = {
    "read-only": ("--disable-write", "--sandbox-network", "restricted"),
    "write": ("--sandbox-network", "restricted"),
    # The owned build lowers its priority (os.nice), which the sandbox denies;
    # the Lean host is therefore unsandboxed and the approval allowlist is the
    # control for every shell command.
    "lean": ("--disable-sandbox",),
}
_SCRUBBED_PREFIXES = ("MUSE_", "TBH_", "META_", "ANTHROPIC_", "OPENAI_", "OPENROUTER_")


class MuseError(RuntimeError):
    """A condition that stops a run with a user-facing reason."""


def state_root(module_root: Path, environ: Optional[dict] = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(STATE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    from .semaphore import canonical_creme_root

    return canonical_creme_root(module_root) / STATE_RELATIVE


def launch_root(module_root: Path) -> Path:
    from .semaphore import canonical_creme_root

    return canonical_creme_root(module_root)


def tripwire_path(state: Path) -> Path:
    return state / TRIPWIRE_NAME


def record_tripwire(state: Path, run_id: str, session_id: Optional[str], failures: list[str]) -> None:
    PB.private_dir(state)
    path = tripwire_path(state)
    if not path.exists():
        PB.write_private_json(path, {"run": run_id, "muse_session": session_id, "failures": failures,
                                     "at": PB.now_iso()})


def clear_tripwire(state: Path, reason: str, live_sessions: list[str], by: str) -> tuple[int, list[str], dict]:
    """The master's explicit clear: refuse while a session is live; keep the tripwire as a timestamped record."""
    path = tripwire_path(state)
    if not reason or not reason.strip():
        return EXIT_PREFLIGHT_REFUSED, ["refused: --reason must say why the tripwire is a false alarm or resolved"], {}
    if live_sessions:
        return EXIT_PREFLIGHT_REFUSED, [f"refused: {len(live_sessions)} session(s) are live: "
                                        f"{', '.join(live_sessions)}; stop them first"], {}
    if not path.exists():
        return EXIT_OK, ["no tripwire is present"], {}
    record = PB.read_json(path) or {"unparsed": path.read_text(encoding="utf-8", errors="replace")[:4000]}
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    records = PB.private_dir(state / "tripwire-records")
    target = records / f"{TRIPWIRE_NAME}-{stamp}.json"
    record = {**record, "cleared": {"at": PB.now_iso(), "reason": reason.strip(), "by": by}}
    PB.write_private_json(target, record)
    path.unlink()
    return EXIT_OK, [f"tripwire cleared; record kept at {target}", f"reason: {reason.strip()}"], record


def resolve_binary(environ: Optional[dict] = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get(BINARY_ENV) or DEFAULT_BINARY).expanduser()


def stream_idle_timeout_seconds(environ: Optional[dict] = None) -> int:
    """The model-stream idle timeout for every Muse child process, in seconds.

    Default ``DEFAULT_STREAM_IDLE_TIMEOUT_SECS``; the master may override it
    with ``CREME_MUSE_STREAM_IDLE_TIMEOUT_SECS``. Anything but a positive
    integer is refused. The first-event timeout is deliberately left unset.
    """
    source = os.environ if environ is None else environ
    raw = source.get(STREAM_IDLE_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_STREAM_IDLE_TIMEOUT_SECS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise MuseError(f"{STREAM_IDLE_ENV}={raw!r} is not a positive integer of seconds")
    if value < 1:
        raise MuseError(f"{STREAM_IDLE_ENV}={raw!r} is not a positive integer of seconds")
    return value


def child_environment(environ: Optional[dict] = None) -> tuple[dict, list[str]]:
    """The Muse child's environment: provider, model, and prompt overrides removed; no self-update."""
    source = os.environ if environ is None else environ
    timeout_secs = stream_idle_timeout_seconds(source)
    environ = dict(source)
    removed = sorted(key for key in environ if key.startswith(_SCRUBBED_PREFIXES) or key == "CLAUDE_CONFIG_DIR")
    for key in removed:
        environ.pop(key)
    environ["MUSE_NO_AUTO_UPDATE"] = "1"   # the launcher never swaps the binary under a session
    # Muse aborts a model call whose stream is silent for 3 minutes ("model
    # stream idle timeout"); long-context Lean work outlasts that silence, so
    # every child (bootstrap exec and serve) gets the longer idle budget. The
    # first-event timeout is left unset.
    environ[STREAM_IDLE_CHILD_ENV] = str(timeout_secs)
    return environ, removed


def _write(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def git_snapshot(target: Path) -> Optional[dict]:
    """HEAD and porcelain status (ignored files included) of a Git target, or None."""
    try:
        head = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=False, timeout=60)
        if head.returncode != 0:
            return None
        status = subprocess.run(["git", "-C", str(target), "status", "--porcelain", "--ignored"],
                                capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return {"head": head.stdout.strip(), "status": status.stdout}


# ---------------------------------------------------------------------------
# Preflight


def early_refusals(state: Path, effort: str, brief: Optional[str], module_root: Path, target: Optional[str],
                   mode: str, lean_goal: Optional[str] = None,
                   repositories: Optional[Callable[[], tuple]] = None) -> list[str]:
    """Zero-process refusals shared by run, start, send, and resume."""
    refusals = []
    if tripwire_path(state).exists():
        refusals.append(f"model-pin failure tripwire present: {tripwire_path(state)} (only the user clears it)")
    if effort not in EFFORTS:
        refusals.append(f"effort {effort!r} is not one of {', '.join(EFFORTS)}")
    if mode not in MODES:
        refusals.append(f"mode {mode!r} is not one of {', '.join(MODES)}")
    if brief is not None and not brief.strip():
        refusals.append("the brief is empty")
    if target is not None:
        path = Path(target).expanduser()
        if not path.is_dir():
            refusals.append(f"target {path} is not a directory")
        else:
            resolved = path.resolve()
            if mode != "read-only":
                root = launch_root(module_root).resolve()
                if resolved == root:
                    refusals.append("a write session may not target the Creme launch checkout itself")
                if "master" in resolved.parts:
                    refusals.append("a write session may not target a master directory")
            if lean_goal is not None:
                try:
                    roots = repositories() if repositories else luna_lean.repositories(launch_root(module_root))
                    refusals += luna_lean.target_refusals(lean_goal, path, roots)
                except luna_lean.LeanModeError as exc:
                    refusals.append(str(exc))
    return refusals


def usage_refusals(usage: Optional[dict]) -> list[str]:
    """Refuse at or above USAGE_REFUSE_PERCENT on either window; absent usage is recorded, not refused."""
    if not isinstance(usage, dict):
        return []
    refusals = []
    for name in ("window", "weekly"):
        block = usage.get(name) if isinstance(usage.get(name), dict) else {}
        used = block.get("usedPercent")
        if isinstance(used, (int, float)) and not isinstance(used, bool) and used >= USAGE_REFUSE_PERCENT:
            refusals.append(f"Muse {name} usage is {used}% (refused at {USAGE_REFUSE_PERCENT}%)")
    return refusals


def remember_usage(state: Path, usage: Optional[dict]) -> None:
    """Keep the last usage window a host observed, for the admission of a later host."""
    if isinstance(usage, dict):
        PB.private_dir(state)
        PB.write_private_json(state / "usage-last.json", {"usage": usage, "at": PB.now_iso()})


def admission_usage(state: Path, observed: Optional[dict]) -> tuple[Optional[dict], str]:
    """The usage an admission judges: this host's observation, else the last one whose windows have not reset.

    A fresh ``muse serve`` host reports no usage until its first model call, so
    the last observation of an earlier host stands in while its window lasts.
    """
    if isinstance(observed, dict):
        remember_usage(state, observed)
        return observed, "host"
    last = (PB.read_json(state / "usage-last.json") or {}).get("usage")
    if isinstance(last, dict):
        now_ms = time.time() * 1000
        blocks = {name: last.get(name) if isinstance(last.get(name), dict)
                  and (last[name].get("resetsAtMs") or 0) > now_ms else {} for name in ("window", "weekly")}
        if any(blocks.values()):
            return {**last, **blocks}, "last-observed"
    return None, "unobserved"


def lean_mcp_failures(environ: Optional[dict] = None) -> list[str]:
    """The user's Muse ``lean-lsp-mcp`` definition must keep Creme's guarded launcher and pins (read-only check)."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or str(Path(environ.get("HOME", str(Path.home()))) / ".config")
    path = Path(base) / "muse" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"Muse settings {path} are unreadable: {exc}"]
    server = ((settings.get("mcpServers") or {}).get(luna_lean.LEAN_MCP_SERVER)) or {}
    if not server:
        return [f"Muse settings define no {luna_lean.LEAN_MCP_SERVER} server"]
    failures = []
    args = server.get("args")
    if server.get("command") != "/usr/bin/python3":
        failures.append(f"{luna_lean.LEAN_MCP_SERVER} command is {server.get('command')!r}, not /usr/bin/python3")
    if not (isinstance(args, list) and len(args) == 6 and args[:5] == ["-m", "creme", "lean-mcp", "--", "uvx"]
            and isinstance(args[5], str) and luna_lean.PINNED_ARGS.fullmatch(args[5])):
        failures.append(f"{luna_lean.LEAN_MCP_SERVER} args are not the guarded `-m creme lean-mcp -- uvx PIN` launcher")
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    disabled = {item.strip() for item in str(env.get("LEAN_MCP_DISABLED_TOOLS", "")).split(",") if item.strip()}
    if not luna_lean.REQUIRED_DISABLED_ENV <= disabled:
        failures.append("LEAN_MCP_DISABLED_TOOLS does not disable lean_build and lean_profile_proof")
    if env.get("LEAN_LSP_MAX_OPEN_FILES") != "2":
        failures.append("LEAN_LSP_MAX_OPEN_FILES is not 2")
    return failures


def instructions(module_root: Path, target: Path, mode: str, lean_goal: Optional[str]) -> str:
    template = (module_root / (LEAN_PREAMBLE_RELATIVE if lean_goal else PREAMBLE_RELATIVE)).read_text(encoding="utf-8")
    return (template.replace("{launch_root}", str(launch_root(module_root)))
            .replace("{target}", str(target)).replace("{mode}", mode).replace("{goal}", lean_goal or ""))


def first_turn_text(module_root: Path, target: Path, mode: str, lean_goal: Optional[str], brief: str) -> str:
    return instructions(module_root, target, mode, lean_goal).rstrip() + "\n\n# Brief\n\n" + brief.strip() + "\n"


# ---------------------------------------------------------------------------
# Host: bootstrap, attach, pin


class MuseHost:
    """One Muse session: an echo-bootstrapped durable session and the serve host attached to it."""

    def __init__(self, record_dir: Path, target: Path, mode: str, effort: str, environ: dict,
                 lean_goal: Optional[str] = None, session_id: Optional[str] = None,
                 server_request_handler: Callable = receipt_handler) -> None:
        self.dir = record_dir
        self.target = target.resolve()
        self.mode = mode
        self.effort = effort
        self.lean_goal = lean_goal
        self.binary = resolve_binary(environ)
        self.env, self.scrubbed = child_environment(environ)
        self.session_id = session_id
        self.handler = server_request_handler
        self.process: Optional[MuseServeProcess] = None
        self.guard: Optional[MuseGuard] = None
        self.log_path: Optional[Path] = None
        self.log_start = 0
        self.view_cursor: Optional[str] = None   # the last durable view cursor this client has processed
        self.durable = DurableLog(None)
        self.server_info: dict = {}
        self._transcript = None
        self._stderr = None

    def bootstrap(self) -> dict:
        """Create the durable session with the echo provider: explicit profile, no model call."""
        self.session_id = uuid7()
        argv = [str(self.binary), "exec", "--json", "--provider", "echo", "--session-id", self.session_id,
                "--permission-profile", PROFILES[self.mode], "--no-foreign-personal-context",
                "--disable-web-tools", "--workspace", str(self.target), BOOTSTRAP_TEXT]
        record: dict = {"argv": argv, "scrubbed_env": self.scrubbed, "at": PB.now_iso()}
        try:
            completed = subprocess.run(argv, cwd=str(self.target), env=self.env, capture_output=True, text=True,
                                       check=False, timeout=BOOTSTRAP_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            record["error"] = str(exc)
            PB.write_private_json(self.dir / "bootstrap.json", record)
            raise MuseError(f"muse exec bootstrap could not run: {exc}")
        record.update({"exit": completed.returncode, "stderr_tail": completed.stderr[-600:]})
        streams, providers = set(), set()
        for line in completed.stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            streams.add(((row.get("stream") or {}).get("id")))
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("provider_id"):
                providers.add(payload["provider_id"])
        record["streams"] = sorted(str(item) for item in streams)
        PB.write_private_json(self.dir / "bootstrap.json", record)
        if completed.returncode != 0:
            raise MuseError(f"muse exec bootstrap exited {completed.returncode}: {completed.stderr.strip()[-300:]}")
        if self.session_id not in streams:
            raise MuseError("muse exec bootstrap did not create the requested session id")
        return record

    def open(self) -> list[str]:
        """Attach a serve host, resume, pin; return refusals (empty when the session is pinned)."""
        self._transcript = open(self.dir / "transcript.jsonl", "a", encoding="utf-8")
        self._stderr = open(self.dir / "serve.stderr", "a", encoding="utf-8")
        flags = list(SERVE_FLAGS[self.mode])
        self.process = MuseServeProcess(self.binary, flags, self.env, self._transcript, self.handler, self._stderr)
        result = self.process.start() or {}
        self.server_info = {"serverInfo": result.get("serverInfo"), "userAgent": result.get("userAgent"),
                            "schema": result.get("schema"), "durability": result.get("sessionDurability"),
                            "serve_flags": flags}
        if result.get("sessionDurability") not in (None, "durable"):
            return [f"muse serve host is {result.get('sessionDurability')}; resume needs durable sessions"]
        self.guard = MuseGuard(self.process, str(self.session_id), self.effort, self.mode, self.lean_goal)
        resumed = self.guard.command("session/resume", {"excludeItems": True}, timeout=120) or {}
        self.view_cursor = resumed.get("viewCursor") or self.view_head()
        session = resumed.get("session") or {}
        path = session.get("path")
        self.log_path = Path(path) if path else None
        refusals = []
        if Path(str(session.get("workspaceRoot") or "")).resolve() != self.target:
            refusals.append(f"session workspace {session.get('workspaceRoot')!r} is not the target {self.target}")
        refusals += self.guard.pin()
        self.log_start = log_length(self.log_path)
        self.durable = DurableLog(self.log_path, self.log_start)
        return refusals

    def view_head(self) -> Optional[str]:
        """The newest durable view cursor (``session/resume`` sometimes returns none), or None."""
        try:
            page = self.guard.request("view/page", {"sessionId": self.session_id, "limit": 1,
                                                    "direction": "backward"}, timeout=60) or {}
        except (AppServerError, PinViolation):
            return None
        events = [event for event in page.get("events") or [] if isinstance(event, dict)]
        return durable_cursor(events[-1]) if events else None

    def usage(self) -> Optional[dict]:
        """``usage/read`` (no model call): the last observed subscription windows, or None."""
        if self.process is None:
            return None
        return (self.process.request("usage/read", {}, timeout=30) or {}).get("usage")

    def close(self) -> None:
        if self.process is not None:
            self.process.close()
        for handle in (self._transcript, self._stderr):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Reconciliation: the live view stream is best-effort
#
# Measured 2026-09-28 (first real use): a long turn can get one
# ``session/viewHealthChanged`` {"health": "unavailable", "noneReason":
# "projectionUnavailable"}, after which the host pushes no further view
# notifications (items, approvals, turn/completed) although the session runs
# on. ``view/page`` (durable-sourced) and ``approval/listPending`` (a log fold)
# still answer, so a client reconciles from them on that notification, on
# ``view/gap``, and periodically while a turn runs.

RECONCILE_SECONDS = 20.0
RECONCILE_ENV = "CREME_MUSE_RECONCILE_SECONDS"
UNHEALTHY_METHODS = ("session/viewHealthChanged", "view/gap")
MAX_REPLAY_PAGES = 20


def durable_cursor(message: dict) -> Optional[str]:
    """The view cursor of a durable-sourced view notification (ephemeral ones carry no sourceRange)."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if params.get("sourceRange") and isinstance(params.get("viewCursor"), str):
        return params["viewCursor"]
    return None


def event_identity(message: dict) -> Optional[str]:
    """Source records identify events; a folded view cursor is only a paging position."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    source = params.get("sourceRange")
    if not source:
        return None
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    return json.dumps([message.get("method"), source, params.get("turnId") or item.get("turnId"),
                       item.get("itemId"), item.get("revision")], sort_keys=True)


class ViewTracker:
    """Track immutable source events independently of the projection's reusable cursors."""

    def __init__(self, cursor: Optional[str]) -> None:
        self.cursor = cursor
        self.seen: set[str] = set()
        self.unhealthy = False
        self.degraded = False
        self.last_reconcile = time.monotonic()

    def admit(self, message: dict) -> bool:
        identity = event_identity(message)
        cursor = durable_cursor(message)
        if cursor is not None:
            self.cursor = cursor
        if identity is None:
            return True
        if identity in self.seen:
            return False
        self.seen.add(identity)
        return True


def event_turn(message: dict) -> Optional[str]:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    return params.get("turnId") or item.get("turnId")


def replay_view(host: "MuseHost", tracker: ViewTracker, turn_id: Optional[str] = None) -> list[dict]:
    """Durable view events after the tracker's cursor that were not processed yet (approval events excluded).

    The events are returned unadmitted: the caller passes each through the same
    ``process`` path as a live notification, which records it as seen.

    Approvals are reconciled from ``approval/listPending`` instead, which lists
    only what is still pending, so a replay never re-decides a resolved one.
    """
    messages: list[dict] = []
    cursor = tracker.cursor
    for _ in range(MAX_REPLAY_PAGES):
        params: dict = {"sessionId": host.session_id, "limit": 500}
        if cursor:
            params["cursor"] = cursor
        page = host.guard.request("view/page", params, timeout=60) or {}
        for event in page.get("events") or []:
            if not isinstance(event, dict) or event.get("method", "").startswith("approval/"):
                if isinstance(event, dict):
                    tracker.admit(event)
                continue
            other = event_turn(event)
            if turn_id is not None and other is not None and other != turn_id:
                # Another turn's event (earlier turns, the echo bootstrap) never touches this turn.
                tracker.admit(event)
                continue
            if event_identity(event) is None or event_identity(event) not in tracker.seen:
                messages.append(event)   # the caller's process() admits it
        following = page.get("nextCursor")
        if not following or following == cursor:
            break
        cursor = following
    return messages


def split_terminals(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Replayed events without their ``turn/completed`` terminals, and the terminals.

    Measured on Muse 1.4.0 (2026-09-28): a ``view/page`` read while a turn is
    still running folds that turn as ``turn/completed`` ``failed`` with reason
    ``incomplete`` and the running turn's own id, although the turn goes on
    (and the live stream never pushes that event). A replayed terminal is
    therefore applied only after ``session/read`` shows the turn is over.
    """
    others = [message for message in messages if message.get("method") != "turn/completed"]
    return others, [message for message in messages if message.get("method") == "turn/completed"]


def turn_over(current: dict, turn_id: str) -> bool:
    """Muse's own view: the session is idle, or runs another turn."""
    return current.get("status") == "idle" or bool(current.get("active_turn") and current["active_turn"] != turn_id)


def find_terminal(host: "MuseHost", turn_id: str, limit: int = 200) -> Optional[dict]:
    """The latest durable ``turn/completed`` for ``turn_id`` near the view head, or None."""
    page = host.guard.request("view/page", {"sessionId": host.session_id, "limit": limit,
                                            "direction": "backward"}, timeout=60) or {}
    found = [event for event in page.get("events") or [] if isinstance(event, dict)
             and event.get("method") == "turn/completed" and (event.get("params") or {}).get("turnId") == turn_id]
    return found[-1] if found else None


def pending_approvals(host: "MuseHost") -> list[dict]:
    listing = host.guard.request("approval/listPending", {"sessionId": host.session_id}, timeout=60) or {}
    return [item for item in listing.get("approvals") or [] if isinstance(item, dict)]


def session_status(host: "MuseHost") -> dict:
    read = host.guard.request("session/read", {"sessionId": host.session_id}, timeout=60) or {}
    session = read.get("session") or {}
    return {"status": session.get("status"), "active_turn": session.get("activeTurnId")}


def resubscribe(host: "MuseHost", tracker: ViewTracker) -> Optional[str]:
    """Try to re-attach the live view after the tracker's cursor; returns an error text or None."""
    params: dict = {"sessionId": host.session_id}
    if tracker.cursor:
        params["after"] = tracker.cursor
    try:
        host.guard.request("view/subscribe", params, timeout=60)
    except AppServerError as exc:
        return str(exc)[:200]
    return None


def short_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:160]


def reconcile_seconds(environ: dict) -> float:
    try:
        value = float(environ.get(RECONCILE_ENV, RECONCILE_SECONDS))
    except (TypeError, ValueError):
        return RECONCILE_SECONDS
    return value if value > 0 else RECONCILE_SECONDS


# ---------------------------------------------------------------------------
# Turn handling shared by run and the broker


@dataclass
class TurnState:
    """Everything one turn's message handler decided, for records and verdicts."""

    approvals: list = field(default_factory=list)       # every stage decision, by rule or by the master
    master: dict = field(default_factory=dict)          # approvalId -> params waiting for the master
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)        # e.g. a failed reconcile source; never fails a turn
    stage_policy: dict = field(default_factory=dict)    # approvalId -> the decision every stage gets


_BENIGN_DECIDE = ("approvalAlreadyResolved", "already resolved", "approvalRequirementStale", "requirement is stale",
                  "-32053", "-32051")


def once_choice(params: dict, decision: str) -> str:
    """The one-shot choice id for ``approved`` or ``abort`` (Muse's ids when the params list none)."""
    for choice in params.get("availableChoices") or []:
        if isinstance(choice, dict) and choice.get("decision") == decision and choice.get("scope") == "once":
            return choice.get("choiceId")
    return "allow_once" if decision == "approved" else "abort"


def recover_decision(host: "MuseHost", exc: AppServerError, decision: str) -> bool:
    """Reconcile one specific settlement error by reading durable evidence, never retrying the action."""
    error = exc.rpc_error if isinstance(exc.rpc_error, dict) else {}
    request = getattr(exc, "approval_request", None)
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    if not isinstance(request, dict) or error.get("code") != -32603 or data.get("retryable") is not True \
            or "approval ledger durability fence" not in str(error.get("message", "")):
        return False
    for attempt in range(3):
        host.durable.poll()
        if host.durable.confirms_decision(request, decision, host.guard.active_turn):
            return True
        if attempt < 2:
            time.sleep(0.1)
    return False


def finalize_durable(host: "MuseHost", outcome: TurnOutcome, errors: list[str]) -> None:
    """Finalize every terminal from durable parent-run records (also when the live terminal arrived)."""
    for attempt in range(3):
        host.durable.poll()
        logged = host.durable.terminal(outcome.turn_id)
        totals, usage_error = host.durable.usage(outcome.turn_id, PINNED_MODEL)
        if host.durable.error:
            break  # unreadable or malformed evidence fails closed at this boundary
        # Use the full bounded window even when earlier usage/text already exist:
        # a final completion can still be landing behind the live terminal.
        if attempt < 2:
            time.sleep(0.1)
    failure = None
    if host.durable.error:
        failure = host.durable.error
    elif logged is None:
        failure = "durable parent-run terminal is missing"
    else:
        outcome.status = logged.get("terminal") or "failed"
        outcome.completed = outcome.completed or time.time()
        if isinstance(logged.get("duration_ms"), int):
            outcome.duration_ms = logged["duration_ms"]
        text = host.durable.messages.get(outcome.turn_id)
        if text is not None:
            outcome.final_message = text
        totals, usage_error = host.durable.usage(outcome.turn_id, PINNED_MODEL)
        if totals is not None:
            outcome.tokens = totals  # authoritative replacement; repeated finalization is idempotent
            if all(model == PINNED_MODEL for model in outcome.models):
                outcome.models = {PINNED_MODEL: totals["completions"]}
        if outcome.status == "completed":
            failure = usage_error or ("durable final assistant text is missing" if text is None else None)
    if failure:
        message = f"durable finalization failed: {failure}"
        if message not in errors:
            errors.append(message)


def decide_stages(host: "MuseHost", params: dict, decision: str, reason: str, turn: TurnState,
                  emit: Callable[[str, str, str], None]) -> None:
    """Answer the approval's current stage and every later stage of the same approval with one decision.

    A compound shell command is approved stage by stage: ``approval/decide``
    answers ``terminal: false`` and the next stage has the next ``sourceIndex``.
    The decision (by the allowlist or the master) covers the whole command, so
    the remaining stages get the same one-shot choice; each stage decision is
    recorded. A stale requirement stops the loop, and the stage then arrives
    again (notification, listPending, or the durable log) and is answered
    from ``stage_policy``.
    """
    approval_id = params.get("approvalId")
    requirement = dict(params.get("currentRequirementId") or {"approvalId": approval_id, "sourceIndex": 0})
    choice = once_choice(params, decision)
    turn.stage_policy[approval_id] = {"decision": decision, "reason": reason, "summary": describe_approval(params)}
    summary = describe_approval(params)
    for _ in range(64):
        entry = {"approval_id": approval_id, "requirement": dict(requirement), "summary": summary,
                 "action": "approve" if decision == "approved" else "abort", "reason": reason, "choice": choice,
                 "at": PB.now_iso(), "tool": params.get("toolName"),
                 "source": "durable-log" if params.get("fromDurableLog") else "msp"}
        answer = None
        try:
            answer = host.guard.decide({"approvalId": approval_id, "currentRequirementId": requirement}, choice)
            entry["answer"] = answer
        except (AppServerError, PinViolation) as exc:
            entry["error"] = str(exc)[:300]
            if isinstance(exc, AppServerError) and recover_decision(host, exc, decision):
                answer = {"terminal": True, "status": "durably_confirmed"}
                entry["answer"] = answer
                entry["command_id"] = exc.approval_request["commandId"]
                turn.warnings.append("approval/decide settlement error; exact command decision confirmed in durable log")
            elif not any(marker in str(exc) for marker in _BENIGN_DECIDE):
                turn.errors.append(f"approval/decide failed: {exc}")
        turn.approvals.append(entry)
        if not isinstance(answer, dict) or answer.get("terminal") is not False:
            break
        requirement = {"approvalId": approval_id, "sourceIndex": int(requirement.get("sourceIndex") or 0) + 1}
    emit("live" if decision == "approved" else "attention", "approval",
         f"{entry['action']} ({reason}) stages={sum(1 for e in turn.approvals if e.get('approval_id') == approval_id)}: "
         f"{summary}")


def handle_approval(host: MuseHost, params: dict, turn: TurnState, emit: Callable[[str, str, str], None],
                    master_available: bool) -> Optional[dict]:
    """Decide one approval stage by the allowlist (or an earlier stage's decision); return params for the master."""
    if any(entry.get("approval_id") == params.get("approvalId") and entry.get("requirement")
           == params.get("currentRequirementId") for entry in turn.approvals if entry.get("action") != "master"):
        return None
    policy = turn.stage_policy.get(params.get("approvalId"))
    if policy is not None:
        decide_stages(host, params, policy["decision"], policy["reason"] + "; same decision for every stage", turn, emit)
        return None
    decision: ApprovalDecision = decide_approval(params, host.mode, host.lean_goal, host.target)
    if decision.action == "master" and not master_available:
        decision = ApprovalDecision("abort", decision.reason + "; a one-shot run has no master to ask",
                                    once_choice(params, "abort"))
    if decision.action == "master":
        if not any(entry.get("approval_id") == params.get("approvalId") and entry.get("action") == "master"
                   for entry in turn.approvals):
            turn.approvals.append({"approval_id": params.get("approvalId"),
                                   "requirement": params.get("currentRequirementId"),
                                   "summary": describe_approval(params), "action": "master",
                                   "reason": decision.reason, "at": PB.now_iso(), "tool": params.get("toolName")})
        return params
    decide_stages(host, params, "approved" if decision.action == "approve" else "abort", decision.reason, turn, emit)
    return None


def verdict_for(outcome: Optional[TurnOutcome], audit: dict, guard_failures: list[str], errors: list[str],
                timed_out: bool, git_failure: Optional[str]) -> str:
    models = (outcome.models if outcome else {}) or {}
    pin_failures = list(guard_failures) + list(audit.get("failures") or [])
    if any(model != PINNED_MODEL for model in models):
        pin_failures.append(f"observed models {sorted(models)}")
    if outcome is not None and outcome.unattributed_usage and not audit.get("attributed"):
        # Model-less usage for this turn, and the durable log names no pinned completion of it.
        pin_failures.append(f"{outcome.unattributed_usage} token-usage report(s) without a model id, and the "
                            f"session log attributes no completion of turn {outcome.turn_id} to the pin")
    if pin_failures:
        return "PIN_FAILED"
    if outcome is None or timed_out or errors or git_failure or outcome.status != "completed":
        return "INTERRUPTED" if outcome is not None and outcome.status == "cancelled" and not timed_out \
            and not errors and not git_failure else "FAILED"
    if not models and not audit.get("attributed"):
        return "FAILED"   # a completed turn with no attributed model call proves nothing about the pin
    return "PASS"


def tokens_line(tokens: dict) -> str:
    return (f"prompt={tokens.get('prompt')} output={tokens.get('output')} cached={tokens.get('cached')} "
            f"reasoning={tokens.get('reasoning')} completions={tokens.get('completions')}")


# ---------------------------------------------------------------------------
# status


def status(module_root: Path, environ: Optional[dict] = None) -> tuple[int, dict]:
    """Binary, version, catalogue pin, usage windows, and sandbox postures, with zero model tokens."""
    environ = dict(os.environ if environ is None else environ)
    state = state_root(module_root, environ)
    binary = resolve_binary(environ)
    try:
        env, scrubbed = child_environment(environ)
        stream_idle = stream_idle_timeout_seconds(environ)
    except MuseError as exc:
        report: dict = {"binary": str(binary), "state": str(state), "pinned_model": PINNED_MODEL,
                        "tripwire": tripwire_path(state).exists(), "scrubbed_env": [],
                        "postures": {mode: {"profile": PROFILES[mode], "serve_flags": list(SERVE_FLAGS[mode]),
                                            "approval_mode": "promptUnmatched"} for mode in MODES}}
        report.update(verdict="REFUSED", reasons=[str(exc)])
        return EXIT_PREFLIGHT_REFUSED, report
    report = {"binary": str(binary), "state": str(state), "pinned_model": PINNED_MODEL,
              "tripwire": tripwire_path(state).exists(), "scrubbed_env": scrubbed,
              "stream_idle_timeout_secs": stream_idle,
              "postures": {mode: {"profile": PROFILES[mode], "serve_flags": list(SERVE_FLAGS[mode]),
                                  "approval_mode": "promptUnmatched"} for mode in MODES}}
    reasons: list[str] = []
    if not binary.is_file():
        report.update(verdict="REFUSED", reasons=[f"Muse binary {binary} not found (set {BINARY_ENV})"])
        return EXIT_PREFLIGHT_REFUSED, report
    try:
        version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, env=env, timeout=60)
        report["version"] = version.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        report["version"] = f"unavailable: {exc}"
    process = MuseServeProcess(binary, list(SERVE_FLAGS["read-only"]), env)
    try:
        started = process.start() or {}
        report["server"] = {"serverInfo": started.get("serverInfo"), "schema": started.get("schema"),
                            "durability": started.get("sessionDurability")}
        listing = process.request("model/list", {}, timeout=60) or {}
        report["usage"], report["usage_source"] = admission_usage(
            state, (process.request("usage/read", {}, timeout=30) or {}).get("usage"))
    except AppServerError as exc:
        report.update(verdict="FAILED", reasons=[f"muse serve failed: {exc}"])
        return EXIT_MUSE_FAILED, report
    finally:
        process.close()
    rows = [row for row in listing.get("models") or [] if isinstance(row, dict)]
    report["models"] = [{"modelId": row.get("modelId"), "isDefault": row.get("isDefault"),
                         "variants": row.get("variants")} for row in rows]
    pinned = next((row for row in rows if row.get("modelId") == PINNED_MODEL), None)
    default = next((row.get("modelId") for row in rows if row.get("isDefault")), None)
    report["catalogue_default"] = default
    if pinned is None:
        reasons.append(f"{PINNED_MODEL} is not in model/list")
    else:
        missing = [effort for effort in EFFORTS if effort not in (pinned.get("variants") or [])]
        if missing:
            reasons.append(f"{PINNED_MODEL} lacks efforts {missing}")
    reasons += model_failures(PINNED_MODEL, "pin")   # the constant itself must never be a contributor id
    reasons += usage_refusals(report.get("usage"))
    if report["tripwire"]:
        reasons.append(f"model-pin failure tripwire present: {tripwire_path(state)}")
    report["lean_mcp"] = lean_mcp_failures(environ) or "guarded"
    report["verdict"] = "REFUSED" if reasons else "OK"
    report["reasons"] = reasons
    return (EXIT_PREFLIGHT_REFUSED if reasons else EXIT_OK), report


def format_status(report: dict) -> str:
    usage = report.get("usage")
    if isinstance(usage, dict):
        window, weekly = usage.get("window") or {}, usage.get("weekly") or {}
        usage_text = (f"window={window.get('usedPercent')}% weekly={weekly.get('usedPercent')}% "
                      f"({report.get('usage_source')})")
    else:
        usage_text = "unobserved (usage/read returned no window; admission records it and does not refuse)"
    lines = [
        f"verdict={report.get('verdict')} binary={report.get('binary')} version={report.get('version')}",
        f"stream_idle_timeout_secs={report.get('stream_idle_timeout_secs')}",
        f"pin={report.get('pinned_model')} catalogue_default={report.get('catalogue_default')} "
        f"(never used) models={[row.get('modelId') for row in report.get('models') or []]}",
        f"usage: {usage_text}",
        f"lean_mcp={report.get('lean_mcp') if isinstance(report.get('lean_mcp'), str) else 'FAIL'} "
        f"tripwire={'PRESENT' if report.get('tripwire') else 'absent'}",
    ]
    for mode, posture in (report.get("postures") or {}).items():
        lines.append(f"posture {mode}: profile={posture['profile']} serve={' '.join(posture['serve_flags'])} "
                     f"approval={posture['approval_mode']}")
    lines += [f"reason: {reason}" for reason in report.get("reasons") or []]
    if isinstance(report.get("lean_mcp"), list):
        lines += [f"lean_mcp: {item}" for item in report["lean_mcp"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# run: one bounded turn


@dataclass
class RunRequest:
    brief: str
    target: Path
    mode: str = "read-only"
    effort: str = DEFAULT_EFFORT
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    lean_goal: Optional[str] = None


def run(module_root: Path, request: RunRequest, environ: Optional[dict] = None) -> tuple[int, dict]:
    environ = dict(os.environ if environ is None else environ)
    state = state_root(module_root, environ)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{secrets.token_hex(3)}"
    summary: dict = {"run": run_id, "mode": request.mode, "effort": request.effort, "model": PINNED_MODEL,
                     "target": str(request.target)}
    try:
        summary["stream_idle_timeout_secs"] = stream_idle_timeout_seconds(environ)
    except MuseError as exc:
        summary.update(verdict="REFUSED", exit=EXIT_PREFLIGHT_REFUSED, refusals=[str(exc)])
        return EXIT_PREFLIGHT_REFUSED, summary
    refusals = early_refusals(state, request.effort, request.brief, module_root, str(request.target),
                              request.mode, request.lean_goal)
    if request.lean_goal is not None:
        refusals.append("run has no Lean mode; use a brokered `start --lean GOAL` session")
    if refusals:
        summary.update(verdict="REFUSED", exit=EXIT_PREFLIGHT_REFUSED, refusals=refusals)
        return EXIT_PREFLIGHT_REFUSED, summary
    run_dir = PB.private_dir(PB.private_dir(PB.private_dir(state) / "runs") / run_id)
    summary["run_dir"] = str(run_dir)
    target = request.target.resolve()
    _write(run_dir / "brief.md", request.brief)
    git_before = git_snapshot(target)
    if git_before is not None:
        _write(run_dir / "git-before.txt", git_before["head"] + "\n" + git_before["status"])
    events = open(run_dir / "events.jsonl", "a", encoding="utf-8")

    def emit(level: str, kind: str, text: str) -> None:
        events.write(json.dumps({"t": round(time.time(), 3), "level": level, "kind": kind,
                                 "text": PB.one_line(text, 400)}) + "\n")
        events.flush()

    host = MuseHost(run_dir, target, request.mode, request.effort, environ)
    turn = TurnState()
    outcome: Optional[TurnOutcome] = None
    timed_out = False
    usage_before = usage_after = None
    audit: dict = {"verdict": "NOT_RUN", "failures": []}
    errors: list[str] = []
    try:
        host.bootstrap()
        summary["muse_session"] = host.session_id
        refusals = host.open()
        summary["server"] = host.server_info
        if any("model" in item for item in refusals):
            raise PinViolation("; ".join(refusals))
        if refusals:
            raise MuseError("; ".join(refusals))
        usage_before, source = admission_usage(state, host.usage())
        PB.write_private_json(run_dir / "usage-before.json", {"usage": usage_before, "source": source,
                                                              "at": PB.now_iso()})
        refusals = usage_refusals(usage_before)
        if refusals:
            summary.update(verdict="REFUSED", exit=EXIT_PREFLIGHT_REFUSED, refusals=refusals)
            return EXIT_PREFLIGHT_REFUSED, summary
        text = first_turn_text(module_root, target, request.mode, None, request.brief)
        outcome = host.guard.begin_turn(text)
        emit("live", "turn", f"turn started {outcome.turn_id}")
        tracker = ViewTracker(host.view_cursor)
        every = reconcile_seconds(environ)

        def process(message: dict) -> None:
            if not tracker.admit(message):
                return
            host.guard.observe(message)
            if message.get("method") in ("approval/requested", "approval/updated"):
                handle_approval(host, message.get("params") or {}, turn, emit, master_available=False)
            if message.get("method") in UNHEALTHY_METHODS:
                tracker.unhealthy = True
                emit("attention", "view", f"{message.get('method')}: {json.dumps(message.get('params'))[:160]}")

        def reconcile() -> None:
            tracker.last_reconcile = time.monotonic()
            host.durable.poll()

            def failed(source: str, exc: Exception) -> None:
                # A failing projection call is a warning, never a run failure; the durable log stands in.
                tracker.degraded = True
                turn.warnings.append(f"{source} failed ({short_error(exc)}); durable log used")

            terminals: list = []
            try:
                others, terminals = split_terminals(replay_view(host, tracker, outcome.turn_id))
                for message in others:
                    process(message)
            except (AppServerError, PinViolation) as exc:
                failed("view/page", exc)
            try:
                approvals = pending_approvals(host)
            except (AppServerError, PinViolation) as exc:
                failed("approval/listPending", exc)
                approvals = host.durable.pending(host.session_id, host.target, outcome.turn_id)
            for params in approvals:
                handle_approval(host, params, turn, emit, master_available=False)
            if outcome.status is None:
                logged = host.durable.terminal(outcome.turn_id)
                if logged is not None:
                    outcome.status = logged.get("terminal") or "failed"
                    outcome.completed = outcome.completed or time.time()
                    if outcome.final_message is None:
                        outcome.final_message = host.durable.messages.get(outcome.turn_id)
                    if logged.get("terminal") != "completed" and logged.get("reason"):
                        turn.warnings.append(f"Muse run terminal {logged.get('terminal')}: {logged.get('reason')}")
                else:
                    try:
                        current = session_status(host)
                    except (AppServerError, PinViolation) as exc:
                        failed("session/read", exc)
                        current = None
                    if current is not None and turn_over(current, outcome.turn_id):
                        time.sleep(0.5)   # the terminal may have just landed
                        late: list = []
                        try:
                            more, late = split_terminals(replay_view(host, tracker, outcome.turn_id))
                            for message in more:
                                process(message)
                        except (AppServerError, PinViolation) as exc:
                            failed("view/page", exc)
                        mine = [t for t in terminals + late if (t.get("params") or {}).get("turnId") == outcome.turn_id]
                        terminal = mine[-1] if mine else None
                        if terminal is None:
                            try:
                                terminal = find_terminal(host, outcome.turn_id)
                            except (AppServerError, PinViolation) as exc:
                                failed("view/page", exc)
                        if terminal is not None:
                            process(terminal)
                        host.durable.poll()
                        if outcome.status is None and host.durable.terminal(outcome.turn_id) is None \
                                and current.get("status") == "idle":
                            errors.append("Muse ended the turn but its terminal event was not observed")
                            outcome.status = "lost"
            if tracker.unhealthy:
                failure = resubscribe(host, tracker)
                emit("live", "view", "view/subscribe " + ("failed: " + failure[:80] if failure else "re-attached"))
                tracker.unhealthy = False

        deadline = time.monotonic() + request.timeout_seconds
        interrupt_deadline: Optional[float] = None
        while outcome.status is None:
            now = time.monotonic()
            if (now > deadline or host.guard.guard_failures) and interrupt_deadline is None:
                timed_out = now > deadline
                emit("attention", "interrupt", "turn timed out" if timed_out else "guard failure; interrupting")
                try:
                    host.guard.interrupt()
                except (AppServerError, PinViolation) as exc:
                    errors.append(f"interrupt failed: {exc}")
                interrupt_deadline = now + INTERRUPT_WAIT_SECONDS
            if interrupt_deadline is not None and now > interrupt_deadline:
                errors.append("no turn completion after interrupt")
                break
            if tracker.unhealthy or now - tracker.last_reconcile > (min(5.0, every) if tracker.degraded else every):
                reconcile()
                if outcome.status is not None:
                    break
            message = host.process.next_notification(0.5)
            if message is None:
                continue
            if message.get("method") == "creme/transport/closed":
                errors.append("muse serve closed during the turn")
                outcome.status = outcome.status or "lost"
                break
            process(message)
        errors += turn.errors
        usage_after = host.usage()
        remember_usage(state, usage_after)
        PB.write_private_json(run_dir / "usage-after.json", {"usage": usage_after, "at": PB.now_iso()})
        time.sleep(0.5)   # the durable log lands before the notification
        finalize_durable(host, outcome, errors)
        audit = audit_session_log(host.log_path, host.log_start, outcome.turn_id if outcome else None,
                                  host.session_id)
    except PinViolation as exc:
        errors.append(f"pin violation: {exc}")
    except (AppServerError, MuseError, OSError) as exc:
        errors.append(str(exc))
    finally:
        host.close()
        events.close()
    guard_failures = list(host.guard.guard_failures) if host.guard else []
    guard_failures += [error for error in errors if error.startswith("pin violation")]
    git_after = git_snapshot(target)
    git_failure = None
    if git_before is not None:
        after_text = "" if git_after is None else git_after["head"] + "\n" + git_after["status"]
        _write(run_dir / "git-after.txt", after_text)
        if request.mode == "read-only" and git_after != git_before:
            git_failure = "read-only run changed the target's HEAD or porcelain status"
    verdict = verdict_for(outcome, audit, guard_failures, [e for e in errors if not e.startswith("pin")],
                          timed_out, git_failure)
    if outcome is not None and outcome.final_message is not None:
        _write(run_dir / "last-message.md", outcome.final_message.rstrip("\n") + "\n")
    PB.write_private_json(run_dir / "approvals.json", turn.approvals)
    PB.write_private_json(run_dir / "audit.json", audit)
    code = {"PASS": EXIT_OK, "PIN_FAILED": EXIT_PIN_FAILED}.get(verdict, EXIT_MUSE_FAILED)
    record = {
        **summary, "verdict": verdict, "exit": code, "status": outcome.status if outcome else None,
        "turn_id": outcome.turn_id if outcome else None, "timed_out": timed_out,
        "tokens": outcome.tokens if outcome else None, "models": outcome.models if outcome else {},
        "tool_calls": outcome.tool_calls if outcome else 0, "duration_ms": outcome.duration_ms if outcome else None,
        "wall_seconds": round(outcome.completed - outcome.started, 1) if outcome and outcome.completed else None,
        "guard_failures": guard_failures, "errors": errors, "git_failure": git_failure,
        "session_log": str(host.log_path) if host.log_path else None, "session_log_audit": audit.get("verdict"),
        "usage_before": usage_before, "usage_after": usage_after,
        "approvals": len(turn.approvals), "warnings": turn.warnings[:20],
        "last_message": str(run_dir / "last-message.md") if (run_dir / "last-message.md").exists() else None,
    }
    PB.write_private_json(run_dir / "verdict.json", record)
    if verdict == "PIN_FAILED":
        record_tripwire(state, run_id, host.session_id, guard_failures + list(audit.get("failures") or []))
    return code, record


def format_run(record: dict) -> str:
    lines = [f"verdict={record.get('verdict')} exit={record.get('exit')} run={record.get('run')} "
             f"muse_session={record.get('muse_session')} mode={record.get('mode')} effort={record.get('effort')}"]
    if record.get("refusals"):
        lines += [f"refused: {PB.one_line(item)}" for item in record["refusals"][:8]]
        return "\n".join(lines)
    lines.append(f"status={record.get('status')} models={record.get('models')} "
                 f"session_log_audit={record.get('session_log_audit')} approvals={record.get('approvals')} "
                 f"wall={record.get('wall_seconds')}s")
    if record.get("tokens"):
        lines.append("tokens " + tokens_line(record["tokens"]))
    usage = record.get("usage_before")
    lines.append(f"usage before={json.dumps(usage) if usage else 'unobserved'} "
                 f"after={json.dumps(record.get('usage_after')) if record.get('usage_after') else 'unobserved'}")
    if record.get("last_message"):
        lines.append(f"last_message={record['last_message']}")
    for failure in (record.get("guard_failures") or [])[:4] + (record.get("errors") or [])[:4]:
        lines.append(f"failure: {PB.one_line(failure)}")
    if record.get("git_failure"):
        lines.append(f"failure: {record['git_failure']}")
    if record.get("verdict") == "PIN_FAILED":
        lines.append(STOP_MESSAGE)
    return "\n".join(lines)
