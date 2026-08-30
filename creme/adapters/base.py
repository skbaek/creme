from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class CapabilityResult:
    capability: str
    status: str
    adapter: str
    detail: str
    data: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Adapter:
    system = "unsupported"
    optional_capabilities: tuple[str, ...] = ()

    @classmethod
    def unsupported(cls, name: str) -> "Adapter":
        instance = cls()
        instance.system = name or "unknown"
        return instance

    def result(
        self,
        capability: str,
        status: str,
        detail: str,
        data: Optional[dict[str, Any]] = None,
    ) -> CapabilityResult:
        return CapabilityResult(capability, status, self.system, detail, data)

    def static_facts(self) -> CapabilityResult:
        return self.result(
            "static_facts", "UNAVAILABLE",
            f"unsupported operating system: {self.system}",
        )

    def telemetry(self) -> CapabilityResult:
        return self.result(
            "telemetry", "UNAVAILABLE",
            f"telemetry is not implemented for {self.system}",
        )

    def quiet_host(self) -> CapabilityResult:
        return self.result(
            "quiet_host", "UNAVAILABLE",
            "cannot certify host quiet on an unsupported platform",
        )

    def gui_sessions(self, owner_uid: int) -> CapabilityResult:
        return self.result(
            "human_gui_sessions", "UNAVAILABLE",
            "manual GUI-session protection is not available on this platform",
        )

    def reclaim(self, arguments: list[str]) -> CapabilityResult:
        return self.result(
            "lean_reclaim", "UNAVAILABLE",
            "safe ownership-verifying Lean reclamation is not implemented; restart the client",
        )

    def temp_root(self) -> CapabilityResult:
        configured = os.environ.get("TMPDIR")
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_absolute() and candidate.is_dir() and os.access(candidate, os.W_OK):
                return self.result("temporary_directory", "OK", str(candidate), {"path": str(candidate)})
        root = Path(tempfile.gettempdir())
        if root.is_dir() and os.access(root, os.W_OK):
            return self.result("temporary_directory", "OK", str(root), {"path": str(root)})
        return self.result(
            "temporary_directory", "UNAVAILABLE",
            "no writable temporary directory was detected",
        )

    def copy_cache(self, source: Path, destination: Path, execute: bool) -> CapabilityResult:
        if not source.is_dir():
            return self.result("cache_copy", "ERROR", f"source is not a directory: {source}")
        if destination.exists():
            return self.result("cache_copy", "ERROR", f"destination already exists: {destination}")
        if not execute:
            return self.result(
                "cache_copy", "PREVIEW", "portable recursive copy",
                {"source": str(source), "destination": str(destination), "method": "copytree"},
            )
        shutil.copytree(source, destination, symlinks=True)
        return self.result(
            "cache_copy", "OK", "portable recursive copy completed",
            {"source": str(source), "destination": str(destination), "method": "copytree"},
        )
