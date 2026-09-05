from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import signal
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock, patch

from creme import build_ownership as owned
from creme.profile import ADMISSION_DEFAULTS
from creme.cli import cmd_lake_build, cmd_lean_mcp
from creme.doctor import STATUS_FAIL, STATUS_OK, check_client_surface


ROOT = Path(__file__).resolve().parents[2]
SETTINGS = dict(ADMISSION_DEFAULTS)
# A probe that could not run: the wrapper keeps its target-keyed fallback.
UNPROBED = {
    "roots": ["T"], "package_roots": ["T"], "resolution": "T (module)",
    "stale": None, "detail": "probe unavailable: fixture", "stale_set": None, "graph": None,
}


@contextmanager
def _ledger_and_log():
    """Isolate both the build ledger and the shared coordination log."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ledger = root / "ledger.jsonl"
        state = root / "state"
        state.mkdir()
        log = state / "log.jsonl"
        log.touch()
        with patch.dict(os.environ, {
            "CREME_BUILD_LEDGER": str(ledger),
            "CREME_SEMAPHORE_DIR": str(state),
        }):
            yield ledger, log


def _write_ledger(path: Path, rows: list[tuple[str, dict]]) -> None:
    path.write_text(
        "".join(
            json.dumps({"schema_version": 1, "time": time, **row}) + "\n"
            for time, row in rows
        ),
        encoding="utf-8",
    )


def _write_log(path: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    path.write_text(
        "".join(
            json.dumps({
                "time": time, "action": action, "label": label,
                "verdict": verdict, "detail": detail,
            }) + "\n"
            for time, action, label, verdict, detail in rows
        ),
        encoding="utf-8",
    )


class LakeCacheEnvironmentTest(unittest.TestCase):
    def test_cache_overrides_host_defaults_without_mutating_caller(self) -> None:
        base = {"LAKE_CACHE_DIR": "/outside/cache", "KEEP": "value"}
        with patch.object(owned.semaphore, "canonical_creme_root", return_value=Path("/workspace/creme")):
            env = owned.lake_env(base)
        self.assertEqual(env, {"LAKE_CACHE_DIR": "/workspace/creme/.creme/lake-cache", "KEEP": "value"})
        self.assertEqual(base["LAKE_CACHE_DIR"], "/outside/cache")

    def test_linked_creme_worktree_uses_shared_checkout_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "creme"
            gitdir = root / ".git" / "worktrees" / "goal"
            gitdir.mkdir(parents=True)
            (gitdir / "commondir").write_text("../..\n")
            linked = root / ".worktrees" / "goal"
            linked.mkdir(parents=True)
            (linked / ".git").write_text(f"gitdir: {gitdir}\n")
            resolver = owned.semaphore.canonical_creme_root
            with patch.object(owned.semaphore, "canonical_creme_root", side_effect=lambda: resolver(linked)):
                self.assertEqual(owned.lake_env({})["LAKE_CACHE_DIR"], str(root / ".creme/lake-cache"))
            self.assertFalse((root / ".creme").exists())

    def test_mcp_environment_uses_same_cache_even_with_empty_base(self) -> None:
        with patch.object(owned, "guard_bin", return_value=Path("/guard")), patch.dict(
            os.environ, {"LAKE_CACHE_DIR": "/outside/cache", "UNRELATED": "host"}
        ):
            env = owned.guarded_mcp_env({})
        self.assertEqual(env["LAKE_CACHE_DIR"], str(owned.semaphore.canonical_creme_root() / ".creme/lake-cache"))
        self.assertEqual(env["PATH"], "/guard:")
        self.assertNotIn("UNRELATED", env)


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

    def test_every_lean_surface_states_the_diagnostics_first_loop(self) -> None:
        """B6: a reader of any one surface can reproduce the loop."""
        surfaces = (
            ROOT / "AGENTS.md",
            ROOT / "docs" / "guides" / "execution.md",
            ROOT / "docs" / "guides" / "lean-edit-loops.md",
            ROOT / ".agents" / "skills" / "lean-prover" / "SKILL.md",
            ROOT / ".agents" / "skills" / "lean-inspector" / "SKILL.md",
        )
        for surface in surfaces:
            text = " ".join(surface.read_text(encoding="utf-8").split())
            self.assertIn("lean_diagnostic_messages", text, surface)
            self.assertIn("imports are current", text.lower(), surface)
            self.assertIn("loop evidence", text.lower(), surface)
        for surface in surfaces[:4]:
            text = " ".join(surface.read_text(encoding="utf-8").split())
            self.assertIn("--wait", text, surface)

    def test_the_narrow_build_example_no_longer_copies_a_contention_class(self) -> None:
        text = (ROOT / "docs" / "guides" / "execution.md").read_text(encoding="utf-8")
        narrow = [
            line for line in text.splitlines()
            if "lake-build GOAL" in line and "Narrow.Target" in line
        ]
        self.assertTrue(narrow)
        for line in narrow:
            self.assertNotIn("--contention", line)
            self.assertNotIn("--memory-gib", line)

    def test_the_guides_forbid_a_hand_rolled_status_poll(self) -> None:
        for surface in (
            ROOT / "AGENTS.md",
            ROOT / "docs" / "guides" / "execution.md",
            ROOT / ".agents" / "skills" / "lean-prover" / "SKILL.md",
        ):
            text = " ".join(surface.read_text(encoding="utf-8").split()).lower()
            self.assertIn("never write a shell loop around", text, surface)

    def test_no_surface_still_says_restart_the_lean_server(self) -> None:
        """B9: the instruction names a mechanism this client actually has."""
        surfaces = (
            ROOT / "AGENTS.md",
            ROOT / "docs" / "guides" / "execution.md",
            ROOT / "docs" / "guides" / "lean-edit-loops.md",
            ROOT / ".agents" / "skills" / "lean-prover" / "SKILL.md",
            ROOT / ".agents" / "skills" / "lean-inspector" / "SKILL.md",
        )
        for surface in surfaces + (ROOT / "creme" / "build_ownership.py",):
            joined = " ".join(surface.read_text(encoding="utf-8").split())
            self.assertNotIn("restart the Lean server", joined, surface)
            self.assertNotIn("restart the server", joined, surface)
            self.assertIn("two other Lean files", joined, surface)
        for surface in surfaces:
            # The prose names the setting the mechanism depends on; the wrapper
            # stays client-neutral and only states the two extra queries.
            joined = " ".join(surface.read_text(encoding="utf-8").split())
            self.assertIn("LEAN_LSP_MAX_OPEN_FILES", joined, surface)

    def test_the_guide_states_the_fit_arithmetic_a_waiter_needs(self) -> None:
        """B10: a reader can compute why a large estimate is unschedulable."""
        joined = " ".join(
            (ROOT / "docs" / "guides" / "execution.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("currently fit", joined)
        self.assertIn("ceil(1.25 x estimate)", joined)
        self.assertIn("max(2 GiB, 25% of physical RAM)", joined)
        self.assertIn("available >= charged + reserve", joined)
        self.assertIn("10 GiB estimate needs 19.0 GiB", joined)

    def test_the_guide_forbids_a_sleep_loop_on_your_own_process_too(self) -> None:
        joined = " ".join(
            (ROOT / "docs" / "guides" / "execution.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("kill -0 PID", joined)
        self.assertIn("never write one around your own backgrounded process", joined)

    def test_the_class_table_covers_full_and_package_targets(self) -> None:
        joined = " ".join(
            (ROOT / "docs" / "guides" / "execution.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("a warm **full target** whose stale closure is small", joined)
        self.assertIn("a warm **package or library target**", joined)
        self.assertIn("roots unresolved", joined)

    def test_the_guide_says_a_gate_runner_holds_only_for_its_lean_rows(self) -> None:
        joined = " ".join(
            (ROOT / "docs" / "guides" / "execution.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("one** hold sized for its Lean rows", joined)
        self.assertIn("take no hold at all", joined)

    def test_the_guide_says_not_to_filter_the_wrappers_output(self) -> None:
        joined = " ".join(
            (ROOT / "docs" / "guides" / "execution.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("Never filter the wrapper's output", joined)

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
        self.assertEqual(run.call_args.kwargs["env"]["LAKE_CACHE_DIR"], owned.lake_env({})["LAKE_CACHE_DIR"])
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
        self.assertEqual(env["LAKE_CACHE_DIR"], owned.lake_env({})["LAKE_CACHE_DIR"])
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

    @patch("creme.build_ownership.stale_evidence", return_value=UNPROBED)
    @patch("creme.build_ownership.subprocess.Popen")
    @patch("creme.build_ownership.semaphore.adaptive_acquire")
    @patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g"))
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_refused_admission_spawns_no_lake(self, resolve: Mock, goal_label: Mock, acquire: Mock, popen: Mock, probe: Mock) -> None:
        acquire.return_value = (False, "DEFER_FOR_HARD — foreign hard hold")
        output = io.StringIO()
        with _ledger_and_log():
            self.assertEqual(
                owned.run_lake_build("g", ["T"], contention="sensitive", memory_gib=8, stdout=output),
                2,
            )
        # The profile may be read for the evidence comparison; Lake is never run.
        for call in popen.call_args_list:
            self.assertNotIn("/tool/lake", " ".join(map(str, call.args[0])))
        self.assertIn("REFUSED", output.getvalue())

    @patch("creme.build_ownership.append_ledger")
    @patch("creme.build_ownership.subprocess.run")
    @patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g"))
    @patch("creme.build_ownership.resolve_toolchain", return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool")))
    def test_probe_uses_no_build_without_admission(self, resolve: Mock, goal_label: Mock, run: Mock, append: Mock) -> None:
        run.return_value = SimpleNamespace(returncode=3, stdout="stale\n", stderr="")
        output = io.StringIO()
        self.assertEqual(
            owned.run_lake_build("g", ["T"], probe=True, contention="sensitive", stdout=output),
            3,
        )
        for call in run.call_args_list:
            if "--no-build" in call.args[0]:
                self.assertEqual(call.kwargs["env"]["LAKE_CACHE_DIR"], owned.lake_env({})["LAKE_CACHE_DIR"])
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

    def test_window_accepts_absolute_instants_and_rejects_inverted_ranges(self) -> None:
        start, stop = owned.parse_window("2026-09-03", "2026-09-03T05:35:00Z")
        self.assertEqual(start, datetime(2026, 9, 3, tzinfo=timezone.utc))
        self.assertEqual(stop, datetime(2026, 9, 3, 5, 35, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            owned.parse_window("2026-09-03T05:35:00Z", "2026-09-03")

    def test_rollup_reports_failed_share_classes_refusals_and_lockout(self) -> None:
        with _ledger_and_log() as (ledger, log):
            base = {
                "kind": "build", "goal": "g", "targets": ["T"], "command": ["lake", "build", "T"],
                "wall_seconds": 1.0, "threads": 2, "probe": False, "admission": "ADMITTED_HARD",
                "modules_rebuilt": [], "modules_restored": [], "module_hashes": {},
                "module_seconds": {}, "worktree": "/a",
            }
            _write_ledger(ledger, [
                ("2026-09-03T00:10:00Z", {**base, "exit": 0, "contention": "tolerant"}),
                ("2026-09-03T00:20:00Z", {**base, "exit": 1, "contention": "sensitive"}),
                ("2026-09-03T00:30:00Z", {**base, "exit": 1, "contention": "sensitive"}),
                ("2026-09-03T00:40:00Z", {**base, "exit": 0, "goal": "other", "contention": "exclusive"}),
            ])
            _write_log(log, [
                ("2026-09-03T01:00:00Z", "adaptive-acquire", "g", "REFUSED", "DEFER_HEAVY: blocked"),
                ("2026-09-03T01:01:00Z", "adaptive-acquire", "g", "REFUSED", "DEFER_HEAVY: blocked"),
                ("2026-09-03T01:10:00Z", "adaptive-acquire", "g", "OK", "ADMITTED_HARD: in"),
                ("2026-09-03T01:20:00Z", "renew", "g", "REFUSED", "YIELD_HEAVY: older holder"),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")

        self.assertEqual(report["builds"], 4)
        self.assertEqual(report["failed_builds"], 2)
        self.assertEqual(report["failed_build_share"], 0.5)
        self.assertEqual(
            report["contention_class"], {"exclusive": 1, "sensitive": 2, "tolerant": 1}
        )
        goal = report["by_goal"]["g"]
        self.assertEqual(goal["builds"], 3)
        self.assertEqual(goal["failed_build_share"], 0.667)
        self.assertEqual(goal["contention_class"], {"sensitive": 2, "tolerant": 1})
        self.assertEqual(goal["refusals"], {"DEFER_HEAVY": 2})
        self.assertEqual(goal["renew_refusals"], {"YIELD_HEAVY": 1})
        # Two refusals open one episode; the episode closes at the next admission.
        self.assertEqual(goal["lockout_episodes"], 1)
        self.assertEqual(goal["lockout_seconds"], 600.0)
        self.assertFalse(goal["lockout_open"])

    def test_a_queued_admission_closes_a_lockout_and_an_enqueue_does_not(self) -> None:
        with _ledger_and_log() as (_, log):
            _write_log(log, [
                ("2026-09-03T01:00:00Z", "adaptive-acquire", "g", "REFUSED", "DEFER_HEAVY: blocked"),
                ("2026-09-03T01:00:05Z", "wait-enqueue", "g", "OK", "position=1"),
                ("2026-09-03T01:05:00Z", "wait-acquire", "g", "OK", "ADMITTED_HARD: waited=295s"),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        goal = report["by_goal"]["g"]
        self.assertFalse(goal["lockout_open"])
        self.assertEqual(goal["lockout_seconds"], 300.0)
        self.assertEqual(goal["admissions"], 1)

    def test_a_wait_timeout_opens_a_lockout_like_any_other_refusal(self) -> None:
        with _ledger_and_log() as (_, log):
            _write_log(log, [
                ("2026-09-03T01:00:00Z", "wait-acquire", "g", "REFUSED", "WAIT_TIMEOUT: no admission"),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        goal = report["by_goal"]["g"]
        self.assertTrue(goal["lockout_open"])
        self.assertEqual(goal["refusals"], {"WAIT_TIMEOUT": 1})

    def test_rollup_reports_an_unadmitted_refusal_as_open_not_zero(self) -> None:
        with _ledger_and_log() as (_, log):
            _write_log(log, [
                ("2026-09-03T01:00:00Z", "adaptive-acquire", "g", "REFUSED", "LIGHT_ONLY: low"),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        goal = report["by_goal"]["g"]
        self.assertTrue(goal["lockout_open"])
        self.assertEqual(goal["lockout_seconds"], 0.0)
        self.assertEqual(goal["lockout_open_seconds"], 3600.0)
        self.assertEqual(goal["lockout_total_seconds"], 3600.0)

    def test_rollup_skips_and_counts_corrupt_log_lines_without_failing(self) -> None:
        with _ledger_and_log() as (_, log):
            log.write_text(
                "not json\n"
                + json.dumps({"time": "2026-09-03T01:00:00Z", "action": "adaptive-acquire",
                              "label": "g", "verdict": "REFUSED", "detail": "DEFER_HEAVY: x"}) + "\n"
                + json.dumps({"time": "not-a-time", "action": "adaptive-acquire", "label": "g",
                              "verdict": "OK", "detail": "ADMITTED_HARD: x"}) + "\n"
                + json.dumps({"time": "2026-09-03T01:05:00Z", "action": "adaptive-acquire",
                              "label": "g", "verdict": "MAYBE", "detail": "x"}) + "\n",
                encoding="utf-8",
            )
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["semaphore_log_corrupt_lines_skipped"], 3)
        self.assertEqual(report["semaphore_log_status"], "OK")
        self.assertTrue(report["by_goal"]["g"]["lockout_open"])

    def test_swap_is_recorded_from_the_adapter_mib_field(self) -> None:
        sample = SimpleNamespace(data={"swap_used_mib": 2560.0})
        with patch("creme.build_ownership.get_adapter", return_value=SimpleNamespace(
            memory_headroom=lambda: sample
        )):
            self.assertEqual(owned._swap_gib(), 2.5)
        with patch("creme.build_ownership.get_adapter", return_value=SimpleNamespace(
            memory_headroom=lambda: SimpleNamespace(data={"swap_used_mib": None})
        )):
            self.assertIsNone(owned._swap_gib())

    # -- B2/B3: evidence classification and derived estimates ------------
    def _measured(self, ledger: Path, *, peak_mib: float, targets=("T",),
                  worktree="/w", toolchain="tc", manifest="mf", exit_code=0,
                  rebuilt=(), time="2026-09-03T01:00:00Z") -> None:
        _write_ledger(ledger, [(time, {
            "kind": "build", "goal": "g", "targets": list(targets),
            "command": ["lake", "build"], "exit": exit_code, "wall_seconds": 1.0,
            "threads": 2, "probe": False, "admission": "ADMITTED_SOFT",
            "contention": "tolerant", "modules_rebuilt": list(rebuilt),
            "modules_restored": [],
            "module_hashes": {}, "module_seconds": {}, "worktree": worktree,
            "peak_rss_mib": peak_mib, "toolchain_digest": toolchain,
            "manifest_digest": manifest,
        })])

    def _classify(self, stale, **kwargs):
        settings = dict(SETTINGS, **kwargs.pop("settings", {}))
        return owned.classify_contention(
            Path("/w"), ["T"], Path("/lake"), settings, ("tc", "mf"),
            {"roots": ["T"], "package_roots": ["T"], "resolution": "T (module)",
             "stale": stale, "detail": "fixture"},
        )

    def test_a_warm_narrow_target_with_a_small_measured_peak_is_tolerant(self) -> None:
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=2048.0)
            verdict, evidence = self._classify(3)
        self.assertEqual(verdict, "tolerant")
        self.assertEqual(evidence["measured_peak_gib"], 2.0)

    def test_a_cold_worktree_without_measurement_stays_sensitive(self) -> None:
        with _ledger_and_log() as (_ledger, _):
            verdict, evidence = self._classify(3)
        self.assertEqual(verdict, "sensitive")
        self.assertIn("no successful measurement", evidence["reason"])

    def test_a_changed_toolchain_or_manifest_digest_is_ignored_evidence(self) -> None:
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=2048.0, toolchain="other")
            self.assertEqual(self._classify(3)[0], "sensitive")
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=2048.0, manifest="other")
            self.assertEqual(self._classify(3)[0], "sensitive")

    def test_a_stale_set_above_the_configured_count_stays_sensitive(self) -> None:
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=2048.0)
            verdict, evidence = self._classify(9)
        self.assertEqual(verdict, "sensitive")
        self.assertIn("stale set is 9", evidence["reason"])

    def test_a_large_measured_peak_stays_sensitive(self) -> None:
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=9000.0)
            verdict, evidence = self._classify(2)
        self.assertEqual(verdict, "sensitive")
        self.assertIn("not below", evidence["reason"])

    def test_an_unmeasurable_stale_set_stays_sensitive(self) -> None:
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=2048.0)
            self.assertEqual(self._classify(None)[0], "sensitive")

    def test_a_corrupt_ledger_stays_sensitive(self) -> None:
        with _ledger_and_log() as (ledger, _):
            ledger.write_text("{not json\n", encoding="utf-8")
            self.assertEqual(self._classify(1)[0], "sensitive")

    def test_a_full_target_with_no_readable_configuration_stays_sensitive(self) -> None:
        with _ledger_and_log() as (_ledger, _):
            verdict, evidence = owned.classify_contention(
                Path("/w"), [], Path("/lake"), SETTINGS, ("tc", "mf")
            )
        self.assertEqual(verdict, "sensitive")
        self.assertIn("roots unresolved", evidence["reason"])
        self.assertIsNone(evidence["resolved_roots"])

    def test_the_estimate_comes_from_measured_peaks_plus_a_margin(self) -> None:
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=2560.0)
            estimate, evidence = owned.derive_memory_gib(
                Path("/w"), ["T"], SETTINGS, ("tc", "mf"), 8, 0
            )
        self.assertEqual(estimate, 4)          # ceil(2.5) + 1
        self.assertEqual(evidence["rows"], 1)
        self.assertEqual(evidence["measured_peak_gib"], 2.5)

    def test_the_estimate_falls_back_to_the_profile_default(self) -> None:
        with _ledger_and_log() as (_ledger, _):
            estimate, evidence = owned.derive_memory_gib(
                Path("/w"), ["T"], SETTINGS, ("tc", "mf"), 8, 0
            )
        self.assertEqual(estimate, 8)
        self.assertIn("profile default", evidence["source"])

    def test_the_estimate_never_falls_below_the_floor(self) -> None:
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=100.0)
            estimate, _ = owned.derive_memory_gib(
                Path("/w"), ["T"], SETTINGS, ("tc", "mf"), 8, 0
            )
        self.assertEqual(estimate, 2)

    def test_the_stale_count_is_the_closure_not_the_probe_frontier(self) -> None:
        # Lake's --no-build probe stops at the first out-of-date module, so the
        # frontier under-reports what a build would elaborate.  This is the
        # exact shape observed on the pinned Lean 4.32.1 toolchain.
        completed = SimpleNamespace(
            returncode=3,
            stdout=(
                "\u2716 [902/906] Building Blanc.AddressSlot\n"
                "error: target is out-of-date and needs to be rebuilt\n"
                "Some required targets logged failures:\n"
                "- Blanc.AddressSlot\n"
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "Blanc").mkdir()
            (worktree / "Blanc" / "AddressSlot.lean").write_text("def a := 1\n")
            (worktree / "Blanc" / "AddressSlotProofs.lean").write_text(
                "import Blanc.AddressSlot\n\ndef b := 2\n"
            )
            with patch("creme.build_ownership.subprocess.run", return_value=completed):
                count, detail = owned.stale_module_count(
                    worktree, ["Blanc.AddressSlotProofs"], Path("/lake")
                )
            self.assertEqual(count, 2, detail)
            with patch("creme.build_ownership.subprocess.run",
                       return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
                self.assertEqual(
                    owned.stale_module_count(worktree, ["Blanc.AddressSlot"], Path("/lake"))[0], 0
                )
            with patch("creme.build_ownership.subprocess.run",
                       return_value=SimpleNamespace(returncode=1, stdout="boom", stderr="")):
                self.assertIsNone(
                    owned.stale_module_count(worktree, ["Blanc.AddressSlot"], Path("/lake"))[0]
                )
            # A target outside the package graph is not evidence.
            with patch("creme.build_ownership.subprocess.run", return_value=completed):
                self.assertIsNone(
                    owned.stale_module_count(worktree, ["Blanc.Absent"], Path("/lake"))[0]
                )

    def test_the_stale_closure_follows_reverse_imports(self) -> None:
        graph = {
            "P.Leaf": set(),
            "P.Mid": {"P.Leaf"},
            "P.Top": {"P.Mid"},
            "P.Other": set(),
        }
        self.assertEqual(owned.stale_closure(graph, ["P.Top"], {"P.Leaf"}), 3)
        self.assertEqual(owned.stale_closure(graph, ["P.Mid"], {"P.Leaf"}), 2)
        # A frontier outside the target's closure costs the target nothing.
        self.assertEqual(owned.stale_closure(graph, ["P.Other"], {"P.Leaf"}), 0)
        self.assertIsNone(owned.stale_closure(graph, ["P.Missing"], {"P.Leaf"}))
        # A stale module outside the package is not evidence: a stale
        # dependency is the broad case that must stay `sensitive`.
        self.assertIsNone(owned.stale_closure(graph, ["P.Top"], {"Mathlib.Order"}))

    def test_the_import_graph_survives_a_leading_block_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "P").mkdir()
            (worktree / "P" / "Leaf.lean").write_text("def a := 1\n")
            (worktree / "P" / "Top.lean").write_text(
                "/-\nA copyright header that precedes the imports.\n-/\n"
                "import P.Leaf\n\ndef b := 2\n"
            )
            graph = owned.package_import_graph(worktree, ["P.Top"])
        self.assertEqual(graph["P.Top"], {"P.Leaf"})
        self.assertEqual(owned.stale_closure(graph, ["P.Top"], {"P.Leaf"}), 2)

    # -- B6: the repeat-failure hint -------------------------------------
    def test_repeat_fail_needs_a_recent_previous_failure_of_the_same_targets(self) -> None:
        now = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)
        with _ledger_and_log() as (ledger, _):
            self._measured(ledger, peak_mib=10.0, exit_code=1,
                           time="2026-09-03T01:58:00Z")
            self.assertIn("REPEAT_FAIL", owned.repeat_failure(Path("/w"), ["T"], SETTINGS, now))
        with _ledger_and_log() as (ledger, _):   # previous build succeeded
            self._measured(ledger, peak_mib=10.0, exit_code=0,
                           time="2026-09-03T01:58:00Z")
            self.assertIsNone(owned.repeat_failure(Path("/w"), ["T"], SETTINGS, now))
        with _ledger_and_log() as (ledger, _):   # first failure of these targets
            self._measured(ledger, peak_mib=10.0, exit_code=1, targets=("OTHER",),
                           time="2026-09-03T01:58:00Z")
            self.assertIsNone(owned.repeat_failure(Path("/w"), ["T"], SETTINGS, now))
        with _ledger_and_log() as (ledger, _):   # outside the repeat window
            self._measured(ledger, peak_mib=10.0, exit_code=1,
                           time="2026-09-03T01:30:00Z")
            self.assertIsNone(owned.repeat_failure(Path("/w"), ["T"], SETTINGS, now))

    # -- B7: sanctioned suffix worktrees ---------------------------------
    def test_sanctioned_suffix_worktrees_belong_to_their_goal(self) -> None:
        for suffix in ("control", "mutation", "rehearsal"):
            self.assertEqual(owned.split_worktree_suffix(f"g-{suffix}"), ("g", suffix))
        self.assertEqual(owned.split_worktree_suffix("g-foo"), ("g-foo", None))
        self.assertEqual(owned.split_worktree_suffix("g"), ("g", None))

    @patch("creme.build_ownership.resolve_tool")
    def test_census_is_refused_outside_a_rehearsal_worktree(self, resolve: Mock) -> None:
        output = io.StringIO()
        with patch("creme.build_ownership._worktree_identity",
                   return_value=(Path("/w/.worktrees/g"), "g")):
            self.assertEqual(
                owned.run_lake_build("g", ["T"], census=True, dependency="jaune", stdout=output),
                2,
            )
        self.assertIn("GOAL-rehearsal", output.getvalue().replace("g-rehearsal", "GOAL-rehearsal"))
        resolve.assert_not_called()

    @patch("creme.build_ownership.resolve_tool")
    def test_census_requires_a_named_dependency(self, resolve: Mock) -> None:
        output = io.StringIO()
        with patch("creme.build_ownership._worktree_identity",
                   return_value=(Path("/w/.worktrees/g-rehearsal"), "g")):
            self.assertEqual(owned.run_lake_build("g", ["T"], census=True, stdout=output), 2)
        self.assertIn("--dependency", output.getvalue())
        resolve.assert_not_called()

    def test_a_dependency_that_stops_being_git_pinned_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "lake-manifest.json").write_text(json.dumps({
                "packages": [{"name": "jaune", "type": "path", "dir": "../jaune"}]
            }), encoding="utf-8")
            revision, detail = owned._dependency_revision(worktree, "jaune")
            self.assertIsNone(revision)
            self.assertIn("no longer a Git-pinned package", detail)
            (worktree / "lake-manifest.json").write_text(json.dumps({
                "packages": [{"name": "jaune", "type": "git", "rev": "abc123"}]
            }), encoding="utf-8")
            self.assertEqual(owned._dependency_revision(worktree, "jaune")[0], "abc123")

    def test_rollup_survives_a_missing_semaphore_log(self) -> None:
        with _ledger_and_log() as (_, log):
            log.unlink(missing_ok=True)
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["semaphore_log_status"], "MISSING")

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
                "#!/bin/sh\n"
                'case "$*" in *--no-build*) exit 3;; esac\n'
                f"sleep 30 &\necho $! > {str(child_pid)!r}\nwait\n",
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
    raise SystemExit(owned.run_lake_build('g', ['T'], contention='sensitive', memory_gib=8))
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

    @patch("creme.build_ownership.stale_evidence", return_value=UNPROBED)
    def test_measurement_snapshot_precedes_release_mutation(self, _probe: Mock) -> None:
        events: list[str] = []
        trace = {"M": "first"}
        captured: list[dict] = []

        class FakeProc:
            pid = 424242
            stdout = ["Built M (1s)\n"]

            def wait(self, timeout=None):
                return 0

        class FakeSampler:
            def __init__(self, _pid, worktree=None):
                self.samples = 1
                self.unavailable_samples = 0
                self.peak_rss_mib = 10.0
                self.peak_lean_rss_mib = 8.0
                self.max_concurrent_lean = 1
                self.module_peak_mib = {}

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
        ) as popen, patch("creme.build_ownership.ProcessSampler", FakeSampler), patch(
            "creme.build_ownership.RenewalThread", FakeRenewer
        ), patch("creme.build_ownership._process_group_alive", return_value=False), patch(
            "creme.build_ownership._module_hashes", side_effect=hashes
        ), patch("creme.build_ownership._swap_gib", side_effect=[1.0, 2.0]), patch(
            "creme.build_ownership.append_ledger", side_effect=ledger
        ):
            self.assertEqual(
                owned.run_lake_build(
                    "g", ["T"], contention="sensitive", memory_gib=8, stdout=io.StringIO()
                ),
                0,
            )
        self.assertEqual(popen.call_args.kwargs["env"]["LAKE_CACHE_DIR"], owned.lake_env({})["LAKE_CACHE_DIR"])
        self.assertEqual(events, ["hash", "release", "ledger"])
        self.assertEqual(captured[0]["module_hashes"], {"M": "first"})


class TargetResolutionTest(unittest.TestCase):
    """B5/B7/B8: full and package targets classified from evidence."""

    JAUNE_SHAPED = (
        "import Lake\nopen Lake DSL\n\n"
        "package \u00abpkg\u00bb where\n\n"
        "@[default_target]\nlean_lib \u00abLib\u00bb where\n\n"
        "@[default_target]\nlean_exe \u00abpkg\u00bb where\n  root := `Main\n\n"
        "lean_exe \u00abprobe\u00bb where\n  root := `Probe\n"
    )
    BLANC_SHAPED = (
        "import Lake\nopen Lake DSL\n\n"
        "package \u00abblanc\u00bb where\n\n"
        "@[default_target]\nlean_lib \u00abLib\u00bb where\n"
        "lean_exe \u00abblanc\u00bb where\n  root := `Main\n"
    )

    def worktree(self, lakefile: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "lakefile.lean").write_text(lakefile, encoding="utf-8")
        (root / "Lib").mkdir()
        (root / "Lib.lean").write_text("import Lib.Core\nimport Lib.Top\n", encoding="utf-8")
        (root / "Lib" / "Core.lean").write_text("def c := 1\n", encoding="utf-8")
        (root / "Lib" / "Top.lean").write_text("import Lib.Core\ndef t := 2\n", encoding="utf-8")
        (root / "Main.lean").write_text("import Lib\ndef main := pure ()\n", encoding="utf-8")
        (root / "Probe.lean").write_text("def p := 3\n", encoding="utf-8")
        return root

    def probe(self, frontier: list[str]):
        return SimpleNamespace(
            returncode=3,
            stdout="Some required targets logged failures:\n"
                   + "".join(f"- {name}\n" for name in frontier),
            stderr="",
        )

    # -- resolution -------------------------------------------------------
    def test_a_full_target_resolves_to_the_default_target_roots(self) -> None:
        root = self.worktree(self.JAUNE_SHAPED)
        closure, package, detail = owned.resolve_target_roots(root, [])
        self.assertEqual(closure, ["Lib", "Main"])
        self.assertEqual(package, ["Lib", "Main", "Probe"])
        self.assertIn("full target", detail)

    def test_a_package_shaped_exe_target_resolves_to_its_root(self) -> None:
        root = self.worktree(self.JAUNE_SHAPED)
        closure, _package, detail = owned.resolve_target_roots(root, ["pkg"])
        self.assertEqual(closure, ["Main"])
        self.assertIn("pkg -> ['Main']", detail)

    def test_a_library_target_resolves_to_its_library_roots(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        closure, _package, _detail = owned.resolve_target_roots(root, ["Lib"])
        self.assertEqual(closure, ["Lib"])

    def test_an_import_written_in_guillemets_is_read(self) -> None:
        """Jaune's `Main.lean` writes `import «Jaune».Execution`."""
        root = self.worktree(self.JAUNE_SHAPED)
        (root / "Main.lean").write_text(
            "import \u00abLib\u00bb.Top\ndef main := pure ()\n", encoding="utf-8"
        )
        graph = owned.package_import_graph(root, ["Lib", "Main"])
        self.assertEqual(graph["Main"], {"Lib.Top"})
        # Dropping the import would report a closure of 1 and could widen the
        # class on evidence that is not there.
        self.assertEqual(owned.stale_closure(graph, ["Main"], {"Lib.Core"}), 3)

    def test_a_module_target_is_its_own_root(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        closure, _package, detail = owned.resolve_target_roots(root, ["Lib.Top"])
        self.assertEqual(closure, ["Lib.Top"])
        self.assertIn("(module)", detail)

    # -- B5 positive: a warm full target and a warm package target ---------
    def _row(self, ledger: Path, worktree: Path, targets, peak_mib,
             rebuilt=("Lib", "Lib.Core", "Lib.Top", "Main")):
        _write_ledger(ledger, [("2026-09-03T01:00:00Z", {
            "kind": "build", "goal": "g", "targets": list(targets),
            "command": ["lake", "build"], "exit": 0, "wall_seconds": 3.0,
            "threads": 2, "probe": False, "admission": "ADMITTED_HARD",
            "contention": "sensitive", "modules_rebuilt": list(rebuilt),
            "modules_restored": [], "module_hashes": {}, "module_seconds": {},
            "worktree": str(worktree), "peak_rss_mib": peak_mib,
            "toolchain_digest": "tc", "manifest_digest": "mf",
        })])

    def test_a_warm_full_target_with_a_small_closure_is_tolerant(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, [], 2048.0)
            with patch("creme.build_ownership.subprocess.run",
                       return_value=self.probe(["Lib.Core"])):
                verdict, evidence = owned.classify_contention(
                    root, [], Path("/lake"), SETTINGS, ("tc", "mf")
                )
        self.assertEqual(verdict, "tolerant", evidence)
        self.assertEqual(evidence["resolved_roots"], ["Lib"])
        self.assertEqual(evidence["stale_modules"], 3)

    def test_a_warm_package_target_is_classified_by_its_closure(self) -> None:
        root = self.worktree(self.JAUNE_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, ["pkg"], 2048.0)
            with patch("creme.build_ownership.subprocess.run",
                       return_value=self.probe(["Lib.Top"])):
                verdict, evidence = owned.classify_contention(
                    root, ["pkg"], Path("/lake"), SETTINGS, ("tc", "mf")
                )
        self.assertEqual(verdict, "tolerant", evidence)
        self.assertEqual(evidence["resolved_roots"], ["Main"])
        self.assertEqual(evidence["stale_modules"], 3)   # Top, Lib, Main

    # -- B5 controls ------------------------------------------------------
    def test_an_unreadable_lake_configuration_names_the_failure(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        (root / "lakefile.lean").write_text("import Lake\npackage p where\n", encoding="utf-8")
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, [], 2048.0)
            verdict, evidence = owned.classify_contention(
                root, [], Path("/lake"), SETTINGS, ("tc", "mf")
            )
        self.assertEqual(verdict, "sensitive")
        self.assertIn("roots unresolved", evidence["reason"])
        self.assertIn("no lean_lib or lean_exe target", evidence["reason"])

    def test_a_stale_dependency_in_the_frontier_stays_sensitive(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, [], 2048.0)
            with patch("creme.build_ownership.subprocess.run",
                       return_value=self.probe(["Mathlib.Order.Basic"])):
                verdict, evidence = owned.classify_contention(
                    root, [], Path("/lake"), SETTINGS, ("tc", "mf")
                )
        self.assertEqual(verdict, "sensitive")
        self.assertIn("unmeasured", evidence["reason"])

    def test_a_closure_above_the_limit_stays_sensitive(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, [], 2048.0)
            with patch("creme.build_ownership.subprocess.run",
                       return_value=self.probe(["Lib.Core"])):
                verdict, evidence = owned.classify_contention(
                    root, [], Path("/lake"), SETTINGS,
                    ("tc", "mf"),
                )
                self.assertEqual(verdict, "tolerant")
                narrow = dict(SETTINGS, tolerant_module_count=2)
                verdict, evidence = owned.classify_contention(
                    root, [], Path("/lake"), narrow, ("tc", "mf")
                )
        self.assertEqual(verdict, "sensitive")
        self.assertIn("stale set is 3", evidence["reason"])

    # -- B7: estimates are sized from elaboration --------------------------
    def test_a_zero_rebuild_row_does_not_size_a_build_with_a_stale_set(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, ["Lib.Top"], 645.0, rebuilt=())
            estimate, evidence = owned.derive_memory_gib(
                root, ["Lib.Top"], SETTINGS, ("tc", "mf"), 8, 10
            )
        self.assertEqual(estimate, 8)
        self.assertIn("elaborated a module", evidence["source"])
        self.assertTrue(evidence["keyed_on_elaboration"])

    def test_the_same_row_still_sizes_a_build_with_an_empty_stale_set(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, ["Lib.Top"], 645.0, rebuilt=())
            estimate, evidence = owned.derive_memory_gib(
                root, ["Lib.Top"], SETTINGS, ("tc", "mf"), 8, 0
            )
        self.assertEqual(estimate, 2)
        self.assertFalse(evidence["keyed_on_elaboration"])

    def test_an_unmeasured_stale_set_is_keyed_on_elaboration(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, ["Lib.Top"], 645.0, rebuilt=())
            estimate, evidence = owned.derive_memory_gib(
                root, ["Lib.Top"], SETTINGS, ("tc", "mf"), 8, None
            )
        self.assertEqual(estimate, 8)
        self.assertTrue(evidence["keyed_on_elaboration"])

    def test_a_full_target_estimate_now_uses_its_measured_rows(self) -> None:
        root = self.worktree(self.BLANC_SHAPED)
        with _ledger_and_log() as (ledger, _):
            self._row(ledger, root, [], 2560.0)
            estimate, evidence = owned.derive_memory_gib(
                root, [], SETTINGS, ("tc", "mf"), 8, 2
            )
        self.assertEqual(estimate, 4)
        self.assertEqual(evidence["rows"], 1)


class RowEvidenceTest(unittest.TestCase):
    """B8: reasons, stale detail, hints, and under-cover reach the row."""

    def run_build(self, ledger: Path, *, exit_code: int, rebuilt: list[str],
                  peak_mib: float, memory_gib: int = 4, contention="sensitive",
                  probe: Optional[dict] = None, classify=None):
        captured: list[dict] = []
        output = io.StringIO()
        probe_patch = patch(
            "creme.build_ownership.stale_evidence",
            return_value=UNPROBED if probe is None else probe,
        )
        classify_patch = (
            patch("creme.build_ownership.classify_contention", return_value=classify)
            if classify is not None else contextlib.nullcontext()
        )

        class FakeProc:
            pid = 4321
            stdout = iter([])

            def wait(self, timeout=None):
                return exit_code

        class FakeSampler:
            def __init__(self, _pid, worktree=None):
                self.peak_rss_mib = peak_mib
                self.peak_lean_rss_mib = peak_mib
                self.max_concurrent_lean = 1
                self.module_peak_mib = {module: peak_mib for module in rebuilt}
                self.samples = 4
                self.unavailable_samples = 0

            def start(self):
                pass

            def stop(self):
                pass

        class FakeRenewer:
            def __init__(self, *_args, **_kwargs):
                self.verdicts: list[str] = []
                self.refused = False
                self.cleanup_proved = True

            def start(self):
                pass

            def stop(self):
                pass

        with probe_patch, classify_patch, \
             patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")), \
             patch("creme.build_ownership.resolve_toolchain",
                   return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool"))), \
             patch("creme.build_ownership.semaphore.adaptive_acquire",
                   return_value=(True, "ADMITTED_HARD")), \
             patch("creme.build_ownership.semaphore.adaptive_release",
                   return_value=(True, "released")), \
             patch("creme.build_ownership.guard_bin", return_value=Path("/guard")), \
             patch("creme.build_ownership.subprocess.Popen", return_value=FakeProc()), \
             patch("creme.build_ownership.ProcessSampler", FakeSampler), \
             patch("creme.build_ownership.RenewalThread", FakeRenewer), \
             patch("creme.build_ownership._process_group_alive", return_value=False), \
             patch("creme.build_ownership._module_hashes", return_value={}), \
             patch("creme.build_ownership._parse_build_output",
                   return_value=(list(rebuilt), [], {})), \
             patch("creme.build_ownership._swap_gib", return_value=1.0), \
             patch("creme.build_ownership.append_ledger", side_effect=captured.append):
            code = owned.run_lake_build(
                "g", ["T"], contention=contention, memory_gib=memory_gib, stdout=output
            )
        return code, captured[0], output.getvalue()

    def test_the_row_records_the_estimate_and_its_under_cover(self) -> None:
        with _ledger_and_log() as (ledger, _):
            _code, row, _text = self.run_build(
                ledger, exit_code=0, rebuilt=["A"], peak_mib=2560.0, memory_gib=2
            )
        self.assertEqual(row["estimate_gib"], 2)
        self.assertEqual(row["estimate_under_cover_gib"], 0.5)

    def test_an_estimate_that_covered_the_build_records_zero(self) -> None:
        with _ledger_and_log() as (ledger, _):
            _code, row, _text = self.run_build(
                ledger, exit_code=0, rebuilt=["A"], peak_mib=3642.0, memory_gib=4
            )
        self.assertEqual(row["estimate_under_cover_gib"], 0.0)

    def test_the_restart_instruction_is_its_own_stdout_line(self) -> None:
        with _ledger_and_log() as (ledger, _):
            _code, _row, text = self.run_build(
                ledger, exit_code=0, rebuilt=["A", "B"], peak_mib=100.0
            )
        restart = [line for line in text.splitlines() if line.startswith("restart: ")]
        self.assertEqual(len(restart), 1)
        self.assertIn("rebuilt 2 module(s)", restart[0])

    def test_a_repeat_failure_hint_reaches_stdout_and_the_row(self) -> None:
        with _ledger_and_log() as (ledger, _):
            _write_ledger(ledger, [(_iso_recent(), {
                "kind": "build", "goal": "g", "targets": ["T"],
                "command": ["lake", "build"], "exit": 1, "wall_seconds": 1.0,
                "threads": 2, "probe": False, "admission": "ADMITTED_HARD",
                "contention": "sensitive", "modules_rebuilt": [], "modules_restored": [],
                "module_hashes": {}, "module_seconds": {}, "worktree": str(Path.cwd()),
                "peak_rss_mib": 100.0,
            })])
            _code, row, text = self.run_build(
                ledger, exit_code=1, rebuilt=[], peak_mib=100.0
            )
        hints = [line for line in text.splitlines() if line.startswith("hint: ")]
        self.assertEqual(len(hints), 1)
        self.assertIn("REPEAT_FAIL", hints[0])
        self.assertIn("REPEAT_FAIL", row["hint"])

    def test_no_hint_on_a_first_failure_or_after_a_success(self) -> None:
        with _ledger_and_log() as (ledger, _):
            _code, row, text = self.run_build(
                ledger, exit_code=1, rebuilt=[], peak_mib=100.0
            )
        self.assertNotIn("hint: ", text)
        self.assertNotIn("hint", row)

    def test_the_row_records_the_evidence_reason_stale_set_and_roots(self) -> None:
        with _ledger_and_log() as (ledger, _):
            _write_ledger(ledger, [])
            _code, row, _text = self.run_build(
                ledger, exit_code=0, rebuilt=["A"], peak_mib=100.0,
                contention=None,
                probe={
                    "roots": ["Main"], "package_roots": ["Main"],
                    "resolution": "pkg -> ['Main']", "stale": 12, "detail": "fixture",
                    "stale_set": None, "graph": None,
                },
                classify=("sensitive", {
                    "reason": "stale set is 12 (limit 8)",
                    "stale_modules": 12,
                    "stale_detail": "probe frontier ['Lib.Core']; 12 module(s)",
                    "resolved_roots": ["Main"],
                    "resolution": "pkg -> ['Main']",
                }),
            )
        self.assertEqual(row["evidence_reason"], "stale set is 12 (limit 8)")
        self.assertEqual(row["stale_modules"], 12)
        self.assertEqual(row["resolved_roots"], ["Main"])
        self.assertIn("probe frontier", row["stale_detail"])
        # The row must still satisfy the writer's own schema.
        self.assertTrue(owned._valid_ledger_row({"schema_version": 1, "time": row.get("time", "2026-09-04T00:00:00Z"), **row}))

    def test_an_older_reader_skips_rather_than_misreads_a_new_row(self) -> None:
        """Fixed decision 8: unknown entries are reported, never reset."""
        with _ledger_and_log() as (ledger, _):
            _write_ledger(ledger, [])
            _code, row, _text = self.run_build(
                ledger, exit_code=0, rebuilt=["A"], peak_mib=100.0
            )
        legacy_keys = owned._SAFE_LEDGER_KEYS - {
            "evidence_reason", "resolved_roots", "stale_modules", "stale_detail",
            "estimate_gib", "estimate_under_cover_gib", "hint",
        }
        new_keys = sorted(set(row) - legacy_keys)
        self.assertEqual(new_keys, ["estimate_gib", "estimate_under_cover_gib"])
        # This reader tolerates keys it does not know, so a future additive
        # field cannot make a whole window read as corrupt.
        self.assertTrue(owned._valid_ledger_row({
            "schema_version": 1, "time": "2026-09-04T00:00:00Z",
            **row, "a_field_from_the_future": {"nested": True},
        }))


def _iso_recent() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class QueueRollupTest(unittest.TestCase):
    """B4/B7b/B8c: queue time, reasons, and under-cover as roll-up metrics."""

    BUILD = {
        "kind": "build", "goal": "g", "targets": ["T"], "command": ["lake", "build", "T"],
        "wall_seconds": 1.0, "threads": 2, "probe": False, "admission": "ADMITTED_HARD",
        "modules_rebuilt": [], "modules_restored": [], "module_hashes": {},
        "module_seconds": {}, "worktree": "/a", "exit": 0, "contention": "sensitive",
    }

    def test_queue_time_outcomes_pass_overs_and_free_host_time(self) -> None:
        with _ledger_and_log() as (ledger, log):
            _write_ledger(ledger, [("2026-09-03T01:00:00Z", dict(self.BUILD))])
            _write_log(log, [
                # `big` queues at 01:00 and is passed by `small` twice; a hard
                # hold covers only the first five minutes of its wait.
                ("2026-09-03T01:00:00Z", "wait-enqueue", "big", "OK", "position=1"),
                ("2026-09-03T01:00:10Z", "adaptive-acquire", "holder", "OK", "ADMITTED_HARD: in"),
                ("2026-09-03T01:02:00Z", "wait-enqueue", "small", "OK", "position=2"),
                ("2026-09-03T01:05:10Z", "adaptive-release", "holder", "OK", "hard hold released"),
                ("2026-09-03T01:06:00Z", "wait-acquire", "small", "OK", "ADMITTED_SOFT: waited=240s"),
                ("2026-09-03T01:08:00Z", "adaptive-acquire", "small", "OK", "ADMITTED_SOFT: again"),
                ("2026-09-03T01:10:00Z", "wait-acquire", "big", "REFUSED",
                 "WAIT_TIMEOUT: no admission within 600s; dominant verdict LIGHT_ONLY"),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        big = report["by_goal"].get("big") or {}
        self.assertEqual(big["queue_episodes"], 1)
        self.assertEqual(big["queue_outcomes"], {"WAIT_TIMEOUT": 1})
        self.assertEqual(big["queue_seconds_total"], 600.0)
        self.assertEqual(big["queue_seconds_longest"], 600.0)
        self.assertEqual(big["queue_seconds_no_hard_hold"], 300.0)
        self.assertEqual(big["admissions_of_other_labels_while_queued"], 3)
        self.assertEqual(
            big["admissions_of_other_labels_by_label"], {"holder": 1, "small": 2}
        )

    def test_an_enqueue_with_no_outcome_is_unresolved_not_zero(self) -> None:
        with _ledger_and_log() as (_ledger, log):
            _write_log(log, [
                ("2026-09-03T01:00:00Z", "wait-enqueue", "g", "OK", "position=1"),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T01:30:00Z")
        goal = report["by_goal"]["g"]
        self.assertEqual(goal["queue_outcomes"], {"UNRESOLVED": 1})
        self.assertEqual(goal["queue_seconds_total"], 1800.0)

    def test_a_cancelled_and_a_dropped_wait_are_their_own_outcomes(self) -> None:
        with _ledger_and_log() as (_ledger, log):
            _write_log(log, [
                ("2026-09-03T01:00:00Z", "wait-enqueue", "g", "OK", "position=1"),
                ("2026-09-03T01:02:00Z", "wait-acquire", "g", "REFUSED",
                 "WAIT_CANCELLED: the waiting process ended before the queue decided"),
                ("2026-09-03T01:03:00Z", "wait-enqueue", "h", "OK", "position=1"),
                ("2026-09-03T01:04:00Z", "wait-dropped", "h", "REFUSED",
                 "WAIT_DROPPED: the waiting process is gone"),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        self.assertEqual(
            report["by_goal"]["g"]["queue_outcomes"], {"WAIT_CANCELLED": 1}
        )
        self.assertEqual(
            report["by_goal"]["h"]["queue_outcomes"], {"WAIT_DROPPED": 1}
        )

    def test_two_goals_with_no_queue_report_zeros(self) -> None:
        with _ledger_and_log() as (ledger, log):
            _write_ledger(ledger, [
                ("2026-09-03T01:00:00Z", dict(self.BUILD)),
                ("2026-09-03T01:05:00Z", {**self.BUILD, "goal": "h"}),
            ])
            log.write_text("", encoding="utf-8")
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        for goal in ("g", "h"):
            record = report["by_goal"][goal]
            self.assertEqual(record["queue_episodes"], 0)
            self.assertEqual(record["queue_outcomes"], {})
            self.assertEqual(record["queue_seconds_total"], 0.0)
            self.assertEqual(record["admissions_of_other_labels_while_queued"], 0)

    def test_the_reason_histogram_reads_the_row_not_a_transcript(self) -> None:
        with _ledger_and_log() as (ledger, _log):
            _write_ledger(ledger, [
                ("2026-09-03T01:00:00Z", {
                    **self.BUILD, "evidence_reason": "roots unresolved: lakefile.lean is unreadable",
                }),
                ("2026-09-03T01:01:00Z", {
                    **self.BUILD, "evidence_reason": "stale set is unmeasured (limit 8)",
                }),
                ("2026-09-03T01:02:00Z", {
                    **self.BUILD, "evidence_reason": "stale set is 12 (limit 8)",
                }),
                ("2026-09-03T01:03:00Z", {
                    **self.BUILD,
                    "evidence_reason": "measured peak 5.00 GiB is not below 4 GiB",
                }),
                ("2026-09-03T01:04:00Z", dict(self.BUILD)),
                ("2026-09-03T01:05:00Z", {
                    **self.BUILD, "requested_contention": "sensitive",
                }),
                ("2026-09-03T01:06:00Z", {**self.BUILD, "contention": "tolerant"}),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        self.assertEqual(report["by_goal"]["g"]["sensitive_reasons"], {
            "explicit class (no classification ran)": 1,
            "measured peak too high": 1,
            "roots unresolved": 1,
            "stale set above the limit": 1,
            "unmeasured stale set": 1,
            "unrecorded (row predates evidence_reason)": 1,
        })

    def test_under_cover_uses_the_estimate_the_row_carried(self) -> None:
        with _ledger_and_log() as (ledger, _log):
            _write_ledger(ledger, [
                # A pre-update row carries only `memory_gib`.
                ("2026-09-03T01:00:00Z", {
                    **self.BUILD, "memory_gib": 2, "peak_rss_mib": 2488.0,
                }),
                # A post-update row carries `estimate_gib` as well.
                ("2026-09-03T01:01:00Z", {
                    **self.BUILD, "memory_gib": 4, "estimate_gib": 4,
                    "peak_rss_mib": 3642.0,
                }),
                # A row with no peak cannot be measured either way.
                ("2026-09-03T01:02:00Z", {**self.BUILD, "memory_gib": 4}),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        under = report["by_goal"]["g"]["estimate_under_cover"]
        self.assertEqual(under["rows_with_an_estimate_and_a_peak"], 2)
        self.assertEqual(under["under_covered_rows"], 1)
        self.assertEqual(under["worst_under_cover_gib"], 0.43)
        self.assertEqual(under["cases"][0]["estimate_gib"], 2)
        self.assertEqual(under["cases"][0]["peak_gib"], 2.43)

    def test_the_estimate_source_histogram_separates_full_target_defaults(self) -> None:
        with _ledger_and_log() as (ledger, _log):
            _write_ledger(ledger, [
                ("2026-09-03T01:00:00Z", {
                    **self.BUILD, "estimate_source": "profile default (full target)",
                }),
                ("2026-09-03T01:01:00Z", {
                    **self.BUILD,
                    "estimate_source": "profile default (no successful measurement "
                                       "for these targets on the pinned inputs)",
                }),
                ("2026-09-03T01:02:00Z", {
                    **self.BUILD,
                    "estimate_source": "max of 2 measured peak(s) (2.00 GiB) plus 1 GiB",
                }),
            ])
            report = owned.ledger_rollup("2026-09-03", "2026-09-03T02:00:00Z")
        self.assertEqual(report["by_goal"]["g"]["estimate_sources"], {
            "measured (target rows)": 1,
            "profile default (full target)": 1,
            "profile default (no measurement)": 1,
        })


if __name__ == "__main__":
    unittest.main()
