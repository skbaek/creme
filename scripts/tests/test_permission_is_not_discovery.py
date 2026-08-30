"""Self-check the isolated permission-is-not-discovery fixture.

This does not claim to emulate either client. It proves that the acceptance
fixture has separate Git roots, that the granted sibling is readable, and that
root-to-CWD discovery candidates exclude a readable sibling. Live client
acceptance remains mandatory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


CONTROL_CANARY = "CONTROL_ROOT_CANARY"
READ_CANARY = "READ_ACCESS_CANARY"
FORBIDDEN_CANARY = "FORBIDDEN_INSTRUCTION_CANARY"
FORBIDDEN_SKILL = "forbidden-sibling-skill"
FORBIDDEN_MCP = "forbidden-sibling-mcp"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git_marker(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--quiet", str(root)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _root_to_cwd(start: Path) -> tuple[Path, ...]:
    root = _project_root(start)
    resolved = start.resolve()
    relative = resolved.relative_to(root)
    descendants = (
        root / Path(*relative.parts[:index])
        for index in range(1, len(relative.parts) + 1)
    )
    return (root, *descendants)


def _instruction_candidates(start: Path, names: tuple[str, ...]) -> tuple[Path, ...]:
    return tuple(
        directory / name
        for directory in _root_to_cwd(start)
        for name in names
        if (directory / name).is_file()
    )


def _project_candidates(start: Path, relative_paths: tuple[str, ...]) -> tuple[Path, ...]:
    return tuple(
        directory / relative_path
        for directory in _root_to_cwd(start)
        for relative_path in relative_paths
        if (directory / relative_path).exists()
    )


class IsolatedFixture:
    def __init__(self, base: Path) -> None:
        self.base = base.resolve()
        self.control = self.base / "control-root"
        self.granted = self.base / "granted-only"
        self.codex_home = self.base / "client-state/codex"
        self.claude_config = self.base / "client-state/claude"

    def create(self) -> None:
        _git_marker(self.control)
        _git_marker(self.granted)

        _write(self.control / "AGENTS.md", CONTROL_CANARY + "\n")
        _write(self.control / "CLAUDE.md", CONTROL_CANARY + "\n")

        _write(self.granted / "ordinary.txt", READ_CANARY + "\n")
        _write(self.granted / "AGENTS.md", FORBIDDEN_CANARY + "\n")
        _write(self.granted / "CLAUDE.md", FORBIDDEN_CANARY + "\n")
        _write(
            self.granted / f".agents/skills/{FORBIDDEN_SKILL}/SKILL.md",
            f"---\nname: {FORBIDDEN_SKILL}\ndescription: fixture\n---\n",
        )
        _write(
            self.granted / f".claude/skills/{FORBIDDEN_SKILL}/SKILL.md",
            f"---\nname: {FORBIDDEN_SKILL}\ndescription: fixture\n---\n",
        )
        _write(
            self.granted / ".codex/config.toml",
            f'[mcp_servers.{FORBIDDEN_MCP}]\ncommand = "false"\n',
        )
        _write(
            self.granted / ".mcp.json",
            json.dumps(
                {"mcpServers": {FORBIDDEN_MCP: {"command": "false"}}},
                indent=2,
            )
            + "\n",
        )

        codex_config = (
            'default_permissions = "fixture-relay"\n\n'
            f'[projects."{self.control}"]\ntrust_level = "trusted"\n\n'
            "[permissions.fixture-relay]\nextends = \":workspace\"\n\n"
            "[permissions.fixture-relay.workspace_roots]\n"
            f'"{self.control}" = true\n'
            f'"{self.granted}" = true\n'
        )
        _write(self.codex_home / "config.toml", codex_config)

        claude_settings = {
            "permissions": {"additionalDirectories": [str(self.granted)]}
        }
        _write(
            self.claude_config / "settings.json",
            json.dumps(claude_settings, indent=2) + "\n",
        )


class PermissionIsNotDiscoveryFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="creme-client-discovery-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = IsolatedFixture(Path(self.temp.name))
        self.fixture.create()

    def test_temp_config_roots_do_not_touch_real_client_homes(self) -> None:
        home = Path.home().resolve()
        self.assertNotEqual(self.fixture.codex_home, home / ".codex")
        self.assertNotEqual(self.fixture.claude_config, home / ".claude")
        self.assertTrue(self.fixture.codex_home.is_relative_to(self.fixture.base))
        self.assertTrue(self.fixture.claude_config.is_relative_to(self.fixture.base))

    def test_codex_fixture_grants_access_without_adding_discovery_ancestor(self) -> None:
        config = (self.fixture.codex_home / "config.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            f'"{self.fixture.granted}" = true',
            config,
        )
        self.assertEqual(
            (self.fixture.granted / "ordinary.txt").read_text(encoding="utf-8").strip(),
            READ_CANARY,
        )
        chain = _root_to_cwd(self.fixture.control)
        self.assertIn(self.fixture.control, chain)
        self.assertNotIn(self.fixture.granted, chain)
        discovered = _instruction_candidates(
            self.fixture.control, ("AGENTS.override.md", "AGENTS.md")
        )
        self.assertEqual(discovered, (self.fixture.control / "AGENTS.md",))

    def test_claude_fixture_uses_access_setting_not_discovery_flags(self) -> None:
        settings = json.loads(
            (self.fixture.claude_config / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            settings["permissions"]["additionalDirectories"],
            [str(self.fixture.granted)],
        )
        serialized = json.dumps(settings)
        self.assertNotIn("--add-dir", serialized)
        self.assertNotIn("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", serialized)
        discovered = _instruction_candidates(
            self.fixture.control, ("CLAUDE.md", ".claude/CLAUDE.md")
        )
        self.assertEqual(discovered, (self.fixture.control / "CLAUDE.md",))

    def test_negative_and_positive_fixture_canaries_are_distinguishable(self) -> None:
        negative_files = set(
            _instruction_candidates(self.fixture.control, ("AGENTS.md", "CLAUDE.md"))
        )
        positive_files = set(
            _instruction_candidates(self.fixture.granted, ("AGENTS.md", "CLAUDE.md"))
        )
        negative_text = "\n".join(
            path.read_text(encoding="utf-8") for path in negative_files
        )
        positive_text = "\n".join(
            path.read_text(encoding="utf-8") for path in positive_files
        )
        self.assertIn(CONTROL_CANARY, negative_text)
        self.assertNotIn(FORBIDDEN_CANARY, negative_text)
        self.assertIn(FORBIDDEN_CANARY, positive_text)

        negative_skills = _project_candidates(
            self.fixture.control,
            (
                f".agents/skills/{FORBIDDEN_SKILL}/SKILL.md",
                f".claude/skills/{FORBIDDEN_SKILL}/SKILL.md",
            ),
        )
        positive_skills = _project_candidates(
            self.fixture.granted,
            (
                f".agents/skills/{FORBIDDEN_SKILL}/SKILL.md",
                f".claude/skills/{FORBIDDEN_SKILL}/SKILL.md",
            ),
        )
        negative_mcp = _project_candidates(
            self.fixture.control, (".codex/config.toml", ".mcp.json")
        )
        positive_mcp = _project_candidates(
            self.fixture.granted, (".codex/config.toml", ".mcp.json")
        )
        self.assertEqual(negative_skills, ())
        self.assertEqual(negative_mcp, ())
        self.assertEqual(len(positive_skills), 2)
        self.assertEqual(len(positive_mcp), 2)

    def test_fixture_creation_does_not_change_process_environment(self) -> None:
        before = dict(os.environ)
        with tempfile.TemporaryDirectory(prefix="creme-client-discovery-env-") as directory:
            IsolatedFixture(Path(directory)).create()
        self.assertEqual(os.environ, before)


if __name__ == "__main__":
    unittest.main()
