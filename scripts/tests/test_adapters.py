from __future__ import annotations

import inspect
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

    @mock.patch("creme.adapters.darwin.DarwinAdapter._run")
    def test_darwin_failure_is_unavailable(self, run):
        run.side_effect = OSError("blocked")
        self.assertEqual(DarwinAdapter().telemetry().status, "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
