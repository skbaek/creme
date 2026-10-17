"""Authenticated, crash-recoverable publication of the one live goal stack.

The existing master mutex and lease transaction serialize writers on one
host. A small write-ahead record binds the canonical TOML replacement to its
history receipt; readers refuse an unfinished transaction rather than choosing
either queue.

Two storage modes exist. A legacy (schema 1) stack is ``goal-stack.toml`` in
the goal store itself, valid on one host. After the multi-host migration the
goal store's ``goal-stack.toml`` is a stub that old Creme refuses, and the one
canonical schema-2 stack lives on the shared stack branch checked out at
``$GOAL_STORE/.goal-stack`` (see :mod:`creme.goal_stack_git`). Shared writes
add a cross-host compare-and-swap (fetch, replay rule, non-force push) inside
the same local lock, lease transaction and journal discipline.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import goal_stack, goal_stack_git as shared, master_runtime as runtime, semaphore

NAME = "goal-stack.toml"
PENDING = ".goal-stack-pending.json"
PROCEDURE = "master-goal-stack-v1"
SHARED_PROCEDURE = "master-goal-stack-multihost-v1"
MAX_BYTES = 4 * 1024 * 1024
MAX_PUBLISH_ATTEMPTS = 4


class StackStoreError(runtime.MasterRecordError):
    pass


def adopted(events) -> bool:
    return any(e["kind"] == "procedure"
               and e["payload"]["procedure_id"] == PROCEDURE
               and e["payload"]["action"] in {"add", "replace"} for e in events)


def local_host_name():
    """This host's configured name (None when unset); patched in tests."""
    from . import profile

    return profile.load_host_name()


def is_shared(root: Path) -> bool:
    """True once this goal store's ``goal-stack.toml`` is the moved stub."""
    return shared.is_stub(_bytes(root, NAME, absent=True))


def shared_root(root: Path) -> Path:
    path = _safe_path(root, shared.SHARED_DIR)
    if path.is_symlink() or not path.is_dir():
        raise StackStoreError(
            f"shared goal stack worktree missing: {path}; never fall back to the stub's predecessor")
    return path


def _require_host(host):
    host = host if host is not None else local_host_name()
    if host is None:
        raise StackStoreError(
            "this host has no name; a shared goal stack refuses unnamed hosts "
            "(python3 -m creme set-host-name NAME)")
    try:
        goal_stack._check_host(host)
    except ValueError as exc:
        raise StackStoreError(str(exc)) from exc
    return host


def enabled(record_root: Path, events=()) -> bool:
    path = record_root.parent / NAME
    return path.exists() or path.is_symlink() or adopted(events)


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise StackStoreError(f"unsafe goal-stack path: {relative}")
    return path


def _bytes(root: Path, relative: str, *, absent=False) -> bytes | None:
    path = _safe_path(root, relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if absent:
            return None
        raise StackStoreError(f"missing {relative}; never fall back to the historical board")
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise StackStoreError(f"{relative} must be a bounded regular file")
    return path.read_bytes()


def read_unlocked(record_root: Path, host=None) -> dict:
    root = record_root.parent
    if _bytes(root, PENDING, absent=True) is not None:
        raise StackStoreError("unfinished goal-stack transaction; master stack recover before scheduling")
    data = _bytes(root, NAME)
    if shared.is_stub(data):
        return _read_shared(root, _require_host(host))
    try:
        return goal_stack.loads(data.decode("utf-8"), root, schema=goal_stack.SCHEMA_VERSION)
    except (ValueError, UnicodeError) as exc:
        raise StackStoreError(str(exc)) from exc


def _read_shared(root: Path, host: str) -> dict:
    """Read the committed shared stack as this host verifies it."""
    worktree = shared_root(root)
    if _bytes(worktree, PENDING, absent=True) is not None:
        raise StackStoreError("unfinished goal-stack transaction; master stack recover before scheduling")
    data = _bytes(worktree, NAME)
    try:
        committed = shared.show(worktree, "HEAD", NAME)
    except shared.SharedStackError as exc:
        raise StackStoreError(str(exc)) from exc
    if committed != data:
        raise StackStoreError("shared stack file differs from its committed revision; preserve and reconcile")
    try:
        return goal_stack.loads(data.decode("utf-8"), root, host=host,
                                schema=goal_stack.SHARED_SCHEMA_VERSION)
    except (ValueError, UnicodeError) as exc:
        raise StackStoreError(str(exc)) from exc


def read(record_root: Path, host=None) -> dict:
    with runtime._record_lock(record_root, exclusive=False):
        return read_unlocked(record_root, host)


def describe(record_root: Path, host=None) -> dict:
    """Storage facts for display: mode, host, and the shared upstream."""
    root = record_root.parent
    if not is_shared(root):
        return {"mode": "legacy", "host": host if host is not None else local_host_name(),
                "source": NAME}
    host = host if host is not None else local_host_name()
    detail = {"mode": "shared", "host": host, "source": f"{shared.SHARED_DIR}/{NAME}"}
    try:
        worktree = shared_root(root)
        remote, branch = shared.upstream(worktree)
        detail["upstream"] = f"{remote}/{branch}"
        detail["head"] = shared.head(worktree)
    except (StackStoreError, shared.SharedStackError) as exc:
        detail["upstream_error"] = str(exc)
    return detail


def _publish(root: Path, relative: str, data: bytes, fault=None) -> None:
    path = _safe_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(root, relative)
    runtime._atomic_replace(path, data, label=relative, fault=fault)


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _finish(root: Path, pending: dict, *, fault=None, clear=True, ref_root=None,
            host=None, schema=goal_stack.SCHEMA_VERSION) -> None:
    if (not isinstance(pending, dict)
            or set(pending) != {"schema_version", "before", "after", "receipt"}
            or type(pending["schema_version"]) is not int or pending["schema_version"] != 1):
        raise StackStoreError("invalid stack transaction schema")
    after = pending["after"]
    receipt = pending["receipt"]
    if not isinstance(after, str) or not isinstance(receipt, dict):
        raise StackStoreError("invalid stack transaction contents")
    snapshot = goal_stack.loads(after, ref_root or root, host=host, schema=schema)
    after_bytes = after.encode()
    before = pending["before"]
    if before is not None and (not isinstance(before, str) or re.fullmatch(r"[0-9a-f]{64}", before) is None):
        raise StackStoreError("invalid prior stack identity")
    if (receipt.get("after_sha256") != _digest(after_bytes)
            or receipt.get("before_sha256") != before
            or receipt.get("revision") != snapshot["revision"]):
        raise StackStoreError("stack transaction receipt is not bound to its snapshot")
    current = _bytes(root, NAME, absent=True)
    if _digest(current) not in {before, _digest(after_bytes)}:
        raise StackStoreError("stack moved outside the pending transaction; preserve and reconcile")
    relative = f"archive/goal-stack/transitions/{snapshot['revision']:08d}.json"
    data = _json(receipt)
    old = _bytes(root, relative, absent=True)
    if old is not None and old != data:
        raise StackStoreError("stack history receipt conflicts; refusing overwrite")
    if old is None:
        _publish(root, relative, data, fault)
    if current != after_bytes:
        _publish(root, NAME, after_bytes, fault)
    if not clear:
        return
    runtime._fault(fault, "stack:before-clear")
    _safe_path(root, PENDING).unlink()
    runtime._fsync_directory(root)
    runtime._fault(fault, "stack:after-clear")


def _recover(record_root: Path, *, fault=None) -> bool:
    data = _bytes(record_root.parent, PENDING, absent=True)
    if data is None:
        return False
    try:
        pending = runtime._strict_json(data, "goal-stack transaction")
        _finish(record_root.parent, pending, fault=fault)
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise StackStoreError(f"invalid pending stack transaction: {exc}") from exc
    return True


def _record_adoption(writer, snapshot) -> None:
    view = runtime._read_record_unlocked(writer.root)
    if adopted(view.events):
        return
    _require_registered_origin(writer.root.parent)
    writer._append_authorized(snapshot, "procedure", {
        "procedure_id": PROCEDURE, "action": "replace",
        "failure": "Independent goal events, TODOs and backlogs drifted into competing scheduling authorities.",
        "replacement": "goal-stack.toml is the sole live order/status; historical goal events cannot schedule work.",
        "control": "Stack validation, non-preemption, authenticated atomic writes and crash recovery controls; legacy goal writes refuse after adoption.",
        "evidence": "User-authorized goal-stack migration; archive/goal-stack/transitions/00000000.json and reports/master-goal-stack-v1.md.",
    }, fault=None, once_per_acquisition=False)


def _require_registered_origin(root: Path) -> None:
    """A crash-left initial publication has a receipt; a stray file does not."""
    relative = "archive/goal-stack/transitions/00000000.json"
    data = _bytes(root, relative, absent=True)
    if data is None:
        raise StackStoreError("unregistered stack file: preserve it under another name, then use stack init")
    receipt = runtime._strict_json(data, "initial stack receipt")
    if (not isinstance(receipt, dict) or receipt.get("action") != "init"
            or receipt.get("revision") != 0 or receipt.get("before_sha256") is not None
            or not isinstance(receipt.get("payload"), dict)
            or not isinstance(receipt["payload"].get("stack"), dict)):
        raise StackStoreError("invalid initial stack receipt")
    initial = receipt["payload"]["stack"]
    if initial.get("revision") != 0 or receipt.get("after_sha256") != _digest(goal_stack.dumps(initial).encode()):
        raise StackStoreError("initial stack receipt does not bind its snapshot")


def mutate(record_root: Path, action: str, payload: dict, *, expected_revision=None,
           writer=None, fault=None, host=None) -> dict[str, Any]:
    if action not in {"init", "recover", "push", "move", "reorder", "update", "complete", "retire"}:
        raise StackStoreError(f"unsupported stack mutation: {action}")
    if not isinstance(payload, dict):
        raise StackStoreError("stack mutation payload must be an object")
    if action == "init" and set(payload) != {"stack"}:
        raise StackStoreError("stack initialization requires exactly one stack snapshot")
    if action == "recover" and payload:
        raise StackStoreError("stack recovery takes no payload")
    writer = writer or runtime.RecordWriter(record_root)
    if writer.root != record_root:
        raise StackStoreError("writer and stack must use the same master record")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise StackStoreError("expected revision must be a nonnegative integer")
    runtime._renew_or_refuse(writer.renew)
    runtime._normalize_lock_prerequisites(record_root)
    with runtime._locked_record(record_root):
        try:
            with writer.authority_transaction() as lease:
                runtime.normalize_private_modes(record_root)
                view = runtime._read_record_unlocked(record_root)
                root = record_root.parent
                if is_shared(root):
                    return _mutate_shared(root, action, payload, expected_revision,
                                          lease, _require_host(host), fault)
                recovered = _recover(record_root, fault=fault)
                before = _bytes(root, NAME, absent=True)
                if before is not None and not adopted(view.events) and action != "init":
                    _require_registered_origin(root)
                if action == "recover":
                    if before is None:
                        raise StackStoreError("no goal stack to recover")
                    candidate = read_unlocked(record_root)
                    _record_adoption(writer, lease)
                    return {"status": "OK", "recovered": recovered, "stack": candidate}
                if action == "init":
                    if before is not None or adopted(view.events):
                        raise StackStoreError("goal stack already initialized; use explicit mutations")
                    candidate = payload["stack"]
                    goal_stack.validate(candidate, root)
                    if candidate["revision"] != 0:
                        raise StackStoreError("initial stack revision must be zero")
                    archive = None
                else:
                    if before is None:
                        raise StackStoreError("missing goal stack; initialize explicitly, never infer from old goals")
                    current = goal_stack.loads(before.decode(), root)
                    if expected_revision is not None and expected_revision != current["revision"]:
                        raise StackStoreError("stack revision changed; reread before applying this edit")
                    candidate, archive = goal_stack.apply(current, action, payload, root)
                after = goal_stack.dumps(candidate).encode()
                if after == before:
                    return {"status": "OK", "changed": False, "stack": candidate}
                actor = runtime._actor_from_snapshot(lease)
                named = host if host is not None else local_host_name()
                if named is not None:
                    actor["host"] = named
                receipt = {
                    "schema_version": 1, "action": action, "payload": payload,
                    "revision": candidate["revision"], "before_sha256": _digest(before),
                    "after_sha256": _digest(after), "actor": actor,
                    "timestamp": datetime.now(timezone.utc).isoformat(), "archive": archive,
                }
                pending = {"schema_version": 1, "before": _digest(before),
                           "after": after.decode(), "receipt": receipt}
                pending_bytes = _json(pending)
                if max(len(after), len(pending_bytes), len(_json(receipt))) > MAX_BYTES:
                    raise StackStoreError("stack transaction exceeds the bounded file size")
                _publish(root, PENDING, pending_bytes, fault)
                _finish(root, pending, fault=fault)
                _record_adoption(writer, lease)
                return {"status": "OK", "changed": True, "stack": candidate,
                        "archive": archive, "recovered": recovered}
        except semaphore.MasterAuthorityRefused as exc:
            raise runtime.RenewalRefused(f"master renewal refused: {exc}") from exc


# -- shared (schema 2) stack ------------------------------------------------


def _shared_error(exc: Exception) -> StackStoreError:
    return exc if isinstance(exc, StackStoreError) else StackStoreError(str(exc))


def _commit_message(receipt: dict, host: str) -> str:
    payload = receipt.get("payload") or {}
    subject = payload.get("id") or (payload.get("entry") or {}).get("id") or ""
    return (f"goal-stack revision {receipt['revision']}: {receipt['action']}"
            f"{' ' + subject if subject else ''} (host {host})")


def _drop_local_commit(worktree: Path, before_commit: str) -> None:
    shared.git(worktree, "reset", "--hard", "--quiet", before_commit)


def _recover_shared(root: Path, worktree: Path, host: str, *, fault=None) -> bool:
    """Finish an interrupted shared transaction or refuse it explicitly.

    The journal binds the receipt to its snapshot. Its commit is made if
    missing and then published; if another host advanced the chain first,
    the never-published commit is preserved under a ref, the worktree is
    reset to the canonical head, and the write is refused for a re-read.
    Published receipts are never edited or renumbered.
    """
    data = _bytes(worktree, PENDING, absent=True)
    if data is None:
        return False
    try:
        pending = runtime._strict_json(data, "goal-stack transaction")
        _finish(worktree, pending, fault=fault, clear=False, ref_root=root, host=host,
                schema=goal_stack.SHARED_SCHEMA_VERSION)
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise StackStoreError(f"invalid pending stack transaction: {exc}") from exc
    after = pending["after"].encode()
    receipt = pending["receipt"]
    try:
        if shared.show(worktree, "HEAD", NAME) != after:
            shared.commit(worktree, receipt["revision"], _commit_message(receipt, host))
        local = shared.head(worktree)
        remote_head = shared.fetch(worktree)
        if local != remote_head and not shared.is_ancestor(worktree, local, remote_head):
            published = False
            if shared.is_ancestor(worktree, remote_head, local):
                published, _ = shared.push(worktree)
            if not published:
                ref = shared.preservation_ref(local)
                shared.git(worktree, "update-ref", ref, local)
                shared.git(worktree, "reset", "--hard", "--quiet", remote_head)
                _safe_path(worktree, PENDING).unlink()
                runtime._fsync_directory(worktree)
                raise StackStoreError(
                    f"unpublished stack write (revision {receipt['revision']}, "
                    f"{receipt['action']}) was superseded on the canonical branch; "
                    f"preserved at {ref}; re-read and re-apply")
        elif local != remote_head:
            shared.sync(worktree)
    except shared.SharedStackError as exc:
        raise _shared_error(exc) from exc
    runtime._fault(fault, "stack:before-clear")
    _safe_path(worktree, PENDING).unlink()
    runtime._fsync_directory(worktree)
    return True


def _mutate_shared(root: Path, action: str, payload: dict, expected_revision, lease,
                   host: str, fault) -> dict[str, Any]:
    if action == "init":
        raise StackStoreError("goal stack already initialized (shared); use explicit mutations")
    worktree = shared_root(root)
    recovered = _recover_shared(root, worktree, host, fault=fault)
    if action == "recover":
        try:
            shared.sync(worktree)
        except shared.SharedStackError as exc:
            raise _shared_error(exc) from exc
        return {"status": "OK", "recovered": recovered, "stack": _read_shared(root, host)}
    # The revision this host last read: the explicit expectation, else the
    # local committed head before fetching what other hosts published.
    local = _read_shared(root, host)
    prepared = expected_revision if expected_revision is not None else local["revision"]
    for attempt in range(MAX_PUBLISH_ATTEMPTS):
        try:
            shared.sync(worktree)
        except shared.SharedStackError as exc:
            raise _shared_error(exc) from exc
        before = _bytes(worktree, NAME)
        current = _read_shared(root, host)
        replayed = None
        if prepared > current["revision"]:
            raise StackStoreError("stack revision changed; reread before applying this edit")
        if prepared < current["revision"]:
            hosts = shared.receipt_hosts(worktree, prepared, current["revision"])
            where = "stack advanced on host %s (revision %d -> %d)" % (
                ", ".join(hosts) or "unknown", prepared, current["revision"])
            if action not in {"push", "move", "reorder", "update", "complete", "retire"}:
                raise StackStoreError(f"{where}; re-read")
            try:
                base = shared.stack_at_revision(worktree, prepared)
            except shared.SharedStackError as exc:
                raise _shared_error(exc) from exc
            if base is None:
                raise StackStoreError(f"{where}; revision {prepared} is not on the shared chain; re-read")
            conflict = goal_stack.replay_conflict(base, current, action, payload)
            if conflict is not None:
                raise StackStoreError(f"{where}: {conflict}; re-read before applying this edit")
            replayed = prepared
        candidate, archive = goal_stack.apply(current, action, payload, root, host=host)
        after = goal_stack.dumps(candidate).encode()
        if after == before:
            return {"status": "OK", "changed": False, "stack": candidate}
        actor = runtime._actor_from_snapshot(lease)
        actor["host"] = host
        receipt = {
            "schema_version": 1, "action": action, "payload": payload,
            "revision": candidate["revision"], "before_sha256": _digest(before),
            "after_sha256": _digest(after), "actor": actor,
            "timestamp": datetime.now(timezone.utc).isoformat(), "archive": archive,
            "prepared_revision": prepared,
        }
        pending = {"schema_version": 1, "before": _digest(before),
                   "after": after.decode(), "receipt": receipt}
        pending_bytes = _json(pending)
        if max(len(after), len(pending_bytes), len(_json(receipt))) > MAX_BYTES:
            raise StackStoreError("stack transaction exceeds the bounded file size")
        try:
            base_commit = shared.head(worktree)
            _publish(worktree, PENDING, pending_bytes, fault)
            _finish(worktree, pending, fault=fault, clear=False, ref_root=root, host=host,
                    schema=goal_stack.SHARED_SCHEMA_VERSION)
            shared.commit(worktree, candidate["revision"], _commit_message(receipt, host))
            runtime._fault(fault, "shared:before-push")
            published, detail = shared.push(worktree)
            if not published:
                # Never canonical: drop it, then re-prepare against the new
                # head (replay rule) or refuse.
                _drop_local_commit(worktree, base_commit)
                _safe_path(worktree, PENDING).unlink()
                runtime._fsync_directory(worktree)
                try:
                    remote_head = shared.fetch(worktree)
                except shared.SharedStackError as exc:
                    raise StackStoreError(f"could not publish the stack write; nothing written: {exc}") from exc
                if remote_head == base_commit:
                    raise StackStoreError(f"could not publish the stack write; nothing written: {detail}")
                continue
        except shared.SharedStackError as exc:
            raise _shared_error(exc) from exc
        runtime._fault(fault, "stack:before-clear")
        _safe_path(worktree, PENDING).unlink()
        runtime._fsync_directory(worktree)
        return {"status": "OK", "changed": True, "stack": candidate, "archive": archive,
                "recovered": recovered, "replayed_from": replayed, "host": host}
    raise StackStoreError("stack kept advancing on other hosts; re-read and retry")


def sync(record_root: Path, *, writer=None, host=None, preserve_unpublished=False) -> dict[str, Any]:
    """Fetch and fast-forward this host's copy of the shared stack (master only)."""
    writer = writer or runtime.RecordWriter(record_root)
    if writer.root != record_root:
        raise StackStoreError("writer and stack must use the same master record")
    runtime._renew_or_refuse(writer.renew)
    runtime._normalize_lock_prerequisites(record_root)
    with runtime._locked_record(record_root):
        try:
            with writer.authority_transaction():
                root = record_root.parent
                if not is_shared(root):
                    raise StackStoreError("goal stack is not shared; nothing to sync")
                host = _require_host(host)
                worktree = shared_root(root)
                recovered = _recover_shared(root, worktree, host)
                preserved = None
                try:
                    if preserve_unpublished:
                        preserved = shared.preserve_unpublished(worktree)
                    incoming = shared.sync(worktree)
                except shared.SharedStackError as exc:
                    raise _shared_error(exc) from exc
                stack = _read_shared(root, host)
                return {"status": "OK", "recovered": recovered, "preserved": preserved,
                        "incoming": [r.get("revision") for r in incoming], "stack": stack}
        except semaphore.MasterAuthorityRefused as exc:
            raise runtime.RenewalRefused(f"master renewal refused: {exc}") from exc
