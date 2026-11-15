"""Git hook entry point: record a new commit in the host-local journal.

Installed hooks run this file directly (``python3 -I -S PATH``), never through
the ``creme`` package, so it imports only the standard library and must stay
valid on the oldest interpreter a hook may find (Python 3.9: no ``match``, no
``X | Y`` annotations evaluated at run time, no ``tomllib``).

The hook runs after the commit exists. It never changes the commit, never
uses the network and always exits 0; any error goes to the host-local
``hook.log`` beside the journal, and the census finds what was missed.

One journal record is one write-once JSON file under
``<git-common-dir>/creme-attribution/journal/``. The record names the host
(from the host profile), the master lease held on the host at that moment as
its ``acquisition_digest`` (the same digest master events carry; ``none``
without a lease), the repository, SHA, patch-id (non-merge commits), goal
label, times, hook and, for ``post-rewrite``, the rewritten SHA. Raw session
identifiers are never written.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from typing import Dict, List, Optional

SCHEMA = 1
JOURNAL_ROOT = "creme-attribution"
JOURNAL = "journal"
PUBLISHED = "published"
LOG = "hook.log"
INSTALLED = "installed.json"
MAX_LOG_BYTES = 256 * 1024
GIT_TIMEOUT = 10
HOOKS = ("post-commit", "post-merge", "post-rewrite", "post-applypatch")
NONE = "none"
# The ledger's own worktree: its publication commits are not attributed.
SKIP_CHECKOUTS = (".attribution",)

# Mirrors of runtime constants; scripts/tests/test_attribution.py binds them.
ACQUISITION_DOMAIN = b"creme-master-record-acquisition-v1\0"
MASTER_NAME = "master.json"
SANCTIONED_SUFFIXES = ("control", "mutation", "rehearsal")
HOST_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
CLIENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
LEASE_ID_RE = re.compile(r"[0-9a-f]{32}")
SHA_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
STAMP = "%Y%m%dT%H%M%SZ"
ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso(seconds: float) -> str:
    return time.strftime(ISO, time.gmtime(seconds))


def git(cwd: str, *args: str, stdin: Optional[bytes] = None) -> str:
    environ = dict(os.environ)
    environ["GIT_TERMINAL_PROMPT"] = "0"
    environ.pop("GIT_EDITOR", None)
    result = subprocess.run(["git", "-C", cwd, *args], input=stdin, capture_output=True,
                            timeout=GIT_TIMEOUT, env=environ, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: "
                           + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode("utf-8", "replace")


def journal_root(common_dir: str) -> str:
    return os.path.join(common_dir, JOURNAL_ROOT)


def ensure_dir(path: str) -> None:
    """Create ``path`` and any missing parents 0700; existing ones keep their mode."""
    if os.path.isdir(path):
        return
    parent = os.path.dirname(os.path.normpath(path))
    if parent and parent != path:
        ensure_dir(parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)


def write_new(path: str, data: bytes) -> None:
    """Create ``path`` exclusively with mode 0600; never overwrite."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def log_error(common_dir: Optional[str], message: str) -> None:
    if not common_dir:
        return
    try:
        root = journal_root(common_dir)
        ensure_dir(root)
        path = os.path.join(root, LOG)
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            line = f"{iso(time.time())} {' '.join(message.split())}\n"
            os.write(descriptor, line.encode("utf-8", "replace"))
        finally:
            os.close(descriptor)
    except Exception:
        pass


def host_name(profile: Optional[str]) -> Optional[str]:
    if not profile:
        return None
    try:
        with open(profile, encoding="utf-8") as handle:
            name = json.load(handle).get("host_name")
    except Exception:
        return None
    return name if isinstance(name, str) and HOST_RE.fullmatch(name) else None


def lease_identity(state_dir: Optional[str], now: float) -> Dict[str, Optional[str]]:
    """The host's master lease as (client, acquisition digest, lease state).

    Mirrors ``master_runtime._actor_from_snapshot``. A lease file that is
    absent, empty or malformed reads as no lease. A lease past its renewal
    window is not held: the digest is ``none`` and the old digest is kept as
    ``stale_lease`` so a report can say so. ``CREME_SEMAPHORE_DIR``, the
    operator's explicit semaphore override for every Creme command, wins over
    the directory recorded at install time.
    """
    blank = {"client": None, "acquisition_digest": NONE, "lease_state": NONE}
    override = os.environ.get("CREME_SEMAPHORE_DIR")
    directory = os.path.expanduser(override) if override else state_dir
    if not directory:
        return blank
    try:
        with open(os.path.join(directory, MASTER_NAME), encoding="utf-8") as handle:
            lease = json.load(handle).get("lease")
    except (OSError, ValueError, AttributeError):
        return blank
    if not isinstance(lease, dict):
        return blank
    lease_id, client = lease.get("lease_id"), lease.get("client")
    if not isinstance(lease_id, str) or LEASE_ID_RE.fullmatch(lease_id) is None:
        return blank
    digest = hashlib.sha256(ACQUISITION_DOMAIN + lease_id.encode("ascii")).hexdigest()
    try:
        expired = float(lease["renewed_at"]) + int(lease["lease_seconds"]) < now
    except (KeyError, TypeError, ValueError):
        expired = True
    valid_client = isinstance(client, str) and CLIENT_RE.fullmatch(client) is not None
    if expired:
        return {"client": None, "acquisition_digest": NONE, "lease_state": "expired",
                "stale_lease": digest}
    return {"client": client if valid_client else None, "acquisition_digest": digest,
            "lease_state": "live"}


def goal_label(toplevel: str, branch: str) -> str:
    """``.worktrees/GOAL`` (or ``GOAL-control`` etc.) gives GOAL; else the branch.

    A detached primary checkout is ``detached``. The ``-control``,
    ``-mutation`` and ``-rehearsal`` suffixes are the host's sanctioned
    disposable-tree names, so a goal ID ending in those words is ambiguous
    host-wide already.
    """
    parent, name = os.path.split(os.path.normpath(toplevel))
    if os.path.basename(parent) == ".worktrees" and name:
        for suffix in SANCTIONED_SUFFIXES:
            if name.endswith("-" + suffix) and len(name) > len(suffix) + 1:
                return name[:-len(suffix) - 1]
        return name
    if branch == "HEAD":
        return "detached"
    return branch or "unknown"


def safe_patch_id(cwd: str, sha: str) -> Optional[str]:
    """The patch-id, or None when it cannot be computed in time (record kept)."""
    try:
        return patch_id(cwd, sha)
    except Exception:
        return None


def patch_id(cwd: str, sha: str) -> Optional[str]:
    diff = subprocess.run(["git", "-C", cwd, "diff-tree", "-p", "--no-color", "--root", sha],
                          capture_output=True, timeout=GIT_TIMEOUT, check=False)
    if diff.returncode or not diff.stdout:
        return None
    output = git(cwd, "patch-id", "--stable", stdin=diff.stdout).split()
    return output[0] if output else None


def commit_facts(cwd: str, sha: str) -> Dict[str, object]:
    raw = git(cwd, "show", "-s", "--format=%H%x00%P%x00%ct", sha).strip()
    full, parents, committed = raw.split("\x00")
    parent_list = parents.split()
    return {"sha": full, "parents": parent_list, "committed_at": iso(int(committed)),
            "patch_id": safe_patch_id(cwd, full) if len(parent_list) <= 1 else None}


def context(cwd: str) -> Dict[str, str]:
    lines = git(cwd, "rev-parse", "--show-toplevel", "--git-common-dir",
                "--abbrev-ref", "HEAD").splitlines()
    toplevel, common = lines[0], lines[1]
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(cwd, common))
    branch = lines[2] if len(lines) > 2 else "HEAD"
    return {"toplevel": toplevel, "common": common, "branch": branch}


def record_id(sha: str, hook: str, now: float) -> str:
    return f"{time.strftime(STAMP, time.gmtime(now))}-{sha}-{hook}-{secrets.token_hex(4)}"


def build_record(*, cwd: str, sha: str, hook: str, repository: str, host: Optional[str],
                 identity: Dict[str, Optional[str]], where: Dict[str, str], now: float,
                 source: str = "hook", extra: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    record: Dict[str, object] = {
        "schema": SCHEMA, "id": record_id(sha, hook, now), "source": source, "hook": hook,
        "host": host, "client": identity["client"],
        "acquisition_digest": identity["acquisition_digest"],
        "lease_state": identity["lease_state"], "repository": repository,
        **({"stale_lease": identity["stale_lease"]} if identity.get("stale_lease") else {}),
        "goal": goal_label(where["toplevel"], where["branch"]), "branch": where["branch"],
        "recorded_at": iso(now),
    }
    record.update(commit_facts(cwd, sha))
    if extra:
        record.update(extra)
    return record


def write_record(common: str, record: Dict[str, object]) -> str:
    directory = os.path.join(journal_root(common), JOURNAL)
    ensure_dir(journal_root(common))
    ensure_dir(directory)
    path = os.path.join(directory, f"{record['id']}.json")
    write_new(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    return path


def created_by_merge(cwd: str) -> bool:
    """True when the last HEAD move made a merge commit, not a fast-forward."""
    try:
        subject = git(cwd, "reflog", "-1", "--format=%gs", "HEAD").strip()
    except RuntimeError:
        return False
    return "Merge made by" in subject


def targets(hook: str, args: List[str], cwd: str, stdin: bytes) -> List[Dict[str, object]]:
    """The commits this hook invocation created, as (sha, extra) pairs."""
    if hook in ("post-commit", "post-applypatch"):
        return [{"sha": "HEAD", "extra": None}]
    if hook == "post-merge":
        squash = bool(args) and args[0] == "1"
        if squash or not created_by_merge(cwd):
            return []
        return [{"sha": "HEAD", "extra": None}]
    if hook == "post-rewrite":
        kind = args[0] if args else "unknown"
        found = []
        for line in stdin.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and SHA_RE.fullmatch(parts[0]) and SHA_RE.fullmatch(parts[1]):
                found.append({"sha": parts[1],
                              "extra": {"rewrite": kind, "rewritten_from": parts[0]}})
        return found
    return []


def run(argv: List[str], stdin: bytes, cwd: Optional[str] = None,
        now: Optional[float] = None) -> int:
    common = None
    try:
        options: Dict[str, str] = {}
        rest = list(argv)
        while rest and rest[0].startswith("--"):
            key = rest.pop(0)[2:]
            options[key] = rest.pop(0)
        hook, args = rest[0], rest[1:]
        cwd = cwd or os.getcwd()
        where = context(cwd)
        common = where["common"]
        if hook not in HOOKS:
            raise ValueError(f"unknown hook {hook!r}")
        if os.path.basename(os.path.normpath(where["toplevel"])) in SKIP_CHECKOUTS:
            return 0
        moment = time.time() if now is None else now
        identity = lease_identity(options.get("state-dir"), moment)
        host = host_name(options.get("profile"))
        for target in targets(hook, args, cwd, stdin):
            try:  # one bad target (say, a pruned rewrite source) never loses the rest
                record = build_record(cwd=cwd, sha=str(target["sha"]), hook=hook,
                                      repository=options.get("repo", "unknown"), host=host,
                                      identity=identity, where=where, now=moment,
                                      extra=target["extra"])  # type: ignore[arg-type]
                write_record(common, record)
            except Exception as exc:
                log_error(common, f"{hook} {target['sha']}: {type(exc).__name__}: {exc}")
    except Exception as exc:  # the hook never fails a commit
        log_error(common, f"{type(exc).__name__}: {exc}")
    return 0


def main() -> int:
    # Only post-rewrite is fed a finite stdin by Git; any other hook inherits
    # the committer's stdin, which may never close, so it is never read.
    data = b""
    if "post-rewrite" in sys.argv[1:]:
        try:
            data = sys.stdin.buffer.read()
        except Exception:
            data = b""
    return run(sys.argv[1:], data)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
