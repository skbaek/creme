from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from .base import CapabilityResult, swap_compressor_pressure
from .native import NativeAdapter
from ..reclaim import (
    Process,
    narrow_targets,
    build_plan,
    parse_reclaim_arguments,
    process_in_scope,
)


class DarwinAdapter(NativeAdapter):
    system = "Darwin"
    optional_capabilities = (
        "human_gui_sessions", "memory_pressure", "apfs_clone", "lean_reclaim",
    )
    client_pattern = re.compile(
        r"(?:/Applications/(?:ChatGPT|Codex|Claude|Antigravity)\.app/|/(?:codex|claude|antigravity)$|claude\.app/|/muse-bin-[^/\s]+|/muse(?=\s|$))",
        re.IGNORECASE,
    )

    codex_bundle_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

    def codex_binary(self) -> CapabilityResult:
        path = self.codex_bundle_binary
        if path.is_file() and os.access(path, os.X_OK):
            return self.result("codex_binary", "OK", "ChatGPT desktop bundled Codex CLI", {"path": str(path)})
        return self.result("codex_binary", "UNAVAILABLE", f"no executable Codex CLI at {path}")

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
            # Without `-Q` the same probe also prints its page statistics,
            # including "Pages used by compressor" (the figure `vm_stat` calls
            # "Pages occupied by compressor"), so compressor occupancy needs
            # no second command.
            pressure = self._run(["/usr/bin/memory_pressure"])
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
        page_match = re.search(r"page size of\s+(\d+)", pressure.stdout)
        compressor_match = re.search(
            r"Pages (?:used|occupied) by compressor:\s*(\d+)", pressure.stdout
        )
        compressor_bytes = (
            int(compressor_match.group(1)) * int(page_match.group(1))
            if compressor_match and page_match
            else None
        )
        used_mib = total_mib = free_mib = None
        swap_detail = "swap unavailable"
        try:
            swap = self._run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
        except (OSError, subprocess.SubprocessError):
            swap = None
        if swap is not None and swap.returncode == 0:
            def swap_field(name: str) -> Optional[float]:
                match = re.search(rf"{name}\s*=\s*([0-9.]+)([KMG])", swap.stdout)
                if match is None:
                    return None
                scale = {"K": 1 / 1024, "M": 1, "G": 1024}[match.group(2)]
                return float(match.group(1)) * scale

            used_mib = swap_field("used")
            total_mib = swap_field("total")
            free_mib = swap_field("free")
            if used_mib is not None:
                swap_detail = "swap sampled"
        cause = swap_compressor_pressure(total_bytes, total_mib, used_mib, compressor_bytes)
        level = self._vm_pressure_level()
        data = {
            "memory_free_percent": free_pct,
            "memory_available_bytes": (
                int(total_bytes * free_pct / 100) if total_bytes is not None else None
            ),
            "physical_memory_bytes": total_bytes,
            "swap_used_mib": used_mib,
            "swap_total_mib": total_mib,
            "swap_free_mib": free_mib,
            "compressor_bytes": compressor_bytes,
            "memory_pressure_cause": cause,
            # The kernel's own VM pressure level: 1 normal, 2 warning,
            # 4 critical.  Only critical retracts a running build.
            "memory_pressure_level": level,
        }
        detail = f"Darwin aggregate memory headroom sampled; {swap_detail}"
        if cause:
            detail += f"; SWAP_PRESSURE: {cause}"
        return self.result("memory_headroom", "OK", detail, data)

    def _vm_pressure_level(self) -> Optional[int]:
        """`kern.memorystatus_vm_pressure_level`, or None when unreadable."""
        try:
            completed = self._run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
            if completed.returncode:
                return None
            return int(str(completed.stdout).strip())
        except Exception:  # an unreadable level adds no signal
            return None

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
        clients = {"codex": 0, "claude": 0, "muse": 0}
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
            elif Path(command).name == "muse" or Path(command).name.startswith("muse-bin-"):
                clients["muse"] += rss
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

    def process_working_directories(self, pids: list[int]) -> CapabilityResult:
        """Read cwd for each pid with the same `lsof` sample reclamation uses."""
        wanted = sorted({int(pid) for pid in pids})
        if not wanted:
            return self.result(
                "process_working_directories", "OK",
                "no pids requested", {"working_directories": {}, "requested": []},
            )
        try:
            sample = self._run([
                "/usr/sbin/lsof", "-a", "-d", "cwd", "-Fn", "-p",
                ",".join(str(pid) for pid in wanted),
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("process_working_directories", "UNAVAILABLE", str(exc))
        current: Optional[int] = None
        cwds: dict[str, str] = {}
        for line in sample.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current = int(line[1:])
            elif line.startswith("n") and current is not None:
                cwd = line[1:]
                if cwd and not cwd.endswith(" (deleted)"):
                    cwds[str(current)] = cwd
        if not cwds and sample.returncode:
            # lsof exits nonzero when a pid is gone as well as when the sample
            # itself failed; an empty answer with a failure is not evidence.
            return self.result(
                "process_working_directories", "UNAVAILABLE",
                "cwd sampling returned no readable entry",
            )
        return self.result(
            "process_working_directories", "OK",
            f"sampled {len(cwds)} of {len(wanted)} working director(y/ies)",
            {"working_directories": cwds, "requested": wanted},
        )

    def quiet_host(self) -> CapabilityResult:
        sample = self.telemetry()
        if sample.status != "OK" or not sample.data:
            return self.result("quiet_host", "UNAVAILABLE", sample.detail)
        free = sample.data.get("memory_free_percent")
        lean = sample.data.get("lean_processes") or []
        if free is None:
            return self.result("quiet_host", "UNAVAILABLE", "memory headroom is unmeasurable")
        quiet = not lean and free >= 25 and not sample.data.get("memory_pressure_cause")
        return self.result(
            "quiet_host", "OK" if quiet else "BUSY",
            "host meets conservative quiet checks" if quiet else "Lean activity or low memory prevents certification",
            {"memory_free_percent": free, "lean_process_count": len(lean)},
        )

    def lean_workers(self) -> "CapabilityResult":
        return self._lean_worker_sample(
            ["/bin/ps", "-axo", "pid=,ppid=,rss=,time=,command="]
        )

    _TOP_SIZE = re.compile(r"^([0-9.]+)([BKMGT])[+-]?$")

    @classmethod
    def _top_kib(cls, text: str) -> Optional[int]:
        match = cls._TOP_SIZE.match(text.strip())
        if match is None:
            return None
        scale = {"B": 1 / 1024, "K": 1, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}
        return int(float(match.group(1)) * scale[match.group(2)])

    def process_footprints(self, pids: list[int]) -> CapabilityResult:
        """Physical footprint per pid from one `top` sample (compressed pages included).

        `top`'s MEM column is the kernel's physical footprint, which counts
        pages the compressor holds for the process; RSS does not.
        """
        wanted = sorted({int(pid) for pid in pids})
        if not wanted:
            return self.result(
                "process_footprints", "OK", "no pids requested", {"footprints": {}},
            )
        argv = ["/usr/bin/top", "-l", "1", "-stats", "pid,mem,cmprs"]
        for pid in wanted:
            argv += ["-pid", str(pid)]
        try:
            sample = self._run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return self.result("process_footprints", "UNAVAILABLE", str(exc))
        if sample.returncode:
            return self.result("process_footprints", "UNAVAILABLE", "Darwin top sample failed")
        footprints: dict[str, dict[str, int]] = {}
        header_seen = False
        for line in sample.stdout.splitlines():
            fields = line.split()
            if fields[:3] == ["PID", "MEM", "CMPRS"]:
                header_seen = True
                continue
            if not header_seen or len(fields) != 3 or not fields[0].isdigit():
                continue
            pid = int(fields[0])
            footprint = self._top_kib(fields[1])
            compressed = self._top_kib(fields[2])
            if pid not in wanted or footprint is None:
                continue
            footprints[str(pid)] = {
                "footprint_kib": footprint,
                "compressed_kib": compressed if compressed is not None else 0,
            }
        if not header_seen:
            return self.result(
                "process_footprints", "UNAVAILABLE", "Darwin top output had no process table",
            )
        return self.result(
            "process_footprints", "OK",
            f"sampled {len(footprints)} of {len(wanted)} footprint(s)",
            {"footprints": footprints},
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
        plan = replace(plan, targets=narrow_targets(plan.targets, options.only_pids))
        public = {
            "mode": "hard-pressure" if hard_pressure else "ordinary",
            "dry_run": dry_run,
            "only_pids": list(options.only_pids),
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
