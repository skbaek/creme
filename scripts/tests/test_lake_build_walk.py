"""Behaviour of `lake-build --walk` with Lake, the probe, and the semaphore faked."""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock, patch

from creme import build_ownership as owned
from creme.cli import cmd_lake_build
from creme import semaphore
from creme.profile import ADMISSION_DEFAULTS, admission_settings

try:
    from test_admission_accuracy import _isolated, _row, pin_ledger_clock
    from test_semaphore import ProcessAdapter
except ImportError:  # invoked as scripts.tests.test_lake_build_walk
    from scripts.tests.test_admission_accuracy import _isolated, _row, pin_ledger_clock
    from scripts.tests.test_semaphore import ProcessAdapter


# Top imports Left and Right; both import Base.
DIAMOND = {
    "Pkg.Top": {"Pkg.Left", "Pkg.Right"},
    "Pkg.Left": {"Pkg.Base"},
    "Pkg.Right": {"Pkg.Base"},
    "Pkg.Base": set(),
}


def _wide(leaves: int, mid: int = 0) -> dict:
    """`leaves` independent modules under `mid` importers, all under one Top."""
    graph: dict = {f"Pkg.Leaf{index:02d}": set() for index in range(leaves)}
    layer = [f"Pkg.Leaf{index:02d}" for index in range(leaves)]
    if mid:
        for index in range(mid):
            graph[f"Pkg.Mid{index:02d}"] = {layer[index]}
        layer = [f"Pkg.Mid{index:02d}" for index in range(mid)] + layer[mid:]
    graph["Pkg.Top"] = set(layer)
    return graph


real_walk_isolated_modules = owned.walk_isolated_modules


def owned_settings() -> dict:
    return dict(ADMISSION_DEFAULTS)


# The profile the fake host runs under: a 4-thread ceiling and batches of 4 x 2.
FAKE_SETTINGS = {"max_build_threads": 4, "walk_batch_factor": 2}


class _FakeHost:
    """A package whose modules are stale until a fake Lake build builds them.

    The fake semaphore admits any estimate up to `limit_gib` and refuses a
    larger one with `refusal`; the fake estimator charges 2 GiB per stale
    module, so the whole closure and a single module price differently.
    """

    def __init__(self, graph, *, limit_gib=2, refusal="LIGHT_ONLY", fail=(), unit_refusal=None,
                 wait_timeout=False, headroom=None, per_thread_gib=None, evidence=None):
        self.graph = graph
        self.headroom = headroom            # what admission would grant now (None: unknown)
        self.per_thread_gib = per_thread_gib  # price = 1 GiB + this per concurrent thread
        self.evidence = evidence or {}      # module -> {"isolated"/"unproven"}
        self.thread_env: list[str] = []
        self.retract_batches = False        # the watchdog retracts any unit of two or more modules
        self.retract_now = False
        self.built: set[str] = set()
        self.limit_gib = limit_gib
        self.refusal = refusal
        self.fail = set(fail)
        self.unit_refusal = unit_refusal
        self.wait_timeout = wait_timeout   # a too-large need that is waited for times out
        self.priced: list[list[str]] = []  # the stale set each estimate was sized on
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
        threads = args[9]
        self.priced.append(list(stale["stale_set"]))
        if self.per_thread_gib is not None:
            # The estimator's shape: overhead plus the peaks that can run at once.
            need = 1.0 + self.per_thread_gib * min(threads, len(stale["stale_set"]))
            return math.ceil(need), {
                "source": "fixture thread-priced estimate", "kind": "measured",
                "stale_modules": len(stale["stale_set"]), "need_gib": need,
                "unproven": any(
                    self.evidence.get(module) == "unproven" for module in stale["stale_set"]
                ),
            }
        return max(1, 2 * len(stale["stale_set"])), {
            "source": "fixture estimate", "kind": "measured",
            "stale_modules": len(stale["stale_set"]),
        }

    def acquire(self, _goal, _note, _lease, **kwargs):
        self.acquires.append(kwargs)
        if self.unit_refusal is not None and kwargs["memory_gib"] <= self.limit_gib:
            return False, f"{self.unit_refusal} — fixture unit refusal"
        if kwargs["memory_gib"] > self.limit_gib and self.wait_timeout:
            if kwargs.get("wait_seconds") is not None:
                return False, "WAIT_TIMEOUT — fixture: no admission within the wait"
            return False, "DEFER_FOR_HARD — fixture: another hold, for now"
        if kwargs["memory_gib"] > self.limit_gib:
            return False, f"{self.refusal} — fixture: {kwargs['memory_gib']} GiB does not fit"
        return True, "ADMITTED_SOFT — fixture"

    def isolated(self, *args, **kwargs):
        if not any(kind == "isolated" for kind in self.evidence.values()):
            return real_walk_isolated_modules(*args, **kwargs)
        return {module for module, kind in self.evidence.items() if kind == "isolated"}

    def release(self, _goal):
        self.releases += 1
        return True, "released"

    def popen(self, args, **kwargs):
        targets = args[args.index("--verbose") + 1:]
        self.lake_calls.append(list(targets))
        self.thread_env.append(kwargs["env"]["LEAN_NUM_THREADS"])
        host = self

        class FakeProc:
            pid = 4321
            stdout = iter([])

            def wait(self, timeout=None):
                if host.retract_batches and len(targets) > 1 and targets != ["Pkg.Top"]:
                    host.retract_now = True
                    return 1
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
                ("load_admission_settings", Mock(return_value={
                    **owned_settings(), **FAKE_SETTINGS,
                })),
                ("semaphore.largest_fitting_need", Mock(side_effect=lambda *_a, **_k: self.headroom)),
                ("walk_isolated_modules", Mock(side_effect=self.isolated)),
            ]:
                stack.enter_context(patch(f"creme.build_ownership.{target}", value))
            return owned.run_lake_build("g", targets, stdout=self.output, **kwargs)

    def watchdog_class(self):
        """A watchdog that never sees pressure: the real one samples the real host."""

        host = self

        class QuietWatchdog:
            def __init__(self, *_args, **_kwargs):
                self.retracted = False
                self.cleanup_proved = True
                self.min_available_gib = None
                self.events: list[str] = []

            def start(self):
                pass

            def stop(self):
                self.retracted = host.retract_now
                host.retract_now = False

        return QuietWatchdog

    def summary(self) -> dict:
        lines = [line for line in self.output.getvalue().splitlines() if line.startswith("{")]
        return json.loads(lines[-1])


def _walk_lines(host) -> list[str]:
    return [line for line in host.output.getvalue().splitlines() if line.startswith("walk ")]


class WalkBehaviourTest(unittest.TestCase):
    def test_an_admitted_whole_closure_is_one_ordinary_build(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=64)
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 0)
        self.assertEqual(host.lake_calls, [["Pkg.Top"]])
        self.assertEqual(len(host.acquires), 1)
        self.assertEqual(host.releases, 1)
        self.assertNotIn("walk", host.summary())
        self.assertEqual(host.summary()["target_verdicts"], {"Pkg.Top": "built"})

    def test_a_refused_whole_closure_is_walked_by_height_then_the_targets(self) -> None:
        # 2 GiB per stale module and a 4 GiB limit: the whole closure (8) is
        # refused; Left and Right (one height, 4) fit together.
        host = _FakeHost(DIAMOND, limit_gib=4)
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 0)
        self.assertEqual(
            host.lake_calls,
            [["Pkg.Base"], ["Pkg.Left", "Pkg.Right"], ["Pkg.Top"], ["Pkg.Top"]],
        )
        summary = host.summary()
        self.assertEqual(summary["walk"]["waves"], [1, 2, 1])
        self.assertEqual(summary["walk"]["units_planned"], 3)
        order = summary["walk"]["order"]
        self.assertEqual(summary["walk"]["units_built"], order)
        for module, imports in DIAMOND.items():
            for imported in imports:
                self.assertLess(order.index(imported), order.index(module))
        # Each unit was sized on its own stale set, held alone, released before the next.
        self.assertEqual([call["memory_gib"] for call in host.acquires], [8, 2, 4, 2])
        self.assertEqual(host.releases, 3)
        builds = [row for row in host.rows if not row.get("probe")]
        self.assertEqual([row["targets"] for row in builds], host.lake_calls)
        self.assertEqual(len({row["log_path"] for row in builds}), 4)
        self.assertEqual(builds[-1]["admission"], "NOT_REQUIRED_FRESH")
        self.assertIsNone(summary["walk"]["failed_unit"])
        self.assertEqual(summary["walk"]["remaining"], [])
        self.assertEqual(summary["target_verdicts"], {"Pkg.Top": "built"})
        self.assertEqual(len(_walk_lines(host)), 4)

    def test_a_batch_is_priced_by_the_estimator_on_exactly_its_own_stale_set(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=4)
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 0)
        summary = host.summary()
        self.assertEqual(len(host.priced[0]), 4)
        self.assertEqual(
            [sorted(stale) for stale in host.priced[1:4]],
            [["Pkg.Base"], ["Pkg.Left", "Pkg.Right"], ["Pkg.Top"]],
        )
        self.assertEqual(
            [unit["priced_stale_modules"] for unit in summary["walk"]["units"]], [1, 2, 1],
        )
        self.assertEqual(summary["walk"]["targets_unit"]["priced_stale_modules"], 0)

    def test_a_batch_the_host_will_not_admit_is_halved_down_to_single_modules(self) -> None:
        wide = _wide(6)
        host = _FakeHost(wide, limit_gib=4)   # 2 GiB per module: at most 2 fit together
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=4), 0)
        leaves = [name for name in sorted(wide) if name.startswith("Pkg.Leaf")]
        # cap 4 x 2 = 8 >= 6: one batch of 6, refused (12), halves of 3 (6), refused, then 1+2 and 1+2.
        built = [call for call in host.lake_calls if call != ["Pkg.Top"]]
        self.assertEqual(sorted(sum(built, [])), leaves)
        self.assertTrue(all(len(call) <= 2 for call in built))
        refused = [unit for unit in host.summary()["walk"]["units"] if unit.get("split")]
        self.assertEqual([len(unit["modules"]) for unit in refused], [6, 3, 3])
        self.assertEqual(host.summary()["status"], "OK")
        self.assertIn("splitting into 3 + 3 module(s)", host.output.getvalue())

    def test_a_split_ends_at_a_single_module_that_behaves_as_before(self) -> None:
        host = _FakeHost(_wide(3), limit_gib=2, unit_refusal="LIGHT_ONLY")
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=4), 2)
        self.assertEqual(host.lake_calls, [])
        summary = host.summary()
        self.assertEqual(summary["status"], "REFUSED")
        self.assertEqual(summary["admission"], "LIGHT_ONLY")
        # 3 -> 1 + 2 -> the single refused stops the walk before anything else runs.
        self.assertEqual(summary["walk"]["failed_unit"]["modules"], ["Pkg.Leaf00"])
        self.assertEqual(
            summary["walk"]["remaining"], ["Pkg.Leaf01", "Pkg.Leaf02", "Pkg.Top"],
        )

    def test_batches_are_capped_at_the_thread_ceiling_times_the_factor(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=64)
        # Force the whole closure to be refused so it is walked: 2 GiB x 11 = 22 > 8.
        host.limit_gib = 8
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=2), 0)
        summary = host.summary()
        self.assertEqual(summary["walk"]["batch_cap"], 4)          # 2 threads x factor 2
        self.assertEqual(summary["walk"]["waves"], [10, 1])
        sizes = [len(call) for call in host.lake_calls[:-2]]
        self.assertEqual(sizes, [4, 3, 3])                          # even split of 10, not 4+4+2
        self.assertEqual(host.lake_calls[-2], ["Pkg.Top"])

    def test_plan_walk_units_cuts_waves_evenly_and_isolates_first(self) -> None:
        waves = [["a", "b", "c", "d", "e"], ["f"]]
        self.assertEqual(
            owned.plan_walk_units(waves, ["c"], 3),
            [["c"], ["a", "b"], ["d", "e"], ["f"]],
        )
        self.assertEqual(owned.plan_walk_units(waves, [], 1), [[n] for n in "abcdef"])
        self.assertEqual(owned.plan_walk_units(waves, [], 99), [list("abcde"), ["f"]])
        self.assertEqual(owned.plan_walk_units([], [], 4), [])

    def test_a_module_with_only_a_failed_attempt_floor_is_built_alone(self) -> None:
        wide = _wide(5)
        host = _FakeHost(wide, limit_gib=8, evidence={"Pkg.Leaf02": "isolated"})
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=4), 0)
        built = [call for call in host.lake_calls if call != ["Pkg.Top"]]
        self.assertEqual(built[0], ["Pkg.Leaf02"])
        self.assertEqual(built[1:], [["Pkg.Leaf00", "Pkg.Leaf01", "Pkg.Leaf03", "Pkg.Leaf04"]])
        self.assertEqual(host.summary()["walk"]["isolated"], ["Pkg.Leaf02"])
        # The isolated unit is priced on its own module; the batch on the other four.
        self.assertEqual(
            sorted(len(stale) for stale in host.priced[1:3]), [1, 4],
        )

    def test_a_retracted_batch_is_split_and_the_halves_are_retried(self) -> None:
        host = _FakeHost(_wide(4), limit_gib=8)
        host.retract_batches = True
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=4), 0)
        units = host.summary()["walk"]["units"]
        retracted = [unit for unit in units if unit.get("retracted")]
        self.assertEqual([len(unit["modules"]) for unit in retracted], [4, 2, 2])
        self.assertTrue(all(unit.get("split") for unit in retracted))
        built = [call for call in host.lake_calls if len(call) == 1 and call != ["Pkg.Top"]]
        self.assertEqual(sorted(sum(built, [])), [f"Pkg.Leaf{index:02d}" for index in range(4)])
        self.assertEqual(host.summary()["status"], "OK")

    def test_a_timed_out_whole_closure_wait_falls_back_to_the_walk(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=4, wait_timeout=True)
        self.assertEqual(host.run(["Pkg.Top"], walk=True, wait_seconds=30), 0)
        self.assertEqual(
            [call["wait_seconds"] for call in host.acquires[:2]], [None, 30],
        )
        self.assertEqual(
            host.lake_calls,
            [["Pkg.Base"], ["Pkg.Left", "Pkg.Right"], ["Pkg.Top"], ["Pkg.Top"]],
        )
        self.assertEqual(host.summary()["walk"]["whole_closure_admission"].split(" — ")[0], "WAIT_TIMEOUT")

    def test_a_failed_batch_stops_the_walk_and_names_what_remains(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=4, fail={"Pkg.Left"})
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 1)
        self.assertEqual(host.lake_calls, [["Pkg.Base"], ["Pkg.Left", "Pkg.Right"]])
        summary = host.summary()
        self.assertEqual(summary["status"], "ERROR")
        self.assertEqual(summary["walk"]["failed_unit"]["modules"], ["Pkg.Left", "Pkg.Right"])
        self.assertEqual(summary["walk"]["units_built"], ["Pkg.Base"])
        self.assertEqual(summary["walk"]["remaining"], ["Pkg.Top"])
        self.assertIsNone(summary["walk"]["targets_unit"])
        self.assertTrue(summary["target_verdicts"]["Pkg.Top"].startswith("not built"))
        self.assertEqual(host.releases, 2)

    def test_a_failed_single_module_stops_the_walk_as_before(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2, fail={"Pkg.Left"})
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 1)
        self.assertEqual(host.lake_calls, [["Pkg.Base"], ["Pkg.Left"]])
        summary = host.summary()
        self.assertEqual(summary["walk"]["failed_unit"]["module"], "Pkg.Left")
        self.assertEqual(summary["walk"]["remaining"], ["Pkg.Right", "Pkg.Top"])

    def test_wait_applies_to_single_modules_and_a_batch_is_first_asked_without_waiting(self) -> None:
        host = _FakeHost(DIAMOND, limit_gib=2, refusal="NEVER_FITS")
        self.assertEqual(host.run(["Pkg.Top"], walk=True, wait_seconds=30), 0)
        self.assertIsNone(host.acquires[0]["wait_seconds"])
        # Base; Left+Right refused without a wait, then Left and Right waited for; Top.
        self.assertEqual(
            [call["wait_seconds"] for call in host.acquires[1:]], [30, None, 30, 30, 30],
        )

    def test_a_batch_refused_over_other_sessions_holds_is_waited_for_whole(self) -> None:
        host = _FakeHost(_wide(3), limit_gib=64)
        real = host.acquire
        calls = []

        def acquire(goal, note, lease, **kwargs):
            calls.append(kwargs["wait_seconds"])
            if kwargs["wait_seconds"] is None and len(calls) > 1 and not host.lake_calls:
                return False, "DEFER_FOR_HARD — fixture: another session's hold"
            return real(goal, note, lease, **kwargs)

        host.acquire = acquire
        host.limit_gib = 6      # the 3-module batch (6 GiB) fits, the whole closure (8) does not
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=4, wait_seconds=30), 0)
        self.assertEqual(calls[:3], [None, None, 30])
        self.assertEqual(host.lake_calls[0], ["Pkg.Leaf00", "Pkg.Leaf01", "Pkg.Leaf02"])

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

    def test_the_targets_unit_is_last(self) -> None:
        host = _FakeHost(_wide(4, mid=2), limit_gib=8)
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=2), 0)
        self.assertEqual(host.lake_calls[-1], ["Pkg.Top"])
        self.assertEqual(host.lake_calls[-2], ["Pkg.Top"])
        self.assertEqual(host.summary()["walk"]["waves"], [4, 2, 1])
        # every Mid module comes after the Leaf it imports
        flat = sum(host.lake_calls[:-2], [])
        self.assertLess(flat.index("Pkg.Leaf00"), flat.index("Pkg.Mid00"))
        self.assertLess(flat.index("Pkg.Leaf01"), flat.index("Pkg.Mid01"))


class AdaptiveThreadsTest(unittest.TestCase):
    def test_a_build_that_fits_wide_takes_the_ceiling(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=64, headroom=20.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=None), 0)
        self.assertEqual(host.thread_env, ["4"])           # ceiling 4 (FAKE_SETTINGS), 9 GiB fits
        self.assertEqual(host.rows[-1]["threads"], 4)
        self.assertTrue(host.rows[-1]["threads_source"].startswith("adaptive: 4 threads"))
        self.assertEqual(host.summary()["threads"], 4)

    def test_tight_memory_takes_the_widest_that_fits(self) -> None:
        # price(t) = 1 + 2t: 3 -> 7 GiB fits 7.5, 4 -> 9 does not.
        host = _FakeHost(_wide(10), limit_gib=64, headroom=7.5, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=None), 0)
        self.assertEqual(host.thread_env, ["3"])

    def test_memory_that_fits_only_the_floor_keeps_two_threads(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=64, headroom=5.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=None), 0)
        self.assertEqual(host.thread_env, ["2"])
        # Even a headroom below the two-thread price never drops under two.
        host = _FakeHost(_wide(10), limit_gib=64, headroom=1.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=None), 0)
        self.assertEqual(host.thread_env, ["2"])

    def test_unknown_headroom_or_an_unproven_price_keeps_two_threads(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=64, headroom=None, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=None), 0)
        self.assertEqual(host.thread_env, ["2"])
        host = _FakeHost(
            _wide(10), limit_gib=64, headroom=99.0, per_thread_gib=2.0,
            evidence={"Pkg.Leaf03": "unproven"},
        )
        self.assertEqual(host.run(["Pkg.Top"], threads=None), 0)
        self.assertEqual(host.thread_env, ["2"])
        self.assertIn("unproven", host.rows[-1]["threads_source"])

    def test_an_explicit_value_wins_even_when_more_would_fit(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=64, headroom=99.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=1), 0)
        self.assertEqual(host.thread_env, ["1"])
        self.assertEqual(host.rows[-1]["threads_source"], "explicit")
        host = _FakeHost(_wide(10), limit_gib=64, headroom=99.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=3), 0)
        self.assertEqual(host.thread_env, ["3"])

    def test_the_direct_call_default_is_unchanged_two_threads(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=64, headroom=99.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"]), 0)
        self.assertEqual(host.thread_env, ["2"])

    def test_no_more_threads_than_stale_modules(self) -> None:
        graph = {"Pkg.A": set(), "Pkg.B": set()}
        host = _FakeHost(graph, limit_gib=64, headroom=99.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.A", "Pkg.B"], threads=None), 0)
        self.assertEqual(host.thread_env, ["2"])

    def test_the_priced_need_charges_the_chosen_threads(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=64, headroom=99.0, per_thread_gib=2.0)
        self.assertEqual(host.run(["Pkg.Top"], threads=None), 0)
        # 1 GiB + 4 concurrent peaks of 2 GiB: what admission was asked for.
        self.assertEqual(host.acquires[0]["need_gib"], 9.0)

    def test_a_walk_lets_each_unit_choose_its_own_threads(self) -> None:
        host = _FakeHost(_wide(10), limit_gib=8, headroom=99.0, per_thread_gib=2.0)
        host.limit_gib = 3   # the whole closure (9 GiB at 4 threads) is refused, units of 1-2 modules fit
        self.assertEqual(host.run(["Pkg.Top"], walk=True, threads=None), 0)
        summary = host.summary()
        self.assertEqual(summary["walk"]["batch_cap"], 8)          # ceiling 4 x factor 2
        self.assertTrue(all(unit["threads"] == 2 for unit in summary["walk"]["units"] if unit["exit"] == 0))


class ChooseThreadsTest(unittest.TestCase):
    @staticmethod
    def price(per_thread=2.0):
        return lambda count: (1.0 + per_thread * count, False)

    def test_bisection_finds_the_widest_fitting_count(self) -> None:
        for headroom, expected in [(5.0, 2), (7.0, 3), (9.0, 4), (11.0, 5), (13.0, 6), (99.0, 8)]:
            chosen, _note = owned.choose_build_threads(self.price(), headroom, 8, 400)
            self.assertEqual(chosen, expected, headroom)

    def test_the_answer_was_priced_and_fit(self) -> None:
        asked = []

        def price(count):
            asked.append(count)
            return 1.0 + 2.0 * count, False

        chosen, _note = owned.choose_build_threads(price, 12.0, 8, 400)
        self.assertEqual(chosen, 5)
        self.assertIn(5, asked)
        self.assertLessEqual(len(asked), 4)

    def test_it_never_goes_below_two_or_past_the_ceiling_or_the_module_count(self) -> None:
        self.assertEqual(owned.choose_build_threads(self.price(), 0.0, 8, 400)[0], 2)
        self.assertEqual(owned.choose_build_threads(self.price(), 999.0, 3, 400)[0], 3)
        self.assertEqual(owned.choose_build_threads(self.price(), 999.0, 8, 3)[0], 3)
        self.assertEqual(owned.choose_build_threads(self.price(), 999.0, 8, 1)[0], 2)
        self.assertEqual(owned.choose_build_threads(self.price(), 999.0, 1, 400)[0], 2)

    def test_resolve_max_threads(self) -> None:
        self.assertEqual(owned.resolve_max_threads({"max_build_threads": 0}, 10), 5)
        self.assertEqual(owned.resolve_max_threads({}, 4), 2)
        self.assertEqual(owned.resolve_max_threads({"max_build_threads": 6}, 10), 6)
        self.assertEqual(owned.resolve_max_threads({"max_build_threads": 1}, 10), 2)
        self.assertEqual(owned.resolve_max_threads({}, None), 2)


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


class WalkWavesTest(unittest.TestCase):
    def test_heights_group_independent_modules(self) -> None:
        self.assertEqual(
            owned.walk_heights(DIAMOND, DIAMOND),
            {"Pkg.Base": 0, "Pkg.Left": 1, "Pkg.Right": 1, "Pkg.Top": 2},
        )
        self.assertEqual(
            owned.walk_waves(DIAMOND, DIAMOND),
            [["Pkg.Base"], ["Pkg.Left", "Pkg.Right"], ["Pkg.Top"]],
        )

    def test_a_wave_never_contains_two_modules_where_one_imports_the_other(self) -> None:
        graph = _wide(6, mid=3)
        for wave in owned.walk_waves(graph, graph):
            for module in wave:
                self.assertFalse(graph[module] & set(wave))

    def test_waves_are_restricted_to_the_stale_set_and_flatten_to_the_order(self) -> None:
        stale = ["Pkg.Top", "Pkg.Left", "Pkg.Right"]
        waves = owned.walk_waves(stale, DIAMOND)
        self.assertEqual(waves, [["Pkg.Left", "Pkg.Right"], ["Pkg.Top"]])
        self.assertEqual(sum(waves, []), owned.walk_order(stale, DIAMOND))

    def test_a_cycle_or_missing_graph_has_no_waves(self) -> None:
        self.assertIsNone(owned.walk_waves(["A", "B"], {"A": {"B"}, "B": {"A"}}))
        self.assertIsNone(owned.walk_waves(["A"], None))
        self.assertEqual(owned.walk_waves([], DIAMOND), [])


class ThreadPricingTest(unittest.TestCase):
    """The estimator prices a wider build for the concurrency its threads allow."""

    GRAPH = {f"M{index}": set() for index in range(8)}
    ROWS = [
        _row("2026-09-04T00:00:00Z", [f"M{index}"], 1.6 + index * 0.1, lean_gib=1.0 + index * 0.1,
             module_peaks={f"M{index}": 1.0 + index * 0.1})
        for index in range(8)
    ]

    def need(self, threads, names=None):
        names = names or sorted(self.GRAPH)
        return owned.size_stale_set(
            names, self.GRAPH, self.ROWS, ADMISSION_DEFAULTS, 8, threads=threads,
        )

    def test_a_build_at_two_threads_or_fewer_is_priced_exactly_as_before(self) -> None:
        before = owned.size_stale_set(sorted(self.GRAPH), self.GRAPH, self.ROWS, ADMISSION_DEFAULTS, 8)
        for threads in (None, 1, 2):
            self.assertEqual(self.need(threads)["need_gib"], before["need_gib"])

    def test_more_threads_sum_more_of_the_largest_peaks_and_never_less(self) -> None:
        needs = [self.need(threads)["need_gib"] for threads in (2, 3, 4, 8)]
        self.assertEqual(needs, sorted(needs))
        peaks = sorted((1.0 + index * 0.1 for index in range(8)), reverse=True)
        overhead = self.need(2)["overhead_gib"]
        self.assertAlmostEqual(needs[2], overhead + sum(peaks[:4]), places=1)
        self.assertGreater(needs[2], needs[0])
        self.assertEqual(self.need(4)["width"], 4)

    def test_a_batch_never_prices_more_concurrent_peaks_than_it_has_modules(self) -> None:
        self.assertEqual(self.need(8, ["M1", "M2"])["width"], 2)

    def test_importing_modules_never_overlap_whatever_the_threads(self) -> None:
        chain = {"A": set(), "B": {"A"}, "C": {"B"}}
        rows = [_row("2026-09-04T00:00:00Z", [name], 2.0, lean_gib=1.4,
                     module_peaks={name: 1.4}) for name in chain]
        sizing = owned.size_stale_set(sorted(chain), chain, rows, ADMISSION_DEFAULTS, 8, threads=8)
        self.assertEqual(sizing["width"], 1)


class WalkIsolationTest(unittest.TestCase):
    """Which stale modules a walk builds alone."""

    def setUp(self) -> None:
        pin_ledger_clock(self)

    def failed(self, module, *, own=None, outcome=None, others=()):
        row = _row("2026-09-20T00:00:00Z", ["B"], 9.0, exit_code=1)
        row["modules_failed"] = [module, *others]
        if own is not None:
            row["module_peak_mib"] = {module: own * 1024.0}
        if outcome:
            row["outcome"] = outcome
        return row

    def isolated(self, rows, names, measured=()):
        with _isolated() as root:
            ledger = [*rows, *(
                _row("2026-09-21T00:00:00Z", [name], 2.1, lean_gib=1.5, module_peaks={name: 1.5})
                for name in measured
            )]
            (root / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in ledger), encoding="utf-8",
            )
            return owned.walk_isolated_modules(Path("/w"), names, ("tc", "mf"), ADMISSION_DEFAULTS)

    def test_a_module_with_a_failed_attempt_floor_is_isolated(self) -> None:
        self.assertEqual(self.isolated([self.failed("A")], ["A", "B", "C"]), {"A"})
        self.assertEqual(self.isolated([self.failed("A", own=5.0, others=("B",))], ["A", "B"]), {"A"})

    def test_a_failed_module_whose_row_cannot_attribute_a_peak_floors_nothing(self) -> None:
        self.assertEqual(self.isolated([self.failed("A", others=("B",))], ["A", "B"]), set())

    def test_a_measured_module_is_not_isolated_by_an_old_failure(self) -> None:
        self.assertEqual(self.isolated([self.failed("A")], ["A", "B"], measured=["A"]), set())

    def test_every_module_of_a_retracted_attempt_is_isolated(self) -> None:
        rows = [self.failed("A", outcome="retracted", others=("B",))]
        self.assertEqual(self.isolated(rows, ["A", "B", "C"]), {"A", "B"})
        # A retraction whose unfinished modules are not all stale floors nothing here.
        self.assertEqual(self.isolated(rows, ["A", "C"]), set())

    def test_no_failed_rows_isolates_nothing(self) -> None:
        self.assertEqual(self.isolated([], ["A", "B"]), set())


class HeadroomAndSettingsTest(unittest.TestCase):
    POLICY = {
        "task_memory_gib": 2, "heavy_workers": 2, "light_workers": 4,
        "physical_memory_gib": 24.0, "profile_status": "VALID",
    }

    def test_largest_fitting_need_is_available_less_floor_and_other_admitted_needs(self) -> None:
        adapter = ProcessAdapter(free_percent=75, total_gib=24)      # 18 GiB available
        with _isolated():
            free = semaphore.largest_fitting_need("mine", adapter, self.POLICY)
            self.assertEqual(free, 18.0 - semaphore.ADMISSION_FLOOR_GIB)
            ok, detail = semaphore.adaptive_acquire(
                "other", "other build", memory_gib=4, need_gib=4.0,
                adapter=adapter, policy=self.POLICY,
            )
            self.assertTrue(ok, detail)
            self.assertEqual(
                semaphore.largest_fitting_need("mine", adapter, self.POLICY),
                18.0 - semaphore.ADMISSION_FLOOR_GIB - 4.0,
            )
            # Its own hold is not charged against it.
            self.assertEqual(
                semaphore.largest_fitting_need("other", adapter, self.POLICY),
                18.0 - semaphore.ADMISSION_FLOOR_GIB,
            )

    def test_largest_fitting_need_says_nothing_when_the_host_cannot(self) -> None:
        with _isolated():
            blind = ProcessAdapter(free_percent=None, total_gib=24)
            self.assertIsNone(semaphore.largest_fitting_need("mine", blind, self.POLICY))
            with patch.object(semaphore, "_pressure_cause", return_value="swap pressure"):
                adapter = ProcessAdapter(free_percent=75, total_gib=24)
                self.assertIsNone(semaphore.largest_fitting_need("mine", adapter, self.POLICY))
            broken = Mock()
            broken.memory_headroom.side_effect = OSError("no sysctl")
            self.assertIsNone(semaphore.largest_fitting_need("mine", broken, self.POLICY))

    def test_the_new_tunables_default_conservatively_and_stay_in_range(self) -> None:
        self.assertEqual(ADMISSION_DEFAULTS["max_build_threads"], 0)
        self.assertEqual(ADMISSION_DEFAULTS["walk_batch_factor"], 4)
        merged = admission_settings({"admission": {"max_build_threads": 6, "walk_batch_factor": 2}})
        self.assertEqual((merged["max_build_threads"], merged["walk_batch_factor"]), (6, 2))
        bad = admission_settings({"admission": {"max_build_threads": 65, "walk_batch_factor": 0}})
        self.assertEqual((bad["max_build_threads"], bad["walk_batch_factor"]), (0, 4))


class EstimatorEvidenceCostTest(unittest.TestCase):
    """The estimator's evidence scan is cheap enough to price a build at several thread counts."""

    def setUp(self) -> None:
        pin_ledger_clock(self)

    def row(self, minute: int, module: str = "A", restored=("R1", "R2")) -> dict:
        row = _row(f"2026-09-29T00:{minute:02d}:00Z", [module], 2.0, lean_gib=1.5,
                   module_peaks={module: 1.5})
        row["modules_restored"] = list(restored)
        return row

    @staticmethod
    def write(root: Path, rows, mode="w") -> None:
        with (root / "ledger.jsonl").open(mode, encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row) + "\n" for row in rows))

    def test_the_recent_reader_matches_the_full_reader_and_drops_the_restored_list(self) -> None:
        with _isolated() as root:
            self.write(root, [self.row(0), self.row(1, "B")])
            full, _ = owned.read_ledger("30d")
            recent, corrupt = owned.read_recent_ledger(30)
        self.assertEqual(corrupt, 0)
        self.assertEqual([row["modules_rebuilt"] for row in recent], [["A"], ["B"]])
        self.assertEqual(
            [{**row, "modules_restored": []} for row in full], recent,
        )
        self.assertEqual(recent[0]["modules_restored"], [])

    def test_a_second_read_parses_only_what_was_appended(self) -> None:
        with _isolated() as root:
            self.write(root, [self.row(0), self.row(1, "B")])
            self.assertEqual(len(owned.read_recent_ledger(30)[0]), 2)
            self.write(root, [self.row(2, "C")], mode="a")
            with patch.object(owned, "_valid_ledger_row", wraps=owned._valid_ledger_row) as valid:
                rows, _ = owned.read_recent_ledger(30)
            self.assertEqual(valid.call_count, 1)
            self.assertEqual([row["modules_rebuilt"] for row in rows], [["A"], ["B"], ["C"]])
            with patch.object(owned, "_valid_ledger_row", wraps=owned._valid_ledger_row) as valid:
                owned.read_recent_ledger(30)
            self.assertEqual(valid.call_count, 0)

    def test_a_rewritten_or_replaced_ledger_is_read_afresh(self) -> None:
        with _isolated() as root:
            self.write(root, [self.row(0, "A"), self.row(1, "B")])
            owned.read_recent_ledger(30)
            self.write(root, [self.row(5, "X"), self.row(6, "Y"), self.row(7, "Z")])
            rows, _ = owned.read_recent_ledger(30)
            self.assertEqual([row["modules_rebuilt"] for row in rows], [["X"], ["Y"], ["Z"]])
            (root / "ledger.jsonl").unlink()
            self.assertEqual(owned.read_recent_ledger(30), ([], 0))
        with _isolated() as root:
            self.write(root, [self.row(9, "Q")])
            self.assertEqual(
                [row["modules_rebuilt"] for row in owned.read_recent_ledger(30)[0]], [["Q"]],
            )

    def test_a_torn_last_line_waits_for_its_newline_and_a_corrupt_line_is_counted(self) -> None:
        with _isolated() as root:
            text = json.dumps(self.row(0, "A")) + "\n" + "not json\n" + json.dumps(self.row(1, "B"))
            (root / "ledger.jsonl").write_text(text, encoding="utf-8")
            rows, corrupt = owned.read_recent_ledger(30)
            self.assertEqual(([row["modules_rebuilt"] for row in rows], corrupt), ([["A"]], 1))
            with (root / "ledger.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("\n")
            rows, corrupt = owned.read_recent_ledger(30)
            self.assertEqual(([row["modules_rebuilt"] for row in rows], corrupt), ([["A"], ["B"]], 1))

    def test_rows_older_than_the_window_are_not_evidence(self) -> None:
        with _isolated() as root:
            old = _row("2026-01-01T00:00:00Z", ["A"], 2.0, lean_gib=1.5)
            old["modules_restored"] = []
            self.write(root, [old, self.row(0)])
            self.assertEqual(len(owned.read_recent_ledger(30)[0]), 1)
            self.assertEqual(len(owned.read_recent_ledger(400)[0]), 2)

    def test_a_removed_worktrees_repository_is_asked_of_git_once_not_per_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".worktrees").mkdir(parents=True)
            owned._LEGACY_IDENTITY_CACHE.clear()
            with patch.object(owned, "repository_identity", return_value="scope") as asked:
                found = {
                    owned._legacy_repository_identity(str(repo / ".worktrees" / f"goal-{index}"))
                    for index in range(25)
                }
            self.assertEqual(found, {"scope"})
            self.assertEqual(asked.call_count, 1)
            owned._LEGACY_IDENTITY_CACHE.clear()

    def test_unusable_recorded_worktrees_stay_unscoped(self) -> None:
        owned._LEGACY_IDENTITY_CACHE.clear()
        for recorded in (None, "", 7, "/nonexistent/x/y"):
            self.assertIsNone(owned._legacy_repository_identity(recorded))


class WalkCliTest(unittest.TestCase):
    def test_threads_default_to_the_adaptive_choice_and_an_explicit_value_is_forwarded(self) -> None:
        with patch("creme.cli.run_lake_build", return_value=0) as run:
            cmd_lake_build(SimpleNamespace(goal="g", build_args=["--", "T"]))
            self.assertIsNone(run.call_args.kwargs["threads"])
            cmd_lake_build(SimpleNamespace(goal="g", build_args=["--threads", "6", "--", "T"]))
            self.assertEqual(run.call_args.kwargs["threads"], 6)


    def test_the_cli_forwards_walk(self) -> None:
        with patch("creme.cli.run_lake_build", return_value=0) as run:
            cmd_lake_build(SimpleNamespace(goal="g", build_args=["--walk", "--wait", "5", "--", "T"]))
        self.assertTrue(run.call_args.kwargs["walk"])
        self.assertEqual(run.call_args.kwargs["wait_seconds"], 5)


if __name__ == "__main__":
    unittest.main()
