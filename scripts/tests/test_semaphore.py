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


if __name__ == "__main__":
    unittest.main()
