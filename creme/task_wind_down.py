from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import semaphore
from .adapters import Adapter, CapabilityResult
from .profile import DEFAULT_RELATIVE_PROFILE, load as load_profile


FAILURE_STATUSES = {"UNAVAILABLE", "BUSY", "REFUSED", "ERROR"}
GOAL_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SANCTIONED_SUFFIXES = ("control", "mutation", "rehearsal")


class WorktreeScopeError(ValueError):
    pass


def _goal_worktree_roots(label: str, adapter: Adapter) -> tuple[Path, ...]:
    """Resolve the configured per-goal worktrees used as a process boundary."""
    if GOAL_LABEL.fullmatch(label) is None or label in {".", ".."}:
        raise WorktreeScopeError("goal label cannot identify a safe worktree scope")

    creme_root = semaphore.canonical_creme_root()
    checked = load_profile(creme_root / DEFAULT_RELATIVE_PROFILE, adapter)
    if checked.profile is not None and checked.status in {"VALID", "LIMITED", "STALE"}:
        workspace = checked.profile["workspace"]
        workspace_root = Path(workspace["root"]).expanduser().resolve()
        repository_names = (workspace["jaune"], workspace["blanc"])
    else:
        workspace_root = creme_root.parent.resolve()
        repository_names = ("jaune", "blanc")

    roots: list[Path] = []
    for repository_name in repository_names:
        repository = (workspace_root / repository_name).resolve()
        try:
            repository.relative_to(workspace_root)
        except ValueError as exc:
            raise WorktreeScopeError(
                "configured repository escapes the workspace root"
            ) from exc
        worktree_parent = repository / ".worktrees"
        # The build owner treats a goal's sanctioned disposable trees as that
        # goal's own, so wind-down has to reclaim servers left in them too;
        # otherwise it would report OK while a Lean process survived in one.
        names = [label, *(f"{label}-{suffix}" for suffix in SANCTIONED_SUFFIXES)]
        for name in names:
            candidate = worktree_parent / name
            if candidate.is_symlink():
                raise WorktreeScopeError("goal worktree scope cannot be a symlink")
            if not candidate.is_dir() or not (candidate / ".git").is_file():
                continue
            resolved_parent = worktree_parent.resolve()
            resolved = candidate.resolve()
            try:
                resolved.relative_to(resolved_parent)
            except ValueError as exc:
                raise WorktreeScopeError(
                    "goal worktree resolves outside the repository worktree directory"
                ) from exc
            if resolved not in roots:
                roots.append(resolved)

    if not roots:
        raise WorktreeScopeError(
            "no configured per-goal Jaune/Blanc worktree exists; hold retained"
        )
    return tuple(roots)


def wind_down(label: str, adapter: Adapter) -> CapabilityResult:
    """Reclaim owned Lean servers, verify absence, then release ``label``."""
    observations: dict[str, Any] = {"label": label}
    failure_status = "REFUSED"

    if (
        not label
        or label == semaphore.MANUAL_LABEL
        or GOAL_LABEL.fullmatch(label) is None
    ):
        return adapter.result(
            "task_wind_down",
            "REFUSED",
            "reserved, empty, or unsafe goal label",
            observations,
        )

    try:
        scope_roots = _goal_worktree_roots(label, adapter)
    except (OSError, WorktreeScopeError) as exc:
        return adapter.result(
            "task_wind_down",
            "REFUSED",
            f"goal-scoped Lean ownership could not be established: {exc}",
            observations,
        )
    observations["scope_roots"] = [str(root) for root in scope_roots]
    scope_options = [
        item
        for root in scope_roots
        for item in ("--scope-root", str(root))
    ]

    def fail(status: str, detail: str) -> tuple[bool, str]:
        nonlocal failure_status
        failure_status = status if status in FAILURE_STATUSES else "REFUSED"
        return False, detail

    def cleanup() -> tuple[bool, str]:
        try:
            reclaimed = adapter.reclaim(scope_options)
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
            verified = adapter.reclaim(["--dry-run", *scope_options])
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
        ok, detail = semaphore.release_after_cleanup(
            label,
            cleanup,
            goal_scoped=True,
        )
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
