from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import semaphore
from creme.adapters.base import Adapter
from creme.cli import parser
from creme.profile import ProfileValidation
from creme.task_wind_down import (
    WorktreeScopeError,
    _goal_worktree_roots,
    wind_down,
)


class FakeAdapter(Adapter):
    system = "FixtureOS"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def reclaim(self, arguments):
        self.calls.append(list(arguments))
        return self.results.pop(0)


class AdmissionAdapter(Adapter):
    system = "FixtureOS"

    def memory_headroom(self):
        return self.result("memory_headroom", "OK", "fixture", {
            "memory_free_percent": 80,
            "memory_available_bytes": 24 * 1024 ** 3,
            "physical_memory_bytes": 32 * 1024 ** 3,
            "swap_used_mib": 0,
        })


def result(adapter, status="OK", *, owned=None, protected=None, survivors=None):
    data = {
        "owned": [] if owned is None else owned,
        "protected_roots": [] if protected is None else protected,
    }
    if survivors is not None:
        data["survivors"] = survivors
    return adapter.result(
        "lean_reclaim", status, "fixture", data if status == "OK" else None
    )


class TaskWindDownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter_patcher = mock.patch(
            "creme.semaphore.get_adapter", return_value=AdmissionAdapter()
        )
        adapter_patcher.start()
        self.addCleanup(adapter_patcher.stop)
        policy_patcher = mock.patch(
            "creme.semaphore._runtime_admission_policy",
            return_value={
                "task_memory_gib": 2,
                "heavy_workers": 4,
                "light_workers": 4,
                "physical_memory_gib": 32.0,
                "profile_status": "VALID",
            },
        )
        policy_patcher.start()
        self.addCleanup(policy_patcher.stop)
        self.scope = Path("/workspace/blanc/.worktrees/goal")
        scope_patcher = mock.patch(
            "creme.task_wind_down._goal_worktree_roots",
            return_value=(self.scope,),
        )
        scope_patcher.start()
        self.addCleanup(scope_patcher.stop)

    def expected_calls(self):
        scope = str(self.scope)
        return [
            ["--scope-root", scope],
            ["--dry-run", "--scope-root", scope],
        ]

    def adapter_with(self, *specs):
        adapter = FakeAdapter([])
        adapter.results = [result(adapter, **spec) for spec in specs]
        return adapter

    def test_scope_resolver_finds_only_real_configured_goal_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            creme = workspace / "creme"
            goal = workspace / "blanc" / ".worktrees" / "goal"
            creme.mkdir()
            goal.mkdir(parents=True)
            (goal / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
            with mock.patch(
                "creme.task_wind_down.semaphore.canonical_creme_root",
                return_value=creme,
            ), mock.patch(
                "creme.task_wind_down.load_profile",
                return_value=ProfileValidation("MISSING", "fixture"),
            ):
                roots = _goal_worktree_roots("goal", Adapter())

        self.assertEqual(roots, (goal.resolve(),))

    def test_scope_resolver_includes_sanctioned_disposable_worktrees(self):
        """The build owner calls GOAL-control this goal's own; so must wind-down."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            creme = workspace / "creme"
            creme.mkdir()
            parent = workspace / "blanc" / ".worktrees"
            made = []
            for name in ("goal", "goal-control", "goal-mutation", "goal-rehearsal",
                         "goal-foo", "othergoal"):
                tree = parent / name
                tree.mkdir(parents=True)
                (tree / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
                made.append(tree)
            with mock.patch(
                "creme.task_wind_down.semaphore.canonical_creme_root",
                return_value=creme,
            ), mock.patch(
                "creme.task_wind_down.load_profile",
                return_value=ProfileValidation("MISSING", "fixture"),
            ):
                roots = _goal_worktree_roots("goal", Adapter())

        self.assertEqual(
            set(roots),
            {(parent / name).resolve() for name in
             ("goal", "goal-control", "goal-mutation", "goal-rehearsal")},
        )
        # An unknown suffix and another goal stay outside the scope.
        self.assertNotIn((parent / "goal-foo").resolve(), roots)
        self.assertNotIn((parent / "othergoal").resolve(), roots)

    def test_scope_resolver_rejects_path_shaped_label(self):
        with self.assertRaisesRegex(WorktreeScopeError, "safe worktree scope"):
            _goal_worktree_roots("../other", Adapter())

    def test_reclaims_verifies_then_releases(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with(
            {"owned": [{"pid": 10}], "survivors": []},
            {"owned": []},
        )

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "OK")
        self.assertEqual(adapter.calls, self.expected_calls())
        self.assertEqual(semaphore.snapshot()["soft"], [])
        self.assertIn("reclaim", outcome.data)
        self.assertIn("verification", outcome.data)

    def test_protected_root_preserves_hold_and_skips_verification(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with(
            {"owned": [{"pid": 10}], "protected": [10]},
        )

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "REFUSED")
        self.assertEqual(adapter.calls, [self.expected_calls()[0]])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_surviving_process_preserves_hold_and_skips_verification(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with(
            {"owned": [{"pid": 10}], "survivors": [10]},
        )

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "REFUSED")
        self.assertEqual(adapter.calls, [self.expected_calls()[0]])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_verification_finding_owned_process_preserves_hold(self):
        self.assertTrue(semaphore.acquire("hard", "goal", "timing")[0])
        adapter = self.adapter_with(
            {"owned": [{"pid": 10}], "survivors": []},
            {"owned": [{"pid": 11}]},
        )

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "REFUSED")
        self.assertEqual(adapter.calls, self.expected_calls())
        self.assertEqual(semaphore.snapshot()["hard"]["label"], "goal")

    def test_unavailable_reclamation_preserves_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with({"status": "UNAVAILABLE"})

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertEqual(adapter.calls, [self.expected_calls()[0]])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_state_write_failure_preserves_hold_after_verified_cleanup(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with({"owned": []}, {"owned": []})

        with mock.patch("creme.semaphore._save", side_effect=OSError("fixture")):
            outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(adapter.calls, self.expected_calls())
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_other_soft_hold_remains_while_goal_scoped_wind_down_succeeds(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        self.assertTrue(semaphore.acquire("soft", "other", "build")[0])
        adapter = self.adapter_with({"owned": []}, {"owned": []})

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "OK")
        self.assertEqual(adapter.calls, self.expected_calls())
        self.assertEqual(
            [item["label"] for item in semaphore.snapshot()["soft"]],
            ["other"],
        )

    def test_unresolvable_goal_scope_preserves_hold_without_process_inspection(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with({"owned": []}, {"owned": []})

        with mock.patch(
            "creme.task_wind_down._goal_worktree_roots",
            side_effect=WorktreeScopeError("fixture scope failure"),
        ):
            outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "REFUSED")
        self.assertEqual(adapter.calls, [])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_already_released_hold_is_idempotent_when_processes_are_clear(self):
        adapter = self.adapter_with({"owned": []}, {"owned": []})

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "OK")
        self.assertIn("no matching hold", outcome.detail)
        self.assertEqual(adapter.calls, self.expected_calls())

    def test_cli_refuses_ambiguous_wind_down_options_without_adapter_call(self):
        arguments = parser().parse_args(
            ["reclaim", "--wind-down", "goal", "--dry-run"]
        )
        with mock.patch("creme.cli._json") as emit, mock.patch(
            "creme.cli.get_adapter"
        ) as get_adapter:
            self.assertEqual(arguments.func(arguments), 2)

        get_adapter.assert_not_called()
        self.assertEqual(emit.call_args.args[0]["status"], "REFUSED")

    def test_empty_wind_down_label_does_not_fall_through_to_plain_reclaim(self):
        arguments = parser().parse_args(["reclaim", "--wind-down", ""])
        adapter = self.adapter_with({"owned": []}, {"owned": []})
        with mock.patch("creme.cli._json") as emit, mock.patch(
            "creme.cli.get_adapter", return_value=adapter
        ):
            self.assertEqual(arguments.func(arguments), 2)

        self.assertEqual(adapter.calls, [])
        self.assertEqual(emit.call_args.args[0]["status"], "REFUSED")


if __name__ == "__main__":
    unittest.main()
