"""Long-lived broker for steerable Muse pseudo-subagent sessions.

The broker keeps guarded Muse sessions alive between one-shot CLI calls, so a
master in any client can start a task, steer it mid-turn, give follow-up
orders, interrupt or stop it, answer the approvals the allowlist leaves to it,
and read a filtered event feed. It adds no pin logic of its own: every session
is built by ``creme.muse.MuseHost`` (echo bootstrap, serve attach, model pin
read-back), every request goes through ``MuseGuard``, every approval through
``decide_approval``, and every turn ends with the durable session-log audit.
A pin failure records the ``MODEL_PIN_FAILURE`` tripwire and stops every
session.

The socket, registry, and client commands are the protocol-independent ones of
``creme.pseudo_broker``, shared with the Luna reserve broker, so masters use
one recipe for both. Layout under the Muse state directory::

    broker/broker.sock, broker.json, broker.log
    sessions/<id>/   session.json, events.jsonl, transcript.jsonl, bootstrap.json,
                     turns/<n>/ brief.md, steer-<k>.md, usage-before.json,
                     usage-after.json, audit.json, approvals.json, last-message.md
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import luna_lean
from . import muse as M
from . import pseudo_broker as PB
from .codex_app_server import AppServerError, PinViolation
from .muse_client import EFFORTS, PINNED_MODEL, audit_session_log, uuid7

TERMINAL_STATES = ("stopped", "refused", "failed", "tripped", "lost", "unclean")
OPEN_STATES = ("starting", "idle", "running", "stopping")
DECISIONS = ("accept", "decline")

BROKER_IDLE_SECONDS = 600
SESSION_IDLE_SECONDS = 1800
IDLE_ENV = "CREME_MUSE_BROKER_IDLE_SECONDS"
SESSION_IDLE_ENV = "CREME_MUSE_SESSION_IDLE_SECONDS"
MAX_LEAN_SESSIONS = 2
MAX_LEAN_SESSIONS_ENV = "CREME_MUSE_MAX_LEAN_SESSIONS"

_SESSION_ID = re.compile(r"^ms-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_MUSE_SESSION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def code_digest(module_root: Path) -> str:
    return PB.file_digest(module_root, (
        "creme/muse_broker.py", "creme/muse.py", "creme/muse_client.py", "creme/pseudo_broker.py",
        "creme/codex_app_server.py", "creme/luna_lean.py", "templates/muse/preamble.md",
        "templates/muse/lean-preamble.md"))


def _max_lean_sessions(environ: dict) -> int:
    try:
        value = int(environ.get(MAX_LEAN_SESSIONS_ENV, MAX_LEAN_SESSIONS))
    except (TypeError, ValueError):
        return MAX_LEAN_SESSIONS
    return value if 1 <= value <= 4 else MAX_LEAN_SESSIONS


def load_record(state: Path, session_id: str) -> Optional[dict]:
    return PB.load_record(state, session_id, _SESSION_ID)


def all_records(state: Path) -> list[dict]:
    return PB.all_records(state, "ms-")


# ---------------------------------------------------------------------------
# Broker-side session


class MuseSession(PB.SessionRecord):
    """One guarded Muse session held by the broker."""

    def __init__(self, broker: "Broker", session_id: str, record: dict) -> None:
        self.broker = broker
        self.id = session_id
        self.dir = PB.sessions_dir(broker.state) / session_id
        self.record = record
        self.lock = threading.RLock()
        self.data_lock = threading.RLock()
        self.turn_ended = threading.Event()
        self.turn_ended.set()
        self.host: Optional[M.MuseHost] = None
        self.turn: Optional[M.TurnState] = None
        self.pending: dict[str, dict] = {}
        self.approval_counter = 0
        self.seq = 0
        self.closed = False
        self.tripped = False
        self.timed_out = False
        self.interrupted_turn: Optional[str] = None
        self.turn_deadline: Optional[float] = None
        self.git_before: Optional[dict] = None
        self.last_activity = time.monotonic()
        self.pump_thread: Optional[threading.Thread] = None
        self.tracker: Optional[M.ViewTracker] = None

    def is_open(self) -> bool:
        return not self.closed and self.host is not None

    @property
    def guard(self):
        return self.host.guard if self.host is not None else None

    def emit_event(self, level: str, kind: str, text: str) -> None:
        self.emit(level, kind, text)

    # -- opening --------------------------------------------------------
    def open(self, brief: Optional[str], resume: Optional[str]) -> tuple[int, dict]:
        record = self.record
        PB.private_dir(self.dir)
        self.set_state("starting")
        host = M.MuseHost(self.dir, Path(record["target"]), record["mode"], record["effort"], self.broker.environ,
                          (record.get("lean") or {}).get("goal"), session_id=resume)
        self.host = host
        try:
            if resume is None:
                host.bootstrap()
            record["muse_session"] = host.session_id
            self.persist()
            refusals = host.open()
            record.update({"server_pid": host.process.pid if host.process else None,
                           "session_log": str(host.log_path) if host.log_path else None,
                           "server": host.server_info})
            self.persist()
            if any("model" in item for item in refusals):
                return self._trip_open(refusals)
            if refusals:
                return self._refuse_open(refusals)
        except PinViolation as exc:
            return self._trip_open([f"pin violation: {exc}"])
        except (AppServerError, M.MuseError, OSError) as exc:
            self.closed = True
            host.close()
            self.lean_wind_down("open failed")
            self.set_state("failed", str(exc)[:400])
            self.emit("attention", "failed", f"muse failed while opening: {exc}")
            return M.EXIT_MUSE_FAILED, {"verdict": "FAILED", "errors": [str(exc)]}
        self.tracker = M.ViewTracker(host.view_cursor)
        self.set_state("idle")
        self.emit("attention" if not brief else "live", "opened",
                  f"muse session {host.session_id} {'resumed' if resume else 'started'} model={PINNED_MODEL} "
                  f"effort={record['effort']} mode={record['mode']}")
        self.pump_thread = threading.Thread(target=self.pump, name=f"pump-{self.id}", daemon=True)
        self.pump_thread.start()
        if brief:
            lean = (record.get("lean") or {}).get("goal")
            return self.begin_turn(M.first_turn_text(self.broker.module_root, Path(record["target"]),
                                                     record["mode"], lean, brief), brief=brief)
        return M.EXIT_OK, {"verdict": "OPEN"}

    def _refuse_open(self, refusals: list[str]) -> tuple[int, dict]:
        self.closed = True
        self.host.close()
        self.lean_wind_down("refused at open")
        self.set_state("refused", "; ".join(refusals)[:400])
        self.emit("attention", "refused", "; ".join(refusals))
        return M.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": refusals}

    def _trip_open(self, failures: list[str]) -> tuple[int, dict]:
        self.tripped = True
        self.closed = True
        self.host.close()
        self.lean_wind_down("pin failure at open")
        self.broker.trip_all(self, failures)
        self.set_state("tripped", "; ".join(failures)[:400])
        return M.EXIT_PIN_FAILED, {"verdict": "PIN_FAILED", "failures": failures, "message": M.STOP_MESSAGE}

    # -- turns ----------------------------------------------------------
    def begin_turn(self, text: str, brief: Optional[str] = None) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            refusals = M.early_refusals(self.broker.state, self.record["effort"], text, self.broker.module_root,
                                        None, self.record["mode"])
            if self.state != "idle" or not self.is_open():
                refusals.append(f"session is {self.state}; a new turn needs an idle session")
            if refusals:
                return M.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": refusals}
            try:
                usage, source = M.admission_usage(self.broker.state, self.host.usage())
            except AppServerError as exc:
                return M.EXIT_MUSE_FAILED, {"verdict": "FAILED", "errors": [f"usage/read failed: {exc}"]}
            refusals = M.usage_refusals(usage)
            if refusals:
                self.emit("attention", "refused", "usage admission: " + "; ".join(refusals))
                return M.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": refusals}
            number = len(self.record["turns"]) + 1
            turn_dir = PB.private_dir(self.dir / "turns" / str(number))
            M._write(turn_dir / "brief.md", brief if brief is not None else text)
            PB.write_private_json(turn_dir / "usage-before.json", {"usage": usage, "source": source,
                                                                   "at": PB.now_iso()})
            if self.record["mode"] == "read-only":
                self.git_before = M.git_snapshot(Path(self.record["target"]))
            self.turn = M.TurnState()
            self.timed_out = False
            try:
                outcome = self.guard.begin_turn(text)
            except PinViolation as exc:
                self.on_guard_failure([f"pin violation: {exc}"])
                return M.EXIT_PIN_FAILED, {"verdict": "PIN_FAILED", "failures": [str(exc)]}
            except AppServerError as exc:
                self.emit("attention", "error", f"turn/start failed: {exc}")
                return M.EXIT_MUSE_FAILED, {"verdict": "FAILED", "errors": [str(exc)]}
            turn = {"n": number, "started": PB.now_iso(), "turn_id": outcome.turn_id, "status": None,
                    "verdict": None, "steers": 0, "usage_before": usage, "usage_source": source}
            with self.data_lock:
                self.record["turns"].append(turn)
                self.record["state"] = "running"
                self.turn_ended.clear()
                self.turn_deadline = time.monotonic() + float(self.record["turn_timeout_seconds"])
                self.persist()
            self.emit("live", "turn", f"turn {number} started ({outcome.turn_id})")
            return M.EXIT_OK, {"verdict": "STARTED", "turn": number}

    def steer(self, text: str, reconciled: bool = False) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            if not reconciled and self.state == "running":
                self.reconcile("steer")
            if self.state != "running" or self.guard is None or not self.guard.active_turn:
                return M.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [
                    f"session is {self.state}; only a running turn can be steered"]}
            if not text.strip():
                return M.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": ["the steer text is empty"]}
            number = len(self.record["turns"])
            try:
                answer = self.guard.steer(text.strip())
            except PinViolation as exc:
                return M.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [f"pin: {exc}"]}
            except AppServerError as exc:
                return M.EXIT_MUSE_FAILED, {"verdict": "FAILED", "errors": [str(exc)]}
            with self.data_lock:
                turn = self.record["turns"][-1]
                turn["steers"] = turn.get("steers", 0) + 1
                M._write(self.dir / "turns" / str(number) / f"steer-{turn['steers']}.md", text)
                turn.setdefault("steer_acks", []).append({"at": PB.now_iso(), "turn_id": (answer or {}).get("turnId")})
                self.persist()
            self.emit("live", "steer", f"turn {number} steered: {PB.one_line(text, 100)}")
            return M.EXIT_OK, {"verdict": "STEERED", "turn": number}

    def send(self, text: str) -> tuple[int, dict]:
        with self.lock:
            if self.state == "running":
                self.reconcile("send")   # a turn Muse already ended is not steered
            if self.state == "running" and self.guard is not None and self.guard.active_turn:
                code, result = self.steer(text, reconciled=True)
                if code != M.EXIT_MUSE_FAILED or "already_terminal" not in " ".join(result.get("errors") or []):
                    return code, result
                # Muse ended the turn between the reconcile and the steer: catch up, then start a turn.
                self.reconcile("steer rejected: already terminal")
                if self.state == "running":
                    return code, result
            return self.begin_turn(text)

    def request_interrupt(self) -> None:
        turn = self.guard.active_turn if self.guard is not None else None
        if not turn or self.interrupted_turn == turn:
            return
        self.interrupted_turn = turn
        self.guard.interrupt()

    def interrupt(self) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            if self.state not in ("running", "stopping") or self.guard is None or not self.guard.active_turn:
                return M.EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [
                    f"session is {self.state}; no active turn to interrupt"]}
            try:
                self.request_interrupt()
            except (AppServerError, PinViolation) as exc:
                return M.EXIT_MUSE_FAILED, {"verdict": "FAILED", "errors": [str(exc)]}
            self.emit("live", "interrupt", f"interrupt requested for turn {len(self.record['turns'])}")
            return M.EXIT_OK, {"verdict": "INTERRUPT_REQUESTED"}

    def end_turn(self) -> None:
        with self.lock:
            outcome = self.guard.finish_turn()
            if outcome is None:
                return
            self.turn_deadline = None
            turn = self.record["turns"][-1]
            number = turn["n"]
            turn_dir = self.dir / "turns" / str(number)
            state = self.turn or M.TurnState()
            errors = list(state.errors)
            try:
                usage_after = self.host.usage()
                M.remember_usage(self.broker.state, usage_after)
            except AppServerError as exc:
                usage_after = None
                errors.append(f"usage/read failed: {exc}")
            PB.write_private_json(turn_dir / "usage-after.json", {"usage": usage_after, "at": PB.now_iso()})
            time.sleep(0.3)
            audit = audit_session_log(self.host.log_path, self.host.log_start, outcome.turn_id, self.host.session_id)
            PB.write_private_json(turn_dir / "audit.json", audit)
            PB.write_private_json(turn_dir / "approvals.json", state.approvals)
            git_failure = None
            if self.record["mode"] == "read-only" and self.git_before is not None:
                if M.git_snapshot(Path(self.record["target"])) != self.git_before:
                    git_failure = "read-only turn changed the target's HEAD or porcelain status"
            last_message = turn_dir / "last-message.md"
            if outcome.final_message is not None:
                M._write(last_message, outcome.final_message.rstrip("\n") + "\n")
            verdict = M.verdict_for(outcome, audit, list(outcome.guard_failures), errors, self.timed_out, git_failure)
            failures = list(outcome.guard_failures) + list(audit.get("failures") or [])
            with self.data_lock:
                turn.update({
                    "status": outcome.status, "verdict": verdict, "completed": PB.now_iso(),
                    "timed_out": self.timed_out, "errors": errors, "failures": failures,
                    "git_failure": git_failure, "tokens": outcome.tokens, "models": outcome.models,
                    "tool_calls": outcome.tool_calls, "duration_ms": outcome.duration_ms,
                    "wall_seconds": round((outcome.completed or time.time()) - outcome.started, 1),
                    "steered_items": len(outcome.steer_items), "usage_after": usage_after,
                    "session_log_audit": audit.get("verdict"), "approvals": len(state.approvals),
                    "last_message": str(last_message) if last_message.exists() else None,
                })
                self.pending.clear()
                self.record["pending_approvals"] = []
                if self.state == "running":
                    self.record["state"] = "idle"
                self.persist()
            self.emit("attention", "turn", (
                f"turn {number} {outcome.status} verdict={verdict} models={outcome.models} "
                f"tokens {M.tokens_line(outcome.tokens)}"
                + (f" failures={PB.one_line('; '.join(failures + errors), 160)}" if failures or errors else "")))
            if outcome.final_message is not None:
                self.emit("summary", "final", f"{last_message} ({len(outcome.final_message.splitlines())} lines): "
                          f"{PB.one_line(outcome.final_message, 140)}")
            self.turn = None
            self.last_activity = time.monotonic()
            self.turn_ended.set()
        if verdict == "PIN_FAILED":
            self.tripped = True
            self.broker.trip_all(self, failures)

    # -- approvals ------------------------------------------------------
    def on_approval(self, params: dict) -> None:
        if self.turn is None:
            self.turn = M.TurnState()
        waiting = M.handle_approval(self.host, params, self.turn, self.emit, master_available=True)
        if waiting is None:
            return
        with self.data_lock:
            if any(entry["params"].get("approvalId") == params.get("approvalId")
                   and entry["params"].get("currentRequirementId") == params.get("currentRequirementId")
                   for entry in self.pending.values()):
                return
            self.approval_counter += 1
            key = f"a{self.approval_counter}"
            summary = M.describe_approval(params)
            self.pending[key] = {"params": params}
            self.record.setdefault("pending_approvals", []).append({
                "id": key, "summary": summary, "received": PB.now_iso(), "decisions": list(DECISIONS)})
            self.persist()
        self.emit("attention", "approval", f"{key} [accept|decline] {summary} -> approve {self.id} {key} DECISION")

    def approve(self, key: str, decision: str) -> tuple[int, dict]:
        with self.lock:
            self.last_activity = time.monotonic()
            if decision not in DECISIONS:
                return PB.EXIT_USAGE, {"verdict": "REFUSED", "refusals": [f"decision must be one of {DECISIONS}"]}
            with self.data_lock:
                entry = self.pending.pop(key, None)
                if entry is None:
                    return PB.EXIT_USAGE, {"verdict": "REFUSED", "refusals": [f"no pending approval {key}"]}
                self.record["pending_approvals"] = [
                    item for item in self.record.get("pending_approvals") or [] if item.get("id") != key]
                self.persist()
            params = entry["params"]
            wanted = "approved" if decision == "accept" else "abort"
            choice = next((c.get("choiceId") for c in params.get("availableChoices") or []
                           if c.get("decision") == wanted and c.get("scope") == "once"), None)
            if choice is None:
                return M.EXIT_MUSE_FAILED, {"verdict": "FAILED", "errors": [f"no one-shot {wanted} choice offered"]}
            try:
                self.guard.decide(params, choice)
            except (AppServerError, PinViolation) as exc:
                return M.EXIT_MUSE_FAILED, {"verdict": "FAILED", "errors": [str(exc)]}
            if self.turn is not None:
                self.turn.approvals.append({"approval_id": params.get("approvalId"), "action": wanted,
                                            "reason": f"master {decision}", "summary": M.describe_approval(params),
                                            "at": PB.now_iso()})
            self.emit("live", "approval", f"{key} answered {decision} by the master")
            return M.EXIT_OK, {"verdict": "ANSWERED", "approval": key, "decision": decision}

    # -- pump -----------------------------------------------------------
    def pump(self) -> None:
        process = self.host.process
        next_check = 0.0
        while not self.closed:
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 1.0
                self.periodic(now)
            message = process.next_notification(0.5)
            if message is None or self.closed:
                continue
            if message.get("method") == "creme/transport/closed":
                self.on_lost()
                return
            with self.lock:
                self.process(message)
            if self.tracker is not None and self.tracker.unhealthy:
                self.reconcile("view unavailable")

    def process(self, message: dict) -> None:
        """One view notification, live or replayed; a durable event is processed at most once."""
        if self.tracker is not None and not self.tracker.admit(message):
            return
        method = message.get("method")
        failures = self.guard.observe(message)
        self.describe(message)
        if method in ("approval/requested", "approval/updated"):
            self.on_approval(message.get("params") or {})
        if method in M.UNHEALTHY_METHODS and self.tracker is not None:
            self.tracker.unhealthy = True
            self.emit("attention", "view", f"{method}: {json.dumps(message.get('params'))[:160]}; reconciling")
        if failures:
            self.on_guard_failure(failures)
        if method == "turn/completed":
            outcome = self.guard.outcome
            if outcome is not None and outcome.status is not None:
                self.end_turn()

    def reconcile(self, reason: str) -> None:
        """Catch up from the server when the live stream may have dropped events.

        Replays durable view events (``view/page``) through ``process``, decides
        still-pending approvals (``approval/listPending``) through the same
        allowlist and master queue, ends the turn if Muse reports the session
        idle without a terminal event, and re-attaches the view stream.
        """
        with self.lock:
            if not self.is_open() or self.tracker is None:
                return
            tracker = self.tracker
            tracker.last_reconcile = time.monotonic()
            try:
                active = self.guard.outcome.turn_id if self.guard is not None and self.guard.outcome else None
                replayed = M.replay_view(self.host, tracker, active)
                others, terminals = M.split_terminals(replayed)
                for message in others:
                    self.process(message)
                recovered = 0
                if self.state == "running":
                    for params in M.pending_approvals(self.host):
                        before = len(self.pending)
                        self.on_approval(params)
                        recovered += len(self.pending) - before
                outcome = self.guard.outcome if self.guard is not None else None
                ended = False
                reopened = False
                if outcome is not None and outcome.status is None:
                    current = M.session_status(self.host)
                    if M.turn_over(current, outcome.turn_id):
                        time.sleep(0.5)   # the terminal may have landed after the first replay
                        more, late = M.split_terminals(M.replay_view(self.host, tracker, outcome.turn_id))
                        for message in more:
                            self.process(message)
                        mine = [t for t in terminals + late
                                if (t.get("params") or {}).get("turnId") == outcome.turn_id]
                        terminal = mine[-1] if mine else M.find_terminal(self.host, outcome.turn_id)
                        if terminal is not None and self.guard.outcome is outcome:
                            self.process(terminal)       # ends the turn through the live path
                        if self.guard.outcome is outcome and outcome.status is None \
                                and current.get("status") == "idle":
                            outcome.status = "lost"
                            if self.turn is not None:
                                self.turn.errors.append("Muse ended the turn but its terminal event was not observed")
                            ended = True
                elif outcome is None and self.state == "idle":
                    reopened = self.reopen_if_running()
                note = None
                if tracker.unhealthy:
                    failure = M.resubscribe(self.host, tracker)
                    note = "view/subscribe " + ("failed: " + failure if failure else "re-attached")
                    tracker.unhealthy = False
            except (AppServerError, PinViolation) as exc:
                self.emit("attention", "error", f"reconcile ({reason}) failed: {exc}")
                return
            if replayed or recovered or ended or note or reopened:
                self.emit("live" if not (recovered or ended or reopened) else "attention", "reconcile",
                          f"{reason}: replayed={len(replayed)} approvals_recovered={recovered} "
                          f"turn_ended_unobserved={ended} reopened={reopened}" + (f"; {note}" if note else ""))
            if ended:
                self.end_turn()

    def reopen_if_running(self) -> bool:
        """Called under the lock on an idle session: reopen the last turn if Muse is still running it.

        A turn this broker ended as failed or lost (a wrongly applied terminal,
        or a lost stream) while Muse went on is put back to running, keeping the
        tokens and models already counted, so its genuine terminal is recorded.
        """
        turns = self.record.get("turns") or []
        last = turns[-1] if turns else None
        if not last or last.get("verdict") != "FAILED" or last.get("status") not in ("failed", "lost") \
                or not last.get("turn_id") or self.guard is None:
            return False
        current = M.session_status(self.host)
        if current.get("status") != "running" or current.get("active_turn") != last["turn_id"]:
            return False
        outcome = self.guard.reopen(last["turn_id"], last.get("tokens"), last.get("models"))
        with self.data_lock:
            last.setdefault("reopened", []).append({"at": PB.now_iso(), "status": last.get("status"),
                                                    "verdict": last.get("verdict")})
            last.update({"status": None, "verdict": None, "completed": None})
            self.record["state"] = "running"
            self.turn_ended.clear()
            self.turn_deadline = time.monotonic() + float(self.record["turn_timeout_seconds"])
            self.persist()
        self.turn = M.TurnState()
        self.emit("attention", "turn", f"turn {last['n']} reopened: Muse is still running {outcome.turn_id}")
        return True

    def periodic(self, now: float) -> None:
        broker = self.broker
        recheck = self.state == "idle" and (self.record.get("turns") or [{}])[-1].get("verdict") == "FAILED" \
            and (self.record.get("turns") or [{}])[-1].get("status") in ("failed", "lost")
        if (self.state == "running" or recheck) and self.tracker is not None \
                and now - self.tracker.last_reconcile > broker.reconcile_seconds:
            self.reconcile("periodic" if not recheck else "recheck")
        if M.tripwire_path(broker.state).exists() and not broker.tripped:
            broker.trip_all(None, ["a model-pin failure tripwire was recorded by another run"])
            return
        if self.state == "running" and self.turn_deadline is not None and now > self.turn_deadline \
                and not self.timed_out:
            self.timed_out = True
            self.emit("attention", "timeout", "turn timed out; interrupting")
            try:
                self.request_interrupt()
            except (AppServerError, PinViolation) as exc:
                self.emit("attention", "error", f"interrupt failed: {exc}")
        if self.state == "idle" and not self.pending and now - self.last_activity > broker.session_idle_seconds:
            threading.Thread(target=self.stop, kwargs={"reason": "idle"}, daemon=True).start()
            self.last_activity = now

    def describe(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        kind = item.get("kind")
        if method == "item/started" and kind == "toolCall":
            self.emit("live", "tool", f"start {item.get('tool')}: {PB.one_line(item.get('args') or '', 150)}")
        elif method == "item/completed" and kind == "toolCall":
            self.emit("live", "tool", f"{item.get('status')} {item.get('tool')}: "
                      f"{PB.one_line(item.get('visibleOutput') or item.get('failureReason') or '', 120)}")
        elif method == "item/completed" and kind == "userMessage" and item.get("steered"):
            self.emit("live", "steered", f"steer absorbed: {PB.one_line(item.get('text') or '', 120)}")
        elif method == "item/completed" and kind == "agentMessage":
            self.emit("live", "message", PB.one_line(item.get("text") or "", 150))
        elif method == "turn/retryScheduled":
            self.emit("live", "retry", f"attempt {params.get('attempt')}/{params.get('maxAttempts')}: "
                      f"{params.get('reason')}")

    def on_guard_failure(self, failures: list[str]) -> None:
        self.tripped = True
        try:
            self.request_interrupt()
        except (AppServerError, PinViolation):
            pass
        self.broker.trip_all(self, failures)

    def on_lost(self) -> None:
        if self.closed:
            return
        running = self.state == "running"
        self.closed = True
        self.host.close()
        self.lean_wind_down("muse serve lost")
        if running:
            with self.data_lock:
                self.record["turns"][-1].update({"status": "lost", "verdict": "FAILED",
                                                 "errors": ["muse serve exited during the turn"]})
                self.persist()
            self.guard.finish_turn()
            self.turn_ended.set()
        self.set_state("failed", "muse serve exited")
        self.emit("attention", "failed", "muse serve exited; resume the Muse session in a new broker session")

    # -- stopping -------------------------------------------------------
    def stop(self, reason: str = "requested") -> tuple[int, dict]:
        with self.lock:
            if self.closed or self.state in TERMINAL_STATES or self.host is None:
                return M.EXIT_OK, {"verdict": "ALREADY_CLOSED", "state": self.state}
            self.set_state("stopping", f"stop: {reason}")
            with self.data_lock:
                pending = list(self.pending.items())
                self.pending.clear()
                self.record["pending_approvals"] = []
                self.persist()
            for key, entry in pending:
                params = entry["params"]
                choice = next((c.get("choiceId") for c in params.get("availableChoices") or []
                               if c.get("decision") == "abort" and c.get("scope") == "once"), None)
                try:
                    if choice:
                        self.guard.decide(params, choice)
                except (AppServerError, PinViolation):
                    pass
                self.emit("live", "approval", f"{key} aborted by stop")
            active = self.guard is not None and self.guard.active_turn
            if active:
                try:
                    self.request_interrupt()
                except (AppServerError, PinViolation):
                    pass
        if active and threading.current_thread() is not self.pump_thread:
            self.turn_ended.wait(PB.STOP_WAIT_SECONDS)
        with self.lock:
            if self.guard is not None and self.guard.outcome is not None:
                with self.data_lock:
                    self.record["turns"][-1].update({"status": "abandoned", "verdict": "FAILED",
                                                     "errors": ["no completion after interrupt at stop"]})
                self.guard.finish_turn()
            audit = audit_session_log(self.host.log_path, self.host.log_start, None, self.host.session_id)
            PB.write_private_json(self.dir / "stop-audit.json", audit)
            self.closed = True
            self.host.close()
            if self.pump_thread is not None and threading.current_thread() is not self.pump_thread:
                self.pump_thread.join(5)
            wind_down = self.lean_wind_down(f"stop: {reason}")
            failed = audit["verdict"] != "PASS"
            unclean = wind_down is not None and wind_down.get("verdict") != "OK"
            final = "tripped" if (self.tripped or failed) else "unclean" if unclean else "stopped"
            if failed and not self.tripped:
                self.tripped = True
                self.broker.trip_all(self, audit["failures"])
            with self.data_lock:
                self.record["stop_audit"] = {"verdict": audit["verdict"], "failures": audit["failures"][:10],
                                             "models": audit.get("models")}
                self.record["state"] = final
                self.record["note"] = f"stop: {reason}"
                self.persist()
            self.emit("attention", "stopped", f"{final} ({reason}) stop_audit={audit['verdict']}"
                      + (f" wind_down={wind_down.get('verdict')}" if wind_down is not None else ""))
        code = M.EXIT_PIN_FAILED if final == "tripped" else M.EXIT_MUSE_FAILED if final == "unclean" else M.EXIT_OK
        result = {"verdict": final.upper(), "state": final, "stop_audit": audit["verdict"]}
        if wind_down is not None:
            result["wind_down"] = wind_down.get("verdict")
        return code, result

    def lean_wind_down(self, reason: str) -> Optional[dict]:
        lean = self.record.get("lean")
        if not lean:
            return None
        try:
            result = self.broker.wind_down_function(lean["goal"], Path(self.record["target"]))
        except Exception as exc:  # a wind-down that cannot run is not OK
            result = {"verdict": "NOT_OK", "status": "ERROR", "detail": f"wind-down raised: {exc!r}", "residual": []}
        entry = {"verdict": result.get("verdict"), "status": result.get("status"),
                 "detail": PB.one_line(result.get("detail") or "", 300), "residual": (result.get("residual") or [])[:10],
                 "reason": reason, "at": PB.now_iso(), "exit": result.get("exit")}
        with self.data_lock:
            lean["wind_down"] = entry
            if self.dir.is_dir():
                PB.write_private_json(self.dir / "wind-down.json", result)
            self.persist()
        self.emit("attention", "wind-down", f"goal {lean['goal']} wind_down={entry['verdict']} "
                  f"status={entry['status']} residual={len(entry['residual'])} ({reason})")
        return entry


# ---------------------------------------------------------------------------
# Broker process


class Broker:
    def __init__(self, module_root: Path, state: Path, instance: str, environ: dict,
                 idle_seconds: float = BROKER_IDLE_SECONDS, session_idle_seconds: float = SESSION_IDLE_SECONDS,
                 peer_uid_function: Callable[[socket.socket], Optional[int]] = PB.peer_uid) -> None:
        self.module_root = module_root
        self.state = state
        self.instance = instance
        self.environ = dict(environ)
        self.idle_seconds = idle_seconds
        self.session_idle_seconds = session_idle_seconds
        self.reconcile_seconds = M.reconcile_seconds(self.environ)
        self.peer_uid_function = peer_uid_function
        self.sessions: dict[str, MuseSession] = {}
        self.lock = threading.RLock()
        self.tripped = False
        self.stopping = threading.Event()
        self.last_request = time.monotonic()
        self.code = code_digest(module_root)
        # Lean-mode host hooks (shared with Luna's Lean mode); tests replace them.
        self.lean_repositories: Callable[[], tuple[Path, ...]] = \
            lambda: luna_lean.repositories(M.launch_root(self.module_root))
        self.host_probe: Callable[[str], tuple[list[str], dict]] = luna_lean.host_observation
        self.residual_scan: Callable[[Path], list] = luna_lean.residual_processes
        self.wind_down_function: Callable[[str, Path], dict] = lambda goal, target: luna_lean.run_wind_down(
            self.module_root, self.environ, goal, target, self.residual_scan)
        self.lean_mcp_check: Callable[[], list[str]] = lambda: M.lean_mcp_failures(self.environ)
        self.reconciling: set[str] = set()

    def serve(self) -> int:
        listener = PB.listen(self.state, M.STATE_ENV)
        self.reconcile_registry()
        PB.write_private_json(PB.info_path(self.state), {
            "pid": os.getpid(), "instance": self.instance, "socket": str(PB.socket_path(self.state)),
            "started": PB.now_iso(), "uid": os.getuid(), "module_root": str(self.module_root), "code": self.code,
        })
        try:
            PB.serve_loop(listener, self.stopping, self.check_idle, self.handle)
        finally:
            self.stop_all("broker shutdown")
            listener.close()
            PB.remove_own_socket(self.state, self.instance)
        return 0

    def check_idle(self) -> None:
        with self.lock:
            live = [session for session in self.sessions.values() if session.is_open()]
        if not live and time.monotonic() - self.last_request > self.idle_seconds:
            self.stopping.set()

    def reconcile_registry(self) -> None:
        """Mark sessions of a dead broker lost; wind down their Lean goals beside the listener."""
        pending = []
        for record in all_records(self.state):
            if record.get("state") not in OPEN_STATES or record.get("broker_instance") == self.instance:
                continue
            record["state"] = "lost"
            record["note"] = "broker exited while the session was open (its muse serve child exits with its stdin)"
            if record.get("lean"):
                record["lean"]["wind_down"] = {"verdict": "PENDING", "reason": "crash recovery", "at": PB.now_iso()}
                pending.append(record)
                self.reconciling.add(record["lean"]["goal"])
            PB.write_private_json(PB.sessions_dir(self.state) / record["id"] / "session.json", record)
        if pending:
            threading.Thread(target=self._reconcile_lean, args=(pending,), daemon=True).start()

    def _reconcile_lean(self, records: list[dict]) -> None:
        for record in records:
            goal = record["lean"]["goal"]
            try:
                result = self.wind_down_function(goal, Path(record["target"]))
            except Exception as exc:
                result = {"verdict": "NOT_OK", "status": "ERROR", "detail": f"wind-down raised: {exc!r}"}
            record["lean"]["wind_down"] = {
                "verdict": result.get("verdict"), "status": result.get("status"),
                "detail": PB.one_line(result.get("detail") or "", 300),
                "residual": (result.get("residual") or [])[:10], "reason": "crash recovery", "at": PB.now_iso()}
            PB.write_private_json(PB.sessions_dir(self.state) / record["id"] / "session.json", record)
            with self.lock:
                self.reconciling.discard(goal)

    def stop_all(self, reason: str) -> None:
        with self.lock:
            sessions = [session for session in self.sessions.values() if session.is_open()]
        threads = [threading.Thread(target=session.stop, kwargs={"reason": reason}, daemon=True)
                   for session in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(PB.STOP_WAIT_SECONDS + 15)

    def trip_all(self, offender: Optional[MuseSession], failures: list[str]) -> None:
        with self.lock:
            first = not self.tripped
            self.tripped = True
            sessions = [session for session in self.sessions.values() if session.is_open()]
        if offender is not None:
            offender.emit("attention", "pin-alarm", f"{M.STOP_MESSAGE} Failures: {'; '.join(failures)}")
            M.record_tripwire(self.state, offender.id, offender.record.get("muse_session"), failures)
        if not first:
            return
        for session in sessions:
            if session is not offender:
                session.emit("attention", "pin-alarm", "stopping: the model-pin failure tripwire is recorded")
            threading.Thread(target=session.stop, kwargs={"reason": "tripwire"}, daemon=True).start()

    # -- requests -------------------------------------------------------
    def handle(self, connection: socket.socket) -> None:
        def touch() -> None:
            self.last_request = time.monotonic()

        def failure(exc: Exception) -> dict:
            if isinstance(exc, (PB.BrokerError, M.MuseError)):
                return {"ok": False, "code": M.EXIT_PREFLIGHT_REFUSED, "error": str(exc)}
            return {"ok": False, "code": M.EXIT_MUSE_FAILED, "error": f"broker error: {exc!r}"}

        PB.handle_connection(connection, self.peer_uid_function, self.dispatch, touch, failure)

    def session(self, session_id: Any) -> MuseSession:
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            record = load_record(self.state, str(session_id))
            if record is None:
                raise PB.BrokerError(f"no session {session_id}")
            raise PB.BrokerError(f"session {session_id} is {record.get('state')} and not held by this broker; "
                                 f"resume Muse session {record.get('muse_session')} in a new session")
        return session

    def dispatch(self, request: dict) -> dict:
        op = request.get("op")
        if op == "ping":
            with self.lock:
                live = sorted(sid for sid, session in self.sessions.items() if session.is_open())
            return {"ok": True, "code": 0, "instance": self.instance, "pid": os.getpid(), "uid": os.getuid(),
                    "live": live, "tripped": self.tripped, "module_root": str(self.module_root),
                    "code_digest": self.code}
        if op in ("start", "resume"):
            return self.open_session(request, op)
        if op == "shutdown":
            with self.lock:
                count = sum(1 for session in self.sessions.values() if session.is_open())
            self.stop_all("shutdown")
            self.stopping.set()
            return {"ok": True, "code": 0, "stopped": count}
        session = self.session(request.get("session"))
        if op == "send":
            text = str(request.get("text") or "")
            code, result = session.steer(text) if request.get("steer") else session.send(text)
        elif op == "interrupt":
            code, result = session.interrupt()
        elif op == "approve":
            code, result = session.approve(str(request.get("approval")), str(request.get("decision")))
        elif op == "detail":
            level = request.get("level")
            if level not in PB.DETAIL_LEVELS:
                return {"ok": False, "code": PB.EXIT_USAGE, "error": f"detail must be one of {PB.DETAIL_LEVELS}"}
            with session.data_lock:
                session.record["detail"] = level
                session.persist()
            code, result = 0, {"verdict": "UPDATED", "detail": level}
        elif op == "stop":
            code, result = session.stop("requested")
        else:
            return {"ok": False, "code": PB.EXIT_USAGE, "error": f"unknown op {op!r}"}
        return {"ok": code == 0, "code": code, "session": session.id, "state": session.state, **result}

    def open_session(self, request: dict, op: str) -> dict:
        detail = request.get("detail") or PB.DEFAULT_DETAIL
        if detail not in PB.DETAIL_LEVELS:
            return {"ok": False, "code": PB.EXIT_USAGE, "error": f"detail must be one of {PB.DETAIL_LEVELS}"}
        brief = request.get("brief") if op == "start" else None
        target = request.get("target")
        lean_goal = request.get("lean")
        mode = "lean" if lean_goal is not None else ("write" if request.get("write") else "read-only")
        effort = request.get("effort") or M.DEFAULT_EFFORT
        resume: Optional[str] = None
        prior: Optional[dict] = None
        refusals: list[str] = []
        if op == "start" and brief is None:
            refusals.append("start needs a brief")
        if op == "resume":
            resume = str(request.get("muse_session") or "")
            if not _MUSE_SESSION.match(resume):
                return {"ok": False, "code": PB.EXIT_USAGE, "error": f"not a Muse session id: {resume!r}"}
            priors = [record for record in all_records(self.state) if record.get("muse_session") == resume]
            prior = priors[-1] if priors else None
            if prior is None:
                refusals.append(f"no session record names Muse session {resume}; only recorded sessions resume")
            else:
                if target is not None and Path(str(target)).expanduser().resolve() != Path(prior["target"]):
                    refusals.append("resume keeps the recorded target")
                target = prior["target"]
                recorded_mode = prior.get("mode")
                if request.get("mode_explicit") and mode != recorded_mode:
                    refusals.append(f"resume keeps the recorded mode {recorded_mode}; the session's permission "
                                    f"profile was fixed at bootstrap")
                mode = recorded_mode
                lean_goal = (prior.get("lean") or {}).get("goal")
                effort = request.get("effort") or prior.get("effort") or effort
            with self.lock:
                if any(session.is_open() and session.record.get("muse_session") == resume
                       for session in self.sessions.values()):
                    refusals.append(f"Muse session {resume} is already open in this broker")
        if target is None:
            refusals.append("no target")
        else:
            refusals += M.early_refusals(self.state, effort, brief, self.module_root, str(target), mode, lean_goal,
                                         self.lean_repositories)
        if lean_goal is not None:
            refusals += self.lean_mcp_check()
        if refusals:
            return {"ok": False, "code": M.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED", "refusals": refusals}
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        session_id = f"ms-{stamp}-{secrets.token_hex(3)}"
        record = {
            "id": session_id, "created": PB.now_iso(), "state": "starting", "muse_session": resume,
            "target": str(Path(str(target)).expanduser().resolve()), "mode": mode, "effort": effort,
            "detail": detail, "broker_instance": self.instance, "turns": [], "pending_approvals": [],
            "last_event": None, "last_attention_seq": 0, "resumed_from": (prior or {}).get("id"),
            "turn_timeout_seconds": int(request.get("turn_timeout_seconds") or M.DEFAULT_TIMEOUT_SECONDS),
            "model": PINNED_MODEL, "profile": M.PROFILES[mode], "serve_flags": list(M.SERVE_FLAGS[mode]),
        }
        if lean_goal is not None:
            record["lean"] = {"goal": str(lean_goal)}
        session = MuseSession(self, session_id, record)
        with self.lock:
            if lean_goal is not None:
                refusals = self.lean_admission(str(lean_goal), Path(record["target"]), record)
                if refusals:
                    return {"ok": False, "code": M.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED", "refusals": refusals}
            self.sessions[session_id] = session
        code, result = session.open(brief, resume)
        return {"ok": code == 0, "code": code, "session": session_id, "state": session.state,
                "muse_session": record.get("muse_session"), "mode": mode, "effort": effort, "detail": detail,
                "resumed_from": record.get("resumed_from"), "lean": lean_goal, **result}

    def lean_admission(self, goal: str, target: Path, record: dict) -> list[str]:
        """Called with the broker lock held: the Lean cap and goal rule, host admits, target is quiet."""
        live = [session.id for session in self.sessions.values()
                if session.record.get("lean") and session.state not in TERMINAL_STATES]
        limit = _max_lean_sessions(self.environ)
        refusals: list[str] = []
        if len(live) >= limit:
            refusals.append(f"already {len(live)} Lean sessions are live ({', '.join(live)}); at most {limit}")
        same = [session.id for session in self.sessions.values()
                if (session.record.get("lean") or {}).get("goal") == goal and session.state not in TERMINAL_STATES]
        if same:
            refusals.append(f"a Lean session for goal {goal} is already live ({', '.join(same)})")
        if refusals:
            return refusals
        if self.reconciling:
            return [f"crash-recovery wind-down is still running for {sorted(self.reconciling)}"]
        refusals, observed = self.host_probe(goal)
        residual = self.residual_scan(target)
        if residual:
            refusals.append(f"{len(residual)} Lean/Lake process(es) already run in {target}; wind down first")
        record["lean"]["host_admission"] = {"observed": observed, "refusals": refusals,
                                            "residual": residual[:10], "at": PB.now_iso()}
        return refusals


def serve_main(module_root: Path, instance: str, environ: Optional[dict] = None) -> int:
    environ = dict(os.environ if environ is None else environ)

    def number(name: str, default: float) -> float:
        try:
            return float(environ.get(name, default))
        except ValueError:
            return default

    broker = Broker(module_root, M.state_root(module_root, environ), instance, environ,
                    idle_seconds=number(IDLE_ENV, BROKER_IDLE_SECONDS),
                    session_idle_seconds=number(SESSION_IDLE_ENV, SESSION_IDLE_SECONDS))
    return broker.serve()


# ---------------------------------------------------------------------------
# Client commands: each returns (exit code, bounded lines, JSON record)


def ensure_broker(module_root: Path, environ: dict, start_timeout: float = 20.0) -> dict:
    return PB.ensure_broker(
        M.state_root(module_root, environ), module_root, environ, code_digest(module_root),
        lambda instance: [sys.executable, "-m", "creme", "muse", "broker-serve", "--instance", instance],
        start_timeout)


def verdict_code(record: dict) -> int:
    state = record.get("state")
    if state == "tripped":
        return M.EXIT_PIN_FAILED
    if state == "refused":
        return M.EXIT_PREFLIGHT_REFUSED
    if state == "unclean":
        return M.EXIT_MUSE_FAILED
    if state == "stopped":
        return M.EXIT_OK if (record.get("stop_audit") or {}).get("verdict", "PASS") == "PASS" else M.EXIT_PIN_FAILED
    if record.get("pending_approvals"):
        return PB.EXIT_ATTENTION
    turns = record.get("turns") or []
    verdict = turns[-1].get("verdict") if turns else None
    if verdict == "PIN_FAILED":
        return M.EXIT_PIN_FAILED
    if verdict == "FAILED" or state in ("failed", "lost"):
        return M.EXIT_MUSE_FAILED
    if verdict == "INTERRUPTED":
        return PB.EXIT_INTERRUPTED
    return M.EXIT_OK


def session_lines(record: dict, excerpt_lines: int = 3) -> list[str]:
    turns = record.get("turns") or []
    last = turns[-1] if turns else {}
    tokens = last.get("tokens") or {}
    lines = [PB.one_line(
        f"session={record.get('id')} state={record.get('state')} muse_session={record.get('muse_session')} "
        f"turn={last.get('n')} status={last.get('status')} verdict={last.get('verdict')} "
        f"models={last.get('models')} steers={last.get('steers', 0)} "
        f"tokens prompt={tokens.get('prompt')} out={tokens.get('output')}", 260)]
    if record.get("stop_audit"):
        lines[0] += f" stop_audit={record['stop_audit'].get('verdict')}"
    wind_down = (record.get("lean") or {}).get("wind_down")
    if wind_down:
        lines[0] += f" wind_down={wind_down.get('verdict')}"
    for approval in record.get("pending_approvals") or []:
        lines.append(PB.one_line(f"approval {approval.get('id')} [accept|decline]: {approval.get('summary')}"))
    for failure in (last.get("failures") or [])[:3] + (last.get("errors") or [])[:2]:
        lines.append(f"failure: {PB.one_line(failure)}")
    if last.get("last_message"):
        try:
            text = Path(last["last_message"]).read_text(encoding="utf-8").splitlines()
        except OSError:
            text = []
        lines.append(f"final={last['last_message']} ({len(text)} lines)")
        lines.extend("  " + PB.one_line(line, 160) for line in text[:excerpt_lines])
    if record.get("state") == "tripped" or last.get("verdict") == "PIN_FAILED":
        lines.append(M.STOP_MESSAGE)
    return lines


SPEC = PB.ClientSpec(
    command="muse", display="Muse", state_root=M.state_root, session_pattern=_SESSION_ID, session_prefix="ms-",
    verdict_code=verdict_code, session_lines=session_lines, exit_ok=M.EXIT_OK,
    exit_refused=M.EXIT_PREFLIGHT_REFUSED, exit_failed=M.EXIT_MUSE_FAILED, server_label="muse",
    resume_hint="resume its Muse session in a new broker session",
    list_header=lambda state: f"tripwire={'PRESENT' if M.tripwire_path(state).exists() else 'absent'}",
    open_states=OPEN_STATES, terminal_states=TERMINAL_STATES,
)


def _call(module_root: Path, environ: dict, op: str, autostart: bool, **arguments: Any) -> dict:
    return PB.broker_call(SPEC, module_root, environ, op, autostart, ensure_broker, **arguments)


def _open_output(answer: dict) -> tuple[int, list[str], dict]:
    code = int(answer.get("code", M.EXIT_MUSE_FAILED))
    if not answer.get("session"):
        return code, PB.refusal_lines(SPEC, answer), answer
    lines = [f"session={answer['session']} state={answer.get('state')} muse_session={answer.get('muse_session')} "
             f"mode={answer.get('mode')} effort={answer.get('effort')} model={PINNED_MODEL} "
             f"detail={answer.get('detail')}" + (f" lean={answer['lean']}" if answer.get("lean") else "")
             + (f" resumed_from={answer['resumed_from']}" if answer.get("resumed_from") else "")]
    if code == 0:
        lines.append(f"next: muse wait {answer['session']}  |  muse events {answer['session']} --follow")
    else:
        lines.extend(PB.refusal_lines(SPEC, answer))
    return code, lines, answer


def _client_refusals(module_root: Path, environ: dict, effort: str, brief: Optional[str], target: Optional[str],
                     mode: str, lean: Optional[str]) -> Optional[tuple[int, list[str], dict]]:
    refusals = M.early_refusals(M.state_root(module_root, environ), effort, brief, module_root, target, mode, lean)
    if not refusals:
        return None
    answer = {"ok": False, "code": M.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED", "refusals": refusals}
    return answer["code"], PB.refusal_lines(SPEC, answer), answer


def cmd_start(module_root: Path, environ: dict, brief: str, target: str, write: bool, effort: str, detail: str,
              turn_timeout_seconds: int, lean: Optional[str] = None) -> tuple[int, list[str], dict]:
    mode = "lean" if lean is not None else ("write" if write else "read-only")
    refused = _client_refusals(module_root, environ, effort, brief, target, mode, lean)
    if refused:
        return refused
    arguments = {"lean": lean} if lean is not None else {}
    return _open_output(_call(module_root, environ, "start", True, brief=brief, target=target, write=write,
                              effort=effort, detail=detail, turn_timeout_seconds=turn_timeout_seconds, **arguments))


def cmd_resume(module_root: Path, environ: dict, muse_session: str, target: Optional[str], write: bool,
               effort: Optional[str], detail: str, turn_timeout_seconds: int,
               lean: Optional[str] = None) -> tuple[int, list[str], dict]:
    if effort is not None and effort not in EFFORTS:
        answer = {"ok": False, "code": M.EXIT_PREFLIGHT_REFUSED, "verdict": "REFUSED",
                  "refusals": [f"effort {effort!r} is not one of {', '.join(EFFORTS)}"]}
        return answer["code"], PB.refusal_lines(SPEC, answer), answer
    arguments = {"lean": lean} if lean is not None else {}
    return _open_output(_call(module_root, environ, "resume", True, muse_session=muse_session, target=target,
                              write=write, mode_explicit=bool(write or lean), effort=effort, detail=detail,
                              turn_timeout_seconds=turn_timeout_seconds, **arguments))


def cmd_simple(module_root: Path, environ: dict, op: str, session: str, **arguments: Any) -> tuple[int, list[str], dict]:
    return PB.simple_output(SPEC, _call(module_root, environ, op, False, session=session, **arguments))


def cmd_wait(module_root: Path, environ: dict, session: str, timeout: float,
             poll_seconds: float = 0.5) -> tuple[int, list[str], dict]:
    return PB.cmd_wait(SPEC, module_root, environ, session, timeout, poll_seconds)


def cmd_events(module_root: Path, environ: dict, session: str, follow: bool, last: int, since: int,
               emit: Callable[[str], None], poll_seconds: float = 0.5, timeout: Optional[float] = None) -> int:
    return PB.cmd_events(SPEC, module_root, environ, session, follow, last, since, emit, poll_seconds, timeout)


def cmd_read(module_root: Path, environ: dict, session: str, lines: int) -> tuple[int, list[str], dict]:
    state = M.state_root(module_root, environ)
    record = load_record(state, session)
    if record is None:
        return PB.EXIT_USAGE, [f"no session {session}"], {}
    lines = max(1, min(lines, 60))
    message = next((turn.get("last_message") for turn in reversed(record.get("turns") or [])
                    if turn.get("last_message")), None)
    if not message:
        return M.EXIT_OK, [f"session={session} has no final message yet (state={record.get('state')})"], record
    text = Path(message).read_text(encoding="utf-8").splitlines()
    output = [f"final={message} ({len(text)} lines, showing {min(lines, len(text))})"]
    output.extend(line[:PB.LINE_WIDTH] for line in text[:lines])
    return M.EXIT_OK, output, record


def cmd_sessions(module_root: Path, environ: dict, limit: int) -> tuple[int, list[str], dict]:
    return PB.cmd_list(SPEC, module_root, environ, limit,
                       lambda record: f"muse_session={record.get('muse_session')} {record.get('mode')}")


def cmd_detail(module_root: Path, environ: dict, session: str, level: str) -> tuple[int, list[str], dict]:
    return PB.cmd_detail(SPEC, module_root, environ, session, level, ensure_broker)


def cmd_clear_tripwire(module_root: Path, environ: dict, reason: str) -> tuple[int, list[str], dict]:
    """Clear MODEL_PIN_FAILURE (a master action with a recorded reason); refused while any session is live."""
    state = M.state_root(module_root, environ)
    answer = PB.probe_broker(state)
    live = list((answer or {}).get("live") or [])
    live += [record["id"] for record in all_records(state)
             if record.get("state") in OPEN_STATES and record["id"] not in live and answer is not None]
    by = f"{environ.get('USER', 'unknown')} via creme muse clear-tripwire"
    return M.clear_tripwire(state, reason, live, by)


def cmd_shutdown(module_root: Path, environ: dict) -> tuple[int, list[str], dict]:
    return PB.cmd_shutdown(SPEC, module_root, environ)
