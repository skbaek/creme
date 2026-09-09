from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

from . import master_migrate, master_reconcile, master_runtime, semaphore
from .adapters import Adapter, get_adapter
from .doctor import STATUS_FAIL, check_goal_store
from .profile import DEFAULT_RELATIVE_PROFILE, load as load_profile


DIGEST_SCHEMA_VERSION = 1
DEFAULT_DIGEST_LIMIT = 20
MAX_DIGEST_LIMIT = 100
HUMAN_PREVIEW_CHARS = 160

_DIGEST_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}")
_GOAL_PRIORITY = {
    "active": 0,
    "ready": 1,
    "blocked": 2,
    "queued": 3,
    "paused": 4,
    "complete": 5,
    "retired": 6,
}


class MasterOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeLocation:
    shared_creme_root: Path
    workspace_root: Path
    goal_store: Path
    record_root: Path
    repository_roots: tuple[tuple[str, Path], ...]


@dataclass(frozen=True)
class InitAction:
    path: str
    action: str
    kind: str
    mode: str
    detail: str


@dataclass(frozen=True)
class InitPlan:
    status: str
    record_root: str
    detail: str
    actions: tuple[InitAction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "record_root": self.record_root,
            "detail": self.detail,
            "actions": [asdict(action) for action in self.actions],
        }


Acquire = Callable[..., tuple[bool, str]]
Renew = Callable[[], tuple[bool, str]]
Release = Callable[[], tuple[bool, str]]
Heartbeat = Callable[[int], tuple[bool, str]]
Snapshot = Callable[[], dict[str, Any]]
LeaseStatus = Callable[[], str]


def _logical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _reject_symlink_traversal(path: Path, what: str) -> Path:
    logical = _logical_absolute(path)
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise MasterOperationError(f"{what} cannot be resolved safely: {exc}") from exc
    if resolved != logical:
        raise MasterOperationError(f"{what} crosses a symlinked boundary")
    return logical


def resolve_runtime_location(
    creme_root: Path,
    *,
    adapter: Optional[Adapter] = None,
) -> RuntimeLocation:
    """Resolve the one configured record root and repeat doctor's privacy check."""
    selected = adapter or get_adapter()
    try:
        shared_root = semaphore.canonical_creme_root(creme_root)
    except semaphore.SemaphoreError as exc:
        raise MasterOperationError(f"canonical Creme root is unavailable: {exc}") from exc
    checked = load_profile(shared_root / DEFAULT_RELATIVE_PROFILE, selected)
    if checked.status != "VALID" or checked.profile is None:
        raise MasterOperationError(
            f"host profile must be VALID for persistent master operations: "
            f"{checked.status}: {checked.detail}"
        )
    profile = checked.profile
    workspace_value = Path(profile["workspace"]["root"]).expanduser()
    if not workspace_value.is_absolute():
        raise MasterOperationError("configured workspace root must be absolute")
    workspace = _reject_symlink_traversal(workspace_value, "configured workspace root")
    goal_store_name = profile["workspace"].get("goal_store")
    if not isinstance(goal_store_name, str) or not goal_store_name:
        raise MasterOperationError("goal store is not configured; persistent master mode is unavailable")
    logical_store = workspace / goal_store_name
    store = _reject_symlink_traversal(logical_store, "configured goal store")
    if not store.is_dir():
        raise MasterOperationError(f"configured goal store is missing or not a directory: {store}")
    privacy = check_goal_store(workspace, profile)
    failed = [check.detail for check in privacy if check.status == STATUS_FAIL]
    if failed:
        raise MasterOperationError(f"goal-store privacy preflight failed: {'; '.join(failed)}")
    record_root = _reject_symlink_traversal(store / "master", "master record root")
    if record_root.exists() and not record_root.is_dir():
        raise MasterOperationError("master record root exists with the wrong file type")
    repository_roots = (
        ("creme", shared_root),
        ("jaune", _logical_absolute(workspace / profile["workspace"]["jaune"])),
        ("blanc", _logical_absolute(workspace / profile["workspace"]["blanc"])),
        ("goal-store", store),
    )
    return RuntimeLocation(shared_root, workspace, store, record_root, repository_roots)


def reconcile_location(
    location: RuntimeLocation,
    *,
    runner: master_reconcile.GitRunner = master_reconcile.run_git,
) -> master_reconcile.ReconciliationResult:
    return master_reconcile.reconcile_record(
        location.record_root,
        dict(location.repository_roots),
        runner=runner,
    )


def _standard_actions(action: str, detail: str) -> tuple[InitAction, ...]:
    actions = [InitAction("master/", action, "directory", "0700", detail)]
    actions.extend(
        InitAction(f"master/{name}/", action, "directory", "0700", detail)
        for name in master_runtime.PRIVATE_DIRECTORIES
    )
    actions.extend(
        InitAction(f"master/{name}", action, "file", "0600", detail)
        for name in (
            master_runtime.README_NAME,
            master_runtime.LOCK_NAME,
            master_runtime.EVENTS_NAME,
            master_runtime.BOARD_NAME,
        )
    )
    return tuple(actions)


def plan_initialization(location: RuntimeLocation) -> InitPlan:
    root = location.record_root
    if not root.exists():
        return InitPlan(
            "PREVIEW",
            str(root),
            "create the standard private master runtime; rerun with --apply",
            _standard_actions("create", "absent; create only with --apply"),
        )
    try:
        info = root.lstat()
    except OSError as exc:
        return InitPlan("REFUSED", str(root), str(exc), _standard_actions("refuse", str(exc)))
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        detail = "existing master root must be an owner-only non-symlink directory"
        return InitPlan("REFUSED", str(root), detail, _standard_actions("refuse", detail))

    if (root / master_migrate.MIGRATION_REPORT_NAME).exists():
        migration = master_migrate.plan_migration(root)
        if migration.status == "FINALIZE":
            detail = "prepared legacy migration requires explicit `master init --migrate --apply`"
            return InitPlan(
                "MIGRATION_REQUIRED",
                str(root),
                detail,
                _standard_actions("refuse", "prepared migration is not yet authoritative"),
            )
        if migration.status != "CURRENT":
            detail = f"legacy migration is not verified: {migration.detail}"
            return InitPlan(
                "REFUSED",
                str(root),
                detail,
                _standard_actions("refuse", "migration evidence requires explicit recovery"),
            )

    core = [
        root / master_runtime.EVENTS_NAME,
        root / master_runtime.BOARD_NAME,
        root / master_runtime.LOCK_NAME,
    ]
    if all(path.exists() for path in core):
        try:
            master_runtime.read_record(root)
        except master_runtime.MasterRecordError as exc:
            detail = f"malformed current record refuses initialization: {exc}"
            return InitPlan("REFUSED", str(root), detail, _standard_actions("refuse", detail))
        return InitPlan(
            "CURRENT",
            str(root),
            "standard private master runtime is already current; no bytes change",
            _standard_actions("keep", "current private node"),
        )
    entries = list(root.iterdir())
    if not entries:
        return InitPlan(
            "PREVIEW",
            str(root),
            "populate the empty private master directory; rerun with --apply",
            _standard_actions("create", "empty private root; create only with --apply"),
        )
    if any(path.exists() for path in core):
        detail = "partial structured record is unsafe and is not a legacy migration input"
        return InitPlan("REFUSED", str(root), detail, _standard_actions("refuse", detail))
    detail = "legacy or unknown master record requires explicit `master init --migrate`"
    return InitPlan(
        "MIGRATION_REQUIRED",
        str(root),
        detail,
        _standard_actions("refuse", "retained unchanged until explicit migration"),
    )


def initialize(location: RuntimeLocation, *, apply: bool) -> InitPlan:
    plan = plan_initialization(location)
    if not apply or plan.status == "CURRENT":
        return plan
    if plan.status != "PREVIEW":
        return plan
    try:
        master_runtime.initialize_empty_record(location.record_root)
    except master_runtime.MasterRecordError as exc:
        detail = f"initialization refused without recovery or reset: {exc}"
        return InitPlan("REFUSED", str(location.record_root), detail, _standard_actions("refuse", detail))
    current = plan_initialization(location)
    if current.status != "CURRENT":
        raise MasterOperationError("initialized record did not validate as current")
    return InitPlan(
        "OK",
        current.record_root,
        "standard private master runtime created and validated",
        _standard_actions("created", "created by this apply transaction"),
    )


def _bounded(
    rows: Sequence[dict[str, Any]],
    limit: int,
    key: Union[str, Callable[[dict[str, Any]], str]],
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_DIGEST_LIMIT:
        raise MasterOperationError(f"digest limit must be 0..{MAX_DIGEST_LIMIT}")
    items = list(rows[:limit])
    omitted = max(0, len(rows) - len(items))
    continuation = None
    if omitted:
        first_omitted = rows[len(items)]
        continuation = key(first_omitted) if callable(key) else first_omitted[key]
    return {
        "items": items,
        "limit": limit,
        "omitted": omitted,
        "continuation_key": continuation,
    }


def _focused_page(
    rows: Sequence[dict[str, Any]],
    limit: int,
    *,
    key: str,
    after: Optional[str],
) -> dict[str, Any]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_DIGEST_LIMIT
    ):
        raise MasterOperationError(f"focused digest limit must be 1..{MAX_DIGEST_LIMIT}")
    start = 0
    if after is not None:
        _validate_lookup_id(after, "goal cursor")
        for index, row in enumerate(rows):
            if row[key] == after:
                start = index + 1
                break
        else:
            raise MasterOperationError(f"unknown goal cursor: {after}")
    items = list(rows[start:start + limit])
    omitted = max(0, len(rows) - start - len(items))
    return {
        "items": items,
        "limit": limit,
        "total": len(rows),
        "after": after,
        "omitted": omitted,
        # Focused pagination resumes after the last row actually shown.
        "continuation_key": items[-1][key] if omitted and items else None,
    }


def _validate_lookup_id(value: str, what: str) -> str:
    if not isinstance(value, str) or _DIGEST_IDENTIFIER.fullmatch(value) is None:
        raise MasterOperationError(f"{what} must be a valid master-record identifier")
    return value


def _record_metadata(view: master_runtime.RecordView) -> dict[str, Any]:
    rendered_board = master_runtime.render_board(view.events)
    return {
        "source": view.expected_board["source"],
        "board_current": view.board_current,
        "board_repair": {
            "required": not view.board_current,
            "rendered_sha256": hashlib.sha256(rendered_board).hexdigest(),
        },
    }


def _safe_lease(
    snapshot: Any,
    events: Sequence[dict[str, Any]],
    status_text: str,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != semaphore.MASTER_SCHEMA_VERSION:
        raise MasterOperationError("master lease summary is unavailable")
    lease = snapshot.get("lease")
    if lease is None:
        return {
            "present": False,
            "client": None,
            "state": "none",
            "matches_recorded_acquisition": False,
        }
    if not isinstance(lease, dict):
        raise MasterOperationError("master lease summary is malformed")
    actor = master_runtime._actor_from_snapshot(snapshot)
    recorded = next(
        (
            event["actor"]["acquisition_digest"]
            for event in reversed(events)
            if event["kind"] == "master"
            and event["payload"]["action"] in {"start", "resume"}
        ),
        None,
    )
    match = re.search(r"(?m)^master: [^ ]+ \((live|lapsed|stranded)\)", status_text)
    return {
        "present": True,
        "client": actor["client"],
        "state": match.group(1) if match else "unclassified",
        "matches_recorded_acquisition": recorded == actor["acquisition_digest"],
    }


def digest_record(
    root: Path,
    *,
    goals_limit: int = DEFAULT_DIGEST_LIMIT,
    decisions_limit: int = DEFAULT_DIGEST_LIMIT,
    findings_limit: int = DEFAULT_DIGEST_LIMIT,
    discrepancies_limit: int = DEFAULT_DIGEST_LIMIT,
    live_reconciliation: Optional[master_reconcile.ReconciliationResult] = None,
    lease_snapshot: Snapshot = semaphore.master_snapshot,
    lease_status: LeaseStatus = semaphore.status_text,
    _view: Optional[master_runtime.RecordView] = None,
) -> dict[str, Any]:
    view = _view if _view is not None else master_runtime.read_record(root)
    lease = _safe_lease(lease_snapshot(), view.events, lease_status())
    board = view.expected_board
    reconciliation: list[dict[str, Any]] = []
    for event in reversed(view.events):
        if event["kind"] == "master":
            reconciliation = list(event["payload"]["reconciliation"])
            break
    goals = [
        {
            key: row[key]
            for key in ("goal_id", "status", "branch", "checkpoint", "next_unit")
        }
        for row in board["goals"]
    ]
    decisions = [
        {
            key: row[key]
            for key in ("decision_id", "status", "title", "choice", "authority")
        }
        for row in board["open_decisions"]
    ]
    findings = [
        {
            key: row[key]
            for key in (
                "finding_id",
                "status",
                "severity",
                "summary",
                "audit_kind",
                "report",
            )
        }
        for row in board["open_audit_findings"]
    ]
    master = board["master"]
    recorded_role = (
        "none"
        if master is None
        else "ended"
        if master["action"] == "end"
        else "master"
    )
    digest = {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "status": "OK",
        "role": {"recorded": recorded_role, "authoritative": False},
        "lease": lease,
        "record": _record_metadata(view),
        "goals": _bounded(goals, goals_limit, "goal_id"),
        "open_decisions": _bounded(decisions, decisions_limit, "decision_id"),
        "open_audit_findings": _bounded(findings, findings_limit, "finding_id"),
        "reconciliation_discrepancies": _bounded(
            reconciliation,
            discrepancies_limit,
            lambda row: f"{row['repository']}:{row['kind']}:{row['subject']}",
        ),
        "last_durable_event": board["last_event"],
        "next_unit": board["next_unit"],
    }
    if live_reconciliation is not None:
        digest["live_reconciliation"] = {
            "schema_version": master_reconcile.RECONCILIATION_SCHEMA_VERSION,
            "repositories": list(live_reconciliation.repositories),
            "discrepancies": _bounded(
                live_reconciliation.discrepancies,
                discrepancies_limit,
                lambda row: f"{row['repository']}:{row['kind']}:{row['subject']}",
            ),
        }
    return digest


def focused_digest_record(
    root: Path,
    *,
    goals_limit: int = DEFAULT_DIGEST_LIMIT,
    goals_after: Optional[str] = None,
    decisions_limit: int = DEFAULT_DIGEST_LIMIT,
    findings_limit: int = DEFAULT_DIGEST_LIMIT,
    discrepancies_limit: int = DEFAULT_DIGEST_LIMIT,
    live_reconciliation: Optional[master_reconcile.ReconciliationResult] = None,
    lease_snapshot: Snapshot = semaphore.master_snapshot,
    lease_status: LeaseStatus = semaphore.status_text,
) -> dict[str, Any]:
    """Return a prioritized, paged continuity view without changing the record."""
    view = master_runtime.read_record(root)
    digest = digest_record(
        root,
        goals_limit=MAX_DIGEST_LIMIT,
        decisions_limit=decisions_limit,
        findings_limit=findings_limit,
        discrepancies_limit=discrepancies_limit,
        live_reconciliation=live_reconciliation,
        lease_snapshot=lease_snapshot,
        lease_status=lease_status,
        _view=view,
    )
    board = view.expected_board
    goals = [
        {
            key: row[key]
            for key in ("goal_id", "status", "branch", "checkpoint", "next_unit")
        }
        for row in board["goals"]
    ]
    goals.sort(key=lambda row: (_GOAL_PRIORITY[row["status"]], row["goal_id"]))
    digest["view"] = "focused"
    digest["goals"] = _focused_page(
        goals,
        goals_limit,
        key="goal_id",
        after=goals_after,
    )
    digest["open_decisions"]["all_ids"] = [
        row["decision_id"] for row in board["open_decisions"]
    ]
    digest["open_decisions"]["retrieve"] = (
        "python3 -m creme master digest --decision DECISION_ID"
    )
    digest["open_audit_findings"]["all_ids"] = [
        row["finding_id"] for row in board["open_audit_findings"]
    ]
    digest["open_audit_findings"]["retrieve"] = (
        "python3 -m creme master digest --finding FINDING_ID"
    )
    return digest


def lookup_digest_record(root: Path, *, kind: str, identifier: str) -> dict[str, Any]:
    """Return one complete validated projection row, selected by exact identifier."""
    plural = {
        "goal": ("goals", "goal_id"),
        "decision": ("open_decisions", "decision_id"),
        "finding": ("open_audit_findings", "finding_id"),
    }
    if kind not in plural:
        raise MasterOperationError(f"unsupported digest lookup kind: {kind}")
    _validate_lookup_id(identifier, f"{kind} ID")
    view = master_runtime.read_record(root)
    section, key = plural[kind]
    selected = next(
        (row for row in view.expected_board[section] if row[key] == identifier),
        None,
    )
    if selected is None:
        raise MasterOperationError(f"unknown {kind} ID: {identifier}")
    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "status": "OK",
        "view": "lookup",
        "record": _record_metadata(view),
        "lookup": {
            "kind": kind,
            "id": identifier,
            "item": dict(selected),
        },
    }


def _human_preview(value: Optional[str], route: str) -> str:
    if not value:
        return "<none>"
    single_line = " ".join(value.split())
    if len(single_line) <= HUMAN_PREVIEW_CHARS:
        return single_line
    prefix = single_line[:HUMAN_PREVIEW_CHARS - 1].rstrip()
    return f"{prefix}… [truncated; retrieve: {route}]"


def render_digest_human(digest: dict[str, Any]) -> str:
    if digest.get("view") == "focused":
        return render_focused_digest_human(digest)
    source = digest["record"]["source"]
    lease = digest["lease"]
    lines = [
        f"master digest schema {digest['schema_version']}",
        f"role: {digest['role']['recorded']} (descriptive, not authority)",
        f"lease: {lease['client'] if lease['present'] else 'none'} ({lease['state']})",
        f"events: {source['event_count']} ({source['log_digest']})",
        f"board repair required: {str(digest['record']['board_repair']['required']).lower()}",
    ]
    for name in ("goals", "open_decisions", "open_audit_findings", "reconciliation_discrepancies"):
        section = digest[name]
        lines.append(f"{name}: {len(section['items'])} shown, {section['omitted']} omitted")
    if "live_reconciliation" in digest:
        live = digest["live_reconciliation"]["discrepancies"]
        lines.append(
            f"live_reconciliation: {len(live['items'])} shown, {live['omitted']} omitted"
        )
    lines.append(f"next unit: {digest['next_unit'] or '<none>'}")
    return "\n".join(lines) + "\n"


def render_focused_digest_human(digest: dict[str, Any]) -> str:
    source = digest["record"]["source"]
    lease = digest["lease"]
    lines = [
        f"master continuity schema {digest['schema_version']}",
        f"role: {digest['role']['recorded']} (descriptive, not authority)",
        f"lease: {lease['client'] if lease['present'] else 'none'} ({lease['state']})",
        f"events: {source['event_count']} ({source['log_digest']})",
        f"board repair required: {str(digest['record']['board_repair']['required']).lower()}",
        "",
        "goals (active, ready, and blocked first):",
    ]
    goals = digest["goals"]
    for row in goals["items"]:
        route = f"python3 -m creme master digest --goal {row['goal_id']}"
        lines.extend([
            f"- {row['goal_id']} [{row['status']}]",
            f"  branch: {_human_preview(row['branch'], route)}",
            f"  checkpoint: {_human_preview(row['checkpoint'], route)}",
            f"  next unit: {_human_preview(row['next_unit'], route)}",
            f"  retrieve: {route}",
        ])
    if not goals["items"]:
        lines.append("- <none>")
    if goals["omitted"]:
        cursor = goals["continuation_key"]
        lines.append(
            f"more goals: {goals['omitted']}; continue after last shown ({cursor}): "
            f"python3 -m creme master digest --focused --after-goal {cursor} --human"
        )

    decisions = digest["open_decisions"]
    lines.extend([
        "",
        f"open decision IDs ({len(decisions['all_ids'])}): "
        + (", ".join(decisions["all_ids"]) or "<none>"),
        f"retrieve any decision: {decisions['retrieve']}",
    ])
    for row in decisions["items"]:
        route = f"python3 -m creme master digest --decision {row['decision_id']}"
        lines.append(
            f"- {row['decision_id']} [{row['authority']}]: "
            f"{_human_preview(row['title'], route)}"
        )
    if decisions["omitted"]:
        lines.append(
            f"decision prose previews omitted: {decisions['omitted']}; all IDs remain listed above"
        )

    findings = digest["open_audit_findings"]
    lines.extend([
        "",
        f"open finding IDs ({len(findings['all_ids'])}): "
        + (", ".join(findings["all_ids"]) or "<none>"),
        f"retrieve any finding: {findings['retrieve']}",
    ])
    for row in findings["items"]:
        route = f"python3 -m creme master digest --finding {row['finding_id']}"
        lines.append(
            f"- {row['finding_id']} [{row['severity']}, {row['status']}]: "
            f"{_human_preview(row['summary'], route)}"
        )
    if findings["omitted"]:
        lines.append(
            f"finding prose previews omitted: {findings['omitted']}; all IDs remain listed above"
        )

    discrepancies = digest["reconciliation_discrepancies"]
    lines.extend([
        "",
        "recorded reconciliation discrepancies: "
        f"{len(discrepancies['items']) + discrepancies['omitted']}; retrieve: "
        "python3 -m creme master digest",
    ])
    if "live_reconciliation" in digest:
        live = digest["live_reconciliation"]["discrepancies"]
        lines.append(
            "live reconciliation discrepancies: "
            f"{len(live['items']) + live['omitted']}; retrieve: "
            "python3 -m creme master digest --reconcile"
        )
    lines.append(
        "next unit: "
        + _human_preview(digest["next_unit"], "python3 -m creme master digest")
    )
    return "\n".join(lines) + "\n"


def _holder(snapshot: Any, state: str) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("lease"), dict):
        return {"client": "unknown", "state": state}
    client = snapshot["lease"].get("client")
    if not isinstance(client, str) or semaphore.CLIENT_LABEL.fullmatch(client) is None:
        client = "unknown"
    return {"client": client, "state": state}


def _refusal_state(detail: str) -> str:
    folded = detail.casefold()
    if "live" in folded:
        return "reader"
    if "lapsed" in folded or "stranded" in folded:
        return "takeover-required"
    return "unavailable"


def start_master(
    root: Path,
    *,
    client: str,
    model: str,
    effort: str,
    note: str,
    take_over: bool = False,
    reconciliation: Optional[master_reconcile.ReconciliationResult] = None,
    acquire: Acquire = semaphore.master_acquire,
    renew: Renew = semaphore.master_renew,
    release: Release = semaphore.master_release,
    heartbeat: Heartbeat = semaphore.master_heartbeat_detached,
    lease_snapshot: Snapshot = semaphore.master_snapshot,
    lease_status: LeaseStatus = semaphore.status_text,
    authority_transaction: Optional[master_runtime.AuthorityTransaction] = None,
) -> dict[str, Any]:
    if not isinstance(client, str) or semaphore.CLIENT_LABEL.fullmatch(client) is None:
        raise MasterOperationError("client must be a short client label")
    reconciliation_rows = [] if reconciliation is None else list(reconciliation.discrepancies)
    master_runtime.validate_payload("master", {
        "action": "start",
        "model": model,
        "effort": effort,
        "note": note,
        "next_unit": "",
        "reconciliation": reconciliation_rows,
    })
    # Record validation happens before any lease operation.
    record_before = master_runtime.read_record(root)
    next_unit = record_before.expected_board["next_unit"] or ""
    try:
        before = lease_snapshot()
    except Exception as exc:
        raise MasterOperationError(f"master lease state is unavailable: {exc}") from exc
    existing = isinstance(before, dict) and before.get("lease") is not None
    acquired_now = False
    if existing:
        try:
            renewed, renewal_detail = renew()
        except Exception as exc:
            renewed, renewal_detail = False, f"renewal failed: {exc}"
        if renewed:
            mode = "resumed"
        else:
            try:
                acquired, acquire_detail = acquire(
                    client, note, take_over=take_over
                )
            except Exception as exc:
                raise MasterOperationError(f"master acquisition failed: {exc}") from exc
            if not acquired:
                state = _refusal_state(acquire_detail or renewal_detail)
                try:
                    holder = _holder(lease_snapshot(), "live" if state == "reader" else state)
                except Exception:
                    holder = {"client": "unknown", "state": state}
                result = {"status": state, "holder": holder}
                if reconciliation is not None:
                    result["reconciliation"] = reconciliation.to_dict()
                return result
            acquired_now = True
            mode = "taken-over" if take_over else "acquired"
    else:
        try:
            acquired, acquire_detail = acquire(client, note, take_over=take_over)
        except Exception as exc:
            raise MasterOperationError(f"master acquisition failed: {exc}") from exc
        if not acquired:
            state = _refusal_state(acquire_detail)
            try:
                holder = _holder(lease_snapshot(), "live" if state == "reader" else state)
            except Exception:
                holder = {"client": "unknown", "state": state}
            result = {"status": state, "holder": holder}
            if reconciliation is not None:
                result["reconciliation"] = reconciliation.to_dict()
            return result
        acquired_now = True
        mode = "acquired"

    payload = {
        "action": "resume" if mode == "resumed" else "start",
        "model": model,
        "effort": effort,
        "note": note,
        "next_unit": next_unit,
        "reconciliation": reconciliation_rows,
    }
    writer = master_runtime.RecordWriter(
        root,
        renew=renew,
        lease_snapshot=lease_snapshot,
        authority_transaction=authority_transaction,
    )
    try:
        acquisition_digest = master_runtime._actor_from_snapshot(
            lease_snapshot()
        )["acquisition_digest"]
    except Exception as exc:
        raise MasterOperationError(
            f"renewed acquisition binding is unavailable: {exc}"
        ) from exc
    try:
        recorded = writer.append(
            "master", payload, once_per_acquisition=True
        )
    except Exception:
        if acquired_now:
            try:
                current = master_runtime.read_record(root)
                committed = any(
                    event["kind"] == "master"
                    and event["payload"]["action"] in {"start", "resume"}
                    and event["actor"]["acquisition_digest"] == acquisition_digest
                    for event in current.events
                )
            except Exception:
                committed = True
            if not committed:
                try:
                    release()
                except Exception:
                    pass
        raise
    try:
        heartbeat_ok, heartbeat_detail = heartbeat(1500)
    except Exception as exc:
        heartbeat_ok, heartbeat_detail = False, f"heartbeat start failed: {exc}"
    if not heartbeat_ok:
        raise MasterOperationError(
            f"master event is durable but heartbeat is unavailable; retry start: {heartbeat_detail}"
        )
    digest = digest_record(
        root,
        live_reconciliation=reconciliation,
        lease_snapshot=lease_snapshot,
        lease_status=lease_status,
    )
    return {
        "status": "master",
        "mode": mode,
        "event": {
            "event_id": recorded.event["event_id"],
            "timestamp": recorded.event["timestamp"],
            "already_present": recorded.already_present,
            "board_repaired": recorded.repaired_stale_board,
        },
        "digest": digest,
    }
