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
from typing import Any, Callable, ContextManager, Iterator, Mapping, Optional, Sequence

from . import semaphore


EVENT_SCHEMA_VERSION = 1
BOARD_SCHEMA_VERSION = 1
BOARD_RENDERER_VERSION = 1

EVENTS_NAME = "events.jsonl"
BOARD_NAME = "board.json"
LOCK_NAME = ".record.lock"
README_NAME = "README.md"
PRIVATE_DIRECTORIES = ("intent", "briefs", "audits")
MIGRATION_REPORT_NAME = "migration.json"
MIGRATION_BACKUP_ROOT_NAME = "migration-backups"
MIGRATION_RETAINED_ROOT_FILES = ("log.md", "board.md", "observations.md")

_PUBLICATION_MARKER_PREFIX = ".record-transaction-v1."
_PUBLICATION_MARKER = re.compile(
    r"^\.record-transaction-v1\.(append|board)\.([0-9a-f]{16})\."
    r"([0-9a-f]{64})\.([0-9a-f]{64})\.([0-9a-f]{64})$"
)
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
    """The current invocation could not prove master lease authority."""


FaultInjector = Callable[[str], None]
Renewal = Callable[[], tuple[bool, str]]
LeaseSnapshot = Callable[[], dict[str, Any]]
AuthorityTransaction = Callable[[], ContextManager[dict[str, Any]]]


@dataclass(frozen=True)
class _PublicationTransaction:
    operation: str
    nonce: str
    source_log_digest: str
    target_log_digest: str
    target_board_digest: str
    marker_name: str
    temporary_names: tuple[str, ...]


@dataclass(frozen=True)
class RecordView:
    events: tuple[dict[str, Any], ...]
    board: dict[str, Any]
    expected_board: dict[str, Any]
    board_current: bool
    log_bytes: bytes
    publication_transaction: Optional[_PublicationTransaction] = None


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


def _publication_temp_name(destination: str, nonce: str) -> str:
    return f".{destination}.{nonce}.record-tmp"


def _publication_marker_name(
    operation: str,
    nonce: str,
    source_log_digest: str,
    target_log_digest: str,
    target_board_digest: str,
) -> str:
    return (
        f"{_PUBLICATION_MARKER_PREFIX}{operation}.{nonce}."
        f"{source_log_digest}.{target_log_digest}.{target_board_digest}"
    )


def _publication_transaction_from_inventory(
    root: Path,
    extras: set[str],
) -> tuple[Optional[_PublicationTransaction], set[str]]:
    """Recognize one atomically described W1 publication, without following it.

    The marker is an empty private directory whose complete description is in
    its atomically created name.  A temporary-looking file without that exact
    description remains an unknown root node.
    """
    marker_names = sorted(
        name for name in extras if name.startswith(_PUBLICATION_MARKER_PREFIX)
    )
    if not marker_names:
        return None, set()
    if len(marker_names) != 1:
        raise MasterRecordError("multiple record publication descriptions refuse recovery")
    marker_name = marker_names[0]
    match = _PUBLICATION_MARKER.fullmatch(marker_name)
    if match is None:
        raise MasterRecordError("record publication description is malformed")
    operation, nonce, source_digest, target_digest, board_digest = match.groups()
    marker = root / marker_name
    _validate_owner_mode(marker, 0o700, directory=True)
    try:
        with os.scandir(marker) as entries:
            if next(iter(entries), None) is not None:
                raise MasterRecordError("record publication description must be empty")
    except OSError as exc:
        raise MasterRecordError(
            f"cannot inspect record publication description: {exc}"
        ) from exc

    possible = {_publication_temp_name(BOARD_NAME, nonce)}
    if operation == "append":
        possible.add(_publication_temp_name(EVENTS_NAME, nonce))
    temporary_names = tuple(sorted(name for name in extras if name in possible))
    if len(temporary_names) > 1:
        raise MasterRecordError("record publication has an impossible temporary inventory")
    for name in temporary_names:
        _validate_owner_mode(root / name, 0o600, directory=False)
    transaction = _PublicationTransaction(
        operation=operation,
        nonce=nonce,
        source_log_digest=source_digest,
        target_log_digest=target_digest,
        target_board_digest=board_digest,
        marker_name=marker_name,
        temporary_names=temporary_names,
    )
    return transaction, {marker_name, *temporary_names}


def _validate_layout(
    root: Path,
    *,
    migration_root_nodes: Optional[Sequence[str]] = None,
) -> tuple[Path, bool, Optional[_PublicationTransaction]]:
    root = _normalized_root(root)
    _validate_owner_mode(root, 0o700, directory=True)
    required_files = (EVENTS_NAME, BOARD_NAME, LOCK_NAME, README_NAME)
    required_directories = PRIVATE_DIRECTORIES
    standard = set(required_files) | set(required_directories)
    try:
        with os.scandir(root) as entries:
            observed = {entry.name for entry in entries}
    except OSError as exc:
        raise MasterRecordError(f"cannot inventory private master root: {exc}") from exc

    extras = observed - standard
    transaction, transaction_nodes = _publication_transaction_from_inventory(
        root, extras
    )
    extras -= transaction_nodes
    if migration_root_nodes is None:
        allowed_migration = {
            MIGRATION_REPORT_NAME,
            MIGRATION_BACKUP_ROOT_NAME,
            *MIGRATION_RETAINED_ROOT_FILES,
        }
        unknown = extras - allowed_migration
        if unknown:
            raise MasterRecordError(
                f"unexpected private master root node: {sorted(unknown)[0]}"
            )
        if extras and MIGRATION_REPORT_NAME not in extras:
            raise MasterRecordError(
                "migration root nodes require a verified migration report"
            )
    else:
        permitted = set(migration_root_nodes)
        if extras != permitted:
            unexpected = sorted(extras - permitted)
            missing = sorted(permitted - extras)
            detail = unexpected[0] if unexpected else missing[0]
            raise MasterRecordError(
                f"migration root inventory changed at node: {detail}"
            )

    for name in required_files:
        _validate_owner_mode(root / name, 0o600, directory=False)
    for name in required_directories:
        private = root / name
        _validate_owner_mode(private, 0o700, directory=True)
        _validate_private_tree(private)
    for name in sorted(extras):
        path = root / name
        if name == MIGRATION_BACKUP_ROOT_NAME:
            _validate_owner_mode(path, 0o700, directory=True)
            _validate_private_tree(path)
        else:
            _validate_owner_mode(path, 0o600, directory=False)
    return root, bool(extras), transaction


def _validated_layout(
    root: Path,
    *,
    migration_root_nodes: Optional[Sequence[str]] = None,
) -> tuple[Path, Optional[_PublicationTransaction]]:
    root, has_migration_nodes, transaction = _validate_layout(
        root,
        migration_root_nodes=migration_root_nodes,
    )
    if has_migration_nodes and migration_root_nodes is None:
        # Import lazily: migration owns the report/backup interpretation while
        # this module owns the record layout used during that interpretation.
        from . import master_migrate

        migration = master_migrate.plan_migration(root)
        if migration.status != "CURRENT":
            raise MasterRecordError(
                f"migration root nodes are not verified current: {migration.detail}"
            )
    return root, transaction


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


def _read_publication_temp(root: Path, name: str) -> bytes:
    path = root / name
    _validate_owner_mode(path, 0o600, directory=False)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MasterRecordError(
            f"could not read record publication temporary {name}: {exc}"
        ) from exc


def _verify_publication_transaction(
    root: Path,
    transaction: _PublicationTransaction,
    events: tuple[dict[str, Any], ...],
    log_bytes: bytes,
    expected_board: dict[str, Any],
) -> None:
    """Bind a crash-left publication description to the authoritative log.

    Empty temporaries are reachable immediately after creation.  Non-empty
    temporaries must already equal the complete deterministic target; partial
    or forged bytes are preserved and refused.
    """
    current_digest = hashlib.sha256(log_bytes).hexdigest()
    expected_board_bytes = _canonical_json(expected_board)
    expected_board_digest = hashlib.sha256(expected_board_bytes).hexdigest()
    temporary = transaction.temporary_names[0] if transaction.temporary_names else None
    log_temporary = _publication_temp_name(EVENTS_NAME, transaction.nonce)
    board_temporary = _publication_temp_name(BOARD_NAME, transaction.nonce)

    if transaction.operation == "board":
        if transaction.source_log_digest != transaction.target_log_digest:
            raise MasterRecordError("board publication description changes the log")
        if current_digest != transaction.source_log_digest:
            raise MasterRecordError("board publication description has the wrong source log")
        if transaction.target_board_digest != expected_board_digest:
            raise MasterRecordError("board publication description has the wrong board target")
        if temporary not in {None, board_temporary}:
            raise MasterRecordError("board publication has an impossible temporary")
        if temporary is not None:
            data = _read_publication_temp(root, temporary)
            if data not in {b"", expected_board_bytes}:
                raise MasterRecordError("board publication temporary is not empty or complete")
        return

    if transaction.source_log_digest == transaction.target_log_digest:
        raise MasterRecordError("append publication description does not advance the log")
    if current_digest == transaction.source_log_digest:
        if temporary not in {None, log_temporary}:
            raise MasterRecordError("pre-commit append has an impossible temporary")
        if temporary is None:
            return
        data = _read_publication_temp(root, temporary)
        if data == b"":
            return
        if hashlib.sha256(data).hexdigest() != transaction.target_log_digest:
            raise MasterRecordError("append publication temporary has the wrong target digest")
        temporary_events, canonical = _load_events(root / temporary)
        if canonical != data or len(temporary_events) != len(events) + 1:
            raise MasterRecordError("append publication temporary is not one complete event")
        if tuple(temporary_events[:-1]) != events:
            raise MasterRecordError("append publication temporary does not extend the source log")
        target_board = render_board(temporary_events)
        if hashlib.sha256(target_board).hexdigest() != transaction.target_board_digest:
            raise MasterRecordError("append publication description has the wrong board target")
        return

    if current_digest != transaction.target_log_digest:
        raise MasterRecordError("append publication description matches neither log state")
    if temporary not in {None, board_temporary}:
        raise MasterRecordError("post-commit append has an impossible temporary")
    if not events:
        raise MasterRecordError("append publication target has no appended event")
    source_bytes = _canonical_log(events[:-1])
    if hashlib.sha256(source_bytes).hexdigest() != transaction.source_log_digest:
        raise MasterRecordError("append publication target is not a one-event extension")
    if transaction.target_board_digest != expected_board_digest:
        raise MasterRecordError("append publication description has the wrong board target")
    if temporary is not None:
        data = _read_publication_temp(root, temporary)
        if data not in {b"", expected_board_bytes}:
            raise MasterRecordError("board publication temporary is not empty or complete")


def _read_record_unlocked(
    root: Path,
    *,
    _migration_root_nodes: Optional[Sequence[str]] = None,
) -> RecordView:
    root, transaction = _validated_layout(
        root, migration_root_nodes=_migration_root_nodes
    )
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
    if transaction is not None:
        _verify_publication_transaction(
            root,
            transaction,
            events,
            log_bytes,
            expected,
        )
    return RecordView(
        events=events,
        board=board,
        expected_board=expected,
        board_current=board_bytes == expected_bytes,
        log_bytes=log_bytes,
        publication_transaction=transaction,
    )


@contextlib.contextmanager
def _record_lock(root: Path, *, exclusive: bool) -> Iterator[Path]:
    root = _normalized_root(root)
    _validate_owner_mode(root, 0o700, directory=True)
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
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield root
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def read_record(
    root: Path,
    *,
    _migration_root_nodes: Optional[Sequence[str]] = None,
) -> RecordView:
    with _record_lock(root, exclusive=False) as locked_root:
        return _read_record_unlocked(
            locked_root,
            _migration_root_nodes=_migration_root_nodes,
        )


@contextlib.contextmanager
def _locked_record(root: Path) -> Iterator[None]:
    with _record_lock(root, exclusive=True):
        yield


def _atomic_replace(
    path: Path,
    data: bytes,
    *,
    label: str,
    fault: Optional[FaultInjector],
    temporary: Optional[Path] = None,
) -> None:
    root = path.parent
    if temporary is None:
        temporary = root / f".{path.name}.{secrets.token_hex(8)}.tmp"
    elif temporary.parent != root:
        raise MasterRecordError("publication temporary must share the destination directory")
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


def _begin_publication_transaction(
    root: Path,
    *,
    operation: str,
    source_log: bytes,
    target_log: bytes,
    target_board: bytes,
    fault: Optional[FaultInjector],
) -> _PublicationTransaction:
    nonce = secrets.token_hex(8)
    source_digest = hashlib.sha256(source_log).hexdigest()
    target_digest = hashlib.sha256(target_log).hexdigest()
    board_digest = hashlib.sha256(target_board).hexdigest()
    marker_name = _publication_marker_name(
        operation,
        nonce,
        source_digest,
        target_digest,
        board_digest,
    )
    marker = root / marker_name
    _fault(fault, "transaction-description:before-create")
    try:
        marker.mkdir(mode=0o700)
    except OSError as exc:
        raise MasterRecordError(
            f"could not create record publication description: {exc}"
        ) from exc
    _fault(fault, "transaction-description:after-create")
    _fault(fault, "transaction-description:before-dir-fsync")
    _fsync_directory(root)
    _fault(fault, "transaction-description:after-dir-fsync")
    return _PublicationTransaction(
        operation=operation,
        nonce=nonce,
        source_log_digest=source_digest,
        target_log_digest=target_digest,
        target_board_digest=board_digest,
        marker_name=marker_name,
        temporary_names=(),
    )


def _discard_publication_transaction(
    root: Path,
    transaction: _PublicationTransaction,
    *,
    fault: Optional[FaultInjector] = None,
) -> None:
    _fault(fault, "transaction-description:before-remove-temporaries")
    for destination in (EVENTS_NAME, BOARD_NAME):
        temporary = root / _publication_temp_name(destination, transaction.nonce)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise MasterRecordError(
                f"could not remove verified publication temporary: {exc}"
            ) from exc
    _fault(fault, "transaction-description:after-remove-temporaries")
    try:
        (root / transaction.marker_name).rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise MasterRecordError(
            f"could not remove record publication description: {exc}"
        ) from exc
    _fault(fault, "transaction-description:after-remove")
    _fsync_directory(root)
    _fault(fault, "transaction-description:after-remove-dir-fsync")


def _publish_record_transaction(
    root: Path,
    *,
    source_log: bytes,
    target_log: bytes,
    target_board: bytes,
    operation: str,
    fault: Optional[FaultInjector],
) -> None:
    transaction: Optional[_PublicationTransaction] = None
    try:
        transaction = _begin_publication_transaction(
            root,
            operation=operation,
            source_log=source_log,
            target_log=target_log,
            target_board=target_board,
            fault=fault,
        )
        if operation == "append":
            _atomic_replace(
                root / EVENTS_NAME,
                target_log,
                label="log",
                fault=fault,
                temporary=root
                / _publication_temp_name(EVENTS_NAME, transaction.nonce),
            )
            _fault(fault, "transaction:after-log-commit")
        _atomic_replace(
            root / BOARD_NAME,
            target_board,
            label="board",
            fault=fault,
            temporary=root / _publication_temp_name(BOARD_NAME, transaction.nonce),
        )
    finally:
        if transaction is not None and (root / transaction.marker_name).exists():
            _discard_publication_transaction(root, transaction, fault=fault)


def _actor_from_snapshot(snapshot: Any) -> dict[str, str]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != semaphore.MASTER_SCHEMA_VERSION:
        raise RenewalRefused("current master lease snapshot is unavailable")
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


@contextlib.contextmanager
def _composed_authority_transaction(
    renew: Renewal,
    lease_snapshot: LeaseSnapshot,
) -> Iterator[dict[str, Any]]:
    """Compatibility transaction for isolated callers with injected lease I/O."""
    _renew_or_refuse(renew)
    try:
        snapshot = lease_snapshot()
    except MasterRecordError:
        raise
    except Exception as exc:
        raise RenewalRefused(f"master lease snapshot failed: {exc}") from exc
    yield snapshot


class RecordWriter:
    """Renew-first private append transaction over one configured record root."""

    def __init__(
        self,
        root: Path,
        *,
        renew: Renewal = semaphore.master_renew,
        lease_snapshot: LeaseSnapshot = semaphore.master_snapshot,
        authority_transaction: Optional[AuthorityTransaction] = None,
        event_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = _normalized_root(root)
        self.renew = renew
        self.lease_snapshot = lease_snapshot
        if authority_transaction is not None:
            self.authority_transaction = authority_transaction
        elif lease_snapshot is semaphore.master_snapshot:
            self.authority_transaction = semaphore.master_authority_transaction
        else:
            self.authority_transaction = lambda: _composed_authority_transaction(
                self.renew,
                self.lease_snapshot,
            )
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
            try:
                with self.authority_transaction() as snapshot:
                    return self._append_authorized(
                        snapshot,
                        kind,
                        payload,
                        fault=fault,
                        once_per_acquisition=once_per_acquisition,
                    )
            except semaphore.MasterAuthorityRefused as exc:
                raise RenewalRefused(f"master renewal refused: {exc}") from exc

    def _append_authorized(
        self,
        snapshot: dict[str, Any],
        kind: str,
        payload: Mapping[str, Any],
        *,
        fault: Optional[FaultInjector],
        once_per_acquisition: bool,
    ) -> AppendResult:
        """Append while the caller retains both record and lease mutexes."""
        actor = _actor_from_snapshot(snapshot)
        view = _read_record_unlocked(self.root)
        if view.publication_transaction is not None:
            _discard_publication_transaction(
                self.root,
                view.publication_transaction,
            )
            view = _read_record_unlocked(self.root)
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
                        _publish_record_transaction(
                            self.root,
                            source_log=view.log_bytes,
                            target_log=view.log_bytes,
                            target_board=_canonical_json(view.expected_board),
                            operation="board",
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
        _publish_record_transaction(
            self.root,
            source_log=view.log_bytes,
            target_log=log_bytes,
            target_board=_canonical_json(board),
            operation="append",
            fault=fault,
        )
        return AppendResult(
            event=event,
            board=board,
            repaired_stale_board=not view.board_current,
            already_present=False,
        )
