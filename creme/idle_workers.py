"""Idleness accounting for Lean language-server workers.

Admission refuses for headroom while several gibibytes sit in workers that
have done nothing for many minutes.  Deciding that a worker is reclaimable is
a measurement, not a guess: idleness is established by comparing cumulative
CPU seconds between two observations, so a worker is never called idle on
first sight and a worker that is merely blocked on I/O is still called busy
only when it actually consumed CPU.

The ownership boundary is unchanged: this module classifies and reports; the
existing reclamation adapter decides what may be signalled.
"""

from __future__ import annotations

import re
from typing import Any, Optional


CPU_BUSY_PERCENT = 5.0
OBSERVATION_KEYS = {"cpu_seconds", "seen_at", "idle_since"}


def _valid_observation(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == OBSERVATION_KEYS
        and isinstance(value["cpu_seconds"], (int, float))
        and not isinstance(value["cpu_seconds"], bool)
        and isinstance(value["seen_at"], (int, float))
        and not isinstance(value["seen_at"], bool)
        and (
            value["idle_since"] is None
            or (
                isinstance(value["idle_since"], (int, float))
                and not isinstance(value["idle_since"], bool)
            )
        )
    )


def update_observations(
    workers: list[dict[str, Any]],
    previous: dict[str, Any],
    now: float,
) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Fold one CPU sample into the persisted record and report idleness.

    Returns the observations to persist and, per pid, the derived
    ``idle_seconds`` and ``cpu_percent``.  A worker seen for the first time is
    recorded but never reported idle: with no earlier CPU reading there is no
    evidence that it did nothing.
    """
    observations: dict[str, dict[str, Any]] = {}
    derived: dict[int, dict[str, Any]] = {}
    for worker in workers:
        pid = int(worker["pid"])
        cpu = float(worker["cpu_seconds"])
        prior = previous.get(str(pid))
        if not _valid_observation(prior):
            prior = None
        idle_since: Optional[float] = None
        percent: Optional[float] = None
        if prior is not None:
            elapsed = now - float(prior["seen_at"])
            delta = cpu - float(prior["cpu_seconds"])
            if elapsed <= 0 or delta < 0:
                # A restarted counter or a reused pid is not evidence of idleness.
                idle_since = None
            else:
                percent = 100.0 * delta / elapsed
                if percent <= CPU_BUSY_PERCENT:
                    idle_since = (
                        float(prior["idle_since"])
                        if prior["idle_since"] is not None
                        else float(prior["seen_at"])
                    )
        observations[str(pid)] = {
            "cpu_seconds": cpu,
            "seen_at": now,
            "idle_since": idle_since,
        }
        derived[pid] = {
            "idle_seconds": (now - idle_since) if idle_since is not None else None,
            "cpu_percent": round(percent, 2) if percent is not None else None,
        }
    return observations, derived


def owner_label(
    worker: dict[str, Any],
    hold_pids: dict[int, str],
    client_pattern: re.Pattern[str],
) -> str:
    """Name who should reclaim this worker, from its own process ancestry."""
    for ancestor in worker.get("ancestry") or []:
        pid = int(ancestor["pid"])
        if pid in hold_pids:
            return f"goal {hold_pids[pid]}"
    for ancestor in worker.get("ancestry") or []:
        match = client_pattern.search(str(ancestor["command"]))
        if match:
            # Name the client family from the matched marker: an executable
            # path can contain spaces, so the first token is not its name.
            matched = match.group(0).lower()
            family = next(
                (name for name in ("codex", "chatgpt", "claude") if name in matched),
                "agent",
            )
            return f"client {family} pid {ancestor['pid']}"
    return f"unattributed pid {worker.get('ppid')}"


def select_reclaimable(
    workers: list[dict[str, Any]],
    derived: dict[int, dict[str, Any]],
    owned_pids: set[int],
    minimum_idle_seconds: float,
) -> tuple[list[int], list[int]]:
    """Split idle workers into caller-owned targets and reported foreigners."""
    targets: list[int] = []
    reported: list[int] = []
    for worker in workers:
        pid = int(worker["pid"])
        idle = (derived.get(pid) or {}).get("idle_seconds")
        if idle is None or idle < minimum_idle_seconds:
            continue
        (targets if pid in owned_pids else reported).append(pid)
    return sorted(targets), sorted(reported)
