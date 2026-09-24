from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .adapters import get_adapter
from . import antigravity
from .doctor import exit_code as doctor_exit_code
from .doctor import run_doctor
from .guidance import default_path as default_guidance_path
from .guidance import load as load_guidance
from .host_wrappers import (
    BROKER_NAME,
    WORKFLOW_BROKER_NAME,
    RULES_FILENAME,
    default_output_dir as default_host_wrapper_output_dir,
    default_rules_dir as default_host_rules_dir,
    install_host_bundle,
    render_host_rules,
    render_host_wrappers,
)
from .profile import DEFAULT_RELATIVE_PROFILE, load, propose, write_reviewed
from . import idle_workers
from . import luna_broker, luna_reserve
from . import model_fit
from . import master_operations
from . import master_reconcile
from . import master_retire
from . import master_runtime
from . import semaphore
from .task_wind_down import WorktreeScopeError, _goal_worktree_roots, wind_down
from .build_ownership import (
    DEFAULT_MEMORY_GIB,
    DEFAULT_THREADS,
    guarded_mcp_env,
    ledger_rollup,
    run_lake_build,
    trusted_uvx,
)


ROOT = Path(__file__).resolve().parents[1]


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _nonnegative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return value


def _task_memory(text: str) -> int:
    value = _positive(text)
    if value > 8:
        raise argparse.ArgumentTypeError("must be between 1 and 8 GiB")
    return value


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _path_has_text(path: Path, expected: str) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.read_text(
            encoding="utf-8",
        ) == expected
    except (OSError, UnicodeDecodeError):
        return False


def _toml_string(value: object) -> str:
    """Render a TOML basic string using its JSON-compatible escape subset."""
    return json.dumps(str(value), ensure_ascii=False)


def _profile_path(text: Optional[str]) -> Path:
    return (
        Path(text).expanduser().resolve()
        if text
        else semaphore.canonical_creme_root(ROOT) / DEFAULT_RELATIVE_PROFILE
    )


def cmd_platform(arguments: argparse.Namespace) -> int:
    adapter = get_adapter()
    facts = adapter.static_facts()
    identity = adapter.platform_identity()
    _json({
        "adapter": adapter.system,
        "status": facts.status,
        "detail": facts.detail,
        "facts": facts.data,
        "platform_identity": identity.to_dict(),
        "optional_capabilities": list(adapter.optional_capabilities),
    })
    return 0 if facts.status == "OK" and identity.status == "OK" else 1


def cmd_python_runtime(arguments: argparse.Namespace) -> int:
    result = get_adapter().python_runtime(arguments.version)
    _json(result.to_dict())
    return 0 if result.status == "OK" else 2


def cmd_init(arguments: argparse.Namespace) -> int:
    adapter = get_adapter()
    try:
        candidate = propose(
            ROOT,
            Path(arguments.workspace_root).expanduser() if arguments.workspace_root else None,
            adapter,
            goal_store=arguments.goal_store,
        )
    except RuntimeError as exc:
        _json({"status": "UNAVAILABLE", "detail": str(exc)})
        return 1
    path = _profile_path(arguments.profile)
    if not arguments.write:
        _json({
            "status": "PREVIEW",
            "detail": "review this profile, then rerun with --write",
            "path": str(path),
            "profile": candidate,
        })
        return 0
    if path.exists() and not arguments.replace:
        _json({"status": "REFUSED", "detail": f"profile exists: {path}; use --replace after review"})
        return 1
    write_reviewed(path, candidate)
    _json({"status": "OK", "detail": "reviewed host profile written", "path": str(path)})
    return 0


def cmd_validate_profile(arguments: argparse.Namespace) -> int:
    checked = load(_profile_path(arguments.profile), get_adapter())
    _json({"status": checked.status, "detail": checked.detail, "profile": checked.profile})
    return 0 if checked.status in {"VALID", "LIMITED"} else 1


def cmd_host_guidance(arguments: argparse.Namespace) -> int:
    path = default_guidance_path(ROOT)
    checked = load_guidance(path)
    _json({
        "status": checked.status,
        "detail": checked.detail,
        "path": str(path),
        "guidance": checked.content,
    })
    return 0 if checked.status in {"OK", "MISSING"} else 1


def _luna_policy(arguments: argparse.Namespace) -> luna_reserve.Policy:
    return luna_reserve.Policy(
        min_remaining_percent=arguments.min_remaining_percent,
        jitter_seconds=arguments.jitter_seconds,
        discrimination_seconds=arguments.discrimination_seconds,
        allow_regular_available=getattr(arguments, "allow_regular_available", False),
    )


def cmd_luna_reserve_status(arguments: argparse.Namespace) -> int:
    code, report = luna_reserve.status(_luna_policy(arguments), module_root=ROOT)
    if arguments.json:
        _json(report)
    else:
        print(luna_reserve.format_status(report))
    return code


def cmd_antigravity_status(arguments: argparse.Namespace) -> int:
    try:
        quota = antigravity.read_quota(antigravity.resolve_binary())
    except antigravity.AntigravityError as exc:
        report = {"verdict": "FAILED", "exit": antigravity.EXIT_FAILED, "reasons": [str(exc)]}
        if arguments.json:
            _json(report)
        else:
            print(f"verdict=FAILED exit={antigravity.EXIT_FAILED}")
            print(f"reason: {exc}")
        return antigravity.EXIT_FAILED
    report = {
        "pools": quota["pools"],
        "remaining_credits": quota["remaining_credits"],
        "useG1Credits": quota["use_g1_credits"],
    }
    reasons = []
    if arguments.model:
        try:
            reasons = antigravity.admission(quota, arguments.model, antigravity.DEFAULT_MIN_REMAINING_FRACTION)
        except ValueError as exc:
            reasons = [str(exc)]
        report["admission"] = "ADMITTED" if not reasons else "REFUSED"
        report["admission_reasons"] = reasons
    if arguments.json:
        _json(report)
    else:
        print(f"quota={json.dumps(report['pools'], sort_keys=True)}")
        print(f"credits={report['remaining_credits']} useG1Credits={report['useG1Credits']}")
        if arguments.model:
            print(f"admission={report['admission']}")
            for reason in reasons:
                print(f"refused: {reason}")
    return antigravity.EXIT_PREFLIGHT_REFUSED if reasons else antigravity.EXIT_OK


def cmd_antigravity_run(arguments: argparse.Namespace) -> int:
    if arguments.brief == "-":
        brief = sys.stdin.read()
    else:
        try:
            brief = Path(arguments.brief).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            summary = {
                "verdict": "REFUSED", "exit": antigravity.EXIT_USAGE,
                "run": "-", "reasons": [f"cannot read brief: {exc}"],
            }
            if arguments.json:
                _json(summary)
            else:
                print(antigravity.format_summary(summary))
            return antigravity.EXIT_USAGE
    code, summary = antigravity.run(
        brief=brief,
        target=arguments.target,
        model=arguments.model,
        effort=arguments.effort,
        timeout_seconds=arguments.timeout_seconds,
    )
    if arguments.json:
        _json(summary)
    else:
        print(antigravity.format_summary(summary))
    return code


def cmd_luna_reserve_run(arguments: argparse.Namespace) -> int:
    overrides = list(arguments.overrides or [])
    if arguments.brief == "-":
        brief = sys.stdin.read()
    else:
        try:
            brief = Path(arguments.brief).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"verdict=REFUSED exit={luna_reserve.EXIT_PREFLIGHT_REFUSED}\nrefused: cannot read brief: {exc}")
            return luna_reserve.EXIT_PREFLIGHT_REFUSED
    request = luna_reserve.RunRequest(
        brief=brief,
        workdir=Path(arguments.target).expanduser(),
        effort=arguments.effort,
        write=arguments.write,
        timeout_seconds=arguments.timeout_seconds,
        policy=_luna_policy(arguments),
        overrides=overrides,
        preflight_only=arguments.preflight_only,
    )
    code, summary = luna_reserve.run(ROOT, request)
    if arguments.json:
        _json(summary)
    else:
        print(luna_reserve.format_run(summary))
    if code == luna_reserve.EXIT_ATTRIBUTION_FAILED:
        print(luna_reserve.STOP_MESSAGE, file=sys.stderr)
    return code


def cmd_luna_reserve_audit(arguments: argparse.Namespace) -> int:
    code, result = luna_reserve.audit_target(ROOT, arguments.target, _luna_policy(arguments))
    if arguments.json:
        _json(result)
    else:
        print(
            f"verdict={result.get('verdict')} turns={result.get('turns')} "
            f"models={result.get('turn_models')} snapshots={result.get('attributed_snapshots')}/"
            f"{result.get('token_snapshots')} rollout={result.get('rollout')}"
        )
        print(f"reference={result.get('reference_source')}")
        for line in (result.get("failures") or []) + (result.get("refusals") or []):
            print(f"  {line}")
    if code == luna_reserve.EXIT_ATTRIBUTION_FAILED:
        print(luna_reserve.STOP_MESSAGE, file=sys.stderr)
    return code


def _read_brief(value: str) -> tuple[Optional[str], Optional[str]]:
    if value == "-":
        return sys.stdin.read(), None
    try:
        return Path(value).expanduser().read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read brief: {exc}"


def _luna_print(arguments: argparse.Namespace, code: int, lines: list[str], record: dict) -> int:
    if getattr(arguments, "json", False):
        _json({"exit_code": code, **(record or {})})
    else:
        for line in lines:
            print(line)
    if code == luna_reserve.EXIT_ATTRIBUTION_FAILED:
        print(luna_reserve.STOP_MESSAGE, file=sys.stderr)
    return code


def _luna_refused(arguments: argparse.Namespace, reason: str) -> int:
    return _luna_print(arguments, luna_reserve.EXIT_PREFLIGHT_REFUSED,
                       [f"verdict=REFUSED exit={luna_reserve.EXIT_PREFLIGHT_REFUSED}", f"refused: {reason}"], {})


def cmd_luna_reserve_start(arguments: argparse.Namespace) -> int:
    brief, error = _read_brief(arguments.brief)
    if error:
        return _luna_refused(arguments, error)
    code, lines, record = luna_broker.cmd_start(
        ROOT, dict(os.environ), brief, arguments.target, arguments.write, arguments.effort, arguments.detail,
        _luna_policy(arguments).__dict__, list(arguments.overrides or []), arguments.timeout_seconds,
        lean=arguments.lean,
    )
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_resume(arguments: argparse.Namespace) -> int:
    code, lines, record = luna_broker.cmd_resume(
        ROOT, dict(os.environ), arguments.thread, arguments.target, arguments.write, arguments.effort,
        arguments.detail, _luna_policy(arguments).__dict__, list(arguments.overrides or []),
        arguments.timeout_seconds, lean=arguments.lean,
    )
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_send(arguments: argparse.Namespace) -> int:
    if arguments.text is not None:
        text = arguments.text
    else:
        text, error = _read_brief(arguments.brief or "-")
        if error:
            return _luna_refused(arguments, error)
    steer = arguments.luna_action == "steer"
    code, lines, record = luna_broker.cmd_simple(ROOT, dict(os.environ), "send", arguments.session,
                                                  text=text, steer=steer)
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_session_op(arguments: argparse.Namespace) -> int:
    extra = {}
    if arguments.luna_action == "approve":
        extra = {"approval": arguments.approval, "decision": arguments.decision}
    code, lines, record = luna_broker.cmd_simple(ROOT, dict(os.environ), arguments.luna_action,
                                                  arguments.session, **extra)
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_detail(arguments: argparse.Namespace) -> int:
    code, lines, record = luna_broker.cmd_detail(ROOT, dict(os.environ), arguments.session, arguments.level)
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_wait(arguments: argparse.Namespace) -> int:
    code, lines, record = luna_broker.cmd_wait(ROOT, dict(os.environ), arguments.session, arguments.timeout)
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_approve_builds(arguments: argparse.Namespace) -> int:
    header_files = [filename for group in (arguments.header_file or []) for filename in group]
    allowed = [name for group in (arguments.allow_removed or []) for name in group]
    code, lines, record = luna_broker.cmd_approve_builds(
        ROOT, dict(os.environ), arguments.session, arguments.header_base, header_files, allowed, arguments.timeout,
    )
    for line in lines:
        print(line)
    return code


def cmd_luna_reserve_events(arguments: argparse.Namespace) -> int:
    def emit(line: str) -> None:
        print(line, flush=True)

    return luna_broker.cmd_events(ROOT, dict(os.environ), arguments.session, arguments.follow, arguments.last,
                                  arguments.since, emit, timeout=arguments.timeout)


def cmd_luna_reserve_read(arguments: argparse.Namespace) -> int:
    code, lines, record = luna_broker.cmd_read(ROOT, dict(os.environ), arguments.session, arguments.items,
                                               arguments.lines)
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_list(arguments: argparse.Namespace) -> int:
    code, lines, record = luna_broker.cmd_list(ROOT, dict(os.environ), arguments.limit)
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_shutdown(arguments: argparse.Namespace) -> int:
    code, lines, record = luna_broker.cmd_shutdown(ROOT, dict(os.environ))
    return _luna_print(arguments, code, lines, record)


def cmd_luna_reserve_broker_serve(arguments: argparse.Namespace) -> int:
    return luna_broker.serve_main(ROOT, arguments.instance)


def _add_luna_refused_overrides(item: argparse.ArgumentParser) -> None:
    for flag in luna_reserve.FORBIDDEN_OVERRIDE_FLAGS:
        item.add_argument(flag, dest="overrides", nargs="?", action=_RefusedOverride, help=argparse.SUPPRESS)
    item.set_defaults(overrides=[], collects_extra_overrides=True)


class _RefusedOverride(argparse.Action):
    """Record a forbidden Codex override so the run refuses it loudly."""

    def __call__(self, parser, namespace, values, option_string=None):
        recorded = list(getattr(namespace, self.dest, None) or [])
        recorded.append(option_string if values is None else f"{option_string} {values}")
        setattr(namespace, self.dest, recorded)


def _add_luna_policy_arguments(item: argparse.ArgumentParser) -> None:
    item.add_argument("--json", action="store_true", help="print the full JSON record")
    item.add_argument(
        "--min-remaining-percent", type=float,
        default=luna_reserve.DEFAULT_MIN_REMAINING_PERCENT,
        help="refuse below this remaining reserve share",
    )
    item.add_argument(
        "--jitter-seconds", type=_positive, default=luna_reserve.DEFAULT_JITTER_SECONDS,
        help="reset-time tolerance when attributing a token snapshot",
    )
    item.add_argument(
        "--discrimination-seconds", type=_positive,
        default=luna_reserve.DEFAULT_DISCRIMINATION_SECONDS,
        help="minimum distance between reserve and regular reset times",
    )


def cmd_doctor(arguments: argparse.Namespace) -> int:
    checks, context = run_doctor(
        ROOT,
        Path.cwd().resolve(),
        _profile_path(arguments.profile),
        Path(arguments.workspace_root).expanduser().resolve() if arguments.workspace_root else None,
        get_adapter(),
        {
            "task_memory_gib": arguments.task_memory_gib,
            "heavy_workers": arguments.heavy_workers,
            "light_workers": arguments.light_workers,
        },
    )
    if arguments.json:
        _json({"context": context, "checks": [check.to_dict() for check in checks]})
    else:
        for check in checks:
            print(f"{check.status.upper():4} {check.name}: {check.detail}")
        print(json.dumps({"context": context}, sort_keys=True))
    return doctor_exit_code(checks)


def cmd_telemetry(arguments: argparse.Namespace) -> int:
    result = get_adapter().telemetry()
    _json(result.to_dict())
    return 0 if result.status == "OK" else 2


def cmd_memory_headroom(arguments: argparse.Namespace) -> int:
    result = get_adapter().memory_headroom()
    _json(result.to_dict())
    return 0 if result.status == "OK" else 2


def _master_location() -> tuple[Optional[master_operations.RuntimeLocation], Optional[str]]:
    try:
        return master_operations.resolve_runtime_location(ROOT), None
    except master_operations.MasterOperationError as exc:
        return None, str(exc)


def _master_record_status(
    location: master_operations.RuntimeLocation,
) -> tuple[Optional[str], master_operations.InitPlan]:
    plan = master_operations.plan_initialization(location)
    if plan.status == "CURRENT":
        return None, plan
    if plan.status == "MIGRATION_REQUIRED":
        return "migration-required", plan
    if plan.status == "PREVIEW":
        return "init-required", plan
    return "unavailable", plan


def cmd_master_init(arguments: argparse.Namespace) -> int:
    location, error = _master_location()
    if location is None:
        _json({"status": "unavailable", "detail": error})
        return 2
    plan = master_operations.initialize(location, apply=arguments.apply)
    _json(plan.to_dict())
    return 0 if plan.status in {"PREVIEW", "CURRENT", "OK"} else 2


def cmd_master_start(arguments: argparse.Namespace) -> int:
    location, error = _master_location()
    if location is None:
        _json({"status": "unavailable", "detail": error})
        return 2
    status, plan = _master_record_status(location)
    if status is not None:
        _json({"status": status, "detail": plan.detail})
        return 2
    try:
        reconciliation = master_operations.reconcile_location(location)
        result = master_operations.start_master(
            location.record_root,
            client=arguments.client,
            model=arguments.model,
            effort=arguments.effort,
            note=arguments.note,
            take_over=arguments.take_over,
            reconciliation=reconciliation,
        )
    except (
        master_operations.MasterOperationError,
        master_reconcile.ReconciliationError,
        master_runtime.MasterRecordError,
    ) as exc:
        _json({"status": "unavailable", "detail": str(exc)})
        return 2
    _json(result)
    return 0 if result["status"] in {"master", "reader", "takeover-required"} else 2


def _read_event_source(source: str) -> bytes:
    if source == "-":
        data = sys.stdin.buffer.read(master_runtime.MAX_EVENT_BYTES + 1)
    else:
        path = Path(source).expanduser()
        try:
            if path.stat().st_size > master_runtime.MAX_EVENT_BYTES:
                raise master_runtime.MasterRecordError(
                    f"event input exceeds {master_runtime.MAX_EVENT_BYTES} bytes"
                )
            data = path.read_bytes()
        except OSError as exc:
            raise master_runtime.MasterRecordError(f"event input could not be read: {exc}") from exc
    if len(data) > master_runtime.MAX_EVENT_BYTES:
        raise master_runtime.MasterRecordError(
            f"event input exceeds {master_runtime.MAX_EVENT_BYTES} bytes"
        )
    return data


def cmd_master_retire_migration(arguments: argparse.Namespace) -> int:
    location, error = _master_location()
    if location is None:
        _json({"status": "unavailable", "detail": error})
        return 2
    archive_parent = location.goal_store / master_retire.ARCHIVE_ROOT_NAME
    plan = master_retire.retire(
        location.record_root,
        archive_parent,
        apply=arguments.apply,
        privacy=master_retire.goal_store_privacy(location.goal_store),
    )
    _json(plan.to_dict())
    return 0 if plan.status in {"PREVIEW", "FINALIZE", "CURRENT", "OK"} else 2


def cmd_master_restore_migration(arguments: argparse.Namespace) -> int:
    location, error = _master_location()
    if location is None:
        _json({"status": "unavailable", "detail": error})
        return 2
    archive_parent = location.goal_store / master_retire.ARCHIVE_ROOT_NAME
    archive = archive_parent / arguments.archive
    if Path(arguments.archive).name != arguments.archive:
        _json({"status": "REFUSED", "detail": "name an archive directory under master-archive/"})
        return 2
    plan = master_retire.restore(location.record_root, archive, apply=arguments.apply)
    _json(plan.to_dict())
    return 0 if plan.status in {"PREVIEW", "OK"} else 2


def cmd_master_event(arguments: argparse.Namespace) -> int:
    location, error = _master_location()
    if location is None:
        _json({"status": "unavailable", "detail": error})
        return 2
    writer = master_runtime.RecordWriter(location.record_root)
    normalized: tuple[master_runtime.ModeChange, ...] = ()
    try:
        # The authenticated writer tightens owned modes (for example a brief
        # written 0644 by a client tool) before the read-side preflight, which
        # would otherwise refuse the record.  A nonholder is refused here.
        if location.record_root.is_dir() and master_runtime.mode_violations(
            location.record_root
        ):
            normalized = writer.normalize_modes()
    except master_runtime.MasterRecordError as exc:
        _json({"status": "refused", "detail": str(exc)})
        return 2
    status, plan = _master_record_status(location)
    if status is not None:
        _json({
            "status": status,
            "detail": plan.detail,
            "modes_normalized": [change.to_dict() for change in normalized],
        })
        return 2
    try:
        kind, payload = master_runtime.parse_event_input(_read_event_source(arguments.source))
        result = writer.append(kind, payload)
    except master_runtime.MasterRecordError as exc:
        _json({
            "status": "refused",
            "detail": str(exc),
            "modes_normalized": [change.to_dict() for change in normalized],
        })
        return 2
    normalized = (*normalized, *result.normalized_modes)
    _json({
        "status": "OK",
        "event": {
            "event_id": result.event["event_id"],
            "kind": result.event["kind"],
            "timestamp": result.event["timestamp"],
        },
        "source": result.board["source"],
        "board_repaired": result.repaired_stale_board,
        "modes_normalized": [change.to_dict() for change in normalized],
    })
    return 0


def cmd_master_digest(arguments: argparse.Namespace) -> int:
    location, error = _master_location()
    if location is None:
        _json({"status": "unavailable", "detail": error})
        return 2
    status, plan = _master_record_status(location)
    if status is not None:
        _json({"schema_version": 1, "status": status, "detail": plan.detail})
        return 2
    try:
        lookups = [
            (kind, identifier)
            for kind, identifier in (
                ("goal", arguments.goal),
                ("decision", arguments.decision),
                ("finding", arguments.finding),
            )
            if identifier is not None
        ]
        if arguments.after_goal is not None and not arguments.focused:
            raise master_operations.MasterOperationError(
                "--after-goal requires --focused"
            )
        if lookups:
            if (
                arguments.focused
                or arguments.after_goal is not None
                or arguments.reconcile
                or arguments.human
                or arguments.goals_limit is not None
                or arguments.decisions_limit is not None
                or arguments.findings_limit is not None
                or arguments.discrepancies_limit is not None
            ):
                raise master_operations.MasterOperationError(
                    "exact lookup cannot be combined with focused, pagination, human, "
                    "reconciliation, or limit options"
                )
            kind, identifier = lookups[0]
            _json(master_operations.lookup_digest_record(
                location.record_root,
                kind=kind,
                identifier=identifier,
            ))
            return 0
        reconciliation = (
            master_operations.reconcile_location(location)
            if arguments.reconcile
            else None
        )
        digest_function = (
            master_operations.focused_digest_record
            if arguments.focused
            else master_operations.digest_record
        )
        digest_options = {
            "goals_limit": (
                master_operations.DEFAULT_DIGEST_LIMIT
                if arguments.goals_limit is None else arguments.goals_limit
            ),
            "decisions_limit": (
                master_operations.DEFAULT_DIGEST_LIMIT
                if arguments.decisions_limit is None else arguments.decisions_limit
            ),
            "findings_limit": (
                master_operations.DEFAULT_DIGEST_LIMIT
                if arguments.findings_limit is None else arguments.findings_limit
            ),
            "discrepancies_limit": (
                master_operations.DEFAULT_DIGEST_LIMIT
                if arguments.discrepancies_limit is None
                else arguments.discrepancies_limit
            ),
            "live_reconciliation": reconciliation,
        }
        if arguments.focused:
            digest_options["goals_after"] = arguments.after_goal
        digest = digest_function(location.record_root, **digest_options)
    except (
        master_operations.MasterOperationError,
        master_reconcile.ReconciliationError,
        master_runtime.MasterRecordError,
    ) as exc:
        _json({"schema_version": 1, "status": "unavailable", "detail": str(exc)})
        return 2
    if arguments.human:
        print(master_operations.render_digest_human(digest), end="")
    else:
        _json(digest)
    return 0


def cmd_tempdir(arguments: argparse.Namespace) -> int:
    adapter = get_adapter()
    root = adapter.temp_root()
    if root.status != "OK" or not root.data:
        _json(root.to_dict())
        return 2
    if not arguments.create:
        _json({**root.to_dict(), "detail": "temporary-root preview; use --create to allocate"})
        return 0
    created = tempfile.mkdtemp(prefix=arguments.prefix, dir=root.data["path"])
    _json({"capability": "temporary_directory", "status": "OK", "adapter": adapter.system, "path": created})
    return 0


def cmd_cache_copy(arguments: argparse.Namespace) -> int:
    result = get_adapter().copy_cache(
        Path(arguments.source).expanduser().resolve(),
        Path(arguments.destination).expanduser().resolve(),
        arguments.execute,
    )
    _json(result.to_dict())
    return 0 if result.status in {"OK", "PREVIEW"} else 1


def cmd_idle_workers(arguments: argparse.Namespace) -> int:
    """Reclaim the caller's own idle Lean workers; report everyone else's.

    Ownership is the goal worktree a worker is working in.  Under the master
    model every worker on the host is a subagent of one client process, so
    the client ancestry that reclamation trusts names everyone at once; a
    caller therefore names its goal with ``--goal`` and reclaims only inside
    that goal's worktrees.  Without ``--goal`` a worker inside any goal
    worktree is reported to its goal, never signalled.
    """
    adapter = get_adapter()
    minimum_seconds = arguments.idle_workers * 60
    goal = getattr(arguments, "goal", None)
    scope_options: list[str] = []
    if goal is not None:
        try:
            roots = _goal_worktree_roots(goal, adapter)
        except (OSError, WorktreeScopeError) as exc:
            _json({
                "capability": "idle_workers",
                "status": "REFUSED",
                "detail": f"goal-scoped worker ownership could not be established: {exc}",
            })
            return 2
        for root in roots:
            scope_options.extend(("--scope-root", str(root)))
    signals = semaphore.refresh_signals(adapter)
    report = signals["lean_workers"]
    if report["status"] != "OK":
        _json({
            "capability": "idle_workers",
            "status": report["status"],
            "detail": f"Lean worker sampling unavailable: {report['detail']}",
        })
        return 2
    ownership = adapter.reclaim(["--dry-run", *scope_options])
    if ownership.status != "OK" or not isinstance(ownership.data, dict):
        _json({
            "capability": "idle_workers",
            "status": ownership.status,
            "detail": f"ownership boundary unavailable: {ownership.detail}",
        })
        return 2
    owned = {int(row["pid"]) for row in ownership.data.get("owned") or []}
    eligible = [
        worker for worker in report["idle_workers"]
        if (worker["idle_seconds"] or 0) >= minimum_seconds
    ]

    def foreign_reason(worker: dict) -> Optional[str]:
        """Why this caller may not signal the worker, or None when it may."""
        worker_goal = idle_workers.goal_of_directory(worker.get("cwd"))
        if goal is None and worker_goal is not None:
            return f"working in goal {worker_goal}'s worktree; name it with --goal"
        if goal is not None and worker_goal is not None and worker_goal != goal:
            return f"working in goal {worker_goal}'s worktree, not {goal}'s"
        if worker["pid"] not in owned:
            return (
                "outside every worktree of the named goal"
                if goal is not None and worker_goal is None
                else "outside the caller's ownership boundary"
            )
        if goal is None and worker.get("cwd") is None:
            return "working directory unreadable; a goal worktree cannot be excluded"
        return None

    reasons = {worker["pid"]: foreign_reason(worker) for worker in eligible}
    targets = sorted(worker["pid"] for worker in eligible if reasons[worker["pid"]] is None)
    foreign = [worker for worker in eligible if reasons[worker["pid"]] is not None]
    observed = {
        "capability": "idle_workers",
        "minimum_idle_minutes": arguments.idle_workers,
        "goal": goal,
        "sampled_workers": len(report["workers"]),
        "idle_workers": eligible,
        "owned_targets": targets,
        "reported_not_owned": [
            {
                "pid": worker["pid"],
                "rss_gib": worker["rss_gib"],
                "idle_seconds": round(worker["idle_seconds"] or 0.0, 1),
                "owner": worker["owner"],
                "reason": reasons[worker["pid"]],
                "owner_should_run": (
                    "python3 -m creme reclaim --idle-workers "
                    f"{arguments.idle_workers}"
                    + (
                        f" --goal {worker['owner'][len('goal '):]}"
                        if str(worker["owner"]).startswith("goal ") else ""
                    )
                ),
            }
            for worker in foreign
        ],
    }
    if not targets:
        _json({
            **observed,
            "status": "OK",
            "detail": "no caller-owned Lean worker met the idleness threshold",
        })
        return 0
    if arguments.dry_run:
        _json({**observed, "status": "OK", "detail": "dry-run frozen idle-worker plan"})
        return 0
    result = adapter.reclaim(
        [*scope_options, *(option for pid in targets for option in ("--only-pid", str(pid)))]
    )
    _json({**observed, "status": result.status, "detail": result.detail, "reclaim": result.to_dict()})
    return 0 if result.status == "OK" else 2


def cmd_reclaim(arguments: argparse.Namespace) -> int:
    if getattr(arguments, "idle_workers", None) is not None:
        if arguments.hard_pressure or arguments.wind_down:
            _json({
                "capability": "idle_workers",
                "status": "REFUSED",
                "detail": "--idle-workers cannot be combined with --wind-down or --hard-pressure",
            })
            return 2
        return cmd_idle_workers(arguments)
    if getattr(arguments, "goal", None) is not None:
        _json({
            "capability": "idle_workers",
            "status": "REFUSED",
            "detail": "--goal is only meaningful with --idle-workers",
        })
        return 2
    wind_down_label = getattr(arguments, "wind_down", None)
    if wind_down_label is not None:
        if arguments.hard_pressure or arguments.dry_run:
            _json({
                "capability": "task_wind_down",
                "status": "REFUSED",
                "detail": "--wind-down cannot be combined with --dry-run or --hard-pressure",
            })
            return 2
        result = wind_down(wind_down_label, get_adapter())
        _json(result.to_dict())
        return 0 if result.status == "OK" else 2
    options = []
    if arguments.hard_pressure:
        options.append("--hard-pressure")
    if arguments.dry_run:
        options.append("--dry-run")
    result = get_adapter().reclaim(options)
    _json(result.to_dict())
    return 0 if result.status == "OK" else 2


def _sem_result(ok: bool, detail: str) -> int:
    print(("OK" if ok else "REFUSED") + " — " + detail)
    return 0 if ok else 1


def _hold_heartbeat(label: str, interval: int, detach: bool, owner_pid: Optional[int]) -> int:
    """Start (or, without ``detach``, run) the heartbeat of an acquired hold."""
    if owner_pid is None:
        owner_pid, found = semaphore.hold_heartbeat_owner()
        if owner_pid is None:
            return _sem_result(False, (
                f"heartbeat not started ({found}); the hold {label} stays acquired — "
                "renew it manually or release it"
            ))
    if detach:
        ok, detail = semaphore.hold_heartbeat_detached(label, interval, owner_pid)
    else:
        ok, detail = semaphore.hold_heartbeat(label, interval, owner_pid=owner_pid)
    if not ok:
        detail += f"; the hold {label} is not renewed by a heartbeat — renew it manually or release it"
    return _sem_result(ok, detail)


def cmd_semaphore(arguments: argparse.Namespace) -> int:
    action = arguments.action
    if action == "status":
        print(semaphore.status_text())
        return 0
    if action == "adaptive-acquire":
        if arguments.detach and arguments.heartbeat is None:
            return _sem_result(False, "--detach needs --heartbeat SECS")
        if arguments.heartbeat is not None and arguments.heartbeat >= arguments.lease:
            return _sem_result(False, (
                f"--heartbeat {arguments.heartbeat} must be shorter than --lease {arguments.lease}"
            ))
        ok, detail = semaphore.adaptive_acquire(
            arguments.label,
            arguments.note,
            arguments.lease,
            memory_gib=arguments.memory_gib,
            contention=arguments.contention,
            wait_seconds=arguments.wait,
            # Without --memory-gib the need is the host default, not evidence:
            # at most one such unproven unit runs at a time.
            unproven=arguments.memory_gib is None,
            # A queued request blocks the caller's turn, so the arithmetic that
            # decides it is printed before the wait begins, not after it fails.
            announce=(print if arguments.wait is not None else None),
        )
        if not ok or arguments.heartbeat is None:
            return _sem_result(ok, detail)
        _sem_result(ok, detail)
        return _hold_heartbeat(arguments.label, arguments.heartbeat, arguments.detach, None)
    if action == "hard-release":
        # Kept for the contained-workflow runtime's recovery path.
        return _sem_result(*semaphore.release("hard", arguments.label))
    if action == "release":
        return _sem_result(*semaphore.adaptive_release(arguments.label))
    if action == "renew":
        if arguments.heartbeat is not None:
            return _hold_heartbeat(arguments.label, arguments.heartbeat, arguments.detach, arguments.owner_pid)
        if arguments.detach or arguments.owner_pid is not None:
            return _sem_result(False, "--detach and --owner-pid need --heartbeat SECS")
        return _sem_result(*semaphore.renew(arguments.label, arguments.lease))
    if action == "break":
        return _sem_result(*semaphore.break_expired(arguments.label, arguments.reason))
    if action == "manual-acquire":
        return _sem_result(*semaphore.manual_acquire(arguments.note))
    if action == "manual-release":
        return _sem_result(*semaphore.manual_release())
    if action == "migrate-state":
        return _sem_result(*semaphore.migrate_legacy_state())
    if action == "master-acquire":
        return _sem_result(*semaphore.master_acquire(
            arguments.client, arguments.note, arguments.lease, take_over=arguments.take_over,
        ))
    if action == "master-renew":
        if arguments.heartbeat is not None:
            child_mode = os.environ.pop(semaphore.MASTER_HEARTBEAT_CHILD_ENV, None)
            if arguments.detach:
                if child_mode is not None:
                    return _sem_result(False, "a detached heartbeat child cannot detach again")
                return _sem_result(*semaphore.master_heartbeat_detached(arguments.heartbeat))
            launch_capability = None
            if child_mode is not None:
                if child_mode != "1":
                    return _sem_result(False, "detached heartbeat child marker is malformed")
                ok, capability = semaphore.read_master_heartbeat_launch_capability()
                if not ok:
                    return _sem_result(False, capability)
                launch_capability = capability
            return _sem_result(*semaphore.master_heartbeat(
                arguments.heartbeat,
                launch_capability=launch_capability,
            ))
        return _sem_result(*semaphore.master_renew(arguments.lease))
    if action == "master-release":
        return _sem_result(*semaphore.master_release(
            force=arguments.force, reason=arguments.reason,
        ))
    return _sem_result(False, f"unknown action: {action}")


def render_codex_profile(workspace: Path, auto_review: bool = False) -> str:
    creme = workspace / "creme"
    jaune = workspace / "jaune"
    blanc = workspace / "blanc"
    roots = [creme, jaune, blanc]
    lines = [
        'default_permissions = "creme-relay"',
        *([
            'approval_policy = "on-request"',
            'approvals_reviewer = "auto_review"',
        ] if auto_review else []),
        "",
        "[features]",
        "network_proxy = true",
        "",
        f"[projects.{_toml_string(creme)}]",
        'trust_level = "trusted"',
        "",
        "[permissions.creme-relay]",
        'description = "Creme with reviewed access to its Jaune and Blanc siblings."',
        'extends = ":workspace"',
        "",
        "[permissions.creme-relay.workspace_roots]",
    ]
    lines.extend(f"{_toml_string(root)} = true" for root in roots)
    lines.extend(["", "[permissions.creme-relay.filesystem]"])
    lines.extend(f'{_toml_string(root / ".git")} = "write"' for root in roots)
    lines.extend([
        "", "[permissions.creme-relay.network]", "enabled = true",
        "", "[permissions.creme-relay.network.domains]",
        '"github.com" = "allow"',
        '"api.github.com" = "allow"',
        '"objects.githubusercontent.com" = "allow"',
        "",
    ])
    return "\n".join(lines)


def cmd_client_profile(arguments: argparse.Namespace) -> int:
    workspace = Path(arguments.workspace_root).expanduser().resolve() if arguments.workspace_root else ROOT.parent
    rendered = render_codex_profile(workspace, auto_review=arguments.auto_review)
    if not arguments.write:
        print("# PREVIEW — review before writing; permission profiles are client-version-sensitive.")
        print(rendered, end="")
        return 0
    if not arguments.output:
        print("REFUSED — --output is required with --write; Creme never chooses or overwrites global config implicitly")
        return 1
    output = Path(arguments.output).expanduser().resolve()
    if output.exists() and not arguments.replace:
        print(f"REFUSED — output exists: {output}; use --replace after review")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=output.name + ".tmp.",
        dir=str(output.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"OK — reviewed Codex profile written to {output}")
    return 0


def cmd_host_wrappers(arguments: argparse.Namespace) -> int:
    output = (
        Path(arguments.output_dir).expanduser().absolute()
        if arguments.output_dir
        else default_host_wrapper_output_dir().absolute()
    )
    rules = (
        Path(arguments.rules_dir).expanduser().absolute()
        if arguments.rules_dir
        else default_host_rules_dir().absolute()
    )
    try:
        rendered = render_host_wrappers(ROOT)
    except (OSError, RuntimeError, ValueError) as exc:
        _json({"status": "REFUSED", "detail": str(exc)})
        return 1
    if not arguments.write:
        expected_rules = render_host_rules(
            output, include_build=BROKER_NAME in rendered,
            include_workflow=WORKFLOW_BROKER_NAME in rendered,
        )
        rules_changed = not _path_has_text(rules / RULES_FILENAME, expected_rules)
        _json({
            "status": "PREVIEW",
            "detail": (
                "review this complete delegate-and-rules bundle, then rerun with --write; "
                + (
                    "the rule bytes would change, so Codex must then be fully restarted"
                    if rules_changed else
                    "the rule bytes are identical, so an already-loaded rule needs no restart"
                )
            ),
            "creme_root": str(ROOT),
            "output_dir": str(output),
            "rules_dir": str(rules),
            "rules_changed_by_install": rules_changed,
            "wrappers": {
                str(output / name): content for name, content in rendered.items()
            },
            "rules": {
                str(rules / RULES_FILENAME): expected_rules,
            },
        })
        return 0
    if not arguments.output_dir or not arguments.rules_dir:
        _json({
            "status": "REFUSED",
            "detail": (
                "--output-dir and --rules-dir are both required with --write; "
                "Creme never chooses or overwrites user authorization state implicitly"
            ),
        })
        return 1
    try:
        expected_rules = render_host_rules(
            output, include_build=BROKER_NAME in rendered,
            include_workflow=WORKFLOW_BROKER_NAME in rendered,
        )
        rules_changed = not _path_has_text(rules / RULES_FILENAME, expected_rules)
        written = install_host_bundle(
            ROOT, output, rules, replace=arguments.replace,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _json({"status": "REFUSED", "detail": str(exc)})
        return 1
    _json({
        "status": "OK",
        "detail": (
            "reviewed host capability bundle written; fully quit and restart Codex "
            "before expecting new rule bytes to apply"
            if rules_changed else
            "reviewed host capability bundle refreshed; rule bytes were already identical, "
            "so an already-loaded rule needs no restart"
        ),
        "creme_root": str(ROOT),
        "rules_changed_by_install": rules_changed,
        "paths": [str(path) for path in written],
    })
    return 0


def cmd_lean_mcp(arguments: argparse.Namespace) -> int:
    command = list(arguments.mcp_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        _json({"status": "REFUSED", "detail": "a pinned MCP command is required after --"})
        return 2
    if command[0] != "uvx":
        _json({"status": "REFUSED", "detail": "the guarded MCP launcher requires the reviewed uvx runner"})
        return 2
    try:
        runner = trusted_uvx()
        env = guarded_mcp_env()
    except (OSError, RuntimeError) as exc:
        _json({"status": "REFUSED", "detail": str(exc)})
        return 2
    command[0] = str(runner)
    os.execve(runner, command, env)


def cmd_lake_build(arguments: argparse.Namespace) -> int:
    options = argparse.ArgumentParser(prog=f"~/creme/scripts/creme lake-build {arguments.goal}")
    options.add_argument(
        "--memory-gib",
        type=_positive,
        help="conservative whole-GiB peak; derived from the ledger's measured peaks when omitted",
    )
    options.add_argument(
        "--contention",
        choices=sorted(semaphore.ADMISSION_CONTENTION),
        help="override the evidence class; omit to classify from the stale set and measured peaks",
    )
    options.add_argument(
        "--wait",
        type=_positive,
        metavar="SECS",
        help="queue this build and return when admitted, on WAIT_TIMEOUT, or on a verdict waiting cannot change",
    )
    options.add_argument(
        "--threads",
        type=int,
        choices=(1, 2),
        default=DEFAULT_THREADS,
        help=f"set LEAN_NUM_THREADS for this owned build (default: {DEFAULT_THREADS})",
    )
    options.add_argument("--probe", action="store_true")
    options.add_argument(
        "--census",
        action="store_true",
        help="update one Git-pinned dependency and rebuild the full target, exclusively, in a GOAL-rehearsal worktree",
    )
    options.add_argument("--dependency", metavar="NAME")
    options.add_argument(
        "--full-output",
        action="store_true",
        help="print Lake's full verbose stream (default: failed/warning jobs only, bounded; the full stream is always in the run's log file)",
    )
    options.add_argument(
        "--walk",
        action="store_true",
        help=(
            "if the whole stale closure is refused LIGHT_ONLY or NEVER_FITS, build its stale "
            "modules one owned unit at a time, imports first, then the targets"
        ),
    )
    options.add_argument("targets", nargs=argparse.REMAINDER)
    selected = options.parse_args(arguments.build_args)
    targets = list(selected.targets)
    if targets and targets[0] == "--":
        targets = targets[1:]
    return run_lake_build(
        arguments.goal,
        targets,
        memory_gib=selected.memory_gib,
        contention=selected.contention,
        threads=selected.threads,
        probe=selected.probe,
        wait_seconds=selected.wait,
        census=selected.census,
        dependency=selected.dependency,
        full_output=selected.full_output,
        walk=selected.walk,
    )


def cmd_build_ledger(arguments: argparse.Namespace) -> int:
    try:
        _json(ledger_rollup(arguments.since, arguments.until))
    except ValueError as exc:
        _json({"status": "REFUSED", "detail": str(exc)})
        return 2
    return 0


def _model_fit_dir(arguments: argparse.Namespace) -> Path:
    if arguments.dir:
        return Path(arguments.dir).expanduser()
    return model_fit.default_dir(ROOT)


def cmd_model_fit_validate(arguments: argparse.Namespace) -> int:
    try:
        directory = _model_fit_dir(arguments)
    except model_fit.ModelFitError as exc:
        print(f"model-fit: {exc}", file=sys.stderr)
        return 2
    errors, counts = model_fit.validate_dir(directory)
    for name, tally in counts.items():
        print(f"{name}: {tally['observations']} observations "
              f"({tally['verified']} verified, {tally['unknown']} verdict unknown)")
    for error in errors:
        print(f"ERROR {error}")
    print(f"model-fit validate {directory}: {'FAIL' if errors else 'OK'}")
    return 1 if errors else 0


def cmd_model_fit_init(arguments: argparse.Namespace) -> int:
    try:
        directory = _model_fit_dir(arguments)
    except model_fit.ModelFitError as exc:
        print(f"model-fit: {exc}", file=sys.stderr)
        return 2
    for client in model_fit.CLIENTS.values():
        path = directory / f"{client.name}.md"
        if path.exists():
            print(f"kept {path}")
            continue
        model_fit.write_atomic(path, model_fit.skeleton(client))
        print(f"created {path}")
    return 0


def cmd_model_fit_summarize(arguments: argparse.Namespace) -> int:
    failed = False
    for name in arguments.files:
        errors = model_fit.summarize_file(Path(name).expanduser())
        for error in errors:
            print(f"ERROR {error}")
        failed = failed or bool(errors)
        print(f"{name}: {'FAIL' if errors else 'summary regenerated'}")
    return 1 if failed else 0


def cmd_model_fit_add(arguments: argparse.Namespace) -> int:
    fields: dict[str, str] = {}
    extracted: dict[str, Any] = {}
    try:
        if arguments.from_claude_transcript:
            path = Path(arguments.from_claude_transcript).expanduser()
            extracted = model_fit.claude_transcript_usage(path, arguments.since, arguments.until)
            fields["source"] = str(path)
            fields["run"] = path.stem
        elif arguments.from_luna_session:
            path = Path(arguments.from_luna_session).expanduser()
            extracted = model_fit.luna_session_usage(path)
            fields["source"] = str(path)
            fields["run"] = path.name
        elif arguments.from_codex_rollout:
            path = Path(arguments.from_codex_rollout).expanduser()
            extracted = model_fit.codex_rollout_usage(path)
            fields["source"] = str(path)
            fields["run"] = path.stem
    except (OSError, ValueError, KeyError) as exc:
        print(f"model-fit: cannot read run record: {exc}", file=sys.stderr)
        return 2
    for key in ("tokens", "wall_time", "turns", "date"):
        if extracted.get(key):
            fields[key] = str(extracted[key])
    fields.setdefault("retries", "0")
    fields.setdefault("rework", "none")
    fields.setdefault("failure_modes", "none")
    for key in model_fit.FIELDS:
        value = getattr(arguments, key, None)
        if value is not None:
            fields[key] = value
    if extracted.get("model"):
        print(f"run record: served model {extracted['model']}")
    errors, ident = model_fit.add_observation(Path(arguments.file).expanduser(), fields, arguments.dry_run)
    for error in errors:
        print(f"ERROR {error}")
    if errors:
        return 1
    for key in model_fit.FIELDS:
        if key in fields:
            print(f"- {key}: {fields[key]}")
    print(f"{'would add' if arguments.dry_run else 'added'} {ident} to {arguments.file}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python3 -m creme")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    platform_parser = commands.add_parser("platform", help="report the selected OS adapter and static facts")
    platform_parser.set_defaults(func=cmd_platform)

    python_runtime = commands.add_parser(
        "python-runtime",
        help="report the native uv-managed CPython identity for this platform",
    )
    python_runtime.add_argument("version", help="exact major.minor.patch version")
    python_runtime.set_defaults(func=cmd_python_runtime)

    init_parser = commands.add_parser("init", help="preview or write the ignored host profile")
    init_parser.add_argument("--profile")
    init_parser.add_argument("--workspace-root")
    init_parser.add_argument("--write", action="store_true")
    init_parser.add_argument("--replace", action="store_true")
    init_parser.add_argument(
        "--goal-store",
        metavar="NAME",
        help="name of the private goal store beside the siblings (e.g. plans); optional",
    )
    init_parser.set_defaults(func=cmd_init)

    validate = commands.add_parser("validate-profile")
    validate.add_argument("--profile")
    validate.set_defaults(func=cmd_validate_profile)

    guidance = commands.add_parser(
        "host-guidance",
        help="read validated ignored machine-local safety guidance",
    )
    guidance.set_defaults(func=cmd_host_guidance)

    doctor = commands.add_parser("doctor", help="read-only launch, client, sibling, and host diagnostics")
    doctor.add_argument("--profile")
    doctor.add_argument("--workspace-root")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--task-memory-gib", type=_task_memory)
    doctor.add_argument("--heavy-workers", type=_positive)
    doctor.add_argument("--light-workers", type=_positive)
    doctor.set_defaults(func=cmd_doctor)

    master = commands.add_parser(
        "master",
        help="operate the configured ignored master-session record",
    )
    master_commands = master.add_subparsers(dest="master_action", required=True)
    master_init = master_commands.add_parser(
        "init",
        help="preview the standard private layout; --apply is the only mutation",
    )
    master_init.add_argument("--apply", action="store_true")
    master_init.set_defaults(func=cmd_master_init)

    master_retire_migration = master_commands.add_parser(
        "retire-migration",
        help="archive a completed legacy migration's retained nodes outside the record",
    )
    master_retire_migration.add_argument("--apply", action="store_true")
    master_retire_migration.set_defaults(func=cmd_master_retire_migration)

    master_restore_migration = master_commands.add_parser(
        "restore-migration",
        help="copy an archived legacy migration back into the record byte for byte",
    )
    master_restore_migration.add_argument("archive", help="directory name under master-archive/")
    master_restore_migration.add_argument("--apply", action="store_true")
    master_restore_migration.set_defaults(func=cmd_master_restore_migration)

    master_start = master_commands.add_parser(
        "start",
        help="enter as the master or report the current reader state",
    )
    master_start.add_argument("--client", required=True)
    master_start.add_argument("--model", required=True)
    master_start.add_argument("--effort", required=True)
    master_start.add_argument("--note", required=True)
    master_start.add_argument("--take-over", action="store_true")
    master_start.set_defaults(func=cmd_master_start)

    master_event = master_commands.add_parser(
        "event",
        help="append one validated JSON event from a file or standard input",
    )
    master_event.add_argument("--from", dest="source", required=True, metavar="FILE")
    master_event.set_defaults(func=cmd_master_event)

    master_digest = master_commands.add_parser(
        "digest",
        help="read a bounded source-validated continuity digest without acquiring",
    )
    master_digest.add_argument("--goals-limit", type=_nonnegative)
    master_digest.add_argument("--decisions-limit", type=_nonnegative)
    master_digest.add_argument("--findings-limit", type=_nonnegative)
    master_digest.add_argument("--discrepancies-limit", type=_nonnegative)
    master_digest.add_argument(
        "--focused",
        action="store_true",
        help="prioritize current goals and show bounded actionable continuity",
    )
    master_digest.add_argument(
        "--after-goal",
        metavar="GOAL_ID",
        help="continue a focused goal page after its last shown goal",
    )
    digest_lookup = master_digest.add_mutually_exclusive_group()
    digest_lookup.add_argument(
        "--goal", metavar="GOAL_ID", help="retrieve one exact goal row"
    )
    digest_lookup.add_argument(
        "--decision", metavar="DECISION_ID", help="retrieve one exact open decision row"
    )
    digest_lookup.add_argument(
        "--finding", metavar="FINDING_ID", help="retrieve one exact open finding row"
    )
    master_digest.add_argument(
        "--reconcile",
        action="store_true",
        help="compare the current board with configured Git/worktree facts without mutation",
    )
    master_digest.add_argument("--human", action="store_true")
    master_digest.set_defaults(func=cmd_master_digest)

    telemetry = commands.add_parser("telemetry", help="sample dynamic host state through the selected adapter")
    telemetry.set_defaults(func=cmd_telemetry)

    headroom = commands.add_parser(
        "memory-headroom",
        help="sample aggregate memory admission state without process discovery",
    )
    headroom.set_defaults(func=cmd_memory_headroom)

    temporary = commands.add_parser("tempdir", help="preview or create a portable temporary directory")
    temporary.add_argument("--create", action="store_true")
    temporary.add_argument("--prefix", default="creme-")
    temporary.set_defaults(func=cmd_tempdir)

    copy = commands.add_parser("cache-copy", help="preview or perform capability-selected cache copying")
    copy.add_argument("source")
    copy.add_argument("destination")
    copy.add_argument("--execute", action="store_true")
    copy.set_defaults(func=cmd_cache_copy)

    reclaim = commands.add_parser("reclaim", help="ownership-verifying Lean-server reclamation")
    reclaim.add_argument("--dry-run", action="store_true")
    reclaim.add_argument("--hard-pressure", action="store_true")
    reclaim.add_argument("--wind-down", metavar="GOAL")
    reclaim.add_argument(
        "--goal",
        metavar="GOAL",
        help="with --idle-workers: reclaim only workers working inside this goal's worktrees",
    )
    reclaim.add_argument(
        "--idle-workers",
        type=_nonnegative,
        metavar="MIN",
        help=(
            "terminate caller-owned lean --worker processes idle for more than MIN "
            "minutes; every other idle worker is reported with its owner, never killed"
        ),
    )
    reclaim.set_defaults(func=cmd_reclaim)

    sem = commands.add_parser("semaphore", help="atomic cross-session host coordination")
    sem_commands = sem.add_subparsers(dest="action", required=True)
    sem_commands.add_parser("status")
    adaptive = sem_commands.add_parser(
        "adaptive-acquire",
        help="atomically choose soft, hard, or deferred heavy work from live headroom",
    )
    adaptive.add_argument("label")
    adaptive.add_argument("--note", required=True)
    adaptive.add_argument(
        "--memory-gib",
        type=_positive,
        help="conservative whole-GiB peak estimate; defaults to the host policy",
    )
    adaptive.add_argument(
        "--contention",
        choices=sorted(semaphore.ADMISSION_CONTENTION),
        default="tolerant",
        help="use sensitive for bursty/unknown work and exclusive for authoritative runs",
    )
    adaptive.add_argument(
        "--lease",
        type=int,
        default=semaphore.ADAPTIVE_LEASE_SECONDS,
    )
    adaptive.add_argument(
        "--wait",
        type=_positive,
        metavar="SECS",
        help=(
            "queue the request and return when it is admitted, when SECS elapses "
            "(WAIT_TIMEOUT), or on a verdict waiting cannot change; never poll by hand"
        ),
    )
    adaptive.add_argument(
        "--heartbeat",
        type=_positive,
        metavar="SECS",
        help=(
            "after admission, renew the hold every SECS seconds while the agent client "
            "above this command lives; stops by itself on release, on the client's "
            "exit, or on a YIELD_HEAVY/DRAIN_HEAVY renewal verdict"
        ),
    )
    adaptive.add_argument(
        "--detach",
        action="store_true",
        help="with --heartbeat: run it in its own process session and return once it has started",
    )
    sem_commands.add_parser("hard-release").add_argument("label")
    adaptive_release = sem_commands.add_parser(
        "release",
        help="release whichever hold kind adaptive acquisition selected",
    )
    adaptive_release.add_argument("label")
    renew = sem_commands.add_parser("renew")
    renew.add_argument("label")
    renew.add_argument("--lease", type=int, default=semaphore.DEFAULT_LEASE_SECONDS)
    renew.add_argument(
        "--heartbeat",
        type=_positive,
        metavar="SECS",
        help="keep renewing the existing hold every SECS seconds (see adaptive-acquire --heartbeat)",
    )
    renew.add_argument("--detach", action="store_true", help="with --heartbeat: run it detached")
    renew.add_argument(
        "--owner-pid",
        type=_positive,
        help="with --heartbeat: the process whose exit ends it; defaults to the agent client above this command",
    )
    breaking = sem_commands.add_parser("break")
    breaking.add_argument("label")
    breaking.add_argument("--reason", required=True)
    manual = sem_commands.add_parser("manual-acquire")
    manual.add_argument("--note", default="human using another macOS account")
    sem_commands.add_parser("manual-release")
    sem_commands.add_parser(
        "migrate-state",
        help="copy legacy host state into .semaphore/state without deleting the legacy files",
    )
    master_acquire = sem_commands.add_parser(
        "master-acquire",
        help="take the single master lease; refused while another master is live",
    )
    master_acquire.add_argument(
        "--client",
        help="claude, codex, muse, or human; detected from the process ancestry when omitted",
    )
    master_acquire.add_argument("--note", required=True)
    master_acquire.add_argument("--lease", type=int, default=semaphore.MASTER_LEASE_SECONDS)
    master_acquire.add_argument(
        "--take-over",
        action="store_true",
        help="replace a lapsed or stranded lease; never a live one",
    )
    master_renew = sem_commands.add_parser("master-renew", help="heartbeat the master lease")
    master_renew.add_argument("--lease", type=int, default=None)
    master_renew.add_argument(
        "--heartbeat",
        type=_positive,
        metavar="SECS",
        help=(
            "run in the background: renew every SECS seconds until the lease is gone, "
            "the bound session disappears, or bounded fallback renewal becomes passive"
        ),
    )
    master_renew.add_argument(
        "--detach",
        action="store_true",
        help="with --heartbeat: start it in its own process session and return at once",
    )
    master_release = sem_commands.add_parser("master-release", help="end the master lease")
    master_release.add_argument(
        "--force",
        action="store_true",
        help="release a live lease held by another client; logged with --reason",
    )
    master_release.add_argument("--reason", default="")
    sem.set_defaults(func=cmd_semaphore)

    client = commands.add_parser("client-profile", help="preview a machine-local Codex sibling-access profile")
    client.add_argument("--workspace-root")
    client.add_argument(
        "--auto-review", action="store_true",
        help="opt in to native risk-based approval review; preserve the workspace sandbox",
    )
    client.add_argument("--output")
    client.add_argument("--write", action="store_true")
    client.add_argument("--replace", action="store_true")
    client.set_defaults(func=cmd_client_profile)

    wrappers = commands.add_parser(
        "host-wrappers",
        help="preview or install stable Codex delegates and their least-privilege rules",
    )
    wrappers.add_argument("--output-dir")
    wrappers.add_argument("--rules-dir")
    wrappers.add_argument("--write", action="store_true")
    wrappers.add_argument("--replace", action="store_true")
    wrappers.set_defaults(func=cmd_host_wrappers)

    lean_mcp = commands.add_parser(
        "lean-mcp",
        help="launch the pinned Lean MCP with the fail-closed Lake guard",
    )
    lean_mcp.add_argument("mcp_command", nargs=argparse.REMAINDER)
    lean_mcp.set_defaults(func=cmd_lean_mcp)

    lake_build = commands.add_parser(
        "lake-build",
        help="run one admitted, classified, measured Lake build",
    )
    lake_build.add_argument("goal")
    lake_build.add_argument("build_args", nargs=argparse.REMAINDER)
    lake_build.set_defaults(func=cmd_lake_build)

    build_ledger = commands.add_parser(
        "build-ledger",
        help="summarize ignored host-local Lean build ownership measurements",
    )
    build_ledger.add_argument(
        "--since",
        default="7d",
        help="duration such as 7d/24h/30m, or an absolute UTC instant such as 2026-09-03",
    )
    build_ledger.add_argument(
        "--until",
        help="optional absolute UTC instant closing the window, for a fixed baseline",
    )
    build_ledger.set_defaults(func=cmd_build_ledger)

    fit = commands.add_parser("model-fit", help="per-client model/effort fit tables in the goal store")
    fit_commands = fit.add_subparsers(dest="fit_action", required=True)
    fit_validate = fit_commands.add_parser("validate", help="validate every client table in DIR")
    fit_validate.add_argument("dir", nargs="?", help=f"default: the goal store's {model_fit.TABLE_DIR}/")
    fit_validate.set_defaults(func=cmd_model_fit_validate)
    fit_init = fit_commands.add_parser("init", help="create missing client table skeletons in DIR")
    fit_init.add_argument("dir", nargs="?")
    fit_init.set_defaults(func=cmd_model_fit_init)
    fit_summarize = fit_commands.add_parser("summarize", help="regenerate the derived summary of FILE")
    fit_summarize.add_argument("files", nargs="+")
    fit_summarize.set_defaults(func=cmd_model_fit_summarize)
    fit_add = fit_commands.add_parser("add", help="append one master-verified observation and regenerate")
    fit_add.add_argument("file", help="the client table, e.g. $GOAL_STORE/model-fit/claude-code.md")
    fit_add.add_argument("--dry-run", action="store_true")
    record = fit_add.add_mutually_exclusive_group()
    record.add_argument("--from-claude-transcript", metavar="JSONL")
    record.add_argument("--from-luna-session", metavar="DIR")
    record.add_argument("--from-codex-rollout", metavar="JSONL")
    fit_add.add_argument("--since", metavar="ISO", help="Claude transcript: count only entries at or after this time")
    fit_add.add_argument("--until", metavar="ISO", help="Claude transcript: count only entries at or before this time")
    for key in model_fit.FIELDS:
        fit_add.add_argument("--" + key.replace("_", "-"), dest=key)
    fit_add.set_defaults(func=cmd_model_fit_add)

    antigravity_parser = commands.add_parser(
        "antigravity", help="read-only Antigravity (agy) pseudo-subagent runs",
    )
    antigravity_commands = antigravity_parser.add_subparsers(dest="antigravity_action", required=True)
    antigravity_status = antigravity_commands.add_parser("status", help="zero-token quota and admission read")
    antigravity_status.add_argument("--model", default=antigravity.DEFAULT_MODEL)
    antigravity_status.add_argument("--json", action="store_true", help="print the full JSON record")
    antigravity_status.set_defaults(func=cmd_antigravity_status)
    antigravity_run = antigravity_commands.add_parser("run", help="run one bounded read-only brief")
    antigravity_run.add_argument("--brief", required=True, help="brief file, or - for stdin")
    antigravity_run.add_argument("--target", required=True, help="directory the brief is about")
    antigravity_run.add_argument("--model", default=antigravity.DEFAULT_MODEL)
    antigravity_run.add_argument("--effort", choices=("low", "medium", "high"), default=antigravity.DEFAULT_EFFORT)
    antigravity_run.add_argument("--timeout-seconds", type=_positive, default=1800)
    antigravity_run.add_argument("--json", action="store_true", help="print the full JSON record")
    antigravity_run.set_defaults(func=cmd_antigravity_run)

    luna = commands.add_parser(
        "luna-reserve",
        help="guarded Codex Luna reserve (gpt-reserve) pseudo-subagent runs",
    )
    luna_commands = luna.add_subparsers(dest="luna_action", required=True)
    luna_status = luna_commands.add_parser("status", help="zero-token reserve and regular bucket read")
    _add_luna_policy_arguments(luna_status)
    luna_status.set_defaults(func=cmd_luna_reserve_status)
    luna_run = luna_commands.add_parser(
        "run",
        help="run one bounded brief on gpt-reserve with preflight and attribution audit",
    )
    _add_luna_policy_arguments(luna_run)
    luna_run.add_argument("--brief", required=True, help="brief file, or - for stdin")
    luna_run.add_argument(
        "--target", required=True,
        help="directory the brief is about; the only writable root in --write mode",
    )
    luna_run.add_argument(
        "--effort", default=luna_reserve.DEFAULT_EFFORT,
        help="reasoning effort: low, medium (default), high, xhigh, or max",
    )
    luna_run.add_argument(
        "--write", action="store_true",
        help="allow edits confined to --target (default is read-only)",
    )
    luna_run.add_argument(
        "--timeout-seconds", type=_positive, default=luna_reserve.DEFAULT_TIMEOUT_SECONDS,
    )
    luna_run.add_argument(
        "--preflight-only", action="store_true",
        help="launch the isolated server and verify admission and isolation, then stop (no thread, no tokens)",
    )
    luna_run.add_argument(
        "--allow-regular-available", action="store_true",
        help="accepted but has no effect: the condition it guarded (reserve attribution with an "
             "available regular bucket) was verified 2026-09-20; retained for compatibility",
    )
    _add_luna_refused_overrides(luna_run)
    luna_run.set_defaults(func=cmd_luna_reserve_run)
    luna_audit = luna_commands.add_parser("audit", help="re-audit a rollout path or thread id")
    _add_luna_policy_arguments(luna_audit)
    luna_audit.add_argument("target", help="rollout .jsonl path or Codex thread id")
    luna_audit.set_defaults(func=cmd_luna_reserve_audit)

    # Broker sessions: every command prints a few lines; see docs/guides/luna-reserve.md.
    def session_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        item = luna_commands.add_parser(name, help=help_text)
        item.add_argument("--json", action="store_true", help="print the full JSON record")
        return item

    def open_arguments(item: argparse.ArgumentParser) -> None:
        _add_luna_policy_arguments(item)
        item.add_argument("--write", action="store_true",
                          help="allow edits confined to --target; approvals are routed to the master")
        item.add_argument("--lean", metavar="GOAL",
                          help="Lean mode (implies --write): --target must be the Jaune or Blanc .worktrees/GOAL; "
                               "keeps only the tracked lean-lsp-mcp and winds GOAL down at every stop")
        item.add_argument("--detail", default=luna_broker.DEFAULT_DETAIL, choices=luna_broker.DETAIL_LEVELS)
        item.add_argument("--timeout-seconds", type=_positive, default=luna_reserve.DEFAULT_TIMEOUT_SECONDS,
                          help="per-turn timeout; the broker interrupts a longer turn")
        item.add_argument("--allow-regular-available", action="store_true",
                          help="accepted but has no effect (verified 2026-09-20); retained for compatibility")
        _add_luna_refused_overrides(item)

    luna_start = luna_commands.add_parser("start", help="start a brokered session and its first turn; returns at once")
    open_arguments(luna_start)
    luna_start.add_argument("--brief", required=True, help="brief file, or - for stdin")
    luna_start.add_argument("--target", required=True, help="directory the brief is about")
    luna_start.add_argument("--effort", default=luna_reserve.DEFAULT_EFFORT, help="low, medium (default), high, xhigh, or max")
    luna_start.set_defaults(func=cmd_luna_reserve_start)

    luna_resume = luna_commands.add_parser("resume", help="attach a new brokered session to a recorded thread")
    open_arguments(luna_resume)
    luna_resume.add_argument("thread", help="Codex thread id")
    luna_resume.add_argument("--target", help="defaults to the thread's recorded target")
    luna_resume.add_argument("--effort", help="defaults to the thread's recorded effort")
    luna_resume.set_defaults(func=cmd_luna_reserve_resume)

    for name, help_text in (("send", "new turn when idle, steer when a turn is running"),
                            ("steer", "steer the running turn only")):
        item = session_parser(name, help_text)
        item.add_argument("session")
        item.add_argument("--text", help="message text (else --brief FILE, or stdin)")
        item.add_argument("--brief", help="message file, or - for stdin")
        item.set_defaults(func=cmd_luna_reserve_send)

    for name, help_text in (("interrupt", "interrupt the running turn"),
                            ("stop", "interrupt if needed, audit, close; records are kept")):
        item = session_parser(name, help_text)
        item.add_argument("session")
        item.set_defaults(func=cmd_luna_reserve_session_op)

    luna_approve = session_parser("approve", "answer a queued approval request")
    luna_approve.add_argument("session")
    luna_approve.add_argument("approval", help="approval id such as a1")
    luna_approve.add_argument("decision", choices=luna_broker.DECISIONS)
    luna_approve.set_defaults(func=cmd_luna_reserve_session_op)

    luna_detail = session_parser("detail", "change the session's event detail level")
    luna_detail.add_argument("session")
    luna_detail.add_argument("level", choices=luna_broker.DETAIL_LEVELS)
    luna_detail.set_defaults(func=cmd_luna_reserve_detail)

    luna_wait = session_parser("wait", "block until the session is idle, needs attention, or ends")
    luna_wait.add_argument("session")
    luna_wait.add_argument("--timeout", type=_positive, default=540)
    luna_wait.set_defaults(func=cmd_luna_reserve_wait)

    luna_approve_builds = luna_commands.add_parser(
        "approve-builds", help="opt-in: accept only the exact guarded Lean lake-build approvals",
    )
    luna_approve_builds.add_argument("session")
    luna_approve_builds.add_argument("--header-base", metavar="REF")
    luna_approve_builds.add_argument("--header-file", metavar="PATH", action="append", nargs="+")
    luna_approve_builds.add_argument("--allow-removed", metavar="NAME", action="append", nargs="+")
    luna_approve_builds.add_argument("--timeout", type=_positive, default=3600)
    luna_approve_builds.set_defaults(func=cmd_luna_reserve_approve_builds)

    luna_events = luna_commands.add_parser("events", help="the session's event feed at its detail level")
    luna_events.add_argument("session")
    luna_events.add_argument("--follow", action="store_true", help="keep printing until the session ends")
    luna_events.add_argument("--last", type=_positive, default=20)
    luna_events.add_argument("--since", type=int, default=0, help="only events after this sequence number")
    luna_events.add_argument("--timeout", type=_positive, default=None)
    luna_events.set_defaults(func=cmd_luna_reserve_events)

    luna_read = session_parser("read", "the latest final message, or the last N thread items")
    luna_read.add_argument("session")
    luna_read.add_argument("--items", type=int, default=0, help="show the last N items (at most 50)")
    luna_read.add_argument("--lines", type=_positive, default=20, help="final-message lines (at most 60)")
    luna_read.set_defaults(func=cmd_luna_reserve_read)

    luna_list = session_parser("list", "recorded sessions and the broker")
    luna_list.add_argument("--limit", type=_positive, default=10)
    luna_list.set_defaults(func=cmd_luna_reserve_list)

    luna_shutdown = session_parser("shutdown", "stop every session and the broker")
    luna_shutdown.set_defaults(func=cmd_luna_reserve_shutdown)

    luna_serve = luna_commands.add_parser("broker-serve", help="internal: run the broker (started by clients)")
    luna_serve.add_argument("--instance", required=True)
    luna_serve.set_defaults(func=cmd_luna_reserve_broker_serve)
    return root


def main(argv: Optional[list[str]] = None) -> int:
    built = parser()
    arguments, extra = built.parse_known_args(argv)
    if extra:
        if not getattr(arguments, "collects_extra_overrides", False):
            built.error("unrecognized arguments: " + " ".join(extra))
        arguments.overrides = list(arguments.overrides or []) + extra
    return int(arguments.func(arguments))
