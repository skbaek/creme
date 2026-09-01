from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creme.guidance import MAX_GUIDANCE_BYTES, load


class GuidanceTest(unittest.TestCase):
    def test_missing_valid_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-guidance.md"
            self.assertEqual(load(path).status, "MISSING")
            path.write_text("# Local safety\n", encoding="utf-8")
            checked = load(path)
            self.assertEqual(checked.status, "OK")
            self.assertEqual(checked.content, "# Local safety\n")
            path.write_text(" \n", encoding="utf-8")
            self.assertEqual(load(path).status, "INVALID")

    def test_non_utf8_nul_and_oversize_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-guidance.md"
            path.write_bytes(b"\xff")
            self.assertEqual(load(path).status, "INVALID")
            path.write_bytes(b"local\x00guidance")
            self.assertEqual(load(path).status, "INVALID")
            path.write_bytes(b"x" * (MAX_GUIDANCE_BYTES + 1))
            self.assertEqual(load(path).status, "INVALID")


if __name__ == "__main__":
    unittest.main()
