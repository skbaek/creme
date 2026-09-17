"""Black-box tests for scripts/muse-edit-file (Muse-only helper)."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "muse-edit-file"


def run(*args):
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class MuseEditFileTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def target(self, name="target.txt", content="alpha\nOLD\nomega\n"):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_helper_is_executable(self):
        self.assertTrue(os.access(str(HELPER), os.X_OK))

    def test_replace_once_ok(self):
        path = self.target()
        proc = run(str(path), "--find", "OLD\n", "--replace", "NEW\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), "alpha\nNEW\nomega\n")
        self.assertIn("-OLD", proc.stdout)
        self.assertIn("+NEW", proc.stdout)

    def test_zero_matches_leaves_file_unchanged(self):
        path = self.target()
        before = path.read_text(encoding="utf-8")
        proc = run(str(path), "--find", "ABSENT", "--replace", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("0 times", proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_multiple_matches_listed_and_unchanged(self):
        path = self.target(content="x\nx\nx\n")
        proc = run(str(path), "--find", "x", "--replace", "y")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("3 times", proc.stderr)
        self.assertIn("line 1", proc.stderr)
        self.assertIn("line 3", proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), "x\nx\nx\n")

    def test_find_and_replace_from_files(self):
        path = self.target(content="one\ntwo\nthree\n")
        find_file = self.root / "find.txt"
        find_file.write_text("two\n", encoding="utf-8")
        repl_file = self.root / "repl.txt"
        repl_file.write_text("TWO\nTWO\n", encoding="utf-8")
        proc = run(str(path), "--find-file", str(find_file),
                   "--replace-file", str(repl_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), "one\nTWO\nTWO\nthree\n")

    def test_create_writes_new_file(self):
        path = self.root / "new.txt"
        proc = run(str(path), "--create", "--replace", "fresh\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), "fresh\n")
        self.assertIn("created", proc.stdout)

    def test_create_refuses_existing(self):
        path = self.target()
        before = path.read_text(encoding="utf-8")
        proc = run(str(path), "--create", "--replace", "x")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_create_rejects_find(self):
        proc = run(str(self.root / "n.txt"), "--create",
                   "--find", "x", "--replace", "y")
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_target_suggests_create(self):
        proc = run(str(self.root / "nope.txt"), "--find", "x", "--replace", "y")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--create", proc.stderr)

    def test_invalid_utf8_unchanged(self):
        path = self.root / "bin.dat"
        path.write_bytes(b"\xff\xfe\x00bad")
        proc = run(str(path), "--find", "x", "--replace", "y")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("UTF-8", proc.stderr)
        self.assertEqual(path.read_bytes(), b"\xff\xfe\x00bad")

    def test_empty_find_rejected(self):
        path = self.target()
        before = path.read_text(encoding="utf-8")
        proc = run(str(path), "--find", "", "--replace", "y")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_executable_bit_preserved(self):
        path = self.target(content="#!/bin/sh\necho OLD\n")
        path.chmod(0o755)
        proc = run(str(path), "--find", "OLD", "--replace", "NEW")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_no_temp_files_left_behind(self):
        path = self.target()
        run(str(path), "--find", "OLD", "--replace", "NEW")
        run(str(path), "--find", "ABSENT", "--replace", "x")
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, ["target.txt"])


if __name__ == "__main__":
    unittest.main()
