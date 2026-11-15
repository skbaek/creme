"""Write-once channel branches of the goal store's origin.

A channel is a branch that shares no history with the goal store's default
branch, checked out as a worktree at ``$GOAL_STORE/<directory>`` and ignored
through that clone's ``.git/info/exclude``. Hosts only ever add files at
distinct paths, so a rejected push is resolved by rebasing the never-published
local commit onto the new head, which cannot conflict, and pushing again a
bounded number of times. Publication never forces.

Host comms (``host-comms``) and commit attribution (``attribution``) are the
two channels. Each names itself with a :class:`Spec`; every refusal raises
the spec's own error class so callers keep their public exception types.
Git plumbing comes from :mod:`creme.goal_stack_git`.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import goal_stack_git as shared

MAX_PUBLISH_ATTEMPTS = 6


@dataclass(frozen=True)
class Spec:
    """One channel: where it lives, what it is called, how it refuses."""

    directory: str
    branch: str
    lock_name: str
    readme: bytes
    opening: str
    remedy: str
    error: type
    remote: str = shared.DEFAULT_REMOTE
    # When set, fetch and push name ``remote`` and the branch explicitly and
    # never follow the branch's configured upstream. ``remote`` may then be a
    # literal URL; ``tracking_remote`` names the remote-tracking ref and the
    # upstream recorded on attach (default: ``remote``).
    explicit_remote: bool = False
    tracking_remote: Optional[str] = None
    # Untracked paths this predicate accepts never block a write (they are
    # never committed either: publication adds only its own files).
    untracked_ok: Optional[Callable[[str], bool]] = None
    # Extra patterns added to the clone's ``info/exclude`` on attach.
    extra_excludes: tuple = ()


def _tracking_name(spec: Spec, remote: str) -> str:
    return spec.tracking_remote or remote


def worktree_path(root: Path, spec: Spec) -> Path:
    return Path(root) / spec.directory


@contextlib.contextmanager
def lock(root: Path, spec: Spec) -> Iterator[None]:
    """Serialize this host's git work on the channel (readers fetch too)."""
    common = Path(shared.out(root, "rev-parse", "--git-common-dir"))
    path = common if common.is_absolute() else Path(root) / common
    descriptor = os.open(path / spec.lock_name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def ignore(root: Path, spec: Spec) -> None:
    """Ignore the worktree in this clone only, so no tracked file changes."""
    exclude = Path(shared.out(root, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else Path(root) / exclude
    wanted = []
    if shared.git(root, "check-ignore", "-q", "--no-index", "--",
                  f"{spec.directory}/x", check=False).returncode != 0:
        wanted.append("/" + spec.directory + "/")
    existing = exclude.read_bytes() if exclude.exists() else b""
    lines = existing.decode("utf-8", "replace").splitlines()
    wanted += [pattern for pattern in spec.extra_excludes if pattern not in lines]
    if not wanted:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = b"" if not existing or existing.endswith(b"\n") else b"\n"
    exclude.write_bytes(existing + prefix + "".join(f"{w}\n" for w in wanted).encode())


def create_branch(root: Path, spec: Spec, remote: str, branch: str, host: str) -> bool:
    """Publish the channel's history-free first commit; False if another host won."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        tree = shared.write_tree(root, {"README.md": spec.readme}, env)
    first = shared.commit_tree(root, tree, f"{spec.opening} (host {host})")
    pushed = shared.git(root, "push", "--quiet", remote, f"{first}:refs/heads/{branch}", check=False)
    return pushed.returncode == 0


def attach(root: Path, spec: Spec, *, create: bool, host: str, remote: Optional[str] = None,
           branch: Optional[str] = None) -> Optional[Path]:
    """This host's channel worktree, attaching (or with ``create``, opening) it.

    Returns None when the channel does not exist yet and ``create`` is false.
    Call with the channel lock held.
    """
    remote = remote or spec.remote
    branch = branch or spec.branch
    root = Path(root)
    path = worktree_path(root, spec)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise spec.error(f"{path} must be a directory, not a link or file")
    if path.exists():
        current = shared.out(path, "symbolic-ref", "--short", "HEAD")
        if current != branch:
            raise spec.error(f"{spec.directory} has {current!r} checked out, not {branch!r}")
        return path
    if not shared.remote_branch_exists(root, remote, branch):
        if not create:
            return None
        if not create_branch(root, spec, remote, branch, host) and not \
                shared.remote_branch_exists(root, remote, branch):
            raise spec.error(f"could not publish {remote}/{branch}; channel not opened")
    tracking = shared.tracking_ref(_tracking_name(spec, remote), branch)
    shared.git(root, "fetch", "--quiet", remote, f"refs/heads/{branch}:{tracking}")
    canonical = shared.out(root, "rev-parse", "--verify", tracking)
    local = shared.git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if local.returncode == 0:
        if not shared.is_ancestor(root, local.stdout.decode().strip(), canonical):
            raise spec.error(f"local branch {branch!r} diverges from {remote}/{branch}")
        shared.git(root, "branch", "-f", branch, canonical)
    else:
        shared.git(root, "branch", branch, canonical)
    shared.configure_upstream(root, _tracking_name(spec, remote), branch)
    ignore(root, spec)
    shared.git(root, "worktree", "add", "--quiet", spec.directory, branch)
    return path


def rebase(worktree: Path, onto: str, spec: Spec) -> None:
    result = shared.git(worktree, "-c", "commit.gpgsign=false", "rebase", "--quiet", onto, check=False)
    if result.returncode:
        shared.git(worktree, "rebase", "--abort", check=False)
        raise spec.error("could not rebase unpublished channel commits: "
                         + result.stderr.decode("utf-8", "replace").strip())


def changed_paths(worktree: Path) -> list[tuple[str, str]]:
    """Paths any commit on this copy modified or deleted after adding them."""
    listed = shared.out(worktree, "log", "--format=", "--name-status", "--no-renames",
                        "--diff-filter=MDT", "HEAD")
    problems: dict[str, str] = {}
    for line in listed.splitlines():
        status, _, path = line.partition("\t")
        if path and path not in problems:
            problems[path] = f"changed after publication (git status {status}); files are immutable"
    return sorted(problems.items())


def blocking_paths(worktree: Path, spec: Spec) -> list[str]:
    """Uncommitted paths that block a write; accepted untracked files do not."""
    status = shared.git(worktree, "status", "--porcelain", "--untracked-files=all").stdout.decode(
        "utf-8", "replace")
    found = []
    for line in status.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:]
        if code == "??" and spec.untracked_ok is not None and spec.untracked_ok(path):
            continue
        found.append(path)
    return found


def require_clean(worktree: Path, spec: Spec) -> None:
    paths = blocking_paths(worktree, spec)
    if paths:
        shown = ", ".join(paths[:5]) + (" ..." if len(paths) > 5 else "")
        raise shared.SharedStackError(
            f"{worktree.name} worktree has uncommitted or untracked paths ({shown}); {spec.remedy}")


def fetch(worktree: Path, spec: Spec) -> str:
    """The canonical branch head, fetched without force."""
    if not spec.explicit_remote:
        return shared.fetch(worktree)
    tracking = shared.tracking_ref(_tracking_name(spec, spec.remote), spec.branch)
    result = shared.git(worktree, "fetch", "--quiet", spec.remote,
                        f"refs/heads/{spec.branch}:{tracking}", check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise shared.SharedStackError(
            f"canonical {spec.branch} unreachable or rewritten "
            f"({_tracking_name(spec, spec.remote)}/{spec.branch}): {detail}")
    return shared.out(worktree, "rev-parse", "--verify", tracking)


def push(worktree: Path, spec: Spec) -> tuple[bool, str]:
    if not spec.explicit_remote:
        return shared.push(worktree)
    result = shared.git(worktree, "push", "--quiet", spec.remote,
                        f"HEAD:refs/heads/{spec.branch}", check=False)
    return result.returncode == 0, result.stderr.decode("utf-8", "replace").strip()


def sync(worktree: Path, spec: Spec) -> None:
    """Fetch, then fast-forward or rebase local-only (unpublished) commits."""
    require_clean(worktree, spec)
    remote_head = fetch(worktree, spec)
    local = shared.head(worktree)
    if local == remote_head or shared.is_ancestor(worktree, remote_head, local):
        return
    if shared.is_ancestor(worktree, local, remote_head):
        shared.git(worktree, "merge", "--ff-only", "--quiet", remote_head)
    else:
        rebase(worktree, remote_head, spec)


def reset_to_remote(worktree: Path, spec: Spec) -> str:
    """Fetch and discard local commits the origin does not have.

    For channels whose source of truth is elsewhere until publication is
    confirmed (attribution keeps its journal), an unpublished local commit
    was never canonical and is rebuilt from that source.
    """
    require_clean(worktree, spec)
    remote_head = fetch(worktree, spec)
    shared.git(worktree, "reset", "--hard", "--quiet", remote_head)
    return remote_head


def _drop_tip(worktree: Path) -> None:
    shared.git(worktree, "reset", "--hard", "--quiet", "HEAD~1")


def publish(worktree: Path, files: dict[str, bytes], message: str, spec: Spec) -> str:
    """Commit new files and push without force, rebasing on rejection.

    The new commit is always the tip; when publication fails it is dropped,
    so a refused write leaves nothing behind.
    """
    for relative in files:
        if (worktree / relative).exists() or (worktree / relative).is_symlink():
            raise spec.error(f"{relative} already exists; channel files are immutable")
    written = []
    try:
        for relative, data in files.items():
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            written.append(path)
        shared.git(worktree, "add", "--", *files)
        shared.git(worktree, "-c", "commit.gpgsign=false", "commit", "--no-verify", "--quiet",
                   "-m", message)
    except shared.SharedStackError:
        shared.git(worktree, "reset", "--quiet", check=False)
        for path in written:
            path.unlink(missing_ok=True)
        raise
    for _ in range(MAX_PUBLISH_ATTEMPTS):
        try:
            published, detail = push(worktree, spec)
        except Exception as exc:  # a timeout or crash must not strand the commit
            _drop_tip(worktree)
            raise spec.error(f"could not publish; nothing sent: {exc}") from exc
        if published:
            return shared.head(worktree)
        try:
            remote_head = fetch(worktree, spec)
        except shared.SharedStackError as exc:
            _drop_tip(worktree)
            raise spec.error(f"could not publish; nothing sent: {exc}") from exc
        if shared.is_ancestor(worktree, remote_head, "HEAD"):
            _drop_tip(worktree)
            raise spec.error(f"could not publish; nothing sent: {detail}")
        # Another host published first. Only new, distinct paths exist on
        # either side, so the rebase cannot conflict.
        try:
            rebase(worktree, remote_head, spec)
        except spec.error:
            _drop_tip(worktree)
            raise
    _drop_tip(worktree)
    raise spec.error("channel kept advancing on other hosts; nothing sent; retry")


def authorized(record_root: Path, writer, action: Callable[..., dict], spec: Spec, *,
               pass_lease: bool = False) -> dict:
    """Run ``action`` inside the master lease transaction stack writes use.

    With ``pass_lease`` the action receives the authenticated lease snapshot.
    """
    from . import master_runtime as runtime, semaphore

    writer = writer or runtime.RecordWriter(record_root)
    if writer.root != record_root:
        raise spec.error("writer and channel must use the same master record")
    runtime._renew_or_refuse(writer.renew)
    runtime._normalize_lock_prerequisites(record_root)
    with runtime._locked_record(record_root):
        try:
            with writer.authority_transaction() as lease:
                runtime._actor_from_snapshot(lease)
                return action(lease) if pass_lease else action()
        except semaphore.MasterAuthorityRefused as exc:
            raise runtime.RenewalRefused(f"master renewal refused: {exc}") from exc
