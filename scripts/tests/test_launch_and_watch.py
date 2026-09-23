"""launch-and-watch-v1: the owned-build watchdog, its retraction, and the walk re-queue."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creme import build_ownership as owned
from creme.adapters import get_adapter
from creme.profile import ADMISSION_DEFAULTS

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lake_build_walk import DIAMOND, _FakeHost  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
SETTINGS = dict(ADMISSION_DEFAULTS)


def _sample(available_gib: float, total_gib: float = 24.0, cause=None, *,
            level=None, swap_mib=None, direct=False, psi=None):
    data = {
        "memory_free_percent": int(100 * available_gib / total_gib),
        "memory_available_bytes": int(available_gib * 1024 ** 3),
        "physical_memory_bytes": int(total_gib * 1024 ** 3),
        "memory_pressure_cause": cause,
        "memory_pressure_level": level,
        "swap_used_mib": swap_mib,
    }
    if direct:
        data["memory_available_direct"] = True
        data["memory_psi_full_avg10"] = psi
    return SimpleNamespace(status="OK", detail="fixture", data=data)


def _critical(available_gib: float = 12.0):
    return _sample(available_gib, level=owned.DARWIN_CRITICAL_PRESSURE_LEVEL)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class WatchdogUnitTest(unittest.TestCase):
    """The watchdog's order of answers under an injected host and clock."""

    def setUp(self) -> None:
        # A retraction is logged to the semaphore state: keep it out of the host's.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, samples, *, goal="g", order=None, grace=3.0, step=5.0):
        clock = _Clock()
        calls: list[str] = []
        feed = iter(samples)

        def probe():
            return next(feed)

        def reclaim(label):
            calls.append(f"reclaim {label}")
            return "reclaim OK: 1 worker(s)"

        def terminate(_proc):
            calls.append("terminate")
            return True

        dog = owned.Watchdog(
            goal, SimpleNamespace(pid=0), floor_gib=2.0, probe=probe, reclaim=reclaim,
            order=order or (lambda: [{"label": goal, "unproven": True}]),
            terminate=terminate, grace=grace, step=step, clock=clock,
        )
        return dog, clock, calls

    def run_until(self, dog, clock, seconds):
        """One check per simulated second; returns the second it retracted."""
        for second in range(seconds + 1):
            clock.now = float(second)
            if dog.check():
                return second
        return None

    def test_reclaim_comes_first_and_retraction_only_after_the_grace(self) -> None:
        dog, clock, calls = self.make([_critical()] * 10)
        self.assertEqual(self.run_until(dog, clock, 9), 3)
        self.assertEqual(calls, ["reclaim g", "terminate"])
        self.assertTrue(dog.retracted)
        self.assertIn("retract after 3.0s critical", dog.events[-1])

    def test_drain_level_pressure_reclaims_but_never_retracts(self) -> None:
        # The 2026-09-19 shape: a saturated compressor at a healthy free
        # percentage, held for the whole window.  Builds completed under it.
        for sample in (
            _sample(12.0, cause="compressor occupies 11.5 of 24 GiB physical (48%)"),
            _sample(1.5),                    # Darwin free-% availability below the floor
        ):
            with self.subTest(sample=sample.data):
                dog, clock, calls = self.make([sample] * 30)
                self.assertIsNone(self.run_until(dog, clock, 29))
                self.assertEqual(calls, ["reclaim g"])
                self.assertFalse(dog.retracted)
                self.assertTrue(dog.events[0].startswith("drain: "))

    def test_drain_then_critical_retracts_grace_after_the_critical_signal(self) -> None:
        drain = _sample(12.0, cause="compressor occupies 11.5 of 24 GiB physical (48%)")
        dog, clock, calls = self.make([drain] * 5 + [_critical()] * 10)
        self.assertEqual(self.run_until(dog, clock, 14), 8)
        self.assertEqual(calls, ["reclaim g", "terminate"])

    def test_each_critical_signal_retracts(self) -> None:
        swap = [_sample(12.0, swap_mib=1000.0 + 150.0 * second) for second in range(15)]
        cases = {
            "kernel critical level": [_critical()] * 15,
            "swap growth": swap,
            "direct availability below the floor": [_sample(1.5, direct=True)] * 15,
            "PSI full": [_sample(8.0, direct=True, psi=25.0)] * 15,
        }
        for name, samples in cases.items():
            with self.subTest(name=name):
                dog, clock, calls = self.make(samples)
                self.assertIsNotNone(self.run_until(dog, clock, 14), dog.events)
                self.assertIn("terminate", calls)

    def test_slow_swap_growth_and_a_warning_level_are_not_critical(self) -> None:
        slow = [_sample(12.0, swap_mib=1000.0 + 50.0 * second) for second in range(30)]
        warning = [_sample(12.0, level=2)] * 30
        for samples in (slow, warning):
            dog, clock, calls = self.make(samples)
            self.assertIsNone(self.run_until(dog, clock, 29))
            self.assertNotIn("terminate", calls)

    def test_pressure_that_clears_within_the_grace_retracts_nothing(self) -> None:
        dog, clock, calls = self.make([_critical(), _critical(), _sample(6.0)] + [_sample(6.0)] * 8)
        self.assertIsNone(self.run_until(dog, clock, 9))
        self.assertEqual(calls, ["reclaim g"])
        self.assertIn("cleared after 2.0s", dog.events)
        self.assertEqual(dog.min_available_gib, 6.0)

    def test_an_unreadable_sample_is_never_red(self) -> None:
        unreadable = SimpleNamespace(status="UNAVAILABLE", detail="denied", data=None)
        dog, clock, calls = self.make([unreadable] * 10)
        self.assertIsNone(self.run_until(dog, clock, 9))
        self.assertEqual(calls, [])

    def test_units_retract_youngest_unproven_first_then_older_ones_five_seconds_apart(self) -> None:
        order = [
            {"label": "young-unproven", "unproven": True},
            {"label": "young-proven", "unproven": False},
            {"label": "old-proven", "unproven": False},
        ]
        retracted = {}
        for rank, item in enumerate(order):
            dog, clock, _calls = self.make([_critical()] * 20, goal=item["label"], order=lambda: order)
            retracted[item["label"]] = self.run_until(dog, clock, 19)
            self.assertIn(f"retraction rank {rank + 1} of 3", " ".join(dog.events))
        self.assertEqual(retracted, {"young-unproven": 3, "young-proven": 8, "old-proven": 13})

    def test_an_older_unit_survives_when_the_first_retraction_relieves_the_host(self) -> None:
        order = [{"label": "young", "unproven": True}, {"label": "old", "unproven": False}]
        # Critical for four seconds (the young unit goes at 3 s), then green.
        samples = [_critical()] * 4 + [_sample(8.0)] * 16
        dog, clock, calls = self.make(samples, goal="old", order=lambda: order)
        self.assertIsNone(self.run_until(dog, clock, 19))
        self.assertNotIn("terminate", calls)

    def test_the_real_order_ranks_only_watched_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": tmp}):
            from creme import semaphore

            adapter = SimpleNamespace(memory_headroom=lambda: _sample(20.0))
            policy = {"task_memory_gib": 2, "heavy_workers": 4, "light_workers": 4,
                      "physical_memory_gib": 24.0, "profile_status": "VALID"}
            with patch("creme.semaphore._refresh_signals",
                       return_value={"lean_workers": None, "holds": {}}):
                for label, unproven, watched in (("lsp", False, False), ("build", True, True)):
                    ok, detail = semaphore.adaptive_acquire(
                        label, "unit", memory_gib=2, adapter=adapter, policy=policy,
                        unproven=unproven, watched=watched,
                    )
                    self.assertTrue(ok, detail)
            self.assertEqual([item["label"] for item in semaphore.retraction_order()], ["build"])


class _RetractingHost(_FakeHost):
    """The walk fixture, plus a watchdog that retracts named builds."""

    def __init__(self, graph, *, retract=None, **kwargs):
        super().__init__(graph, **kwargs)
        self.retract = dict(retract or {})
        self.next_retracted = False
        self.watchdogs = 0

    def popen(self, args, **kwargs):
        targets = args[args.index("--verbose") + 1:]
        key = " ".join(targets)
        if self.retract.get(key, 0) > 0:
            self.retract[key] -= 1
            self.next_retracted = True
            self.lake_calls.append(list(targets))

            class Killed:
                pid = 4321
                stdout = iter([])

                def wait(self, timeout=None):
                    return -15

            return Killed()
        self.next_retracted = False
        return super().popen(args, **kwargs)

    def watchdog_class(self):
        host = self

        class FakeWatchdog:
            def __init__(self, goal, proc, **_kwargs):
                host.watchdogs += 1
                self.retracted = host.next_retracted
                self.cleanup_proved = True
                self.min_available_gib = 1.2
                self.events = (
                    ["red: available 1.20 GiB is below the 2.00 GiB floor; retraction rank 1 of 1",
                     "reclaim OK: 0 worker(s)", "retract after 3.0s red: fixture"]
                    if self.retracted else []
                )

            def start(self):
                pass

            def stop(self):
                pass

        return FakeWatchdog


class RetractedOutcomeTest(unittest.TestCase):
    def test_a_retraction_exits_75_with_a_verdict_and_ledger_evidence(self) -> None:
        host = _RetractingHost({"Pkg.Base": set()}, limit_gib=64, retract={"Pkg.Base": 1})
        self.assertEqual(host.run(["Pkg.Base"]), owned.RETRACTED_EXIT)
        summary = host.summary()
        self.assertEqual((summary["status"], summary["outcome"]), ("RETRACTED", "retracted"))
        self.assertEqual(summary["target_verdicts"], {"Pkg.Base": "retracted"})
        self.assertEqual(summary["retracted_modules"], ["Pkg.Base"])
        self.assertIn("RETRACTED", summary["hint"])
        self.assertIn("observed peak 0.98 GiB is now ledger evidence", summary["hint"])
        row = [row for row in host.rows if not row.get("probe")][-1]
        self.assertEqual((row["exit"], row["outcome"]), (owned.RETRACTED_EXIT, "retracted"))
        self.assertEqual(row["modules_failed"], ["Pkg.Base"])
        self.assertEqual(row["min_available_gib"], 1.2)
        self.assertIn("retract after 3.0s red: fixture", row["watchdog_events"])
        self.assertEqual(host.releases, 1)
        # The row is the sole failure of its run: it floors the module's next need.
        row = {"schema_version": 1, "time": "2026-09-23T00:00:00Z", **row}
        self.assertTrue(owned._valid_ledger_row(row))
        sizing = owned.size_stale_set(
            ["Pkg.Base"], {"Pkg.Base": set()}, [], SETTINGS, 8, failed_rows=[row],
        )
        self.assertEqual(sizing["failed_attempt_modules"], ["Pkg.Base"])
        self.assertAlmostEqual(sizing["need_gib"], 1000.0 / 1024.0, places=2)
        self.assertFalse(sizing["unproven"])

    def test_the_admitted_hold_is_marked_watched(self) -> None:
        host = _RetractingHost({"Pkg.Base": set()}, limit_gib=64)
        self.assertEqual(host.run(["Pkg.Base"]), 0)
        self.assertEqual(host.acquires[-1]["watched"], True)
        self.assertEqual(host.watchdogs, 1)
        host = _RetractingHost({"Pkg.Base": set()}, limit_gib=64)
        self.assertEqual(host.run(["Pkg.Base"], watchdog=False), 0)
        self.assertEqual((host.acquires[-1]["watched"], host.watchdogs), (False, 0))


class WalkRequeueTest(unittest.TestCase):
    def test_a_retracted_unit_is_requeued_once_at_its_observed_peak(self) -> None:
        host = _RetractingHost(DIAMOND, limit_gib=2, retract={"Pkg.Left": 1})
        self.assertEqual(host.run(["Pkg.Top"], walk=True), 0)
        self.assertEqual([call for call in host.lake_calls if call == ["Pkg.Left"]],
                         [["Pkg.Left"], ["Pkg.Left"]])
        requeued = host.acquires[host.lake_calls.index(["Pkg.Left"]) + 2]
        self.assertGreaterEqual(requeued["need_gib"], 1000.0 / 1024.0)
        self.assertFalse(requeued["unproven"])
        self.assertEqual(requeued["wait_seconds"], owned.WALK_REQUEUE_WAIT_SECONDS)
        summary = host.summary()
        self.assertEqual(summary["status"], "OK")
        self.assertIn("Pkg.Left", summary["walk"]["units_built"])
        self.assertIn("re-queueing once", host.output.getvalue())

    def test_a_second_retraction_stops_the_walk(self) -> None:
        host = _RetractingHost(DIAMOND, limit_gib=2, retract={"Pkg.Left": 2})
        self.assertEqual(host.run(["Pkg.Top"], walk=True), owned.RETRACTED_EXIT)
        self.assertEqual(host.lake_calls.count(["Pkg.Left"]), 2)
        summary = host.summary()
        self.assertEqual(summary["walk"]["failed_unit"]["module"], "Pkg.Left")
        self.assertTrue(summary["walk"]["failed_unit"]["retracted"])
        self.assertIn("Pkg.Top", summary["walk"]["remaining"])
        self.assertNotIn(["Pkg.Top"], host.lake_calls)


_HOG = r'''#!{python}
import sys, time
if "--no-build" in sys.argv:
    sys.exit(3)
import os
step = 32 * 1024 * 1024
target = {target_mib} * 1024 * 1024
noise = os.urandom(step)   # incompressible: the compressor cannot hide the hog
blocks = []
total = 0
while total < target:
    block = bytearray(noise)
    blocks.append(block)
    total += step
    with open({progress!r}, "w") as handle:
        handle.write(str(total // (1024 * 1024)))
    time.sleep(0.1)
with open({progress!r}, "a") as handle:
    handle.write(" done")
time.sleep(1.0)
'''

# The Darwin adapter's `memory_available_bytes` is derived from
# `memory_pressure`'s free percentage, which counts compressible anonymous
# memory as free: on this host it stayed at 79% while 2 GiB of incompressible
# memory was allocated.  The control therefore feeds the real watchdog a real
# measure that moves with allocation -- memory obtainable without compressing
# or swapping anything (free + file-backed + purgeable pages; MemAvailable on
# Linux) -- carrying the adapter's own pressure cause.
_DIRECT_PROBE = r'''
import re, subprocess
from types import SimpleNamespace
from creme.adapters import get_adapter

def direct_available_bytes():
    try:
        with open("/proc/meminfo") as handle:
            return int(re.search(r"MemAvailable:\s+(\d+)", handle.read()).group(1)) * 1024
    except OSError:
        pass
    text = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", text).group(1))
    pages = lambda name: int(re.search(name + r":\s+(\d+)", text).group(1))
    return (pages("Pages free") + pages("File-backed pages") + pages("Pages purgeable")) * page

def direct_probe():
    sample = get_adapter().memory_headroom()
    data = dict(sample.data or {{}})
    data["memory_available_bytes"] = direct_available_bytes()
    data["memory_available_direct"] = True   # below the floor is then critical
    return SimpleNamespace(status=sample.status, detail=sample.detail, data=data)
'''

_DRIVER = _DIRECT_PROBE + r'''
import json, os
from pathlib import Path
from unittest.mock import patch
from creme import build_ownership as owned

config = owned.WatchdogConfig(
    floor_gib={floor}, probe=direct_probe, reclaim=lambda goal: "reclaim skipped (test)",
    order=lambda: [], interval=0.25, grace=1.0, step=5.0,
)
probe = {{"roots": ["Hog"], "package_roots": ["Hog"], "resolution": "fixture",
         "stale": 1, "detail": "fixture", "stale_set": ["Hog"], "graph": {{"Hog": set()}}}}
with patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")), \
     patch("creme.build_ownership.resolve_toolchain", return_value=(Path({lake!r}), Path("/bin/sh"), Path("/"))), \
     patch("creme.build_ownership.guard_bin", return_value=Path({bin!r})), \
     patch("creme.build_ownership.stale_evidence", return_value=probe), \
     patch("creme.build_ownership.semaphore.adaptive_acquire", return_value=(True, "ADMITTED_SOFT")), \
     patch("creme.build_ownership.semaphore.adaptive_release", return_value=(True, "released")):
    raise SystemExit(owned.run_lake_build(
        "g", ["Hog"], contention="tolerant", memory_gib=3, watchdog={watchdog}, watch=config,
    ))
'''


class MemoryHogControlTest(unittest.TestCase):
    """The control that bites: a real child allocating memory under the real wrapper.

    The red line is set 0.75 GiB below the host's availability at the start,
    so the watchdog must retract the hog after roughly 0.75-1.25 GiB of its
    2.5 GiB target; with the watchdog disabled the same hog reaches its
    target and the build succeeds.  Nothing here approaches exhaustion.
    """

    TARGET_MIB = 2560

    def available_gib(self) -> float:
        namespace: dict = {}
        exec(_DIRECT_PROBE.replace("{{", "{").replace("}}", "}"), namespace)
        try:
            sample = namespace["direct_probe"]()
        except (OSError, AttributeError, ValueError) as exc:
            self.skipTest(f"no direct memory measure on this host: {exc}")
        _free, available, _total = owned.semaphore._headroom_values(sample, None)
        if sample.status != "OK" or available is None:
            self.skipTest(f"memory headroom unavailable: {sample.detail}")
        if owned.watchdog_red(sample, 0.0) is not None:
            self.skipTest("host is under swap/compressor pressure; not adding a hog")
        return float(available)

    def run_hog(self, *, watchdog: bool) -> tuple[int, int, bool, list[dict]]:
        available = self.available_gib()
        if available < 8.0:
            self.skipTest(f"only {available:.1f} GiB available; the hog needs a calm host")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = root / "progress"
            lake = root / "lake"
            lake.write_text(_HOG.format(
                python=sys.executable, target_mib=self.TARGET_MIB, progress=str(progress),
            ), encoding="utf-8")
            lake.chmod(0o700)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "nice").write_text("#!/bin/sh\nshift 2\nexec \"$@\"\n", encoding="utf-8")
            (fake_bin / "nice").chmod(0o700)
            driver = _DRIVER.format(
                floor=round(available - 0.75, 3), lake=str(lake), bin=str(fake_bin),
                watchdog=watchdog,
            )
            env = dict(os.environ, CREME_BUILD_LEDGER=str(root / "ledger.jsonl"),
                       CREME_SEMAPHORE_DIR=str(root / "state"))
            (root / "state").mkdir()
            completed = subprocess.run(
                [sys.executable, "-c", driver], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=120,
            )
            text = progress.read_text() if progress.exists() else "0"
            reached = int(text.split()[0])
            rows = [json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()
                    if line.strip()]
        return completed.returncode, reached, "done" in text, rows

    def test_the_watchdog_retracts_the_hog_before_it_reaches_its_target(self) -> None:
        code, reached, done, rows = self.run_hog(watchdog=True)
        self.assertEqual(code, owned.RETRACTED_EXIT, rows)
        self.assertFalse(done)
        self.assertLess(reached, self.TARGET_MIB)
        build = [row for row in rows if not row.get("probe")][-1]
        self.assertEqual(build["outcome"], "retracted")
        self.assertEqual(build["modules_failed"], ["Hog"])
        self.assertGreater(build["peak_rss_mib"], 500.0)

    def test_without_the_watchdog_the_same_hog_reaches_its_target(self) -> None:
        code, reached, done, rows = self.run_hog(watchdog=False)
        self.assertEqual(code, 0, rows)
        self.assertTrue(done)
        self.assertEqual(reached, self.TARGET_MIB)
        build = [row for row in rows if not row.get("probe")][-1]
        self.assertNotIn("outcome", build)


if __name__ == "__main__":
    unittest.main()
