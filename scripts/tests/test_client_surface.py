"""Static checks for Creme's committed, public client surface.

These tests parse files only. They deliberately do not launch a client, start
MCP, inspect a user's home directory, or mutate trust/approval state.
"""

from __future__ import annotations

import ast
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
# Reviewed against all 22 decorators in the pinned package, without importing
# its server. Keep the source audit explicit when changing PINNED_MCP.
LOCAL_READ_ONLY_TOOLS = (
    "lean_file_outline", "lean_diagnostic_messages", "lean_goal", "lean_term_goal",
    "lean_hover_info", "lean_completions", "lean_declaration_file", "lean_references",
    "lean_multi_attempt", "lean_run_code", "lean_local_search", "lean_code_actions",
    "lean_get_widgets", "lean_get_widget_source", "lean_profile_proof",
)
REMOTE_READ_ONLY_TOOLS = (
    "lean_leansearch", "lean_loogle", "lean_leanfinder", "lean_state_search",
    "lean_hammer_premise",
)
REVIEWED_ANNOTATIONS = {
    name: {
        "readOnlyHint": name not in {"lean_build", "lean_verify"},
        "openWorldHint": name in REMOTE_READ_ONLY_TOOLS,
        "idempotentHint": True,
        # Missing is distinct from false; do not invent an upstream annotation.
        "destructiveHint": True if name == "lean_build" else None,
    }
    for name in (*LOCAL_READ_ONLY_TOOLS, *REMOTE_READ_ONLY_TOOLS, "lean_build", "lean_verify")
}


def audit_mcp_annotations(source: str) -> None:
    """Compare a supplied server.py to the review; never execute its contents."""
    actual = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
            ):
                continue
            if len(decorator.args) != 1:
                raise AssertionError("MCP tool declaration shape changed")
            name = ast.literal_eval(decorator.args[0])
            if not isinstance(name, str) or name in actual:
                raise AssertionError("MCP tool name is invalid or duplicated")
            annotation = next(
                (kw.value for kw in decorator.keywords if kw.arg == "annotations"), None
            )
            if not (
                isinstance(annotation, ast.Call)
                and isinstance(annotation.func, ast.Name)
                and annotation.func.id == "ToolAnnotations"
                and not annotation.args
            ):
                raise AssertionError("MCP tool annotation shape changed")
            values = {kw.arg: ast.literal_eval(kw.value) for kw in annotation.keywords}
            if not set(values) <= {"title", "readOnlyHint", "openWorldHint", "idempotentHint", "destructiveHint"}:
                raise AssertionError("MCP tool annotation fields changed")
            actual[name] = {
                field: values.get(field)
                for field in ("readOnlyHint", "openWorldHint", "idempotentHint", "destructiveHint")
            }
            if any(value is not None and type(value) is not bool for value in actual[name].values()):
                raise AssertionError("MCP tool annotation types changed")
    if actual != REVIEWED_ANNOTATIONS:
        raise AssertionError("Pinned MCP tool inventory/annotations drifted; re-review approval policy")


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _project_codex_config(text: str) -> dict:
    """Read the committed TOML subset on Python 3.9; reject unfamiliar syntax.

    This is a static surface check, not a general client configuration parser.
    Quoted tables, inline tables, multiline values and non-string arrays need
    explicit review if ever introduced into this small project config.
    """
    result = {}
    table = result
    declared_tables = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        section = re.fullmatch(r"\[([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\]", line)
        if section:
            path = section.group(1)
            if path in declared_tables:
                raise AssertionError("duplicate config table")
            declared_tables.add(path)
            table = result
            for part in path.split("."):
                table = table.setdefault(part, {})
                if not isinstance(table, dict):
                    raise AssertionError("config table/value collision")
            continue
        assignment = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if not assignment:
            raise AssertionError("unreviewed project TOML syntax")
        key, value = assignment.groups()
        if key in table:
            raise AssertionError("duplicate config key")
        if value.startswith("'''") and value.endswith("'''") and "'''" not in value[3:-3]:
            parsed = value[3:-3]
        else:
            parsed = json.loads(value)
        if not (
            isinstance(parsed, (str, bool))
            or isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)
        ):
            raise AssertionError("unreviewed project TOML value")
        table[key] = parsed
    return result


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
        self.assertRegex(text, r'(?m)^command\s*=\s*"/usr/bin/python3"\s*$')
        self.assertRegex(
            text,
            rf'(?m)^args\s*=\s*\["-m", "creme", "lean-mcp", "--", "uvx", "{re.escape(PINNED_MCP)}"\]\s*$',
        )
        self.assertRegex(text, r"(?m)^required\s*=\s*true\s*$")
        self.assertRegex(
            text,
            r'(?m)^default_tools_approval_mode\s*=\s*"writes"\s*$',
        )
        self.assertNotRegex(text, r"(?m)^\[permissions(?:\.|])")
        self.assertNotRegex(text, r"(?m)^\[projects(?:\.|])")

    def assert_codex_approval_policy(self, text: str) -> None:
        config = _project_codex_config(text)
        self.assertEqual(set(config), {"mcp_servers"})
        self.assertEqual(set(config["mcp_servers"]), {"lean-lsp-mcp"})
        server = config["mcp_servers"]["lean-lsp-mcp"]
        self.assertEqual(
            set(server),
            {"command", "args", "required", "default_tools_approval_mode", "tools", "env"},
        )
        self.assertEqual(server["default_tools_approval_mode"], "writes")
        self.assertEqual(server["tools"], {"lean_verify": {"approval_mode": "approve"}})
        self.assertEqual(server["command"], "/usr/bin/python3")
        self.assertEqual(server["args"], ["-m", "creme", "lean-mcp", "--", "uvx", PINNED_MCP])
        self.assertIs(server["required"], True)
        self.assertEqual(server["env"], _json(".mcp.json")["mcpServers"]["lean-lsp-mcp"]["env"])

    def test_codex_approval_exception_is_only_reviewed_lean_verify(self) -> None:
        self.assert_codex_approval_policy(
            (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
        )

    def test_codex_approval_policy_rejects_missing_or_widened_exception(self) -> None:
        text = (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
        exception = '[mcp_servers.lean-lsp-mcp.tools.lean_verify]\napproval_mode = "approve"\n'
        mutations = {
            "missing": text.replace(exception, ""),
            "server-wide": text.replace('default_tools_approval_mode = "writes"', 'default_tools_approval_mode = "approve"'),
            "wrong tool": text.replace("tools.lean_verify]", "tools.lean_build]"),
            "still prompts": text.replace('approval_mode = "approve"', 'approval_mode = "prompt"'),
            "extra tool": text + '\n[mcp_servers.lean-lsp-mcp.tools.lean_run_code]\napproval_mode = "approve"\n',
            "top-level": 'approval_policy = "never"\n' + text,
            "build enabled": text.replace("lean_build,lean_profile_proof", "lean_profile_proof"),
        }
        self.assert_codex_approval_policy(text)
        for label, mutated in mutations.items():
            with self.subTest(mutation=label):
                self.assertNotEqual(mutated, text)
                with self.assertRaises(AssertionError):
                    self.assert_codex_approval_policy(mutated)
        self.assert_codex_approval_policy(text)

    def test_reviewed_inventory_covers_all_enabled_tool_policies(self) -> None:
        server = _project_codex_config((ROOT / ".codex/config.toml").read_text())["mcp_servers"]["lean-lsp-mcp"]
        self.assertIn(PINNED_MCP, server["args"])
        disabled = set(server["env"]["LEAN_MCP_DISABLED_TOOLS"].split(","))
        self.assertEqual(disabled, {"lean_build", "lean_profile_proof"})
        self.assertEqual(len(REVIEWED_ANNOTATIONS), 22)
        enabled = set(REVIEWED_ANNOTATIONS) - disabled
        self.assertEqual(len(enabled), 20)
        self.assertEqual(
            {name for name in enabled if not REVIEWED_ANNOTATIONS[name]["readOnlyHint"]},
            set(server["tools"]),
        )
        guide = (ROOT / "docs/client-discovery.md").read_text()
        for name in REVIEWED_ANNOTATIONS:
            self.assertIn(f"`{name}`", guide)

    def test_project_config_reader_rejects_unreviewed_syntax(self) -> None:
        text = (ROOT / ".codex/config.toml").read_text()
        parsed = _project_codex_config(text)
        for addition in (
            '\n[permissions]\nallow = {all = true}\n',
            '\n["quoted-table"]\nvalue = "x"\n',
            '\n[mcp_servers.lean-lsp-mcp]\n',
            '\nLEAN_LOG_LEVEL = "duplicate"\n',
        ):
            with self.assertRaises((AssertionError, ValueError)):
                _project_codex_config(text + addition)
        # Independent standard-library parity where available. Python 3.9/3.10
        # still run every policy/control test using the constrained reader.
        try:
            import tomllib
        except ImportError:
            pass
        else:
            self.assertEqual(parsed, tomllib.loads(text))

    def test_source_annotation_audit_rejects_tool_and_metadata_drift(self) -> None:
        # Synthetic decorators exercise the auditor without depending on a
        # user's package cache. Release evidence separately audits real source.
        source = "\n".join(
            f'@mcp.tool({name!r}, annotations=ToolAnnotations('
            + ", ".join(f"{field}={value!r}" for field, value in fields.items() if value is not None)
            + f"))\ndef tool_{index}(): pass\n"
            for index, (name, fields) in enumerate(REVIEWED_ANNOTATIONS.items())
        )
        audit_mcp_annotations(source)
        for mutated in (
            source.replace("'lean_goal'", "'lean_new_tool'"),
            source.replace("readOnlyHint=True", "readOnlyHint=False", 1),
            source.replace("openWorldHint=False", "openWorldHint=True", 1),
            source + "\n@mcp.tool('extra', annotations=ToolAnnotations())\ndef extra(): pass\n",
        ):
            with self.assertRaisesRegex(AssertionError, "inventory/annotations drifted"):
                audit_mcp_annotations(mutated)
        audit_mcp_annotations(source)

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

    def test_host_semaphore_has_one_tracked_client_neutral_entry_point(self) -> None:
        launcher = ROOT / ".semaphore" / "semaphore"
        self.assertTrue(launcher.is_file())
        self.assertTrue(launcher.stat().st_mode & stat.S_IXUSR)
        text = launcher.read_text(encoding="utf-8")
        self.assertIn('main(["semaphore", *sys.argv[1:]])', text)
        self.assertNotIn(".codex", text)
        self.assertNotIn(".claude", text)
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.semaphore/state/", ignored)

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
        self.assertEqual(claude_server["command"], "/usr/bin/python3")
        self.assertEqual(
            claude_server["args"],
            ["-m", "creme", "lean-mcp", "--", "uvx", PINNED_MCP],
        )
        self.assertEqual(
            {key: claude_server["env"][key] for key in (
                "LEAN_MCP_DISABLED_TOOLS", "LEAN_LSP_MAX_OPEN_FILES", "LEAN_LSP_TEST_MODE"
            )},
            {
                "LEAN_MCP_DISABLED_TOOLS": "lean_build,lean_profile_proof",
                "LEAN_LSP_MAX_OPEN_FILES": "2",
                "LEAN_LSP_TEST_MODE": "1",
            },
        )

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
            "python3 -m creme host-guidance",
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

    def test_briefs_guide_keeps_model_effort_contract(self) -> None:
        guide = (ROOT / "docs/guides/briefs.md").read_text(encoding="utf-8")
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

    def test_adaptive_memory_admission_is_part_of_the_agent_contract(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        execution = (ROOT / "docs/guides/execution.md").read_text(encoding="utf-8")
        capabilities = (ROOT / "docs/capabilities.md").read_text(encoding="utf-8")

        for text in (agents, execution):
            for concept in (
                "adaptive-acquire",
                "DEFER_FOR_HARD",
                "LIGHT_ONLY",
                "YIELD_HEAVY",
                "DRAIN_HEAVY",
                "light work",
            ):
                self.assertIn(concept, text)
        for concept in (
            "memory_headroom",
            "25% physical memory",
            "25% margin",
            "one hard holder",
            "process enumeration",
        ):
            self.assertIn(concept, capabilities)

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
