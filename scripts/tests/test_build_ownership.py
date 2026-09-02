from __future__ import annotations

import io
import json
import os
import subprocess
import signal
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from creme import build_ownership as owned
from creme.cli import cmd_lake_build, cmd_lean_mcp
from creme.doctor import STATUS_FAIL, STATUS_OK, check_client_surface


ROOT = Path(__file__).resolve().parents[2]


class BuildOwnershipTest(unittest.TestCase):
    def test_canonical_creme_launcher_binds_system_python(self) -> None:
        self.assertEqual((ROOT / "scripts" / "creme").read_text().splitlines()[0], "#!/usr/bin/python3")

    def test_build_guidance_uses_canonical_launcher_from_sibling_worktrees(self) -> None:
        surfaces = (
            ROOT / "AGENTS.md",
            ROOT / "docs" / "guides" / "execution.md",
            ROOT / "docs" / "guides" / "lean-edit-loops.md",
            ROOT / ".agents" / "skills" / "lean-inspector" / "SKILL.md",
            ROOT / ".agents" / "skills" / "lean-prover" / "SKILL.md",
        )
        for surface in surfaces:
            text = surface.read_text(encoding="utf-8")
            self.assertNotIn("python3 -m creme lake-build", text, surface)
            self.assertNotIn("python3 -m creme\nlake-build", text, surface)
            self.assertIn("~/creme/scripts/creme lake-build", text.replace("\n", " "), surface)

    def test_doctor_validates_every_client_surface(self) -> None:
        checks = check_client_surface(ROOT)
        by_name = {check.name: check for check in checks}
        self.assertEqual(by_name["client: guarded MCP launcher"].status, STATUS_OK)
        self.assertEqual(by_name["client: Lean build ownership"].status, STATUS_OK)
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            for relative in (".codex/config.toml", ".mcp.json", ".agents/mcp_config.json"):
                path = fixture / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
            (fixture / "scripts").mkdir()
            (fixture / "scripts" / "versions.json").write_text('{"lean_lsp_mcp":"0.26.1"}\n')
            (fixture / ".mcp.json").write_text(
                (fixture / ".mcp.json").read_text().replace('"LEAN_LSP_TEST_MODE": "1",\n', "")
            )
            checks = check_client_surface(fixture)
            row = next(check for check in checks if check.name == "client: Lean build ownership")
            self.assertEqual(row.status, STATUS_FAIL)
            self.assertIn(".mcp.json", row.detail)
            (fixture / ".mcp.json").write_text((ROOT / ".mcp.json").read_text().replace(
                '"command": "/usr/bin/python3"', '"command": "echo"'
            ))
            checks = check_client_surface(fixture)
            row = next(check for check in checks if check.name == "client: guarded MCP launcher")
            self.assertEqual(row.status, STATUS_FAIL)

    def test_ledger_skips_corruption_and_finds_cross_worktree_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREME_BUILD_LEDGER": str(Path(tmp) / "ledger.jsonl")}):
            common = {
                "kind": "build", "goal": "g", "targets": ["T"], "command": ["lake", "build", "T"],
                "exit": 0, "wall_seconds": 4.0, "threads": 2, "probe": False,
                "admission": "ADMITTED_HARD", "contention": "sensitive",
                "modules_rebuilt": ["M"], "modules_restored": [], "module_hashes": {"M": "abc"},
                "module_seconds": {"M": 1.25},
            }
            owned.append_ledger({**common, "worktree": "/a"})
            with owned.ledger_path().open("a") as output:
                output.write("not json\n")
            owned.append_ledger({**common, "worktree": "/b"})
            report = owned.ledger_rollup("7d")
            self.assertEqual(report["corrupt_lines_skipped"], 1)
            self.assertEqual(report["duplicate_hash_pairs"], 1)
            self.assertEqual(report["duplicate_hash_seconds"], 1.25)

    def test_ledger_concurrent_writers_produce_complete_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREME_BUILD_LEDGER": str(Path(tmp) / "ledger.jsonl")}):
            def write(index: int) -> None:
                owned.append_ledger({
                    "kind": "guard", "worktree": f"/{index}", "goal": "g", "targets": [],
                    "command": ["lake", "setup-file"], "exit": 0, "rewritten": False,
                    "reason": "control",
                })
            threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rows = [json.loads(line) for line in owned.ledger_path().read_text().splitlines()]
            self.assertEqual(len(rows), 20)

    def test_ledger_rejects_private_free_form_fields(self) -> None:
        with self.assertRaises(ValueError):
            owned.append_ledger({"kind": "build", "note": "private semaphore note"})

    @patch("creme.build_ownership.append_ledger")
    @patch("creme.build_ownership.subprocess.run")
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_setup_file_is_rewritten_and_stale_is_logged(self, resolve: Mock, run: Mock, append: Mock) -> None:
        run.return_value = SimpleNamespace(returncode=3)
        with patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")):
            self.assertEqual(owned.lake_guard_main(["setup-file", "A.lean", "-"]), 3)
        invoked = run.call_args.args[0]
        self.assertIn("--no-build", invoked)
        self.assertIn("--no-cache", invoked)
        self.assertEqual(append.call_args.args[0]["kind"], "guard_refusal")
        self.assertTrue(append.call_args.args[0]["rewritten"])

    @patch("creme.build_ownership.append_ledger")
    @patch("creme.build_ownership.subprocess.run")
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_already_guarded_setup_file_passes_unchanged(self, resolve: Mock, run: Mock, append: Mock) -> None:
        run.return_value = SimpleNamespace(returncode=0)
        args = ["setup-file", "A.lean", "-", "--no-build", "--no-cache"]
        with patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")):
            self.assertEqual(owned.lake_guard_main(args), 0)
        self.assertEqual(run.call_args.args[0][1:], args)
        self.assertFalse(append.call_args.args[0]["rewritten"])

    @patch("creme.build_ownership.append_ledger")
    @patch("creme.build_ownership.subprocess.run")
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_unknown_and_build_invocations_fail_closed(self, resolve: Mock, run: Mock, append: Mock) -> None:
        with patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")):
            self.assertEqual(owned.lake_guard_main(["build", "Target"]), owned.GUARD_REFUSAL_EXIT)
        run.assert_not_called()
        self.assertEqual(append.call_args.args[0]["kind"], "guard_refusal")

    @patch("creme.build_ownership.os.execve")
    @patch("creme.build_ownership._toolchain_facade")
    @patch("creme.build_ownership.guard_bin")
    @patch("creme.build_ownership.resolve_toolchain")
    def test_serve_uses_real_lake_and_injects_proxy_environment(
        self, resolve: Mock, guard_bin: Mock, facade: Mock, execve: Mock,
    ) -> None:
        resolve.return_value = (Path("/tool/lake"), Path("/tool/lean"), Path("/real/sysroot"))
        guard_bin.return_value = Path("/guard/bin")
        facade.return_value = Path("/facade")
        execve.side_effect = RuntimeError("exec captured")
        with self.assertRaisesRegex(RuntimeError, "captured"):
            owned.lake_guard_main(["serve", "--", "-Dserver.reportDelayMs=0"])
        executable, argv, env = execve.call_args.args
        self.assertEqual(executable, Path("/tool/lake"))
        self.assertEqual(argv[1], "serve")
        self.assertEqual(env["LEAN_SYSROOT"], "/facade")
        self.assertEqual(env["CREME_LAKE_GUARD"], "/guard/bin/lake")

    def test_lean_proxy_fails_closed_without_guard_identity(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(owned.lean_proxy_main(["--server"]), owned.GUARD_REFUSAL_EXIT)

    def test_lean_proxy_rejects_non_server_shape(self) -> None:
        with patch.dict(os.environ, {
            "CREME_REAL_LEAN": "/bin/sh", "CREME_REAL_SYSROOT": "/", "CREME_LAKE_GUARD": "/bin/sh",
        }, clear=True):
            self.assertEqual(owned.lean_proxy_main(["A.lean"]), owned.GUARD_REFUSAL_EXIT)

    @patch("creme.build_ownership.subprocess.Popen")
    @patch("creme.build_ownership.semaphore.adaptive_acquire")
    @patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g"))
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_refused_admission_spawns_no_lake(self, resolve: Mock, goal_label: Mock, acquire: Mock, popen: Mock) -> None:
        acquire.return_value = (False, "DEFER_FOR_HARD — foreign hard hold")
        output = io.StringIO()
        self.assertEqual(owned.run_lake_build("g", ["T"], stdout=output), 2)
        popen.assert_not_called()
        self.assertIn("REFUSED", output.getvalue())

    @patch("creme.build_ownership.append_ledger")
    @patch("creme.build_ownership.subprocess.run")
    @patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g"))
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_probe_uses_no_build_without_admission(self, resolve: Mock, goal_label: Mock, run: Mock, append: Mock) -> None:
        run.return_value = SimpleNamespace(returncode=3, stdout="stale\n")
        output = io.StringIO()
        self.assertEqual(owned.run_lake_build("g", ["T"], probe=True, stdout=output), 3)
        self.assertIn("--no-build", run.call_args.args[0])
        self.assertEqual(append.call_args.args[0]["admission"], "NOT_REQUIRED_NO_BUILD")
        self.assertIn('"status": "STALE"', output.getvalue())

    def test_build_output_parser_separates_rebuilt_and_restored(self) -> None:
        rebuilt, restored, seconds = owned._parse_build_output([
            "[1/2] Built Blanc.A (1.25s)\n",
            "[2/2] Replayed Blanc.B (20ms)\n",
        ])
        self.assertEqual(rebuilt, ["Blanc.A"])
        self.assertEqual(restored, ["Blanc.B"])
        self.assertEqual(seconds, {"Blanc.A": 1.25, "Blanc.B": 0.02})

    def test_since_validation_is_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            owned._parse_since("yesterday", datetime.now(timezone.utc))

    @patch("creme.build_ownership.resolve_tool")
    def test_wrapper_rejects_lake_options_disguised_as_targets(self, resolve: Mock) -> None:
        output = io.StringIO()
        self.assertEqual(owned.run_lake_build("g", ["--old"], stdout=output), 2)
        resolve.assert_not_called()
        self.assertIn("not options", output.getvalue())

    def test_guard_attributes_goal_worktree_without_free_form_note(self) -> None:
        self.assertEqual(
            owned._apparent_goal(Path("/workspace/blanc/.worktrees/goal-v1")),
            "goal-v1",
        )

    def test_worktree_identity_uses_canonical_root_for_subdirectories(self) -> None:
        root = Path("/workspace/blanc/.worktrees/g")
        with patch("creme.build_ownership._goal_worktree_roots", return_value=(root,)):
            self.assertEqual(owned._worktree_identity(root / "Blanc", "g"), (root, "g"))
            self.assertEqual(owned._worktree_identity(Path("/tmp/.worktrees/g/project"), "g")[1], "<unowned>")

    @patch("creme.build_ownership.resolve_toolchain")
    def test_wrapper_refuses_main_clone_or_wrong_goal(self, resolve: Mock) -> None:
        output = io.StringIO()
        self.assertEqual(owned.run_lake_build("not-this-worktree", ["T"], stdout=output), 2)
        resolve.assert_not_called()

    @patch("creme.build_ownership.append_ledger")
    @patch("creme.build_ownership.subprocess.run")
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_lake_env_lean_is_refused(self, resolve: Mock, run: Mock, append: Mock) -> None:
        with patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")):
            self.assertEqual(owned.lake_guard_main(["env", "lean", "A.lean"]), owned.GUARD_REFUSAL_EXIT)
        run.assert_not_called()

    def test_valid_json_wrong_types_are_counted_as_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREME_BUILD_LEDGER": str(Path(tmp) / "ledger.jsonl")}):
            path = owned.ledger_path()
            path.write_text(json.dumps({
                "schema_version": 1, "time": datetime.now(timezone.utc).isoformat(),
                "kind": "build", "wall_seconds": "oops",
            }) + "\n")
            report = owned.ledger_rollup("7d")
            self.assertEqual(report["corrupt_lines_skipped"], 1)

    def test_duplicate_identity_includes_module_and_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREME_BUILD_LEDGER": str(Path(tmp) / "ledger.jsonl")}):
            base = {
                "kind": "build", "goal": "g", "targets": ["T"], "command": ["lake", "build", "T"],
                "exit": 0, "wall_seconds": 1.0, "threads": 2, "probe": False,
                "admission": "ADMITTED_HARD", "contention": "sensitive", "modules_restored": [],
                "module_seconds": {"A": 0.5},
            }
            owned.append_ledger({**base, "worktree": "/a", "toolchain": "/tc1/lake", "modules_rebuilt": ["A"], "module_hashes": {"A": "same"}})
            owned.append_ledger({**base, "worktree": "/b", "toolchain": "/tc2/lake", "modules_rebuilt": ["B"], "module_hashes": {"B": "same"}, "module_seconds": {"B": 0.5}})
            self.assertEqual(owned.ledger_rollup("7d")["duplicate_hash_pairs"], 0)

    def test_resolved_lake_and_lean_must_share_one_sysroot(self) -> None:
        with patch("creme.build_ownership.resolve_tool", side_effect=[Path("/a/bin/lake"), Path("/b/bin/lean")]), patch(
            "creme.build_ownership._real_sysroot", return_value=Path("/b")
        ):
            with self.assertRaisesRegex(RuntimeError, "incoherent"):
                owned.resolve_toolchain(Path.cwd())

    def test_generated_launchers_bind_interpreter_not_hostile_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREME_BUILD_OWNERSHIP_DIR": tmp}):
            binary = owned.guard_bin()
            for name in ("lake", "lean", "nice"):
                launcher = binary / name
                self.assertFalse(launcher.is_symlink())
                self.assertEqual(launcher.read_text().splitlines()[0], f"#!{Path(os.sys.executable).resolve()}")

    def test_trusted_uvx_ignores_hostile_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted-uvx"
            hostile = root / "uvx"
            trusted.write_text("trusted", encoding="utf-8")
            hostile.write_text("hostile", encoding="utf-8")
            trusted.chmod(0o500)
            hostile.chmod(0o700)
            with patch.dict(os.environ, {"PATH": str(root)}):
                self.assertEqual(owned.trusted_uvx([trusted]), trusted.resolve())

    def test_mcp_launcher_execs_identity_bound_uvx(self) -> None:
        runner = Path("/reviewed/uvx")
        with patch("creme.cli.trusted_uvx", return_value=runner), patch(
            "creme.cli.guarded_mcp_env", return_value={"PATH": "/guard"}
        ), patch("creme.cli.os.execve", side_effect=RuntimeError("captured")) as execve:
            with self.assertRaisesRegex(RuntimeError, "captured"):
                cmd_lean_mcp(SimpleNamespace(mcp_command=["--", "uvx", "lean-lsp-mcp==0.26.1"]))
        executable, argv, env = execve.call_args.args
        self.assertEqual(executable, runner)
        self.assertEqual(argv[0], str(runner))
        self.assertEqual(env["PATH"], "/guard")

    def test_terminate_process_group_leaves_no_owned_child(self) -> None:
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"], start_new_session=True)
        self.assertTrue(owned._terminate_process_group(proc, timeout=2.0))
        self.assertFalse(owned._process_group_alive(proc.pid))

    def test_sampler_reports_unavailable_instead_of_false_zero(self) -> None:
        with patch("creme.build_ownership.subprocess.run", side_effect=PermissionError("denied")):
            self.assertIsNone(owned._process_snapshot())

    def test_public_cli_has_no_thread_cap_override(self) -> None:
        with self.assertRaises(SystemExit):
            cmd_lake_build(SimpleNamespace(goal="g", build_args=["--threads", "100", "--", "T"]))

    def test_renewal_refusal_stops_current_process_group(self) -> None:
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"], start_new_session=True)
        with patch("creme.build_ownership.semaphore.renew", return_value=(False, "DRAIN_HEAVY")):
            renewer = owned.RenewalThread("g", proc, interval=0.01)
            renewer.start()
            renewer.join(timeout=3)
        self.assertTrue(renewer.refused)
        self.assertTrue(renewer.cleanup_proved)
        self.assertFalse(owned._process_group_alive(proc.pid))

    def test_renewal_exception_stops_current_process_group(self) -> None:
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"], start_new_session=True)
        with patch("creme.build_ownership.semaphore.renew", side_effect=OSError("corrupt state")):
            renewer = owned.RenewalThread("g", proc, interval=0.01)
            renewer.start()
            renewer.join(timeout=3)
        self.assertTrue(renewer.refused)
        self.assertIn("OSError", renewer.verdicts[0])
        self.assertTrue(renewer.cleanup_proved)
        self.assertFalse(owned._process_group_alive(proc.pid))

    def test_priority_launcher_refuses_before_exec_when_niceness_fails(self) -> None:
        with patch("creme.build_ownership.os.nice", side_effect=PermissionError("denied")), patch(
            "creme.build_ownership.os.execv"
        ) as execv:
            self.assertEqual(owned.nice_main(["-n", "10", "/bin/sh", "-c", "true"]), owned.GUARD_REFUSAL_EXIT)
            execv.assert_not_called()

    def test_parent_only_sigterm_cleans_children_before_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_lake = root / "lake"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_nice = fake_bin / "nice"
            child_pid = root / "child.pid"
            released = root / "released"
            ledger = root / "ledger.jsonl"
            fake_lake.write_text(
                f"#!/bin/sh\nsleep 30 &\necho $! > {str(child_pid)!r}\nwait\n",
                encoding="utf-8",
            )
            fake_lake.chmod(0o700)
            fake_nice.write_text("#!/bin/sh\nshift 2\nexec \"$@\"\n", encoding="utf-8")
            fake_nice.chmod(0o700)
            script = f"""
import os
from pathlib import Path
from unittest.mock import patch
from creme import build_ownership as owned

child = Path({str(child_pid)!r})
released = Path({str(released)!r})
def release(_goal):
    pid = int(child.read_text())
    try:
        os.kill(pid, 0)
        state = 'alive'
    except ProcessLookupError:
        state = 'dead'
    released.write_text(state)
    return True, 'released'

with patch('creme.build_ownership._worktree_identity', return_value=(Path.cwd(), 'g')), \\
     patch('creme.build_ownership.resolve_toolchain', return_value=(Path({str(fake_lake)!r}), Path('/bin/sh'), Path('/'))), \\
     patch('creme.build_ownership.guard_bin', return_value=Path({str(fake_bin)!r})), \\
     patch('creme.build_ownership.semaphore.adaptive_acquire', return_value=(True, 'ADMITTED_HARD')), \\
     patch('creme.build_ownership.semaphore.adaptive_release', side_effect=release):
    raise SystemExit(owned.run_lake_build('g', ['T']))
"""
            env = os.environ.copy()
            env["CREME_BUILD_LEDGER"] = str(ledger)
            proc = subprocess.Popen([os.sys.executable, "-c", script], cwd=ROOT, env=env)
            for _ in range(100):
                if child_pid.exists():
                    break
                proc.poll()
                if proc.returncode is not None:
                    break
                threading.Event().wait(0.02)
            self.assertTrue(child_pid.exists())
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=5), 143)
            self.assertEqual(released.read_text(), "dead")
            child = int(child_pid.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_measurement_snapshot_precedes_release_mutation(self) -> None:
        events: list[str] = []
        trace = {"M": "first"}
        captured: list[dict] = []

        class FakeProc:
            pid = 424242
            stdout = ["Built M (1s)\n"]

            def wait(self, timeout=None):
                return 0

        class FakeSampler:
            def __init__(self, _pid):
                self.samples = 1
                self.unavailable_samples = 0
                self.peak_rss_mib = 10.0
                self.peak_lean_rss_mib = 8.0
                self.max_concurrent_lean = 1

            def start(self):
                pass

            def stop(self):
                pass

        class FakeRenewer:
            def __init__(self, _goal, _proc):
                self.refused = False
                self.cleanup_proved = True
                self.verdicts = []

            def start(self):
                pass

            def stop(self):
                pass

        def hashes(_worktree, _modules):
            events.append("hash")
            return dict(trace)

        def release(_goal):
            events.append("release")
            trace["M"] = "second"
            return True, "released"

        def ledger(row):
            events.append("ledger")
            captured.append(row)

        with patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")), patch(
            "creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool"))
        ), patch("creme.build_ownership.semaphore.adaptive_acquire", return_value=(True, "ADMITTED_HARD")), patch(
            "creme.build_ownership.semaphore.adaptive_release", side_effect=release
        ), patch("creme.build_ownership.guard_bin", return_value=Path("/guard")), patch(
            "creme.build_ownership.subprocess.Popen", return_value=FakeProc()
        ), patch("creme.build_ownership.ProcessSampler", FakeSampler), patch(
            "creme.build_ownership.RenewalThread", FakeRenewer
        ), patch("creme.build_ownership._process_group_alive", return_value=False), patch(
            "creme.build_ownership._module_hashes", side_effect=hashes
        ), patch("creme.build_ownership._swap_gib", side_effect=[1.0, 2.0]), patch(
            "creme.build_ownership.append_ledger", side_effect=ledger
        ):
            self.assertEqual(owned.run_lake_build("g", ["T"], stdout=io.StringIO()), 0)
        self.assertEqual(events, ["hash", "release", "ledger"])
        self.assertEqual(captured[0]["module_hashes"], {"M": "first"})


if __name__ == "__main__":
    unittest.main()
