"""The lean-mcp watchdog: only owned file workers, stopped past a ceiling or under pressure."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creme import build_ownership as owned
from creme import lsp_watchdog as lsp
from creme.profile import fingerprint, lsp_worker_ceiling_gib, validate_data


GIB_KIB = 1024 ** 2
ROOT = 100
WORKER_CMD = "/elan/toolchains/v4/bin/lean --worker file:///repo/.worktrees/goal-a/A%20B.lean"
FOREIGN_CMD = "/elan/toolchains/v4/bin/lean --worker file:///repo/.worktrees/goal-b/B.lean"


def _rows(**workers_gib):
    """ROOT -> uvx -> mcp -> lake serve -> lean --server -> owned workers; a foreign tree beside it."""
    rows = {
        ROOT: (1, 50_000, "python3 -m creme lean-mcp -- uvx lean-lsp-mcp==0.26.1"),
        101: (ROOT, 50_000, "/reviewed/uvx lean-lsp-mcp==0.26.1"),
        102: (101, 50_000, "python lean-lsp-mcp"),
        103: (102, 50_000, "lake serve"),
        104: (103, 900_000, "/elan/toolchains/v4/bin/lean --server"),
        # A shell that merely quotes the words is not a worker.
        105: (ROOT, 1_000, "/bin/sh -c pgrep -f 'lean --worker'"),
        # Another session's server and worker: never ours, however large.
        900: (1, 900_000, "/elan/toolchains/v4/bin/lean --server"),
        901: (900, 40 * GIB_KIB, FOREIGN_CMD),
    }
    for index, (name, gib) in enumerate(sorted(workers_gib.items())):
        rows[110 + index] = (104, int(gib * GIB_KIB), f"{WORKER_CMD.rsplit('/', 1)[0]}/{name}.lean")
    return rows


def _sample(level=None, swap_mib=None):
    return SimpleNamespace(status="OK", detail="fixture", data={
        "memory_free_percent": 20,
        "memory_available_bytes": 5 * 1024 ** 3,
        "physical_memory_bytes": 24 * 1024 ** 3,
        "memory_pressure_level": level,
        "swap_used_mib": swap_mib,
    })


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Host:
    """An injected process table, footprint sampler, and memory probe."""

    def __init__(self, rows, footprints=None, samples=None) -> None:
        self.rows = rows
        self.sizes = footprints or {}
        self.samples = list(samples or [])
        self.probes = 0
        self.stopped: list[int] = []
        self.records: list[tuple[int, str, int]] = []

    def probe(self):
        self.probes += 1
        return self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]

    def stop(self, worker):
        self.stopped.append(worker.pid)
        self.rows.pop(worker.pid, None)
        return -int(signal.SIGTERM)

    def watchdog(self, ceiling_gib=16, clock=None):
        return lsp.LspWorkerWatchdog(
            ROOT, ceiling_gib,
            snapshot=lambda: self.rows,
            footprints=lambda pids: {pid: self.sizes[pid] for pid in pids if pid in self.sizes},
            probe=self.probe,
            stop=self.stop,
            record=lambda worker, reason, code: self.records.append((worker.pid, reason, code)),
            clock=clock or _Clock(),
        )


class OwnershipTest(unittest.TestCase):
    def test_only_lean_workers_in_the_launchers_tree_are_owned(self) -> None:
        rows = _rows(A=1, B=2)
        self.assertEqual(sorted(lsp.owned_workers(ROOT, rows)), [110, 111])

    def test_worker_document_is_the_unquoted_file_uri(self) -> None:
        worker = lsp.Worker(1, WORKER_CMD, 0, None)
        self.assertEqual(worker.document, "/repo/.worktrees/goal-a/A B.lean")


class CeilingTest(unittest.TestCase):
    def test_a_worker_past_the_ceiling_is_stopped_and_a_foreign_one_is_not(self) -> None:
        host = _Host(_rows(A=20, B=3), samples=[_sample()])
        host.watchdog().check()
        self.assertEqual(host.stopped, [110])
        self.assertIn("above the 16 GiB lsp worker ceiling", host.records[0][1])
        self.assertEqual(host.records[0][2], -int(signal.SIGTERM))
        self.assertIn(901, host.rows)

    def test_the_footprint_counts_where_rss_understates_it(self) -> None:
        # 2026-09-19: a worker's RSS read 3.9 GiB while its footprint was 25 GiB.
        host = _Host(_rows(A=3.9), footprints={110: 25 * GIB_KIB}, samples=[_sample()])
        host.watchdog().check()
        self.assertEqual(host.stopped, [110])
        self.assertIn("footprint 25.0 GiB", host.records[0][1])

    def test_workers_under_the_ceiling_without_pressure_are_left_alone(self) -> None:
        host = _Host(_rows(A=12, B=3), samples=[_sample(level=1, swap_mib=1000)])
        host.watchdog().check()
        self.assertEqual((host.stopped, host.records), ([], []))


class PressureTest(unittest.TestCase):
    def test_sustained_critical_pressure_stops_the_largest_heavy_worker_once(self) -> None:
        clock = _Clock()
        host = _Host(_rows(A=9, B=12, C=2), samples=[_sample(level=4)])
        guard = host.watchdog(clock=clock)
        # Kernel warning must hold 10 s to be critical, then the grace runs.
        for moment in (0, 5, 10, 12):
            clock.now = moment
            guard.check()
        self.assertEqual(host.stopped, [])
        clock.now = 13
        guard.check()
        self.assertEqual(host.stopped, [111])
        self.assertIn("under critical host pressure", host.records[0][1])
        # The cooldown lets the host settle before the next worker is judged.
        for moment in (14, 20, 30, 40):
            clock.now = moment
            guard.check()
        self.assertEqual(host.stopped, [111])
        clock.now = 60
        guard.check()
        self.assertEqual(host.stopped, [111, 110])

    def test_swap_growth_is_critical_too(self) -> None:
        clock = _Clock()
        host = _Host(_rows(A=9), samples=[
            _sample(swap_mib=1000), _sample(swap_mib=1600), _sample(swap_mib=2100),
            _sample(swap_mib=2600), _sample(swap_mib=3100),
        ])
        guard = host.watchdog(clock=clock)
        for moment in (0, 3, 6, 9, 12):
            clock.now = moment
            guard.check()
        self.assertEqual(host.stopped, [110])
        self.assertIn("swap grew", host.records[0][1])

    def test_pressure_never_stops_a_worker_below_the_heavy_size(self) -> None:
        clock = _Clock()
        host = _Host(_rows(A=3, B=5), samples=[_sample(level=4)])
        guard = host.watchdog(clock=clock)
        for moment in range(0, 60, 3):
            clock.now = moment
            guard.check()
        self.assertEqual(host.stopped, [])
        # Without an owned heavy worker the probe is never paid for.
        self.assertEqual(host.probes, 0)


class StopWorkerTest(unittest.TestCase):
    """Real signals, against a process whose command line reads `lean --worker`."""

    def _spawn(self, script: str) -> tuple[subprocess.Popen, lsp.Worker]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = Path(tmp.name) / "lean"
        fake.symlink_to("/bin/sh")
        proc = subprocess.Popen([str(fake), "-c", script, "--worker", "file:///w/.worktrees/g/X.lean"])
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = owned._process_snapshot() or {}
            command = lsp.owned_workers(os.getpid(), rows).get(proc.pid)
            if command:
                return proc, lsp.Worker(proc.pid, command, 0, None)
            time.sleep(0.05)
        self.fail("fake worker never appeared in the process table")

    def test_sigterm_stops_a_worker(self) -> None:
        proc, worker = self._spawn("while :; do sleep 0.1; done")
        code = lsp.stop_worker(worker, os.getpid(), grace=2.0)
        self.assertEqual(code, -int(signal.SIGTERM))
        self.assertEqual(proc.wait(timeout=5), -int(signal.SIGTERM))

    def test_a_worker_ignoring_sigterm_is_killed(self) -> None:
        proc, worker = self._spawn("trap '' TERM; while :; do sleep 0.1; done")
        code = lsp.stop_worker(worker, os.getpid(), grace=0.6)
        self.assertEqual(code, -int(signal.SIGKILL))
        self.assertEqual(proc.wait(timeout=5), -int(signal.SIGKILL))

    def test_a_process_outside_the_tree_is_never_signalled(self) -> None:
        proc, worker = self._spawn("while :; do sleep 0.1; done")
        # A root that is not its ancestor owns nothing.
        self.assertEqual(lsp.stop_worker(worker, 99_999_999, grace=0.2), 0)
        changed = lsp.Worker(worker.pid, worker.command + " other", 0, None)
        self.assertEqual(lsp.stop_worker(changed, os.getpid(), grace=0.2), 0)
        self.assertIsNone(proc.poll())


class RecordTest(unittest.TestCase):
    def test_a_stop_is_a_valid_ledger_row_and_a_stderr_line(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worker = lsp.Worker(7, WORKER_CMD, 3 * GIB_KIB, 20 * GIB_KIB)
        with patch.dict(os.environ, {"CREME_BUILD_LEDGER": str(Path(tmp.name) / "ledger.jsonl")}), \
                patch("sys.stderr") as stderr, patch("sys.stdout") as stdout:
            lsp.record_stop(worker, "fixture reason", -15)
            rows, corrupt = owned.read_ledger("1d")
        self.assertEqual(corrupt, 0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "lsp_worker")
        self.assertEqual(row["goal"], "goal-a")
        self.assertEqual(row["worktree"], "/repo/.worktrees/goal-a")
        self.assertEqual(row["targets"], ["/repo/.worktrees/goal-a/A B.lean"])
        self.assertEqual((row["exit"], row["peak_rss_mib"]), (-15, 20480.0))
        self.assertTrue(stderr.write.called)
        self.assertFalse(stdout.write.called)  # stdout is the MCP channel

    def test_an_lsp_worker_row_without_a_reason_is_invalid(self) -> None:
        row = {
            "schema_version": owned.SCHEMA_VERSION, "time": "2026-09-26T00:00:00Z",
            "kind": "lsp_worker", "worktree": "/w", "goal": "g", "targets": [],
            "command": ["lean", "--worker"], "exit": -15, "peak_rss_mib": 1.0,
        }
        self.assertFalse(owned._valid_ledger_row(row))
        row["reason"] = "r"
        self.assertTrue(owned._valid_ledger_row(row))


class SuperviseTest(unittest.TestCase):
    def test_the_server_runs_as_a_watched_child_and_its_exit_is_returned(self) -> None:
        events: list[str] = []

        class Child:
            pid = 4242

            def wait(self):
                events.append("wait")
                return -int(signal.SIGTERM)

            def send_signal(self, signum):
                events.append(f"signal {signum}")

        class Guard:
            def __init__(self, pid, ceiling):
                events.append(f"watch {pid} {ceiling}")

            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

        launched = {}

        def popen(command, **kwargs):
            launched.update(command=command, **kwargs)
            return Child()

        before = signal.getsignal(signal.SIGTERM)
        code = lsp.supervise(["/reviewed/uvx", "lean-lsp-mcp==0.26.1"], {"PATH": "/guard"},
                             ceiling_gib=12, popen=popen, watchdog=Guard)
        self.assertEqual(code, 128 + int(signal.SIGTERM))
        self.assertEqual(events, ["watch 4242 12", "start", "wait", "stop"])
        self.assertEqual(launched["env"], {"PATH": "/guard"})
        self.assertEqual(launched["executable"], "/reviewed/uvx")
        self.assertNotIn("stdout", launched)  # the child inherits the MCP stdio
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


class CeilingSettingTest(unittest.TestCase):
    FACTS = {"system": "Darwin", "machine": "arm64", "logical_cores": 10,
             "physical_memory_bytes": 25769803776}

    def test_default_is_two_thirds_of_memory_and_a_configured_value_wins(self) -> None:
        self.assertEqual(lsp_worker_ceiling_gib(None, 24 * 1024 ** 3), 16)
        self.assertEqual(lsp_worker_ceiling_gib({"lsp_watchdog": {"worker_ceiling_gib": 10}}, 24 * 1024 ** 3), 10)
        self.assertEqual(lsp_worker_ceiling_gib({"lsp_watchdog": {"worker_ceiling_gib": 0}}, 24 * 1024 ** 3), 16)
        self.assertEqual(lsp_worker_ceiling_gib(None, None), 16)

    def test_the_profile_validates_the_lsp_watchdog_object(self) -> None:
        data = {
            "schema_version": 1, "fingerprint": fingerprint(self.FACTS), "facts": self.FACTS,
            "workspace": {"root": "/x", "jaune": "jaune", "blanc": "blanc", "goal_store": None},
            "policy": {"task_memory_gib": 8, "heavy_workers": 2, "light_workers": 5},
            "overrides": {"task_memory_gib": None, "heavy_workers": None, "light_workers": None},
            "lsp_watchdog": {"worker_ceiling_gib": 12},
        }
        self.assertEqual(validate_data(data).status, "VALID")
        data["lsp_watchdog"]["worker_ceiling_gib"] = 1
        self.assertEqual(validate_data(data).status, "INVALID")
        data["lsp_watchdog"] = {"ceiling": 12}
        self.assertEqual(validate_data(data).status, "INVALID")


if __name__ == "__main__":
    unittest.main()
