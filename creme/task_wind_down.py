from __future__ import annotations

from typing import Any

from . import semaphore
from .adapters import Adapter, CapabilityResult


FAILURE_STATUSES = {"UNAVAILABLE", "BUSY", "REFUSED", "ERROR"}


def wind_down(label: str, adapter: Adapter) -> CapabilityResult:
    """Reclaim owned Lean servers, verify absence, then release ``label``."""
    observations: dict[str, Any] = {"label": label}
    failure_status = "REFUSED"

    def fail(status: str, detail: str) -> tuple[bool, str]:
        nonlocal failure_status
        failure_status = status if status in FAILURE_STATUSES else "REFUSED"
        return False, detail

    def cleanup() -> tuple[bool, str]:
        try:
            reclaimed = adapter.reclaim([])
        except Exception:
            return fail("ERROR", "Lean reclamation raised an exception")
        observations["reclaim"] = reclaimed.to_dict()
        if reclaimed.status != "OK":
            return fail(
                reclaimed.status,
                f"Lean reclamation {reclaimed.status}: {reclaimed.detail}",
            )
        if not isinstance(reclaimed.data, dict):
            return fail("ERROR", "Lean reclamation returned no inspectable process plan")
        if reclaimed.data.get("protected_roots"):
            return fail("REFUSED", "owned Lean roots remain protected by active descendants")
        if reclaimed.data.get("survivors"):
            return fail("REFUSED", "owned Lean processes survived reclamation")

        try:
            verified = adapter.reclaim(["--dry-run"])
        except Exception:
            return fail("ERROR", "post-reclamation verification raised an exception")
        observations["verification"] = verified.to_dict()
        if verified.status != "OK":
            return fail(
                verified.status,
                f"post-reclamation verification {verified.status}: {verified.detail}",
            )
        if not isinstance(verified.data, dict):
            return fail(
                "ERROR",
                "post-reclamation verification returned no inspectable process plan",
            )
        if verified.data.get("owned"):
            return fail("REFUSED", "post-reclamation verification still found owned Lean processes")
        if verified.data.get("protected_roots"):
            return fail("REFUSED", "post-reclamation verification found protected Lean roots")
        return True, "no owned Lean servers remain"

    try:
        ok, detail = semaphore.release_after_cleanup(label, cleanup)
    except (OSError, semaphore.SemaphoreError) as exc:
        return adapter.result(
            "task_wind_down",
            "ERROR",
            f"semaphore state could not be updated safely; hold retained: {exc}",
            observations,
        )
    return adapter.result(
        "task_wind_down",
        "OK" if ok else failure_status,
        detail,
        observations,
    )
