from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, TextIO

from . import semaphore
from .adapters import get_adapter
from .task_wind_down import _goal_worktree_roots


SCHEMA_VERSION = 1
GUARD_REFUSAL_EXIT = 64
STALE_EXIT = 3
DEFAULT_MEMORY_GIB = 8
DEFAULT_THREADS = 2
RENEW_INTERVAL_SECONDS = 240
RUNTIME_RELATIVE = Path(".creme/lean-build-ownership")
LEDGER_NAME = "ledger.jsonl"
_SAFE_LEDGER_KEYS = {
    "kind", "worktree", "goal", "targets",
    "command", "exit", "wall_seconds", "peak_rss_mib", "swap_before_gib",
    "swap_after_gib", "threads", "admission", "contention", "modules_rebuilt",
    "modules_restored", "module_hashes", "module_seconds", "rewritten",
    "reason", "toolchain", "probe", "renewals", "max_concurrent_lean",
    "peak_lean_rss_mib",
    "sampling_samples", "sampling_unavailable",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_root() -> Path:
    override = os.environ.get("CREME_BUILD_OWNERSHIP_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / RUNTIME_RELATIVE


def ledger_path() -> Path:
    override = os.environ.get("CREME_BUILD_LEDGER")
    return (
        Path(override).expanduser().resolve()
        if override
        else semaphore.canonical_creme_root() / RUNTIME_RELATIVE / LEDGER_NAME
    )


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def append_ledger(record: dict[str, Any]) -> None:
    unexpected = set(record).difference(_SAFE_LEDGER_KEYS)
    if unexpected:
        raise ValueError(f"ledger record contains unsupported fields: {sorted(unexpected)}")
    row = {"schema_version": SCHEMA_VERSION, "time": _iso_now(), **record}
    if not _valid_ledger_row(row):
        raise ValueError("ledger record does not match the versioned schema")
    path = ledger_path()
    _secure_dir(path.parent)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as output:
            output.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            output.flush()
            os.fsync(output.fileno())


def _parse_since(text: str, now: Optional[datetime] = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    match = re.fullmatch(r"([1-9][0-9]*)([dhm])", text)
    if not match:
        raise ValueError("--since must be a positive duration such as 7d, 24h, or 30m")
    value = int(match.group(1))
    unit = match.group(2)
    delta = {"d": timedelta(days=value), "h": timedelta(hours=value), "m": timedelta(minutes=value)}[unit]
    return current - delta


def read_ledger(since: str) -> tuple[list[dict[str, Any]], int]:
    cutoff = _parse_since(since)
    path = ledger_path()
    if not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    corrupt = 0
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                    if not _valid_ledger_row(row):
                        raise ValueError("unsupported row")
                    when = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                    if when >= cutoff:
                        rows.append(row)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    corrupt += 1
    return rows, corrupt


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _number_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _valid_ledger_row(row: Any) -> bool:
    if not isinstance(row, dict) or set(row).difference(_SAFE_LEDGER_KEYS | {"schema_version", "time"}):
        return False
    if row.get("schema_version") != SCHEMA_VERSION or not isinstance(row.get("time"), str):
        return False
    try:
        when = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            return False
    except ValueError:
        return False
    kind = row.get("kind")
    if kind not in {"build", "guard", "guard_refusal"}:
        return False
    common = (
        isinstance(row.get("worktree"), str)
        and isinstance(row.get("goal"), str)
        and _string_list(row.get("targets"))
        and _string_list(row.get("command"))
        and isinstance(row.get("exit"), int)
        and not isinstance(row.get("exit"), bool)
    )
    if not common:
        return False
    if kind != "build":
        return (
            isinstance(row.get("rewritten"), bool)
            and isinstance(row.get("reason"), str)
        )
    required_numbers = ("wall_seconds", "threads")
    if not all(_number_or_none(row.get(key)) for key in required_numbers):
        return False
    if not isinstance(row.get("probe"), bool):
        return False
    if not isinstance(row.get("admission"), str) or not isinstance(row.get("contention"), str):
        return False
    if not _string_list(row.get("modules_rebuilt")) or not _string_list(row.get("modules_restored")):
        return False
    hashes = row.get("module_hashes")
    seconds = row.get("module_seconds")
    if not isinstance(hashes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in hashes.items()):
        return False
    if not isinstance(seconds, dict) or not all(isinstance(k, str) and _number_or_none(v) for k, v in seconds.items()):
        return False
    optional_numbers = (
        "peak_rss_mib", "peak_lean_rss_mib", "max_concurrent_lean",
        "swap_before_gib", "swap_after_gib", "sampling_samples", "sampling_unavailable",
    )
    if not all(key not in row or _number_or_none(row[key]) for key in optional_numbers):
        return False
    return (
        ("toolchain" not in row or isinstance(row["toolchain"], str))
        and ("renewals" not in row or _string_list(row["renewals"]))
    )


def ledger_rollup(since: str) -> dict[str, Any]:
    rows, corrupt = read_ledger(since)
    builds = [row for row in rows if row["kind"] == "build" and not row.get("probe")]
    by_goal: dict[str, float] = {}
    seen_hashes: dict[tuple[str, str, str], str] = {}
    duplicate_seconds = 0.0
    duplicate_pairs = 0
    incomplete_timings = 0
    full: list[float] = []
    narrow: list[float] = []
    for row in builds:
        goal = str(row.get("goal", "<unknown>"))
        wall = float(row.get("wall_seconds") or 0.0)
        seconds = row.get("module_seconds") or {}
        rebuilt = row.get("modules_rebuilt") or []
        measured = sum(float(seconds[module]) for module in rebuilt if module in seconds)
        if rebuilt and any(module not in seconds for module in rebuilt):
            incomplete_timings += 1
        by_goal[goal] = by_goal.get(goal, 0.0) + measured
        (full if not row.get("targets") else narrow).append(wall)
        for module, digest in (row.get("module_hashes") or {}).items():
            key = (str(row.get("toolchain", "<unknown>")), str(module), str(digest))
            worktree = str(row.get("worktree", ""))
            if key in seen_hashes and seen_hashes[key] != worktree:
                duplicate_pairs += 1
                duplicate_seconds += float(seconds.get(module, 0.0))
            else:
                seen_hashes[key] = worktree
    full_seconds = sum(full)
    narrow_seconds = sum(narrow)
    return {
        "status": "OK",
        "since": since,
        "rows": len(rows),
        "corrupt_lines_skipped": corrupt,
        "elaboration_seconds_by_goal": {key: round(value, 3) for key, value in sorted(by_goal.items())},
        "elaboration_timing_incomplete_builds": incomplete_timings,
        "duplicate_hash_pairs": duplicate_pairs,
        "duplicate_hash_seconds": round(duplicate_seconds, 3),
        "guard_refusals": sum(row["kind"] == "guard_refusal" for row in rows),
        "full_build_seconds": round(full_seconds, 3),
        "narrow_build_seconds": round(narrow_seconds, 3),
        "full_vs_narrow_ratio": round(full_seconds / narrow_seconds, 3) if narrow_seconds else None,
    }


def _elan() -> str:
    candidate = Path.home() / ".elan" / "bin" / "elan"
    try:
        mode = candidate.stat().st_mode
    except OSError as exc:
        raise RuntimeError(f"cannot resolve the user-owned Elan manager: {exc}") from exc
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError("the user-owned Elan manager is not executable")
    if candidate.stat().st_uid != os.getuid() or mode & 0o022:
        raise RuntimeError("the Elan manager identity is not private to the current user")
    return str(candidate.resolve())


def trusted_uvx(candidates: Optional[Iterable[Path]] = None) -> Path:
    choices = list(candidates or (
        Path.home() / ".local" / "bin" / "uvx",
        Path.home() / ".cargo" / "bin" / "uvx",
        Path("/opt/homebrew/bin/uvx"),
        Path("/usr/local/bin/uvx"),
        Path("/usr/bin/uvx"),
    ))
    for candidate in choices:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK) and not metadata.st_mode & 0o022:
            return resolved
    raise RuntimeError("no identity-bound, non-writable uvx installation is available")


def resolve_tool(name: str, cwd: Path) -> Path:
    completed = subprocess.run(
        [_elan(), "which", name], cwd=cwd, capture_output=True, text=True, check=False,
    )
    candidate = Path(completed.stdout.strip()).resolve() if completed.returncode == 0 and completed.stdout.strip() else None
    if candidate is None or not candidate.is_file() or not os.access(candidate, os.X_OK):
        detail = completed.stderr.strip() or completed.stdout.strip() or "no executable returned"
        raise RuntimeError(f"cannot resolve toolchain {name}: {detail}")
    return candidate


def _real_sysroot(real_lean: Path, cwd: Path) -> Path:
    completed = subprocess.run(
        [str(real_lean), "--print-prefix"], cwd=cwd, capture_output=True, text=True, check=False,
    )
    prefix = Path(completed.stdout.strip()).resolve() if completed.returncode == 0 and completed.stdout.strip() else None
    if prefix is None or not (prefix / "lib" / "lean").is_dir():
        raise RuntimeError("resolved Lean executable did not report a valid sysroot")
    return prefix


def resolve_toolchain(cwd: Path) -> tuple[Path, Path, Path]:
    real_lake = resolve_tool("lake", cwd)
    real_lean = resolve_tool("lean", cwd)
    sysroot = _real_sysroot(real_lean, cwd)
    expected_lake = (sysroot / "bin" / "lake").resolve()
    expected_lean = (sysroot / "bin" / "lean").resolve()
    if real_lake != expected_lake or real_lean != expected_lean:
        raise RuntimeError(
            "Elan returned incoherent Lake/Lean identities for the selected toolchain"
        )
    return real_lake, real_lean, sysroot


def _launcher_text(entrypoint: str) -> str:
    interpreter = str(Path(sys.executable).resolve())
    source_root = str(Path(__file__).resolve().parents[1])
    if "\n" in interpreter or "\n" in source_root:
        raise RuntimeError("unsafe newline in trusted launcher identity")
    return (
        f"#!{interpreter}\n"
        "import sys\n"
        f"sys.path.insert(0, {source_root!r})\n"
        f"from creme.build_ownership import {entrypoint}\n"
        f"raise SystemExit({entrypoint}(sys.argv[1:]))\n"
    )


def _ensure_launcher(path: Path, entrypoint: str) -> None:
    expected = _launcher_text(entrypoint)
    if path.is_file() and not path.is_symlink():
        try:
            if path.read_text(encoding="utf-8") == expected and os.access(path, os.X_OK):
                return
        except OSError:
            pass
        raise RuntimeError(f"refusing to replace unexpected guard path: {path}")
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"refusing to replace unexpected guard path: {path}")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(expected)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def guard_bin() -> Path:
    root = runtime_root()
    binary = root / "bin"
    _secure_dir(binary)
    _ensure_launcher(binary / "lake", "lake_guard_main")
    _ensure_launcher(binary / "lean", "lean_proxy_main")
    _ensure_launcher(binary / "nice", "nice_main")
    return binary


def guarded_mcp_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(base or os.environ)
    binary = guard_bin()
    env["PATH"] = str(binary) + os.pathsep + env.get("PATH", "")
    return env


def _toolchain_facade(real_lake: Path, real_lean: Path, sysroot: Path) -> Path:
    launchers = guard_bin()
    identity = hashlib.sha256(
        (str(real_lake) + "\0" + str(real_lean) + "\0" + str(sysroot)).encode()
    ).hexdigest()[:16]
    root = runtime_root() / "toolchains" / identity
    lock_path = runtime_root() / "toolchains.lock"
    _secure_dir(lock_path.parent)
    _secure_dir(root.parent)
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if root.exists():
            expected = {
                root / "bin" / "lean": launchers / "lean",
                root / "bin" / "lake": launchers / "lake",
            }
            expected.update({root / name: sysroot / name for name in ("lib", "include", "src") if (sysroot / name).exists()})
            invalid = [str(path) for path, target in expected.items() if not path.is_symlink() or path.resolve() != target.resolve()]
            if invalid:
                raise RuntimeError(f"existing guarded toolchain facade is invalid: {invalid}")
            return root
        staging = Path(tempfile.mkdtemp(prefix=identity + ".", dir=str(root.parent)))
        try:
            (staging / "bin").mkdir(mode=0o700)
            (staging / "bin" / "lean").symlink_to(launchers / "lean")
            (staging / "bin" / "lake").symlink_to(launchers / "lake")
            for source in (sysroot / "bin").iterdir():
                if source.name not in {"lean", "lake"}:
                    (staging / "bin" / source.name).symlink_to(source)
            for name in ("lib", "include", "src"):
                source = sysroot / name
                if source.exists():
                    (staging / name).symlink_to(source)
            os.replace(staging, root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return root


def _guard_event(
    *, cwd: Path, args: list[str], exit_code: int, rewritten: bool, reason: str,
) -> None:
    root, goal = _worktree_identity(cwd)
    append_ledger({
        "kind": "guard_refusal" if exit_code in {STALE_EXIT, GUARD_REFUSAL_EXIT} else "guard",
        "worktree": str(root),
        "goal": goal,
        "targets": [args[1]] if args and args[0] == "setup-file" and len(args) > 1 else [],
        "command": ["lake", *args],
        "exit": exit_code,
        "rewritten": rewritten,
        "reason": reason,
    })


def _apparent_goal(worktree: Path) -> str:
    parts = worktree.resolve().parts
    try:
        index = parts.index(".worktrees")
        return parts[index + 1]
    except (ValueError, IndexError):
        return "<unowned>"


def _worktree_identity(cwd: Path, expected_goal: Optional[str] = None) -> tuple[Path, str]:
    resolved_cwd = cwd.resolve()
    goal = expected_goal or _apparent_goal(resolved_cwd)
    if goal == "<unowned>":
        return resolved_cwd, goal
    try:
        roots = _goal_worktree_roots(goal, get_adapter())
    except Exception:
        return resolved_cwd, "<unowned>"
    matches = []
    for root in roots:
        try:
            resolved_cwd.relative_to(root)
            matches.append(root)
        except ValueError:
            continue
    if len(matches) != 1:
        return resolved_cwd, "<unowned>"
    return matches[0], goal


def lake_guard_main(argv: list[str]) -> int:
    cwd = Path.cwd().resolve()
    args = list(argv)
    if not args:
        _guard_event(cwd=cwd, args=args, exit_code=GUARD_REFUSAL_EXIT, rewritten=False, reason="missing command")
        print("creme lake guard: missing invocation; refusing", file=os.sys.stderr)
        return GUARD_REFUSAL_EXIT
    try:
        real_lake, real_lean, sysroot = resolve_toolchain(cwd)
        if real_lake == Path(os.sys.argv[0]).resolve():
            raise RuntimeError("elan resolved the guard instead of the toolchain lake")
    except RuntimeError as exc:
        _guard_event(cwd=cwd, args=args, exit_code=GUARD_REFUSAL_EXIT, rewritten=False, reason=str(exc))
        print(f"creme lake guard: {exc}", file=os.sys.stderr)
        return GUARD_REFUSAL_EXIT

    command = args[0]
    if command == "serve":
        try:
            facade = _toolchain_facade(real_lake, real_lean, sysroot)
        except (OSError, RuntimeError) as exc:
            _guard_event(cwd=cwd, args=args, exit_code=GUARD_REFUSAL_EXIT, rewritten=False, reason=str(exc))
            print(f"creme lake guard: cannot construct guarded serve environment: {exc}", file=os.sys.stderr)
            return GUARD_REFUSAL_EXIT
        env = os.environ.copy()
        env.update({
            "LEAN_SYSROOT": str(facade),
            "LEAN": str(facade / "bin" / "lean"),
            "LAKE_OVERRIDE_LEAN": "1",
            "CREME_REAL_LEAN": str(real_lean),
            "CREME_REAL_SYSROOT": str(sysroot),
            "CREME_LAKE_GUARD": str(guard_bin() / "lake"),
        })
        os.execve(real_lake, [str(real_lake), *args], env)

    if command == "setup-file":
        rewritten = "--no-build" not in args or "--no-cache" not in args
        guarded = list(args)
        if "--no-build" not in guarded:
            guarded.append("--no-build")
        if "--no-cache" not in guarded:
            guarded.append("--no-cache")
        completed = subprocess.run([str(real_lake), *guarded], cwd=cwd, check=False)
        _guard_event(
            cwd=cwd, args=guarded, exit_code=completed.returncode, rewritten=rewritten,
            reason="setup-file forced to no-build/no-cache" if rewritten else "already guarded setup-file",
        )
        return completed.returncode

    if command in {"--version", "-h", "--help", "help"}:
        return subprocess.run([str(real_lake), *args], cwd=cwd, check=False).returncode

    reason = f"unowned or unknown lake invocation: {' '.join(args)}"
    _guard_event(cwd=cwd, args=args, exit_code=GUARD_REFUSAL_EXIT, rewritten=False, reason=reason)
    print(f"creme lake guard: {reason}; use `python3 -m creme lake-build ...` for builds", file=os.sys.stderr)
    return GUARD_REFUSAL_EXIT


def lean_proxy_main(argv: list[str]) -> int:
    real = os.environ.get("CREME_REAL_LEAN")
    sysroot = os.environ.get("CREME_REAL_SYSROOT")
    guard = os.environ.get("CREME_LAKE_GUARD")
    if not real or not sysroot or not guard or not argv or argv[0] != "--server":
        print("creme lean proxy: incomplete guarded environment; refusing", file=os.sys.stderr)
        return GUARD_REFUSAL_EXIT
    real_path = Path(real).resolve()
    sysroot_path = Path(sysroot).resolve()
    guard_path = Path(guard).resolve()
    try:
        expected_guard = (guard_bin() / "lake").resolve()
    except (OSError, RuntimeError):
        expected_guard = Path("/__creme_guard_unavailable__")
    if (
        not real_path.is_file()
        or not (sysroot_path / "lib" / "lean").is_dir()
        or real_path != (sysroot_path / "bin" / "lean").resolve()
        or not guard_path.is_file()
        or guard_path != expected_guard
    ):
        print("creme lean proxy: guarded executables are unavailable; refusing", file=os.sys.stderr)
        return GUARD_REFUSAL_EXIT
    env = os.environ.copy()
    env.update({"LEAN": str(real_path), "LEAN_SYSROOT": str(sysroot_path), "LAKE": str(guard_path)})
    env.pop("LAKE_OVERRIDE_LEAN", None)
    os.execve(real_path, [str(real_path), *argv], env)


def nice_main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[:2] != ["-n", "10"]:
        print("creme priority launcher: expected `-n 10 EXECUTABLE ...`; refusing", file=os.sys.stderr)
        return GUARD_REFUSAL_EXIT
    executable = Path(argv[2])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        print("creme priority launcher: executable identity is invalid; refusing", file=os.sys.stderr)
        return GUARD_REFUSAL_EXIT
    try:
        os.nice(10)
    except OSError as exc:
        print(f"creme priority launcher: cannot apply niceness: {exc}; refusing", file=os.sys.stderr)
        return GUARD_REFUSAL_EXIT
    os.execv(str(executable), [str(executable), *argv[3:]])


def _swap_gib() -> Optional[float]:
    try:
        result = get_adapter().memory_headroom()
        value = result.data.get("swap_used_gib") if result.data else None
        return round(float(value), 3) if value is not None else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _process_snapshot() -> Optional[dict[int, tuple[int, int, str]]]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss=,comm="], capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    rows: dict[int, tuple[int, int, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) == 4:
            try:
                rows[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3])
            except ValueError:
                continue
    return rows


def _descendants(root_pid: int, rows: dict[int, tuple[int, int, str]]) -> set[int]:
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _, _) in rows.items():
            if parent in found and pid not in found:
                found.add(pid)
                changed = True
    return found


class ProcessSampler(threading.Thread):
    def __init__(self, pid: int, interval: float = 0.5):
        super().__init__(daemon=True)
        self.pid = pid
        self.interval = interval
        self.stop_event = threading.Event()
        self.peak_rss_mib = 0.0
        self.max_concurrent_lean = 0
        self.peak_lean_rss_mib = 0.0
        self.samples = 0
        self.unavailable_samples = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            rows = _process_snapshot()
            if rows is None:
                self.unavailable_samples += 1
                self.stop_event.wait(self.interval)
                continue
            self.samples += 1
            pids = _descendants(self.pid, rows)
            rss_kib = sum(rows[pid][1] for pid in pids if pid in rows)
            lean_count = sum(Path(rows[pid][2]).name == "lean" for pid in pids if pid in rows)
            lean_rss_kib = max(
                (rows[pid][1] for pid in pids if pid in rows and Path(rows[pid][2]).name == "lean"),
                default=0,
            )
            self.peak_rss_mib = max(self.peak_rss_mib, rss_kib / 1024.0)
            self.max_concurrent_lean = max(self.max_concurrent_lean, lean_count)
            self.peak_lean_rss_mib = max(self.peak_lean_rss_mib, lean_rss_kib / 1024.0)
            self.stop_event.wait(self.interval)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=max(2.0, self.interval * 3))


_JOB_RE = re.compile(r"\b(Built|Replayed|Fetched)\s+([A-Za-z0-9_'.]+)(?:\.(?:olean|ilean))?.*?(?:\(([0-9.]+)(ms|s)\))?$")


def _parse_build_output(lines: Iterable[str]) -> tuple[list[str], list[str], dict[str, float]]:
    rebuilt: list[str] = []
    restored: list[str] = []
    seconds: dict[str, float] = {}
    for line in lines:
        match = _JOB_RE.search(line)
        if not match:
            continue
        action, module, value, unit = match.groups()
        (rebuilt if action == "Built" else restored).append(module)
        if value:
            seconds[module] = float(value) / 1000.0 if unit == "ms" else float(value)
    return sorted(set(rebuilt)), sorted(set(restored)), seconds


def _module_hashes(worktree: Path, modules: Iterable[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for module in modules:
        relative = Path(*module.split("."))
        candidates = list((worktree / ".lake" / "build").glob(f"**/lean/{relative}.trace"))
        for path in candidates:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                digest = row.get("depHash")
                if isinstance(digest, str):
                    hashes[module] = digest
                    break
            except (OSError, json.JSONDecodeError):
                continue
    return hashes


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _terminate_process_group(proc: subprocess.Popen[str], timeout: float = 10.0) -> bool:
    """Stop the wrapper-owned process group and prove it is gone."""
    pgid = proc.pid
    if _process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
    deadline = time.monotonic() + timeout
    while _process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _process_group_alive(pgid)


class RenewalThread(threading.Thread):
    def __init__(self, goal: str, proc: subprocess.Popen[str], interval: int = RENEW_INTERVAL_SECONDS):
        super().__init__(daemon=True)
        self.goal = goal
        self.proc = proc
        self.interval = interval
        self.stop_event = threading.Event()
        self.verdicts: list[str] = []
        self.refused = False
        self.cleanup_proved = True

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                ok, detail = semaphore.renew(self.goal, semaphore.ADAPTIVE_LEASE_SECONDS)
            except Exception as exc:
                ok = False
                detail = f"renewal raised {type(exc).__name__}"
            self.verdicts.append(("OK: " if ok else "REFUSED: ") + detail)
            if not ok:
                self.refused = True
                self.cleanup_proved = _terminate_process_group(self.proc)
                return

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=2)


def run_lake_build(
    goal: str,
    targets: list[str],
    *,
    memory_gib: int = DEFAULT_MEMORY_GIB,
    contention: str = "sensitive",
    threads: int = DEFAULT_THREADS,
    probe: bool = False,
    stdout: Optional[TextIO] = None,
) -> int:
    output = stdout or os.sys.stdout
    cwd = Path.cwd().resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", goal):
        print(json.dumps({"status": "REFUSED", "detail": "goal label must be a simple stable identifier"}, sort_keys=True), file=output)
        return 2
    if any(not target or target.startswith("-") for target in targets):
        print(json.dumps({"status": "REFUSED", "detail": "targets must be explicit Lake target names, not options"}, sort_keys=True), file=output)
        return 2
    worktree, actual_goal = _worktree_identity(cwd, goal)
    if actual_goal != goal:
        print(json.dumps({
            "status": "REFUSED",
            "detail": f"build cwd belongs to goal {actual_goal!r}, not {goal!r}",
        }, sort_keys=True), file=output)
        return 2
    try:
        real_lake, _, _ = resolve_toolchain(worktree)
    except RuntimeError as exc:
        print(json.dumps({"status": "REFUSED", "detail": str(exc)}, sort_keys=True), file=output)
        return GUARD_REFUSAL_EXIT
    lake_args = [str(real_lake), "build"]
    if probe:
        lake_args.append("--no-build")
    else:
        lake_args.append("--verbose")
    lake_args.extend(targets)
    if probe:
        started = time.monotonic()
        completed = subprocess.run(lake_args, cwd=worktree, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        wall = time.monotonic() - started
        print(completed.stdout, end="", file=output)
        append_ledger({
            "kind": "build", "worktree": str(worktree), "goal": goal, "targets": targets,
            "command": lake_args, "exit": completed.returncode, "wall_seconds": round(wall, 3),
            "threads": threads, "probe": True, "admission": "NOT_REQUIRED_NO_BUILD",
            "contention": contention, "modules_rebuilt": [], "modules_restored": [],
            "module_hashes": {}, "module_seconds": {},
        })
        state = "fresh" if completed.returncode == 0 else "stale" if completed.returncode == STALE_EXIT else "error"
        print(json.dumps({"status": state.upper(), "exit": completed.returncode}, sort_keys=True), file=output)
        return completed.returncode

    admitted, admission = semaphore.adaptive_acquire(
        goal,
        "classified lake build",
        semaphore.ADAPTIVE_LEASE_SECONDS,
        memory_gib=memory_gib,
        contention=contention,
    )
    if not admitted:
        print(json.dumps({"status": "REFUSED", "admission": admission}, sort_keys=True), file=output)
        return 2
    try:
        priority_launcher = guard_bin() / "nice"
    except (OSError, RuntimeError) as exc:
        semaphore.adaptive_release(goal)
        print(json.dumps({"status": "REFUSED", "detail": f"priority launcher is unavailable: {exc}"}, sort_keys=True), file=output)
        return GUARD_REFUSAL_EXIT
    args = [str(priority_launcher), "-n", "10", *lake_args]
    before = _swap_gib()
    started = time.monotonic()
    env = os.environ.copy()
    env["LEAN_NUM_THREADS"] = str(threads)
    proc: Optional[subprocess.Popen[str]] = None
    sampler: Optional[ProcessSampler] = None
    renewer: Optional[RenewalThread] = None
    lines: list[str] = []
    exit_code = 1
    interrupted = False
    cleanup_proved = True
    termination_signal: Optional[int] = None
    prior_handlers: dict[int, Any] = {}

    def request_termination(signum: int, _frame: Any) -> None:
        nonlocal termination_signal
        termination_signal = signum
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGHUP):
            prior_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_termination)
    try:
        proc = subprocess.Popen(
            args, cwd=worktree, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        sampler = ProcessSampler(proc.pid)
        sampler.start()
        renewer = RenewalThread(goal, proc)
        renewer.start()
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            print(line, end="", file=output)
        exit_code = proc.wait()
        if renewer.refused:
            exit_code = exit_code or 2
            cleanup_proved = renewer.cleanup_proved
    except KeyboardInterrupt:
        interrupted = True
        exit_code = 128 + termination_signal if termination_signal else 130
    finally:
        if proc is not None and _process_group_alive(proc.pid):
            cleanup_proved = _terminate_process_group(proc) and cleanup_proved
        if sampler:
            sampler.stop()
        if renewer:
            renewer.stop()
    wall = time.monotonic() - started
    after = _swap_gib()
    rebuilt, restored, module_seconds = _parse_build_output(lines)
    hashes = _module_hashes(worktree, rebuilt)
    record = {
        "kind": "build", "worktree": str(worktree), "goal": goal, "targets": targets,
        "command": args, "exit": exit_code, "wall_seconds": round(wall, 3),
        "peak_rss_mib": round(sampler.peak_rss_mib, 1) if sampler and sampler.samples else None,
        "peak_lean_rss_mib": round(sampler.peak_lean_rss_mib, 1) if sampler and sampler.samples else None,
        "max_concurrent_lean": sampler.max_concurrent_lean if sampler and sampler.samples else None,
        "sampling_samples": sampler.samples if sampler else 0,
        "sampling_unavailable": sampler.unavailable_samples if sampler else 0,
        "swap_before_gib": before, "swap_after_gib": after, "threads": threads,
        "probe": False, "admission": admission, "contention": contention,
        "modules_rebuilt": rebuilt, "modules_restored": restored,
        "module_hashes": hashes, "module_seconds": module_seconds,
        "toolchain": str(real_lake), "renewals": renewer.verdicts if renewer else [],
    }
    if cleanup_proved:
        released, release_detail = semaphore.adaptive_release(goal)
        if not released:
            print(json.dumps({"status": "RELEASE_FAILED", "detail": release_detail}, sort_keys=True), file=output)
            exit_code = exit_code or 2
            record["exit"] = exit_code
    else:
        print(json.dumps({
            "status": "HOLD_PRESERVED", "detail": "could not prove the Lake process group exited",
        }, sort_keys=True), file=output)
        exit_code = exit_code or 2
        record["exit"] = exit_code
    for signum, handler in prior_handlers.items():
        signal.signal(signum, handler)
    append_ledger(record)
    print(json.dumps({
        "status": "OK" if exit_code == 0 else "ERROR", "exit": exit_code,
        "wall_seconds": round(wall, 3), "peak_rss_mib": round(sampler.peak_rss_mib, 1) if sampler and sampler.samples else None,
        "peak_lean_rss_mib": round(sampler.peak_lean_rss_mib, 1) if sampler and sampler.samples else None,
        "max_concurrent_lean": sampler.max_concurrent_lean if sampler and sampler.samples else None,
        "sampling_samples": sampler.samples if sampler else 0,
        "sampling_unavailable": sampler.unavailable_samples if sampler else 0,
        "modules_rebuilt": len(rebuilt), "modules_restored": len(restored), "admission": admission,
        "interrupted": interrupted,
    }, sort_keys=True), file=output)
    return exit_code
