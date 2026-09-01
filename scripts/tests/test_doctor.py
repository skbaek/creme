from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creme.adapters.base import Adapter
from creme.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    check_host_guidance,
    check_launch_root,
    check_neutral_semaphore,
    check_public_runtime_boundary,
)


class DoctorTest(unittest.TestCase):
    def test_wrong_root_is_informative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "creme"
            root.mkdir()
            checks = check_launch_root(root, Path(tmp))
            self.assertEqual(checks[0].status, STATUS_FAIL)
            self.assertIn("WRONG_ROOT", checks[0].detail)

    def test_private_runtime_reference_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            private = "/" + "Users" + "/example/" + "plans"
            (root / "scripts" / "bad.py").write_text(repr(private), encoding="utf-8")
            checks = check_public_runtime_boundary(root)
            self.assertEqual(checks[0].status, STATUS_FAIL)

    def test_current_runtime_boundary_is_clean(self):
        root = Path(__file__).resolve().parents[2]
        checks = check_public_runtime_boundary(root)
        self.assertTrue(all(check.status == STATUS_OK for check in checks), checks)

    def test_host_guidance_is_optional_but_invalid_content_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-guidance.md"
            checks, validation = check_host_guidance(path)
            self.assertEqual(checks[0].status, STATUS_WARN)
            self.assertEqual(validation.status, "MISSING")
            path.write_text("\n", encoding="utf-8")
            checks, validation = check_host_guidance(path)
            self.assertEqual(checks[0].status, STATUS_FAIL)
            self.assertEqual(validation.status, "INVALID")
            path.write_text(
                "# Local safety\n\nDo not run the unsafe command.\n",
                encoding="utf-8",
            )
            checks, validation = check_host_guidance(path)
            self.assertEqual(checks[0].status, STATUS_OK)
            self.assertEqual(validation.status, "OK")

    def test_neutral_semaphore_interface_is_complete(self):
        root = Path(__file__).resolve().parents[2]
        checks = check_neutral_semaphore(root)
        self.assertEqual(checks[0].status, STATUS_OK, checks)

    def test_neutral_semaphore_check_rejects_unignored_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / ".semaphore" / "semaphore"
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o700)
            (launcher.parent / "README.md").write_text("protocol\n", encoding="utf-8")
            (root / ".gitignore").write_text("", encoding="utf-8")

            checks = check_neutral_semaphore(root)

            self.assertEqual(checks[0].status, STATUS_FAIL)
            self.assertIn("not ignored", checks[0].detail)


if __name__ == "__main__":
    unittest.main()
