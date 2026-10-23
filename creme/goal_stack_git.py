"""Git plumbing for the one shared goal stack (schema 2).

The canonical stack lives on a stack-only branch (default ``goal-stack``) of
the goal store's origin. The branch carries only ``goal-stack.toml``, the
transition receipts under ``archive/goal-stack/transitions/`` and a
``.gitignore`` for the publication journal; it never shares history with the
goal store's default branch, so a host whose default branch carries private,
never-pushed history can publish stack revisions by pushing this branch
alone.

Each host checks the branch out as a separate worktree at
``$GOAL_STORE/.goal-stack`` (ignored on the default branch). One commit is
one revision. A write is a compare-and-swap: fetch, verify the incoming chain
is linear with exactly one bound receipt per revision, fast-forward, commit
revision N+1 on the canonical head, and push without force. A rejected push
means another host advanced the chain; the unpublished local commit is
dropped (it was never canonical) and the write is re-prepared or refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from . import goal_stack

SHARED_DIR = ".goal-stack"
DEFAULT_BRANCH = "goal-stack"
DEFAULT_REMOTE = "origin"
NAME = "goal-stack.toml"
PENDING = ".goal-stack-pending.json"
RECEIPTS = "archive/goal-stack/transitions"
BRANCH_IGNORE = "/" + PENDING + "\n"
UNPUBLISHED_PREFIX = "refs/goal-stack/unpublished/"
GIT_TIMEOUT = 120
MAX_REPLAY_WALK = 4096

# The default-branch file that marks the move. Old Creme refuses it (unknown
# keys, schema 2), so it can never misread a stale schema-1 queue.
STUB = (
    "# The canonical goal stack moved to the shared stack branch (schema 2).\n"
    "# Read it with `python3 -m creme master stack list`; never edit by hand.\n"
    "schema_version = 2\n"
    'moved_to = ".goal-stack/goal-stack.toml"\n'
)


class SharedStackError(ValueError):
    """A shared-stack git operation or chain check refused."""


def digest(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else hashlib.sha256(data).hexdigest()


def receipt_path(revision: int) -> str:
    return f"{RECEIPTS}/{revision:08d}.json"


def git(cwd: Path, *args: str, check: bool = True, input_bytes: Optional[bytes] = None,
        env: Optional[dict] = None) -> subprocess.CompletedProcess:
    environ = dict(os.environ)
    environ["GIT_TERMINAL_PROMPT"] = "0"
    environ.setdefault("GIT_ASKPASS", "true")
    if env:
        environ.update(env)
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], input=input_bytes, capture_output=True,
            timeout=GIT_TIMEOUT, env=environ, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SharedStackError(f"git {args[0]} failed: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SharedStackError(f"git {' '.join(args[:2])} failed: {detail}")
    return result


def out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.decode("utf-8", "replace").strip()


def is_stub(data: Optional[bytes]) -> bool:
    if data is None:
        return False
    try:
        parsed = goal_stack.parse(data.decode("utf-8"))
    except (ValueError, UnicodeError):
        return False
    return parsed == {"schema_version": 2, "moved_to": SHARED_DIR + "/" + NAME}


def upstream(worktree: Path) -> tuple[str, str]:
    """The (remote, branch) the stack worktree publishes to."""
    branch = out(worktree, "symbolic-ref", "--short", "HEAD")
    remote = git(worktree, "config", f"branch.{branch}.remote", check=False)
    merge = git(worktree, "config", f"branch.{branch}.merge", check=False)
    remote_name = remote.stdout.decode().strip()
    merge_ref = merge.stdout.decode().strip()
    if remote.returncode or merge.returncode or not remote_name or merge_ref != f"refs/heads/{branch}":
        raise SharedStackError(
            f"shared branch {branch!r} has no canonical upstream; refusing to fork it")
    return remote_name, branch


def tracking_ref(remote: str, branch: str) -> str:
    return f"refs/remotes/{remote}/{branch}"


def show(worktree: Path, revision: str, path: str) -> Optional[bytes]:
    result = git(worktree, "show", f"{revision}:{path}", check=False)
    return None if result.returncode else result.stdout


def head(worktree: Path) -> str:
    return out(worktree, "rev-parse", "--verify", "HEAD")


def is_ancestor(worktree: Path, older: str, newer: str) -> bool:
    return git(worktree, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def dirty_paths(worktree: Path) -> list[str]:
    status = git(worktree, "status", "--porcelain", "--untracked-files=all").stdout.decode(
        "utf-8", "replace")
    return [line[3:] for line in status.splitlines() if line.strip()]


def clean(worktree: Path) -> bool:
    return not dirty_paths(worktree)


def require_clean(worktree: Path, remedy: str = (
        "master stack recover, or move them out of " + SHARED_DIR + "/")) -> None:
    paths = dirty_paths(worktree)
    if paths:
        shown = ", ".join(paths[:5]) + (" ..." if len(paths) > 5 else "")
        raise SharedStackError(
            f"{worktree.name} worktree has uncommitted or untracked paths ({shown}); {remedy}")


def fetch(worktree: Path) -> str:
    """Fetch the canonical branch without force; return its commit."""
    remote, branch = upstream(worktree)
    # No leading '+': a rewritten canonical history refuses to fetch.
    result = git(worktree, "fetch", "--quiet", remote,
                 f"refs/heads/{branch}:{tracking_ref(remote, branch)}", check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SharedStackError(f"canonical {branch} unreachable or rewritten ({remote}/{branch}): {detail}")
    return out(worktree, "rev-parse", "--verify", tracking_ref(remote, branch))


def _receipt(worktree: Path, commit: str, revision: int) -> dict:
    data = show(worktree, commit, receipt_path(revision))
    if data is None:
        raise SharedStackError(f"stack commit {commit[:12]} lacks receipt {revision:08d}")
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise SharedStackError(f"stack receipt {revision:08d} is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SharedStackError(f"stack receipt {revision:08d} is not an object")
    return value


def manifest_at(worktree: Path, commit: str) -> tuple[bytes, dict]:
    data = show(worktree, commit, NAME)
    if data is None:
        raise SharedStackError(f"stack commit {commit[:12]} has no {NAME}")
    try:
        stack = goal_stack.parse(data.decode("utf-8"))
        goal_stack.check_structure(stack)
    except (ValueError, UnicodeError) as exc:
        raise SharedStackError(f"stack commit {commit[:12]} is malformed: {exc}") from exc
    return data, stack


def verify_chain(worktree: Path, older: str, newer: str) -> list[dict]:
    """Check ``older..newer`` is one linear chain of bound revisions.

    Every commit has exactly one parent (no merges), advances the manifest
    revision by exactly one, adds its receipt, and the receipt binds the
    previous and new manifest digests. Returns the new receipts in order.
    """
    rows = out(worktree, "rev-list", "--reverse", "--parents", f"{older}..{newer}")
    previous_commit = older
    previous_bytes, previous = manifest_at(worktree, older)
    receipts = []
    for line in rows.splitlines():
        parts = line.split()
        commit, parents = parts[0], parts[1:]
        if parents != [previous_commit]:
            raise SharedStackError(
                f"canonical stack is not one linear chain at {commit[:12]}; refusing")
        data, stack = manifest_at(worktree, commit)
        revision = previous["revision"] + 1
        if stack["revision"] != revision:
            raise SharedStackError(
                f"canonical stack commit {commit[:12]} skips or repeats a revision")
        receipt = _receipt(worktree, commit, revision)
        if (receipt.get("revision") != revision
                or receipt.get("before_sha256") != digest(previous_bytes)
                or receipt.get("after_sha256") != digest(data)):
            raise SharedStackError(f"canonical receipt {revision:08d} is not bound to its revision")
        if show(worktree, previous_commit, receipt_path(revision)) is not None:
            raise SharedStackError(f"canonical receipt {revision:08d} existed before its revision")
        receipts.append(receipt)
        previous_commit, previous_bytes, previous = commit, data, stack
    return receipts


def sync(worktree: Path) -> list[dict]:
    """Fetch and fast-forward the stack worktree; return incoming receipts.

    Refuses (never merges) when local commits are not on the canonical
    branch: those are unpublished writes that `master stack sync
    --preserve-unpublished` sets aside explicitly.
    """
    require_clean(worktree)
    remote_head = fetch(worktree)
    local = head(worktree)
    if local == remote_head:
        return []
    if is_ancestor(worktree, local, remote_head):
        receipts = verify_chain(worktree, local, remote_head)
        git(worktree, "merge", "--ff-only", "--quiet", remote_head)
        return receipts
    raise SharedStackError(
        "local stack has unpublished revisions not on the canonical branch; "
        "run `python3 -m creme master stack sync --preserve-unpublished`")


def preserve_unpublished(worktree: Path) -> Optional[str]:
    """Move local-only commits to a preservation ref and reset to canonical."""
    require_clean(worktree)
    remote_head = fetch(worktree)
    local = head(worktree)
    if local == remote_head or is_ancestor(worktree, local, remote_head):
        return None
    # The canonical extension past the last commit both sides share must be
    # verified exactly as a fast-forward would be before it is installed.
    base = git(worktree, "merge-base", local, remote_head, check=False).stdout.decode().strip()
    if not base:
        raise SharedStackError("local stack shares no history with the canonical branch; refusing")
    verify_chain(worktree, base, remote_head)
    ref = preservation_ref(local)
    git(worktree, "update-ref", ref, local)
    git(worktree, "reset", "--hard", "--quiet", remote_head)
    return ref


def remote_branch_exists(root: Path, remote: str, branch: str) -> bool:
    result = git(root, "ls-remote", "--exit-code", "--heads", remote,
                 f"refs/heads/{branch}", check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise SharedStackError(f"cannot reach {remote}: {result.stderr.decode('utf-8', 'replace').strip()}")


def configure_upstream(root: Path, remote: str, branch: str) -> None:
    git(root, "config", f"branch.{branch}.remote", remote)
    git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")


def write_tree(root: Path, files: dict[str, bytes], env: dict) -> str:
    """A tree of exactly ``files``, built in the index named by ``env``."""
    git(root, "read-tree", "--empty", env=env)
    for path, content in sorted(files.items()):
        blob = git(root, "hash-object", "-w", "--stdin", input_bytes=content).stdout.decode().strip()
        git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
    return git(root, "write-tree", env=env).stdout.decode().strip()


def commit_tree(root: Path, tree: str, message: str, parent: Optional[str] = None) -> str:
    args = ["-c", "commit.gpgsign=false", "commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return git(root, *args).stdout.decode().strip()


def preservation_ref(commit_id: str) -> str:
    """Full-hash ref, so distinct superseded commits never overwrite each other."""
    return f"{UNPUBLISHED_PREFIX}{commit_id}"


def commit(worktree: Path, revision: int, message: str) -> str:
    git(worktree, "add", "--", NAME, receipt_path(revision))
    git(worktree, "-c", "commit.gpgsign=false", "commit", "--no-verify", "--quiet",
        "-m", message)
    require_clean(worktree)
    return head(worktree)


def push(worktree: Path) -> tuple[bool, str]:
    remote, branch = upstream(worktree)
    result = git(worktree, "push", "--quiet", remote, f"HEAD:refs/heads/{branch}", check=False)
    return result.returncode == 0, result.stderr.decode("utf-8", "replace").strip()


def stack_at_revision(worktree: Path, revision: int) -> Optional[dict]:
    """The committed stack at ``revision`` on the current chain, if present."""
    commits = out(worktree, "rev-list", "--first-parent", f"--max-count={MAX_REPLAY_WALK}", "HEAD")
    for commit_id in commits.splitlines():
        try:
            _, stack = manifest_at(worktree, commit_id)
        except SharedStackError:
            return None
        if stack["revision"] == revision:
            return stack
        if stack["revision"] < revision:
            return None
    return None


def receipt_hosts(worktree: Path, after: int, through: int) -> list[str]:
    """Hosts that wrote revisions ``after+1..through`` (from receipt actors)."""
    hosts: list[str] = []
    for revision in range(after + 1, through + 1):
        path = worktree / receipt_path(revision)
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        actor = receipt.get("actor") if isinstance(receipt, dict) else None
        name = actor.get("host") if isinstance(actor, dict) else None
        label = name if isinstance(name, str) else "unknown"
        if label not in hosts:
            hosts.append(label)
    return hosts
