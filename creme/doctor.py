from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .adapters import Adapter, get_adapter
from .guidance import GuidanceValidation
from .guidance import default_path as default_guidance_path
from .guidance import load as load_guidance
from .host_wrappers import (
    RULES_FILENAME,
    bundle_install_issues,
    default_output_dir,
    default_rules_dir,
    render_host_wrappers,
)
from .profile import DEFAULT_RELATIVE_PROFILE, ProfileValidation, effective_policy, load
from .semaphore import canonical_creme_root


STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )


def _normalize_origin(url: str) -> str:
    value = url.strip()
    value = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", value)
    value = re.sub(r"^[^@/]+@", "", value)
    if ":" in value and "/" not in value.split(":", 1)[0]:
        value = value.replace(":", "/", 1)
    if value.endswith(".git"):
        value = value[:-4]
    return value.rstrip("/")


def _repo_root_from_cwd(cwd: Path) -> Optional[Path]:
    found = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    return Path(found.stdout.strip()).resolve() if found.returncode == 0 else None


def check_launch_root(creme_root: Path, cwd: Path) -> list[Check]:
    active = _repo_root_from_cwd(cwd)
    if active == creme_root:
        return [Check("launch root", STATUS_OK, str(creme_root))]
    detail = (
        f"WRONG_ROOT: active Git root is {active or '<none>'}; launch from {creme_root}. "
        "Sibling permission alone does not load Creme instructions, skills, or MCP config."
    )
    return [Check("launch root", STATUS_FAIL, detail)]


def check_profile(path: Path, adapter: Adapter) -> tuple[list[Check], ProfileValidation]:
    validated = load(path, adapter)
    if validated.status == "VALID":
        status = STATUS_OK
    elif validated.status in {"MISSING", "LIMITED"}:
        status = STATUS_WARN
    else:
        status = STATUS_FAIL
    detail = validated.detail
    if validated.status == "MISSING":
        detail += "; conservative defaults are active until `python3 -m creme init --write` is reviewed"
    return [Check("host profile", status, f"{validated.status}: {detail}")], validated


def check_host_guidance(path: Path) -> tuple[list[Check], GuidanceValidation]:
    checked = load_guidance(path)
    if checked.status == "OK":
        checks = [Check(
            "host guidance",
            STATUS_OK,
            f"{checked.detail}; read with `python3 -m creme host-guidance`",
        )]
    elif checked.status == "MISSING":
        checks = [Check("host guidance", STATUS_WARN, checked.detail)]
    else:
        checks = [Check("host guidance", STATUS_FAIL, checked.detail)]
    return checks, checked


def _tracked_top_names(repo: Path) -> set[str]:
    result = _git(repo, "ls-files")
    if result.returncode:
        return set()
    return {line.split("/", 1)[0] for line in result.stdout.splitlines() if line}


def check_sibling(name: str, repo: Path, expected_origin: str) -> list[Check]:
    checks: list[Check] = []
    if not repo.is_dir():
        return [Check(f"{name}: repository", STATUS_FAIL, f"missing: {repo}")]
    git_probe = _git(repo, "rev-parse", "--is-inside-work-tree")
    if git_probe.returncode or git_probe.stdout.strip() != "true":
        return [Check(f"{name}: repository", STATUS_FAIL, f"not a Git worktree: {repo}")]
    checks.append(Check(f"{name}: repository", STATUS_OK, str(repo)))
    access = []
    if os.access(repo, os.R_OK):
        access.append("read")
    if os.access(repo, os.W_OK):
        access.append("write")
    status = STATUS_OK if {"read", "write"}.issubset(access) else STATUS_FAIL
    checks.append(Check(f"{name}: access", status, "/".join(access) or "none"))
    origin = _git(repo, "remote", "get-url", "origin")
    if origin.returncode:
        checks.append(Check(f"{name}: origin", STATUS_WARN, "origin remote is absent"))
    else:
        normalized = _normalize_origin(origin.stdout)
        checks.append(Check(
            f"{name}: origin",
            STATUS_OK if normalized == expected_origin else STATUS_FAIL,
            normalized,
        ))
    agent_names = {"AGENTS.md", "CLAUDE.md", ".agents", ".codex", ".claude", ".mcp.json"}
    found = sorted(_tracked_top_names(repo).intersection(agent_names))
    checks.append(Check(
        f"{name}: standalone agent boundary",
        STATUS_FAIL if found else STATUS_OK,
        f"unexpected tracked agent surfaces: {found}" if found else "no tracked Creme/client surfaces",
    ))
    return checks


def _extract_pin(text: str) -> Optional[str]:
    match = re.search(r"lean-lsp-mcp==([0-9][0-9.]*)", text)
    return match.group(1) if match else None


def _load_mcp_surface(path: Path) -> dict[str, Any]:
    """Parse the one MCP entry we own instead of accepting stray text tokens."""
    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        server = payload["mcpServers"]["lean-lsp-mcp"]
        if not isinstance(server, dict):
            raise ValueError("lean-lsp-mcp entry is not an object")
        return server

    section: Optional[str] = None
    values: dict[str, dict[str, Any]] = {"server": {}, "env": {}}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            header = line[1:-1]
            section = {
                "mcp_servers.lean-lsp-mcp": "server",
                "mcp_servers.lean-lsp-mcp.env": "env",
            }.get(header)
            continue
        if section is None or "=" not in line:
            continue
        key, encoded = (piece.strip() for piece in line.split("=", 1))
        if key not in {"command", "args", "LEAN_MCP_DISABLED_TOOLS", "LEAN_LSP_MAX_OPEN_FILES", "LEAN_LSP_TEST_MODE"}:
            continue
        try:
            values[section][key] = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid TOML value for {key}: {exc}") from exc
    return {**values["server"], "env": values["env"]}


def check_goal_store(workspace: Path, profile: Optional[dict[str, Any]]) -> list[Check]:
    """Report the configured goal store and validate master-state privacy.

    The store is optional and never a runtime dependency.  When it is named,
    the master role keeps host-local durable state under ``master/`` there.
    If the store is a Git worktree, that entire runtime directory must be
    ignored and untracked; reusable protocol and any templates belong in Creme.
    """
    name = (profile or {}).get("workspace", {}).get("goal_store") if profile else None
    if not name:
        return [Check(
            "goal store",
            STATUS_OK,
            "not configured (optional; persistent master state unavailable; "
            "set with `init --goal-store NAME`)",
        )]
    store = (workspace / name).resolve()
    if not store.is_dir():
        return [Check("goal store", STATUS_FAIL, f"configured but missing: {store}")]

    master = store / "master"
    board = master / "board.md"
    state = f"master state {'present' if board.is_file() else 'absent'}: {board}"
    git_probe = _git(store, "rev-parse", "--is-inside-work-tree")
    if git_probe.returncode or git_probe.stdout.strip() != "true":
        return [Check("goal store", STATUS_OK, f"{store} ({state}; not a Git worktree)")]

    tracked = _git(store, "ls-files", "--", "master")
    if tracked.returncode:
        return [Check(
            "goal store",
            STATUS_FAIL,
            f"{store} ({state}; could not inspect tracked master state: {tracked.stderr.strip()})",
        )]
    tracked_paths = [line for line in tracked.stdout.splitlines() if line]
    if tracked_paths:
        preview = tracked_paths[:5]
        suffix = " ..." if len(tracked_paths) > len(preview) else ""
        return [Check(
            "goal store",
            STATUS_FAIL,
            f"{store} ({state}; master/ is Git-tracked: {preview}{suffix}; "
            "remove it from the index without deleting the local files)",
        )]

    ignored = _git(
        store,
        "check-ignore",
        "-q",
        "--no-index",
        "--",
        "master/.creme-ignore-probe",
    )
    if ignored.returncode:
        return [Check(
            "goal store",
            STATUS_FAIL,
            f"{store} ({state}; master/ is not ignored; add `/master/` to the "
            "goal store's .gitignore before creating runtime state)",
        )]

    detail = f"{store} ({state}; master/ is ignored and untracked)"
    return [Check("goal store", STATUS_OK, detail)]


def check_client_surface(root: Path) -> list[Check]:
    checks: list[Check] = []
    versions_path = root / "scripts" / "versions.json"
    try:
        versions = json.loads(versions_path.read_text(encoding="utf-8"))
        expected_pin = versions["lean_lsp_mcp"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return [Check("client surface", STATUS_FAIL, f"invalid versions manifest: {exc}")]
    surfaces = [
        root / ".codex" / "config.toml",
        root / ".mcp.json",
        root / ".agents" / "mcp_config.json",
    ]
    pins: dict[str, Optional[str]] = {}
    configs: dict[str, dict[str, Any]] = {}
    parse_errors: dict[str, str] = {}
    for surface in surfaces:
        relative = str(surface.relative_to(root))
        try:
            config = _load_mcp_surface(surface)
            configs[relative] = config
            args = config.get("args")
            pins[relative] = _extract_pin(" ".join(args)) if isinstance(args, list) and all(isinstance(arg, str) for arg in args) else None
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            configs[relative] = {}
            pins[relative] = None
            parse_errors[relative] = str(exc)
    mismatches = {path: pin for path, pin in pins.items() if pin != expected_pin}
    checks.append(Check(
        "client: MCP pin",
        STATUS_FAIL if mismatches else STATUS_OK,
        f"expected {expected_pin}; mismatches={mismatches}" if mismatches else f"all shims pin {expected_pin}",
    ))
    expected_args = ["-m", "creme", "lean-mcp", "--", "uvx", f"lean-lsp-mcp=={expected_pin}"]
    launcher_bad = [
        path for path, config in configs.items()
        if config.get("command") != "/usr/bin/python3" or config.get("args") != expected_args
    ]
    checks.append(Check(
        "client: guarded MCP launcher",
        STATUS_FAIL if launcher_bad else STATUS_OK,
        f"unguarded shims={launcher_bad}; parse_errors={parse_errors}" if launcher_bad else "all shims launch through Creme's Lake guard",
    ))
    try:
        from .build_ownership import trusted_uvx
        uvx_detail = str(trusted_uvx())
        uvx_status = STATUS_OK
    except (OSError, RuntimeError) as exc:
        uvx_detail = str(exc)
        uvx_status = STATUS_FAIL
    checks.append(Check("client: uvx identity", uvx_status, uvx_detail))
    required_env = {
        "LEAN_MCP_DISABLED_TOOLS": "lean_build,lean_profile_proof",
        "LEAN_LSP_MAX_OPEN_FILES": "2",
        "LEAN_LSP_TEST_MODE": "1",
    }
    env_bad: dict[str, list[str]] = {}
    for path, config in configs.items():
        env = config.get("env") if isinstance(config.get("env"), dict) else {}
        missing = [
            f"{key}={value}"
            for key, value in required_env.items()
            if env.get(key) != value
        ]
        if missing:
            env_bad[path] = missing
    checks.append(Check(
        "client: Lean build ownership",
        STATUS_FAIL if env_bad else STATUS_OK,
        f"missing or invalid settings={env_bad}" if env_bad else
        "lean_build and lean_profile_proof disabled, startup cache get suppressed, file workers capped at 2",
    ))
    claude = root / "CLAUDE.md"
    checks.append(Check(
        "client: Claude instruction shim",
        STATUS_OK if claude.is_file() and claude.read_text(encoding="utf-8").strip() == "@AGENTS.md" else STATUS_FAIL,
        "CLAUDE.md imports AGENTS.md" if claude.is_file() else "CLAUDE.md missing",
    ))
    skills = ["lean-inspector", "lean-prover"]
    missing_agents = [name for name in skills if not (root / ".agents" / "skills" / name / "SKILL.md").is_file()]
    missing_claude = [name for name in skills if not (root / ".claude" / "skills" / name / "SKILL.md").is_file()]
    checks.append(Check(
        "client: skills",
        STATUS_FAIL if missing_agents or missing_claude else STATUS_OK,
        f"missing canonical={missing_agents}, Claude={missing_claude}" if missing_agents or missing_claude else "canonical and Claude skill paths resolve",
    ))
    settings_path = root / ".claude" / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        directories = settings["permissions"]["additionalDirectories"]
        portable = directories == ["../jaune/", "../blanc/"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        portable = False
        directories = None
    checks.append(Check(
        "client: Claude sibling access",
        STATUS_OK if portable else STATUS_FAIL,
        f"relative additionalDirectories={directories!r}",
    ))
    return checks


def check_neutral_semaphore(root: Path) -> list[Check]:
    launcher = root / ".semaphore" / "semaphore"
    readme = root / ".semaphore" / "README.md"
    ignore = root / ".gitignore"
    issues = []
    if not launcher.is_file():
        issues.append("launcher missing")
    elif not os.access(launcher, os.X_OK):
        issues.append("launcher not executable")
    if not readme.is_file():
        issues.append("protocol documentation missing")
    try:
        ignored = "/.semaphore/state/" in ignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        ignored = False
    if not ignored:
        issues.append("runtime state is not ignored")
    return [Check(
        "client: neutral semaphore",
        STATUS_FAIL if issues else STATUS_OK,
        f"invalid interface: {issues}" if issues else str(launcher),
    )]


def check_host_wrappers(
    root: Path,
    output_dir: Optional[Path] = None,
    rules_dir: Optional[Path] = None,
) -> list[Check]:
    directory = (output_dir or default_output_dir()).expanduser().absolute()
    rules_directory = (rules_dir or default_rules_dir()).expanduser().absolute()
    try:
        rendered = render_host_wrappers(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return [Check(
            "client: host capability bundle",
            STATUS_FAIL,
            f"cannot render the expected host capability bundle: {exc}",
        )]
    members = [*(directory / name for name in rendered), rules_directory / RULES_FILENAME]
    present = [path for path in members if os.path.lexists(path)]
    if not present:
        return [Check(
            "client: host capability bundle",
            STATUS_WARN,
            (
                f"not installed in {directory} and {rules_directory}; direct Creme "
                "capability commands remain canonical and host escalation may prompt"
            ),
        )]
    issues = bundle_install_issues(root, directory, rules_directory)
    if issues:
        command = (
            "python3 -m creme host-wrappers --output-dir "
            f"{shlex.quote(str(directory))} --rules-dir "
            f"{shlex.quote(str(rules_directory))} --write --replace"
        )
        return [Check(
            "client: host capability bundle",
            STATUS_FAIL,
            f"invalid install: {issues}; review a fresh preview, then run `{command}`",
        )]
    return [Check(
        "client: host capability bundle",
        STATUS_OK,
        (
            f"all {len(members)} installed files match; fully restart Codex after "
            "any change because rules load at process startup; stricter managed "
            "requirements may still override these user allows"
        ),
    )]


def _runtime_files(root: Path) -> Iterable[Path]:
    for relative in ("creme", ".codex", ".claude", ".agents", "scripts"):
        base = root / relative
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and not (relative == "scripts" and "tests" in path.relative_to(base).parts)
            ):
                yield path


def check_public_runtime_boundary(root: Path) -> list[Check]:
    private_hits = []
    absolute_hits = []
    secret_hits = []
    secret_pattern = re.compile(
        r"(?i)(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    )
    for path in _runtime_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = str(path.relative_to(root))
        personal = "/" + "Users" + r"/[^/\s]+"
        private_home = "~/" + r"(?:elanc|plans)(?:/|\b)"
        if re.search(r"(?:" + personal + "|" + private_home + ")", text):
            absolute_hits.append(relative)
        private_repo = "/" + r"(?:elanc|plans)(?:/|\b)"
        if re.search(private_repo, text):
            private_hits.append(relative)
        if secret_pattern.search(text):
            secret_hits.append(relative)
    return [
        Check("public runtime: personal/private paths", STATUS_FAIL if absolute_hits or private_hits else STATUS_OK,
              f"hits={sorted(set(absolute_hits + private_hits))}" if absolute_hits or private_hits else "none"),
        Check("public runtime: secret patterns", STATUS_FAIL if secret_hits else STATUS_OK,
              f"hits={sorted(set(secret_hits))}" if secret_hits else "none"),
    ]


def run_doctor(
    root: Path,
    cwd: Path,
    profile_path: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
    adapter: Optional[Adapter] = None,
    cli_overrides: Optional[dict[str, Optional[int]]] = None,
) -> tuple[list[Check], dict[str, Any]]:
    selected = adapter or get_adapter()
    shared_root = canonical_creme_root(root)
    path = profile_path or shared_root / DEFAULT_RELATIVE_PROFILE
    checks = check_launch_root(root, cwd)
    profile_checks, validated = check_profile(path, selected)
    checks.extend(profile_checks)
    guidance_checks, guidance = check_host_guidance(default_guidance_path(root))
    checks.extend(guidance_checks)
    profile = validated.profile if validated.status in {"VALID", "LIMITED"} else None
    if workspace_root:
        workspace = workspace_root.expanduser().resolve()
    elif profile:
        workspace = Path(profile["workspace"]["root"]).expanduser().resolve()
    else:
        workspace = root.parent
    jaune_name = profile["workspace"]["jaune"] if profile else "jaune"
    blanc_name = profile["workspace"]["blanc"] if profile else "blanc"
    checks.extend(check_sibling("jaune", workspace / jaune_name, "github.com/skbaek/jaune"))
    checks.extend(check_sibling("blanc", workspace / blanc_name, "github.com/skbaek/blanc"))
    checks.extend(check_goal_store(workspace, profile))
    checks.extend(check_client_surface(root))
    checks.extend(check_neutral_semaphore(root))
    checks.extend(check_host_wrappers(shared_root))
    checks.extend(check_public_runtime_boundary(root))
    facts = selected.static_facts()
    checks.append(Check(
        "platform adapter", STATUS_OK if facts.status == "OK" else STATUS_FAIL,
        f"{selected.system}: {facts.detail}",
    ))
    policy = effective_policy(profile, selected, cli_overrides)
    context = {
        "root": str(root), "workspace_root": str(workspace),
        "profile": validated.status, "platform": selected.system,
        "effective_policy": policy, "host_guidance": guidance.status,
    }
    return checks, context


def exit_code(checks: Iterable[Check]) -> int:
    return 1 if any(check.status == STATUS_FAIL for check in checks) else 0
