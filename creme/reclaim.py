"""Fail-closed selection logic for agent-owned Lean server reclamation.

Process discovery and signalling stay in an OS adapter.  This module operates
only on an immutable snapshot so ownership, descendant protection, and kill
order can be tested without touching live processes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    rss_kib: int
    started: str
    command: str

    @property
    def kind(self) -> str:
        if "lake serve" in self.command:
            return "lake-serve"
        if "--worker" in self.command:
            return "lean-worker"
        if "lean --server" in self.command:
            return "lean-server"
        return os.path.basename(self.command.split(None, 1)[0]) or "process"


@dataclass(frozen=True)
class ReclaimPlan:
    owned: tuple[int, ...]
    foreign: tuple[int, ...]
    protected_roots: tuple[int, ...]
    targets: tuple[int, ...]


def ancestry(table: dict[int, Process], pid: int) -> tuple[int, ...]:
    chain = []
    seen = set()
    current = pid
    while current > 1 and current in table and current not in seen and len(chain) < 64:
        seen.add(current)
        chain.append(current)
        current = table[current].ppid
    return tuple(chain)


def is_candidate(process: Process) -> bool:
    command = process.command
    return (
        "lean --server" in command
        or "lean --worker" in command
        or "lake serve" in command
        or ("--worker" in command and "lean" in command)
    )


def _client_above(
    table: dict[int, Process],
    pid: int,
    is_client: Callable[[Process], bool],
) -> Optional[int]:
    for ancestor in ancestry(table, pid):
        if is_client(table[ancestor]):
            return ancestor
    return None


def _shared_ancestor(
    table: dict[int, Process],
    invocation_parent: int,
    candidate: int,
) -> Optional[int]:
    theirs = set(ancestry(table, candidate))
    return next((pid for pid in ancestry(table, invocation_parent) if pid in theirs), None)


def _is_descendant(table: dict[int, Process], pid: int, root: int) -> bool:
    return root in ancestry(table, pid)


def _depth(table: dict[int, Process], pid: int) -> int:
    return len(ancestry(table, pid))


def build_plan(
    table: dict[int, Process],
    invocation_parent: int,
    is_client: Callable[[Process], bool],
    hard_pressure: bool = False,
) -> ReclaimPlan:
    """Return a frozen plan; an empty invocation ancestry refuses all ownership."""
    if not ancestry(table, invocation_parent):
        return ReclaimPlan((), tuple(sorted(p.pid for p in table.values() if is_candidate(p))), (), ())

    owned = []
    foreign = []
    for process in table.values():
        if not is_candidate(process):
            continue
        shared = _shared_ancestor(table, invocation_parent, process.pid)
        if shared is not None and _client_above(table, shared, is_client) is not None:
            owned.append(process.pid)
        else:
            foreign.append(process.pid)
    owned_set = set(owned)

    roots = []
    for pid in owned:
        if not any(ancestor in owned_set for ancestor in ancestry(table, table[pid].ppid)):
            roots.append(pid)

    protected = []
    targets = set()
    for root in roots:
        closure = {pid for pid in table if _is_descendant(table, pid, root)}
        excluded = closure - owned_set
        if excluded and not hard_pressure:
            protected.append(root)
        else:
            targets.update(closure)

    ordered = tuple(sorted(targets, key=lambda pid: _depth(table, pid), reverse=True))
    return ReclaimPlan(
        tuple(sorted(owned)), tuple(sorted(foreign)), tuple(sorted(protected)), ordered,
    )
