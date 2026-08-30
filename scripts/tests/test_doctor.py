from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creme.adapters.base import Adapter
from creme.doctor import STATUS_FAIL, STATUS_OK, check_launch_root, check_public_runtime_boundary


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


if __name__ == "__main__":
    unittest.main()
