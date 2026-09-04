from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import cli, master_operations, master_runtime, semaphore
from creme.adapters.base import Adapter
from creme.profile import propose, write_reviewed


class FixtureAdapter(Adapter):
    system = "Linux"

    def static_facts(self):
        return self.result("static_facts", "OK", "synthetic fixture", {
            "system": "Linux",
            "machine": "synthetic-machine",
            "logical_cores": 4,
            "physical_memory_bytes": 16 * 1024 ** 3,
        })


class LeaseHarness:
    def __init__(self, *, lease=None, state="live", can_renew=True):
        self.lease = lease
        self.state = state
        self.can_renew = can_renew
        self.acquire_count = 0
        self.renew_count = 0
        self.release_count = 0
        self.heartbeat_count = 0
        self.heartbeat_ok = True
        self.next_id = 1

    def snapshot(self):
        return {"schema_version": 3, "lease": self.lease}

    def acquire(self, client, note, *, take_over=False):
        self.acquire_count += 1
        if self.lease is not None and not (take_over and self.state in {"lapsed", "stranded"}):
            return False, f"the master lease is {self.state}: client {self.lease['client']}"
        self.lease = {
            "client": client,
            "lease_id": f"{self.next_id:031x}a",
        }
        self.next_id += 1
        self.state = "live"
        self.can_renew = True
        return True, "master lease acquired"

    def renew(self):
        self.renew_count += 1
        if self.lease is None or not self.can_renew:
            return False, "the master lease belongs to another session"
        return True, "holder verified"

    def release(self):
        self.release_count += 1
        self.lease = None
        return True, "released"

    def heartbeat(self, interval):
        self.heartbeat_count += 1
        return self.heartbeat_ok, "heartbeat started" if self.heartbeat_ok else "synthetic failure"


class MasterOperationsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.creme = self.workspace / "creme"
        self.creme.mkdir()
        (self.creme / ".creme").mkdir()
        self.store = self.workspace / "goals"
        self.store.mkdir()
        subprocess.run(["git", "init", "-q", str(self.store)], check=True)
        (self.store / ".gitignore").write_text("/master/\n", encoding="utf-8")
        self.adapter = FixtureAdapter()
        self.write_profile("goals")
        self.location = master_operations.resolve_runtime_location(
            self.creme, adapter=self.adapter
        )

    def write_profile(self, goal_store):
        candidate = propose(
            self.creme,
            self.workspace,
            self.adapter,
            goal_store=goal_store,
        )
        write_reviewed(self.creme / ".creme/host-profile.json", candidate)

    def initialize(self):
        plan = master_operations.initialize(self.location, apply=True)
        self.assertEqual(plan.status, "OK")
        return self.location.record_root

    @staticmethod
    def note_payload(title="note", *, body="synthetic note", next_unit="next"):
        return {
            "title": title,
            "note": body,
            "evidence": "synthetic-evidence.json",
            "next_unit": next_unit,
        }

    @staticmethod
    def tree_snapshot(root):
        if not root.exists():
            return None
        return [
            (
                str(path.relative_to(root)),
                path.read_bytes() if path.is_file() else None,
                path.lstat().st_mode,
            )
            for path in sorted(root.rglob("*"))
        ]

    def test_resolution_requires_the_valid_configured_ignored_untracked_store(self):
        location = master_operations.resolve_runtime_location(
            self.creme, adapter=self.adapter
        )
        self.assertEqual(location.record_root, self.store / "master")
        self.assertEqual(
            dict(location.repository_roots),
            {
                "creme": self.creme,
                "jaune": self.workspace / "jaune",
                "blanc": self.workspace / "blanc",
                "goal-store": self.store,
            },
        )

        self.write_profile(None)
        with self.assertRaisesRegex(master_operations.MasterOperationError, "not configured"):
            master_operations.resolve_runtime_location(self.creme, adapter=self.adapter)

        self.write_profile("goals")
        (self.store / ".gitignore").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(master_operations.MasterOperationError, "not ignored"):
            master_operations.resolve_runtime_location(self.creme, adapter=self.adapter)

        (self.creme / ".creme/host-profile.json").unlink()
        with self.assertRaisesRegex(master_operations.MasterOperationError, "must be VALID"):
            master_operations.resolve_runtime_location(self.creme, adapter=self.adapter)

    def test_resolution_refuses_a_tracked_descendant_and_symlinked_store(self):
        master = self.store / "master"
        master.mkdir(mode=0o700)
        (master / "tracked").write_text("synthetic\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.store), "add", "-f", "master/tracked"],
            check=True,
        )
        with self.assertRaisesRegex(master_operations.MasterOperationError, "Git-tracked"):
            master_operations.resolve_runtime_location(self.creme, adapter=self.adapter)

        other = self.workspace / "other-goals"
        other.mkdir()
        link = self.workspace / "linked-goals"
        link.symlink_to(other, target_is_directory=True)
        self.write_profile("linked-goals")
        with self.assertRaisesRegex(master_operations.MasterOperationError, "symlinked"):
            master_operations.resolve_runtime_location(self.creme, adapter=self.adapter)

    def test_initialization_preview_apply_and_current_are_exact_and_idempotent(self):
        before_status = subprocess.run(
            ["git", "-C", str(self.store), "status", "--porcelain=v1"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        plan = master_operations.initialize(self.location, apply=False)
        self.assertEqual(plan.status, "PREVIEW")
        self.assertFalse(self.location.record_root.exists())
        self.assertTrue(all(action.action == "create" for action in plan.actions))
        after_status = subprocess.run(
            ["git", "-C", str(self.store), "status", "--porcelain=v1"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        self.assertEqual(after_status, before_status)

        applied = master_operations.initialize(self.location, apply=True)
        self.assertEqual(applied.status, "OK")
        snapshot = self.tree_snapshot(self.location.record_root)
        current = master_operations.initialize(self.location, apply=True)
        self.assertEqual(current.status, "CURRENT")
        self.assertEqual(self.tree_snapshot(self.location.record_root), snapshot)

    def test_legacy_partial_and_malformed_records_never_initialize_opportunistically(self):
        root = self.location.record_root
        root.mkdir(mode=0o700)
        (root / "log.md").write_text("legacy bytes\n", encoding="utf-8")
        before = self.tree_snapshot(root)
        preview = master_operations.initialize(self.location, apply=False)
        applied = master_operations.initialize(self.location, apply=True)
        self.assertEqual(preview.status, "MIGRATION_REQUIRED")
        self.assertEqual(applied.status, "MIGRATION_REQUIRED")
        self.assertEqual(self.tree_snapshot(root), before)

        (root / "log.md").unlink()
        (root / master_runtime.EVENTS_NAME).write_bytes(b"")
        os.chmod(root / master_runtime.EVENTS_NAME, 0o600)
        partial = master_operations.initialize(self.location, apply=True)
        self.assertEqual(partial.status, "REFUSED")

    def test_digest_is_bounded_deterministic_private_and_read_only(self):
        root = self.initialize()
        lease = LeaseHarness()
        lease.acquire("codex", "synthetic")
        writer = master_runtime.RecordWriter(
            root,
            renew=lease.renew,
            lease_snapshot=lease.snapshot,
        )
        writer.append("master", {
            "action": "start",
            "model": "synthetic-model",
            "effort": "high",
            "note": "private note body",
            "next_unit": "unit-four",
            "reconciliation": [{
                "repository": "creme",
                "kind": "head-drift",
                "subject": "branch-main",
                "recorded": "1" * 40,
                "observed": "2" * 40,
                "detail": "synthetic drift",
            }],
        })
        for index in range(3):
            writer.append("goal", {
                "goal_id": f"goal-{index}",
                "status": "active",
                "worktree": f"/private/synthetic/worktree-{index}",
                "branch": f"codex/goal-{index}",
                "checkpoint": f"{index + 1:040x}",
                "next_unit": f"unit-{index}",
            })
        for index in range(2):
            writer.append("decision", {
                "decision_id": f"decision-{index}",
                "status": "open",
                "title": f"decision title {index}",
                "choice": "recommended",
                "reason": "synthetic reason",
                "alternatives": ["alternative"],
                "reversible": True,
                "undo": "restore alternative",
                "evidence": "synthetic-decision.json",
                "authority": "master",
            })
        writer.append("audit", {
            "audit_id": "audit-one",
            "audit_kind": "continuity",
            "verdict": "REJECT",
            "report": "synthetic-audit.md",
            "findings": [
                {
                    "finding_id": f"finding-{index}",
                    "status": "open",
                    "severity": "medium",
                    "summary": f"finding summary {index}",
                    "evidence": "synthetic-finding.json",
                }
                for index in range(2)
            ],
        })
        writer.append("note", self.note_payload(body="DO-NOT-LEAK-FILE-CONTENT"))
        board_path = root / master_runtime.BOARD_NAME
        board_path.write_bytes(master_runtime.render_board(()))
        before = board_path.read_bytes()
        digest = master_operations.digest_record(
            root,
            goals_limit=1,
            decisions_limit=0,
            findings_limit=0,
            discrepancies_limit=0,
            lease_snapshot=lease.snapshot,
        )
        second = master_operations.digest_record(
            root,
            goals_limit=1,
            decisions_limit=0,
            findings_limit=0,
            discrepancies_limit=0,
            lease_snapshot=lease.snapshot,
        )
        self.assertEqual(digest, second)
        self.assertEqual(board_path.read_bytes(), before)
        self.assertTrue(digest["record"]["board_repair"]["required"])
        self.assertEqual(len(digest["goals"]["items"]), 1)
        self.assertEqual(digest["goals"]["omitted"], 2)
        self.assertEqual(digest["goals"]["continuation_key"], "goal-1")
        self.assertEqual(digest["open_decisions"]["omitted"], 2)
        self.assertEqual(digest["open_decisions"]["continuation_key"], "decision-0")
        self.assertEqual(digest["open_audit_findings"]["omitted"], 2)
        self.assertEqual(
            digest["open_audit_findings"]["continuation_key"], "finding-0"
        )
        self.assertEqual(digest["reconciliation_discrepancies"]["omitted"], 1)
        self.assertEqual(
            digest["reconciliation_discrepancies"]["continuation_key"],
            "creme:head-drift:branch-main",
        )
        human = master_operations.render_digest_human(digest)
        self.assertEqual(human, master_operations.render_digest_human(second))
        serialized = json.dumps(digest, sort_keys=True)
        for forbidden in (
            lease.lease["lease_id"],
            "private note body",
            "DO-NOT-LEAK-FILE-CONTENT",
            "/private/synthetic/worktree",
        ):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaisesRegex(master_operations.MasterOperationError, "0..100"):
            master_operations.digest_record(root, goals_limit=101, lease_snapshot=lease.snapshot)

    def test_start_acquires_resumes_and_records_once_per_acquisition(self):
        root = self.initialize()
        lease = LeaseHarness()
        first = master_operations.start_master(
            root,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="first start",
            acquire=lease.acquire,
            renew=lease.renew,
            release=lease.release,
            heartbeat=lease.heartbeat,
            lease_snapshot=lease.snapshot,
        )
        second = master_operations.start_master(
            root,
            client="codex",
            model="changed-on-retry",
            effort="low",
            note="retry",
            acquire=lease.acquire,
            renew=lease.renew,
            release=lease.release,
            heartbeat=lease.heartbeat,
            lease_snapshot=lease.snapshot,
        )
        self.assertEqual(first["status"], "master")
        self.assertEqual(first["mode"], "acquired")
        self.assertEqual(second["mode"], "resumed")
        self.assertTrue(second["event"]["already_present"])
        view = master_runtime.read_record(root)
        self.assertEqual(len(view.events), 1)
        self.assertEqual(view.events[0]["payload"]["model"], "synthetic-model")
        self.assertEqual(lease.acquire_count, 1)
        self.assertEqual(lease.heartbeat_count, 2)

    def test_start_returns_safe_reader_and_explicit_takeover_required_results(self):
        root = self.initialize()
        original = self.tree_snapshot(root)
        live = LeaseHarness(
            lease={"client": "claude", "lease_id": "c" * 32},
            state="live",
            can_renew=False,
        )
        reader = master_operations.start_master(
            root,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="DO-NOT-LEAK-HOLDER-NOTE",
            acquire=live.acquire,
            renew=live.renew,
            release=live.release,
            heartbeat=live.heartbeat,
            lease_snapshot=live.snapshot,
        )
        self.assertEqual(reader, {
            "status": "reader",
            "holder": {"client": "claude", "state": "live"},
        })
        self.assertNotIn("DO-NOT-LEAK", json.dumps(reader))
        self.assertEqual(self.tree_snapshot(root), original)

        lapsed = LeaseHarness(
            lease={"client": "claude", "lease_id": "d" * 32},
            state="lapsed",
            can_renew=False,
        )
        waiting = master_operations.start_master(
            root,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="take over later",
            acquire=lapsed.acquire,
            renew=lapsed.renew,
            release=lapsed.release,
            heartbeat=lapsed.heartbeat,
            lease_snapshot=lapsed.snapshot,
        )
        self.assertEqual(waiting["status"], "takeover-required")
        taken = master_operations.start_master(
            root,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="take over now",
            take_over=True,
            acquire=lapsed.acquire,
            renew=lapsed.renew,
            release=lapsed.release,
            heartbeat=lapsed.heartbeat,
            lease_snapshot=lapsed.snapshot,
        )
        self.assertEqual(taken["status"], "master")
        self.assertEqual(taken["mode"], "taken-over")

    def test_malformed_record_refuses_before_acquisition(self):
        root = self.initialize()
        (root / master_runtime.EVENTS_NAME).write_bytes(b'{"partial":')
        lease = LeaseHarness()
        with self.assertRaises(master_runtime.MasterRecordError):
            master_operations.start_master(
                root,
                client="codex",
                model="synthetic-model",
                effort="high",
                note="must not acquire",
                acquire=lease.acquire,
                renew=lease.renew,
                release=lease.release,
                heartbeat=lease.heartbeat,
                lease_snapshot=lease.snapshot,
            )
        self.assertEqual(lease.acquire_count, 0)

    def test_invalid_start_configuration_refuses_before_acquisition(self):
        root = self.initialize()
        lease = LeaseHarness()
        for changed in (
            {"client": "bad client"},
            {"model": ""},
            {"effort": ""},
            {"note": ""},
        ):
            arguments = {
                "client": "codex",
                "model": "synthetic-model",
                "effort": "high",
                "note": "valid note",
                **changed,
            }
            with self.subTest(changed=changed):
                with self.assertRaises((master_operations.MasterOperationError, master_runtime.MasterRecordError)):
                    master_operations.start_master(
                        root,
                        **arguments,
                        acquire=lease.acquire,
                        renew=lease.renew,
                        release=lease.release,
                        heartbeat=lease.heartbeat,
                        lease_snapshot=lease.snapshot,
                    )
                self.assertEqual(lease.acquire_count, 0)

    def test_failed_precommit_start_releases_but_durable_split_retries(self):
        root = self.initialize()
        lease = LeaseHarness()
        with mock.patch.object(
            master_runtime.RecordWriter,
            "append",
            side_effect=master_runtime.MasterRecordError("precommit failure"),
        ):
            with self.assertRaises(master_runtime.MasterRecordError):
                master_operations.start_master(
                    root,
                    client="codex",
                    model="synthetic-model",
                    effort="high",
                    note="precommit",
                    acquire=lease.acquire,
                    renew=lease.renew,
                    release=lease.release,
                    heartbeat=lease.heartbeat,
                    lease_snapshot=lease.snapshot,
                )
        self.assertEqual(lease.release_count, 1)
        self.assertIsNone(lease.lease)
        self.assertEqual(master_runtime.read_record(root).events, ())

        original_append = master_runtime.RecordWriter.append
        lease = LeaseHarness()

        def split_append(writer, kind, payload, **kwargs):
            def inject(stage):
                if stage == "transaction:after-log-commit":
                    raise RuntimeError("split")
            return original_append(writer, kind, payload, fault=inject, **kwargs)

        with mock.patch.object(master_runtime.RecordWriter, "append", split_append):
            with self.assertRaisesRegex(RuntimeError, "split"):
                master_operations.start_master(
                    root,
                    client="codex",
                    model="synthetic-model",
                    effort="high",
                    note="split",
                    acquire=lease.acquire,
                    renew=lease.renew,
                    release=lease.release,
                    heartbeat=lease.heartbeat,
                    lease_snapshot=lease.snapshot,
                )
        self.assertEqual(lease.release_count, 0)
        self.assertFalse(master_runtime.read_record(root).board_current)
        recovered = master_operations.start_master(
            root,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="retry split",
            acquire=lease.acquire,
            renew=lease.renew,
            release=lease.release,
            heartbeat=lease.heartbeat,
            lease_snapshot=lease.snapshot,
        )
        self.assertTrue(recovered["event"]["already_present"])
        self.assertTrue(recovered["event"]["board_repaired"])
        self.assertEqual(len(master_runtime.read_record(root).events), 1)

    def test_heartbeat_failure_is_recoverable_without_duplicate_start(self):
        root = self.initialize()
        lease = LeaseHarness()
        lease.heartbeat_ok = False
        with self.assertRaisesRegex(master_operations.MasterOperationError, "retry start"):
            master_operations.start_master(
                root,
                client="codex",
                model="synthetic-model",
                effort="high",
                note="heartbeat fails",
                acquire=lease.acquire,
                renew=lease.renew,
                release=lease.release,
                heartbeat=lease.heartbeat,
                lease_snapshot=lease.snapshot,
            )
        self.assertEqual(len(master_runtime.read_record(root).events), 1)
        lease.heartbeat_ok = True
        recovered = master_operations.start_master(
            root,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="heartbeat retry",
            acquire=lease.acquire,
            renew=lease.renew,
            release=lease.release,
            heartbeat=lease.heartbeat,
            lease_snapshot=lease.snapshot,
        )
        self.assertTrue(recovered["event"]["already_present"])
        self.assertEqual(len(master_runtime.read_record(root).events), 1)

    def test_cli_init_event_digest_and_grammar(self):
        with self.assertRaises(SystemExit):
            cli.parser().parse_args(["master", "init", "/tmp/not-allowed"])
        with self.assertRaises(SystemExit):
            cli.parser().parse_args(["master", "init", "--profile", "/tmp/not-allowed"])

        output = io.StringIO()
        with (
            mock.patch("creme.cli._master_location", return_value=(self.location, None)),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(cli.main(["master", "init"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "PREVIEW")
        self.assertFalse(self.location.record_root.exists())

        output = io.StringIO()
        with (
            mock.patch("creme.cli._master_location", return_value=(self.location, None)),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(cli.main([
                "master", "start",
                "--client", "codex",
                "--model", "synthetic-model",
                "--effort", "high",
                "--note", "synthetic start",
            ]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "init-required")

        output = io.StringIO()
        with (
            mock.patch("creme.cli._master_location", return_value=(self.location, None)),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(cli.main(["master", "init", "--apply"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "OK")

        output = io.StringIO()
        reader = {"status": "reader", "holder": {"client": "claude", "state": "live"}}
        with (
            mock.patch("creme.cli._master_location", return_value=(self.location, None)),
            mock.patch("creme.cli.master_operations.start_master", return_value=reader) as start,
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(cli.main([
                "master", "start",
                "--client", "codex",
                "--model", "synthetic-model",
                "--effort", "high",
                "--note", "synthetic start",
                "--take-over",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue()), reader)
        self.assertTrue(start.call_args.kwargs["take_over"])

        semaphore_root = self.workspace / "semaphore"
        event = json.dumps({"kind": "note", "payload": self.note_payload()}).encode()
        stdin = SimpleNamespace(buffer=io.BytesIO(event))
        with (
            mock.patch.dict(os.environ, {
                "CREME_SEMAPHORE_DIR": str(semaphore_root),
                "CREME_MASTER_SESSION_ID": "",
                "CREME_MASTER_LIVENESS_SOCKET": "",
            }, clear=False),
            mock.patch(
                "creme.semaphore._client_process",
                return_value=(os.getpid(), "claude", "synthetic client"),
            ),
        ):
            ok, detail = semaphore.master_acquire("claude", "synthetic CLI")
            self.assertTrue(ok, detail)
            output = io.StringIO()
            with (
                mock.patch("creme.cli._master_location", return_value=(self.location, None)),
                mock.patch("creme.cli.sys.stdin", stdin),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(cli.main(["master", "event", "--from", "-"]), 0)
            self.assertEqual(json.loads(output.getvalue())["event"]["kind"], "note")

            event_file = self.workspace / "event.json"
            event_file.write_bytes(event)
            output = io.StringIO()
            with (
                mock.patch("creme.cli._master_location", return_value=(self.location, None)),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(
                    cli.main(["master", "event", "--from", str(event_file)]), 0
                )
            self.assertEqual(json.loads(output.getvalue())["event"]["kind"], "note")

            before = master_runtime.read_record(self.location.record_root).log_bytes
            bad_stdin = SimpleNamespace(buffer=io.BytesIO(event + b"{}"))
            output = io.StringIO()
            with (
                mock.patch("creme.cli._master_location", return_value=(self.location, None)),
                mock.patch("creme.cli.sys.stdin", bad_stdin),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(cli.main(["master", "event", "--from", "-"]), 2)
            self.assertEqual(master_runtime.read_record(self.location.record_root).log_bytes, before)

            output = io.StringIO()
            with (
                mock.patch("creme.cli._master_location", return_value=(self.location, None)),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(
                    cli.main(["master", "digest", "--goals-limit", "0"]), 0
                )
            digest = json.loads(output.getvalue())
            self.assertEqual(digest["status"], "OK")
            self.assertEqual(digest["goals"]["limit"], 0)

            output = io.StringIO()
            with (
                mock.patch("creme.cli._master_location", return_value=(self.location, None)),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(cli.main(["master", "digest", "--human"]), 0)
            self.assertIn("descriptive, not authority", output.getvalue())


if __name__ == "__main__":
    unittest.main()
