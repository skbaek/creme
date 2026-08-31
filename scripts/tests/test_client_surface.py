"""Static checks for Creme's committed, public client surface.

These tests parse files only. They deliberately do not launch a client, start
MCP, inspect a user's home directory, or mutate trust/approval state.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import stat
import tempfile
from types import SimpleNamespace
import unittest

from creme.cli import cmd_client_profile, render_codex_profile


ROOT = Path(__file__).resolve().parents[2]
PINNED_MCP = "lean-lsp-mcp==0.26.1"
SKILLS = ("lean-inspector", "lean-prover")


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class ClientSurfaceTest(unittest.TestCase):
    def test_canonical_instruction_shim(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        normalized_agents = " ".join(agents.split())
        self.assertIn("Creme is the public launch root", agents)
        self.assertIn("does not load these instructions", normalized_agents)
        self.assertLess(len(agents.encode("utf-8")), 32 * 1024)
        self.assertEqual(
            (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip(),
            "@AGENTS.md",
        )

    def test_codex_config_is_project_scoped_and_pinned(self) -> None:
        text = (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
        sections = re.findall(r"^\[([^]]+)]\s*$", text, flags=re.MULTILINE)
        self.assertTrue(sections)
        self.assertTrue(
            all(section.startswith("mcp_servers.lean-lsp-mcp") for section in sections)
        )
        self.assertRegex(text, r'(?m)^command\s*=\s*"uvx"\s*$')
        self.assertRegex(
            text, rf'(?m)^args\s*=\s*\["{re.escape(PINNED_MCP)}"\]\s*$'
        )
        self.assertRegex(text, r"(?m)^required\s*=\s*true\s*$")
        self.assertRegex(
            text,
            r'(?m)^default_tools_approval_mode\s*=\s*"writes"\s*$',
        )
        self.assertNotRegex(text, r"(?m)^\[permissions(?:\.|])")
        self.assertNotRegex(text, r"(?m)^\[projects(?:\.|])")

    def test_claude_settings_grant_only_relative_sibling_access(self) -> None:
        settings = _json(".claude/settings.json")
        self.assertEqual(set(settings), {"$schema", "permissions"})
        permissions = settings["permissions"]
        self.assertEqual(set(permissions), {"additionalDirectories"})
        self.assertEqual(
            permissions["additionalDirectories"], ["../jaune/", "../blanc/"]
        )
        for path in permissions["additionalDirectories"]:
            self.assertFalse(Path(path).is_absolute())
        self.assertNotIn("enableAllProjectMcpServers", settings)
        self.assertNotIn("allow", permissions)

    def test_mcp_surfaces_have_one_matching_pinned_server(self) -> None:
        claude = _json(".mcp.json")
        antigravity = _json(".agents/mcp_config.json")
        self.assertEqual(set(claude["mcpServers"]), {"lean-lsp-mcp"})
        self.assertEqual(set(antigravity["mcpServers"]), {"lean-lsp-mcp"})
        claude_server = claude["mcpServers"]["lean-lsp-mcp"]
        antigravity_server = antigravity["mcpServers"]["lean-lsp-mcp"]
        self.assertEqual(claude_server.get("type", "stdio"), "stdio")
        self.assertEqual(
            {key: value for key, value in claude_server.items() if key != "type"},
            antigravity_server,
        )
        self.assertEqual(claude_server["command"], "uvx")
        self.assertEqual(claude_server["args"], [PINNED_MCP])

        codex = (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
        codex_descriptions = re.search(
            r"(?ms)^LEAN_MCP_TOOL_DESCRIPTIONS\s*=\s*'''(.*?)'''\s*$",
            codex,
        )
        self.assertIsNotNone(codex_descriptions)
        self.assertEqual(
            json.loads(codex_descriptions.group(1)),
            json.loads(claude_server["env"]["LEAN_MCP_TOOL_DESCRIPTIONS"]),
        )

    def test_claude_skills_are_documented_per_skill_symlinks(self) -> None:
        for skill in SKILLS:
            canonical = ROOT / ".agents/skills" / skill
            skill_file = canonical / "SKILL.md"
            shim = ROOT / ".claude/skills" / skill
            with self.subTest(skill=skill):
                self.assertTrue(skill_file.is_file(), skill_file)
                self.assertTrue(shim.is_symlink(), shim)
                self.assertEqual(shim.resolve(), canonical.resolve())

    def test_machine_readable_surface_has_no_host_paths_or_credentials(self) -> None:
        files = (
            ROOT / ".codex/config.toml",
            ROOT / ".claude/settings.json",
            ROOT / ".mcp.json",
            ROOT / ".agents/mcp_config.json",
        )
        forbidden = (
            re.compile("/" + "Users" + "/"),
            re.compile(r"/home/[^/]+/"),
            re.compile(r"~/(?:\.codex|\.claude)/"),
            re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|secret)\s*[:=]"),
        )
        for path in files:
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                with self.subTest(path=path.relative_to(ROOT), pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_antigravity_is_retained_but_not_acceptance_supported(self) -> None:
        discovery = (ROOT / "docs/client-discovery.md").read_text(encoding="utf-8")
        acceptance = (ROOT / "acceptance/client-discovery.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("experimental and not a v0.1 acceptance-supported client", discovery)
        self.assertIn("retained experimental compatibility", acceptance)

    def test_first_machine_setup_is_public_and_self_contained(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        setup = (ROOT / "docs/setup.md").read_text(encoding="utf-8")
        self.assertIn("docs/setup.md", readme)
        for repository in ("creme", "jaune", "blanc"):
            self.assertIn(
                f"https://github.com/skbaek/{repository}.git",
                setup,
            )
        for command in (
            "python3 -m creme init",
            "python3 -m creme validate-profile",
            "python3 -m creme doctor",
            "lake build",
        ):
            self.assertIn(command, setup)
        for authority in (
            "guides/goal.md",
            "guides/execution.md",
            "jaune/blob/main/scripts/GATES.md",
            "blanc/blob/main/scripts/GATES.md",
        ):
            self.assertIn(authority, setup)
        for desktop_project_step in (
            "Edit project",
            "Add folder",
            "Make primary",
            "secondary folders",
            "https://learn.chatgpt.com/docs/projects",
        ):
            self.assertIn(desktop_project_step, setup)
        self.assertIn("project whose primary folder is Jaune or Blanc", setup)
        self.assertIn("public-only acceptance run", setup)
        self.assertIn("Plans is not the method authority", setup)
        self.assertNotIn("~/elanc", setup)
        self.assertNotIn("/" + "Users" + "/", setup)
        self.assertNotRegex(setup, r"/home/[^/]+/")

    def test_goal_guide_keeps_model_effort_contract(self) -> None:
        guide = (ROOT / "docs/guides/goal.md").read_text(encoding="utf-8")
        for required_concept in (
            "Recommended lead configuration",
            "required but advisory",
            "The six-selector model",
            "The intelligence ceiling",
            "not a sixth intelligence rung",
            "automatic multi-agent orchestration",
            "Ultra: Max reasoning with automatic task delegation",
            "recheck its effective lead effort at launch",
            "client-visible model name",
            "https://learn.chatgpt.com/docs/models",
            "https://developers.openai.com/api/docs/guides/latest-model",
        ):
            self.assertIn(required_concept, guide)
        self.assertNotIn(
            "Extra-High-class lead reasoning plus automatic", guide
        )
        for model in ("Sol", "Terra", "Luna", "Fable", "Opus"):
            self.assertIn(model, guide)
        for selector in (
            "Light",
            "Medium",
            "High",
            "Extra High",
            "Max",
            "Ultra",
        ):
            self.assertIn(selector, guide)

    def test_lean_task_wind_down_is_part_of_the_execution_contract(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        execution = (ROOT / "docs/guides/execution.md").read_text(encoding="utf-8")
        lean_loops = (ROOT / "docs/guides/lean-edit-loops.md").read_text(
            encoding="utf-8"
        )
        for text in (agents, execution, lean_loops):
            self.assertIn("reclaim --wind-down GOAL", text)
        self.assertIn("before yielding to a requested pause or restart", agents)
        self.assertIn("not wind-down evidence", agents)
        normalized_execution = " ".join(execution.split())
        self.assertIn("only then", normalized_execution)
        self.assertIn("leaves the matching hold intact", normalized_execution)

    def test_generated_codex_profile_escapes_legal_hostile_paths(self) -> None:
        workspace = Path('/tmp/work"space\\line\nnext')
        rendered = render_codex_profile(workspace)
        self.assertIn(json.dumps(str(workspace / "creme")), rendered)
        self.assertNotIn(str(workspace / "creme"), rendered)
        self.assertIn('\\"', rendered)
        self.assertIn('\\\\', rendered)
        self.assertNotIn("\nnext", rendered)

    def test_client_profile_write_is_private_atomic_and_leaves_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "client" / "creme.config.toml"
            arguments = SimpleNamespace(
                workspace_root=str(root / "workspace"),
                write=True,
                output=str(output),
                replace=False,
            )
            self.assertEqual(cmd_client_profile(arguments), 0)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(
                output.read_text(),
                render_codex_profile((root / "workspace").resolve()),
            )
            self.assertEqual(list(output.parent.glob(output.name + ".tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
