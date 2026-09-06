"""Incarnation and liveness witnesses for master leases, never numeric PID authority.

Codex's per-process arg0 PATH guard holds an exclusive flock for its lifetime.
That existing kernel witness is visible through a shared filesystem even when
the two clients' process tables are in disjoint PID namespaces. We only open
the lock read-only, briefly try a shared lock, and never create or remove it.
See openai/codex, codex-rs/arg0/src/lib.rs (Arg0PathEntryGuard).
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import json
from pathlib import Path
from typing import Any


LOCK_KEYS = {"kind", "path", "device", "inode", "uid", "session"}
PROCESS_KEYS = {"kind", "pid", "scope", "start", "uid"}
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def valid_identity(identity: Any) -> bool:
    if not isinstance(identity, dict):
        return False
    kind = identity.get("kind")
    if kind == "codex-arg0-lock":
        if set(identity) != LOCK_KEYS:
            return False
        path = identity["path"]
        session = identity["session"]
        return (
            isinstance(path, str) and "\x00" not in path and Path(path).is_absolute()
            and str(Path(path)) == path and ".." not in Path(path).parts
            and Path(path).name == ".lock"
            and re.fullmatch(r"codex-arg0[A-Za-z0-9]+", Path(path).parent.name) is not None
            and Path(path).parent.parent.name == "arg0"
            and Path(path).parent.parent.parent.name == "tmp"
            and all(type(identity[k]) is int and identity[k] >= 0 for k in ("device", "inode", "uid"))
            and identity["inode"] > 0
            and (session is None or isinstance(session, str) and re.fullmatch(r"[0-9a-f]{64}", session) is not None)
        )
    if kind in {"linux-process", "darwin-process"}:
        shape = (
            set(identity) == PROCESS_KEYS
            and type(identity["pid"]) is int and identity["pid"] > 0
            and type(identity["uid"]) is int and identity["uid"] >= 0
            and all(isinstance(identity[k], str) and 0 < len(identity[k]) <= 256 for k in ("scope", "start"))
        )
        if not shape:
            return False
        scope_pattern = UUID + r":pid:\[[0-9]+\]" if kind == "linux-process" else UUID
        start_pattern = r"[0-9]+" if kind == "linux-process" else r"[0-9]+\.[0-9]{6}"
        return re.fullmatch(scope_pattern, identity["scope"]) is not None and re.fullmatch(start_pattern, identity["start"]) is not None
    return False


def lock_alive(identity: dict[str, Any]) -> bool | None:
    """True = original exclusive lock still held; None = cannot observe it."""
    try:
        fd = os.open(identity["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != identity["uid"]:
                return None
            if (info.st_dev, info.st_ino) != (identity["device"], identity["inode"]):
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            # Closing releases only our own short-lived shared lock.
            return False
        finally:
            os.close(fd)
    except FileNotFoundError:
        return False
    except OSError:
        return None


def codex_identity() -> dict[str, Any] | None:
    # Codex prepends the current guard. Never skip a broken first candidate
    # and accidentally identify an older inherited parent session instead.
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry)
        if not directory.is_absolute() or re.fullmatch(r"codex-arg0[A-Za-z0-9]+", directory.name) is None:
            continue
        if directory.parent.name != "arg0" or directory.parent.parent.name != "tmp":
            continue
        path = directory / ".lock"
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return None
        except OSError:
            return None
        session = [os.environ.get("CODEX_SESSION_ID"), os.environ.get("CODEX_THREAD_ID")]
        identity = {
            "kind": "codex-arg0-lock", "path": str(path),
            "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
            "session": hashlib.sha256(json.dumps(session).encode()).hexdigest() if any(session) else None,
        }
        return identity if lock_alive(identity) is True else None
    return None
