"""Lean mode for brokered Luna reserve pseudo-subagent sessions.

A Lean-mode session is an ordinary brokered write session with four additions,
each fail-closed:

* **Target.** The target is exactly ``<repo>/.worktrees/GOAL`` or
  ``<repo>/.worktrees/GOAL-<suffix>`` for a sanctioned suffix, in the Jaune or
  Blanc repository the Creme host profile resolves, so the session's goal label
  names the worktree that ``creme lake-build`` and ``reclaim --wind-down``
  scope on.
* **One MCP server.** Every MCP server stays a disabled stub except
  ``lean-lsp-mcp``, which is launched from Creme's *tracked* project
  definition (the committed ``.codex/config.toml`` of the launch root, which
  must match its Git ``HEAD``) with the open-world search tools disabled. The
  running server's effective definition must equal that definition exactly.
* **Host discipline.** A session starts only when memory headroom and the
  semaphore admit heavy work, and no Lean or Lake process already runs in the
  target. The broker holds at most two live Lean sessions by default
  (`CREME_LUNA_MAX_LEAN_SESSIONS` accepts 1--4), with distinct goal labels
  because wind-down also covers sanctioned goal worktree suffixes.
* **Wind-down.** Every way a Lean session ends runs
  ``python3 -m creme reclaim --wind-down GOAL`` after its app-server has
  closed, then checks that no ``lean`` or ``lake`` process is left with a
  working directory inside the target. The session is reported cleanly
  stopped only when both hold.

The module never starts Lean itself and passes no model string to Codex.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from .task_wind_down import SANCTIONED_SUFFIXES

LEAN_MCP_SERVER = "lean-lsp-mcp"
PROJECT_CONFIG_RELATIVE = Path(".codex/config.toml")
LEAN_PREAMBLE_RELATIVE = Path("templates/luna-reserve/lean-preamble.md")
GOAL_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Tools whose upstream annotation is openWorldHint=true: they reach the network
# from an MCP server Codex does not sandbox, so a Lean session never offers them.
OPEN_WORLD_TOOLS = (
    "lean_hammer_premise", "lean_leanfinder", "lean_leansearch", "lean_loogle", "lean_state_search",
)
# Disabled by the tracked definition itself; they must never appear.
FORBIDDEN_TOOLS = ("lean_build", "lean_profile_proof")
REQUIRED_DISABLED_ENV = {"lean_build", "lean_profile_proof"}
PINNED_ARGS = re.compile(r"^lean-lsp-mcp==[0-9]+(?:\.[0-9]+){1,3}$")

# Host guidance: do not start another heavy unit below 30% free memory; the
# semaphore's own drain floor is 20%.
MEMORY_FLOOR_PERCENT = 30
MCP_READY_SECONDS = 90.0
RESIDUAL_WAIT_SECONDS = 20.0
WIND_DOWN_TIMEOUT_SECONDS = 120.0


class LeanModeError(RuntimeError):
    """A fail-closed Lean-mode condition with a user-facing reason."""


# ---------------------------------------------------------------------------
# Target


def repositories(creme_root: Path, adapter: Any = None) -> tuple[Path, ...]:
    """The Jaune and Blanc repositories named by the host profile (or the default layout)."""
    from .profile import DEFAULT_RELATIVE_PROFILE, load as load_profile

    checked = load_profile(creme_root / DEFAULT_RELATIVE_PROFILE, adapter)
    if checked.profile is not None and checked.status in {"VALID", "LIMITED", "STALE"}:
        workspace = checked.profile["workspace"]
        root = Path(workspace["root"]).expanduser().resolve()
        names = (workspace["jaune"], workspace["blanc"])
    else:
        root = creme_root.parent.resolve()
        names = ("jaune", "blanc")
    found = []
    for name in names:
        repository = (root / name).resolve()
        if repository == root or root not in repository.parents:
            raise LeanModeError("a configured repository escapes the workspace root")
        found.append(repository)
    return tuple(found)


def target_refusals(goal: str, target: Path, repository_roots: tuple[Path, ...]) -> list[str]:
    """Refuse anything but exactly ``<jaune|blanc>/.worktrees/GOAL`` or a sanctioned ``-suffix`` tree."""
    if not isinstance(goal, str) or GOAL_LABEL.fullmatch(goal) is None or goal in (".", ".."):
        return [f"Lean goal label {goal!r} is not a simple stable identifier"]
    raw = Path(target).expanduser()
    names = (goal, *(f"{goal}-{suffix}" for suffix in SANCTIONED_SUFFIXES))
    allowed = [repository / ".worktrees" / name for repository in repository_roots for name in names]
    candidate = next((path for path in allowed if raw.absolute() == path), None)
    if candidate is None:
        candidate = next((path for path in allowed if raw.resolve() == path), None)
    if candidate is not None:
        if candidate.is_symlink():
            return [f"Lean target {candidate} is a symlink"]
        if not candidate.is_dir() or not (candidate / ".git").is_file():
            return [f"Lean target {candidate} is not a Git worktree"]
        if raw.resolve() != candidate.resolve() or candidate.resolve().parent != candidate.parent.resolve():
            return [f"Lean target {candidate} resolves outside its repository's .worktrees directory"]
        return []
    candidates = " or ".join(str(path) for path in allowed) or "no configured repository"
    return [f"Lean mode needs the goal's own worktree ({candidates}), or its sanctioned disposable tree, not {raw}"]


# ---------------------------------------------------------------------------
# Tracked MCP definition


def parse_project_config(text: str) -> dict:
    """Parse the small TOML subset Creme's project configuration uses; refuse anything else.

    Python 3.9 has no ``tomllib``. The committed file holds only tables with
    bare dotted names and ``key = value`` lines whose value is a JSON-compatible
    string, boolean, or string array, or a ``'''literal'''`` string.
    """
    result: dict = {}
    table = result
    declared = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        section = re.fullmatch(r"\[([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\]", line)
        if section:
            path = section.group(1)
            if path in declared:
                raise LeanModeError(f"duplicate table [{path}] in the project configuration")
            declared.add(path)
            table = result
            for part in path.split("."):
                table = table.setdefault(part, {})
                if not isinstance(table, dict):
                    raise LeanModeError("table and value collide in the project configuration")
            continue
        assignment = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if not assignment:
            raise LeanModeError("the project configuration uses unreviewed TOML syntax")
        key, value = assignment.groups()
        if key in table:
            raise LeanModeError(f"duplicate key {key} in the project configuration")
        if value.startswith("'''") and value.endswith("'''") and len(value) >= 6 and "'''" not in value[3:-3]:
            parsed: Any = value[3:-3]
        else:
            try:
                parsed = json.loads(value)
            except ValueError:
                raise LeanModeError(f"unreviewed TOML value for {key} in the project configuration")
        if not (isinstance(parsed, (str, bool))
                or isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)):
            raise LeanModeError(f"unreviewed TOML value for {key} in the project configuration")
        table[key] = parsed
    return result


def tracked_definition(launch_root: Path, git: Callable[[Path], Optional[str]] = None) -> dict:
    """Creme's committed ``lean-lsp-mcp`` definition, checked against its pins."""
    path = launch_root / PROJECT_CONFIG_RELATIVE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LeanModeError(f"cannot read the project configuration {path}: {exc}")
    committed = (git or _git_head_config)(launch_root)
    if committed is None:
        raise LeanModeError(f"cannot read {PROJECT_CONFIG_RELATIVE} at the launch root's Git HEAD")
    if committed != text:
        raise LeanModeError(f"{path} differs from its committed version; the Lean MCP definition has drifted")
    server = (parse_project_config(text).get("mcp_servers") or {}).get(LEAN_MCP_SERVER)
    if not isinstance(server, dict):
        raise LeanModeError(f"the project configuration defines no {LEAN_MCP_SERVER} server")
    failures = pin_failures(server)
    if failures:
        raise LeanModeError("the tracked Lean MCP definition breaks its pins: " + "; ".join(failures))
    return server


def _git_head_config(launch_root: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(launch_root), "show", f"HEAD:{PROJECT_CONFIG_RELATIVE.as_posix()}"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def pin_failures(server: dict) -> list[str]:
    """The guarded launcher, pinned package, and host-safety settings the definition must keep."""
    failures = []
    args = server.get("args")
    if server.get("command") != "/usr/bin/python3":
        failures.append(f"command is {server.get('command')!r}, not /usr/bin/python3")
    if not (isinstance(args, list) and len(args) == 6 and args[:5] == ["-m", "creme", "lean-mcp", "--", "uvx"]
            and isinstance(args[5], str) and PINNED_ARGS.fullmatch(args[5])):
        failures.append(f"args {args!r} are not the guarded `-m creme lean-mcp -- uvx lean-lsp-mcp==PIN` launcher")
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    disabled = {item.strip() for item in str(env.get("LEAN_MCP_DISABLED_TOOLS", "")).split(",") if item.strip()}
    if not REQUIRED_DISABLED_ENV <= disabled:
        failures.append("LEAN_MCP_DISABLED_TOOLS does not disable lean_build and lean_profile_proof")
    if env.get("LEAN_LSP_MAX_OPEN_FILES") != "2":
        failures.append("LEAN_LSP_MAX_OPEN_FILES is not 2")
    if server.get("default_tools_approval_mode") != "writes":
        failures.append("default_tools_approval_mode is not writes")
    tools = server.get("tools") if isinstance(server.get("tools"), dict) else {}
    for name, settings in tools.items():
        if name != "lean_verify" or settings != {"approval_mode": "approve"}:
            failures.append(f"tool approval setting for {name!r} is not the reviewed lean_verify exception")
    if "enabled_tools" in server or "disabled_tools" in server or "cwd" in server or "url" in server:
        failures.append("the definition carries an unreviewed tool filter, cwd, or URL")
    return failures


def launch_definition(tracked: dict) -> dict:
    """The definition the Lean session launches: the tracked one plus the open-world tools disabled."""
    return {**json.loads(json.dumps(tracked)), "disabled_tools": list(OPEN_WORLD_TOOLS)}


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            label = key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)
            parts.append(f"{label}={_toml(value[key])}")
        return "{" + ",".join(parts) + "}"
    raise LeanModeError(f"cannot write {type(value).__name__} as a launch override")


def launch_override(definition: dict) -> str:
    """One ``-c`` value defining the whole server, so no project-only nested key lacks a transport."""
    return f"mcp_servers.{LEAN_MCP_SERVER}={_toml(definition)}"


# Keys Codex adds to every effective MCP definition with these default values.
_EFFECTIVE_DEFAULTS = {"enabled": True, "environment_id": "local", "tool_timeout_sec": None}


def definition_failures(config_read: dict, expected: dict) -> list[str]:
    """The running server's effective definition must be exactly the launched one."""
    servers = ((config_read or {}).get("config") or {}).get("mcp_servers") or {}
    effective = servers.get(LEAN_MCP_SERVER)
    if not isinstance(effective, dict):
        return [f"{LEAN_MCP_SERVER} is absent from the effective configuration"]
    normalized = dict(effective)
    for key, default in _EFFECTIVE_DEFAULTS.items():
        if key in normalized and normalized[key] == default:
            normalized.pop(key)
    if normalized != expected:
        drift = sorted(key for key in set(normalized) | set(expected) if normalized.get(key) != expected.get(key))
        return [f"effective {LEAN_MCP_SERVER} definition differs from Creme's tracked definition in {drift}"]
    return []


def tool_failures(status_list: dict) -> list[str]:
    """``mcpServerStatus/list``: the Lean server connected without forbidden tools; nothing else runs."""
    failures = []
    entries = {entry.get("name"): entry for entry in (status_list or {}).get("data") or [] if isinstance(entry, dict)}
    lean = entries.get(LEAN_MCP_SERVER)
    if lean is None or lean.get("runtimeStatus") != "connected":
        failures.append(f"{LEAN_MCP_SERVER} is not connected: {(lean or {}).get('runtimeStatus')!r}")
    else:
        offered = set((lean.get("tools") or {}).keys())
        leaked = sorted(offered & (set(OPEN_WORLD_TOOLS) | set(FORBIDDEN_TOOLS)))
        if leaked:
            failures.append(f"{LEAN_MCP_SERVER} offers tools that must be unavailable: {leaked}")
        if lean.get("pluginId"):
            failures.append(f"{LEAN_MCP_SERVER} comes from a plugin")
    for name, entry in entries.items():
        if name != LEAN_MCP_SERVER and entry.get("runtimeStatus") not in ("disabled", None):
            failures.append(f"MCP server {name!r} is {entry.get('runtimeStatus')!r}, not disabled")
    return failures


# ---------------------------------------------------------------------------
# Forbidden commands


# The broker owns wind-down for a Lean session (see ``run_wind_down``), and the
# semaphore and Lean reclamation are the host's, not the pseudo-subagent's.
# ``templates/luna-reserve/lean-preamble.md`` already forbids them in words; a
# model that asks anyway must not cost the master a decision, so the broker
# declines the escalation itself and records why.
COMMAND_APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval", "execCommandApproval",
})
# Executables, matched on the token's basename so any path spelling is caught.
FORBIDDEN_BASENAMES = frozenset({"semaphore", "codex-reclaim-lean"})
# Subcommands of the Creme CLI, matched only after a ``creme`` invocation, so
# ``creme lake-build`` keeps its escalation path.
FORBIDDEN_CREME_SUBCOMMANDS = frozenset({"reclaim", "semaphore"})
WIND_DOWN_FLAG = "--wind-down"
_CREME_MODULES = frozenset({"creme"} | {f"creme.{name}" for name in FORBIDDEN_CREME_SUBCOMMANDS})


def _split(text: str) -> list[str]:
    try:
        pieces = shlex.split(text)
    except ValueError:
        pieces = text.split()
    return pieces or [text]


def command_tokens(value: Any, depth: int = 3) -> list[str]:
    """Flatten an approval request's command into tokens.

    ``item/commandExecution/requestApproval`` carries one string and
    ``execCommandApproval`` an argv list, and either may wrap a script in
    ``sh -c``. Each element is split with shell quoting, and any piece that
    still holds whitespace is split again to a bounded depth, so a nested
    ``bash -lc "... && semaphore ..."`` is read as tokens too.
    """
    if isinstance(value, list):
        items = [str(item) for item in value]
    elif isinstance(value, str):
        items = [value]
    else:
        return []
    tokens: list[str] = []
    for text in items:
        pieces = _split(text)
        tokens.extend(pieces)
        if depth <= 0:
            continue
        for piece in pieces:
            if piece != text and any(character.isspace() for character in piece):
                tokens.extend(command_tokens(piece, depth - 1))
    return tokens


def forbidden_command(value: Any) -> Optional[str]:
    """Why a Lean pseudo-subagent may never run this command, or ``None``.

    Fail-closed and deliberately blunt: any token that names the semaphore
    launcher or the generated reclamation delegate, any ``--wind-down``
    argument, and any ``reclaim``/``semaphore`` subcommand that follows a
    ``creme`` invocation. The cost of a false positive is one declined
    escalation with a printed reason; the cost of a miss is the failure this
    guard exists to prevent.
    """
    tokens = [token.strip("'\"") for token in command_tokens(value)]
    creme = False
    for index, token in enumerate(tokens):
        if token == WIND_DOWN_FLAG or token.startswith(WIND_DOWN_FLAG + "="):
            return (f"{WIND_DOWN_FLAG} belongs to the broker, which runs "
                    "reclaim --wind-down itself when this session ends")
        name = PurePosixPath(token).name
        if name in FORBIDDEN_BASENAMES:
            return f"{name} is a semaphore or Lean reclamation command, which a Lean session never runs"
        if name == "creme":
            creme = True
        elif token == "-m" and index + 1 < len(tokens):
            module = tokens[index + 1]
            if module in _CREME_MODULES:
                creme = True
            if module.startswith("creme.") and module.split(".", 1)[1] in FORBIDDEN_CREME_SUBCOMMANDS:
                return f"{module} is a semaphore or Lean reclamation command, which a Lean session never runs"
        elif creme and token in FORBIDDEN_CREME_SUBCOMMANDS:
            return f"creme {token} is a semaphore or Lean reclamation command, which a Lean session never runs"
    return None


# ---------------------------------------------------------------------------
# Host discipline


def admission_refusals(sample: Any, state: dict, goal: str, floor: int = MEMORY_FLOOR_PERCENT) -> list[str]:
    """Refuse when headroom or the semaphore says no heavy work may start."""
    from . import semaphore

    refusals = []
    data = sample.data if getattr(sample, "status", None) == "OK" and isinstance(sample.data, dict) else None
    free = data.get("memory_free_percent") if data else None
    if isinstance(free, bool) or not isinstance(free, (int, float)):
        refusals.append(f"memory headroom is unavailable ({getattr(sample, 'detail', 'no sample')}); "
                        "a Lean session is not started blind")
    elif free < semaphore.ADMISSION_DRAIN_PERCENT:
        refusals.append(f"DRAIN_HEAVY/LIGHT_ONLY: available memory is {free}% "
                        f"(<{semaphore.ADMISSION_DRAIN_PERCENT}%)")
    elif free < floor:
        refusals.append(f"available memory is {free}%, below the host guidance floor of {floor}% for heavy work")
    hard = (state or {}).get("hard")
    if hard and hard.get("label") != goal:
        refusals.append(f"DEFER_HEAVY: hard hold {hard.get('label')} holds the host")
    if any(item.get("manual") for item in (state or {}).get("soft") or []):
        refusals.append("LIGHT_ONLY: a manual human-session hold is active")
    return refusals


def host_observation(goal: str) -> tuple[list[str], dict]:
    """Sample headroom and the semaphore (no hold is taken) and judge them."""
    from . import semaphore
    from .adapters import get_adapter

    try:
        sample = get_adapter().memory_headroom()
        state = semaphore.snapshot()
    except Exception as exc:  # fail closed on any unreadable host state
        return [f"host state could not be read: {exc}"], {}
    observed = {
        "memory": {"status": sample.status, "data": sample.data},
        "hard": (state.get("hard") or {}).get("label"),
        "soft": [item.get("label") for item in state.get("soft") or []],
    }
    return admission_refusals(sample, state, goal), observed


# ---------------------------------------------------------------------------
# Processes and wind-down


def residual_processes(target: Path) -> list[dict]:
    """``lean`` and ``lake`` processes whose working directory lies inside ``target``."""
    from .reclaim import lean_executable

    try:
        table = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True,
                               check=False, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return [{"pid": None, "command": f"process table unavailable: {exc}"}]
    candidates = {}
    for line in table.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) == os.getpid():
            continue
        executable, _ = lean_executable(parts[1])
        if executable in ("lean", "lake"):
            candidates[int(parts[0])] = parts[1][:200]
    if not candidates:
        return []
    try:
        sample = subprocess.run(["/usr/sbin/lsof", "-a", "-d", "cwd", "-Fn", "-p",
                                 ",".join(str(pid) for pid in sorted(candidates))],
                                capture_output=True, text=True, check=False, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return [{"pid": pid, "command": command, "cwd": f"uninspectable: {exc}"} for pid, command in candidates.items()]
    cwds: dict[int, str] = {}
    current = None
    for line in sample.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
        elif line.startswith("n") and current is not None:
            cwds[current] = line[1:]
    root = target.resolve()
    residual = []
    for pid, command in sorted(candidates.items()):
        cwd = cwds.get(pid)
        if cwd is None:
            if _alive(pid):
                residual.append({"pid": pid, "command": command, "cwd": "uninspectable"})
            continue
        path = Path(cwd)
        if path == root or root in path.parents:
            residual.append({"pid": pid, "command": command, "cwd": cwd})
    return residual


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_wind_down(module_root: Path, environ: dict, goal: str, target: Path,
                  residual: Callable[[Path], list] = residual_processes,
                  settle_seconds: float = RESIDUAL_WAIT_SECONDS) -> dict:
    """Wind down after the app-server closed: goal-scoped reclaim, then a residual scan.

    The broker is not a client process, so ``reclaim`` finds no Lean process
    *owned* by it; the closed app-server's process tree must exit by itself.
    The scan therefore waits for in-target ``lean``/``lake`` processes to go and
    reports any that remain. ``OK`` needs both a structured wind-down ``OK``
    and an empty scan.
    """
    started = time.time()
    deadline = time.monotonic() + settle_seconds
    left = residual(target)
    while left and time.monotonic() < deadline:
        time.sleep(0.5)
        left = residual(target)
    env = dict(environ)
    env["PYTHONPATH"] = str(module_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-m", "creme", "reclaim", "--wind-down", goal]
    result: dict = {"command": command[1:], "started": started}
    try:
        completed = subprocess.run(command, cwd=str(module_root), env=env, capture_output=True, text=True,
                                   check=False, timeout=WIND_DOWN_TIMEOUT_SECONDS)
        result["exit"] = completed.returncode
        try:
            report = json.loads(completed.stdout)
        except ValueError:
            report = {"status": "ERROR", "detail": f"unparseable wind-down output: {completed.stdout[-300:]!r} "
                                                   f"{completed.stderr[-300:]!r}"}
    except (OSError, subprocess.SubprocessError) as exc:
        result["exit"] = None
        report = {"status": "ERROR", "detail": f"wind-down could not run: {exc}"}
    result["status"] = report.get("status")
    result["detail"] = report.get("detail")
    result["report"] = report
    left = residual(target)
    result["residual"] = left
    result["verdict"] = "OK" if report.get("status") == "OK" and not left else "NOT_OK"
    return result


# ---------------------------------------------------------------------------
# Preamble


@dataclass
class LeanMode:
    goal: str
    target: Path
    definition: dict = field(default_factory=dict)
    # Source of Creme's tracked definition; tests substitute a fixture.
    tracked: Callable[[Path], dict] = tracked_definition

    def summary(self) -> dict:
        return {"goal": self.goal, "target": str(self.target), "mcp_server": LEAN_MCP_SERVER,
                "disabled_tools": list(OPEN_WORLD_TOOLS)}


def developer_instructions(module_root: Path, launch_root: Path, mode: LeanMode) -> str:
    template = (module_root / LEAN_PREAMBLE_RELATIVE).read_text(encoding="utf-8")
    return (
        template.replace("{launch_root}", str(launch_root))
        .replace("{target}", str(mode.target))
        .replace("{goal}", mode.goal)
    )
