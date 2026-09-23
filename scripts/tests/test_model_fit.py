from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from creme import model_fit
from creme.cli import main

ROOT = Path(__file__).resolve().parents[2]

GOOD = {
    "task_type": "lean-elaboration",
    "option": "opus/high",
    "route": "claude-agent-tool (profile worker-opus-high)",
    "goal": "example-goal-v1",
    "date": "2026-09-18",
    "run": "agent-a0000000000000001",
    "source": "~/.claude/projects/example/subagents/agent-a0000000000000001.jsonl",
    "verdict": "pass",
    "verdict_source": "$GOAL_STORE/master/events.jsonl#0123456789abcdef",
    "failure_modes": "none",
    "tokens": "uncached_input=10 cache_read=2000 cache_write=300 output=400 reasoning=n/a",
    "wall_time": "900s",
    "turns": "12",
    "retries": "0",
    "rework": "none",
    "recorded_by": "master claude opus/xhigh",
}


def run_cli(*argv: str) -> tuple[int, str]:
    output = StringIO()
    with redirect_stdout(output):
        code = main(list(argv))
    return code, output.getvalue()


class ModelFitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        code, _ = run_cli("model-fit", "init", str(self.dir))
        self.assertEqual(code, 0)
        self.claude = self.dir / "claude-code.md"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add(self, path: Path, **overrides: str) -> tuple[list[str], str]:
        fields = dict(GOOD)
        fields.update(overrides)
        return model_fit.add_observation(path, fields)

    def errors(self) -> list[str]:
        errors, _ = model_fit.validate_dir(self.dir)
        return errors

    def assertRejected(self, needle: str) -> None:
        errors = self.errors()
        self.assertTrue(any(needle in e for e in errors), f"{needle!r} not in {errors}")
        code, out = run_cli("model-fit", "validate", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    # ------------------------------------------------------------ positive

    def test_skeletons_carry_every_row_and_column_and_validate(self):
        self.assertEqual(self.errors(), [])
        for client in model_fit.CLIENTS.values():
            text = (self.dir / f"{client.name}.md").read_text(encoding="utf-8")
            for family, efforts in client.families.items():
                self.assertIn(f"### {family}", text)
                self.assertIn("| task type | " + " | ".join(efforts) + " |", text)
            for task in model_fit.TASK_TYPES:
                self.assertIn(f"| {task} | no data", text)
        code, out = run_cli("model-fit", "validate", str(self.dir))
        self.assertEqual(code, 0, out)

    def test_add_appends_observation_and_derives_the_cell(self):
        for _ in range(3):
            errors, ident = self.add(self.claude)
            self.assertEqual(errors, [])
        self.assertEqual(ident, "cc-0003")
        self.add(self.claude, verdict="unknown", verdict_source="none — no master record names this run")
        text = self.claude.read_text(encoding="utf-8")
        self.assertIn("| lean-elaboration | no data | no data | 3v: 3P 0A 0F +1U guides |", text)
        self.assertIn("- lean-elaboration × opus/high: 3v: 3P 0A 0F +1U guides; output 400; wall 900s", text)
        self.assertEqual(self.errors(), [])
        _, counts = model_fit.validate_dir(self.dir)
        self.assertEqual(counts["claude-code"], {"observations": 4, "verified": 3, "unknown": 1})

    def test_cli_add_from_claude_transcript(self):
        transcript = self.dir / "agent-a1.jsonl"
        rows = [
            {"type": "user", "timestamp": "2026-09-18T10:00:00Z", "message": {"role": "user"}},
            {"type": "assistant", "timestamp": "2026-09-18T10:00:05Z",
             "message": {"id": "m1", "model": "claude-opus-5",
                         "usage": {"input_tokens": 3, "cache_read_input_tokens": 100,
                                   "cache_creation_input_tokens": 50, "output_tokens": 7}}},
            {"type": "assistant", "timestamp": "2026-09-18T10:00:06Z",
             "message": {"id": "m1", "model": "claude-opus-5",
                         "usage": {"input_tokens": 3, "cache_read_input_tokens": 100,
                                   "cache_creation_input_tokens": 50, "output_tokens": 7}}},
            {"type": "assistant", "timestamp": "2026-09-18T10:10:00Z",
             "message": {"id": "m2", "model": "claude-opus-5",
                         "usage": {"input_tokens": 1, "cache_read_input_tokens": 200,
                                   "cache_creation_input_tokens": 0, "output_tokens": 9}}},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        code, out = run_cli(
            "model-fit", "add", str(self.claude), "--from-claude-transcript", str(transcript),
            "--task-type", "fact-finding", "--option", "opus/medium", "--route", "claude-agent-tool",
            "--goal", "g", "--verdict", "pass", "--verdict-source", "$GOAL_STORE/master/events.jsonl#e1",
            "--recorded-by", "master",
        )
        self.assertEqual(code, 0, out)
        self.assertIn("uncached_input=4 cache_read=300 cache_write=50 output=16 reasoning=n/a", out)
        self.assertIn("wall_time: 600s", out)
        self.assertIn("turns: 2", out)
        self.assertEqual(self.errors(), [])
        segment = model_fit.claude_transcript_usage(transcript, until="2026-09-18T10:01:00Z")
        self.assertEqual(segment["turns"], "1")
        self.assertEqual(segment["wall_time"], "6s")

    def test_luna_session_usage_subtracts_the_resumed_base(self):
        sessions = self.dir / "sessions"
        for name, total, resumed in (("lr-a", 1000, None), ("lr-b", 1600, "lr-a")):
            turn = sessions / name / "turns" / "1"
            turn.mkdir(parents=True)
            (turn / "audit.json").write_text(json.dumps({"token_usage": {
                "input_tokens": total, "cached_input_tokens": total // 2, "cache_write_input_tokens": 0,
                "output_tokens": total // 10, "reasoning_output_tokens": total // 20}}), encoding="utf-8")
            (sessions / name / "session.json").write_text(json.dumps({
                "effort": "high", "created": "2026-09-18T10:00:00Z", "resumed_from": resumed,
                "turns": [{"started": "2026-09-18T10:00:00Z", "completed": "2026-09-18T10:05:00Z"}]}),
                encoding="utf-8")
        usage = model_fit.luna_session_usage(sessions / "lr-b")
        self.assertEqual(usage["tokens"], "uncached_input=300 cache_read=300 cache_write=0 output=60 reasoning=30")
        self.assertEqual(usage["wall_time"], "300s")
        run = self.dir / "20260918T040422Z-abc123"
        run.mkdir()
        (run / "verdict.json").write_text(json.dumps({"effort": "medium"}), encoding="utf-8")
        (run / "audit.json").write_text(json.dumps({"token_usage": {
            "input_tokens": 150, "cached_input_tokens": 100, "cache_write_input_tokens": 0,
            "output_tokens": 7, "reasoning_output_tokens": 3}}), encoding="utf-8")
        (run / "transcript.jsonl").write_text('{"t": 100.0}\n{"t": 190.5}\n', encoding="utf-8")
        usage = model_fit.luna_session_usage(run)
        self.assertEqual(usage["tokens"], "uncached_input=50 cache_read=100 cache_write=0 output=7 reasoning=3")
        self.assertEqual((usage["wall_time"], usage["effort"], usage["date"]), ("90s", "medium", "2026-09-18"))

    # ------------------------------------------------------------ negative controls

    def _hand_edit(self, old: str, new: str) -> None:
        text = self.claude.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.claude.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_rejects_unknown_task_type(self):
        self.add(self.claude)
        self._hand_edit("- task_type: lean-elaboration", "- task_type: vibes")
        self.assertRejected("unknown task type 'vibes'")

    def test_rejects_cell_summary_without_observation(self):
        self._hand_edit("| fact-finding | no data", "| fact-finding | 3v: 3P 0A 0F guides")
        self.assertRejected("but no observation stands behind it")

    def test_rejects_hand_edited_cell_that_disagrees_with_its_observations(self):
        self.add(self.claude)
        self._hand_edit("1v: 1P 0A 0F", "1v: 0P 0A 1F")
        self.assertRejected("its observations derive '1v: 1P 0A 0F'")

    def test_rejects_observation_without_evidence_link(self):
        self.add(self.claude)
        self._hand_edit("- source: ~/.claude/projects/example/subagents/agent-a0000000000000001.jsonl\n", "")
        self.assertRejected("missing field 'source'")

    def test_rejects_non_link_evidence_and_unsourced_verdict(self):
        errors, _ = self.add(self.claude, source="n/a")
        self.assertTrue(any("not an evidence link" in e for e in errors), errors)
        self.add(self.claude)
        self._hand_edit("- verdict_source: $GOAL_STORE/master/events.jsonl#0123456789abcdef",
                        "- verdict_source: the worker said it passed")
        self.assertRejected("needs a verdict_source link")

    def test_rejects_column_naming_another_clients_model(self):
        errors, _ = self.add(self.claude, option="sol/high")
        self.assertTrue(any("names a codex model" in e for e in errors), errors)
        self.add(self.claude)
        self._hand_edit("- option: opus/high", "- option: astra/high")
        self.assertRejected("names a codex model")
        errors, _ = self.add(self.dir / "codex.md", option="opus/high", route="codex-subagent")
        self.assertTrue(any("names a claude-code model" in e for e in errors), errors)

    def test_new_ids_skip_ids_held_in_the_archive(self):
        archive = self.dir / "archive"
        archive.mkdir()
        (archive / "claude-code-opus-5.md").write_text("### cc-0007\n\n- option: opus/high\n", encoding="utf-8")
        self.add(self.claude)
        self.assertIn("### cc-0008", self.claude.read_text(encoding="utf-8"))
        self.assertEqual(self.errors(), [])

    def test_rejects_luna_reserve_route_confusion(self):
        codex = self.dir / "codex.md"
        errors, _ = self.add(codex, option="luna-reserve/high", route="codex-subagent")
        self.assertTrue(any("reachable only through" in e for e in errors), errors)
        errors, _ = self.add(codex, option="luna/high", route="luna-reserve-broker")
        self.assertTrue(any("does not serve model 'luna'" in e for e in errors), errors)
        errors, _ = self.add(codex, option="luna-reserve/high", route="luna-reserve-broker (Claude master)")
        self.assertEqual(errors, [])

    def test_rejects_malformed_cost_fields(self):
        for key, value in (("tokens", "uncached_input=1 output=2"), ("wall_time", "15 minutes"),
                           ("turns", "-3"), ("tokens", "dollars=3 uncached_input=1 cache_read=1 "
                                                       "cache_write=1 output=1 reasoning=1")):
            errors, _ = self.add(self.claude, **{key: value})
            self.assertTrue(any(f"malformed cost field {key}" in e for e in errors), (key, errors))
        self.add(self.claude)
        self._hand_edit("output=400", "output=lots")
        self.assertRejected("malformed cost field tokens")

    def test_rejects_worker_self_recording_and_foreign_files(self):
        errors, _ = self.add(self.claude, recorded_by="worker self-report")
        self.assertTrue(any("recorded by the master" in e for e in errors), errors)
        (self.dir / "all-clients.md").write_text("x\n", encoding="utf-8")
        self.assertRejected("not a known client table")

    def test_rejects_missing_client_file(self):
        (self.dir / "muse.md").unlink()
        self.assertRejected("missing table file for client muse")

    def test_summarize_repairs_a_stale_summary_but_not_a_bad_observation(self):
        self.add(self.claude)
        self._hand_edit("1v: 1P 0A 0F", "stale")
        code, out = run_cli("model-fit", "summarize", str(self.claude))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.errors(), [])
        self._hand_edit("- task_type: lean-elaboration", "- task_type: vibes")
        code, out = run_cli("model-fit", "summarize", str(self.claude))
        self.assertEqual(code, 1)

    # ------------------------------------------------------------ pointer

    def test_pointer_rule_and_path_constant_are_documented(self):
        self.assertEqual(model_fit.TABLE_DIR, "model-fit")
        self.assertEqual(model_fit.POINTER, "$GOAL_STORE/model-fit/<client>.md")
        def flat(name: str) -> str:
            return " ".join((ROOT / "docs/guides" / name).read_text(encoding="utf-8").split())

        briefs, master, method = flat("briefs.md"), flat("master.md"), flat("model-fit.md")
        for text in (briefs, master, method):
            self.assertIn(model_fit.POINTER, text)
        for text in (briefs, master):
            self.assertIn("(model-fit.md)", text)
        for task in model_fit.TASK_TYPES:
            self.assertIn(f"`{task}`", method)
        for client in model_fit.CLIENTS:
            self.assertIn(f"`{client}.md`", method)


if __name__ == "__main__":
    unittest.main()
