from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from creme.cli import cmd_host_guidance
from creme.guidance import (
    DEFAULT_RELATIVE_GUIDANCE,
    MAX_GUIDANCE_BYTES,
    default_path,
    load,
)


class GuidanceTest(unittest.TestCase):
    def test_cli_prints_valid_guidance_and_rejects_invalid_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-guidance.md"
            path.write_text("# Local safety\n", encoding="utf-8")
            output = StringIO()
            with mock.patch("creme.cli.default_guidance_path", return_value=path):
                with redirect_stdout(output):
                    self.assertEqual(cmd_host_guidance(mock.Mock()), 0)
            self.assertIn("# Local safety", output.getvalue())

            path.write_text("\n", encoding="utf-8")
            with mock.patch("creme.cli.default_guidance_path", return_value=path):
                with redirect_stdout(StringIO()):
                    self.assertEqual(cmd_host_guidance(mock.Mock()), 1)

    def test_default_path_uses_the_canonical_shared_checkout(self):
        source = Path("/temporary/worktree")
        canonical = Path("/canonical/creme")
        with mock.patch(
            "creme.semaphore.canonical_creme_root",
            return_value=canonical,
        ) as resolve:
            self.assertEqual(default_path(source), canonical / DEFAULT_RELATIVE_GUIDANCE)
        resolve.assert_called_once_with(source)

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

    def test_symlink_and_non_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.md"
            target.write_text("# Local safety\n", encoding="utf-8")
            link = root / "host-guidance.md"
            link.symlink_to(target)
            self.assertEqual(load(link).status, "INVALID")
            self.assertEqual(load(root).status, "INVALID")


if __name__ == "__main__":
    unittest.main()
