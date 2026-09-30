import json
import tempfile
import unittest
from pathlib import Path

from creme.model_fit_capture import CaptureError, codex_snapshot, codex_window, muse_turn


class CaptureTests(unittest.TestCase):
    def record(self, incoming, output):
        return json.dumps({"type": "event_msg", "timestamp": "now", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": incoming, "cached_input_tokens": incoming - 1,
                "output_tokens": output, "reasoning_output_tokens": output // 2,
                "total_tokens": incoming + output}}}}) + "\n"

    def test_incremental_window_counts_cumulative_once_and_not_subcategories(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "own.jsonl"
            p.write_text(self.record(100, 20))
            before = codex_snapshot(p)
            with p.open("a") as out:
                out.write(self.record(110, 25))
            after = codex_snapshot(p, before)
            delta = codex_window(before, after)
            self.assertEqual((delta["total_input"], delta["total_output"]), (10, 5))
            replay = codex_snapshot(p, after)
            self.assertEqual(after, replay)
            self.assertEqual(codex_window(after, replay)["total_input"], 0)

    def test_partial_line_retried_but_malformed_complete_line_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "own.jsonl"
            initial = self.record(100, 20)
            next_row = self.record(110, 25)
            p.write_text(initial + next_row[:10])
            before = codex_snapshot(p)
            self.assertEqual(before["cursor"], len(initial))
            with p.open("a") as out:
                out.write(next_row[10:])
            self.assertEqual(codex_snapshot(p, before)["raw"]["input_tokens"], 110)
            with p.open("a") as out:
                out.write("broken\n")
            with self.assertRaises(CaptureError):
                codex_snapshot(p, before)

    def test_counter_reset_and_unknown_later_snapshot_cannot_look_free(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "own.jsonl"
            p.write_text(self.record(100, 20))
            before = codex_snapshot(p)
            with p.open("a") as out:
                out.write(self.record(10, 2))
            with self.assertRaises(CaptureError):
                codex_window(before, codex_snapshot(p, before))
            with p.open("a") as out:
                out.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}}}) + "\n")
            with self.assertRaises(CaptureError):
                codex_window(before, codex_snapshot(p, before))

    def test_intermediate_reset_is_not_hidden_by_larger_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "own.jsonl"
            p.write_text(self.record(100, 20))
            before = codex_snapshot(p)
            with p.open("a") as out:
                out.write(self.record(10, 2) + self.record(200, 40))
            with self.assertRaisesRegex(CaptureError, "counter reset"):
                codex_snapshot(p, before)

    def test_muse_protocol_pass_is_not_master_quality_acceptance(self):
        row = {"id": "session", "model": "muse-spark-1.3", "effort": "high", "turns": [
            {"n": 1, "turn_id": "turn", "models": {"muse-spark-1.3": 1}, "status": "completed",
             "verdict": "PASS", "tokens": {"prompt": 100, "cached": 90, "output": 20,
                                             "reasoning": 15, "total": 120}}]}
        captured = muse_turn(row, 1)
        self.assertEqual(captured["usage"]["total_input"], 100)
        self.assertIsNone(captured["quality_verdict"])
        self.assertEqual(captured["usage_scope"], "parent-turn")
        row["turns"][0]["tokens"].pop("prompt")
        with self.assertRaises(CaptureError):
            muse_turn(row, 1)


if __name__ == "__main__":
    unittest.main()
