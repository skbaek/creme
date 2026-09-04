from __future__ import annotations

import contextlib
import hashlib
import json
import random
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest import mock

from creme import master_operations, master_runtime, semaphore
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

    def _direct_renew(self, name: str) -> bool:
        if not self._same_holder(name):
            return False
        assert self.lease is not None
        self.lease.expires_at = self.now + LEASE_SECONDS
        self.lease.direct_activity_at = self.now
        self.lease.heartbeat_renewals = 0
        return True

    def _record_event(self, kind: str) -> None:
        assert self.lease is not None
        self.events.append((kind, self.lease.generation))
        self.board_current = True

    def transition(self, action: Action) -> str:
        name = action.principal
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
            if not self._direct_renew(name):
                return "refused"
            self._record_event("note")
            return "ok"
        if action.operation == "digest":
            return "ok"
        if action.operation == "stale-board":
            if self.events:
                self.board_current = False
            return "ok"
        if action.operation == "start":
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
            event_id=self._next_event_id,
        )

    @staticmethod
    def note_payload(index: int) -> dict[str, str]:
        return {
            "title": f"model-note-{index}",
            "note": "synthetic transition observation",
            "evidence": f"evidence/model-{index}.json",
            "next_unit": f"unit-{index}",
        }

    def record_bytes(self) -> tuple[bytes, bytes]:
        return (
            (self.record_root / master_runtime.EVENTS_NAME).read_bytes(),
            (self.record_root / master_runtime.BOARD_NAME).read_bytes(),
        )

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
            return "ok" if ok else "refused"
        if action.operation == "release":
            ok, _detail = self._release()
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
            try:
                self.writer().append(
                    "note", self.note_payload(self.event_counter + 1)
                )
            except master_runtime.MasterRecordError:
                return "refused"
            return "ok"
        if action.operation == "digest":
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
            view = master_runtime.read_record(self.record_root)
            if view.events:
                prior = master_runtime.render_board(view.events[:-1])
                (self.record_root / master_runtime.BOARD_NAME).write_bytes(prior)
            return "ok"
        if action.operation == "start":
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
                )
            except (master_runtime.MasterRecordError, master_operations.MasterOperationError):
                return "error"
            if result["status"] == "master":
                self._register_lease()
            return result["status"]
        raise AssertionError(f"unknown concrete action: {action}")

    def state(self) -> dict[str, object]:
        self._register_lease()
        lease = semaphore.master_snapshot()["lease"]
        generation = (
            None if lease is None else self.lease_generations.get(lease["lease_id"])
        )
        view = master_runtime.read_record(self.record_root)
        event_rows = [
            (
                event["kind"],
                self.actor_generations.get(event["actor"]["acquisition_digest"], -1),
            )
            for event in view.events
        ]
        return {
            "lease_generation": generation,
            "lease_client": None if lease is None else lease["client"],
            "pending": False if lease is None else lease["heartbeat_launch_digest"] is not None,
            "events": event_rows,
            "event_ids": [event["event_id"] for event in view.events],
            "board_current": view.board_current,
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
        self.assertEqual(actual["events"], model.events, f"events after {action}")
        event_ids = actual["event_ids"]
        self.assertEqual(len(event_ids), len(set(event_ids)), f"duplicate event after {action}")
        self.assertEqual(actual["board_current"], model.board_current, f"board after {action}")

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
            model = ReferenceModel(world.principals)
            names = ["process-a", "process-b", "session-a", "listener-a"]
            rng.shuffle(names)
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
            winner = next(name for name, status in start_results if status == "master")
            self.assertEqual(model.transition(Action("start", winner)), "master")
            for name, status in start_results:
                if name != winner:
                    self.assertEqual(model.transition(Action("start", name)), status)
            self.assert_world_matches(model, world, Action("start", winner))
            state = world.state()
            self.assertEqual(len(state["events"]), 1)
            self.assertEqual(state["events"][0][0], "master")

        with ConcreteWorld(CONCURRENT_SEED + 2) as world:
            model = ReferenceModel(world.principals)
            self.assertEqual(world.execute(Action("acquire", "session-a")), "ok")
            self.assertEqual(model.transition(Action("acquire", "session-a")), "ok")
            count = 16
            barrier = threading.Barrier(count)
            results = []
            lock = threading.Lock()

            def append(index: int) -> None:
                barrier.wait()
                world.select("session-a")
                try:
                    world.writer().append("note", world.note_payload(index))
                    result = "ok"
                except Exception as exc:  # pragma: no cover - failure diagnostic
                    result = repr(exc)
                with lock:
                    results.append(result)

            threads = [threading.Thread(target=append, args=(index,)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(results, ["ok"] * count, f"seed={CONCURRENT_SEED + 2}: {results}")
            for _index in range(count):
                self.assertEqual(model.transition(Action("event", "session-a")), "ok")
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
            self.assertEqual(semaphore.master_snapshot()["schema_version"], 3)
            self.assertEqual(lease_path.read_bytes(), legacy_bytes)
            self.assertTrue(world._renew()[0])
            self.assertEqual(json.loads(lease_path.read_bytes())["schema_version"], 3)

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
            writer = world.writer()

            def stop_after_log(stage: str) -> None:
                if stage == "transaction:after-log-commit":
                    raise RuntimeError("synthetic crash after log commit")

            with self.assertRaisesRegex(RuntimeError, "after log commit"):
                writer.append("note", world.note_payload(1), fault=stop_after_log)
            split = master_runtime.read_record(world.record_root)
            self.assertEqual(len(split.events), 1)
            self.assertFalse(split.board_current)
            digest_before = world.record_bytes()
            digest = master_operations.digest_record(
                world.record_root,
                lease_snapshot=semaphore.master_snapshot,
                lease_status=world._status,
            )
            self.assertTrue(digest["record"]["board_repair"]["required"])
            self.assertEqual(world.record_bytes(), digest_before)
            writer.append("note", world.note_payload(2))
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

    @staticmethod
    @contextlib.contextmanager
    def _record_renew_mutation(_world: ConcreteWorld) -> Iterator[None]:
        with mock.patch("creme.master_runtime._renew_or_refuse", return_value=None):
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
            "renew-before-record-write": (
                0xAE0E,
                self._record_renew_mutation,
                [Action("acquire", "process-a"), Action("event", "process-b")],
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
