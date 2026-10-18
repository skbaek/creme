"""Free-space headroom for the Linux filesystem and its host-drive mounts.

On WSL2 the distro's virtual disk grows monotonically inside a VHDX file that
lives on a Windows drive.  Linux `statvfs` on `/` reports the virtual disk, so
it can show plenty of space while the host drive underneath is nearly full.
The host drives are therefore measured too, from `disk.host_mounts` in the
host profile.  A configured mount that is absent or unreadable is never
skipped: the verdict becomes UNKNOWN, which enforcement treats as a refusal.

One reader serves three callers: `cache-copy --execute` (refuses a copy that
would leave less than the floor), owned `lake-build` (refuses before a build
starts), and `doctor` (reports every path).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Optional

from .profile import DEFAULT_RELATIVE_PROFILE, disk_settings, load

GIB = 1024 ** 3
OK = "OK"
LOW = "LOW"
UNKNOWN = "UNKNOWN"
PROFILE_STATUSES_WITH_SETTINGS = {"VALID", "LIMITED", "STALE", "MISSING"}


def _unknown(creme_root: str, detail: str) -> dict[str, Any]:
    return {
        "verdict": UNKNOWN,
        "free_gib": None,
        "binding_path": None,
        "per_path": {},
        "floor_gib": None,
        "host_mounts": [],
        "profile": "INVALID",
        "detail": detail,
        "creme_root": creme_root,
    }


def headroom(creme_root: Path, profile_path: Optional[Path] = None) -> dict[str, Any]:
    """Minimum free GiB over the Creme root's filesystem and each host mount.

    ``profile_path`` defaults to the canonical host profile beneath
    ``creme_root``.  The returned ``verdict`` is OK, LOW (binding free space is
    below the floor), or UNKNOWN (a host mount is missing or unreadable, or the
    profile is invalid).
    """
    root = Path(creme_root).expanduser().resolve()
    checked = load(profile_path or root / DEFAULT_RELATIVE_PROFILE)
    if checked.status not in PROFILE_STATUSES_WITH_SETTINGS:
        return _unknown(str(root), f"host profile is {checked.status.lower()} ({checked.detail}); disk settings cannot be read")
    mounts, floor = disk_settings(checked.profile)

    per_path: dict[str, dict[str, Any]] = {}
    for index, path in enumerate([str(root), *mounts]):
        error: Optional[str] = None
        free: Optional[float] = None
        if index > 0 and not os.path.ismount(path):
            # A missing drvfs mount leaves an empty directory on the root
            # filesystem, whose statvfs would report the wrong disk.
            error = "configured host mount is not mounted"
        else:
            try:
                stat = os.statvfs(path)
            except OSError as exc:
                error = f"statvfs failed: {exc.strerror or exc}"
            else:
                free = stat.f_bavail * stat.f_frsize / GIB
        per_path[path] = {
            "free_gib": None if free is None else round(free, 2),
            "error": error,
        }

    readable = {path: entry["free_gib"] for path, entry in per_path.items() if entry["free_gib"] is not None}
    binding = min(readable, key=readable.__getitem__) if readable else None
    errors = {path: entry["error"] for path, entry in per_path.items() if entry["error"]}
    if errors:
        verdict = UNKNOWN
        detail = "; ".join(f"{path}: {error}" for path, error in errors.items())
    elif readable[binding] < floor:
        verdict = LOW
        detail = f"binding path {binding} has {readable[binding]:.2f} GiB free, below the {floor} GiB floor"
    else:
        verdict = OK
        detail = f"binding path {binding} has {readable[binding]:.2f} GiB free; floor {floor} GiB"
    return {
        "verdict": verdict,
        "free_gib": readable[binding] if binding is not None else None,
        "binding_path": binding,
        "per_path": per_path,
        "floor_gib": floor,
        "host_mounts": list(mounts),
        "profile": checked.status,
        "detail": detail,
        "creme_root": str(root),
    }


def tree_bytes(source: Path) -> int:
    """Bytes a copy of ``source`` would write: every entry's size, no symlink following.

    Raises OSError when the tree cannot be walked, so the caller refuses rather
    than guessing.
    """
    total = 0

    def fail(exc: OSError) -> None:
        raise exc

    for directory, names, files in os.walk(source, onerror=fail, followlinks=False):
        for name in names + files:
            status = os.lstat(os.path.join(directory, name))
            if not stat.S_ISDIR(status.st_mode):
                total += status.st_size
    return total


def assess(reading: dict[str, Any], needed_bytes: int = 0) -> dict[str, Any]:
    """Judge a reading against a floor after a planned write of ``needed_bytes``.

    The projection subtracts the write from the binding (least-free) path, which
    is conservative: the destination may sit on any measured filesystem.  An
    UNKNOWN reading never allows a write.
    """
    needed_gib = needed_bytes / GIB
    result: dict[str, Any] = {
        "verdict": reading["verdict"],
        "binding_path": reading["binding_path"],
        "free_gib": reading["free_gib"],
        "floor_gib": reading["floor_gib"],
        "needed_gib": round(needed_gib, 2),
        "projected_free_gib": None,
        "per_path": reading["per_path"],
        "allowed": False,
    }
    if reading["verdict"] == UNKNOWN:
        result["detail"] = f"disk headroom UNKNOWN: {reading['detail']}"
        return result
    projected = reading["free_gib"] - needed_gib
    result["projected_free_gib"] = round(projected, 2)
    if projected < reading["floor_gib"]:
        result["verdict"] = LOW
        if needed_bytes:
            result["detail"] = (
                f"disk headroom LOW: binding path {reading['binding_path']} has "
                f"{reading['free_gib']:.2f} GiB free; a {needed_gib:.2f} GiB copy projects "
                f"{projected:.2f} GiB, below the {reading['floor_gib']} GiB floor"
            )
        else:
            result["detail"] = (
                f"disk headroom LOW: binding path {reading['binding_path']} has "
                f"{reading['free_gib']:.2f} GiB free, below the {reading['floor_gib']} GiB floor"
            )
        return result
    result["allowed"] = True
    result["detail"] = reading["detail"]
    return result


def describe(reading: dict[str, Any]) -> str:
    """One line for doctor: every path's free GiB, the binding one, and the floor."""
    parts = []
    for path, entry in reading["per_path"].items():
        if entry["error"]:
            parts.append(f"{path} unreadable ({entry['error']})")
        else:
            parts.append(f"{path} {entry['free_gib']:.2f} GiB free")
    if not parts:
        return "no readable path"
    binding = reading["binding_path"] or "none"
    return f"{'; '.join(parts)}; binding {binding}; floor {reading['floor_gib']} GiB"


def guard_build(creme_root: Optional[Path] = None) -> Optional[str]:
    """The refusal text for an owned build, or None when the build may start.

    Pre-admission only: the memory admission path is unchanged.
    """
    from . import semaphore

    root = creme_root or semaphore.canonical_creme_root()
    verdict = assess(headroom(root))
    if verdict["allowed"]:
        return None
    return verdict["detail"]
