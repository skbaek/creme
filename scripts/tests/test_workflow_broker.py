from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from creme.host_workflow_broker import (
    RECIPES_RELATIVE, WORKFLOW_BROKER_NAME, render_workflow_broker, validate_recipes,
)
from creme.host_wrappers import install_host_bundle, bundle_install_issues, render_host_rules


def recipes(root):
    return {"version": 1, "repositories": {"blanc": str(root / "blanc"), "jaune": str(root / "jaune")},
            "operations": {"fixtures": {"profile": "blanc", "memory_gib": 4, "modes": {
                "validate": {"argv": ["/usr/bin/python3", "-B", "{repo}/scripts/gen.py", "--validate-runtime"], "env": {}},
                "write": {"argv": ["/usr/bin/python3", "-B", "{repo}/scripts/gen.py", "--write"], "env": {}},
            }}}}


class WorkflowBrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve()
        self.root = self.path / "creme"
        self.root.mkdir()
        self.config = recipes(self.path)
        recipe = self.root / RECIPES_RELATIVE
        recipe.parent.mkdir()
        recipe.write_text(json.dumps(self.config))
        with mock.patch("creme.host_workflow_broker.broker_inputs", return_value=("fixture", "fixture", "0" * 64)):
            self.code = render_workflow_broker(self.root)
        self.home = self.path / "codex"
        self.home.mkdir(mode=0o700)
        self.ns = {"__name__": "workflow_fixture", "__file__": str(self.home / "bin" / WORKFLOW_BROKER_NAME)}
        exec(compile(self.code, self.ns["__file__"], "exec"), self.ns)
        self.repo = self.path / "blanc/.worktrees/task"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts/gen.py").write_text("pass\n")
        self.args = ["blanc", "task", "fixtures", "validate"]

    def refused(self, call):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            call()
        self.assertEqual(error.exception.code, 2)

    def test_exact_parser_accepts_modes_and_purpose(self):
        self.assertIsNone(self.ns["workflow_parse"](["status"]))
        self.assertEqual(self.ns["workflow_parse"](self.args), ("blanc", "task", "fixtures", "validate", "goal"))
        self.assertEqual(self.ns["workflow_parse"](self.args + ["--purpose", "mutation"])[-1], "mutation")

    def test_parser_rejects_command_path_env_and_resource_injection(self):
        invalid = [[], ["status", "anything"], ["--contained", "status"],
                   ["blanc", "../task", "fixtures", "validate"],
                   ["blanc", "task;id", "fixtures", "validate"],
                   ["jaune", "task", "fixtures", "validate"],
                   ["blanc", "task", "/tmp/script", "validate"],
                   ["blanc", "task", "fixtures", "validate;id"],
                   self.args + ["--", "bash"], self.args + ["--env", "PATH=/tmp"],
                   self.args + ["--memory-gib", "1"], self.args + ["--purpose", "../../x"],
                   self.args + ["--purpose", "goal", "extra"]]
        for argv in invalid:
            with self.subTest(argv=argv):
                self.refused(lambda: self.ns["workflow_parse"](argv))

    def test_recipe_schema_rejects_unreviewable_shapes(self):
        changes = [lambda c: c.update(extra=True),
                   lambda c: c["repositories"].update(blanc="/tmp/../blanc"),
                   lambda c: c["operations"]["fixtures"].update(memory_gib=True),
                   lambda c: c["operations"]["fixtures"]["modes"]["validate"].update(argv=["/usr/bin/bash", "-c", "id"]),
                   lambda c: c["operations"]["fixtures"]["modes"]["validate"].update(argv=["/usr/bin/python3", "-c", "pass"]),
                   lambda c: c["operations"]["fixtures"]["modes"]["validate"].update(argv=["/usr/bin/python3", "{repo}/scripts/../evil.py"]),
                   lambda c: c["operations"]["fixtures"]["modes"]["validate"].update(env={"PYTHONPATH": "/tmp"}),
                   lambda c: c["operations"]["fixtures"]["modes"]["validate"].update(env={"PATH": "{unknown}"})]
        for change in changes:
            candidate = copy.deepcopy(self.config)
            change(candidate)
            with self.assertRaises(ValueError):
                validate_recipes(candidate)

    def test_recipe_pins_fail_before_launch(self):
        with mock.patch.dict(self.ns, {"require_control_plane": lambda: None}):
            self.ns["workflow_control_plane"]()
            (self.root / RECIPES_RELATIVE).write_text("{}")
            self.refused(self.ns["workflow_control_plane"])

    def test_cgroup_requires_exact_profile_memory_and_swap_boundary(self):
        relative = "/user.slice/user-1000.slice/user@1000.service/creme.slice/creme-lean.slice/unit.service"
        for profile, swap in [("blanc", "0"), ("jaune", str(1024 ** 3))]:
            values = {"memory.high": "max", "memory.max": str(8 * 1024 ** 3),
                      "memory.swap.max": swap, "memory.oom.group": "1"}
            with mock.patch.object(Path, "read_text", return_value="0::" + relative), mock.patch.dict(self.ns, {
                "cgroup_value": lambda directory, name: values[name],
            }):
                self.ns["require_containment"](profile)
                values["memory.swap.max"] = str(2 * 1024 ** 3)
                self.refused(lambda: self.ns["require_containment"](profile))
                values["memory.swap.max"] = swap
                values["memory.oom.group"] = "0"
                self.refused(lambda: self.ns["require_containment"](profile))
        with mock.patch.object(Path, "read_text", return_value="0::/ordinary.service"):
            self.refused(lambda: self.ns["require_containment"]("blanc"))

    def test_linked_recipe_is_refused(self):
        target = self.path / "other.json"
        target.write_text(json.dumps(self.config))
        (self.root / RECIPES_RELATIVE).unlink()
        (self.root / RECIPES_RELATIVE).symlink_to(target)
        with self.assertRaises(ValueError):
            render_workflow_broker(self.root)

    def test_script_link_and_parent_link_are_refused(self):
        script = self.repo / "scripts/gen.py"
        script.unlink()
        script.symlink_to(self.path / "outside.py")
        self.refused(lambda: self.ns["workflow_command"](self.repo, "fixtures", "validate"))
        script.unlink()
        script.parent.rmdir()
        (self.path / "outside-scripts").mkdir()
        script.parent.symlink_to(self.path / "outside-scripts", target_is_directory=True)
        self.refused(lambda: self.ns["workflow_command"](self.repo, "fixtures", "validate"))

    def test_worktree_root_and_git_identity_checks_are_preserved(self):
        (self.repo / "lakefile.lean").write_text("")
        (self.repo / "Blanc").mkdir()
        with mock.patch.dict(self.ns, {"run_text": mock.Mock(side_effect=[str(self.repo), str(self.path / "wrong.git")])}):
            self.refused(lambda: self.ns["workflow_worktree"]("blanc", "task", "goal"))
        with mock.patch.dict(self.ns, {"run_text": mock.Mock(side_effect=[str(self.repo), str(self.path / "blanc/.git")])}):
            self.assertEqual(self.ns["workflow_worktree"]("blanc", "task", "goal"), self.repo)

    def test_environment_is_fixed_and_cache_canonical(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/evil", "LAKE_CACHE_DIR": "/other", "BASH_ENV": "/evil"}):
            command, environment = self.ns["workflow_command"](self.repo, "fixtures", "validate")
        self.assertEqual(command, ["/usr/bin/python3", "-B", str(self.repo / "scripts/gen.py"), "--validate-runtime"])
        self.assertEqual(environment["LAKE_CACHE_DIR"], str(self.root / ".creme/lake-cache"))
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("BASH_ENV", environment)

    def service(self, runner):
        with mock.patch.dict(self.ns, {
            "require_containment": mock.Mock(), "workflow_control_plane": lambda: None,
            "workflow_worktree": lambda *args: self.repo,
        }), mock.patch.object(self.ns["subprocess"], "run", side_effect=runner), contextlib.redirect_stdout(io.StringIO()):
            return self.ns["workflow_service"](self.args, self.ns["workflow_parse"](self.args))

    def test_service_owns_lock_through_command_and_releases_after_exit(self):
        calls = []
        inherited = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "/usr/bin/python3":
                descriptor, = kwargs["pass_fds"]
                self.assertTrue(os.get_inheritable(descriptor))
                self.refused(self.ns["broker_lock"])
                inherited.append(os.dup(descriptor))
            return subprocess.CompletedProcess(argv, 0)
        self.assertEqual(self.service(run), 0)
        self.assertIn("adaptive-acquire", calls[1])
        self.assertIn("exclusive", calls[1])
        self.assertIn("hard-release", calls[-1])
        # Model a surviving child after its service supervisor has disappeared:
        # the inherited file description continues excluding all broker starts.
        self.refused(self.ns["broker_lock"])
        os.close(inherited[0])
        descriptor = self.ns["broker_lock"]()
        os.close(descriptor)
        record = json.loads((self.ns["BROKER_STATE"] / "workflow-last.json").read_text())
        self.assertEqual(record["status"], "TERMINAL")

    def test_lost_supervisor_preserves_hold_without_false_terminal(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "/usr/bin/python3":
                raise KeyboardInterrupt()
            return subprocess.CompletedProcess(argv, 0)
        with self.assertRaises(KeyboardInterrupt):
            self.service(run)
        self.assertFalse(any("hard-release" in command for command in calls))
        record = json.loads((self.ns["BROKER_STATE"] / "workflow-last.json").read_text())
        self.assertEqual(record["status"], "RUNNING")

    def test_interruption_after_admission_preserves_precommitted_owner(self):
        write_record = self.ns["workflow_record"]
        calls = []
        def record(payload):
            if payload["status"] == "RUNNING":
                raise KeyboardInterrupt()
            write_record(payload)
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.dict(self.ns, {"workflow_record": record}), self.assertRaises(KeyboardInterrupt):
            self.service(run)
        last = json.loads((self.ns["BROKER_STATE"] / "workflow-last.json").read_text())
        self.assertEqual(last["status"], "ADMITTING")
        self.assertIn(last["owner"], calls[-1])
        self.assertIn("adaptive-acquire", calls[-1])
        self.assertFalse(any("hard-release" in command for command in calls))

    def test_retry_preserves_unresolved_owner_and_starts_nothing(self):
        descriptor = self.ns["broker_lock"]()
        os.close(descriptor)
        for status in ["ADMITTING", "RUNNING", "RELEASE_FAILED"]:
            previous = {"status": status, "owner": "workflow-preserve"}
            path = self.ns["BROKER_STATE"] / "workflow-last.json"
            path.write_text(json.dumps(previous))
            run = mock.Mock(side_effect=AssertionError("must refuse before any subprocess"))
            self.refused(lambda: self.service(run))
            run.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), previous)

    def test_missing_or_stale_certificate_refuses_before_gate_and_current_proceeds(self):
        (self.repo / "scripts/gate-cache.py").write_text("")
        self.ns["RECIPES"]["operations"]["fixtures"]["guard"] = "blanc-build-certificate"
        for code in [2, 1, 0]:
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, code if "-c" in argv else 0)
            self.assertEqual(self.service(run), code)
            self.assertIn("-c", calls[1])
            if code:
                self.assertEqual(len(calls), 2)
            else:
                self.assertIn("adaptive-acquire", calls[2])
                self.assertIn("--validate-runtime", calls[3])

    def test_status_missing_service_with_pending_owner_is_unknown(self):
        descriptor = self.ns["broker_lock"]()
        os.close(descriptor)
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["workflow_record"]({"status": "ADMITTING", "owner": "workflow-test"})
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "LoadState=not-found\n") if "show" in argv else subprocess.CompletedProcess(argv, 0, "")
        with mock.patch.dict(self.ns, {"workflow_control_plane": lambda: None, "regular_path": lambda *a, **k: None}), mock.patch.object(self.ns["subprocess"], "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.ns["main"](["status"]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "UNKNOWN")

    def test_preflight_and_admission_refusals_never_execute_recipe(self):
        for failure_at in [0, 1]:
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 2 if len(calls) - 1 == failure_at else 0)
            self.assertEqual(self.service(run), 2)
            self.assertEqual(len(calls), failure_at + 1)

    def test_outer_only_launches_fixed_unit_no_lock_or_hold(self):
        with mock.patch.dict(self.ns, {
            "workflow_control_plane": lambda: None, "workflow_worktree": lambda *args: self.repo,
            "broker_lock": mock.Mock(side_effect=AssertionError("outer must not own lock")),
        }), mock.patch.object(self.ns["subprocess"], "run", return_value=subprocess.CompletedProcess([], 0)) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.ns["main"](self.args), 0)
            argv = run.call_args.args[0]
            for option in ["--unit=creme-contained-workflow.service", "--property=MemoryMax=8G",
                           "--property=MemorySwapMax=0", "--property=KillMode=control-group"]:
                self.assertIn(option, argv)
            self.assertEqual(argv[-5:], ["--contained", *self.args])

    def test_status_is_narrow_read_only_and_survives_circuit_breaker(self):
        (self.root / ".creme/lean-heavy-suspended").touch()
        with mock.patch.dict(self.ns, {"workflow_control_plane": lambda: None, "regular_path": lambda *a, **k: None}), mock.patch.object(self.ns["subprocess"], "run", return_value=subprocess.CompletedProcess([], 0, "LoadState=not-found\n")) as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.ns["main"](["status"]), 0)
            self.assertEqual(run.call_count, 4)
            self.assertIn("systemctl", run.call_args_list[0].args[0][0])
            self.assertEqual(run.call_args_list[1].args[0][1:], ["telemetry"])
            self.assertEqual(run.call_args_list[2].args[0][1:], ["semaphore", "status"])
            self.assertIn("run-*", run.call_args_list[3].args[0])
            self.assertIsNone(json.loads(output.getvalue())["last_record"])
        self.assertFalse((self.home / "state").exists())

    def test_bundle_installs_and_validates_workflow_and_rule_together(self):
        output, rules = self.home / "bin", self.home / "rules"
        with mock.patch("creme.host_wrappers.broker_inputs", return_value=None), mock.patch("creme.host_workflow_broker.broker_inputs", return_value=("fixture", "fixture", "0" * 64)), mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            paths = install_host_bundle(self.root, output, rules, replace=False)
            self.assertIn(output / WORKFLOW_BROKER_NAME, paths)
            self.assertEqual(bundle_install_issues(self.root, output, rules), [])
            (output / WORKFLOW_BROKER_NAME).write_text("changed")
            self.assertTrue(any(WORKFLOW_BROKER_NAME in issue for issue in bundle_install_issues(self.root, output, rules)))
        rule = render_host_rules(output, include_workflow=True)
        self.assertEqual(rule.count("prefix_rule("), 4)
        self.assertNotIn("lean-safe-run", rule)


if __name__ == "__main__":
    unittest.main()
