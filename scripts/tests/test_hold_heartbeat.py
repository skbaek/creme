"""Goal-hold heartbeat: renews, stops on its own, never duplicates, never kills.

Every test runs against a temporary semaphore directory with fake memory
headroom, a fake clock, and a fake sleep; nothing touches the host's state.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from creme import cli, semaphore

try:
    from test_semaphore import HeadroomAdapter, ProcessAdapter
except ImportError:  # pragma: no cover - package-style discovery
    from scripts.tests.test_semaphore import HeadroomAdapter, ProcessAdapter

OWNER = 424242


class FakeTime:
    """A wall clock that the heartbeat's sleep advances, with a per-sleep hook."""

    def __init__(self, hook=None):
        self.now = 1_000_000.0
        self.sleeps: list[float] = []
        self.hook = hook

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.hook is not None:
            self.hook(len(self.sleeps))
        if len(self.sleeps) > 200:
            raise AssertionError("heartbeat did not stop")


class HoldHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = HeadroomAdapter()
        self.policy = {
            "task_memory_gib": 2, "heavy_workers": 4, "light_workers": 4,
            "physical_memory_gib": 32.0, "profile_status": "VALID",
        }
        self.dead: set[int] = set()
        real_alive = semaphore._pid_alive
        for patcher in (
            mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False),
            mock.patch("creme.semaphore.get_adapter", return_value=self.adapter),
            mock.patch("creme.semaphore._runtime_admission_policy", return_value=self.policy),
            mock.patch(
                "creme.semaphore._pid_alive",
                side_effect=lambda pid: pid == OWNER and pid not in self.dead or (
                    pid != OWNER and pid not in self.dead and real_alive(pid)
                ),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertTrue(semaphore.adaptive_acquire("goal", "proof loop", 600, memory_gib=4)[0])

    def hold(self, label="goal"):
        return semaphore._find_hold(semaphore.snapshot(), label)

    def registry(self):
        return json.loads((self.root / semaphore.HOLD_HEARTBEATS_NAME).read_text(encoding="utf-8"))["heartbeats"]

    def log(self, action):
        rows = [json.loads(line) for line in (self.root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["action"] == action]

    def run_heartbeat(self, fake, interval=120, **kwargs):
        return semaphore.hold_heartbeat(
            "goal", interval, owner_pid=OWNER, sleep=fake.sleep, clock=fake.clock, **kwargs,
        )

    def test_it_renews_every_interval_and_records_its_beats(self):
        before = self.hold()["renewed_at"]
        fake = FakeTime()
        ok, detail = self.run_heartbeat(fake, max_beats=3)
        self.assertTrue(ok, detail)
        self.assertIn("after 3 renewal(s)", detail)
        self.assertEqual(len([row for row in self.log("renew") if row["verdict"] == "OK"]), 3)
        self.assertGreaterEqual(self.hold()["renewed_at"], before)
        self.assertEqual(self.hold()["lease_seconds"], 600)
        entry = self.registry()["goal"]
        self.assertEqual((entry["beats"], entry["owner_pid"], entry["pid"]), (3, OWNER, os.getpid()))
        # Slices never exceed the shared heartbeat slice, so a host sleep costs
        # at most one slice of lateness after waking.
        self.assertLessEqual(max(fake.sleeps), semaphore.HEARTBEAT_SLICE_SECONDS)
        self.assertAlmostEqual(fake.now - 1_000_000.0, 360.0, delta=1.0)

    def test_it_stops_by_itself_when_the_hold_is_released(self):
        fake = FakeTime(hook=lambda n: n == 3 and semaphore.adaptive_release("goal"))
        ok, detail = self.run_heartbeat(fake, interval=60)
        self.assertTrue(ok, detail)
        self.assertIn("the hold was released", detail)
        self.assertEqual(self.registry()["goal"]["stop_reason"], "the hold was released")
        self.assertEqual(self.log("hold-heartbeat")[-1]["verdict"], "STOPPED")

    def test_it_never_renews_a_hold_acquired_again_under_the_same_label(self):
        def reacquire(n):
            if n == 1:
                semaphore.adaptive_release("goal")
                semaphore.adaptive_acquire("goal", "someone else", 600, memory_gib=4)
        fake = FakeTime(hook=reacquire)
        ok, detail = self.run_heartbeat(fake, interval=60)
        self.assertTrue(ok, detail)
        self.assertIn("acquired again", detail)
        self.assertEqual([row for row in self.log("renew")], [])
        refused = semaphore.renew("goal", 600, acquired_at=1.0)
        self.assertFalse(refused[0])
        self.assertIn("acquired again", refused[1])

    def test_it_stops_when_the_owning_client_is_gone(self):
        fake = FakeTime(hook=lambda n: n == 2 and self.dead.add(OWNER))
        ok, detail = self.run_heartbeat(fake, interval=60)
        self.assertTrue(ok, detail)
        self.assertIn(f"client pid {OWNER} is gone", detail)
        self.assertIsNotNone(self.hold(), "the heartbeat never releases or kills; the hold lapses")

    def test_a_drain_verdict_stops_renewal_and_records_why_without_releasing(self):
        self.adapter.free_percent = 18
        fake = FakeTime()
        ok, detail = self.run_heartbeat(fake)
        self.assertFalse(ok)
        self.assertIn("DRAIN_HEAVY", detail)
        entry = self.registry()["goal"]
        self.assertIn("DRAIN_HEAVY", entry["stop_reason"])
        self.assertEqual(entry["beats"], 0)
        self.assertIsNotNone(self.hold())

    def test_a_yield_verdict_stops_renewal_too(self):
        self.assertTrue(semaphore.adaptive_release("goal")[0])
        self.assertTrue(semaphore.adaptive_acquire("older", "proof", 600, memory_gib=4)[0])
        self.assertTrue(semaphore.adaptive_acquire("goal", "proof", 600, memory_gib=4)[0])
        self.adapter.free_percent = 25
        ok, detail = self.run_heartbeat(FakeTime())
        self.assertFalse(ok)
        self.assertIn("YIELD_HEAVY", detail)
        self.assertIn("YIELD_HEAVY", self.registry()["goal"]["stop_reason"])

    def test_one_heartbeat_per_hold(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        first = FakeTime(hook=lambda n: n == 1 and self._claim_as(other.pid))
        ok, detail = self.run_heartbeat(first, interval=60)
        # A running heartbeat that finds itself superseded stops quietly.
        self.assertTrue(ok, detail)
        self.assertIn("another heartbeat owns goal", detail)
        self.assertEqual(self.registry()["goal"]["pid"], other.pid)
        # A second request while one runs is a no-op success, detached or not.
        ok, detail = self.run_heartbeat(FakeTime())
        self.assertTrue(ok)
        self.assertIn(f"already runs as pid {other.pid}", detail)
        with mock.patch("creme.semaphore.subprocess.Popen") as popen:
            ok, detail = semaphore.hold_heartbeat_detached("goal", 60, OWNER)
        self.assertTrue(ok)
        self.assertIn("no second heartbeat started", detail)
        popen.assert_not_called()

    def _claim_as(self, pid):
        with semaphore.locked_state() as (path, _state):
            data = semaphore._load_hold_heartbeats(path.parent)
            data["heartbeats"]["goal"]["pid"] = pid
            semaphore._write_json(semaphore.hold_heartbeats_path(path.parent), data)

    def test_an_interval_not_shorter_than_the_lease_is_refused(self):
        ok, detail = self.run_heartbeat(FakeTime(), interval=600)
        self.assertFalse(ok)
        self.assertIn("shorter than the hold's lease", detail)
        ok, detail = semaphore.hold_heartbeat("missing", 60, owner_pid=OWNER)
        self.assertFalse(ok)
        self.assertIn("hold not found", detail)

    def test_detach_launches_the_bound_child_and_waits_for_its_claim(self):
        launched = []

        class Child:
            pid = 515151

            def __init__(self, argv, **kwargs):
                launched.append((argv, kwargs))
                with semaphore.locked_state() as (path, _state):
                    data = semaphore._load_hold_heartbeats(path.parent)
                    data["heartbeats"]["goal"] = {
                        "pid": self.pid, "owner_pid": OWNER, "hold_acquired_at": 1.0,
                        "interval": 60, "started_at": 1.0, "beats": 0, "renewed_at": None,
                        "stopped_at": None, "stop_reason": None,
                    }
                    semaphore._write_json(semaphore.hold_heartbeats_path(path.parent), data)

            def poll(self):
                return None

        with mock.patch("creme.semaphore.subprocess.Popen", Child):
            ok, detail = semaphore.hold_heartbeat_detached("goal", 60, OWNER, poll=lambda s: None)
        self.assertTrue(ok, detail)
        self.assertIn("detached as pid 515151", detail)
        ((argv, kwargs),) = launched
        self.assertEqual(argv[2:], ["renew", "goal", "--heartbeat", "60", "--owner-pid", str(OWNER)])
        self.assertTrue(kwargs["start_new_session"])

    def test_detach_reports_a_child_that_exits_before_claiming(self):
        class Child:
            pid = 515152

            def __init__(self, argv, **kwargs):
                pass

            def poll(self):
                return 2

        with mock.patch("creme.semaphore.subprocess.Popen", Child):
            ok, detail = semaphore.hold_heartbeat_detached("goal", 60, OWNER, poll=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("exited with status 2", detail)


class HoldHeartbeatCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # No agent client above the test runner: the heartbeat cannot bind.
        self.adapter = ProcessAdapter(processes=[], workers=[])
        policy = {
            "task_memory_gib": 2, "heavy_workers": 4, "light_workers": 4,
            "physical_memory_gib": 32.0, "profile_status": "VALID",
        }
        for patcher in (
            mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False),
            mock.patch("creme.semaphore.get_adapter", return_value=self.adapter),
            mock.patch("creme.semaphore._runtime_admission_policy", return_value=policy),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["semaphore", *argv])
        return code, output.getvalue()

    def test_bad_combinations_are_refused_before_any_hold(self):
        code, text = self.run_cli("adaptive-acquire", "goal", "--note", "n", "--memory-gib", "4",
                                  "--lease", "600", "--heartbeat", "600")
        self.assertEqual(code, 1)
        self.assertIn("shorter than --lease", text)
        code, text = self.run_cli("adaptive-acquire", "goal", "--note", "n", "--memory-gib", "4", "--detach")
        self.assertEqual(code, 1)
        self.assertIn("--detach needs --heartbeat", text)
        self.assertEqual(semaphore.snapshot()["soft"], [])

    def test_an_unbindable_heartbeat_says_the_hold_stays_acquired(self):
        code, text = self.run_cli("adaptive-acquire", "goal", "--note", "n", "--memory-gib", "4",
                                  "--heartbeat", "240", "--detach")
        self.assertEqual(code, 1)
        self.assertTrue(text.startswith("OK"), text)
        self.assertIn("heartbeat not started", text)
        self.assertIn("stays acquired", text)
        self.assertEqual([hold["label"] for hold in semaphore.snapshot()["soft"]], ["goal"])

    def test_the_bound_client_is_passed_to_the_detached_child(self):
        with mock.patch("creme.semaphore.hold_heartbeat_owner", return_value=(4321, "client claude pid 4321")), \
                mock.patch("creme.semaphore.hold_heartbeat_detached", return_value=(True, "detached")) as detached:
            code, text = self.run_cli("adaptive-acquire", "goal", "--note", "n", "--memory-gib", "4",
                                      "--heartbeat", "240", "--detach")
        self.assertEqual(code, 0, text)
        detached.assert_called_once_with("goal", 240, 4321)


if __name__ == "__main__":
    unittest.main()
