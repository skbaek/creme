from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

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
    fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
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


def _hold_reservation(hold: dict[str, Any], default_memory_gib: int) -> int:
    _, memory_gib, _ = _decode_admission_note(hold["note"], default_memory_gib)
    return _charged_memory_gib(memory_gib)


def _admission_decision(
    state: dict[str, Any],
    requested_kind: str,
    label: str,
    memory_gib: int,
    contention: str,
    adapter: Adapter,
    policy: dict[str, Any],
) -> tuple[bool, Optional[str], str, str]:
    hard = state["hard"]
    matching_soft = [item for item in state["soft"] if item["label"] == label]
    other_soft = [item for item in state["soft"] if item["label"] != label]
    if hard and hard["label"] != label:
        detail = f"hard hold {hard['label']} blocks acquisition; run light work until it releases"
        return False, None, "DEFER_HEAVY", detail
    if hard and hard["label"] == label:
        return False, None, "ALREADY_HELD", "label already owns the hard hold; use renew"
    if matching_soft and requested_kind != "hard":
        return False, None, "ALREADY_HELD", "label already owns a soft hold; use renew"
    if any(item.get("manual") for item in other_soft):
        return (
            False,
            None,
            "LIGHT_ONLY",
            "a manual human-session hold is active; run light work until it is released",
        )
    if requested_kind == "hard" and other_soft:
        labels = ", ".join(item["label"] for item in other_soft)
        return False, None, "DEFER_FOR_HARD", f"soft holds block hard acquisition: {labels}"

    converting = requested_kind == "hard" and bool(matching_soft)
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
        return (
            False,
            None,
            "LIGHT_ONLY",
            f"available memory is {free_percent}% (<{ADMISSION_DRAIN_PERCENT}%); "
            "do not start heavy work; checkpoint or wind down heavy sessions and run light work",
        )

    if not converting and free_percent is None:
        requires_hard = True
        reasons.append(f"memory headroom unavailable ({sample.detail}); limited mode serializes heavy work")

    if not converting and reserve_gib is not None:
        capacity_gib = max(0.0, float(total_gib) - reserve_gib)
        if charged_gib > capacity_gib:
            return (
                False,
                None,
                "LIGHT_ONLY",
                f"{memory_gib} GiB estimate charges {charged_gib} GiB with peak margin, "
                f"exceeding the {capacity_gib:.1f} GiB heavy-work budget; split or reduce the task",
            )
        if available_gib is not None and available_gib < charged_gib + reserve_gib:
            decision = "DEFER_FOR_HARD" if other_soft else "LIGHT_ONLY"
            action = (
                "wait for current heavy holds to wind down, then acquire hard"
                if other_soft
                else "wait for memory to recover and run light work"
            )
            return (
                False,
                None,
                decision,
                f"{available_gib:.1f} GiB is available but this task needs {charged_gib} GiB "
                f"plus a {reserve_gib:.1f} GiB usability reserve; {action}",
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
        return False, None, decision, "; ".join(reasons + [action])

    selected_kind = "hard" if requires_hard else "soft"
    if selected_kind == "hard" and other_soft:
        labels = ", ".join(item["label"] for item in other_soft)
        return (
            False,
            None,
            "DEFER_FOR_HARD",
            "; ".join(reasons + [f"soft holds still active: {labels}; run light work first"]),
        )

    headroom = (
        f"headroom={free_percent}%" if free_percent is not None else "headroom=unavailable"
    )
    rationale = "; ".join(reasons) if reasons else "parallel admission fits the live and reserved budgets"
    return (
        True,
        selected_kind,
        f"ADMITTED_{selected_kind.upper()}",
        f"{headroom}; memory={memory_gib} GiB (charged={charged_gib} GiB); {rationale}",
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


def snapshot() -> dict[str, Any]:
    with locked_state() as (_, state):
        return json.loads(json.dumps(state))


def status_text() -> str:
    state = snapshot()
    now = _now()
    try:
        default_memory_gib = int(_runtime_admission_policy(get_adapter())["task_memory_gib"])
    except (KeyError, OSError, SemaphoreError, ValueError):
        default_memory_gib = 2
    lines = []
    hard = state["hard"]
    if hard:
        state_word = "expired-blocking" if _expired(hard, now) else "live"
        note, memory_gib, contention = _decode_admission_note(hard["note"], default_memory_gib)
        lines.append(
            f"hard: {hard['label']} ({state_word}) pid={hard['pid']} "
            f"memory={memory_gib}GiB contention={contention} note={note!r}"
        )
    else:
        lines.append("hard: free")
    lines.append(f"soft (S={len(state['soft'])}):")
    for hold in state["soft"]:
        state_word = "expired-blocking" if _expired(hold, now) else "live"
        note, memory_gib, contention = _decode_admission_note(hold["note"], default_memory_gib)
        lines.append(
            f"  {hold['label']} ({state_word}) pid={hold['pid']} "
            f"memory={memory_gib}GiB contention={contention} note={note!r}"
        )
    if not state["soft"]:
        lines.append("  none")
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
    if not label or label == MANUAL_LABEL:
        return False, "reserved or empty label"
    if lease < 1 or lease > MAX_LEASE_SECONDS:
        return False, f"lease must be 1..{MAX_LEASE_SECONDS} seconds"
    if contention not in ADMISSION_CONTENTION:
        return False, f"unknown contention class: {contention}"
    selected = adapter or get_adapter()
    selected_policy = policy or _runtime_admission_policy(selected)
    requested_memory = memory_gib if memory_gib is not None else int(selected_policy["task_memory_gib"])
    if (
        not isinstance(requested_memory, int)
        or isinstance(requested_memory, bool)
        or requested_memory < 1
    ):
        return False, "memory estimate must be a positive whole number of GiB"
    with locked_state() as (path, state):
        admitted, selected_kind, decision, detail = _admission_decision(
            state,
            requested_kind,
            label,
            requested_memory,
            contention,
            selected,
            selected_policy,
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


def adaptive_acquire(
    label: str,
    note: str,
    lease: int = ADAPTIVE_LEASE_SECONDS,
    *,
    memory_gib: Optional[int] = None,
    contention: str = "tolerant",
    adapter: Optional[Adapter] = None,
    policy: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
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
    pressure = (
        f"headroom={free_percent}%"
        if free_percent is not None
        else f"headroom unavailable ({sample.detail}); checkpoint frequently"
    )
    detail = f"CONTINUE_HEAVY — hold renewed; {pressure}"
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
