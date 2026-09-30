"""Tests for the canonical goal-stack core (creme.goal_stack)."""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

from creme.goal_stack import GoalStackError, apply, dumps, loads, summary
from creme.goal_stack import select_next, validate


def fresh_stack(entries=()):
    return {"schema_version": 1, "revision": 0,
            "entries": [copy.deepcopy(e) for e in entries]}


class GoalStackCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for rel in ("goals/a.md", "goals/b.md", "goals/c.md",
                    "master/m.md", ".worktrees/w.md"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture %s\n" % rel, encoding="utf-8")

    def entry(self, eid, **over):
        base = {"id": eid, "title": "Title %s" % eid, "status": "ready",
                "goal": "goals/a.md"}
        base.update(over)
        return base

    def stack_of(self, *entries):
        return fresh_stack(entries)

    def assertStackError(self, func, *args, **kwargs):
        with self.assertRaises(GoalStackError):
            func(*args, **kwargs)


class LoadsDumpsTests(GoalStackCase):
    def test_missing_stack_errors(self):
        for bad in ("", "   \n  "):
            self.assertStackError(loads, bad, self.root)
        self.assertStackError(loads, None, self.root)
        self.assertStackError(loads, 123, self.root)

    def test_invalid_toml_errors(self):
        self.assertStackError(loads, "[[[broken", self.root)

    def test_top_level_keys_frozen(self):
        stack = self.stack_of(self.entry("a"))
        text = dumps(stack)
        with_extra = text + 'extra = 1\n'
        self.assertStackError(loads, with_extra, self.root)
        missing = "schema_version = 1\n"
        self.assertStackError(loads, missing, self.root)

    def test_roundtrip_unicode_escaped(self):
        entry = self.entry(
            "uni",
            title="Tïtle «ünï» — 日本語 🚀",
            done='done "quoted" with \\ backslash',
            next="line1\nline2\ttab ☃",
            reason="",
            trigger="",
        )
        stack = self.stack_of(entry)
        stack["revision"] = 7
        text = dumps(stack)
        back = loads(text, self.root)
        self.assertEqual(back, stack)

    def test_roundtrip_order_revision_and_context_fields(self):
        stack = self.stack_of(
            self.entry("a", context="master/m.md", worktree="wt-1",
                       branch="feat/x", checkpoint="cp-1"),
            self.entry("b", depends_on=["a"]),
            self.entry("c", status="blocked", reason="waiting",
                       done="condition met"),
        )
        stack["revision"] = 3
        back = loads(dumps(stack), self.root)
        self.assertEqual(back, stack)
        self.assertEqual([e["id"] for e in back["entries"]],
                         ["a", "b", "c"])

    def test_error_is_value_error(self):
        self.assertTrue(issubclass(GoalStackError, ValueError))

    def test_rejects_bool_revision_and_schema(self):
        stack = self.stack_of(self.entry("a"))
        stack["revision"] = True
        self.assertStackError(validate, stack, self.root)
        stack["revision"] = 0
        stack["schema_version"] = True
        self.assertStackError(validate, stack, self.root)

    def test_rejects_unknown_and_missing_keys(self):
        bad = self.entry("a", bogus=1)
        self.assertStackError(validate, self.stack_of(bad), self.root)
        bad = self.entry("a")
        del bad["title"]
        self.assertStackError(validate, self.stack_of(bad), self.root)
        top = self.stack_of(self.entry("a"))
        top["surprise"] = 1
        self.assertStackError(validate, top, self.root)

    def test_rejects_duplicate_ids(self):
        stack = self.stack_of(self.entry("a"), self.entry("a"))
        self.assertStackError(validate, stack, self.root)

    def test_rejects_bad_types(self):
        bad = self.entry("a", title=123)
        self.assertStackError(validate, self.stack_of(bad), self.root)
        bad = self.entry("a", depends_on="a")
        self.assertStackError(validate, self.stack_of(bad), self.root)
        bad = self.entry("a", depends_on=["a", "a"])
        self.assertStackError(validate, self.stack_of(bad), self.root)

    def test_id_regex(self):
        for good in ("a", "a0", "x.y-z_w"):
            validate(self.stack_of(self.entry(good)), self.root)
        for badid in ("A", "-a", "_a", ".a", "a b", "", "a/b"):
            bad = self.entry("ok", goal="goals/b.md")
            bad["id"] = badid
            self.assertStackError(validate, self.stack_of(bad), self.root)


class ReferenceTests(GoalStackCase):
    def test_missing_ref_errors(self):
        bad = self.entry("a", goal="goals/nope.md")
        self.assertStackError(validate, self.stack_of(bad), self.root)

    def test_directory_ref_errors(self):
        bad = self.entry("a", goal="goals")
        self.assertStackError(validate, self.stack_of(bad), self.root)

    def test_traversal_escape_errors(self):
        bad = self.entry("a", goal="../outside.md")
        self.assertStackError(validate, self.stack_of(bad), self.root)
        bad = self.entry("a", goal="/etc/hosts")
        self.assertStackError(validate, self.stack_of(bad), self.root)

    def test_url_ref_errors(self):
        bad = self.entry("a", goal="https://example.com/x.md")
        self.assertStackError(validate, self.stack_of(bad), self.root)

    def test_symlink_escape_errors(self):
        outside = self.root.parent / "gs-outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        os.symlink(str(outside), self.root / "goals" / "evil.md")
        bad = self.entry("a", goal="goals/evil.md")
        self.assertStackError(validate, self.stack_of(bad), self.root)

    def test_worktrees_and_master_refs_allowed(self):
        stack = self.stack_of(
            self.entry("a", goal=".worktrees/w.md"),
            self.entry("b", goal="master/m.md"),
        )
        validate(stack, self.root)

    def test_bad_root_errors(self):
        stack = self.stack_of(self.entry("a"))
        self.assertStackError(validate, stack, self.root / "goals" / "a.md")
        self.assertStackError(validate, stack, self.root / "missing-dir")


class DependencyTests(GoalStackCase):
    def test_cycle_errors(self):
        stack = self.stack_of(
            self.entry("a", depends_on=["b"]),
            self.entry("b", depends_on=["a"]),
        )
        self.assertStackError(validate, stack, self.root)
        longer = self.stack_of(
            self.entry("a", depends_on=["b"]),
            self.entry("b", depends_on=["c"]),
            self.entry("c", depends_on=["a"]),
        )
        self.assertStackError(validate, longer, self.root)

    def test_self_dependency_errors(self):
        stack = self.stack_of(self.entry("a", depends_on=["a"]))
        self.assertStackError(validate, stack, self.root)

    def test_unknown_dependency_errors(self):
        stack = self.stack_of(self.entry("a", depends_on=["ghost"]))
        self.assertStackError(validate, stack, self.root)

    def test_requires_goal_or_done(self):
        bad = {"id": "a", "title": "T", "status": "ready"}
        self.assertStackError(validate, self.stack_of(bad), self.root)
        ok = {"id": "a", "title": "T", "status": "ready",
              "done": "condition holds"}
        validate(self.stack_of(ok), self.root)

    def test_blocked_needs_reason(self):
        bad = self.entry("a", status="blocked")
        self.assertStackError(validate, self.stack_of(bad), self.root)
        ok = self.entry("a", status="blocked", reason="waiting on b")
        validate(self.stack_of(ok), self.root)

    def test_parked_needs_trigger_or_reason(self):
        bad = self.entry("a", status="parked")
        self.assertStackError(validate, self.stack_of(bad), self.root)
        validate(self.stack_of(
            self.entry("a", status="parked", trigger="friday")), self.root)
        validate(self.stack_of(
            self.entry("b", status="parked", reason="later")), self.root)

    def test_active_may_not_have_deps(self):
        bad = self.entry("a", status="active", depends_on=["b"])
        stack = self.stack_of(bad, self.entry("b"))
        self.assertStackError(validate, stack, self.root)
        ok = self.entry("a", status="active")
        validate(self.stack_of(ok, self.entry("b")), self.root)


class SelectionTests(GoalStackCase):
    def test_active_not_preempted_by_push(self):
        stack = self.stack_of(
            self.entry("new"),
            self.entry("old", status="active"),
        )
        self.assertEqual(select_next(stack)["id"], "old")

    def test_multiple_active_lists_all_first_in_order(self):
        stack = self.stack_of(
            self.entry("a1", status="active"),
            self.entry("r"),
            self.entry("a2", status="active"),
        )
        self.assertEqual(select_next(stack)["id"], "a1")
        info = summary(stack)
        self.assertEqual(info["active_ids"], ["a1", "a2"])
        self.assertEqual(info["next_id"], "a1")

    def test_ready_skips_unsatisfied_deps(self):
        stack = self.stack_of(
            self.entry("blocked-by-b", depends_on=["b"]),
            self.entry("b"),
        )
        self.assertEqual(select_next(stack)["id"], "b")

    def test_blocked_parked_skipped(self):
        stack = self.stack_of(
            self.entry("b", status="blocked", reason="x"),
            self.entry("p", status="parked", trigger="t"),
        )
        self.assertIsNone(select_next(stack))

    def test_empty_stack_selects_none(self):
        stack = fresh_stack()
        self.assertIsNone(select_next(stack))
        info = summary(stack)
        self.assertEqual(info["total"], 0)
        self.assertIsNone(info["next_id"])

    def test_select_next_is_non_mutating_copy(self):
        stack = self.stack_of(self.entry("a"))
        nxt = select_next(stack)
        nxt["title"] = "MUTATED"
        self.assertEqual(stack["entries"][0]["title"], "Title a")
        before = copy.deepcopy(stack)
        select_next(stack)
        self.assertEqual(stack, before)

    def test_summary_counts(self):
        stack = self.stack_of(
            self.entry("r"),
            self.entry("a", status="active"),
            self.entry("b", status="blocked", reason="x"),
            self.entry("p", status="parked", trigger="t"),
        )
        info = summary(stack)
        self.assertEqual(
            (info["total"], info["ready"], info["active"],
             info["blocked"], info["parked"], info["schema_version"]),
            (4, 1, 1, 1, 1, 1))


class OperationTests(GoalStackCase):
    def test_lifo_default_push_position(self):
        stack = fresh_stack()
        stack, _ = apply(stack, "push",
                         {"entry": self.entry("first")}, self.root)
        stack, _ = apply(stack, "push",
                         {"entry": self.entry("second")}, self.root)
        self.assertEqual([e["id"] for e in stack["entries"]],
                         ["second", "first"])
        self.assertEqual(stack["revision"], 2)

    def test_push_explicit_position(self):
        stack = self.stack_of(self.entry("a"), self.entry("b"))
        stack, _ = apply(stack, "push",
                         {"entry": self.entry("c"), "position": 3},
                         self.root)
        self.assertEqual([e["id"] for e in stack["entries"]],
                         ["a", "b", "c"])

    def test_push_position_boundaries(self):
        stack = self.stack_of(self.entry("a"))
        self.assertStackError(apply, stack, "push",
                              {"entry": self.entry("x"), "position": 0},
                              self.root)
        self.assertStackError(apply, stack, "push",
                              {"entry": self.entry("x"), "position": 3},
                              self.root)
        self.assertStackError(apply, stack, "push",
                              {"entry": self.entry("x"),
                               "position": True}, self.root)

    def test_move_boundaries(self):
        stack = self.stack_of(
            self.entry("a"), self.entry("b"), self.entry("c"))
        stack, _ = apply(stack, "move", {"id": "c", "position": 1},
                         self.root)
        self.assertEqual([e["id"] for e in stack["entries"]],
                         ["c", "a", "b"])
        self.assertStackError(apply, stack, "move",
                              {"id": "a", "position": 0}, self.root)
        self.assertStackError(apply, stack, "move",
                              {"id": "a", "position": 4}, self.root)
        self.assertStackError(apply, stack, "move",
                              {"id": "ghost", "position": 1}, self.root)

    def test_move_noop_returns_unchanged_revision(self):
        stack = self.stack_of(self.entry("a"), self.entry("b"))
        stack["revision"] = 5
        new, record = apply(stack, "move", {"id": "a", "position": 1},
                            self.root)
        self.assertIsNone(record)
        self.assertEqual(new["revision"], 5)
        self.assertEqual(new, stack)

    def test_reorder_permutation(self):
        stack = self.stack_of(
            self.entry("a"), self.entry("b"), self.entry("c"))
        stack, _ = apply(stack, "reorder", {"ids": ["c", "a", "b"]},
                         self.root)
        self.assertEqual([e["id"] for e in stack["entries"]],
                         ["c", "a", "b"])
        self.assertEqual(stack["revision"], 1)

    def test_reorder_rejects_non_permutation(self):
        stack = self.stack_of(self.entry("a"), self.entry("b"))
        for bad_ids in (["a"], ["a", "b", "c"], ["a", "a"],
                        ["a", "ghost"], "ab", [1, 2]):
            self.assertStackError(apply, stack, "reorder",
                                  {"ids": bad_ids}, self.root)

    def test_reorder_identical_is_noop(self):
        stack = self.stack_of(self.entry("a"), self.entry("b"))
        stack["revision"] = 4
        new, record = apply(stack, "reorder", {"ids": ["a", "b"]},
                            self.root)
        self.assertIsNone(record)
        self.assertEqual(new["revision"], 4)

    def test_update_changes(self):
        stack = self.stack_of(self.entry("a"))
        stack, _ = apply(stack, "update",
                         {"id": "a",
                          "changes": {"title": "New",
                                      "status": "active",
                                      "branch": "feat/q"}},
                         self.root)
        got = stack["entries"][0]
        self.assertEqual(
            (got["title"], got["status"], got["branch"]),
            ("New", "active", "feat/q"))
        self.assertEqual(stack["revision"], 1)

    def test_update_rejects_id_and_unknown(self):
        stack = self.stack_of(self.entry("a"))
        self.assertStackError(apply, stack, "update",
                              {"id": "a", "changes": {"id": "b"}},
                              self.root)
        self.assertStackError(apply, stack, "update",
                              {"id": "a", "changes": {"nope": 1}},
                              self.root)
        self.assertStackError(apply, stack, "update",
                              {"id": "a", "changes": {}}, self.root)
        self.assertStackError(apply, stack, "update",
                              {"id": "ghost", "changes": {"title": "T"}},
                              self.root)

    def test_complete_receipt_and_edge_removal(self):
        stack = self.stack_of(
            self.entry("a"),
            self.entry("b", depends_on=["a"]),
        )
        stack["revision"] = 2
        before_entry = copy.deepcopy(stack["entries"][0])
        new, record = apply(stack, "complete",
                            {"id": "a", "evidence": "merged PR #1"},
                            self.root)
        self.assertEqual([e["id"] for e in new["entries"]], ["b"])
        self.assertNotIn("depends_on", new["entries"][0])
        self.assertEqual(new["revision"], 3)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["entry"], before_entry)
        self.assertEqual(record["evidence"], "merged PR #1")
        self.assertEqual(
            (record["old_revision"], record["new_revision"]), (2, 3))
        # Completed entry's dependent is now eligible.
        self.assertEqual(select_next(new)["id"], "b")

    def test_complete_keeps_other_edges(self):
        stack = self.stack_of(
            self.entry("a"),
            self.entry("b"),
            self.entry("c", depends_on=["a", "b"]),
        )
        new, _ = apply(stack, "complete", {"id": "a", "evidence": "done"},
                       self.root)
        by_id = {e["id"]: e for e in new["entries"]}
        self.assertEqual(by_id["c"]["depends_on"], ["b"])

    def test_complete_rejects_empty_evidence(self):
        stack = self.stack_of(self.entry("a"))
        for bad_evidence in ("", "   ", 123, None):
            self.assertStackError(apply, stack, "complete",
                                  {"id": "a", "evidence": bad_evidence},
                                  self.root)

    def test_retire_refuses_dependents(self):
        stack = self.stack_of(
            self.entry("a"),
            self.entry("b", depends_on=["a"]),
        )
        self.assertStackError(apply, stack, "retire",
                              {"id": "a", "reason": "stale"}, self.root)
        # Dependent retires first, then the base can retire.
        stack, record = apply(stack, "retire",
                              {"id": "b", "reason": "not needed"},
                              self.root)
        self.assertEqual(record["status"], "retired")
        self.assertEqual(record["reason"], "not needed")
        self.assertEqual(record["entry"]["id"], "b")
        stack, _ = apply(stack, "retire", {"id": "a", "reason": "stale"},
                         self.root)
        self.assertEqual(stack["entries"], [])

    def test_retire_rejects_empty_reason(self):
        stack = self.stack_of(self.entry("a"))
        self.assertStackError(apply, stack, "retire",
                              {"id": "a", "reason": "  "}, self.root)

    def test_context_fields_accepted_and_ignored_by_order(self):
        stack = fresh_stack()
        entry = self.entry("a", worktree="/tmp/wt", branch="b",
                           checkpoint="c" * 100)
        stack, _ = apply(stack, "push", {"entry": entry}, self.root)
        self.assertEqual(select_next(stack)["id"], "a")
        back = loads(dumps(stack), self.root)
        self.assertEqual(back["entries"][0]["checkpoint"], "c" * 100)

    def test_bootstrap_empty(self):
        stack, record = apply(fresh_stack(), "bootstrap", {}, self.root)
        self.assertIsNone(record)
        self.assertEqual(stack,
                         {"schema_version": 1, "revision": 0, "entries": []})
        nonempty = self.stack_of(self.entry("a"))
        self.assertStackError(apply, nonempty, "bootstrap", {}, self.root)
        self.assertStackError(apply, fresh_stack(), "bootstrap",
                              {"extra": 1}, self.root)

    def test_unknown_action_and_payload_keys(self):
        stack = self.stack_of(self.entry("a"))
        self.assertStackError(apply, stack, "delete", {"id": "a"},
                              self.root)
        self.assertStackError(apply, stack, "push",
                              {"entry": self.entry("x"), "bogus": 1},
                              self.root)
        self.assertStackError(apply, stack, "push", "entry", self.root)

    def test_original_unchanged_on_error(self):
        stack = self.stack_of(
            self.entry("a"),
            self.entry("b", depends_on=["a"]),
        )
        stack["revision"] = 9
        snapshot = copy.deepcopy(stack)
        with self.assertRaises(GoalStackError):
            apply(stack, "reorder", {"ids": ["a"]}, self.root)
        with self.assertRaises(GoalStackError):
            apply(stack, "push",
                  {"entry": self.entry("a")}, self.root)  # duplicate id
        with self.assertRaises(GoalStackError):
            apply(stack, "retire", {"id": "a", "reason": "x"}, self.root)
        with self.assertRaises(GoalStackError):
            validate({"schema_version": 1, "revision": 0,
                      "entries": "nope"}, self.root)
        self.assertEqual(stack, snapshot)

    def test_revision_increments_once_per_mutation(self):
        stack = fresh_stack()
        for i in range(3):
            stack, _ = apply(
                stack, "push",
                {"entry": self.entry("e%d" % i)}, self.root)
        self.assertEqual(stack["revision"], 3)


if __name__ == "__main__":
    unittest.main()
