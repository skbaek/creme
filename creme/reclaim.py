"""Fail-closed selection logic for agent-owned Lean server reclamation.

Process discovery and signalling stay in an OS adapter.  This module operates
only on an immutable snapshot so ownership, descendant protection, and kill
order can be tested without touching live processes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    rss_kib: int
    started: str
    command: str
    cwd: Optional[str] = None

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


@dataclass(frozen=True)
class ReclaimOptions:
    dry_run: bool
    hard_pressure: bool
    scope_roots: tuple[Path, ...]


def parse_reclaim_arguments(arguments: list[str]) -> ReclaimOptions:
    """Parse the adapter-private reclaim protocol without accepting ambiguity."""
    dry_run = False
    hard_pressure = False
    scope_roots: list[Path] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option == "--dry-run":
            if dry_run:
                raise ValueError("duplicate reclaim option: --dry-run")
            dry_run = True
        elif option == "--hard-pressure":
            if hard_pressure:
                raise ValueError("duplicate reclaim option: --hard-pressure")
            hard_pressure = True
        elif option == "--scope-root":
            index += 1
            if index >= len(arguments):
                raise ValueError("--scope-root requires an absolute path")
            raw = arguments[index]
            root = Path(raw)
            if not raw or not root.is_absolute():
                raise ValueError("--scope-root requires an absolute path")
            normalized = Path(os.path.normpath(raw))
            if normalized in scope_roots:
                raise ValueError("duplicate reclaim scope root")
            scope_roots.append(normalized)
        else:
            raise ValueError(f"unsupported reclaim option: {option}")
        index += 1
    if hard_pressure and scope_roots:
        raise ValueError("goal-scoped reclaim cannot use hard-pressure mode")
    return ReclaimOptions(dry_run, hard_pressure, tuple(scope_roots))


def process_in_scope(process: Process, roots: tuple[Path, ...]) -> bool:
    if process.cwd is None:
        return False
    current = Path(os.path.normpath(process.cwd))
    for root in roots:
        try:
            current.relative_to(root)
            return True
        except ValueError:
            continue
    return False


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
    candidate_scope: Optional[Callable[[Process], bool]] = None,
) -> ReclaimPlan:
    """Return a frozen plan; an empty invocation ancestry refuses all ownership."""
    if not ancestry(table, invocation_parent):
        return ReclaimPlan((), tuple(sorted(p.pid for p in table.values() if is_candidate(p))), (), ())

    owned = []
    foreign = []
    for process in table.values():
        if not is_candidate(process):
            continue
        if candidate_scope is not None and not candidate_scope(process):
            foreign.append(process.pid)
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
