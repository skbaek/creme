from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme.adapters.base import Adapter
from creme.doctor import (
    check_client_surface,
    check_goal_store,
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

    def test_goal_store_reports_master_state_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.assertIn("not configured", check_goal_store(workspace, None)[0].detail)
            profile = {"workspace": {"goal_store": "plans"}}
            missing = check_goal_store(workspace, profile)[0]
            self.assertEqual(missing.status, STATUS_FAIL)
            (workspace / "plans").mkdir()
            absent = check_goal_store(workspace, profile)[0]
            self.assertEqual(absent.status, STATUS_OK)
            self.assertIn("master state absent", absent.detail)
            (workspace / "plans" / "master").mkdir()
            (workspace / "plans" / "master" / "board.md").write_text("# Board\n", encoding="utf-8")
            present = check_goal_store(workspace, profile)[0]
            self.assertIn("master state present", present.detail)
            (workspace / "plans" / "master" / "board.md").unlink()
            (workspace / "plans" / "master" / "board.json").write_text("{}\n", encoding="utf-8")
            structured = check_goal_store(workspace, profile)[0]
            self.assertIn("master state present", structured.detail)

    def test_git_goal_store_requires_master_state_to_be_ignored_and_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = workspace / "plans"
            store.mkdir()
            subprocess.run(["git", "init", "-q", str(store)], check=True)
            master = store / "master"
            master.mkdir()
            (master / "board.md").write_text("# Board\n", encoding="utf-8")
            profile = {"workspace": {"goal_store": "plans"}}

            unignored = check_goal_store(workspace, profile)[0]
            self.assertEqual(unignored.status, STATUS_FAIL)
            self.assertIn("not ignored", unignored.detail)

            (store / ".gitignore").write_text("/master/\n", encoding="utf-8")
            private = check_goal_store(workspace, profile)[0]
            self.assertEqual(private.status, STATUS_OK)
            self.assertIn("ignored and untracked", private.detail)

            subprocess.run(
                ["git", "-C", str(store), "add", "-f", "master/board.md"],
                check=True,
            )
            tracked = check_goal_store(workspace, profile)[0]
            self.assertEqual(tracked.status, STATUS_FAIL)
            self.assertIn("Git-tracked", tracked.detail)

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

    def test_muse_global_mcp_check_follows_settings_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "creme"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "versions.json").write_text(
                json.dumps({"lean_lsp_mcp": "0.26.1"}), encoding="utf-8"
            )
            home = Path(tmp) / "home"
            settings = home / ".config" / "muse" / "settings.json"

            def muse_check():
                with mock.patch.object(Path, "home", return_value=home):
                    checks = check_client_surface(root)
                return [c for c in checks if c.name == "client: Muse global MCP"]

            self.assertEqual(muse_check(), [])
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            missing = muse_check()
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0].status, STATUS_WARN)
            self.assertIn("not configured", missing[0].detail)
            settings.write_text(json.dumps({"mcpServers": {"lean-lsp-mcp": {
                "transport": "stdio",
                "command": "/usr/bin/python3",
                "args": ["-m", "creme", "lean-mcp", "--", "uvx", "lean-lsp-mcp==0.26.1"],
                "env": {
                    "LEAN_MCP_DISABLED_TOOLS": "lean_build,lean_profile_proof",
                    "LEAN_LSP_MAX_OPEN_FILES": "2",
                    "LEAN_LSP_TEST_MODE": "1",
                },
            }}}), encoding="utf-8")
            matching = muse_check()
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].status, STATUS_OK, matching[0].detail)
            self.assertIn("pinned 0.26.1", matching[0].detail)
            drifted = json.loads(settings.read_text(encoding="utf-8"))
            drifted["mcpServers"]["lean-lsp-mcp"]["args"][-1] = "lean-lsp-mcp==0.0.0"
            settings.write_text(json.dumps(drifted), encoding="utf-8")
            stale = muse_check()
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0].status, STATUS_WARN)
            self.assertIn("drift", stale[0].detail)


if __name__ == "__main__":
    unittest.main()
