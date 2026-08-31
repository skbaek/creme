from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from creme.cli import cmd_host_wrappers
from creme.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, check_host_wrappers
from creme.host_wrappers import (
    WRAPPER_COMMANDS,
    install_host_wrappers,
    render_host_wrappers,
)


class HostWrappersTest(unittest.TestCase):
    def test_rendered_wrappers_are_thin_delegates(self) -> None:
        root = Path("/portable/workspace/creme")
        rendered = render_host_wrappers(root)
        self.assertEqual(set(rendered), {name for name, _ in WRAPPER_COMMANDS})
        for name, command in WRAPPER_COMMANDS:
            with self.subTest(name=name):
                self.assertEqual(
                    rendered[name],
                    "#!/bin/sh\n"
                    "set -eu\n"
                    f"exec /portable/workspace/creme/scripts/creme {command} \"$@\"\n",
                )
                self.assertNotIn("elanc", rendered[name].lower())

    def test_rendered_wrapper_quotes_a_hostile_checkout_path(self) -> None:
        root = Path("/portable/work space/quo'ted/creme")
        rendered = render_host_wrappers(root)
        launcher = shlex.quote(str(root / "scripts" / "creme"))
        for name, command in WRAPPER_COMMANDS:
            self.assertIn(f"exec {launcher} {command} \"$@\"", rendered[name])

    def test_write_is_atomic_private_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "workspace" / "creme"
            output = temporary / "client" / "bin"
            expected = render_host_wrappers(root)
            written = install_host_wrappers(root, output, replace=False)
            self.assertEqual(written, [output / name for name in expected])
            for path in written:
                self.assertEqual(path.read_text(encoding="utf-8"), expected[path.name])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(list(output.glob(".*.tmp.*")), [])

    def test_existing_wrapper_requires_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "creme"
            output = temporary / "bin"
            output.mkdir()
            stale = output / WRAPPER_COMMANDS[0][0]
            stale.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                install_host_wrappers(root, output, replace=False)
            self.assertEqual(stale.read_text(encoding="utf-8"), "#!/bin/sh\nexit 99\n")

            install_host_wrappers(root, output, replace=True)
            expected = render_host_wrappers(root)
            self.assertEqual(stale.read_text(encoding="utf-8"), expected[stale.name])

    def test_doctor_warns_when_optional_install_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            checks = check_host_wrappers(temporary / "creme", temporary / "bin")
            self.assertEqual(checks[0].status, STATUS_WARN)

    def test_cli_preview_does_not_create_output_and_write_requires_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "missing" / "bin"
            preview = SimpleNamespace(
                output_dir=str(output), write=False, replace=False,
            )
            with patch("creme.cli._json") as emit:
                self.assertEqual(cmd_host_wrappers(preview), 0)
            self.assertFalse(output.exists())
            self.assertEqual(emit.call_args.args[0]["status"], "PREVIEW")

            implicit = SimpleNamespace(
                output_dir=None, write=True, replace=False,
            )
            with patch("creme.cli._json") as emit:
                self.assertEqual(cmd_host_wrappers(implicit), 1)
            self.assertEqual(emit.call_args.args[0]["status"], "REFUSED")

    def test_doctor_rejects_partial_stale_and_non_executable_installs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "creme"
            output = temporary / "bin"
            output.mkdir()
            first = output / WRAPPER_COMMANDS[0][0]
            first.write_text("stale\n", encoding="utf-8")
            first.chmod(0o700)
            checks = check_host_wrappers(root, output)
            self.assertEqual(checks[0].status, STATUS_FAIL)
            self.assertIn("content mismatch", checks[0].detail)
            self.assertIn("missing", checks[0].detail)

            install_host_wrappers(root, output, replace=True)
            first.chmod(0o600)
            checks = check_host_wrappers(root, output)
            self.assertEqual(checks[0].status, STATUS_FAIL)
            self.assertIn("not executable", checks[0].detail)

    def test_doctor_accepts_only_the_current_exact_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "creme"
            output = temporary / "bin"
            install_host_wrappers(root, output, replace=False)
            checks = check_host_wrappers(root, output)
            self.assertEqual(checks[0].status, STATUS_OK)

            target = output / WRAPPER_COMMANDS[0][0]
            target.unlink()
            os.symlink(output / WRAPPER_COMMANDS[1][0], target)
            checks = check_host_wrappers(root, output)
            self.assertEqual(checks[0].status, STATUS_FAIL)
            self.assertIn("symbolic link", checks[0].detail)


if __name__ == "__main__":
    unittest.main()
