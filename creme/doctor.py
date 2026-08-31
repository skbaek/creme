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
from .host_wrappers import default_output_dir, render_host_wrappers, wrapper_install_issues
from .profile import DEFAULT_RELATIVE_PROFILE, ProfileValidation, effective_policy, load


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
    pins = {}
    for surface in surfaces:
        try:
            pins[str(surface.relative_to(root))] = _extract_pin(surface.read_text(encoding="utf-8"))
        except OSError:
            pins[str(surface.relative_to(root))] = None
    mismatches = {path: pin for path, pin in pins.items() if pin != expected_pin}
    checks.append(Check(
        "client: MCP pin",
        STATUS_FAIL if mismatches else STATUS_OK,
        f"expected {expected_pin}; mismatches={mismatches}" if mismatches else f"all shims pin {expected_pin}",
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


def check_host_wrappers(root: Path, output_dir: Optional[Path] = None) -> list[Check]:
    directory = (output_dir or default_output_dir()).expanduser().resolve()
    rendered = render_host_wrappers(root)
    present = [
        name for name in rendered
        if os.path.lexists(directory / name)
    ]
    if not present:
        return [Check(
            "client: host wrappers",
            STATUS_WARN,
            f"not installed in {directory}; direct Creme capability commands remain canonical",
        )]
    issues = wrapper_install_issues(root, directory)
    if issues:
        command = (
            "python3 -m creme host-wrappers --output-dir "
            f"{shlex.quote(str(directory))} --write --replace"
        )
        return [Check(
            "client: host wrappers",
            STATUS_FAIL,
            f"invalid install: {issues}; review a fresh preview, then run `{command}`",
        )]
    return [Check(
        "client: host wrappers",
        STATUS_OK,
        f"all {len(rendered)} delegates match {root / 'scripts' / 'creme'}",
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
    path = profile_path or root / DEFAULT_RELATIVE_PROFILE
    checks = check_launch_root(root, cwd)
    profile_checks, validated = check_profile(path, selected)
    checks.extend(profile_checks)
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
    checks.extend(check_client_surface(root))
    checks.extend(check_neutral_semaphore(root))
    checks.extend(check_host_wrappers(root))
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
        "effective_policy": policy,
    }
    return checks, context


def exit_code(checks: Iterable[Check]) -> int:
    return 1 if any(check.status == STATUS_FAIL for check in checks) else 0
