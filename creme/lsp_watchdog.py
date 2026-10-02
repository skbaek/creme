"""Memory watchdog for the Lean file workers a guarded `lean-mcp` launch owns.

The semaphore admits builds, never the language server, so a file worker can
grow without any admission.  On 2026-09-25/26 one `lean --worker` for a proof
file reached 46 GiB (42 GiB of it compressed) on a 24 GiB host and left owned
builds refused `LIGHT_ONLY` until its session was stopped.

`creme lean-mcp` therefore runs the MCP server as its child and watches the
`lean --worker` processes descended from that child.  Only those workers are
ever signalled: another session's workers, the `lean --server` and `lake
serve` above them, and anything outside the child's process tree are never
touched.  A worker is stopped when

* its footprint (the larger of RSS and the physical footprint, which counts
  compressed pages that RSS omits) passes the configured ceiling; or
* aggregate owned worker memory passes its emergency ceiling; or
* the host holds the build watchdog's critical signal for `grace` seconds,
  and the worker is the largest owned one. Individually small workers can
  exhaust memory together, so there is no heavy-worker size exclusion.

Stopping a worker is the restart: the Lean server reports the crashed file and
starts a fresh worker when the file is next used.  Every stop is logged to the
build ledger as an `lsp_worker` row and to stderr, never to stdout, which is
the MCP channel.  Semaphore state is neither read for ownership nor written.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote

from . import build_ownership as owned
from . import idle_workers, semaphore
from .adapters import get_adapter
from .reclaim import is_lean_worker


LSP_WATCH_INTERVAL_SECONDS = 1.0
# Critical host pressure must persist this long before a worker is stopped.
LSP_PRESSURE_GRACE_SECONDS = owned.WATCHDOG_GRACE_SECONDS
# After a pressure stop, swap and the kernel level need time to settle before
# a second worker is judged by them.
LSP_PRESSURE_COOLDOWN_SECONDS = 30.0
# SIGTERM first; SIGKILL if the worker is still there this much later.
LSP_TERM_GRACE_SECONDS = 1.0
KIB_PER_GIB = 1024 ** 2

Snapshot = dict[int, tuple[int, int, str]]
Instances = dict[int, tuple[str, str]]


@dataclass(frozen=True)
class Worker:
    pid: int
    command: str
    rss_kib: int
    footprint_kib: Optional[int]
    started: Optional[str] = None

    @property
    def size_kib(self) -> int:
        return max(self.rss_kib, self.footprint_kib or 0)

    @property
    def document(self) -> Optional[str]:
        for token in self.command.split():
            if token.startswith("file://"):
                return unquote(token[len("file://"):])
        return None


def owned_workers(root_pid: int, rows: Snapshot) -> dict[int, str]:
    """The `lean --worker` processes in `root_pid`'s process tree, by pid."""
    tree = owned._descendants(root_pid, rows)
    return {
        pid: command
        for pid, (_ppid, _rss, command) in rows.items()
        if pid in tree and pid != root_pid and is_lean_worker(command)
    }


def _footprints(pids: list[int]) -> dict[int, int]:
    try:
        sampled = get_adapter().process_footprints(pids)
    except Exception:
        return {}
    raw = sampled.data.get("footprints") if sampled.status == "OK" and sampled.data else None
    found: dict[int, int] = {}
    for pid, row in (raw or {}).items():
        if isinstance(row, dict) and isinstance(row.get("footprint_kib"), int):
            found[int(pid)] = row["footprint_kib"]
    return found


def _instances(pids: list[int]) -> Instances:
    try:
        sampled = get_adapter().process_instances(pids)
    except Exception:
        return {}
    raw = sampled.data.get("instances") if sampled.status == "OK" and sampled.data else None
    found = {}
    for pid, row in (raw or {}).items():
        if isinstance(row, dict) and isinstance(row.get("started"), str) and row["started"] \
                and isinstance(row.get("command"), str):
            found[int(pid)] = (row["started"], row["command"])
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_worker(
    worker: Worker,
    root_pid: int,
    *,
    snapshot: Callable[[], Optional[Snapshot]] = owned._process_snapshot,
    instances: Callable[[list[int]], Instances] = _instances,
    grace: float = LSP_TERM_GRACE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """SIGTERM the worker, then SIGKILL it if it outlives `grace`.

    Each signal needs a fresh tree/command check and matching process start
    identity. Missing identity fails closed, including on unsupported hosts.
    Returns the negated last signal sent, or 0 when none was.
    """
    sent = 0
    for signum in (signal.SIGTERM, signal.SIGKILL):
        rows = snapshot()
        if rows is None or owned_workers(root_pid, rows).get(worker.pid) != worker.command:
            return sent
        if worker.started is None or instances([worker.pid]).get(worker.pid) != (worker.started, worker.command):
            return sent
        try:
            os.kill(worker.pid, signum)
        except ProcessLookupError:
            return sent
        sent = -int(signum)
        waited = 0.0
        while waited < grace and _alive(worker.pid):
            sleep(0.2)
            waited += 0.2
        if not _alive(worker.pid):
            return sent
    return sent


def _worktree_of(document: Optional[str]) -> Path:
    if not document:
        return Path("<unknown>")
    path = Path(document)
    parts = path.parts
    if ".worktrees" in parts:
        index = parts.index(".worktrees")
        if index + 1 < len(parts):
            return Path(*parts[: index + 2])
    return path.parent


def record_stop(worker: Worker, reason: str, exit_code: int) -> None:
    """One `lsp_worker` ledger row and one stderr line; neither may fail the launcher."""
    document = worker.document
    worktree = _worktree_of(document)
    goal = idle_workers.goal_of_directory(document) or "<unowned>"
    line = (
        f"creme lean-mcp: stopped lean --worker pid {worker.pid} "
        f"({document or 'unknown document'}, goal {goal}): {reason}"
    )
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        owned.append_ledger({
            "kind": "lsp_worker",
            "worktree": str(worktree),
            "goal": goal,
            "targets": [document] if document else [],
            "command": worker.command.split(),
            "exit": exit_code,
            "reason": reason,
            "peak_rss_mib": round(worker.size_kib / 1024, 1),
        })
    except Exception:
        pass


class LspWorkerWatchdog(threading.Thread):
    """Stop owned file workers past the ceiling or at the head of critical pressure."""

    def __init__(
        self,
        root_pid: int,
        ceiling_gib: float,
        *,
        snapshot: Callable[[], Optional[Snapshot]] = owned._process_snapshot,
        footprints: Callable[[list[int]], dict[int, int]] = _footprints,
        instances: Callable[[list[int]], Instances] = _instances,
        aggregate_ceiling_gib: Optional[float] = None,
        probe: Optional[Callable[[], Any]] = None,
        stop: Optional[Callable[[Worker], int]] = None,
        record: Callable[[Worker, str, int], None] = record_stop,
        clock: Callable[[], float] = time.monotonic,
        interval: float = LSP_WATCH_INTERVAL_SECONDS,
        grace: float = LSP_PRESSURE_GRACE_SECONDS,
        cooldown: float = LSP_PRESSURE_COOLDOWN_SECONDS,
    ) -> None:
        super().__init__(daemon=True)
        self.root_pid = root_pid
        self.ceiling_kib = int(ceiling_gib * KIB_PER_GIB)
        self.aggregate_ceiling_kib = (
            int(aggregate_ceiling_gib * KIB_PER_GIB) if aggregate_ceiling_gib is not None else None
        )
        self.snapshot = snapshot
        self.footprints = footprints
        self.instances = instances
        self.probe = probe or (lambda: get_adapter().memory_headroom())
        self.stop_worker = stop or (lambda worker: stop_worker(worker, root_pid, snapshot=snapshot, instances=instances))
        self.record = record
        self.clock = clock
        self.interval = interval
        self.grace = grace
        self.cooldown = cooldown
        self.stop_event = threading.Event()
        self.stopped: list[int] = []
        self._swap: list[tuple[float, float]] = []
        self._warning: Optional[float] = None
        self._critical: Optional[float] = None
        self._quiet_until: Optional[float] = None
        self._last_sample: Optional[float] = None
        self._telemetry_missing = False
        self.telemetry_status = "UNAVAILABLE"

    def _workers(self) -> Optional[list[Worker]]:
        rows = self.snapshot()
        if rows is None:
            return None
        commands = owned_workers(self.root_pid, rows)
        sizes = self.footprints(sorted(commands)) if commands else {}
        identities = self.instances(sorted(commands)) if commands else {}
        return [
            Worker(pid, command, rows[pid][1], sizes.get(pid),
                   identities[pid][0] if identities.get(pid, (None, None))[1] == command else None)
            for pid, command in sorted(commands.items())
        ]

    def _critical_reason(self, now: float) -> tuple[bool, Optional[str]]:
        try:
            sample = self.probe()
        except Exception:
            self._telemetry_missing = True
            self.telemetry_status = "UNAVAILABLE"
            return False, None
        data = getattr(sample, "data", None)
        if getattr(sample, "status", None) != "OK" or not isinstance(data, dict):
            self._telemetry_missing = True
            self.telemetry_status = "UNAVAILABLE"
            return False, None
        swap = data.get("swap_used_mib") if isinstance(data, dict) else None
        level = owned._pressure_level(sample)
        direct = data.get("memory_available_direct") is True
        _free, available, _total = semaphore._headroom_values(sample, None)
        psi = data.get("memory_psi_full_avg10")
        numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
        if not (level is not None or numeric(swap) or (direct and (numeric(available) or numeric(psi)))):
            self._telemetry_missing = True
            self.telemetry_status = "UNAVAILABLE"
            return False, None
        if self._telemetry_missing and self._last_sample is not None:
            gap = now - self._last_sample
            # Unknown time cannot prove sustained pressure or healthy recovery.
            if self._warning is not None:
                self._warning += gap
            if self._critical is not None:
                self._critical += gap
            self._swap = []
        self._telemetry_missing = False
        self.telemetry_status = "OK"
        self._last_sample = now
        if isinstance(swap, (int, float)) and not isinstance(swap, bool):
            self._swap.append((now, float(swap)))
            self._swap = [
                item for item in self._swap if now - item[0] <= owned.SWAP_GROWTH_WINDOW_SECONDS
            ]
        if level is not None and level >= owned.DARWIN_WARNING_PRESSURE_LEVEL:
            if self._warning is None:
                self._warning = now
        else:
            self._warning = None
        return True, owned.watchdog_critical(
            sample, semaphore.ADMISSION_FLOOR_GIB, self._swap,
            None if self._warning is None else now - self._warning,
        )

    def _stop_worker(self, worker: Worker, reason: str) -> bool:
        code = self.stop_worker(worker)
        if code:
            self.stopped.append(worker.pid)
            self.record(worker, reason, code)
        return bool(code)

    def check(self) -> None:
        """One sample: apply emergency ceilings, then answer host pressure."""
        workers = self._workers()
        if workers is None:
            self._telemetry_missing = True
            self.telemetry_status = "UNAVAILABLE"
            return
        remaining = []
        for worker in workers:
            if worker.size_kib > self.ceiling_kib:
                stopped = self._stop_worker(worker, (
                    f"footprint {worker.size_kib / KIB_PER_GIB:.1f} GiB above the "
                    f"{self.ceiling_kib / KIB_PER_GIB:g} GiB lsp worker ceiling"
                ))
                if not stopped:
                    remaining.append(worker)
            else:
                remaining.append(worker)
        remaining = [worker for worker in remaining if worker.size_kib > 0]
        if not remaining:
            self._swap, self._warning, self._critical = [], None, None
            self._last_sample, self._telemetry_missing = None, False
            return
        now = self.clock()
        aggregate = sum(worker.size_kib for worker in remaining)
        eligible = [worker for worker in remaining if worker.started is not None]
        if eligible and self.aggregate_ceiling_kib is not None and aggregate > self.aggregate_ceiling_kib:
            largest = max(eligible, key=lambda worker: worker.size_kib)
            self._stop_worker(largest, (
                f"aggregate owned workers {aggregate / KIB_PER_GIB:.1f} GiB above the "
                f"{self.aggregate_ceiling_kib / KIB_PER_GIB:g} GiB aggregate ceiling; "
                f"largest owned worker {largest.size_kib / KIB_PER_GIB:.1f} GiB"
            ))
            return
        observed, critical = self._critical_reason(now)
        if not observed:
            return
        if critical is None:
            self._critical = None
            return
        if self._critical is None:
            self._critical = now
        if now - self._critical < self.grace:
            return
        if self._quiet_until is not None and now < self._quiet_until:
            return
        if not eligible:
            return  # unverified process identity never grants signalling authority
        largest = max(eligible, key=lambda worker: worker.size_kib)
        stopped = self._stop_worker(largest, (
            f"largest owned worker ({largest.size_kib / KIB_PER_GIB:.1f} GiB) "
            f"of aggregate {aggregate / KIB_PER_GIB:.1f} GiB under critical host pressure: {critical}"
        ))
        if stopped:
            self._quiet_until = self.clock() + self.cooldown
            self._critical = None
            self._swap = []

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.check()
            except Exception:
                pass  # the watchdog must never take the MCP server down with it
            self.stop_event.wait(self.interval)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=max(2.0, self.interval * 3))


_FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def supervise(
    command: list[str],
    env: dict[str, str],
    *,
    ceiling_gib: Optional[float] = None,
    aggregate_ceiling_gib: Optional[float] = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    watchdog: Callable[..., LspWorkerWatchdog] = LspWorkerWatchdog,
) -> int:
    """Run the MCP server on this process's stdio, watching its file workers."""
    from .profile import load_lsp_worker_ceiling

    ceiling = ceiling_gib if ceiling_gib is not None else load_lsp_worker_ceiling()
    if aggregate_ceiling_gib is None:
        from .profile import load_lsp_aggregate_ceiling
        aggregate_ceiling_gib = load_lsp_aggregate_ceiling()
    child = popen(command, env=env, executable=command[0])

    def forward(signum: int, _frame: Any) -> None:
        try:
            child.send_signal(signum)
        except (OSError, ValueError):
            pass

    previous = {signum: signal.signal(signum, forward) for signum in _FORWARDED_SIGNALS}
    guard = watchdog(child.pid, ceiling, aggregate_ceiling_gib=aggregate_ceiling_gib)
    guard.start()
    try:
        code = child.wait()
    finally:
        guard.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 128 - code if code < 0 else code
