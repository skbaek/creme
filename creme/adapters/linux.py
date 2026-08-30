from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from .base import Adapter, CapabilityResult


class LinuxAdapter(Adapter):
    system = "Linux"
    optional_capabilities = ("reflink_copy",)

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

    def telemetry(self) -> CapabilityResult:
        try:
            memory = self._meminfo()
            processes = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,rss=,comm="],
                capture_output=True, text=True, timeout=10,
            )
            swap_used_kib = 0
            with open("/proc/swaps", encoding="utf-8") as swaps:
                next(swaps, None)
                for line in swaps:
                    fields = line.split()
                    if len(fields) >= 4:
                        swap_used_kib += int(fields[3])
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return self.result("telemetry", "UNAVAILABLE", str(exc))
        if processes.returncode:
            return self.result("telemetry", "UNAVAILABLE", "ps failed")
        total = memory.get("MemTotal")
        available = memory.get("MemAvailable")
        free_pct = int(available * 100 / total) if total and available is not None else None
        clients = {"codex": 0, "claude": 0}
        lean = []
        largest = []
        for line in processes.stdout.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) != 4:
                continue
            pid, ppid, rss_text, command = fields
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
            "memory_free_percent": free_pct,
            "swap_used_mib": swap_used_kib / 1024,
            "client_family_rss_kib": {**clients, "total": sum(clients.values())},
            "lean_processes": lean,
            "largest_processes": largest[:15],
        }
        return self.result("telemetry", "OK", "Linux telemetry sampled", data)

    def process_snapshot(self) -> CapabilityResult:
        try:
            processes = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,rss=,comm="],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("process_snapshot", "UNAVAILABLE", str(exc))
        if processes.returncode:
            return self.result("process_snapshot", "UNAVAILABLE", "Linux ps snapshot failed")
        rows = []
        for line in processes.stdout.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) != 4:
                continue
            try:
                rows.append({
                    "pid": int(fields[0]), "ppid": int(fields[1]),
                    "rss_kib": int(fields[2]), "command": Path(fields[3]).name,
                })
            except ValueError:
                continue
        return self.result("process_snapshot", "OK", "Linux process snapshot sampled", {"processes": rows})

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
        if not source.is_dir() or destination.exists():
            return super().copy_cache(source, destination, execute)
        data = {"source": str(source), "destination": str(destination), "method": "reflink-auto-or-copy"}
        if not execute:
            return self.result("cache_copy", "PREVIEW", "reflink-auto copy with portable fallback", data)
        cp = shutil.which("cp")
        if cp:
            try:
                copied = subprocess.run(
                    [cp, "--reflink=auto", "-a", str(source), str(destination)],
                    capture_output=True, text=True, timeout=1800,
                )
            except (OSError, subprocess.SubprocessError):
                copied = None
            if copied is not None and copied.returncode == 0:
                return self.result("cache_copy", "OK", "Linux reflink-auto copy completed", data)
        fallback = super().copy_cache(source, destination, True)
        return self.result("cache_copy", fallback.status, "reflink copy unavailable; portable copy used", fallback.data)
