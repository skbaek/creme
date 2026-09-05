from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import random
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest import mock

from creme import master_migrate, master_operations, master_runtime, semaphore
from creme.adapters.base import Adapter


SEQUENTIAL_SEEDS = (0x51CC, 0xC0DE, 0x5EED)
CONCURRENT_SEED = 0xC011AB
LEASE_SECONDS = 60


@dataclass(frozen=True)
class Principal:
    name: str
    client: str
    family: Optional[str]
    pid: Optional[int]
    session_digest: Optional[str]
    liveness_digest: Optional[str] = None
    liveness_socket: Optional[str] = None


@dataclass(frozen=True)
class Action:
    operation: str
    principal: str = "process-a"
    argument: str = ""

    def __str__(self) -> str:
        suffix = f":{self.argument}" if self.argument else ""
        return f"{self.operation}@{self.principal}{suffix}"


@dataclass
class ReferenceLease:
    holder: str
    client: str
    generation: int
    expires_at: float
    direct_activity_at: float
    session_digest: Optional[str]
    liveness_digest: Optional[str]
    heartbeat_renewals: int = 0


@dataclass
class ReferencePending:
    generation: int
    expires_at: float


@dataclass
class ReferenceBinding:
    generation: int
    holder: str


class ReferenceModel:
    """Small executable model for the lease/record composition."""

    def __init__(self, principals: dict[str, Principal], now: float = 1_000.0):
        self.principals = principals
        self.now = now
        self.alive = {p.pid for p in principals.values() if p.pid is not None}
        self.listener_up = {
            p.liveness_socket for p in principals.values() if p.liveness_socket
        }
        self.lease: Optional[ReferenceLease] = None
        self.pending: Optional[ReferencePending] = None
        self.binding: Optional[ReferenceBinding] = None
        self.generation = 0
        self.events: list[tuple[str, int]] = []
        self.started: set[int] = set()
        self.board_current = True
        self.publication_pending = False
        self.lease_encoding = "current"
        self.record_encoding = "current"

    def _principal(self, name: str) -> Principal:
        return self.principals[name]

    def _same_holder(self, name: str) -> bool:
        if self.lease is None:
            return False
        holder = self._principal(self.lease.holder)
        acting = self._principal(name)
        if self.lease.session_digest is not None:
            return acting.session_digest == self.lease.session_digest
        if holder.pid is None:
            return False
        return acting.pid == holder.pid

    def _state(self) -> str:
        if self.lease is None:
            return "none"
        holder = self._principal(self.lease.holder)
        if holder.pid is not None and holder.pid not in self.alive:
            return "stranded"
        if self.now <= self.lease.expires_at:
            return "live"
        return "lapsed" if holder.pid is not None else "stranded"

    def _new_lease(self, name: str) -> None:
        principal = self._principal(name)
        self.generation += 1
        liveness = (
            principal.liveness_digest
            if principal.liveness_socket in self.listener_up
            else None
        )
        self.lease = ReferenceLease(
            holder=name,
            client=principal.client,
            generation=self.generation,
            expires_at=self.now + LEASE_SECONDS,
            direct_activity_at=self.now,
            session_digest=principal.session_digest,
            liveness_digest=liveness,
        )
        self.pending = None
        self.lease_encoding = "current"

    def _direct_renew(self, name: str) -> bool:
        if self.lease_encoding == "malformed":
            return False
        if not self._same_holder(name):
            return False
        assert self.lease is not None
        self.lease.expires_at = self.now + LEASE_SECONDS
        self.lease.direct_activity_at = self.now
        self.lease.heartbeat_renewals = 0
        self.lease_encoding = "current"
        return True

    def _record_event(self, kind: str) -> None:
        assert self.lease is not None
        self.events.append((kind, self.lease.generation))
        self.board_current = True
        self.publication_pending = False

    def transition(self, action: Action) -> str:
        name = action.principal
        if action.operation == "kill":
            pid = self._principal(name).pid
            if pid is not None:
                self.alive.discard(pid)
            return "ok"
        if action.operation == "legacy-lease":
            if self.lease is not None or self.lease_encoding == "malformed":
                return "refused"
            if self._principal(name).pid is None:
                return "refused"
            self._new_lease(name)
            self.lease_encoding = "legacy"
            return "ok"
        if action.operation == "malformed-lease":
            if self.lease is not None:
                return "refused"
            self.lease_encoding = "malformed"
            return "ok"
        if action.operation == "legacy-record":
            if self.record_encoding != "current":
                return "refused"
            self.record_encoding = "legacy"
            self.board_current = False
            return "ok"
        if action.operation == "malformed-record":
            if self.record_encoding != "current":
                return "refused"
            self.record_encoding = "malformed"
            self.board_current = False
            return "ok"
        if action.operation == "post-log-crash":
            if self.record_encoding != "current" or not self._direct_renew(name):
                return "refused"
            self._record_event("note")
            self.board_current = False
            self.publication_pending = True
            return "crash"
        if action.operation == "migrate-record":
            if self.record_encoding != "legacy" or not self._direct_renew(name):
                return "refused"
            self.record_encoding = "current"
            self.board_current = True
            return "ok"
        if action.operation == "advance":
            self.now += float(action.argument)
            return "ok"
        if action.operation == "lapse":
            self.now = (
                self.now + LEASE_SECONDS + 1
                if self.lease is None
                else max(self.now, self.lease.expires_at + 1)
            )
            return "ok"
        if action.operation == "listener-down":
            listener = self._principal(name).liveness_socket
            if listener is not None:
                self.listener_up.discard(listener)
            return "ok"
        if self.lease_encoding == "malformed":
            if action.operation in {"event", "recover"}:
                return "refused"
            return "error"
        if action.operation == "acquire":
            if self.lease is not None:
                return "refused"
            self._new_lease(name)
            return "ok"
        if action.operation == "takeover":
            if self.lease is not None and self._state() == "live":
                return "refused"
            self._new_lease(name)
            return "ok"
        if action.operation == "renew":
            return "ok" if self._direct_renew(name) else "refused"
        if action.operation == "release":
            if self.lease is None:
                return "refused"
            if self._state() == "live" and not self._same_holder(name):
                return "refused"
            self.lease = None
            self.pending = None
            self.lease_encoding = "current"
            return "ok"
        if action.operation == "prepare":
            if not self._same_holder(name):
                return "refused"
            if self.pending is not None and self.now <= self.pending.expires_at:
                return "refused"
            assert self.lease is not None
            self.pending = ReferencePending(
                self.lease.generation,
                self.now + semaphore.MASTER_HEARTBEAT_LAUNCH_SECONDS,
            )
            return "ok"
        if action.operation == "consume":
            if self.pending is None or action.argument != "correct":
                return "refused"
            pending = self.pending
            self.pending = None
            if self.lease is None or pending.generation != self.lease.generation:
                return "refused"
            if self.now > pending.expires_at:
                return "refused"
            acting = self._principal(name)
            if (
                self.lease.session_digest is not None
                and acting.session_digest != self.lease.session_digest
            ):
                return "refused"
            acting_liveness = (
                acting.liveness_digest
                if acting.liveness_socket in self.listener_up
                else None
            )
            if (
                self.lease.liveness_digest is not None
                and acting_liveness != self.lease.liveness_digest
            ):
                return "refused"
            self.binding = ReferenceBinding(self.lease.generation, self.lease.holder)
            return "ok"
        if action.operation == "heartbeat":
            if self.lease is None or self.binding is None:
                return "refused"
            if self.binding.generation != self.lease.generation:
                return "refused"
            holder = self._principal(self.binding.holder)
            if holder.pid is not None and holder.pid not in self.alive:
                return "refused"
            bounded = holder.pid is None and self.lease.liveness_digest is None
            if bounded and (
                self.now
                > self.lease.direct_activity_at
                + semaphore.MASTER_UNVERIFIED_HEARTBEAT_GRACE_SECONDS
                or self.lease.heartbeat_renewals
                >= semaphore.MASTER_UNVERIFIED_HEARTBEAT_RENEWALS
            ):
                return "refused"
            self.lease.expires_at = self.now + LEASE_SECONDS
            self.lease.heartbeat_renewals += 1
            return "ok"
        if action.operation == "event" or action.operation == "recover":
            if self.record_encoding != "current":
                return "refused"
            if not self._direct_renew(name):
                return "refused"
            self._record_event("note")
            return "ok"
        if action.operation == "digest":
            return "ok" if self.record_encoding == "current" else "error"
        if action.operation == "stale-board":
            if self.record_encoding != "current":
                return "error"
            if self.events:
                self.board_current = False
            return "ok"
        if action.operation == "start":
            if self.record_encoding != "current":
                return "error"
            take_over = action.argument == "takeover"
            if self.lease is None:
                self._new_lease(name)
            elif not self._direct_renew(name):
                if self._state() == "live":
                    return "reader"
                if not take_over:
                    return "takeover-required"
                self._new_lease(name)
            if not self._direct_renew(name):
                # start_master attempts the normal authenticated release.  A
                # deliberately identity-free acquisition cannot authenticate
                # that cleanup either, so it remains a non-authoritative live
                # lease until lapse/takeover; no record event is created.
                return "error"
            assert self.lease is not None
            if self.lease.generation not in self.started:
                self.started.add(self.lease.generation)
                self._record_event("master")
            else:
                self.board_current = True
            return "master"
        raise AssertionError(f"unknown model action: {action}")


class SyntheticAdapter(Adapter):
    system = "synthetic"


class ConcreteWorld:
    """Synthetic private state driven through the real frozen operations."""

    def __init__(self, seed: int):
        self.seed = seed
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.state_root = self.root / "lease-state"
        self.record_root = self.root / "record"
        self.adapter = SyntheticAdapter()
        self.now = 1_000.0
        self.principals = make_principals()
        self.alive = {p.pid for p in self.principals.values() if p.pid is not None}
        self.listener_up = {
            p.liveness_socket for p in self.principals.values() if p.liveness_socket
        }
        self.local = threading.local()
        self.local.principal = "process-a"
        self.last_capability: Optional[str] = None
        self.binding: Optional[semaphore._HeartbeatBinding] = None
        self.lease_generations: dict[str, int] = {}
        self.actor_generations: dict[str, int] = {}
        self.event_counter = 0
        self.event_counter_lock = threading.Lock()
        self.patchers: list[mock._patch] = []
        self.lease_encoding = "current"
        self.record_encoding = "current"

    def __enter__(self) -> "ConcreteWorld":
        self.patchers = [
            mock.patch.dict(
                "os.environ",
                {
                    "CREME_SEMAPHORE_DIR": str(self.state_root),
                    "CREME_MASTER_SESSION_ID": "",
                    "CREME_MASTER_LIVENESS_SOCKET": "",
                    "CODEX_SESSION_ID": "",
                    "CODEX_THREAD_ID": "",
                },
                clear=False,
            ),
            mock.patch("creme.semaphore._now", side_effect=lambda: self.now),
            mock.patch("creme.semaphore._client_process", side_effect=self._client_process),
            mock.patch("creme.semaphore._client_session", side_effect=self._client_session),
            mock.patch("creme.semaphore._session_socket_live", side_effect=self._socket_live),
            mock.patch("creme.semaphore._pid_alive", side_effect=lambda pid: pid in self.alive),
        ]
        for patcher in self.patchers:
            patcher.start()
        master_runtime.initialize_empty_record(self.record_root)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def active(self) -> Principal:
        return self.principals[getattr(self.local, "principal", "process-a")]

    def select(self, name: str) -> None:
        self.local.principal = name

    def _client_process(self, _adapter, start_pid=None):
        principal = self.active()
        if principal.pid is None:
            return None, principal.family, f"synthetic {principal.name} without process"
        return principal.pid, principal.family, f"synthetic {principal.name} pid"

    def _client_session(self, _client) -> semaphore._ClientSession:
        principal = self.active()
        live = principal.liveness_socket in self.listener_up
        return semaphore._ClientSession(
            principal.session_digest,
            principal.liveness_digest if live else None,
            principal.liveness_socket if live else None,
            f"synthetic {principal.name} identity",
        )

    def _socket_live(self, path: Optional[str]) -> bool:
        return path is not None and path in self.listener_up

    def _renew(self):
        return semaphore.master_renew(adapter=self.adapter)

    def _authority_transaction(self):
        return semaphore.master_authority_transaction(adapter=self.adapter)

    def _release(self):
        return semaphore.master_release(adapter=self.adapter)

    def _acquire(self, client, note, *, take_over=False):
        return semaphore.master_acquire(
            client,
            note,
            lease=LEASE_SECONDS,
            take_over=take_over,
            adapter=self.adapter,
        )

    def _status(self) -> str:
        snapshot = semaphore.master_snapshot()
        lease = snapshot["lease"]
        view = semaphore._master_view(lease, self.now)
        if lease is None:
            return "master: none\n"
        return f"master: {lease['client']} ({view['state']})\n"

    def _next_event_id(self) -> str:
        with self.event_counter_lock:
            self.event_counter += 1
            return f"{self.event_counter:032x}"

    def writer(self) -> master_runtime.RecordWriter:
        return master_runtime.RecordWriter(
            self.record_root,
            renew=self._renew,
            lease_snapshot=semaphore.master_snapshot,
            authority_transaction=self._authority_transaction,
            event_id=self._next_event_id,
        )

    def append_then_die(self, payload: dict[str, str], stage: str) -> int:
        """Publish in a real child process and terminate at the selected boundary."""
        event_id = self._next_event_id()
        pid = os.fork()
        if pid == 0:
            writer = master_runtime.RecordWriter(
                self.record_root,
                renew=self._renew,
                lease_snapshot=semaphore.master_snapshot,
                authority_transaction=self._authority_transaction,
                event_id=lambda: event_id,
            )

            def die(candidate: str) -> None:
                if candidate == stage:
                    os._exit(71)

            writer.append("note", payload, fault=die)
            os._exit(99)
        waited, status = os.waitpid(pid, 0)
        if waited != pid or not os.WIFEXITED(status):
            raise AssertionError("synthetic publication child did not exit normally")
        return os.WEXITSTATUS(status)

    @staticmethod
    def note_payload(index: int) -> dict[str, str]:
        return {
            "title": f"model-note-{index}",
            "note": "synthetic transition observation",
            "evidence": f"evidence/model-{index}.json",
            "next_unit": f"unit-{index}",
        }

    def record_bytes(self) -> tuple[tuple[str, int, int, Optional[bytes]], ...]:
        rows = []
        for path in [self.record_root, *sorted(self.record_root.rglob("*"))]:
            info = path.lstat()
            data = path.read_bytes() if path.is_file() else None
            rows.append(
                (
                    path.relative_to(self.record_root).as_posix(),
                    info.st_mode,
                    info.st_nlink,
                    data,
                )
            )
        return tuple(rows)

    def _register_lease(self) -> None:
        snapshot = semaphore.master_snapshot()
        lease = snapshot["lease"]
        if lease is None:
            return
        lease_id = lease["lease_id"]
        if lease_id not in self.lease_generations:
            generation = len(self.lease_generations) + 1
            self.lease_generations[lease_id] = generation
            digest = hashlib.sha256(
                master_runtime._ACQUISITION_DOMAIN + lease_id.encode("ascii")
            ).hexdigest()
            self.actor_generations[digest] = generation

    def execute(self, action: Action) -> str:
        self.select(action.principal)
        if action.operation == "kill":
            pid = self.active().pid
            if pid is not None:
                self.alive.discard(pid)
            return "ok"
        if action.operation == "legacy-lease":
            if self.lease_encoding == "malformed":
                return "refused"
            try:
                if semaphore.master_snapshot()["lease"] is not None or self.active().pid is None:
                    return "refused"
            except semaphore.SemaphoreError:
                return "refused"
            self.state_root.mkdir(parents=True, exist_ok=True)
            lease_path = self.state_root / semaphore.MASTER_NAME
            legacy = {
                "schema_version": 1,
                "lease": {
                    "client": self.active().client,
                    "client_pid": self.active().pid,
                    "pid": 20_001,
                    "uid": 501,
                    "note": "synthetic legacy lease",
                    "acquired_at": self.now - 10,
                    "renewed_at": self.now - 5,
                    "lease_seconds": LEASE_SECONDS,
                },
            }
            lease_path.write_bytes(json.dumps(legacy, separators=(",", ":")).encode())
            self.lease_encoding = "legacy"
            self._register_lease()
            return "ok"
        if action.operation == "malformed-lease":
            try:
                if semaphore.master_snapshot()["lease"] is not None:
                    return "refused"
            except semaphore.SemaphoreError:
                return "refused"
            self.state_root.mkdir(parents=True, exist_ok=True)
            (self.state_root / semaphore.MASTER_NAME).write_bytes(b"{malformed lease")
            self.lease_encoding = "malformed"
            return "ok"
        if action.operation == "legacy-record":
            if self.record_encoding != "current":
                return "refused"
            view = master_runtime.read_record(self.record_root)
            for name in (
                master_runtime.EVENTS_NAME,
                master_runtime.BOARD_NAME,
                master_runtime.README_NAME,
            ):
                (self.record_root / name).unlink()
            (self.record_root / "README.md").write_bytes(b"# Synthetic legacy record\n")
            (self.record_root / "log.md").write_bytes(view.log_bytes)
            (self.record_root / "board.md").write_bytes(b"synthetic legacy board\n")
            for name in ("README.md", "log.md", "board.md"):
                (self.record_root / name).chmod(0o600)
            self.record_encoding = "legacy"
            return "ok"
        if action.operation == "malformed-record":
            if self.record_encoding != "current":
                return "refused"
            (self.record_root / master_runtime.EVENTS_NAME).write_bytes(
                b"{malformed record"
            )
            self.record_encoding = "malformed"
            return "ok"
        if action.operation == "post-log-crash":
            if self.record_encoding != "current":
                return "refused"

            try:
                exit_code = self.append_then_die(
                    self.note_payload(self.event_counter + 1),
                    "board:after-fsync",
                )
            except master_runtime.MasterRecordError:
                return "refused"
            if exit_code != 71:
                raise AssertionError(
                    f"post-log crash boundary was not reached: exit {exit_code}"
                )
            return "crash"
        if action.operation == "migrate-record":
            if self.record_encoding != "legacy":
                return "refused"
            result = master_migrate.migrate(
                self.record_root,
                apply=True,
                renew=self._renew,
                authority_transaction=self._authority_transaction,
            )
            if result.status == "OK":
                self.record_encoding = "current"
                return "ok"
            return "refused"
        if action.operation == "advance":
            self.now += float(action.argument)
            return "ok"
        if action.operation == "lapse":
            lease = semaphore.master_snapshot()["lease"]
            self.now = (
                self.now + LEASE_SECONDS + 1
                if lease is None
                else max(
                    self.now,
                    float(lease["renewed_at"]) + int(lease["lease_seconds"]) + 1,
                )
            )
            return "ok"
        if action.operation == "listener-down":
            listener = self.principals[action.principal].liveness_socket
            if listener is not None:
                self.listener_up.discard(listener)
            return "ok"
        if self.lease_encoding == "malformed":
            if action.operation in {"event", "recover"}:
                return "refused"
            return "error"
        if action.operation in {"acquire", "takeover"}:
            ok, _detail = self._acquire(
                self.active().client,
                f"seed-{self.seed}",
                take_over=action.operation == "takeover",
            )
            if ok:
                self._register_lease()
            return "ok" if ok else "refused"
        if action.operation == "renew":
            ok, _detail = self._renew()
            if ok:
                self.lease_encoding = "current"
            return "ok" if ok else "refused"
        if action.operation == "release":
            ok, _detail = self._release()
            if ok:
                self.lease_encoding = "current"
            return "ok" if ok else "refused"
        if action.operation == "prepare":
            ok, _detail, prepared = semaphore._prepare_master_heartbeat_launch(
                self.adapter
            )
            if ok and prepared is not None:
                self.last_capability = prepared.capability
            return "ok" if ok else "refused"
        if action.operation == "consume":
            capability = (
                self.last_capability
                if action.argument == "correct" and self.last_capability is not None
                else "f" * 64
            )
            ok, _detail, binding = semaphore._consume_master_heartbeat_launch(
                capability
            )
            if ok and binding is not None:
                self.binding = binding
            return "ok" if ok else "refused"
        if action.operation == "heartbeat":
            if self.binding is None:
                return "refused"
            ok, _detail = semaphore._renew_master_heartbeat(
                self.binding,
                adapter=self.adapter,
            )
            return "ok" if ok else "refused"
        if action.operation in {"event", "recover"}:
            if self.record_encoding != "current":
                return "refused"
            try:
                self.writer().append(
                    "note", self.note_payload(self.event_counter + 1)
                )
            except master_runtime.MasterRecordError:
                return "refused"
            return "ok"
        if action.operation == "digest":
            if self.record_encoding != "current":
                return "error"
            try:
                digest = master_operations.digest_record(
                    self.record_root,
                    lease_snapshot=semaphore.master_snapshot,
                    lease_status=self._status,
                )
            except (master_runtime.MasterRecordError, master_operations.MasterOperationError):
                return "error"
            if digest["role"]["authoritative"] is not False:
                raise AssertionError("digest treated record role as authority")
            return "ok"
        if action.operation == "stale-board":
            if self.record_encoding != "current":
                return "error"
            view = master_runtime.read_record(self.record_root)
            if view.events:
                prior = master_runtime.render_board(view.events[:-1])
                (self.record_root / master_runtime.BOARD_NAME).write_bytes(prior)
            return "ok"
        if action.operation == "start":
            if self.record_encoding != "current":
                return "error"
            try:
                result = master_operations.start_master(
                    self.record_root,
                    client=self.active().client,
                    model="synthetic-model",
                    effort="synthetic-effort",
                    note=f"seed-{self.seed}",
                    take_over=action.argument == "takeover",
                    acquire=self._acquire,
                    renew=self._renew,
                    release=self._release,
                    heartbeat=lambda _interval: (True, "synthetic heartbeat"),
                    lease_snapshot=semaphore.master_snapshot,
                    lease_status=self._status,
                    authority_transaction=self._authority_transaction,
                )
            except (master_runtime.MasterRecordError, master_operations.MasterOperationError):
                return "error"
            if result["status"] == "master":
                self._register_lease()
            return result["status"]
        raise AssertionError(f"unknown concrete action: {action}")

    def state(self) -> dict[str, object]:
        lease_path = self.state_root / semaphore.MASTER_NAME
        actual_lease_encoding = "current"
        if lease_path.exists():
            try:
                raw_lease = json.loads(lease_path.read_bytes())
                if raw_lease.get("schema_version") == 1:
                    actual_lease_encoding = "legacy"
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                actual_lease_encoding = "malformed"
        if actual_lease_encoding == "malformed":
            lease = None
        else:
            self._register_lease()
            lease = semaphore.master_snapshot()["lease"]
        generation = (
            None if lease is None else self.lease_generations.get(lease["lease_id"])
        )
        try:
            view = master_runtime.read_record(self.record_root)
            actual_record_encoding = "current"
            events = view.events
            board_current = view.board_current
            publication_pending = view.publication_transaction is not None
        except master_runtime.MasterRecordError:
            if not (self.record_root / master_runtime.EVENTS_NAME).exists() and (
                self.record_root / "log.md"
            ).exists():
                actual_record_encoding = "legacy"
                data = (self.record_root / "log.md").read_bytes()
                translated, _rows, ambiguity = master_migrate._recognize_log(data)
                events = tuple(
                    master_runtime.validate_event(
                        master_runtime._strict_json(row, "synthetic legacy row")
                    )
                    for row in translated.splitlines(keepends=True)
                ) if ambiguity is None else ()
            else:
                actual_record_encoding = "malformed"
                events = ()
            board_current = False
            publication_pending = False
        event_rows = [
            (
                event["kind"],
                self.actor_generations.get(event["actor"]["acquisition_digest"], -1),
            )
            for event in events
        ]
        return {
            "lease_encoding": actual_lease_encoding,
            "record_encoding": actual_record_encoding,
            "lease_generation": generation,
            "lease_client": None if lease is None else lease["client"],
            "pending": False if lease is None else lease["heartbeat_launch_digest"] is not None,
            "events": event_rows,
            "event_ids": [event["event_id"] for event in events],
            "board_current": board_current,
            "publication_pending": publication_pending,
        }


def make_principals() -> dict[str, Principal]:
    session_a = semaphore._digest_session_value(
        semaphore.MASTER_SESSION_DIGEST_DOMAIN, "synthetic-session-a"
    )
    listener_session = semaphore._digest_session_value(
        semaphore.MASTER_SESSION_DIGEST_DOMAIN, "synthetic-listener-a"
    )
    listener_path = "/synthetic/listener-a.sock"
    listener_digest = semaphore._digest_session_value(
        semaphore.MASTER_LIVENESS_DIGEST_DOMAIN, listener_path
    )
    bounded_session = semaphore._digest_session_value(
        semaphore.MASTER_SESSION_DIGEST_DOMAIN, "synthetic-bounded-a"
    )
    return {
        "process-a": Principal("process-a", "process-a", "claude", 11_001, None),
        "process-b": Principal("process-b", "process-b", "claude", 11_002, None),
        "session-a": Principal("session-a", "session-a", "claude", 11_003, session_a),
        "listener-a": Principal(
            "listener-a",
            "listener-a",
            "codex",
            None,
            listener_session,
            listener_digest,
            listener_path,
        ),
        "bounded-anonymous": Principal(
            "bounded-anonymous",
            "bounded",
            "codex",
            None,
            bounded_session,
        ),
        "anonymous": Principal("anonymous", "anonymous", None, None, None),
    }


def required_prefix() -> list[Action]:
    return [
        Action("acquire", "process-a"),
        Action("renew", "process-a"),
        Action("prepare", "process-a"),
        Action("consume", "process-a", "correct"),
        Action("consume", "process-a", "correct"),
        Action("heartbeat", "process-a"),
        Action("event", "process-a"),
        Action("digest", "anonymous"),
        Action("stale-board", "process-a"),
        Action("digest", "process-b"),
        Action("recover", "process-a"),
        Action("release", "process-a"),
        Action("start", "session-a"),
        Action("start", "process-b"),
        Action("lapse", "process-a"),
        Action("start", "process-b"),
        Action("takeover", "process-b"),
        Action("start", "process-b"),
        Action("event", "session-a"),
        Action("release", "process-b"),
        Action("acquire", "listener-a"),
        Action("renew", "listener-a"),
        Action("release", "listener-a"),
        Action("start", "session-a"),
        Action("lapse", "session-a"),
        Action("start", "bounded-anonymous", "takeover"),
        Action("prepare", "bounded-anonymous"),
        Action("consume", "bounded-anonymous", "correct"),
        Action("heartbeat", "bounded-anonymous"),
        Action("release", "bounded-anonymous"),
        Action("acquire", "anonymous"),
        Action("renew", "anonymous"),
        Action("lapse", "anonymous"),
        Action("release", "process-a"),
    ]


def generated_trace(seed: int, steps: int = 90) -> list[Action]:
    rng = random.Random(seed)
    names = tuple(make_principals())
    operations = (
        "acquire",
        "renew",
        "prepare",
        "consume",
        "heartbeat",
        "release",
        "takeover",
        "start",
        "event",
        "digest",
        "stale-board",
        "recover",
        "lapse",
        "advance",
    )
    trace = required_prefix()
    for _index in range(steps):
        operation = rng.choice(operations)
        principal = rng.choice(names)
        argument = ""
        if operation == "consume":
            argument = rng.choice(("correct", "wrong"))
        elif operation == "start" and rng.randrange(3) == 0:
            argument = "takeover"
        elif operation == "advance":
            argument = str(rng.choice((1, 31, 61, 301, 3_001)))
        trace.append(Action(operation, principal, argument))
    return trace


def generated_recovery_traces(seed: int) -> list[tuple[str, list[Action]]]:
    """Seeded corruption/death/crash traces, each in its own concrete world."""
    rng = random.Random(seed)
    malformed_lease_probes = [
        Action("acquire", "process-b"),
        Action("renew", "process-a"),
        Action("digest", "anonymous"),
        Action("event", "process-a"),
    ]
    malformed_record_probes = [
        Action("digest", "anonymous"),
        Action("start", "process-a"),
        Action("event", "process-a"),
    ]
    rng.shuffle(malformed_lease_probes)
    rng.shuffle(malformed_record_probes)
    traces = [
        (
            "process-death",
            [
                Action("acquire", "process-a"),
                Action("kill", "process-a"),
                Action("takeover", "process-b"),
                Action("event", "process-b"),
            ],
        ),
        (
            "legacy-lease",
            [
                Action("legacy-lease", "process-a"),
                Action("renew", "process-a"),
                Action("release", "process-a"),
            ],
        ),
        (
            "malformed-lease",
            [Action("malformed-lease", "process-a"), *malformed_lease_probes],
        ),
        (
            "post-log-crash",
            [
                Action("acquire", "session-a"),
                Action("post-log-crash", "session-a"),
                Action("digest", "anonymous"),
                Action("recover", "session-a"),
            ],
        ),
        (
            "legacy-record",
            [
                Action("acquire", "process-a"),
                Action("event", "process-a"),
                Action("legacy-record", "process-a"),
                Action("digest", "anonymous"),
                Action("event", "process-a"),
                Action("migrate-record", "process-a"),
                Action("digest", "anonymous"),
            ],
        ),
        (
            "malformed-record",
            [Action("malformed-record", "process-a"), *malformed_record_probes],
        ),
    ]
    rng.shuffle(traces)
    return traces


class ModelMismatch(AssertionError):
    pass


Mutation = Callable[[ConcreteWorld], contextlib.AbstractContextManager[None]]


class MasterTransitionModelTest(unittest.TestCase):
    maxDiff = None

    def assert_world_matches(
        self,
        model: ReferenceModel,
        world: ConcreteWorld,
        action: Action,
    ) -> None:
        actual = world.state()
        lease = model.lease
        self.assertEqual(
            actual["lease_encoding"], model.lease_encoding, f"lease encoding after {action}"
        )
        self.assertEqual(
            actual["record_encoding"], model.record_encoding, f"record encoding after {action}"
        )
        self.assertEqual(
            actual["lease_generation"],
            None if lease is None else lease.generation,
            f"lease generation after {action}",
        )
        self.assertEqual(
            actual["lease_client"],
            None if lease is None else lease.client,
            f"lease holder after {action}",
        )
        self.assertEqual(actual["pending"], model.pending is not None, f"pending after {action}")
        if model.record_encoding != "malformed":
            self.assertEqual(actual["events"], model.events, f"events after {action}")
            event_ids = actual["event_ids"]
            self.assertEqual(len(event_ids), len(set(event_ids)), f"duplicate event after {action}")
        self.assertEqual(actual["board_current"], model.board_current, f"board after {action}")
        self.assertEqual(
            actual["publication_pending"],
            model.publication_pending,
            f"publication after {action}",
        )

    def run_trace(
        self,
        trace: list[Action],
        *,
        seed: int,
        mutation: Optional[Mutation] = None,
    ) -> None:
        with ConcreteWorld(seed) as world:
            model = ReferenceModel(world.principals)
            manager = contextlib.nullcontext() if mutation is None else mutation(world)
            with manager:
                for index, action in enumerate(trace):
                    before = world.record_bytes()
                    expected = model.transition(action)
                    actual = world.execute(action)
                    prefix = ", ".join(str(item) for item in trace[: index + 1])
                    if actual != expected:
                        raise ModelMismatch(
                            f"seed={seed} step={index} trace=[{prefix}] "
                            f"expected={expected} actual={actual}"
                        )
                    if expected in {"refused", "reader", "takeover-required"} or action.operation == "digest":
                        if world.record_bytes() != before:
                            raise ModelMismatch(
                                f"seed={seed} step={index} trace=[{prefix}] reader/refusal mutated record"
                            )
                    try:
                        self.assert_world_matches(model, world, action)
                    except AssertionError as exc:
                        raise ModelMismatch(
                            f"seed={seed} step={index} trace=[{prefix}]: {exc}"
                        ) from exc

    def test_seeded_sequential_traces_match_reference_model(self):
        prefix = required_prefix()
        self.assertTrue(
            {
                "acquire",
                "renew",
                "prepare",
                "consume",
                "release",
                "lapse",
                "takeover",
                "start",
                "event",
                "digest",
                "recover",
            }.issubset({action.operation for action in prefix})
        )
        self.assertTrue(
            {
                "process-a",
                "process-b",
                "session-a",
                "listener-a",
                "bounded-anonymous",
                "anonymous",
            }.issubset({action.principal for action in prefix})
        )
        for seed in SEQUENTIAL_SEEDS:
            with self.subTest(seed=seed):
                self.run_trace(generated_trace(seed), seed=seed)

    def test_seeded_process_legacy_malformed_and_crash_traces_match_model(self):
        required = {
            "kill",
            "legacy-lease",
            "malformed-lease",
            "legacy-record",
            "malformed-record",
            "post-log-crash",
            "migrate-record",
            "recover",
        }
        for seed in SEQUENTIAL_SEEDS:
            traces = generated_recovery_traces(seed)
            observed = {
                action.operation
                for _name, trace in traces
                for action in trace
            }
            self.assertTrue(required.issubset(observed), f"seed={seed}")
            for offset, (name, trace) in enumerate(traces):
                with self.subTest(seed=seed, state_trace=name):
                    self.run_trace(trace, seed=seed + offset + 1)

    def test_seeded_concurrent_consume_start_and_event_campaigns(self):
        rng = random.Random(CONCURRENT_SEED)

        with ConcreteWorld(CONCURRENT_SEED) as world:
            model = ReferenceModel(world.principals)
            self.assertEqual(world.execute(Action("acquire", "process-a")), "ok")
            self.assertEqual(model.transition(Action("acquire", "process-a")), "ok")
            self.assertEqual(world.execute(Action("prepare", "process-a")), "ok")
            self.assertEqual(model.transition(Action("prepare", "process-a")), "ok")
            actions = [Action("consume", "process-a", "correct") for _ in range(12)]
            rng.shuffle(actions)
            barrier = threading.Barrier(len(actions))
            results: list[str] = []
            lock = threading.Lock()

            def consume(action: Action) -> None:
                barrier.wait()
                result = world.execute(action)
                with lock:
                    results.append(result)

            threads = [threading.Thread(target=consume, args=(action,)) for action in actions]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(results.count("ok"), 1, f"seed={CONCURRENT_SEED}")
            self.assertEqual(results.count("refused"), 11, f"seed={CONCURRENT_SEED}")
            expected = [model.transition(action) for action in actions]
            self.assertEqual(expected.count("ok"), results.count("ok"))
            self.assertEqual(expected.count("refused"), results.count("refused"))
            self.assert_world_matches(model, world, actions[-1])

        with ConcreteWorld(CONCURRENT_SEED + 1) as world:
            names = ["process-a", "process-b", "session-a", "listener-a"]
            rng.shuffle(names)
            serial_histories = []
            for order in itertools.permutations(names):
                candidate = ReferenceModel(world.principals)
                outcome = {
                    name: candidate.transition(Action("start", name))
                    for name in order
                }
                serial_histories.append((order, outcome, candidate))
            self.assertEqual(len(serial_histories), 24)
            self.assertEqual(
                {
                    tuple(outcome[name] for name in names)
                    for _order, outcome, _candidate in serial_histories
                },
                {
                    tuple("master" if name == winner else "reader" for name in names)
                    for winner in names
                },
            )
            barrier = threading.Barrier(len(names))
            start_results: list[tuple[str, str]] = []
            lock = threading.Lock()

            def start(name: str) -> None:
                barrier.wait()
                result = world.execute(Action("start", name))
                with lock:
                    start_results.append((name, result))

            threads = [threading.Thread(target=start, args=(name,)) for name in names]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            statuses = [status for _name, status in start_results]
            self.assertEqual(statuses.count("master"), 1, f"seed={CONCURRENT_SEED + 1}")
            self.assertEqual(statuses.count("reader"), 3, f"seed={CONCURRENT_SEED + 1}")
            observed = dict(start_results)
            matching = [
                (order, candidate)
                for order, outcome, candidate in serial_histories
                if outcome == observed
            ]
            self.assertTrue(
                matching,
                f"seed={CONCURRENT_SEED + 1}: no legal serial history for {observed}",
            )
            final_state_matched = False
            mismatches = []
            for order, candidate in matching:
                try:
                    self.assert_world_matches(
                        candidate,
                        world,
                        Action("start", ",".join(order)),
                    )
                except AssertionError as exc:
                    mismatches.append(f"{order}: {exc}")
                else:
                    final_state_matched = True
                    break
            self.assertTrue(
                final_state_matched,
                "no response-compatible serial history matched final state:\n"
                + "\n".join(mismatches),
            )
            state = world.state()
            self.assertEqual(len(state["events"]), 1)
            self.assertEqual(state["events"][0][0], "master")

        with ConcreteWorld(CONCURRENT_SEED + 2) as world:
            model = ReferenceModel(world.principals)
            self.assertEqual(world.execute(Action("acquire", "session-a")), "ok")
            self.assertEqual(model.transition(Action("acquire", "session-a")), "ok")
            count = 16
            barrier = threading.Barrier(count)
            invoked: set[int] = set()
            results: dict[int, str] = {}
            completion_saw_all_invocations: dict[int, bool] = {}
            lock = threading.Lock()

            def append(index: int) -> None:
                with lock:
                    invoked.add(index)
                barrier.wait()
                world.select("session-a")
                try:
                    world.writer().append("note", world.note_payload(index))
                    result = "ok"
                except Exception as exc:  # pragma: no cover - failure diagnostic
                    result = repr(exc)
                with lock:
                    results[index] = result
                    completion_saw_all_invocations[index] = len(invoked) == count

            threads = [threading.Thread(target=append, args=(index,)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(invoked, set(range(count)))
            self.assertEqual(
                results,
                {index: "ok" for index in range(count)},
                f"seed={CONCURRENT_SEED + 2}: {results}",
            )
            self.assertTrue(all(completion_saw_all_invocations.values()))
            events = master_runtime.read_record(world.record_root).events
            logged_titles = [event["payload"]["title"] for event in events]
            expected_titles = {f"model-note-{index}" for index in range(count)}
            self.assertEqual(len(logged_titles), count)
            self.assertEqual(set(logged_titles), expected_titles)
            self.assertEqual(len(logged_titles), len(set(logged_titles)))
            linearized = [
                int(title.removeprefix("model-note-")) for title in logged_titles
            ]
            self.assertEqual(set(linearized), set(range(count)))
            accepted_prefix = []
            for index in linearized:
                status = model.transition(Action("event", "session-a", str(index)))
                self.assertEqual(status, "ok", f"rejected prefix {accepted_prefix + [index]}")
                accepted_prefix.append(index)
            self.assert_world_matches(model, world, Action("event", "session-a"))
            state = world.state()
            self.assertEqual(len(state["events"]), count)
            self.assertEqual(len(state["event_ids"]), len(set(state["event_ids"])))
            self.assertTrue(state["board_current"])

    def test_record_content_never_grants_authority_and_readers_never_mutate(self):
        with ConcreteWorld(0xA017) as world:
            forged = master_runtime.RecordWriter(
                world.record_root,
                renew=lambda: (True, "synthetic setup"),
                lease_snapshot=lambda: {
                    "schema_version": semaphore.MASTER_SCHEMA_VERSION,
                    "lease": {"client": "record-only", "lease_id": "a" * 32},
                },
                event_id=lambda: "b" * 32,
            )
            forged.append(
                "master",
                {
                    "action": "start",
                    "model": "synthetic-model",
                    "effort": "synthetic-effort",
                    "note": "record content only",
                    "next_unit": "none",
                    "reconciliation": [],
                },
            )
            before = world.record_bytes()
            world.select("session-a")
            with self.assertRaises(master_runtime.RenewalRefused):
                world.writer().append("note", world.note_payload(1))
            self.assertEqual(world.record_bytes(), before)
            digest = master_operations.digest_record(
                world.record_root,
                lease_snapshot=semaphore.master_snapshot,
                lease_status=world._status,
            )
            self.assertEqual(digest["role"], {"recorded": "master", "authoritative": False})
            self.assertFalse(digest["lease"]["present"])
            self.assertEqual(world.record_bytes(), before)

    def test_legacy_malformed_and_board_crash_states_never_reset(self):
        with ConcreteWorld(0x1E6A) as world:
            process = world.principals["process-a"]
            legacy = {
                "schema_version": 1,
                "lease": {
                    "client": process.client,
                    "client_pid": process.pid,
                    "pid": 20_001,
                    "uid": 501,
                    "note": "synthetic legacy lease",
                    "acquired_at": world.now - 10,
                    "renewed_at": world.now - 5,
                    "lease_seconds": LEASE_SECONDS,
                },
            }
            lease_path = world.state_root / semaphore.MASTER_NAME
            world.state_root.mkdir(parents=True, exist_ok=True)
            legacy_bytes = json.dumps(legacy, separators=(",", ":")).encode()
            lease_path.write_bytes(legacy_bytes)
            world.select("process-a")
            self.assertEqual(semaphore.master_snapshot()["schema_version"], 4)
            self.assertEqual(lease_path.read_bytes(), legacy_bytes)
            self.assertTrue(world._renew()[0])
            self.assertEqual(json.loads(lease_path.read_bytes())["schema_version"], 4)

        with ConcreteWorld(0xBAD5) as world:
            lease_path = world.state_root / semaphore.MASTER_NAME
            world.state_root.mkdir(parents=True, exist_ok=True)
            corrupt_lease = b"{synthetic malformed lease"
            lease_path.write_bytes(corrupt_lease)
            operations = (
                semaphore.master_snapshot,
                lambda: world._acquire("process-a", "replacement"),
                world._renew,
                world._release,
            )
            for operation in operations:
                with self.assertRaises(semaphore.SemaphoreError):
                    operation()
                self.assertEqual(lease_path.read_bytes(), corrupt_lease)

        with ConcreteWorld(0xBAD6) as world:
            log_path = world.record_root / master_runtime.EVENTS_NAME
            corrupt_record = b"synthetic legacy or malformed record\n"
            log_path.write_bytes(corrupt_record)
            before = world.record_bytes()
            world.select("process-a")
            with self.assertRaises(master_runtime.MasterRecordError):
                master_operations.start_master(
                    world.record_root,
                    client="process-a",
                    model="synthetic-model",
                    effort="synthetic-effort",
                    note="must refuse",
                    acquire=world._acquire,
                    renew=world._renew,
                    release=world._release,
                    heartbeat=lambda _interval: (True, "unused"),
                    lease_snapshot=semaphore.master_snapshot,
                    lease_status=world._status,
                )
            with self.assertRaises(master_runtime.MasterRecordError):
                master_operations.digest_record(
                    world.record_root,
                    lease_snapshot=semaphore.master_snapshot,
                    lease_status=world._status,
                )
            with self.assertRaises(master_runtime.RenewalRefused):
                world.writer().append("note", world.note_payload(1))
            self.assertEqual(world.record_bytes(), before)
            self.assertIsNone(semaphore.master_snapshot()["lease"])

        with ConcreteWorld(0xB0A4D) as world:
            self.assertEqual(world.execute(Action("acquire", "session-a")), "ok")
            self.assertEqual(
                world.append_then_die(world.note_payload(1), "board:after-fsync"),
                71,
            )
            split = master_runtime.read_record(world.record_root)
            self.assertEqual(len(split.events), 1)
            self.assertFalse(split.board_current)
            self.assertIsNotNone(split.publication_transaction)
            digest_before = world.record_bytes()
            digest = master_operations.digest_record(
                world.record_root,
                lease_snapshot=semaphore.master_snapshot,
                lease_status=world._status,
            )
            self.assertTrue(digest["record"]["board_repair"]["required"])
            self.assertEqual(world.record_bytes(), digest_before)
            world.writer().append("note", world.note_payload(2))
            repaired = master_runtime.read_record(world.record_root)
            self.assertEqual(len(repaired.events), 2)
            self.assertEqual(len({event["event_id"] for event in repaired.events}), 2)
            self.assertTrue(repaired.board_current)

    @staticmethod
    @contextlib.contextmanager
    def _direct_auth_mutation(_world: ConcreteWorld) -> Iterator[None]:
        with mock.patch("creme.semaphore._same_client", return_value=True):
            yield

    @staticmethod
    @contextlib.contextmanager
    def _successor_binding_mutation(_world: ConcreteWorld) -> Iterator[None]:
        original = semaphore.hmac.compare_digest

        def compare(left, right):
            if isinstance(left, str) and isinstance(right, str) and len(left) == len(right) == 32:
                return True
            return original(left, right)

        with mock.patch("creme.semaphore.hmac.compare_digest", side_effect=compare):
            yield

    @staticmethod
    @contextlib.contextmanager
    def _capability_digest_mutation(_world: ConcreteWorld) -> Iterator[None]:
        original = semaphore.hmac.compare_digest

        def compare(left, right):
            if isinstance(left, str) and isinstance(right, str) and len(left) == len(right) == 64:
                return True
            return original(left, right)

        with mock.patch("creme.semaphore.hmac.compare_digest", side_effect=compare):
            yield

    @staticmethod
    @contextlib.contextmanager
    def _capability_expiry_mutation(_world: ConcreteWorld) -> Iterator[None]:
        original = semaphore._consume_master_heartbeat_launch

        def consume(capability: str):
            snapshot = semaphore.master_snapshot()
            lease = snapshot["lease"]
            expiry = None if lease is None else lease["heartbeat_launch_expires_at"]
            if expiry is None:
                return original(capability)
            with mock.patch("creme.semaphore._now", return_value=expiry - 0.1):
                return original(capability)

        with mock.patch(
            "creme.semaphore._consume_master_heartbeat_launch",
            side_effect=consume,
        ):
            yield

    @staticmethod
    @contextlib.contextmanager
    def _single_consume_mutation(_world: ConcreteWorld) -> Iterator[None]:
        original = semaphore._consume_master_heartbeat_launch

        def consume(capability: str):
            before = semaphore.master_snapshot()["lease"]
            digest = before["heartbeat_launch_digest"] if before else None
            expiry = before["heartbeat_launch_expires_at"] if before else None
            result = original(capability)
            if result[0] and digest is not None and expiry is not None:
                with semaphore.locked_state() as (path, _state):
                    data = semaphore._load_master(path.parent)
                    current = data["lease"]
                    if current is not None:
                        current["heartbeat_launch_digest"] = digest
                        current["heartbeat_launch_expires_at"] = expiry
                        semaphore._save_master(path.parent, data)
            return result

        with mock.patch(
            "creme.semaphore._consume_master_heartbeat_launch",
            side_effect=consume,
        ):
            yield

    @contextlib.contextmanager
    def _record_renew_mutation(self, world: ConcreteWorld) -> Iterator[None]:
        @contextlib.contextmanager
        def split_renew_and_snapshot(*, adapter=None):
            ok, detail = semaphore.master_renew(adapter=adapter)
            if not ok:
                raise semaphore.MasterAuthorityRefused(detail)
            predecessor = world.active().name
            world.now += LEASE_SECONDS + 1
            world.select("process-b")
            acquired, takeover_detail = world._acquire(
                world.active().client,
                "mutated split authority transaction",
                take_over=True,
            )
            if not acquired:
                raise AssertionError(takeover_detail)
            world._register_lease()
            world.select(predecessor)
            yield semaphore.master_snapshot()

        with mock.patch(
            "creme.semaphore.master_authority_transaction",
            side_effect=split_renew_and_snapshot,
        ):
            yield

    @staticmethod
    @contextlib.contextmanager
    def _actor_binding_mutation(_world: ConcreteWorld) -> Iterator[None]:
        def actor(snapshot):
            lease = snapshot["lease"]
            return {"client": lease["client"], "acquisition_digest": "0" * 64}

        with mock.patch("creme.master_runtime._actor_from_snapshot", side_effect=actor):
            yield

    @staticmethod
    @contextlib.contextmanager
    def _listener_binding_mutation(_world: ConcreteWorld) -> Iterator[None]:
        original = semaphore._heartbeat_binding

        def bind(current, session):
            binding, error = original(current, session)
            if binding is not None:
                return binding, error
            return (
                semaphore._HeartbeatBinding(
                    current["lease_id"],
                    current["client"],
                    semaphore._trusted_master_client_pid(current),
                    current["session_digest"],
                    current["liveness_digest"],
                    None,
                ),
                None,
            )

        with mock.patch("creme.semaphore._heartbeat_binding", side_effect=bind):
            yield

    def trace_fails(self, trace: list[Action], seed: int, mutation: Mutation) -> bool:
        try:
            self.run_trace(trace, seed=seed, mutation=mutation)
        except ModelMismatch:
            return True
        return False

    def minimize(
        self,
        trace: list[Action],
        seed: int,
        mutation: Mutation,
    ) -> list[Action]:
        current = list(trace)
        changed = True
        while changed:
            changed = False
            for index in range(len(current)):
                candidate = current[:index] + current[index + 1 :]
                if candidate and self.trace_fails(candidate, seed, mutation):
                    current = candidate
                    changed = True
                    break
        return current

    def test_each_authorization_and_binding_mutation_has_a_minimized_counterexample(self):
        campaigns: dict[str, tuple[int, Mutation, list[Action]]] = {
            "direct-holder": (
                0xD1EC7,
                self._direct_auth_mutation,
                [Action("acquire", "process-a"), Action("renew", "process-b")],
            ),
            "successor-lease": (
                0x5ACC,
                self._successor_binding_mutation,
                [
                    Action("acquire", "process-a"),
                    Action("prepare", "process-a"),
                    Action("consume", "process-a", "correct"),
                    Action("release", "process-a"),
                    Action("acquire", "process-b"),
                    Action("heartbeat", "process-a"),
                ],
            ),
            "capability-digest": (
                0xCA9,
                self._capability_digest_mutation,
                [
                    Action("acquire", "process-a"),
                    Action("prepare", "process-a"),
                    Action("consume", "process-a", "wrong"),
                ],
            ),
            "capability-expiry": (
                0xE891,
                self._capability_expiry_mutation,
                [
                    Action("acquire", "process-a"),
                    Action("prepare", "process-a"),
                    Action("advance", "process-a", "31"),
                    Action("consume", "process-a", "correct"),
                ],
            ),
            "single-consume": (
                0x51A61E,
                self._single_consume_mutation,
                [
                    Action("acquire", "process-a"),
                    Action("prepare", "process-a"),
                    Action("consume", "process-a", "correct"),
                    Action("consume", "process-a", "correct"),
                ],
            ),
            "record-authority-transaction": (
                0xAE0E,
                self._record_renew_mutation,
                [Action("acquire", "process-a"), Action("event", "process-a")],
            ),
            "record-acquisition": (
                0xAC701,
                self._actor_binding_mutation,
                [Action("acquire", "process-a"), Action("event", "process-a")],
            ),
            "listener-binding": (
                0x1157E,
                self._listener_binding_mutation,
                [
                    Action("acquire", "listener-a"),
                    Action("prepare", "listener-a"),
                    Action("listener-down", "listener-a"),
                    Action("consume", "listener-a", "correct"),
                ],
            ),
        }
        for name, (seed, mutation, trace) in campaigns.items():
            with self.subTest(mutation=name, seed=seed):
                self.run_trace(trace, seed=seed)
                self.assertTrue(self.trace_fails(trace, seed, mutation))
                minimal = self.minimize(trace, seed, mutation)
                self.assertTrue(self.trace_fails(minimal, seed, mutation))
                for index in range(len(minimal)):
                    reduced = minimal[:index] + minimal[index + 1 :]
                    self.assertFalse(
                        reduced and self.trace_fails(reduced, seed, mutation),
                        f"seed={seed} mutation={name} was not one-action minimal: "
                        f"{[str(action) for action in minimal]}",
                    )


if __name__ == "__main__":
    unittest.main()
