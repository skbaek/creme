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
from creme.adapters.base import Adapter
from creme.adapters.linux import LinuxAdapter
from creme.adapters.darwin import DarwinAdapter
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


def _linux_sample(available_gib=5, psi=0):
    return SimpleNamespace(status="OK", data={
        "memory_free_percent": int(available_gib / 16 * 100),
        "physical_memory_bytes": 16 * 1024 ** 3,
        "memory_available_direct": True,
        "memory_available_bytes": int(available_gib * 1024 ** 3),
        "memory_psi_full_avg10": psi,
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

    def watchdog(self, ceiling_gib=16, clock=None, aggregate_ceiling_gib=None):
        return lsp.LspWorkerWatchdog(
            ROOT, ceiling_gib,
            snapshot=lambda: self.rows,
            footprints=lambda pids: {pid: self.sizes[pid] for pid in pids if pid in self.sizes},
            instances=lambda pids: {pid: ("fixture start", self.rows[pid][2]) for pid in pids if pid in self.rows},
            aggregate_ceiling_gib=aggregate_ceiling_gib,
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


class ProcessInstanceTest(unittest.TestCase):
    def test_supported_adapters_sample_start_identity_and_preserve_command(self) -> None:
        for adapter in (LinuxAdapter(), DarwinAdapter()):
            with self.subTest(system=adapter.system), patch.object(adapter, "_run", return_value=
                    SimpleNamespace(returncode=0, stdout="110 Fri Oct  2 01:02:03 2026 " + WORKER_CMD + "\n")) as run:
                sample = adapter.process_instances([110])
            self.assertEqual(sample.status, "OK")
            self.assertEqual(sample.data["instances"], {
                110: {"started": "Fri Oct 2 01:02:03 2026", "command": WORKER_CMD},
            })
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_missing_or_malformed_identity_never_becomes_verified(self) -> None:
        adapter = LinuxAdapter()
        with patch.object(adapter, "_run", return_value=SimpleNamespace(returncode=0, stdout="bad row\n")):
            self.assertEqual(adapter.process_instances([110]).data["instances"], {})
        with patch.object(adapter, "_run", side_effect=subprocess.TimeoutExpired("ps", 2)):
            self.assertEqual(adapter.process_instances([110]).status, "UNAVAILABLE")
        self.assertEqual(Adapter.unsupported("Windows").process_instances([110]).status, "UNAVAILABLE")

    def test_invalid_pid_is_refused_without_launching_a_command(self) -> None:
        with patch.object(LinuxAdapter, "_run") as run:
            self.assertEqual(LinuxAdapter().process_instances([True]).status, "REFUSED")
        run.assert_not_called()


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

    def test_two_workers_below_eight_gib_answer_aggregate_pressure(self) -> None:
        clock = _Clock()
        host = _Host(_rows(A=3, B=5), samples=[_sample(level=4)])
        guard = host.watchdog(clock=clock)
        for moment in (0, 10, 13):
            clock.now = moment
            guard.check()
        self.assertEqual(host.stopped, [111])
        self.assertEqual(host.probes, 3)
        self.assertIn("aggregate 8.0 GiB", host.records[0][1])
        self.assertIn(901, host.rows)

    def test_linux_low_availability_and_psi_stop_small_workers(self) -> None:
        for sample in (_linux_sample(1), _linux_sample(5, psi=25)):
            with self.subTest(sample=sample):
                clock = _Clock()
                host = _Host(_rows(A=3, B=5), samples=[sample])
                guard = host.watchdog(clock=clock)
                guard.check()
                clock.now = 3
                guard.check()
                self.assertEqual(host.stopped, [111])
                self.assertIn(901, host.rows)

    def test_recovery_resets_the_pressure_grace(self) -> None:
        clock = _Clock()
        host = _Host(_rows(A=5), samples=[_linux_sample(1), _linux_sample(5), _linux_sample(1)])
        guard = host.watchdog(clock=clock)
        for moment in (0, 2, 3, 5):
            clock.now = moment
            guard.check()
        self.assertEqual(host.stopped, [])
        clock.now = 6
        guard.check()
        self.assertEqual(host.stopped, [110])

    def test_unavailable_telemetry_pauses_pressure_without_asserting_recovery(self) -> None:
        unknowns = [SimpleNamespace(status="UNAVAILABLE", data=None),
                    SimpleNamespace(status="OK", data={}),
                    SimpleNamespace(status="UNAVAILABLE", data=_linux_sample(1).data)]
        for unknown in unknowns:
            with self.subTest(unknown=unknown):
                clock = _Clock()
                host = _Host(_rows(A=5), samples=[_linux_sample(1), _linux_sample(1), unknown, _linux_sample(1)])
                guard = host.watchdog(clock=clock)
                for moment in (0, 2, 3):
                    clock.now = moment
                    guard.check()
                self.assertEqual(guard.telemetry_status, "UNAVAILABLE")
                self.assertIsNotNone(guard._critical)
                clock.now = 100
                guard.check()
                self.assertEqual(host.stopped, [])
                clock.now = 101
                guard.check()
                self.assertEqual(host.stopped, [110])

    def test_probe_exception_is_unknown_and_keeps_ceiling_active(self) -> None:
        host = _Host(_rows(A=5))
        guard = host.watchdog()
        guard.probe = lambda: (_ for _ in ()).throw(OSError("unreadable"))
        guard.check()
        self.assertEqual(guard.telemetry_status, "UNAVAILABLE")
        host.rows[110] = (104, 17 * GIB_KIB, host.rows[110][2])
        guard.check()
        self.assertEqual(host.stopped, [110])

    def test_missing_process_snapshot_keeps_pressure_episode_unknown(self) -> None:
        clock = _Clock()
        host = _Host(_rows(A=5), samples=[_linux_sample(1)])
        guard = host.watchdog(clock=clock)
        guard.check()
        clock.now = 2
        guard.check()
        snapshot = guard.snapshot
        guard.snapshot = lambda: None
        clock.now = 3
        guard.check()
        self.assertEqual(guard.telemetry_status, "UNAVAILABLE")
        self.assertIsNotNone(guard._critical)
        guard.snapshot = snapshot
        clock.now = 100
        guard.check()
        self.assertEqual(host.stopped, [])
        clock.now = 101
        guard.check()
        self.assertEqual(host.stopped, [110])

    def test_aggregate_counts_unverified_workers_but_stops_only_verified_worker(self) -> None:
        host = _Host(_rows(A=8, B=5))
        guard = host.watchdog(aggregate_ceiling_gib=12)
        guard.instances = lambda pids: {111: ("fixture start", host.rows[111][2])}
        guard.check()
        self.assertEqual(host.stopped, [111])
        self.assertIn("aggregate owned workers 13.0 GiB", host.records[0][1])
        self.assertIn(110, host.rows)

    def test_aggregate_ceiling_answers_fast_growth_even_without_host_telemetry(self) -> None:
        host = _Host(_rows(A=4, B=4), samples=[SimpleNamespace(status="UNAVAILABLE", data=None)])
        guard = host.watchdog(ceiling_gib=9, aggregate_ceiling_gib=12)
        guard.check()
        self.assertEqual(host.stopped, [])
        host.rows[110] = (104, 6 * GIB_KIB, host.rows[110][2])
        host.rows[111] = (104, 7 * GIB_KIB, host.rows[111][2])
        guard.check()
        self.assertEqual(host.stopped, [111])
        self.assertIn("aggregate owned workers 13.0 GiB", host.records[0][1])
        self.assertIn(901, host.rows)

    def test_missing_identity_does_not_authorize_a_stop(self) -> None:
        host = _Host(_rows(A=5, B=4), samples=[_linux_sample(1)])
        clock = _Clock()
        guard = host.watchdog(clock=clock)
        guard.instances = lambda pids: {}
        for moment in (0, 3, 30):
            clock.now = moment
            guard.check()
        self.assertEqual(host.stopped, [])
        self.assertEqual(host.probes, 3)


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
                instance = lsp._instances([proc.pid]).get(proc.pid)
                if instance and instance[1] == command:
                    return proc, lsp.Worker(proc.pid, command, 0, None, instance[0])
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

    def test_recycled_pid_with_same_command_is_not_signalled(self) -> None:
        rows = _rows(A=3)
        worker = lsp.Worker(110, rows[110][2], 3 * GIB_KIB, None, "original start")
        with patch("os.kill") as kill:
            self.assertEqual(lsp.stop_worker(worker, ROOT, snapshot=lambda: rows,
                             instances=lambda pids: {110: ("new start", worker.command)}), 0)
        kill.assert_not_called()

    def test_identity_is_rechecked_before_sigkill(self) -> None:
        rows = _rows(A=3)
        worker = lsp.Worker(110, rows[110][2], 3 * GIB_KIB, None, "original start")
        identities = iter([{110: (worker.started, worker.command)}, {110: ("new start", worker.command)}])
        with patch("os.kill") as kill, patch.object(lsp, "_alive", return_value=True):
            code = lsp.stop_worker(worker, ROOT, snapshot=lambda: rows,
                                   instances=lambda pids: next(identities), grace=0)
        self.assertEqual(code, -int(signal.SIGTERM))
        kill.assert_called_once_with(110, signal.SIGTERM)


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
            def __init__(self, pid, ceiling, aggregate_ceiling_gib=None):
                events.append(f"watch {pid} {ceiling}")
                self.aggregate = aggregate_ceiling_gib
                if self.aggregate != 14:
                    raise AssertionError("aggregate ceiling was not passed to the watchdog")

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
                             ceiling_gib=12, aggregate_ceiling_gib=14, popen=popen, watchdog=Guard)
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
