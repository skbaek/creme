"""Common mechanics explicitly adopted by supported native adapters.

Concrete adapters own platform identity and process-command selection. The
unsupported Adapter does not inherit these capabilities.
"""

from __future__ import annotations

import re
import subprocess

from .base import Adapter, CapabilityResult
from ..reclaim import is_lean_worker as _is_lean_worker, parse_cpu_seconds as _parse_cpu_seconds


class NativeAdapter(Adapter):
    """Share native runtime and worker-snapshot mechanics, without OS probes."""

    @staticmethod
    def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

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
            if _is_lean_worker(command)
        ]
        return self.result(
            "lean_workers", "OK",
            f"{len(workers)} Lean worker(s) sampled",
            {"workers": workers},
        )
