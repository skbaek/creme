"""Retire the nodes a completed pre-master migration left in the record.

The one-time legacy migration (the former ``creme/master_migrate.py``) left a
report, a byte-identical backup, and the obsolete root files beside the
structured record, and every record read re-verified them.  Retirement moves
them, verified, into an archive outside the validated record::

    $GOAL_STORE/master-archive/legacy-migration-YYYYMMDD/
        manifest.json          canonical; SHA-256, size, and mode per node
        record/<node>          the retained nodes at their record paths

and then records a ``procedure`` event that binds the archive manifest digest.
``restore`` reverses it byte for byte; a restored record needs a Creme
checkout that still carries the migration verifier.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ContextManager, Optional, Sequence

from . import master_runtime, semaphore


ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_KIND = "creme-master-legacy-migration-archive"
ARCHIVE_ROOT_NAME = "master-archive"
ARCHIVE_PREFIX = "legacy-migration-"
ARCHIVE_MANIFEST_NAME = "manifest.json"
ARCHIVE_NODES_NAME = "record"
PROCEDURE_ID = "legacy-migration-retirement"

_REPORT = master_runtime.LEGACY_MIGRATION_REPORT_NAME
_BACKUPS = master_runtime.LEGACY_MIGRATION_BACKUP_ROOT_NAME
_ROOT_FILES = master_runtime.LEGACY_MIGRATION_ROOT_FILES
_NODES = master_runtime.LEGACY_MIGRATION_NODES
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ARCHIVE_NAME = re.compile(r"legacy-migration-[0-9]{8}(-[0-9]+)?")
_STAGING_NAME = re.compile(r"\.staging-legacy-migration-[0-9]{8}(-[0-9]+)?-[0-9a-f]{16}")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_LEGACY_MIGRATOR_COMMIT = "1cc5c48"

Renewal = Callable[[], tuple[bool, str]]
AuthorityTransaction = Callable[[], ContextManager[Any]]
PrivacyCheck = Callable[[Path], Optional[str]]


class RetirementError(RuntimeError):
    """Retirement or restore refused; nothing further was changed."""


@dataclass(frozen=True)
class RetirementPlan:
    status: str
    detail: str
    record_root: str
    archive: Optional[str]
    manifest_sha256: Optional[str]
    nodes: tuple[str, ...]
    files: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "record_root": self.record_root,
            "archive": self.archive,
            "manifest_sha256": self.manifest_sha256,
            "nodes": list(self.nodes),
            "files": self.files,
            "bytes": self.bytes,
        }


# ---------------------------------------------------------------- inventory


def _canonical(value: Any) -> bytes:
    return master_runtime._canonical_json(value)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_private(path: Path, expected_mode: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != expected_mode
    ):
        raise RetirementError(f"{path} is not an owner-only regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def _check_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RetirementError(f"{path} is not an owner-only directory")


def _inventory(base: Path, names: Sequence[str]) -> dict[str, Any]:
    """Hash every file and directory under ``base`` for the named nodes."""
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for name in sorted(names):
        top = base / name
        if top.is_symlink():
            raise RetirementError(f"legacy node {name} must not be a symlink")
        if top.is_dir():
            pending = [top]
            while pending:
                directory = pending.pop()
                _check_directory(directory)
                directories.append(
                    {"path": directory.relative_to(base).as_posix(), "mode": 0o700}
                )
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda item: item.name)
                for child in children:
                    path = Path(child.path)
                    if child.is_symlink():
                        raise RetirementError(f"{path} must not be a symlink")
                    if child.is_dir(follow_symlinks=False):
                        pending.append(path)
                    else:
                        data = _read_private(path, 0o600)
                        files.append(
                            {
                                "path": path.relative_to(base).as_posix(),
                                "mode": 0o600,
                                "size": len(data),
                                "sha256": _sha256(data),
                            }
                        )
        else:
            data = _read_private(top, 0o600)
            files.append(
                {"path": name, "mode": 0o600, "size": len(data), "sha256": _sha256(data)}
            )
    directories.sort(key=lambda row: row["path"])
    files.sort(key=lambda row: row["path"])
    return {"directories": directories, "files": files}


def _present_nodes(root: Path) -> tuple[str, ...]:
    return tuple(
        name for name in _NODES if (root / name).exists() or (root / name).is_symlink()
    )


def _strict_object(data: bytes, context: str) -> dict[str, Any]:
    try:
        value = master_runtime._strict_json(data, context)
    except master_runtime.MasterRecordError as exc:
        raise RetirementError(str(exc)) from exc
    if not isinstance(value, dict):
        raise RetirementError(f"{context} is not a JSON object")
    return value


def _verify_legacy(root: Path, present: Sequence[str]) -> dict[str, Any]:
    """Re-check the completed migration's own seals before archiving.

    This is the part of the retired verifier that protects the bytes being
    archived: a complete report, a backup whose manifest digest the report
    names, backup files that match that manifest exactly, and retained root
    files still identical to their backed-up originals.
    """
    if _REPORT not in present:
        raise RetirementError("legacy nodes without migration.json are not a completed migration")
    report = _strict_object(_read_private(root / _REPORT, 0o600), "migration report")
    if report.get("status") != "complete":
        raise RetirementError("migration report is not complete; finish or recover it first")
    # The translator that could re-derive a translated legacy log is retired,
    # so only a migration that translated nothing can be checked here.
    if report.get("translations") != [] or report.get(
        "translated_log_sha256", _EMPTY_SHA256
    ) != _EMPTY_SHA256:
        raise RetirementError(
            "migration report records translated legacy history whose event-log "
            "prefix this Creme cannot verify; retire it with a pre-retirement "
            f"Creme checkout that carries the migrator ({_LEGACY_MIGRATOR_COMMIT})"
        )
    backup = report.get("backup")
    if not isinstance(backup, dict):
        raise RetirementError("migration report has no backup reference")
    backup_id = backup.get("id")
    manifest_digest = backup.get("manifest_sha256")
    if not (
        isinstance(backup_id, str)
        and _DIGEST.fullmatch(backup_id)
        and isinstance(manifest_digest, str)
        and _DIGEST.fullmatch(manifest_digest)
        and backup.get("manifest") == f"{_BACKUPS}/{backup_id}/manifest.json"
    ):
        raise RetirementError("migration report backup reference is invalid")
    if _BACKUPS not in present:
        raise RetirementError("migration backup namespace is missing")
    backups = root / _BACKUPS
    _check_directory(backups)
    if sorted(os.listdir(backups)) != [backup_id]:
        raise RetirementError("migration backup namespace does not match the report")
    backup_dir = backups / backup_id
    _check_directory(backup_dir)
    if sorted(os.listdir(backup_dir)) != ["manifest.json", "originals"]:
        raise RetirementError("migration backup has an unexpected child inventory")
    manifest_bytes = _read_private(backup_dir / "manifest.json", 0o600)
    if _sha256(manifest_bytes) != manifest_digest:
        raise RetirementError("migration backup manifest does not match the report digest")
    manifest = _strict_object(manifest_bytes, "migration backup manifest")
    rows = manifest.get("files")
    if manifest.get("backup_id") != backup_id or not isinstance(rows, list):
        raise RetirementError("migration backup manifest is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise RetirementError("migration backup manifest row is invalid")
        path = PurePosixPath(row["path"])
        if path.is_absolute() or ".." in path.parts:
            raise RetirementError("migration backup manifest path is unsafe")
        size, digest = row.get("size"), row.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or not isinstance(digest, str):
            raise RetirementError("migration backup manifest row is invalid")
        expected[row["path"]] = (size, digest)
    originals = backup_dir / "originals"
    observed = _inventory(originals.parent, ["originals"])
    seen = {}
    for row in observed["files"]:
        relative = PurePosixPath(row["path"]).relative_to("originals").as_posix()
        seen[relative] = (row["size"], row["sha256"])
    if seen != expected:
        mismatch = sorted(set(seen.items()) ^ set(expected.items()))
        raise RetirementError(
            f"migration backup does not match its manifest at {mismatch[0][0]}"
        )
    for name in _ROOT_FILES:
        original = expected.get(name)
        if name in present:
            if original is None:
                raise RetirementError(f"legacy root file {name} has no backed-up original")
            data = _read_private(root / name, 0o600)
            if (len(data), _sha256(data)) != original:
                raise RetirementError(f"retained legacy file {name} changed after migration")
        elif original is not None:
            raise RetirementError(f"retained legacy file {name} is missing")
    return report


# ------------------------------------------------------------------ archive


def _write_file(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, mode)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_nodes(source: Path, target: Path, inventory: dict[str, Any]) -> None:
    for row in inventory["directories"]:
        path = target / row["path"]
        if not path.is_dir():
            path.mkdir(mode=row["mode"])
        os.chmod(path, row["mode"])
    for row in inventory["files"]:
        destination = target / row["path"]
        if destination.exists() or destination.is_symlink():
            data = _read_private(destination, row["mode"])
            if (len(data), _sha256(data)) != (row["size"], row["sha256"]):
                raise RetirementError(f"{destination} exists with different bytes")
            continue
        data = _read_private(source / row["path"], row["mode"])
        if (len(data), _sha256(data)) != (row["size"], row["sha256"]):
            raise RetirementError(f"{row['path']} changed while it was copied")
        _write_file(destination, data, row["mode"])
    for row in reversed(inventory["directories"]):
        _fsync_directory(target / row["path"])
    _fsync_directory(target)


def _matches(base: Path, inventory: dict[str, Any], names: Sequence[str]) -> bool:
    try:
        observed = _inventory(base, names)
    except (OSError, RetirementError):
        return False
    wanted = set(names)
    subset = {
        key: [row for row in inventory[key] if row["path"].split("/", 1)[0] in wanted]
        for key in ("directories", "files")
    }
    return observed == subset


def read_archive(archive: Path) -> tuple[dict[str, Any], str]:
    """Verify an archive against its own manifest; return it and its digest."""
    _check_directory(archive)
    manifest_bytes = _read_private(archive / ARCHIVE_MANIFEST_NAME, 0o600)
    manifest = _strict_object(manifest_bytes, "archive manifest")
    if _canonical(manifest) != manifest_bytes:
        raise RetirementError("archive manifest is not canonical JSON")
    if (
        manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION
        or manifest.get("kind") != ARCHIVE_KIND
        or not isinstance(manifest.get("nodes"), list)
        or not set(manifest["nodes"]) <= set(_NODES)
    ):
        raise RetirementError("archive manifest has an unsupported shape")
    if sorted(os.listdir(archive)) != sorted([ARCHIVE_MANIFEST_NAME, ARCHIVE_NODES_NAME]):
        raise RetirementError("archive has an unexpected child inventory")
    nodes = archive / ARCHIVE_NODES_NAME
    _check_directory(nodes)
    if sorted(os.listdir(nodes)) != sorted(manifest["nodes"]):
        raise RetirementError("archive node inventory does not match its manifest")
    inventory = {"directories": manifest["directories"], "files": manifest["files"]}
    if not _matches(nodes, inventory, manifest["nodes"]):
        raise RetirementError("archive bytes or modes do not match its manifest")
    return manifest, _sha256(manifest_bytes)


def _archive_candidates(archive_parent: Path) -> list[Path]:
    if not archive_parent.is_dir():
        return []
    return sorted(
        archive_parent / name
        for name in os.listdir(archive_parent)
        if _ARCHIVE_NAME.fullmatch(name)
    )


def stale_staging(archive_parent: Path) -> list[Path]:
    """Staging directories an interrupted retirement left behind."""
    if not archive_parent.is_dir() or archive_parent.is_symlink():
        return []
    return sorted(
        archive_parent / name
        for name in os.listdir(archive_parent)
        if name.startswith(".staging-")
    )


def _remove_stale_staging(archive_parent: Path) -> None:
    """Remove this command's own interrupted staging copies, verified first.

    A staging directory holds private copies of legacy nodes; one left by a
    crash would otherwise persist.  Only an exact staging name, owned
    owner-only directories and files, no links, and the staging layout
    (``record/`` and optionally ``manifest.json``) are removed; anything else
    refuses.
    """
    for staging in stale_staging(archive_parent):
        if _STAGING_NAME.fullmatch(staging.name) is None:
            raise RetirementError(f"unrecognized staging entry {staging.name}; inspect it by hand")
        _check_directory(staging)
        children = set(os.listdir(staging))
        if not children <= {ARCHIVE_NODES_NAME, ARCHIVE_MANIFEST_NAME}:
            raise RetirementError(f"staging {staging.name} has an unexpected layout")
        doomed_files: list[Path] = []
        doomed_directories: list[Path] = [staging]
        pending = [staging]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink():
                        raise RetirementError(f"staging path {path} is a symlink")
                    if entry.is_dir(follow_symlinks=False):
                        _check_directory(path)
                        doomed_directories.append(path)
                        pending.append(path)
                    else:
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                            raise RetirementError(f"staging path {path} is not an owned file")
                        doomed_files.append(path)
        for path in doomed_files:
            path.unlink()
        for path in sorted(doomed_directories, key=lambda item: len(item.parts), reverse=True):
            path.rmdir()
        _fsync_directory(archive_parent)


def _new_archive_path(archive_parent: Path, today: str) -> Path:
    base = archive_parent / f"{ARCHIVE_PREFIX}{today}"
    if not base.exists():
        return base
    suffix = 2
    while (archive_parent / f"{base.name}-{suffix}").exists():
        suffix += 1
    return archive_parent / f"{base.name}-{suffix}"


def goal_store_privacy(goal_store: Path) -> PrivacyCheck:
    """Refuse an archive root the goal store's Git would track or not ignore."""

    def check(archive_parent: Path) -> Optional[str]:
        from .doctor import _git

        probe = _git(goal_store, "rev-parse", "--is-inside-work-tree")
        if probe.returncode or probe.stdout.strip() != "true":
            return None
        relative = archive_parent.relative_to(goal_store).as_posix()
        tracked = _git(goal_store, "ls-files", "--", relative)
        if tracked.returncode or tracked.stdout.strip():
            return f"{relative}/ is Git-tracked in the goal store"
        ignored = _git(
            goal_store, "check-ignore", "-q", "--no-index", "--",
            f"{relative}/.creme-ignore-probe",
        )
        if ignored.returncode:
            return (
                f"{relative}/ is not ignored; add `/{relative}/` to the goal "
                "store's .gitignore before retiring"
            )
        return None

    return check


# ------------------------------------------------------------------ operations


def _plan(
    root: Path, archive_parent: Path, today: str
) -> tuple[RetirementPlan, Optional[dict[str, Any]], Optional[Path]]:
    present = _present_nodes(root)
    resumable = None
    for candidate in _archive_candidates(archive_parent):
        try:
            manifest, digest = read_archive(candidate)
        except (OSError, RetirementError):
            continue
        if manifest["record_root"] == str(root) and set(present) <= set(manifest["nodes"]):
            resumable = (candidate, manifest, digest)
    if not present:
        archive, digest = (str(resumable[0]), resumable[2]) if resumable else (None, None)
        return (
            RetirementPlan(
                "CURRENT",
                "no retained legacy migration nodes remain in the record",
                str(root), archive, digest, (), 0, 0,
            ),
            resumable[1] if resumable else None,
            resumable[0] if resumable else None,
        )
    inventory = {"directories": [], "files": []}
    if resumable is not None:
        candidate, manifest, digest = resumable
        inventory = {"directories": manifest["directories"], "files": manifest["files"]}
        if _matches(root, inventory, present):
            master_runtime._read_record_unlocked(root, _legacy_nodes=present)
            return (
                RetirementPlan(
                    "FINALIZE",
                    "archive is verified; remove the archived nodes from the record with --apply",
                    str(root), str(candidate), digest, present,
                    len(manifest["files"]),
                    sum(row["size"] for row in manifest["files"]),
                ),
                manifest,
                candidate,
            )
    report = _verify_legacy(root, present)
    # The structured record must itself be sound before anything moves.
    master_runtime._read_record_unlocked(root, _legacy_nodes=present)
    inventory = _inventory(root, present)
    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "kind": ARCHIVE_KIND,
        "created": today,
        "record_root": str(root),
        "nodes": list(present),
        "migration_report_sha256": _sha256(_read_private(root / _REPORT, 0o600)),
        "migration_source_snapshot_sha256": report.get("source_snapshot_sha256"),
        "directories": inventory["directories"],
        "files": inventory["files"],
    }
    target = _new_archive_path(archive_parent, today)
    return (
        RetirementPlan(
            "PREVIEW",
            "verified; rerun with --apply as master to archive and record the procedure",
            str(root), str(target), _sha256(_canonical(manifest)), present,
            len(inventory["files"]),
            sum(row["size"] for row in inventory["files"]),
        ),
        manifest,
        target,
    )


def _remove_nodes(root: Path, manifest: dict[str, Any], present: Sequence[str]) -> None:
    for name in [n for n in present if n != _REPORT] + [n for n in present if n == _REPORT]:
        path = root / name
        if path.is_dir() and not path.is_symlink():
            for row in reversed(manifest["files"]):
                child = root / row["path"]
                if row["path"].startswith(f"{name}/") and child.exists():
                    child.unlink()
            for row in reversed(manifest["directories"]):
                if row["path"] == name or row["path"].startswith(f"{name}/"):
                    (root / row["path"]).rmdir()
        else:
            path.unlink()
        _fsync_directory(root)


def _default_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _authority(
    renew: Renewal, authority_transaction: Optional[AuthorityTransaction]
) -> AuthorityTransaction:
    if authority_transaction is not None:
        return authority_transaction
    if renew is semaphore.master_renew:
        return semaphore.master_authority_transaction
    return lambda: master_runtime._composed_authority_transaction(
        renew, lambda: {}
    )


def plan_retirement(
    record_root: Path, archive_parent: Path, *, today: Optional[str] = None
) -> RetirementPlan:
    root = master_runtime._normalized_root(record_root)
    try:
        with master_runtime._record_lock(root, exclusive=False):
            return _plan(root, archive_parent, today or _default_today())[0]
    except (OSError, RetirementError, master_runtime.MasterRecordError) as exc:
        return RetirementPlan("REFUSED", str(exc), str(root), None, None, (), 0, 0)


def retire(
    record_root: Path,
    archive_parent: Path,
    *,
    apply: bool,
    today: Optional[str] = None,
    privacy: Optional[PrivacyCheck] = None,
    renew: Renewal = semaphore.master_renew,
    lease_snapshot: Callable[[], dict[str, Any]] = semaphore.master_snapshot,
    authority_transaction: Optional[AuthorityTransaction] = None,
    event_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> RetirementPlan:
    """Archive the retained legacy nodes and record the procedure event."""
    root = master_runtime._normalized_root(record_root)
    today = today or _default_today()
    preview = plan_retirement(root, archive_parent, today=today)
    if not apply or preview.status == "REFUSED":
        return preview
    if preview.status == "CURRENT" and preview.archive is None:
        return preview
    try:
        if privacy is not None:
            refusal = privacy(archive_parent)
            if refusal:
                raise RetirementError(refusal)
        master_runtime._renew_or_refuse(renew)
        with master_runtime._locked_record(root):
            with _authority(renew, authority_transaction)():
                if archive_parent.is_dir():
                    _check_directory(archive_parent)
                    _remove_stale_staging(archive_parent)
                plan, manifest, target = _plan(root, archive_parent, today)
                if plan.status == "PREVIEW":
                    assert manifest is not None and target is not None
                    if not archive_parent.exists():
                        archive_parent.mkdir(mode=0o700)
                    _check_directory(archive_parent)
                    staging = archive_parent / f".staging-{target.name}-{uuid.uuid4().hex[:16]}"
                    staging.mkdir(mode=0o700)
                    (staging / ARCHIVE_NODES_NAME).mkdir(mode=0o700)
                    _copy_nodes(root, staging / ARCHIVE_NODES_NAME, manifest)
                    _write_file(staging / ARCHIVE_MANIFEST_NAME, _canonical(manifest), 0o600)
                    _fsync_directory(staging)
                    read_archive(staging)
                    os.rename(staging, target)
                    _fsync_directory(archive_parent)
                    plan, manifest, target = _plan(root, archive_parent, today)
                if plan.status == "FINALIZE":
                    assert manifest is not None
                    _remove_nodes(root, manifest, plan.nodes)
                plan, manifest, target = _plan(root, archive_parent, today)
        if plan.status != "CURRENT" or manifest is None or target is None:
            raise RetirementError(f"retirement did not converge: {plan.detail}")
        _record_procedure(
            root, target, manifest, plan.manifest_sha256 or "",
            renew=renew, lease_snapshot=lease_snapshot,
            authority_transaction=authority_transaction, event_id=event_id,
        )
    except (OSError, RetirementError, master_runtime.MasterRecordError) as exc:
        status = "REFUSED"
        return RetirementPlan(status, str(exc), str(root), preview.archive, None, preview.nodes, 0, 0)
    return RetirementPlan(
        "OK",
        "legacy migration nodes archived outside the record; procedure event recorded",
        str(root), str(target), plan.manifest_sha256, tuple(manifest["nodes"]),
        len(manifest["files"]), sum(row["size"] for row in manifest["files"]),
    )


def _record_procedure(
    root: Path,
    archive: Path,
    manifest: dict[str, Any],
    digest: str,
    *,
    renew: Renewal,
    lease_snapshot: Callable[[], dict[str, Any]],
    authority_transaction: Optional[AuthorityTransaction],
    event_id: Callable[[], str],
) -> None:
    evidence = f"archive {archive} manifest sha256 {digest}"
    view = master_runtime.read_record(root)
    for event in view.events:
        payload = event["payload"]
        if (
            event["kind"] == "procedure"
            and payload.get("procedure_id") == PROCEDURE_ID
            and payload.get("action") == "retire"
            and digest in payload.get("evidence", "")
        ):
            return
    files = manifest["files"]
    master_runtime.RecordWriter(
        root,
        renew=renew,
        lease_snapshot=lease_snapshot,
        authority_transaction=authority_transaction,
        event_id=event_id,
    ).append(
        "procedure",
        {
            "procedure_id": PROCEDURE_ID,
            "action": "retire",
            "failure": (
                "every record read re-verified the completed one-time legacy "
                "migration (report, backup, retained root files), roughly "
                "doubling read cost and keeping the pre-master migrator and "
                "its tests in every check"
            ),
            "replacement": (
                f"retained nodes {', '.join(manifest['nodes'])} "
                f"({len(files)} files, {sum(r['size'] for r in files)} bytes) "
                "moved to a private archive outside the validated record"
            ),
            "control": (
                "the record layout refuses any legacy migration node; the "
                "archive manifest binds every byte and mode; "
                "`master restore-migration ARCHIVE --apply` restores them"
            ),
            "evidence": evidence,
        },
    )


def restore(
    record_root: Path,
    archive: Path,
    *,
    apply: bool,
    renew: Renewal = semaphore.master_renew,
    authority_transaction: Optional[AuthorityTransaction] = None,
) -> RetirementPlan:
    """Copy archived nodes back into the record byte for byte.

    The archive stays in place.  The restored record is readable only by a
    Creme checkout that still carries the legacy migration verifier.
    """
    root = master_runtime._normalized_root(record_root)
    try:
        manifest, digest = read_archive(archive)
        if manifest["record_root"] != str(root):
            raise RetirementError("archive belongs to a different record root")
        nodes = tuple(manifest["nodes"])
        present = _present_nodes(root)
        inventory = {"directories": manifest["directories"], "files": manifest["files"]}
        if not set(present) <= set(nodes):
            raise RetirementError("record holds legacy nodes the archive does not")
        plan = RetirementPlan(
            "PREVIEW",
            "archive verified; rerun with --apply as master to restore it",
            str(root), str(archive), digest, nodes,
            len(manifest["files"]), sum(row["size"] for row in manifest["files"]),
        )
        if not apply:
            return plan
        master_runtime._renew_or_refuse(renew)
        with master_runtime._locked_record(root):
            with _authority(renew, authority_transaction)():
                read_archive(archive)
                _copy_nodes(archive / ARCHIVE_NODES_NAME, root, inventory)
                if not _matches(root, inventory, nodes):
                    raise RetirementError("restored nodes do not match the archive manifest")
    except (OSError, RetirementError, master_runtime.MasterRecordError) as exc:
        return RetirementPlan("REFUSED", str(exc), str(root), str(archive), None, (), 0, 0)
    return RetirementPlan(
        "OK",
        "archived nodes restored byte-identical; reading this record now needs a "
        "Creme checkout that carries the legacy migration verifier",
        str(root), str(archive), digest, nodes, plan.files, plan.bytes,
    )
