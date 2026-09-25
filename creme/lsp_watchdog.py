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
* the host holds the build watchdog's critical signal (`watchdog_critical`:
  kernel pressure at warning held 10 s, or swap +1 GiB within 10 s) for
  `grace` seconds, and the worker is the largest owned one at or above the
  semaphore's heavy-worker size.  A smaller worker is not the pressure's cause,
  so it is left to the owned-build watchdog and the host's other answers.

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


LSP_WATCH_INTERVAL_SECONDS = 3.0
# Critical host pressure must persist this long before a worker is stopped.
LSP_PRESSURE_GRACE_SECONDS = owned.WATCHDOG_GRACE_SECONDS
# After a pressure stop, swap and the kernel level need time to settle before
# a second worker is judged by them.
LSP_PRESSURE_COOLDOWN_SECONDS = 30.0
# SIGTERM first; SIGKILL if the worker is still there this much later.
LSP_TERM_GRACE_SECONDS = 5.0
KIB_PER_GIB = 1024 ** 2

Snapshot = dict[int, tuple[int, int, str]]


@dataclass(frozen=True)
class Worker:
    pid: int
    command: str
    rss_kib: int
    footprint_kib: Optional[int]

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
    grace: float = LSP_TERM_GRACE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """SIGTERM the worker, then SIGKILL it if it outlives `grace`.

    Each signal is sent only while a fresh snapshot still shows the same
    command in `root_pid`'s tree, so a recycled pid is never signalled.
    Returns the negated last signal sent, or 0 when none was.
    """
    sent = 0
    for signum in (signal.SIGTERM, signal.SIGKILL):
        rows = snapshot()
        if rows is None or owned_workers(root_pid, rows).get(worker.pid) != worker.command:
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
        probe: Optional[Callable[[], Any]] = None,
        stop: Optional[Callable[[Worker], int]] = None,
        record: Callable[[Worker, str, int], None] = record_stop,
        clock: Callable[[], float] = time.monotonic,
        interval: float = LSP_WATCH_INTERVAL_SECONDS,
        grace: float = LSP_PRESSURE_GRACE_SECONDS,
        cooldown: float = LSP_PRESSURE_COOLDOWN_SECONDS,
        heavy_gib: float = semaphore.HEAVY_WORKER_GIB,
    ) -> None:
        super().__init__(daemon=True)
        self.root_pid = root_pid
        self.ceiling_kib = int(ceiling_gib * KIB_PER_GIB)
        self.heavy_kib = int(heavy_gib * KIB_PER_GIB)
        self.snapshot = snapshot
        self.footprints = footprints
        self.probe = probe or (lambda: get_adapter().memory_headroom())
        self.stop_worker = stop or (lambda worker: stop_worker(worker, root_pid, snapshot=snapshot))
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

    def _workers(self) -> list[Worker]:
        rows = self.snapshot()
        if rows is None:
            return []
        commands = owned_workers(self.root_pid, rows)
        sizes = self.footprints(sorted(commands)) if commands else {}
        return [
            Worker(pid, command, rows[pid][1], sizes.get(pid))
            for pid, command in sorted(commands.items())
        ]

    def _critical_reason(self, now: float) -> Optional[str]:
        try:
            sample = self.probe()
        except Exception:
            return None
        data = getattr(sample, "data", None)
        swap = data.get("swap_used_mib") if isinstance(data, dict) else None
        if isinstance(swap, (int, float)) and not isinstance(swap, bool):
            self._swap.append((now, float(swap)))
            self._swap = [
                item for item in self._swap if now - item[0] <= owned.SWAP_GROWTH_WINDOW_SECONDS
            ]
        level = owned._pressure_level(sample)
        if level is not None and level >= owned.DARWIN_WARNING_PRESSURE_LEVEL:
            if self._warning is None:
                self._warning = now
        else:
            self._warning = None
        return owned.watchdog_critical(
            sample, semaphore.ADMISSION_FLOOR_GIB, self._swap,
            None if self._warning is None else now - self._warning,
        )

    def _stop_worker(self, worker: Worker, reason: str) -> None:
        code = self.stop_worker(worker)
        if code:
            self.stopped.append(worker.pid)
            self.record(worker, reason, code)

    def check(self) -> None:
        """One sample: stop every worker past the ceiling, then answer pressure."""
        workers = self._workers()
        now = self.clock()
        remaining = []
        for worker in workers:
            if worker.size_kib > self.ceiling_kib:
                self._stop_worker(worker, (
                    f"footprint {worker.size_kib / KIB_PER_GIB:.1f} GiB above the "
                    f"{self.ceiling_kib / KIB_PER_GIB:g} GiB lsp worker ceiling"
                ))
            else:
                remaining.append(worker)
        heavy = [worker for worker in remaining if worker.size_kib >= self.heavy_kib]
        if not heavy:
            # Without an owned heavy worker there is nothing here to answer
            # pressure with; the probe is not paid for.
            self._swap, self._warning, self._critical = [], None, None
            return
        critical = self._critical_reason(now)
        if critical is None:
            self._critical = None
            return
        if self._critical is None:
            self._critical = now
        if now - self._critical < self.grace:
            return
        if self._quiet_until is not None and now < self._quiet_until:
            return
        largest = max(heavy, key=lambda worker: worker.size_kib)
        self._stop_worker(largest, (
            f"largest owned worker ({largest.size_kib / KIB_PER_GIB:.1f} GiB) "
            f"under critical host pressure: {critical}"
        ))
        self._quiet_until = now + self.cooldown
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
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    watchdog: Callable[..., LspWorkerWatchdog] = LspWorkerWatchdog,
) -> int:
    """Run the MCP server on this process's stdio, watching its file workers."""
    from .profile import load_lsp_worker_ceiling

    ceiling = ceiling_gib if ceiling_gib is not None else load_lsp_worker_ceiling()
    child = popen(command, env=env, executable=command[0])

    def forward(signum: int, _frame: Any) -> None:
        try:
            child.send_signal(signum)
        except (OSError, ValueError):
            pass

    previous = {signum: signal.signal(signum, forward) for signum in _FORWARDED_SIGNALS}
    guard = watchdog(child.pid, ceiling)
    guard.start()
    try:
        code = child.wait()
    finally:
        guard.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 128 - code if code < 0 else code
