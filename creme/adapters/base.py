from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional


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

    def platform_identity(self, machine: Optional[str] = None) -> CapabilityResult:
        return self.result(
            "platform_identity", "UNAVAILABLE",
            f"no canonical platform identity is defined for {self.system}",
        )

    def python_runtime(
        self,
        version: str,
        machine: Optional[str] = None,
    ) -> CapabilityResult:
        return self.result(
            "python_runtime", "UNAVAILABLE",
            f"no native managed-Python identity is defined for {self.system}",
        )

    def telemetry(self) -> CapabilityResult:
        return self.result(
            "telemetry", "UNAVAILABLE",
            f"telemetry is not implemented for {self.system}",
        )

    def memory_headroom(self) -> CapabilityResult:
        """Sample admission-critical memory state without process discovery.

        Process enumeration is commonly denied inside an agent sandbox even
        when the operating system's aggregate memory counters remain readable.
        Heavy-work admission therefore uses this narrower capability instead
        of treating a failed process snapshot as an unknown memory state.
        """
        return self.result(
            "memory_headroom", "UNAVAILABLE",
            f"memory headroom is not implemented for {self.system}",
        )

    def quiet_host(self) -> CapabilityResult:
        return self.result(
            "quiet_host", "UNAVAILABLE",
            "cannot certify host quiet on an unsupported platform",
        )

    def process_snapshot(self) -> CapabilityResult:
        return self.result(
            "process_snapshot", "UNAVAILABLE",
            f"process snapshots are not implemented for {self.system}",
        )

    def process_working_directories(self, pids: list[int]) -> CapabilityResult:
        """Sample the working directory of each named pid.

        Attribution of a Lean process to a goal cannot use a parent chain: a
        hold taken from one shell call is not an ancestor of the gate launched
        by the next.  The reclaim path already resolves working directories
        for the processes it may signal; this exposes the same sample as a
        read-only capability so idleness can be judged the same way.  A pid the
        sample cannot answer for is simply absent from the map, and the caller
        must fail closed rather than treat it as out of scope.
        """
        return self.result(
            "process_working_directories", "UNAVAILABLE",
            f"process working directories are not readable on {self.system}",
        )

    def lean_workers(self) -> CapabilityResult:
        """Sample Lean language-server workers with cumulative CPU time.

        Admission needs to know whether resident Lean memory is reclaimable
        before it refuses a request for headroom.  Cumulative CPU seconds make
        idleness measurable across calls without holding a sampling window
        open, so `status` and `renew` stay fast.
        """
        return self.result(
            "lean_workers", "UNAVAILABLE",
            f"Lean worker sampling is not implemented for {self.system}",
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

    def _portable_cache_copy(
        self,
        source: Path,
        destination: Path,
    ) -> CapabilityResult:
        data = {
            "source": str(source),
            "destination": str(destination),
            "method": "copytree",
        }
        try:
            shutil.copytree(source, destination, symlinks=True)
        except OSError as exc:
            return self.result(
                "cache_copy",
                "ERROR",
                f"portable recursive copy failed; any partial destination was retained: {exc}",
                data,
            )
        return self.result(
            "cache_copy", "OK", "portable recursive copy completed", data
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
        return self._portable_cache_copy(source, destination)

    def _copy_cache_with_optimized_staging(
        self,
        source: Path,
        destination: Path,
        execute: bool,
        *,
        preview_method: str,
        preview_detail: str,
        success_method: str,
        success_detail: str,
        unavailable_detail: str,
        optimized_copy: Callable[[Path], bool],
    ) -> CapabilityResult:
        if not source.is_dir() or destination.exists():
            # Bypass the platform override: both optimized adapters enter this
            # helper from their own `copy_cache`, so dynamic dispatch here
            # would recurse instead of returning the portable validation error.
            return Adapter.copy_cache(self, source, destination, execute)
        preview_data = {
            "source": str(source),
            "destination": str(destination),
            "method": preview_method,
        }
        if not execute:
            return self.result("cache_copy", "PREVIEW", preview_detail, preview_data)

        try:
            stage_root = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.creme-copy-",
                    dir=destination.parent,
                )
            )
        except OSError:
            fallback = self._portable_cache_copy(source, destination)
            return self.result(
                "cache_copy",
                fallback.status,
                f"{unavailable_detail}; {fallback.detail}",
                fallback.data,
            )

        staged = stage_root / "payload"
        try:
            optimized = optimized_copy(staged)
            if optimized:
                data = dict(preview_data)
                data["method"] = success_method
                if not staged.is_dir():
                    return self.result(
                        "cache_copy",
                        "ERROR",
                        "optimized copy reported success without a staged directory",
                        data,
                    )
                if destination.exists():
                    return self.result(
                        "cache_copy",
                        "ERROR",
                        "destination appeared during the copy; staged data was not published",
                        data,
                    )
                try:
                    staged.rename(destination)
                except OSError as exc:
                    return self.result(
                        "cache_copy",
                        "ERROR",
                        f"completed staged copy could not be published: {exc}",
                        data,
                    )
                return self.result("cache_copy", "OK", success_detail, data)

            fallback = self._portable_cache_copy(source, destination)
            return self.result(
                "cache_copy",
                fallback.status,
                f"{unavailable_detail}; {fallback.detail}",
                fallback.data,
            )
        finally:
            # This path was allocated by mkdtemp above and is never a caller's
            # destination. A failed direct fallback is deliberately retained.
            shutil.rmtree(stage_root, ignore_errors=True)
