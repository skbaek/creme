from __future__ import annotations

import fcntl
import json
import math
import os
import sys
import subprocess
import re
import signal
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Optional

from . import idle_workers
from .adapters import Adapter, get_adapter
from .profile import DEFAULT_RELATIVE_PROFILE, effective_policy, load as load_profile


SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 1800
MAX_LEASE_SECONDS = 14400
MANUAL_LABEL = "manual-macos-session"
MANUAL_GRACE_SECONDS = 300
ADAPTIVE_LEASE_SECONDS = 600
NEUTRAL_STATE_RELATIVE = Path(".semaphore/state")
ADMISSION_NOTE_PREFIX = "@creme-admission:"
ADMISSION_CONTENTION = {"tolerant", "sensitive", "exclusive"}
ADMISSION_PEAK_MULTIPLIER = 1.25
ADMISSION_RESERVE_FRACTION = 0.25
ADMISSION_MIN_RESERVE_GIB = 2.0
ADMISSION_DRAIN_PERCENT = 20
ADMISSION_CONTENTION_PERCENT = 30
QUEUE_NAME = "queue.json"
QUEUE_SCHEMA_VERSION = 1
WAIT_POLL_SECONDS = 3.0
WAITER_STALE_SECONDS = 15.0
MAX_WAIT_SECONDS = MAX_LEASE_SECONDS
IDLE_HOLD_SECONDS = 120
WAITER_KEYS = {
    "id", "label", "pid", "uid", "contention",
    "memory_gib", "enqueued_at", "heartbeat_at",
}
HOLD_KEYS = {
    "label", "pid", "uid", "note", "manual",
    "acquired_at", "renewed_at", "lease_seconds",
}


class SemaphoreError(Exception):
    pass


def _now() -> float:
    return time.time()


def _iso(timestamp: Optional[float] = None) -> str:
    return datetime.fromtimestamp(timestamp or _now(), timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_creme_root(module_root: Optional[Path] = None) -> Path:
    """Return the shared checkout root, including from a linked worktree."""
    root = (module_root or Path(__file__).resolve().parents[1]).resolve()
    git_marker = root / ".git"
    if git_marker.is_dir() or not git_marker.is_file():
        return root
    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
        prefix = "gitdir: "
        if not marker.startswith(prefix):
            raise ValueError("worktree .git file has an unexpected shape")
        git_dir = Path(marker[len(prefix):])
        if not git_dir.is_absolute():
            git_dir = git_marker.parent / git_dir
        common_reference = (git_dir.resolve() / "commondir").read_text(encoding="utf-8").strip()
        common_dir = Path(common_reference)
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        return common_dir.resolve().parent
    except (OSError, ValueError) as exc:
        raise SemaphoreError(f"cannot resolve canonical Creme root: {exc}")


def neutral_state_root() -> Path:
    return canonical_creme_root() / NEUTRAL_STATE_RELATIVE


def legacy_state_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "creme" / "host-semaphore"
    return Path.home() / ".local" / "state" / "creme" / "host-semaphore"


def _has_runtime_state(root: Path) -> bool:
    return any((root / name).exists() for name in ("state.json", "mutex", "log.jsonl"))


def _select_state_root(neutral: Path, legacy: Path) -> Path:
    if (neutral / "state.json").exists():
        return neutral
    if _has_runtime_state(legacy):
        return legacy
    return neutral


def state_root() -> Path:
    override = os.environ.get("CREME_SEMAPHORE_DIR")
    if override:
        return Path(override).expanduser()
    return _select_state_root(neutral_state_root(), legacy_state_root())


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "hard": None, "soft": []}


def _validate(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or set(state) != {"schema_version", "hard", "soft"}:
        raise SemaphoreError("state has an unexpected shape")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise SemaphoreError("state schema is unsupported")
    if state["hard"] is not None and not isinstance(state["hard"], dict):
        raise SemaphoreError("hard hold is malformed")
    if not isinstance(state["soft"], list) or not all(isinstance(item, dict) for item in state["soft"]):
        raise SemaphoreError("soft holds are malformed")
    holds = ([state["hard"]] if state["hard"] else []) + state["soft"]
    for hold in holds:
        if set(hold) != HOLD_KEYS:
            raise SemaphoreError("hold has an unexpected shape")
        if not isinstance(hold["label"], str) or not hold["label"]:
            raise SemaphoreError("hold label must be a non-empty string")
        if not isinstance(hold["note"], str):
            raise SemaphoreError("hold note must be a string")
        if not isinstance(hold["manual"], bool):
            raise SemaphoreError("hold manual flag must be boolean")
        if not isinstance(hold["pid"], int) or isinstance(hold["pid"], bool) or hold["pid"] < 1:
            raise SemaphoreError("hold pid must be a positive integer")
        if not isinstance(hold["uid"], int) or isinstance(hold["uid"], bool) or hold["uid"] < 0:
            raise SemaphoreError("hold uid must be a non-negative integer")
        for key in ("acquired_at", "renewed_at"):
            value = hold[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise SemaphoreError(f"hold {key} must be a positive finite number")
        lease = hold["lease_seconds"]
        if isinstance(lease, bool) or not isinstance(lease, int) or not 1 <= lease <= MAX_LEASE_SECONDS:
            raise SemaphoreError("hold lease_seconds is outside the supported range")
        if hold["renewed_at"] < hold["acquired_at"]:
            raise SemaphoreError("hold renewed_at predates acquired_at")
        if hold["manual"] != (hold["label"] == MANUAL_LABEL):
            raise SemaphoreError("manual hold flag and reserved label disagree")
    if state["hard"] and state["hard"]["manual"]:
        raise SemaphoreError("manual hold cannot be hard")
    labels = [hold["label"] for hold in holds]
    if len(labels) != len(set(labels)):
        raise SemaphoreError("hold labels must be unique non-empty strings")
    return state


@contextmanager
def _locked_root(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError as exc:
        raise SemaphoreError(f"cannot secure semaphore directory: {exc}")
    mutex = root / "mutex"
    with mutex.open("a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, SemaphoreError) as exc:
        raise SemaphoreError(f"refusing to replace corrupt semaphore state: {exc}")


@contextmanager
def locked_state() -> Iterator[tuple[Path, dict[str, Any]]]:
    root = state_root()
    override = os.environ.get("CREME_SEMAPHORE_DIR")
    legacy = legacy_state_root()
    if not override and root == legacy:
        # A migration may activate neutral state while this invocation waits on
        # the legacy mutex. Re-check under that mutex instead of writing a stale
        # post-migration update back into the retained legacy copy.
        with _locked_root(root):
            refreshed = state_root()
            if refreshed == root:
                path = root / "state.json"
                yield path, _load_state(path)
                return
        root = refreshed
    with _locked_root(root):
        path = root / "state.json"
        yield path, _load_state(path)


def _save(path: Path, state: dict[str, Any]) -> None:
    _validate(state)
    _write_json(path, state)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace ``path`` with ``data`` as private, pretty JSON."""
    fd, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".json", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _log_to(root: Path, action: str, label: str, verdict: str, detail: str) -> None:
    record = {
        "time": _iso(), "action": action, "label": label,
        "verdict": verdict, "detail": detail,
    }
    path = root / "log.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _log(action: str, label: str, verdict: str, detail: str) -> None:
    _log_to(state_root(), action, label, verdict, detail)


def log_path() -> Path:
    return state_root() / "log.jsonl"


def _valid_log_row(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and all(isinstance(row.get(key), str) for key in ("time", "action", "label", "verdict", "detail"))
        and row["verdict"] in {"OK", "REFUSED"}
    )


def read_log(
    since: datetime,
    until: Optional[datetime] = None,
) -> tuple[list[dict[str, Any]], int, str]:
    """Return coordination rows in the window, corrupt-line count, and a status.

    The audit log is append-only host state written by every client.  A
    malformed line is skipped and counted rather than raised: a roll-up that
    dies on one bad line cannot be used to compare a return watch against a
    baseline.
    """
    path = log_path()
    if not path.exists():
        return [], 0, "MISSING"
    rows: list[dict[str, Any]] = []
    corrupt = 0
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not _valid_log_row(row):
                        raise ValueError("unsupported row")
                    when = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        raise ValueError("naive timestamp")
                except (json.JSONDecodeError, TypeError, ValueError):
                    corrupt += 1
                    continue
                if when >= since and (until is None or when <= until):
                    rows.append({**row, "when": when})
    except OSError as exc:
        return [], corrupt, f"UNREADABLE: {exc}"
    rows.sort(key=lambda row: row["when"])
    return rows, corrupt, "OK"


def migrate_legacy_state(
    neutral_root: Optional[Path] = None,
    legacy_root: Optional[Path] = None,
) -> tuple[bool, str]:
    """Copy the legacy state into Creme without removing the legacy files."""
    if os.environ.get("CREME_SEMAPHORE_DIR") and neutral_root is None and legacy_root is None:
        return False, "state migration is unavailable while CREME_SEMAPHORE_DIR is set"
    neutral = (neutral_root or neutral_state_root()).expanduser().resolve()
    legacy = (legacy_root or legacy_state_root()).expanduser().resolve()
    if neutral == legacy:
        return False, "neutral and legacy state paths are identical"

    neutral_path = neutral / "state.json"
    if neutral_path.exists():
        with _locked_root(neutral):
            _load_state(neutral_path)
        return True, f"neutral state already active at {neutral}; legacy state retained at {legacy}"

    with _locked_root(legacy):
        with _locked_root(neutral):
            if neutral_path.exists():
                _load_state(neutral_path)
                return True, f"neutral state already active at {neutral}; legacy state retained at {legacy}"
            legacy_state = _load_state(legacy / "state.json")
            _save(neutral_path, legacy_state)

    detail = f"neutral state activated at {neutral}; legacy state retained at {legacy}"
    try:
        _log_to(neutral, "migrate-state", "host-semaphore", "OK", detail)
    except OSError:
        detail += "; audit log write failed"
    return True, detail


def queue_path(root: Optional[Path] = None) -> Path:
    return (root or state_root()) / QUEUE_NAME


def _empty_queue() -> dict[str, Any]:
    return {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "waiters": [],
        "activity": {},
        "workers": {},
    }


def _valid_waiter(entry: Any) -> bool:
    if not isinstance(entry, dict) or set(entry) != WAITER_KEYS:
        return False
    if not isinstance(entry["id"], str) or not entry["id"]:
        return False
    if not isinstance(entry["label"], str) or not entry["label"]:
        return False
    if entry["contention"] not in ADMISSION_CONTENTION:
        return False
    for key in ("pid", "uid", "memory_gib"):
        value = entry[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    for key in ("enqueued_at", "heartbeat_at"):
        value = entry[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value) or value <= 0:
            return False
    return entry["memory_gib"] >= 1 and entry["pid"] >= 1


def _load_queue(root: Path) -> tuple[dict[str, Any], list[str]]:
    """Read the waiting queue beside the holds, reporting what it had to drop.

    The queue is scheduling state, never a safety verdict: an unreadable or
    partly invalid queue degrades waiting to independent polling instead of
    blocking work or rewriting the hold state that governs safety.  Nothing is
    discarded silently — a wholly unreadable file is preserved under a
    ``.corrupt`` name and the drop is reported.
    """
    path = queue_path(root)
    notes: list[str] = []
    if not path.exists():
        return _empty_queue(), notes
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        preserved = path.with_suffix(f".corrupt.{int(_now())}.json")
        try:
            os.replace(path, preserved)
            notes.append(f"unreadable queue preserved at {preserved.name}: {exc}")
        except OSError:
            notes.append(f"queue is unreadable and could not be preserved: {exc}")
        return _empty_queue(), notes
    if not isinstance(raw, dict) or raw.get("schema_version") != QUEUE_SCHEMA_VERSION:
        notes.append("queue schema is unsupported; waiting proceeds without arrival order")
        return _empty_queue(), notes
    waiters = raw.get("waiters")
    kept = [entry for entry in waiters if _valid_waiter(entry)] if isinstance(waiters, list) else []
    dropped = (len(waiters) - len(kept)) if isinstance(waiters, list) else 0
    if dropped:
        notes.append(f"{dropped} malformed queue entr(y/ies) ignored")
    activity = raw.get("activity")
    if not isinstance(activity, dict):
        activity = {}
    clean_activity = {
        str(key): float(value)
        for key, value in activity.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    }
    workers = raw.get("workers")
    clean_workers = {
        str(key): value
        for key, value in (workers or {}).items()
        if isinstance(workers, dict) and idle_workers._valid_observation(value)
    }
    return {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "waiters": kept,
        "activity": clean_activity,
        "workers": clean_workers,
    }, notes


def _save_queue(root: Path, queue: dict[str, Any]) -> None:
    path = queue_path(root)
    fd, temporary = tempfile.mkstemp(prefix="queue-", suffix=".json", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(queue, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _pid_alive(pid: int) -> bool:
    """Report liveness without claiming a foreign or unreadable pid is gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _prune_waiters(
    waiters: list[dict[str, Any]], now: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    live = []
    dropped = []
    for entry in waiters:
        if (
            _pid_alive(int(entry["pid"]))
            and now - float(entry["heartbeat_at"]) <= WAITER_STALE_SECONDS
        ):
            live.append(entry)
        else:
            dropped.append(entry)
    return live, dropped


def _log_dropped_waiters(root: Path, dropped: list[dict[str, Any]], now: float) -> None:
    """One `wait-dropped` row per pruned waiter, so no enqueue is left open.

    Dropping is a decision about a queued request; without a row its enqueue
    would be indistinguishable in the roll-up from a wait that is still live.
    """
    for entry in dropped:
        waited = round(now - float(entry["enqueued_at"]), 1)
        try:
            _log_to(
                root, "wait-dropped", str(entry["label"]), "REFUSED",
                f"WAIT_DROPPED: the waiting process is gone or stopped heartbeating "
                f"(waited={waited}s); waiter={str(entry['id'])[:8]}; "
                f"memory={int(entry['memory_gib'])}GiB; contention={entry['contention']}",
            )
        except OSError:
            continue


def _ordered_waiters(waiters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(waiters, key=lambda entry: (float(entry["enqueued_at"]), str(entry["id"])))


def _encode_admission_note(note: str, memory_gib: int, contention: str) -> str:
    metadata = json.dumps(
        {"contention": contention, "memory_gib": memory_gib},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{ADMISSION_NOTE_PREFIX}{metadata} {note}"


def _decode_admission_note(
    note: str,
    default_memory_gib: int,
) -> tuple[str, int, str]:
    """Read additive admission metadata while preserving schema-v1 state.

    Holds are shared with sessions that may still be running the pre-admission
    launcher. Encoding the reservation in the existing free-form note keeps
    those sessions fail-closed and avoids an in-place state-schema cutover.
    """
    if not note.startswith(ADMISSION_NOTE_PREFIX):
        return note, default_memory_gib, "legacy"
    encoded, separator, user_note = note[len(ADMISSION_NOTE_PREFIX):].partition(" ")
    if not separator:
        return note, default_memory_gib, "legacy"
    try:
        metadata = json.loads(encoded)
    except json.JSONDecodeError:
        return note, default_memory_gib, "legacy"
    if not isinstance(metadata, dict) or set(metadata) != {"contention", "memory_gib"}:
        return note, default_memory_gib, "legacy"
    memory_gib = metadata.get("memory_gib")
    contention = metadata.get("contention")
    if (
        not isinstance(memory_gib, int)
        or isinstance(memory_gib, bool)
        or memory_gib < 1
        or contention not in ADMISSION_CONTENTION
    ):
        return note, default_memory_gib, "legacy"
    return user_note, memory_gib, str(contention)


def _runtime_admission_policy(adapter: Adapter) -> dict[str, Any]:
    checked = load_profile(
        canonical_creme_root() / DEFAULT_RELATIVE_PROFILE,
        adapter,
    )
    if checked.status in {"VALID", "LIMITED"} and checked.profile:
        values = effective_policy(checked.profile, adapter)
        facts = checked.profile.get("facts", {})
    else:
        # A missing, invalid, or stale profile must not silently recover the
        # more permissive OS-derived worker count.
        values = {"task_memory_gib": 2, "heavy_workers": 1, "light_workers": 1}
        facts_result = adapter.static_facts()
        facts = facts_result.data if facts_result.status == "OK" and facts_result.data else {}
    physical_bytes = facts.get("physical_memory_bytes")
    physical_gib = (
        float(physical_bytes) / (1024 ** 3)
        if isinstance(physical_bytes, int) and not isinstance(physical_bytes, bool)
        else None
    )
    return {
        **values,
        "physical_memory_gib": physical_gib,
        "profile_status": checked.status,
    }


def _reserve_gib(total_gib: Optional[float]) -> Optional[float]:
    if total_gib is None:
        return None
    return min(
        total_gib / 2,
        max(ADMISSION_MIN_RESERVE_GIB, total_gib * ADMISSION_RESERVE_FRACTION),
    )


def _charged_memory_gib(memory_gib: int) -> int:
    return max(1, math.ceil(memory_gib * ADMISSION_PEAK_MULTIPLIER))


def _headroom_values(
    sample: Any,
    configured_total_gib: Optional[float],
) -> tuple[Optional[int], Optional[float], Optional[float]]:
    if sample.status != "OK" or not sample.data:
        return None, None, configured_total_gib
    free = sample.data.get("memory_free_percent")
    if isinstance(free, bool) or not isinstance(free, (int, float)) or not 0 <= free <= 100:
        return None, None, configured_total_gib
    total_bytes = sample.data.get("physical_memory_bytes")
    total_gib = configured_total_gib
    if isinstance(total_bytes, int) and not isinstance(total_bytes, bool) and total_bytes > 0:
        total_gib = total_bytes / (1024 ** 3)
    available_bytes = sample.data.get("memory_available_bytes")
    if isinstance(available_bytes, int) and not isinstance(available_bytes, bool) and available_bytes >= 0:
        available_gib = available_bytes / (1024 ** 3)
    elif total_gib is not None:
        available_gib = total_gib * float(free) / 100
    else:
        available_gib = None
    return int(free), available_gib, total_gib


def fit_arithmetic(sample: Any, policy: dict[str, Any], memory_gib: int) -> dict[str, Any]:
    """The numbers behind the usability-reserve test, deciding nothing.

    `_admission_decision` stays the only producer of a verdict.  This exposes
    the arithmetic it applies so a waiter, and anyone reading `status`, can see
    why a request does or does not fit right now.
    """
    configured_total = policy.get("physical_memory_gib")
    if isinstance(configured_total, bool) or not isinstance(configured_total, (int, float)):
        configured_total = None
    free_percent, available_gib, total_gib = _headroom_values(sample, configured_total)
    reserve_gib = _reserve_gib(total_gib)
    charged_gib = _charged_memory_gib(memory_gib)
    needed_gib = charged_gib + reserve_gib if reserve_gib is not None else None
    chargeable = (
        available_gib - reserve_gib
        if available_gib is not None and reserve_gib is not None
        else None
    )
    return {
        "estimate_gib": memory_gib,
        "charged_gib": charged_gib,
        "reserve_gib": round(reserve_gib, 1) if reserve_gib is not None else None,
        "needed_gib": round(needed_gib, 1) if needed_gib is not None else None,
        "available_gib": round(available_gib, 2) if available_gib is not None else None,
        "free_percent": free_percent,
        "largest_fitting_estimate_gib": (
            max(0, int(chargeable / ADMISSION_PEAK_MULTIPLIER))
            if chargeable is not None
            else None
        ),
        "fits": (
            None
            if needed_gib is None or available_gib is None
            else available_gib >= needed_gib
        ),
    }


def fit_line(fit: dict[str, Any]) -> str:
    if fit["needed_gib"] is None or fit["available_gib"] is None:
        return (
            f"fit: estimate {fit['estimate_gib']} GiB charges {fit['charged_gib']} GiB "
            f"(x{ADMISSION_PEAK_MULTIPLIER}); memory headroom is unavailable, so heavy "
            "work serializes under a hard hold"
        )
    return (
        f"fit: estimate {fit['estimate_gib']} GiB -> charged {fit['charged_gib']} GiB "
        f"(x{ADMISSION_PEAK_MULTIPLIER}) + reserve {fit['reserve_gib']} GiB = "
        f"{fit['needed_gib']} GiB needed; {fit['available_gib']} GiB available now "
        f"({fit['free_percent']}% free) -> "
        + ("fits now" if fit["fits"] else "does not fit now")
    )


def _hold_reservation(hold: dict[str, Any], default_memory_gib: int) -> int:
    _, memory_gib, _ = _decode_admission_note(hold["note"], default_memory_gib)
    return _charged_memory_gib(memory_gib)


class Decision(NamedTuple):
    """One admission verdict plus whether waiting could still change it."""

    admitted: bool
    kind: Optional[str]
    verdict: str
    detail: str
    waitable: bool


def _refuse(verdict: str, detail: str, *, waitable: bool) -> Decision:
    return Decision(False, None, verdict, detail, waitable)


def _admission_decision(
    state: dict[str, Any],
    requested_kind: str,
    label: str,
    memory_gib: int,
    contention: str,
    adapter: Adapter,
    policy: dict[str, Any],
    sample: Any = None,
    idle_report: Optional[dict[str, Any]] = None,
) -> Decision:
    hard = state["hard"]
    matching_soft = [item for item in state["soft"] if item["label"] == label]
    other_soft = [item for item in state["soft"] if item["label"] != label]
    if hard and hard["label"] != label:
        detail = f"hard hold {hard['label']} blocks acquisition; run light work until it releases"
        return _refuse("DEFER_HEAVY", detail, waitable=True)
    if hard and hard["label"] == label:
        return _refuse(
            "ALREADY_HELD", "label already owns the hard hold; use renew", waitable=False
        )
    if matching_soft and requested_kind != "hard":
        return _refuse(
            "ALREADY_HELD", "label already owns a soft hold; use renew", waitable=False
        )
    if any(item.get("manual") for item in other_soft):
        # A human decides when a manual session hold ends.  Waiting on it would
        # turn an immediate, actionable refusal into a silent stall.
        return _refuse(
            "LIGHT_ONLY",
            "a manual human-session hold is active; run light work until it is released",
            waitable=False,
        )
    if requested_kind == "hard" and other_soft:
        labels = ", ".join(item["label"] for item in other_soft)
        return _refuse(
            "DEFER_FOR_HARD", f"soft holds block hard acquisition: {labels}", waitable=True
        )

    converting = requested_kind == "hard" and bool(matching_soft)
    # A queue pass evaluates every waiter against one sample so arrival order
    # is decided from a single view of the host, not a drifting one.
    if sample is None:
        sample = adapter.memory_headroom()
    configured_total = policy.get("physical_memory_gib")
    if isinstance(configured_total, bool) or not isinstance(configured_total, (int, float)):
        configured_total = None
    free_percent, available_gib, total_gib = _headroom_values(sample, configured_total)
    reserve_gib = _reserve_gib(total_gib)
    charged_gib = _charged_memory_gib(memory_gib)
    requires_hard = requested_kind == "hard" or contention != "tolerant"
    reasons = []

    if not converting and free_percent is not None and free_percent < ADMISSION_DRAIN_PERCENT:
        return _refuse(
            "LIGHT_ONLY",
            f"available memory is {free_percent}% (<{ADMISSION_DRAIN_PERCENT}%); "
            "do not start heavy work; checkpoint or wind down heavy sessions and run light work",
            waitable=False,
        )

    if not converting and free_percent is None:
        requires_hard = True
        reasons.append(f"memory headroom unavailable ({sample.detail}); limited mode serializes heavy work")

    if not converting and reserve_gib is not None:
        capacity_gib = max(0.0, float(total_gib) - reserve_gib)
        if charged_gib > capacity_gib:
            # No amount of waiting shrinks the request below the host budget.
            return _refuse(
                "LIGHT_ONLY",
                f"{memory_gib} GiB estimate charges {charged_gib} GiB with peak margin, "
                f"exceeding the {capacity_gib:.1f} GiB heavy-work budget; split or reduce the task",
                waitable=False,
            )
        if available_gib is not None and available_gib < charged_gib + reserve_gib:
            decision = "DEFER_FOR_HARD" if other_soft else "LIGHT_ONLY"
            action = (
                "wait for current heavy holds to wind down, then acquire hard"
                if other_soft
                else "wait for memory to recover and run light work"
            )
            return _refuse(
                decision,
                f"{available_gib:.1f} GiB is available but this task needs {charged_gib} GiB "
                f"plus a {reserve_gib:.1f} GiB usability reserve; {action}"
                + _reclaimable_note(idle_report),
                waitable=True,
            )

        active_reservations = sum(
            _hold_reservation(item, int(policy["task_memory_gib"]))
            for item in other_soft
            if not item.get("manual")
        )
        if active_reservations + charged_gib > capacity_gib:
            requires_hard = True
            reasons.append(
                f"parallel peak reservations would charge {active_reservations + charged_gib} GiB "
                f"against a {capacity_gib:.1f} GiB budget"
            )

    active_workers = sum(not item.get("manual") for item in other_soft)
    if active_workers >= int(policy["heavy_workers"]):
        requires_hard = True
        reasons.append(
            f"{active_workers} soft heavy worker(s) already meet the configured limit "
            f"of {policy['heavy_workers']}"
        )

    if contention != "tolerant":
        reasons.append(f"contention={contention} requires host exclusivity")

    if requested_kind == "soft" and requires_hard:
        decision = "DEFER_FOR_HARD" if other_soft else "USE_HARD"
        action = (
            "run light work until the other holds release, then acquire hard"
            if other_soft
            else "use hard acquisition for this task"
        )
        return _refuse(decision, "; ".join(reasons + [action]), waitable=True)

    selected_kind = "hard" if requires_hard else "soft"
    if selected_kind == "hard" and other_soft:
        labels = ", ".join(item["label"] for item in other_soft)
        return _refuse(
            "DEFER_FOR_HARD",
            "; ".join(reasons + [f"soft holds still active: {labels}; run light work first"]),
            waitable=True,
        )

    headroom = (
        f"headroom={free_percent}%" if free_percent is not None else "headroom=unavailable"
    )
    rationale = "; ".join(reasons) if reasons else "parallel admission fits the live and reserved budgets"
    return Decision(
        True,
        selected_kind,
        f"ADMITTED_{selected_kind.upper()}",
        f"{headroom}; memory={memory_gib} GiB (charged={charged_gib} GiB); {rationale}",
        True,
    )


def _reclaimable_note(report: Optional[dict[str, Any]]) -> str:
    """Name reclaimable language-server memory instead of refusing blind."""
    idle = (report or {}).get("idle_workers") or []
    if not idle:
        return ""
    owners = ", ".join((report or {}).get("owners") or []) or "unattributed"
    return (
        f"; {(report or {}).get('idle_rss_gib')} GiB sits in {len(idle)} idle "
        f"lean --worker process(es) owned by {owners} — that owner can free it with "
        "`python3 -m creme reclaim --idle-workers MIN`"
    )


def _hold(
    label: str,
    note: str,
    lease: int,
    manual: bool = False,
    *,
    memory_gib: Optional[int] = None,
    contention: str = "tolerant",
) -> dict[str, Any]:
    now = _now()
    return {
        "label": label,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "note": (
            note
            if manual or memory_gib is None
            else _encode_admission_note(note, memory_gib, contention)
        ),
        "manual": manual,
        "acquired_at": now,
        "renewed_at": now,
        "lease_seconds": lease,
    }


def _expired(hold: dict[str, Any], now: Optional[float] = None) -> bool:
    return (now or _now()) > float(hold["renewed_at"]) + int(hold["lease_seconds"])


def _is_elaborating(command: str) -> bool:
    first = command.split(None, 1)[0] if command else ""
    return os.path.basename(first) in {"lake", "lean"} or "lean" in first


class HostView(NamedTuple):
    """One process and working-directory sample shared by a refresh pass.

    Attribution has to run for every hold on every ``status`` and ``renew``,
    so the expensive parts — the process table and one ``lsof``/procfs sample
    bounded to the host's ``lake``/``lean`` pids — are taken once and reused.
    """

    table: Optional[dict[int, tuple[int, str]]]
    elaborating: tuple[int, ...]
    cwds: Optional[dict[int, str]]
    unattributed: tuple[int, ...]


def _host_view(adapter: Adapter) -> HostView:
    result = adapter.process_snapshot()
    if result.status != "OK" or not isinstance(result.data, dict):
        return HostView(None, (), None, ())
    rows = result.data.get("processes")
    if not isinstance(rows, list):
        return HostView(None, (), None, ())
    table: dict[int, tuple[int, str]] = {}
    for row in rows:
        try:
            table[int(row["pid"])] = (int(row["ppid"]), str(row["command"]))
        except (KeyError, TypeError, ValueError):
            continue
    elaborating = tuple(
        sorted(pid for pid, (_, command) in table.items() if _is_elaborating(command))
    )
    if not elaborating:
        return HostView(table, (), {}, ())
    sample = adapter.process_working_directories(list(elaborating))
    if sample.status != "OK" or not isinstance(sample.data, dict):
        return HostView(table, elaborating, None, elaborating)
    raw = sample.data.get("working_directories")
    cwds: dict[int, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                cwds[int(key)] = str(value)
            except (TypeError, ValueError):
                continue
    unattributed = tuple(pid for pid in elaborating if pid not in cwds)
    return HostView(table, elaborating, cwds, unattributed)


def _descendants(table: dict[int, tuple[int, str]], pid: int) -> set[int]:
    found = {pid}
    changed = True
    while changed:
        changed = False
        for child, (parent, _) in table.items():
            if parent in found and child not in found:
                found.add(child)
                changed = True
    found.discard(pid)
    return found


def _path_under(candidate: str, roots: tuple[Path, ...]) -> bool:
    current = Path(os.path.normpath(candidate))
    for root in roots:
        try:
            current.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _hold_is_working(
    hold: dict[str, Any],
    adapter: Adapter,
    scope_roots: Optional[tuple[Path, ...]] = (),
    view: Optional[HostView] = None,
) -> Optional[bool]:
    """Is this holder's gate elaborating right now, whoever started it?

    A hold taken by one shell call is not the parent of the gate launched by
    the next, so descent from the acquiring pid answers only the easy case.
    The truthful test adds the one the reclaim path already trusts: a
    ``lake``/``lean`` process whose working directory lies inside the goal's
    own worktrees is that goal's work.  ``None`` means attribution could not
    be completed, and an uninspectable holder is never reported idle.
    """
    sampled = view if view is not None else _host_view(adapter)
    if sampled.table is None:
        return None
    roots = [str(root) for root in (scope_roots or ())]
    for pid in _descendants(sampled.table, int(hold["pid"])):
        command = sampled.table[pid][1]
        if _is_elaborating(command) or any(root in command for root in roots):
            return True
    if not sampled.elaborating:
        return False
    if scope_roots is None:
        # The goal's boundary itself is unreadable, so a Lean process on this
        # host can be neither claimed nor excluded.
        return None
    if sampled.cwds is None or sampled.unattributed:
        return None
    if scope_roots:
        for pid in sampled.elaborating:
            if _path_under(sampled.cwds[pid], scope_roots):
                return True
    return False


def _goal_scope_roots(label: str, adapter: Adapter) -> Optional[tuple[Path, ...]]:
    """The goal's worktrees, ``()`` when it has none, ``None`` when unreadable."""
    from .task_wind_down import (
        NoGoalWorktreeError,
        WorktreeScopeError,
        _goal_worktree_roots,
    )

    try:
        return _goal_worktree_roots(label, adapter)
    except NoGoalWorktreeError:
        return ()
    except (OSError, WorktreeScopeError):
        return None


def _refresh_signals(
    root: Path,
    state: dict[str, Any],
    adapter: Adapter,
    now: float,
) -> dict[str, Any]:
    """Update idleness observations for holds and workers, and derive signals."""
    queue, _notes = _load_queue(root)
    activity = dict(queue.get("activity") or {})
    holds = ([state["hard"]] if state["hard"] else []) + state["soft"]
    labels = {hold["label"] for hold in holds}
    hold_signals: dict[str, dict[str, Any]] = {}
    view = _host_view(adapter) if any(not hold.get("manual") for hold in holds) else None
    for hold in holds:
        label = hold["label"]
        scope: Optional[tuple[Path, ...]] = ()
        if hold.get("manual"):
            working: Optional[bool] = None
        else:
            scope = _goal_scope_roots(label, adapter)
            working = _hold_is_working(hold, adapter, scope, view)
        alive = _pid_alive(int(hold["pid"]))
        if working:
            activity.pop(label, None)
        elif working is False:
            activity.setdefault(label, now)
        idle_since = activity.get(label)
        hold_signals[label] = {
            "pid_alive": alive,
            "working": working,
            "manual": bool(hold.get("manual")),
            "scope_roots": None if scope is None else [str(root) for root in scope],
            "unattributed_pids": list((view.unattributed if view else ()) or ()),
            "idle_seconds": (now - float(idle_since)) if idle_since is not None else None,
            # Idleness is only ever claimed from a pass that could attribute
            # the host's Lean work; an uninspectable pass suspends the signal
            # without discarding the streak it had already measured.
            "idle_hold": (
                working is False
                and idle_since is not None
                and now - float(idle_since) > IDLE_HOLD_SECONDS
            ),
            # A hold acquired from the command line normally outlives the
            # process that took it, so a gone pid is not by itself a fault.
            # Stranded means gone pid, no Lean work in the owned tree, and a
            # lease nobody is renewing: nothing will ever release it.
            "stranded": alive is False and working is False and _expired(hold, now),
        }
    queue["activity"] = {
        label: value for label, value in activity.items() if label in labels
    }

    sample = adapter.lean_workers()
    if sample.status == "OK" and isinstance(sample.data, dict):
        workers = [
            worker for worker in (sample.data.get("workers") or [])
            if isinstance(worker, dict)
        ]
        previous = queue.get("workers")
        observations, derived = idle_workers.update_observations(
            workers, previous if isinstance(previous, dict) else {}, now
        )
        queue["workers"] = observations
        hold_pids = {int(hold["pid"]): hold["label"] for hold in holds}
        client_pattern = getattr(adapter, "client_pattern", _NEVER_MATCHES)
        worker_report = {
            "status": "OK",
            "workers": [
                {
                    "pid": int(worker["pid"]),
                    "rss_gib": round(int(worker["rss_kib"]) / (1024 ** 2), 2),
                    "idle_seconds": (derived.get(int(worker["pid"])) or {}).get("idle_seconds"),
                    "cpu_percent": (derived.get(int(worker["pid"])) or {}).get("cpu_percent"),
                    "owner": idle_workers.owner_label(worker, hold_pids, client_pattern),
                }
                for worker in workers
            ],
            "detail": sample.detail,
        }
    else:
        queue.setdefault("workers", {})
        worker_report = {"status": sample.status, "workers": [], "detail": sample.detail}

    idle = [
        worker for worker in worker_report["workers"]
        if worker["idle_seconds"] is not None
    ]
    worker_report["idle_workers"] = idle
    worker_report["idle_rss_gib"] = round(sum(worker["rss_gib"] for worker in idle), 2)
    worker_report["owners"] = sorted({worker["owner"] for worker in idle})
    try:
        _save_queue(root, queue)
    except OSError:
        pass
    return {"holds": hold_signals, "lean_workers": worker_report}


class _NeverMatches:
    @staticmethod
    def search(_text: str) -> None:
        return None


_NEVER_MATCHES = _NeverMatches()


def refresh_signals(adapter: Optional[Adapter] = None) -> dict[str, Any]:
    selected = adapter or get_adapter()
    with locked_state() as (path, state):
        return _refresh_signals(path.parent, state, selected, _now())


def _idle_worker_line(report: dict[str, Any]) -> Optional[str]:
    idle = report.get("idle_workers") or []
    if not idle:
        return None
    owners = ", ".join(report.get("owners") or []) or "unattributed"
    return (
        f"IDLE_WORKERS: {report['idle_rss_gib']} GiB across {len(idle)} idle "
        f"lean --worker process(es) (owner {owners}); "
        "reclaim your own with `python3 -m creme reclaim --idle-workers MIN`"
    )


def snapshot() -> dict[str, Any]:
    with locked_state() as (_, state):
        return json.loads(json.dumps(state))


def _attribution_text(signal: dict[str, Any]) -> str:
    """Name what idleness looked for, so the reader can check the claim."""
    roots = signal.get("scope_roots")
    if roots is None:
        return "no lake or lean child process, and this goal's worktree scope is unreadable"
    if roots:
        listed = ", ".join(roots)
        return (
            "no lake or lean process among this hold's children or working in "
            f"{listed}"
        )
    return (
        "no lake or lean child process, and this goal has no Jaune/Blanc worktree "
        "for one to work in"
    )


def _signal_lines(label: str, signals: dict[str, dict[str, Any]], indent: str) -> list[str]:
    signal = signals.get(label) or {}
    lines = []
    if signal.get("stranded"):
        lines.append(
            f"{indent}STRANDED: the holding process is gone and {_attribution_text(signal)}; "
            f"run `python3 -m creme reclaim --wind-down {label}`"
        )
    elif signal.get("idle_hold"):
        seconds = int(signal.get("idle_seconds") or 0)
        lines.append(
            f"{indent}IDLE_HOLD: {_attribution_text(signal)} for {seconds}s; "
            "release between gates and reacquire with --wait for the next elaborating command"
        )
    elif signal.get("working") is None and not signal.get("manual"):
        unattributed = signal.get("unattributed_pids") or []
        if unattributed or signal.get("scope_roots") is None:
            detail = (
                f"{len(unattributed)} lake/lean process(es) could not be placed"
                if unattributed
                else "this goal's worktree scope is unreadable"
            )
            lines.append(
                f"{indent}ATTRIBUTION_UNAVAILABLE: {detail}; this hold is not reported idle"
            )
    return lines


def status_text(adapter: Optional[Adapter] = None) -> str:
    selected = adapter or get_adapter()
    now = _now()
    with locked_state() as (path, state):
        root = path.parent
        state = json.loads(json.dumps(state))
        derived = _refresh_signals(root, state, selected, now)
        master_lines = _master_lines(root, now)
        signals = derived["holds"]
        worker_report = derived["lean_workers"]
        queue, queue_notes = _load_queue(root)
        waiters, dropped = _prune_waiters(queue["waiters"], now)
        if dropped:
            queue["waiters"] = waiters
            _log_dropped_waiters(root, dropped, now)
            try:
                _save_queue(root, queue)
            except OSError:
                pass
        try:
            policy = _runtime_admission_policy(selected)
        except (KeyError, OSError, SemaphoreError, ValueError):
            policy = None
        would: dict[str, tuple[Decision, dict[str, Any]]] = {}
        if waiters and policy is not None:
            # One sample for every waiter, and the same decision function the
            # queue uses, so the printed verdict is the live one rather than a
            # second implementation that could drift from it.
            sample = selected.memory_headroom()
            for waiter in waiters:
                decision = _admission_decision(
                    state, "adaptive", str(waiter["label"]), int(waiter["memory_gib"]),
                    str(waiter["contention"]), selected, policy, sample,
                    idle_report=worker_report,
                )
                would[str(waiter["id"])] = (
                    decision,
                    fit_arithmetic(sample, policy, int(waiter["memory_gib"])),
                )
    try:
        default_memory_gib = int((policy or {})["task_memory_gib"])
    except (KeyError, TypeError, ValueError):
        default_memory_gib = 2
    lines = list(master_lines)
    hard = state["hard"]
    if hard:
        state_word = "expired-blocking" if _expired(hard, now) else "live"
        note, memory_gib, contention = _decode_admission_note(hard["note"], default_memory_gib)
        lines.append(
            f"hard: {hard['label']} ({state_word}) pid={hard['pid']} "
            f"memory={memory_gib}GiB contention={contention} "
            f"held={int(now - float(hard['acquired_at']))}s note={note!r}"
        )
        lines.extend(_signal_lines(hard["label"], signals, "  "))
    else:
        lines.append("hard: free")
    lines.append(f"soft (S={len(state['soft'])}):")
    for hold in state["soft"]:
        state_word = "expired-blocking" if _expired(hold, now) else "live"
        note, memory_gib, contention = _decode_admission_note(hold["note"], default_memory_gib)
        lines.append(
            f"  {hold['label']} ({state_word}) pid={hold['pid']} "
            f"memory={memory_gib}GiB contention={contention} "
            f"held={int(now - float(hold['acquired_at']))}s note={note!r}"
        )
        lines.extend(_signal_lines(hold["label"], signals, "    "))
    if not state["soft"]:
        lines.append("  none")
    lines.append(f"waiting (W={len(waiters)}):")
    for index, waiter in enumerate(_ordered_waiters(waiters), start=1):
        lines.append(
            f"  {index}. {waiter['label']} pid={waiter['pid']} "
            f"memory={waiter['memory_gib']}GiB contention={waiter['contention']} "
            f"waited={int(now - float(waiter['enqueued_at']))}s"
        )
        verdict = would.get(str(waiter["id"]))
        if verdict is not None:
            decision, fit = verdict
            lines.append(f"     would: {decision.verdict} — {decision.detail}")
            lines.append("     " + fit_line(fit))
    if not waiters:
        lines.append("  none")
    lines.extend(f"queue: {note}" for note in queue_notes)
    idle_line = _idle_worker_line(worker_report)
    if idle_line:
        lines.append(idle_line)
    return "\n".join(lines)


def _admit(
    requested_kind: str,
    label: str,
    note: str,
    lease: int,
    *,
    memory_gib: Optional[int],
    contention: str,
    adapter: Optional[Adapter],
    policy: Optional[dict[str, Any]],
) -> tuple[bool, str]:
    invalid, selected, selected_policy, requested_memory = _validate_request(
        label, lease, contention, memory_gib, adapter, policy
    )
    if invalid:
        return False, invalid
    with locked_state() as (path, state):
        signals = _refresh_signals(path.parent, state, selected, _now())
        admitted, selected_kind, decision, detail, _ = _admission_decision(
            state,
            requested_kind,
            label,
            requested_memory,
            contention,
            selected,
            selected_policy,
            idle_report=signals["lean_workers"],
        )
        if not admitted or selected_kind is None:
            _log(f"{requested_kind}-acquire", label, "REFUSED", f"{decision}: {detail}")
            return False, f"{decision} — {detail}"
        if selected_kind == "soft":
            state["soft"].append(
                _hold(
                    label,
                    note,
                    lease,
                    memory_gib=requested_memory,
                    contention=contention,
                )
            )
        else:
            state["soft"] = []
            state["hard"] = _hold(
                label,
                note,
                lease,
                memory_gib=requested_memory,
                contention=contention,
            )
        _save(path, state)
    _log(f"{requested_kind}-acquire", label, "OK", f"{decision}: {detail}")
    return True, f"{decision} — {detail}"


def acquire(
    kind: str,
    label: str,
    note: str,
    lease: int = DEFAULT_LEASE_SECONDS,
    *,
    memory_gib: Optional[int] = None,
    adapter: Optional[Adapter] = None,
    policy: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    if kind not in {"soft", "hard"}:
        return False, f"unknown hold kind: {kind}"
    return _admit(
        kind,
        label,
        note,
        lease,
        memory_gib=memory_gib,
        contention="exclusive" if kind == "hard" else "tolerant",
        adapter=adapter,
        policy=policy,
    )


def _validate_request(
    label: str,
    lease: int,
    contention: str,
    memory_gib: Optional[int],
    adapter: Optional[Adapter],
    policy: Optional[dict[str, Any]],
) -> tuple[Optional[str], Adapter, dict[str, Any], int]:
    if not label or label == MANUAL_LABEL:
        return "reserved or empty label", get_adapter(), {}, 0
    if lease < 1 or lease > MAX_LEASE_SECONDS:
        return f"lease must be 1..{MAX_LEASE_SECONDS} seconds", get_adapter(), {}, 0
    if contention not in ADMISSION_CONTENTION:
        return f"unknown contention class: {contention}", get_adapter(), {}, 0
    selected = adapter or get_adapter()
    selected_policy = policy or _runtime_admission_policy(selected)
    requested = memory_gib if memory_gib is not None else int(selected_policy["task_memory_gib"])
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        return (
            "memory estimate must be a positive whole number of GiB",
            selected,
            selected_policy,
            0,
        )
    return None, selected, selected_policy, requested


class _WaitCancelled(BaseException):
    """A signal ended the wait before the queue decided it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _dominant(tally: dict[str, dict[str, float]]) -> Optional[str]:
    if not tally:
        return None
    return max(
        tally,
        key=lambda verdict: (tally[verdict]["seconds"], tally[verdict]["passes"], verdict),
    )


def _wait_summary(
    tally: dict[str, dict[str, float]],
    tightest: Optional[tuple[float, float]],
) -> str:
    """Say which verdict actually held the request, not which one closed it."""
    dominant = _dominant(tally)
    if dominant is None:
        return "no queue pass completed"
    passes = int(sum(record["passes"] for record in tally.values()))
    breakdown = ", ".join(
        f"{verdict} {int(record['passes'])}"
        for verdict, record in sorted(tally.items(), key=lambda item: -item[1]["passes"])
    )
    summary = (
        f"dominant verdict {dominant} over {int(tally[dominant]['passes'])} pass(es) "
        f"and {tally[dominant]['seconds']:.1f}s of {passes} pass(es) ({breakdown})"
    )
    if tightest is not None:
        summary += (
            f"; tightest headroom margin: needed {tightest[0]:.1f} GiB; "
            f"most available {tightest[1]:.2f} GiB"
        )
    return summary


def _waiting_admit(
    label: str,
    note: str,
    lease: int,
    *,
    memory_gib: Optional[int],
    contention: str,
    adapter: Optional[Adapter],
    policy: Optional[dict[str, Any]],
    wait_seconds: int,
    poll_seconds: float = WAIT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    announce: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """Enqueue one request and return only when it is decided.

    Waiting can postpone a request; it can never admit one past a floor. Every
    pass re-evaluates the live decision under the mutex, so the drain
    threshold, the usability reserve, and manual human holds behave exactly as
    they do without ``--wait``.  Only the queue decides *which* fitting waiter
    goes first, and only one log row is written per enqueue and per outcome.

    Every pass is also *observed*: the verdict it produced and the seconds it
    held are tallied so a timeout can report what actually kept the request
    out, and a cancelled wait still writes its one outcome row.
    """
    invalid, selected, selected_policy, requested_memory = _validate_request(
        label, lease, contention, memory_gib, adapter, policy
    )
    if invalid:
        return False, invalid
    if wait_seconds < 1 or wait_seconds > MAX_WAIT_SECONDS:
        return False, f"--wait must be 1..{MAX_WAIT_SECONDS} seconds"

    waiter_id = uuid.uuid4().hex
    holder: Optional[str] = None
    enqueued_at = _now()
    deadline = enqueued_at + wait_seconds
    announced = False
    registered = False
    decided = False
    tally: dict[str, dict[str, float]] = {}
    tightest: Optional[tuple[float, float]] = None
    cancelled: Optional[BaseException] = None

    if announce is not None:
        fit = fit_arithmetic(selected.memory_headroom(), selected_policy, requested_memory)
        announce(fit_line(fit))
        if fit["fits"] is False and fit["largest_fitting_estimate_gib"] is not None:
            announce(
                f"fit: at this instant an estimate of at most "
                f"{fit['largest_fitting_estimate_gib']} GiB would fit; a larger one is "
                "queued in arrival order but passed over by every request that fits"
            )
        default_gib = int(selected_policy["task_memory_gib"])
        if memory_gib is not None and memory_gib > default_gib:
            announce(
                f"fit: an explicit --memory-gib {memory_gib} exceeds this host's default "
                f"estimate of {default_gib} GiB and is charged "
                f"{_charged_memory_gib(memory_gib)} GiB; state it only when you know the "
                "build is cold or broad"
            )

    def entry(now: float) -> dict[str, Any]:
        return {
            "id": waiter_id,
            "label": label,
            "pid": os.getpid(),
            "uid": os.getuid(),
            "contention": contention,
            "memory_gib": requested_memory,
            "enqueued_at": enqueued_at,
            "heartbeat_at": now,
        }

    def deregister() -> None:
        if not registered:
            return
        try:
            with locked_state() as (path, _):
                _drop_waiter(path.parent, waiter_id)
        except (OSError, SemaphoreError):
            # A stale entry is dropped by the next pass's liveness pruning.
            pass

    def on_signal(signum: int, _frame: Any) -> None:
        raise _WaitCancelled(signum)

    prior_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGHUP):
            try:
                prior_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, on_signal)
            except (OSError, ValueError):
                prior_handlers.pop(signum, None)

    try:
        while True:
            with locked_state() as (path, state):
                root = path.parent
                queue, notes = _load_queue(root)
                now = _now()
                queue["waiters"], dropped = _prune_waiters(queue["waiters"], now)
                _log_dropped_waiters(root, dropped, now)
                queue["waiters"] = [
                    item for item in queue["waiters"] if item["id"] != waiter_id
                ]
                queue["waiters"].append(entry(now))
                registered = True
                _save_queue(root, queue)

                sample = selected.memory_headroom()
                signals = _refresh_signals(root, state, selected, now)
                decisions = {
                    item["id"]: _admission_decision(
                        state, "adaptive", item["label"], int(item["memory_gib"]),
                        str(item["contention"]), selected, selected_policy, sample,
                        idle_report=signals["lean_workers"],
                    )
                    for item in _ordered_waiters(queue["waiters"])
                }
                winner = next(
                    (
                        item["id"]
                        for item in _ordered_waiters(queue["waiters"])
                        if decisions[item["id"]].admitted
                    ),
                    None,
                )
                mine = decisions[waiter_id]
                hard = state["hard"]
                if hard:
                    # Enough to size a requeue: which class is ahead, and how
                    # long it has been running. No note text.
                    _note, _gib, holder_class = _decode_admission_note(
                        hard["note"], int(selected_policy["task_memory_gib"])
                    )
                    holder = (
                        f"{hard['label']} contention={holder_class} "
                        f"held={int(now - float(hard['acquired_at']))}s"
                    )
                else:
                    holder = None
                record = tally.setdefault(mine.verdict, {"passes": 0.0, "seconds": 0.0})
                record["passes"] += 1
                if not mine.admitted:
                    fit = fit_arithmetic(sample, selected_policy, requested_memory)
                    if (
                        fit["fits"] is False
                        and fit["needed_gib"] is not None
                        and fit["available_gib"] is not None
                        and (tightest is None or fit["available_gib"] > tightest[1])
                    ):
                        tightest = (float(fit["needed_gib"]), float(fit["available_gib"]))

                if not announced:
                    detail = "; ".join(
                        [
                            f"position={_position(queue['waiters'], waiter_id)}",
                            f"waiter={waiter_id[:8]}",
                            f"memory={requested_memory}GiB",
                            f"contention={contention}",
                            *notes,
                        ]
                    )
                    _log_to(root, "wait-enqueue", label, "OK", detail)
                    announced = True

                if mine.admitted and winner == waiter_id:
                    passed = [
                        f"{item['label']}({decisions[item['id']].verdict})"
                        for item in _ordered_waiters(queue["waiters"])
                        if item["id"] != waiter_id
                        and float(item["enqueued_at"]) < enqueued_at
                    ]
                    if mine.kind == "soft":
                        state["soft"].append(_hold(
                            label, note, lease,
                            memory_gib=requested_memory, contention=contention,
                        ))
                    else:
                        state["soft"] = []
                        state["hard"] = _hold(
                            label, note, lease,
                            memory_gib=requested_memory, contention=contention,
                        )
                    _drop_waiter(root, waiter_id)
                    _save(path, state)
                    registered = False
                    decided = True
                    waited = round(_now() - enqueued_at, 1)
                    passed_note = (
                        f"; passed {len(passed)} older waiter(s): " + ", ".join(passed)
                        if passed else ""
                    )
                    _log_to(
                        root, "wait-acquire", label, "OK",
                        f"{mine.verdict}: waited={waited}s; waiter={waiter_id[:8]}; "
                        f"{mine.detail}{passed_note}",
                    )
                    return True, (
                        f"{mine.verdict} — waited {waited}s; {mine.detail}{passed_note}"
                    )

                if not mine.admitted and not mine.waitable:
                    _drop_waiter(root, waiter_id)
                    registered = False
                    decided = True
                    _log_to(
                        root, "wait-acquire", label, "REFUSED",
                        f"{mine.verdict}: waiting cannot change this verdict; "
                        f"waiter={waiter_id[:8]}; {mine.detail}",
                    )
                    return False, f"{mine.verdict} — {mine.detail}"

            remaining = deadline - _now()
            if remaining <= 0:
                decided = True
                waited = round(_now() - enqueued_at, 1)
                detail = (
                    f"WAIT_TIMEOUT: no admission within {wait_seconds}s "
                    f"(waited={waited}s); waiter={waiter_id[:8]}; "
                    f"{_wait_summary(tally, tightest)}"
                    + (f"; holder at timeout: {holder}" if holder else "")
                    + f"; last verdict {mine.verdict}: {mine.detail}"
                )
                _log("wait-acquire", label, "REFUSED", detail)
                return False, f"WAIT_TIMEOUT — {detail.split(': ', 1)[1]}"
            slept_from = _now()
            sleep(min(poll_seconds, remaining))
            tally[mine.verdict]["seconds"] += _now() - slept_from
    except (_WaitCancelled, KeyboardInterrupt) as exc:
        cancelled = exc
    finally:
        for signum, handler in prior_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
        if not decided and announced:
            waited = round(_now() - enqueued_at, 1)
            try:
                _log(
                    "wait-acquire", label, "REFUSED",
                    f"WAIT_CANCELLED: the waiting process ended before the queue decided "
                    f"(waited={waited}s); waiter={waiter_id[:8]}; "
                    f"{_wait_summary(tally, tightest)}",
                )
            except OSError:
                pass
        deregister()
    if isinstance(cancelled, _WaitCancelled):
        signal.signal(cancelled.signum, signal.SIG_DFL)
        signal.raise_signal(cancelled.signum)
    raise cancelled if cancelled is not None else SemaphoreError("wait ended without a decision")


def _drop_waiter(root: Path, waiter_id: str) -> None:
    """Remove one entry from the freshest queue, preserving other updates."""
    queue, _notes = _load_queue(root)
    queue["waiters"] = [item for item in queue["waiters"] if item["id"] != waiter_id]
    _save_queue(root, queue)


def _position(waiters: list[dict[str, Any]], waiter_id: str) -> int:
    ordered = _ordered_waiters(waiters)
    return next(
        (index + 1 for index, item in enumerate(ordered) if item["id"] == waiter_id),
        len(ordered),
    )


def adaptive_acquire(
    label: str,
    note: str,
    lease: int = ADAPTIVE_LEASE_SECONDS,
    *,
    memory_gib: Optional[int] = None,
    contention: str = "tolerant",
    adapter: Optional[Adapter] = None,
    policy: Optional[dict[str, Any]] = None,
    wait_seconds: Optional[int] = None,
    poll_seconds: float = WAIT_POLL_SECONDS,
    announce: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    if wait_seconds is not None:
        return _waiting_admit(
            label,
            note,
            lease,
            memory_gib=memory_gib,
            contention=contention,
            adapter=adapter,
            policy=policy,
            wait_seconds=wait_seconds,
            poll_seconds=poll_seconds,
            announce=announce,
        )
    return _admit(
        "adaptive",
        label,
        note,
        lease,
        memory_gib=memory_gib,
        contention=contention,
        adapter=adapter,
        policy=policy,
    )


def release(kind: str, label: str) -> tuple[bool, str]:
    if label == MANUAL_LABEL:
        return False, "manual hold requires manual-release"
    with locked_state() as (path, state):
        if kind == "hard":
            if not state["hard"] or state["hard"]["label"] != label:
                return False, "matching hard hold not found"
            state["hard"] = None
        elif kind == "soft":
            before = len(state["soft"])
            state["soft"] = [item for item in state["soft"] if item["label"] != label]
            if len(state["soft"]) == before:
                return False, "matching soft hold not found"
        else:
            return False, f"unknown hold kind: {kind}"
        _save(path, state)
    _log(f"{kind}-release", label, "OK", "released")
    return True, f"{kind} hold released"


def adaptive_release(label: str) -> tuple[bool, str]:
    """Release the matching agent hold without guessing adaptive hold kind."""
    if not label or label == MANUAL_LABEL:
        return False, "reserved or empty label"
    with locked_state() as (path, state):
        if state["hard"] and state["hard"]["label"] == label:
            state["hard"] = None
            released = "hard"
        else:
            before = len(state["soft"])
            state["soft"] = [item for item in state["soft"] if item["label"] != label]
            if len(state["soft"]) == before:
                return False, "matching agent hold not found"
            released = "soft"
        _save(path, state)
    _log("adaptive-release", label, "OK", f"{released} hold released")
    return True, f"{released} hold released"


def release_after_cleanup(
    label: str,
    cleanup: Callable[[], tuple[bool, str]],
    *,
    goal_scoped: bool = False,
) -> tuple[bool, str]:
    """Run verified cleanup and release ``label`` without an acquisition race.

    The semaphore mutex stays held while ``cleanup`` runs.  This is deliberately
    reserved for task wind-down: ordinary releases must remain fast and must not
    discard a useful language-server cache at an intermediate boundary. Other
    holds may coexist only when the caller has established a validated per-goal
    worktree scope for both cleanup and verification.
    """
    if not label or label == MANUAL_LABEL:
        return False, "reserved or empty label"
    with locked_state() as (path, state):
        holds = ([state["hard"]] if state["hard"] else []) + state["soft"]
        other_labels = [item["label"] for item in holds if item["label"] != label]
        if other_labels and not goal_scoped:
            detail = "other holds block task wind-down: " + ", ".join(other_labels)
            _log("wind-down", label, "REFUSED", detail)
            return False, detail
        try:
            cleaned, cleanup_detail = cleanup()
        except Exception:
            detail = "cleanup raised an exception; matching hold retained"
            _log("wind-down", label, "REFUSED", detail)
            return False, detail
        if not cleaned:
            detail = f"cleanup not verified; matching hold retained: {cleanup_detail}"
            _log("wind-down", label, "REFUSED", detail)
            return False, detail

        released = None
        if state["hard"] and state["hard"]["label"] == label:
            state["hard"] = None
            released = "hard"
        else:
            before = len(state["soft"])
            state["soft"] = [item for item in state["soft"] if item["label"] != label]
            if len(state["soft"]) != before:
                released = "soft"
        if released:
            _save(path, state)

    detail = (
        f"cleanup verified and {released} hold released"
        if released
        else "cleanup verified; no matching hold remained"
    )
    try:
        _log("wind-down", label, "OK", detail)
    except OSError:
        # Cleanup and the atomic state update have already succeeded.  A log
        # failure must not misreport the hold as retained or invite a retry
        # that races with a newly acquired hold.
        detail += "; audit log write failed"
    return True, detail


def renew(
    label: str,
    lease: int = DEFAULT_LEASE_SECONDS,
    *,
    adapter: Optional[Adapter] = None,
    policy: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    if label == MANUAL_LABEL:
        return False, "manual hold has no heartbeat"
    if lease < 1 or lease > MAX_LEASE_SECONDS:
        return False, f"lease must be 1..{MAX_LEASE_SECONDS} seconds"
    selected = adapter or get_adapter()
    selected_policy = policy or _runtime_admission_policy(selected)
    with locked_state() as (path, state):
        candidates = ([state["hard"]] if state["hard"] else []) + state["soft"]
        hold = next((item for item in candidates if item["label"] == label), None)
        if hold is None:
            return False, "hold not found"
        sample = selected.memory_headroom()
        configured_total = selected_policy.get("physical_memory_gib")
        if isinstance(configured_total, bool) or not isinstance(configured_total, (int, float)):
            configured_total = None
        free_percent, _, total_gib = _headroom_values(sample, configured_total)
        soft = sorted(
            (item for item in state["soft"] if not item.get("manual")),
            key=lambda item: (item["acquired_at"], item["label"]),
        )
        manual_active = any(item.get("manual") for item in state["soft"])
        live_soft = [item for item in soft if not _expired(item)]
        priority_pool = live_soft or soft
        priority_label = priority_pool[0]["label"] if priority_pool else None
        over_worker_limit = len(soft) > int(selected_policy["heavy_workers"])
        reserve_gib = _reserve_gib(total_gib)
        over_reservation_budget = False
        if reserve_gib is not None and total_gib is not None:
            charged = sum(
                _hold_reservation(item, int(selected_policy["task_memory_gib"]))
                for item in soft
            )
            over_reservation_budget = charged > max(0.0, total_gib - reserve_gib)
        if free_percent is not None and free_percent < ADMISSION_DRAIN_PERCENT:
            detail = (
                f"DRAIN_HEAVY — available memory is {free_percent}% "
                f"(<{ADMISSION_DRAIN_PERCENT}%); do not launch another heavy step; "
                "checkpoint, wind down, and run light work"
            )
            _log("renew", label, "REFUSED", detail)
            return False, detail
        if manual_active:
            detail = (
                "YIELD_HEAVY — a manual human-session hold is active; checkpoint, "
                "wind down, and run light work"
            )
            _log("renew", label, "REFUSED", detail)
            return False, detail
        pressure_requires_serialization = (
            len(soft) > 1
            and (free_percent is None or free_percent < ADMISSION_CONTENTION_PERCENT)
        )
        budget_requires_serialization = over_worker_limit or over_reservation_budget
        if label != priority_label and (
            pressure_requires_serialization or budget_requires_serialization
        ):
            pressure = (
                f"available memory is {free_percent}%"
                if free_percent is not None
                else f"memory headroom is unavailable ({sample.detail})"
            )
            budget = []
            if over_worker_limit:
                budget.append(
                    f"{len(soft)} holders exceed worker limit {selected_policy['heavy_workers']}"
                )
            if over_reservation_budget:
                budget.append("parallel peak reservations exceed the safe budget")
            trigger = "; ".join([pressure, *budget])
            detail = (
                f"YIELD_HEAVY — {trigger}; priority hold {priority_label} keeps the next "
                "coherent unit; checkpoint, wind down, and run light work"
            )
            _log("renew", label, "REFUSED", detail)
            return False, detail
        hold["renewed_at"] = _now()
        hold["lease_seconds"] = lease
        _save(path, state)
        signals = _refresh_signals(path.parent, state, selected, _now())
    pressure = (
        f"headroom={free_percent}%"
        if free_percent is not None
        else f"headroom unavailable ({sample.detail}); checkpoint frequently"
    )
    detail = f"CONTINUE_HEAVY — hold renewed; {pressure}"
    detail += "".join(
        "\n  " + line for line in _signal_lines(label, signals["holds"], "")
    )
    idle_line = _idle_worker_line(signals["lean_workers"])
    if idle_line:
        detail += "\n  " + idle_line
    _log("renew", label, "OK", f"lease={lease}; {detail}")
    return True, detail


def manual_acquire(note: str = "human using another macOS account") -> tuple[bool, str]:
    adapter = get_adapter()
    if adapter.system != "Darwin":
        return False, "UNAVAILABLE — manual GUI-session holds are macOS-only"
    with locked_state() as (path, state):
        if state["hard"]:
            return False, "REFUSED — hard hold is active; no manual hold acquired"
        if any(item["label"] == MANUAL_LABEL for item in state["soft"]):
            return False, "manual hold already exists"
        state["soft"].append(_hold(MANUAL_LABEL, note, MANUAL_GRACE_SECONDS, manual=True))
        _save(path, state)
    _log("manual-acquire", MANUAL_LABEL, "OK", note)
    return True, "manual soft hold acquired"


def manual_release() -> tuple[bool, str]:
    with locked_state() as (path, state):
        before = len(state["soft"])
        state["soft"] = [item for item in state["soft"] if item["label"] != MANUAL_LABEL]
        if len(state["soft"]) == before:
            return False, "manual hold not found"
        _save(path, state)
    _log("manual-release", MANUAL_LABEL, "OK", "released by human")
    return True, "manual hold released"


def break_expired(label: str, reason: str, adapter: Optional[Adapter] = None) -> tuple[bool, str]:
    selected = adapter or get_adapter()
    with locked_state() as (path, state):
        candidates = ([state["hard"]] if state["hard"] else []) + state["soft"]
        hold = next((item for item in candidates if item["label"] == label), None)
        if hold is None:
            return False, "hold not found"
        if not _expired(hold):
            return False, "hold lease is still live"
        if hold.get("manual"):
            sessions = selected.gui_sessions(int(hold["uid"]))
            if sessions.status != "OK":
                return False, f"manual-session scan unavailable: {sessions.detail}"
            if sessions.data and sessions.data.get("sessions"):
                return False, "another human GUI session is still logged in"
        quiet = selected.quiet_host()
        if quiet.status != "OK":
            return False, f"quiet-host certification refused: {quiet.detail}"
        if state["hard"] and state["hard"]["label"] == label:
            state["hard"] = None
        else:
            state["soft"] = [item for item in state["soft"] if item["label"] != label]
        _save(path, state)
    _log("break", label, "OK", reason)
    return True, "expired hold broken after fail-closed quiet-host certification"


# ---------------------------------------------------------------------------
# Master lease
#
# One session at a time represents the user for all sibling work on a host
# (docs/guides/master.md).  The lease lives in ``master.json`` beside the hold
# state, under the same mutex, and is never read by admission: it charges no
# memory and changes no verdict.  It is a separate file for the reason the
# queue is — hold-state validation rejects unknown keys, and a pre-update
# reader must still validate ``state.json``.
# ---------------------------------------------------------------------------

MASTER_NAME = "master.json"
MASTER_SCHEMA_VERSION = 1
MASTER_LEASE_SECONDS = 1800
MASTER_KEYS = {
    "client", "client_pid", "pid", "uid", "note",
    "acquired_at", "renewed_at", "lease_seconds",
}
CLIENT_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,32}")


def master_path(root: Optional[Path] = None) -> Path:
    return (root or state_root()) / MASTER_NAME


def _empty_master() -> dict[str, Any]:
    return {"schema_version": MASTER_SCHEMA_VERSION, "lease": None}


def _validate_master(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"schema_version", "lease"}:
        raise SemaphoreError("master lease state has an unexpected shape")
    if data["schema_version"] != MASTER_SCHEMA_VERSION:
        raise SemaphoreError("master lease schema is unsupported")
    lease = data["lease"]
    if lease is None:
        return data
    if not isinstance(lease, dict) or set(lease) != MASTER_KEYS:
        raise SemaphoreError("master lease has an unexpected shape")
    if not isinstance(lease["client"], str) or CLIENT_LABEL.fullmatch(lease["client"]) is None:
        raise SemaphoreError("master lease client must be a short label")
    client_pid = lease["client_pid"]
    if client_pid is not None and (
        isinstance(client_pid, bool) or not isinstance(client_pid, int) or client_pid < 1
    ):
        raise SemaphoreError("master lease client_pid must be a positive integer or null")
    if not isinstance(lease["pid"], int) or isinstance(lease["pid"], bool) or lease["pid"] < 1:
        raise SemaphoreError("master lease pid must be a positive integer")
    if not isinstance(lease["uid"], int) or isinstance(lease["uid"], bool) or lease["uid"] < 0:
        raise SemaphoreError("master lease uid must be a non-negative integer")
    if not isinstance(lease["note"], str):
        raise SemaphoreError("master lease note must be a string")
    for key in ("acquired_at", "renewed_at"):
        value = lease[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise SemaphoreError(f"master lease {key} must be a positive finite number")
    window = lease["lease_seconds"]
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= MAX_LEASE_SECONDS:
        raise SemaphoreError("master lease lease_seconds is outside the supported range")
    if lease["renewed_at"] < lease["acquired_at"]:
        raise SemaphoreError("master lease renewed_at predates acquired_at")
    return data


def _load_master(root: Path) -> dict[str, Any]:
    path = master_path(root)
    if not path.exists():
        return _empty_master()
    try:
        return _validate_master(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, SemaphoreError) as exc:
        raise SemaphoreError(f"refusing to replace corrupt master lease state: {exc}")


def _save_master(root: Path, data: dict[str, Any]) -> None:
    _validate_master(data)
    _write_json(master_path(root), data)


def master_snapshot() -> dict[str, Any]:
    with locked_state() as (path, _state):
        return _load_master(path.parent)


def _client_process(
    adapter: Adapter,
    start_pid: Optional[int] = None,
) -> tuple[Optional[int], Optional[str], str]:
    """Find the agent client above this invocation: ``(pid, family, detail)``.

    The launcher's own pid dies with the command, so the lease records the
    client process — the Claude Code or Codex session — found by walking the
    process table upwards from the launcher's parent.  A snapshot the sandbox
    denies leaves the client unknown; the lease then degrades toward
    take-over once its window passes, never toward two masters.
    """
    result = adapter.process_snapshot()
    if result.status != "OK" or not isinstance(result.data, dict):
        return None, None, f"no client identified (process snapshot unavailable: {result.detail})"
    rows = result.data.get("processes")
    if not isinstance(rows, list):
        return None, None, "no client identified (process snapshot carried no process table)"
    table: dict[int, tuple[int, str]] = {}
    for row in rows:
        try:
            table[int(row["pid"])] = (int(row["ppid"]), str(row["command"]))
        except (KeyError, TypeError, ValueError):
            continue
    pattern = getattr(adapter, "client_pattern", _NEVER_MATCHES)
    pid = os.getppid() if start_pid is None else start_pid
    seen: set[int] = set()
    while pid and pid not in seen and pid in table:
        seen.add(pid)
        ppid, command = table[pid]
        family = _client_family(command, pattern)
        if family:
            return pid, family, f"client {family} pid {pid}"
        pid = ppid
    return None, None, "no client identified above this invocation"


def _client_family(command: str, pattern: Any) -> Optional[str]:
    """Name the agent client a process command belongs to, or ``None``.

    A snapshot may carry a full command line (Linux ``ps -o args``) or only
    the executable name (Darwin ``ps -o comm``), so the adapter's path-shaped
    pattern is tried first and the bare executable name second.
    """
    match = pattern.search(command)
    matched = match.group(0).lower() if match else ""
    if not matched:
        first = command.split(None, 1)[0] if command.strip() else ""
        matched = os.path.basename(first).lower()
        if matched not in {"claude", "codex", "chatgpt"}:
            return None
    if "claude" in matched:
        return "claude"
    if "codex" in matched or "chatgpt" in matched:
        return "codex"
    return "agent"


def _master_view(lease: Optional[dict[str, Any]], now: float) -> dict[str, Any]:
    """Classify the lease: ``none``, ``live``, ``lapsed``, or ``stranded``."""
    if lease is None:
        return {"lease": None, "state": "none"}
    expired = _expired(lease, now)
    client_pid = lease["client_pid"]
    client_alive = _pid_alive(int(client_pid)) if client_pid is not None else None
    if client_alive is False:
        # The session that took the lease is gone: nothing legitimate can
        # renew it, so a successor may take over at once.
        state = "stranded"
    elif not expired:
        state = "live"
    elif client_alive:
        # The window passed but the session that took the lease is still a
        # process: an idle tab nobody wound down, or a master that stopped
        # renewing.  Either way it is not a second master's to assume.
        state = "lapsed"
    else:
        state = "stranded"
    return {
        "lease": lease,
        "state": state,
        "expired": expired,
        "client_alive": client_alive,
        "held": now - float(lease["acquired_at"]),
        "since_renewal": now - float(lease["renewed_at"]),
        "remaining": float(lease["renewed_at"]) + int(lease["lease_seconds"]) - now,
    }


def _master_holder_text(lease: dict[str, Any]) -> str:
    client_pid = lease["client_pid"]
    where = f"client_pid={client_pid}" if client_pid is not None else "client process unknown"
    return f"client {lease['client']} ({where}, taken by pid {lease['pid']}, note={lease['note']!r})"


def _same_client(lease: dict[str, Any], client_pid: Optional[int]) -> Optional[bool]:
    """Is this invocation the lease holder's session? ``None`` when unverifiable."""
    if client_pid is None or lease["client_pid"] is None:
        return None
    return int(client_pid) == int(lease["client_pid"])


def _master_lines(root: Path, now: float) -> list[str]:
    try:
        data = _load_master(root)
    except SemaphoreError as exc:
        return [f"master: {exc}"]
    view = _master_view(data["lease"], now)
    if view["state"] == "none":
        return ["master: none"]
    lease = view["lease"]
    lines = [
        f"master: {lease['client']} ({view['state']}) client_pid={lease['client_pid']} "
        f"pid={lease['pid']} held={int(view['held'])}s "
        f"renewed={int(view['since_renewal'])}s ago lease={lease['lease_seconds']}s "
        f"note={lease['note']!r}"
    ]
    if view["state"] == "lapsed":
        lines.append(
            "  LAPSED: the lease window passed but the client process is alive; "
            "wind that session down and run `master-release` from it, or replace "
            "it with `~/creme/.semaphore/semaphore master-acquire --take-over "
            "--client CLIENT --note \"...\"`"
        )
    elif view["state"] == "stranded":
        lines.append(
            "  STRANDED: the lease window passed and the client process is gone; "
            "run `~/creme/.semaphore/semaphore master-acquire --take-over "
            "--client CLIENT --note \"...\"`"
        )
    return lines


def master_acquire(
    client: Optional[str],
    note: str,
    lease: int = MASTER_LEASE_SECONDS,
    *,
    take_over: bool = False,
    adapter: Optional[Adapter] = None,
) -> tuple[bool, str]:
    if lease < 1 or lease > MAX_LEASE_SECONDS:
        return False, f"lease must be 1..{MAX_LEASE_SECONDS} seconds"
    if not isinstance(note, str) or not note.strip():
        return False, "a non-empty --note is required"
    selected = adapter or get_adapter()
    client_pid, family, found = _client_process(selected)
    if client is None:
        client = family
    if client is None or CLIENT_LABEL.fullmatch(client) is None:
        return False, (
            f"the agent client could not be identified ({found}); "
            "pass --client claude, codex, or human"
        )
    with locked_state() as (path, _state):
        root = path.parent
        data = _load_master(root)
        now = _now()
        view = _master_view(data["lease"], now)
        replaced: Optional[tuple[str, str]] = None
        if data["lease"] is not None:
            holder = _master_holder_text(data["lease"])
            if view["state"] == "live":
                detail = (
                    f"the master lease is live: {holder}, renewed "
                    f"{int(view['since_renewal'])}s ago with {int(view['remaining'])}s left; "
                    "end it from that session with `master-release`, or wait for the "
                    "lease to lapse; never edit master.json"
                )
                _log("master-acquire", client, "REFUSED", detail)
                return False, detail
            if not take_over:
                detail = (
                    f"the master lease is {view['state'].upper()}: {holder}; replace it "
                    f"with `master-acquire --take-over --client {client} --note ...`"
                )
                _log("master-acquire", client, "REFUSED", detail)
                return False, detail
            replaced = (view["state"], holder)
        data["lease"] = {
            "client": client,
            "client_pid": client_pid,
            "pid": os.getpid(),
            "uid": os.getuid(),
            "note": note,
            "acquired_at": now,
            "renewed_at": now,
            "lease_seconds": lease,
        }
        _save_master(root, data)
    if replaced:
        state_word, holder = replaced
        _log("master-take-over", client, "OK", f"replaced {state_word} lease of {holder}; {found}; {note}")
        return True, (
            f"master lease taken over from {holder} ({state_word}); this session is "
            f"the master as {found}; renew within {lease}s"
        )
    _log("master-acquire", client, "OK", f"{found}; lease={lease}; {note}")
    return True, f"master lease acquired by client {client} ({found}); renew within {lease}s"


def master_renew(
    lease: Optional[int] = None,
    *,
    adapter: Optional[Adapter] = None,
    as_client_pid: Optional[int] = None,
) -> tuple[bool, str]:
    if lease is not None and (lease < 1 or lease > MAX_LEASE_SECONDS):
        return False, f"lease must be 1..{MAX_LEASE_SECONDS} seconds"
    selected = adapter or get_adapter()
    if as_client_pid is not None:
        # The detached heartbeat is orphaned to the init process and cannot
        # find a client above itself; it acts for the holder it read from
        # the lease, and it exits the moment that holder's process is gone.
        client_pid, found = as_client_pid, f"heartbeat for client pid {as_client_pid}"
    else:
        client_pid, _family, found = _client_process(selected)
    with locked_state() as (path, _state):
        root = path.parent
        data = _load_master(root)
        current = data["lease"]
        if current is None:
            return False, "no master lease exists; run master-acquire"
        now = _now()
        view = _master_view(current, now)
        holder = _master_holder_text(current)
        same = _same_client(current, client_pid)
        if view["state"] == "live" and same is False:
            detail = f"the master lease belongs to {holder}; this invocation: {found}"
            _log("master-renew", current["client"], "REFUSED", detail)
            return False, detail
        if view["state"] != "live" and same is not True:
            detail = (
                f"the master lease is {view['state'].upper()} and this invocation "
                f"({found}) is not its holder; use `master-acquire --take-over`"
            )
            _log("master-renew", current["client"], "REFUSED", detail)
            return False, detail
        current["renewed_at"] = now
        if lease is not None:
            current["lease_seconds"] = lease
        _save_master(root, data)
    verified = "holder verified" if same else "holder unverified"
    _log("master-renew", current["client"], "OK", f"lease={current['lease_seconds']}; {verified}")
    return True, (
        f"master lease renewed for client {current['client']} ({verified}); "
        f"{current['lease_seconds']}s window"
    )


def master_release(
    *,
    force: bool = False,
    reason: str = "",
    adapter: Optional[Adapter] = None,
) -> tuple[bool, str]:
    selected = adapter or get_adapter()
    client_pid, _family, found = _client_process(selected)
    with locked_state() as (path, _state):
        root = path.parent
        data = _load_master(root)
        current = data["lease"]
        if current is None:
            return False, "no master lease exists"
        view = _master_view(current, _now())
        holder = _master_holder_text(current)
        same = _same_client(current, client_pid)
        if view["state"] == "live" and same is not True and not force:
            detail = (
                f"the master lease is live and belongs to {holder}; this invocation: "
                f"{found}; release it from that session, wait for it to lapse, or pass "
                "--force with --reason"
            )
            _log("master-release", current["client"], "REFUSED", detail)
            return False, detail
        data["lease"] = None
        _save_master(root, data)
    if same:
        how = "by its holder"
    elif force and view["state"] == "live":
        how = "by force"
    else:
        how = f"after it {view['state']}"
    _log("master-release", current["client"], "OK", f"released {how}; {found}; {reason}")
    return True, f"master lease of {holder} released {how}"


HEARTBEAT_SLICE_SECONDS = 60


def master_heartbeat(
    interval: int,
    *,
    adapter: Optional[Adapter] = None,
    sleep: Callable[[float], None] = time.sleep,
    max_beats: Optional[int] = None,
    clock: Callable[[], float] = time.time,
) -> tuple[bool, str]:
    """Renew the master lease every ``interval`` seconds from a background process.

    The loop is the sanctioned heartbeat: it exits when a renewal is refused,
    when the lease is gone, or when the client process that holds it is no
    longer alive — an orphaned heartbeat must never make a dead master look
    live to the next session.

    It sleeps in short slices and judges "due" by the wall clock.  A system
    sleep freezes the process, and a frozen ``nanosleep`` resumes with its
    remaining time on wake, while the lease window is wall-clock and has
    already passed; slicing means the lease is renewed within a minute of
    waking instead of up to a whole interval later.  The liveness check runs
    every slice too, so a closed session is noticed within a minute.
    """
    if interval < 1 or interval > MAX_LEASE_SECONDS:
        return False, f"interval must be 1..{MAX_LEASE_SECONDS} seconds"
    selected = adapter or get_adapter()
    beats = 0
    last: Optional[float] = None
    while True:
        try:
            data = master_snapshot()
        except SemaphoreError as exc:
            return False, str(exc)
        lease = data["lease"]
        if lease is None:
            return True, f"heartbeat stopped after {beats} renewal(s): no master lease exists"
        client_pid = lease["client_pid"]
        if client_pid is not None and not _pid_alive(int(client_pid)):
            return True, (
                f"heartbeat stopped after {beats} renewal(s): the holding client "
                f"pid {client_pid} is gone; the lease will read STRANDED"
            )
        now = clock()
        if last is None or now - last >= interval:
            ok, detail = master_renew(adapter=selected, as_client_pid=client_pid)
            if not ok:
                return False, f"heartbeat stopped after {beats} renewal(s): {detail}"
            beats += 1
            last = now
            if max_beats is not None and beats >= max_beats:
                return True, f"heartbeat stopped after {beats} renewal(s): beat limit reached"
        remaining = interval - (clock() - last)
        sleep(max(1.0, min(float(HEARTBEAT_SLICE_SECONDS), remaining)))


def master_heartbeat_detached(interval: int) -> tuple[bool, str]:
    """Start the heartbeat in its own process session, detached from the caller.

    A client's tool call is reaped when it ends or times out, and macOS has no
    ``setsid`` binary, so the launcher detaches itself: the child starts a new
    session, reads and writes nothing on the caller's descriptors, and logs to
    ``heartbeat.log`` beside the lease state.  It still exits on its own when
    the holding client process is gone or a renewal is refused.
    """
    if interval < 1 or interval > MAX_LEASE_SECONDS:
        return False, f"interval must be 1..{MAX_LEASE_SECONDS} seconds"
    with locked_state() as (path, _state):
        root = path.parent
        lease = _load_master(root)["lease"]
    if lease is None:
        return False, "no master lease exists; run master-acquire first"
    launcher = canonical_creme_root() / NEUTRAL_STATE_RELATIVE.parent / "semaphore"
    log = root / "heartbeat.log"
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        process = subprocess.Popen(
            [sys.executable, str(launcher), "master-renew", "--heartbeat", str(interval)],
            stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
            start_new_session=True, close_fds=True,
        )
    finally:
        os.close(fd)
    _log("master-heartbeat", lease["client"], "OK", f"detached pid {process.pid}; interval={interval}")
    return True, (
        f"heartbeat detached as pid {process.pid}, renewing every {interval}s for "
        f"client {lease['client']}; log: {log}"
    )
