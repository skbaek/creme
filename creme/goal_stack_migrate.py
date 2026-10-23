"""One-time move of a host's schema-1 goal stack onto the shared stack branch.

``migrate`` (the first host): byte-preserves the schema-1 manifest and every
receipt in an import commit on a new history-free branch, appends one
``migrate`` revision whose receipt records how each host-local reference was
qualified, publishes that branch alone, checks it out at
``$GOAL_STORE/.goal-stack``, replaces the goal store's ``goal-stack.toml``
with a stub that old Creme refuses, and records the procedure event.

``adopt`` (every other host): fetches the published branch, verifies the
whole chain, proves its local schema-1 chain is a prefix of the canonical one
(or, explicitly, sets local-only revisions aside for re-application),
validates the head as this host, and performs the same stub and event steps.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import goal_stack, goal_stack_git as shared, goal_stack_store as store
from . import master_runtime as runtime, semaphore

RECEIPT_NAME = re.compile(r"(\d{8})\.json")
MAX_RECEIPTS = 1_000_000


class MigrationError(store.StackStoreError):
    pass


def parse_host_map(items) -> list[tuple[str, str]]:
    """``PREFIX=HOST`` pairs, longest prefix first."""
    pairs = []
    for item in items or ():
        prefix, sep, host = item.rpartition("=")
        if not sep or not prefix or goal_stack.HOST_RE.fullmatch(host) is None:
            raise MigrationError(f"host map entry must be PREFIX=HOST: {item!r}")
        pairs.append((prefix, host))
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def _mapped(value: str, host_map) -> Optional[str]:
    for prefix, host in host_map:
        if value.startswith(prefix):
            return host
    return None


def _regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > store.MAX_BYTES:
        raise MigrationError(f"{path} must be a bounded regular file")
    return path.read_bytes()


def _legacy_chain(root: Path) -> tuple[bytes, dict, dict[str, bytes]]:
    """The schema-1 manifest and its complete, linked receipt chain."""
    if store._bytes(root, store.PENDING, absent=True) is not None:
        raise MigrationError("unfinished goal-stack transaction; master stack recover first")
    data = store._bytes(root, store.NAME, absent=True)
    if data is None:
        raise MigrationError("no goal-stack.toml to migrate")
    if shared.is_stub(data):
        raise MigrationError("goal stack is already shared")
    try:
        stack = goal_stack.parse(data.decode("utf-8"))
        goal_stack.check_structure(stack, schema=goal_stack.SCHEMA_VERSION)
    except (ValueError, UnicodeError) as exc:
        raise MigrationError(f"schema-1 stack is malformed: {exc}") from exc
    directory = store._safe_path(root, shared.RECEIPTS)
    if not directory.is_dir() or directory.is_symlink():
        raise MigrationError(f"missing receipt directory {shared.RECEIPTS}")
    receipts: dict[str, bytes] = {}
    for path in sorted(directory.iterdir()):
        match = RECEIPT_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"unexpected file in receipt directory: {path.name}")
        receipts[f"{shared.RECEIPTS}/{path.name}"] = _regular(path)
    revision = stack["revision"]
    expected = [shared.receipt_path(n) for n in range(revision + 1)]
    if sorted(receipts) != expected:
        raise MigrationError(
            f"receipts must be exactly 00000000..{revision:08d}; found {len(receipts)} files")
    previous_after = None
    for number, name in enumerate(expected):
        try:
            receipt = json.loads(receipts[name].decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise MigrationError(f"receipt {name} is not JSON: {exc}") from exc
        if not isinstance(receipt, dict) or receipt.get("revision") != number:
            raise MigrationError(f"receipt {name} does not record revision {number}")
        if number and receipt.get("before_sha256") != previous_after:
            raise MigrationError(f"receipt {name} does not extend revision {number - 1}")
        previous_after = receipt.get("after_sha256")
    if previous_after != shared.digest(data):
        raise MigrationError(f"receipt {revision:08d} does not bind the live manifest")
    return data, stack, receipts


def _on_shared_ref(root: Path, shared_ref: Optional[str], relative: str) -> bool:
    if shared_ref is None:
        return False
    return shared.git(root, "cat-file", "-e", f"{shared_ref}:{relative}",
                      check=False).returncode == 0


def _local_file(root: Path, relative: str) -> bool:
    if Path(relative).is_absolute():
        return False
    target = (root / relative).resolve()
    return target.is_relative_to(root.resolve()) and target.is_file()


def qualify(root: Path, stack: dict, host: str, host_map, shared_ref: Optional[str]):
    """Return (schema-2 entries, qualifications, unresolved)."""
    entries, qualified, unresolved = [], {}, []
    for entry in stack["entries"]:
        new = dict(entry)
        for field in goal_stack.REF_FIELDS:
            if field not in entry:
                continue
            value = entry[field]
            if _local_file(root, value) and _on_shared_ref(root, shared_ref, value):
                continue  # tracked in the shared store: stays unqualified
            owner = _mapped(value, host_map) or (host if _local_file(root, value) else None)
            if owner is None:
                unresolved.append({"id": entry["id"], "field": field, "value": value})
                continue
            new[field] = f"{owner}:{value}"
        if "worktree" in entry:
            value = entry["worktree"]
            owner = _mapped(value, host_map) or (host if Path(value).is_absolute()
                                                 and Path(value).exists() else None)
            if owner is None or not value.startswith("/"):
                unresolved.append({"id": entry["id"], "field": "worktree", "value": value})
            else:
                new["worktree"] = f"{owner}:{value}"
        changes = {k: [entry[k], new[k]] for k in new if new[k] != entry.get(k)}
        if changes:
            qualified[entry["id"]] = changes
        entries.append(new)
    return entries, qualified, unresolved


def plan(goal_store: Path, *, host: str, host_map=(), shared_ref: Optional[str] = None) -> dict[str, Any]:
    """Read-only migration plan; writes nothing anywhere."""
    goal_stack._check_host(host)
    root = Path(goal_store)
    data, stack, receipts = _legacy_chain(root)
    entries, qualified, unresolved = qualify(root, stack, host, parse_host_map(host_map), shared_ref)
    candidate = {"schema_version": goal_stack.SHARED_SCHEMA_VERSION,
                 "revision": stack["revision"] + 1, "entries": entries}
    result = {
        "status": "OK", "host": host, "shared_ref": shared_ref,
        "revision": stack["revision"], "new_revision": candidate["revision"],
        "receipts": len(receipts), "entries": len(entries),
        "qualified": qualified, "unresolved": unresolved,
        "before_sha256": shared.digest(data),
    }
    if unresolved:
        result["status"] = "REFUSED"
        result["detail"] = "unresolved host for some references; add --host-map PREFIX=HOST"
        return result
    try:
        goal_stack.validate(candidate, root, host=host, schema=goal_stack.SHARED_SCHEMA_VERSION)
    except ValueError as exc:
        result["status"] = "REFUSED"
        result["detail"] = f"schema-2 candidate does not validate on {host}: {exc}"
        return result
    text = goal_stack.dumps(candidate)
    result["after_sha256"] = shared.digest(text.encode())
    result["manifest"] = text
    result["_private"] = (data, receipts, candidate, text)
    return result


def _ensure_ignored(root: Path) -> bool:
    """Ignore the stack worktree on the goal store's default branch."""
    probe = shared.git(root, "check-ignore", "-q", "--no-index", "--",
                       f"{shared.SHARED_DIR}/goal-stack.toml", check=False)
    if probe.returncode == 0:
        return False
    path = store._safe_path(root, ".gitignore")
    existing = path.read_bytes() if path.exists() else b""
    addition = (b"" if not existing or existing.endswith(b"\n") else b"\n") + (
        b"# Shared goal-stack branch worktree (one canonical stack across hosts).\n/"
        + shared.SHARED_DIR.encode() + b"/\n")
    runtime._atomic_replace(path, existing + addition, label=".gitignore", fault=None)
    return True


def _remote_branch_exists(root: Path, remote: str, branch: str) -> bool:
    try:
        return shared.remote_branch_exists(root, remote, branch)
    except shared.SharedStackError as exc:
        raise MigrationError(str(exc)) from exc


def _finish_adoption(writer, lease, root: Path, procedure_payload: dict) -> None:
    _ensure_ignored(root)
    runtime._atomic_replace(store._safe_path(root, store.NAME), shared.STUB.encode(),
                            label=store.NAME, fault=None)
    view = runtime._read_record_unlocked(writer.root)
    if not any(e["kind"] == "procedure"
               and e["payload"]["procedure_id"] == store.SHARED_PROCEDURE for e in view.events):
        writer._append_authorized(lease, "procedure", procedure_payload, fault=None,
                                  once_per_acquisition=False)


def _procedure(evidence: str) -> dict:
    return {
        "procedure_id": store.SHARED_PROCEDURE, "action": "add",
        "failure": "Per-host goal-stack copies forked their revision chains and host-local references failed closed on other hosts.",
        "replacement": "One canonical schema-2 stack on a history-free shared branch; host-qualified references; compare-and-swap writes.",
        "control": "Linear-chain verification on fetch, non-force push with replay-or-refuse, own-host and shared references existence-checked.",
        "evidence": evidence,
    }


def _authorized(record_root: Path, writer):
    writer = writer or runtime.RecordWriter(record_root)
    if writer.root != record_root:
        raise MigrationError("writer and stack must use the same master record")
    runtime._renew_or_refuse(writer.renew)
    runtime._normalize_lock_prerequisites(record_root)
    return writer


def _require_absent(root: Path, branch: str) -> None:
    if (root / shared.SHARED_DIR).exists() or (root / shared.SHARED_DIR).is_symlink():
        raise MigrationError(f"{shared.SHARED_DIR} already exists; remove a partial attempt first")
    if shared.git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                  check=False).returncode == 0:
        raise MigrationError(f"local branch {branch!r} already exists; remove a partial attempt first")


def migrate(record_root: Path, *, host: str, host_map=(), shared_ref: Optional[str] = None,
            remote: str = shared.DEFAULT_REMOTE, branch: str = shared.DEFAULT_BRANCH,
            writer=None) -> dict[str, Any]:
    """Move this host's schema-1 stack to a new shared branch and publish it."""
    writer = _authorized(record_root, writer)
    with runtime._locked_record(record_root):
        try:
            with writer.authority_transaction() as lease:
                runtime.normalize_private_modes(record_root)
                root = record_root.parent
                view = runtime._read_record_unlocked(record_root)
                if not store.adopted(view.events):
                    raise MigrationError("goal stack not adopted on this host; nothing to migrate")
                result = plan(root, host=host, host_map=list(host_map), shared_ref=shared_ref)
                if result["status"] != "OK":
                    raise MigrationError(result["detail"])
                data, receipts, candidate, text = result.pop("_private")
                _require_absent(root, branch)
                if _remote_branch_exists(root, remote, branch):
                    raise MigrationError(
                        f"{remote}/{branch} already exists: another host migrated; use adopt-shared")
                after = text.encode()
                actor = runtime._actor_from_snapshot(lease)
                actor["host"] = host
                receipt = {
                    "schema_version": 1, "action": "migrate",
                    "payload": {"from_schema_version": 1,
                                "to_schema_version": goal_stack.SHARED_SCHEMA_VERSION,
                                "qualified": result["qualified"], "shared_ref": shared_ref},
                    "revision": candidate["revision"], "before_sha256": shared.digest(data),
                    "after_sha256": shared.digest(after), "actor": actor,
                    "timestamp": datetime.now(timezone.utc).isoformat(), "archive": None,
                    "prepared_revision": result["revision"],
                }
                with tempfile.TemporaryDirectory() as scratch:
                    env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
                    files = dict(receipts)
                    files[shared.NAME] = data
                    files[".gitignore"] = shared.BRANCH_IGNORE.encode()
                    tree = shared.write_tree(root, files, env)
                    first = shared.commit_tree(root, tree, (
                        f"goal-stack import: schema-1 chain through revision {result['revision']} "
                        f"(host {host}), bytes preserved"))
                    files[shared.NAME] = after
                    files[shared.receipt_path(candidate["revision"])] = store._json(receipt)
                    tree = shared.write_tree(root, files, env)
                    second = shared.commit_tree(root, tree, (
                        f"goal-stack revision {candidate['revision']}: migrate to schema 2 (host {host})"),
                        parent=first)
                shared.git(root, "branch", branch, second)
                shared.configure_upstream(root, remote, branch)
                pushed = shared.git(root, "push", "--quiet", remote,
                                    f"refs/heads/{branch}:refs/heads/{branch}", check=False)
                if pushed.returncode:
                    shared.git(root, "branch", "-D", branch, check=False)
                    shared.git(root, "config", "--unset", f"branch.{branch}.remote", check=False)
                    shared.git(root, "config", "--unset", f"branch.{branch}.merge", check=False)
                    raise MigrationError("could not publish the shared stack branch; nothing adopted: "
                                         + pushed.stderr.decode("utf-8", "replace").strip())
                shared.git(root, "fetch", "--quiet", remote,
                           f"refs/heads/{branch}:{shared.tracking_ref(remote, branch)}")
                shared.git(root, "worktree", "add", "--quiet", shared.SHARED_DIR, branch)
                worktree = store.shared_root(root)
                if shared.show(worktree, "HEAD", shared.NAME) != after:
                    raise MigrationError("shared worktree does not hold the migrated manifest")
                _finish_adoption(writer, lease, root, _procedure(
                    f"Migrated revision {result['revision']} -> {candidate['revision']} on {host}; "
                    f"{remote}/{branch} {second}"))
                result.update({"commit": second, "branch": f"{remote}/{branch}"})
                result.pop("manifest", None)
                return result
        except semaphore.MasterAuthorityRefused as exc:
            raise runtime.RenewalRefused(f"master renewal refused: {exc}") from exc


def _local_receipts(root: Path) -> dict[int, bytes]:
    directory = root / shared.RECEIPTS
    found: dict[int, bytes] = {}
    if directory.is_dir() and not directory.is_symlink():
        for path in directory.iterdir():
            match = RECEIPT_NAME.fullmatch(path.name)
            if match is not None:
                found[int(match.group(1))] = _regular(path)
    return found


def _require_complete_import(root: Path, commit: str) -> None:
    """The import commit holds exactly receipts 00000000..N for its manifest."""
    _, imported = shared.manifest_at(root, commit)
    listed = shared.out(root, "ls-tree", "-r", "--name-only", commit, "--", shared.RECEIPTS)
    names = sorted(line for line in listed.splitlines() if line)
    expected = [shared.receipt_path(n) for n in range(imported["revision"] + 1)]
    if names != expected:
        raise MigrationError(
            f"shared import commit must hold exactly receipts 00000000.."
            f"{imported['revision']:08d}; found {len(names)}")
    data = shared.show(root, commit, shared.NAME)
    receipt = json.loads(shared.show(root, commit, expected[-1]).decode())
    if receipt.get("after_sha256") != shared.digest(data):
        raise MigrationError("shared import receipt does not bind the imported manifest")


def adopt(record_root: Path, *, host: str, remote: str = shared.DEFAULT_REMOTE,
          branch: str = shared.DEFAULT_BRANCH, set_aside_local: bool = False,
          writer=None) -> dict[str, Any]:
    """Adopt the canonical shared stack published by another host."""
    goal_stack._check_host(host)
    writer = _authorized(record_root, writer)
    with runtime._locked_record(record_root):
        try:
            with writer.authority_transaction() as lease:
                runtime.normalize_private_modes(record_root)
                root = record_root.parent
                if store.is_shared(root):
                    raise MigrationError("goal stack is already shared on this host")
                if store._bytes(root, store.PENDING, absent=True) is not None:
                    raise MigrationError("unfinished goal-stack transaction; master stack recover first")
                tracking = shared.tracking_ref(remote, branch)
                fetched = shared.git(root, "fetch", "--quiet", remote,
                                     f"refs/heads/{branch}:{tracking}", check=False)
                if fetched.returncode:
                    raise MigrationError(f"cannot fetch {remote}/{branch}: "
                                         + fetched.stderr.decode("utf-8", "replace").strip())
                canonical = shared.out(root, "rev-parse", "--verify", tracking)
                roots = shared.out(root, "rev-list", "--max-parents=0", canonical).split()
                if len(roots) != 1:
                    raise MigrationError("shared stack branch must have exactly one import root")
                _require_complete_import(root, roots[0])
                shared.verify_chain(root, roots[0], canonical)
                _, head_stack = shared.manifest_at(root, canonical)
                head_revision = head_stack["revision"]
                # Is the local schema-1 chain a prefix of the canonical one?
                local = _local_receipts(root)
                common = -1
                for number in sorted(local):
                    if number != common + 1 or number > head_revision:
                        break
                    if shared.show(root, canonical, shared.receipt_path(number)) != local[number]:
                        break
                    common = number
                local_only = sorted(n for n in local if n > common)
                legacy = store._bytes(root, store.NAME, absent=True)
                if legacy is not None and common >= 0:
                    bound = json.loads(local[common].decode()).get("after_sha256")
                    if local_only == [] and bound != shared.digest(legacy):
                        local_only = [common]
                set_aside = []
                for number in local_only:
                    try:
                        receipt = json.loads(local[number].decode())
                    except (ValueError, UnicodeError):
                        receipt = {}
                    payload = receipt.get("payload") if isinstance(receipt, dict) else None
                    set_aside.append({"revision": number, "action": receipt.get("action"),
                                      "payload": payload})
                if set_aside and not set_aside_local:
                    raise MigrationError(
                        f"local stack has {len(set_aside)} revision(s) after canonical revision "
                        f"{common} that the shared chain lacks; rerun with --set-aside-local and "
                        f"re-apply them with stack commands: {[r['revision'] for r in set_aside]}")
                worktree_path = root / shared.SHARED_DIR
                if worktree_path.exists():
                    raise MigrationError(f"{shared.SHARED_DIR} already exists; remove a partial attempt first")
                if shared.git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                              check=False).returncode == 0:
                    if shared.out(root, "rev-parse", f"refs/heads/{branch}") != canonical and not \
                            shared.is_ancestor(root, f"refs/heads/{branch}", canonical):
                        raise MigrationError(f"local branch {branch!r} diverges from {remote}/{branch}")
                    shared.git(root, "branch", "-f", branch, canonical)
                else:
                    shared.git(root, "branch", branch, canonical)
                shared.configure_upstream(root, remote, branch)
                shared.git(root, "worktree", "add", "--quiet", shared.SHARED_DIR, branch)
                try:
                    stack = store._read_shared(root, host)
                except store.StackStoreError as exc:
                    shared.git(root, "worktree", "remove", "--force", shared.SHARED_DIR, check=False)
                    raise MigrationError(
                        f"canonical stack does not validate on {host} (pull the goal store's "
                        f"default branch for shared refs): {exc}") from exc
                _finish_adoption(writer, lease, root, _procedure(
                    f"Adopted {remote}/{branch} {canonical} at revision {head_revision} on {host}; "
                    f"common schema-1 revision {common}; set aside {[r['revision'] for r in set_aside]}"))
                return {"status": "OK", "host": host, "revision": stack["revision"],
                        "commit": canonical, "common_revision": common, "set_aside": set_aside}
        except semaphore.MasterAuthorityRefused as exc:
            raise runtime.RenewalRefused(f"master renewal refused: {exc}") from exc
        except shared.SharedStackError as exc:
            raise MigrationError(str(exc)) from exc
