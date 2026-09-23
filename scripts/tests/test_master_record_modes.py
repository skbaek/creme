from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import cli, master_operations, master_runtime, semaphore
from creme.profile import propose, write_reviewed
from test_master_operations import FixtureAdapter


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class MasterRecordModesTest(unittest.TestCase):
    """Widened modes: the writer tightens them, readers name them exactly."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.creme = self.workspace / "creme"
        (self.creme / ".creme").mkdir(parents=True)
        self.store = self.workspace / "goals"
        self.store.mkdir()
        subprocess.run(["git", "init", "-q", str(self.store)], check=True)
        (self.store / ".gitignore").write_text("/master/\n", encoding="utf-8")
        adapter = FixtureAdapter()
        candidate = propose(self.creme, self.workspace, adapter, goal_store="goals")
        write_reviewed(self.creme / ".creme/host-profile.json", candidate)
        self.location = master_operations.resolve_runtime_location(
            self.creme, adapter=adapter
        )
        plan = master_operations.initialize(self.location, apply=True)
        self.assertEqual(plan.status, "OK")
        self.root = self.location.record_root
        self.environment = mock.patch.dict(os.environ, {
            "CREME_SEMAPHORE_DIR": str(self.workspace / "semaphore"),
            "CREME_MASTER_SESSION_ID": "",
            "CREME_MASTER_LIVENESS_SOCKET": "",
        }, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        client = mock.patch(
            "creme.semaphore._client_process",
            return_value=(os.getpid(), "claude", "synthetic client"),
        )
        client.start()
        self.addCleanup(client.stop)

    def acquire(self):
        ok, detail = semaphore.master_acquire("claude", "synthetic modes")
        self.assertTrue(ok, detail)

    def cli(self, *arguments, stdin_bytes=None):
        output = io.StringIO()
        patches = [
            mock.patch("creme.cli._master_location", return_value=(self.location, None)),
            mock.patch("sys.stdout", output),
        ]
        if stdin_bytes is not None:
            patches.append(
                mock.patch("creme.cli.sys.stdin", SimpleNamespace(buffer=io.BytesIO(stdin_bytes)))
            )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            code = cli.main(list(arguments))
        return code, json.loads(output.getvalue())

    def event(self):
        payload = {
            "title": "note",
            "note": "synthetic note",
            "evidence": "synthetic-evidence.json",
            "next_unit": "next",
        }
        data = json.dumps({"kind": "note", "payload": payload}).encode()
        return self.cli("master", "event", "--from", "-", stdin_bytes=data)

    def widen(self):
        brief = self.root / "briefs" / "brief.md"
        brief.write_text("synthetic brief\n", encoding="utf-8")
        brief.chmod(0o644)
        nested = self.root / "audits" / "nested"
        nested.mkdir()
        nested.chmod(0o755)
        inner = nested / "inner.md"
        inner.write_text("inner\n", encoding="utf-8")
        inner.chmod(0o600)
        return brief, nested

    def test_event_tightens_widened_brief_and_directory(self):
        brief, nested = self.widen()
        self.acquire()
        code, result = self.event()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(_mode(brief), 0o600)
        self.assertEqual(_mode(nested), 0o700)
        self.assertEqual(_mode(nested / "inner.md"), 0o600)
        self.assertEqual(
            sorted(result["modes_normalized"], key=lambda item: item["path"]),
            [
                {"path": "audits/nested", "from": "0755", "to": "0700"},
                {"path": "briefs/brief.md", "from": "0644", "to": "0600"},
            ],
        )
        view = master_runtime.read_record(self.root)
        self.assertEqual([row["kind"] for row in view.events], ["note"])
        # An already-private record reports nothing on the next event.
        code, result = self.event()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["modes_normalized"], [])

    def test_nonholder_event_refuses_and_changes_no_mode(self):
        brief, _ = self.widen()
        code, result = self.event()
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(_mode(brief), 0o644)

    def test_digest_names_the_mode_violation_and_changes_nothing(self):
        brief, nested = self.widen()
        self.acquire()
        code, result = self.cli("master", "digest", "--goals-limit", "0")
        self.assertEqual(code, 2)
        detail = result["detail"]
        self.assertNotIn("migration", detail)
        self.assertIn(f"file {brief} has mode 0644, expected 0600", detail)
        self.assertIn(f"directory {nested} has mode 0755, expected 0700", detail)
        self.assertIn(f"chmod 600 {brief}", detail)
        self.assertIn(f"chmod 700 {nested}", detail)
        self.assertEqual(_mode(brief), 0o644)
        self.assertEqual(_mode(nested), 0o755)

    def test_mode_error_is_not_diagnosed_as_migration(self):
        brief, _ = self.widen()
        with self.assertRaises(master_runtime.MasterModeError) as caught:
            master_runtime.read_record(self.root)
        self.assertEqual(caught.exception.violation.path, str(brief))
        self.assertEqual(caught.exception.violation.mode, 0o644)
        from creme import master_migrate

        with self.assertRaises(master_runtime.MasterModeError):
            master_migrate._current_view(self.root)
        with self.assertRaises(master_runtime.MasterModeError):
            master_migrate._idempotent_plan(self.root)
        plan = master_migrate.plan_migration(self.root)
        self.assertEqual(plan.status, "REFUSED")
        self.assertIn("has mode 0644", plan.detail)

    def test_foreign_owned_file_is_not_touched_and_still_refuses(self):
        brief, _ = self.widen()
        real_nodes = master_runtime._private_nodes

        def foreign(root):
            for path, info in real_nodes(root):
                if path == brief:
                    values = list(info)
                    values[stat.ST_UID] = os.geteuid() + 1
                    info = os.stat_result(values)
                yield path, info

        real_validate = master_runtime._validate_private_tree

        def validate(root):
            if brief.parent == root:
                raise master_runtime.MasterRecordError(
                    f"private path {brief.name} is not owned by the current user"
                )
            real_validate(root)

        self.acquire()
        with (
            mock.patch.object(master_runtime, "_private_nodes", foreign),
            mock.patch.object(master_runtime, "_validate_private_tree", validate),
        ):
            code, result = self.event()
        self.assertEqual(code, 2, result)
        self.assertIn("not owned by the current user", result["detail"])
        self.assertEqual(_mode(brief), 0o644)
        # The descriptor check refuses an inode whose owner changed after lstat.
        info = brief.lstat()
        real_fstat = os.fstat

        def fstat(descriptor):
            values = list(real_fstat(descriptor))
            values[stat.ST_UID] = os.geteuid() + 1
            return os.stat_result(values)

        with mock.patch.object(master_runtime.os, "fstat", fstat):
            self.assertIsNone(master_runtime._tighten_node(brief, info, 0o600))
        self.assertEqual(_mode(brief), 0o644)

    def test_symlinks_are_never_followed(self):
        outside = self.workspace / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        outside.chmod(0o644)
        outside_directory = self.workspace / "outside-dir"
        outside_directory.mkdir()
        outside_directory.chmod(0o755)
        (outside_directory / "leaf.md").write_text("leaf\n", encoding="utf-8")
        (outside_directory / "leaf.md").chmod(0o644)
        (self.root / "briefs" / "link.md").symlink_to(outside)
        (self.root / "briefs" / "link-dir").symlink_to(outside_directory)
        self.acquire()
        code, result = self.event()
        self.assertEqual(code, 2, result)
        self.assertIn("must not be a symlink", result["detail"])
        self.assertEqual(_mode(outside), 0o644)
        self.assertEqual(_mode(outside_directory), 0o755)
        self.assertEqual(_mode(outside_directory / "leaf.md"), 0o644)
        self.assertEqual(master_runtime.normalize_private_modes(self.root), ())
        self.assertEqual(master_runtime.mode_violations(self.root), ())

    def test_normalization_never_loosens(self):
        brief = self.root / "briefs" / "readonly.md"
        brief.write_text("readonly\n", encoding="utf-8")
        brief.chmod(0o444)
        changes = master_runtime.normalize_private_modes(self.root)
        self.assertEqual([change.to_dict() for change in changes], [
            {"path": "briefs/readonly.md", "from": "0444", "to": "0400"},
        ])
        self.assertEqual(_mode(brief), 0o400)
        with self.assertRaises(master_runtime.MasterModeError):
            master_runtime.read_record(self.root)


if __name__ == "__main__":
    unittest.main()
