from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from creme import build_ownership as owned
from creme import cli
from creme import disk_headroom
from creme.adapters import get_adapter
from creme.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, check_disk_headroom
from creme.profile import DISK_DEFAULT_FLOOR_GIB, disk_settings, load, propose, validate_data

GIB = 1024 ** 3


def statvfs_for(free_gib_by_path):
    """A fake os.statvfs: free space per path, in GiB, from a mapping."""
    def fake(path):
        if path not in free_gib_by_path:
            raise OSError(2, "No such file or directory")
        blocks = int(free_gib_by_path[path] * GIB // 4096)
        return SimpleNamespace(f_bavail=blocks, f_frsize=4096)
    return fake


def mounted(*paths):
    return lambda path: path in paths


class DiskProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = get_adapter()
        self.base = propose(self.root / "creme", self.root, self.adapter)

    def with_disk(self, section):
        data = dict(self.base)
        data["disk"] = section
        return data

    def test_absent_section_and_well_formed_sections_are_valid(self):
        self.assertEqual(validate_data(dict(self.base)).status, "VALID")
        for section in [{}, {"host_mounts": ["/mnt/d"]}, {"min_free_gib": 50},
                        {"host_mounts": ["/mnt/d", "/mnt/e"], "min_free_gib": 50}]:
            self.assertEqual(validate_data(self.with_disk(section)).status, "VALID", section)

    def test_malformed_sections_are_refused(self):
        refused = [
            "not-an-object",
            {"host_mounts": ["/mnt/d"], "unknown": 1},
            {"host_mounts": "/mnt/d"},
            {"host_mounts": ["mnt/d"]},
            {"host_mounts": ["/mnt/d/"]},
            {"host_mounts": ["/mnt/../mnt/d"]},
            {"host_mounts": ["/mnt/d", "/mnt/d"]},
            {"host_mounts": [1]},
            {"host_mounts": ["/mnt/" + str(index) for index in range(17)]},
            {"min_free_gib": 0},
            {"min_free_gib": 10001},
            {"min_free_gib": True},
            {"min_free_gib": "50"},
            {"min_free_gib": 50.5},
        ]
        for section in refused:
            with self.subTest(section=section):
                result = validate_data(self.with_disk(section))
                self.assertEqual(result.status, "INVALID", result.detail)
        for floor in (1, 10000):
            self.assertEqual(validate_data(self.with_disk({"min_free_gib": floor})).status, "VALID")

    def test_defaults_when_absent(self):
        self.assertEqual(disk_settings(None), ([], DISK_DEFAULT_FLOOR_GIB))
        self.assertEqual(disk_settings(dict(self.base)), ([], 50))
        self.assertEqual(disk_settings(self.with_disk({"host_mounts": ["/mnt/d"]})), (["/mnt/d"], 50))


class DiskReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Resolved, as headroom() resolves it: macOS temp directories sit under
        # the /var -> /private/var symlink.
        self.root = Path(self.tmp.name).resolve() / "creme"
        self.root.mkdir()
        base = propose(self.root, self.root.parent, get_adapter())
        base["disk"] = {"host_mounts": ["/mnt/d"], "min_free_gib": 50}
        self.profile = self.root / "host-profile.json"
        self.profile.write_text(json.dumps(base), encoding="utf-8")

    def read(self, free, mounted_paths=("/mnt/d",)):
        with patch("creme.disk_headroom.os.statvfs", side_effect=statvfs_for(free)), \
                patch("creme.disk_headroom.os.path.ismount", side_effect=mounted(*mounted_paths)):
            return disk_headroom.headroom(self.root, self.profile)

    def test_minimum_over_root_and_host_mount_is_binding(self):
        reading = self.read({str(self.root): 720.0, "/mnt/d": 194.0})
        self.assertEqual(reading["verdict"], disk_headroom.OK)
        self.assertEqual(reading["binding_path"], "/mnt/d")
        self.assertAlmostEqual(reading["free_gib"], 194.0, places=1)
        self.assertEqual(reading["floor_gib"], 50)

    def test_host_drive_below_floor_is_low_even_when_root_has_space(self):
        reading = self.read({str(self.root): 720.0, "/mnt/d": 40.0})
        self.assertEqual(reading["verdict"], disk_headroom.LOW)
        self.assertEqual(reading["binding_path"], "/mnt/d")

    def test_unmounted_host_mount_is_unknown_not_ignored(self):
        # The root fs still reports plenty, and statvfs on the empty directory
        # would succeed; the mount check must refuse to use it.
        reading = self.read({str(self.root): 720.0, "/mnt/d": 900.0}, mounted_paths=())
        self.assertEqual(reading["verdict"], disk_headroom.UNKNOWN)
        self.assertIn("/mnt/d", reading["detail"])
        self.assertIsNone(reading["per_path"]["/mnt/d"]["free_gib"])

    def test_unreadable_mount_is_unknown(self):
        def fail(path):
            if path == "/mnt/d":
                raise OSError(5, "Input/output error")
            return SimpleNamespace(f_bavail=GIB // 4096 * 720, f_frsize=4096)
        with patch("creme.disk_headroom.os.statvfs", side_effect=fail), \
                patch("creme.disk_headroom.os.path.ismount", return_value=True):
            reading = disk_headroom.headroom(self.root, self.profile)
        self.assertEqual(reading["verdict"], disk_headroom.UNKNOWN)

    def test_invalid_profile_is_unknown(self):
        self.profile.write_text(json.dumps({"disk": "broken"}), encoding="utf-8")
        reading = disk_headroom.headroom(self.root, self.profile)
        self.assertEqual(reading["verdict"], disk_headroom.UNKNOWN)

    def test_missing_profile_uses_defaults_for_root_only(self):
        self.profile.unlink()
        with patch("creme.disk_headroom.os.statvfs", side_effect=statvfs_for({str(self.root): 300.0})):
            reading = disk_headroom.headroom(self.root, self.profile)
        self.assertEqual(reading["verdict"], disk_headroom.OK)
        self.assertEqual(reading["host_mounts"], [])
        self.assertEqual(reading["floor_gib"], 50)

    def test_assess_projects_copy_and_never_allows_unknown(self):
        ok = {"verdict": "OK", "free_gib": 120.0, "binding_path": "/mnt/d", "per_path": {},
              "floor_gib": 50, "detail": "fine"}
        # 120 GiB free less an 80 GiB copy projects 40 GiB, below the 50 GiB floor.
        self.assertFalse(disk_headroom.assess(ok, 80 * GIB)["allowed"])
        self.assertEqual(disk_headroom.assess(ok, 80 * GIB)["verdict"], "LOW")
        self.assertIn("/mnt/d", disk_headroom.assess(ok, 80 * GIB)["detail"])
        self.assertTrue(disk_headroom.assess(ok, 10 * GIB)["allowed"])
        unknown = dict(ok, verdict="UNKNOWN", detail="not mounted")
        self.assertFalse(disk_headroom.assess(unknown, 0)["allowed"])

    def test_tree_bytes_counts_entries_without_following_symlinks(self):
        source = Path(self.tmp.name) / "src"
        (source / "sub").mkdir(parents=True)
        (source / "a.bin").write_bytes(b"x" * 100)
        (source / "sub" / "b.bin").write_bytes(b"y" * 50)
        os.symlink("/nonexistent-target-for-test", source / "link")
        total = disk_headroom.tree_bytes(source)
        # Directories carry no copied data; the symlink counts its own size, not its target.
        self.assertEqual(total, 100 + 50 + os.lstat(source / "link").st_size)


class CacheCopyGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source"
        self.source.mkdir()
        (self.source / "big.bin").write_bytes(b"z" * (2 * 1024 * 1024))
        self.destination = Path(self.tmp.name) / "copy"

    def run_copy(self, execute, free_gib):
        reading = {"verdict": "OK", "free_gib": free_gib, "binding_path": "/mnt/d", "per_path": {},
                   "floor_gib": 50, "detail": "binding path /mnt/d has %.2f GiB free" % free_gib,
                   "host_mounts": ["/mnt/d"], "profile": "VALID", "creme_root": "/x"}
        args = argparse.Namespace(source=str(self.source), destination=str(self.destination), execute=execute)
        output = io.StringIO()
        with patch("creme.disk_headroom.headroom", return_value=reading), redirect_stdout(output):
            code = cli.cmd_cache_copy(args)
        return code, json.loads(output.getvalue())

    def test_execute_refused_below_floor_and_copies_nothing(self):
        code, payload = self.run_copy(execute=True, free_gib=50.001)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "REFUSED")
        self.assertIn("/mnt/d", payload["detail"])
        self.assertIn("floor", payload["detail"])
        # The refusal is the control: without it the copy would create the destination.
        self.assertFalse(self.destination.exists())

    def test_execute_allowed_when_projection_clears_floor(self):
        code, payload = self.run_copy(execute=True, free_gib=500.0)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(self.destination.exists())
        self.assertTrue(payload["data"]["disk"]["allowed"])

    def test_preview_reports_projection_without_refusing(self):
        code, payload = self.run_copy(execute=False, free_gib=50.001)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "PREVIEW")
        disk = payload["data"]["disk"]
        self.assertFalse(disk["allowed"])
        self.assertEqual(disk["verdict"], "LOW")
        self.assertIsNotNone(disk["projected_free_gib"])
        self.assertFalse(self.destination.exists())


class LakeBuildGuardTest(unittest.TestCase):
    def run_build(self, reading, probe=False):
        output = io.StringIO()
        resolve = Mock(side_effect=RuntimeError("toolchain reached"))
        with patch("creme.disk_headroom.headroom", return_value=reading), \
                patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")), \
                patch("creme.build_ownership.resolve_toolchain", resolve):
            code = owned.run_lake_build("g", ["T"], probe=probe, contention="sensitive", memory_gib=2, stdout=output)
        return code, output.getvalue(), resolve

    def test_low_headroom_refuses_before_toolchain_and_admission(self):
        reading = {"verdict": "LOW", "free_gib": 30.0, "binding_path": "/mnt/d", "per_path": {},
                   "floor_gib": 50, "detail": "x", "host_mounts": ["/mnt/d"], "profile": "VALID",
                   "creme_root": "/x"}
        code, text, resolve = self.run_build(reading)
        self.assertEqual(code, owned.GUARD_REFUSAL_EXIT)
        self.assertIn("/mnt/d", text)
        self.assertIn("floor", text)
        resolve.assert_not_called()

    def test_unknown_headroom_refuses(self):
        reading = {"verdict": "UNKNOWN", "free_gib": None, "binding_path": None, "per_path": {},
                   "floor_gib": 50, "detail": "/mnt/d: configured host mount is not mounted",
                   "host_mounts": ["/mnt/d"], "profile": "VALID", "creme_root": "/x"}
        code, text, resolve = self.run_build(reading)
        self.assertEqual(code, owned.GUARD_REFUSAL_EXIT)
        self.assertIn("UNKNOWN", text)
        resolve.assert_not_called()

    def test_probe_is_exempt_from_the_guard(self):
        reading = {"verdict": "LOW", "free_gib": 30.0, "binding_path": "/mnt/d", "per_path": {},
                   "floor_gib": 50, "detail": "x", "host_mounts": [], "profile": "VALID", "creme_root": "/x"}
        code, text, resolve = self.run_build(reading, probe=True)
        # The probe reaches the toolchain (which fails here, on purpose): the guard does not refuse it.
        resolve.assert_called_once()
        self.assertNotIn("disk headroom", text)


class DoctorDiskRowTest(unittest.TestCase):
    def reading(self, verdict, mounts=("/mnt/d",), detail="binding /mnt/d has 40.00 GiB free"):
        return {"verdict": verdict, "free_gib": 40.0, "binding_path": "/mnt/d",
                "per_path": {"/home/agent/creme": {"free_gib": 720.0, "error": None},
                             "/mnt/d": {"free_gib": 40.0, "error": None}},
                "floor_gib": 50, "host_mounts": list(mounts), "profile": "VALID", "detail": detail,
                "creme_root": "/home/agent/creme"}

    def row(self, reading):
        with patch("creme.disk_headroom.headroom", return_value=reading):
            (check,) = check_disk_headroom(Path("/home/agent/creme"), Path("/nonexistent/profile.json"))
        return check

    def test_low_fails_and_names_binding_path_and_floor(self):
        check = self.row(self.reading("LOW"))
        self.assertEqual(check.name, "disk headroom")
        self.assertEqual(check.status, STATUS_FAIL)

    def test_ok_with_host_mount_passes_and_lists_each_path(self):
        check = self.row(self.reading("OK"))
        self.assertEqual(check.status, STATUS_OK)

    def test_ok_without_host_mount_warns(self):
        check = self.row(self.reading("OK", mounts=()))
        self.assertEqual(check.status, STATUS_WARN)
        self.assertIn("host_mounts", check.detail)

    def test_unknown_fails(self):
        check = self.row(self.reading("UNKNOWN", detail="/mnt/d: not mounted"))
        self.assertEqual(check.status, STATUS_FAIL)


if __name__ == "__main__":
    unittest.main()
