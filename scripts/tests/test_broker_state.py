from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme.host_build_broker import BROKER_NAME, render_contained_build_broker


def generated_broker(temporary: Path) -> dict:
    """Load the actual generated runtime without entering host/build actions."""
    code = render_contained_build_broker(temporary / "creme", "fixture", "fixture", "0" * 64)
    namespace = {"__name__": "broker_fixture", "__file__": str(temporary / "codex" / "bin" / BROKER_NAME)}
    exec(compile(code, namespace["__file__"], "exec"), namespace)
    return namespace


class BrokerStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.temporary = Path(self.tmp.name)
        self.root = self.temporary / "codex"
        self.root.mkdir(mode=0o700)
        self.broker = generated_broker(self.temporary)
        self.parent = self.root / "state"
        self.state = self.parent / "creme-build-broker"

    def refused(self, call, message):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as error:
            call()
        self.assertEqual(error.exception.code, 2)
        self.assertIn(message, output.getvalue())

    def test_generated_first_start_creates_private_parent_and_exclusive_lock(self):
        self.assertFalse(self.parent.exists())
        descriptor = self.broker["broker_lock"]()
        try:
            self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.state / "active.lock").stat().st_mode), 0o600)
            self.assertEqual(self.parent.stat().st_uid, os.getuid())
            self.refused(self.broker["broker_lock"], "another contained-build broker is active")
        finally:
            os.close(descriptor)
        descriptor = self.broker["broker_lock"]()
        os.close(descriptor)

    def test_existing_safe_parent_is_preserved(self):
        self.parent.mkdir(mode=0o750)
        before = self.parent.stat()
        descriptor = self.broker["broker_lock"]()
        os.close(descriptor)
        self.assertEqual(self.parent.stat().st_mode, before.st_mode)
        self.assertEqual(self.parent.stat().st_ino, before.st_ino)

    def test_unsafe_parent_is_refused_without_chmod_or_children(self):
        self.parent.mkdir(mode=0o777)
        self.parent.chmod(0o777)
        self.refused(self.broker["broker_lock"], "not group/other writable")
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), 0o777)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_symlink_parent_target_is_untouched(self):
        target = self.temporary / "target"
        target.mkdir(mode=0o755)
        self.parent.symlink_to(target, target_is_directory=True)
        before = target.stat()
        self.refused(self.broker["broker_lock"], "not a regular directory")
        self.assertEqual(target.stat().st_mode, before.st_mode)
        self.assertEqual(list(target.iterdir()), [])

    def test_file_parent_is_untouched(self):
        self.parent.write_text("preserve")
        before = self.parent.stat()
        self.refused(self.broker["broker_lock"], "not a regular directory")
        self.assertEqual(self.parent.read_text(), "preserve")
        self.assertEqual(self.parent.stat().st_mode, before.st_mode)

    def test_wrong_owner_parent_is_refused_without_children(self):
        self.parent.mkdir(mode=0o700)
        owner = os.getuid()
        with mock.patch.object(self.broker["os"], "getuid", return_value=owner + 1):
            self.refused(self.broker["broker_lock"], "owned by this user")
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), 0o700)

    def test_missing_parent_requires_private_owned_root(self):
        self.root.chmod(0o755)
        self.refused(self.broker["broker_lock"], "Codex root must be private")
        self.assertFalse(self.parent.exists())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.root.chmod(0o700)
        owner = os.getuid()
        with mock.patch.object(self.broker["os"], "getuid", return_value=owner + 1):
            self.refused(self.broker["broker_lock"], "owned by this user")
        self.assertFalse(self.parent.exists())

    def test_provisioning_rejects_missing_file_and_symlink_roots(self):
        missing = self.temporary / "missing"
        ordinary_file = self.temporary / "file"
        ordinary_file.write_text("preserve")
        link = self.temporary / "link"
        link.symlink_to(self.root, target_is_directory=True)
        for root in (missing, ordinary_file, link):
            with self.subTest(root=root):
                self.refused(lambda: self.broker["provision_state_parent"](root / "state"),
                             "Codex state parent")
        self.assertFalse(missing.exists())
        self.assertFalse(self.parent.exists())
        self.assertEqual(ordinary_file.read_text(), "preserve")

    def test_concurrent_parent_creation_is_revalidated(self):
        original = os.mkdir

        def race(path, mode=0o777, *, dir_fd=None):
            if path == "state" and dir_fd is not None:
                original(path, mode, dir_fd=dir_fd)
                raise FileExistsError("other startup won")
            return original(path, mode, dir_fd=dir_fd)

        with mock.patch.object(self.broker["os"], "mkdir", side_effect=race):
            descriptor = self.broker["broker_lock"]()
        os.close(descriptor)
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), 0o700)

    def test_unsafe_concurrent_parent_creation_is_refused(self):
        original = os.mkdir

        def race(path, mode=0o777, *, dir_fd=None):
            if path == "state" and dir_fd is not None:
                self.parent.write_text("concurrent file must survive")
                raise FileExistsError("other creator won")
            return original(path, mode, dir_fd=dir_fd)

        with mock.patch.object(self.broker["os"], "mkdir", side_effect=race):
            self.refused(self.broker["broker_lock"], "not a regular directory")
        self.assertEqual(self.parent.read_text(), "concurrent file must survive")


if __name__ == "__main__":
    unittest.main()
