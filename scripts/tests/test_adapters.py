from __future__ import annotations

import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme.adapters import get_adapter
from creme.adapters.base import Adapter
from creme.adapters.darwin import DarwinAdapter
from creme.adapters.linux import LinuxAdapter


class AdapterTest(unittest.TestCase):
    def test_forced_selection(self):
        self.assertIsInstance(get_adapter("Darwin"), DarwinAdapter)
        self.assertIsInstance(get_adapter("Linux"), LinuxAdapter)
        self.assertEqual(get_adapter("Plan9").system, "Plan9")

    def test_unsupported_never_falls_through_to_another_os(self):
        adapter = get_adapter("Plan9")
        self.assertEqual(adapter.telemetry().status, "UNAVAILABLE")
        self.assertEqual(adapter.process_snapshot().status, "UNAVAILABLE")
        self.assertEqual(adapter.reclaim([]).status, "UNAVAILABLE")
        self.assertEqual(adapter.gui_sessions(1).status, "UNAVAILABLE")

    def test_shared_modules_do_not_name_platform_executables(self):
        root = Path(__file__).resolve().parents[2] / "creme"
        forbidden = (
            "vm_stat", "launchctl", "dscacheutil",
            "/Applications/", "/System/Library", "--reflink", "cp\", \"-c",
        )
        offenders = []
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    offenders.append((path.name, token))
        self.assertEqual(offenders, [])

    def test_linux_source_does_not_invoke_macos_commands(self):
        source = inspect.getsource(LinuxAdapter)
        for token in ("memory_pressure", "vm_stat", "launchctl", "dscacheutil", "/Applications/"):
            self.assertNotIn(token, source)

    def test_cache_copy_preview_never_mutates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            destination = Path(tmp) / "dst"
            source.mkdir()
            (source / "x").write_text("x", encoding="utf-8")
            result = Adapter().copy_cache(source, destination, False)
            self.assertEqual(result.status, "PREVIEW")
            self.assertFalse(destination.exists())

    @mock.patch("creme.adapters.base.shutil.copytree")
    def test_portable_copy_failure_is_structured_and_retains_partial(self, copytree):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            destination = Path(tmp) / "dst"
            source.mkdir()

            def fail_with_partial(_source, target, **_kwargs):
                Path(target).mkdir()
                (Path(target) / "partial").write_text("partial", encoding="utf-8")
                raise OSError("simulated copy failure")

            copytree.side_effect = fail_with_partial
            result = Adapter().copy_cache(source, destination, True)
            self.assertEqual(result.status, "ERROR")
            self.assertIn("partial destination was retained", result.detail)
            self.assertEqual((destination / "partial").read_text(), "partial")

    @mock.patch("creme.adapters.darwin.DarwinAdapter._run")
    def test_darwin_partial_clone_failure_falls_back_from_owned_stage(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            destination = Path(tmp) / "dst"
            source.mkdir()
            (source / "complete").write_text("yes", encoding="utf-8")

            def fail_after_partial(argv, timeout=10.0):
                staged = Path(argv[-1])
                staged.mkdir()
                (staged / "partial").write_text("partial", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 1)

            run.side_effect = fail_after_partial
            result = DarwinAdapter().copy_cache(source, destination, True)
            self.assertEqual(result.status, "OK")
            self.assertEqual(result.data["method"], "copytree")
            self.assertEqual((destination / "complete").read_text(), "yes")
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["dst", "src"])

    @mock.patch("creme.adapters.linux.subprocess.run")
    @mock.patch("creme.adapters.linux.shutil.which", return_value="/bin/cp")
    def test_linux_partial_reflink_failure_falls_back_from_owned_stage(self, _which, run):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            destination = Path(tmp) / "dst"
            source.mkdir()
            (source / "complete").write_text("yes", encoding="utf-8")

            def fail_after_partial(argv, **_kwargs):
                staged = Path(argv[-1])
                staged.mkdir()
                (staged / "partial").write_text("partial", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 1)

            run.side_effect = fail_after_partial
            result = LinuxAdapter().copy_cache(source, destination, True)
            self.assertEqual(result.status, "OK")
            self.assertEqual(result.data["method"], "copytree")
            self.assertEqual((destination / "complete").read_text(), "yes")
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["dst", "src"])

    @mock.patch("creme.adapters.base.shutil.copytree")
    @mock.patch("creme.adapters.darwin.DarwinAdapter._run")
    def test_darwin_clone_and_fallback_failure_return_error(self, run, copytree):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            destination = Path(tmp) / "dst"
            source.mkdir()
            run.return_value = subprocess.CompletedProcess([], 1)
            copytree.side_effect = OSError("fallback blocked")
            result = DarwinAdapter().copy_cache(source, destination, True)
            self.assertEqual(result.status, "ERROR")
            self.assertIn("APFS clone unavailable", result.detail)
            self.assertIn("portable recursive copy failed", result.detail)
            self.assertFalse(destination.exists())

    @mock.patch("creme.adapters.darwin.DarwinAdapter._run")
    def test_darwin_failure_is_unavailable(self, run):
        run.side_effect = OSError("blocked")
        self.assertEqual(DarwinAdapter().telemetry().status, "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
