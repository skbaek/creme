"""Behaviour of `lake-build --walk` with Lake, the probe, and the semaphore faked."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock, patch

from creme import build_ownership as owned
from creme.cli import cmd_lake_build


# Top imports Left and Right; both import Base.
DIAMOND = {
    "Pkg.Top": {"Pkg.Left", "Pkg.Right"},
    "Pkg.Left": {"Pkg.Base"},
    "Pkg.Right": {"Pkg.Base"},
    "Pkg.Base": set(),
}


class _FakeHost:
    """A package whose modules are stale until a fake Lake build builds them.

    The fake semaphore admits any estimate up to `limit_gib` and refuses a
    larger one with `refusal`; the fake estimator charges 2 GiB per stale
    module, so the whole closure and a single module price differently.
    """

    def __init__(self, graph, *, limit_gib=2, refusal="LIGHT_ONLY", fail=(), unit_refusal=None):
        self.graph = graph
        self.built: set[str] = set()
        self.limit_gib = limit_gib
        self.refusal = refusal
        self.fail = set(fail)
        self.unit_refusal = unit_refusal
        self.lake_calls: list[list[str]] = []
        self.acquires: list[dict] = []
        self.releases = 0
        self.rows: list[dict] = []
        self.output = io.StringIO()

    # -- fakes -------------------------------------------------------------
    def stale_evidence(self, _worktree, targets, _lake):
        unbuilt = set(self.graph) - self.built
        modules = owned.stale_closure_modules(self.graph, targets, unbuilt)
        return {
            "roots": list(targets), "package_roots": list(targets), "resolution": "fixture",
            "stale": len(modules), "detail": "fixture probe",
            "stale_set": sorted(modules), "graph": self.graph,
        }

    def derive(self, *args, **_kwargs):
        stale = args[6]
        return max(1, 2 * len(stale["stale_set"])), {"source": "fixture estimate", "kind": "measured"}

    def acquire(self, _goal, _note, _lease, **kwargs):
        self.acquires.append(kwargs)
        if self.unit_refusal is not None and kwargs["memory_gib"] <= self.limit_gib:
            return False, f"{self.unit_refusal} — fixture unit refusal"
        if kwargs["memory_gib"] > self.limit_gib:
            return False, f"{self.refusal} — fixture: {kwargs['memory_gib']} GiB does not fit"
        return True, "ADMITTED_SOFT — fixture"

    def release(self, _goal):
        self.releases += 1
        return True, "released"

    def popen(self, args, **_kwargs):
        targets = args[args.index("--verbose") + 1:]
        self.lake_calls.append(list(targets))
        host = self

        class FakeProc:
            pid = 4321
            stdout = iter([])

            def wait(self, timeout=None):
                if set(targets) & host.fail:
                    return 1
                closure = owned.stale_closure_modules(host.graph, targets, set(host.graph))
                host.built |= closure
                return 0

        return FakeProc()

    # -- driver ------------------------------------------------------------
    def run(self, targets, **kwargs) -> int:
        class FakeSampler:
            def __init__(self, _pid, worktree=None):
                self.peak_rss_mib = 1000.0
                self.peak_lean_rss_mib = 800.0
                self.max_concurrent_lean = 1
                self.module_peak_mib = {}
                self.samples = 2
                self.unavailable_samples = 0

            def start(self):
                pass

            def stop(self):
                pass

        class FakeRenewer:
            def __init__(self, *_args, **_kwargs):
                self.verdicts: list[str] = []
                self.refused = False
                self.cleanup_proved = True

            def start(self):
                pass

            def stop(self):
                pass

        identity = lambda _worktree, modules, *_rest: (  # noqa: E731
            {"repository_identity": "repo", "input_context": "ctx",
             "module_inputs": {str(m): f"in-{m}" for m in modules}},
            "fixture identity",
        )
        with contextlib.ExitStack() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.dict(os.environ, {
                "CREME_BUILD_LEDGER": str(tmp / "ledger.jsonl"),
                "CREME_SEMAPHORE_DIR": str(tmp),
            }))
            for target, value in [
                ("_worktree_identity", Mock(return_value=(Path.cwd(), "g"))),
                ("_apparent_goal", Mock(return_value="g")),
                ("resolve_toolchain", Mock(return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))),
                ("stale_evidence", self.stale_evidence),
                ("worktree_digests", Mock(return_value=("tc", "mf"))),
                ("build_input_identity", identity),
                ("classify_contention", Mock(return_value=("tolerant", {"reason": "fixture"}))),
                ("derive_memory_gib", self.derive),
                ("semaphore.adaptive_acquire", self.acquire),
                ("semaphore.adaptive_release", self.release),
                ("guard_bin", Mock(return_value=Path("/guard"))),
                ("subprocess.Popen", self.popen),
                ("ProcessSampler", FakeSampler),
                ("RenewalThread", FakeRenewer),
                ("Watchdog", self.watchdog_class()),
                ("_process_group_alive", Mock(return_value=False)),
                ("_module_hashes", Mock(return_value={})),
                ("_swap_gib", Mock(return_value=1.0)),
                ("repeat_failure", Mock(return_value=None)),
                ("append_ledger", self.rows.append),
            ]:
                stack.enter_context(patch(f"creme.build_ownership.{target}", value))
            return owned.run_lake_build("g", targets, stdout=self.output, **kwargs)

    def watchdog_class(self):
        """A watchdog that never sees pressure: the real one samples the real host."""

        class QuietWatchdog:
            def __init__(self, *_args, **_kwargs):
                self.retracted = False
                self.cleanup_proved = True
                self.min_available_gib = None
                self.events: list[str] = []

            def start(self):
                pass

            def stop(self):
                pass

        return QuietWatchdog

    def summary(self) -> dict:
        lines = [line for line in self.output.getvalue().splitlines() if line.startswith("{")]
        return json.loads(lines[-1])


class WalkBehaviourTest(unittest.TestCase):
    def test_an_admitted_whole_closure_is_one_ordinary_build(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=64)
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 0)
        self.assertEqual(host.lake_calls, [["Pkg.Top"]])
        self.assertEqual(len(host.acquires), 1)
        self.assertEqual(host.releases, 1)
        self.assertNotIn("walk", host.summary())
        self.assertEqual(host.summary()["target_verdicts"], {"Pkg.Top": "built"})

    def test_a_refused_whole_closure_is_walked_imports_first_then_the_targets(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2)
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 0)
        units = host.lake_calls[:-1]
        self.assertEqual(host.lake_calls[-1], ["Pkg.Top"])
        self.assertTrue(all(len(unit) == 1 for unit in units))
        order = [unit[0] for unit in units]
        self.assertEqual(sorted(order), sorted(DIAMOND))
        for module, imports in DIAMOND.items():
            for imported in imports:
                self.assertLess(order.index(imported), order.index(module))
        # Each unit was sized alone, held alone, and released before the next.
        self.assertEqual([call["memory_gib"] for call in host.acquires], [8, 2, 2, 2, 2])
        self.assertEqual(host.releases, 4)
        # Every unit and the final targets build left its own ledger row and log.
        builds = [row for row in host.rows if not row.get("probe")]
        self.assertEqual([row["targets"] for row in builds], host.lake_calls)
        self.assertEqual(len({row["log_path"] for row in builds}), 5)
        self.assertEqual(builds[-1]["admission"], "NOT_REQUIRED_FRESH")
        summary = host.summary()
        self.assertEqual(summary["status"], "OK")
        self.assertEqual(summary["walk"]["units_built"], order)
        self.assertIsNone(summary["walk"]["failed_unit"])
        self.assertEqual(summary["walk"]["remaining"], [])
        self.assertEqual(summary["target_verdicts"], {"Pkg.Top": "built"})
        unit_lines = [line for line in host.output.getvalue().splitlines() if line.startswith("walk ")]
        self.assertEqual(len(unit_lines), 5)

    def test_a_failed_unit_stops_the_walk_and_names_what_remains(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2, fail={"Pkg.Left"})
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 1)
        self.assertEqual(host.lake_calls, [["Pkg.Base"], ["Pkg.Left"]])
        summary = host.summary()
        self.assertEqual(summary["status"], "ERROR")
        self.assertEqual(summary["walk"]["failed_unit"]["module"], "Pkg.Left")
        self.assertEqual(summary["walk"]["units_built"], ["Pkg.Base"])
        self.assertEqual(summary["walk"]["remaining"], ["Pkg.Right", "Pkg.Top"])
        self.assertIsNone(summary["walk"]["targets_unit"])
        self.assertTrue(summary["target_verdicts"]["Pkg.Top"].startswith("not built"))
        self.assertEqual(host.releases, 2)

    def test_wait_applies_to_each_unit_not_to_the_whole_closure_probe(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2, refusal="NEVER_FITS")
        self.assertEqual(host.run(["Pkg.Top"], walk=True, wait_seconds=30), 0)
        self.assertIsNone(host.acquires[0]["wait_seconds"])
        self.assertEqual([call["wait_seconds"] for call in host.acquires[1:]], [30] * 4)

    def test_a_unit_refusal_stops_the_walk_with_the_refusal_exit(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2, unit_refusal="LIGHT_ONLY")
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 2)
        self.assertEqual(host.lake_calls, [])
        summary = host.summary()
        self.assertEqual(summary["status"], "REFUSED")
        self.assertEqual(summary["admission"], "LIGHT_ONLY")
        self.assertEqual(summary["walk"]["failed_unit"]["module"], "Pkg.Base")
        self.assertEqual(summary["walk"]["remaining"], ["Pkg.Left", "Pkg.Right", "Pkg.Top"])

    def test_a_refusal_a_smaller_unit_would_meet_too_is_not_walked(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2, refusal="DEFER_HEAVY")
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 2)
        self.assertEqual(host.lake_calls, [])
        self.assertEqual(len(host.acquires), 1)
        self.assertEqual(host.summary()["status"], "REFUSED")

    def test_a_non_walked_refusal_still_waits_for_the_whole_closure(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2, refusal="DEFER_HEAVY")
        self.assertEqual(host.run(["Pkg.Top"], walk=True, wait_seconds=30), 2)
        self.assertEqual([call["wait_seconds"] for call in host.acquires], [None, 30])

    def test_without_walk_a_refused_closure_is_refused_as_before(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2)
        self.assertEqual(host.run(["Pkg.Top"]), 2)
        self.assertEqual(host.lake_calls, [])
        self.assertEqual(host.summary()["status"], "REFUSED")

    def test_walk_refuses_a_stated_class_or_estimate(self) -> None:
        host = _FakeHost(DIAMOND)
        self.assertEqual(host.run(["Pkg.Top"], walk=True, memory_gib=4), 2)
        self.assertEqual(host.run(["Pkg.Top"], walk=True, probe=True), 2)
        self.assertEqual(host.lake_calls, [])
        self.assertEqual(host.acquires, [])


class WalkOrderTest(unittest.TestCase):
    def test_a_diamond_orders_every_import_before_its_importer(self) -> None:
        self.assertEqual(
            owned.walk_order(DIAMOND, DIAMOND),
            ["Pkg.Base", "Pkg.Left", "Pkg.Right", "Pkg.Top"],
        )

    def test_the_order_is_restricted_to_the_stale_set(self) -> None:
        self.assertEqual(
            owned.walk_order(["Pkg.Top", "Pkg.Left"], DIAMOND), ["Pkg.Left", "Pkg.Top"],
        )

    def test_a_cycle_or_missing_graph_has_no_order(self) -> None:
        cycle = {"A": {"B"}, "B": {"A"}}
        self.assertIsNone(owned.walk_order(["A", "B"], cycle))
        self.assertIsNone(owned.walk_order(["A"], None))


class WalkCliTest(unittest.TestCase):
    def test_the_cli_forwards_walk(self) -> None:
        with patch("creme.cli.run_lake_build", return_value=0) as run:
            cmd_lake_build(SimpleNamespace(goal="g", build_args=["--walk", "--wait", "5", "--", "T"]))
        self.assertTrue(run.call_args.kwargs["walk"])
        self.assertEqual(run.call_args.kwargs["wait_seconds"], 5)


if __name__ == "__main__":
    unittest.main()
