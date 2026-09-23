from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import cli, master_operations, master_retire, master_runtime, semaphore
from creme.profile import propose, write_reviewed
from test_master_operations import FixtureAdapter


def _canonical(value) -> bytes:
    return master_runtime._canonical_json(value)


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.write_bytes(data)
    path.chmod(mode)


def _tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        result[path.relative_to(root).as_posix()] = (stat.S_IMODE(info.st_mode), data)
    return result


class MasterRetireTest(unittest.TestCase):
    """A completed legacy migration is archived outside the record, reversibly."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.creme = self.workspace / "creme"
        (self.creme / ".creme").mkdir(parents=True)
        self.store = self.workspace / "goals"
        self.store.mkdir()
        subprocess.run(["git", "init", "-q", str(self.store)], check=True)
        (self.store / ".gitignore").write_text(
            "/master/\n/master-archive/\n", encoding="utf-8"
        )
        adapter = FixtureAdapter()
        candidate = propose(self.creme, self.workspace, adapter, goal_store="goals")
        write_reviewed(self.creme / ".creme/host-profile.json", candidate)
        self.location = master_operations.resolve_runtime_location(
            self.creme, adapter=adapter
        )
        self.assertEqual(master_operations.initialize(self.location, apply=True).status, "OK")
        self.root = self.location.record_root
        self.archive_parent = self.store / master_retire.ARCHIVE_ROOT_NAME
        environment = mock.patch.dict(os.environ, {
            "CREME_SEMAPHORE_DIR": str(self.workspace / "semaphore"),
            "CREME_MASTER_SESSION_ID": "",
            "CREME_MASTER_LIVENESS_SOCKET": "",
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        client = mock.patch(
            "creme.semaphore._client_process",
            return_value=(os.getpid(), "claude", "synthetic client"),
        )
        client.start()
        self.addCleanup(client.stop)
        self.acquire()
        code, result = self.event("before retirement")
        self.assertEqual(code, 0, result)
        self.legacy = self.write_migrated_legacy()

    # ------------------------------------------------------------ fixtures

    def acquire(self):
        ok, detail = semaphore.master_acquire("claude", "synthetic retire")
        self.assertTrue(ok, detail)

    def cli(self, *arguments, stdin_bytes=None):
        output = io.StringIO()
        patches = [
            mock.patch("creme.cli._master_location", return_value=(self.location, None)),
            mock.patch("sys.stdout", output),
        ]
        if stdin_bytes is not None:
            patches.append(
                mock.patch("creme.cli.sys.stdin", SimpleNamespace(buffer=io.BytesIO(stdin_bytes)))
            )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            code = cli.main(list(arguments))
        return code, json.loads(output.getvalue())

    def event(self, title):
        payload = {
            "title": title,
            "note": "synthetic note",
            "evidence": "synthetic-evidence.json",
            "next_unit": "next",
        }
        data = json.dumps({"kind": "note", "payload": payload}).encode()
        return self.cli("master", "event", "--from", "-", stdin_bytes=data)

    def write_migrated_legacy(self) -> dict[str, tuple[int, bytes | None]]:
        """Lay down the nodes a completed legacy migration leaves behind."""
        originals = {
            "README.md": b"# legacy readme\n",
            "board.md": b"# legacy board\n",
            "log.md": b"legacy log row\n",
            "observations.md": b"legacy observations\n",
            "briefs/old.md": b"old brief\n",
        }
        rows = [
            {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for path, data in sorted(originals.items())
        ]
        backup_id = hashlib.sha256(b"synthetic snapshot").hexdigest()
        manifest = _canonical({
            "schema_version": 1,
            "backup_id": backup_id,
            "source_snapshot_sha256": backup_id,
            "directories": ["briefs"],
            "files": rows,
        })
        backups = self.root / "migration-backups"
        backup = backups / backup_id
        (backup / "originals" / "briefs").mkdir(parents=True, mode=0o700)
        for directory in (backups, backup, backup / "originals", backup / "originals/briefs"):
            directory.chmod(0o700)
        _write(backup / "manifest.json", manifest)
        for path, data in originals.items():
            _write(backup / "originals" / path, data)
        for name in ("board.md", "log.md", "observations.md"):
            _write(self.root / name, originals[name])
        _write(self.root / "migration.json", _canonical({
            "schema_version": 1,
            "status": "complete",
            "source_snapshot_sha256": backup_id,
            "backup": {
                "id": backup_id,
                "manifest": f"migration-backups/{backup_id}/manifest.json",
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            },
            "translated_log_sha256": hashlib.sha256(b"").hexdigest(),
            "translations": [],
            "retained_artifacts": ["board.md", "log.md", "observations.md"],
            "ambiguities": [],
        }))
        return {
            key: value
            for key, value in _tree(self.root).items()
            if key.split("/", 1)[0] in master_runtime.LEGACY_MIGRATION_NODES
        }

    def retire(self, apply=True):
        return self.cli("master", "retire-migration", *(["--apply"] if apply else []))

    # --------------------------------------------------------------- tests

    def test_record_with_legacy_nodes_refuses_with_the_retirement_command(self):
        with self.assertRaises(master_runtime.MasterRecordError) as caught:
            master_runtime.read_record(self.root)
        self.assertIn("master retire-migration --apply", str(caught.exception))
        code, result = self.cli("master", "digest")
        self.assertEqual(code, 2)
        self.assertIn("retire-migration", result["detail"])

    def test_preview_changes_nothing(self):
        before = _tree(self.root)
        code, result = self.retire(apply=False)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "PREVIEW")
        self.assertEqual(result["files"], 10)
        self.assertEqual(_tree(self.root), before)
        self.assertFalse(self.archive_parent.exists())

    def test_retire_archives_records_procedure_and_reads_work(self):
        events_before = (self.root / "events.jsonl").read_bytes()
        code, result = self.retire()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "OK")
        for name in master_runtime.LEGACY_MIGRATION_NODES:
            self.assertFalse((self.root / name).exists(), name)
        archive = Path(result["archive"])
        self.assertEqual(archive.parent, self.archive_parent)
        self.assertEqual(stat.S_IMODE(self.archive_parent.stat().st_mode), 0o700)
        manifest, digest = master_retire.read_archive(archive)
        self.assertEqual(digest, result["manifest_sha256"])
        archived = {
            key.split("/", 1)[1]: value
            for key, value in _tree(archive).items()
            if key.startswith("record/")
        }
        self.assertEqual(archived, self.legacy)
        # The event log grows only by the procedure event.
        view = master_runtime.read_record(self.root)
        self.assertTrue(view.log_bytes.startswith(events_before))
        last = view.events[-1]
        self.assertEqual(last["kind"], "procedure")
        self.assertEqual(last["payload"]["procedure_id"], master_retire.PROCEDURE_ID)
        self.assertEqual(last["payload"]["action"], "retire")
        self.assertIn(digest, last["payload"]["evidence"])
        code, result = self.cli("master", "digest")
        self.assertEqual(code, 0, result)
        code, result = self.event("after retirement")
        self.assertEqual(code, 0, result)
        # Idempotent: a second run reports current and appends nothing.
        count = len(master_runtime.read_record(self.root).events)
        code, result = self.retire()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(master_runtime.read_record(self.root).events), count)

    def test_retired_reads_are_faster_than_the_verified_legacy_path(self):
        self.retire()
        started = time.perf_counter()
        for _ in range(5):
            master_runtime.read_record(self.root)
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_nonholder_is_refused_and_nothing_changes(self):
        ok, detail = semaphore.master_release()
        self.assertTrue(ok, detail)
        before = _tree(self.root)
        code, result = self.retire()
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "REFUSED")
        self.assertIn("renewal", result["detail"])
        self.assertEqual(_tree(self.root), before)
        self.assertFalse(self.archive_parent.exists())

    def test_hash_mismatch_is_refused_and_nothing_changes(self):
        tampered = self.root / "board.md"
        _write(tampered, b"# changed after migration\n")
        before = _tree(self.root)
        code, result = self.retire()
        self.assertEqual(code, 2)
        self.assertIn("board.md changed after migration", result["detail"])
        self.assertEqual(_tree(self.root), before)
        self.assertFalse(self.archive_parent.exists())

    def test_backup_mismatch_is_refused(self):
        backup = next((self.root / "migration-backups").iterdir())
        _write(backup / "originals" / "briefs" / "old.md", b"rewritten\n")
        code, result = self.retire()
        self.assertEqual(code, 2)
        self.assertIn("does not match its manifest", result["detail"])

    def test_unignored_archive_root_is_refused(self):
        (self.store / ".gitignore").write_text("/master/\n", encoding="utf-8")
        before = _tree(self.root)
        code, result = self.retire()
        self.assertEqual(code, 2)
        self.assertIn("master-archive/ is not ignored", result["detail"])
        self.assertEqual(_tree(self.root), before)

    def test_restore_is_byte_and_mode_identical(self):
        code, result = self.retire()
        self.assertEqual(code, 0, result)
        archive = Path(result["archive"])
        code, preview = self.cli("master", "restore-migration", archive.name)
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview["status"], "PREVIEW")
        self.assertFalse((self.root / "migration.json").exists())
        code, restored = self.cli("master", "restore-migration", archive.name, "--apply")
        self.assertEqual(code, 0, restored)
        self.assertEqual(restored["status"], "OK")
        after = {
            key: value
            for key, value in _tree(self.root).items()
            if key.split("/", 1)[0] in master_runtime.LEGACY_MIGRATION_NODES
        }
        self.assertEqual(after, self.legacy)
        self.assertTrue(archive.is_dir())
        # Retiring again after a restore reuses the verified archive.
        code, again = self.retire()
        self.assertEqual(code, 0, again)
        self.assertEqual(again["archive"], str(archive))

    def test_interrupted_removal_resumes(self):
        real = master_retire._remove_nodes

        def partial(root, manifest, present):
            (root / "board.md").unlink()
            raise OSError("synthetic crash during removal")

        with mock.patch("creme.master_retire._remove_nodes", partial):
            code, result = self.retire()
        self.assertEqual(code, 2)
        self.assertTrue(self.archive_parent.is_dir())
        with mock.patch("creme.master_retire._remove_nodes", real):
            code, result = self.retire()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "OK")
        for name in master_runtime.LEGACY_MIGRATION_NODES:
            self.assertFalse((self.root / name).exists(), name)

    def test_tampered_archive_refuses_restore(self):
        code, result = self.retire()
        archive = Path(result["archive"])
        _write(archive / "record" / "log.md", b"tampered\n")
        code, restored = self.cli("master", "restore-migration", archive.name, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("do not match its manifest", restored["detail"])
        self.assertFalse((self.root / "log.md").exists())

    def test_record_without_legacy_nodes_is_current(self):
        for name in ("migration.json", "board.md", "log.md", "observations.md"):
            (self.root / name).unlink()
        import shutil

        shutil.rmtree(self.root / "migration-backups")
        code, result = self.retire()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "CURRENT")
        self.assertFalse(self.archive_parent.exists())

    def test_doctor_requires_the_archive_to_be_ignored(self):
        from creme import doctor

        self.retire()
        profile = {"workspace": {"goal_store": "goals"}}
        [check] = doctor.check_goal_store(self.workspace, profile)
        self.assertEqual(check.status, doctor.STATUS_OK, check.detail)
        self.assertIn("master-archive/ are ignored", check.detail)
        (self.store / ".gitignore").write_text("/master/\n", encoding="utf-8")
        [check] = doctor.check_goal_store(self.workspace, profile)
        self.assertEqual(check.status, doctor.STATUS_FAIL)
        self.assertIn("master-archive/ must be ignored", check.detail)


if __name__ == "__main__":
    unittest.main()
