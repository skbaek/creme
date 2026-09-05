"""Read-only process-lifetime witnesses, distinct from task liveness.

Codex's Arg0PathEntryGuard retains an exclusive flock on a unique PATH
directory for the process lifetime (codex-rs/arg0/src/lib.rs). That existing
kernel lock is visible across PID namespaces through their shared filesystem.
Its disappearance proves process departure; its presence does not prove an
individual task in a shared application is still alive.
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
from pathlib import Path
from typing import Any


LOCK_KEYS = {"kind", "path", "device", "inode", "uid"}


def valid_process_witness(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != LOCK_KEYS or value["kind"] != "codex-arg0-lock":
        return False
    path = value["path"]
    return (
        isinstance(path, str) and "\x00" not in path and Path(path).is_absolute()
        and str(Path(path)) == path and ".." not in Path(path).parts
        and Path(path).name == ".lock"
        and re.fullmatch(r"codex-arg0[A-Za-z0-9]+", Path(path).parent.name) is not None
        and Path(path).parent.parent.name == "arg0"
        and Path(path).parent.parent.parent.name == "tmp"
        and all(type(value[k]) is int and value[k] >= 0 for k in ("device", "inode", "uid"))
        and value["inode"] > 0
    )


def lock_alive(witness: dict[str, Any]) -> bool | None:
    """False = original exclusive lock gone; None = cannot observe it."""
    if not valid_process_witness(witness):
        return None
    try:
        fd = os.open(witness["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != witness["uid"]:
                return None
            if (info.st_dev, info.st_ino) != (witness["device"], witness["inode"]):
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            # Closing releases only our own short-lived shared lock.
            os.close(fd)
    except FileNotFoundError:
        return False
    except OSError:
        return None


def codex_process_witness() -> dict[str, Any] | None:
    # Codex prepends its current guard. A broken first candidate must not make
    # an older inherited parent guard look like the current process's guard.
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
        witness = {
            "kind": "codex-arg0-lock", "path": str(path),
            "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
        }
        return witness if lock_alive(witness) is True else None
    return None
