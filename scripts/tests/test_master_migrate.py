from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import cli, master_migrate, master_operations, master_runtime


class SyntheticFault(RuntimeError):
    pass


def kill_migration_at(root: str, stage: str) -> None:
    def inject(candidate: str) -> None:
        if candidate == stage:
            os._exit(73)

    master_migrate.migrate(
        Path(root),
        apply=True,
        renew=lambda: (True, "synthetic holder"),
        fault=inject,
    )


class MasterMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "master"
        self.renewals = 0

    def renew(self):
        self.renewals += 1
        return True, "synthetic schema-3 holder verified"

    @staticmethod
    def event(ordinal: int) -> bytes:
        return master_runtime._canonical_json(
            {
                "schema_version": 1,
                "event_id": f"{ordinal:032x}",
                "timestamp": f"2026-09-05T00:00:0{ordinal}.000000Z",
                "kind": "note",
                "actor": {
                    "client": "codex",
                    "acquisition_digest": f"{ordinal:064x}",
                },
                "payload": {
                    "title": f"synthetic-{ordinal}",
                    "note": "bounded synthetic migration event",
                    "evidence": f"evidence-{ordinal}.json",
                    "next_unit": f"unit-{ordinal}",
                },
            }
        )

    @staticmethod
    def write_private(root: Path, relative: str, data: bytes) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cursor = path.parent
        while cursor != root.parent:
            os.chmod(cursor, 0o700)
            if cursor == root:
                break
            cursor = cursor.parent
        path.write_bytes(data)
        os.chmod(path, 0o600)

    def make_legacy(
        self, root: Path | None = None, *, recognizable: bool = True
    ) -> dict[str, bytes]:
        selected = root or self.root
        log = self.event(1) + self.event(2) if recognizable else b"legacy canary text\n"
        files = {
            "README.md": b"private legacy guide\n",
            "board.md": b"legacy derived board\n",
            "log.md": log,
            "intent/objective.md": b"synthetic private intent\n",
            "briefs/goal-a.md": b"synthetic private brief\n",
        }
        for relative, data in files.items():
            self.write_private(selected, relative, data)
        return files

    @staticmethod
    def tree_snapshot(root: Path):
        if not root.exists():
            return None
        rows = []
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                data = path.read_bytes()
            elif stat.S_ISLNK(info.st_mode):
                data = os.readlink(path).encode()
            else:
                data = None
            rows.append((path.relative_to(root).as_posix(), data, info.st_mode, info.st_nlink))
        return rows

    def test_preview_is_exact_nonmutating_and_ordinary_commands_only_diagnose(self):
        self.make_legacy(recognizable=False)
        before = self.tree_snapshot(self.root)
        plan = master_migrate.migrate(self.root, apply=False, renew=self.renew)
        self.assertEqual(plan.status, "PREVIEW")
        self.assertEqual(self.renewals, 0)
        self.assertEqual(self.tree_snapshot(self.root), before)
        self.assertEqual(
            master_operations.plan_initialization(SimpleNamespace(record_root=self.root)).status,
            "MIGRATION_REQUIRED",
        )
        rendered = json.dumps(plan.to_dict(), sort_keys=True)
        self.assertNotIn("legacy canary text", rendered)
        self.assertTrue(any(action.action == "backup" for action in plan.actions))
        self.assertTrue(
            any(
                action.path == "log.md" and action.action == "retain"
                for action in plan.actions
            )
        )

        location = SimpleNamespace(record_root=self.root)
        for arguments in (
            [
                "master",
                "start",
                "--client",
                "codex",
                "--model",
                "synthetic",
                "--effort",
                "high",
                "--note",
                "synthetic",
            ],
            ["master", "event", "--from", "-"],
            ["master", "digest"],
        ):
            output = io.StringIO()
            with (
                mock.patch("creme.cli._master_location", return_value=(location, None)),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(cli.main(arguments), 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "migration-required")
            self.assertEqual(self.tree_snapshot(self.root), before)

    def test_cli_exposes_only_configured_preview_and_explicit_apply_flag(self):
        self.make_legacy()
        before = self.tree_snapshot(self.root)
        location = SimpleNamespace(record_root=self.root)
        output = io.StringIO()
        with (
            mock.patch("creme.cli._master_location", return_value=(location, None)),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(cli.main(["master", "init", "--migrate"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "PREVIEW")
        self.assertEqual(payload["record_root"], str(self.root))
        self.assertEqual(self.tree_snapshot(self.root), before)
        with self.assertRaises(SystemExit):
            cli.parser().parse_args(["master", "init", "/private/tmp/not-allowed", "--migrate"])

        expected = master_migrate.plan_migration(self.root)
        with (
            mock.patch("creme.cli._master_location", return_value=(location, None)),
            mock.patch("creme.cli.master_migrate.migrate", return_value=expected) as migrate,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(cli.main(["master", "init", "--migrate", "--apply"]), 0)
        migrate.assert_called_once_with(self.root, apply=True)

    def test_apply_preserves_bytes_order_hashes_privacy_and_is_idempotent(self):
        originals = self.make_legacy()
        result = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(result.status, "OK")
        self.assertGreaterEqual(self.renewals, 3)
        view = master_runtime.read_record(self.root)
        self.assertEqual([event["event_id"] for event in view.events], [f"{1:032x}", f"{2:032x}"])
        self.assertEqual(view.log_bytes, originals["log.md"])
        self.assertTrue(view.board_current)

        verified = master_migrate.verify_backup(self.root, result.backup_id)
        manifest = verified["manifest"]
        self.assertEqual(manifest["backup_id"], result.source_snapshot_sha256)
        rows = {row["path"]: row for row in manifest["files"]}
        self.assertEqual(set(rows), set(originals))
        for relative, data in originals.items():
            if relative != "README.md":
                self.assertEqual((self.root / relative).read_bytes(), data)
            backup = (
                self.root
                / master_migrate.BACKUP_ROOT_NAME
                / result.backup_id
                / "originals"
                / relative
            )
            self.assertEqual(backup.read_bytes(), data)
            self.assertEqual(rows[relative]["size"], len(data))
            self.assertEqual(rows[relative]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(
            [row["source_sha256"] for row in result.translations],
            [hashlib.sha256(self.event(1)).hexdigest(), hashlib.sha256(self.event(2)).hexdigest()],
        )

        snapshot = self.tree_snapshot(self.root)
        renewals = self.renewals
        rerun = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(rerun.status, "CURRENT")
        self.assertEqual(self.renewals, renewals)
        self.assertEqual(self.tree_snapshot(self.root), snapshot)
        self.assertEqual(
            master_operations.plan_initialization(SimpleNamespace(record_root=self.root)).status,
            "CURRENT",
        )

    def test_verified_migration_artifacts_remain_operable_after_new_events(self):
        self.make_legacy()
        migrated = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(migrated.status, "OK")
        initial = master_runtime.read_record(self.root)
        lease = {
            "schema_version": 3,
            "lease": {"client": "codex", "lease_id": "a" * 32},
        }
        writer = master_runtime.RecordWriter(
            self.root,
            renew=lambda: (True, "synthetic holder verified"),
            lease_snapshot=lambda: lease,
        )
        for ordinal in range(2):
            writer.append(
                "note",
                {
                    "title": f"post-migration event {ordinal}",
                    "note": "verified migration remains an operable current record",
                    "evidence": f"synthetic-post-migration-{ordinal}.json",
                    "next_unit": f"continue from migrated record {ordinal}",
                },
            )
            self.assertEqual(master_migrate.plan_migration(self.root).status, "CURRENT")
            self.assertEqual(
                master_operations.plan_initialization(
                    SimpleNamespace(record_root=self.root)
                ).status,
                "CURRENT",
            )
            digest = master_operations.digest_record(
                self.root,
                lease_snapshot=lambda: lease,
                lease_status=lambda: "master: codex (live)\n",
            )
            self.assertEqual(digest["status"], "OK")
            self.assertEqual(
                digest["next_unit"], f"continue from migrated record {ordinal}"
            )
        current = master_runtime.read_record(self.root)
        self.assertTrue(current.log_bytes.startswith(initial.log_bytes))
        self.assertEqual(len(current.events), len(initial.events) + 2)
        self.assertEqual(master_migrate.plan_migration(self.root).status, "CURRENT")

        snapshot = self.tree_snapshot(self.root)
        renewals = self.renewals
        rerun = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(rerun.status, "CURRENT")
        self.assertEqual(self.renewals, renewals)
        self.assertEqual(self.tree_snapshot(self.root), snapshot)
        self.assertEqual(
            master_operations.plan_initialization(SimpleNamespace(record_root=self.root)).status,
            "CURRENT",
        )

    def test_completed_migration_rejects_changed_prefix_and_malformed_suffix(self):
        cases = ("altered", "reordered", "truncated", "malformed-suffix")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                self.make_legacy(root)
                migrated = master_migrate.migrate(root, apply=True, renew=self.renew)
                self.assertEqual(migrated.status, "OK")
                original = master_runtime.read_record(root)
                rows = original.log_bytes.splitlines(keepends=True)
                if case == "altered":
                    event = json.loads(rows[0])
                    event["event_id"] = "f" * 32
                    changed = [master_runtime._canonical_json(event), *rows[1:]]
                elif case == "reordered":
                    changed = [rows[1], rows[0], *rows[2:]]
                elif case == "truncated":
                    changed = rows[:-1]
                else:
                    changed = [*rows, b'{"malformed":']
                log_bytes = b"".join(changed)
                (root / master_runtime.EVENTS_NAME).write_bytes(log_bytes)
                os.chmod(root / master_runtime.EVENTS_NAME, 0o600)
                if case != "malformed-suffix":
                    events = [
                        master_runtime.validate_event(json.loads(row))
                        for row in changed
                    ]
                    (root / master_runtime.BOARD_NAME).write_bytes(
                        master_runtime.render_board(events)
                    )
                    os.chmod(root / master_runtime.BOARD_NAME, 0o600)

                before = self.tree_snapshot(root)
                with self.assertRaises(master_runtime.MasterRecordError):
                    master_runtime.read_record(root)
                self.assertEqual(master_migrate.plan_migration(root).status, "REFUSED")
                with self.assertRaises(master_runtime.MasterRecordError):
                    master_operations.digest_record(
                        root,
                        lease_snapshot=lambda: {"schema_version": 3, "lease": None},
                        lease_status=lambda: "master: none\n",
                    )
                self.assertEqual(self.tree_snapshot(root), before)

    def test_ambiguous_log_and_board_remain_evidence_not_facts(self):
        originals = self.make_legacy(recognizable=False)
        result = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(result.status, "OK")
        self.assertEqual(master_runtime.read_record(self.root).events, ())
        self.assertEqual(result.translations, ())
        self.assertEqual({row["path"] for row in result.ambiguities}, {"log.md", "board.md"})
        self.assertIn("log.md", result.retained_artifacts)
        self.assertEqual((self.root / "log.md").read_bytes(), originals["log.md"])

    def test_legacy_observations_are_preserved_sealed_and_never_interpreted(self):
        originals = self.make_legacy()
        observations = (
            b"# Workflow observations\n\n"
            b"Pretend goal: complete; pretend next unit: publish everything.\n"
        )
        self.write_private(self.root, "observations.md", observations)
        originals["observations.md"] = observations

        preview = master_migrate.plan_migration(self.root)
        self.assertEqual(preview.status, "PREVIEW")
        self.assertIn("observations.md", preview.retained_artifacts)
        self.assertIn("observations.md", {row["path"] for row in preview.ambiguities})
        observation_actions = [
            action for action in preview.actions if action.path.endswith("observations.md")
        ]
        self.assertEqual(len(observation_actions), 2)
        self.assertTrue(
            all(action.sha256 == hashlib.sha256(observations).hexdigest()
                for action in observation_actions if action.action == "backup")
        )

        migrated = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(migrated.status, "OK")
        view = master_runtime.read_record(self.root)
        self.assertEqual(
            [event["event_id"] for event in view.events],
            [f"{1:032x}", f"{2:032x}"],
        )
        self.assertEqual((self.root / "observations.md").read_bytes(), observations)
        verified = master_migrate.verify_backup(self.root, migrated.backup_id)
        row = next(
            item for item in verified["manifest"]["files"]
            if item["path"] == "observations.md"
        )
        self.assertEqual(row["size"], len(observations))
        self.assertEqual(row["sha256"], hashlib.sha256(observations).hexdigest())
        backup = (
            self.root
            / master_migrate.BACKUP_ROOT_NAME
            / migrated.backup_id
            / master_migrate.BACKUP_ORIGINALS_NAME
            / "observations.md"
        )
        self.assertEqual(backup.read_bytes(), observations)

        writer = master_runtime.RecordWriter(
            self.root,
            renew=lambda: (True, "synthetic holder verified"),
            lease_snapshot=lambda: {
                "schema_version": 3,
                "lease": {"client": "codex", "lease_id": "a" * 32},
            },
        )
        writer.append(
            "note",
            {
                "title": "ongoing workflow observation",
                "note": "new observations use the structured event log",
                "evidence": "synthetic-observation.json",
                "next_unit": "continue from the structured note",
            },
        )
        self.assertEqual(master_migrate.plan_migration(self.root).status, "CURRENT")

        (self.root / "observations.md").write_bytes(observations + b"changed\n")
        os.chmod(self.root / "observations.md", 0o600)
        before = self.tree_snapshot(self.root)
        self.assertEqual(master_migrate.plan_migration(self.root).status, "REFUSED")
        with self.assertRaises(master_runtime.MasterRecordError):
            master_runtime.read_record(self.root)
        self.assertEqual(self.tree_snapshot(self.root), before)

    def test_legacy_observations_metadata_and_node_controls_refuse_unchanged(self):
        for case in ("symlink", "hardlink", "wrong-mode"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                self.write_private(root, "log.md", b"legacy\n")
                observations = root / "observations.md"
                if case == "symlink":
                    observations.symlink_to(root / "log.md")
                elif case == "hardlink":
                    os.link(root / "log.md", observations)
                else:
                    self.write_private(root, "observations.md", b"private notes\n")
                    os.chmod(observations, 0o644)
                before = self.tree_snapshot(root)
                self.assertEqual(master_migrate.plan_migration(root).status, "REFUSED")
                self.assertEqual(
                    master_migrate.migrate(
                        root, apply=True, renew=lambda: (True, "holder")
                    ).status,
                    "REFUSED",
                )
                self.assertEqual(self.tree_snapshot(root), before)

        for case in ("report", "backup"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                self.make_legacy(root)
                observations = b"private legacy observations\n"
                self.write_private(root, "observations.md", observations)
                migrated = master_migrate.migrate(
                    root, apply=True, renew=lambda: (True, "holder")
                )
                self.assertEqual(migrated.status, "OK")
                if case == "report":
                    path = root / master_migrate.MIGRATION_REPORT_NAME
                    report = json.loads(path.read_bytes())
                    report["retained_artifacts"].remove("observations.md")
                    path.write_bytes(master_runtime._canonical_json(report))
                else:
                    path = (
                        root
                        / master_migrate.BACKUP_ROOT_NAME
                        / migrated.backup_id
                        / master_migrate.BACKUP_ORIGINALS_NAME
                        / "observations.md"
                    )
                    path.write_bytes(b"forged legacy observations\n")
                os.chmod(path, 0o600)
                before = self.tree_snapshot(root)
                self.assertEqual(master_migrate.plan_migration(root).status, "REFUSED")
                with self.assertRaises(master_runtime.MasterRecordError):
                    master_runtime.read_record(root)
                self.assertEqual(self.tree_snapshot(root), before)

    def test_renewal_refusal_precedes_every_mutation(self):
        self.make_legacy()
        before = self.tree_snapshot(self.root)
        calls = 0

        def refused():
            nonlocal calls
            calls += 1
            return False, "synthetic successor owns the lease"

        result = master_migrate.migrate(self.root, apply=True, renew=refused)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(calls, 1)
        self.assertIn("renewal refused", result.detail)
        self.assertEqual(self.tree_snapshot(self.root), before)

    def test_malformed_unexpected_and_unsafe_legacy_inputs_refuse_unchanged(self):
        cases = ("malformed", "unexpected", "symlink", "hardlink", "wrong-mode")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                self.write_private(root, "log.md", b"legacy\n")
                if case == "malformed":
                    (root / "log.md").write_bytes(b"\xff\xfe")
                elif case == "unexpected":
                    self.write_private(root, "secret.bin", b"unexpected")
                elif case == "symlink":
                    (root / "intent").mkdir(mode=0o700)
                    (root / "intent/link").symlink_to(root / "log.md")
                elif case == "hardlink":
                    os.link(root / "log.md", root / "board.md")
                else:
                    os.chmod(root / "log.md", 0o644)
                before = self.tree_snapshot(root)
                result = master_migrate.migrate(root, apply=True, renew=self.renew)
                self.assertEqual(result.status, "REFUSED")
                self.assertEqual(self.tree_snapshot(root), before)

    def test_no_report_optional_artifacts_use_the_ordinary_exact_validator(self):
        for name in (
            "log.md",
            "board.md",
            "observations.md",
            master_migrate.BACKUP_ROOT_NAME,
        ):
            with self.subTest(node=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                master_runtime.initialize_empty_record(root)
                if name == master_migrate.BACKUP_ROOT_NAME:
                    (root / name).mkdir(mode=0o700)
                else:
                    self.write_private(root, name, b"unclassified synthetic bytes\n")
                before = self.tree_snapshot(root)
                renewals = []
                self.assertEqual(master_migrate.plan_migration(root).status, "REFUSED")
                result = master_migrate.migrate(
                    root,
                    apply=True,
                    renew=lambda: renewals.append(True) or (True, "holder"),
                )
                self.assertEqual(result.status, "REFUSED")
                self.assertEqual(renewals, [])
                self.assertEqual(self.tree_snapshot(root), before)

    def test_unverified_migration_temporaries_refuse_without_deletion(self):
        cases = (
            "current-retired-writer",
            "legacy-report",
            "legacy-events",
            "legacy-backup",
            "legacy-backup-exact-out-of-order",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                if case == "current-retired-writer":
                    master_runtime.initialize_empty_record(root)
                    self.write_private(root, "log.md", b"unclassified retired writer\n")
                    candidate = root / ".events.jsonl.1234567890abcdef.tmp"
                    candidate.write_bytes(b"unverified synthetic bytes\n")
                    candidate.chmod(0o600)
                else:
                    originals = self.make_legacy(root)
                    if case == "legacy-report":
                        candidate = root / ".migration.json.1234567890abcdef.tmp"
                        candidate.write_bytes(b"unverified synthetic bytes\n")
                        candidate.chmod(0o600)
                    elif case == "legacy-events":
                        candidate = root / ".events.jsonl.1234567890abcdef.tmp"
                        candidate.write_bytes(b"unverified synthetic bytes\n")
                        candidate.chmod(0o600)
                    else:
                        preview = master_migrate.plan_migration(root)
                        backup_root = root / master_migrate.BACKUP_ROOT_NAME
                        backup_root.mkdir(mode=0o700)
                        candidate = backup_root / (
                            f".{preview.backup_id}.1234567890abcdef.tmp"
                        )
                        candidate.mkdir(mode=0o700)
                        if case == "legacy-backup":
                            forged = candidate / "forged"
                            forged.write_bytes(b"unverified synthetic bytes\n")
                            forged.chmod(0o600)
                        else:
                            originals_root = candidate / master_migrate.BACKUP_ORIGINALS_NAME
                            originals_root.mkdir(mode=0o700)
                            forged = originals_root / "log.md"
                            forged.write_bytes(originals["log.md"])
                            forged.chmod(0o600)
                before = self.tree_snapshot(root)
                renewals = []
                self.assertEqual(master_migrate.plan_migration(root).status, "REFUSED")
                result = master_migrate.migrate(
                    root,
                    apply=True,
                    renew=lambda: renewals.append(True) or (True, "holder"),
                )
                self.assertEqual(result.status, "REFUSED")
                self.assertEqual(renewals, [])
                self.assertTrue(candidate.exists())
                self.assertEqual(self.tree_snapshot(root), before)

    def test_backup_collision_refuses_before_renewal(self):
        self.make_legacy()
        preview = master_migrate.plan_migration(self.root)
        backup = self.root / master_migrate.BACKUP_ROOT_NAME / preview.backup_id
        backup.mkdir(mode=0o700, parents=True)
        os.chmod(backup.parent, 0o700)
        before = self.tree_snapshot(self.root)
        result = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(result.status, "REFUSED")
        self.assertIn("backup", result.detail)
        self.assertEqual(self.renewals, 0)
        self.assertEqual(self.tree_snapshot(self.root), before)

    def test_backup_and_report_tampering_fail_closed(self):
        self.make_legacy()
        result = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        backup_log = (
            self.root
            / master_migrate.BACKUP_ROOT_NAME
            / result.backup_id
            / "originals/log.md"
        )
        data = backup_log.read_bytes()
        backup_log.write_bytes(b"X" + data[1:])
        os.chmod(backup_log, 0o600)
        refused = master_migrate.plan_migration(self.root)
        self.assertEqual(refused.status, "REFUSED")
        self.assertIn("manifest", refused.detail)
        self.assertEqual(
            master_operations.plan_initialization(SimpleNamespace(record_root=self.root)).status,
            "REFUSED",
        )

        # Restore only the backed-up byte, then prove a canonical report edit also bites.
        backup_log.write_bytes(data)
        os.chmod(backup_log, 0o600)
        report_path = self.root / master_migrate.MIGRATION_REPORT_NAME
        report = json.loads(report_path.read_bytes())
        report["translations"][0]["source_sha256"] = "f" * 64
        report_path.write_bytes(master_runtime._canonical_json(report))
        os.chmod(report_path, 0o600)
        self.assertEqual(master_migrate.plan_migration(self.root).status, "REFUSED")

    def test_fault_at_every_mutation_boundary_leaves_a_classified_authority(self):
        stages: list[str] = []
        self.make_legacy()
        master_migrate.migrate(
            self.root,
            apply=True,
            renew=self.renew,
            fault=lambda stage: stages.append(stage),
        )
        self.assertGreater(len(stages), 30)
        self.assertEqual(len(stages), len(set(stages)))

        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                originals = self.make_legacy(root)

                def inject(candidate: str):
                    if candidate == stage:
                        raise SyntheticFault(stage)

                result = master_migrate.migrate(
                    root,
                    apply=True,
                    renew=lambda: (True, "holder"),
                    fault=inject,
                )
                self.assertEqual(result.status, "REFUSED")
                self.assertEqual((root / "log.md").read_bytes(), originals["log.md"])
                classified = master_migrate.plan_migration(root)
                self.assertIn(classified.status, {"PREVIEW", "FINALIZE", "CURRENT"})
                if classified.status != "CURRENT":
                    recovered = master_migrate.migrate(
                        root, apply=True, renew=lambda: (True, "holder")
                    )
                    self.assertEqual(recovered.status, "OK")
                current = master_migrate.plan_migration(root)
                self.assertEqual(current.status, "CURRENT")
                self.assertEqual(
                    master_operations.plan_initialization(
                        SimpleNamespace(record_root=root)
                    ).status,
                    "CURRENT",
                )
                self.assertTrue(master_runtime.read_record(root).board_current)
                master_migrate.verify_backup(root, current.backup_id)

    def test_process_death_at_every_boundary_leaves_legacy_or_verified_current(self):
        stages: list[str] = []
        self.make_legacy()
        master_migrate.migrate(
            self.root,
            apply=True,
            renew=self.renew,
            fault=lambda stage: stages.append(stage),
        )
        context = multiprocessing.get_context("fork")
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                originals = self.make_legacy(root)
                process = context.Process(target=kill_migration_at, args=(str(root), stage))
                process.start()
                process.join(10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 73)
                self.assertEqual((root / "log.md").read_bytes(), originals["log.md"])
                classified = master_migrate.plan_migration(root)
                self.assertIn(classified.status, {"PREVIEW", "FINALIZE", "CURRENT"})
                if classified.status != "CURRENT":
                    recovered = master_migrate.migrate(
                        root, apply=True, renew=lambda: (True, "holder")
                    )
                    self.assertEqual(recovered.status, "OK")
                current = master_migrate.plan_migration(root)
                self.assertEqual(current.status, "CURRENT")
                self.assertTrue(master_runtime.read_record(root).board_current)
                master_migrate.verify_backup(root, current.backup_id)

    def test_verified_backup_cleanup_recovers_through_repeated_process_deaths(self):
        originals = self.make_legacy()
        context = multiprocessing.get_context("fork")

        process = context.Process(
            target=kill_migration_at,
            args=(str(self.root), "backup:before-publish"),
        )
        process.start()
        process.join(10)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 73)

        candidates = master_migrate._candidate_temp_paths(self.root)
        self.assertEqual(len(candidates), 1)
        temporary = candidates[0]
        snapshot = master_migrate._scan_legacy(self.root, candidates)
        node_count = master_migrate._verify_partial_backup(temporary, snapshot)

        def die_after_one_cleanup_step() -> None:
            def inject(stage: str) -> None:
                if stage in {
                    "recovery-temp-0:node-0:after-remove",
                    "recovery-temp-0:root:after-remove",
                }:
                    os._exit(74)

            master_migrate.migrate(
                self.root,
                apply=True,
                renew=lambda: (True, "synthetic holder"),
                fault=inject,
            )

        deaths = 0
        while temporary.exists():
            process = context.Process(target=die_after_one_cleanup_step)
            process.start()
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 74)
            deaths += 1
            self.assertLessEqual(deaths, node_count + 1)
            self.assertEqual(master_migrate.plan_migration(self.root).status, "PREVIEW")
            for relative, data in originals.items():
                self.assertEqual((self.root / relative).read_bytes(), data)

        self.assertEqual(deaths, node_count + 1)
        recovered = master_migrate.migrate(
            self.root,
            apply=True,
            renew=lambda: (True, "synthetic holder"),
        )
        self.assertEqual(recovered.status, "OK")
        self.assertEqual(master_migrate.plan_migration(self.root).status, "CURRENT")

    def test_backup_exception_cleanup_death_recovers_at_every_reverse_boundary(self):
        self.make_legacy()
        observed_stages: list[str] = []

        def record_exception_cleanup(stage: str) -> None:
            observed_stages.append(stage)
            if stage == "backup:before-publish":
                raise SyntheticFault(stage)

        failed = master_migrate.migrate(
            self.root,
            apply=True,
            renew=self.renew,
            fault=record_exception_cleanup,
        )
        self.assertEqual(failed.status, "REFUSED")
        cleanup_death_stages = tuple(
            stage
            for stage in observed_stages
            if stage.startswith("backup-exception-cleanup:")
            and stage.endswith(":after-remove")
        )
        self.assertGreater(len(cleanup_death_stages), 1)
        self.assertEqual(master_migrate.plan_migration(self.root).status, "PREVIEW")

        context = multiprocessing.get_context("fork")
        for death_stage in cleanup_death_stages:
            with self.subTest(stage=death_stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                originals = self.make_legacy(root)

                def die_during_exception_cleanup() -> None:
                    def inject(stage: str) -> None:
                        if stage == "backup:before-publish":
                            raise SyntheticFault(stage)
                        if stage == death_stage:
                            os._exit(74)

                    master_migrate.migrate(
                        root,
                        apply=True,
                        renew=lambda: (True, "synthetic holder"),
                        fault=inject,
                    )

                process = context.Process(target=die_during_exception_cleanup)
                process.start()
                process.join(10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 74)
                for relative, data in originals.items():
                    self.assertEqual((root / relative).read_bytes(), data)

                preview_snapshot = self.tree_snapshot(root)
                self.assertEqual(master_migrate.plan_migration(root).status, "PREVIEW")
                self.assertEqual(self.tree_snapshot(root), preview_snapshot)

                recovered = master_migrate.migrate(
                    root,
                    apply=True,
                    renew=lambda: (True, "synthetic holder"),
                )
                self.assertEqual(recovered.status, "OK")
                self.assertEqual(master_migrate.plan_migration(root).status, "CURRENT")
                for relative, data in originals.items():
                    backup = (
                        root
                        / master_migrate.BACKUP_ROOT_NAME
                        / recovered.backup_id
                        / master_migrate.BACKUP_ORIGINALS_NAME
                        / relative
                    )
                    self.assertEqual(backup.read_bytes(), data)

    def test_backup_exception_cleanup_preserves_corrupt_partial_unchanged(self):
        originals = self.make_legacy()

        def corrupt_then_raise(stage: str) -> None:
            if stage != "backup:before-publish":
                return
            candidates = master_migrate._candidate_temp_paths(self.root)
            self.assertEqual(len(candidates), 1)
            target = (
                candidates[0]
                / master_migrate.BACKUP_ORIGINALS_NAME
                / "README.md"
            )
            target.write_bytes(b"corrupt staged bytes\n")
            target.chmod(0o600)
            raise SyntheticFault(stage)

        failed = master_migrate.migrate(
            self.root,
            apply=True,
            renew=self.renew,
            fault=corrupt_then_raise,
        )
        self.assertEqual(failed.status, "REFUSED")
        self.assertIn("changed", failed.detail)
        for relative, data in originals.items():
            self.assertEqual((self.root / relative).read_bytes(), data)
        refused_snapshot = self.tree_snapshot(self.root)
        self.assertEqual(master_migrate.plan_migration(self.root).status, "REFUSED")
        self.assertEqual(
            master_migrate.migrate(self.root, apply=True, renew=self.renew).status,
            "REFUSED",
        )
        self.assertEqual(self.tree_snapshot(self.root), refused_snapshot)

    def test_verified_backup_requires_exact_top_level_inventory(self):
        self.make_legacy()
        migrated = master_migrate.migrate(
            self.root,
            apply=True,
            renew=self.renew,
        )
        self.assertEqual(migrated.status, "OK")
        backup = self.root / master_migrate.BACKUP_ROOT_NAME / migrated.backup_id
        extra = backup / "unmanifested.bin"
        extra.write_bytes(b"unknown private backup bytes\n")
        extra.chmod(0o600)
        before = self.tree_snapshot(self.root)
        self.assertEqual(master_migrate.plan_migration(self.root).status, "REFUSED")
        with self.assertRaises(master_runtime.MasterRecordError):
            master_runtime.read_record(self.root)
        self.assertEqual(self.tree_snapshot(self.root), before)

    def test_ignored_fixture_git_state_is_unchanged_by_preview_and_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary).resolve() / "goals"
            store.mkdir()
            subprocess.run(["git", "init", "-q", str(store)], check=True)
            (store / ".gitignore").write_text("/master/\n", encoding="utf-8")
            root = store / "master"
            self.make_legacy(root)
            before = subprocess.run(
                ["git", "-C", str(store), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(master_migrate.migrate(root, apply=False).status, "PREVIEW")
            self.assertEqual(
                master_migrate.migrate(root, apply=True, renew=lambda: (True, "holder")).status,
                "OK",
            )
            after = subprocess.run(
                ["git", "-C", str(store), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(after, before)
            ignored = subprocess.run(
                ["git", "-C", str(store), "check-ignore", "master/migration.json"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(ignored, "master/migration.json")

    def test_prepared_valid_record_requires_explicit_finalize(self):
        self.make_legacy()

        def inject(stage: str):
            if stage == "migration-report-complete:before-temp-create":
                raise SyntheticFault(stage)

        failed = master_migrate.migrate(self.root, apply=True, renew=self.renew, fault=inject)
        self.assertEqual(failed.status, "REFUSED")
        self.assertEqual(master_migrate.plan_migration(self.root).status, "FINALIZE")
        self.assertEqual(
            master_operations.plan_initialization(SimpleNamespace(record_root=self.root)).status,
            "MIGRATION_REQUIRED",
        )
        completed = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(completed.status, "OK")
        self.assertEqual(master_migrate.plan_migration(self.root).status, "CURRENT")

    def test_verified_backup_from_an_interrupted_apply_is_reused(self):
        self.make_legacy()

        def inject(stage: str):
            if stage == "backup:after-dir-fsync":
                raise SyntheticFault(stage)

        failed = master_migrate.migrate(
            self.root,
            apply=True,
            renew=self.renew,
            fault=inject,
        )
        self.assertEqual(failed.status, "REFUSED")
        preview = master_migrate.plan_migration(self.root)
        self.assertEqual(preview.status, "PREVIEW")
        self.assertTrue(
            all(
                action.action == "keep"
                for action in preview.actions
                if action.path.startswith(master_migrate.BACKUP_ROOT_NAME + "/")
            )
        )
        completed = master_migrate.migrate(self.root, apply=True, renew=self.renew)
        self.assertEqual(completed.status, "OK")
        self.assertEqual(completed.backup_id, failed.backup_id)
        self.assertEqual(master_migrate.plan_migration(self.root).status, "CURRENT")

    def test_source_change_during_backup_and_concurrent_migrations_do_not_corrupt(self):
        originals = self.make_legacy()
        backup_ready = threading.Event()
        source_changed = threading.Event()

        def pause_after_backup(stage: str):
            if stage == "backup:after-dir-fsync":
                backup_ready.set()
                self.assertTrue(source_changed.wait(5))

        def concurrent_writer():
            self.assertTrue(backup_ready.wait(5))
            (self.root / "log.md").write_bytes(originals["log.md"] + self.event(3))
            os.chmod(self.root / "log.md", 0o600)
            source_changed.set()

        writer = threading.Thread(target=concurrent_writer)
        writer.start()
        changed = master_migrate.migrate(
            self.root,
            apply=True,
            renew=self.renew,
            fault=pause_after_backup,
        )
        writer.join(10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(changed.status, "REFUSED")
        self.assertIn("changed during backup", changed.detail)
        self.assertFalse((self.root / master_runtime.EVENTS_NAME).exists())
        self.assertEqual((self.root / "log.md").read_bytes(), originals["log.md"] + self.event(3))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "master"
            self.make_legacy(root)
            barrier = threading.Barrier(2)
            local = threading.local()
            results = []

            def renew():
                if not getattr(local, "started", False):
                    local.started = True
                    barrier.wait(5)
                return True, "holder"

            def run():
                results.append(master_migrate.migrate(root, apply=True, renew=renew))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(sorted(result.status for result in results), ["OK", "REFUSED"])
            self.assertEqual(master_migrate.plan_migration(root).status, "CURRENT")
            self.assertTrue(master_runtime.read_record(root).board_current)

    def test_late_legacy_writes_cannot_cross_the_atomic_authority_handoff(self):
        stages = (
            "migration-readme:after-replace",
            "migration-log:after-replace",
            "migration-board:after-replace",
            "migration-report-complete:after-replace",
        )
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                originals = self.make_legacy(root)
                late = self.event(3)

                def write_late(candidate: str):
                    if candidate == stage:
                        (root / "log.md").write_bytes(originals["log.md"] + late)
                        os.chmod(root / "log.md", 0o600)

                result = master_migrate.migrate(
                    root,
                    apply=True,
                    renew=lambda: (True, "holder"),
                    fault=write_late,
                )
                self.assertEqual(result.status, "REFUSED")
                self.assertEqual((root / "log.md").read_bytes(), originals["log.md"] + late)
                self.assertEqual(master_migrate.plan_migration(root).status, "REFUSED")
                self.assertNotEqual(
                    master_operations.plan_initialization(
                        SimpleNamespace(record_root=root)
                    ).status,
                    "CURRENT",
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "master"
            originals = self.make_legacy(root)
            self.assertEqual(
                master_migrate.migrate(
                    root, apply=True, renew=lambda: (True, "holder")
                ).status,
                "OK",
            )
            (root / "log.md").write_bytes(originals["log.md"] + self.event(3))
            os.chmod(root / "log.md", 0o600)
            self.assertEqual(master_migrate.plan_migration(root).status, "REFUSED")


if __name__ == "__main__":
    unittest.main()
