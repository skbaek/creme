from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from creme import semaphore
from creme.adapters.base import Adapter
from creme.cli import parser
from creme.task_wind_down import wind_down


class FakeAdapter(Adapter):
    system = "FixtureOS"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def reclaim(self, arguments):
        self.calls.append(list(arguments))
        return self.results.pop(0)


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

    def adapter_with(self, *specs):
        adapter = FakeAdapter([])
        adapter.results = [result(adapter, **spec) for spec in specs]
        return adapter

    def test_reclaims_verifies_then_releases(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with(
            {"owned": [{"pid": 10}], "survivors": []},
            {"owned": []},
        )

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "OK")
        self.assertEqual(adapter.calls, [[], ["--dry-run"]])
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
        self.assertEqual(adapter.calls, [[]])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_surviving_process_preserves_hold_and_skips_verification(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with(
            {"owned": [{"pid": 10}], "survivors": [10]},
        )

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "REFUSED")
        self.assertEqual(adapter.calls, [[]])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_verification_finding_owned_process_preserves_hold(self):
        self.assertTrue(semaphore.acquire("hard", "goal", "timing")[0])
        adapter = self.adapter_with(
            {"owned": [{"pid": 10}], "survivors": []},
            {"owned": [{"pid": 11}]},
        )

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "REFUSED")
        self.assertEqual(adapter.calls, [[], ["--dry-run"]])
        self.assertEqual(semaphore.snapshot()["hard"]["label"], "goal")

    def test_unavailable_reclamation_preserves_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with({"status": "UNAVAILABLE"})

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "UNAVAILABLE")
        self.assertEqual(adapter.calls, [[]])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_state_write_failure_preserves_hold_after_verified_cleanup(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        adapter = self.adapter_with({"owned": []}, {"owned": []})

        with mock.patch("creme.semaphore._save", side_effect=OSError("fixture")):
            outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(adapter.calls, [[], ["--dry-run"]])
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_other_hold_blocks_before_process_inspection(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        self.assertTrue(semaphore.acquire("soft", "other", "build")[0])
        adapter = self.adapter_with({"owned": []}, {"owned": []})

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "REFUSED")
        self.assertEqual(adapter.calls, [])

    def test_already_released_hold_is_idempotent_when_processes_are_clear(self):
        adapter = self.adapter_with({"owned": []}, {"owned": []})

        outcome = wind_down("goal", adapter)

        self.assertEqual(outcome.status, "OK")
        self.assertIn("no matching hold", outcome.detail)
        self.assertEqual(adapter.calls, [[], ["--dry-run"]])

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
