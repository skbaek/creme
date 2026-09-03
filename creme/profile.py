from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .adapters import Adapter, get_adapter


SCHEMA_VERSION = 1
DEFAULT_RELATIVE_PROFILE = Path(".creme/host-profile.json")
DYNAMIC_KEYS = {
    "free_disk", "free_disk_bytes", "memory_free", "memory_free_percent",
    "memory_pressure", "swap", "swap_used", "swap_used_mib", "process_rss",
}
# Scheduling tunables, not safety floors.  Each has a working default in code;
# the profile may carry an optional `admission` object to retune one host.  The
# key is omitted from a proposed profile so `init` output stays byte-identical
# and pre-cutover launchers keep reading an untuned profile unchanged.
ADMISSION_DEFAULTS = {
    "tolerant_module_count": 8,
    "tolerant_peak_gib": 4,
    "estimate_margin_gib": 1,
    "minimum_estimate_gib": 2,
    "estimate_sample_rows": 5,
    "idle_hold_seconds": 120,
    "repeat_fail_seconds": 600,
    "wait_poll_seconds": 3,
}
ADMISSION_RANGES = {
    "tolerant_module_count": (1, 4096),
    "tolerant_peak_gib": (1, 64),
    "estimate_margin_gib": (0, 32),
    "minimum_estimate_gib": (1, 32),
    "estimate_sample_rows": (1, 200),
    "idle_hold_seconds": (10, 86400),
    "repeat_fail_seconds": (10, 86400),
    "wait_poll_seconds": (2, 5),
}


@dataclass(frozen=True)
class ProfileValidation:
    status: str
    detail: str
    profile: Optional[dict[str, Any]] = None


def fingerprint(facts: dict[str, Any]) -> str:
    stable = {
        "system": facts.get("system"),
        "machine": facts.get("machine"),
        "logical_cores": facts.get("logical_cores"),
        "physical_memory_bytes": facts.get("physical_memory_bytes"),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def derive_policy(facts: dict[str, Any]) -> dict[str, int]:
    memory_gib = int(facts["physical_memory_bytes"]) // (1024 ** 3)
    cores = max(1, int(facts["logical_cores"]))
    task_memory = max(1, min(8, memory_gib // 3 or 1))
    heavy = 1 if memory_gib < 16 else 2
    light = max(1, min(8, cores // 2 or 1))
    return {
        "task_memory_gib": task_memory,
        "heavy_workers": heavy,
        "light_workers": light,
    }


def propose(
    creme_root: Path,
    workspace_root: Optional[Path] = None,
    adapter: Optional[Adapter] = None,
) -> dict[str, Any]:
    selected = adapter or get_adapter()
    facts_result = selected.static_facts()
    if facts_result.status != "OK" or not facts_result.data:
        raise RuntimeError(f"cannot detect static host facts: {facts_result.detail}")
    facts = facts_result.data
    root = (workspace_root or creme_root.parent).expanduser().resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint(facts),
        "facts": facts,
        "workspace": {
            "root": str(root),
            "jaune": "jaune",
            "blanc": "blanc",
            "goal_store": None,
        },
        "policy": derive_policy(facts),
        "overrides": {
            "task_memory_gib": None,
            "heavy_workers": None,
            "light_workers": None,
        },
    }


def _is_positive_int(value: Any, maximum: Optional[int] = None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1 and (
        maximum is None or value <= maximum
    )


def validate_data(
    data: Any,
    current_facts: Optional[dict[str, Any]] = None,
) -> ProfileValidation:
    if not isinstance(data, dict):
        return ProfileValidation("INVALID", "profile root must be an object")
    required = {"schema_version", "fingerprint", "facts", "workspace", "policy", "overrides"}
    optional = {"admission"}
    if set(data) - optional != required:
        missing = sorted(required - set(data))
        extra = sorted(set(data) - required - optional)
        return ProfileValidation("INVALID", f"profile keys differ; missing={missing}, extra={extra}")
    admission = data.get("admission")
    if admission is not None:
        if not isinstance(admission, dict):
            return ProfileValidation("INVALID", "admission must be an object")
        unknown = sorted(set(admission) - set(ADMISSION_DEFAULTS))
        if unknown:
            return ProfileValidation("INVALID", f"unknown admission settings: {unknown}")
        for key, value in admission.items():
            low, high = ADMISSION_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                return ProfileValidation(
                    "INVALID", f"admission.{key} must be an integer in {low}..{high}"
                )
    if data.get("schema_version") != SCHEMA_VERSION:
        return ProfileValidation("INVALID", f"unsupported schema_version: {data.get('schema_version')!r}")
    facts = data.get("facts")
    if not isinstance(facts, dict) or set(facts) != {
        "system", "machine", "logical_cores", "physical_memory_bytes",
    }:
        return ProfileValidation("INVALID", "facts must contain only the four tracked static facts")
    if facts.get("system") not in {"Darwin", "Linux"}:
        return ProfileValidation("INVALID", "facts.system must be Darwin or Linux")
    if not isinstance(facts.get("machine"), str) or not facts["machine"]:
        return ProfileValidation("INVALID", "facts.machine must be non-empty")
    if not _is_positive_int(facts.get("logical_cores")) or not _is_positive_int(facts.get("physical_memory_bytes")):
        return ProfileValidation("INVALID", "static numeric facts must be positive integers")
    dynamic = DYNAMIC_KEYS.intersection(facts)
    if dynamic:
        return ProfileValidation("INVALID", f"dynamic facts may not be persisted: {sorted(dynamic)}")
    if data.get("fingerprint") != fingerprint(facts):
        return ProfileValidation("INVALID", "fingerprint does not match recorded static facts")
    workspace = data.get("workspace")
    if not isinstance(workspace, dict) or set(workspace) != {"root", "jaune", "blanc", "goal_store"}:
        return ProfileValidation("INVALID", "workspace has an unexpected shape")
    if not all(isinstance(workspace.get(key), str) and workspace[key] for key in ("root", "jaune", "blanc")):
        return ProfileValidation("INVALID", "workspace root and repository names must be non-empty strings")
    if workspace.get("goal_store") is not None and not isinstance(workspace["goal_store"], str):
        return ProfileValidation("INVALID", "workspace.goal_store must be a string or null")
    policy = data.get("policy")
    overrides = data.get("overrides")
    if not isinstance(policy, dict) or set(policy) != {"task_memory_gib", "heavy_workers", "light_workers"}:
        return ProfileValidation("INVALID", "policy has an unexpected shape")
    if not isinstance(overrides, dict) or set(overrides) != set(policy):
        return ProfileValidation("INVALID", "overrides must contain exactly the policy keys")
    for key, value in policy.items():
        maximum = 8 if key == "task_memory_gib" else None
        if not _is_positive_int(value, maximum):
            return ProfileValidation("INVALID", f"policy.{key} is outside its safe range")
        override = overrides[key]
        if override is not None and not _is_positive_int(override, maximum):
            return ProfileValidation("INVALID", f"overrides.{key} is outside its safe range")
    if current_facts is not None and data["fingerprint"] != fingerprint(current_facts):
        return ProfileValidation("STALE", "static host fingerprint changed; regenerate and review the profile", data)
    return ProfileValidation("VALID", "profile is valid and current", data)


def load(path: Path, adapter: Optional[Adapter] = None) -> ProfileValidation:
    if not path.is_file():
        return ProfileValidation("MISSING", f"profile not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ProfileValidation("INVALID", f"profile could not be read: {exc}")
    selected = adapter or get_adapter()
    current = selected.static_facts()
    current_facts = current.data if current.status == "OK" else None
    checked = validate_data(data, current_facts)
    if current_facts is None and checked.status == "VALID":
        return ProfileValidation("LIMITED", f"profile shape is valid but freshness is unverified: {current.detail}", data)
    return checked


def effective_policy(
    profile: Optional[dict[str, Any]],
    adapter: Optional[Adapter] = None,
    cli_overrides: Optional[dict[str, Optional[int]]] = None,
) -> dict[str, int]:
    selected = adapter or get_adapter()
    facts = selected.static_facts()
    shared = {"task_memory_gib": 2, "heavy_workers": 1, "light_workers": 1}
    os_defaults = derive_policy(facts.data) if facts.status == "OK" and facts.data else shared
    base = dict(os_defaults)
    if profile:
        base.update(profile.get("policy", {}))
        base.update({key: value for key, value in profile.get("overrides", {}).items() if value is not None})
    if cli_overrides:
        base.update({key: value for key, value in cli_overrides.items() if value is not None})
    base["task_memory_gib"] = max(1, min(8, int(base["task_memory_gib"])))
    base["heavy_workers"] = max(1, int(base["heavy_workers"]))
    base["light_workers"] = max(1, int(base["light_workers"]))
    return base


def write_reviewed(path: Path, profile: dict[str, Any]) -> None:
    validation = validate_data(profile)
    if validation.status != "VALID":
        raise ValueError(validation.detail)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="host-profile-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def admission_settings(profile: Optional[dict[str, Any]]) -> dict[str, int]:
    """Merge the host profile's optional scheduling tunables over the defaults.

    These change *when* work is scheduled, never whether a safety floor holds.
    An absent, malformed, or out-of-range value falls back to its default
    rather than widening admission.
    """
    settings = dict(ADMISSION_DEFAULTS)
    configured = (profile or {}).get("admission")
    if not isinstance(configured, dict):
        return settings
    for key, value in configured.items():
        if key not in ADMISSION_DEFAULTS:
            continue
        low, high = ADMISSION_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            continue
        settings[key] = value
    return settings


def load_admission_settings(
    creme_root: Optional[Path] = None,
    adapter: Optional[Adapter] = None,
) -> dict[str, int]:
    from . import semaphore

    try:
        root = creme_root or semaphore.canonical_creme_root()
        checked = load(root / DEFAULT_RELATIVE_PROFILE, adapter or get_adapter())
    except Exception:
        # Scheduling tunables must never be able to break a build; the
        # in-code defaults are the conservative values.
        return dict(ADMISSION_DEFAULTS)
    return admission_settings(checked.profile)
