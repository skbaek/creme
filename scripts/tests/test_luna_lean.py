from __future__ import annotations

import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import luna_broker as B
from creme import luna_lean as LL
from creme import luna_reserve as L
from creme.cli import main
from creme.codex_app_server import WRITE_PROFILE, GuardedSession, isolation_arguments, isolation_failures

try:
    from test_luna_broker import BrokerHarness
    from test_luna_reserve import ROOT
except ImportError:  # run as scripts.tests.test_luna_lean
    from scripts.tests.test_luna_broker import BrokerHarness
    from scripts.tests.test_luna_reserve import ROOT


TRACKED_TEXT = (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
ALLOWED_TOOLS = (
    "lean_code_actions", "lean_completions", "lean_declaration_file", "lean_diagnostic_messages",
    "lean_file_outline", "lean_get_widget_source", "lean_get_widgets", "lean_goal", "lean_hover_info",
    "lean_local_search", "lean_multi_attempt", "lean_references", "lean_run_code", "lean_term_goal", "lean_verify",
)


def tracked() -> dict:
    return LL.parse_project_config(TRACKED_TEXT)["mcp_servers"]["lean-lsp-mcp"]


def effective(definition: dict) -> dict:
    """What Codex reports for a launched definition: the same keys plus its defaults."""
    return {**json.loads(json.dumps(definition)), "enabled": True, "environment_id": "local",
            "tool_timeout_sec": None}


def startup(name: str, status: str) -> dict:
    return {"method": "mcpServer/startupStatus/updated", "params": {
        "name": name, "status": status, "error": None, "threadId": "t"}}


# ---------------------------------------------------------------------------
# Units


class LeanTargetTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.base = Path(tempfile.mkdtemp(prefix="llt")).resolve()
        self.blanc = self.base / "blanc"
        self.jaune = self.base / "jaune"
        for repository in (self.blanc, self.jaune):
            (repository / ".worktrees").mkdir(parents=True)
        self.worktree = self.worktree_at(self.blanc, "goal-v1")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.base, ignore_errors=True)

    def worktree_at(self, repository: Path, name: str) -> Path:
        path = repository / ".worktrees" / name
        path.mkdir(parents=True)
        (path / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
        return path

    def refusals(self, goal, target):
        return LL.target_refusals(goal, Path(target), (self.jaune, self.blanc))

    def test_only_the_goal_worktree_of_jaune_or_blanc_is_accepted(self):
        self.assertEqual(self.refusals("goal-v1", self.worktree), [])
        for suffix in ("mutation", "control", "rehearsal"):
            disposable = self.worktree_at(self.blanc, f"goal-v1-{suffix}")
            with self.subTest(suffix=suffix):
                self.assertEqual(self.refusals("goal-v1", disposable), [])
        jaune = self.worktree_at(self.jaune, "other-v1")
        self.assertEqual(self.refusals("other-v1", jaune), [])
        other = self.worktree_at(self.blanc, "other-v1")
        unsanctioned = self.worktree_at(self.blanc, "goal-v1-other")
        no_hyphen = self.worktree_at(self.blanc, "goal-v1mutation")
        foreign = self.worktree_at(self.blanc, "OTHERGOAL-mutation")
        (self.worktree / "Blanc").mkdir()
        plain = self.blanc / ".worktrees" / "plain-v1"
        plain.mkdir()
        third = self.worktree_at(self.base / "elsewhere", "goal-v1")
        link = self.blanc / ".worktrees" / "link-v1"
        link.symlink_to(self.worktree)
        for goal, target in (
            ("goal-v1", other),                      # another goal's worktree
            ("goal-v1", self.blanc),                 # the main clone
            ("goal-v1", self.worktree / "Blanc"),    # inside the worktree
            ("plain-v1", plain),                     # not a Git worktree
            ("goal-v1", third),                      # a repository the profile does not name
            ("link-v1", link),                       # a symlinked worktree
            ("goal-v1", unsanctioned),               # unsanctioned suffix
            ("goal-v1", no_hyphen),                  # no hyphen
            ("goal-v1", foreign),                    # different goal
            ("../goal-v1", self.worktree),           # an unsafe label
            ("goal-v1", self.base / "missing"),
        ):
            with self.subTest(goal=goal, target=target):
                self.assertTrue(self.refusals(goal, target))

    def test_sanctioned_disposable_symlink_is_refused(self):
        mutation = self.blanc / ".worktrees" / "goal-v1-mutation"
        mutation.symlink_to(self.worktree)
        self.assertTrue(self.refusals("goal-v1", mutation))

    def test_sanctioned_disposable_directory_must_be_a_git_worktree(self):
        mutation = self.blanc / ".worktrees" / "goal-v1-mutation"
        mutation.mkdir()
        self.assertTrue(self.refusals("goal-v1", mutation))


class LeanDefinitionTest(unittest.TestCase):
    def test_tracked_definition_keeps_its_pins_and_must_match_head(self):
        definition = LL.tracked_definition(ROOT, git=lambda root: TRACKED_TEXT)
        self.assertEqual(definition, tracked())
        self.assertEqual(LL.pin_failures(definition), [])
        with self.assertRaisesRegex(LL.LeanModeError, "differs from its committed version"):
            LL.tracked_definition(ROOT, git=lambda root: TRACKED_TEXT.replace('"2"', '"4"'))
        with self.assertRaisesRegex(LL.LeanModeError, "Git HEAD"):
            LL.tracked_definition(ROOT, git=lambda root: None)

    def test_pin_drift_is_named(self):
        base = tracked()
        for change in (
            {"command": "/opt/homebrew/bin/python3"},
            {"args": ["-m", "creme", "lean-mcp", "--", "uvx", "lean-lsp-mcp"]},
            {"env": {**base["env"], "LEAN_LSP_MAX_OPEN_FILES": "4"}},
            {"env": {**base["env"], "LEAN_MCP_DISABLED_TOOLS": "lean_profile_proof"}},
            {"default_tools_approval_mode": "approve"},
            {"tools": {"lean_run_code": {"approval_mode": "approve"}}},
            {"cwd": "/tmp"},
        ):
            with self.subTest(change=change):
                self.assertTrue(LL.pin_failures({**base, **change}))

    def test_effective_definition_must_equal_the_launched_one(self):
        launched = LL.launch_definition(tracked())
        self.assertEqual(sorted(launched["disabled_tools"]), sorted(LL.OPEN_WORLD_TOOLS))
        read = {"config": {"mcp_servers": {"lean-lsp-mcp": effective(launched)}}}
        self.assertEqual(LL.definition_failures(read, launched), [])
        drifted = effective(launched)
        drifted["env"] = {**drifted["env"], "LEAN_LSP_MAX_OPEN_FILES": "4"}
        for servers in ({"lean-lsp-mcp": drifted},
                        {"lean-lsp-mcp": {**effective(launched), "cwd": "/tmp"}},
                        {"lean-lsp-mcp": {**effective(launched), "disabled_tools": []}},
                        {"lean-lsp-mcp": {**effective(launched), "enabled": False}},
                        {}):
            with self.subTest(servers=servers):
                self.assertTrue(LL.definition_failures({"config": {"mcp_servers": servers}}, launched))

    def test_launch_override_defines_the_whole_server(self):
        launched = LL.launch_definition(tracked())
        override = LL.launch_override(launched)
        self.assertTrue(override.startswith("mcp_servers.lean-lsp-mcp={"))
        for value in ("/usr/bin/python3", "lean-lsp-mcp==", "lean_build,lean_profile_proof",
                      'LEAN_LSP_MAX_OPEN_FILES="2"', 'default_tools_approval_mode="writes"',
                      'lean_verify={approval_mode="approve"}', "lean_loogle"):
            self.assertIn(value, override)
        self.assertNotIn("\n", override)

    def test_isolation_keeps_only_the_named_server(self):
        servers = {"lean-lsp-mcp": {"command": "/usr/bin/python3"}, "node_repl": {"command": "/x"},
                   "remote": {"url": "https://example.invalid"}}
        plain = isolation_arguments("gpt-reserve", "low", servers, (), "on-request")
        self.assertIn('mcp_servers.lean-lsp-mcp={command="/usr/bin/false",args=[],enabled=false}', plain)
        override = LL.launch_override(LL.launch_definition(tracked()))
        lean = isolation_arguments("gpt-reserve", "low", servers, (), "on-request", {"lean-lsp-mcp": override})
        self.assertIn(override, lean)
        self.assertNotIn('mcp_servers.lean-lsp-mcp={command="/usr/bin/false",args=[],enabled=false}', lean)
        self.assertIn('mcp_servers.node_repl={command="/usr/bin/false",args=[],enabled=false}', lean)
        self.assertIn('mcp_servers.remote={url="http://127.0.0.1:9/",enabled=false}', lean)
        self.assertEqual([item for item in lean if item != override],
                         [item for item in plain if not item.startswith("mcp_servers.lean-lsp-mcp=")])
        config = {"config": {"model": "gpt-reserve", "review_model": "gpt-reserve", "web_search": "disabled",
                             "approval_policy": "on-request", "approvals_reviewer": "user", "mcp_servers": {
                                 "lean-lsp-mcp": effective(tracked()),
                                 "node_repl": {"command": "/usr/bin/false", "enabled": False}}}}
        features = [{"name": "shell_tool", "enabled": True}]
        self.assertEqual(isolation_failures(config, features, "gpt-reserve", ROOT, None, "on-request",
                                            ("lean-lsp-mcp",)), [])
        self.assertTrue(any("lean-lsp-mcp" in failure for failure in isolation_failures(
            config, features, "gpt-reserve", ROOT, None, "on-request")))

    def test_tool_listing_offers_no_forbidden_tool_and_nothing_else_runs(self):
        good = {"data": [
            {"name": "lean-lsp-mcp", "runtimeStatus": "connected", "tools": {name: {} for name in ALLOWED_TOOLS}},
            {"name": "node_repl", "runtimeStatus": "disabled", "tools": {}}]}
        self.assertEqual(LL.tool_failures(good), [])
        for broken in (
            {"data": [{"name": "lean-lsp-mcp", "runtimeStatus": "connected",
                       "tools": {"lean_loogle": {}, "lean_goal": {}}}]},
            {"data": [{"name": "lean-lsp-mcp", "runtimeStatus": "connected", "tools": {"lean_build": {}}}]},
            {"data": [{"name": "lean-lsp-mcp", "runtimeStatus": "starting", "tools": {}}]},
            {"data": [good["data"][0], {"name": "node_repl", "runtimeStatus": "connected", "tools": {}}]},
            {"data": []},
        ):
            with self.subTest(broken=broken):
                self.assertTrue(LL.tool_failures(broken))

    def test_live_guard_admits_only_the_kept_server_and_permitted_tools(self):
        forbidden = LL.OPEN_WORLD_TOOLS + LL.FORBIDDEN_TOOLS
        lean = GuardedSession(None, "gpt-reserve", lambda snap: (True, ""), ROOT, "workspace-write", "low",
                              writable_roots=(Path("/w"),), approval_policy="on-request",
                              mcp_servers=("lean-lsp-mcp",), forbidden_mcp_tools=forbidden)
        plain = GuardedSession(None, "gpt-reserve", lambda snap: (True, ""), ROOT, "workspace-write", "low",
                               writable_roots=(Path("/w"),), approval_policy="on-request")

        def call(server, tool, plugin=None):
            return {"method": "item/started", "params": {"item": {
                "type": "mcpToolCall", "server": server, "tool": tool, "pluginId": plugin}}}

        self.assertEqual(lean.observe(startup("lean-lsp-mcp", "ready")), [])
        self.assertEqual(lean.mcp_startup, {"lean-lsp-mcp": "ready"})
        self.assertEqual(lean.observe(call("lean-lsp-mcp", "lean_diagnostic_messages")), [])
        for message in (startup("node_repl", "ready"), call("lean-lsp-mcp", "lean_loogle"),
                        call("lean-lsp-mcp", "lean_build"), call("node_repl", "run"),
                        call("lean-lsp-mcp", "lean_goal", plugin="x@y")):
            with self.subTest(message=message):
                self.assertTrue(lean.observe(message))
        # A session without Lean mode keeps the version 1 guard.
        self.assertTrue(plain.observe(startup("lean-lsp-mcp", "ready")))
        self.assertTrue(plain.observe(call("lean-lsp-mcp", "lean_diagnostic_messages")))
        with self.assertRaises(Exception):
            GuardedSession(None, "gpt-reserve", lambda snap: (True, ""), ROOT, "read-only", "low",
                           mcp_servers=("lean-lsp-mcp",))


class LeanForbiddenCommandTest(unittest.TestCase):
    """The matching rule: resolved basename, --wind-down, and creme subcommands."""

    def test_semaphore_reclaim_and_wind_down_commands_are_named(self):
        # The command observed on 2026-09-18, which the master had to decline by hand.
        self.assertIn("codex-reclaim-lean", LL.forbidden_command(
            "~/.codex/bin/codex-reclaim-lean --wind-down vault-pair-inhabitant-probe-v1"))
        self.assertIn("semaphore", LL.forbidden_command(
            ["~/creme/.semaphore/semaphore", "adaptive-acquire", "g", "--memory-gib", "8"]))
        self.assertIn("creme reclaim", LL.forbidden_command(["python3", "-m", "creme", "reclaim", "--dry-run"]))
        self.assertIn("semaphore", LL.forbidden_command(
            ["/usr/bin/python3", "-m", "creme", "semaphore", "status"]))
        self.assertIn("creme.reclaim", LL.forbidden_command(["python3", "-m", "creme.reclaim", "--wind-down"]))
        self.assertIn("creme reclaim", LL.forbidden_command(["/Users/a/creme/scripts/creme", "reclaim"]))
        # A bare --wind-down on any launcher is refused on the flag alone.
        self.assertIn("--wind-down", LL.forbidden_command(["/opt/local/bin/helper", "--wind-down=g"]))

    def test_a_nested_shell_script_is_read_as_tokens(self):
        for command in (
                'bash -lc "cd /Users/agent/creme && ~/creme/.semaphore/semaphore status"',
                ["/bin/bash", "-lc", "cd /x && python3 -m creme reclaim --wind-down g"],
                ["sh", "-c", 'printf x; /Users/agent/.codex/bin/codex-reclaim-lean --dry-run'],
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(LL.forbidden_command(command))

    def test_the_build_escalation_and_ordinary_commands_are_not_matched(self):
        for command in (
                "~/creme/scripts/creme lake-build g -- Blanc.Basic",
                ["/Users/agent/creme/scripts/creme", "lake-build", "g", "--probe", "--", "Blanc.Basic"],
                ["bash", "-lc", "~/creme/scripts/creme lake-build g -- Blanc.Basic Blanc.Vault"],
                ["git", "-C", "/x", "diff"],
                "python3 -m creme build-ledger",
                None,
        ):
            with self.subTest(command=command):
                self.assertIsNone(LL.forbidden_command(command))


class LeanHostAdmissionTest(unittest.TestCase):
    def sample(self, free=None, status="OK"):
        data = {"memory_free_percent": free} if free is not None else {}
        return SimpleNamespace(status=status, data=data, detail="sampled")

    def test_headroom_and_semaphore_refusals(self):
        empty = {"hard": None, "soft": []}
        self.assertEqual(LL.admission_refusals(self.sample(80), empty, "goal-v1"), [])
        self.assertEqual(LL.admission_refusals(self.sample(80), {"hard": {"label": "goal-v1"}, "soft": []},
                                               "goal-v1"), [])
        for sample, state, expected in (
            (self.sample(15), empty, "DRAIN_HEAVY"),
            (self.sample(25), empty, "host guidance floor"),
            (self.sample(status="UNAVAILABLE"), empty, "unavailable"),
            (self.sample(80), {"hard": {"label": "other-goal"}, "soft": []}, "DEFER_HEAVY"),
            (self.sample(80), {"hard": None, "soft": [{"label": "manual-macos-session", "manual": True}]},
             "LIGHT_ONLY"),
        ):
            with self.subTest(expected=expected):
                refusals = LL.admission_refusals(sample, state, "goal-v1")
                self.assertTrue(any(expected in refusal for refusal in refusals), refusals)


class LeanPreambleTest(unittest.TestCase):
    def test_lean_preamble_carries_the_lean_rules(self):
        mode = LL.LeanMode("goal-v1", Path("/w/blanc/.worktrees/goal-v1"))
        text = LL.developer_instructions(ROOT, Path("/launch/creme"), mode)
        self.assertNotRegex(text, r"\{(launch_root|target|goal)\}")
        for phrase in (
            "`/launch/creme`", "`/w/blanc/.worktrees/goal-v1`", "`goal-v1`",
            "`lean_diagnostic_messages`", "`lean_goal`", "`lean_hover_info`",
            "~/creme/scripts/creme lake-build goal-v1 -- <narrow module targets>",
            "`--memory-gib`", "`--contention`", "bare `lake build`", "`lean_build`", "`lean_profile_proof`",
            "request escalated permissions", "language server: query diagnostics of two other Lean files", "`YIELD_HEAVY`", "`DRAIN_HEAVY`",
            "Never push, merge", "never enter the master role", "STATUS: DONE | PARTIAL | BLOCKED",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        plain = (ROOT / L.PREAMBLE_RELATIVE).read_text(encoding="utf-8")
        self.assertIn("Never run Lean elaboration or builds", plain)


# ---------------------------------------------------------------------------
# Broker sessions against the fake app-server


class LeanBrokerHarness(BrokerHarness):
    def setUp(self):
        super().setUp()
        self.goal = "lean-goal-v1"
        self.repository = self.base / "blanc"
        self.worktree = self.repository / ".worktrees" / self.goal
        self.worktree.mkdir(parents=True)
        (self.worktree / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
        self.definition = tracked()
        self.launched = LL.launch_definition(self.definition)
        self.scenario["config"]["mcp_servers"] = {
            "lean-lsp-mcp": self.definition, "node_repl": {"command": "/x", "enabled": True}}
        self.scenario["kept_mcp"] = {"lean-lsp-mcp": effective(self.launched)}
        self.scenario["thread_notifications"] = [startup("lean-lsp-mcp", "starting"),
                                                 startup("lean-lsp-mcp", "ready")]
        self.scenario["mcp_status"] = {"data": [
            {"name": "lean-lsp-mcp", "runtimeStatus": "connected", "tools": {name: {} for name in ALLOWED_TOOLS}},
            {"name": "node_repl", "runtimeStatus": "disabled", "tools": {}}]}
        self.write_scenario()
        self.broker = B.Broker(ROOT, self.state, "test-instance", self.environ, settle_seconds=0.2,
                               rollout_wait_seconds=1, session_idle_seconds=600)
        B.private_dir(self.state)
        B.private_dir(B.sessions_dir(self.state))
        self.broker.lean_repositories = lambda: (self.repository,)
        self.broker.tracked_definition = lambda root: json.loads(json.dumps(self.definition))
        self.host_calls: list[str] = []
        self.host_refusals: list[str] = []
        self.broker.host_probe = self.fake_host
        self.residual: list[dict] = []
        self.broker.residual_scan = lambda target: list(self.residual)
        self.wind_downs: list[dict] = []
        self.wind_down_verdict = "OK"
        self.wind_down_gate = threading.Event()
        self.wind_down_gate.set()
        self.broker.wind_down_function = self.fake_wind_down
        self.broker.mcp_ready_seconds = 3

    def tearDown(self):
        self.wind_down_gate.set()
        self.broker.stop_all("test teardown")
        super().tearDown()

    def fake_host(self, goal):
        self.host_calls.append(goal)
        return list(self.host_refusals), {"fake": True}

    def fake_wind_down(self, goal, target):
        self.wind_down_gate.wait(20)
        pids = [session.record.get("app_server_pid") for session in self.broker.sessions.values()]
        self.wind_downs.append({"goal": goal, "target": str(target),
                                "live_app_servers": [pid for pid in pids if B._pid_alive(pid)]})
        return {"verdict": self.wind_down_verdict, "status": "OK" if self.wind_down_verdict == "OK" else "REFUSED",
                "detail": "fake wind-down", "residual": [], "exit": 0}

    def open(self, brief="Run lean_diagnostic_messages on Blanc/Basic.lean.", target=None, op="start",
             lean=True, write=False, **extra):
        request = {"brief": brief, "target": str(target or self.worktree), "effort": "low", "detail": "live",
                   "write": write, **extra}
        if lean:
            request["lean"] = self.goal
        return self.broker.open_session(request, op)

    def session(self, reply) -> B.BrokerSession:
        return self.broker.sessions[reply["session"]]

    def settle(self, session_id, states=("idle",), timeout=20):
        self.until(lambda: (self.record(session_id) or {}).get("state") in states, timeout=timeout)
        return self.record(session_id)


class LeanBrokerSessionTest(LeanBrokerHarness):
    def test_lean_session_keeps_one_tracked_server_and_winds_down_after_close(self):
        self.scenario["turn_notifications"] = [
            self.live(self.reserve_reset),
            {"method": "item/started", "params": {"item": {
                "type": "mcpToolCall", "server": "lean-lsp-mcp", "tool": "lean_diagnostic_messages",
                "pluginId": None}}},
        ]
        self.write_scenario()
        reply = self.open()
        self.assertEqual(reply["code"], 0, reply)
        self.assertEqual(reply["mode"], "write")
        self.assertEqual(reply["lean"], self.goal)
        record = self.settle(reply["session"])
        self.assertEqual(record["turns"][-1]["verdict"], "PASS", record["turns"][-1])
        self.assertEqual(self.host_calls, [self.goal])
        launch = self.entries("launch")[-1]["argv"]
        self.assertIn(LL.launch_override(self.launched), launch)
        self.assertIn('mcp_servers.node_repl={command="/usr/bin/false",args=[],enabled=false}', launch)
        self.assertFalse(any(item.startswith("mcp_servers.lean-lsp-mcp={command=\"/usr/bin/false\"")
                             for item in launch))
        (thread_start,) = self.entries("request", "thread/start")
        instructions = LL.developer_instructions(ROOT, self.broker.root, LL.LeanMode(self.goal, self.worktree))
        self.assertEqual(thread_start["params"]["developerInstructions"], instructions)
        self.assertEqual(thread_start["params"]["approvalPolicy"], "on-request")
        self.assertEqual(thread_start["params"]["config"]["permissions"][WRITE_PROFILE]["filesystem"],
                         {str(self.worktree): "write"})
        self.assertTrue(self.entries("request", "mcpServerStatus/list"))
        self.assertEqual(record["lean"]["mcp"]["startup"], {"lean-lsp-mcp": "ready"})
        self.assertEqual(record["lean"]["mcp"]["tools"], sorted(ALLOWED_TOOLS))
        preflight = json.loads((self.state / "sessions" / reply["session"] / "preflight.json").read_text())
        self.assertEqual(preflight["lean"]["definition"], self.launched)
        self.assertEqual(self.wind_downs, [])
        code, result = self.session(reply).stop("requested")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["wind_down"], "OK")
        self.assertEqual(result["state"], "stopped")
        (call,) = self.wind_downs
        self.assertEqual(call["goal"], self.goal)
        self.assertEqual(call["target"], str(self.worktree))
        self.assertEqual(call["live_app_servers"], [], "wind-down ran before the app-server closed")
        record = self.record(reply["session"])
        self.assertEqual(record["lean"]["wind_down"]["verdict"], "OK")
        self.assertEqual(record["lean"]["wind_down"]["reason"], "stop: requested")
        self.assertIn("wind_down=OK", B.session_lines(record)[0])
        self.assertEqual(self.entries("request", "turn/start")[0]["params"]["model"], "gpt-reserve")
        self.assertFalse((self.state / L.TRIPWIRE_NAME).exists())

    def test_definition_drift_is_refused_before_any_thread(self):
        drifted = effective(self.launched)
        drifted["env"] = {**drifted["env"], "LEAN_LSP_MAX_OPEN_FILES": "4"}
        self.scenario["kept_mcp"] = {"lean-lsp-mcp": drifted}
        self.write_scenario()
        reply = self.open()
        self.assertEqual(reply["code"], L.EXIT_PREFLIGHT_REFUSED, reply)
        self.assertTrue(any("differs from Creme's tracked definition" in item for item in reply["refusals"]), reply)
        self.assertEqual(self.entries("request", "thread/start"), [])
        self.assertEqual(self.entries("request", "turn/start"), [])
        self.assertEqual(self.record(reply["session"])["lean"]["wind_down"]["verdict"], "OK")

    def test_tracked_definition_breaking_its_pins_is_refused_before_any_run_server(self):
        self.broker.tracked_definition = lambda root: LL.tracked_definition(
            root, git=lambda _: TRACKED_TEXT.replace('LEAN_LSP_MAX_OPEN_FILES = "2"', 'LEAN_LSP_MAX_OPEN_FILES = "4"'))
        reply = self.open()
        self.assertEqual(reply["code"], L.EXIT_PREFLIGHT_REFUSED, reply)
        self.assertEqual(len(self.entries("launch")), 1)  # only the zero-token probe server
        self.assertEqual(self.entries("request", "thread/start"), [])

    def test_a_second_lean_session_is_refused_while_one_is_live(self):
        self.scenario["hang"] = True
        self.write_scenario()
        first = self.open()
        self.assertEqual(first["code"], 0, first)
        launches = len(self.entries("launch"))
        second = self.open()
        self.assertEqual(second["code"], L.EXIT_PREFLIGHT_REFUSED, second)
        self.assertTrue(any("one Lean session at a time" in item for item in second["refusals"]), second)
        self.assertEqual(len(self.entries("launch")), launches)
        self.session(first).stop("requested")
        self.scenario["hang"] = False
        self.write_scenario()
        third = self.open()
        self.assertEqual(third["code"], 0, third)

    def test_host_refusal_or_a_busy_worktree_starts_nothing(self):
        self.host_refusals = ["DRAIN_HEAVY/LIGHT_ONLY: available memory is 15% (<20%)"]
        reply = self.open()
        self.assertEqual(reply["code"], L.EXIT_PREFLIGHT_REFUSED, reply)
        self.assertIn("DRAIN_HEAVY", "\n".join(reply["refusals"]))
        self.assertEqual(self.entries("launch"), [])
        self.host_refusals = []
        self.residual = [{"pid": 4242, "command": "lake serve", "cwd": str(self.worktree)}]
        reply = self.open()
        self.assertEqual(reply["code"], L.EXIT_PREFLIGHT_REFUSED, reply)
        self.assertIn("already run in", "\n".join(reply["refusals"]))
        self.assertEqual(self.entries("launch"), [])
        self.assertEqual(self.broker.sessions, {})

    def test_target_outside_the_goal_worktree_is_refused_by_the_broker(self):
        other = self.repository / ".worktrees" / "other-goal"
        other.mkdir(parents=True)
        (other / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
        for target in (other, self.target, self.repository):
            with self.subTest(target=target):
                reply = self.open(target=target)
                self.assertEqual(reply["code"], L.EXIT_PREFLIGHT_REFUSED, reply)
        self.assertEqual(self.entries("launch"), [])

    def test_another_mcp_server_starting_is_an_isolation_failure(self):
        self.scenario["thread_notifications"] = [startup("lean-lsp-mcp", "ready"), startup("node_repl", "ready")]
        self.write_scenario()
        reply = self.open()
        self.assertEqual(reply["code"], L.EXIT_ATTRIBUTION_FAILED, reply)
        self.assertEqual(self.entries("request", "turn/start"), [])
        record = self.settle(reply["session"], states=("tripped",))
        self.assertEqual(record["lean"]["wind_down"]["verdict"], "OK")

    def test_lean_server_that_never_becomes_ready_refuses_and_winds_down(self):
        self.scenario["thread_notifications"] = [startup("lean-lsp-mcp", "starting")]
        self.write_scenario()
        self.broker.mcp_ready_seconds = 1
        reply = self.open()
        self.assertEqual(reply["code"], L.EXIT_PREFLIGHT_REFUSED, reply)
        self.assertIn("did not become ready", "\n".join(reply["refusals"]))
        self.assertEqual(self.entries("request", "turn/start"), [])
        record = self.record(reply["session"])
        self.assertEqual(record["state"], "refused")
        self.assertEqual(record["lean"]["wind_down"]["reason"], "refused at open")

    def test_forbidden_tool_in_the_listing_refuses_the_session(self):
        self.scenario["mcp_status"]["data"][0]["tools"]["lean_loogle"] = {}
        self.write_scenario()
        reply = self.open()
        self.assertEqual(reply["code"], L.EXIT_PREFLIGHT_REFUSED, reply)
        self.assertIn("lean_loogle", "\n".join(reply["refusals"]))
        self.assertEqual(self.entries("request", "turn/start"), [])

    def test_mcp_elicitation_is_queued_for_the_master(self):
        self.scenario["turns"] = [{"approval": {"method": "mcpServer/elicitation/request", "params": {
            "serverName": "lean-lsp-mcp", "mode": "form", "message": "Allow lean_verify?",
            "requestedSchema": {"type": "object", "properties": {}}}}}]
        self.write_scenario()
        reply = self.open()
        record = self.until(lambda: (self.record(reply["session"]) or {}).get("pending_approvals"))
        self.assertEqual(record[0]["id"], "a1")
        self.assertIn("Allow lean_verify?", record[0]["summary"])
        time.sleep(0.3)
        self.assertEqual(self.entries("server-request-reply"), [])
        code, result = self.session(reply).approve("a1", "accept")
        self.assertEqual(code, 0, result)
        self.settle(reply["session"])
        (answer,) = self.entries("server-request-reply")
        self.assertEqual(answer["result"], {"action": "accept", "content": {}})

    def approval_scenario(self, command, cwd=None, reason="needs escalation"):
        self.scenario["turns"] = [{"approval": {"method": "item/commandExecution/requestApproval", "params": {
            "itemId": "i1", "command": command, "cwd": str(cwd or self.worktree), "reason": reason,
            "availableDecisions": ["accept", "cancel"]}}}]
        self.write_scenario()

    def test_a_wind_down_request_is_declined_by_the_broker_and_recorded(self):
        # Exactly the request a Lean session made on 2026-09-18.
        self.approval_scenario("~/.codex/bin/codex-reclaim-lean --wind-down vault-pair-inhabitant-probe-v1",
                               cwd=Path("/Users/agent/creme"), reason="the sandbox denies it")
        reply = self.open()
        self.assertEqual(reply["code"], 0, reply)
        record = self.until(lambda: (self.record(reply["session"]) or {}).get("refused_approvals"))
        self.assertEqual(record[0]["id"], "a1")
        self.assertEqual(record[0]["by"], "broker-lean-guard")
        self.assertIn("codex-reclaim-lean", record[0]["summary"])
        self.assertIn("codex-reclaim-lean is a semaphore or Lean reclamation command", record[0]["reason"])
        # It was never offered to the master, and the turn was answered at once.
        self.assertEqual(self.record(reply["session"])["pending_approvals"], [])
        (answer,) = self.until(lambda: self.entries("server-request-reply") or None)
        self.assertEqual(answer["result"], {"decision": "decline"})
        settled = self.settle(reply["session"])
        self.assertEqual(settled["turns"][-1]["verdict"], "PASS")
        # Visible in the event feed and in the printed session record.
        events = self.events(reply["session"])
        self.assertTrue(any("approval-refused" in line and "not offered to the master" in line for line in events),
                        events)
        lines = B.session_lines(settled)
        self.assertTrue(any(line.startswith("refused a1 by the broker:") for line in lines), lines)
        self.assertBounded(lines)

    def test_a_semaphore_command_inside_a_shell_is_declined_too(self):
        self.approval_scenario('bash -lc "cd /Users/agent/creme && ~/creme/.semaphore/semaphore status"')
        reply = self.open()
        refusals = self.until(lambda: (self.record(reply["session"]) or {}).get("refused_approvals"))
        self.assertIn("semaphore is a semaphore or Lean reclamation command", refusals[0]["reason"])
        self.assertEqual(self.record(reply["session"])["pending_approvals"], [])

    def test_the_lake_build_escalation_still_goes_to_the_master(self):
        self.approval_scenario("~/creme/scripts/creme lake-build lean-goal-v1 -- Blanc.Basic")
        reply = self.open()
        pending = self.until(lambda: (self.record(reply["session"]) or {}).get("pending_approvals"))
        self.assertEqual(pending[0]["id"], "a1")
        self.assertIn("lake-build", pending[0]["summary"])
        self.assertEqual(self.record(reply["session"]).get("refused_approvals"), [])
        time.sleep(0.3)
        self.assertEqual(self.entries("server-request-reply"), [], "the build escalation was answered without "
                                                                  "the master")
        code, result = self.session(reply).approve("a1", "accept")
        self.assertEqual(code, 0, result)
        self.settle(reply["session"])
        (answer,) = self.entries("server-request-reply")
        self.assertEqual(answer["result"], {"decision": "accept"})

    def test_wind_down_that_is_not_ok_leaves_the_session_unclean(self):
        reply = self.open()
        self.settle(reply["session"])
        self.wind_down_verdict = "NOT_OK"
        code, result = self.session(reply).stop("requested")
        self.assertEqual(code, L.EXIT_CODEX_FAILED, result)
        self.assertEqual(result["state"], "unclean")
        record = self.record(reply["session"])
        self.assertEqual(B._verdict_code(record), L.EXIT_CODEX_FAILED)
        lines = B.session_lines(record)
        self.assertIn("wind_down=NOT_OK", lines[0])
        self.assertTrue(any(line.startswith("wind-down ") for line in lines), lines)


class LeanStopPathTest(LeanBrokerHarness):
    """Every way a Lean session ends records a wind-down verdict."""

    def assertWoundDown(self, session_id, reason_prefix):
        def finished():
            entry = ((self.record(session_id) or {}).get("lean") or {}).get("wind_down")
            return entry if entry and entry.get("verdict") != "PENDING" else None

        record = self.until(finished)
        self.assertEqual(record["verdict"], "OK")
        self.assertTrue(record["reason"].startswith(reason_prefix), record)
        self.assertTrue(self.wind_downs)
        self.assertEqual(self.wind_downs[-1]["goal"], self.goal)

    def test_idle_close(self):
        reply = self.open()
        self.settle(reply["session"])
        self.broker.session_idle_seconds = 0.5
        self.assertWoundDown(reply["session"], "stop: idle")
        self.assertEqual(self.settle(reply["session"], states=("stopped",))["state"], "stopped")

    def test_shutdown(self):
        reply = self.open()
        self.settle(reply["session"])
        result = self.broker.dispatch({"op": "shutdown"})
        self.assertEqual(result["stopped"], 1)
        self.assertWoundDown(reply["session"], "stop: shutdown")

    def test_tripwire_stop(self):
        reply = self.open()
        self.settle(reply["session"])
        L.record_tripwire(self.state, "elsewhere", "thread", ["recorded by another run"])
        self.assertWoundDown(reply["session"], "stop: tripwire")

    def test_app_server_lost(self):
        self.scenario["turns"] = [{"crash": True}]
        self.write_scenario()
        reply = self.open()
        self.settle(reply["session"], states=("tripped",))
        self.assertWoundDown(reply["session"], "app-server lost")

    def test_crash_recovery_reconciliation(self):
        session_id = "lr-20260918-010203-abcdef"
        directory = B.private_dir(B.sessions_dir(self.state) / session_id)
        B.write_private_json(directory / "session.json", {
            "id": session_id, "state": "running", "broker_instance": "dead-instance", "target": str(self.worktree),
            "thread_id": None, "turns": [], "lean": {"goal": self.goal}, "app_server_pid": None,
        })
        self.wind_down_gate.clear()
        self.broker.reconcile_registry()
        record = self.record(session_id)
        self.assertEqual(record["state"], "lost")
        self.assertEqual(record["lean"]["wind_down"]["verdict"], "PENDING")
        refused = self.open()
        self.assertEqual(refused["code"], L.EXIT_PREFLIGHT_REFUSED, refused)
        self.assertIn("crash-recovery wind-down", "\n".join(refused["refusals"]))
        self.wind_down_gate.set()
        self.assertWoundDown(session_id, "crash recovery")
        self.until(lambda: not self.broker.reconciling)
        self.assertEqual(self.open()["code"], 0)


class NonLeanSessionUnchangedTest(LeanBrokerHarness):
    def test_write_session_without_lean_keeps_version_one_behaviour(self):
        self.scenario["thread_notifications"] = []
        self.write_scenario()
        reply = self.open(lean=False, write=True, target=self.target)
        self.assertEqual(reply["code"], 0, reply)
        self.assertIsNone(reply["lean"])
        record = self.settle(reply["session"])
        self.assertNotIn("lean", record)
        launch = self.entries("launch")[-1]["argv"]
        self.assertIn('mcp_servers.lean-lsp-mcp={command="/usr/bin/false",args=[],enabled=false}', launch)
        self.assertFalse(any("lean-mcp" in item for item in launch))
        (thread_start,) = self.entries("request", "thread/start")
        expected = L.developer_instructions(
            ROOT, L.RunRequest(brief="", workdir=self.target, write=True), self.broker.root)
        self.assertEqual(thread_start["params"]["developerInstructions"], expected)
        self.assertEqual(self.entries("request", "mcpServerStatus/list"), [])
        code, result = self.session(reply).stop("requested")
        self.assertEqual(code, 0)
        self.assertNotIn("wind_down", result)
        self.assertEqual(self.wind_downs, [])
        self.assertEqual(self.host_calls, [])

    def test_a_non_lean_session_still_offers_a_reclaim_command_to_the_master(self):
        self.scenario["thread_notifications"] = []
        self.scenario["turns"] = [{"approval": {"method": "item/commandExecution/requestApproval", "params": {
            "itemId": "i1", "command": "~/.codex/bin/codex-reclaim-lean --wind-down some-goal-v1",
            "cwd": str(self.target), "availableDecisions": ["accept", "cancel"]}}}]
        self.write_scenario()
        reply = self.open(lean=False, write=True, target=self.target)
        self.assertEqual(reply["code"], 0, reply)
        pending = self.until(lambda: (self.record(reply["session"]) or {}).get("pending_approvals"))
        self.assertEqual(pending[0]["id"], "a1")
        self.assertEqual(self.record(reply["session"]).get("refused_approvals"), [])
        time.sleep(0.3)
        self.assertEqual(self.entries("server-request-reply"), [])

    def test_mcp_startup_still_trips_a_non_lean_session(self):
        self.scenario["thread_notifications"] = [startup("lean-lsp-mcp", "ready")]
        self.write_scenario()
        reply = self.open(lean=False, write=True, target=self.target)
        self.assertEqual(reply["code"], L.EXIT_ATTRIBUTION_FAILED, reply)


class LeanCliRefusalTest(BrokerHarness):
    def test_non_worktree_target_is_refused_before_any_process(self):
        brief = self.base / "brief.md"
        brief.write_text("Check diagnostics.\n", encoding="utf-8")
        output = StringIO()
        with mock.patch.dict(os.environ, self.environ, clear=True), redirect_stdout(output), \
                redirect_stderr(StringIO()):
            code = main(["luna-reserve", "start", "--lean", "some-goal-v1", "--target", str(self.target),
                         "--brief", str(brief), "--effort", "low"])
        self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED, output.getvalue())
        self.assertIn("Lean mode needs the goal's own worktree", output.getvalue())
        self.assertFalse(self.log.exists())
        self.assertFalse(B.socket_path(self.state).exists())


if __name__ == "__main__":
    unittest.main()
