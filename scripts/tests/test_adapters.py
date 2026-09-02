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
from creme.cli import cmd_memory_headroom


class AdapterTest(unittest.TestCase):
    def test_memory_headroom_cli_emits_narrow_capability(self):
        adapter = Adapter()
        result = adapter.result(
            "memory_headroom", "OK", "fixture", {"memory_free_percent": 50}
        )
        with mock.patch("creme.cli.get_adapter", return_value=adapter), mock.patch.object(
            adapter, "memory_headroom", return_value=result
        ), mock.patch("creme.cli._json") as emit:
            exit_status = cmd_memory_headroom(mock.Mock())

        self.assertEqual(exit_status, 0)
        self.assertEqual(emit.call_args.args[0]["capability"], "memory_headroom")

    def test_forced_selection(self):
        self.assertIsInstance(get_adapter("Darwin"), DarwinAdapter)
        self.assertIsInstance(get_adapter("Linux"), LinuxAdapter)
        self.assertEqual(get_adapter("Plan9").system, "Plan9")

    def test_native_platform_keys_normalize_os_architecture_aliases(self):
        darwin = DarwinAdapter().platform_identity("aarch64")
        linux = LinuxAdapter().platform_identity("amd64")
        self.assertEqual(darwin.status, "OK")
        self.assertEqual(darwin.data["key"], "macos-arm64")
        self.assertEqual(darwin.data["machine"], "arm64")
        self.assertEqual(linux.status, "OK")
        self.assertEqual(linux.data["key"], "linux-x86_64")
        self.assertEqual(linux.data["machine"], "x86_64")

    def test_native_python_identity_is_os_specific_and_home_relative(self):
        darwin = DarwinAdapter().python_runtime("3.11.9", "arm64")
        linux = LinuxAdapter().python_runtime("3.11.9", "x86_64")
        self.assertEqual(darwin.status, "OK")
        self.assertEqual(linux.status, "OK")
        self.assertEqual(darwin.data["platform_key"], "macos-arm64")
        self.assertEqual(linux.data["platform_key"], "linux-x86_64")
        self.assertEqual(
            darwin.data["uv_base_prefix"],
            "~/.local/share/uv/python/cpython-3.11.9-macos-aarch64-none",
        )
        self.assertEqual(
            linux.data["uv_base_prefix"],
            "~/.local/share/uv/python/cpython-3.11.9-linux-x86_64-gnu",
        )
        for result in (darwin, linux):
            self.assertTrue(result.data["uv_alias_prefix"].startswith("~/"))
            self.assertTrue(result.data["uv_base_prefix"].startswith("~/"))
            self.assertNotIn("/Users/", result.data["uv_base_prefix"])
            self.assertNotIn("/home/", result.data["uv_base_prefix"])

    def test_native_python_identity_fails_closed(self):
        self.assertEqual(
            LinuxAdapter().python_runtime("3.11", "x86_64").status,
            "REFUSED",
        )
        self.assertEqual(
            DarwinAdapter().platform_identity("mips64").status,
            "UNAVAILABLE",
        )

    def test_unsupported_never_falls_through_to_another_os(self):
        adapter = get_adapter("Plan9")
        self.assertEqual(adapter.telemetry().status, "UNAVAILABLE")
        self.assertEqual(adapter.memory_headroom().status, "UNAVAILABLE")
        self.assertEqual(adapter.process_snapshot().status, "UNAVAILABLE")
        self.assertEqual(adapter.reclaim([]).status, "UNAVAILABLE")
        self.assertEqual(adapter.gui_sessions(1).status, "UNAVAILABLE")
        self.assertEqual(adapter.platform_identity().status, "UNAVAILABLE")
        self.assertEqual(adapter.python_runtime("3.11.9").status, "UNAVAILABLE")

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

    @mock.patch("creme.adapters.linux.os.getppid", return_value=12)
    @mock.patch("creme.adapters.linux.os.getuid", return_value=1001)
    @mock.patch("creme.adapters.linux.LinuxAdapter._run")
    def test_linux_reclaim_dry_run_uses_only_same_user_client_tree(
        self, run, _uid, _ppid
    ):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="""\
10 1 1001 100 S Mon Jan 1 00:00:00 2024 /usr/lib/chatgpt/resources/codex app-server
11 10 1001 10 S Mon Jan 1 00:00:01 2024 /bin/bash
12 11 1001 10 S Mon Jan 1 00:00:02 2024 python3 -m creme reclaim --dry-run
20 10 1001 300 S Mon Jan 1 00:01:00 2024 /tool/lake serve
21 20 1001 200 S Mon Jan 1 00:01:01 2024 /tool/lean --server
22 20 1001 0 Z Mon Jan 1 00:01:02 2024 /tool/lean --worker
30 10 1002 400 S Mon Jan 1 00:02:00 2024 /tool/lake serve
31 30 1002 300 S Mon Jan 1 00:02:01 2024 /tool/lean --server
""")
        result = LinuxAdapter().reclaim(["--dry-run"])
        self.assertEqual(result.status, "OK")
        self.assertEqual([row["pid"] for row in result.data["owned"]], [20, 21])
        self.assertEqual(result.data["termination_order"], [21, 20])
        self.assertEqual(result.data["foreign_left_alone"], [])

    @mock.patch("creme.adapters.linux.LinuxAdapter._run")
    def test_linux_reclaim_rejects_duplicate_options_without_snapshot(self, run):
        result = LinuxAdapter().reclaim(["--dry-run", "--dry-run"])
        self.assertEqual(result.status, "REFUSED")
        run.assert_not_called()

    def test_cache_copy_preview_never_mutates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            destination = Path(tmp) / "dst"
            source.mkdir()
            (source / "x").write_text("x", encoding="utf-8")
            result = Adapter().copy_cache(source, destination, False)
            self.assertEqual(result.status, "PREVIEW")
            self.assertFalse(destination.exists())

    def test_optimized_cache_copy_invalid_inputs_do_not_recurse(self):
        for adapter in (DarwinAdapter(), LinuxAdapter()):
            with self.subTest(adapter=adapter.system), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "src"
                destination = Path(tmp) / "dst"

                missing = adapter.copy_cache(source, destination, False)
                self.assertEqual(missing.status, "ERROR")
                self.assertIn("source is not a directory", missing.detail)

                source.mkdir()
                destination.mkdir()
                existing = adapter.copy_cache(source, destination, False)
                self.assertEqual(existing.status, "ERROR")
                self.assertIn("destination already exists", existing.detail)

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

    @mock.patch("creme.adapters.darwin.DarwinAdapter._run")
    def test_darwin_headroom_survives_denied_swap_and_avoids_process_scan(self, run):
        pressure = subprocess.CompletedProcess(
            ["memory_pressure"],
            0,
            stdout=(
                "The system has 25769803776 (1572864 pages with a page size of 16384).\n"
                "System-wide memory free percentage: 19%\n"
            ),
        )
        denied_swap = subprocess.CompletedProcess(
            ["sysctl"], 1, stdout="", stderr="Operation not permitted"
        )
        run.side_effect = [pressure, denied_swap]

        result = DarwinAdapter().memory_headroom()

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.data["memory_free_percent"], 19)
        self.assertEqual(result.data["physical_memory_bytes"], 25769803776)
        self.assertIsNone(result.data["swap_used_mib"])
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("ps", " ".join(run.call_args_list[-1].args[0]))

    @mock.patch("creme.adapters.linux.LinuxAdapter._meminfo")
    def test_linux_headroom_uses_proc_memory_without_process_scan(self, meminfo):
        meminfo.return_value = {"MemTotal": 16 * 1024 ** 2, "MemAvailable": 4 * 1024 ** 2}
        swaps = mock.mock_open(
            read_data="Filename Type Size Used Priority\n/swap file 1000 512 -2\n"
        )

        with mock.patch("builtins.open", swaps), mock.patch(
            "creme.adapters.linux.subprocess.run"
        ) as run:
            result = LinuxAdapter().memory_headroom()

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.data["memory_free_percent"], 25)
        self.assertEqual(result.data["memory_available_bytes"], 4 * 1024 ** 3)
        self.assertEqual(result.data["swap_used_mib"], 0.5)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
