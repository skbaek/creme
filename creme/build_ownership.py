from __future__ import annotations

import fcntl
import hashlib
import math
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
from .profile import load_admission_settings
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
    "outcome", "toolchain_digest", "manifest_digest",
    "requested_contention", "evidence_contention", "estimate_source",
    "memory_gib", "dependency", "dependency_rev", "census",
    # Additive, from creme-admission-visibility-v1: why a class was chosen,
    # what the probe measured, what the estimate proposed, and the hint the
    # build emitted.  A reader that does not know these keys skips the row and
    # counts it; the ledger itself is never rewritten.
    "evidence_reason", "resolved_roots", "stale_modules", "stale_detail",
    "estimate_gib", "estimate_under_cover_gib", "hint",
}
SANCTIONED_WORKTREE_SUFFIXES = ("control", "mutation", "rehearsal")


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _iso_now() -> str:
    return _iso(datetime.now(timezone.utc))


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


def _parse_instant(text: str, option: str, now: Optional[datetime] = None) -> datetime:
    """Accept a relative duration or an absolute UTC date/timestamp.

    A return watch compares a fixed historical window against a live one, so
    the roll-up has to name an exact boundary as well as "the last 5 hours".
    """
    current = now or datetime.now(timezone.utc)
    match = re.fullmatch(r"([1-9][0-9]*)([dhm])", text)
    if match:
        value = int(match.group(1))
        delta = {
            "d": timedelta(days=value),
            "h": timedelta(hours=value),
            "m": timedelta(minutes=value),
        }[match.group(2)]
        return current - delta
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{option} must be a positive duration such as 7d, 24h, or 30m, "
            "or an absolute UTC instant such as 2026-09-03 or 2026-09-03T05:35:00Z"
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_since(text: str, now: Optional[datetime] = None) -> datetime:
    return _parse_instant(text, "--since", now)


def parse_window(
    since: str,
    until: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[datetime, Optional[datetime]]:
    start = _parse_instant(since, "--since", now)
    stop = _parse_instant(until, "--until", now) if until else None
    if stop is not None and stop <= start:
        raise ValueError("--until must be later than --since")
    return start, stop


def read_ledger(
    since: str,
    until: Optional[str] = None,
) -> tuple[list[dict[str, Any]], int]:
    cutoff, stop = parse_window(since, until)
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
                    if when >= cutoff and (stop is None or when <= stop):
                        rows.append(row)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    corrupt += 1
    return rows, corrupt


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _number_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _valid_ledger_row(row: Any) -> bool:
    # Unknown keys are tolerated so a later writer's additive field cannot make
    # this reader treat a whole window of rows as corrupt.  Every key this
    # reader *uses* is still validated below.
    if not isinstance(row, dict):
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
        "stale_modules", "estimate_gib", "estimate_under_cover_gib",
    )
    if not all(key not in row or _number_or_none(row[key]) for key in optional_numbers):
        return False
    optional_strings = (
        "toolchain", "outcome", "toolchain_digest", "manifest_digest",
        "requested_contention", "evidence_contention", "estimate_source",
        "dependency", "dependency_rev",
        "evidence_reason", "stale_detail", "hint",
    )
    if not all(key not in row or isinstance(row[key], str) for key in optional_strings):
        return False
    if "census" in row and not isinstance(row["census"], bool):
        return False
    if "memory_gib" in row and not _number_or_none(row["memory_gib"]):
        return False
    if "resolved_roots" in row and not (
        row["resolved_roots"] is None or _string_list(row["resolved_roots"])
    ):
        return False
    return "renewals" not in row or _string_list(row["renewals"])


# `wait-acquire` is the outcome of a queued request and decides a lock-out
# exactly as a direct acquisition does. `wait-enqueue` is not a decision.
ACQUIRE_ACTIONS = ("adaptive-acquire", "soft-acquire", "hard-acquire", "wait-acquire")
_VERDICT_TOKEN = re.compile(r"^([A-Z][A-Z_]*):")


def _verdict_token(row: dict[str, Any]) -> str:
    match = _VERDICT_TOKEN.match(str(row.get("detail", "")))
    return match.group(1) if match else "UNCLASSIFIED"


def coordination_rollup(
    since: datetime,
    until: Optional[datetime],
    labels: Iterable[str] = (),
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Per-goal refusal counts and lock-out seconds from the semaphore log.

    A lock-out episode runs from an acquisition refusal to that label's next
    admission.  An episode with no admission before the window closes is
    reported open with the seconds accrued so far; reporting it as zero would
    make the worst case look like the best one.
    """
    rows, corrupt, status = semaphore.read_log(since, until)
    window_end = until or (rows[-1]["when"] if rows else since)
    by_label: dict[str, dict[str, Any]] = {}

    def entry(label: str) -> dict[str, Any]:
        return by_label.setdefault(label, {
            "refusals": {},
            "renew_refusals": {},
            "admissions": 0,
            "lockout_episodes": 0,
            "lockout_seconds": 0.0,
            "lockout_open": False,
            "lockout_open_seconds": 0.0,
        })

    for label in labels:
        entry(str(label))

    open_since: dict[str, datetime] = {}
    for row in rows:
        label = str(row["label"])
        action = str(row["action"])
        token = _verdict_token(row)
        if action == "renew":
            if row["verdict"] == "REFUSED":
                record = entry(label)["renew_refusals"]
                record[token] = record.get(token, 0) + 1
            continue
        if action not in ACQUIRE_ACTIONS:
            continue
        record = entry(label)
        if row["verdict"] == "REFUSED":
            counts = record["refusals"]
            counts[token] = counts.get(token, 0) + 1
            open_since.setdefault(label, row["when"])
        else:
            record["admissions"] += 1
            started = open_since.pop(label, None)
            if started is not None:
                record["lockout_episodes"] += 1
                record["lockout_seconds"] += (row["when"] - started).total_seconds()

    for label, started in open_since.items():
        record = entry(label)
        record["lockout_open"] = True
        record["lockout_episodes"] += 1
        record["lockout_open_seconds"] = max(0.0, (window_end - started).total_seconds())

    for record in by_label.values():
        record["lockout_seconds"] = round(record["lockout_seconds"], 1)
        record["lockout_open_seconds"] = round(record["lockout_open_seconds"], 1)
        record["lockout_total_seconds"] = round(
            record["lockout_seconds"] + record["lockout_open_seconds"], 1
        )
    meta = {
        "semaphore_log_status": status,
        "semaphore_log_rows": len(rows),
        "semaphore_log_corrupt_lines_skipped": corrupt,
        "window_end": (window_end.isoformat().replace("+00:00", "Z")) if rows or until else None,
    }
    return by_label, meta


def ledger_rollup(since: str, until: Optional[str] = None) -> dict[str, Any]:
    rows, corrupt = read_ledger(since, until)
    window_since, window_until = parse_window(since, until)
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

    coordination, coordination_meta = coordination_rollup(
        window_since,
        window_until,
        {str(row.get("goal", "<unknown>")) for row in builds},
    )
    per_goal: dict[str, dict[str, Any]] = {}
    for goal in sorted(set(by_goal) | set(coordination)):
        goal_builds = [row for row in builds if str(row.get("goal", "<unknown>")) == goal]
        failed = [row for row in goal_builds if row["exit"] != 0]
        classes: dict[str, int] = {}
        for row in goal_builds:
            key = str(row.get("contention", "<unknown>"))
            classes[key] = classes.get(key, 0) + 1
        coordinated = coordination.get(goal, {})
        per_goal[goal] = {
            "builds": len(goal_builds),
            "failed_builds": len(failed),
            "failed_builds_exit_1": sum(row["exit"] == 1 for row in goal_builds),
            "failed_build_share": (
                round(len(failed) / len(goal_builds), 3) if goal_builds else None
            ),
            "contention_class": dict(sorted(classes.items())),
            "elaboration_seconds": round(by_goal.get(goal, 0.0), 3),
            "refusals": dict(sorted(coordinated.get("refusals", {}).items())),
            "renew_refusals": dict(sorted(coordinated.get("renew_refusals", {}).items())),
            "admissions": coordinated.get("admissions", 0),
            "lockout_episodes": coordinated.get("lockout_episodes", 0),
            "lockout_seconds": coordinated.get("lockout_seconds", 0.0),
            "lockout_open": coordinated.get("lockout_open", False),
            "lockout_open_seconds": coordinated.get("lockout_open_seconds", 0.0),
            "lockout_total_seconds": coordinated.get("lockout_total_seconds", 0.0),
        }
    all_classes: dict[str, int] = {}
    for row in builds:
        key = str(row.get("contention", "<unknown>"))
        all_classes[key] = all_classes.get(key, 0) + 1
    failed_builds = sum(row["exit"] != 0 for row in builds)
    return {
        "status": "OK",
        "since": since,
        "until": until,
        "window": {
            "since": window_since.isoformat().replace("+00:00", "Z"),
            "until": window_until.isoformat().replace("+00:00", "Z") if window_until else None,
        },
        "rows": len(rows),
        "corrupt_lines_skipped": corrupt,
        **coordination_meta,
        "builds": len(builds),
        "failed_builds": failed_builds,
        "failed_build_share": round(failed_builds / len(builds), 3) if builds else None,
        "contention_class": dict(sorted(all_classes.items())),
        "by_goal": per_goal,
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


def split_worktree_suffix(directory: str) -> tuple[str, Optional[str]]:
    """Split ``GOAL-control`` into its goal and sanctioned purpose.

    A disposable control, mutation, or rehearsal tree is the same goal's work;
    refusing it only pushed destructive experiments back into the goal
    worktree.  Any other suffix stays unowned.
    """
    for suffix in SANCTIONED_WORKTREE_SUFFIXES:
        marker = f"-{suffix}"
        if directory.endswith(marker) and len(directory) > len(marker):
            return directory[: -len(marker)], suffix
    return directory, None


def _worktree_identity(cwd: Path, expected_goal: Optional[str] = None) -> tuple[Path, str]:
    resolved_cwd = cwd.resolve()
    directory = _apparent_goal(resolved_cwd)
    if directory == "<unowned>":
        return resolved_cwd, directory
    base, suffix = split_worktree_suffix(directory)
    goal = base if suffix else directory
    if expected_goal is not None and goal != expected_goal:
        return resolved_cwd, "<unowned>"
    try:
        roots = _goal_worktree_roots(directory, get_adapter())
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
    print(f"creme lake guard: {reason}; use `~/creme/scripts/creme lake-build ...` for builds", file=os.sys.stderr)
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
        # Adapters report swap in MiB; a GiB lookup silently recorded None on
        # every row, which would have made the memory-pressure column of a
        # return watch unusable.
        value = result.data.get("swap_used_mib") if result.data else None
        return round(float(value) / 1024.0, 3) if value is not None else None
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


def _digest_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def worktree_digests(worktree: Path) -> tuple[Optional[str], Optional[str]]:
    """Digest the two inputs that make an older measurement comparable."""
    return (
        _digest_file(worktree / "lean-toolchain"),
        _digest_file(worktree / "lake-manifest.json"),
    )


_STALE_FAILURE_RE = re.compile(r"^\s*-\s+([A-Za-z0-9_'.]+)\s*$")
_IMPORT_RE = re.compile(r"^import\s+([A-Za-z0-9_'.]+)")
_HEADER_SCAN_LINES = 400


def _module_name(worktree: Path, path: Path) -> str:
    relative = path.relative_to(worktree).with_suffix("")
    return ".".join(relative.parts)


def package_import_graph(worktree: Path, roots: Iterable[str]) -> Optional[dict[str, set[str]]]:
    """Map each in-package module to the in-package modules it imports.

    Only sources inside the worktree are read: dependency packages are Git
    pinned, so their artifacts are either current or would themselves appear
    in the probe's out-of-date frontier.
    """
    graph: dict[str, set[str]] = {}
    prefixes = {str(root).split(".", 1)[0] for root in roots}
    if not prefixes:
        return None
    try:
        for prefix in sorted(prefixes):
            candidates = [worktree / f"{prefix}.lean"]
            directory = worktree / prefix
            if directory.is_dir():
                candidates.extend(sorted(directory.rglob("*.lean")))
            for path in candidates:
                if not path.is_file():
                    continue
                module = _module_name(worktree, path)
                imports: set[str] = set()
                with path.open(encoding="utf-8", errors="replace") as source:
                    for index, line in enumerate(source):
                        match = _IMPORT_RE.match(line)
                        if match:
                            imports.add(match.group(1))
                            continue
                        stripped = line.strip()
                        if not stripped or stripped.startswith("--"):
                            continue
                        # Imports may only appear in the header, but the header
                        # may open with a block comment, so the scan ends at
                        # the first declaration *after* an import was seen.
                        if imports or index >= _HEADER_SCAN_LINES:
                            break
                graph[module] = imports
    except OSError:
        return None
    return {module: {name for name in imports if name in graph} for module, imports in graph.items()}


_PACKAGE_RE = re.compile(r"^\s*package\s+[«\"]?([A-Za-z0-9_'.\-]+)[»\"]?", re.M)
_TARGET_RE = re.compile(
    r"(?P<default>@\[[^\]]*default_target[^\]]*\]\s*)?"
    r"^\s*(?P<kind>lean_lib|lean_exe)\s+[«\"]?(?P<name>[A-Za-z0-9_'.\-]+)[»\"]?",
    re.M,
)
_ROOT_RE = re.compile(r"^\s*roots?\s*:=\s*(?P<value>.+)$", re.M)
_ROOT_NAME_RE = re.compile(r"`+([A-Za-z0-9_'.]+)")


def _lean_lakefile_targets(source: str) -> dict[str, Any]:
    """Read package name, targets, roots, and default targets from Lean DSL."""
    package = _PACKAGE_RE.search(source)
    targets: dict[str, dict[str, Any]] = {}
    defaults: list[str] = []
    matches = list(_TARGET_RE.finditer(source))
    for index, match in enumerate(matches):
        name = match.group("name")
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        body = source[match.end():stop]
        roots = [name]
        root_match = _ROOT_RE.search(body)
        if root_match:
            named = _ROOT_NAME_RE.findall(root_match.group("value"))
            if named:
                roots = named
        targets[name] = {"kind": match.group("kind"), "roots": roots}
        if match.group("default"):
            defaults.append(name)
    return {
        "package": package.group(1) if package else None,
        "targets": targets,
        "default_targets": defaults,
    }


def _toml_lakefile_targets(source: str) -> dict[str, Any]:
    import tomllib

    data = tomllib.loads(source)
    targets: dict[str, dict[str, Any]] = {}
    for kind, key in (("lean_lib", "lean_lib"), ("lean_exe", "lean_exe")):
        for entry in data.get(key) or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            declared = entry.get("roots") or entry.get("root") or name
            roots = [declared] if isinstance(declared, str) else [
                item for item in declared if isinstance(item, str)
            ]
            targets[name] = {"kind": kind, "roots": roots or [name]}
    declared_defaults = data.get("defaultTargets") or []
    defaults = [name for name in declared_defaults if isinstance(name, str)]
    if not defaults:
        defaults = sorted(targets)
    return {
        "package": data.get("name") if isinstance(data.get("name"), str) else None,
        "targets": targets,
        "default_targets": defaults,
    }


def lake_configuration(worktree: Path) -> tuple[Optional[dict[str, Any]], str]:
    """Read the package's declared targets, or say why they are unreadable.

    A full or package target names no module, so its stale closure cannot be
    computed until the configuration says which module roots it builds.  The
    configuration is only ever read: an unreadable or empty one leaves the
    caller with no roots, which is what keeps such a build `sensitive`.
    """
    for name, parse in (
        ("lakefile.toml", _toml_lakefile_targets),
        ("lakefile.lean", _lean_lakefile_targets),
    ):
        path = worktree / name
        if not path.is_file():
            continue
        try:
            config = parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:                       # noqa: BLE001 - fail closed
            return None, f"{name} is unreadable: {type(exc).__name__}"
        if not config["targets"]:
            return None, f"{name} declares no lean_lib or lean_exe target"
        if not config["default_targets"]:
            return None, f"{name} declares no default target"
        return config, f"{name} declares {len(config['targets'])} target(s)"
    return None, "no lakefile.toml or lakefile.lean in the worktree"


def resolve_target_roots(
    worktree: Path, targets: list[str]
) -> tuple[Optional[list[str]], list[str], str]:
    """Map Lake targets to the module roots a build of them would elaborate.

    Returns the closure roots, the package-wide roots the import graph is built
    over, and a human detail.  ``None`` roots means the resolution failed and
    the caller must keep the conservative class.
    """
    config, detail = lake_configuration(worktree)
    if config is None:
        if targets:
            # A named module target needs no configuration: it is its own root.
            return list(targets), list(targets), f"module targets ({detail})"
        return None, [], f"roots unresolved: {detail}"
    package_roots: list[str] = []
    for entry in config["targets"].values():
        for root in entry["roots"]:
            if root not in package_roots:
                package_roots.append(root)

    def roots_of(names: list[str]) -> list[str]:
        collected: list[str] = []
        for name in names:
            for root in config["targets"][name]["roots"]:
                if root not in collected:
                    collected.append(root)
        return collected

    if not targets:
        resolved = roots_of(config["default_targets"])
        if not resolved:
            return None, package_roots, "roots unresolved: no default target has a root"
        return resolved, package_roots, (
            f"full target -> default target(s) {config['default_targets']} "
            f"-> root(s) {resolved}"
        )
    resolved = []
    named: list[str] = []
    for target in targets:
        if target in config["targets"]:
            named.append(f"{target} -> {config['targets'][target]['roots']}")
            for root in config["targets"][target]["roots"]:
                if root not in resolved:
                    resolved.append(root)
        elif config["package"] and target == config["package"]:
            named.append(f"{target} (package) -> {config['default_targets']}")
            for root in roots_of(config["default_targets"]):
                if root not in resolved:
                    resolved.append(root)
        else:
            named.append(f"{target} (module)")
            if target not in resolved:
                resolved.append(target)
    return resolved, package_roots, "; ".join(named)


def stale_closure(
    graph: dict[str, set[str]],
    targets: Iterable[str],
    frontier: set[str],
) -> Optional[int]:
    """Count the modules a build of ``targets`` would have to elaborate.

    Lake's `--no-build` probe names only the frontier it stopped at, so the
    frontier alone under-reports what an actual build would elaborate.  The
    answer is the frontier plus every module in the target's import closure
    that reaches it.
    """
    named = [str(target) for target in targets]
    if any(target not in graph for target in named):
        return None
    if any(module not in graph for module in frontier):
        # A stale module outside this package — a dependency, or a target
        # shape the graph does not model — is not evidence about the closure,
        # and a stale dependency is exactly the broad case that must stay
        # `sensitive`.
        return None
    closure: set[str] = set()
    stack = list(named)
    while stack:
        module = stack.pop()
        if module in closure:
            continue
        closure.add(module)
        stack.extend(graph.get(module, ()))
    stale = frontier & closure
    changed = True
    while changed:
        changed = False
        for module in closure - stale:
            if graph.get(module, set()) & stale:
                stale.add(module)
                changed = True
    return len(stale)


def stale_module_count(
    worktree: Path,
    targets: list[str],
    real_lake: Path,
    closure_roots: Optional[list[str]] = None,
    package_roots: Optional[list[str]] = None,
) -> tuple[Optional[int], str]:
    """Count the modules a probe proves out of date, or explain why it cannot.

    Exit 0 means nothing is stale.  Exit 3 means Lake refused to build and
    named the out-of-date frontier; anything else is not evidence.
    """
    try:
        completed = subprocess.run(
            [str(real_lake), "build", "--no-build", *targets],
            cwd=worktree, text=True, capture_output=True, check=False, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"probe unavailable: {exc}"
    if completed.returncode == 0:
        return 0, "probe reports the selected artifacts current"
    if completed.returncode != STALE_EXIT:
        return None, f"probe exited {completed.returncode}; not stale-set evidence"
    output = (completed.stdout or "") + (completed.stderr or "")
    lines = output.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines)
            if "logged failures" in line
        )
    except StopIteration:
        return None, "probe reported stale artifacts without naming a frontier"
    frontier = set()
    for line in lines[start + 1:]:
        match = _STALE_FAILURE_RE.match(line)
        if not match:
            break
        frontier.add(match.group(1))
    if not frontier:
        return None, "probe named no out-of-date module"
    roots = list(closure_roots if closure_roots is not None else targets)
    graph = package_import_graph(worktree, list(package_roots or []) + roots)
    if graph is None:
        return None, "package import graph unavailable"
    count = stale_closure(graph, roots, frontier)
    if count is None:
        return None, "targets are outside the package import graph"
    return count, (
        f"probe frontier {sorted(frontier)}; {count} module(s) in the closure of "
        f"{roots} would be elaborated"
    )


def stale_evidence(
    worktree: Path, targets: list[str], real_lake: Path
) -> dict[str, Any]:
    """Resolve the targets to module roots, then measure their stale closure.

    A full or package target names no module, so before this the probe's
    frontier had nothing to be a closure *of* and the class was decided by the
    shape of the request rather than by evidence.  Resolution failure keeps the
    conservative class and says which part failed.
    """
    closure_roots, package_roots, resolution = resolve_target_roots(worktree, targets)
    if closure_roots is None:
        return {
            "roots": None, "package_roots": package_roots, "resolution": resolution,
            "stale": None, "detail": resolution,
        }
    stale, detail = stale_module_count(
        worktree, targets, real_lake, closure_roots, package_roots
    )
    return {
        "roots": closure_roots, "package_roots": package_roots,
        "resolution": resolution, "stale": stale, "detail": detail,
    }


def _measured_rows(
    worktree: Path,
    targets: list[str],
    toolchain_digest: Optional[str],
    manifest_digest: Optional[str],
    settings: dict[str, int],
    require_elaboration: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Ledger rows that measured *this* worktree, targets, and pinned inputs.

    ``require_elaboration`` keeps only rows that rebuilt at least one module.
    A row that restored everything from the artifact cache measured a build
    that elaborated nothing; it cannot size one that will.
    """
    try:
        rows, _corrupt = read_ledger("30d")
    except (OSError, ValueError):
        # Unreadable performance state is not evidence; it must never widen
        # admission, so the caller falls back to the conservative class.
        return [], "ledger unreadable"
    if toolchain_digest is None or manifest_digest is None:
        return [], "worktree toolchain or manifest digest unavailable"
    matching = [
        row for row in rows
        if row.get("kind") == "build"
        and not row.get("probe")
        and row.get("exit") == 0
        and str(row.get("worktree")) == str(worktree)
        and list(row.get("targets") or []) == list(targets)
        and row.get("toolchain_digest") == toolchain_digest
        and row.get("manifest_digest") == manifest_digest
        and isinstance(row.get("peak_rss_mib"), (int, float))
    ]
    if not matching:
        return [], "no successful measurement for these targets on the pinned inputs"
    if require_elaboration:
        elaborated = [row for row in matching if row.get("modules_rebuilt")]
        if not elaborated:
            return [], (
                "no successful measurement that elaborated a module for these "
                "targets on the pinned inputs"
            )
        matching = elaborated
    matching.sort(key=lambda row: str(row["time"]))
    keep = matching[-int(settings["estimate_sample_rows"]):]
    detail = f"{len(keep)} matching measurement(s)"
    return keep, (detail + " that elaborated" if require_elaboration else detail)


def classify_contention(
    worktree: Path,
    targets: list[str],
    real_lake: Path,
    settings: dict[str, int],
    digests: tuple[Optional[str], Optional[str]],
    stale: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    """Choose a contention class from measurement, defaulting to `sensitive`.

    `tolerant` requires all three: a small stale set now, a measured peak below
    the configured threshold, and a ledger row taken on the same toolchain and
    Lake manifest.  Any missing, drifted, or unreadable evidence keeps the
    conservative class; evidence can only ever relax scheduling, never a floor.
    """
    evidence: dict[str, Any] = {}
    probe = stale if stale is not None else stale_evidence(worktree, targets, real_lake)
    stale_count = probe["stale"]
    evidence["resolved_roots"] = probe["roots"]
    evidence["resolution"] = probe["resolution"]
    evidence["stale_modules"] = stale_count
    evidence["stale_detail"] = probe["detail"]
    if probe["roots"] is None:
        evidence["reason"] = probe["resolution"]
        return "sensitive", evidence
    limit = int(settings["tolerant_module_count"])
    if stale_count is None or stale_count > limit:
        evidence["reason"] = (
            f"stale set is {stale_count if stale_count is not None else 'unmeasured'} "
            f"(limit {limit})"
        )
        return "sensitive", evidence
    rows, rows_detail = _measured_rows(worktree, targets, *digests, settings)
    evidence["measurements"] = rows_detail
    if not rows:
        evidence["reason"] = rows_detail
        return "sensitive", evidence
    peak_gib = max(float(row["peak_rss_mib"]) for row in rows) / 1024.0
    evidence["measured_peak_gib"] = round(peak_gib, 2)
    threshold = float(settings["tolerant_peak_gib"])
    if peak_gib >= threshold:
        evidence["reason"] = f"measured peak {peak_gib:.2f} GiB is not below {threshold} GiB"
        return "sensitive", evidence
    evidence["reason"] = (
        f"{stale_count} stale module(s) at or below {limit} and a measured peak of "
        f"{peak_gib:.2f} GiB below {threshold} GiB on the pinned toolchain and manifest"
    )
    return "tolerant", evidence


def derive_memory_gib(
    worktree: Path,
    targets: list[str],
    settings: dict[str, int],
    digests: tuple[Optional[str], Optional[str]],
    default_gib: int,
    stale_modules: Optional[int] = None,
) -> tuple[int, dict[str, Any]]:
    """Propose a whole-GiB estimate from measurement, never below the floor.

    ``stale_modules`` is the probe's count for this build.  A build with
    nothing stale may be sized by any successful row of the same targets; a
    build that will elaborate — or one whose stale set could not be measured —
    is sized only by rows that themselves elaborated a module, because a
    cache-restored row measures a build that did no work.
    """
    floor = int(settings["minimum_estimate_gib"])
    require_elaboration = stale_modules != 0
    rows, detail = _measured_rows(
        worktree, targets, *digests, settings, require_elaboration
    )
    if not rows:
        return max(floor, default_gib), {
            "source": f"profile default ({detail})",
            "rows": 0,
            "keyed_on_elaboration": require_elaboration,
        }
    peak_gib = max(float(row["peak_rss_mib"]) for row in rows) / 1024.0
    estimate = max(floor, math.ceil(peak_gib) + int(settings["estimate_margin_gib"]))
    return estimate, {
        "source": (
            f"max of {len(rows)} measured peak(s) ({peak_gib:.2f} GiB) "
            f"plus {settings['estimate_margin_gib']} GiB"
            + (" from rows that elaborated" if require_elaboration else "")
        ),
        "rows": len(rows),
        "keyed_on_elaboration": require_elaboration,
        "measured_peak_gib": round(peak_gib, 2),
        "row_times": [str(row["time"]) for row in rows],
    }


def repeat_failure(
    worktree: Path,
    targets: list[str],
    settings: dict[str, int],
    before: Optional[datetime] = None,
) -> Optional[str]:
    """Was the previous build of exactly these targets also a failure, recently?"""
    window = int(settings["repeat_fail_seconds"])
    cutoff = before or datetime.now(timezone.utc)
    start = cutoff - timedelta(seconds=window + 60)
    try:
        rows, _corrupt = read_ledger(_iso(start), _iso(cutoff))
    except (OSError, ValueError):
        return None
    candidates = [
        row for row in rows
        if row.get("kind") == "build"
        and not row.get("probe")
        and str(row.get("worktree")) == str(worktree)
        and list(row.get("targets") or []) == list(targets)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda row: str(row["time"]))
    previous = candidates[-1]
    when = datetime.fromisoformat(str(previous["time"]).replace("Z", "+00:00"))
    if previous.get("exit") == 0 or (cutoff - when).total_seconds() > window:
        return None
    return (
        "REPEAT_FAIL: the previous build of these targets also failed within "
        f"{window // 60} minute(s). Read every error at once with "
        "`lean_diagnostic_messages` on the edited file, and use `lean_goal` or "
        "`lean_hover_info` for a type mismatch, before building again."
    )


def _digest_fields(digests: tuple[Optional[str], Optional[str]]) -> dict[str, str]:
    toolchain, manifest = digests
    fields = {}
    if toolchain:
        fields["toolchain_digest"] = toolchain
    if manifest:
        fields["manifest_digest"] = manifest
    return fields


def _dependency_revision(worktree: Path, dependency: str) -> tuple[Optional[str], str]:
    """Read the pinned revision Lake resolved, refusing a non-Git dependency."""
    try:
        manifest = json.loads((worktree / "lake-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"lake-manifest.json is unreadable: {exc}"
    for package in manifest.get("packages") or []:
        if not isinstance(package, dict) or package.get("name") != dependency:
            continue
        if package.get("type") != "git" or not isinstance(package.get("rev"), str):
            return None, f"dependency {dependency} is no longer a Git-pinned package"
        return str(package["rev"]), f"{dependency} pinned at {package['rev']}"
    return None, f"dependency {dependency} is absent from the resolved manifest"


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
    memory_gib: Optional[int] = None,
    contention: Optional[str] = None,
    threads: int = DEFAULT_THREADS,
    probe: bool = False,
    wait_seconds: Optional[int] = None,
    census: bool = False,
    dependency: Optional[str] = None,
    stdout: Optional[TextIO] = None,
) -> int:
    output = stdout or os.sys.stdout
    cwd = Path.cwd().resolve()
    _settings_cache: dict[str, int] = {}

    def settings() -> dict[str, int]:
        # Loaded only when a tunable is actually consulted, so an explicit
        # classification and estimate reach Lake without touching the profile.
        if not _settings_cache:
            _settings_cache.update(load_admission_settings())
        return _settings_cache

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
    _base, suffix = split_worktree_suffix(_apparent_goal(worktree))
    if census:
        if suffix != "rehearsal":
            print(json.dumps({
                "status": "REFUSED",
                "detail": (
                    "--census rewrites the pinned dependency and rebuilds the full "
                    f"target; it runs only in .worktrees/{goal}-rehearsal"
                ),
            }, sort_keys=True), file=output)
            return 2
        if not dependency or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dependency):
            print(json.dumps({
                "status": "REFUSED",
                "detail": "--census requires --dependency NAME naming one Lake dependency",
            }, sort_keys=True), file=output)
            return 2
        if probe:
            print(json.dumps({
                "status": "REFUSED", "detail": "--census cannot be combined with --probe",
            }, sort_keys=True), file=output)
            return 2
    elif dependency:
        print(json.dumps({
            "status": "REFUSED", "detail": "--dependency is only meaningful with --census",
        }, sort_keys=True), file=output)
        return 2
    try:
        real_lake, _, _ = resolve_toolchain(worktree)
    except RuntimeError as exc:
        print(json.dumps({"status": "REFUSED", "detail": str(exc)}, sort_keys=True), file=output)
        return GUARD_REFUSAL_EXIT
    digests = worktree_digests(worktree)
    requested_contention = contention
    evidence: dict[str, Any] = {}
    probe_evidence: Optional[dict[str, Any]] = None

    def stale() -> dict[str, Any]:
        # One probe per build, shared by the class and the estimate: the two
        # answers must describe the same stale set.
        nonlocal probe_evidence
        if probe_evidence is None:
            probe_evidence = stale_evidence(worktree, targets, real_lake)
        return probe_evidence

    if census:
        contention = "exclusive"
        evidence["reason"] = "a dependency census rebuilds the full closure"
    elif contention is None:
        contention, evidence = classify_contention(
            worktree, targets, real_lake, settings(), digests, stale()
        )
    estimate_evidence: dict[str, Any] = {}
    # The estimate reuses the class's probe when there was one.  It never asks
    # for a probe of its own: a caller who states the class deserves the
    # conservative keying, not a second Lake invocation.
    measured_stale = probe_evidence["stale"] if probe_evidence is not None else None
    if memory_gib is None:
        memory_gib, estimate_evidence = derive_memory_gib(
            worktree, targets, settings(), digests, DEFAULT_MEMORY_GIB, measured_stale
        )
    elif probe_evidence is not None:
        # An explicit estimate is honoured, but the reader is told what the
        # evidence would have proposed: a larger one is charged 1.25x and can
        # be passed over by every smaller request on a busy host.
        derived, derived_evidence = derive_memory_gib(
            worktree, targets, settings(), digests, DEFAULT_MEMORY_GIB, measured_stale
        )
        estimate_evidence = {
            "source": "explicit",
            "explicit_gib": memory_gib,
            "derived_gib": derived,
            "derived_source": derived_evidence["source"],
        }
        if memory_gib > derived:
            print(
                f"estimate: an explicit --memory-gib {memory_gib} exceeds the "
                f"{derived} GiB this worktree's evidence supports "
                f"({derived_evidence['source']}); it is charged "
                f"{semaphore._charged_memory_gib(memory_gib)} GiB and can be passed over by "
                "every smaller request while it waits",
                file=output,
            )

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
            **_digest_fields(digests),
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
        wait_seconds=wait_seconds,
        **(
            {
                "poll_seconds": float(settings()["wait_poll_seconds"]),
                "announce": lambda line: print(line, file=output, flush=True),
            }
            if wait_seconds is not None else {}
        ),
    )
    if not admitted:
        print(json.dumps({
            "status": "REFUSED",
            "admission": admission,
            "contention": contention,
            "requested_contention": requested_contention,
            "evidence": evidence,
            "memory_gib": memory_gib,
            "estimate": estimate_evidence,
        }, sort_keys=True), file=output)
        return 2
    dependency_rev: Optional[str] = None
    if census:
        update = subprocess.run(
            [str(real_lake), "update", str(dependency)],
            cwd=worktree, text=True, capture_output=True, check=False,
        )
        print(update.stdout, end="", file=output)
        print(update.stderr, end="", file=output)
        dependency_rev, dependency_detail = _dependency_revision(worktree, str(dependency))
        if update.returncode != 0 or dependency_rev is None:
            semaphore.adaptive_release(goal)
            print(json.dumps({
                "status": "REFUSED",
                "detail": f"dependency census aborted before building: {dependency_detail}",
                "exit": update.returncode,
            }, sort_keys=True), file=output)
            return update.returncode or 2
        digests = worktree_digests(worktree)
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
    peak_mib = round(sampler.peak_rss_mib, 1) if sampler and sampler.samples else None
    hint = repeat_failure(worktree, targets, settings()) if exit_code == 1 else None
    restart_line = (
        (
            f"rebuilt {len(rebuilt)} module(s): {', '.join(rebuilt[:12])}"
            + ("…" if len(rebuilt) > 12 else "")
            + " — restart the Lean server before trusting diagnostics in files "
            "that import them"
        )
        if rebuilt else None
    )
    # The margin the estimate carried over what the build actually needed.
    # Recording it per row makes the +1 GiB a measured quantity rather than a
    # belief; the margin itself is unchanged.
    under_cover = (
        round(max(0.0, peak_mib / 1024.0 - float(memory_gib)), 2)
        if peak_mib is not None else None
    )
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
        "memory_gib": memory_gib,
        "evidence_contention": contention,
        "estimate_source": str(estimate_evidence.get("source", "explicit")),
        "estimate_gib": memory_gib,
        "estimate_under_cover_gib": under_cover,
        **({"evidence_reason": str(evidence["reason"])} if evidence.get("reason") else {}),
        **({"resolved_roots": [str(root) for root in evidence["resolved_roots"]]}
           if isinstance(evidence.get("resolved_roots"), list) else {}),
        **({"stale_modules": int(evidence["stale_modules"])}
           if isinstance(evidence.get("stale_modules"), int)
           and not isinstance(evidence.get("stale_modules"), bool) else {}),
        **({"stale_detail": str(evidence["stale_detail"])}
           if evidence.get("stale_detail") else {}),
        **({"hint": hint} if hint else {}),
        **({"requested_contention": requested_contention} if requested_contention else {}),
        **({"outcome": "killed"} if interrupted else {}),
        **({"census": True, "dependency": str(dependency)} if census else {}),
        **({"dependency_rev": dependency_rev} if dependency_rev else {}),
        **_digest_fields(digests),
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
    summary: dict[str, Any] = {
        "status": "OK" if exit_code == 0 else "ERROR", "exit": exit_code,
        "wall_seconds": round(wall, 3), "peak_rss_mib": round(sampler.peak_rss_mib, 1) if sampler and sampler.samples else None,
        "peak_lean_rss_mib": round(sampler.peak_lean_rss_mib, 1) if sampler and sampler.samples else None,
        "max_concurrent_lean": sampler.max_concurrent_lean if sampler and sampler.samples else None,
        "sampling_samples": sampler.samples if sampler else 0,
        "sampling_unavailable": sampler.unavailable_samples if sampler else 0,
        "modules_rebuilt": len(rebuilt), "modules_restored": len(restored), "admission": admission,
        "interrupted": interrupted,
        "contention": contention,
        "requested_contention": requested_contention,
        "evidence": evidence,
        "memory_gib": memory_gib,
        "estimate": estimate_evidence,
    }
    if interrupted:
        summary["outcome"] = "killed"
    if census:
        summary["dependency"] = dependency
        summary["dependency_rev"] = dependency_rev
    if hint:
        summary["hint"] = hint
    if restart_line:
        summary["restart_lean_server"] = restart_line
    # These two are what a caller most needs and most often filters away: a
    # pipeline keeping only `^error` and `Build complete` drops the JSON line
    # entirely. They are printed on their own prefixed lines as well.
    if hint:
        print(f"hint: {hint}", file=output)
    if restart_line:
        print(f"restart: {restart_line}", file=output)
    print(json.dumps(summary, sort_keys=True), file=output)
    return exit_code
