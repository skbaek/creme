"""Authenticated, crash-recoverable publication of the one live goal stack.

The existing master mutex and lease transaction serialize writers. A small
write-ahead record binds the canonical TOML replacement to its history receipt;
readers refuse an unfinished transaction rather than choosing either queue.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import goal_stack, master_runtime as runtime, semaphore

NAME = "goal-stack.toml"
PENDING = ".goal-stack-pending.json"
PROCEDURE = "master-goal-stack-v1"
MAX_BYTES = 4 * 1024 * 1024


class StackStoreError(runtime.MasterRecordError):
    pass


def adopted(events) -> bool:
    return any(e["kind"] == "procedure"
               and e["payload"]["procedure_id"] == PROCEDURE
               and e["payload"]["action"] in {"add", "replace"} for e in events)


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


def read_unlocked(record_root: Path) -> dict:
    root = record_root.parent
    if _bytes(root, PENDING, absent=True) is not None:
        raise StackStoreError("unfinished goal-stack transaction; master stack recover before scheduling")
    try:
        return goal_stack.loads(_bytes(root, NAME).decode("utf-8"), root)
    except (ValueError, UnicodeError) as exc:
        raise StackStoreError(str(exc)) from exc


def read(record_root: Path) -> dict:
    with runtime._record_lock(record_root, exclusive=False):
        return read_unlocked(record_root)


def _publish(root: Path, relative: str, data: bytes, fault=None) -> None:
    path = _safe_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(root, relative)
    runtime._atomic_replace(path, data, label=relative, fault=fault)


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _finish(root: Path, pending: dict, *, fault=None) -> None:
    if (not isinstance(pending, dict)
            or set(pending) != {"schema_version", "before", "after", "receipt"}
            or type(pending["schema_version"]) is not int or pending["schema_version"] != 1):
        raise StackStoreError("invalid stack transaction schema")
    after = pending["after"]
    receipt = pending["receipt"]
    if not isinstance(after, str) or not isinstance(receipt, dict):
        raise StackStoreError("invalid stack transaction contents")
    snapshot = goal_stack.loads(after, root)
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
           writer=None, fault=None) -> dict[str, Any]:
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
                recovered = _recover(record_root, fault=fault)
                root = record_root.parent
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
                receipt = {
                    "schema_version": 1, "action": action, "payload": payload,
                    "revision": candidate["revision"], "before_sha256": _digest(before),
                    "after_sha256": _digest(after), "actor": runtime._actor_from_snapshot(lease),
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
