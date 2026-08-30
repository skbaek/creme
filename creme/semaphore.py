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
from typing import Any, Iterator, Optional

from .adapters import Adapter, get_adapter


SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 1800
MAX_LEASE_SECONDS = 14400
MANUAL_LABEL = "manual-macos-session"
MANUAL_GRACE_SECONDS = 300
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


def state_root() -> Path:
    override = os.environ.get("CREME_SEMAPHORE_DIR")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "creme" / "host-semaphore"
    return Path.home() / ".local" / "state" / "creme" / "host-semaphore"


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
def locked_state() -> Iterator[tuple[Path, dict[str, Any]]]:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError as exc:
        raise SemaphoreError(f"cannot secure semaphore directory: {exc}")
    mutex = root / "mutex"
    with mutex.open("a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = root / "state.json"
        if path.exists():
            try:
                state = _validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, SemaphoreError) as exc:
                raise SemaphoreError(f"refusing to replace corrupt semaphore state: {exc}")
        else:
            state = _empty_state()
        yield path, state


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


def _log(action: str, label: str, verdict: str, detail: str) -> None:
    record = {
        "time": _iso(), "action": action, "label": label,
        "verdict": verdict, "detail": detail,
    }
    path = state_root() / "log.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _hold(label: str, note: str, lease: int, manual: bool = False) -> dict[str, Any]:
    now = _now()
    return {
        "label": label,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "note": note,
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
    lines = []
    hard = state["hard"]
    if hard:
        state_word = "expired-blocking" if _expired(hard, now) else "live"
        lines.append(f"hard: {hard['label']} ({state_word}) pid={hard['pid']} note={hard['note']!r}")
    else:
        lines.append("hard: free")
    lines.append(f"soft (S={len(state['soft'])}):")
    for hold in state["soft"]:
        state_word = "expired-blocking" if _expired(hold, now) else "live"
        lines.append(f"  {hold['label']} ({state_word}) pid={hold['pid']} note={hold['note']!r}")
    if not state["soft"]:
        lines.append("  none")
    return "\n".join(lines)


def acquire(kind: str, label: str, note: str, lease: int = DEFAULT_LEASE_SECONDS) -> tuple[bool, str]:
    if not label or label == MANUAL_LABEL:
        return False, "reserved or empty label"
    if lease < 1 or lease > MAX_LEASE_SECONDS:
        return False, f"lease must be 1..{MAX_LEASE_SECONDS} seconds"
    with locked_state() as (path, state):
        if state["hard"] and state["hard"]["label"] != label:
            detail = f"hard hold {state['hard']['label']} blocks acquisition"
            _log(f"{kind}-acquire", label, "REFUSED", detail)
            return False, detail
        matching_soft = [item for item in state["soft"] if item["label"] == label]
        other_soft = [item for item in state["soft"] if item["label"] != label]
        if kind == "soft":
            if state["hard"]:
                return False, "label already owns the hard hold; release it before soft acquisition"
            if matching_soft:
                return False, "label already owns a soft hold; use renew"
            state["soft"].append(_hold(label, note, lease))
        elif kind == "hard":
            if other_soft:
                detail = "soft holds block hard acquisition: " + ", ".join(item["label"] for item in other_soft)
                _log("hard-acquire", label, "REFUSED", detail)
                return False, detail
            if state["hard"]:
                return False, "label already owns the hard hold; use renew"
            state["soft"] = []
            state["hard"] = _hold(label, note, lease)
        else:
            return False, f"unknown hold kind: {kind}"
        _save(path, state)
    _log(f"{kind}-acquire", label, "OK", note)
    return True, f"{kind} hold acquired"


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


def renew(label: str, lease: int = DEFAULT_LEASE_SECONDS) -> tuple[bool, str]:
    if label == MANUAL_LABEL:
        return False, "manual hold has no heartbeat"
    if lease < 1 or lease > MAX_LEASE_SECONDS:
        return False, f"lease must be 1..{MAX_LEASE_SECONDS} seconds"
    with locked_state() as (path, state):
        candidates = ([state["hard"]] if state["hard"] else []) + state["soft"]
        hold = next((item for item in candidates if item["label"] == label), None)
        if hold is None:
            return False, "hold not found"
        hold["renewed_at"] = _now()
        hold["lease_seconds"] = lease
        _save(path, state)
    _log("renew", label, "OK", f"lease={lease}")
    return True, "hold renewed"


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
