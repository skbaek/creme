from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .adapters import get_adapter
from .doctor import exit_code as doctor_exit_code
from .doctor import run_doctor
from .guidance import default_path as default_guidance_path
from .guidance import load as load_guidance
from .host_wrappers import (
    default_output_dir as default_host_wrapper_output_dir,
    install_host_wrappers,
    render_host_wrappers,
)
from .profile import DEFAULT_RELATIVE_PROFILE, load, propose, write_reviewed
from . import idle_workers
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


def cmd_semaphore(arguments: argparse.Namespace) -> int:
    action = arguments.action
    if action == "status":
        print(semaphore.status_text())
        return 0
    if action in {"soft-acquire", "hard-acquire"}:
        kind = action.split("-", 1)[0]
        return _sem_result(*semaphore.acquire(
            kind,
            arguments.label,
            arguments.note,
            arguments.lease,
            memory_gib=arguments.memory_gib,
        ))
    if action == "adaptive-acquire":
        return _sem_result(*semaphore.adaptive_acquire(
            arguments.label,
            arguments.note,
            arguments.lease,
            memory_gib=arguments.memory_gib,
            contention=arguments.contention,
            wait_seconds=arguments.wait,
            # A queued request blocks the caller's turn, so the arithmetic that
            # decides it is printed before the wait begins, not after it fails.
            announce=(print if arguments.wait is not None else None),
        ))
    if action in {"soft-release", "hard-release"}:
        kind = action.split("-", 1)[0]
        return _sem_result(*semaphore.release(kind, arguments.label))
    if action == "release":
        return _sem_result(*semaphore.adaptive_release(arguments.label))
    if action == "renew":
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
            if arguments.detach:
                if arguments.heartbeat_lease_id is not None:
                    return _sem_result(
                        False,
                        "--heartbeat-lease-id is internal to the detached child",
                    )
                return _sem_result(*semaphore.master_heartbeat_detached(arguments.heartbeat))
            return _sem_result(*semaphore.master_heartbeat(
                arguments.heartbeat,
                expected_lease_id=arguments.heartbeat_lease_id,
            ))
        if arguments.heartbeat_lease_id is not None:
            return _sem_result(False, "--heartbeat-lease-id requires --heartbeat")
        return _sem_result(*semaphore.master_renew(arguments.lease))
    if action == "master-release":
        return _sem_result(*semaphore.master_release(
            force=arguments.force, reason=arguments.reason,
        ))
    return _sem_result(False, f"unknown action: {action}")


def render_codex_profile(workspace: Path) -> str:
    creme = workspace / "creme"
    jaune = workspace / "jaune"
    blanc = workspace / "blanc"
    roots = [creme, jaune, blanc]
    lines = [
        'default_permissions = "creme-relay"',
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
    rendered = render_codex_profile(workspace)
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
        Path(arguments.output_dir).expanduser().resolve()
        if arguments.output_dir
        else default_host_wrapper_output_dir().resolve()
    )
    rendered = render_host_wrappers(ROOT)
    if not arguments.write:
        _json({
            "status": "PREVIEW",
            "detail": "review these delegates, then rerun with --write",
            "creme_root": str(ROOT),
            "output_dir": str(output),
            "wrappers": {
                str(output / name): content for name, content in rendered.items()
            },
        })
        return 0
    if not arguments.output_dir:
        _json({
            "status": "REFUSED",
            "detail": "--output-dir is required with --write; Creme never chooses or overwrites user executables implicitly",
        })
        return 1
    try:
        written = install_host_wrappers(ROOT, output, replace=arguments.replace)
    except OSError as exc:
        _json({"status": "REFUSED", "detail": str(exc)})
        return 1
    _json({
        "status": "OK",
        "detail": "reviewed host capability delegates written",
        "creme_root": str(ROOT),
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
    options.add_argument("--probe", action="store_true")
    options.add_argument(
        "--census",
        action="store_true",
        help="update one Git-pinned dependency and rebuild the full target, exclusively, in a GOAL-rehearsal worktree",
    )
    options.add_argument("--dependency", metavar="NAME")
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
        threads=DEFAULT_THREADS,
        probe=selected.probe,
        wait_seconds=selected.wait,
        census=selected.census,
        dependency=selected.dependency,
    )


def cmd_build_ledger(arguments: argparse.Namespace) -> int:
    try:
        _json(ledger_rollup(arguments.since, arguments.until))
    except ValueError as exc:
        _json({"status": "REFUSED", "detail": str(exc)})
        return 2
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
    for name in ("soft-acquire", "hard-acquire"):
        item = sem_commands.add_parser(name)
        item.add_argument("label")
        item.add_argument("--note", required=True)
        item.add_argument("--lease", type=int, default=semaphore.DEFAULT_LEASE_SECONDS)
        item.add_argument(
            "--memory-gib",
            type=_positive,
            help="conservative whole-GiB peak estimate; defaults to the host policy",
        )
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
    for name in ("soft-release", "hard-release"):
        item = sem_commands.add_parser(name)
        item.add_argument("label")
    adaptive_release = sem_commands.add_parser(
        "release",
        help="release whichever hold kind adaptive acquisition selected",
    )
    adaptive_release.add_argument("label")
    renew = sem_commands.add_parser("renew")
    renew.add_argument("label")
    renew.add_argument("--lease", type=int, default=semaphore.DEFAULT_LEASE_SECONDS)
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
        help="claude, codex, or human; detected from the process ancestry when omitted",
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
    master_renew.add_argument(
        "--heartbeat-lease-id",
        help=argparse.SUPPRESS,
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
    client.add_argument("--output")
    client.add_argument("--write", action="store_true")
    client.add_argument("--replace", action="store_true")
    client.set_defaults(func=cmd_client_profile)

    wrappers = commands.add_parser(
        "host-wrappers",
        help="preview or install stable Codex delegates for telemetry and reclamation",
    )
    wrappers.add_argument("--output-dir")
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
    return root


def main(argv: Optional[list[str]] = None) -> int:
    arguments = parser().parse_args(argv)
    return int(arguments.func(arguments))
