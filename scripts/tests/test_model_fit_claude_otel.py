"""Claude Code usage joined from OpenTelemetry: controls on fixtures and the 2026-10-03 probe."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from creme import cli
from creme import model_fit_adapters as A
from creme import model_fit_capture as C
from creme import model_fit_episodes as E
from creme import model_fit_runtime as R
from scripts.tests.test_model_fit_claude_antigravity import (OPUS, complete_call, entry, otel_request,
                                                             write_otel)
from scripts.tests.test_model_fit_runtime import base_config

PROBE = Path(__file__).resolve().parent / "fixtures" / "claude_otel"
PROBE_SESSION = "959d5c39-454a-4906-a7b5-18c9d6b78ba2"
PROBE_AGENT = "a60485bc6eea21e6a"


class TelemetryJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = self.root / "project" / "s1"
        self.subagents = self.session / "subagents"
        self.subagents.mkdir(parents=True)
        self.profiles = self.root / "agents"
        self.profiles.mkdir()
        (self.profiles / "worker-high.md").write_text("---\nname: worker-high\neffort: 3\n---\n")
        self.otel = self.root / "otel"

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, agent, rows, tool="toolu_parent"):
        path = self.subagents / f"agent-{agent}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        (self.subagents / f"agent-{agent}.meta.json").write_text(
            json.dumps({"agentType": "worker-high", "toolUseId": tool, "model": "opus"}))
        return path

    def receipt(self, path, **extra):
        extra.setdefault("telemetry", self.otel)
        return A.claude_code_run(path, "ep", "h1", "completed", profiles=self.profiles, **extra)

    def usage(self, receipt):
        return receipt["segments"][0]["usage"]

    def test_telemetry_supplies_final_usage_and_counts_event_and_span_once(self):
        # Two placeholder-only responses: no final usage in the transcript at all.
        path = self.agent("a1", [entry("m1", 1, 3), entry("m2", 2, 8)])
        self.assertFalse(self.receipt(path, telemetry=None)["usage_complete"])
        requests = [otel_request("m1", 1, 40, agent="a1"), otel_request("m2", 2, 60, agent="a1")]
        write_otel(self.otel, requests)
        write_otel(self.otel, requests, name="otlp-20261002.jsonl")  # a retransmitted batch
        receipt = self.receipt(path)
        self.assertTrue(receipt["usage_complete"])
        self.assertEqual((self.usage(receipt)["total_input"], self.usage(receipt)["total_output"]), (230, 100))
        self.assertEqual(self.usage(receipt)["provider_raw"]["telemetry"]["joined"], 2)

    def test_missing_telemetry_is_a_gap_never_zero(self):
        path = self.agent("a1", complete_call("m1", 1, 40) + complete_call("m2", 2, 60))
        for name, telemetry in (("none", None), ("empty", self.root / "nothing")):
            with self.subTest(name):
                receipt = self.receipt(path, telemetry=telemetry)
                self.assertFalse(receipt["usage_complete"])
                self.assertIsNone(self.usage(receipt)["total_output"])
        write_otel(self.otel, [otel_request("m1", 1, 40, agent="a1")])
        receipt = self.receipt(path)
        self.assertFalse(receipt["usage_complete"])
        self.assertIn("1 of 2 responses have no telemetry record", receipt["detail"])

    def test_disagreeing_records_refuse(self):
        cases = {
            "final usage": ([otel_request("m1", 1, 41, agent="a1")], {}),
            "model": ([otel_request("m1", 1, 40, agent="a1", model="claude-sonnet-5-5")], {}),
            "agent": ([otel_request("m1", 1, 40, agent="zz")], {}),
            "session": ([otel_request("m1", 1, 40, agent="a1", session="s2")], {}),
        }
        path = self.agent("a1", complete_call("m1", 1, 40))
        for name, (requests, extra) in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.otel, ignore_errors=True)
                write_otel(self.otel, requests)
                with self.assertRaises(C.CaptureError):
                    self.receipt(path, **extra)
        shutil.rmtree(self.otel)
        write_otel(self.otel, [otel_request("m1", 1, 40, agent="a1")], signals=("traces",))
        write_otel(self.otel, [otel_request("m1", 1, 41, agent="a1")], signals=("logs",))
        with self.assertRaises(C.CaptureError):
            self.receipt(path)

    def test_attributed_request_without_transcript_entry_is_added(self):
        path = self.agent("a1", complete_call("m1", 1, 40))
        write_otel(self.otel, [otel_request("m1", 1, 40, agent="a1"),
                               otel_request("hidden", 5, 7, agent="a1", input_tokens=1, cache_read=0,
                                            cache_creation=0),
                               otel_request("other", 6, 900, agent="b2")])
        receipt = self.receipt(path)
        self.assertTrue(receipt["usage_complete"])
        self.assertEqual((self.usage(receipt)["total_input"], self.usage(receipt)["total_output"]), (116, 47))
        self.assertEqual(self.usage(receipt)["provider_raw"]["telemetry"]["added"], 1)
        # A continuation window excludes the agent's requests outside it.
        windowed = self.receipt(path, until="2026-10-01T10:00:03Z")
        self.assertEqual(self.usage(windowed)["total_output"], 40)
        write_otel(self.otel, [otel_request("side", 7, 3, agent="a1", model="claude-sonnet-5-5")])
        with self.assertRaises(C.CaptureError):
            self.receipt(path)

    def test_unattributable_agent_event_is_a_gap_unless_a_sibling_transcript_has_it(self):
        path = self.agent("a1", complete_call("m1", 1, 40))
        write_otel(self.otel, [otel_request("m1", 1, 40, agent="a1")])
        write_otel(self.otel, [otel_request("lost", 3, 9, query_source="agent:custom:worker-high")],
                   signals=("logs",))
        receipt = self.receipt(path)
        self.assertFalse(receipt["usage_complete"])
        self.assertIn("cannot be attributed", receipt["detail"])
        self.agent("b2", complete_call("lost", 3, 9))
        self.assertTrue(self.receipt(path)["usage_complete"])
        write_otel(self.otel, [otel_request(None, 4, 9, agent="a1")], signals=("traces",))
        self.assertFalse(self.receipt(path)["usage_complete"])

    def test_partial_final_line_is_ignored_and_malformed_line_refuses(self):
        path = self.agent("a1", complete_call("m1", 1, 40))
        write_otel(self.otel, [otel_request("m1", 1, 40, agent="a1")])
        with (self.otel / "otlp-20261001.jsonl").open("a") as handle:
            handle.write('{"signal": "logs", "bo')
        self.assertTrue(self.receipt(path)["usage_complete"])
        with (self.otel / "otlp-20261001.jsonl").open("a") as handle:
            handle.write('dy"\n')
        with self.assertRaises(C.CaptureError):
            self.receipt(path)

    def test_master_window_charges_untranscribed_main_requests(self):
        master = self.root / "master.jsonl"
        rows = complete_call("m1", 10, 7, sidechain=False) + complete_call("m2", 40, 9, sidechain=False)
        master.write_text("".join(json.dumps(row) + "\n" for row in rows))
        write_otel(self.otel, [otel_request("m1", 10, 7), otel_request("m2", 40, 9),
                               otel_request("aux", 12, 20, model="claude-sonnet-5-5", input_tokens=50,
                                            cache_read=0, cache_creation=0, query_source="auxiliary"),
                               otel_request("late", 45, 30, query_source="auxiliary"),
                               otel_request("sub", 13, 500, agent="a1")])
        window = A.claude_master_window(master, "2026-10-01T10:00:05Z", "2026-10-01T10:00:30Z",
                                        telemetry=self.otel)
        usage = window["usage"]
        self.assertEqual((usage["total_input"], usage["total_output"]), (165, 27))
        self.assertEqual(usage["observed_models"], [OPUS, "claude-sonnet-5-5"])
        self.assertEqual(E.normalize_usage(usage)["total"], 192)
        write_otel(self.otel, [otel_request("nospan", 14, 1, query_source="auxiliary")], signals=("logs",))
        gap = A.claude_master_window(master, "2026-10-01T10:00:05Z", "2026-10-01T10:00:30Z",
                                     telemetry=self.otel)
        self.assertIsNone(gap["usage"]["total_output"])
        bad = otel_request("m1", 10, 8)
        shutil.rmtree(self.otel)
        write_otel(self.otel, [bad])
        with self.assertRaises(C.CaptureError):
            A.claude_master_window(master, "2026-10-01T10:00:05Z", "2026-10-01T10:00:30Z", telemetry=self.otel)


class ProbeSession(unittest.TestCase):
    """The 2026-10-03 probe: a sonnet session whose general-purpose subagent replied once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = PROBE / "project" / PROBE_SESSION / "subagents" / f"agent-{PROBE_AGENT}.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe_subagent_receipt_is_complete(self):
        receipt = A.claude_code_run(self.agent, "ep", "h1", "completed", telemetry=PROBE / "claude-otel")
        usage = receipt["segments"][0]["usage"]
        self.assertTrue(receipt["usage_complete"])
        self.assertEqual((receipt["option"], receipt["release"]), ("sonnet/medium", "claude-sonnet-5-5"))
        self.assertEqual((usage["total_input"], usage["cache_write"], usage["total_output"]), (24321, 24319, 4))

    def test_probe_master_window_and_acceptance(self):
        window = A.claude_master_window(PROBE / "project" / f"{PROBE_SESSION}.jsonl", "2026-10-03T19:28:00Z",
                                        "2026-10-03T19:30:00Z", telemetry=PROBE / "claude-otel")
        self.assertEqual((window["usage"]["total_input"], window["usage"]["total_output"]), (61643, 150))
        store = R.open_runtime(self.root / "fit")
        try:
            config = base_config(execution_client="claude-code", default="sonnet/medium", candidates=[{
                "option": "sonnet/medium", "prior_tokens": 100, "release": "claude-sonnet-5-5",
                "recipe_version": "r1", "route": "claude-agent-tool"}])
            R.configure(store, "policy", config, "active")
            R.prepare(store, "ep", "policy", [{"milestone": "done", "credit": 1}], "claude-code", "fixture")
            request = {"receipt_id": "ep:accept", "episode_id": "ep", "verdict": "pass", "milestones": ["done"],
                       "verifier": "master", "worker_ref": PROBE_AGENT, "verification_ref": "fixture",
                       "claude_code_sources": [{"path": str(self.agent), "harness_version": "h1",
                                                "terminal": "completed",
                                                "telemetry": str(PROBE / "claude-otel")}],
                       "claude_code_master_windows": [{"path": str(PROBE / "project" / f"{PROBE_SESSION}.jsonl"),
                                                       "start": "2026-10-03T19:28:00Z",
                                                       "end": "2026-10-03T19:30:00Z",
                                                       "telemetry": str(PROBE / "claude-otel")}]}
            source = self.root / "accept.json"
            source.write_text(json.dumps(request))
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(["model-fit", "episode", "accept", "--dir", str(self.root / "fit"),
                                 "--from", str(source)])
            self.assertEqual((code, errors.getvalue()), (0, ""))
            self.assertEqual(E.get_episode(store, "ep")["status"], "closed")
            self.assertEqual(E.episode_accounting(store, "ep")["spend_uncapped_tokens"], 24325 + 61793)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
