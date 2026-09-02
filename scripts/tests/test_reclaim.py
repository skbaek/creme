from __future__ import annotations

import unittest
from pathlib import Path

from creme.reclaim import (
    Process,
    build_plan,
    parse_reclaim_arguments,
    process_in_scope,
)


def proc(pid, ppid, command, rss=10):
    return Process(pid, ppid, rss, "Mon Jan  1 00:00:00 2024", command)


class ReclaimPlanTest(unittest.TestCase):
    @staticmethod
    def is_client(process):
        return "ChatGPT.app" in process.command or "Claude.app" in process.command

    def tree(self):
        rows = [
            proc(10, 1, "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
            proc(11, 10, "/Applications/ChatGPT.app/Contents/Resources/codex"),
            proc(12, 11, "/bin/zsh"),
            proc(20, 11, "/tool/lake serve"),
            proc(21, 20, "/tool/lean --server"),
            proc(22, 21, "/tool/lean --worker"),
            proc(30, 1, "/Applications/Claude.app/Contents/MacOS/Claude"),
            proc(31, 30, "/tool/lake serve"),
        ]
        return {row.pid: row for row in rows}

    def test_only_shared_client_tree_is_owned(self):
        plan = build_plan(self.tree(), 12, self.is_client)
        self.assertEqual(plan.owned, (20, 21, 22))
        self.assertEqual(plan.foreign, (31,))
        self.assertEqual(plan.protected_roots, ())
        self.assertEqual(plan.targets, (22, 21, 20))

    def test_active_non_server_descendant_protects_entire_root(self):
        table = self.tree()
        table[23] = proc(23, 21, "/tool/lake env lean Blanc/X.lean")
        plan = build_plan(table, 12, self.is_client)
        self.assertEqual(plan.protected_roots, (20,))
        self.assertEqual(plan.targets, ())

    def test_hard_pressure_includes_frozen_descendant_closure(self):
        table = self.tree()
        table[23] = proc(23, 21, "/tool/lake env lean Blanc/X.lean")
        plan = build_plan(table, 12, self.is_client, hard_pressure=True)
        self.assertEqual(plan.targets, (22, 23, 21, 20))

    def test_no_client_ancestor_refuses_ownership(self):
        table = self.tree()
        table[40] = proc(40, 1, "/bin/terminal")
        table[41] = proc(41, 40, "/bin/zsh")
        plan = build_plan(table, 41, self.is_client)
        self.assertEqual(plan.owned, ())
        self.assertEqual(plan.targets, ())

    def test_goal_scope_separates_sibling_roots_in_one_client_tree(self):
        table = self.tree()
        goal_root = Path("/workspace/blanc/.worktrees/goal")
        table[20] = Process(**{**table[20].__dict__, "cwd": str(goal_root)})
        table[21] = Process(**{**table[21].__dict__, "cwd": str(goal_root)})
        table[22] = Process(**{**table[22].__dict__, "cwd": str(goal_root)})
        table[40] = proc(40, 11, "/tool/lake serve")
        table[41] = proc(41, 40, "/tool/lean --server")
        other_root = "/workspace/blanc/.worktrees/other"
        table[40] = Process(**{**table[40].__dict__, "cwd": other_root})
        table[41] = Process(**{**table[41].__dict__, "cwd": other_root})

        plan = build_plan(
            table,
            12,
            self.is_client,
            candidate_scope=lambda process: process_in_scope(process, (goal_root,)),
        )

        self.assertEqual(plan.owned, (20, 21, 22))
        self.assertEqual(plan.foreign, (31, 40, 41))
        self.assertEqual(plan.targets, (22, 21, 20))

    def test_scoped_hard_pressure_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot use hard-pressure"):
            parse_reclaim_arguments([
                "--hard-pressure",
                "--scope-root",
                "/workspace/blanc/.worktrees/goal",
            ])

    def test_public_kind_does_not_expose_arguments(self):
        table = self.tree()
        self.assertEqual(table[21].kind, "lean-server")
        self.assertNotIn("--server", table[21].kind)


if __name__ == "__main__":
    unittest.main()
