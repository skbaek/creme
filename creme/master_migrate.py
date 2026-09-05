from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import secrets
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Optional, Sequence

from . import master_runtime, semaphore


MIGRATION_SCHEMA_VERSION = 1
MIGRATION_REPORT_NAME = master_runtime.MIGRATION_REPORT_NAME
BACKUP_ROOT_NAME = master_runtime.MIGRATION_BACKUP_ROOT_NAME
BACKUP_MANIFEST_NAME = "manifest.json"
BACKUP_ORIGINALS_NAME = "originals"

LEGACY_CORE_FILES = ("README.md", "board.md", "log.md")
LEGACY_DIRECTORIES = master_runtime.PRIVATE_DIRECTORIES
MAX_LEGACY_FILES = 4096
MAX_LEGACY_BYTES = master_runtime.MAX_LOG_BYTES

_ROOT_TEMP = re.compile(
    r"^\.(?:migration\.json|README\.md|events\.jsonl|board\.json)\.[0-9a-f]{16}\.tmp$"
)
_BACKUP_TEMP = re.compile(r"^\.[0-9a-f]{64}\.[0-9a-f]{16}\.tmp$")


class MigrationError(RuntimeError):
    pass


FaultInjector = Callable[[str], None]
Renewal = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class LegacyFile:
    path: str
    data: bytes
    size: int
    sha256: str

    def manifest_row(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class LegacySnapshot:
    directories: tuple[str, ...]
    files: tuple[LegacyFile, ...]
    source_snapshot_sha256: str
    translated_log: bytes
    translations: tuple[dict[str, Any], ...]
    retained_artifacts: tuple[str, ...]
    ambiguities: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class MigrationAction:
    path: str
    action: str
    detail: str
    size: Optional[int] = None
    sha256: Optional[str] = None


@dataclass(frozen=True)
class MigrationPlan:
    status: str
    detail: str
    source_snapshot_sha256: Optional[str]
    backup_id: Optional[str]
    actions: tuple[MigrationAction, ...]
    translations: tuple[dict[str, Any], ...]
    retained_artifacts: tuple[str, ...]
    ambiguities: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "status": self.status,
            "detail": self.detail,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "backup_id": self.backup_id,
            "actions": [asdict(action) for action in self.actions],
            "translations": list(self.translations),
            "retained_artifacts": list(self.retained_artifacts),
            "ambiguities": list(self.ambiguities),
        }


def _fault(injector: Optional[FaultInjector], stage: str) -> None:
    if injector is not None:
        injector(stage)


def _canonical_json(value: Any) -> bytes:
    return master_runtime._canonical_json(value)


def _safe_relative(value: str, context: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        raise MigrationError(f"{context} is not a safe relative path")
    return path


def _private_directory(path: Path) -> None:
    master_runtime._validate_owner_mode(path, 0o700, directory=True)


def _private_file(path: Path) -> None:
    master_runtime._validate_owner_mode(path, 0o600, directory=False)


def _mkdir_private(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _private_directory(path)
    except OSError as exc:
        raise MigrationError(f"could not create private directory {path.name}: {exc}") from exc


def _read_private_text_file(path: Path) -> bytes:
    _private_file(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"could not read private legacy file {path.name}: {exc}") from exc
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"legacy file {path.name} is not UTF-8") from exc
    return data


def _recognize_log(data: bytes) -> tuple[bytes, tuple[dict[str, Any], ...], Optional[str]]:
    if data and not data.endswith(b"\n"):
        return b"", (), "legacy log has a partial final line"
    events: list[dict[str, Any]] = []
    translations: list[dict[str, Any]] = []
    try:
        for ordinal, row in enumerate(data.splitlines(keepends=True), start=1):
            value = master_runtime._strict_json(row, f"legacy log row {ordinal}")
            event = master_runtime.validate_event(value)
            if _canonical_json(event) != row:
                raise master_runtime.MasterRecordError(
                    f"legacy log row {ordinal} is not canonical current-schema JSON"
                )
            events.append(event)
            translations.append(
                {
                    "source": "log.md",
                    "ordinal": ordinal,
                    "event_id": event["event_id"],
                    "source_sha256": hashlib.sha256(row).hexdigest(),
                }
            )
        master_runtime.reduce_events(events)
    except master_runtime.MasterRecordError as exc:
        return b"", (), f"legacy log is retained without translation: {exc}"
    return data, tuple(translations), None


def _snapshot_descriptor(
    directories: Sequence[str], files: Sequence[LegacyFile]
) -> dict[str, Any]:
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "directories": list(directories),
        "files": [item.manifest_row() for item in files],
    }


def _interpreted_snapshot(
    directories: Sequence[str], files: Sequence[LegacyFile]
) -> LegacySnapshot:
    ordered_directories = tuple(sorted(set(directories)))
    ordered_files = tuple(sorted(files, key=lambda item: item.path))
    log = next((item for item in ordered_files if item.path == "log.md"), None)
    translated_log = b""
    translations: tuple[dict[str, Any], ...] = ()
    ambiguities: list[dict[str, str]] = []
    if log is not None:
        translated_log, translations, ambiguity = _recognize_log(log.data)
        if ambiguity is not None:
            ambiguities.append({"path": "log.md", "detail": ambiguity})
    if any(item.path == "board.md" for item in ordered_files):
        ambiguities.append(
            {
                "path": "board.md",
                "detail": (
                    "legacy board is derived free-form data and is retained "
                    "without translation"
                ),
            }
        )
    retained = tuple(item.path for item in ordered_files if item.path != "README.md")
    descriptor = _snapshot_descriptor(ordered_directories, ordered_files)
    source_digest = hashlib.sha256(_canonical_json(descriptor)).hexdigest()
    return LegacySnapshot(
        ordered_directories,
        ordered_files,
        source_digest,
        translated_log,
        translations,
        retained,
        tuple(ambiguities),
    )


def _orphan_temp_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    try:
        root_entries = list(root.iterdir())
    except OSError as exc:
        raise MigrationError(f"could not inventory migration temporaries: {exc}") from exc
    for entry in root_entries:
        if _ROOT_TEMP.fullmatch(entry.name):
            _private_file(entry)
            paths.append(entry)
    backup_root = root / BACKUP_ROOT_NAME
    if backup_root.exists():
        _private_directory(backup_root)
        try:
            backup_entries = list(backup_root.iterdir())
        except OSError as exc:
            raise MigrationError(f"could not inventory backup temporaries: {exc}") from exc
        for entry in backup_entries:
            if _BACKUP_TEMP.fullmatch(entry.name):
                _private_directory(entry)
                master_runtime._validate_private_tree(entry)
                paths.append(entry)
    return tuple(sorted(paths, key=lambda path: str(path)))


def _cleanup_orphan_temps(
    root: Path,
    *,
    fault: Optional[FaultInjector],
) -> None:
    for index, path in enumerate(_orphan_temp_paths(root)):
        _fault(fault, f"recovery-temp-{index}:before-remove")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        master_runtime._fsync_directory(path.parent)
        _fault(fault, f"recovery-temp-{index}:after-remove")


def _scan_legacy(root: Path) -> LegacySnapshot:
    root = master_runtime._normalized_root(root)
    _private_directory(root)
    allowed_files = set(LEGACY_CORE_FILES) | {master_runtime.LOCK_NAME}
    allowed_directories = set(LEGACY_DIRECTORIES) | {BACKUP_ROOT_NAME}
    files: list[LegacyFile] = []
    directories: list[str] = []
    total_bytes = 0

    try:
        top_level = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MigrationError(f"could not inventory legacy record: {exc}") from exc
    orphan_names = {path.name for path in _orphan_temp_paths(root) if path.parent == root}
    for entry in top_level:
        if entry.name in orphan_names:
            continue
        if entry.name in {
            MIGRATION_REPORT_NAME,
            master_runtime.EVENTS_NAME,
            master_runtime.BOARD_NAME,
        }:
            raise MigrationError(
                f"partial or unexpected structured path {entry.name} refuses migration"
            )
        if entry.name == master_runtime.LOCK_NAME:
            _private_file(entry)
            continue
        if entry.name == BACKUP_ROOT_NAME:
            _private_directory(entry)
            continue
        if entry.name in allowed_files:
            data = _read_private_text_file(entry)
            relative = entry.name
            total_bytes += len(data)
            files.append(
                LegacyFile(relative, data, len(data), hashlib.sha256(data).hexdigest())
            )
            continue
        if entry.name not in allowed_directories:
            raise MigrationError(f"unexpected legacy path {entry.name} refuses migration")
        _private_directory(entry)
        directories.append(entry.name)
        for path in sorted(entry.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise MigrationError(f"legacy path {relative} must not be a symlink")
            if path.is_dir():
                _private_directory(path)
                directories.append(relative)
                continue
            data = _read_private_text_file(path)
            total_bytes += len(data)
            files.append(
                LegacyFile(relative, data, len(data), hashlib.sha256(data).hexdigest())
            )
    files.sort(key=lambda item: item.path)
    directories = sorted(set(directories))
    if not any(item.path in LEGACY_CORE_FILES for item in files):
        raise MigrationError("no legacy core file is available to migrate")
    if len(files) > MAX_LEGACY_FILES or total_bytes > MAX_LEGACY_BYTES:
        raise MigrationError("legacy record exceeds the supported migration bound")

    return _interpreted_snapshot(directories, files)


def _manifest(snapshot: LegacySnapshot, backup_id: str) -> dict[str, Any]:
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "backup_id": backup_id,
        "source_snapshot_sha256": snapshot.source_snapshot_sha256,
        "directories": list(snapshot.directories),
        "files": [item.manifest_row() for item in snapshot.files],
    }


def _report(
    snapshot: LegacySnapshot,
    backup_id: str,
    manifest_sha256: str,
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "status": status,
        "source_snapshot_sha256": snapshot.source_snapshot_sha256,
        "backup": {
            "id": backup_id,
            "manifest": f"{BACKUP_ROOT_NAME}/{backup_id}/{BACKUP_MANIFEST_NAME}",
            "manifest_sha256": manifest_sha256,
        },
        "translated_log_sha256": hashlib.sha256(snapshot.translated_log).hexdigest(),
        "translations": list(snapshot.translations),
        "retained_artifacts": list(snapshot.retained_artifacts),
        "ambiguities": list(snapshot.ambiguities),
    }


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_manifest(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "backup_id",
        "source_snapshot_sha256",
        "directories",
        "files",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MigrationError("backup manifest has an unexpected shape")
    if value["schema_version"] != MIGRATION_SCHEMA_VERSION or isinstance(
        value["schema_version"], bool
    ):
        raise MigrationError("backup manifest schema is unsupported")
    for key in ("backup_id", "source_snapshot_sha256"):
        if not _valid_digest(value[key]):
            raise MigrationError(f"backup manifest {key} is invalid")
    directories = value["directories"]
    files = value["files"]
    if not isinstance(directories, list) or not all(
        isinstance(item, str) for item in directories
    ):
        raise MigrationError("backup manifest directories are invalid")
    if directories != sorted(set(directories)):
        raise MigrationError("backup manifest directories are not canonical")
    for item in directories:
        _safe_relative(item, "backup directory")
    if not isinstance(files, list):
        raise MigrationError("backup manifest files are invalid")
    names: list[str] = []
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            raise MigrationError("backup manifest file row is invalid")
        if not isinstance(row["path"], str):
            raise MigrationError("backup manifest file path is invalid")
        _safe_relative(row["path"], "backup file")
        if (
            isinstance(row["size"], bool)
            or not isinstance(row["size"], int)
            or row["size"] < 0
        ):
            raise MigrationError("backup manifest file size is invalid")
        if not _valid_digest(row["sha256"]):
            raise MigrationError("backup manifest file digest is invalid")
        names.append(row["path"])
    if names != sorted(set(names)):
        raise MigrationError("backup manifest files are not canonical")
    return value


def _read_canonical_json(path: Path, context: str) -> tuple[dict[str, Any], bytes]:
    _private_file(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"could not read {context}: {exc}") from exc
    try:
        value = master_runtime._strict_json(data, context)
    except master_runtime.MasterRecordError as exc:
        raise MigrationError(str(exc)) from exc
    if not isinstance(value, dict) or _canonical_json(value) != data:
        raise MigrationError(f"{context} is not canonical JSON")
    return value, data


def _verified_backup(
    root: Path, backup_id: str
) -> tuple[dict[str, Any], bytes, LegacySnapshot]:
    if not _valid_digest(backup_id):
        raise MigrationError("backup id is invalid")
    backup = root / BACKUP_ROOT_NAME / backup_id
    _private_directory(root / BACKUP_ROOT_NAME)
    _private_directory(backup)
    _private_directory(backup / BACKUP_ORIGINALS_NAME)
    manifest, manifest_bytes = _read_canonical_json(
        backup / BACKUP_MANIFEST_NAME, "backup manifest"
    )
    _validate_manifest(manifest)
    if manifest["backup_id"] != backup_id:
        raise MigrationError("backup manifest id does not match its directory")
    originals = backup / BACKUP_ORIGINALS_NAME
    expected_directories = set(manifest["directories"])
    expected_files = {row["path"]: row for row in manifest["files"]}
    observed_directories: set[str] = set()
    observed_files: set[str] = set()
    legacy_files: list[LegacyFile] = []
    for path in sorted(originals.rglob("*")):
        relative = path.relative_to(originals).as_posix()
        if path.is_symlink():
            raise MigrationError(f"backup path {relative} must not be a symlink")
        if path.is_dir():
            _private_directory(path)
            observed_directories.add(relative)
            continue
        _private_file(path)
        observed_files.add(relative)
        row = expected_files.get(relative)
        if row is None:
            raise MigrationError(f"unexpected backup file {relative}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MigrationError(f"could not read backup file {relative}: {exc}") from exc
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise MigrationError(f"backup file {relative} does not match its manifest")
        legacy_files.append(LegacyFile(relative, data, len(data), row["sha256"]))
    if observed_directories != expected_directories or observed_files != set(expected_files):
        raise MigrationError("backup tree does not match its manifest")
    snapshot = _interpreted_snapshot(manifest["directories"], legacy_files)
    if snapshot.source_snapshot_sha256 != manifest["source_snapshot_sha256"]:
        raise MigrationError("backup manifest source digest does not match its inventory")
    if manifest["backup_id"] != snapshot.source_snapshot_sha256:
        raise MigrationError("backup id does not match the source snapshot")
    return manifest, manifest_bytes, snapshot


def verify_backup(root: Path, backup_id: str) -> dict[str, Any]:
    manifest, manifest_bytes, _ = _verified_backup(root, backup_id)
    return {
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def _validate_report(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "status",
        "source_snapshot_sha256",
        "backup",
        "translated_log_sha256",
        "translations",
        "retained_artifacts",
        "ambiguities",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MigrationError("migration report has an unexpected shape")
    if value["schema_version"] != MIGRATION_SCHEMA_VERSION or isinstance(
        value["schema_version"], bool
    ):
        raise MigrationError("migration report schema is unsupported")
    if value["status"] not in {"prepared", "complete"}:
        raise MigrationError("migration report status is invalid")
    for key in ("source_snapshot_sha256", "translated_log_sha256"):
        if not _valid_digest(value[key]):
            raise MigrationError(f"migration report {key} is invalid")
    backup = value["backup"]
    if not isinstance(backup, dict) or set(backup) != {
        "id",
        "manifest",
        "manifest_sha256",
    }:
        raise MigrationError("migration report backup reference is invalid")
    if not all(isinstance(backup[key], str) for key in backup):
        raise MigrationError("migration report backup values are invalid")
    if not _valid_digest(backup["id"]) or not _valid_digest(backup["manifest_sha256"]):
        raise MigrationError("migration report backup digests are invalid")
    expected_manifest = f"{BACKUP_ROOT_NAME}/{backup['id']}/{BACKUP_MANIFEST_NAME}"
    if backup["manifest"] != expected_manifest:
        raise MigrationError("migration report backup path is invalid")
    for key in ("translations", "retained_artifacts", "ambiguities"):
        if not isinstance(value[key], list):
            raise MigrationError(f"migration report {key} is invalid")
    return value


def _verified_report(root: Path) -> tuple[dict[str, Any], bytes, LegacySnapshot]:
    report, report_bytes = _read_canonical_json(
        root / MIGRATION_REPORT_NAME, "migration report"
    )
    _validate_report(report)
    backup_root = root / BACKUP_ROOT_NAME
    _private_directory(backup_root)
    try:
        backup_entries = sorted(
            entry.name
            for entry in backup_root.iterdir()
            if _BACKUP_TEMP.fullmatch(entry.name) is None
        )
    except OSError as exc:
        raise MigrationError(f"could not inspect migration backup namespace: {exc}") from exc
    if backup_entries != [report["backup"]["id"]]:
        raise MigrationError("migration backup namespace does not match the report")
    manifest, manifest_bytes, snapshot = _verified_backup(root, report["backup"]["id"])
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != report["backup"]["manifest_sha256"]:
        raise MigrationError("migration report manifest digest does not match the backup")
    if manifest["source_snapshot_sha256"] != report["source_snapshot_sha256"]:
        raise MigrationError("migration report source digest does not match the backup")
    expected = _report(
        snapshot,
        report["backup"]["id"],
        manifest_sha256,
        status=report["status"],
    )
    if report != expected:
        raise MigrationError("migration report does not match the verified backup interpretation")
    return report, report_bytes, snapshot


def _read_private_bytes(path: Path, context: str) -> bytes:
    _private_file(path)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"could not read {context}: {exc}") from exc


def _verify_snapshot_boundary(
    root: Path,
    snapshot: LegacySnapshot,
    *,
    current_readme: bool,
) -> None:
    """Verify the legacy source on the prepared side of the authority marker.

    The complete migration report is the atomic handoff marker.  This check is
    run on both sides of publishing it, so a writer in any publication window
    either changes the prepared source and refuses or lands after the handoff
    as a separately owned current-runtime input.  Root legacy core files remain
    sealed against the backup after completion as well.
    """
    expected_files = {item.path: item for item in snapshot.files}
    known_root_names = {
        *LEGACY_CORE_FILES,
        *LEGACY_DIRECTORIES,
        master_runtime.LOCK_NAME,
        master_runtime.EVENTS_NAME,
        master_runtime.BOARD_NAME,
        MIGRATION_REPORT_NAME,
        BACKUP_ROOT_NAME,
    }
    known_root_names.update(
        path.name for path in _orphan_temp_paths(root) if path.parent == root
    )
    try:
        observed_root_names = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise MigrationError(f"could not inventory prepared migration root: {exc}") from exc
    unexpected = sorted(observed_root_names - known_root_names)
    if unexpected:
        raise MigrationError(f"unexpected path appeared during migration: {unexpected[0]}")

    readme = root / master_runtime.README_NAME
    legacy_readme = expected_files.get(master_runtime.README_NAME)
    if readme.exists():
        data = _read_private_bytes(readme, "migration README")
        permitted = {master_runtime._README.encode("utf-8")}
        if not current_readme and legacy_readme is not None:
            permitted.add(legacy_readme.data)
        if data not in permitted:
            raise MigrationError("migration README changed outside the authority handoff")
    elif current_readme or legacy_readme is not None:
        raise MigrationError("migration README disappeared during authority handoff")

    for name in ("log.md", "board.md"):
        expected = expected_files.get(name)
        path = root / name
        if expected is None:
            if path.exists() or path.is_symlink():
                raise MigrationError(f"legacy source {name} appeared during migration")
            continue
        if _read_private_bytes(path, f"legacy source {name}") != expected.data:
            raise MigrationError(f"legacy source {name} changed during migration")

    expected_private_files = {
        path: item
        for path, item in expected_files.items()
        if PurePosixPath(path).parts[0] in LEGACY_DIRECTORIES
    }
    expected_private_directories = set(snapshot.directories) | set(LEGACY_DIRECTORIES)
    observed_private_files: dict[str, bytes] = {}
    observed_private_directories: set[str] = set()
    for name in LEGACY_DIRECTORIES:
        top = root / name
        if not top.exists():
            raise MigrationError(f"private migration directory {name} is missing")
        _private_directory(top)
        observed_private_directories.add(name)
        pending = [top]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                raise MigrationError(
                    f"could not inventory private migration directory {directory.name}: {exc}"
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise MigrationError(
                        f"could not inspect legacy path {relative}: {exc}"
                    ) from exc
                if stat.S_ISLNK(info.st_mode):
                    raise MigrationError(f"legacy path {relative} must not be a symlink")
                if stat.S_ISDIR(info.st_mode):
                    _private_directory(path)
                    observed_private_directories.add(relative)
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    observed_private_files[relative] = _read_private_bytes(
                        path, f"legacy path {relative}"
                    )
                else:
                    raise MigrationError(
                        f"legacy path {relative} must be a regular file or directory"
                    )
    if observed_private_directories != expected_private_directories:
        raise MigrationError("legacy private directory population changed during migration")
    if set(observed_private_files) != set(expected_private_files):
        raise MigrationError("legacy private file population changed during migration")
    for relative, expected in expected_private_files.items():
        if observed_private_files[relative] != expected.data:
            raise MigrationError(f"legacy source {relative} changed during migration")


def _verify_completed_legacy_core(root: Path, snapshot: LegacySnapshot) -> None:
    """Keep obsolete root-level writer paths sealed after the handoff."""
    expected = {item.path: item for item in snapshot.files}
    readme = _read_private_bytes(root / master_runtime.README_NAME, "migration README")
    if readme != master_runtime._README.encode("utf-8"):
        raise MigrationError("current migration README does not match the runtime guide")
    for name in ("log.md", "board.md"):
        item = expected.get(name)
        path = root / name
        if item is None:
            if path.exists() or path.is_symlink():
                raise MigrationError(f"obsolete legacy writer path {name} reappeared")
        elif _read_private_bytes(path, f"retained legacy source {name}") != item.data:
            raise MigrationError(f"retained legacy source {name} changed after migration")


def _migration_root_nodes(
    root: Path,
    orphan_temps: Sequence[Path] = (),
) -> tuple[str, ...]:
    candidates = {
        MIGRATION_REPORT_NAME,
        BACKUP_ROOT_NAME,
        *master_runtime.MIGRATION_RETAINED_ROOT_FILES,
    }
    names = {
        name
        for name in candidates
        if (root / name).exists() or (root / name).is_symlink()
    }
    names.update(path.name for path in orphan_temps if path.parent == root)
    return tuple(sorted(names))


def _read_migration_record(
    root: Path,
    orphan_temps: Sequence[Path] = (),
) -> master_runtime.RecordView:
    return master_runtime.read_record(
        root,
        _migration_root_nodes=_migration_root_nodes(root, orphan_temps),
    )


def _current_view(
    root: Path,
    orphan_temps: Sequence[Path] = (),
) -> Optional[master_runtime.RecordView]:
    if not (root / master_runtime.EVENTS_NAME).exists() and not (
        root / master_runtime.BOARD_NAME
    ).exists():
        return None
    try:
        return _read_migration_record(root, orphan_temps)
    except master_runtime.MasterRecordError:
        return None


def _idempotent_plan(
    root: Path,
    orphan_temps: Sequence[Path] = (),
) -> Optional[MigrationPlan]:
    report_path = root / MIGRATION_REPORT_NAME
    current = _current_view(root, orphan_temps)
    if not report_path.exists():
        if current is None:
            return None
        return MigrationPlan(
            "CURRENT",
            "structured record is current; no legacy migration is required",
            None,
            None,
            (),
            (),
            (),
            (),
        )
    try:
        report, _, snapshot = _verified_report(root)
    except MigrationError as exc:
        return MigrationPlan("REFUSED", str(exc), None, None, (), (), (), ())
    backup_id = report["backup"]["id"]
    if current is None:
        if report["status"] == "prepared":
            return MigrationPlan(
                "FINALIZE",
                "prepared migration and backup verify; reconstruct and complete with --apply",
                report["source_snapshot_sha256"],
                backup_id,
                (
                    MigrationAction(master_runtime.README_NAME, "create-or-replace", "current runtime guide"),
                    MigrationAction(master_runtime.EVENTS_NAME, "create-or-keep", "translated event log"),
                    MigrationAction(master_runtime.BOARD_NAME, "create-or-keep", "deterministic board"),
                    MigrationAction(MIGRATION_REPORT_NAME, "replace", "mark prepared report complete"),
                ),
                tuple(report["translations"]),
                tuple(report["retained_artifacts"]),
                tuple(report["ambiguities"]),
            )
        return MigrationPlan(
            "REFUSED",
            "complete migration has no verifiable structured record",
            report["source_snapshot_sha256"],
            backup_id,
            (),
            tuple(report["translations"]),
            tuple(report["retained_artifacts"]),
            tuple(report["ambiguities"]),
        )
    translated_log_matches = (
        current.log_bytes == snapshot.translated_log
        if report["status"] == "prepared"
        else current.log_bytes.startswith(snapshot.translated_log)
    )
    if not translated_log_matches:
        return MigrationPlan(
            "REFUSED",
            "structured log does not preserve the migration report prefix",
            report["source_snapshot_sha256"],
            backup_id,
            (),
            tuple(report["translations"]),
            tuple(report["retained_artifacts"]),
            tuple(report["ambiguities"]),
        )
    if report["status"] == "prepared":
        try:
            _verify_snapshot_boundary(root, snapshot, current_readme=True)
        except MigrationError as exc:
            return MigrationPlan(
                "REFUSED",
                str(exc),
                report["source_snapshot_sha256"],
                backup_id,
                (),
                tuple(report["translations"]),
                tuple(report["retained_artifacts"]),
                tuple(report["ambiguities"]),
            )
        return MigrationPlan(
            "FINALIZE",
            "structured record and backup verify; complete the prepared report with --apply",
            report["source_snapshot_sha256"],
            backup_id,
            (MigrationAction(MIGRATION_REPORT_NAME, "replace", "mark prepared report complete"),),
            tuple(report["translations"]),
            tuple(report["retained_artifacts"]),
            tuple(report["ambiguities"]),
        )
    try:
        _verify_completed_legacy_core(root, snapshot)
    except MigrationError as exc:
        return MigrationPlan(
            "REFUSED",
            str(exc),
            report["source_snapshot_sha256"],
            backup_id,
            (),
            tuple(report["translations"]),
            tuple(report["retained_artifacts"]),
            tuple(report["ambiguities"]),
        )
    return MigrationPlan(
        "CURRENT",
        "migration, backup, and structured record are current and verified",
        report["source_snapshot_sha256"],
        backup_id,
        (),
        tuple(report["translations"]),
        tuple(report["retained_artifacts"]),
        tuple(report["ambiguities"]),
    )


def _backup_namespace_is_available(root: Path, backup_id: str) -> bool:
    backup_root = root / BACKUP_ROOT_NAME
    if not backup_root.exists():
        return True
    _private_directory(backup_root)
    try:
        entries = [
            entry
            for entry in backup_root.iterdir()
            if _BACKUP_TEMP.fullmatch(entry.name) is None
        ]
    except OSError as exc:
        raise MigrationError(f"could not inspect backup namespace: {exc}") from exc
    if not entries:
        return True
    if len(entries) == 1 and entries[0].name == backup_id:
        verified = verify_backup(root, backup_id)
        if verified["manifest"]["source_snapshot_sha256"] != backup_id:
            raise MigrationError("backup collision refuses migration")
        return False
    raise MigrationError("unexpected backup namespace entry refuses migration")


def _migration_actions(
    snapshot: LegacySnapshot,
    backup_id: str,
    *,
    create_backup: bool,
) -> tuple[MigrationAction, ...]:
    actions: list[MigrationAction] = []
    for item in snapshot.files:
        actions.append(
            MigrationAction(
                f"{BACKUP_ROOT_NAME}/{backup_id}/{BACKUP_ORIGINALS_NAME}/{item.path}",
                "backup" if create_backup else "keep",
                (
                    "create byte-identical legacy original"
                    if create_backup
                    else "verified byte-identical legacy original"
                ),
                item.size,
                item.sha256,
            )
        )
    actions.append(
        MigrationAction(
            f"{BACKUP_ROOT_NAME}/{backup_id}/{BACKUP_MANIFEST_NAME}",
            "create" if create_backup else "keep",
            (
                "create canonical size and SHA-256 manifest"
                if create_backup
                else "verified canonical size and SHA-256 manifest"
            ),
        )
    )
    for item in snapshot.retained_artifacts:
        actions.append(
            MigrationAction(item, "retain", "legacy evidence remains byte-identical")
        )
    readme_action = (
        "replace"
        if any(item.path == master_runtime.README_NAME for item in snapshot.files)
        else "create"
    )
    actions.extend(
        [
            MigrationAction(master_runtime.LOCK_NAME, "create-or-keep", "private record lock"),
            MigrationAction(
                master_runtime.README_NAME,
                readme_action,
                "current private runtime guide",
            ),
            MigrationAction(
                master_runtime.EVENTS_NAME,
                "create",
                "translated authoritative event log",
            ),
            MigrationAction(master_runtime.BOARD_NAME, "create", "deterministic board projection"),
            MigrationAction(
                MIGRATION_REPORT_NAME,
                "create",
                "prepared then complete migration report",
            ),
        ]
    )
    for directory in LEGACY_DIRECTORIES:
        if directory not in snapshot.directories:
            actions.append(
                MigrationAction(f"{directory}/", "create", "standard private directory")
            )
    return tuple(actions)


def plan_migration(root: Path) -> MigrationPlan:
    try:
        root = master_runtime._normalized_root(root)
        _private_directory(root)
        orphan_temps = _orphan_temp_paths(root)
        idempotent = _idempotent_plan(root, orphan_temps)
        if idempotent is not None:
            if not orphan_temps:
                return idempotent
            if idempotent.status == "CURRENT":
                return MigrationPlan(
                    "FINALIZE",
                    "remove verified orphaned migration temporaries with --apply",
                    idempotent.source_snapshot_sha256,
                    idempotent.backup_id,
                    tuple(
                        MigrationAction(
                            path.relative_to(root).as_posix(),
                            "remove",
                            "verified private temporary from an interrupted migration",
                        )
                        for path in orphan_temps
                    ),
                    idempotent.translations,
                    idempotent.retained_artifacts,
                    idempotent.ambiguities,
                )
            return MigrationPlan(
                idempotent.status,
                idempotent.detail,
                idempotent.source_snapshot_sha256,
                idempotent.backup_id,
                tuple(
                    MigrationAction(
                        path.relative_to(root).as_posix(),
                        "remove",
                        "verified private temporary from an interrupted migration",
                    )
                    for path in orphan_temps
                ) + idempotent.actions,
                idempotent.translations,
                idempotent.retained_artifacts,
                idempotent.ambiguities,
            )
        snapshot = _scan_legacy(root)
        backup_id = snapshot.source_snapshot_sha256
        create_backup = _backup_namespace_is_available(root, backup_id)
        return MigrationPlan(
            "PREVIEW",
            "legacy migration preview; rerun with --apply while holding the schema-3 lease",
            snapshot.source_snapshot_sha256,
            backup_id,
            tuple(
                MigrationAction(
                    path.relative_to(root).as_posix(),
                    "remove",
                    "verified private temporary from an interrupted migration",
                )
                for path in orphan_temps
            ) + _migration_actions(snapshot, backup_id, create_backup=create_backup),
            snapshot.translations,
            snapshot.retained_artifacts,
            snapshot.ambiguities,
        )
    except (MigrationError, master_runtime.MasterRecordError) as exc:
        return MigrationPlan("REFUSED", str(exc), None, None, (), (), (), ())


def _renew_or_refuse(renew: Renewal) -> None:
    try:
        ok, detail = renew()
    except Exception as exc:
        raise master_runtime.RenewalRefused(f"master renewal failed: {exc}") from exc
    if not ok:
        raise master_runtime.RenewalRefused(f"master renewal refused: {detail}")


@contextlib.contextmanager
def _migration_lock(
    root: Path,
    renew: Renewal,
    fault: Optional[FaultInjector],
) -> Iterator[None]:
    root = master_runtime._normalized_root(root)
    _private_directory(root)
    local = master_runtime._thread_lock(root)
    with local:
        root_descriptor = os.open(root, os.O_RDONLY)
        try:
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
            _renew_or_refuse(renew)
            lock_path = root / master_runtime.LOCK_NAME
            if not lock_path.exists():
                _fault(fault, "lock:before-create")
                master_runtime._write_new_file(lock_path, b"")
                master_runtime._fsync_directory(root)
                _fault(fault, "lock:after-create")
            _private_file(lock_path)
            flags = os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(lock_path, flags)
            try:
                opened = os.fstat(descriptor)
                named = lock_path.lstat()
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise MigrationError("record lock changed during migration open")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                _renew_or_refuse(renew)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        finally:
            fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            os.close(root_descriptor)


def _publish_backup(
    root: Path,
    snapshot: LegacySnapshot,
    backup_id: str,
    *,
    fault: Optional[FaultInjector],
) -> tuple[dict[str, Any], bytes]:
    backup_root = root / BACKUP_ROOT_NAME
    _fault(fault, "backup:before-root")
    _mkdir_private(backup_root)
    master_runtime._fsync_directory(root)
    _fault(fault, "backup:after-root")
    entries = list(backup_root.iterdir())
    if entries:
        if len(entries) != 1 or entries[0].name != backup_id:
            raise MigrationError("backup namespace changed or collided before publication")
        manifest, manifest_bytes, existing = _verified_backup(root, backup_id)
        if existing.source_snapshot_sha256 != snapshot.source_snapshot_sha256:
            raise MigrationError("existing backup does not match the legacy snapshot")
        return manifest, manifest_bytes
    temporary = backup_root / f".{backup_id}.{secrets.token_hex(8)}.tmp"
    final = backup_root / backup_id
    published = False
    try:
        _mkdir_private(temporary)
        originals = temporary / BACKUP_ORIGINALS_NAME
        _mkdir_private(originals)
        for directory in sorted(
            snapshot.directories, key=lambda value: (len(PurePosixPath(value).parts), value)
        ):
            target = originals / Path(directory)
            _mkdir_private(target)
        for index, item in enumerate(snapshot.files):
            target = originals / Path(item.path)
            if target.parent != originals:
                _private_directory(target.parent)
            _fault(fault, f"backup-file-{index}:before-write")
            master_runtime._write_new_file(target, item.data)
            _fault(fault, f"backup-file-{index}:after-write")
        manifest = _manifest(snapshot, backup_id)
        manifest_bytes = _canonical_json(manifest)
        _fault(fault, "backup-manifest:before-write")
        master_runtime._write_new_file(temporary / BACKUP_MANIFEST_NAME, manifest_bytes)
        _fault(fault, "backup-manifest:after-write")
        master_runtime._fsync_directory(originals)
        master_runtime._fsync_directory(temporary)
        _fault(fault, "backup:before-publish")
        if final.exists():
            raise MigrationError("backup collision refuses migration")
        os.rename(temporary, final)
        published = True
        _fault(fault, "backup:after-publish")
        master_runtime._fsync_directory(backup_root)
        _fault(fault, "backup:after-dir-fsync")
    except OSError as exc:
        raise MigrationError(f"could not publish migration backup: {exc}") from exc
    finally:
        if not published and temporary.exists():
            shutil.rmtree(temporary)
    verified = verify_backup(root, backup_id)
    return verified["manifest"], manifest_bytes


def _atomic_report(
    root: Path,
    report: dict[str, Any],
    *,
    label: str,
    fault: Optional[FaultInjector],
) -> None:
    master_runtime._atomic_replace(
        root / MIGRATION_REPORT_NAME,
        _canonical_json(report),
        label=label,
        fault=fault,
    )


def _publish_or_keep(
    path: Path,
    data: bytes,
    *,
    label: str,
    fault: Optional[FaultInjector],
) -> None:
    if path.exists() or path.is_symlink():
        if _read_private_bytes(path, label) != data:
            raise MigrationError(f"existing {path.name} conflicts with prepared migration")
        return
    master_runtime._atomic_replace(path, data, label=label, fault=fault)


def _publish_prepared_runtime(
    root: Path,
    snapshot: LegacySnapshot,
    *,
    fault: Optional[FaultInjector],
) -> master_runtime.RecordView:
    for index, directory in enumerate(LEGACY_DIRECTORIES):
        path = root / directory
        if not path.exists():
            _fault(fault, f"runtime-directory-{index}:before-create")
            _mkdir_private(path)
            master_runtime._fsync_directory(root)
            _fault(fault, f"runtime-directory-{index}:after-create")
        else:
            _private_directory(path)

    _verify_snapshot_boundary(root, snapshot, current_readme=False)
    readme_path = root / master_runtime.README_NAME
    readme_bytes = master_runtime._README.encode("utf-8")
    if not readme_path.exists() or _read_private_bytes(readme_path, "migration README") != readme_bytes:
        master_runtime._atomic_replace(
            readme_path,
            readme_bytes,
            label="migration-readme",
            fault=fault,
        )

    events: list[dict[str, Any]] = []
    for row in snapshot.translated_log.splitlines(keepends=True):
        events.append(
            master_runtime.validate_event(
                master_runtime._strict_json(row, "translated event")
            )
        )
    board_bytes = master_runtime.render_board(events)
    _publish_or_keep(
        root / master_runtime.EVENTS_NAME,
        snapshot.translated_log,
        label="migration-log",
        fault=fault,
    )
    _fault(fault, "migration:after-log-commit")
    _publish_or_keep(
        root / master_runtime.BOARD_NAME,
        board_bytes,
        label="migration-board",
        fault=fault,
    )
    _verify_snapshot_boundary(root, snapshot, current_readme=True)
    current = _read_migration_record(root)
    if not current.board_current or current.log_bytes != snapshot.translated_log:
        raise MigrationError("published structured record does not match prepared migration")
    return current


def _finalize_prepared(
    root: Path,
    *,
    fault: Optional[FaultInjector],
) -> MigrationPlan:
    report, _, snapshot = _verified_report(root)
    if report["status"] != "prepared":
        raise MigrationError("migration report is not prepared")
    current = _publish_prepared_runtime(root, snapshot, fault=fault)
    if current.expected_board["source"]["log_digest"] != report["translated_log_sha256"]:
        raise MigrationError("structured log does not match prepared migration report")
    # This is the last check on the prepared side.  The atomic report replace
    # below is the authority handoff; the same snapshot is checked immediately
    # after it so every injected publication window is covered on one side.
    _verify_snapshot_boundary(root, snapshot, current_readme=True)
    complete = {**report, "status": "complete"}
    _atomic_report(root, complete, label="migration-report-complete", fault=fault)
    verified, _, _ = _verified_report(root)
    if verified["status"] != "complete":
        raise MigrationError("migration report did not become complete")
    _verify_snapshot_boundary(root, snapshot, current_readme=True)
    current = _read_migration_record(root)
    if not current.board_current or current.log_bytes != snapshot.translated_log:
        raise MigrationError("completed structured record no longer matches its handoff")
    return MigrationPlan(
        "OK",
        "prepared migration report completed and verified",
        complete["source_snapshot_sha256"],
        complete["backup"]["id"],
        (MigrationAction(MIGRATION_REPORT_NAME, "replaced", "migration is complete"),),
        tuple(complete["translations"]),
        tuple(complete["retained_artifacts"]),
        tuple(complete["ambiguities"]),
    )


def migrate(
    root: Path,
    *,
    apply: bool,
    renew: Renewal = semaphore.master_renew,
    fault: Optional[FaultInjector] = None,
) -> MigrationPlan:
    preview = plan_migration(root)
    if not apply or preview.status in {"CURRENT", "REFUSED"}:
        return preview
    if preview.status not in {"PREVIEW", "FINALIZE"}:
        return MigrationPlan(
            "REFUSED",
            "migration plan is not applicable",
            None,
            None,
            (),
            (),
            (),
            (),
        )
    try:
        _renew_or_refuse(renew)
        with _migration_lock(root, renew, fault):
            _cleanup_orphan_temps(root, fault=fault)
            locked = plan_migration(root)
            if locked.status == "CURRENT" and preview.status == "FINALIZE":
                return MigrationPlan(
                    "OK",
                    "orphaned migration temporaries removed; current record verified",
                    locked.source_snapshot_sha256,
                    locked.backup_id,
                    preview.actions,
                    locked.translations,
                    locked.retained_artifacts,
                    locked.ambiguities,
                )
            if locked.status == "FINALIZE":
                return _finalize_prepared(root, fault=fault)
            if locked.status != "PREVIEW":
                raise MigrationError(
                    f"migration changed after preview: {locked.status}: {locked.detail}"
                )
            if locked.source_snapshot_sha256 != preview.source_snapshot_sha256:
                raise MigrationError("legacy source changed after preview")
            snapshot = _scan_legacy(root)
            backup_id = snapshot.source_snapshot_sha256
            manifest, manifest_bytes = _publish_backup(
                root, snapshot, backup_id, fault=fault
            )
            if manifest["source_snapshot_sha256"] != snapshot.source_snapshot_sha256:
                raise MigrationError("published backup does not match the legacy snapshot")
            after_backup = _scan_legacy(root)
            if after_backup.source_snapshot_sha256 != snapshot.source_snapshot_sha256:
                raise MigrationError("legacy source changed during backup")
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            prepared = _report(
                snapshot,
                backup_id,
                manifest_sha256,
                status="prepared",
            )
            _atomic_report(
                root,
                prepared,
                label="migration-report-prepared",
                fault=fault,
            )
            completed = _finalize_prepared(root, fault=fault)
            return MigrationPlan(
                "OK",
                "legacy record migrated with a verified byte-identical backup",
                completed.source_snapshot_sha256,
                completed.backup_id,
                tuple(
                    MigrationAction(
                        action.path,
                        (
                            "completed"
                            if action.action not in {"retain", "keep", "create-or-keep"}
                            else action.action
                        ),
                        action.detail,
                        action.size,
                        action.sha256,
                    )
                    for action in locked.actions
                ),
                completed.translations,
                completed.retained_artifacts,
                completed.ambiguities,
            )
    except Exception as exc:
        return MigrationPlan(
            "REFUSED",
            str(exc),
            preview.source_snapshot_sha256,
            preview.backup_id,
            preview.actions,
            preview.translations,
            preview.retained_artifacts,
            preview.ambiguities,
        )
