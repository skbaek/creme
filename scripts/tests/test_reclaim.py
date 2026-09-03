from __future__ import annotations

import re
import unittest
from pathlib import Path

from creme import idle_workers
from creme import reclaim
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


class IdleWorkerTest(unittest.TestCase):
    """B4: idleness is measured, and the ownership boundary still decides."""

    def worker(self, pid, cpu, rss_kib=2 * 1024 * 1024, ancestry=()):
        return {
            "pid": pid, "ppid": 1, "rss_kib": rss_kib, "cpu_seconds": cpu,
            "command": "lean --worker", "ancestry": list(ancestry),
        }

    def test_a_worker_is_never_called_idle_on_first_sight(self):
        observations, derived = idle_workers.update_observations(
            [self.worker(10, 5.0)], {}, 1000.0
        )
        self.assertIsNone(derived[10]["idle_seconds"])
        self.assertIsNone(observations["10"]["idle_since"])

    def test_unchanged_cpu_between_samples_establishes_idleness(self):
        first, _ = idle_workers.update_observations([self.worker(10, 5.0)], {}, 1000.0)
        second, derived = idle_workers.update_observations(
            [self.worker(10, 5.0)], first, 1600.0
        )
        self.assertEqual(derived[10]["cpu_percent"], 0.0)
        self.assertEqual(derived[10]["idle_seconds"], 600.0)
        # A third idle sample keeps the original idle start, not the last one.
        _third, later = idle_workers.update_observations(
            [self.worker(10, 5.0)], second, 1900.0
        )
        self.assertEqual(later[10]["idle_seconds"], 900.0)

    def test_a_busy_worker_above_the_cpu_threshold_is_never_idle(self):
        first, _ = idle_workers.update_observations([self.worker(10, 5.0)], {}, 1000.0)
        _second, derived = idle_workers.update_observations(
            [self.worker(10, 65.0)], first, 1100.0
        )
        self.assertEqual(derived[10]["cpu_percent"], 60.0)
        self.assertIsNone(derived[10]["idle_seconds"])

    def test_a_reused_pid_restarts_the_measurement(self):
        first, _ = idle_workers.update_observations([self.worker(10, 500.0)], {}, 1000.0)
        _second, derived = idle_workers.update_observations(
            [self.worker(10, 1.0)], first, 1600.0
        )
        self.assertIsNone(derived[10]["idle_seconds"])

    def test_only_caller_owned_idle_workers_become_targets(self):
        workers = [self.worker(10, 1.0), self.worker(11, 1.0)]
        derived = {10: {"idle_seconds": 900.0}, 11: {"idle_seconds": 900.0}}
        targets, reported = idle_workers.select_reclaimable(workers, derived, {10}, 600.0)
        self.assertEqual(targets, [10])
        self.assertEqual(reported, [11])

    def test_an_idle_worker_below_the_threshold_is_left_alone(self):
        workers = [self.worker(10, 1.0)]
        derived = {10: {"idle_seconds": 60.0}}
        targets, reported = idle_workers.select_reclaimable(workers, derived, {10}, 600.0)
        self.assertEqual((targets, reported), ([], []))

    def test_owner_label_prefers_the_holding_goal_then_the_client(self):
        pattern = re.compile(r"/(?:codex|claude)$")
        held = self.worker(10, 1.0, ancestry=[{"pid": 4, "command": "/usr/bin/claude"}])
        self.assertEqual(idle_workers.owner_label(held, {4: "goal-a"}, pattern), "goal goal-a")
        self.assertEqual(
            idle_workers.owner_label(held, {}, pattern), "client claude pid 4"
        )
        self.assertIn("unattributed", idle_workers.owner_label(self.worker(10, 1.0), {}, pattern))

    def test_pid_narrowing_can_only_shrink_a_proven_target_set(self):
        self.assertEqual(reclaim.narrow_targets((1, 2, 3), (2, 9)), (2,))
        self.assertEqual(reclaim.narrow_targets((1, 2), ()), (1, 2))
        self.assertEqual(reclaim.narrow_targets((), (5,)), ())

    def test_pid_narrowing_is_refused_alongside_hard_pressure(self):
        with self.assertRaises(ValueError):
            reclaim.parse_reclaim_arguments(["--hard-pressure", "--only-pid", "5"])
        with self.assertRaises(ValueError):
            reclaim.parse_reclaim_arguments(["--only-pid", "0"])
        with self.assertRaises(ValueError):
            reclaim.parse_reclaim_arguments(["--only-pid", "5", "--only-pid", "5"])
        self.assertEqual(
            reclaim.parse_reclaim_arguments(["--only-pid", "7"]).only_pids, (7,)
        )

    def test_cpu_time_parsing_matches_ps_output(self):
        self.assertAlmostEqual(reclaim.parse_cpu_seconds("358:00.60"), 21480.6)
        self.assertAlmostEqual(reclaim.parse_cpu_seconds("0:01.30"), 1.3)
        self.assertAlmostEqual(reclaim.parse_cpu_seconds("1:02:03"), 3723.0)
        self.assertIsNone(reclaim.parse_cpu_seconds("nonsense"))
