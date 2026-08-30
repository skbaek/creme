#!/usr/bin/env python3
"""Peak resident-memory sampler for a task-owned process tree.

Why peak and not a final reading: a Lean elaboration's footprint rises and then
falls when the process exits, so a reading taken at the end of a run measures
nothing.  This samples throughout and reports the maximum.

What is measured: the sum of RSS over the process subtree rooted at a given pid,
INCLUDING that pid.  For ``lake env lean X`` that is ``lake`` + its ``lean``
child; for ``lake serve`` it is ``lake`` + ``lean --server`` + any worker it
spawns.  Creme's selected OS adapter reads one process snapshot per sample and
the tree is reconstructed from it, so a child that appears mid-run is picked
up. Darwin uses ``ps -axo`` and Linux uses ``ps -eo``; unsupported adapters
return a structured capability error.

Sampling interval: default 0.15 s (``--interval``). Sampling has non-zero host
cost, so the interval and sample count are recorded rather than implied.

Known limitation, stated rather than hidden: RSS summed over a tree
double-counts shared pages (the ``lean`` binary's text, and any shared mapping
between parent and child).  It is an upper bound on unique footprint.  The same
bias applies to every quadrant, so comparisons between quadrants are fair even
though the absolute number is not a unique-set-size.

Usage:
  procmon.py [--interval S] -- <command> [args...]     # run and monitor
  procmon.py --pid N --duration S [--interval S]       # attach to a live tree
  (as a library)  from procmon import ProcMon; m = ProcMon(pid); m.start(); ...
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time

_CREME_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if _CREME_ROOT not in sys.path:
    sys.path.insert(0, _CREME_ROOT)

from creme.adapters import get_adapter  # noqa: E402


def snapshot_result():
    """Return a portable process table plus a structured capability error."""
    result = get_adapter().process_snapshot()
    if result.status != "OK" or not result.data:
        return {}, result.detail
    procs = {}
    for row in result.data.get("processes", []):
        procs[row["pid"]] = (row["ppid"], row["rss_kib"], row["command"])
    return procs, None


def subtree(procs, root):
    """pids of the subtree rooted at ``root``, inclusive."""
    children = {}
    for pid, (ppid, _rss, _c) in procs.items():
        children.setdefault(ppid, []).append(pid)
    seen = set()
    stack = [root]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(children.get(p, []))
    return seen


class ProcMon:
    """Background sampler.  ``start()`` / ``stop()`` -> result dict."""

    def __init__(self, root_pid, interval=0.15, label=None):
        self.root_pid = root_pid
        self.interval = interval
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        self.peak_tree_kib = 0
        self.peak_single_kib = 0
        self.peak_at_s = None
        self.peak_members = []
        self.samples = 0
        self.nonempty_samples = 0
        self.capability_error = None
        self.t0 = None

    def _loop(self):
        while not self._stop.is_set():
            t = time.perf_counter()
            procs, error = snapshot_result()
            if error:
                self.capability_error = error
            self.samples += 1
            pids = subtree(procs, self.root_pid) & set(procs)
            if pids:
                self.nonempty_samples += 1
                total = sum(procs[p][1] for p in pids)
                biggest = max(procs[p][1] for p in pids)
                if total > self.peak_tree_kib:
                    self.peak_tree_kib = total
                    self.peak_at_s = t - self.t0
                    self.peak_members = sorted(
                        ((procs[p][1], p, procs[p][2].split("/")[-1])
                         for p in pids), reverse=True)[:4]
                self.peak_single_kib = max(self.peak_single_kib, biggest)
            elapsed = time.perf_counter() - t
            self._stop.wait(max(0.0, self.interval - elapsed))

    def start(self):
        self.t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.result()

    def result(self):
        return {
            "label": self.label,
            "root_pid": self.root_pid,
            "interval_s": self.interval,
            "samples": self.samples,
            "samples_with_live_tree": self.nonempty_samples,
            "process_snapshot_error": self.capability_error,
            "peak_tree_rss_kib": self.peak_tree_kib,
            "peak_tree_rss_gib": round(self.peak_tree_kib / 1048576.0, 3),
            "peak_single_rss_kib": self.peak_single_kib,
            "peak_at_s": (round(self.peak_at_s, 3)
                          if self.peak_at_s is not None else None),
            "peak_members": [[k, p, c] for k, p, c in self.peak_members],
            "note": ("tree RSS sums shared pages; upper bound on unique "
                     "footprint, comparable across quadrants"),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.15)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--out")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    if args.pid:
        m = ProcMon(args.pid, args.interval).start()
        time.sleep(args.duration or 5.0)
        res = m.stop()
        res["mode"] = "attach"
    else:
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("procmon: need --pid or a command after --", file=sys.stderr)
            return 2
        t0 = time.perf_counter()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        m = ProcMon(proc.pid, args.interval).start()
        out, err = proc.communicate()
        res = m.stop()
        res["mode"] = "run"
        res["cmd"] = cmd
        res["returncode"] = proc.returncode
        res["wall_s"] = round(time.perf_counter() - t0, 4)
        res["stdout_tail"] = out[-2000:]
        res["stderr_tail"] = err[-2000:]

    text = json.dumps(res, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
