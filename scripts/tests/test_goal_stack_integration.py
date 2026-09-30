"""End-to-end integration of the canonical goal stack with the master record.

Covers runtime adoption, stack-backed digests, reconciliation scoping, the
`master stack` CLI surface, and read-only history. Never touches a live
goal store: every fixture record lives under a temporary directory with an
injected lease.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import (
    cli,
    goal_stack,
    goal_stack_store,
    master_operations,
    master_reconcile,
    master_runtime,
)

STACK_NEXT = "python3 -m creme master stack next"


def no_lease():
    return {"schema_version": 4, "lease": None}


def no_lease_status():
    return "master: none\n"


class StackIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.store_root = self.workspace / "plans"
        self.store_root.mkdir()
        self.record_root = self.store_root / "master"
        master_runtime.initialize_empty_record(self.record_root)
        for rel in ("goals/a.md", "goals/b.md"):
            path = self.store_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture %s\n" % rel, encoding="utf-8")
        self.lease_snapshot = lambda: {
            "schema_version": 4,
            "lease": {"client": "codex", "lease_id": "7" * 32},
        }
        self.writer = master_runtime.RecordWriter(
            self.record_root,
            renew=lambda: (True, "synthetic holder"),
            lease_snapshot=self.lease_snapshot,
        )
        self.location = SimpleNamespace(
            record_root=self.record_root,
            goal_store=self.store_root,
        )

    # -- fixtures ------------------------------------------------------

    @staticmethod
    def goal_payload(gid="legacy-goal", status="active"):
        return {
            "goal_id": gid,
            "status": status,
            "worktree": "/synthetic/legacy",
            "branch": "codex/legacy",
            "checkpoint": "0" * 40,
            "next_unit": "legacy-next",
        }

    @staticmethod
    def decision_payload(did="decision-1"):
        return {
            "decision_id": did,
            "status": "open",
            "title": "keep the old board readable",
            "choice": "adopt the stack",
            "reason": "one scheduler",
            "alternatives": ["two queues"],
            "reversible": False,
            "undo": None,
            "evidence": "synthetic-decision.json",
            "authority": "master",
        }

    @staticmethod
    def adoption_payload(action="replace"):
        return {
            "procedure_id": "master-goal-stack-v1",
            "action": action,
            "failure": "goal events drifted into a competing queue",
            "replacement": "goal-stack.toml is the sole live order",
            "control": "validation and authenticated writes",
            "evidence": "synthetic-adoption",
        }

    def stack_candidate(self):
        return {
            "schema_version": 1,
            "revision": 0,
            "entries": [
                {"id": "second", "title": "Second unit", "status": "active",
                 "goal": "goals/b.md", "next": "do second"},
                {"id": "first", "title": "First unit", "status": "ready",
                 "goal": "goals/a.md", "next": "do first"},
                {"id": "inline", "title": "Inline queued",
                 "status": "ready", "done": "inline condition met"},
            ],
        }

    def adopt_with_manifest(self):
        result = goal_stack_store.mutate(
            self.record_root, "init", {"stack": self.stack_candidate()},
            writer=self.writer,
        )
        self.assertTrue(result["changed"])
        return result["stack"]

    def digest(self, **kwargs):
        kwargs.setdefault("lease_snapshot", no_lease)
        kwargs.setdefault("lease_status", no_lease_status)
        return master_operations.digest_record(self.record_root, **kwargs)

    # -- runtime adoption ----------------------------------------------

    def test_adoption_drops_legacy_goals_pins_next_and_keeps_history(self):
        self.writer.append("goal", self.goal_payload())
        self.writer.append("decision", self.decision_payload())
        self.writer.append("note", {
            "title": "last note", "note": "old next lives here",
            "evidence": "synthetic-note.json", "next_unit": "note-next",
        })
        self.writer.append("procedure", self.adoption_payload())
        board = master_runtime.read_record(self.record_root).expected_board
        self.assertEqual(board["goals"], [])
        self.assertEqual(board["next_unit"], STACK_NEXT)
        self.assertEqual(len(board["open_decisions"]), 1)
        self.assertIsNotNone(board["last_event"])
        self.assertEqual(board["last_event"]["kind"], "procedure")

    def test_new_goal_events_refused_but_notes_and_decisions_allowed(self):
        self.writer.append("goal", self.goal_payload())
        self.adopt_with_manifest()
        with self.assertRaises(master_runtime.MasterRecordError):
            self.writer.append("goal", self.goal_payload(gid="late-goal"))
        self.writer.append("note", {
            "title": "still allowed", "note": "notes are not scheduling",
            "evidence": "synthetic-note.json", "next_unit": "",
        })
        self.writer.append("decision", self.decision_payload(did="decision-2"))
        board = master_runtime.read_record(self.record_root).expected_board
        self.assertEqual(board["goals"], [])
        self.assertEqual(len(board["open_decisions"]), 1)

    # -- stack-backed digests -------------------------------------------

    def test_digest_uses_canonical_order_active_first_and_revision(self):
        self.writer.append("goal", self.goal_payload())
        self.adopt_with_manifest()
        digest = self.digest()
        items = digest["goals"]["items"]
        self.assertEqual([row["goal_id"] for row in items],
                         ["second", "first", "inline"])
        self.assertNotIn("legacy-goal",
                         [row["goal_id"] for row in items])
        # Active work is not preempted: next_unit comes from select_next,
        # not from the last legacy note.
        self.assertEqual(digest["next_unit"], "do second")
        self.assertEqual(digest["stack"]["revision"], 0)
        self.assertEqual(digest["stack"]["source"], "goal-stack.toml")
        self.assertEqual(digest["stack"]["next_id"], "second")
        self.assertEqual(digest["stack"]["count"], 3)

    def test_focused_digest_preserves_canonical_order_without_sorting(self):
        self.adopt_with_manifest()
        digest = master_operations.focused_digest_record(
            self.record_root,
            lease_snapshot=no_lease,
            lease_status=no_lease_status,
        )
        self.assertEqual(
            [row["goal_id"] for row in digest["goals"]["items"]],
            ["second", "first", "inline"],
        )
        human = master_operations.render_focused_digest_human(digest)
        self.assertIn("Second unit", human)
        self.assertIn("do second", human)

    def test_digest_human_shows_titles_and_next(self):
        self.adopt_with_manifest()
        human = master_operations.render_digest_human(self.digest())
        self.assertIn("Second unit", human)
        self.assertIn("do second", human)
        self.assertIn("revision 0", human)

    def test_durable_digest_survives_unavailable_live_lease_inspection(self):
        self.adopt_with_manifest()
        self.writer.append("decision", self.decision_payload())
        for source in ('snapshot', 'status'):
            with self.subTest(source=source):
                denied = mock.Mock(side_effect=master_operations.semaphore.SemaphoreError('read-only sandbox'))
                digest = master_operations.focused_digest_record(
                    self.record_root,
                    lease_snapshot=denied if source == 'snapshot' else no_lease,
                    lease_status=denied if source == 'status' else no_lease_status,
                )
                self.assertIsNone(digest['lease']['present'])
                self.assertEqual(digest['lease']['state'], 'unavailable')
                self.assertEqual(digest['stack']['next_id'], 'second')
                self.assertEqual(digest['open_decisions']['all_ids'], ['decision-1'])
                self.assertIn('lease: unknown (unavailable)', master_operations.render_digest_human(digest))

    def test_lookup_returns_full_stack_entry(self):
        self.adopt_with_manifest()
        found = master_operations.lookup_digest_record(
            self.record_root, kind="goal", identifier="first")
        item = found["lookup"]["item"]
        self.assertEqual(item["goal_id"], "first")
        self.assertEqual(item["title"], "First unit")
        self.assertEqual(item["goal"], "goals/a.md")
        self.assertEqual(found["stack"]["revision"], 0)

    def test_lookup_unknown_goal_points_to_history(self):
        self.adopt_with_manifest()
        with self.assertRaises(master_operations.MasterOperationError) as ctx:
            master_operations.lookup_digest_record(
                self.record_root, kind="goal", identifier="gone")
        self.assertIn("master stack history gone", str(ctx.exception))

    def test_missing_corrupt_pending_stack_refuses_without_fallback(self):
        self.writer.append("goal", self.goal_payload())
        self.writer.append("procedure", self.adoption_payload())
        manifest = self.store_root / goal_stack_store.NAME
        with self.assertRaises(master_runtime.MasterRecordError) as ctx:
            self.digest()
        self.assertIn("never fall back", str(ctx.exception))
        manifest.write_text("not toml [[[\n", encoding="utf-8")
        with self.assertRaises(master_runtime.MasterRecordError):
            self.digest()
        manifest.write_text(
            'schema_version = 1\nrevision = 0\nentries = []\n',
            encoding="utf-8",
        )
        (self.store_root / goal_stack_store.PENDING).write_text(
            "{}\n", encoding="utf-8")
        with self.assertRaises(master_runtime.MasterRecordError) as ctx:
            self.digest()
        self.assertIn("recover", str(ctx.exception))

    def test_legacy_digest_unchanged_before_adoption(self):
        self.writer.append("goal", self.goal_payload())
        digest = self.digest()
        self.assertNotIn("stack", digest)
        self.assertEqual(
            [row["goal_id"] for row in digest["goals"]["items"]],
            ["legacy-goal"],
        )
        self.assertEqual(digest["next_unit"], "legacy-next")

    def test_completion_of_last_goal_roundtrips_to_empty(self):
        self.adopt_with_manifest()
        for gid in ("second", "first", "inline"):
            result = goal_stack_store.mutate(
                self.record_root, "complete",
                {"id": gid, "evidence": "done %s" % gid},
                writer=self.writer)
        stack = result["stack"]
        self.assertEqual(stack["entries"], [])
        text = goal_stack.dumps(stack)
        self.assertEqual(goal_stack.loads(text, self.store_root), stack)
        live = goal_stack_store.read(self.record_root)
        self.assertEqual(live["entries"], [])
        digest = self.digest()
        self.assertEqual(digest["goals"]["items"], [])
        self.assertIsNone(digest["stack"]["next_id"])
        human = master_operations.render_digest_human(digest)
        self.assertIn("no eligible goal", human)

    # -- store mutations --------------------------------------------------

    def test_store_mutations_with_fake_lease(self):
        stack = self.adopt_with_manifest()
        result = goal_stack_store.mutate(
            self.record_root, "push",
            {"entry": {"id": "third", "title": "Third", "status": "ready",
                        "goal": "goals/a.md", "next": "do third"}},
            writer=self.writer,
        )
        self.assertEqual(result["stack"]["revision"], 1)
        self.assertEqual(result["stack"]["entries"][0]["id"], "third")
        with self.assertRaises(goal_stack_store.StackStoreError):
            goal_stack_store.mutate(
                self.record_root, "move",
                {"id": "third", "position": 3},
                expected_revision=99, writer=self.writer)
        result = goal_stack_store.mutate(
            self.record_root, "complete",
            {"id": "third", "evidence": "shipped"},
            expected_revision=1, writer=self.writer)
        archive = result["archive"]
        self.assertEqual(archive["status"], "complete")
        self.assertEqual(archive["entry"]["id"], "third")
        self.assertEqual(archive["evidence"], "shipped")
        live = goal_stack_store.read(self.record_root)
        self.assertNotIn("third", [e["id"] for e in live["entries"]])

    # -- reconciliation scoping --------------------------------------------

    def git(self, root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments], cwd=root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True, text=True,
        )
        return completed.stdout.strip()

    def test_reconcile_only_worktree_entries(self):
        repository = self.workspace / "repo"
        repository.mkdir()
        self.git(repository, "init", "-q")
        self.git(repository, "config", "user.name", "Synthetic")
        self.git(repository, "config", "user.email", "s@example.invalid")
        (repository / "tracked.txt").write_text("x\n", encoding="utf-8")
        self.git(repository, "add", "tracked.txt")
        self.git(repository, "commit", "-q", "-m", "initial")
        stack = self.stack_candidate()
        stack["entries"].append(
            {"id": "wt-one", "title": "Worktree one", "status": "ready",
             "goal": "goals/a.md", "worktree": "/nonexistent/wt-one"},
        )
        goal_stack_store.mutate(
            self.record_root, "init", {"stack": stack}, writer=self.writer)
        result = master_reconcile.reconcile_record(
            self.record_root, {"synthetic": repository})
        subjects = [row["subject"] for row in result.discrepancies]
        self.assertIn("goal:wt-one", subjects)
        # Queued inline entries without a worktree invent no obligations.
        self.assertFalse(
            [subject for subject in subjects if subject.startswith("goal:inline")],
            subjects,
        )
        self.assertFalse(
            [subject for subject in subjects if ":branch" in subject
             and "wt-one" in subject],
            subjects,
        )
        self.assertTrue(result.repositories)

    # -- CLI mapping and read commands ---------------------------------------

    def write_json(self, name, value):
        path = self.workspace / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def test_cli_mutation_payload_mapping(self):
        built = cli.parser()
        entry_file = self.write_json(
            "entry.json",
            {"id": "n", "title": "N", "status": "ready", "done": "d"},
        )
        args = built.parse_args(
            ["master", "stack", "push", "--from", entry_file,
             "--position", "2", "--expect-revision", "4"])
        action, payload, expected = cli._stack_mutation_request(args)
        self.assertEqual(
            (action, payload, expected),
            ("push", {"entry": {"id": "n", "title": "N", "status": "ready",
                                "done": "d"}, "position": 2}, 4),
        )
        args = built.parse_args(["master", "stack", "move", "n", "1"])
        self.assertEqual(cli._stack_mutation_request(args),
                         ("move", {"id": "n", "position": 1}, None))
        args = built.parse_args(["master", "stack", "reorder", "a", "b"])
        self.assertEqual(cli._stack_mutation_request(args),
                         ("reorder", {"ids": ["a", "b"]}, None))
        changes_file = self.write_json("changes.json", {"title": "New"})
        args = built.parse_args(
            ["master", "stack", "update", "n", "--from", changes_file])
        self.assertEqual(cli._stack_mutation_request(args),
                         ("update", {"id": "n", "changes": {"title": "New"}},
                          None))
        args = built.parse_args(
            ["master", "stack", "complete", "n", "--evidence", "did it"])
        self.assertEqual(cli._stack_mutation_request(args),
                         ("complete", {"id": "n", "evidence": "did it"}, None))
        args = built.parse_args(
            ["master", "stack", "retire", "n", "--reason", "stale"])
        self.assertEqual(cli._stack_mutation_request(args),
                         ("retire", {"id": "n", "reason": "stale"}, None))

    def test_cli_formatters_cover_empty_and_next(self):
        self.assertIn("no eligible goal",
                      cli._format_stack_next_human(None))
        text = cli._format_stack_list_human(
            {"schema_version": 1, "revision": 0, "entries": []})
        self.assertIn("no eligible goal", text)
        text = cli._format_stack_list_human(self.stack_candidate())
        self.assertLess(text.index("second"), text.index("first"))
        self.assertIn("Second unit", text)
        self.assertIn("do second", text)

    def test_compact_list_is_one_line_per_entry(self):
        text = cli._format_stack_list_human(self.stack_candidate())
        lines = text.splitlines()
        # Header + one line per entry + next summary + a single show hint.
        self.assertEqual(len(lines), 1 + 3 + 1 + 1)
        self.assertNotIn("retrieve:", text)
        self.assertEqual(text.count("show: python3 -m creme master stack show ID"), 1)
        self.assertIn("next: second (do second)", text)

    def test_mutation_json_rejects_duplicate_keys(self):
        path = self.workspace / "dup.json"
        path.write_text(
            '{"id": "n", "id": "m", "title": "T", "status": "ready",'
            ' "done": "d"}\n',
            encoding="utf-8",
        )
        args = Namespace(stack_action="push", source=str(path),
                         position=None, expect_revision=None)
        with self.assertRaises(master_runtime.MasterRecordError):
            cli._stack_mutation_request(args)

    def test_history_exact_id_searches_every_receipt_file(self):
        archive = self.store_root.joinpath(*cli.STACK_ARCHIVE_PARTS)
        archive.mkdir(parents=True, exist_ok=True)
        total = cli.STACK_HISTORY_RECEIPT_LIMIT + 5
        for index in range(total):
            gid = "needle" if index == 0 else "bulk-%d" % index
            (archive / ("%08d.json" % index)).write_text(
                json.dumps({"action": "push",
                            "payload": {"id": gid},
                            "revision": index,
                            "timestamp": "2026-09-30T00:00:00.000000+00:00"}),
                encoding="utf-8",
            )
        broad = cli.stack_history_report(
            self.record_root, self.store_root, None)
        self.assertEqual(broad["receipt_count"],
                         cli.STACK_HISTORY_RECEIPT_LIMIT)
        self.assertEqual(broad["omitted_receipts"],
                         total - cli.STACK_HISTORY_RECEIPT_LIMIT)
        detail = cli.stack_history_report(
            self.record_root, self.store_root, "needle")
        self.assertEqual(len(detail["receipts"]), 1)
        self.assertEqual(detail["receipts"][0]["file"], "00000000.json")
        self.assertEqual(detail["omitted_receipts"], 0)

    def test_history_includes_init_payload_ids(self):
        self.write_receipt("00000002.json", {
            "schema_version": 1, "action": "init",
            "payload": {"stack": {"schema_version": 1, "revision": 0,
                                  "entries": [{"id": "init-a"},
                                              {"id": "init-b"}]}},
            "revision": 0, "before_sha256": None,
            "after_sha256": "3" * 64,
            "actor": {"client": "codex", "acquisition_digest": "2" * 64},
            "timestamp": "2026-09-30T00:00:00.000000+00:00",
            "archive": None,
        })
        report = cli.stack_history_report(
            self.record_root, self.store_root, None)
        terminal = {row["id"]: row["latest_action"]
                    for row in report["ids"]}
        self.assertEqual(terminal["init-a"], "stack:init")
        self.assertEqual(terminal["init-b"], "stack:init")

    def patched_location(self):
        return mock.patch.object(
            cli, "_master_location", return_value=(self.location, None))

    def run_cmd(self, func, **kwargs):
        kwargs.setdefault("json", True)
        buffer = io.StringIO()
        with self.patched_location(), contextlib.redirect_stdout(buffer):
            code = func(Namespace(**kwargs))
        return code, json.loads(buffer.getvalue())

    def test_cli_read_commands_end_to_end(self):
        self.writer.append("goal", self.goal_payload())
        self.adopt_with_manifest()
        code, listed = self.run_cmd(cli.cmd_master_stack_list)
        self.assertEqual(code, 0)
        self.assertEqual([e["id"] for e in listed["stack"]["entries"]],
                         ["second", "first", "inline"])
        code, nxt = self.run_cmd(cli.cmd_master_stack_next)
        self.assertEqual((code, nxt["next"]["id"]), (0, "second"))
        code, shown = self.run_cmd(cli.cmd_master_stack_show,
                                   entry_id="first")
        self.assertEqual((code, shown["entry"]["title"]), (0, "First unit"))
        buffer = io.StringIO()
        with self.patched_location(), contextlib.redirect_stdout(buffer):
            code = cli.cmd_master_stack_show(
                Namespace(entry_id="ghost", json=True))
        self.assertEqual(code, 2)
        code, valid = self.run_cmd(cli.cmd_master_stack_validate)
        self.assertEqual((code, valid["revision"], valid["count"]),
                         (0, 0, 3))

    def test_cli_mutation_without_lease_reports_refused(self):
        # The --from file fails before any lease is touched.
        code, body = self.run_cmd(
            cli.cmd_master_stack_mutate, stack_action="push",
            source=str(self.workspace / "missing.json"),
            position=None, expect_revision=None)
        self.assertEqual(code, 2)
        self.assertEqual(body["status"], "refused")

    # -- read-only history -----------------------------------------------------

    def write_receipt(self, name, receipt):
        archive = self.store_root.joinpath(*cli.STACK_ARCHIVE_PARTS)
        archive.mkdir(parents=True, exist_ok=True)
        (archive / name).write_text(
            json.dumps(receipt), encoding="utf-8")

    def test_history_summary_and_selected_id(self):
        self.writer.append("goal", self.goal_payload(
            gid="gone", status="complete"))
        self.writer.append("goal", self.goal_payload(gid="old"))
        self.write_receipt("00000001.json", {
            "schema_version": 1, "action": "complete",
            "payload": {"id": "gone", "evidence": "did it"},
            "revision": 5, "before_sha256": "0" * 64,
            "after_sha256": "1" * 64,
            "actor": {"client": "codex", "acquisition_digest": "2" * 64},
            "timestamp": "2026-09-30T00:00:00.000000+00:00",
            "archive": {"id": "gone", "status": "complete",
                        "entry": {"id": "gone"}, "evidence": "did it",
                        "old_revision": 4, "new_revision": 5},
        })
        report = cli.stack_history_report(
            self.record_root, self.store_root, None)
        terminal = {row["id"]: row["latest_action"]
                    for row in report["ids"]}
        self.assertEqual(terminal["gone"], "stack:complete")
        self.assertEqual(terminal["old"], "goal:active")
        detail = cli.stack_history_report(
            self.record_root, self.store_root, "gone")
        self.assertEqual(len(detail["legacy_goal_events"]), 1)
        self.assertEqual(len(detail["receipts"]), 1)
        self.assertEqual(
            detail["receipts"][0]["receipt"]["archive"]["evidence"], "did it")
        with self.assertRaises(master_operations.MasterOperationError):
            cli.stack_history_report(
                self.record_root, self.store_root, "ghost")
        human = cli._format_history_human(report, None)
        self.assertIn("gone: stack:complete", human)
        code, body = self.run_cmd(cli.cmd_master_stack_history,
                                  entry_id="gone")
        self.assertEqual(code, 0)
        self.assertEqual(body["id"], "gone")


if __name__ == "__main__":
    unittest.main()
