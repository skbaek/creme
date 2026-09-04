from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from creme.cli import cmd_host_wrappers
from creme.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, check_host_wrappers
from creme.host_build_broker import BROKER_NAME, PREFLIGHT_RELATIVE
from creme.host_wrappers import (
    RULES_FILENAME,
    WRAPPER_COMMANDS,
    bundle_install_issues,
    install_host_bundle,
    render_host_rules,
    render_host_wrappers,
)


class HostWrappersTest(unittest.TestCase):
    def _layout(self, temporary: Path) -> tuple[Path, Path, Path]:
        home = temporary / "codex"
        return temporary / "workspace" / "creme", home / "bin", home / "rules"

    def test_wrapper_set_excludes_the_client_neutral_semaphore(self) -> None:
        self.assertEqual(
            WRAPPER_COMMANDS,
            (
                ("codex-host-telemetry", "telemetry"),
                ("codex-reclaim-lean", "reclaim"),
            ),
        )

    def test_rendered_wrappers_reject_unapproved_argument_shapes(self) -> None:
        root = Path("/portable/work space/quo'ted/creme")
        rendered = render_host_wrappers(root)
        telemetry = rendered["codex-host-telemetry"]
        reclaim = rendered["codex-reclaim-lean"]
        self.assertIn("accepts no arguments", telemetry)
        self.assertIn("telemetry", telemetry)
        self.assertIn("use exactly --dry-run or --wind-down GOAL", reclaim)
        self.assertIn("invalid goal label", reclaim)
        self.assertNotIn("--hard-pressure)", reclaim)

    def test_rules_are_dedicated_narrow_and_self_testing(self) -> None:
        output = Path("/portable/client/bin")
        rules = render_host_rules(output)
        self.assertEqual(rules.count("prefix_rule("), 3)
        self.assertIn('pattern=["/portable/client/bin/codex-host-telemetry"]', rules)
        self.assertIn(
            'pattern=["/portable/client/bin/codex-reclaim-lean", "--dry-run"]', rules,
        )
        self.assertIn(
            'pattern=["/portable/client/bin/codex-reclaim-lean", "--wind-down"]', rules,
        )
        self.assertNotIn(
            'pattern=["/portable/client/bin/codex-reclaim-lean"]', rules,
        )
        self.assertIn("match=[", rules)
        self.assertIn("not_match=[", rules)
        self.assertIn("Restart Codex", rules)

    def test_optional_build_broker_is_runtime_and_preflight_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "creme"
            preflight = root / PREFLIGHT_RELATIVE
            preflight.parent.mkdir(parents=True)
            preflight.write_text("#!/usr/bin/python3\nraise SystemExit(99)\n", encoding="utf-8")
            preflight.chmod(0o700)
            launcher = root / "scripts" / "creme"
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
            package = root / "creme" / "__init__.py"
            package.parent.mkdir()
            package.write_text("", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)

            rendered = render_host_wrappers(root)
            self.assertIn(BROKER_NAME, rendered)
            broker = rendered[BROKER_NAME]
            runtime_tree = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD:creme"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertIn(runtime_tree, broker)
            self.assertIn("EXPECTED_LAUNCHER_ENTRY", broker)
            self.assertIn("EXPECTED_PREFLIGHT_SHA256", broker)
            self.assertIn("MemorySwapMax", broker)
            self.assertIn("unsupported broker option", broker)

            rules = render_host_rules(Path("/portable/bin"), include_build=True)
            self.assertEqual(rules.count("prefix_rule("), 4)
            self.assertIn(f'pattern=["/portable/bin/{BROKER_NAME}"]', rules)

    def test_build_broker_rejects_malformed_argv_before_host_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "creme"
            preflight = root / PREFLIGHT_RELATIVE
            preflight.parent.mkdir(parents=True)
            preflight.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            preflight.chmod(0o700)
            launcher = root / "scripts" / "creme"
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
            package = root / "creme" / "__init__.py"
            package.parent.mkdir()
            package.write_text("", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            broker = Path(tmp) / BROKER_NAME
            broker.write_text(render_host_wrappers(root)[BROKER_NAME], encoding="utf-8")
            broker.chmod(0o700)

            bad = (
                [],
                ["other", "goal", "--"],
                ["blanc", "../goal", "--"],
                ["blanc", "goal", "--repo", "/tmp/x", "--"],
                ["blanc", "goal", "--memory-gib", "1", "--"],
                ["blanc", "goal", "--", "../Target"],
                ["blanc", "goal", "--probe", "--wait", "5", "--", "Blanc.X"],
            )
            for arguments in bad:
                completed = subprocess.run(
                    [str(broker), *arguments], capture_output=True, text=True, check=False,
                )
                self.assertEqual(completed.returncode, 2, arguments)
                self.assertIn("REFUSED", completed.stderr)

    def test_doctor_detects_broker_preflight_and_revision_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root, output, rules = self._layout(temporary)
            preflight = root / PREFLIGHT_RELATIVE
            preflight.parent.mkdir(parents=True)
            preflight.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            preflight.chmod(0o700)
            launcher = root / "scripts" / "creme"
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
            package = root / "creme" / "__init__.py"
            package.parent.mkdir()
            package.write_text("", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            with patch.dict(os.environ, {"CODEX_HOME": str(temporary / "codex")}):
                written = install_host_bundle(root, output, rules, replace=False)
            self.assertIn(output / BROKER_NAME, written)
            self.assertEqual(check_host_wrappers(root, output, rules)[0].status, STATUS_OK)

            preflight.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
            check = check_host_wrappers(root, output, rules)[0]
            self.assertEqual(check.status, STATUS_FAIL)
            self.assertIn("content mismatch", check.detail)

    def test_write_is_private_complete_and_rule_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root, output, rules = self._layout(temporary)
            with patch.dict(os.environ, {"CODEX_HOME": str(temporary / "codex")}):
                written = install_host_bundle(root, output, rules, replace=False)
            self.assertEqual(
                written,
                [
                    output / "codex-host-telemetry",
                    output / "codex-reclaim-lean",
                    rules / RULES_FILENAME,
                ],
            )
            self.assertEqual(stat.S_IMODE(written[0].stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(written[1].stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(written[2].stat().st_mode), 0o600)
            self.assertEqual(list(output.glob(".*.tmp.*")), [])
            self.assertEqual(list(rules.glob(".*.tmp.*")), [])

    def test_existing_bundle_requires_reviewed_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root, output, rules = self._layout(temporary)
            with patch.dict(os.environ, {"CODEX_HOME": str(temporary / "codex")}):
                install_host_bundle(root, output, rules, replace=False)
                with self.assertRaises(FileExistsError):
                    install_host_bundle(root, output, rules, replace=False)
                install_host_bundle(root, output, rules, replace=True)
            self.assertEqual(bundle_install_issues(root, output, rules), [])

    def test_install_refuses_non_codex_and_linked_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root, output, rules = self._layout(temporary)
            with patch.dict(os.environ, {"CODEX_HOME": str(temporary / "codex")}):
                with self.assertRaises(ValueError):
                    install_host_bundle(
                        root, temporary / "outside" / "bin", rules, replace=False,
                    )
                (temporary / "codex").mkdir()
                target = temporary / "real-bin"
                target.mkdir()
                os.symlink(target, output)
                with self.assertRaises((ValueError, NotADirectoryError)):
                    install_host_bundle(root, output, rules, replace=False)

    def test_cli_previews_both_and_write_requires_both_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            _, output, rules = self._layout(temporary)
            preview = SimpleNamespace(
                output_dir=str(output), rules_dir=str(rules), write=False, replace=False,
            )
            with patch("creme.cli._json") as emit:
                self.assertEqual(cmd_host_wrappers(preview), 0)
            payload = emit.call_args.args[0]
            self.assertEqual(payload["status"], "PREVIEW")
            wrapper_names = {Path(path).name for path in payload["wrappers"]}
            self.assertTrue({
                "codex-host-telemetry", "codex-reclaim-lean",
            }.issubset(wrapper_names))
            self.assertEqual(len(payload["rules"]), 1)
            self.assertIs(payload["rules_changed_by_install"], True)
            self.assertFalse(output.exists())
            self.assertFalse(rules.exists())

            for output_arg, rules_arg in ((None, str(rules)), (str(output), None)):
                request = SimpleNamespace(
                    output_dir=output_arg,
                    rules_dir=rules_arg,
                    write=True,
                    replace=False,
                )
                with patch("creme.cli._json") as emit:
                    self.assertEqual(cmd_host_wrappers(request), 1)
                self.assertIn("both required", emit.call_args.args[0]["detail"])

    def test_doctor_warns_only_when_the_whole_bundle_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root, output, rules = self._layout(temporary)
            checks = check_host_wrappers(root, output, rules)
            self.assertEqual(checks[0].status, STATUS_WARN)

    def test_doctor_rejects_partial_stale_linked_and_permissive_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root, output, rules = self._layout(temporary)
            with patch.dict(os.environ, {"CODEX_HOME": str(temporary / "codex")}):
                install_host_bundle(root, output, rules, replace=False)

            rule = rules / RULES_FILENAME
            rule.unlink()
            check = check_host_wrappers(root, output, rules)[0]
            self.assertEqual(check.status, STATUS_FAIL)
            self.assertIn("missing", check.detail)

            with patch.dict(os.environ, {"CODEX_HOME": str(temporary / "codex")}):
                install_host_bundle(root, output, rules, replace=True)
            rule.chmod(0o644)
            check = check_host_wrappers(root, output, rules)[0]
            self.assertEqual(check.status, STATUS_FAIL)
            self.assertIn("expected 0600", check.detail)

            rule.unlink()
            os.symlink(output / "codex-host-telemetry", rule)
            check = check_host_wrappers(root, output, rules)[0]
            self.assertEqual(check.status, STATUS_FAIL)
            self.assertIn("symbolic link", check.detail)

            rule.unlink()
            rule.write_text("stale\n", encoding="utf-8")
            rule.chmod(0o600)
            check = check_host_wrappers(root, output, rules)[0]
            self.assertEqual(check.status, STATUS_FAIL)
            self.assertIn("content mismatch", check.detail)

    def test_doctor_accepts_exact_bundle_without_overclaiming_runtime_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root, output, rules = self._layout(temporary)
            with patch.dict(os.environ, {"CODEX_HOME": str(temporary / "codex")}):
                install_host_bundle(root, output, rules, replace=False)
            check = check_host_wrappers(root, output, rules)[0]
            self.assertEqual(check.status, STATUS_OK)
            self.assertIn("restart Codex", check.detail)
            self.assertIn("managed requirements", check.detail)

    def test_execpolicy_accepts_only_the_intended_prefixes(self) -> None:
        executable = Path("/usr/lib/chatgpt/resources/codex")
        if not executable.is_file():
            self.skipTest("local Codex execpolicy checker is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            rule = temporary / RULES_FILENAME
            output = Path("/portable/client/bin")
            rule.write_text(
                render_host_rules(output, include_build=True), encoding="utf-8",
            )

            def decision(*command: str) -> str | None:
                completed = subprocess.run(
                    [str(executable), "execpolicy", "check", "--rules", str(rule), "--", *command],
                    capture_output=True, text=True, check=True,
                )
                return json.loads(completed.stdout).get("decision")

            self.assertEqual(decision(str(output / "codex-host-telemetry")), "allow")
            self.assertEqual(decision(str(output / "codex-reclaim-lean"), "--dry-run"), "allow")
            self.assertEqual(
                decision(str(output / "codex-reclaim-lean"), "--wind-down", "goal"),
                "allow",
            )
            self.assertIsNone(decision(str(output / "codex-reclaim-lean")))
            self.assertIsNone(decision(str(output / "codex-reclaim-lean"), "--hard-pressure"))
            self.assertEqual(
                decision(
                    str(output / BROKER_NAME),
                    "blanc", "drip-etude-v1", "--probe", "--", "Blanc.DripFresh",
                ),
                "allow",
            )
            self.assertEqual(
                decision(
                    str(output / BROKER_NAME), "blanc", "goal", "--repo", "/tmp/x", "--",
                ),
                "allow",
            )
            # The prefix authorizes the stable broker; its own parser is the
            # second boundary and rejects the malformed invocation above.
            self.assertIsNone(decision("/usr/bin/systemd-run", "--user", "true"))
            self.assertIsNone(decision("bash", "/tmp/drip-contained-build.sh"))


if __name__ == "__main__":
    unittest.main()
