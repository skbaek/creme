from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import semaphore
from creme.adapters.base import Adapter


class QuietAdapter(Adapter):
    system = "Linux"

    def quiet_host(self):
        return self.result("quiet_host", "OK", "fixture quiet")

    def gui_sessions(self, owner_uid):
        return self.result("human_gui_sessions", "OK", "fixture", {"sessions": []})


class SemaphoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def expire(self, label):
        path = Path(self.tmp.name) / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        holds = ([data["hard"]] if data["hard"] else []) + data["soft"]
        hold = next(item for item in holds if item["label"] == label)
        hold["acquired_at"] = 1
        hold["renewed_at"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_xy_interleaving_and_conversion(self):
        self.assertTrue(semaphore.acquire("soft", "X", "build")[0])
        self.assertTrue(semaphore.acquire("soft", "Y", "build")[0])
        self.assertFalse(semaphore.acquire("hard", "Y", "timing")[0])
        self.assertTrue(semaphore.release("soft", "X")[0])
        self.assertTrue(semaphore.acquire("hard", "Y", "timing")[0])
        state = semaphore.snapshot()
        self.assertEqual(state["hard"]["label"], "Y")
        self.assertEqual(state["soft"], [])
        self.assertFalse(semaphore.acquire("soft", "X", "build")[0])

    def test_expired_hold_still_blocks_until_certified_break(self):
        semaphore.acquire("soft", "old", "work", 1)
        self.expire("old")
        self.assertFalse(semaphore.acquire("hard", "new", "timing")[0])
        self.assertTrue(semaphore.break_expired("old", "orphaned", QuietAdapter())[0])
        self.assertTrue(semaphore.acquire("hard", "new", "timing")[0])

    def test_corrupt_state_is_not_replaced(self):
        path = Path(self.tmp.name) / "state.json"
        path.write_text("not-json", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(semaphore.SemaphoreError):
            semaphore.snapshot()
        self.assertEqual(path.read_bytes(), before)

    def test_shape_valid_but_incomplete_hold_is_rejected_without_replacement(self):
        self.assertTrue(semaphore.acquire("soft", "incomplete", "fixture")[0])
        path = Path(self.tmp.name) / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["soft"][0]["renewed_at"]
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(semaphore.SemaphoreError):
            semaphore.snapshot()
        self.assertEqual(path.read_bytes(), before)

    def test_state_directory_and_files_are_private(self):
        self.assertTrue(semaphore.acquire("soft", "private", "sensitive note")[0])
        root = Path(self.tmp.name)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for name in ("mutex", "state.json", "log.jsonl"):
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)

    def test_reserved_manual_label_is_not_an_agent_hold(self):
        self.assertFalse(semaphore.acquire("soft", semaphore.MANUAL_LABEL, "x")[0])
        self.assertFalse(semaphore.release("soft", semaphore.MANUAL_LABEL)[0])

    def test_cleanup_success_releases_soft_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        cleanup = mock.Mock(return_value=(True, "clean"))

        self.assertEqual(
            semaphore.release_after_cleanup("goal", cleanup),
            (True, "cleanup verified and soft hold released"),
        )
        cleanup.assert_called_once_with()
        self.assertEqual(semaphore.snapshot()["soft"], [])

    def test_cleanup_success_releases_hard_hold(self):
        self.assertTrue(semaphore.acquire("hard", "goal", "timing")[0])

        self.assertTrue(
            semaphore.release_after_cleanup("goal", lambda: (True, "clean"))[0]
        )
        self.assertIsNone(semaphore.snapshot()["hard"])

    def test_cleanup_failure_preserves_matching_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])

        ok, detail = semaphore.release_after_cleanup(
            "goal", lambda: (False, "server survived")
        )

        self.assertFalse(ok)
        self.assertIn("hold retained", detail)
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_cleanup_exception_preserves_matching_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])

        ok, detail = semaphore.release_after_cleanup(
            "goal", mock.Mock(side_effect=RuntimeError("fixture"))
        )

        self.assertFalse(ok)
        self.assertIn("hold retained", detail)
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_other_hold_blocks_cleanup_before_callback(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        self.assertTrue(semaphore.acquire("soft", "other", "build")[0])
        cleanup = mock.Mock(return_value=(True, "clean"))

        ok, detail = semaphore.release_after_cleanup("goal", cleanup)

        self.assertFalse(ok)
        self.assertIn("other", detail)
        cleanup.assert_not_called()
        self.assertEqual(
            [item["label"] for item in semaphore.snapshot()["soft"]],
            ["goal", "other"],
        )

    def test_cleanup_is_idempotent_after_matching_hold_was_released(self):
        self.assertEqual(
            semaphore.release_after_cleanup("goal", lambda: (True, "clean")),
            (True, "cleanup verified; no matching hold remained"),
        )
        self.assertEqual(semaphore.snapshot(), semaphore._empty_state())

    def test_successful_state_change_is_not_misreported_when_audit_log_fails(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])

        with mock.patch("creme.semaphore._log", side_effect=OSError("fixture")):
            ok, detail = semaphore.release_after_cleanup(
                "goal", lambda: (True, "clean")
            )

        self.assertTrue(ok)
        self.assertIn("audit log write failed", detail)
        self.assertEqual(semaphore.snapshot()["soft"], [])


if __name__ == "__main__":
    unittest.main()
