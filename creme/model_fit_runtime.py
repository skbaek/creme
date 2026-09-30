"""Durable episode dispatch, feedback inbox and within-client selection.

All public mutations share the episode store's SQLite transaction. Inbound
receipts are committed before application, then retried after dependencies arrive.
The database is private runtime evidence, not a vendor benchmark or a price table.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import model_fit_episodes as E
from .model_fit_efficiency import Budget, Candidate, Moments, choose, finite

RuntimeError = E.EpisodeError


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def open_runtime(directory: Path):
    runtime = Path(directory) / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = E.open_store(runtime / "episodes-v2.sqlite3")
    os.chmod(store.path, 0o600)
    return store


def _policy(store, policy_id):
    row = store.conn.execute("SELECT * FROM fit_policies WHERE policy_id=?", (policy_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"unknown policy {policy_id!r}")
    return dict(row), json.loads(row["config"])


def configure(store, policy_id: str, config: dict, mode: str = "shadow"):
    """Immutable population; material changes require another policy identity.

    No legacy samples are imported. A legacy default may be explicitly chosen
    as a provisional incumbent, never as measured evidence. This controller
    provides advisory reservations only: it refuses an 'enforced' cap claim.
    """
    if mode not in {"shadow", "active", "off"}:
        raise RuntimeError("mode must be shadow, active or off")
    if not policy_id or not config.get("context_version") or not config.get("harness_version"):
        raise RuntimeError("policy, context_version and harness_version are required")
    if config.get("task_type") not in E.model_fit.TASK_TYPES:
        raise RuntimeError("unknown task type")
    candidates = config.get("candidates", [])
    options = []
    for c in candidates:
        family, effort = c["option"].split("/")
        ok, reason = E.check_capability(config["execution_client"], family, effort, c["route"])
        if not ok:
            raise RuntimeError(reason)
        if not c.get("release") or not c.get("recipe_version"):
            raise RuntimeError("candidate needs an observed catalogue release and fixed recipe")
        if family == "sol" and c["release"] not in E.SOL_CURRENT_IDS:
            raise RuntimeError("retired or unknown Sol release")
        candidate = Candidate(c["option"], c.get("token_bound"), c["prior_tokens"],
                              c.get("bound_assumption", "unavailable"))
        candidate.validate()
        if candidate.bound_assumption == "enforced":
            raise RuntimeError("this adapter has advisory reservations, no enforced episode cap")
        options.append(c["option"])
    if not options or len(options) != len(set(options)) or config.get("default") not in options:
        raise RuntimeError("distinct feasible candidates and an eligible default are required")
    if not 0 < finite(config.get("delta", .05), "delta") < 1:
        raise RuntimeError("delta must be in (0,1)")
    finite(config.get("seed_allowance", 0), "seed allowance")
    if not 0 <= finite(config.get("learning_fraction", .1), "learning fraction") <= .1:
        raise RuntimeError("learning fraction must be between zero and .1")
    with E._write_txn(store.conn):
        old = store.conn.execute("SELECT config FROM fit_policies WHERE policy_id=?", (policy_id,)).fetchone()
        if old:
            if old["config"] != encode(config):
                raise E.ConflictError("immutable population changed; use a new policy identity")
            return _policy(store, policy_id)[0]
        E._record_event(store.conn, f"policy:{policy_id}", "policy.configure",
                        {"policy_id": policy_id, "config": config, "mode": mode})
        store.conn.execute("INSERT INTO fit_policies VALUES (?,?,?,?,?)",
                           (policy_id, encode(config), mode, config["default"], E._now()))
    return _policy(store, policy_id)[0]


def set_mode(store, policy_id, mode, reason, event_id):
    if mode not in {"off", "shadow", "active"} or not reason:
        raise RuntimeError("mode and reason required")
    with E._write_txn(store.conn):
        _policy(store, policy_id)
        if E._record_event(store.conn, event_id, "policy.mode",
                            {"policy_id": policy_id, "mode": mode, "reason": reason}):
            store.conn.execute("UPDATE fit_policies SET mode=? WHERE policy_id=?", (mode, policy_id))
    return _policy(store, policy_id)[0]


def generation(config, candidate):
    return E.generation_key(candidate["release"], candidate["route"], candidate["recipe_version"],
                            E.material_descriptor({"version": config["context_version"]}),
                            config["harness_version"])


def statistics(store, config, candidate):
    """Aggregate only the joined prefix; never load the historical corpus into a prompt."""
    args = (config["execution_client"], config["task_type"], candidate["option"],
            generation(config, candidate))
    rows = store.conn.execute("""WITH population AS (
        SELECT e.*, COALESCE((SELECT SUM(CAST(s.amount_tokens AS REAL)) FROM spend_ledger s
          WHERE s.episode_id=e.episode_id AND s.amount_tokens!='null'),0) AS cost,
          COALESCE((SELECT SUM(a.accepted_work) FROM acceptances a WHERE a.episode_id=e.episode_id
          AND NOT EXISTS (SELECT 1 FROM acceptances b WHERE b.correction_of=a.accept_id)),0) AS work
        FROM episodes e WHERE execution_client=? AND task_type=? AND owner_option=? AND owner_generation=?
      ), prefix AS (SELECT * FROM population WHERE owner_seq < COALESCE(
          (SELECT MIN(owner_seq) FROM population WHERE status!='closed' OR usage_complete!=1), 9223372036854775807))
      SELECT COUNT(*) AS n, COALESCE(SUM(work),0) AS work, COALESCE(SUM(cost),0) AS cost,
        COALESCE(SUM(work*work),0) AS w2, COALESCE(SUM(cost*cost),0) AS c2,
        COALESCE(MAX(cost),0) AS maxcost FROM prefix""", args).fetchone()
    n = rows["n"]
    return Moments(n, rows["work"], rows["cost"],
                   max(0, rows["w2"] - rows["work"]**2/n) if n else 0,
                   max(0, rows["c2"] - rows["cost"]**2/n) if n else 0, rows["maxcost"])


def _budget(store, policy_id, config):
    # Spend is the single ledger (usage rows already mirror into it). Never add
    # usage_tokens to spend_uncapped_tokens. Pending reservations cover only
    # the remaining predicted cost, so incurred cost is not counted twice.
    rows = store.conn.execute("""SELECT o.*, e.owner_option, e.status AS episode_status,
       COALESCE((SELECT SUM(CAST(s.amount_tokens AS REAL)) FROM spend_ledger s
                 WHERE s.episode_id=o.episode_id AND s.amount_tokens!='null'),0) AS cost,
       (SELECT COUNT(*) FROM spend_ledger s WHERE s.episode_id=o.episode_id AND s.amount_tokens='null') AS unknown
       FROM fit_opportunities o LEFT JOIN episodes e ON e.episode_id=o.episode_id WHERE policy_id=?""",
                              (policy_id,)).fetchall()
    normal = spent = reserved = 0.0
    last = count = concurrent = 0
    unknown = False
    trials, pending = {}, {}
    for row in rows:
        d = json.loads(row["decision"])
        if row["owner_option"]:
            key = row["owner_option"]
            trials[key] = trials.get(key, 0) + 1
        elif row["status"] == "pending":
            key = d["actual"]
            pending[key] = pending.get(key, 0) + 1
        if d["actual_exploration"] and row["status"] != "cancelled":
            last = max(last, row["sequence"])
            count += 1
            spent += row["cost"]
            if row["episode_status"] != "closed":
                concurrent += 1
                reserved += max(0, d["reservation"] - row["cost"])
            unknown = unknown or bool(row["unknown"])
        else:
            normal += row["cost"]
    allowance = config.get("seed_allowance", 0) + config.get("learning_fraction", .1) * normal
    return Budget(allowance, spent, reserved, concurrent, 1, unknown), last, count, trials, pending


def prepare(store, episode_id, policy_id, milestones, master_client, opportunity_ref, features=None):
    """One idempotent genuine ordinary-work opportunity, before any model outcome."""
    if not opportunity_ref:
        raise RuntimeError("ordinary-work reference required; recommendation queries are not opportunities")
    request = dict(episode_id=episode_id, policy_id=policy_id, milestones=milestones,
                   master_client=master_client, opportunity_ref=opportunity_ref, features=features or {})
    with E._write_txn(store.conn):
        prior = store.conn.execute("SELECT * FROM fit_opportunities WHERE episode_id=?", (episode_id,)).fetchone()
        if prior:
            if prior["request"] != encode(request):
                raise E.ConflictError("opportunity id reused with changed task")
            return json.loads(prior["decision"])
        policy, config = _policy(store, policy_id)
        if (features or {}) != config.get("context_features", {}):
            raise RuntimeError("task features differ from this fixed context; choose another policy population")
        seq = store.conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM fit_opportunities WHERE policy_id=?",
                                 (policy_id,)).fetchone()[0]
        candidates = [Candidate(c["option"], c.get("token_bound"), c["prior_tokens"],
                                 c.get("bound_assumption", "unavailable")) for c in config["candidates"]]
        moments = {c["option"]: statistics(store, config, c) for c in config["candidates"]}
        budget, last, count, trials, pending = _budget(store, policy_id, config)
        decision = choose(candidates, moments, policy["incumbent"], opportunity=seq,
                           last_exploration=last, exploration_count=count, trials=trials,
                           pending=pending, budget=budget, delta=config.get("delta", .05))
        d = asdict(decision)
        # JSON has no infinity; keep the diagnostic explicit and standard.
        for interval in d["intervals"].values():
            if math.isinf(interval["upper"]):
                interval["upper"] = None
        actual = decision.selected if policy["mode"] == "active" else config["default"]
        d.update(actual=actual, actual_exploration=decision.exploration and policy["mode"] == "active",
                 mode=policy["mode"], opportunity=seq, policy_id=policy_id, episode_id=episode_id,
                 budget=asdict(budget), selection_probability=1.0,
                 eligible_options=[c["option"] for c in config["candidates"]])
        candidate = next(c for c in config["candidates"] if c["option"] == actual)
        E.create_episode(store, episode_id, config["execution_client"], config["task_type"],
                          milestones, candidate["recipe_version"], master_client,
                          context={"version": config["context_version"]}, route=candidate["route"])
        E.propose(store, episode_id, episode_id, actual, candidate["prior_tokens"])
        if d["actual_exploration"]:
            E.reserve(store, episode_id, episode_id, decision.reservation, "expected allowance only")
        store.conn.execute("INSERT INTO fit_opportunities VALUES (?,?,?,?,?,?,?)",
                           (episode_id, policy_id, seq, encode(request), encode(d), "pending", ""))
        if policy["mode"] == "active":
            store.conn.execute("UPDATE fit_policies SET incumbent=? WHERE policy_id=?",
                               (decision.incumbent, policy_id))
    reconcile(store)
    return d


def _context(store, episode_id):
    row = store.conn.execute("SELECT * FROM fit_opportunities WHERE episode_id=?", (episode_id,)).fetchone()
    if not row:
        raise RuntimeError(f"opportunity {episode_id!r} has not arrived")
    _, config = _policy(store, row["policy_id"])
    return dict(row), config, json.loads(row["decision"])


def _launch(store, receipt):
    episode = receipt["episode_id"]
    row, config, decision = _context(store, episode)
    if row["status"] == "cancelled":
        raise RuntimeError("cancelled before launch; explicitly reconcile the cancellation first")
    actual = receipt["option"]
    if actual != decision["actual"] and not receipt.get("override_reason"):
        raise RuntimeError("actual launch differs from proposal: override reason required")
    family, effort = actual.split("/")
    # Actual release and route come from the adapter receipt, never the proposal.
    E.register_launch(store, receipt["run_id"], episode, family, effort, receipt["route"],
                      receipt["harness_version"], [c["option"] for c in config["candidates"]],
                      receipt.get("capability_exclusions"), receipt.get("override_reason", ""), receipt["release"])
    store.conn.execute("UPDATE fit_opportunities SET status='launched' WHERE episode_id=?", (episode,))


def _receipt(store, receipt):
    """Normalized native/broker receipt: actual launch plus usage and terminal."""
    _launch(store, receipt)
    run, episode = receipt["run_id"], receipt["episode_id"]
    for segment in receipt.get("segments", []):
        E.record_usage(store, segment["id"], episode, segment["usage"], run_id=run)
    if receipt.get("terminal"):
        E.record_attempt(store, receipt["receipt_id"] + ":terminal", run, receipt["terminal"],
                         receipt.get("detail", ""))
    if receipt.get("usage_complete"):
        if not receipt.get("usage_evidence"):
            raise RuntimeError("complete usage must cite adapter evidence")
        E.mark_run_usage_final(store, run, event_id=receipt["receipt_id"] + ":usage-final")
    else:
        store.conn.execute("UPDATE launches SET usage_final_count=NULL WHERE run_id=?", (run,))
        E._reopen(store.conn, episode)


def _accept(store, receipt):
    episode = receipt["episode_id"]
    _context(store, episode)
    # Combined native adapter: the normal acceptance imports the actual run.
    for run in receipt.get("runs", []):
        _receipt(store, {**run, "episode_id": episode})
    for segment in receipt.get("master_segments", []):
        E.record_master_segment(store, segment["id"], segment["client"], segment["usage"])
        E.allocate_shared(store, segment["id"] + ":" + episode, episode, segment["id"],
                           segment["weight"], "verification")
    credits = receipt.get("milestones", [])
    declared = json.loads(E.get_episode(store, episode)["work_credits"])
    amounts = {E._credit_name(c): c["credit"] for c in declared}
    if any(m not in amounts for m in credits):
        raise RuntimeError("acceptance credits undeclared work")
    if not receipt.get("verification_ref"):
        raise RuntimeError("master acceptance needs its verification reference")
    E.record_acceptance(store, receipt["receipt_id"], episode, receipt["verdict"],
                        sum(amounts[m] for m in credits), receipt["verifier"], receipt["worker_ref"],
                        correction_of=receipt.get("correction_of"), milestones=credits,
                        verification_ref=receipt["verification_ref"])


def submit(store, receipt):
    """Durably retain even out-of-order feedback; same id + changed payload refuses."""
    key = receipt.get("receipt_id")
    if not key or receipt.get("kind") not in {"launch", "run", "accept"}:
        raise RuntimeError("receipt_id and kind launch/run/accept required")
    payload = encode(receipt)
    with E._write_txn(store.conn):
        prior = store.conn.execute("SELECT * FROM fit_inbox WHERE receipt_id=?", (key,)).fetchone()
        if prior and prior["payload"] != payload:
            raise E.ConflictError("receipt id reused with different bytes")
        if not prior:
            if receipt.get("supersedes"):
                old = store.conn.execute("SELECT * FROM fit_inbox WHERE receipt_id=?", (receipt["supersedes"],)).fetchone()
                if not old or old["status"] != "pending":
                    raise RuntimeError("only an unapplied pending receipt may be superseded")
                old_payload = json.loads(old["payload"])
                if any(old_payload.get(k) != receipt.get(k) for k in ("episode_id", "kind")):
                    raise RuntimeError("inbox correction must stay in its episode and kind")
            if receipt.get("episode_id"):
                E._reopen(store.conn, receipt["episode_id"])
            store.conn.execute("INSERT INTO fit_inbox VALUES (?,?, 'pending','',?,'')", (key, payload, E._now()))
    reconcile(store)
    return dict(store.conn.execute("SELECT receipt_id,status,error FROM fit_inbox WHERE receipt_id=?", (key,)).fetchone())


def reconcile(store, limit=100):
    """Retry durable delayed receipts; atomically apply each or preserve its error.

    A blocked row never blocks unrelated rows. Reconciliation is bounded per
    pass, and the health view reports the remainder. No receipt is evicted.
    """
    if type(limit) is not int or limit < 1 or limit > 10000:
        raise RuntimeError("reconciliation limit must be in 1..10000")
    rows = store.conn.execute("SELECT * FROM fit_inbox WHERE status='pending' ORDER BY attempted_at,created_at,receipt_id LIMIT ?",
                              (limit,)).fetchall()
    for row in rows:
        receipt = json.loads(row["payload"])
        try:
            with E._write_txn(store.conn):
                {"launch": _launch, "run": _receipt, "accept": _accept}[receipt["kind"]](store, receipt)
                if receipt.get("supersedes"):
                    old = store.conn.execute("SELECT status FROM fit_inbox WHERE receipt_id=?", (receipt["supersedes"],)).fetchone()
                    if not old or old["status"] != "pending":
                        raise RuntimeError("pending receipt was already replaced; correction cannot fork")
                    store.conn.execute("UPDATE fit_inbox SET status='superseded' WHERE receipt_id=?", (receipt["supersedes"],))
                    E._record_event(store.conn, "inbox-correction:" + row["receipt_id"], "inbox.supersede",
                                    {"receipt_id": row["receipt_id"], "supersedes": receipt["supersedes"]})
                store.conn.execute("UPDATE fit_inbox SET status='applied',error='' WHERE receipt_id=?", (row["receipt_id"],))
        except (E.EpisodeError, KeyError, TypeError, ValueError) as exc:
            with E._write_txn(store.conn):
                store.conn.execute("UPDATE fit_inbox SET error=?,attempted_at=? WHERE receipt_id=?", (str(exc), E._now(), row["receipt_id"]))
    # Acceptance may precede final worker usage. Its one master action persists;
    # later adapter evidence closes the join automatically, without reacceptance.
    accepted = store.conn.execute("""SELECT e.episode_id FROM episodes e JOIN fit_opportunities o USING(episode_id)
       WHERE e.status!='closed' AND EXISTS (SELECT 1 FROM acceptances a WHERE a.episode_id=e.episode_id)
       ORDER BY o.reconciled_at,e.created_at,e.episode_id LIMIT ?""", (limit,)).fetchall()
    for row in accepted:
        episode = row["episode_id"]
        with E._write_txn(store.conn):
            store.conn.execute("UPDATE fit_opportunities SET reconciled_at=? WHERE episode_id=?", (E._now(), episode))
        if store.conn.execute("SELECT 1 FROM fit_inbox WHERE status='pending' AND json_extract(payload,'$.episode_id')=?",
                               (episode,)).fetchone():
            continue
        try:
            with E._write_txn(store.conn):
                if not store.conn.execute("SELECT 1 FROM launches WHERE episode_id=?", (episode,)).fetchone():
                    continue
                revision = store.conn.execute("SELECT COUNT(*) FROM fit_inbox WHERE json_extract(payload,'$.episode_id')=?", (episode,)).fetchone()[0]
                E.finalize_episode(store, episode, "master-acceptance-reconciler", "joined normal master acceptance",
                                   event_id=f"runtime-close:{episode}:{revision}")
                if store.conn.execute("SELECT 1 FROM reservations WHERE reservation_id=? AND status='open'", (episode,)).fetchone():
                    E.release_reservation(store, episode, "consumed")
        except E.EpisodeError:
            pass  # Compact health exposes the incomplete join, never a fake zero.
    return health(store, limit)


def cancel(store, episode_id, reason):
    """Cancel a never-launched proposal; actual attempts must retain their spend."""
    if not reason:
        raise RuntimeError("cancellation reason required")
    with E._write_txn(store.conn):
        _context(store, episode_id)
        if store.conn.execute("SELECT 1 FROM launches WHERE episode_id=?", (episode_id,)).fetchone():
            raise RuntimeError("launched episode needs terminal and acceptance, not proposal cancellation")
        if E._record_event(store.conn, "cancel:" + episode_id, "opportunity.cancel", {"episode_id": episode_id, "reason": reason}):
            store.conn.execute("UPDATE fit_opportunities SET status='cancelled' WHERE episode_id=?", (episode_id,))
            if store.conn.execute("SELECT 1 FROM reservations WHERE reservation_id=?", (episode_id,)).fetchone():
                E.release_reservation(store, episode_id, "cancelled")
    return {"episode_id": episode_id, "status": "cancelled"}


def health(store, limit=100):
    integrity = store.conn.execute("PRAGMA quick_check").fetchone()[0]
    pending = [dict(r) for r in store.conn.execute(
        "SELECT receipt_id,error FROM fit_inbox WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,))]
    return {"integrity": integrity, "episodes": store.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "closed": store.conn.execute("SELECT COUNT(*) FROM episodes WHERE status='closed'").fetchone()[0],
            "inbox_pending": store.conn.execute("SELECT COUNT(*) FROM fit_inbox WHERE status='pending'").fetchone()[0],
            "pending": pending, "joins": E.reconcile(store, limit),
            "policies": [dict(r) for r in store.conn.execute("SELECT policy_id,mode,incumbent FROM fit_policies")],
            "legacy": "preserved separately; no statistical import"}


def table(store, policy_id):
    row, config = _policy(store, policy_id)
    budget, last, count, trials, pending = _budget(store, policy_id, config)
    return {"policy_id": policy_id, "mode": row["mode"], "incumbent": row["incumbent"],
            "budget": asdict(budget), "last_exploration": last, "explorations": count,
            "candidates": [{"option": c["option"], "moments": asdict(statistics(store, config, c)),
                            "launched": trials.get(c["option"], 0), "pending": pending.get(c["option"], 0)}
                           for c in config["candidates"]]}
