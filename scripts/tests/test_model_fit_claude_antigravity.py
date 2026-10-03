"""Claude Code and Antigravity capture controls on metadata-only fixtures."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from creme import cli
from creme import model_fit_adapters as A
from creme import model_fit_capture as C
from creme import model_fit_episodes as E
from creme import model_fit_runtime as R
from scripts.tests.test_model_fit_runtime import base_config

OPUS = "claude-opus-5-5"


def entry(ident, second, output, stop=None, model=OPUS, effort=None, tools=(), sidechain=True,
          usage=None, iterations=None):
    usage = dict(usage or {"input_tokens": 10, "cache_read_input_tokens": 100,
                           "cache_creation_input_tokens": 5}, output_tokens=output)
    if iterations is not None:
        usage["iterations"] = iterations
    content = [{"type": "tool_use", "id": tool, "name": "Agent", "input": {}} for tool in tools]
    return {"type": "assistant", "isSidechain": sidechain, "perTurnEffort": effort,
            "timestamp": f"2026-10-01T10:00:{second:02d}.000Z",
            "message": {"id": ident, "model": model, "stop_reason": stop, "usage": usage,
                        "content": content}}


def launched(tool, agent, second):
    return {"type": "user", "timestamp": f"2026-10-01T10:00:{second:02d}.000Z",
            "toolUseResult": {"agentId": agent, "status": "async_launched"},
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool}]}}


def complete_call(ident, second, output=40, **extra):
    """A streamed response: a placeholder entry, then two final entries repeating its usage."""
    return [entry(ident, second, 3, **extra), entry(ident, second, output, stop="tool_use", **extra),
            entry(ident, second, output, stop="tool_use", **extra)]


class ClaudeCodeCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.subagents = self.root / "session" / "subagents"
        self.subagents.mkdir(parents=True)
        self.profiles = self.root / "agents"
        self.profiles.mkdir()
        for name, effort in (("worker-high", 3), ("worker-xhigh", 4), ("worker-odd", 1)):
            (self.profiles / f"{name}.md").write_text(f"---\nname: {name}\neffort: {effort}\n---\nbody\n")
        (self.profiles / "worker-none.md").write_text("---\nname: worker-none\n---\nbody\n")
        (self.profiles / "worker-low.md").write_text("---\nname: worker-low\neffort: 5\n---\n")
        self.store = R.open_runtime(self.root / "fit")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def agent(self, agent, rows, agent_type="worker-high", tool="toolu_parent", model="opus"):
        path = self.subagents / f"agent-{agent}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        meta = {"agentType": agent_type, "toolUseId": tool, "spawnDepth": 1}
        if model:
            meta["model"] = model
        (self.subagents / f"agent-{agent}.meta.json").write_text(json.dumps(meta))
        return path

    def run_receipt(self, path, **extra):
        return A.claude_code_run(path, "ep", "h1", "completed", profiles=self.profiles, **extra)

    def test_streamed_response_counts_once_from_final_entry(self):
        path = self.agent("a1", complete_call("m1", 1, 40) + complete_call("m2", 2, 60))
        receipt = self.run_receipt(path)
        usage = receipt["segments"][0]["usage"]
        self.assertTrue(receipt["usage_complete"])
        self.assertEqual((receipt["option"], receipt["release"]), ("opus/high", OPUS))
        self.assertEqual((usage["total_input"], usage["cached_input"], usage["total_output"]), (230, 200, 100))
        self.assertEqual(E.normalize_usage(usage)["total"], 330)

    def test_placeholder_output_is_never_charged(self):
        path = self.agent("a1", complete_call("m1", 1) + [entry("m2", 2, 8)])
        receipt = self.run_receipt(path)
        usage = receipt["segments"][0]["usage"]
        self.assertFalse(receipt["usage_complete"])
        self.assertIsNone(usage["total_output"])
        self.assertIn("1 of 2 responses lack final output", receipt["detail"])

    def test_release_must_be_single_known_and_unfallen(self):
        cases = {"two releases": complete_call("m1", 1) + complete_call("m2", 2, model="claude-sonnet-5-5"),
                 "unknown release": complete_call("m1", 1, model="claude-opus-9"),
                 "fallback": [entry("m1", 1, 3, model="claude-fable-5-1"),
                              entry("m1", 1, 9, stop="end_turn", model="claude-opus-4-8")]}
        for name, rows in cases.items():
            with self.subTest(name):
                with self.assertRaises(C.CaptureError):
                    self.run_receipt(self.agent("a1", rows, model=None))
        fallback = [{"input_tokens": 1, "output_tokens": 2, "type": "message", "model": "claude-fable-5-1"},
                    {"input_tokens": 1, "output_tokens": 3, "type": "fallback_message", "model": "claude-opus-4-8"}]
        with self.assertRaises(C.CaptureError):
            self.run_receipt(self.agent("a1", [entry("m1", 1, 3, stop="end_turn", iterations=fallback)]))

    def test_effort_comes_from_profile_and_disagreement_refuses(self):
        rows = complete_call("m1", 1)
        self.assertEqual(self.run_receipt(self.agent("a1", rows, "worker-xhigh"))["option"], "opus/xhigh")
        for agent_type in ("worker-none", "worker-low"):
            with self.subTest(agent_type), self.assertRaises(C.CaptureError):
                self.run_receipt(self.agent("a1", rows, agent_type))
        builtin = complete_call("m1", 1, effort="medium")
        self.assertEqual(self.run_receipt(self.agent("a1", builtin, "general-purpose"))["option"], "opus/medium")
        with self.assertRaises(C.CaptureError):
            self.run_receipt(self.agent("a1", rows, "general-purpose"))
        with self.assertRaises(C.CaptureError):
            self.run_receipt(self.agent("a1", complete_call("m1", 1, effort="low"), "worker-high"))

    def test_nested_child_is_part_of_run_and_missing_child_is_a_gap(self):
        parent = complete_call("m1", 1, tools=("toolu_child",)) + [launched("toolu_child", "c1", 2)]
        path = self.agent("a1", parent)
        self.agent("c1", complete_call("m9", 3, 1000, model="claude-sonnet-5-5"),
                   "worker-high", "toolu_child", "sonnet")
        receipt = self.run_receipt(path)
        usage = receipt["segments"][0]["usage"]
        self.assertTrue(receipt["usage_complete"])
        self.assertEqual((usage["total_output"], usage["child_models"]), (1040, ["claude-sonnet-5-5"]))
        (self.subagents / "agent-c1.jsonl").unlink()
        receipt = self.run_receipt(path)
        self.assertFalse(receipt["usage_complete"])
        self.assertIsNone(receipt["segments"][0]["usage"]["total_output"])
        self.assertIn("child agent c1 transcript is missing", receipt["detail"])
        unresolved = self.agent("a2", complete_call("m1", 1, tools=("toolu_lost",)))
        self.assertFalse(self.run_receipt(unresolved)["usage_complete"])

    def test_master_window_selects_main_responses_and_compaction_is_unknown(self):
        path = self.root / "master.jsonl"
        rows = (complete_call("m1", 1, 7, sidechain=False) + complete_call("m2", 20, 11, sidechain=False)
                + complete_call("s1", 21, 500, sidechain=True) + complete_call("m3", 40, 13, sidechain=False))
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        window = A.claude_master_window(path, "2026-10-01T10:00:10Z", "2026-10-01T10:00:30Z")
        self.assertEqual((window["usage"]["total_input"], window["usage"]["total_output"]), (115, 11))
        E.record_master_segment(self.store, window["id"], "claude-code", window["usage"])
        overlap = A.claude_master_window(path, "2026-10-01T10:00:25Z", "2026-10-01T10:00:45Z")
        with self.assertRaises(E.EpisodeError):
            E.record_master_segment(self.store, overlap["id"], "claude-code", overlap["usage"])
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "system", "subtype": "compact_boundary",
                                     "timestamp": "2026-10-01T10:00:50.000Z"}) + "\n")
        later = A.claude_master_window(path, "2026-10-01T10:00:45Z", "2026-10-01T10:00:55Z")
        self.assertIsNone(E.normalize_usage(later["usage"])["total"])

    def test_cli_acceptance_imports_claude_run_and_master_window(self):
        config = base_config(execution_client="claude-code", default="opus/high", candidates=[{
            "option": "opus/high", "prior_tokens": 100, "release": OPUS,
            "recipe_version": "r1", "route": "claude-agent-tool"}])
        R.configure(self.store, "policy", config, "active")
        R.prepare(self.store, "ep", "policy", [{"milestone": "done", "credit": 1}], "claude-code", "fixture")
        worker = self.agent("a1", complete_call("m1", 1, 40))
        master = self.root / "master.jsonl"
        master.write_text("".join(json.dumps(row) + "\n" for row in complete_call("x1", 30, 5, sidechain=False)))
        request = {"receipt_id": "ep:accept", "episode_id": "ep", "verdict": "pass", "milestones": ["done"],
                   "verifier": "master", "worker_ref": "a1", "verification_ref": "fixture",
                   "claude_code_sources": [{"path": str(worker), "harness_version": "h1",
                                            "terminal": "completed", "profiles": str(self.profiles)}],
                   "claude_code_master_windows": [{"path": str(master), "start": "2026-10-01T10:00:25Z",
                                                   "end": "2026-10-01T10:00:35Z"}]}
        source = self.root / "accept.json"
        source.write_text(json.dumps(request))
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["model-fit", "episode", "accept", "--dir", str(self.root / "fit"),
                             "--from", str(source)])
        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertEqual(E.get_episode(self.store, "ep")["status"], "closed")
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 155 + 120)


def agy_run(root, name="run1", usage=None, steps=None, result=True, init="claude-sonnet-5-5-low",
            extra=()):
    run_dir = Path(root) / name
    run_dir.mkdir()
    usage = usage or {"input_tokens": 9010, "output_tokens": 245, "thinking_tokens": 0,
                      "cache_read_tokens": 14086, "total_tokens": 9255}
    steps = [usage] if steps is None else steps
    events = [{"event": "init", "conversation_id": "c1", "init": {"model": init}}]
    events += [{"event": "step_update", "step_update": {"step_index": index, "step_type": "agent_response",
                                                        "usage": step}} for index, step in enumerate(steps)]
    events += list(extra)
    if result:
        events.append({"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS",
                                                     "usage": usage}})
    (run_dir / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    (run_dir / "verdict.json").write_text(json.dumps({"run": name, "family": "claude-sonnet-5-5",
                                                      "effort": "low", "init_model": init}))
    return run_dir


class AntigravityCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_result_usage_adds_disjoint_cache_reads_once(self):
        receipt = A.antigravity_run(agy_run(self.root), "ep", "h1")
        usage = receipt["segments"][0]["usage"]
        self.assertTrue(receipt["usage_complete"])
        self.assertEqual((receipt["option"], receipt["release"], receipt["route"], receipt["terminal"]),
                         ("claude-sonnet-5-5/low", "claude-sonnet-5-5-low", "antigravity-run", "completed"))
        self.assertEqual(E.normalize_usage(usage), {"input": 23096, "output": 245, "total": 23341})

    def test_missing_result_and_unreported_steps_are_gaps(self):
        checkpoint = {"event": "step_update", "step_update": {"step_index": 9, "step_type": "checkpoint"}}
        subagent = {"event": "step_update", "step_update": {"step_index": 8, "step_type": "subagent",
                                                            "state": "DONE",
                                                            "subagent_info": {"subagents": ["s"]}}}
        for name, run_dir in (("no-result", agy_run(self.root, "a", result=False)),
                              ("checkpoint", agy_run(self.root, "b", extra=[checkpoint])),
                              ("subagent", agy_run(self.root, "c", extra=[subagent]))):
            with self.subTest(name):
                receipt = A.antigravity_run(run_dir, "ep", "h1")
                self.assertFalse(receipt["usage_complete"])
                self.assertIn("usage gap", receipt["detail"])
        failed = {"event": "step_update", "step_update": {"step_index": 8, "step_type": "subagent",
                                                          "state": "ERROR", "subagent_info": {"subagents": []}}}
        self.assertTrue(A.antigravity_run(agy_run(self.root, "d", extra=[failed]), "ep", "h1")["usage_complete"])

    def test_inconsistent_records_refuse(self):
        bad_total = {"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 0,
                     "cache_read_tokens": 4, "total_tokens": 7}
        partial = {"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 0,
                   "cache_read_tokens": 4, "total_tokens": 3}
        cases = {"init": agy_run(self.root, "a", init="claude-opus-5-5-low"),
                 "total": agy_run(self.root, "b", usage=bad_total),
                 "steps": agy_run(self.root, "c", usage=partial, steps=[])}
        for name, run_dir in cases.items():
            with self.subTest(name), self.assertRaises(C.CaptureError):
                A.antigravity_run(run_dir, "ep", "h1")

    def test_cli_acceptance_imports_antigravity_run(self):
        store = R.open_runtime(self.root / "fit")
        try:
            config = base_config(execution_client="antigravity", default="claude-sonnet-5-5/low", candidates=[{
                "option": "claude-sonnet-5-5/low", "prior_tokens": 100, "release": "claude-sonnet-5-5-low",
                "recipe_version": "r1", "route": "antigravity-run"}])
            R.configure(store, "policy", config, "active")
            R.prepare(store, "ep", "policy", [{"milestone": "done", "credit": 1}], "claude-code", "fixture")
            request = {"receipt_id": "ep:accept", "episode_id": "ep", "verdict": "pass", "milestones": ["done"],
                       "verifier": "master", "worker_ref": "run1", "verification_ref": "fixture",
                       "antigravity_runs": [{"run_dir": str(agy_run(self.root)), "harness_version": "h1"}],
                       "master_segments": [{"id": "m", "client": "claude-code", "weight": 1,
                                            "usage": {"total_input": 10, "total_output": 5}}]}
            source = self.root / "accept.json"
            source.write_text(json.dumps(request))
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(["model-fit", "episode", "accept", "--dir", str(self.root / "fit"),
                                 "--from", str(source)])
            self.assertEqual((code, errors.getvalue()), (0, ""))
            self.assertEqual(E.get_episode(store, "ep")["status"], "closed")
            self.assertEqual(E.episode_accounting(store, "ep")["spend_uncapped_tokens"], 23341 + 15)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
