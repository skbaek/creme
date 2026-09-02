from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from .base import Adapter, CapabilityResult
from ..reclaim import (
    Process,
    build_plan,
    parse_reclaim_arguments,
    process_in_scope,
)


class DarwinAdapter(Adapter):
    system = "Darwin"
    optional_capabilities = (
        "human_gui_sessions", "memory_pressure", "apfs_clone", "lean_reclaim",
    )
    client_pattern = re.compile(
        r"(?:/Applications/(?:ChatGPT|Codex|Claude)\.app/|/(?:codex|claude)$|claude\.app/)",
        re.IGNORECASE,
    )

    @staticmethod
    def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def platform_identity(self, machine: str | None = None) -> CapabilityResult:
        detected = (machine or platform.machine()).strip().lower()
        if detected in {"arm64", "aarch64"}:
            canonical_machine = "arm64"
            uv_platform = "macos-aarch64-none"
        elif detected in {"x86_64", "amd64"}:
            canonical_machine = "x86_64"
            uv_platform = "macos-x86_64-none"
        else:
            return self.result(
                "platform_identity", "UNAVAILABLE",
                f"unsupported Darwin machine architecture: {detected or '<empty>'}",
            )
        key = f"macos-{canonical_machine}"
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

    def static_facts(self) -> CapabilityResult:
        try:
            memory = self._run(["/usr/sbin/sysctl", "-n", "hw.memsize"])
            cores = self._run(["/usr/sbin/sysctl", "-n", "hw.logicalcpu"])
            machine = self._run(["/usr/bin/uname", "-m"])
        except (OSError, subprocess.SubprocessError) as exc:
            memory = cores = machine = None
        try:
            memory_bytes = int(memory.stdout.strip()) if memory is not None and memory.returncode == 0 else (
                int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
            )
            logical_cores = int(cores.stdout.strip()) if cores is not None and cores.returncode == 0 else (os.cpu_count() or 1)
            machine_name = machine.stdout.strip() if machine is not None and machine.returncode == 0 else platform.machine()
        except (OSError, ValueError) as exc:
            return self.result("static_facts", "UNAVAILABLE", str(exc))
        data = {
            "system": self.system,
            "machine": machine_name or "unknown",
            "logical_cores": logical_cores,
            "physical_memory_bytes": memory_bytes,
        }
        detail = "static Darwin facts detected"
        if memory is None or cores is None or memory.returncode or cores.returncode:
            detail += " with portable sysconf fallbacks"
        return self.result("static_facts", "OK", detail, data)

    def memory_headroom(self) -> CapabilityResult:
        try:
            pressure = self._run(["/usr/bin/memory_pressure", "-Q"])
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("memory_headroom", "UNAVAILABLE", str(exc))
        if pressure.returncode:
            return self.result("memory_headroom", "UNAVAILABLE", "Darwin memory-pressure probe failed")
        free_match = re.search(r"free percentage:\s*(\d+)%", pressure.stdout)
        total_match = re.search(r"system has\s+(\d+)", pressure.stdout, re.IGNORECASE)
        if free_match is None:
            return self.result(
                "memory_headroom", "UNAVAILABLE",
                "Darwin memory-pressure output did not contain free percentage",
            )
        free_pct = int(free_match.group(1))
        total_bytes = int(total_match.group(1)) if total_match else None
        used_mib = None
        swap_detail = "swap unavailable"
        try:
            swap = self._run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
        except (OSError, subprocess.SubprocessError):
            swap = None
        if swap is not None and swap.returncode == 0:
            swap_match = re.search(r"used\s*=\s*([0-9.]+)([MG])", swap.stdout)
            if swap_match:
                value = float(swap_match.group(1))
                used_mib = value * (1024 if swap_match.group(2) == "G" else 1)
                swap_detail = "swap sampled"
        data = {
            "memory_free_percent": free_pct,
            "memory_available_bytes": (
                int(total_bytes * free_pct / 100) if total_bytes is not None else None
            ),
            "physical_memory_bytes": total_bytes,
            "swap_used_mib": used_mib,
        }
        return self.result(
            "memory_headroom", "OK",
            f"Darwin aggregate memory headroom sampled; {swap_detail}",
            data,
        )

    def telemetry(self) -> CapabilityResult:
        headroom = self.memory_headroom()
        if headroom.status != "OK" or not headroom.data:
            return self.result("telemetry", "UNAVAILABLE", headroom.detail)
        try:
            processes = self._run(["/bin/ps", "-axo", "pid=,ppid=,rss=,comm="])
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("telemetry", "UNAVAILABLE", str(exc))
        if processes.returncode:
            return self.result("telemetry", "UNAVAILABLE", "Darwin process snapshot failed")
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
            if "ChatGPT.app" in command or "Codex.app" in command or command.endswith("/codex"):
                clients["codex"] += rss
            elif "Claude.app" in command or command.endswith("/claude"):
                clients["claude"] += rss
            if command.endswith("/lean") or command.endswith("/lake") or "lean-lsp-mcp" in command:
                lean.append({"pid": int(pid), "ppid": int(ppid), "rss_kib": rss, "command": Path(command).name})
            largest.append({"pid": int(pid), "ppid": int(ppid), "rss_kib": rss, "command": Path(command).name})
        largest.sort(key=lambda row: row["rss_kib"], reverse=True)
        data = {
            **headroom.data,
            "client_family_rss_kib": {**clients, "total": sum(clients.values())},
            "lean_processes": lean,
            "largest_processes": largest[:15],
        }
        return self.result("telemetry", "OK", "Darwin telemetry sampled", data)

    def process_snapshot(self) -> CapabilityResult:
        try:
            processes = self._run(["/bin/ps", "-axo", "pid=,ppid=,rss=,comm="])
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("process_snapshot", "UNAVAILABLE", str(exc))
        if processes.returncode:
            return self.result("process_snapshot", "UNAVAILABLE", "Darwin ps snapshot failed")
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
        return self.result("process_snapshot", "OK", "Darwin process snapshot sampled", {"processes": rows})

    def quiet_host(self) -> CapabilityResult:
        sample = self.telemetry()
        if sample.status != "OK" or not sample.data:
            return self.result("quiet_host", "UNAVAILABLE", sample.detail)
        free = sample.data.get("memory_free_percent")
        lean = sample.data.get("lean_processes") or []
        if free is None:
            return self.result("quiet_host", "UNAVAILABLE", "memory headroom is unmeasurable")
        quiet = not lean and free >= 25
        return self.result(
            "quiet_host", "OK" if quiet else "BUSY",
            "host meets conservative quiet checks" if quiet else "Lean activity or low memory prevents certification",
            {"memory_free_percent": free, "lean_process_count": len(lean)},
        )

    def gui_sessions(self, owner_uid: int) -> CapabilityResult:
        try:
            users = self._run(["/usr/bin/dscacheutil", "-q", "user"])
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("human_gui_sessions", "UNAVAILABLE", str(exc))
        if users.returncode:
            return self.result("human_gui_sessions", "UNAVAILABLE", "local-user enumeration failed")
        accounts: dict[int, str] = {}
        for block in re.split(r"\n\s*\n", users.stdout.strip()):
            fields = dict(
                line.split(":", 1) for line in block.splitlines() if ":" in line
            )
            try:
                uid = int(fields.get("uid", "-1").strip())
            except ValueError:
                continue
            name = fields.get("name", "").strip()
            if uid >= 500 and name and not name.startswith("_"):
                accounts[uid] = name
        if owner_uid not in accounts:
            return self.result("human_gui_sessions", "UNAVAILABLE", "manual-hold owner is not a detected login account")
        sessions = []
        for uid, name in sorted(accounts.items()):
            if uid == owner_uid:
                continue
            probe = self._run(["/bin/launchctl", "print", f"gui/{uid}"])
            if probe.returncode == 0:
                sessions.append({"uid": uid, "name": name})
            elif not any(marker in probe.stderr for marker in (
                "Domain does not support specified action", "Could not find specified service",
            )):
                return self.result("human_gui_sessions", "UNAVAILABLE", f"GUI-domain query failed for uid {uid}")
        return self.result("human_gui_sessions", "OK", "GUI sessions enumerated", {"sessions": sessions})

    def copy_cache(self, source: Path, destination: Path, execute: bool) -> CapabilityResult:
        def clone(staged: Path) -> bool:
            try:
                result = self._run(
                    ["/bin/cp", "-c", "-R", str(source), str(staged)],
                    timeout=1800,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return result.returncode == 0

        return self._copy_cache_with_optimized_staging(
            source,
            destination,
            execute,
            preview_method="apfs-clone-or-copy",
            preview_detail="APFS clone with portable fallback",
            success_method="apfs-clone",
            success_detail="APFS clone completed",
            unavailable_detail="APFS clone unavailable",
            optimized_copy=clone,
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
                "/bin/ps", "-axo", "pid=,ppid=,rss=,lstart=,command=",
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("lean_reclaim", "UNAVAILABLE", str(exc))
        if snapshot.returncode:
            return self.result("lean_reclaim", "UNAVAILABLE", "process snapshot failed")
        table = {}
        for line in snapshot.stdout.splitlines():
            fields = line.split(None, 8)
            if len(fields) != 9:
                continue
            try:
                pid, ppid, rss = map(int, fields[:3])
            except ValueError:
                continue
            table[pid] = Process(pid, ppid, rss, " ".join(fields[3:8]), fields[8])
        invocation_parent = os.getppid()
        unscoped = build_plan(
            table,
            invocation_parent,
            lambda process: bool(self.client_pattern.search(process.command)),
            hard_pressure,
        )
        if options.scope_roots and unscoped.owned:
            try:
                cwd_sample = self._run([
                    "/usr/sbin/lsof", "-a", "-d", "cwd", "-Fn", "-p",
                    ",".join(str(pid) for pid in unscoped.owned),
                ])
            except (OSError, subprocess.SubprocessError) as exc:
                return self.result("lean_reclaim", "UNAVAILABLE", str(exc))
            if cwd_sample.returncode:
                return self.result(
                    "lean_reclaim", "UNAVAILABLE",
                    "goal-scoped cwd sampling failed; no process was signalled",
                )
            current_pid = None
            cwds: dict[int, str] = {}
            for line in cwd_sample.stdout.splitlines():
                if line.startswith("p") and line[1:].isdigit():
                    current_pid = int(line[1:])
                elif line.startswith("n") and current_pid is not None:
                    cwd = line[1:]
                    if cwd and not cwd.endswith(" (deleted)"):
                        cwds[current_pid] = cwd
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
        public = {
            "mode": "hard-pressure" if hard_pressure else "ordinary",
            "dry_run": dry_run,
            "scope_roots": [str(root) for root in options.scope_roots],
            "owned": [{"pid": pid, "rss_kib": table[pid].rss_kib, "kind": table[pid].kind} for pid in plan.owned],
            "foreign_left_alone": [{"pid": pid, "rss_kib": table[pid].rss_kib, "kind": table[pid].kind} for pid in plan.foreign],
            "protected_roots": list(plan.protected_roots),
            "termination_order": list(plan.targets),
        }
        if dry_run or not plan.targets:
            detail = "dry-run frozen plan" if dry_run else "nothing proven safe and owned to reclaim"
            return self.result("lean_reclaim", "OK", detail, public)

        def same_instance(pid: int) -> bool:
            try:
                current = self._run(["/bin/ps", "-p", str(pid), "-o", "lstart=,command="])
            except (OSError, subprocess.SubprocessError):
                return False
            fields = current.stdout.strip().split(None, 5)
            return (
                current.returncode == 0
                and len(fields) == 6
                and " ".join(fields[:5]) == table[pid].started
                and fields[5] == table[pid].command
            )

        live = [pid for pid in plan.targets if same_instance(pid)]
        for pid in live:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            except OSError:
                return self.result("lean_reclaim", "REFUSED", "SIGTERM failed; stopped without widening target set", public)
        time.sleep(2)
        remaining = [pid for pid in plan.targets if same_instance(pid)]
        for pid in remaining:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            except OSError:
                return self.result("lean_reclaim", "REFUSED", "SIGKILL failed for a proven target", public)
        time.sleep(1)
        survivors = [pid for pid in plan.targets if same_instance(pid)]
        public["sigterm_count"] = len(live)
        public["sigkill_count"] = len(remaining)
        public["survivors"] = survivors
        status = "OK" if not survivors and not plan.protected_roots else "REFUSED"
        detail = "reclamation completed" if status == "OK" else "partial or surviving subtree; restart the client"
        return self.result("lean_reclaim", status, detail, public)
