"""Swap/compressor-aware headroom and heavy Lean worker detection.

The incident fixture reproduces 2026-09-19 on this 24 GiB host: a language-
server worker at a 25 GiB footprint (21 GiB compressed, RSS 3.9 GiB), swap
10.7 GiB used with 528 MiB free, the compressor at 11.7 GiB, and the aggregate
probe still reading 33% free.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import semaphore
from creme.adapters.base import Adapter, swap_compressor_pressure
from creme.adapters.darwin import DarwinAdapter
from creme.adapters.linux import LinuxAdapter


PHYSICAL = 25769803776  # 24 GiB
PAGE = 16384
GIB_PAGES = 1024 ** 3 // PAGE


def pressure_output(free_percent: int, compressor_gib: float) -> str:
    return (
        f"The system has {PHYSICAL} (1572864 pages with a page size of {PAGE}).\n\n"
        "Stats: \nPages free: 316961 \n\n"
        "Compressor Stats:\n"
        f"Pages used by compressor: {int(compressor_gib * GIB_PAGES)} \n"
        "Pages decompressed: 7594470354 \n\n"
        f"System-wide memory free percentage: {free_percent}%\n"
    )


def swap_output(total_mib: float, used_mib: float) -> str:
    return (
        f"total = {total_mib:.2f}M  used = {used_mib:.2f}M  "
        f"free = {total_mib - used_mib:.2f}M  (encrypted)\n"
    )


INCIDENT = (pressure_output(33, 11.7), swap_output(11484.80, 10956.80))
HEALTHY = (pressure_output(79, 0.77), swap_output(3072.00, 1699.81))


def darwin_headroom(outputs: tuple[str, str]):
    pressure, swap = outputs
    runs = [
        subprocess.CompletedProcess(["memory_pressure"], 0, stdout=pressure),
        subprocess.CompletedProcess(["sysctl"], 0, stdout=swap),
    ]
    with mock.patch.object(DarwinAdapter, "_run", side_effect=runs):
        return DarwinAdapter().memory_headroom()


class SwapCompressorRuleTest(unittest.TestCase):
    def test_incident_names_swap_and_compressor(self):
        cause = swap_compressor_pressure(PHYSICAL, 11484.80, 10956.80, int(11.7 * 1024 ** 3))
        self.assertIsNotNone(cause)
        self.assertIn("swap nearly exhausted: 10.7 of 11.2 GiB used (528 MiB free", cause)
        self.assertIn("compressor occupies 11.7 of 24 GiB physical (49%)", cause)

    def test_host_normal_states_do_not_fire(self):
        samples = {
            "today": (3072.0, 1699.81, int(0.77 * 1024 ** 3)),
            "normal upper end": (11571.2, 3072.0, 4 * 1024 ** 3),
            # macOS grows swap on demand: a small, nearly full swap is ordinary.
            "small full swap": (2048.0, 1990.0, 2 * 1024 ** 3),
            # Swap left allocated after the incident's wind-down.
            "after recovery": (11571.2, 6553.6, 3 * 1024 ** 3),
        }
        for name, (total, used, compressor) in samples.items():
            with self.subTest(name):
                self.assertIsNone(swap_compressor_pressure(PHYSICAL, total, used, compressor))

    def test_missing_measurements_contribute_nothing(self):
        self.assertIsNone(swap_compressor_pressure(None, 100.0, 99.0, 10 ** 12))
        self.assertIsNone(swap_compressor_pressure(PHYSICAL, None, None, None))


class DarwinHeadroomTest(unittest.TestCase):
    def test_incident_sample_carries_the_cause(self):
        result = darwin_headroom(INCIDENT)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.data["memory_free_percent"], 33)
        self.assertAlmostEqual(result.data["swap_free_mib"], 528.0)
        self.assertAlmostEqual(result.data["swap_total_mib"], 11484.8)
        self.assertEqual(result.data["compressor_bytes"], int(11.7 * GIB_PAGES) * PAGE)
        self.assertIn("swap nearly exhausted", result.data["memory_pressure_cause"])
        self.assertIn("SWAP_PRESSURE", result.detail)

    def test_healthy_sample_has_no_cause(self):
        result = darwin_headroom(HEALTHY)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.data["memory_free_percent"], 79)
        self.assertIsNone(result.data["memory_pressure_cause"])
        self.assertNotIn("SWAP_PRESSURE", result.detail)

    def test_vm_stat_label_and_gib_swap_units_parse(self):
        pressure = pressure_output(40, 1.0).replace("used by", "occupied by")
        result = darwin_headroom((pressure, "total = 11.00G  used = 10.50G  free = 512.00M\n"))
        self.assertEqual(result.data["compressor_bytes"], GIB_PAGES * PAGE)
        self.assertAlmostEqual(result.data["swap_used_mib"], 10.5 * 1024)
        self.assertIn("swap nearly exhausted", result.data["memory_pressure_cause"])

    def test_linux_headroom_is_unchanged(self):
        with mock.patch.object(
            LinuxAdapter, "_meminfo",
            return_value={"MemTotal": 16 * 1024 ** 2, "MemAvailable": 4 * 1024 ** 2},
        ), mock.patch("builtins.open", mock.mock_open(read_data="Filename Type Size Used Priority\n")):
            result = LinuxAdapter().memory_headroom()
        self.assertNotIn("memory_pressure_cause", result.data)

    def test_top_footprints_parse(self):
        top = (
            "Processes: 600 total\nPhysMem: 23G used\n\n"
            "PID    MEM  CMPRS\n"
            "28744  25G+ 21G+\n"
            "36902  301M 0B\n"
            "99999  1K   0B\n"
        )
        with mock.patch.object(
            DarwinAdapter, "_run", return_value=subprocess.CompletedProcess([], 0, top, "")
        ) as run:
            result = DarwinAdapter().process_footprints([36902, 28744])
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/top", "-l", "1", "-stats", "pid,mem,cmprs",
             "-pid", "28744", "-pid", "36902"],
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.data["footprints"], {
            "28744": {"footprint_kib": 25 * 1024 ** 2, "compressed_kib": 21 * 1024 ** 2},
            "36902": {"footprint_kib": 301 * 1024, "compressed_kib": 0},
        })

    def test_top_failure_is_unavailable(self):
        with mock.patch.object(
            DarwinAdapter, "_run", return_value=subprocess.CompletedProcess([], 1, "", "denied")
        ):
            self.assertEqual(DarwinAdapter().process_footprints([1]).status, "UNAVAILABLE")
        self.assertEqual(Adapter().process_footprints([1]).status, "UNAVAILABLE")

    def test_worker_sample_lists_servers_separately(self):
        snapshot = (
            "10 1 32 0:00.00 /client\n"
            "12 10 128 0:01.00 /tool/lean --worker file:///x/A.lean\n"
            "13 10 256 0:01.30 /tool/lean --server\n"
            "14 10 64 0:00.10 /bin/sh -c pgrep -f 'lean --server'\n"
        )
        with mock.patch.object(
            DarwinAdapter, "_run", return_value=subprocess.CompletedProcess([], 0, snapshot, "")
        ):
            result = DarwinAdapter().lean_workers()
        self.assertEqual([row["pid"] for row in result.data["workers"]], [12])
        self.assertEqual([row["pid"] for row in result.data["servers"]], [13])


class FixtureAdapter(Adapter):
    """A Darwin headroom parse plus fixture process answers."""

    system = "Darwin"

    def __init__(self, outputs=HEALTHY, workers=(), footprints=None, cwds=None):
        self.outputs = outputs
        self.workers = [dict(worker) for worker in workers]
        self.footprints = footprints
        self.cwds = cwds or {}

    def static_facts(self):
        return self.result("static_facts", "OK", "fixture", {
            "system": "Darwin", "machine": "arm64", "logical_cores": 8,
            "physical_memory_bytes": PHYSICAL,
        })

    def memory_headroom(self):
        return darwin_headroom(self.outputs)

    def process_snapshot(self):
        return self.result("process_snapshot", "OK", "fixture", {"processes": [
            {"pid": worker["pid"], "ppid": worker["ppid"], "rss_kib": worker["rss_kib"],
             "command": "lean"}
            for worker in self.workers
        ]})

    def lean_workers(self):
        return self.result("lean_workers", "OK", "fixture", {
            "workers": [dict(worker) for worker in self.workers], "servers": [],
        })

    def process_footprints(self, pids):
        if self.footprints is None:
            return self.result("process_footprints", "UNAVAILABLE", "fixture")
        return self.result("process_footprints", "OK", "fixture", {"footprints": {
            str(pid): self.footprints[pid] for pid in pids if pid in self.footprints
        }})

    def process_working_directories(self, pids):
        return self.result("process_working_directories", "OK", "fixture", {
            "working_directories": {str(pid): self.cwds[pid] for pid in pids if pid in self.cwds},
            "requested": sorted(pids),
        })


POLICY = {
    "task_memory_gib": 2,
    "heavy_workers": 2,
    "light_workers": 4,
    "physical_memory_gib": 24.0,
    "profile_status": "VALID",
}

INCIDENT_WORKER = {
    "pid": 28744, "ppid": 28700, "rss_kib": int(3.9 * 1024 ** 2), "cpu_seconds": 100.0,
    "command": "/Users/x/.elan/toolchains/v4/bin/lean --worker "
               "file:///Users/x/blanc/.worktrees/drip-spawns-v1/Blanc/DripFrameSpawns.lean",
    "ancestry": [],
}
INCIDENT_FOOTPRINT = {28744: {"footprint_kib": 25 * 1024 ** 2, "compressed_kib": 21 * 1024 ** 2}}


class SemaphorePressureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patcher in (
            mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False),
            mock.patch("creme.semaphore._runtime_admission_policy", return_value=POLICY),
            mock.patch("creme.semaphore._goal_scope_roots", return_value=()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use(self, adapter):
        patcher = mock.patch("creme.semaphore.get_adapter", return_value=adapter)
        patcher.start()
        self.addCleanup(patcher.stop)
        return adapter

    def log_rows(self, action):
        path = self.root / "log.jsonl"
        if not path.exists():
            return []
        return [
            row for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if row["action"] == action
        ]

    # (a) the incident drains, naming swap and the compressor
    def test_incident_refuses_light_only_naming_swap(self):
        adapter = self.use(FixtureAdapter(INCIDENT))
        ok, detail = semaphore.adaptive_acquire(
            "other-goal", "proof", adapter=adapter, policy=POLICY,
        )
        self.assertFalse(ok)
        self.assertIn("LIGHT_ONLY", detail)
        self.assertIn("swap nearly exhausted: 10.7 of 11.2 GiB used (528 MiB free", detail)
        self.assertIn("compressor occupies 11.7", detail)
        self.assertIn("33% free by the aggregate probe", detail)
        self.assertEqual(semaphore.snapshot(), semaphore._empty_state())

    def test_incident_drains_a_renewing_holder(self):
        self.use(FixtureAdapter(HEALTHY))
        self.assertTrue(semaphore.adaptive_acquire("goal", "proof", policy=POLICY)[0])
        ok, detail = semaphore.renew("goal", adapter=FixtureAdapter(INCIDENT), policy=POLICY)
        self.assertFalse(ok)
        self.assertIn("DRAIN_HEAVY", detail)
        self.assertIn("swap nearly exhausted", detail)

    def test_incident_status_names_the_cause(self):
        self.use(FixtureAdapter(INCIDENT))
        text = semaphore.status_text()
        self.assertIn("SWAP_PRESSURE: swap/compressor pressure: swap nearly exhausted", text)

    def test_the_compressor_incident_drains_but_does_not_retract(self):
        from creme import build_ownership

        sample = darwin_headroom((pressure_output(60, 11.7), swap_output(3072.0, 1699.81)))
        self.assertIsNotNone(sample.data["memory_pressure_cause"])
        # 60% free is far above the floor; the pressure cause alone is drain level.
        self.assertIn("swap/compressor pressure", build_ownership.watchdog_red(sample, 2.0))
        # The kernel level was not sampled (fixture): nothing here is critical.
        self.assertIsNone(build_ownership.watchdog_critical(sample, 2.0))

    def test_the_kernel_critical_level_is_read_and_is_critical(self):
        from creme import build_ownership

        runs = [
            subprocess.CompletedProcess(["memory_pressure"], 0, stdout=pressure_output(60, 1.0)),
            subprocess.CompletedProcess(["sysctl"], 0, stdout=swap_output(3072.0, 100.0)),
            subprocess.CompletedProcess(["sysctl"], 0, stdout="4\n"),
        ]
        with mock.patch.object(DarwinAdapter, "_run", side_effect=runs):
            sample = DarwinAdapter().memory_headroom()
        self.assertEqual(sample.data["memory_pressure_level"], 4)
        self.assertIn("critical", build_ownership.watchdog_critical(sample, 2.0))

    # (b) a healthy sample keeps today's verdict
    def test_healthy_sample_is_admitted_unchanged(self):
        adapter = self.use(FixtureAdapter(HEALTHY))
        ok, detail = semaphore.adaptive_acquire("goal", "proof", adapter=adapter, policy=POLICY)
        self.assertTrue(ok, detail)
        self.assertIn("ADMITTED_SOFT", detail)
        self.assertIn("headroom=79%", detail)
        ok, detail = semaphore.renew("goal", adapter=adapter, policy=POLICY)
        self.assertTrue(ok, detail)
        self.assertIn("CONTINUE_HEAVY", detail)
        self.assertNotIn("SWAP_PRESSURE", semaphore.status_text())

    # (c) a 25 GiB busy worker is named once per window
    def test_heavy_busy_worker_is_reported_and_logged_once(self):
        adapter = self.use(FixtureAdapter(
            INCIDENT, [INCIDENT_WORKER], INCIDENT_FOOTPRINT, {28744: "/Users/x/blanc"},
        ))
        with mock.patch("creme.semaphore._now", return_value=1_000_000.0):
            first = semaphore.status_text()
        adapter.workers[0]["cpu_seconds"] = 109.7
        with mock.patch("creme.semaphore._now", return_value=1_000_010.0):
            second = semaphore.status_text()
        for text in (first, second):
            self.assertIn(
                "HEAVY_LEAN_WORKER: pid 28744 lean --worker 25.0 GiB footprint "
                "(21.0 GiB compressed; RSS 3.9 GiB), owner goal drip-spawns-v1, "
                "no hold covers it",
                text,
            )
        self.assertIn("busy 97.0% CPU", second)
        rows = self.log_rows("worker_pressure")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["label"], "host")
        self.assertIn("WORKER_PRESSURE: 1 Lean process(es) at or above 8 GiB: pid 28744", rows[0]["detail"])
        with mock.patch("creme.semaphore._now", return_value=1_000_000.0 + 400):
            semaphore.status_text()
        self.assertEqual(len(self.log_rows("worker_pressure")), 2)

    def test_heavy_worker_named_in_refusal_and_hold_coverage(self):
        adapter = self.use(FixtureAdapter(
            INCIDENT, [INCIDENT_WORKER], INCIDENT_FOOTPRINT, {28744: "/Users/x/blanc"},
        ))
        ok, detail = semaphore.adaptive_acquire("other-goal", "proof", adapter=adapter, policy=POLICY)
        self.assertFalse(ok)
        self.assertIn("1 Lean process(es) at or above 8 GiB: pid 28744", detail)
        # The same worker under its own goal's hold reads as covered.
        heavy = semaphore._heavy_lean_processes(
            adapter, adapter.lean_workers().data, {"workers": []},
            {"drip-spawns-v1"}, [], None,
        )
        self.assertEqual(heavy[0]["covered_by"], "drip-spawns-v1")

    def test_rss_fallback_says_so(self):
        worker = dict(INCIDENT_WORKER, rss_kib=9 * 1024 ** 2)
        self.use(FixtureAdapter(HEALTHY, [worker], None, {28744: "/Users/x/blanc"}))
        text = semaphore.status_text()
        self.assertIn("9.0 GiB rss (footprint unavailable; may be understated)", text)

    # (d) a small idle worker is not heavy
    def test_small_idle_worker_is_not_reported(self):
        worker = dict(INCIDENT_WORKER, rss_kib=1024 ** 2)
        adapter = self.use(FixtureAdapter(
            HEALTHY, [worker], {28744: {"footprint_kib": 1024 ** 2, "compressed_kib": 0}},
        ))
        with mock.patch("creme.semaphore._now", return_value=2_000_000.0):
            semaphore.status_text()
        with mock.patch("creme.semaphore._now", return_value=2_000_300.0):
            text = semaphore.status_text()
        self.assertIn("IDLE_WORKERS", text)
        self.assertNotIn("HEAVY_LEAN_WORKER", text)
        self.assertEqual(self.log_rows("worker_pressure"), [])
        del adapter


if __name__ == "__main__":
    unittest.main()
