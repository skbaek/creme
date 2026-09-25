"""Read-only Antigravity pseudo-subagent runs."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


READ_ONLY_TOOLS = ("view_file", "list_dir", "grep_search", "find_by_name", "finish")
WRITE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file")
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PREFLIGHT_REFUSED = 10
EXIT_USAGE = 2
DEFAULT_MIN_REMAINING_FRACTION = 0.05
DEFAULT_FLOOR = DEFAULT_MIN_REMAINING_FRACTION
DEFAULT_FAMILY = "gemini-3.8-flash"
# Provisional until the effort-ladder experiment (goal store reports/antigravity-effort-ladder-20260925.md) decides; user does not assume high is optimal.
DEFAULT_EFFORT = "medium"
DEFAULT_BINARY = Path("~/.local/bin/agy")
SETTINGS_ENV = "CREME_AGY_SETTINGS"
BINARY_ENV = "CREME_AGY_BIN"


class AntigravityError(RuntimeError):
    """A quota, record, or process failure that prevents a trustworthy run."""


def resolve_model(model: str, effort: str) -> str:
    for suffix in ("-low", "-medium", "-high"):
        if model.endswith(suffix):
            selected = suffix[1:]
            if selected != effort:
                raise ValueError(
                    f"model {model!r} has effort {selected!r}, conflicting with effort {effort!r}"
                )
            return model
    return f"{model}-{effort}"


def pool_for_model(model: str) -> str:
    if model.startswith("gemini-"):
        return "gemini"
    if model.startswith("claude-") or model.startswith("gpt-oss-"):
        return "3p"
    raise ValueError(f"unknown Antigravity model slug: {model}")


def resolve_binary(value: Optional[str] = None) -> Path:
    selected = value or os.environ.get(BINARY_ENV) or str(DEFAULT_BINARY)
    return Path(selected).expanduser()


def _command_json(binary: Path, prompt: str) -> dict:
    try:
        completed = subprocess.run(
            [str(binary), "-p", prompt, "--output-format", "json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AntigravityError(f"agy {prompt} read failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise AntigravityError(f"agy {prompt} read failed: {detail}")
    try:
        value = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise AntigravityError(f"agy {prompt} returned unparsable JSON") from exc
    if not isinstance(value, dict):
        raise AntigravityError(f"agy {prompt} returned a non-object JSON value")
    return value


def _command_data(value: dict, prompt: str) -> dict:
    command = value.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    if not isinstance(data, dict):
        raise AntigravityError(f"agy {prompt} JSON has no command.data object")
    return data


def _settings() -> Optional[bool]:
    path = Path(os.environ.get(SETTINGS_ENV, "~/.gemini/antigravity-cli/settings.json")).expanduser()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AntigravityError(f"Antigravity settings could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise AntigravityError("Antigravity settings are not a JSON object")
    setting = value.get("useG1Credits")
    if setting is None:
        return None
    if type(setting) is not bool:
        raise AntigravityError("Antigravity useG1Credits is not boolean")
    return setting


def read_quota(bin: Any) -> dict:
    """Read both zero-token quota surfaces and the local paid-credit setting."""
    binary = Path(bin).expanduser()
    usage = _command_data(_command_json(binary, "/usage"), "/usage")
    credits_data = _command_data(_command_json(binary, "/credits"), "/credits")
    groups = usage.get("groups")
    if not isinstance(groups, list):
        raise AntigravityError("agy /usage JSON has no groups list")
    pools = {"gemini": {}, "3p": {}}
    group_names = {"Gemini Models": "gemini", "Claude and GPT models": "3p"}
    bucket_ids = {"gemini-weekly": "weekly", "gemini-5h": "5h", "3p-weekly": "weekly", "3p-5h": "5h"}
    for group in groups:
        if not isinstance(group, dict):
            raise AntigravityError("agy /usage contains a malformed group")
        pool = group_names.get(group.get("name"))
        if pool is None:
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            raise AntigravityError(f"agy /usage group {group.get('name')} has no buckets list")
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise AntigravityError("agy /usage contains a malformed bucket")
            window = bucket_ids.get(bucket.get("id"))
            if window is None:
                continue
            fraction = bucket.get("remaining_fraction")
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
                raise AntigravityError(f"agy /usage bucket {bucket.get('id')} has no fraction")
            pools[pool][window] = dict(bucket)
    remaining_credits = credits_data.get("remaining_credits")
    if not isinstance(remaining_credits, (int, float)) or isinstance(remaining_credits, bool):
        raise AntigravityError("agy /credits has no numeric remaining_credits")
    return {
        "pools": pools,
        "remaining_credits": remaining_credits,
        "use_g1_credits": _settings(),
    }


def admission(quota: dict, model: str, floor: float) -> list[str]:
    pool = pool_for_model(model)
    reasons = []
    selected = ((quota.get("pools") or {}).get(pool)) if isinstance(quota, dict) else None
    if not isinstance(selected, dict):
        return [f"{pool} pool is missing"]
    for window in ("5h", "weekly"):
        bucket = selected.get(window)
        if not isinstance(bucket, dict):
            reasons.append(f"{pool} {window} window is missing")
            continue
        fraction = bucket.get("remaining_fraction")
        if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
            reasons.append(f"{pool} {window} remaining fraction is missing")
        elif fraction < floor:
            reasons.append(f"{pool} {window} remaining fraction {fraction} is below floor {floor}")
    credits = quota.get("remaining_credits") if isinstance(quota, dict) else None
    if isinstance(credits, (int, float)) and not isinstance(credits, bool):
        if credits > 0 and quota.get("use_g1_credits") is not False:
            reasons.append("remaining credits are positive while useG1Credits is not false")
    return reasons


def _guard_paths(tool_call: dict) -> list[str]:
    path_keys = {
        "AbsolutePath", "DirectoryPath", "SearchPath", "SearchDirectory", "Path",
        "Directory", "File", "FilePath", "TargetFile",
    }
    args = tool_call.get("args")
    found: list[str] = []

    def collect(value: Any, key: Optional[str] = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, child_key if isinstance(child_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and (key in path_keys or value.startswith(("/", "~/"))):
            found.append(value)

    collect(args)
    return found


def _outside_guard_roots(path: str, roots: tuple[str, ...] | list[str]) -> Optional[str]:
    if not roots:
        return None
    expanded = os.path.expanduser(path)
    # agy resolves a relative tool path against the workspace, i.e. the target (first root).
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.path.expanduser(roots[0]), expanded)
    candidate = os.path.realpath(expanded)
    resolved_roots = [os.path.realpath(os.path.expanduser(root)) for root in roots]
    for root in resolved_roots:
        try:
            if os.path.commonpath((candidate, root)) == root:
                return None
        except ValueError:
            continue
    return candidate


def _guard_argument(tool_call: dict, key: str) -> Optional[str]:
    args = tool_call.get("args")
    found: list[str] = []

    def collect(value: Any, child_key: Optional[str] = None) -> None:
        if isinstance(value, dict):
            for nested_key, child in value.items():
                collect(child, nested_key if isinstance(nested_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                collect(child, child_key)
        elif isinstance(value, str) and child_key == key:
            found.append(value)

    collect(args)
    return found[0] if found else None


UNSAFE_COMMAND_TEXT = ("\n", "\r", "`", "$(")


def _guard_named_paths(tool_call: dict) -> list[str]:
    path_keys = {
        "AbsolutePath", "DirectoryPath", "SearchPath", "SearchDirectory", "Path",
        "Directory", "File", "FilePath", "TargetFile",
    }
    found: list[str] = []

    def collect(value: Any, key: Optional[str] = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, child_key if isinstance(child_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key in path_keys:
            found.append(value)

    collect(tool_call.get("args"))
    return found


def _command_allowed(command: str, allow_commands: tuple[str, ...] | list[str]) -> bool:
    # A newline, carriage return, backtick or $( would let a pattern such as `git add [^;&|]+` smuggle a second command.
    if any(token in command for token in UNSAFE_COMMAND_TEXT):
        return False
    for pattern in allow_commands:
        try:
            if re.fullmatch(pattern, command) is not None:
                return True
        except (re.error, TypeError):
            continue
    return False


def guard_decision(
    payload: dict,
    pinned_model: str,
    roots: tuple[str, ...] | list[str] = (),
    mode: str = "read-only",
    allow_commands: tuple[str, ...] | list[str] = (),
) -> dict:
    model = payload.get("modelName")
    if model != pinned_model:
        return {
            "decision": "deny",
            "reason": f"creme pinned model mismatch: {model!r} != {pinned_model!r}",
        }
    tool_call = payload.get("toolCall")
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    if name in READ_ONLY_TOOLS:
        for path in _guard_paths(tool_call):
            outside = _outside_guard_roots(path, roots)
            if outside is not None:
                return {
                    "decision": "deny",
                    "reason": f"creme read-only run: {name} path outside target: {outside}",
                }
        return {"decision": "allow", "reason": f"creme read-only run: {name} allowed"}
    if mode == "write" and name in WRITE_TOOLS:
        # Named path arguments only: content arguments (CodeContent, ReplacementContent, ...) may start with "/".
        paths = _guard_named_paths(tool_call)
        if not paths:
            return {"decision": "deny", "reason": f"creme write run: {name} path missing: <none>"}
        if not roots:
            return {"decision": "deny", "reason": f"creme write run: {name} path missing root"}
        for path in paths:
            outside = _outside_guard_roots(path, roots)
            if outside is not None:
                return {
                    "decision": "deny",
                    "reason": f"creme write run: {name} path outside target: {outside}",
                }
        return {"decision": "allow", "reason": f"creme write run: {name} allowed"}
    if mode == "write" and name == "run_command":
        command = _guard_argument(tool_call, "CommandLine")
        cwd = _guard_argument(tool_call, "Cwd")
        if command is None:
            command = ""
        if cwd is None:
            cwd = roots[0] if roots else ""
        cwd_outside = _outside_guard_roots(cwd, roots) if roots else cwd
        if cwd_outside is not None or not _command_allowed(command.strip(), allow_commands):
            return {"decision": "deny", "reason": f"command not allowed: {command.strip()}"}
        return {"decision": "allow", "reason": f"command allowed: {command.strip()}"}
    return {"decision": "deny", "reason": f"creme {mode} run: {name} denied"}


GUARD_SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
import sys

READ_ONLY_TOOLS = ("view_file", "list_dir", "grep_search", "find_by_name", "finish")
WRITE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file")
PATH_KEYS = {
    "AbsolutePath", "DirectoryPath", "SearchPath", "SearchDirectory", "Path",
    "Directory", "File", "FilePath", "TargetFile",
}
here = Path(__file__).resolve().parent
guard = json.loads((here / "guard.json").read_text(encoding="utf-8"))
pinned_model = guard["pinned_model"]
roots = guard.get("roots", [])
mode = guard.get("mode", "read-only")
allow_commands = guard.get("allow_commands", [])
payload = json.load(sys.stdin)
model = payload.get("modelName")

def paths_for(tool_call):
    paths = []
    def collect(value, key=None):
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, child_key if isinstance(child_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and (key in PATH_KEYS or value.startswith(("/", "~/"))):
            paths.append(value)
    collect(tool_call.get("args"))
    return paths

def argument(tool_call, wanted):
    found = []
    def collect(value, key=None):
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, child_key if isinstance(child_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key == wanted:
            found.append(value)
    collect(tool_call.get("args"))
    return found[0] if found else None

def outside(path):
    if not roots:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.path.expanduser(roots[0]), expanded)
    candidate = os.path.realpath(expanded)
    for root in roots:
        root_path = os.path.realpath(os.path.expanduser(root))
        try:
            if os.path.commonpath((candidate, root_path)) == root_path:
                return None
        except ValueError:
            pass
    return candidate

def named_paths(tool_call):
    paths = []
    def collect(value, key=None):
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, child_key if isinstance(child_key, str) else None)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key in PATH_KEYS:
            paths.append(value)
    collect(tool_call.get("args"))
    return paths

def command_allowed(command):
    if any(token in command for token in ("\n", "\r", "`", "$(")):
        return False
    for pattern in allow_commands:
        try:
            if re.fullmatch(pattern, command) is not None:
                return True
        except (re.error, TypeError):
            pass
    return False

if model != pinned_model:
    decision = {"decision": "deny", "reason": f"creme pinned model mismatch: {model!r} != {pinned_model!r}"}
else:
    tool_call = payload.get("toolCall")
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    if name in READ_ONLY_TOOLS:
        outside_path = None
        for path in paths_for(tool_call):
            outside_path = outside(path)
            if outside_path is not None:
                break
        if outside_path is not None:
            decision = {"decision": "deny", "reason": f"creme read-only run: {name} path outside target: {outside_path}"}
        else:
            decision = {"decision": "allow", "reason": f"creme read-only run: {name} allowed"}
    elif mode == "write" and name in WRITE_TOOLS:
        paths = named_paths(tool_call)
        if not paths:
            decision = {"decision": "deny", "reason": f"creme write run: {name} path missing: <none>"}
        elif not roots:
            decision = {"decision": "deny", "reason": f"creme write run: {name} path missing root"}
        else:
            outside_path = next((path for path in paths if outside(path) is not None), None)
            if outside_path is not None:
                decision = {"decision": "deny", "reason": f"creme write run: {name} path outside target: {outside(outside_path)}"}
            else:
                decision = {"decision": "allow", "reason": f"creme write run: {name} allowed"}
    elif mode == "write" and name == "run_command":
        command = argument(tool_call, "CommandLine") or ""
        cwd = argument(tool_call, "Cwd")
        cwd = cwd if cwd is not None else (roots[0] if roots else "")
        cwd_outside = outside(cwd) if roots else cwd
        if cwd_outside is not None or not command_allowed(command.strip()):
            decision = {"decision": "deny", "reason": f"command not allowed: {command.strip()}"}
        else:
            decision = {"decision": "allow", "reason": f"command allowed: {command.strip()}"}
    else:
        decision = {"decision": "deny", "reason": f"creme {mode} run: {name} denied"}
with (here.parent / "payloads.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"payload": payload, "decision": decision}, sort_keys=True) + "\n")
print(json.dumps(decision, sort_keys=True))
'''


def _fractions(quota: Optional[dict], pool: str) -> dict:
    selected = ((quota or {}).get("pools") or {}).get(pool) if isinstance(quota, dict) else None
    selected = selected if isinstance(selected, dict) else {}
    return {
        window: (selected.get(window) or {}).get("remaining_fraction")
        if isinstance(selected.get(window), dict) else None
        for window in ("5h", "weekly")
    }


def _run_id() -> str:
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{now}-{secrets.token_hex(3)}"


def _base_summary(model: str, effort: str, pool: str) -> dict:
    family = model
    for suffix in ("-low", "-medium", "-high"):
        if model.endswith(suffix):
            family = model[:-len(suffix)]
            break
    return {
        "verdict": "FAIL",
        "exit": EXIT_FAILED,
        "run": None,
        "run_dir": None,
        "model": model,
        "family": family,
        "mode": "read-only",
        "edited_paths": [],
        "commands_run": [],
        "init_model": None,
        "effort": effort,
        "conversation_id": None,
        "tool_calls": 0,
        "denied": 0,
        "tokens": {},
        "pool": pool,
        "pool_before": {"5h": None, "weekly": None},
        "pool_after": {"5h": None, "weekly": None},
        "reasons": [],
        "warnings": [],
        "last_message": None,
    }


def _lean_goal_command(goal: str) -> str:
    return (
        rf"(?:~|{re.escape(os.path.expanduser('~'))})/creme/scripts/creme lake-build "
        rf"{re.escape(goal)} -- [A-Za-z0-9_.]+(?: [A-Za-z0-9_.]+)*"
    )


def _git_snapshot(target: Path) -> Optional[str]:
    def git(*args: str) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                ["git", "-C", str(target), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    inside = git("rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    records = []
    for label, args in (
        ("HEAD", ("rev-parse", "HEAD")),
        ("git status --porcelain=v1", ("status", "--porcelain=v1")),
        ("git diff --stat", ("diff", "--stat")),
    ):
        result = git(*args)
        output = result.stdout if result is not None and result.returncode == 0 else ""
        records.append(f"{label}\n{output}")
    return "\n".join(records)


def _target_state(target: Path, run_dir: Optional[Path] = None) -> Optional[dict]:
    """HEAD and full porcelain status of a Git target, or None when it is not a Git work tree.

    The guard is the read-only control; this is the fail-closed check that it held: if the hook
    ever failed to load, workspace writes would be auto-allowed and would show up here. The run's own
    record directory is excluded when it lies inside the target (a Creme target holds `.creme/`).
    """
    def git(*args: str) -> Optional[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(target), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout if completed.returncode == 0 else None

    head = git("rev-parse", "HEAD")
    pathspec = ["--", "."]
    if run_dir is not None:
        try:
            pathspec.append(f":(exclude){run_dir.resolve().relative_to(target.resolve())}")
        except ValueError:
            pass
    status = git("status", "--porcelain=v1", "-uall", "--ignored", *pathspec)
    if head is None or status is None:
        return None
    return {"head": head.strip(), "status": status}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(
    brief: str,
    target: Any,
    model: str,
    effort: str,
    timeout_seconds: int,
    runs_root: Optional[Any] = None,
    bin: Optional[Any] = None,
    floor: float = DEFAULT_MIN_REMAINING_FRACTION,
    write: bool = False,
    allow_commands: tuple[str, ...] | list[str] = (),
    lean_goal: Optional[str] = None,
) -> tuple[int, dict]:
    target_path = Path(target).expanduser()
    try:
        resolved_model = resolve_model(model, effort)
    except ValueError as exc:
        summary = _base_summary(model, effort, "unknown")
        summary["reasons"].append(str(exc))
        summary["exit"] = EXIT_USAGE
        return EXIT_USAGE, summary
    try:
        pool = pool_for_model(resolved_model)
    except ValueError as exc:
        summary = _base_summary(model, effort, "unknown")
        summary["reasons"].append(str(exc))
        summary["exit"] = EXIT_USAGE
        return EXIT_USAGE, summary
    summary = _base_summary(resolved_model, effort, pool)
    summary["mode"] = "write" if write else "read-only"
    try:
        command_patterns = tuple(allow_commands)
    except TypeError:
        command_patterns = ()
        summary["reasons"].append("allow_commands must be a sequence of regex strings")
    if not summary["reasons"]:
        for pattern in command_patterns:
            if not isinstance(pattern, str):
                summary["reasons"].append("allow_commands must contain only regex strings")
                break
            try:
                re.compile(pattern)
            except re.error as exc:
                summary["reasons"].append(f"invalid allow-command regex {pattern!r}: {exc}")
                break
    if lean_goal is not None:
        if not isinstance(lean_goal, str) or re.fullmatch(r"[A-Za-z0-9._-]+", lean_goal) is None:
            summary["reasons"].append("lean_goal must match [A-Za-z0-9._-]+")
        else:
            command_patterns = command_patterns + (_lean_goal_command(lean_goal),)
    if summary["reasons"]:
        summary["exit"] = EXIT_USAGE
        return EXIT_USAGE, summary
    if not target_path.is_dir() or effort not in ("low", "medium", "high") or timeout_seconds < 1:
        reasons = []
        if not target_path.is_dir():
            reasons.append(f"target is not an existing directory: {target_path}")
        if effort not in ("low", "medium", "high"):
            reasons.append(f"invalid effort: {effort}")
        if timeout_seconds < 1:
            reasons.append("timeout_seconds must be positive")
        summary["reasons"] = reasons
        summary["exit"] = EXIT_USAGE
        return EXIT_USAGE, summary
    binary = resolve_binary(str(bin) if bin is not None else None)
    try:
        quota_before = read_quota(binary)
    except AntigravityError as exc:
        summary["reasons"].append(str(exc))
        return EXIT_FAILED, summary
    summary["pool_before"] = _fractions(quota_before, pool)
    reasons = admission(quota_before, resolved_model, floor)
    if reasons:
        summary["verdict"] = "REFUSED"
        summary["exit"] = EXIT_PREFLIGHT_REFUSED
        summary["reasons"] = reasons
        return EXIT_PREFLIGHT_REFUSED, summary

    root = Path(runs_root).expanduser() if runs_root is not None else Path(__file__).resolve().parents[1] / ".creme/antigravity/runs"
    run_id = _run_id()
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    agents = run_dir / ".agents"
    agents.mkdir()
    target_path = target_path.resolve()
    (run_dir / "brief.md").write_text(brief, encoding="utf-8")
    (agents / "guard.py").write_text(GUARD_SCRIPT, encoding="utf-8")
    (agents / "guard.py").chmod(0o700)
    _write_json(agents / "guard.json", {
        "pinned_model": resolved_model,
        "roots": [str(target_path)],
        "mode": "write" if write else "read-only",
        "allow_commands": list(command_patterns),
    })
    hooks = {
        "creme-run-guard": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{"command": f"{os.path.abspath(sys.executable)} ./guard.py", "timeout": 10}],
            }],
        },
    }
    _write_json(agents / "hooks.json", hooks)
    _write_json(run_dir / "usage-before.json", quota_before)
    target_before = _target_state(target_path, run_dir)
    git_before = _git_snapshot(target_path) if write else None
    if git_before is not None:
        (run_dir / "git-before.txt").write_text(git_before, encoding="utf-8")
    if target_before is None:
        summary["warnings"].append("target is not a Git work tree; its read-only state is not checked")

    command = [
        str(binary), "-p", brief, "--add-dir", str(target_path), "--add-dir", str(run_dir),
        "--model", resolved_model, "--effort", effort, "--disable-slash-commands",
        "--output-format", "stream-json", "--print-timeout", f"{timeout_seconds}s",
    ]
    if write:
        command.append("--dangerously-skip-permissions")
    stdout = ""
    stderr = ""
    process_exit = EXIT_FAILED
    try:
        process = subprocess.Popen(
            command,
            cwd=str(target_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds + 60)
            process_exit = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            process_exit = EXIT_FAILED
            summary["reasons"].append("agy exceeded wall timeout")
    except OSError as exc:
        summary["reasons"].append(f"could not launch agy: {exc}")
    (run_dir / "events.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / "agy.stderr").write_text(stderr, encoding="utf-8")

    events = []
    for line_number, line in enumerate(stdout.splitlines(), 1):
        try:
            event = json.loads(line)
        except ValueError:
            summary["reasons"].append(f"events.jsonl line {line_number} is not JSON")
            continue
        if not isinstance(event, dict):
            summary["reasons"].append(f"events.jsonl line {line_number} is not an object")
            continue
        events.append(event)
    init_event = next((event for event in events if event.get("event") == "init"), None)
    result_event = next((event for event in events if event.get("event") == "result"), None)
    init_value = (init_event or {}).get("init")
    init_data = init_value if isinstance(init_value, dict) else {}
    result_value = (result_event or {}).get("result")
    result_data = result_value if isinstance(result_value, dict) else {}
    init_model = init_data.get("model")
    summary["init_model"] = init_model
    summary["conversation_id"] = result_data.get("conversation_id") or init_data.get("conversation_id")
    summary["tokens"] = result_data.get("usage") if isinstance(result_data.get("usage"), dict) else {}
    response = result_data.get("response") if isinstance(result_data.get("response"), str) else ""
    last_message = run_dir / "last-message.md"
    last_message.write_text(response, encoding="utf-8")
    summary["last_message"] = str(last_message)
    payloads = run_dir / "payloads.jsonl"
    payload_lines = []
    if payloads.exists():
        for line_number, line in enumerate(payloads.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except ValueError:
                summary["reasons"].append(f"payloads.jsonl line {line_number} is not JSON")
                continue
            if not isinstance(record, dict):
                summary["reasons"].append(f"payloads.jsonl line {line_number} is not an object")
                continue
            payload_lines.append(record)
    summary["tool_calls"] = len(payload_lines)
    summary["denied"] = sum(1 for record in payload_lines if (record.get("decision") or {}).get("decision") == "deny")
    edited_paths = []
    commands_run = []
    for record in payload_lines:
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("modelName") != resolved_model:
            summary["reasons"].append(f"payload modelName is not pinned to {resolved_model}")
            continue
        if (record.get("decision") or {}).get("decision") != "allow":
            continue
        tool_call = payload.get("toolCall")
        if not isinstance(tool_call, dict):
            continue
        name = tool_call.get("name")
        if write and name in WRITE_TOOLS:
            edited_paths.extend(_guard_named_paths(tool_call))
        if write and name == "run_command":
            command = _guard_argument(tool_call, "CommandLine")
            if command is not None:
                commands_run.append(command.strip())
    edited_paths = list(dict.fromkeys(edited_paths))  # named path arguments only, once each
    summary["edited_paths"] = edited_paths
    summary["commands_run"] = commands_run
    if write:
        _write_json(run_dir / "edits.json", edited_paths)
    try:
        quota_after = read_quota(binary)
        _write_json(run_dir / "usage-after.json", quota_after)
        summary["pool_after"] = _fractions(quota_after, pool)
    except AntigravityError as exc:
        summary["warnings"].append(f"could not read after quota: {exc}")

    if write:
        git_after = _git_snapshot(target_path)
        if git_after is not None:
            (run_dir / "git-after.txt").write_text(git_after, encoding="utf-8")
    if not write and target_before is not None and _target_state(target_path, run_dir) != target_before:
        summary["reasons"].append("target Git state changed during a read-only run")
    if process_exit != 0:
        summary["reasons"].append(f"agy exited with status {process_exit}")
    if result_event is None:
        summary["reasons"].append("missing result event")
    elif result_data.get("status") != "SUCCESS":
        summary["reasons"].append(f"result status was {result_data.get('status')}")
    if init_event is None:
        summary["reasons"].append("missing init event")
    elif init_model != resolved_model:
        summary["reasons"].append(f"init model {init_model!r} does not match {resolved_model!r}")
    if not summary["reasons"]:
        summary["verdict"] = "PASS"
        summary["exit"] = EXIT_OK
    summary["run"] = run_id
    summary["run_dir"] = str(run_dir)
    _write_json(run_dir / "verdict.json", summary)
    return summary["exit"], summary


def format_summary(summary: dict) -> str:
    run = summary.get("run") or "-"
    lines = [
        f"verdict={summary.get('verdict')} exit={summary.get('exit')} run={run}",
        f"model={summary.get('model')} init_model={summary.get('init_model')} effort={summary.get('effort')} mode={summary.get('mode')} conversation={summary.get('conversation_id')}",
        f"tokens={json.dumps(summary.get('tokens') or {}, sort_keys=True)}",
        f"{summary.get('pool')} 5h {summary.get('pool_before', {}).get('5h')}->{summary.get('pool_after', {}).get('5h')} weekly {summary.get('pool_before', {}).get('weekly')}->{summary.get('pool_after', {}).get('weekly')}",
        f"edited_paths={json.dumps(summary.get('edited_paths') or [])} commands_run={json.dumps(summary.get('commands_run') or [])}",
        f"last_message={summary.get('last_message')}",
    ]
    prefix = "refused" if summary.get("verdict") == "REFUSED" else "reason"
    lines.extend(f"{prefix}: {reason}" for reason in summary.get("reasons") or [])
    lines.extend(f"warning: {warning}" for warning in summary.get("warnings") or [])
    return "\n".join(lines)
