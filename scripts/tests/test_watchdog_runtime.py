"""Bounded real process controls; no Lean or host memory exhaustion."""

from __future__ import annotations

import functools
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from creme import build_ownership as owned


class GroupTerminationTest(unittest.TestCase):
    def test_exited_leader_does_not_hide_a_stubborn_child(self):
        alive = [True]
        signals = []

        def send(pgid, sig):
            signals.append((pgid, sig))
            if sig == signal.SIGKILL:
                alive[0] = False

        proc = SimpleNamespace(pid=424242, wait=Mock(return_value=0))
        with patch.object(owned, "_process_group_alive", side_effect=lambda _: alive[0]), \
                patch.object(owned.os, "killpg", side_effect=send):
            self.assertTrue(owned._terminate_process_group(proc, timeout=0))
        self.assertEqual(signals, [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)])


@unittest.skipUnless(os.name == "posix", "process-group control requires POSIX")
class WatchdogRuntimeTest(unittest.TestCase):
    def test_critical_signal_stops_owned_group_but_leaves_other_group_alive(self):
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        processes = []
        guard = None
        try:
            for _ in range(2):
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-c", code],
                    stdout=subprocess.PIPE, text=True, start_new_session=True,
                )
                processes.append(proc)
                self.assertEqual(proc.stdout.readline().strip(), "ready")
            target, foreign = processes
            sample = SimpleNamespace(status="OK", data={
                "memory_available_direct": True,
                "memory_available_bytes": 1024 ** 3,
                "physical_memory_bytes": 16 * 1024 ** 3,
                "memory_free_percent": 6,
                "swap_used_mib": 0,
            })
            with tempfile.TemporaryDirectory() as state, \
                    patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": state}):
                guard = owned.Watchdog(
                    "bounded-runtime-control", target,
                    probe=lambda: sample,
                    reclaim=lambda _: "no retained workers in this control",
                    order=lambda: [{"label": "bounded-runtime-control"}],
                    terminate=functools.partial(owned._terminate_process_group, timeout=0.1),
                    interval=0.02, grace=0.03,
                )
                guard.start()
                target.wait(timeout=5)
                guard.stop()
            self.assertTrue(guard.retracted)
            self.assertTrue(guard.cleanup_proved)
            self.assertEqual(target.returncode, -signal.SIGKILL)
            self.assertIsNone(foreign.poll())
            self.assertIs(owned._process_group_alive(target.pid), False)
        finally:
            if guard is not None:
                guard.stop()
            for proc in processes:
                owned._terminate_process_group(proc, timeout=0.1)
                if proc.stdout is not None:
                    proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
