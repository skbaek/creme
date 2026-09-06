from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from creme.host_workflow_broker import RECIPES_RELATIVE, WORKFLOW_BROKER_NAME, render_workflow_broker


OWNER = "workflow-0123456789abcdef0123456789abcdef"


def recipes(root: Path) -> dict:
    return {
        "version": 1,
        "repositories": {"blanc": str(root / "blanc"), "jaune": str(root / "jaune")},
        "operations": {
            "fixtures": {
                "profile": "blanc",
                "memory_gib": 4,
                "modes": {
                    "validate": {
                        "argv": [
                            "/usr/bin/python3", "-B", "{repo}/scripts/gen.py", "--validate-runtime",
                        ],
                        "env": {},
                    },
                    "write": {
                        "argv": ["/usr/bin/python3", "-B", "{repo}/scripts/gen.py", "--write"],
                        "env": {},
                    },
                },
            }
        },
    }


class WorkflowRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve()
        self.root = self.path / "creme"
        self.root.mkdir()
        recipe_path = self.root / RECIPES_RELATIVE
        recipe_path.parent.mkdir()
        recipe_path.write_text(json.dumps(recipes(self.path)))
        with mock.patch(
            "creme.host_workflow_broker.broker_inputs",
            return_value=("fixture", "fixture", "0" * 64),
        ):
            code = render_workflow_broker(self.root)
        self.home = self.path / "codex"
        self.home.mkdir(mode=0o700)
        self.ns = {
            "__name__": "workflow_recovery_fixture",
            "__file__": str(self.home / "bin" / WORKFLOW_BROKER_NAME),
        }
        exec(compile(code, self.ns["__file__"], "exec"), self.ns)
        self.repo = self.path / "blanc/.worktrees/task"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts/gen.py").write_text("pass\n")
        self.args = ["blanc", "task", "fixtures", "validate"]
        descriptor = self.ns["broker_lock"]()
        os.close(descriptor)

    @property
    def last_path(self) -> Path:
        return self.ns["BROKER_STATE"] / "workflow-last.json"

    @property
    def recovery_path(self) -> Path:
        return self.ns["workflow_recovery_path"](OWNER)

    def valid_previous(self, status: str = "RUNNING") -> dict:
        payload = {
            "status": status,
            "unit": self.ns["UNIT"],
            "owner": OWNER,
            "profile": "blanc",
            "goal": "task",
            "operation": "fixtures",
            "mode": "validate",
            "argv": [
                "/usr/bin/python3", "-B", str(self.repo / "scripts/gen.py"), "--validate-runtime",
            ],
            "recipes_sha256": self.ns["RECIPES_SHA256"],
        }
        if status == "RELEASE_FAILED":
            payload.update(exit_code=0, release_exit_code=1)
        return payload

    def write_last(self, payload: dict) -> None:
        self.last_path.write_text(json.dumps(payload))

    def refused(self, call) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            call()
        self.assertEqual(error.exception.code, 2)

    def service(self, runner, extra: dict | None = None) -> int:
        patches = {
            "require_containment": mock.Mock(),
            "require_workflow_unit": mock.Mock(),
            "workflow_control_plane": lambda: None,
            "workflow_worktree": lambda *args: self.repo,
        }
        patches.update(extra or {})
        with mock.patch.dict(self.ns, patches), mock.patch.object(
            self.ns["subprocess"], "run", side_effect=runner,
        ), contextlib.redirect_stdout(io.StringIO()):
            return self.ns["workflow_service"](self.args, self.ns["workflow_parse"](self.args))

    def successful_runner(self, recovery_code: int, recovery_stdout: str, calls: list) -> object:
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if "hard-release" in argv and argv[-1] == OWNER:
                return subprocess.CompletedProcess(argv, recovery_code, recovery_stdout, "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run

    def test_exact_workflow_unit_is_required_even_inside_matching_slice(self) -> None:
        accepted = (
            "0::/user.slice/user-1000.slice/user@1000.service/"
            "creme.slice/creme-lean.slice/creme-contained-workflow.service"
        )
        with mock.patch.object(Path, "read_text", return_value=accepted):
            self.ns["require_workflow_unit"]()
        for relative in [
            "0::/user.slice/creme.slice/creme-lean.slice/other.service",
            "0::/user.slice/creme.slice/creme-lean.slice/creme-contained-workflow.service/child.scope",
            "0::/creme-contained-workflow.service-other",
        ]:
            with self.subTest(relative=relative), mock.patch.object(Path, "read_text", return_value=relative):
                self.refused(self.ns["require_workflow_unit"])

    def test_recovery_runs_new_explicit_mode_instead_of_replaying_old_mode(self) -> None:
        previous = self.valid_previous()
        previous["mode"] = "write"
        previous["argv"] = ["/usr/bin/python3", "-B", str(self.repo / "scripts/gen.py"), "--write"]
        self.write_last(previous)
        calls: list = []
        self.assertEqual(self.service(self.successful_runner(
            1, "REFUSED — matching hard hold not found\n", calls,
        )), 0)
        recipe_calls = [argv for argv, _ in calls if argv and argv[0] == "/usr/bin/python3"]
        self.assertEqual(len(recipe_calls), 1)
        self.assertIn("--validate-runtime", recipe_calls[0])
        self.assertNotIn("--write", recipe_calls[0])

    def test_recovery_control_bites_before_release_and_only_exact_unit_restores_path(self) -> None:
        self.write_last(self.valid_previous("RUNNING"))
        calls: list = []
        runner = self.successful_runner(0, "OK — hard hold released\n", calls)
        original_read_text = Path.read_text
        def invoke(cgroup):
            def read_text(path, *args, **kwargs):
                if str(path) == "/proc/self/cgroup":
                    return cgroup
                return original_read_text(path, *args, **kwargs)
            with mock.patch.dict(self.ns, {
                "require_containment": mock.Mock(),
                "workflow_control_plane": lambda: None,
                "workflow_worktree": lambda *args: self.repo,
            }), mock.patch.object(Path, "read_text", read_text), mock.patch.object(
                self.ns["subprocess"], "run", side_effect=runner,
            ), contextlib.redirect_stdout(io.StringIO()):
                return self.ns["workflow_service"](
                    self.args, self.ns["workflow_parse"](self.args),
                )
        wrong = "0::/user.slice/creme.slice/creme-lean.slice/not-the-workflow.service"
        self.refused(lambda: invoke(wrong))
        self.assertEqual(calls, [])
        exact = (
            "0::/user.slice/user-1000.slice/user@1000.service/"
            "creme.slice/creme-lean.slice/creme-contained-workflow.service"
        )
        self.assertEqual(invoke(exact), 0)
        self.assertTrue(any("hard-release" in argv and argv[-1] == OWNER for argv, _ in calls))

    def test_admitting_running_and_release_failed_recover_then_launch_new_operation(self) -> None:
        cases = [
            ("ADMITTING", 1, "REFUSED — matching hard hold not found\n", "matching-hard-hold-not-found"),
            ("RUNNING", 0, "OK — hard hold released\n", "released"),
            ("RELEASE_FAILED", 0, "OK — hard hold released\n", "released"),
        ]
        for status, code, stdout, outcome in cases:
            with self.subTest(status=status):
                for path in self.ns["BROKER_STATE"].glob("workflow-*.json"):
                    path.unlink()
                self.write_last(self.valid_previous(status))
                calls: list = []
                self.assertEqual(self.service(self.successful_runner(code, stdout, calls)), 0)
                durable = json.loads(self.recovery_path.read_text())
                self.assertEqual(durable["status"], "RECOVERED_UNKNOWN")
                self.assertEqual(durable["previous_record"]["status"], status)
                self.assertEqual(durable["recovery"]["outcome"], outcome)
                self.assertEqual(json.loads(self.last_path.read_text())["status"], "TERMINAL")
                self.assertTrue(any("--validate-runtime" in argv for argv, _ in calls))

    def test_live_inherited_child_lock_refuses_before_recovery_subprocess(self) -> None:
        self.write_last(self.valid_previous())
        held = self.ns["broker_lock"]()
        try:
            runner = mock.Mock(side_effect=AssertionError("recovery must wait for the inherited lock"))
            self.refused(lambda: self.service(runner))
            runner.assert_not_called()
            self.assertEqual(json.loads(self.last_path.read_text())["status"], "RUNNING")
        finally:
            os.close(held)

    def test_terminal_and_admission_refused_records_keep_normal_retry_path(self) -> None:
        for previous in [
            {**self.valid_previous(), "status": "TERMINAL", "exit_code": 0, "release_exit_code": 0},
            {**self.valid_previous(), "status": "ADMISSION_REFUSED", "exit_code": 2},
        ]:
            with self.subTest(status=previous["status"]):
                self.write_last(previous)
                calls: list = []
                self.assertEqual(self.service(self.successful_runner(
                    0, "OK — hard hold released\n", calls,
                )), 0)
                self.assertFalse(any("hard-release" in argv and argv[-1] == OWNER for argv, _ in calls))
                self.assertTrue(any("--validate-runtime" in argv for argv, _ in calls))

    def test_incomplete_foreign_and_inconsistent_records_remain_refused(self) -> None:
        valid = self.valid_previous()
        records = [
            {"status": "ADMITTING", "owner": "workflow-preserve"},
            {**valid, "status": []},
            {**valid, "unit": "other.service"},
            {**valid, "owner": "task"},
            {**valid, "owner": "workflow-" + "A" * 32},
            {**valid, "owner": "workflow-" + "0" * 31},
            {**valid, "profile": []},
            {**valid, "mode": {}},
            {**valid, "recipes_sha256": "f" * 64},
            {**valid, "argv": ["/usr/bin/python3", "/tmp/other.py"]},
            {key: value for key, value in valid.items() if key != "mode"},
            {**self.valid_previous("RELEASE_FAILED"), "release_exit_code": 0},
        ]
        for previous in records:
            with self.subTest(previous=previous):
                self.write_last(previous)
                runner = mock.Mock(side_effect=AssertionError("invalid metadata must start nothing"))
                self.refused(lambda: self.service(runner))
                runner.assert_not_called()
                self.assertEqual(json.loads(self.last_path.read_text()), previous)

    def test_linked_previous_or_recovery_record_is_preserved_and_refused(self) -> None:
        target = self.ns["BROKER_STATE"] / "preserved.json"
        target.write_text(json.dumps(self.valid_previous()))
        self.last_path.symlink_to(target)
        runner = mock.Mock(side_effect=AssertionError("linked state must start nothing"))
        self.refused(lambda: self.service(runner))
        runner.assert_not_called()
        self.last_path.unlink()
        self.write_last(self.valid_previous())
        self.recovery_path.symlink_to(target)
        self.refused(lambda: self.service(runner))
        runner.assert_not_called()
        self.assertTrue(self.recovery_path.is_symlink())

    def test_unrelated_hold_and_unknown_release_responses_refuse_without_recipe(self) -> None:
        responses = [
            (1, "REFUSED — hard hold workflow-other blocks release\n", ""),
            (2, "", "corrupt semaphore state"),
            (0, "OK — released something\n", ""),
            (1, "REFUSED — matching soft hold not found\n", ""),
        ]
        for code, stdout, stderr in responses:
            with self.subTest(response=(code, stdout, stderr)):
                self.recovery_path.unlink(missing_ok=True)
                previous = self.valid_previous()
                self.write_last(previous)
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, code, stdout, stderr)
                self.refused(lambda: self.service(run))
                self.assertEqual(len(calls), 1)
                self.assertNotIn("--validate-runtime", calls[0])
                self.assertEqual(json.loads(self.last_path.read_text()), previous)
                self.assertFalse(self.recovery_path.exists())

    def test_interrupted_persistence_retry_is_idempotent_and_keeps_first_release_evidence(self) -> None:
        previous = self.valid_previous()
        self.write_last(previous)
        real_record = self.ns["workflow_record"]
        first_calls: list = []
        def interrupt_recovered(payload):
            if payload.get("status") == "RECOVERED_UNKNOWN":
                raise KeyboardInterrupt()
            real_record(payload)
        with self.assertRaises(KeyboardInterrupt):
            self.service(
                self.successful_runner(0, "OK — hard hold released\n", first_calls),
                {"workflow_record": interrupt_recovered},
            )
        durable = json.loads(self.recovery_path.read_text())
        self.assertEqual(durable["recovery"]["outcome"], "released")
        self.assertEqual(json.loads(self.last_path.read_text()), previous)

        second_calls: list = []
        self.assertEqual(self.service(self.successful_runner(
            1, "REFUSED — matching hard hold not found\n", second_calls,
        )), 0)
        self.assertFalse(any("hard-release" in argv and argv[-1] == OWNER for argv, _ in second_calls))
        self.assertEqual(json.loads(self.recovery_path.read_text()), durable)
        self.assertEqual(json.loads(self.last_path.read_text())["status"], "TERMINAL")

    def test_interruption_between_release_and_archive_retries_as_exact_absent_owner(self) -> None:
        previous = self.valid_previous()
        self.write_last(previous)
        real_atomic_record = self.ns["workflow_atomic_record"]
        first_calls: list = []
        def interrupt_archive(path, payload):
            if path == self.recovery_path:
                raise KeyboardInterrupt()
            real_atomic_record(path, payload)
        with self.assertRaises(KeyboardInterrupt):
            self.service(
                self.successful_runner(0, "OK — hard hold released\n", first_calls),
                {"workflow_atomic_record": interrupt_archive},
            )
        self.assertFalse(self.recovery_path.exists())
        self.assertEqual(json.loads(self.last_path.read_text()), previous)

        second_calls: list = []
        self.assertEqual(self.service(self.successful_runner(
            1, "REFUSED — matching hard hold not found\n", second_calls,
        )), 0)
        recovered = json.loads(self.recovery_path.read_text())
        self.assertEqual(recovered["recovery"]["outcome"], "matching-hard-hold-not-found")
        self.assertTrue(any("hard-release" in argv and argv[-1] == OWNER for argv, _ in second_calls))

    def test_recovered_unknown_last_record_requires_matching_durable_evidence(self) -> None:
        previous = self.valid_previous()
        recovered = {
            "status": "RECOVERED_UNKNOWN",
            "unit": self.ns["UNIT"],
            "owner": OWNER,
            "previous_record": previous,
            "recovery": {
                "action": "hard-release",
                "outcome": "released",
                "exit_code": 0,
                "stdout": "OK — hard hold released",
            },
        }
        self.write_last(recovered)
        runner = mock.Mock(side_effect=AssertionError("missing durable evidence must start nothing"))
        self.refused(lambda: self.service(runner))
        runner.assert_not_called()
        self.recovery_path.write_text(json.dumps({**recovered, "unit": "other.service"}))
        self.refused(lambda: self.service(runner))
        runner.assert_not_called()
        for recovery in [
            {"action": "hard-release", "outcome": [], "exit_code": 0,
             "stdout": "OK — hard hold released"},
            {"action": "hard-release", "outcome": "matching-hard-hold-not-found",
             "exit_code": True, "stdout": "REFUSED — matching hard hold not found"},
        ]:
            with self.subTest(recovery=recovery):
                candidate = {**recovered, "recovery": recovery}
                self.write_last(candidate)
                self.recovery_path.write_text(json.dumps(candidate))
                self.refused(lambda: self.service(runner))
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
