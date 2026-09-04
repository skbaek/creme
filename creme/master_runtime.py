from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from . import semaphore


EVENT_SCHEMA_VERSION = 1
BOARD_SCHEMA_VERSION = 1
BOARD_RENDERER_VERSION = 1

EVENTS_NAME = "events.jsonl"
BOARD_NAME = "board.json"
LOCK_NAME = ".record.lock"
README_NAME = "README.md"
PRIVATE_DIRECTORIES = ("intent", "briefs", "audits")

MAX_EVENT_BYTES = 64 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_TEXT_BYTES = 16 * 1024
MAX_LIST_ITEMS = 256

_EVENT_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)
_ACQUISITION_DOMAIN = b"creme-master-record-acquisition-v1\0"

_MASTER_ACTIONS = {"start", "resume", "end"}
_GOAL_STATUSES = {"ready", "queued", "active", "paused", "blocked", "complete", "retired"}
_DECISION_STATUSES = {"open", "resolved"}
_DECISION_AUTHORITIES = {"master", "user", "intent"}
_PROCEDURE_ACTIONS = {"add", "replace", "retire"}
_AUDIT_KINDS = {
    "intent-drift",
    "gate-integrity",
    "decision-review",
    "procedure-review",
    "process-waste",
    "merge-hygiene",
    "continuity",
}
_AUDIT_VERDICTS = {"ACCEPT", "REJECT", "MIXED", "OPEN"}
_FINDING_STATUSES = {"open", "addressed", "closed", "reopened"}
_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "note"}
_DISCREPANCY_KINDS = {
    "missing-repository",
    "missing-worktree",
    "missing-ref",
    "detached-head",
    "head-drift",
    "upstream-drift",
    "tracked-dirt",
    "untracked-data",
    "inaccessible-fact",
    "stale-board",
}

_README = """# Private master runtime

This directory is host-local runtime state. It must remain ignored and
untracked. The event log is authoritative; `board.json` is a deterministic
projection. See Creme's `docs/guides/master.md` before operating on it.
"""


class MasterRecordError(RuntimeError):
    """The private master record is unavailable or failed validation."""


class RenewalRefused(MasterRecordError):
    """The current invocation could not prove schema-3 lease authority."""


FaultInjector = Callable[[str], None]
Renewal = Callable[[], tuple[bool, str]]
LeaseSnapshot = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class RecordView:
    events: tuple[dict[str, Any], ...]
    board: dict[str, Any]
    expected_board: dict[str, Any]
    board_current: bool
    log_bytes: bytes


@dataclass(frozen=True)
class AppendResult:
    event: dict[str, Any]
    board: dict[str, Any]
    repaired_stale_board: bool
    already_present: bool = False


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


def _thread_lock(root: Path) -> threading.Lock:
    key = str(root)
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _fault(injector: Optional[FaultInjector], stage: str) -> None:
    if injector is not None:
        injector(stage)


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MasterRecordError(f"value is not canonical JSON: {exc}") from exc
    return encoded + b"\n"


def _strict_json(data: bytes, context: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MasterRecordError(f"{context} is not UTF-8: {exc}") from exc

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise MasterRecordError(f"{context} is malformed JSON: {exc}") from exc


def _object(
    value: Any,
    required: set[str],
    context: str,
    *,
    optional: set[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MasterRecordError(f"{context} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing or extra:
        raise MasterRecordError(
            f"{context} fields differ; missing={missing}, extra={extra}"
        )
    return value


def _text(value: Any, context: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "a string" if empty else "a non-empty string"
        raise MasterRecordError(f"{context} must be {qualifier}")
    if "\x00" in value:
        raise MasterRecordError(f"{context} must not contain NUL")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise MasterRecordError(f"{context} exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    return value


def _identifier(value: Any, context: str) -> str:
    text = _text(value, context)
    if _IDENTIFIER.fullmatch(text) is None:
        raise MasterRecordError(f"{context} is not a supported identifier")
    return text


def _choice(value: Any, choices: set[str], context: str) -> str:
    text = _text(value, context)
    if text not in choices:
        raise MasterRecordError(f"{context} must be one of {sorted(choices)}")
    return text


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise MasterRecordError(f"{context} must be boolean")
    return value


def _strings(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise MasterRecordError(
            f"{context} must be an array of at most {MAX_LIST_ITEMS} strings"
        )
    return [_text(item, f"{context}[{index}]") for index, item in enumerate(value)]


def _nullable_text(value: Any, context: str) -> Optional[str]:
    return None if value is None else _text(value, context, empty=True)


def _validate_discrepancy(value: Any, context: str) -> None:
    row = _object(
        value,
        {"repository", "kind", "subject", "recorded", "observed", "detail"},
        context,
    )
    _identifier(row["repository"], f"{context}.repository")
    _choice(row["kind"], _DISCREPANCY_KINDS, f"{context}.kind")
    _text(row["subject"], f"{context}.subject")
    _nullable_text(row["recorded"], f"{context}.recorded")
    _nullable_text(row["observed"], f"{context}.observed")
    _text(row["detail"], f"{context}.detail")


def _validate_payload(kind: str, value: Any) -> None:
    context = f"{kind} payload"
    if kind == "master":
        payload = _object(
            value,
            {"action", "model", "effort", "note", "next_unit", "reconciliation"},
            context,
        )
        _choice(payload["action"], _MASTER_ACTIONS, f"{context}.action")
        for key in ("model", "effort", "note"):
            _text(payload[key], f"{context}.{key}")
        _text(payload["next_unit"], f"{context}.next_unit", empty=True)
        reconciliation = payload["reconciliation"]
        if not isinstance(reconciliation, list) or len(reconciliation) > MAX_LIST_ITEMS:
            raise MasterRecordError(
                f"{context}.reconciliation must be an array of at most {MAX_LIST_ITEMS} rows"
            )
        for index, row in enumerate(reconciliation):
            _validate_discrepancy(row, f"{context}.reconciliation[{index}]")
        return
    if kind == "goal":
        payload = _object(
            value,
            {"goal_id", "status", "worktree", "branch", "checkpoint", "next_unit"},
            context,
        )
        _identifier(payload["goal_id"], f"{context}.goal_id")
        _choice(payload["status"], _GOAL_STATUSES, f"{context}.status")
        for key in ("worktree", "branch", "checkpoint"):
            _text(payload[key], f"{context}.{key}")
        _text(payload["next_unit"], f"{context}.next_unit", empty=True)
        return
    if kind == "merge":
        payload = _object(
            value,
            {"goal_id", "candidate", "result", "evidence", "audit_worthy"},
            context,
        )
        _identifier(payload["goal_id"], f"{context}.goal_id")
        for key in ("candidate", "result", "evidence"):
            _text(payload[key], f"{context}.{key}")
        _boolean(payload["audit_worthy"], f"{context}.audit_worthy")
        return
    if kind == "decision":
        payload = _object(
            value,
            {
                "decision_id",
                "status",
                "title",
                "choice",
                "reason",
                "alternatives",
                "reversible",
                "undo",
                "evidence",
                "authority",
            },
            context,
        )
        _identifier(payload["decision_id"], f"{context}.decision_id")
        _choice(payload["status"], _DECISION_STATUSES, f"{context}.status")
        for key in ("title", "choice", "reason", "evidence"):
            _text(payload[key], f"{context}.{key}")
        _strings(payload["alternatives"], f"{context}.alternatives")
        _boolean(payload["reversible"], f"{context}.reversible")
        undo = _nullable_text(payload["undo"], f"{context}.undo")
        if payload["reversible"] and not undo:
            raise MasterRecordError(f"{context}.undo is required when reversible is true")
        if not payload["reversible"] and undo is not None:
            raise MasterRecordError(f"{context}.undo must be null when reversible is false")
        _choice(payload["authority"], _DECISION_AUTHORITIES, f"{context}.authority")
        return
    if kind == "procedure":
        payload = _object(
            value,
            {"procedure_id", "action", "failure", "replacement", "control", "evidence"},
            context,
        )
        _identifier(payload["procedure_id"], f"{context}.procedure_id")
        _choice(payload["action"], _PROCEDURE_ACTIONS, f"{context}.action")
        for key in ("failure", "replacement", "control", "evidence"):
            _text(payload[key], f"{context}.{key}")
        return
    if kind == "audit":
        payload = _object(
            value,
            {"audit_id", "audit_kind", "verdict", "report", "findings"},
            context,
        )
        _identifier(payload["audit_id"], f"{context}.audit_id")
        _choice(payload["audit_kind"], _AUDIT_KINDS, f"{context}.audit_kind")
        _choice(payload["verdict"], _AUDIT_VERDICTS, f"{context}.verdict")
        _text(payload["report"], f"{context}.report")
        findings = payload["findings"]
        if not isinstance(findings, list) or len(findings) > MAX_LIST_ITEMS:
            raise MasterRecordError(
                f"{context}.findings must be an array of at most {MAX_LIST_ITEMS} rows"
            )
        seen: set[str] = set()
        for index, value in enumerate(findings):
            finding = _object(
                value,
                {"finding_id", "status", "severity", "summary", "evidence"},
                f"{context}.findings[{index}]",
            )
            finding_id = _identifier(
                finding["finding_id"], f"{context}.findings[{index}].finding_id"
            )
            if finding_id in seen:
                raise MasterRecordError(f"{context}.findings contains duplicate {finding_id!r}")
            seen.add(finding_id)
            _choice(
                finding["status"],
                _FINDING_STATUSES,
                f"{context}.findings[{index}].status",
            )
            _choice(
                finding["severity"],
                _FINDING_SEVERITIES,
                f"{context}.findings[{index}].severity",
            )
            for key in ("summary", "evidence"):
                _text(finding[key], f"{context}.findings[{index}].{key}")
        return
    if kind == "note":
        payload = _object(value, {"title", "note", "evidence", "next_unit"}, context)
        for key in ("title", "note", "evidence"):
            _text(payload[key], f"{context}.{key}")
        _text(payload["next_unit"], f"{context}.next_unit", empty=True)
        return
    raise MasterRecordError(f"unsupported event kind: {kind!r}")


def _validate_timestamp(value: Any) -> str:
    text = _text(value, "event.timestamp")
    if _TIMESTAMP.fullmatch(text) is None:
        raise MasterRecordError("event.timestamp must be UTC RFC-3339 with six fractional digits")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise MasterRecordError(f"event.timestamp is invalid: {exc}") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != text:
        raise MasterRecordError("event.timestamp is not canonical UTC RFC-3339")
    return text


def validate_event(value: Any) -> dict[str, Any]:
    event = _object(
        value,
        {"schema_version", "event_id", "timestamp", "kind", "actor", "payload"},
        "event",
    )
    version = event["schema_version"]
    if isinstance(version, bool) or version != EVENT_SCHEMA_VERSION:
        raise MasterRecordError(f"unsupported event schema: {version!r}")
    event_id = _text(event["event_id"], "event.event_id")
    if _EVENT_ID.fullmatch(event_id) is None:
        raise MasterRecordError("event.event_id must be a lowercase 128-bit hexadecimal ID")
    _validate_timestamp(event["timestamp"])
    kind = _text(event["kind"], "event.kind")
    actor = _object(event["actor"], {"client", "acquisition_digest"}, "event.actor")
    client = _text(actor["client"], "event.actor.client")
    if semaphore.CLIENT_LABEL.fullmatch(client) is None:
        raise MasterRecordError("event.actor.client must be a short client label")
    acquisition_digest = _text(
        actor["acquisition_digest"], "event.actor.acquisition_digest"
    )
    if _DIGEST.fullmatch(acquisition_digest) is None:
        raise MasterRecordError("event.actor.acquisition_digest must be a SHA-256 digest")
    _validate_payload(kind, event["payload"])
    if len(_canonical_json(event)) > MAX_EVENT_BYTES:
        raise MasterRecordError(f"event exceeds {MAX_EVENT_BYTES} bytes")
    return event


def validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    """Validate and detach one caller-supplied kind payload."""
    kind = _text(kind, "event kind")
    frozen = _strict_json(_canonical_json(payload), "event payload")
    _validate_payload(kind, frozen)
    return frozen


def parse_event_input(data: bytes) -> tuple[str, dict[str, Any]]:
    """Parse one bounded caller object; IDs, time, and actor remain injected."""
    if len(data) > MAX_EVENT_BYTES:
        raise MasterRecordError(f"event input exceeds {MAX_EVENT_BYTES} bytes")
    value = _strict_json(data, "event input")
    request = _object(value, {"kind", "payload"}, "event input")
    kind = _text(request["kind"], "event input.kind")
    return kind, validate_payload(kind, request["payload"])


def _canonical_log(events: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(dict(event)) for event in events)


def _event_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "kind": event["kind"],
        "timestamp": event["timestamp"],
    }


def reduce_events(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if len(events) > MAX_EVENTS:
        raise MasterRecordError(f"event sequence exceeds {MAX_EVENTS} events")
    goals: dict[str, dict[str, Any]] = {}
    decisions: dict[str, dict[str, Any]] = {}
    findings: dict[str, dict[str, Any]] = {}
    master: Optional[dict[str, Any]] = None
    next_unit: Optional[str] = None
    seen: set[str] = set()

    for event in events:
        validate_event(event)
        event_id = event["event_id"]
        if event_id in seen:
            raise MasterRecordError(f"duplicate event ID: {event_id}")
        seen.add(event_id)
        kind = event["kind"]
        payload = event["payload"]
        if kind == "master":
            master = {
                "action": payload["action"],
                "client": event["actor"]["client"],
                "effort": payload["effort"],
                "event_id": event_id,
                "model": payload["model"],
                "note": payload["note"],
                "timestamp": event["timestamp"],
            }
        elif kind == "goal":
            goals[payload["goal_id"]] = {
                **payload,
                "event_id": event_id,
                "timestamp": event["timestamp"],
            }
        elif kind == "decision":
            decision_id = payload["decision_id"]
            if payload["status"] == "resolved":
                decisions.pop(decision_id, None)
            else:
                decisions[decision_id] = {
                    **payload,
                    "event_id": event_id,
                    "timestamp": event["timestamp"],
                }
        elif kind == "audit":
            for finding in payload["findings"]:
                finding_id = finding["finding_id"]
                if finding["status"] == "closed":
                    findings.pop(finding_id, None)
                else:
                    findings[finding_id] = {
                        **finding,
                        "audit_id": payload["audit_id"],
                        "audit_kind": payload["audit_kind"],
                        "event_id": event_id,
                        "report": payload["report"],
                        "timestamp": event["timestamp"],
                    }
        if kind in {"master", "goal", "note"} and payload["next_unit"]:
            next_unit = payload["next_unit"]

    log_bytes = _canonical_log(events)
    last = _event_summary(events[-1]) if events else None
    return {
        "schema_version": BOARD_SCHEMA_VERSION,
        "renderer_version": BOARD_RENDERER_VERSION,
        "source": {
            "event_count": len(events),
            "last_event_id": events[-1]["event_id"] if events else None,
            "log_digest": hashlib.sha256(log_bytes).hexdigest(),
        },
        "master": master,
        "goals": [goals[key] for key in sorted(goals)],
        "open_decisions": [decisions[key] for key in sorted(decisions)],
        "open_audit_findings": [findings[key] for key in sorted(findings)],
        "last_event": last,
        "next_unit": next_unit,
    }


def render_board(events: Sequence[dict[str, Any]]) -> bytes:
    return _canonical_json(reduce_events(events))


def _normalized_root(root: Path) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        raise MasterRecordError("master record root must be an absolute configured path")
    normalized = Path(os.path.abspath(candidate))
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise MasterRecordError(f"master record root cannot be resolved safely: {exc}") from exc
    if resolved != normalized:
        raise MasterRecordError("master record root crosses a symlinked path")
    return normalized


def _validate_owner_mode(path: Path, mode: int, *, directory: bool) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MasterRecordError(f"cannot inspect private path {path.name}: {exc}") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode) or stat.S_ISLNK(info.st_mode):
        kind = "directory" if directory else "regular file"
        raise MasterRecordError(f"private path {path.name} must be a non-symlink {kind}")
    if info.st_uid != os.geteuid():
        raise MasterRecordError(f"private path {path.name} is not owned by the current user")
    if stat.S_IMODE(info.st_mode) != mode:
        raise MasterRecordError(f"private path {path.name} must have mode {mode:04o}")
    if not directory and info.st_nlink != 1:
        raise MasterRecordError(f"private file {path.name} must not have hard links")
    return info


def _validate_private_tree(root: Path) -> None:
    """Validate one separately owned private subtree without following links."""
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise MasterRecordError(
                f"cannot inventory private path {directory.name}: {exc}"
            ) from exc
        for entry in children:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise MasterRecordError(
                    f"cannot inspect private path {path.name}: {exc}"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise MasterRecordError(
                    f"private path {path.name} must not be a symlink"
                )
            if info.st_uid != os.geteuid():
                raise MasterRecordError(
                    f"private path {path.name} is not owned by the current user"
                )
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o700:
                    raise MasterRecordError(
                        f"private directory {path.name} must have mode 0700"
                    )
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise MasterRecordError(
                    f"private path {path.name} must be a regular file or directory"
                )
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise MasterRecordError(
                    f"private file {path.name} must have mode 0600"
                )
            if info.st_nlink != 1:
                raise MasterRecordError(
                    f"private file {path.name} must not have hard links"
                )


def _validate_layout(root: Path) -> Path:
    root = _normalized_root(root)
    _validate_owner_mode(root, 0o700, directory=True)
    for name in (EVENTS_NAME, BOARD_NAME, LOCK_NAME, README_NAME):
        _validate_owner_mode(root / name, 0o600, directory=False)
    for name in PRIVATE_DIRECTORIES:
        private = root / name
        _validate_owner_mode(private, 0o700, directory=True)
        _validate_private_tree(private)
    return root


def _write_new_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise MasterRecordError(f"could not create private file {path.name}: {exc}") from exc


def _fsync_directory(root: Path) -> None:
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialize_empty_record(root: Path) -> bool:
    """Create the private W1 layout, or validate an already-current empty record.

    The configured-store and ignoredness preflight belongs to the W2 command
    layer. This primitive deliberately accepts only an absolute, symlink-free
    record root and never selects one.
    """
    root = _normalized_root(root)
    if root.exists():
        _validate_owner_mode(root, 0o700, directory=True)
        core = [root / EVENTS_NAME, root / BOARD_NAME, root / LOCK_NAME]
        if all(path.exists() for path in core):
            view = read_record(root)
            if view.events or not view.board_current:
                raise MasterRecordError("existing master record is not current and empty")
            return False
        if any(root.iterdir()):
            raise MasterRecordError("existing master record requires explicit migration")
    else:
        try:
            root.mkdir(mode=0o700)
        except OSError as exc:
            raise MasterRecordError(f"could not create private master directory: {exc}") from exc
    try:
        for name in PRIVATE_DIRECTORIES:
            (root / name).mkdir(mode=0o700)
        _write_new_file(root / README_NAME, _README.encode("utf-8"))
        _write_new_file(root / LOCK_NAME, b"")
        _write_new_file(root / EVENTS_NAME, b"")
        _write_new_file(root / BOARD_NAME, render_board(()))
        _fsync_directory(root)
    except Exception:
        # Bootstrap is previewed by W2 before use. A partial bootstrap remains
        # visible and is refused on retry rather than guessed at or reset.
        raise
    read_record(root)
    return True


def _load_events(path: Path) -> tuple[tuple[dict[str, Any], ...], bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MasterRecordError(f"could not read authoritative event log: {exc}") from exc
    if len(data) > MAX_LOG_BYTES:
        raise MasterRecordError(f"authoritative event log exceeds {MAX_LOG_BYTES} bytes")
    if data and not data.endswith(b"\n"):
        raise MasterRecordError("authoritative event log has a partial final record")
    rows = data.splitlines(keepends=True)
    if len(rows) > MAX_EVENTS:
        raise MasterRecordError(f"authoritative event log exceeds {MAX_EVENTS} events")
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        value = _strict_json(row, f"event log row {index}")
        event = validate_event(value)
        canonical = _canonical_json(event)
        if canonical != row:
            raise MasterRecordError(f"event log row {index} is not canonical JSON")
        if event["event_id"] in seen:
            raise MasterRecordError(f"duplicate event ID: {event['event_id']}")
        seen.add(event["event_id"])
        events.append(event)
    return tuple(events), data


def _validate_board(value: Any) -> dict[str, Any]:
    board = _object(
        value,
        {
            "schema_version",
            "renderer_version",
            "source",
            "master",
            "goals",
            "open_decisions",
            "open_audit_findings",
            "last_event",
            "next_unit",
        },
        "board",
    )
    if (
        isinstance(board["schema_version"], bool)
        or not isinstance(board["schema_version"], int)
        or board["schema_version"] != BOARD_SCHEMA_VERSION
    ):
        raise MasterRecordError("board schema requires explicit migration")
    if (
        isinstance(board["renderer_version"], bool)
        or not isinstance(board["renderer_version"], int)
        or board["renderer_version"] != BOARD_RENDERER_VERSION
    ):
        raise MasterRecordError("board renderer requires explicit migration")
    return board


def read_record(root: Path) -> RecordView:
    root = _validate_layout(root)
    events, log_bytes = _load_events(root / EVENTS_NAME)
    expected = reduce_events(events)
    board_path = root / BOARD_NAME
    try:
        board_bytes = board_path.read_bytes()
    except OSError as exc:
        raise MasterRecordError(f"could not read derived board: {exc}") from exc
    board = _validate_board(_strict_json(board_bytes, "board"))
    canonical = _canonical_json(board)
    if canonical != board_bytes:
        raise MasterRecordError("derived board is not canonical JSON")
    expected_bytes = _canonical_json(expected)
    return RecordView(
        events=events,
        board=board,
        expected_board=expected,
        board_current=board_bytes == expected_bytes,
        log_bytes=log_bytes,
    )


@contextlib.contextmanager
def _locked_record(root: Path) -> Iterator[None]:
    root = _validate_layout(root)
    lock_path = root / LOCK_NAME
    local = _thread_lock(root)
    with local:
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags)
        except OSError as exc:
            raise MasterRecordError(f"could not open private record lock: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            named = lock_path.lstat()
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise MasterRecordError("private record lock changed during open")
            if not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o600:
                raise MasterRecordError("private record lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _atomic_replace(
    path: Path,
    data: bytes,
    *,
    label: str,
    fault: Optional[FaultInjector],
) -> None:
    root = path.parent
    temporary = root / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor: Optional[int] = None
    replaced = False
    _fault(fault, f"{label}:before-temp-create")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            _fault(fault, f"{label}:after-temp-create")
            handle.write(data)
            _fault(fault, f"{label}:after-write")
            handle.flush()
            _fault(fault, f"{label}:after-flush")
            os.fsync(handle.fileno())
            _fault(fault, f"{label}:after-fsync")
        _fault(fault, f"{label}:before-replace")
        os.replace(temporary, path)
        replaced = True
        _fault(fault, f"{label}:after-replace")
        _fault(fault, f"{label}:before-dir-fsync")
        _fsync_directory(root)
        _fault(fault, f"{label}:after-dir-fsync")
    except OSError as exc:
        raise MasterRecordError(f"could not publish {path.name}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _actor_from_snapshot(snapshot: Any) -> dict[str, str]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != semaphore.MASTER_SCHEMA_VERSION:
        raise RenewalRefused("schema-3 master lease snapshot is unavailable")
    lease = snapshot.get("lease")
    if not isinstance(lease, dict):
        raise RenewalRefused("no master lease exists after renewal")
    client = lease.get("client")
    lease_id = lease.get("lease_id")
    if not isinstance(client, str) or semaphore.CLIENT_LABEL.fullmatch(client) is None:
        raise RenewalRefused("renewed master lease has an invalid client label")
    if not isinstance(lease_id, str) or _EVENT_ID.fullmatch(lease_id) is None:
        raise RenewalRefused("renewed master lease has an invalid acquisition binding")
    digest = hashlib.sha256(_ACQUISITION_DOMAIN + lease_id.encode("ascii")).hexdigest()
    return {"client": client, "acquisition_digest": digest}


def _renew_or_refuse(renew: Renewal) -> None:
    try:
        ok, detail = renew()
    except Exception as exc:
        raise RenewalRefused(f"master renewal failed: {exc}") from exc
    if not ok:
        raise RenewalRefused(f"master renewal refused: {detail}")


class RecordWriter:
    """Renew-first private append transaction over one configured record root."""

    def __init__(
        self,
        root: Path,
        *,
        renew: Renewal = semaphore.master_renew,
        lease_snapshot: LeaseSnapshot = semaphore.master_snapshot,
        event_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = _normalized_root(root)
        self.renew = renew
        self.lease_snapshot = lease_snapshot
        self.event_id = event_id
        self.clock = clock

    def append(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        fault: Optional[FaultInjector] = None,
        once_per_acquisition: bool = False,
    ) -> AppendResult:
        # The first renewal is deliberately outside the record lock: a
        # nonholder must not serialize or inspect private state as a writer.
        _renew_or_refuse(self.renew)
        with _locked_record(self.root):
            # Close the wait-for-lock race and bind the actor to the exact
            # acquisition that remains current at the transaction boundary.
            _renew_or_refuse(self.renew)
            try:
                actor = _actor_from_snapshot(self.lease_snapshot())
            except MasterRecordError:
                raise
            except Exception as exc:
                raise RenewalRefused(f"master lease snapshot failed: {exc}") from exc
            view = read_record(self.root)
            event_id = self.event_id()
            now = self.clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise MasterRecordError("event clock must return a timezone-aware UTC datetime")
            timestamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            payload_snapshot = _strict_json(
                _canonical_json(dict(payload)), "event payload"
            )
            event = {
                "schema_version": EVENT_SCHEMA_VERSION,
                "event_id": event_id,
                "timestamp": timestamp,
                "kind": kind,
                "actor": actor,
                "payload": payload_snapshot,
            }
            validate_event(event)
            if once_per_acquisition:
                for existing in reversed(view.events):
                    if (
                        existing["kind"] == kind
                        and existing["actor"]["acquisition_digest"]
                        == actor["acquisition_digest"]
                        and (
                            kind != "master"
                            or existing["payload"]["action"] in {"start", "resume"}
                        )
                    ):
                        if not view.board_current:
                            _atomic_replace(
                                self.root / BOARD_NAME,
                                _canonical_json(view.expected_board),
                                label="board",
                                fault=fault,
                            )
                        return AppendResult(
                            event=existing,
                            board=view.expected_board,
                            repaired_stale_board=not view.board_current,
                            already_present=True,
                        )
            if event_id in {row["event_id"] for row in view.events}:
                raise MasterRecordError(f"duplicate event ID: {event_id}")
            events = (*view.events, event)
            log_bytes = _canonical_log(events)
            if len(log_bytes) > MAX_LOG_BYTES or len(events) > MAX_EVENTS:
                raise MasterRecordError("authoritative event log reached its supported bound")
            board = reduce_events(events)
            _atomic_replace(
                self.root / EVENTS_NAME,
                log_bytes,
                label="log",
                fault=fault,
            )
            _fault(fault, "transaction:after-log-commit")
            _atomic_replace(
                self.root / BOARD_NAME,
                _canonical_json(board),
                label="board",
                fault=fault,
            )
            return AppendResult(
                event=event,
                board=board,
                repaired_stale_board=not view.board_current,
                already_present=False,
            )
