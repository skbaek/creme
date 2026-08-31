from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path


WRAPPER_COMMANDS = (
    ("codex-host-semaphore", "semaphore"),
    ("codex-host-telemetry", "telemetry"),
    ("codex-reclaim-lean", "reclaim"),
)


def default_output_dir() -> Path:
    return Path.home() / ".codex" / "bin"


def render_host_wrappers(creme_root: Path) -> dict[str, str]:
    launcher = creme_root.resolve() / "scripts" / "creme"
    quoted_launcher = shlex.quote(str(launcher))
    return {
        name: (
            "#!/bin/sh\n"
            "set -eu\n"
            f"exec {quoted_launcher} {shlex.quote(command)} \"$@\"\n"
        )
        for name, command in WRAPPER_COMMANDS
    }


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def install_host_wrappers(
    creme_root: Path,
    output_dir: Path,
    *,
    replace: bool,
) -> list[Path]:
    rendered = render_host_wrappers(creme_root)
    targets = [output_dir / name for name in rendered]
    blockers = [path for path in targets if _lexists(path)]
    if blockers and not replace:
        joined = ", ".join(str(path) for path in blockers)
        raise FileExistsError(f"wrapper output exists: {joined}; use --replace after review")
    directories = [
        path for path in blockers
        if path.is_dir() and not path.is_symlink()
    ]
    if directories:
        joined = ", ".join(str(path) for path in directories)
        raise IsADirectoryError(f"refusing to replace wrapper directories: {joined}")

    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise NotADirectoryError(f"wrapper output is not a directory: {output_dir}")

    staged: list[tuple[str, Path]] = []
    try:
        for name, content in rendered.items():
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{name}.tmp.",
                dir=str(output_dir),
                text=True,
            )
            temporary_path = Path(temporary)
            staged.append((name, temporary_path))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o700)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

        for name, temporary_path in staged:
            os.replace(temporary_path, output_dir / name)
        return targets
    finally:
        for _, temporary_path in staged:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def wrapper_install_issues(
    creme_root: Path,
    output_dir: Path,
) -> list[str]:
    rendered = render_host_wrappers(creme_root)
    issues: list[str] = []
    for name, expected in rendered.items():
        path = output_dir / name
        if not _lexists(path):
            issues.append(f"{name}: missing")
            continue
        if path.is_symlink():
            issues.append(f"{name}: symbolic link")
            continue
        if not path.is_file():
            issues.append(f"{name}: not a regular file")
            continue
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            issues.append(f"{name}: unreadable ({exc})")
            continue
        if actual != expected:
            issues.append(f"{name}: content mismatch")
        if not os.access(path, os.X_OK):
            issues.append(f"{name}: not executable")
    return issues
