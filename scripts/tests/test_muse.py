from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from creme import muse as M
from creme import muse_broker as MB
from creme import muse_client as C
from creme import model_fit
from creme import pseudo_broker as PB
from creme.cli import main
from creme.codex_app_server import PinViolation

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "scripts/tests/fixtures/muse/fake_muse.py"


class FakeProcess:
    """Records requests; answers nothing (the guard is exercised before any send)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict, timeout: float = 60.0):
        self.sent.append((method, params))
        return {}


def guard(effort: str = "low", lean: str | None = None) -> C.MuseGuard:
    return C.MuseGuard(FakeProcess(), "s-1", effort, "lean" if lean else "read-only", lean)


class ModelPinGuardTest(unittest.TestCase):
    def test_only_the_pinned_model_may_be_selected(self):
        g = guard()
        g.command("session/setModel", {"model": {"modelId": C.PINNED_MODEL}})
        for model in ("muse-spark-1.3-contributor", "muse-spark-1.2", "", None):
            with self.assertRaises(PinViolation, msg=model):
                g.command("session/setModel", {"model": {"modelId": model}})
        self.assertEqual([method for method, _ in g.process.sent], ["session/setModel"])

    def test_the_pin_constant_is_not_a_contributor_model(self):
        self.assertNotIn("contributor", C.PINNED_MODEL)
        self.assertTrue(C.model_failures("muse-spark-1.3-contributor", "x")[0].startswith("x: contributor"))

    def test_turns_carry_the_session_effort_and_steer_names_the_active_turn(self):
        g = guard("low")
        with self.assertRaises(PinViolation):
            g.command("turn/start", {"input": [], "reasoningEffort": "high"})
        with self.assertRaises(PinViolation):
            g.command("turn/start", {"input": [], "reasoningEffort": "low", "ifBusy": "replace"})
        with self.assertRaises(PinViolation):
            g.command("turn/steer", {"expectedTurnId": "t", "input": [], "reasoningEffort": "low"})
        g.active_turn = "t"
        with self.assertRaises(PinViolation):
            g.command("turn/steer", {"expectedTurnId": "other", "input": [], "reasoningEffort": "low"})
        g.command("turn/steer", {"expectedTurnId": "t", "input": [], "reasoningEffort": "low"})
        with self.assertRaises(PinViolation):
            g.command("turn/start", {"input": [], "reasoningEffort": "low"})   # while active

    def test_unlisted_methods_and_other_sessions_are_refused(self):
        g = guard()
        for method in ("session/start", "session/setApprovalMode", "subagent/sendMessage", "session/fork"):
            with self.assertRaises(PinViolation, msg=method):
                g.command(method, {"mode": "allowAll"})
        with self.assertRaises(PinViolation):
            g.request("turn/interrupt", {"commandId": "c", "sessionId": "other"})

    def test_after_a_guard_failure_only_interrupt_is_sent(self):
        g = guard()
        g.observe({"method": "session/modelChanged", "params": {"sessionId": "s-1", "modelId": "muse-spark-1.3-contributor"}})
        self.assertTrue(g.guard_failures)
        with self.assertRaises(PinViolation):
            g.command("session/setModel", {"model": {"modelId": C.PINNED_MODEL}})
        g.request("turn/interrupt", {"commandId": "c", "sessionId": "s-1"})

    def test_served_model_evidence_is_checked(self):
        g = guard()
        g.outcome = C.TurnOutcome(turn_id="t")
        self.assertEqual(g.observe({"method": "session/tokenUsage", "params": {
            "sessionId": "s-1", "turnId": "t", "modelId": C.PINNED_MODEL, "usage": {"outputTokens": 1},
            "promptTokens": 2, "totalTokens": 3}}), [])
        self.assertEqual(g.outcome.tokens["prompt"], 2)
        self.assertTrue(g.observe({"method": "session/tokenUsage", "params": {
            "sessionId": "s-1", "turnId": "t", "modelId": "muse-spark-1.2"}}))
        self.assertTrue(guard().observe({"method": "session/modelRouteUnserved", "params": {"sessionId": "s-1"}}))
        self.assertTrue(guard().observe({"method": "item/started", "params": {
            "sessionId": "s-1", "item": {"kind": "toolCall", "tool": "web_fetch"}}}))
        self.assertTrue(guard().observe({"method": "item/started", "params": {
            "sessionId": "s-1", "item": {"kind": "subagent"}}}))
        self.assertTrue(guard().observe({"method": "item/completed", "params": {
            "sessionId": "s-1", "item": {"kind": "toolCall", "tool": "mcp__lean_lsp_mcp__lean_goal",
                                          "status": "completed"}}}))
        self.assertEqual(guard(lean="g").observe({"method": "item/completed", "params": {
            "sessionId": "s-1", "item": {"kind": "toolCall", "tool": "mcp__lean_lsp_mcp__lean_goal",
                                          "status": "completed"}}}), [])

    def test_session_log_audit_rejects_any_other_model(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text(json.dumps({"payload": {"model_id": "muse-spark-1.3-contributor"}}) + "\n"
                            + json.dumps({"payload": {"model_id": C.PINNED_MODEL, "roster": [{"model": "same-as-main"}]}})
                            + "\n" + json.dumps({"nested": json.dumps({"modelId": "muse-spark-1.2"})}) + "\n")
            self.assertEqual(C.audit_session_log(path, 1)["verdict"], "FAIL")   # the nested 1.2
            self.assertEqual(C.audit_session_log(path, 0)["verdict"], "FAIL")
            path.write_text(json.dumps({"payload": {"model_id": C.PINNED_MODEL, "model": "same-as-main"}}) + "\n")
            self.assertEqual(C.audit_session_log(path, 0)["verdict"], "PASS")
            self.assertEqual(C.audit_session_log(Path(temp) / "missing", 0)["verdict"], "FAIL")


def approval(kind: str, **subject) -> dict:
    return {"approvalId": "a", "currentRequirementId": {"approvalId": "a", "sourceIndex": 0},
            "availableChoices": [
                {"choiceId": "once", "decision": "approved", "scope": "once"},
                {"choiceId": "session", "decision": "approvedForSession", "scope": "session"},
                {"choiceId": "always", "decision": "approvedPolicyAmendment", "scope": "localPersistent"},
                {"choiceId": "abort", "decision": "abort", "scope": "once"}],
            "subject": {"kind": kind, **subject}, "toolName": subject.get("toolName", "bash")}


class ApprovalAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.target = Path(tempfile.mkdtemp()).resolve()

    def decide(self, params, mode="read-only", lean=None):
        return C.decide_approval(params, mode, lean, self.target)

    def test_sandboxed_modes_approve_shell_once_and_abort_everything_else(self):
        for mode in ("read-only", "write"):
            decision = self.decide(approval("shell", command="git status"), mode)
            self.assertEqual((decision.action, decision.choice), ("approve", "once"))
            for params in (approval("tool", toolName="mcp__lean_lsp_mcp__lean_goal"),
                           approval("network", host="example.com"), approval("fileAccess", path="/etc"),
                           {**approval("shell", command="ls"), "protectedWrite": True}):
                self.assertEqual(self.decide(params, mode).action, "abort", params)

    def test_no_session_wide_or_persistent_choice_is_ever_taken(self):
        for params in (approval("shell", command="ls"), approval("tool", toolName="x")):
            self.assertIn(self.decide(params).choice, ("once", "abort"))

    def test_lean_allowlist(self):
        build = f"~/creme/scripts/creme lake-build g --wait 900 -- Blanc.Basic"
        ws = str(self.target)
        self.assertEqual(self.decide(approval("shell", command=build, workspaceRoot=ws), "lean", "g").action, "approve")
        for command in (build.replace("-- Blanc", "--memory-gib 3 -- Blanc"), build + " ; rm -rf x",
                        build.replace("lake-build g", "lake-build other"), build.replace("900", "901")):
            self.assertEqual(self.decide(approval("shell", command=command, workspaceRoot=ws), "lean", "g").action,
                             "master", command)
        self.assertEqual(self.decide(approval("shell", command=build, workspaceRoot="/tmp"), "lean", "g").action,
                         "master")
        self.assertEqual(self.decide(approval("shell", command="~/creme/.semaphore/semaphore status"),
                                     "lean", "g").action, "abort")
        self.assertEqual(self.decide(approval("shell", command="python3 -m creme reclaim --wind-down g"),
                                     "lean", "g").action, "abort")
        piped = approval("shell", command="git diff | sh", stages=[{"argv": ["git", "diff"], "argvComplete": True},
                                                                 {"argv": ["sh"], "argvComplete": True}])
        self.assertEqual(self.decide(piped, "lean", "g").action, "master")
        self.assertEqual(self.decide(approval("tool", toolName="mcp__lean_lsp_mcp__lean_goal"), "lean", "g").action,
                         "approve")
        for tool in ("lean_leansearch", "lean_build", "lean_loogle"):
            self.assertEqual(self.decide(approval("tool", toolName="mcp__lean_lsp_mcp__" + tool), "lean", "g").action,
                             "abort", tool)


class LeanShellBypassTest(unittest.TestCase):
    """The Lean host is unsandboxed: no shell command but the exact owned build is approved by rule.

    Each case was auto-approved by the 0340d04 read-only allowlist (review of 2026-09-28).
    """

    BYPASSES = (
        ["git", "diff", "-o", "/tmp/x"],                  # writes anywhere (short form of --output)
        ["git", "log", "--output=/tmp/x"],
        ["git", "--exec-path=/tmp", "status"],            # runs /tmp/git-status
        ["git", "-c", "core.pager=/tmp/p", "log"],        # any git global option
        ["git", "-C", "/Users/agent/other", "status"],    # reads outside the target
        ["git", "-C/Users/agent/other", "status"],        # the same, joined form
        ["rg", "--pre", "/bin/sh", "x", "file.lean"],     # runs an arbitrary preprocessor
        ["rg", "--pre-glob", "*", "--pre", "/tmp/p", "x"],
        ["rg", "-z", "x"],
        ["cat", str(Path.home() / ".ssh/id_rsa")],        # unconfined read
        ["ls", "/"],
        ["git", "status"],                                # even a benign read goes to the master
    )

    def test_every_bypass_goes_to_the_master(self):
        target = Path(tempfile.mkdtemp()).resolve()
        for argv in self.BYPASSES:
            params = approval("shell", command=C.shell_join(argv), workspaceRoot=str(target),
                              stages=[{"argv": argv, "argvComplete": True}])
            with self.subTest(argv=" ".join(argv)):
                decision = C.decide_approval(params, "lean", "g", target)
                self.assertEqual(decision.action, "master", argv)
                self.assertIsNone(decision.choice, argv)

    def test_sandboxed_modes_still_approve_shell_once(self):
        # read-only and write hosts are sandboxed (no writes outside, no network), so the sandbox is the control.
        target = Path(tempfile.mkdtemp()).resolve()
        params = approval("shell", command="git diff -o /tmp/x",
                          stages=[{"argv": ["git", "diff", "-o", "/tmp/x"], "argvComplete": True}])
        self.assertEqual(C.decide_approval(params, "read-only", None, target).action, "approve")


class FakeMuseHarness(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="mse")).resolve()
        self.state = self.base / "state"
        self.target = self.base / "target"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        (self.target / "README.md").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.target), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.target), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
                        "init"], check=True)
        binary = self.base / "muse"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        binary.chmod(0o755)
        self.scenario_path = self.base / "scenario.json"
        self.scenario = {"home": str(self.base / "home"), "turn": {"text": "STATUS: DONE"}}
        self.write_scenario()
        self.environ = {**os.environ, M.BINARY_ENV: str(binary), M.STATE_ENV: str(self.state),
                        "FAKE_MUSE_SCENARIO": str(self.scenario_path), "MUSE_MODEL": "muse-spark-1.3-contributor",
                        MB.IDLE_ENV: "60"}

    def tearDown(self):
        try:
            MB.cmd_shutdown(ROOT, self.environ)
        except Exception:
            pass
        info = PB.read_json(PB.info_path(self.state))
        if info and PB.pid_alive(info.get("pid")) and f"--instance {info.get('instance')}" in PB.process_command(info["pid"]):
            os.kill(info["pid"], signal.SIGKILL)
        subprocess.run(["rm", "-rf", str(self.base)], check=False)

    def write_scenario(self):
        self.scenario_path.write_text(json.dumps(self.scenario))

    def calls(self) -> list[dict]:
        path = Path(self.scenario["home"]) / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def sent(self, method: str) -> list[dict]:
        return [call["in"]["params"] for call in self.calls() if (call.get("in") or {}).get("method") == method]

    def run_brief(self, **overrides) -> tuple[int, dict]:
        request = M.RunRequest(brief="Say OK.", target=self.target, effort="low", timeout_seconds=30, **overrides)
        return M.run(ROOT, request, self.environ)


class RunTest(FakeMuseHarness):
    def test_run_passes_pins_the_model_and_keeps_records(self):
        code, record = self.run_brief()
        self.assertEqual((code, record["verdict"]), (0, "PASS"), record)
        self.assertEqual(set(record["models"]), {C.PINNED_MODEL})
        exec_call = next(call for call in self.calls() if call.get("argv", [None])[0] == "exec")
        argv = exec_call["argv"]
        for flag in ("--provider", "--permission-profile", "--no-foreign-personal-context", "--disable-web-tools"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-profile") + 1], ":read-only")
        self.assertIsNone(exec_call["env_model"])            # MUSE_MODEL is scrubbed
        self.assertEqual(exec_call["no_update"], "1")
        serve = next(call for call in self.calls() if call.get("argv", [None])[0] == "serve")
        self.assertIn("--disable-write", serve["argv"])
        self.assertEqual([p["model"]["modelId"] for p in self.sent("session/setModel")], [C.PINNED_MODEL])
        self.assertEqual([p["mode"] for p in self.sent("session/setApprovalMode")], ["promptUnmatched"])
        self.assertTrue(all(p["reasoningEffort"] == "low" for p in self.sent("turn/start")))
        run_dir = Path(record["run_dir"])
        for name in ("brief.md", "events.jsonl", "approvals.json", "last-message.md", "usage-before.json",
                     "usage-after.json", "verdict.json", "audit.json", "bootstrap.json", "transcript.jsonl"):
            self.assertTrue((run_dir / name).exists(), name)
        self.assertEqual(json.loads((run_dir / "audit.json").read_text())["verdict"], "PASS")

    def test_a_served_contributor_model_fails_closed_and_trips(self):
        self.scenario["served_model"] = "muse-spark-1.3-contributor"
        self.write_scenario()
        code, record = self.run_brief()
        self.assertEqual((code, record["verdict"]), (M.EXIT_PIN_FAILED, "PIN_FAILED"), record)
        self.assertEqual(self.sent("turn/start"), [])          # no turn on an unpinned session
        self.assertTrue(M.tripwire_path(self.state).exists())
        code, record = self.run_brief()
        self.assertEqual(code, M.EXIT_PREFLIGHT_REFUSED)
        self.assertIn("tripwire", record["refusals"][0])

    def test_a_contributor_model_in_the_session_log_fails_the_turn(self):
        self.scenario["turn"]["log_model"] = "muse-spark-1.3-contributor"
        self.write_scenario()
        code, record = self.run_brief()
        self.assertEqual(record["verdict"], "PIN_FAILED", record)
        self.assertEqual(record["session_log_audit"], "FAIL")

    def test_read_only_run_that_changes_the_target_fails(self):
        self.scenario["turn"]["touch"] = str(self.target / "stray.txt")
        self.write_scenario()
        code, record = self.run_brief()
        self.assertEqual((code, record["verdict"]), (M.EXIT_MUSE_FAILED, "FAILED"), record)
        self.assertIn("read-only run changed", record["git_failure"])

    def test_usage_at_the_refusal_threshold_refuses_before_any_turn(self):
        self.scenario["usage"] = {"window": {"usedPercent": 99, "resetsAtMs": 1, "windowDurationMins": 300},
                                  "weekly": {"usedPercent": 5, "resetsAtMs": 1}, "tier": "t", "observedAtMs": 1}
        self.write_scenario()
        code, record = self.run_brief()
        self.assertEqual(code, M.EXIT_PREFLIGHT_REFUSED, record)
        self.assertEqual(self.sent("turn/start"), [])

    def test_approvals_follow_the_allowlist(self):
        self.scenario["turn"]["approvals"] = [{"kind": "shell", "command": "ls"},
                                              {"kind": "tool", "toolName": "mcp__lean_lsp_mcp__lean_goal"}]
        self.write_scenario()
        code, record = self.run_brief()
        self.assertEqual(code, 0, record)
        self.assertEqual([p["choiceId"] for p in self.sent("approval/decide")], ["allow_once", "abort"])

    def test_forbidden_tool_is_a_guard_failure(self):
        self.scenario["turn"]["tools"] = ["web_search"]
        self.write_scenario()
        code, record = self.run_brief()
        self.assertEqual(record["verdict"], "PIN_FAILED", record)

    def test_early_refusals(self):
        self.assertTrue(M.early_refusals(self.state, "ultra", "x", ROOT, str(self.target), "read-only"))
        self.assertTrue(M.early_refusals(self.state, "low", " ", ROOT, str(self.target), "read-only"))
        self.assertTrue(M.early_refusals(self.state, "low", "x", ROOT, str(M.launch_root(ROOT)), "write"))
        self.assertEqual(M.early_refusals(self.state, "low", "x", ROOT, str(self.target), "write"), [])

    def test_status_reports_the_pin_and_never_the_default(self):
        code, report = M.status(ROOT, self.environ)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["catalogue_default"], "muse-spark-1.3-contributor")
        self.assertEqual(report["pinned_model"], C.PINNED_MODEL)
        self.assertEqual(self.sent("turn/start"), [])


class BrokerTest(FakeMuseHarness):
    def wait(self, session, timeout=20):
        return MB.cmd_wait(ROOT, self.environ, session, timeout, poll_seconds=0.05)

    def start(self, **extra):
        code, lines, answer = MB.cmd_start(ROOT, self.environ, "Say OK.", str(self.target), False, "low", "live", 60,
                                           **extra)
        self.assertEqual(code, 0, lines)
        return answer["session"]

    def test_steer_reaches_the_running_turn(self):
        self.scenario["turn"]["wait_for_steer"] = True
        self.write_scenario()
        session = self.start()
        code, lines, _ = MB.cmd_simple(ROOT, self.environ, "send", session, text="use BRAVO", steer=True)
        self.assertEqual(code, 0, lines)
        code, lines, record = self.wait(session)
        self.assertEqual(code, 0, lines)
        turn = record["turns"][0]
        self.assertEqual((turn["verdict"], turn["steers"], turn["steered_items"]), ("PASS", 1, 1))
        self.assertIn("STEERED: use BRAVO", Path(turn["last_message"]).read_text())
        steer = self.sent("turn/steer")[0]
        self.assertEqual(steer["expectedTurnId"], turn["turn_id"])
        self.assertEqual(steer["reasoningEffort"], "low")

    def test_a_host_lost_mid_turn_ends_the_session_and_wait_exits_11(self):
        self.scenario["turn"]["die"] = True
        self.write_scenario()
        session = self.start()
        started = time.monotonic()
        code, lines, record = self.wait(session, timeout=20)
        self.assertLess(time.monotonic() - started, 15, lines)
        self.assertEqual(code, M.EXIT_MUSE_FAILED, lines)
        self.assertEqual(record["state"], "failed")
        self.assertEqual((record["turns"][0]["status"], record["turns"][0]["verdict"]), ("lost", "FAILED"))

    def reconciling(self, seconds: str) -> None:
        self.environ[M.RECONCILE_ENV] = seconds
        MB.cmd_shutdown(ROOT, self.environ)   # a broker reads the interval at start

    def test_an_approval_visible_only_through_list_pending_reaches_the_master(self):
        # Projection lost before the approval: no approval/request, no approval/requested is pushed.
        self.reconciling("1")
        self.scenario["turn"].update(lose_before_approval=True,
                                     approvals=[{"kind": "shell", "command": "ls", "no_abort": True}])
        self.write_scenario()
        session = self.start()
        code, lines, record = self.wait(session)
        self.assertEqual(code, PB.EXIT_ATTENTION, lines)
        self.assertEqual(len(record["pending_approvals"]), 1, lines)
        self.assertIn("shell `ls`", record["pending_approvals"][0]["summary"])
        code, lines, _ = MB.cmd_simple(ROOT, self.environ, "approve", session, approval="a1", decision="accept")
        self.assertEqual(code, 0, lines)
        code, lines, record = self.wait(session)
        self.assertEqual((code, record["turns"][0]["verdict"]), (0, "PASS"), lines)   # recovered by view/page
        events: list[str] = []
        MB.cmd_events(ROOT, self.environ, session, False, 50, 0, events.append)
        self.assertTrue(any("viewHealthChanged" in line for line in events), events)
        self.assertTrue(any("approvals_recovered=1" in line for line in events), events)

    def test_an_allowlisted_approval_seen_only_by_polling_is_decided_by_the_allowlist(self):
        self.reconciling("1")
        self.scenario["turn"].update(lose_before_approval=True, approvals=[{"kind": "shell", "command": "ls"}])
        self.write_scenario()
        session = self.start()
        code, lines, record = self.wait(session)
        self.assertEqual((code, record["turns"][0]["verdict"]), (0, "PASS"), lines)
        self.assertEqual([p["choiceId"] for p in self.sent("approval/decide")], ["allow_once"])

    def test_a_turn_that_ends_while_notifications_are_lost_is_not_steered(self):
        self.reconciling("600")          # only `send` reconciles here
        self.scenario["turn"].update(lose_before_end=True, silent_loss=True)
        self.write_scenario()
        session = self.start()
        time.sleep(1.5)                  # the fake has ended the turn; the broker saw nothing
        self.assertEqual(MB.load_record(self.state, session)["state"], "running")
        self.scenario["turn"] = {"text": "STATUS: DONE"}
        self.write_scenario()
        code, lines, answer = MB.cmd_simple(ROOT, self.environ, "send", session, text="next order")
        self.assertEqual((code, answer.get("verdict")), (0, "STARTED"), lines)
        self.assertEqual(self.sent("turn/steer"), [])
        record = MB.load_record(self.state, session)
        self.assertEqual(record["turns"][0]["verdict"], "PASS")   # its terminal was replayed from view/page

    def test_a_turn_whose_terminal_is_never_seen_ends_when_muse_reports_idle(self):
        self.reconciling("1")
        self.scenario["turn"].update(lose_before_end=True, drop_terminal=True)
        self.write_scenario()
        session = self.start()
        code, lines, record = self.wait(session)
        self.assertEqual(code, M.EXIT_MUSE_FAILED, lines)
        turn = record["turns"][0]
        self.assertEqual((turn["status"], turn["verdict"]), ("lost", "FAILED"))
        self.assertIn("terminal event was not observed", " ".join(turn["errors"]))

    def test_run_reconciles_a_lost_view(self):
        self.environ[M.RECONCILE_ENV] = "1"
        self.scenario["turn"].update(lose_before_approval=True, approvals=[{"kind": "shell", "command": "ls"}])
        self.write_scenario()
        code, record = RunTest.run_brief(self)
        self.assertEqual((code, record["verdict"]), (0, "PASS"), record)

    def test_send_starts_a_new_turn_when_idle_and_steer_refuses(self):
        session = self.start()
        self.wait(session)
        code, lines, _ = MB.cmd_simple(ROOT, self.environ, "send", session, text="more", steer=True)
        self.assertEqual(code, M.EXIT_PREFLIGHT_REFUSED, lines)
        code, lines, _ = MB.cmd_simple(ROOT, self.environ, "send", session, text="more")
        self.assertEqual(code, 0, lines)
        code, lines, record = self.wait(session)
        self.assertEqual([turn["verdict"] for turn in record["turns"]], ["PASS", "PASS"])
        code, lines, record = MB.cmd_simple(ROOT, self.environ, "stop", session)
        self.assertEqual(code, 0, lines)
        self.assertIn("stop_audit=PASS", lines[0])

    def test_interrupt_and_resume(self):
        self.scenario["turn"]["sleep"] = 20
        self.write_scenario()
        session = self.start()
        time.sleep(0.5)
        code, lines, _ = MB.cmd_simple(ROOT, self.environ, "interrupt", session)
        self.assertEqual(code, 0, lines)
        code, lines, record = self.wait(session)
        self.assertEqual(code, PB.EXIT_INTERRUPTED, lines)
        MB.cmd_simple(ROOT, self.environ, "stop", session)
        muse_session = record["muse_session"]
        self.scenario["turn"]["sleep"] = 0
        self.write_scenario()
        code, lines, answer = MB.cmd_resume(ROOT, self.environ, muse_session, None, False, None, "silent", 60)
        self.assertEqual(code, 0, lines)
        self.assertEqual(answer["resumed_from"], session)
        self.assertEqual(len([c for c in self.calls() if c.get("argv", [None])[0] == "exec"]), 1)  # no new bootstrap
        code, lines, _ = MB.cmd_simple(ROOT, self.environ, "send", answer["session"], text="again")
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.wait(answer["session"])[0], 0)
        code, lines, _ = MB.cmd_resume(ROOT, self.environ, "01a0e589-bf03-7e42-9fb7-a944e586e12e", None, False,
                                       None, "silent", 60)
        self.assertEqual(code, M.EXIT_PREFLIGHT_REFUSED, lines)   # never recorded

    def test_an_unknown_approval_subject_is_aborted_without_asking_the_master(self):
        self.scenario["turn"]["approvals"] = [{"kind": "process", "command": "x"}]
        self.write_scenario()
        session = self.start(lean=None)
        code, lines, record = self.wait(session)
        self.assertEqual(code, 0, lines)
        self.assertEqual([p["choiceId"] for p in self.sent("approval/decide")], ["abort"])

    def test_a_pin_failure_trips_every_session(self):
        first = self.start()
        self.wait(first)
        self.scenario["served_model"] = "muse-spark-1.2"
        self.write_scenario()
        code, lines, answer = MB.cmd_start(ROOT, self.environ, "x", str(self.target), False, "low", "silent", 60)
        self.assertEqual(code, M.EXIT_PIN_FAILED, lines)
        self.assertTrue(M.tripwire_path(self.state).exists())
        deadline = time.time() + 15
        while time.time() < deadline and (MB.load_record(self.state, first) or {}).get("state") != "stopped":
            time.sleep(0.1)
        other = MB.load_record(self.state, first)
        self.assertEqual((other["state"], other["note"]), ("stopped", "stop: tripwire"))
        self.assertEqual(MB.load_record(self.state, answer["session"])["state"], "tripped")

    def test_events_sessions_detail_and_read_are_bounded(self):
        session = self.start()
        self.wait(session)
        lines: list[str] = []
        MB.cmd_events(ROOT, self.environ, session, False, 50, 0, lines.append)
        self.assertTrue(any("turn 1 completed verdict=PASS" in line for line in lines), lines)
        code, listed, _ = MB.cmd_sessions(ROOT, self.environ, 10)
        self.assertIn(session, listed[1])
        self.assertEqual(MB.cmd_detail(ROOT, self.environ, session, "summary")[0], 0)
        code, text, _ = MB.cmd_read(ROOT, self.environ, session, 5)
        self.assertIn("STATUS: DONE", text[1])
        with redirect_stdout(StringIO()) as out:
            os.environ[M.STATE_ENV] = str(self.state)
            try:
                main(["muse", "sessions"])
            finally:
                del os.environ[M.STATE_ENV]
        self.assertIn(session, out.getvalue())


class DoctorMuseTest(FakeMuseHarness):
    def test_doctor_checks_binary_templates_and_tripwire(self):
        from creme.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, check_muse_pseudo_subagent

        self.assertEqual(check_muse_pseudo_subagent(ROOT, self.environ)[0].status, STATUS_OK)
        missing = {**self.environ, M.BINARY_ENV: str(self.base / "absent")}
        self.assertEqual(check_muse_pseudo_subagent(ROOT, missing)[0].status, STATUS_WARN)
        M.record_tripwire(self.state, "r", None, ["x"])
        self.assertEqual(check_muse_pseudo_subagent(ROOT, self.environ)[0].status, STATUS_FAIL)


class ModelFitMuseTest(FakeMuseHarness):
    def test_from_muse_session_reads_tokens_wall_time_and_turns(self):
        session = BrokerTest.start(self)
        MB.cmd_wait(ROOT, self.environ, session, 20, poll_seconds=0.05)
        MB.cmd_simple(ROOT, self.environ, "stop", session)
        usage = model_fit.muse_session_usage(PB.sessions_dir(self.state) / session)
        self.assertEqual(usage["turns"], "1")
        self.assertEqual(usage["tokens"], "uncached_input=60 cache_read=40 cache_write=n/a output=7 reasoning=3")
        self.assertEqual(usage["effort"], "low")
        code, record = self.run_brief()
        usage = model_fit.muse_session_usage(Path(record["run_dir"]))
        self.assertEqual((usage["turns"], usage["effort"]), ("1", "low"))


if __name__ == "__main__":
    unittest.main()
