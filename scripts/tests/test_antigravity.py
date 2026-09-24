from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from creme import antigravity


ROOT = Path(__file__).resolve().parents[2]


class AntigravityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / "target"
        self.target.mkdir()
        self.scenario_path = self.root / "scenario.json"
        self.settings_path = self.root / "settings.json"
        self.binary = self.root / "agy-fake"
        self._write_fake()
        self.old_env = dict(os.environ)
        os.environ["CREME_AGY_BIN"] = str(self.binary)
        os.environ["CREME_AGY_SETTINGS"] = str(self.settings_path)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def _write_fake(self) -> None:
        source = '''#!/usr/bin/env python3
import json, os, shlex, subprocess, sys
from pathlib import Path
scenario = json.loads(Path(os.environ["FAKE_AGY_SCENARIO"]).read_text())
prompt = sys.argv[sys.argv.index("-p") + 1]
if prompt == "/usage":
    print(json.dumps(scenario["usage"]))
elif prompt == "/credits":
    print(json.dumps(scenario["credits"]))
else:
    additions = sys.argv[sys.argv.index("--add-dir") + 1:]
    dirs = [Path(value) for value in additions if (Path(value) / ".agents" / "hooks.json").exists()]
    run_dir = dirs[0]
    hooks = json.loads((run_dir / ".agents" / "hooks.json").read_text())
    command = hooks["creme-run-guard"]["PreToolUse"][0]["hooks"][0]["command"]
    for payload in scenario.get("payloads", []):
        subprocess.run(shlex.split(command), cwd=run_dir / ".agents",
                       input=json.dumps(payload), text=True, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if scenario.get("write_target"):
        Path("leak.txt").write_text("written although the run was read-only")
    init_model = scenario.get("init_model") or sys.argv[sys.argv.index("--model") + 1]
    print(json.dumps({"event": "init", "conversation_id": "conv-1",
                      "init": {"model": init_model, "cwd": os.getcwd(), "tools": [],
                               "permission_mode": "headless"}}))
    print(json.dumps({"event": "step_update", "step_update": {"usage": {}}}))
    print(json.dumps({"event": "result", "result": {
        "conversation_id": "conv-1", "status": scenario.get("status", "SUCCESS"),
        "response": "fake final message", "num_turns": 1,
        "usage": {"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 3,
                   "cache_read_tokens": 4, "total_tokens": 10}}}))
'''
        self.binary.write_text(source, encoding="utf-8")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)

    def _scenario(self, *, credits=0, use_g1=None, fraction=0.8, init_model=None, payloads=None, write_target=False):
        usage = {"command": {"data": {"groups": [
            {"name": "Gemini Models", "buckets": [
                {"id": "gemini-5h", "window": "5h", "remaining_fraction": fraction, "reset_time": "2030-01-01T00:00:00Z"},
                {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.9, "reset_time": "2030-01-02T00:00:00Z"},
            ]},
            {"name": "Claude and GPT models", "buckets": [
                {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.9, "reset_time": "2030-01-01T00:00:00Z"},
                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.9, "reset_time": "2030-01-02T00:00:00Z"},
            ]},
        ]}}}
        self.scenario_path.write_text(json.dumps({
            "usage": usage, "credits": {"command": {"data": {"remaining_credits": credits}}},
            "init_model": init_model, "payloads": payloads or [], "write_target": write_target,
        }), encoding="utf-8")
        os.environ["FAKE_AGY_SCENARIO"] = str(self.scenario_path)
        if use_g1 is None:
            self.settings_path.write_text(json.dumps({}), encoding="utf-8")
        else:
            self.settings_path.write_text(json.dumps({"useG1Credits": use_g1}), encoding="utf-8")

    def test_pool_for_model_mapping(self) -> None:
        self.assertEqual(antigravity.pool_for_model("gemini-3-pro"), "gemini")
        self.assertEqual(antigravity.pool_for_model("claude-sonnet"), "3p")
        self.assertEqual(antigravity.pool_for_model("gpt-oss-20b"), "3p")
        with self.assertRaises(ValueError):
            antigravity.pool_for_model("mystery-model")

    def test_resolve_model_combines_family_and_effort(self) -> None:
        self.assertEqual(antigravity.resolve_model("gemini-3.8-flash", "medium"), "gemini-3.8-flash-medium")
        self.assertEqual(antigravity.resolve_model("gemini-3.8-flash-high", "high"), "gemini-3.8-flash-high")
        with self.assertRaisesRegex(ValueError, "gemini-3.8-flash-high.*medium"):
            antigravity.resolve_model("gemini-3.8-flash-high", "medium")

    def test_admission_credits_requires_explicit_false(self) -> None:
        quota = {"pools": {"gemini": {"5h": {"remaining_fraction": .8}, "weekly": {"remaining_fraction": .8}}},
                 "remaining_credits": 2, "use_g1_credits": None}
        self.assertTrue(antigravity.admission(quota, "gemini-model", .05))
        quota["use_g1_credits"] = False
        self.assertEqual(antigravity.admission(quota, "gemini-model", .05), [])
        quota["remaining_credits"] = 0
        quota["use_g1_credits"] = None
        self.assertEqual(antigravity.admission(quota, "gemini-model", .05), [])

    def test_admission_only_selected_pool_floor_matters(self) -> None:
        quota = {"pools": {
            "gemini": {"5h": {"remaining_fraction": .04}, "weekly": {"remaining_fraction": .8}},
            "3p": {"5h": {"remaining_fraction": .01}, "weekly": {"remaining_fraction": .8}},
        }, "remaining_credits": 0, "use_g1_credits": False}
        reasons = antigravity.admission(quota, "gemini-model", .05)
        self.assertEqual(len(reasons), 1)
        self.assertIn("gemini 5h", reasons[0])

    def test_guard_decision(self) -> None:
        self.assertEqual(antigravity.guard_decision({"modelName": "gemini-model", "toolCall": {"name": "view_file"}}, "gemini-model")["decision"], "allow")
        self.assertEqual(antigravity.guard_decision({"modelName": "gemini-model", "toolCall": {"name": "run_command"}}, "gemini-model")["decision"], "deny")
        self.assertEqual(antigravity.guard_decision({"modelName": "gemini-model", "toolCall": {"name": "write_to_file"}}, "gemini-model")["decision"], "deny")
        mismatch = antigravity.guard_decision({"modelName": "other", "toolCall": {"name": "view_file"}}, "gemini-model")
        self.assertEqual(mismatch["decision"], "deny")
        self.assertIn("other", mismatch["reason"])
        self.assertIn("gemini-model", mismatch["reason"])

    def test_guard_relative_path_resolves_against_target(self) -> None:
        roots = [str(self.target)]
        inside = {"modelName": "m", "toolCall": {"name": "view_file", "args": {"Path": "sub/file.lean"}}}
        escape = {"modelName": "m", "toolCall": {"name": "view_file", "args": {"Path": "../outside.txt"}}}
        self.assertEqual(antigravity.guard_decision(inside, "m", roots)["decision"], "allow")
        self.assertEqual(antigravity.guard_decision(escape, "m", roots)["decision"], "deny")

    def test_guard_script_agrees_with_function(self) -> None:
        agents = self.root / "agents"
        agents.mkdir()
        (agents / "guard.py").write_text(antigravity.GUARD_SCRIPT, encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.target / "outside-link"
        link.symlink_to(outside)
        root = str(self.target.resolve())
        (agents / "guard.json").write_text(
            json.dumps({"pinned_model": "gemini-model", "roots": [root]}), encoding="utf-8"
        )
        payloads = [
            {"modelName": "gemini-model", "toolCall": {"name": "view_file", "args": {"AbsolutePath": str(self.target / "inside")}}},
            {"modelName": "gemini-model", "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/etc/hosts"}}},
            {"modelName": "gemini-model", "toolCall": {"name": "view_file", "args": {"Path": "~/outside"}}},
            {"modelName": "gemini-model", "toolCall": {"name": "view_file", "args": {"FilePath": str(link)}}},
            {"modelName": "gemini-model", "toolCall": {"name": "grep_search", "args": {"SearchPath": "/etc"}}},
            {"modelName": "gemini-model", "toolCall": {"name": "run_command"}},
            {"modelName": "gemini-model", "toolCall": {"name": "write_to_file"}},
            {"modelName": "other", "toolCall": {"name": "view_file"}},
        ]
        for payload in payloads:
            result = subprocess.run(
                [sys.executable, "./guard.py"], cwd=agents, input=json.dumps(payload),
                text=True, check=True, stdout=subprocess.PIPE,
            )
            self.assertEqual(
                json.loads(result.stdout), antigravity.guard_decision(payload, "gemini-model", (root,))
            )

    def test_run_pass_records_and_counts_guard_events(self) -> None:
        self._scenario(payloads=[
            {"modelName": "gemini-model-medium", "toolCall": {"name": "view_file"}},
            {"modelName": "gemini-model-medium", "toolCall": {"name": "run_command"}},
        ])
        code, summary = antigravity.run("inspect", self.target, "gemini-model", "medium", 10,
                                       runs_root=self.root / "runs")
        self.assertEqual(code, antigravity.EXIT_OK)
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["family"], "gemini-model")
        self.assertEqual(summary["model"], "gemini-model-medium")
        self.assertEqual(summary["tool_calls"], 2)
        self.assertEqual(summary["denied"], 1)
        run_dir = Path(summary["run_dir"])
        for name in ("brief.md", "events.jsonl", "agy.stderr", "last-message.md", "usage-before.json", "usage-after.json", "verdict.json", "payloads.jsonl"):
            self.assertTrue((run_dir / name).exists(), name)

    def test_run_fails_when_init_model_differs(self) -> None:
        self._scenario(init_model="other-model")
        code, summary = antigravity.run("inspect", self.target, "gemini-model", "medium", 10,
                                       runs_root=self.root / "runs")
        self.assertEqual(code, antigravity.EXIT_FAILED)
        self.assertEqual(summary["verdict"], "FAIL")
        self.assertIn("init model", " ".join(summary["reasons"]))

    def test_run_fails_when_payload_model_differs(self) -> None:
        self._scenario(payloads=[{"modelName": "other-model", "toolCall": {"name": "view_file"}}])
        code, summary = antigravity.run("inspect", self.target, "gemini-model", "medium", 10,
                                       runs_root=self.root / "runs")
        self.assertEqual(code, antigravity.EXIT_FAILED)
        self.assertIn("payload modelName", " ".join(summary["reasons"]))

    def _git_target(self) -> None:
        for args in (["init", "-q"], ["-c", "user.email=t@example.invalid", "-c", "user.name=t",
                                      "commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.target), *args], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_run_passes_on_unchanged_git_target(self) -> None:
        self._git_target()
        self._scenario()
        code, summary = antigravity.run("inspect", self.target, "gemini-model", "medium", 10,
                                       runs_root=self.root / "runs")
        self.assertEqual(code, antigravity.EXIT_OK, summary)
        self.assertEqual(summary["warnings"], [])

    def test_run_fails_when_git_target_changes(self) -> None:
        # The guard is bypassed here (as if its hook had not loaded); the state check must catch the write.
        self._git_target()
        self._scenario(write_target=True)
        code, summary = antigravity.run("inspect", self.target, "gemini-model", "medium", 10,
                                       runs_root=self.root / "runs")
        self.assertEqual(code, antigravity.EXIT_FAILED)
        self.assertIn("target Git state changed", " ".join(summary["reasons"]))

    def test_run_records_inside_git_target_are_not_a_change(self) -> None:
        self._git_target()
        self._scenario()
        code, summary = antigravity.run("inspect", self.target, "gemini-model", "medium", 10,
                                       runs_root=self.target / ".creme" / "runs")
        self.assertEqual(code, antigravity.EXIT_OK, summary)

    def test_run_refused_without_run_directory(self) -> None:
        self._scenario(fraction=.01)
        runs = self.root / "runs"
        code, summary = antigravity.run("inspect", self.target, "gemini-model", "medium", 10,
                                       runs_root=runs)
        self.assertEqual(code, antigravity.EXIT_PREFLIGHT_REFUSED)
        self.assertEqual(summary["verdict"], "REFUSED")
        self.assertFalse(runs.exists())

    def test_cli_antigravity_run_has_no_write_option(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "creme", "antigravity", "run", "--brief", "-",
             "--target", str(self.target), "--model", "gemini-model", "--write"],
            cwd=ROOT, input="inspect", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
