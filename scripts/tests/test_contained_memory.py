from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from creme import build_ownership
from creme.host_build_broker import render_contained_build_broker
from creme.host_workflow_broker import render_workflow_source


class ContainedMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The generated brokers bake in `creme_root.resolve()`; on macOS the
        # temporary directory is under the /var -> /private/var symlink.
        self.root = Path(self.tmp.name).resolve()
        self.creme = self.root / "creme"
        (self.root / "codex").mkdir(mode=0o700)
        recipes = {"version": 1, "repositories": {"blanc": str(self.root / "blanc"), "jaune": str(self.root / "jaune")},
                   "operations": {"gate": {"profile": "blanc", "memory_gib": 4, "modes": {
                       "check": {"argv": ["/usr/bin/python3", "{repo}/scripts/check.py"], "env": {}}}}}}
        self.ns = {"__name__": "fixture", "__file__": str(self.root / "codex/bin/workflow")}
        exec(render_workflow_source(self.creme, ("fixture", "fixture", "0" * 64), json.dumps(recipes).encode()), self.ns)

    def refused(self, fn):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            fn()
        self.assertEqual(error.exception.code, 2)

    def test_build_split_and_threads_forwarded_with_probe_constraints(self):
        parsed = self.ns["parse"](["blanc", "goal", "--walk", "--threads", "64", "--", "Blanc.X"])
        self.assertEqual(parsed[-3:], (True, 64, ["Blanc.X"]))
        command = self.ns["build_command"]("goal", False, 30, True, ["Blanc.X"], walk=True, threads=64)
        self.assertEqual(command[2:], ["goal", "--wait", "30", "--contention", "exclusive", "--walk", "--threads", "64", "--", "Blanc.X"])
        for value in ["0", "65", "-1", "1.0", "true", "", "1;id", "9" * 5000]:
            self.refused(lambda: self.ns["parse"](["blanc", "goal", "--threads", value, "--"]))
        self.refused(lambda: self.ns["parse"](["blanc", "goal", "--walk", "--probe", "--wait", "1", "--"]))
        self.refused(lambda: self.ns["parse"](["blanc", "goal", "--memory-gib", "12", "--"]))

    def test_generated_cap_matches_service_and_shared_slice(self):
        with mock.patch("creme.host_build_broker.load_containment_memory_max", return_value=12):
            source = render_contained_build_broker(self.creme, "fixture", "fixture", "0" * 64)
        namespace = {"__name__": "fixture", "__file__": str(self.root / "broker")}
        exec(source, namespace)
        self.assertEqual(namespace["MEMORY_MAX_GIB"], 12)
        relative = "/user.slice/creme.slice/creme-lean.slice/job.service"
        values = {"memory.high": "max", "memory.max": str(12 * 1024 ** 3),
                  "memory.swap.max": "0", "memory.oom.group": "1"}
        def value(directory, name):
            return values[name]
        with mock.patch.object(Path, "read_text", return_value="0::" + relative), mock.patch.dict(namespace, {"cgroup_value": value}):
            namespace["require_containment"]("blanc")
            values["memory.max"] = str(8 * 1024 ** 3)
            self.refused(lambda: namespace["require_containment"]("blanc"))
        def mismatched_slice(directory, name):
            return str(8 * 1024 ** 3) if directory.name == "creme-lean.slice" else {**values, "memory.max": str(12 * 1024 ** 3)}[name]
        with mock.patch.object(Path, "read_text", return_value="0::" + relative), mock.patch.dict(namespace, {"cgroup_value": mismatched_slice}):
            self.refused(lambda: namespace["require_containment"]("blanc"))

    def test_workflow_retracts_actual_group_under_current_critical_signal(self):
        real_watchdog = build_ownership.Watchdog
        pressure = SimpleNamespace(status="OK", data={"memory_available_direct": True,
                                   "memory_available_bytes": 1024 ** 3, "memory_free_percent": 6})
        def watched(*args, **kwargs):
            kwargs.update(probe=lambda: pressure, reclaim=lambda goal: "test reclaim",
                          interval=0.01, grace=0.02, reclaim_async=False)
            return real_watchdog(*args, **kwargs)
        descriptor = self.ns["broker_lock"]()
        owner = "workflow-" + "a" * 32
        try:
            with mock.patch.object(build_ownership, "Watchdog", side_effect=watched), mock.patch.object(build_ownership.semaphore, "record_retraction") as record, mock.patch.dict(self.ns, {"workflow_peak_gib": lambda: 0.01}):
                result = self.ns["workflow_run_command"]([sys.executable, "-c", "import time; time.sleep(30)"], self.root, dict(os.environ), descriptor, owner, "goal")
            record.assert_called_once()
            self.assertEqual(record.call_args.args[0], owner)
            self.assertEqual(result["exit_code"], 75)
            self.assertTrue(result["cleanup_proved"])
            self.assertEqual(result["evidence_status"], "RETRACTED")
            self.assertEqual(result["peak_gib"], 0.01)
            self.assertLessEqual(result["min_available_gib"], 1)
        finally:
            os.close(descriptor)

    def test_exited_leader_keeps_group_watched_and_surviving_children_are_cleaned(self):
        proc = mock.Mock(pid=12345)
        proc.wait.return_value = proc.poll.return_value = 0
        watchdog = mock.Mock(retracted=False, cleanup_proved=True,
                             events=[], min_available_gib=None)
        cleanup_order = []
        watchdog.stop.side_effect = lambda: cleanup_order.append("watchdog-stop")
        def cleanup(group, timeout):
            cleanup_order.append("group-cleanup")
            return True
        def watched(owner, group, **kwargs):
            # The leader exited, but observed group liveness keeps the monitor
            # eligible to retract its children until cleanup runs.
            self.assertIsNone(group.poll())
            return watchdog
        with mock.patch.object(subprocess, "Popen", return_value=proc) as spawn, mock.patch.object(build_ownership, "Watchdog", side_effect=watched), mock.patch.object(build_ownership, "_process_group_alive", return_value=True), mock.patch.object(build_ownership, "_terminate_process_group", side_effect=cleanup) as terminate, mock.patch.dict(self.ns, {"workflow_peak_gib": lambda: None}):
            result = self.ns["workflow_run_command"](["fixture"], self.root, {}, 99, "owner", "goal")
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(spawn.call_args.kwargs["pass_fds"], (99,))
        terminate.assert_called_once_with(proc, timeout=1.0)
        self.assertEqual(cleanup_order, ["group-cleanup", "watchdog-stop"])
        self.assertTrue(result["cleanup_proved"])
        self.assertEqual(result["exit_code"], 0)

    def test_descendant_cleanup_and_final_watchdog_failure_both_preserve_hold(self):
        proc = mock.Mock(pid=12345)
        proc.wait.return_value = proc.poll.return_value = 0
        for group_cleanup, watchdog_cleanup in [(False, True), (True, False)]:
            with self.subTest(group_cleanup=group_cleanup, watchdog_cleanup=watchdog_cleanup):
                watchdog = mock.Mock(retracted=False, cleanup_proved=True,
                                     events=[], min_available_gib=None)
                watchdog.stop.side_effect = lambda: setattr(watchdog, "cleanup_proved", watchdog_cleanup)
                with mock.patch.object(subprocess, "Popen", return_value=proc), mock.patch.object(build_ownership, "Watchdog", return_value=watchdog), mock.patch.object(build_ownership, "_process_group_alive", return_value=True), mock.patch.object(build_ownership, "_terminate_process_group", return_value=group_cleanup), mock.patch.dict(self.ns, {"workflow_peak_gib": lambda: None}):
                    result = self.ns["workflow_run_command"](["fixture"], self.root, {}, 99, "owner", "goal")
                self.assertFalse(result["cleanup_proved"])

    def test_unproven_cleanup_preserves_exact_owner_and_release_is_not_called(self):
        repo = self.root / "blanc/.worktrees/goal"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts/check.py").write_text("pass")
        argv = ["blanc", "goal", "gate", "check"]
        overrides = {"require_containment": lambda profile: None, "require_workflow_unit": lambda: None,
                     "workflow_control_plane": lambda: None, "workflow_worktree": lambda *args: repo,
                     "workflow_run_command": lambda *args: {"exit_code": 75, "retracted": True, "cleanup_proved": False}}
        with mock.patch.dict(self.ns, overrides), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.ns["workflow_service"](argv, self.ns["workflow_parse"](argv)), 75)
        self.assertFalse(any("hard-release" in call.args[0] for call in run.call_args_list))
        record = json.loads((self.ns["BROKER_STATE"] / "workflow-last.json").read_text())
        self.assertEqual(record["status"], "RELEASE_FAILED")
        self.assertEqual(record["release_action"], "preserved-unproven-cleanup")
        self.assertEqual(self.ns["validate_interrupted_workflow"](record), record["owner"])

    def test_retracted_service_records_observed_failure_and_releases_only_its_owner(self):
        repo = self.root / "blanc/.worktrees/goal"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts/check.py").write_text("pass")
        argv = ["blanc", "goal", "gate", "check"]
        outcome = {"exit_code": 75, "command_exit_code": -15, "retracted": True,
                   "cleanup_proved": True, "evidence_status": "RETRACTED", "peak_gib": 0.01}
        overrides = {"require_containment": lambda profile: None, "require_workflow_unit": lambda: None,
                     "workflow_control_plane": lambda: None, "workflow_worktree": lambda *args: repo,
                     "workflow_run_command": lambda *args: outcome}
        with mock.patch.dict(self.ns, overrides), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.ns["workflow_service"](argv, self.ns["workflow_parse"](argv)), 75)
        record = json.loads((self.ns["BROKER_STATE"] / "workflow-last.json").read_text())
        self.assertEqual(record["status"], "RETRACTED")
        self.assertEqual(record["command_exit_code"], -15)
        self.assertEqual(record["evidence_status"], "RETRACTED")
        release_calls = [call.args[0] for call in run.call_args_list if "hard-release" in call.args[0]]
        self.assertEqual(release_calls, [[str(self.creme / "scripts/creme"), "semaphore", "hard-release", record["owner"]]])
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["reconcile_previous_workflow"]()


if __name__ == "__main__":
    unittest.main()
