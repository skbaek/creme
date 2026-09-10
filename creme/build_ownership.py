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
    # Additive, from creme-admission-accuracy-v1: the peak RSS of each
    # module's own `lean` process, so a later build of the same modules is
    # sized from what they cost rather than from the spelling of a target list.
    "module_peak_mib",
    # Additive, from creme-memory-evidence-v1: a host-local repository scope
    # plus the complete source/configuration/execution identity used when a
    # measured build was admitted.  These fields deliberately do not attempt
    # to retrofit identity to old rows; missing identity is compatibility
    # evidence only, never an assertion that an old peak is exact today.
    "repository_identity", "input_context", "module_inputs", "identity_status",
}
DEFAULT_LAKE_OVERHEAD_GIB = 1.0
# A stale set this small has its concurrency computed exactly from the import
# order; a larger one is bounded by the concurrency the ledger has seen.
_WIDTH_BRUTE_FORCE_LIMIT = 12
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
    module_peaks = row.get("module_peak_mib")
    if module_peaks is not None and not (
        isinstance(module_peaks, dict)
        and all(isinstance(k, str) and _number_or_none(v) for k, v in module_peaks.items())
    ):
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
    identity_fields = ("repository_identity", "input_context", "module_inputs")
    present_identity = [field in row for field in identity_fields]
    if any(present_identity) and not all(present_identity):
        return False
    if all(present_identity) and not (
        isinstance(row["repository_identity"], str)
        and isinstance(row["input_context"], str)
        and isinstance(row["module_inputs"], dict)
        and all(isinstance(module, str) and isinstance(digest, str)
                for module, digest in row["module_inputs"].items())
    ):
        return False
    if "identity_status" in row and not isinstance(row["identity_status"], str):
        return False
    return "renewals" not in row or _string_list(row["renewals"])


# `wait-acquire` is the outcome of a queued request and decides a lock-out
# exactly as a direct acquisition does. `wait-enqueue` is not a decision.
ACQUIRE_ACTIONS = ("adaptive-acquire", "soft-acquire", "hard-acquire", "wait-acquire")
QUEUE_OUTCOME_ACTIONS = ("wait-acquire", "wait-dropped")
RELEASE_ACTIONS = ("adaptive-release", "hard-release", "wind-down")
_VERDICT_TOKEN = re.compile(r"^([A-Z][A-Z_]*):")


def _verdict_token(row: dict[str, Any]) -> str:
    match = _VERDICT_TOKEN.match(str(row.get("detail", "")))
    return match.group(1) if match else "UNCLASSIFIED"


def _hard_hold_intervals(
    rows: list[dict[str, Any]], window_end: datetime
) -> list[tuple[datetime, datetime, str]]:
    """Reconstruct when *some* label held the host exclusively.

    A hold's own row says which kind admission selected, so the intervals come
    from the log rather than from a second source that could disagree with it.
    """
    intervals: list[tuple[datetime, datetime, str]] = []
    holder: Optional[tuple[str, datetime]] = None
    for row in rows:
        action, verdict = str(row["action"]), str(row["verdict"])
        if verdict != "OK":
            continue
        if action in ACQUIRE_ACTIONS and "ADMITTED_HARD" in str(row.get("detail", "")):
            holder = (str(row["label"]), row["when"])
        elif action in RELEASE_ACTIONS and holder and str(row["label"]) == holder[0]:
            intervals.append((holder[1], row["when"], holder[0]))
            holder = None
    if holder:
        intervals.append((holder[1], window_end, holder[0]))
    return intervals


def _queue_episodes(
    rows: list[dict[str, Any]], window_end: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Pair every `wait-enqueue` with the decision that ended it.

    An enqueue with no outcome row is reported `UNRESOLVED` and bounded by the
    label's next enqueue or the window's end.  Reporting it as zero would make
    a wait somebody cancelled look like a wait that never happened.
    """
    episodes: dict[str, list[dict[str, Any]]] = {}
    open_at: dict[str, datetime] = {}

    def close(label: str, start: datetime, end: datetime, outcome: str) -> None:
        episodes.setdefault(label, []).append({
            "start": start, "end": end, "outcome": outcome,
            "seconds": max(0.0, (end - start).total_seconds()),
        })

    for row in rows:
        label, action = str(row["label"]), str(row["action"])
        if action == "wait-enqueue":
            if label in open_at:
                close(label, open_at.pop(label), row["when"], "UNRESOLVED")
            open_at[label] = row["when"]
        elif action in QUEUE_OUTCOME_ACTIONS:
            start = open_at.pop(label, None)
            if start is None:
                continue
            outcome = (
                "ADMITTED" if str(row["verdict"]) == "OK" else _verdict_token(row)
            )
            close(label, start, row["when"], outcome)
    for label, start in open_at.items():
        close(label, start, window_end, "UNRESOLVED")
    return episodes


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
            "queue_episodes": 0,
            "queue_outcomes": {},
            "queue_seconds_total": 0.0,
            "queue_seconds_longest": 0.0,
            "queue_seconds_no_hard_hold": 0.0,
            "admissions_of_other_labels_while_queued": 0,
            "admissions_of_other_labels_by_label": {},
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

    # Queue time: every enqueue, the decision that closed it, and how much of
    # it was spent while nothing held the host exclusively.
    episodes = _queue_episodes(rows, window_end)
    hard_intervals = _hard_hold_intervals(rows, window_end)
    admissions = [
        row for row in rows
        if str(row["action"]) in ACQUIRE_ACTIONS and str(row["verdict"]) == "OK"
    ]

    def hard_held_seconds(start: datetime, end: datetime) -> float:
        total = 0.0
        for hold_start, hold_end, _label in hard_intervals:
            low, high = max(start, hold_start), min(end, hold_end)
            if high > low:
                total += (high - low).total_seconds()
        return total

    for label, label_episodes in episodes.items():
        record = entry(label)
        outcomes: dict[str, int] = {}
        passed_by: dict[str, int] = {}
        for episode in label_episodes:
            outcomes[episode["outcome"]] = outcomes.get(episode["outcome"], 0) + 1
            record["queue_seconds_total"] += episode["seconds"]
            record["queue_seconds_longest"] = max(
                record["queue_seconds_longest"], episode["seconds"]
            )
            record["queue_seconds_no_hard_hold"] += max(
                0.0,
                episode["seconds"] - hard_held_seconds(episode["start"], episode["end"]),
            )
            for row in admissions:
                if str(row["label"]) == label:
                    continue
                if episode["start"] <= row["when"] <= episode["end"]:
                    passed_by[str(row["label"])] = passed_by.get(str(row["label"]), 0) + 1
        record["queue_episodes"] = len(label_episodes)
        record["queue_outcomes"] = dict(sorted(outcomes.items()))
        record["admissions_of_other_labels_by_label"] = dict(sorted(passed_by.items()))
        record["admissions_of_other_labels_while_queued"] = sum(passed_by.values())

    for record in by_label.values():
        for key in (
            "queue_seconds_total", "queue_seconds_longest", "queue_seconds_no_hard_hold",
        ):
            record[key] = round(record[key], 1)
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


_REASON_BUCKETS = (
    ("roots unresolved", "roots unresolved"),
    ("a full target is a broad closure", "full target"),
    ("a dependency census", "dependency census"),
    ("stale set is unmeasured", "unmeasured stale set"),
    ("stale set is", "stale set above the limit"),
    ("ledger unreadable", "ledger unreadable"),
    ("worktree toolchain or manifest digest", "digest unavailable"),
    ("no successful measurement that elaborated", "no elaborating measurement"),
    ("no successful measurement", "no successful measurement"),
    ("stale module(s) unmeasured", "unmeasured stale modules"),
    ("is not below", "measured peak too high"),
)


def _reason_bucket(reason: str) -> str:
    for needle, bucket in _REASON_BUCKETS:
        if needle in reason:
            return bucket
    return "other"


def _reason_histogram(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Why each build was left `sensitive`, from the row rather than a transcript."""
    counts: dict[str, int] = {}
    for row in rows:
        if str(row.get("contention")) != "sensitive":
            continue
        reason = row.get("evidence_reason")
        if not isinstance(reason, str) or not reason:
            bucket = (
                "explicit class (no classification ran)"
                if row.get("requested_contention")
                else "unrecorded (row predates evidence_reason)"
            )
        else:
            bucket = _reason_bucket(reason)
        counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items()))


def _estimate_source_histogram(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        source = str(row.get("estimate_source") or "unrecorded")
        if source.startswith("profile default (full target"):
            bucket = "profile default (full target)"
        elif source.startswith("profile default (heavy module"):
            bucket = "profile default (heavy module)"
        elif source.startswith("profile default"):
            bucket = "profile default (no measurement)"
        elif source.startswith("max of") or source.startswith("target rows:"):
            bucket = "measured (target rows)"
        elif source.startswith("measured stale set"):
            bucket = "measured (stale set)"
        elif source.startswith("narrow default"):
            bucket = "narrow default"
        elif source.startswith("broader rebuild"):
            bucket = "broader rebuild"
        elif source.startswith("nothing is stale"):
            bucket = "fresh (no hold)"
        elif source == "explicit" or source.startswith("explicit"):
            bucket = "explicit"
        else:
            bucket = source
        counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items()))


def _under_cover(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How far each admitted estimate fell below the peak it was charged for.

    Computed from the estimate the row carries and the peak it measured, so
    the +1 GiB margin becomes a measured quantity over any window, including
    windows recorded before the field existed.
    """
    cases: list[dict[str, Any]] = []
    measured = 0
    for row in rows:
        peak = row.get("peak_rss_mib")
        estimate = row.get("estimate_gib")
        if estimate is None:
            estimate = row.get("memory_gib")
        if not isinstance(peak, (int, float)) or isinstance(peak, bool):
            continue
        if not isinstance(estimate, (int, float)) or isinstance(estimate, bool):
            continue
        measured += 1
        gap = round(float(peak) / 1024.0 - float(estimate), 2)
        if gap > 0:
            cases.append({
                "time": str(row.get("time")),
                "targets": list(row.get("targets") or []),
                "estimate_gib": estimate,
                "peak_gib": round(float(peak) / 1024.0, 2),
                "under_cover_gib": gap,
            })
    cases.sort(key=lambda case: case["time"])
    return {
        "rows_with_an_estimate_and_a_peak": measured,
        "under_covered_rows": len(cases),
        "worst_under_cover_gib": max((case["under_cover_gib"] for case in cases), default=0.0),
        "cases": cases,
    }


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
            "queue_episodes": coordinated.get("queue_episodes", 0),
            "queue_outcomes": coordinated.get("queue_outcomes", {}),
            "queue_seconds_total": coordinated.get("queue_seconds_total", 0.0),
            "queue_seconds_longest": coordinated.get("queue_seconds_longest", 0.0),
            "queue_seconds_no_hard_hold": coordinated.get(
                "queue_seconds_no_hard_hold", 0.0
            ),
            "admissions_of_other_labels_while_queued": coordinated.get(
                "admissions_of_other_labels_while_queued", 0
            ),
            "admissions_of_other_labels_by_label": coordinated.get(
                "admissions_of_other_labels_by_label", {}
            ),
            "sensitive_reasons": _reason_histogram(goal_builds),
            "estimate_sources": _estimate_source_histogram(goal_builds),
            "estimate_under_cover": _under_cover(goal_builds),
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


def lake_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Keep every workflow Lake process on the canonical Creme cache.

    Override inherited host/toolchain defaults, including from linked worktrees.
    Lake creates the cache on demand; resolving the environment is read-only.
    """
    env = dict(os.environ if base is None else base)
    env["LAKE_CACHE_DIR"] = str(semaphore.canonical_creme_root() / ".creme" / "lake-cache")
    return env


def guarded_mcp_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = lake_env(base)
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
        env = lake_env()
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
        completed = subprocess.run([str(real_lake), *guarded], cwd=cwd, env=lake_env(), check=False)
        _guard_event(
            cwd=cwd, args=guarded, exit_code=completed.returncode, rewritten=rewritten,
            reason="setup-file forced to no-build/no-cache" if rewritten else "already guarded setup-file",
        )
        return completed.returncode

    if command in {"--version", "-h", "--help", "help"}:
        return subprocess.run([str(real_lake), *args], cwd=cwd, env=lake_env(), check=False).returncode

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
    env = lake_env()
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


def _executable(command: str) -> str:
    """The basename of a command line's executable, or `` when it has none."""
    first = command.split(None, 1)[0] if command.strip() else ""
    return os.path.basename(first)


def _lean_module(command: str, worktree: Optional[Path]) -> Optional[str]:
    """Name the module a `lean` process is elaborating, from its command line.

    Lake invokes `lean <source>.lean -o <olean> ...`; the source path names the
    module relative to the worktree, and the `.olean` path names it relative to
    the build's `lib/lean` directory.  Either is enough; neither is guessed.
    """
    tokens = command.split()
    if not tokens or os.path.basename(tokens[0]) != "lean":
        return None
    for token in tokens[1:]:
        if token.endswith(".lean") and not token.startswith("-"):
            path = Path(token)
            if worktree is not None:
                try:
                    candidate = path if path.is_absolute() else worktree / path
                    relative = Path(os.path.normpath(candidate)).relative_to(worktree)
                    return ".".join(relative.with_suffix("").parts)
                except ValueError:
                    pass
            break
    for index, token in enumerate(tokens[1:-1], start=1):
        if token == "-o" and tokens[index + 1].endswith(".olean"):
            olean = tokens[index + 1].replace(os.sep, "/")
            marker = "/lib/lean/"
            if marker in olean:
                return olean.split(marker, 1)[1][:-len(".olean")].replace("/", ".")
    return None


def _process_snapshot() -> Optional[dict[int, tuple[int, int, str]]]:
    # `command=` rather than `comm=`: the arguments are what attribute a `lean`
    # process to the module it is elaborating.
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss=,command="], capture_output=True, text=True, check=False,
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
    def __init__(self, pid: int, interval: float = 0.5, worktree: Optional[Path] = None):
        super().__init__(daemon=True)
        self.pid = pid
        self.interval = interval
        self.worktree = worktree
        self.stop_event = threading.Event()
        self.peak_rss_mib = 0.0
        self.max_concurrent_lean = 0
        self.peak_lean_rss_mib = 0.0
        # Each module's own `lean` process at its peak, in MiB.  This is the
        # additive quantity: a build's tree peak is the Lake overhead plus the
        # `lean` processes that happen to run at the same time.
        self.module_peak_mib: dict[str, float] = {}
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
            lean_rows = [rows[pid] for pid in pids if pid in rows and _executable(rows[pid][2]) == "lean"]
            lean_rss_kib = max((row[1] for row in lean_rows), default=0)
            self.peak_rss_mib = max(self.peak_rss_mib, rss_kib / 1024.0)
            self.max_concurrent_lean = max(self.max_concurrent_lean, len(lean_rows))
            self.peak_lean_rss_mib = max(self.peak_lean_rss_mib, lean_rss_kib / 1024.0)
            for _parent, rss, command in lean_rows:
                module = _lean_module(command, self.worktree)
                if module is not None:
                    self.module_peak_mib[module] = max(
                        self.module_peak_mib.get(module, 0.0), rss / 1024.0
                    )
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


def _identity_digest(parts: Iterable[str]) -> str:
    """A domain-separated digest for locally collected performance inputs."""
    digest = hashlib.sha256(b"creme-build-input-v1\0")
    for part in parts:
        encoded = part.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:32]


def _git_text(worktree: Path, arguments: list[str]) -> Optional[str]:
    """Read a small Git fact, without treating a failed read as identity."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            text=True, capture_output=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def repository_identity(worktree: Path) -> Optional[str]:
    """A stable scope for linked worktrees of one local Git repository.

    A remote URL is neither unique nor immutable enough to distinguish local
    repositories.  Git's common directory is shared by linked worktrees and
    is private host-local state, so only its domain-separated digest reaches
    the ledger.
    """
    common = _git_text(worktree, ["rev-parse", "--git-common-dir"])
    if not common:
        return None
    path = Path(common)
    if not path.is_absolute():
        path = (worktree / path).resolve()
    else:
        path = path.resolve()
    return _identity_digest(["repository", str(path)])


def _legacy_repository_identity(recorded_worktree: Any) -> Optional[str]:
    """Recover scope for an old linked-worktree row only from live Git state.

    A removed `.worktrees/GOAL` directory still has its repository root beside
    it.  Asking Git at that root can recover the shared common directory.  A
    path spelling alone never does: if neither the recorded checkout nor that
    conventional parent can be inspected, the legacy row remains unscoped and
    is excluded from a different worktree.
    """
    if not isinstance(recorded_worktree, str) or not recorded_worktree:
        return None
    path = Path(recorded_worktree)
    if path.is_dir():
        found = repository_identity(path)
        if found is not None:
            return found
    if path.parent.name == ".worktrees" and path.parent.parent.is_dir():
        return repository_identity(path.parent.parent)
    return None


def _lake_config_digest(worktree: Path) -> Optional[str]:
    """Digest every Lake configuration file that can affect target semantics."""
    entries: list[str] = []
    for name in ("lakefile.lean", "lakefile.toml"):
        path = worktree / name
        if not path.exists():
            continue
        value = _digest_file(path)
        if value is None:
            return None
        entries.extend((name, value))
    return _identity_digest(entries) if entries else None


def _execution_environment_digest() -> str:
    """Fingerprint relevant execution overrides without recording their values."""
    effective = lake_env()
    entries: list[str] = []
    for name in sorted(effective):
        if name.startswith(("LEAN_", "LAKE_", "MIMALLOC_", "MALLOC_")) and name != "LEAN_NUM_THREADS":
            entries.extend((name, effective[name]))
    return _identity_digest(entries)


def resolved_toolchain_identity(
    real_lake: Path, real_lean: Path, sysroot: Path,
) -> str:
    """Bind evidence to the executables Elan actually resolved for this run.

    The guarded wrapper has already checked that these resolved paths form one
    coherent sysroot.  Their host-local paths distinguish an `ELAN_TOOLCHAIN`
    override from merely identical `lean-toolchain` file text without writing
    those paths into the ledger.
    """
    return _identity_digest([
        "resolved-toolchain", str(real_lake.resolve()), str(real_lean.resolve()), str(sysroot.resolve()),
    ])


def _dependency_checkout_identity(worktree: Path) -> tuple[Optional[str], str]:
    """Accept only clean, manifest-resolved dependency checkouts as exact.

    Hashing every dependency source tree for each module would make narrow
    admission expensive.  The manifest already identifies clean pinned trees;
    a bounded Git-status check detects a dirty checkout and deliberately makes
    exact reuse unavailable.  That avoids equating a locally edited dependency
    with its manifest revision.
    """
    try:
        manifest = json.loads((worktree / "lake-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "lake manifest unreadable"
    packages_dir = manifest.get("packagesDir", ".lake/packages")
    if not isinstance(packages_dir, str):
        return None, "lake manifest packages directory is invalid"
    relative = Path(packages_dir)
    if relative.is_absolute() or ".." in relative.parts:
        return None, "lake manifest packages directory is outside the worktree"
    root = worktree / relative
    packages = manifest.get("packages")
    if not isinstance(packages, list):
        return None, "lake manifest packages are invalid"
    entries: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            return None, "lake manifest package entry is invalid"
        name = package.get("name")
        revision = package.get("rev")
        if package.get("type") != "git" or not isinstance(name, str) or not isinstance(revision, str):
            return None, "lake manifest contains a non-Git or unpinned dependency"
        checkout = (root / name).resolve()
        top_level = _git_text(checkout, ["rev-parse", "--show-toplevel"])
        if top_level is None or Path(top_level).resolve() != checkout:
            return None, f"dependency {name} checkout root is not the resolved package directory"
        head = _git_text(checkout, ["rev-parse", "HEAD"])
        if head != revision:
            return None, f"dependency {name} checkout revision differs from the manifest"
        status = _git_text(checkout, ["status", "--porcelain", "--untracked-files=all"])
        if status is None:
            return None, f"dependency {name} checkout is unavailable"
        if status:
            return None, f"dependency {name} checkout has local source changes"
        entries.extend((name, revision))
    return _identity_digest(entries), f"{len(entries) // 2} clean pinned dependency checkout(s)"


def _module_source_path(worktree: Path, module: str) -> Path:
    return worktree.joinpath(*module.split(".")).with_suffix(".lean")


def module_input_identities(
    worktree: Path,
    modules: Iterable[str],
    graph: Optional[dict[str, set[str]]],
) -> tuple[Optional[dict[str, str]], str]:
    """Digest each module and its transitive *local* import closure.

    Imports and bytes come from one source read per module.  In particular, a
    graph parsed before an admission wait cannot make a newly-added import
    disappear from the pre-launch or post-build snapshot.  Existing local
    source imports recurse; external imports are covered by the clean pinned
    dependency identity.  File bytes are cached per call, so several stale
    modules sharing imports do not rehash the repository.
    """
    wanted = sorted(set(str(module) for module in modules))
    if not wanted:
        return {}, "no stale module source inputs"
    closure_digests: dict[str, str] = {}
    visiting: set[str] = set()
    # The graph is not used to obtain imports — that would be stale after a
    # wait — but it can prove that a disappeared source used to be local.  An
    # old artifact for such a module is not an external dependency.
    expected_local = set((graph or {}).keys())

    def header_imports(content: bytes) -> Optional[set[str]]:
        """Read supported Lean import forms, or decline exact identity.

        This deliberately handles multi-import and visibility forms.  An
        unfamiliar import form is unknown rather than a partially hashed
        closure.  Block comments between imports remain inside the header.
        """
        imports: set[str] = set()
        saw_import = False
        block_depth = 0
        for index, raw_line in enumerate(content.decode("utf-8", errors="replace").splitlines()):
            line = raw_line.strip()
            if block_depth:
                block_depth += line.count("/-") - line.count("-/")
                if block_depth < 0:
                    return None
                if block_depth == 0 and "-/" in line and line.rsplit("-/", 1)[1].strip():
                    return None
                continue
            if line.startswith("/-"):
                block_depth = line.count("/-") - line.count("-/")
                if block_depth < 0:
                    return None
                if block_depth == 0 and "-/" in line and line.rsplit("-/", 1)[1].strip():
                    return None
                continue
            if not line or line.startswith("--"):
                continue
            match = _IMPORT_HEADER_RE.match(raw_line)
            if match:
                payload = match.group(1).split("--", 1)[0].strip()
                names = payload.split()
                while names and names[0] in {"public", "protected", "private", "meta"}:
                    names.pop(0)
                if not names or not all(_IMPORT_NAME_RE.fullmatch(name) for name in names):
                    return None
                imports.update(_normalise_module(name) for name in names)
                saw_import = True
                continue
            if "import" in line.split()[:3]:
                return None
            if saw_import or index >= _HEADER_SCAN_LINES:
                break
        return None if block_depth else imports

    def source_snapshot(module: str) -> Optional[tuple[str, set[str]]]:
        try:
            content = _module_source_path(worktree, module).read_bytes()
        except OSError:
            return None
        parsed = header_imports(content)
        if parsed is None:
            return None
        imports: set[str] = set()
        for imported in parsed:
            source = _module_source_path(worktree, imported)
            if source.is_file():
                imports.add(imported)
            elif imported in expected_local:
                return None
        return hashlib.sha256(content).hexdigest()[:16], imports

    def digest_module(module: str) -> Optional[str]:
        if module in closure_digests:
            return closure_digests[module]
        if module in visiting:
            return None
        visiting.add(module)
        snapshot = source_snapshot(module)
        if snapshot is None:
            visiting.remove(module)
            return None
        source_digest, imports = snapshot
        imported: list[str] = []
        for dependency in sorted(imports):
            dependency_digest = digest_module(dependency)
            if dependency_digest is None:
                visiting.remove(module)
                return None
            imported.extend((dependency, dependency_digest))
        visiting.remove(module)
        value = _identity_digest(["module", module, source_digest, *imported])
        closure_digests[module] = value
        return value

    result: dict[str, str] = {}
    for module in wanted:
        value = digest_module(module)
        if value is None:
            return None, f"source closure for {module} is incomplete"
        result[module] = value
    return result, f"{len(result)} module source closure(s)"


def build_input_identity(
    worktree: Path,
    modules: Iterable[str],
    graph: Optional[dict[str, set[str]]],
    digests: tuple[Optional[str], Optional[str]],
    threads: Any,
    resolved_toolchain: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """Collect exact-evidence inputs before admission and again before publish."""
    toolchain_digest, manifest_digest = digests
    if toolchain_digest is None or manifest_digest is None:
        return None, "toolchain or manifest digest unavailable"
    if not isinstance(threads, int) or isinstance(threads, bool) or threads <= 0:
        return None, "thread setting is not a positive integer"
    if resolved_toolchain is not None and not isinstance(resolved_toolchain, str):
        return None, "resolved toolchain identity is invalid"
    repository = repository_identity(worktree)
    if repository is None:
        return None, "linked-worktree repository identity unavailable"
    config = _lake_config_digest(worktree)
    if config is None:
        return None, "Lake configuration digest unavailable"
    dependencies, dependency_detail = _dependency_checkout_identity(worktree)
    if dependencies is None:
        return None, dependency_detail
    module_inputs, module_detail = module_input_identities(worktree, modules, graph)
    if module_inputs is None:
        return None, module_detail
    context = _identity_digest([
        "context", toolchain_digest, manifest_digest, config, dependencies,
        _execution_environment_digest(), resolved_toolchain or "unresolved-toolchain", str(threads),
    ])
    return {
        "repository_identity": repository,
        "input_context": context,
        "module_inputs": module_inputs,
    }, f"{module_detail}; {dependency_detail}; threads={threads}"


_STALE_FAILURE_RE = re.compile(r"^\s*-\s+([A-Za-z0-9_'.]+)\s*$")
# Lean allows a component of a module name to be written in guillemets, and
# Jaune's `Main.lean` does exactly that (`import «Jaune».Execution`).  Dropping
# such an import would under-report the closure and could widen a class on
# evidence that is not there, so the scanner reads them and normalises.
_IMPORT_RE = re.compile(r"^import\s+([A-Za-z0-9_'.\u00ab\u00bb]+)")
_IMPORT_HEADER_RE = re.compile(r"^\s*(?:(?:public|protected|private|meta)\s+)?import\s+(.+?)\s*$")
_IMPORT_NAME_RE = re.compile(r"[A-Za-z0-9_'.\u00ab\u00bb]+$")
_HEADER_SCAN_LINES = 400


def _normalise_module(name: str) -> str:
    return name.replace("\u00ab", "").replace("\u00bb", "")


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
                            imports.add(_normalise_module(match.group(1)))
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


def stale_closure_modules(
    graph: dict[str, set[str]],
    targets: Iterable[str],
    frontier: set[str],
) -> Optional[set[str]]:
    """Name the modules a build of ``targets`` would have to elaborate.

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
    return stale


def stale_closure(
    graph: dict[str, set[str]],
    targets: Iterable[str],
    frontier: set[str],
) -> Optional[int]:
    """Count the modules a build of ``targets`` would have to elaborate."""
    modules = stale_closure_modules(graph, targets, frontier)
    return None if modules is None else len(modules)


def stale_module_set(
    worktree: Path,
    targets: list[str],
    real_lake: Path,
    closure_roots: Optional[list[str]] = None,
    package_roots: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Name the modules a probe proves out of date, or explain why it cannot.

    Exit 0 means nothing is stale.  Exit 3 means Lake refused to build and
    named the out-of-date frontier; anything else is not evidence.  The result
    carries the closure itself (``modules``) and the package import graph the
    closure was computed over, because the estimate is sized from exactly
    those modules and their import order.
    """
    def failure(detail: str) -> dict[str, Any]:
        return {"stale": None, "detail": detail, "modules": None, "graph": None}

    try:
        completed = subprocess.run(
            [str(real_lake), "build", "--no-build", *targets],
            cwd=worktree, env=lake_env(), text=True, capture_output=True, check=False, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return failure(f"probe unavailable: {exc}")
    if completed.returncode == 0:
        return {
            "stale": 0, "detail": "probe reports the selected artifacts current",
            "modules": [], "graph": None,
        }
    if completed.returncode != STALE_EXIT:
        return failure(f"probe exited {completed.returncode}; not stale-set evidence")
    output = (completed.stdout or "") + (completed.stderr or "")
    lines = output.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines)
            if "logged failures" in line
        )
    except StopIteration:
        return failure("probe reported stale artifacts without naming a frontier")
    frontier = set()
    for line in lines[start + 1:]:
        match = _STALE_FAILURE_RE.match(line)
        if not match:
            break
        frontier.add(match.group(1))
    if not frontier:
        return failure("probe named no out-of-date module")
    roots = list(closure_roots if closure_roots is not None else targets)
    graph = package_import_graph(worktree, list(package_roots or []) + roots)
    if graph is None:
        return failure("package import graph unavailable")
    modules = stale_closure_modules(graph, roots, frontier)
    if modules is None:
        return failure("targets are outside the package import graph")
    return {
        "stale": len(modules),
        "detail": (
            f"probe frontier {sorted(frontier)}; {len(modules)} module(s) in the "
            f"closure of {roots} would be elaborated"
        ),
        "modules": sorted(modules),
        "graph": graph,
    }


def stale_module_count(
    worktree: Path,
    targets: list[str],
    real_lake: Path,
    closure_roots: Optional[list[str]] = None,
    package_roots: Optional[list[str]] = None,
) -> tuple[Optional[int], str]:
    """Count the modules a probe proves out of date, or explain why it cannot."""
    probe = stale_module_set(worktree, targets, real_lake, closure_roots, package_roots)
    return probe["stale"], probe["detail"]


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
            "stale": None, "detail": resolution, "stale_set": None, "graph": None,
        }
    probe = stale_module_set(worktree, targets, real_lake, closure_roots, package_roots)
    return {
        "roots": closure_roots, "package_roots": package_roots,
        "resolution": resolution, "stale": probe["stale"], "detail": probe["detail"],
        "stale_set": probe["modules"], "graph": probe["graph"],
    }


def _finite_positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)) and float(value) > 0.0
    )


def _usable_sample(row: dict[str, Any]) -> bool:
    if not _finite_positive(row.get("peak_rss_mib")):
        return False
    samples = row.get("sampling_samples")
    if not isinstance(samples, int) or isinstance(samples, bool) or samples <= 0:
        return False
    unavailable = row.get("sampling_unavailable")
    return (
        unavailable is None
        or (isinstance(unavailable, (int, float)) and not isinstance(unavailable, bool)
            and math.isfinite(float(unavailable)) and float(unavailable) >= 0.0)
    )


def _exact_context_row(row: dict[str, Any], identity: dict[str, Any]) -> bool:
    """Whether a row measured the same repository/configuration/execution mode."""
    if row.get("identity_status") != "exact":
        return False
    if (
        row.get("repository_identity") != identity.get("repository_identity")
        or row.get("input_context") != identity.get("input_context")
        or not isinstance(row.get("module_inputs"), dict)
    ):
        return False
    if not isinstance(row.get("threads"), int) or isinstance(row.get("threads"), bool) or row["threads"] <= 0:
        return False
    return _usable_sample(row)


def _exact_module_row(row: dict[str, Any], identity: dict[str, Any], module: str) -> bool:
    inputs = identity.get("module_inputs")
    return (
        _exact_context_row(row, identity)
        and isinstance(inputs, dict)
        and inputs.get(module) == (row.get("module_inputs") or {}).get(module)
    )


def _evidence_rows(
    worktree: Path,
    toolchain_digest: Optional[str],
    manifest_digest: Optional[str],
    input_identity: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], str]:
    """Successful measured rows in the relevant repository evidence cohort.

    Old callers retain the former same-worktree behaviour.  Current build
    paths provide an identity: exact rows may then cross compatible linked
    worktrees, while rows with a different repository identity never enter the
    cohort.  Rows lacking the additive identity fields are retained only from
    the same worktree as conservative legacy fallback.
    """
    try:
        rows, _corrupt = read_ledger("30d")
    except (OSError, ValueError):
        # Unreadable performance state is not evidence; it must never widen
        # admission, so the caller falls back to the conservative class.
        return [], "ledger unreadable"
    if toolchain_digest is None or manifest_digest is None:
        return [], "worktree toolchain or manifest digest unavailable"
    measured = [
        row for row in rows
        if row.get("kind") == "build"
        and not row.get("probe")
        and row.get("exit") == 0
        and _finite_positive(row.get("peak_rss_mib"))
        and (row.get("identity_status") != "exact" or _usable_sample(row))
    ]
    if input_identity is None:
        matching = [
            row for row in measured
            if str(row.get("worktree")) == str(worktree)
            and row.get("toolchain_digest") == toolchain_digest
            and row.get("manifest_digest") == manifest_digest
        ]
        detail = f"{len(matching)} measured row(s) on the pinned inputs"
    else:
        repository = input_identity.get("repository_identity")
        matching = []
        legacy_scopes: dict[str, Optional[str]] = {}
        for row in measured:
            row_repository = row.get("repository_identity")
            if isinstance(row_repository, str):
                if row_repository == repository:
                    matching.append(row)
                continue
            recorded = str(row.get("worktree") or "")
            if recorded not in legacy_scopes:
                legacy_scopes[recorded] = _legacy_repository_identity(recorded)
            # A legacy row may survive a removed linked worktree when Git can
            # still prove that its `.worktrees/GOAL` parent belongs to this
            # repository.  It remains fallback only: it cannot satisfy
            # `_exact_context_row` without the new fields.
            if (
                legacy_scopes[recorded] == repository
                and row.get("toolchain_digest") == toolchain_digest
                and row.get("manifest_digest") == manifest_digest
            ):
                matching.append(row)
        exact_context = sum(1 for row in matching if _exact_context_row(row, input_identity))
        detail = (
            f"{len(matching)} repository-scoped measurement(s); "
            f"{exact_context} exact configuration/execution row(s)"
        )
    matching.sort(key=lambda row: str(row["time"]))
    return matching, detail


def _measured_rows(
    worktree: Path,
    targets: list[str],
    toolchain_digest: Optional[str],
    manifest_digest: Optional[str],
    settings: dict[str, int],
    require_elaboration: bool = False,
    members: bool = False,
    input_identity: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], str]:
    """Ledger rows that measured *this* worktree, targets, and pinned inputs.

    This is the target-keyed fallback, used only when the probe could not
    name the stale set.  ``members`` widens the match from the exact list to
    any row whose targets are a non-empty subset of it, so a list is sized
    from the union of its members' rows rather than starting unmeasured.
    ``require_elaboration`` keeps only rows that rebuilt at least one module.
    A row that restored everything from the artifact cache measured a build
    that elaborated nothing; it cannot size one that will.
    """
    rows, detail = _evidence_rows(worktree, toolchain_digest, manifest_digest, input_identity)
    if not rows and detail in {"ledger unreadable", "worktree toolchain or manifest digest unavailable"}:
        return [], detail
    wanted = set(targets)
    matching = [
        row for row in rows
        if list(row.get("targets") or []) == list(targets)
        or (
            members
            and wanted
            and row.get("targets")
            and set(row["targets"]) <= wanted
        )
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


def module_cost_evidence(
    rows: list[dict[str, Any]], settings: dict[str, int],
    input_identity: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Per-module cost from the ledger: what each module's `lean` process peaked at.

    A row that recorded ``module_peak_mib`` measured each module directly.  An
    older narrow row — at most ``tolerant_module_count`` modules rebuilt —
    bounds every module in it by the largest `lean` process it saw.  A broad
    row without per-module peaks measures no single module: its peak is the
    breadth and the concurrency, not any one member.  ``module_seconds`` is
    kept from every row, broad or narrow, because elaboration time is the one
    signal a broad row does give about a single module.
    """
    narrow = int(settings["tolerant_module_count"])
    sample = int(settings["estimate_sample_rows"])
    peaks: dict[str, list[float]] = {}
    fallback_peaks: dict[str, list[float]] = {}
    exact_seconds: dict[str, float] = {}
    fallback_seconds: dict[str, float] = {}
    overheads: list[float] = []
    concurrency = 1
    context_rows = 0
    for row in rows:
        rebuilt = [str(module) for module in (row.get("modules_rebuilt") or [])]
        if not rebuilt:
            continue
        peak_gib = float(row["peak_rss_mib"]) / 1024.0
        lean_peak = row.get("peak_lean_rss_mib")
        lean_gib = float(lean_peak) / 1024.0 if _finite_positive(lean_peak) else None
        recorded = row.get("module_peak_mib") or {}
        for module, value in (row.get("module_seconds") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0.0:
                name = str(module)
                fallback_seconds[name] = max(fallback_seconds.get(name, 0.0), float(value))
                if input_identity is None or _exact_module_row(row, input_identity, name):
                    exact_seconds[name] = max(exact_seconds.get(name, 0.0), float(value))
        # A row from the same repository but a different source identity is
        # not a measured current module.  Its direct module peak is still a
        # conservative lower bound if current evidence is absent.  It never
        # makes `unmeasured` disappear or relaxes the contention class.
        for module in rebuilt:
            value = recorded.get(module) if isinstance(recorded, dict) else None
            if _finite_positive(value):
                fallback_peaks.setdefault(module, []).append(float(value) / 1024.0)
            elif len(rebuilt) <= narrow:
                fallback_peaks.setdefault(module, []).append(
                    lean_gib if lean_gib is not None else peak_gib
                )
        if input_identity is not None and not _exact_context_row(row, input_identity):
            continue
        context_rows += 1
        seen = row.get("max_concurrent_lean")
        seen = int(seen) if isinstance(seen, int) and not isinstance(seen, bool) and seen > 0 else 1
        concurrency = max(concurrency, seen)
        if lean_gib is not None and seen == 1:
            overheads.append(max(0.0, peak_gib - lean_gib))
        for module in rebuilt:
            if input_identity is not None and not _exact_module_row(row, input_identity, module):
                continue
            value = recorded.get(module) if isinstance(recorded, dict) else None
            if _finite_positive(value):
                cost = float(value) / 1024.0
            elif len(rebuilt) <= narrow:
                cost = lean_gib if lean_gib is not None else peak_gib
            else:
                continue
            peaks.setdefault(module, []).append(cost)
    return {
        "lean_peak_gib": {
            module: max(values[-sample:]) for module, values in peaks.items()
        },
        "fallback_peak_gib": {
            module: max(values[-sample:]) for module, values in fallback_peaks.items()
            if module not in peaks
        },
        "seconds": {
            module: exact_seconds.get(module, fallback)
            for module, fallback in fallback_seconds.items()
        },
        "overhead_gib": max(overheads[-sample:]) if overheads else DEFAULT_LAKE_OVERHEAD_GIB,
        "overhead_measured": bool(overheads),
        "concurrency": concurrency,
        "rows": context_rows if input_identity is not None else len(rows),
        "exact_modules": sorted(peaks),
    }


def _reaches(graph: dict[str, set[str]], origin: str, limit: set[str]) -> set[str]:
    """Modules of ``limit`` that ``origin`` transitively imports."""
    found: set[str] = set()
    stack = list(graph.get(origin, ()))
    seen: set[str] = set()
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        if module in limit:
            found.add(module)
        stack.extend(graph.get(module, ()))
    return found


def stale_set_width(
    modules: Iterable[str], graph: Optional[dict[str, set[str]]], cap: int
) -> int:
    """How many of these modules Lake could elaborate at the same time.

    Two modules can overlap only when neither imports the other, so the answer
    is the largest antichain of the import order restricted to the set.  It
    is computed exactly for a small set and bounded by ``cap`` — the
    concurrency the ledger has actually observed — for a large one.
    """
    names = sorted(set(modules))
    if len(names) <= 1:
        return len(names)
    if graph is None or len(names) > _WIDTH_BRUTE_FORCE_LIMIT:
        return max(1, min(cap, len(names)))
    limit = set(names)
    below = {name: _reaches(graph, name, limit) for name in names}
    comparable = {
        (a, b) for a in names for b in names
        if a != b and (b in below[a] or a in below[b])
    }
    best = 1
    count = len(names)
    for mask in range(1, 1 << count):
        chosen = [names[index] for index in range(count) if mask >> index & 1]
        if len(chosen) <= best:
            continue
        if all((a, b) not in comparable for a in chosen for b in chosen if a < b):
            best = len(chosen)
    return max(1, min(cap, best))


def _model_peak(
    measured: dict[str, float],
    graph: Optional[dict[str, set[str]]],
    evidence: dict[str, Any],
) -> tuple[float, int, list[float]]:
    """Lake overhead plus the `lean` peaks that can run at once."""
    cap = max(int(evidence["concurrency"]), 2 if len(measured) > 1 else 1)
    width = stale_set_width(measured, graph, cap)
    top = sorted(measured.values(), reverse=True)[:width]
    return float(evidence["overhead_gib"]) + sum(top), width, top


def _covering_rows(
    rows: list[dict[str, Any]], stale: list[str], unmeasured: list[str]
) -> list[dict[str, Any]]:
    """Broader successful rebuilds that included nearly all the unmeasured modules."""
    wanted = set(unmeasured)
    covering = []
    for row in rows:
        rebuilt = set(row.get("modules_rebuilt") or [])
        if len(rebuilt) < len(stale):
            continue
        if wanted and len(rebuilt & wanted) < 0.9 * len(wanted):
            continue
        covering.append(row)
    return covering


def size_stale_set(
    stale: list[str],
    graph: Optional[dict[str, set[str]]],
    rows: list[dict[str, Any]],
    settings: dict[str, int],
    default_gib: int,
    input_identity: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Size a build from the modules it will elaborate, never from a target's name.

    Every module in the stale set with a measured `lean` peak contributes it;
    the build's peak is the Lake overhead plus the peaks that can run at the
    same time.  When some module is unmeasured: a small set whose members
    never elaborated for long takes the narrow default; a small set with a
    member that elaborated for ``heavy_module_seconds`` or more in some broad
    rebuild takes the profile default, because that member is a heavy one
    whose cost is simply not yet recorded on its own; a large set is bounded
    by the tightest broader rebuild that included its members, and by the
    profile default when none did.
    """
    floor = int(settings["minimum_estimate_gib"])
    margin = int(settings["estimate_margin_gib"])
    limit = int(settings["tolerant_module_count"])
    narrow_default = int(settings["narrow_default_gib"])
    heavy_seconds = float(settings["heavy_module_seconds"])
    evidence = module_cost_evidence(rows, settings, input_identity)
    names = sorted(set(stale))
    measured = {name: evidence["lean_peak_gib"][name] for name in names if name in evidence["lean_peak_gib"]}
    unmeasured = [name for name in names if name not in measured]
    fallback = {
        name: evidence["fallback_peak_gib"][name]
        for name in unmeasured if name in evidence["fallback_peak_gib"]
    }
    fallback_floor = max(fallback.values(), default=0.0)
    heavy = sorted(
        ((name, evidence["seconds"][name]) for name in unmeasured
         if evidence["seconds"].get(name, 0.0) >= heavy_seconds),
        key=lambda item: -item[1],
    )
    result: dict[str, Any] = {
        "stale_modules": len(names),
        "measured": sorted(measured),
        "unmeasured": unmeasured,
        "heavy": [name for name, _seconds in heavy],
        "fallback_modules": sorted(fallback),
        "fallback_peak_gib": round(fallback_floor, 2),
        "overhead_gib": round(float(evidence["overhead_gib"]), 2),
        "rows": evidence["rows"],
    }
    if not names:
        result.update({
            "kind": "fresh", "peak_gib": 0.0, "estimate_gib": floor,
            "source": "nothing is stale; the build elaborates no module and takes no hold",
        })
        return result

    def listed(items: list[str]) -> str:
        shown = ", ".join(items[:3])
        return shown + (f", … ({len(items)} in all)" if len(items) > 3 else "")

    if not unmeasured:
        peak, width, top = _model_peak(measured, graph, evidence)
        result.update({
            "kind": "measured",
            "peak_gib": round(peak, 2),
            "estimate_gib": max(floor, math.ceil(peak) + margin),
            "width": width,
            "source": (
                f"measured stale set: {len(names)} module(s) all measured; Lake overhead "
                f"{evidence['overhead_gib']:.2f} GiB + {width} concurrent lean peak(s) "
                f"{[round(value, 2) for value in top]} GiB = {peak:.2f} GiB, plus {margin} GiB"
            ),
        })
        return result

    measured_peak = 0.0
    if measured:
        measured_peak, _width, _top = _model_peak(measured, graph, evidence)
    if len(names) <= limit and not heavy:
        estimate = max(
            floor, narrow_default,
            math.ceil(measured_peak) + margin if measured else 0,
            math.ceil(fallback_floor) + margin if fallback else 0,
        )
        result.update({
            "kind": "narrow default",
            "peak_gib": round(measured_peak, 2),
            "estimate_gib": estimate,
            "source": (
                f"narrow default {narrow_default} GiB: {len(unmeasured)} of {len(names)} stale "
                f"module(s) unmeasured ({listed(unmeasured)}) and none of them elaborated for "
                f"{heavy_seconds:.0f}s or more in any measured rebuild"
            ),
        })
        return result
    if len(names) <= limit:
        name, seconds = heavy[0]
        estimate = max(
            floor, int(default_gib),
            math.ceil(measured_peak) + margin if measured else 0,
            math.ceil(fallback_floor) + margin if fallback else 0,
        )
        result.update({
            "kind": "heavy module",
            "peak_gib": round(measured_peak, 2),
            "estimate_gib": estimate,
            "source": (
                f"profile default (heavy module): {name} elaborated for {seconds:.0f}s in a "
                f"broad rebuild but has no measurement of its own; {len(unmeasured)} of "
                f"{len(names)} stale module(s) unmeasured"
            ),
        })
        return result
    context_rows = (
        [row for row in rows if _exact_context_row(row, input_identity)]
        if input_identity is not None else rows
    )
    covering = _covering_rows(context_rows, names, unmeasured)
    if covering:
        tightest = min(covering, key=lambda row: float(row["peak_rss_mib"]))
        peak = float(tightest["peak_rss_mib"]) / 1024.0
        peak = max(peak, measured_peak, fallback_floor)
        result.update({
            "kind": "broader rebuild",
            "peak_gib": round(peak, 2),
            "estimate_gib": max(floor, math.ceil(peak) + margin),
            "covering_rows": len(covering),
            "covering_time": str(tightest.get("time")),
            "source": (
                f"broader rebuild: {len(unmeasured)} of {len(names)} stale module(s) unmeasured; "
                f"the tightest of {len(covering)} successful rebuild(s) of at least {len(names)} "
                f"modules that included them ({len(tightest.get('modules_rebuilt') or [])} modules "
                f"at {str(tightest.get('time'))}) peaked at {peak:.2f} GiB, plus {margin} GiB"
            ),
        })
        return result
    estimate = max(
        floor, int(default_gib), math.ceil(measured_peak) + margin if measured else 0,
        math.ceil(fallback_floor) + margin if fallback else 0,
    )
    result.update({
        "kind": "profile default",
        "peak_gib": round(measured_peak, 2),
        "estimate_gib": estimate,
        "source": (
            f"profile default: {len(unmeasured)} of {len(names)} stale module(s) unmeasured "
            f"({listed(unmeasured)}), the set is above the narrow limit of {limit}, and no "
            "broader successful rebuild included them"
        ),
    })
    return result


def _target_keyed_estimate(
    worktree: Path,
    targets: list[str],
    settings: dict[str, int],
    digests: tuple[Optional[str], Optional[str]],
    default_gib: int,
    stale_modules: Optional[int],
    input_identity: Optional[dict[str, Any]] = None,
    identity_detail: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """The fallback when the probe could not name the stale set.

    A build with nothing stale may be sized by any successful row of the same
    targets; a build whose stale set could not be measured is sized only by
    rows that themselves elaborated a module, because a cache-restored row
    measures a build that did no work.  A list with no row of its own is
    sized from the union of its members' rows.
    """
    floor = int(settings["minimum_estimate_gib"])
    require_elaboration = stale_modules != 0
    rows, detail = _measured_rows(
        worktree, targets, *digests, settings, require_elaboration, members=True,
        input_identity=input_identity,
    )
    fallback_rows = list(rows)
    if input_identity is None and identity_detail is not None:
        fallback_peak = max(
            (float(row["peak_rss_mib"]) for row in fallback_rows), default=0.0,
        ) / 1024.0
        estimate = max(floor, default_gib, math.ceil(fallback_peak) + int(settings["estimate_margin_gib"]))
        return estimate, {
            "kind": "profile default",
            "source": (
                f"profile default (exact input identity unavailable: {identity_detail})"
                + (f"; conservative target fallback peak {fallback_peak:.2f} GiB" if fallback_peak else "")
            ),
            "rows": len(fallback_rows),
            "keyed_on_elaboration": require_elaboration,
            "measured_peak_gib": round(fallback_peak, 2) if fallback_peak else None,
        }
    if input_identity is not None:
        rows = [row for row in rows if _exact_context_row(row, input_identity)]
        if not rows:
            detail = "no exact configuration/execution measurement for these targets"
    if not rows:
        fallback_peak = max(
            (float(row["peak_rss_mib"]) for row in fallback_rows), default=0.0,
        ) / 1024.0
        estimate = max(floor, default_gib, math.ceil(fallback_peak) + int(settings["estimate_margin_gib"]))
        return estimate, {
            "kind": "profile default",
            "source": (
                f"profile default ({identity_detail or detail})"
                + (
                    f"; conservative target fallback peak {fallback_peak:.2f} GiB"
                    if fallback_peak else ""
                )
            ),
            "rows": 0,
            "keyed_on_elaboration": require_elaboration,
            "measured_peak_gib": round(fallback_peak, 2) if fallback_peak else None,
            "estimate_gib": estimate,
        }
    peak_gib = max(float(row["peak_rss_mib"]) for row in rows) / 1024.0
    estimate = max(floor, math.ceil(peak_gib) + int(settings["estimate_margin_gib"]))
    exact = [row for row in rows if list(row.get("targets") or []) == list(targets)]
    return estimate, {
        "kind": "target rows",
        "source": (
            f"target rows: max of {len(rows)} measured peak(s) ({peak_gib:.2f} GiB) "
            f"plus {settings['estimate_margin_gib']} GiB"
            + (" from rows that elaborated" if require_elaboration else "")
            + ("" if len(exact) == len(rows) else f"; {len(rows) - len(exact)} from member targets")
        ),
        "rows": len(rows),
        "keyed_on_elaboration": require_elaboration,
        "measured_peak_gib": round(peak_gib, 2),
        "row_times": [str(row["time"]) for row in rows],
    }


def classify_contention(
    worktree: Path,
    targets: list[str],
    real_lake: Path,
    settings: dict[str, int],
    digests: tuple[Optional[str], Optional[str]],
    stale: Optional[dict[str, Any]] = None,
    input_identity: Optional[dict[str, Any]] = None,
    identity_detail: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Choose a contention class from measurement, defaulting to `sensitive`.

    `tolerant` requires all three: a small stale set now, every module in it
    measured on the same toolchain and Lake manifest, and a modelled peak
    below the configured threshold.  The model is the one the estimate uses,
    so the class and the estimate always describe the same stale set.  Any
    missing, drifted, or unreadable evidence keeps the conservative class;
    evidence can only ever relax scheduling, never a floor.
    """
    evidence: dict[str, Any] = {}
    probe = stale if stale is not None else stale_evidence(worktree, targets, real_lake)
    stale_count = probe["stale"]
    evidence["resolved_roots"] = probe["roots"]
    evidence["resolution"] = probe["resolution"]
    evidence["stale_modules"] = stale_count
    evidence["stale_detail"] = probe["detail"]
    if input_identity is None and identity_detail is not None:
        evidence["reason"] = f"exact input identity unavailable: {identity_detail}"
        return "sensitive", evidence
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
    threshold = float(settings["tolerant_peak_gib"])
    stale_set = probe.get("stale_set")
    if stale_set is None:
        # The probe counted but did not name the modules: the target-keyed
        # fallback is the only evidence there is.
        rows, rows_detail = _measured_rows(
            worktree, targets, *digests, settings, members=True,
            input_identity=input_identity,
        )
        evidence["measurements"] = rows_detail
        if not rows:
            evidence["reason"] = rows_detail
            return "sensitive", evidence
        peak_gib = max(float(row["peak_rss_mib"]) for row in rows) / 1024.0
        evidence["measured_peak_gib"] = round(peak_gib, 2)
        if peak_gib >= threshold:
            evidence["reason"] = f"measured peak {peak_gib:.2f} GiB is not below {threshold} GiB"
            return "sensitive", evidence
        evidence["reason"] = (
            f"{stale_count} stale module(s) at or below {limit} and a measured peak of "
            f"{peak_gib:.2f} GiB below {threshold} GiB on the pinned toolchain and manifest"
        )
        return "tolerant", evidence
    if stale_count == 0:
        evidence["reason"] = "nothing is stale; the build elaborates no module and takes no hold"
        evidence["measured_peak_gib"] = 0.0
        return "tolerant", evidence
    rows, rows_detail = _evidence_rows(worktree, *digests, input_identity)
    evidence["measurements"] = rows_detail
    if not rows and rows_detail in {"ledger unreadable", "worktree toolchain or manifest digest unavailable"}:
        evidence["reason"] = rows_detail
        return "sensitive", evidence
    sizing = size_stale_set(
        list(stale_set), probe.get("graph"), rows, settings, 0, input_identity,
    )
    evidence["measured_peak_gib"] = sizing["peak_gib"]
    evidence["sizing"] = sizing["kind"]
    if sizing["unmeasured"]:
        shown = ", ".join(sizing["unmeasured"][:3])
        evidence["reason"] = (
            f"{len(sizing['unmeasured'])} of {stale_count} stale module(s) unmeasured on the "
            f"pinned inputs ({shown}{', …' if len(sizing['unmeasured']) > 3 else ''})"
        )
        return "sensitive", evidence
    if sizing["peak_gib"] >= threshold:
        evidence["reason"] = (
            f"modelled peak {sizing['peak_gib']:.2f} GiB is not below {threshold} GiB"
        )
        return "sensitive", evidence
    evidence["reason"] = (
        f"{stale_count} stale module(s) at or below {limit}, all measured, and a modelled "
        f"peak of {sizing['peak_gib']:.2f} GiB below {threshold} GiB on the pinned "
        "toolchain and manifest"
    )
    return "tolerant", evidence


def derive_memory_gib(
    worktree: Path,
    targets: list[str],
    settings: dict[str, int],
    digests: tuple[Optional[str], Optional[str]],
    default_gib: int,
    stale_modules: Optional[int] = None,
    stale: Optional[dict[str, Any]] = None,
    input_identity: Optional[dict[str, Any]] = None,
    identity_detail: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """Propose a whole-GiB estimate from measurement, never below the floor.

    ``stale`` is the probe's evidence for this build.  When it names the stale
    set, the estimate is sized from those modules' own measured cost, whatever
    the target list was called; the spelling of a target list is not evidence
    about what it will elaborate.  Without a named set — the probe failed, or
    a caller stated the class and no probe ran — the target-keyed fallback
    applies, keyed on elaboration exactly as before.
    """
    floor = int(settings["minimum_estimate_gib"])
    stale_set = stale.get("stale_set") if stale is not None else None
    if stale_set is None:
        count = stale["stale"] if stale is not None else stale_modules
        return _target_keyed_estimate(
            worktree, targets, settings, digests, default_gib, count, input_identity,
            identity_detail,
        )
    if input_identity is None and identity_detail is not None:
        rows, detail = _evidence_rows(worktree, *digests)
        fallback = size_stale_set(list(stale_set), stale.get("graph"), rows, settings, default_gib)
        estimate = max(floor, default_gib, int(fallback["estimate_gib"]))
        return estimate, {
            "kind": "profile default",
            "source": (
                f"profile default (exact input identity unavailable: {identity_detail}); "
                f"compatibility fallback: {fallback['source']}"
            ),
            "rows": fallback["rows"],
            "keyed_on_elaboration": True,
            "measured_peak_gib": fallback["peak_gib"],
        }
    rows, detail = _evidence_rows(worktree, *digests, input_identity)
    if not rows and detail in {"ledger unreadable", "worktree toolchain or manifest digest unavailable"}:
        return max(floor, default_gib), {
            "kind": "profile default",
            "source": f"profile default ({detail})",
            "rows": 0,
            "keyed_on_elaboration": True,
        }
    sizing = size_stale_set(
        list(stale_set), stale.get("graph"), rows, settings, default_gib, input_identity,
    )
    return int(sizing["estimate_gib"]), {
        "kind": sizing["kind"],
        "source": sizing["source"],
        "rows": sizing["rows"],
        "keyed_on_elaboration": True,
        "measured_peak_gib": sizing["peak_gib"],
        "stale_modules": sizing["stale_modules"],
        "measured_modules": len(sizing["measured"]),
        "unmeasured_modules": sizing["unmeasured"][:12],
        "heavy_modules": sizing["heavy"],
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


def _stale_line(probe: Optional[dict[str, Any]]) -> str:
    """One line naming the whole stale closure, not only Lake's frontier."""
    if probe is None:
        return "stale: not probed"
    modules = probe.get("stale_set")
    if probe.get("roots") is None or modules is None:
        return f"stale: unmeasured — {probe.get('detail')}"
    if not modules:
        return "stale: 0 module(s); every selected artifact is current"
    return (
        f"stale: {len(modules)} module(s) in the closure of {probe.get('roots')} would be "
        f"elaborated: {', '.join(modules)}"
    )


def _stale_fields(probe: Optional[dict[str, Any]]) -> dict[str, Any]:
    if probe is None:
        return {}
    fields: dict[str, Any] = {}
    if isinstance(probe.get("stale"), int) and not isinstance(probe.get("stale"), bool):
        fields["stale_modules"] = int(probe["stale"])
    detail = probe.get("detail")
    modules = probe.get("stale_set")
    if isinstance(detail, str) and detail:
        fields["stale_detail"] = detail + (f": {', '.join(modules)}" if modules else "")
    if isinstance(probe.get("roots"), list):
        fields["resolved_roots"] = [str(root) for root in probe["roots"]]
    return fields


def _estimate_note(estimate: dict[str, Any]) -> str:
    """What the fit line says the estimate is, so a waiter blames the right cause."""
    source = str(estimate.get("source") or "explicit")
    if source != "explicit":
        return f"derived: {source}"
    derived = estimate.get("derived_gib")
    if derived is None:
        return "explicit --memory-gib"
    return (
        f"explicit --memory-gib {estimate.get('explicit_gib')}; the evidence supports "
        f"{derived} GiB ({estimate.get('derived_source')})"
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


def _process_group_alive(pgid: int) -> Optional[bool]:
    """Report observed group liveness, or ``None`` when access is denied."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def _terminate_process_group(proc: subprocess.Popen[str], timeout: float = 10.0) -> bool:
    """Stop the wrapper-owned process group and prove it is gone."""
    pgid = proc.pid
    if _process_group_alive(pgid) is not False:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            # A denied signal proves neither liveness nor cleanup.  Continue
            # through the wait and final ESRCH probe, but never release a hold
            # merely because this host could not inspect or signal the group.
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
    deadline = time.monotonic() + timeout
    while _process_group_alive(pgid) is not False and time.monotonic() < deadline:
        time.sleep(0.05)
    return _process_group_alive(pgid) is False


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
        real_lake, real_lean, sysroot = resolve_toolchain(worktree)
    except RuntimeError as exc:
        print(json.dumps({"status": "REFUSED", "detail": str(exc)}, sort_keys=True), file=output)
        return GUARD_REFUSAL_EXIT
    digests = worktree_digests(worktree)
    toolchain_identity = resolved_toolchain_identity(real_lake, real_lean, sysroot)
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

    input_identity: Optional[dict[str, Any]] = None
    identity_detail = "stale source closure was not available"

    def input_snapshot(
        snapshot_digests: Optional[tuple[Optional[str], Optional[str]]] = None,
    ) -> tuple[Optional[dict[str, Any]], str]:
        probe_state = stale()
        modules = probe_state.get("stale_set")
        if modules is None:
            return None, "stale source closure was not available"
        return build_input_identity(
            worktree, modules, probe_state.get("graph"), snapshot_digests or digests, threads,
            toolchain_identity,
        )

    if census:
        contention = "exclusive"
        evidence["reason"] = "a dependency census rebuilds the full closure"
    else:
        # This snapshot is the evidence used for selection.  It is taken after
        # the probe and rechecked after any admission wait, before Lake starts.
        input_identity, identity_detail = input_snapshot()
    if not census and contention is None:
        contention, evidence = classify_contention(
            worktree, targets, real_lake, settings(), digests, stale(),
            input_identity, identity_detail,
        )
    elif not census:
        # The class is stated, but the probe still runs: it is what tells the
        # wrapper that nothing is stale and no hold is needed, and it is what
        # the estimate is sized from.  One Lake `--no-build` costs a second.
        # `input_snapshot` above already ran that one probe.
        pass
    estimate_evidence: dict[str, Any] = {}
    # The estimate reuses the class's probe: the two answers describe one
    # stale set.
    measured_stale = probe_evidence["stale"] if probe_evidence is not None else None
    if memory_gib is None:
        memory_gib, estimate_evidence = derive_memory_gib(
            worktree, targets, settings(), digests, DEFAULT_MEMORY_GIB, measured_stale,
            probe_evidence, input_identity, identity_detail,
        )
    elif probe_evidence is not None:
        # An explicit estimate is honoured, but the reader is told what the
        # evidence would have proposed: a larger one is charged 1.25x and can
        # be passed over by every smaller request on a busy host.
        derived, derived_evidence = derive_memory_gib(
            worktree, targets, settings(), digests, DEFAULT_MEMORY_GIB, measured_stale,
            probe_evidence, input_identity, identity_detail,
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
    else:
        estimate_evidence = {"source": "explicit", "explicit_gib": memory_gib}
    stale_line = _stale_line(probe_evidence)

    lake_args = [str(real_lake), "build"]
    if probe:
        lake_args.append("--no-build")
    else:
        lake_args.append("--verbose")
    lake_args.extend(targets)
    if probe:
        started = time.monotonic()
        completed = subprocess.run(lake_args, cwd=worktree, env=lake_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        wall = time.monotonic() - started
        print(completed.stdout, end="", file=output)
        # Lake names only the frontier it stopped at; the closure behind it is
        # what a build would elaborate, so it is printed in full.
        print(stale_line, file=output)
        append_ledger({
            "kind": "build", "worktree": str(worktree), "goal": goal, "targets": targets,
            "command": lake_args, "exit": completed.returncode, "wall_seconds": round(wall, 3),
            "threads": threads, "probe": True, "admission": "NOT_REQUIRED_NO_BUILD",
            "contention": contention, "modules_rebuilt": [], "modules_restored": [],
            "module_hashes": {}, "module_seconds": {},
            **_stale_fields(probe_evidence),
            **_digest_fields(digests),
        })
        state = "fresh" if completed.returncode == 0 else "stale" if completed.returncode == STALE_EXIT else "error"
        summary = {"status": state.upper(), "exit": completed.returncode}
        if probe_evidence is not None:
            summary["stale_modules"] = probe_evidence["stale"]
            summary["stale_set"] = probe_evidence.get("stale_set")
            summary["estimate"] = estimate_evidence
            summary["memory_gib"] = memory_gib
        print(json.dumps(summary, sort_keys=True), file=output)
        return completed.returncode

    fresh = (
        probe_evidence is not None
        and probe_evidence["stale"] == 0
        and not census
    )
    if fresh:
        # Nothing is stale: the build restores or links, and elaborates no
        # module.  It takes no hold, so a session behind a fresh checkpoint
        # is never queued for a one-second no-op.
        admitted, admission = True, "NOT_REQUIRED_FRESH"
        print(
            "admission: the probe reports every selected artifact current; this build "
            "elaborates nothing and takes no hold",
            file=output,
        )
    else:
        admitted, admission = semaphore.adaptive_acquire(
            goal,
            "classified lake build",
            semaphore.ADAPTIVE_LEASE_SECONDS,
            memory_gib=memory_gib,
            contention=contention,
            wait_seconds=wait_seconds,
            estimate_source=_estimate_note(estimate_evidence),
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
            release_hold()
            print(json.dumps({
                "status": "REFUSED",
                "detail": f"dependency census aborted before building: {dependency_detail}",
                "exit": update.returncode,
            }, sort_keys=True), file=output)
            return update.returncode or 2
        digests = worktree_digests(worktree)
    def release_hold() -> tuple[bool, str]:
        if fresh:
            return True, "no hold was taken"
        return semaphore.adaptive_release(goal)

    if not census and input_identity is not None:
        launch_digests = worktree_digests(worktree)
        launch_identity, launch_detail = input_snapshot(launch_digests)
        if launch_identity != input_identity:
            released, release_detail = release_hold()
            print(json.dumps({
                "status": "REFUSED",
                "detail": (
                    "SOURCE_CHANGED_REPROBE: build inputs changed after sizing and before "
                    f"Lake launch ({launch_detail}); {release_detail}"
                ),
                "admission": admission,
            }, sort_keys=True), file=output)
            return 2 if released else GUARD_REFUSAL_EXIT
        digests = launch_digests

    try:
        priority_launcher = guard_bin() / "nice"
    except (OSError, RuntimeError) as exc:
        release_hold()
        print(json.dumps({"status": "REFUSED", "detail": f"priority launcher is unavailable: {exc}"}, sort_keys=True), file=output)
        return GUARD_REFUSAL_EXIT
    args = [str(priority_launcher), "-n", "10", *lake_args]
    before = _swap_gib()
    started = time.monotonic()
    env = lake_env()
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
        sampler = ProcessSampler(proc.pid, worktree=worktree)
        sampler.start()
        if not fresh:
            renewer = RenewalThread(goal, proc)
            renewer.start()
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            print(line, end="", file=output)
        exit_code = proc.wait()
        if renewer is not None and renewer.refused:
            exit_code = exit_code or 2
            cleanup_proved = renewer.cleanup_proved
    except KeyboardInterrupt:
        interrupted = True
        exit_code = 128 + termination_signal if termination_signal else 130
    finally:
        if proc is not None and _process_group_alive(proc.pid) is not False:
            cleanup_proved = _terminate_process_group(proc) and cleanup_proved
        if sampler:
            sampler.stop()
        if renewer:
            renewer.stop()
    wall = time.monotonic() - started
    after = _swap_gib()
    rebuilt, restored, module_seconds = _parse_build_output(lines)
    hashes = _module_hashes(worktree, rebuilt)
    identity_fields: dict[str, Any] = {}
    record_digests = worktree_digests(worktree)
    if census:
        identity_status = "census dependency update is not exact measurement evidence"
    elif exit_code != 0:
        identity_status = "non-successful build is not exact measurement evidence"
    elif not rebuilt:
        identity_status = "no elaboration is not exact measurement evidence"
    elif input_identity is None:
        identity_status = f"exact input identity unavailable: {identity_detail}"
    else:
        post_identity, post_detail = input_snapshot(record_digests)
        if post_identity == input_identity:
            identity_fields = dict(input_identity)
            identity_status = "exact"
        else:
            identity_status = (
                "source/configuration inputs changed during build; "
                f"post-build identity unavailable or different ({post_detail})"
            )
    module_peaks = (
        {
            module: round(value, 1)
            for module, value in sorted(sampler.module_peak_mib.items())
            if module in rebuilt
        }
        if sampler and getattr(sampler, "module_peak_mib", None) else {}
    )
    peak_mib = round(sampler.peak_rss_mib, 1) if sampler and sampler.samples else None
    hint = repeat_failure(worktree, targets, settings()) if exit_code == 1 else None
    restart_line = (
        (
            f"rebuilt {len(rebuilt)} module(s): {', '.join(rebuilt[:12])}"
            + ("…" if len(rebuilt) > 12 else "")
            + " — a file worker keeps the imports it loaded; before trusting "
            "diagnostics in a file that imports them, query two other Lean files "
            "and then that one again to evict and reload it"
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
        **({"module_peak_mib": module_peaks} if module_peaks else {}),
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
        "identity_status": identity_status,
        **identity_fields,
        **_digest_fields(record_digests),
    }
    if cleanup_proved:
        released, release_detail = release_hold()
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
