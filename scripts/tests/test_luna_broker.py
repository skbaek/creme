from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from creme import luna_broker as B
from creme import luna_reserve as L
from creme.cli import main

try:
    from test_luna_reserve import FAKE, ROOT, THREAD, limits, rollout, snapshot
except ImportError:  # run as scripts.tests.test_luna_broker
    from scripts.tests.test_luna_reserve import FAKE, ROOT, THREAD, limits, rollout, snapshot


MAX_LINES = 14
MAX_WIDTH = 260


class BrokerHarness(unittest.TestCase):
    def setUp(self):
        # A short base keeps the AF_UNIX socket path well inside its limit.
        self.base = Path(tempfile.mkdtemp(prefix="lrb")).resolve()
        self.codex_home = self.base / "codex-home"
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text("", encoding="utf-8")
        self.state = self.base / "state"
        self.root = L.launch_root(ROOT).resolve()
        self.target = self.base / "target"
        self.target.mkdir()
        self.log = self.base / "log.jsonl"
        binary = self.base / "codex"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        binary.chmod(0o755)
        self.scenario_path = self.base / "scenario.json"
        self.environ = {
            **os.environ,
            L.BINARY_ENV: str(binary),
            L.STATE_ENV: str(self.state),
            "FAKE_CODEX_SCENARIO": str(self.scenario_path),
            B.SETTLE_ENV: "0.2",
            B.ROLLOUT_WAIT_ENV: "1",
            B.IDLE_ENV: "60",
            B.SESSION_IDLE_ENV: "600",
        }
        now = int(time.time())
        self.reserve_reset = now + 86400 * 6
        self.regular_reset = now + 86400 * 2
        self.scenario = {
            "log": str(self.log),
            "state": str(self.base / "executed"),
            "codex_home": str(self.codex_home),
            "models": ["gpt-reserve", "gpt-5.6-luna"],
            "limits": limits(reserve_reset=self.reserve_reset, regular_reset=self.regular_reset),
            "config": {"model": "gpt-6-astra", "service_tier": "default", "mcp_servers": {
                "lean-lsp-mcp": {"command": "/usr/bin/python3", "enabled": True}}},
            "enabled_features": ["shell_tool", "multi_agent"],
            "layers": [{"name": {"type": "sessionFlags"}},
                       {"name": {"type": "project", "dotCodexFolder": str(self.root / ".codex")}},
                       {"name": {"type": "user", "file": "/fake/.codex/config.toml"}}],
            "records": rollout(snapshots=[snapshot(self.reserve_reset)]),
            "skills": [{"cwd": str(self.root), "skills": [
                {"name": "lean-prover", "scope": "repo", "enabled": True,
                 "path": str(self.root / ".agents/skills/lean-prover/SKILL.md")}]}],
            "turn_notifications": [
                self.live(self.reserve_reset),
                {"method": "item/started", "params": {"item": {"type": "commandExecution", "command": "sleep 15"}}},
                {"method": "item/completed", "params": {"item": {
                    "type": "commandExecution", "command": "sleep 15", "exitCode": 0, "status": "completed"}}},
                {"method": "item/agentMessage/delta", "params": {"delta": "tok"}},
                {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {
                    "inputTokens": 10, "cachedInputTokens": 5, "outputTokens": 2}}}},
            ],
        }
        self.write_scenario()

    def tearDown(self):
        try:
            B.cmd_shutdown(ROOT, self.environ)
        except Exception:
            pass
        info = B.read_json(B.info_path(self.state))
        if info and B._pid_alive(info.get("pid")) and f"--instance {info.get('instance')}" in B._process_command(info["pid"]):
            os.kill(info["pid"], signal.SIGKILL)
        subprocess.run(["rm", "-rf", str(self.base)], check=False)

    def live(self, resets_at):
        return {"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 1, "windowDurationMins": 10080, "resetsAt": resets_at},
            "secondary": None, "credits": None}}}

    def write_scenario(self):
        self.scenario_path.write_text(json.dumps(self.scenario), encoding="utf-8")

    def assertBounded(self, lines):
        self.assertLessEqual(len(lines), MAX_LINES, lines)
        for line in lines:
            self.assertLessEqual(len(line), MAX_WIDTH, line)

    def start(self, write=False, detail="silent", brief="Say OK.", expect=0, timeout=60):
        code, lines, reply = B.cmd_start(ROOT, self.environ, brief, str(self.target), write, "low", detail,
                                         L.Policy().__dict__, [], timeout)
        self.assertEqual(code, expect, lines)
        self.assertBounded(lines)
        return reply.get("session"), lines

    def wait(self, session, expect=0, timeout=20):
        code, lines, record = B.cmd_wait(ROOT, self.environ, session, timeout, poll_seconds=0.05)
        self.assertEqual(code, expect, lines)
        self.assertBounded(lines)
        return record, lines

    def simple(self, op, session, expect=0, **arguments):
        code, lines, reply = B.cmd_simple(ROOT, self.environ, op, session, **arguments)
        self.assertEqual(code, expect, lines)
        self.assertBounded(lines)
        return reply, lines

    def entries(self, kind, method=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["kind"] == kind and (method is None or row.get("method") == method)]

    def record(self, session):
        return B.load_record(self.state, session)

    def until(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("condition not reached")

    def events(self, session, last=50):
        lines = []
        B.cmd_events(ROOT, self.environ, session, False, last, 0, lines.append)
        return lines

    def assertTurnPins(self, params, policy="never"):
        self.assertEqual(params["model"], "gpt-reserve")
        self.assertEqual(params["approvalPolicy"], policy)
        self.assertEqual(params["approvalsReviewer"], "user")
        self.assertEqual(params["serviceTier"], "default")
        self.assertEqual(params["effort"], "low")


class BrokerSessionTest(BrokerHarness):
    def test_start_follow_up_turn_and_stop_keep_every_pin(self):
        session, lines = self.start()
        self.assertIn(f"session={session}", lines[0])
        record, lines = self.wait(session)
        self.assertEqual(record["turns"][-1]["verdict"], "PASS")
        self.assertIn("final=", "\n".join(lines))
        reply, _ = self.simple("send", session, text="Now say OK again, using what you said before.")
        self.assertEqual(reply["verdict"], "STARTED")
        self.assertEqual(reply["turn"], 2)
        record, _ = self.wait(session)
        self.assertEqual([turn["verdict"] for turn in record["turns"]], ["PASS", "PASS"])
        self.assertGreater(record["turns"][1]["rollout_start"], 0)
        starts = self.entries("request", "turn/start")
        self.assertEqual(len(starts), 2)
        for start in starts:
            self.assertTurnPins(start["params"])
            self.assertEqual(start["params"]["threadId"], THREAD)
        # Each new turn is preceded by its own zero-token admission read.
        self.assertGreaterEqual(len(self.entries("request", "account/rateLimits/read")), 4)
        self.assertTrue((self.state / "sessions" / session / "turns" / "2" / "audit.json").is_file())
        reply, lines = self.simple("stop", session)
        self.assertEqual(reply["state"], "stopped")
        self.assertIn("stop_audit=PASS", lines[0])
        record = self.record(session)
        self.assertEqual(record["stop_audit"]["verdict"], "PASS")
        code, lines, _ = B.cmd_list(ROOT, self.environ, 10)
        self.assertBounded(lines)
        self.assertTrue(any(session in line and " stopped " in line for line in lines), lines)
        code, lines, _ = B.cmd_read(ROOT, self.environ, session, 0, 5)
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("final="), lines)
        self.assertBounded(lines)
        code, lines, _ = B.cmd_read(ROOT, self.environ, session, 3, 5)
        self.assertEqual(code, 0)
        self.assertBounded(lines)
        self.assertEqual(self.entries("forbidden-invocation"), [])
        self.assertNotIn("someone@example.invalid",
                         (self.state / "sessions" / session / "transcript.jsonl").read_text(encoding="utf-8"))

    def test_steer_changes_the_running_turn(self):
        self.scenario["turns"] = [{"complete_on_steer": True}]
        self.write_scenario()
        session, _ = self.start()
        self.assertEqual(self.record(session)["state"], "running")
        reply, lines = self.simple("send", session, text="Change of orders: reply STEERED.")
        self.assertEqual(reply["verdict"], "STEERED")
        record, lines = self.wait(session)
        self.assertEqual(record["turns"][-1]["verdict"], "PASS")
        self.assertIn("STEERED", "\n".join(lines))
        (steer,) = self.entries("request", "turn/steer")
        self.assertEqual(steer["params"]["expectedTurnId"], "turn-1")
        self.assertEqual(set(steer["params"]), {"threadId", "expectedTurnId", "input"})
        # An explicit steer on an idle session is refused, not turned into a new turn.
        self.simple("send", session, expect=L.EXIT_PREFLIGHT_REFUSED, text="late", steer=True)
        self.assertEqual(len(self.entries("request", "turn/start")), 1)

    def test_interrupt_leaves_a_clean_session_for_follow_up(self):
        self.scenario["turns"] = [{"hang": True}]
        self.write_scenario()
        session, _ = self.start()
        self.simple("interrupt", session)
        record, _ = self.wait(session, expect=B.EXIT_INTERRUPTED)
        self.assertEqual(record["turns"][-1]["status"], "interrupted")
        self.assertEqual(record["turns"][-1]["verdict"], "INTERRUPTED")
        self.assertEqual(record["state"], "idle")
        self.simple("send", session, text="Say OK.")
        record, _ = self.wait(session)
        self.assertEqual(record["turns"][-1]["verdict"], "PASS")
        self.assertFalse((self.state / L.TRIPWIRE_NAME).exists())

    def test_detail_levels_filter_the_event_feed(self):
        session, _ = self.start()
        self.wait(session)
        silent = self.events(session)
        self.assertTrue(silent)
        self.assertFalse(any("command:" in line or "message:" in line or "final:" in line for line in silent), silent)
        self.assertTrue(any("turn: turn 1 completed verdict=PASS" in line for line in silent), silent)
        B.cmd_detail(ROOT, self.environ, session, "summary")
        summary = self.events(session)
        self.assertTrue(any("final:" in line for line in summary), summary)
        self.assertFalse(any("command:" in line for line in summary), summary)
        code, lines, _ = B.cmd_detail(ROOT, self.environ, session, "live")
        self.assertIn("detail=live", lines[0])
        live = self.events(session)
        self.assertTrue(any("command: start: sleep 15" in line for line in live), live)
        self.assertTrue(any("command: exit=0" in line for line in live), live)
        self.assertFalse(any("tok" == line.rsplit(" ", 1)[-1] for line in live), live)
        self.assertBounded(live[-MAX_LINES:])
        for line in live:
            self.assertLessEqual(len(line), MAX_WIDTH)
        followed: list[str] = []
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(code=B.cmd_events(
            ROOT, self.environ, session, True, 20, 0, followed.append, poll_seconds=0.05, timeout=30)))
        thread.start()
        self.simple("send", session, text="Say OK.")
        self.wait(session)
        self.simple("stop", session)
        thread.join(30)
        self.assertEqual(result.get("code"), 0, followed)
        self.assertTrue(followed[-1].endswith("ended: stopped"), followed)
        self.assertEqual(B._verdict_code(self.record(session)), 0)
        self.assertTrue(any("turn 2 started" in line for line in followed), followed)

    def test_approval_is_queued_until_the_master_answers(self):
        self.scenario["turns"] = [{"approval": {"method": "item/commandExecution/requestApproval", "params": {
            "itemId": "i1", "command": "git commit -m x", "cwd": str(self.target), "reason": "needs .git",
            "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["git"]}},
                                   "cancel"]}}}]
        self.write_scenario()
        session, _ = self.start(write=True)
        record, lines = self.wait(session, expect=B.EXIT_ATTENTION)
        self.assertEqual(record["pending_approvals"][0]["id"], "a1")
        self.assertIn("git commit -m x", "\n".join(lines))
        self.assertIn("[accept|cancel]", "\n".join(lines))
        time.sleep(0.5)
        self.assertEqual(self.entries("server-request-reply"), [], "an approval was answered without the master")
        self.assertEqual(self.record(session)["state"], "running")
        self.simple("approve", session, expect=B.EXIT_USAGE, approval="a9", decision="accept")
        # A decision the server did not offer is refused without answering.
        _, lines = self.simple("approve", session, expect=B.EXIT_USAGE, approval="a1", decision="decline")
        self.assertIn("not offered", "\n".join(lines))
        self.assertEqual(self.entries("server-request-reply"), [])
        reply, lines = self.simple("approve", session, approval="a1", decision="accept")
        self.assertIn("a1=accept", lines[0])
        record, lines = self.wait(session)
        self.assertIn("APPROVAL=accept", "\n".join(lines))
        (answer,) = self.entries("server-request-reply")
        self.assertEqual(answer["result"], {"decision": "accept"})
        launches = self.entries("launch")
        self.assertTrue(launches)
        for launch in launches:
            self.assertIn('approval_policy="on-request"', launch["argv"])
            self.assertIn('approvals_reviewer="user"', launch["argv"])
        (thread_start,) = self.entries("request", "thread/start")
        self.assertEqual(thread_start["params"]["approvalPolicy"], "on-request")
        self.assertEqual(thread_start["params"]["approvalsReviewer"], "user")
        self.assertTurnPins(self.entries("request", "turn/start")[0]["params"], "on-request")

    def test_read_only_session_keeps_the_decline_policy(self):
        self.scenario["turns"] = [{"approval": {"method": "item/commandExecution/requestApproval", "params": {
            "itemId": "i1", "command": "touch x"}}}]
        self.write_scenario()
        session, _ = self.start()
        record, _ = self.wait(session)
        self.assertEqual(record["pending_approvals"], [])
        (answer,) = self.entries("server-request-reply")
        self.assertEqual(answer["result"], {"decision": "decline"})
        self.assertIn('approval_policy="never"', self.entries("launch")[-1]["argv"])

    def test_live_attribution_mismatch_interrupts_and_stops_every_session(self):
        bystander, _ = self.start()
        self.wait(bystander)
        self.scenario["turn_notifications"] = [self.live(self.regular_reset)]
        self.scenario["hang"] = True
        self.write_scenario()
        offender, _ = self.start(expect=0)
        self.until(lambda: (self.record(offender) or {}).get("state") == "tripped")
        self.until(lambda: (self.record(bystander) or {}).get("state") in ("stopped", "tripped"))
        self.assertTrue((self.state / L.TRIPWIRE_NAME).exists())
        self.assertEqual(len(self.entries("request", "turn/interrupt")), 1)
        record, lines = self.wait(offender, expect=L.EXIT_ATTRIBUTION_FAILED)
        self.assertEqual(record["turns"][-1]["verdict"], "ATTRIBUTION_FAILED")
        self.assertIn(L.STOP_MESSAGE, lines)
        self.assertIn("stop: tripwire", self.record(bystander)["note"])
        self.assertTrue(any("billing-alarm" in line for line in self.events(offender)))
        _, lines = self.start(expect=L.EXIT_PREFLIGHT_REFUSED)
        self.assertTrue(any("attribution failure" in line for line in lines), lines)
        turn_starts = len(self.entries("request", "turn/start"))
        self.simple("send", bystander, expect=L.EXIT_PREFLIGHT_REFUSED, text="more")
        self.assertEqual(len(self.entries("request", "turn/start")), turn_starts)

    def test_tripwire_recorded_by_another_run_stops_open_sessions(self):
        session, _ = self.start()
        self.wait(session)
        L.record_tripwire(self.state, "one-shot-run", "other-thread", ["recorded elsewhere"])
        self.until(lambda: (self.record(session) or {}).get("state") == "stopped", timeout=15)
        self.assertIn("stop: tripwire", self.record(session)["note"])
        self.assertTrue(any("billing-alarm" in line for line in self.events(session)))

    def test_turn_timeout_interrupts_and_is_a_codex_failure(self):
        self.scenario["hang"] = True
        self.write_scenario()
        session, _ = self.start(timeout=1)
        record, _ = self.wait(session, expect=L.EXIT_CODEX_FAILED)
        self.assertTrue(record["turns"][-1]["timed_out"])
        self.assertEqual(record["turns"][-1]["verdict"], "CODEX_FAILED")
        self.assertEqual(len(self.entries("request", "turn/interrupt")), 1)
        self.assertFalse((self.state / L.TRIPWIRE_NAME).exists())

    def test_app_server_crash_mid_turn_is_an_attribution_failure(self):
        self.scenario["turns"] = [{"crash": True}]
        self.write_scenario()
        session, _ = self.start()
        self.until(lambda: (self.record(session) or {}).get("state") == "tripped")
        self.assertTrue((self.state / L.TRIPWIRE_NAME).exists())

    def test_cli_start_refuses_override_flags_before_any_process(self):
        brief = self.base / "brief.md"
        brief.write_text("Say OK.\n", encoding="utf-8")
        old = dict(os.environ)
        os.environ.update(self.environ)
        try:
            for attempt in (["--model", "gpt-5.6-luna"], ["-c", 'model="x"'], ["--fast"], ["--profile", "x"]):
                with self.subTest(attempt=attempt):
                    output = StringIO()
                    with redirect_stdout(output), redirect_stderr(StringIO()):
                        code = main(["luna-reserve", "start", "--brief", str(brief), "--target", str(self.target),
                                     *attempt])
                    self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED, output.getvalue())
                    self.assertIn("override refused", output.getvalue())
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertFalse(self.log.exists())
        self.assertFalse(B.socket_path(self.state).exists())


class BrokerProcessTest(BrokerHarness):
    def broker_info(self):
        return B.read_json(B.info_path(self.state))

    def test_socket_and_directories_are_private_and_foreign_peers_are_refused(self):
        reply = B.ensure_broker(ROOT, self.environ)
        self.assertEqual(reply["uid"], os.getuid())
        for directory in (self.state, B.broker_dir(self.state), B.sessions_dir(self.state)):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700, directory)
        mode = os.lstat(B.socket_path(self.state)).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(B.info_path(self.state)).st_mode), 0o600)
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left, right:
            self.assertEqual(B.peer_uid(left), os.getuid())
        broker = B.Broker(ROOT, self.base / "other-state", "t", self.environ,
                          peer_uid_function=lambda connection: os.getuid() + 1)
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left:
            right.sendall(b'{"op": "ping"}\n')
            broker.handle(left)
            answer = json.loads(right.recv(4096))
            right.close()
        self.assertFalse(answer["ok"])
        self.assertIn("peer uid refused", answer["error"])

    def test_stale_broker_is_replaced_not_attached(self):
        B.private_dir(self.state)
        B.private_dir(B.broker_dir(self.state))
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(B.socket_path(self.state)))
        stale.close()  # a socket file nobody listens on
        B.write_private_json(B.info_path(self.state), {"pid": os.getpid(), "instance": "deadbeefdeadbeef"})
        reply = B.ensure_broker(ROOT, self.environ)
        self.assertNotEqual(reply["instance"], "deadbeefdeadbeef")
        self.assertIn("not signalled", " ".join(reply["notes"]))
        self.assertIn("removed stale socket", reply["notes"])
        first = self.broker_info()
        # A recorded broker that is alive but unresponsive is stopped only by its instance token.
        B.cmd_shutdown(ROOT, self.environ)
        hung = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "--instance", "feedfacefeedface"])
        try:
            B.write_private_json(B.info_path(self.state), {"pid": hung.pid, "instance": "feedfacefeedface"})
            reply = B.ensure_broker(ROOT, self.environ)
            self.assertIn(f"stopped unresponsive broker pid {hung.pid}", reply["notes"])
            self.assertIsNotNone(hung.wait(timeout=10))
            self.assertNotEqual(reply["pid"], first["pid"])
        finally:
            if hung.poll() is None:
                hung.kill()

    def test_registry_survives_a_broker_crash_and_the_thread_resumes(self):
        session, _ = self.start()
        self.wait(session)
        info = self.broker_info()
        app_server = self.record(session)["app_server_pid"]
        os.kill(info["pid"], signal.SIGKILL)
        code, lines, listing = B.cmd_list(ROOT, self.environ, 10)
        self.assertIn("broker=not running", lines[0])
        self.assertTrue(any(session in line and " lost " in line for line in lines), lines)
        code, lines, reply = B.cmd_resume(ROOT, self.environ, THREAD, None, False, None, "silent",
                                          L.Policy().__dict__, [], 60)
        self.assertEqual(code, 0, lines)
        self.assertBounded(lines)
        resumed = reply["session"]
        self.assertNotEqual(resumed, session)
        self.assertIn(f"resumed_from={session}", lines[0])
        self.assertEqual(self.record(session)["state"], "lost")
        self.until(lambda: not B._pid_alive(app_server))
        (resume,) = self.entries("request", "thread/resume")
        params = resume["params"]
        self.assertEqual(params["threadId"], THREAD)
        self.assertEqual(params["model"], "gpt-reserve")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["approvalsReviewer"], "user")
        self.assertEqual(params["sandbox"], "read-only")
        self.assertNotIn("ephemeral", params)
        self.simple("send", resumed, text="Follow-up after restart.")
        record, _ = self.wait(resumed)
        self.assertEqual(record["turns"][-1]["verdict"], "PASS")
        self.assertEqual(record["resumed_from"], session)
        self.simple("stop", resumed)
        self.assertEqual(self.record(resumed)["stop_audit"]["verdict"], "PASS")

    def test_resume_audits_a_turn_orphaned_by_a_crash(self):
        self.scenario["turns"] = [{"hang": True}]
        self.write_scenario()
        session, _ = self.start()
        os.kill(self.broker_info()["pid"], signal.SIGKILL)
        self.scenario["turns"] = []
        self.write_scenario()
        code, lines, reply = B.cmd_resume(ROOT, self.environ, THREAD, None, False, None, "silent",
                                          L.Policy().__dict__, [], 60)
        self.assertEqual(code, 0, lines)
        orphan = self.record(session)["turns"][0]
        self.assertEqual(orphan["verdict"], "AUDITED_AFTER_LOSS")
        self.assertEqual(orphan["rollout_audit"], "PASS")

    def test_a_broker_on_other_code_is_not_driven(self):
        first = B.ensure_broker(ROOT, self.environ)
        original = B.code_digest
        try:
            B.code_digest = lambda root: "0000000000000000"
            reply = B.ensure_broker(ROOT, self.environ)
        finally:
            B.code_digest = original
        self.assertNotEqual(reply["pid"], first["pid"])
        self.assertTrue(any("running other code" in note for note in reply["notes"]), reply["notes"])
        session, _ = self.start()
        self.wait(session)
        B.code_digest = lambda root: "0000000000000000"
        try:
            with self.assertRaises(B.BrokerError):
                B.ensure_broker(ROOT, self.environ)
        finally:
            B.code_digest = original

    def test_broker_exits_after_idle_period_without_live_sessions(self):
        self.environ[B.IDLE_ENV] = "1"
        reply = B.ensure_broker(ROOT, self.environ)
        self.until(lambda: not B._pid_alive(reply["pid"]), timeout=15)
        self.assertFalse(B.socket_path(self.state).exists())
        self.assertIsNone(B.probe_broker(self.state))

    def test_idle_session_is_parked_and_then_the_broker_exits(self):
        self.environ[B.IDLE_ENV] = "1"
        self.environ[B.SESSION_IDLE_ENV] = "1"
        session, _ = self.start()
        self.wait(session)
        pid = self.broker_info()["pid"]
        self.until(lambda: (self.record(session) or {}).get("state") == "stopped", timeout=15)
        self.assertIn("idle", self.record(session)["note"])
        self.until(lambda: not B._pid_alive(pid), timeout=15)


class SendAfterCompletionRaceTest(unittest.TestCase):
    def test_send_after_completion_transition_starts_a_new_turn(self):
        """The completion may win between a caller's observation and its send."""
        broker = B.Broker.__new__(B.Broker)

        class EndingSession:
            id = "lr-test"
            state = "running"

            def send(self, text):
                # Models end_turn acquiring the session lock before the send
                # operation: the atomic session method must start turn 2.
                self.state = "idle"
                return 0, {"verdict": "STARTED", "turn": 2}

            def steer(self, text):
                raise AssertionError("the stale pre-check routed send to steer")

        session = EndingSession()
        broker.session = lambda _session_id: session
        reply = broker.dispatch({"op": "send", "session": "lr-test", "text": "follow up"})
        self.assertEqual(reply["code"], 0)
        self.assertEqual(reply["verdict"], "STARTED")
        self.assertEqual(reply["turn"], 2)


class BuildApprovalRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="luna-approval-")
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "repo"
        self.target.mkdir()
        self.header_file = self.target / "Example.lean"
        self.header_file.write_text(
            "theorem keep : Nat := 1\n"
            "theorem remove : Nat := 2\n",
            encoding="utf-8",
        )
        self.git("init", "-q")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "Luna tests")
        self.git("add", "Example.lean")
        self.git("commit", "-qm", "baseline")
        self.record = {
            "state": "running", "target": str(self.target), "lean": {"goal": "goal"},
        }

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.target), *arguments], check=True,
                              capture_output=True, text=True)

    def approval(self, command=None, method="item/commandExecution/requestApproval", cwd=None):
        if command is None:
            command = "/bin/zsh -lc '~/creme/scripts/creme lake-build goal -- Module.Name'"
        return {"method": method, "command": command, "cwd": str(self.target if cwd is None else cwd),
                "summary": f"command `{command}` in {self.target if cwd is None else cwd}"}

    def assertRejected(self, approval, text=None, record=None):
        failure = B.build_approval_failure(record or self.record, approval, None, [], set())
        self.assertIsNotNone(failure)
        if text is not None:
            self.assertIn(text, failure)

    def test_exact_command_is_accepted_with_or_without_wait(self):
        for command in (
            "/bin/zsh -lc '~/creme/scripts/creme lake-build goal -- Module.Name'",
            "/bin/zsh -lc '~/creme/scripts/creme lake-build goal --wait 900 -- Module.Name'",
        ):
            with self.subTest(command=command):
                self.assertIsNone(B.build_approval_failure(
                    self.record, self.approval(command), None, [], set()))

    def test_command_variants_are_refused(self):
        commands = [
            "~/creme/scripts/creme lake-build goal -- Module.Name | echo ok",
            "~/creme/scripts/creme lake-build goal -- Module.Name && echo ok",
            "~/creme/scripts/creme lake-build goal -- Module.Name; echo ok",
            "~/creme/scripts/creme lake-build goal -- Module.Name > output",
            "~/creme/scripts/creme lake-build goal --memory-gib 4 -- Module.Name",
            "~/creme/scripts/creme lake-build goal --contention sensitive -- Module.Name",
            "~/creme/scripts/creme lake-build goal --probe -- Module.Name",
        ]
        for inner in commands:
            with self.subTest(inner=inner):
                self.assertRejected(self.approval(f"/bin/zsh -lc '{inner}'"))

    def test_wait_bounds_goal_and_cwd_are_enforced(self):
        for wait in (0, 901):
            with self.subTest(wait=wait):
                self.assertRejected(self.approval(
                    f"/bin/zsh -lc '~/creme/scripts/creme lake-build goal --wait {wait} -- Module.Name'",
                ), "--wait")
        self.assertRejected(self.approval(
            "/bin/zsh -lc '~/creme/scripts/creme lake-build other-goal -- Module.Name'"), "allowed")
        self.assertRejected(self.approval(cwd=self.target.parent), "working directory")

    def test_session_and_approval_kind_are_enforced(self):
        non_lean = {"state": "running", "target": str(self.target)}
        self.assertRejected(self.approval(), "Lean", non_lean)
        self.assertRejected(self.approval(method="item/fileChange/requestApproval"), "command-execution")
        self.assertRejected(self.approval(), "running", {**self.record, "state": "idle"})

    def test_header_drift_and_removal_rules_are_fail_closed(self):
        self.header_file.write_text(
            "theorem keep : Int := 1\n"
            "theorem remove : Nat := 2\n",
            encoding="utf-8",
        )
        failures = B.header_rule_failures(self.target, "HEAD", ["Example.lean"], set())
        self.assertTrue(any("header changed" in failure for failure in failures), failures)

        self.header_file.write_text("theorem keep : Nat := 1\n", encoding="utf-8")
        failures = B.header_rule_failures(self.target, "HEAD", ["Example.lean"], set())
        self.assertTrue(any("header removed" in failure for failure in failures), failures)
        self.assertEqual(B.header_rule_failures(self.target, "HEAD", ["Example.lean"], {"remove"}), [])

    def test_unreadable_ref_and_missing_header_file_are_refused(self):
        missing = self.target / "Untracked.lean"
        missing.write_text("theorem new : Nat := 1\n", encoding="utf-8")
        failures = B.header_rule_failures(self.target, "HEAD", ["Untracked.lean"], set())
        self.assertTrue(any("cannot read header file" in failure for failure in failures), failures)
        failures = B.header_rule_failures(self.target, "HEAD", [], set())
        self.assertIn("requires at least one", failures[0])


class FollowUpPinTest(unittest.TestCase):
    def session(self, sandbox="read-only", roots=(), policy="never"):
        from creme.codex_app_server import GuardedSession

        session = GuardedSession(process=None, pinned_model="gpt-reserve", attribution=lambda snap: (True, ""),
                                 cwd=Path("/launch/creme"), sandbox=sandbox, effort="low", writable_roots=roots,
                                 developer_instructions="contract", approval_policy=policy)
        session.thread_id = "t"
        return session

    def turn(self, **changes):
        return {"threadId": "t", "input": [], "model": "gpt-reserve", "effort": "low", "serviceTier": "default",
                "approvalPolicy": "never", "approvalsReviewer": "user", **changes}

    def test_follow_up_turns_steers_and_resumes_are_pinned(self):
        from creme.codex_app_server import PinViolation

        session = self.session()
        session.check_params("turn/start", self.turn())
        session.check_params("thread/resume", session.resume_parameters())
        session.active_turn = "u"
        session.check_params("turn/steer", {"threadId": "t", "expectedTurnId": "u", "input": []})
        refused = [
            ("turn/steer", {"threadId": "t", "expectedTurnId": "other", "input": []}),
            ("turn/steer", {"threadId": "x", "expectedTurnId": "u", "input": []}),
            ("turn/steer", {"threadId": "t", "expectedTurnId": "u", "input": [], "model": "gpt-reserve"}),
            ("turn/steer", {"threadId": "t", "expectedTurnId": "u", "input": [], "serviceTier": "priority"}),
            ("turn/start", self.turn()),  # a turn is already active
            ("turn/interrupt", {"threadId": "x", "turnId": "u"}),
        ]
        for method, params in refused:
            with self.subTest(method=method, params=params), self.assertRaises(PinViolation):
                session.check_params(method, params)
        session.active_turn = None
        for method, params in (
            ("turn/steer", {"threadId": "t", "expectedTurnId": None, "input": []}),
            ("turn/start", self.turn(threadId="x")),
            ("turn/start", self.turn(effort="high")),
            ("turn/start", self.turn(model="gpt-5.6-luna")),
            ("thread/resume", {**session.resume_parameters(), "threadId": "x"}),
            ("thread/resume", {**session.resume_parameters(), "model": "gpt-5.6-luna"}),
            ("thread/resume", {**session.resume_parameters(), "approvalsReviewer": "auto_review"}),
        ):
            with self.subTest(method=method, params=params), self.assertRaises(PinViolation):
                session.check_params(method, params)
        session.guard_failures.append("live snapshot matched the regular bucket")
        session.active_turn = "u"
        for method, params in (("turn/start", self.turn()), ("thread/resume", session.resume_parameters()),
                               ("turn/steer", {"threadId": "t", "expectedTurnId": "u", "input": []})):
            with self.subTest(after_failure=method), self.assertRaises(PinViolation):
                session.check_params(method, params)
        session.check_params("turn/interrupt", {"threadId": "t", "turnId": "u"})

    def test_only_a_write_session_routes_approvals_to_the_master(self):
        from creme.codex_app_server import PinViolation, isolation_arguments

        with self.assertRaises(PinViolation):
            self.session(policy="on-request")
        with self.assertRaises(PinViolation):
            self.session("workspace-write", (Path("/w"),), policy="untrusted")
        write = self.session("workspace-write", (Path("/w"),), policy="on-request")
        write.check_params("thread/start", write.thread_parameters())
        write.check_params("turn/start", self.turn(approvalPolicy="on-request"))
        with self.assertRaises(PinViolation):
            write.check_params("turn/start", self.turn(approvalPolicy="never"))
        self.assertIn('approval_policy="on-request"', isolation_arguments("gpt-reserve", "low", {}, (), "on-request"))
        with self.assertRaises(PinViolation):
            isolation_arguments("gpt-reserve", "low", {}, (), "untrusted")

    def test_broker_module_names_only_the_reserve_slug(self):
        import re

        source = (ROOT / "creme/luna_broker.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"gpt-[0-9a-z.\-]+", source), [])
        self.assertNotIn("auto_review", source)


if __name__ == "__main__":
    unittest.main()
