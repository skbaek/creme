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
from .host_wrappers import (
    default_output_dir as default_host_wrapper_output_dir,
    install_host_wrappers,
    render_host_wrappers,
)
from .profile import DEFAULT_RELATIVE_PROFILE, load, propose, write_reviewed
from . import semaphore


ROOT = Path(__file__).resolve().parents[1]


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
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
    return Path(text).expanduser().resolve() if text else ROOT / DEFAULT_RELATIVE_PROFILE


def cmd_platform(arguments: argparse.Namespace) -> int:
    adapter = get_adapter()
    facts = adapter.static_facts()
    _json({
        "adapter": adapter.system,
        "status": facts.status,
        "detail": facts.detail,
        "facts": facts.data,
        "optional_capabilities": list(adapter.optional_capabilities),
    })
    return 0 if facts.status == "OK" else 1


def cmd_init(arguments: argparse.Namespace) -> int:
    adapter = get_adapter()
    try:
        candidate = propose(
            ROOT,
            Path(arguments.workspace_root).expanduser() if arguments.workspace_root else None,
            adapter,
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


def cmd_reclaim(arguments: argparse.Namespace) -> int:
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
        return _sem_result(*semaphore.acquire(kind, arguments.label, arguments.note, arguments.lease))
    if action in {"soft-release", "hard-release"}:
        kind = action.split("-", 1)[0]
        return _sem_result(*semaphore.release(kind, arguments.label))
    if action == "renew":
        return _sem_result(*semaphore.renew(arguments.label, arguments.lease))
    if action == "break":
        return _sem_result(*semaphore.break_expired(arguments.label, arguments.reason))
    if action == "manual-acquire":
        return _sem_result(*semaphore.manual_acquire(arguments.note))
    if action == "manual-release":
        return _sem_result(*semaphore.manual_release())
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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python3 -m creme")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    platform_parser = commands.add_parser("platform", help="report the selected OS adapter and static facts")
    platform_parser.set_defaults(func=cmd_platform)

    init_parser = commands.add_parser("init", help="preview or write the ignored host profile")
    init_parser.add_argument("--profile")
    init_parser.add_argument("--workspace-root")
    init_parser.add_argument("--write", action="store_true")
    init_parser.add_argument("--replace", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    validate = commands.add_parser("validate-profile")
    validate.add_argument("--profile")
    validate.set_defaults(func=cmd_validate_profile)

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
    reclaim.set_defaults(func=cmd_reclaim)

    sem = commands.add_parser("semaphore", help="atomic cross-session host coordination")
    sem_commands = sem.add_subparsers(dest="action", required=True)
    sem_commands.add_parser("status")
    for name in ("soft-acquire", "hard-acquire"):
        item = sem_commands.add_parser(name)
        item.add_argument("label")
        item.add_argument("--note", required=True)
        item.add_argument("--lease", type=int, default=semaphore.DEFAULT_LEASE_SECONDS)
    for name in ("soft-release", "hard-release"):
        item = sem_commands.add_parser(name)
        item.add_argument("label")
    renew = sem_commands.add_parser("renew")
    renew.add_argument("label")
    renew.add_argument("--lease", type=int, default=semaphore.DEFAULT_LEASE_SECONDS)
    breaking = sem_commands.add_parser("break")
    breaking.add_argument("label")
    breaking.add_argument("--reason", required=True)
    manual = sem_commands.add_parser("manual-acquire")
    manual.add_argument("--note", default="human using another macOS account")
    sem_commands.add_parser("manual-release")
    sem.set_defaults(func=cmd_semaphore)

    client = commands.add_parser("client-profile", help="preview a machine-local Codex sibling-access profile")
    client.add_argument("--workspace-root")
    client.add_argument("--output")
    client.add_argument("--write", action="store_true")
    client.add_argument("--replace", action="store_true")
    client.set_defaults(func=cmd_client_profile)

    wrappers = commands.add_parser(
        "host-wrappers",
        help="preview or install stable Codex delegates for Creme host capabilities",
    )
    wrappers.add_argument("--output-dir")
    wrappers.add_argument("--write", action="store_true")
    wrappers.add_argument("--replace", action="store_true")
    wrappers.set_defaults(func=cmd_host_wrappers)
    return root


def main(argv: Optional[list[str]] = None) -> int:
    arguments = parser().parse_args(argv)
    return int(arguments.func(arguments))
