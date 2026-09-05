from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


BROKER_NAME = "codex-creme-contained-build"
PREFLIGHT_RELATIVE = Path(".creme/bin/lean-host-preflight")


def _git_output(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"cannot identify canonical Creme revision: {detail}")
    return completed.stdout.strip()


def broker_inputs(creme_root: Path) -> tuple[str, str, str] | None:
    preflight = creme_root / PREFLIGHT_RELATIVE
    if not preflight.exists():
        return None
    if preflight.is_symlink() or not preflight.is_file() or not os.access(preflight, os.X_OK):
        raise RuntimeError(f"contained-build preflight is not a regular file: {preflight}")
    launcher_entry = _git_output(creme_root, "ls-tree", "HEAD", "--", "scripts/creme")
    runtime_tree = _git_output(creme_root, "rev-parse", "HEAD:creme")
    digest = hashlib.sha256(preflight.read_bytes()).hexdigest()
    return launcher_entry, runtime_tree, digest


def render_contained_build_broker(
    creme_root: Path,
    launcher_entry: str,
    runtime_tree: str,
    preflight_sha256: str,
) -> str:
    root = str(creme_root.resolve())
    parent = str(creme_root.resolve().parent)
    return f'''#!/usr/bin/python3 -I
"""Generated least-privilege host broker for contained Creme Lake builds."""

from __future__ import annotations

import hashlib
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys

CREME_ROOT = Path({json.dumps(root)})
WORKSPACE = Path({json.dumps(parent)})
EXPECTED_LAUNCHER_ENTRY = {json.dumps(launcher_entry)}
EXPECTED_RUNTIME_TREE = {json.dumps(runtime_tree)}
PREFLIGHT = CREME_ROOT / {json.dumps(str(PREFLIGHT_RELATIVE))}
EXPECTED_PREFLIGHT_SHA256 = {json.dumps(preflight_sha256)}
GIT = Path("/usr/bin/git")
SYSTEMD_RUN = Path("/usr/bin/systemd-run")
CREME = CREME_ROOT / "scripts/creme"
BROKER_STATE = Path(__file__).resolve().parent.parent / "state" / "creme-build-broker"
GOAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}")
TARGET_RE = re.compile(r"\\+?[A-Za-z0-9_][A-Za-z0-9_./-]{{0,255}}")
PURPOSE_SUFFIX = {{
    "goal": "",
    "control": "-control",
    "mutation": "-mutation",
    "rehearsal": "-rehearsal",
}}


def refuse(detail: str) -> "NoReturn":
    print(f"REFUSED — {{detail}}", file=sys.stderr)
    raise SystemExit(2)


def run_text(arguments: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        arguments, cwd=cwd, capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        refuse(completed.stderr.strip() or completed.stdout.strip() or "identity check failed")
    return completed.stdout.strip()


def regular_path(path: Path, label: str, *, executable: bool = False) -> None:
    if path.is_symlink() or not path.is_file():
        refuse(f"{{label}} is not a regular file: {{path}}")
    if executable and not os.access(path, os.X_OK):
        refuse(f"{{label}} is not executable: {{path}}")


def require_control_plane() -> None:
    regular_path(GIT, "git", executable=True)
    regular_path(SYSTEMD_RUN, "systemd-run", executable=True)
    regular_path(CREME, "Creme launcher", executable=True)
    regular_path(PREFLIGHT, "host preflight", executable=True)
    if hashlib.sha256(PREFLIGHT.read_bytes()).hexdigest() != EXPECTED_PREFLIGHT_SHA256:
        refuse("host preflight changed; review and reinstall the Codex capability bundle")
    launcher_entry = run_text(
        [str(GIT), "ls-tree", "HEAD", "--", "scripts/creme"], cwd=CREME_ROOT,
    )
    runtime_tree = run_text([str(GIT), "rev-parse", "HEAD:creme"], cwd=CREME_ROOT)
    if launcher_entry != EXPECTED_LAUNCHER_ENTRY or runtime_tree != EXPECTED_RUNTIME_TREE:
        refuse("canonical Creme runtime changed; review and reinstall the Codex capability bundle")
    for arguments in (
        [str(GIT), "diff", "--quiet", "HEAD", "--", "scripts/creme", "creme"],
        [str(GIT), "diff", "--quiet", "--cached", "HEAD", "--", "scripts/creme", "creme"],
    ):
        completed = subprocess.run(arguments, cwd=CREME_ROOT, check=False)
        if completed.returncode != 0:
            refuse("canonical Creme runtime is modified; commit, review, and reinstall first")
    untracked = run_text(
        [str(GIT), "status", "--porcelain", "--untracked-files=all", "--", "scripts/creme", "creme"],
        cwd=CREME_ROOT,
    )
    if untracked:
        refuse("canonical Creme runtime contains unreviewed files")


def safe_component_path(path: Path, base: Path) -> None:
    current = base
    if current.is_symlink() or not current.is_dir():
        refuse(f"invalid canonical repository root: {{current}}")
    for part in path.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            refuse(f"worktree path contains a symbolic link: {{current}}")


def require_worktree(profile: str, goal: str, purpose: str) -> Path:
    repository = WORKSPACE / profile
    repo = repository / ".worktrees" / f"{{goal}}{{PURPOSE_SUFFIX[purpose]}}"
    safe_component_path(repo, repository)
    if not repo.is_dir():
        refuse(f"registered goal worktree is missing: {{repo}}")
    top = Path(run_text([str(GIT), "rev-parse", "--show-toplevel"], cwd=repo)).resolve()
    if top != repo.resolve():
        refuse("derived path is not the exact Git worktree root")
    common = Path(run_text([str(GIT), "rev-parse", "--git-common-dir"], cwd=repo))
    if not common.is_absolute():
        common = (repo / common).resolve()
    if common.resolve() != (repository / ".git").resolve():
        refuse("worktree does not belong to the expected repository")
    project = "Jaune" if profile == "jaune" else "Blanc"
    regular_path(repo / "lakefile.lean", "Lake configuration")
    if not (repo / project).is_dir():
        refuse(f"worktree does not match profile {{profile}}")
    return repo


def parse(arguments: list[str]) -> tuple[str, str, str, bool, int | None, bool, list[str]]:
    if len(arguments) < 2:
        refuse("usage: codex-creme-contained-build PROFILE GOAL [options] -- [TARGET ...]")
    profile, goal, *rest = arguments
    if profile not in {{"jaune", "blanc"}}:
        refuse("PROFILE must be jaune or blanc")
    if GOAL_RE.fullmatch(goal) is None or goal in {{".", ".."}}:
        refuse("invalid GOAL")
    purpose = "goal"
    probe = False
    wait = None
    exclusive = False
    index = 0
    while index < len(rest) and rest[index] != "--":
        option = rest[index]
        if option == "--purpose" and index + 1 < len(rest):
            purpose = rest[index + 1]
            index += 2
        elif option == "--probe":
            probe = True
            index += 1
        elif option == "--wait" and index + 1 < len(rest):
            try:
                wait = int(rest[index + 1])
            except ValueError:
                refuse("--wait must be an integer")
            index += 2
        elif option == "--exclusive":
            exclusive = True
            index += 1
        else:
            refuse(f"unsupported broker option: {{option}}")
    if index >= len(rest) or rest[index] != "--":
        refuse("a literal -- must precede build targets")
    targets = rest[index + 1:]
    if purpose not in PURPOSE_SUFFIX:
        refuse("--purpose must be goal, control, mutation, or rehearsal")
    if wait is not None and not 1 <= wait <= 14400:
        refuse("--wait must be between 1 and 14400 seconds")
    if probe and wait is not None:
        refuse("--wait is meaningless with --probe")
    if len(targets) > 64:
        refuse("at most 64 build targets are accepted")
    for target in targets:
        if TARGET_RE.fullmatch(target) is None:
            refuse(f"invalid build target: {{target!r}}")
        plain = target[1:] if target.startswith("+") else target
        if any(part in {{"", ".", ".."}} for part in plain.split("/")):
            refuse(f"invalid build target path: {{target!r}}")
    return profile, goal, purpose, probe, wait, exclusive, targets


def cgroup_value(directory: Path, name: str) -> str:
    try:
        return (directory / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        refuse(f"cannot inspect cgroup {{name}}: {{exc}}")


def require_containment(profile: str) -> None:
    try:
        relative = next(
            line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0:")
        )
    except (OSError, StopIteration):
        refuse("cgroup v2 membership is unavailable")
    if "/creme.slice/creme-lean.slice/" not in relative:
        refuse(f"contained stage is outside the dedicated Lean slice: {{relative}}")
    directory = Path("/sys/fs/cgroup") / relative.lstrip("/")
    expected_swap = "0" if profile == "blanc" else str(1024 ** 3)
    expected = {{
        "memory.high": "max",
        "memory.max": str(8 * 1024 ** 3),
        "memory.swap.max": expected_swap,
        "memory.oom.group": "1",
    }}
    for name, value in expected.items():
        actual = cgroup_value(directory, name)
        if actual != value:
            refuse(f"contained cgroup {{name}}={{actual}}, expected {{value}}")


def build_command(goal: str, probe: bool, wait: int | None, exclusive: bool, targets: list[str]) -> list[str]:
    command = [str(CREME), "lake-build", goal]
    if probe:
        command.append("--probe")
    if wait is not None:
        command.extend(["--wait", str(wait)])
    if exclusive:
        command.extend(["--contention", "exclusive"])
    command.append("--")
    command.extend(targets)
    return command


def provision_state_parent(state_parent: Path) -> None:
    try:
        state_parent.lstat()
        return
    except FileNotFoundError:
        pass
    except OSError as exc:
        refuse(f"cannot inspect Codex state parent: {{exc}}")
    descriptor = None
    try:
        # Only create the exact missing child of an existing private root.
        # A directory descriptor anchors mkdir without following a root link.
        descriptor = os.open(state_parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_stat = os.fstat(descriptor)
        if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o077:
            refuse("Codex root must be private and owned by this user before creating state")
        try:
            os.mkdir(state_parent.name, mode=0o700, dir_fd=descriptor)
        except FileExistsError:
            pass  # Another startup won; broker_lock validates its result.
    except OSError as exc:
        refuse(f"cannot provision Codex state parent: {{exc}}")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def broker_lock() -> int:
    state_parent = BROKER_STATE.parent
    provision_state_parent(state_parent)
    if state_parent.is_symlink() or not state_parent.is_dir():
        refuse("Codex state parent is not a regular directory")
    parent_stat = state_parent.stat()
    if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022:
        refuse("Codex state parent must be owned by this user and not group/other writable")
    BROKER_STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if BROKER_STATE.is_symlink() or not BROKER_STATE.is_dir():
        refuse("broker state path is not a regular directory")
    state_stat = BROKER_STATE.stat()
    if state_stat.st_uid != os.getuid():
        refuse("broker state path is not owned by this user")
    os.chmod(BROKER_STATE, 0o700)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(BROKER_STATE / "active.lock", flags, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        refuse(f"another contained-build broker is active or the lock is unsafe: {{exc}}")
    return descriptor


def main(arguments: list[str]) -> int:
    contained = bool(arguments and arguments[0] == "--contained")
    if contained:
        arguments = arguments[1:]
    profile, goal, purpose, probe, wait, exclusive, targets = parse(arguments)
    require_control_plane()
    repo = require_worktree(profile, goal, purpose)
    command = build_command(goal, probe, wait, exclusive, targets)
    if contained:
        require_containment(profile)
        os.chdir(repo)
        os.execv(command[0], command)
        refuse("exec returned unexpectedly")

    lock = broker_lock()
    try:
        try:
            completed = subprocess.run([str(PREFLIGHT)], check=False)
        except OSError as exc:
            refuse(f"cannot run host preflight: {{exc}}")
        if completed.returncode != 0:
            return completed.returncode
        swap = "0" if profile == "blanc" else "1G"
        outer = [
            str(SYSTEMD_RUN), "--user", "--wait", "--collect", "--pipe", "--quiet",
            "--slice=creme-lean.slice",
            "--property=MemoryAccounting=yes",
            "--property=MemoryHigh=infinity",
            "--property=MemoryMax=8G",
            f"--property=MemorySwapMax={{swap}}",
            "--property=OOMScoreAdjust=500",
            "--property=OOMPolicy=kill",
            str(Path(__file__).resolve()), "--contained", *arguments,
        ]
        try:
            return subprocess.run(outer, check=False).returncode
        except OSError as exc:
            refuse(f"cannot enter the contained Lean cgroup: {{exc}}")
    finally:
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''
