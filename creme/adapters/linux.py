from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from .base import Adapter, CapabilityResult
from ..reclaim import (
    Process,
    narrow_targets,
    parse_cpu_seconds as _parse_cpu_seconds,
    build_plan,
    parse_reclaim_arguments,
    process_in_scope,
)


class LinuxAdapter(Adapter):
    system = "Linux"
    optional_capabilities = ("reflink_copy", "lean_reclaim")
    client_pattern = re.compile(
        r"^(?:\S*/)?(?:ChatGPT|codex|claude|codex-code-mode-host|codex-linux-sandbox)(?:\s|$)",
        re.IGNORECASE,
    )

    @staticmethod
    def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def platform_identity(self, machine: str | None = None) -> CapabilityResult:
        detected = (machine or platform.machine()).strip().lower()
        if detected in {"x86_64", "amd64"}:
            canonical_machine = "x86_64"
            uv_platform = "linux-x86_64-gnu"
        elif detected in {"aarch64", "arm64"}:
            canonical_machine = "arm64"
            uv_platform = "linux-aarch64-gnu"
        else:
            return self.result(
                "platform_identity", "UNAVAILABLE",
                f"unsupported Linux machine architecture: {detected or '<empty>'}",
            )
        key = f"linux-{canonical_machine}"
        return self.result(
            "platform_identity", "OK", f"canonical platform identity is {key}",
            {
                "key": key,
                "system": self.system,
                "machine": canonical_machine,
                "uv_platform": uv_platform,
            },
        )

    def python_runtime(
        self,
        version: str,
        machine: str | None = None,
    ) -> CapabilityResult:
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
            return self.result(
                "python_runtime", "REFUSED",
                "Python version must be an exact major.minor.patch value",
            )
        identity = self.platform_identity(machine)
        if identity.status != "OK" or not identity.data:
            return self.result("python_runtime", "UNAVAILABLE", identity.detail)
        series = version.rsplit(".", 1)[0]
        uv_platform = identity.data["uv_platform"]
        alias = f"~/.local/share/uv/python/cpython-{series}-{uv_platform}"
        base = f"~/.local/share/uv/python/cpython-{version}-{uv_platform}"
        return self.result(
            "python_runtime", "OK",
            f"native CPython {version} identity for {identity.data['key']}",
            {
                "platform_key": identity.data["key"],
                "implementation": "CPython",
                "version": version,
                "uv_alias_prefix": alias,
                "uv_base_prefix": base,
            },
        )

    @staticmethod
    def _meminfo() -> dict[str, int]:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as source:
            for line in source:
                key, _, rest = line.partition(":")
                fields = rest.split()
                if fields:
                    values[key] = int(fields[0])
        return values

    def static_facts(self) -> CapabilityResult:
        try:
            memory = self._meminfo()["MemTotal"] * 1024
        except (OSError, KeyError, ValueError) as exc:
            return self.result("static_facts", "UNAVAILABLE", str(exc))
        data = {
            "system": self.system,
            "machine": platform.machine() or "unknown",
            "logical_cores": os.cpu_count() or 1,
            "physical_memory_bytes": memory,
        }
        return self.result("static_facts", "OK", "static Linux facts detected", data)

    def memory_headroom(self) -> CapabilityResult:
        try:
            memory = self._meminfo()
        except (OSError, ValueError) as exc:
            return self.result("memory_headroom", "UNAVAILABLE", str(exc))
        total = memory.get("MemTotal")
        available = memory.get("MemAvailable")
        if not total or available is None:
            return self.result(
                "memory_headroom", "UNAVAILABLE",
                "MemTotal or MemAvailable is unavailable",
            )
        swap_used_kib = None
        try:
            sampled_swap = 0
            with open("/proc/swaps", encoding="utf-8") as swaps:
                next(swaps, None)
                for line in swaps:
                    fields = line.split()
                    if len(fields) >= 4:
                        sampled_swap += int(fields[3])
            swap_used_kib = sampled_swap
        except (OSError, ValueError):
            pass
        free_pct = int(available * 100 / total)
        return self.result(
            "memory_headroom", "OK", "Linux aggregate memory headroom sampled",
            {
                "memory_free_percent": free_pct,
                "memory_available_bytes": available * 1024,
                "physical_memory_bytes": total * 1024,
                "swap_used_mib": (
                    swap_used_kib / 1024 if swap_used_kib is not None else None
                ),
            },
        )

    def telemetry(self) -> CapabilityResult:
        headroom = self.memory_headroom()
        if headroom.status != "OK" or not headroom.data:
            return self.result("telemetry", "UNAVAILABLE", headroom.detail)
        try:
            processes = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,rss=,stat=,comm="],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("telemetry", "UNAVAILABLE", str(exc))
        if processes.returncode:
            return self.result("telemetry", "UNAVAILABLE", "ps failed")
        clients = {"codex": 0, "claude": 0}
        lean = []
        largest = []
        for line in processes.stdout.splitlines():
            fields = line.split(maxsplit=4)
            if len(fields) != 5:
                continue
            pid, ppid, rss_text, state, command = fields
            if state.startswith("Z"):
                continue
            try:
                rss = int(rss_text)
            except ValueError:
                continue
            base = Path(command).name
            if base == "codex":
                clients["codex"] += rss
            elif base == "claude":
                clients["claude"] += rss
            if base in {"lean", "lake"} or "lean-lsp-mcp" in command:
                lean.append({"pid": int(pid), "ppid": int(ppid), "rss_kib": rss, "command": base})
            largest.append({"pid": int(pid), "ppid": int(ppid), "rss_kib": rss, "command": base})
        largest.sort(key=lambda row: row["rss_kib"], reverse=True)
        data = {
            **headroom.data,
            "client_family_rss_kib": {**clients, "total": sum(clients.values())},
            "lean_processes": lean,
            "largest_processes": largest[:15],
        }
        return self.result("telemetry", "OK", "Linux telemetry sampled", data)

    def process_snapshot(self) -> CapabilityResult:
        try:
            processes = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,rss=,stat=,comm="],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("process_snapshot", "UNAVAILABLE", str(exc))
        if processes.returncode:
            return self.result("process_snapshot", "UNAVAILABLE", "Linux ps snapshot failed")
        rows = []
        for line in processes.stdout.splitlines():
            fields = line.split(maxsplit=4)
            if len(fields) != 5 or fields[3].startswith("Z"):
                continue
            try:
                rows.append({
                    "pid": int(fields[0]), "ppid": int(fields[1]),
                    "rss_kib": int(fields[2]), "command": Path(fields[4]).name,
                })
            except ValueError:
                continue
        return self.result("process_snapshot", "OK", "Linux process snapshot sampled", {"processes": rows})

    def _lean_worker_sample(self, ps_argv: list[str]) -> "CapabilityResult":
        try:
            sample = self._run(ps_argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("lean_workers", "UNAVAILABLE", str(exc))
        if sample.returncode:
            return self.result("lean_workers", "UNAVAILABLE", "process snapshot failed")
        table: dict[int, tuple[int, float, int, str]] = {}
        for line in sample.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) != 5:
                continue
            try:
                pid, ppid, rss = int(fields[0]), int(fields[1]), int(fields[2])
            except ValueError:
                continue
            cpu = _parse_cpu_seconds(fields[3])
            if cpu is None:
                continue
            table[pid] = (ppid, cpu, rss, fields[4])

        def ancestry(pid: int) -> list[dict[str, object]]:
            chain: list[dict[str, object]] = []
            seen = set()
            current = table.get(pid, (0, 0.0, 0, ""))[0]
            while current > 1 and current in table and current not in seen and len(chain) < 12:
                seen.add(current)
                chain.append({"pid": current, "command": table[current][3]})
                current = table[current][0]
            return chain

        workers = [
            {
                "pid": pid,
                "ppid": parent,
                "rss_kib": rss,
                "cpu_seconds": cpu,
                "command": command,
                "ancestry": ancestry(pid),
            }
            for pid, (parent, cpu, rss, command) in sorted(table.items())
            if "lean" in command and "--worker" in command
        ]
        return self.result(
            "lean_workers", "OK",
            f"{len(workers)} Lean worker(s) sampled",
            {"workers": workers},
        )

    def lean_workers(self) -> "CapabilityResult":
        return self._lean_worker_sample(
            ["/bin/ps", "-axo", "pid=,ppid=,rss=,time=,command="]
        )

    def quiet_host(self) -> CapabilityResult:
        sample = self.telemetry()
        if sample.status != "OK" or not sample.data:
            return self.result("quiet_host", "UNAVAILABLE", sample.detail)
        free = sample.data.get("memory_free_percent")
        lean = sample.data.get("lean_processes") or []
        if free is None:
            return self.result("quiet_host", "UNAVAILABLE", "MemAvailable is unavailable")
        quiet = not lean and free >= 25
        return self.result(
            "quiet_host", "OK" if quiet else "BUSY",
            "host meets conservative quiet checks" if quiet else "Lean activity or low memory prevents certification",
            {"memory_free_percent": free, "lean_process_count": len(lean)},
        )

    def copy_cache(self, source: Path, destination: Path, execute: bool) -> CapabilityResult:
        cp = shutil.which("cp")
        def reflink(staged: Path) -> bool:
            if not cp:
                return False
            try:
                result = subprocess.run(
                    [cp, "--reflink=auto", "-a", str(source), str(staged)],
                    capture_output=True, text=True, timeout=1800,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return result.returncode == 0

        return self._copy_cache_with_optimized_staging(
            source,
            destination,
            execute,
            preview_method="reflink-auto-or-copy",
            preview_detail="reflink-auto copy with portable fallback",
            success_method="reflink-auto",
            success_detail="Linux reflink-auto copy completed",
            unavailable_detail="reflink copy unavailable",
            optimized_copy=reflink,
        )

    def reclaim(self, arguments: list[str]) -> CapabilityResult:
        try:
            options = parse_reclaim_arguments(arguments)
        except ValueError as exc:
            return self.result("lean_reclaim", "REFUSED", str(exc))
        dry_run = options.dry_run
        hard_pressure = options.hard_pressure
        try:
            snapshot = self._run([
                "/bin/ps", "-eo", "pid=,ppid=,uid=,rss=,stat=,lstart=,args=",
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("lean_reclaim", "UNAVAILABLE", str(exc))
        if snapshot.returncode:
            return self.result("lean_reclaim", "UNAVAILABLE", "process snapshot failed")
        owner_uid = os.getuid()
        table: dict[int, Process] = {}
        for line in snapshot.stdout.splitlines():
            fields = line.split(None, 10)
            if len(fields) != 11 or fields[4].startswith("Z"):
                continue
            try:
                pid, ppid, uid, rss = map(int, fields[:4])
            except ValueError:
                continue
            if uid != owner_uid:
                continue
            table[pid] = Process(pid, ppid, rss, " ".join(fields[5:10]), fields[10])
        invocation_parent = os.getppid()
        unscoped = build_plan(
            table,
            invocation_parent,
            lambda process: bool(self.client_pattern.search(process.command)),
            hard_pressure,
        )
        if options.scope_roots and unscoped.owned:
            cwds: dict[int, str] = {}
            for pid in unscoped.owned:
                try:
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                except OSError:
                    continue
                if cwd and not cwd.endswith(" (deleted)"):
                    cwds[pid] = cwd
            missing = [pid for pid in unscoped.owned if pid not in cwds]
            if missing:
                return self.result(
                    "lean_reclaim", "UNAVAILABLE",
                    "goal-scoped cwd ownership is incomplete; no process was signalled",
                    {"unscoped_candidate_count": len(unscoped.owned)},
                )
            table = {
                pid: replace(process, cwd=cwds.get(pid))
                for pid, process in table.items()
            }
        plan = build_plan(
            table,
            invocation_parent,
            lambda process: bool(self.client_pattern.search(process.command)),
            hard_pressure,
            (
                (lambda process: process_in_scope(process, options.scope_roots))
                if options.scope_roots else None
            ),
        )
        plan = replace(plan, targets=narrow_targets(plan.targets, options.only_pids))
        public: dict[str, object] = {
            "mode": "hard-pressure" if hard_pressure else "ordinary",
            "dry_run": dry_run,
            "only_pids": list(options.only_pids),
            "scope_roots": [str(root) for root in options.scope_roots],
            "owned": [
                {"pid": pid, "rss_kib": table[pid].rss_kib, "kind": table[pid].kind}
                for pid in plan.owned
            ],
            "foreign_left_alone": [
                {"pid": pid, "rss_kib": table[pid].rss_kib, "kind": table[pid].kind}
                for pid in plan.foreign
            ],
            "protected_roots": list(plan.protected_roots),
            "termination_order": list(plan.targets),
        }
        if dry_run or not plan.targets:
            detail = (
                "dry-run frozen plan"
                if dry_run
                else "nothing proven safe and owned to reclaim"
            )
            return self.result("lean_reclaim", "OK", detail, public)

        def same_instance(pid: int) -> bool:
            try:
                current = self._run([
                    "/bin/ps", "-p", str(pid), "-o", "uid=,stat=,lstart=,args=",
                ])
            except (OSError, subprocess.SubprocessError):
                return False
            fields = current.stdout.strip().split(None, 7)
            try:
                current_uid = int(fields[0])
            except (IndexError, ValueError):
                return False
            return (
                current.returncode == 0
                and len(fields) == 8
                and current_uid == owner_uid
                and not fields[1].startswith("Z")
                and " ".join(fields[2:7]) == table[pid].started
                and fields[7] == table[pid].command
            )

        live = [pid for pid in plan.targets if same_instance(pid)]
        for pid in live:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            except OSError:
                return self.result(
                    "lean_reclaim", "REFUSED",
                    "SIGTERM failed; stopped without widening target set", public,
                )
        time.sleep(2)
        remaining = [pid for pid in plan.targets if same_instance(pid)]
        for pid in remaining:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            except OSError:
                return self.result(
                    "lean_reclaim", "REFUSED",
                    "SIGKILL failed for a proven target", public,
                )
        time.sleep(1)
        survivors = [pid for pid in plan.targets if same_instance(pid)]
        public["sigterm_count"] = len(live)
        public["sigkill_count"] = len(remaining)
        public["survivors"] = survivors
        status = "OK" if not survivors and not plan.protected_roots else "REFUSED"
        detail = (
            "reclamation completed"
            if status == "OK"
            else "partial or surviving subtree; restart the client"
        )
        return self.result("lean_reclaim", status, detail, public)
