"""Model-fit episode evidence and lifecycle store (stages 1/2 foundation).

One durable SQLite store per goal (stdlib ``sqlite3`` only) holds the full
episode lifecycle as an append-preserved event log plus a projected state.
Every mutation runs in one atomic transaction: the event row is inserted
first, then the projection is updated. Reopening the file preserves all
attribution and statistics; there is no in-memory-only state and no eviction.

Why SQLite: atomic multi-row transactions, a unique-constraint idempotency
key on every event, and ordered replay come from one standard-library file.
A second handwritten JSON projection plus unbounded replay on every dispatch
is deliberately not built.

Contract summary (full rules live in the stage-1/2 report):

- Episode identity: fixed predeclared named milestone work credits
  totalling at most 1, the selected execution client, task/context
  descriptors, and a bounded recovery recipe version. The master client is
  context only: all statistics partition by execution client.
- Proposal/reservation is separate from actual launch. A launch records the
  actual run id, adapter-observed effective release, effort, route, and
  harness version, the eligibility snapshot, and any override reason. No
  release is ever synthesized: every actual launch carries its real
  observed model id, and sol-family launches must carry exactly GPT-6.1.
  Capability exclusions happen before ranking: a setting the route cannot
  run (e.g. Muse ``none`` effort on a broker route, which the broker
  rejects before launch) is recorded as a pre-launch rejection, never as a
  model failure, and a later actual run is never credited to the rejected
  setting.
- One episode has one pre-outcome owner: its initial launch (initial
  setting plus the fixed recovery recipe) fixes the owner cell and takes
  the next stable sequence there. All episode cost and credit key exactly
  once to that cell; fallback launches keep actual-configuration
  diagnostics but take no sequence and earn no separate credit, so a
  cheap-first recipe with an expensive fallback never creates a free
  standalone strong success. Context, release, recipe, and harness
  boundaries partition the cells.
- Usage: raw category evidence is retained; normalization validates every
  component (finite, non-negative, non-boolean) and follows explicit
  conventions (reasoning already inside output is not added twice, cached
  input already inside total input is not added twice, ``cache_write``
  counts only when declared additive). Absent components stay unknown and
  contradictory components are rejected, never silently combined.
  Cumulative provider counters enter as sourced, sequenced snapshots with
  store-computed deltas; late or decreasing snapshots are preserved raw
  without adding or subtracting spend. Missing usage stays visibly
  unknown, never invented zero. Shared master turns are allocated across
  episodes with weights summing to at most 1, so one master segment is
  never counted in multiple episodes.
- Attempt completion/cancellation is distinct from master acceptance.
  Partial/unknown/interrupted outcomes are supported, and a worker's own
  verdict is never acceptance. Credit names predeclared milestones and
  must equal their sum; corrections form a single chain with no forks, so
  no milestone is ever counted twice. Incurred cost and genuinely accepted
  credit survive interruption; retry, fallback, and master-verification
  costs stay in the same episode.
- Duplicate events are idempotent, with the replay check running before
  every cap or ceiling check; a conflicting reuse of an identity fails.
  Usage or completion arriving before its launch is rejected, never
  silently held; resubmission after launch recovers idempotently. The
  event log is the durable inbox: nothing is evicted. Generation
  boundaries key on actual release, route, recipe, context, and harness;
  history stays intact and every cell keeps lifetime evidence.
- Master closure (``finalize_episode``) is the explicit usage-complete
  marker: every linked run terminal, declared verification usage present,
  and an acceptance recorded. Only finalized episodes are
  inference-ready; partial cost stays visible in accounting throughout.
- Views expose cumulative per-cell episode work/cost/quality/accounting
  with missing/pending/censored coverage, plus exact ready-episode
  observations (work, total uncapped cost, owner candidate/generation,
  stable sequence) for the selector. This module implements no confidence
  formulas and no streak/backoff selection; selector mathematics lives in
  the master's own new files, never here. An explicit uncapped
  observed-spend ledger and reservation state are retained; predictions
  alone never promise a strict cost cap.
- ``register_launch`` + ``record_acceptance`` form the small combined
  launch-registration/acceptance interface for brokers and native adapters.
- ``candidate_order``/``prefix_ready``/``ready_observations`` expose the
  finalized ready prefix and unresolved earlier gaps per owner cell, so
  later inference uses the fully joined prefix rather than whichever jobs
  finish first; this metadata is not confidence mathematics.

Automatic hooks still needed (not implemented here): broker/native launch
paths must call ``register_launch`` after capability check and stream usage
segments per turn; the normal master acceptance action must call
``record_acceptance`` once with its verification reference; a reconciler
must surface launched-but-unjoined runs. See the STATE-BRIEF beside this
module for exact seams.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import model_fit

SCHEMA_VERSION = 2

# Release identity is always observed, never synthesized: every actual launch
# must carry the real effective model id reported by its adapter. The store
# invents no family/month release names and never substitutes a human label
# for an observed id. The one explicit migration rule is Sol: the current
# tool-catalogue id is ``gpt-6.1-sol``; the retired ``gpt-6-sol`` (and any
# other sol-family id, including the human label ``GPT-6.1``) is rejected,
# never recorded as current evidence. The exact observed id is retained.
SOL_CURRENT_IDS = frozenset({"gpt-6.1-sol"})
SOL_RETIRED_IDS = frozenset({"gpt-6-sol"})

# Efforts a route cannot execute, rejected before launch (never a failure).
# Muse broker routes have no "none" effort in the live catalogue
# (creme/muse_client.py EFFORTS), while model_fit still lists none for
# session-inherited workers: recommending muse-spark/none on a broker route
# is a pre-launch capability rejection, not a model failure.
ROUTE_EFFORT_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "muse-broker": ("none",),
    "muse-run": ("none",),
}

VERDICTS = ("pass", "partial", "fail", "unknown")
ATTEMPT_ENDS = ("completed", "failed", "interrupted", "cancelled")


class EpisodeError(Exception):
    """Malformed request or unusable episode state."""


class ConflictError(EpisodeError):
    """An identity was reused with conflicting payload."""


class CapabilityError(EpisodeError):
    """The setting cannot run on this route; rejected before launch."""


class MigrationError(EpisodeError):
    """Legacy evidence must not be imported as unbiased observations."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _load(raw: Optional[str]) -> Any:
    return json.loads(raw) if raw else None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY,
  execution_client TEXT NOT NULL,
  master_client TEXT NOT NULL,
  task_type TEXT NOT NULL,
  context TEXT NOT NULL,
  work_credits TEXT NOT NULL,
  work_total REAL NOT NULL,
  recipe_version TEXT NOT NULL,
  material_band TEXT NOT NULL DEFAULT '',
  generation TEXT NOT NULL,
  owner_option TEXT,
  owner_generation TEXT,
  owner_seq INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  usage_complete INTEGER NOT NULL DEFAULT 0,
  verification_required INTEGER NOT NULL DEFAULT 1,
  closed_by TEXT,
  closure_ref TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  candidate TEXT NOT NULL,
  predicted_cost TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS launches (
  run_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  family TEXT NOT NULL,
  effort TEXT NOT NULL,
  option TEXT NOT NULL,
  release TEXT NOT NULL,
  route TEXT NOT NULL,
  harness_version TEXT NOT NULL,
  eligibility TEXT NOT NULL,
  excluded TEXT NOT NULL,
  override_reason TEXT,
  generation TEXT NOT NULL,
  candidate_seq INTEGER,
  is_fallback INTEGER NOT NULL DEFAULT 0,
  usage_final_count INTEGER,
  status TEXT NOT NULL DEFAULT 'launched',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_counters (
  execution_client TEXT NOT NULL,
  task_type TEXT NOT NULL,
  option TEXT NOT NULL,
  generation TEXT NOT NULL,
  next_seq INTEGER NOT NULL,
  PRIMARY KEY (execution_client, task_type, option, generation)
);
CREATE TABLE IF NOT EXISTS usage_segments (
  segment_key TEXT PRIMARY KEY,
  run_id TEXT REFERENCES launches(run_id),
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  kind TEXT NOT NULL,
  raw TEXT NOT NULL,
  norm_input TEXT,
  norm_output TEXT,
  norm_total TEXT,
  alloc_weight REAL,
  master_segment_key TEXT REFERENCES master_segments(master_segment_key),
  purpose TEXT NOT NULL DEFAULT '',
  missing INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cumulative_sources (
  source_key TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  run_id TEXT REFERENCES launches(run_id),
  last_sequence INTEGER NOT NULL,
  last_total TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cumulative_snapshots (
  segment_key TEXT PRIMARY KEY,
  source_key TEXT NOT NULL REFERENCES cumulative_sources(source_key),
  sequence INTEGER NOT NULL,
  raw TEXT NOT NULL,
  delta TEXT NOT NULL,
  applied INTEGER NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS master_segments (
  master_segment_key TEXT PRIMARY KEY,
  master_client TEXT NOT NULL,
  raw TEXT NOT NULL,
  norm_total TEXT,
  source_hash TEXT,
  start_offset INTEGER,
  end_offset INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES launches(run_id),
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  status TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acceptances (
  accept_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  run_id TEXT REFERENCES launches(run_id),
  verdict TEXT NOT NULL,
  accepted_work REAL NOT NULL,
  verifier TEXT NOT NULL,
  worker_ref TEXT NOT NULL,
  correction_of TEXT REFERENCES acceptances(accept_id),
  milestones TEXT NOT NULL DEFAULT '[]',
  verification_ref TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
  reservation_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  amount_predicted TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_ledger (
  entry_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
  run_id TEXT REFERENCES launches(run_id),
  amount_tokens TEXT,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


@dataclass
class Store:
    """An open episode-evidence store. Use :func:`open_store`."""

    path: Path
    conn: sqlite3.Connection

    def close(self) -> None:
        self.conn.close()


_EXPECTED_COLUMNS: dict[str, list[str]] = {
    "meta": ["key", "value"],
    "events": ["event_id", "type", "payload", "created_at"],
    "episodes": ["episode_id", "execution_client", "master_client", "task_type",
                 "context", "work_credits", "work_total", "recipe_version", "material_band",
                 "generation", "owner_option", "owner_generation", "owner_seq", "status",
                 "usage_complete", "verification_required", "closed_by", "closure_ref",
                 "archived", "created_at"],
    "proposals": ["proposal_id", "episode_id", "candidate", "predicted_cost",
                  "status", "created_at"],
    "launches": ["run_id", "episode_id", "family", "effort", "option", "release",
                 "route", "harness_version", "eligibility", "excluded", "override_reason",
                 "generation", "candidate_seq", "is_fallback", "usage_final_count",
                 "status", "created_at"],
    "candidate_counters": ["execution_client", "task_type", "option", "generation",
                           "next_seq"],
    "usage_segments": ["segment_key", "run_id", "episode_id", "kind", "raw",
                       "norm_input", "norm_output", "norm_total", "alloc_weight",
                       "master_segment_key", "purpose", "missing", "created_at"],
    "master_segments": ["master_segment_key", "master_client", "raw", "norm_total",
                        "source_hash", "start_offset", "end_offset", "created_at"],
    "cumulative_sources": ["source_key", "episode_id", "run_id", "last_sequence",
                           "last_total", "updated_at"],
    "cumulative_snapshots": ["segment_key", "source_key", "sequence", "raw", "delta",
                             "applied", "resolved", "created_at"],
    "attempts": ["attempt_id", "run_id", "episode_id", "status", "detail", "created_at"],
    "acceptances": ["accept_id", "episode_id", "run_id", "verdict", "accepted_work",
                    "verifier", "worker_ref", "correction_of", "milestones",
                    "verification_ref", "created_at"],
    "reservations": ["reservation_id", "episode_id", "amount_predicted", "status",
                     "detail", "created_at"],
    "spend_ledger": ["entry_id", "episode_id", "run_id", "amount_tokens", "kind",
                     "created_at"],
}


def open_store(path: Path | str) -> Store:
    """Open (creating) the SQLite evidence store; safe across restarts.

    One clean schema suffices: no production v2 store exists, so there is
    no compatibility layer and no destructive rebuild. A file that already
    holds other tables, or lacks exactly this schema, is refused before
    any DDL runs. Foreign keys are enforced on every connection.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fresh = not target.exists() or target.stat().st_size == 0
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if not fresh:
        present = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if present != set(_EXPECTED_COLUMNS):
            raise EpisodeError(
                f"refusing to open {target}: unrecognized store layout"
                f" (found: {sorted(present - set(_EXPECTED_COLUMNS)) or 'nothing expected'})")
    else:
        conn.executescript(_SCHEMA)
    for table, columns in _EXPECTED_COLUMNS.items():
        actual = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if actual != columns:
            raise EpisodeError(
                f"refusing to open {target}: table {table} has an unsupported shape")
    row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif row["value"] != str(SCHEMA_VERSION):
        raise EpisodeError(f"episode store schema {row['value']} != supported {SCHEMA_VERSION}")
    return Store(target, conn)


# --------------------------------------------------------------------------
# Capability gate (exclusions before ranking)


def check_capability(execution_client: str, family: str, effort: str, route: str) -> tuple[bool, str]:
    """Decide whether a setting may launch on a route, before any ranking."""
    client = model_fit.CLIENTS.get(execution_client)
    if client is None:
        return False, f"unknown execution client {execution_client!r}"
    option = f"{family}/{effort}"
    if option not in client.options():
        return False, f"unknown option {option!r} for {execution_client}"
    if route not in client.routes:
        return False, f"route {route!r} is not a {execution_client} route"
    limited = client.family_routes.get(family)
    if limited is not None and route not in limited:
        return False, f"{family} is reachable only through {', '.join(limited)}"
    if family not in client.family_routes and any(
        route in routes for routes in client.family_routes.values()
    ):
        return False, f"route {route!r} does not serve model {family!r}"
    if effort in ROUTE_EFFORT_EXCLUSIONS.get(route, ()):
        return False, (
            f"route {route!r} cannot execute effort {effort!r} "
            f"(broker/catalogue rejection before launch; not a model failure)"
        )
    return True, "eligible"


def material_descriptor(context: Optional[dict[str, Any]], material_context: str = "") -> str:
    """Canonical stable descriptor of the declared predispatch context.

    Every declared context field participates: the full context mapping
    (keys sorted, JSON-canonical) plus the material band. Nothing declared
    is silently ignored, and JSON encoding (not delimiter joining) means
    hostile values such as ``"a|b"`` can never collide two descriptors.
    """
    try:
        return json.dumps({"context": context or {}, "material": material_context},
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise EpisodeError(f"episode context is not JSON-canonical: {exc}")


def generation_key(release: str, route: str, recipe_version: str, material_context: str = "",
                   harness_version: str = "") -> str:
    """Statistical generation: actual release, route, recipe, context, harness.

    All five boundary the cells: context, release, recipe, and harness
    versions partition evidence, so a harness or release change never mixes
    old runs into a new cell. The key is one JSON array, so no field value
    can collide across the boundaries by containing a delimiter.
    """
    return json.dumps([release, route, recipe_version, material_context, harness_version],
                      separators=(",", ":"))


# --------------------------------------------------------------------------
# Usage normalization (explicit conventions, raw retained)


def _close_enough(first: float, second: float) -> bool:
    return abs(first - second) <= 1e-6 * max(1.0, abs(first), abs(second))


def _num(value: Any, name: str) -> Optional[float]:
    """Validate one usage component: a finite non-negative number, None, or n/a."""
    if value is None or value == "n/a":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EpisodeError(f"usage component {name} must be a number, None, or n/a")
    result = float(value)
    if not math.isfinite(result):
        raise EpisodeError(f"usage component {name} must be finite")
    if result < 0:
        raise EpisodeError(f"usage component {name} must not be negative")
    return result


def normalize_usage(raw: dict[str, Any]) -> dict[str, Optional[float]]:
    """Normalize raw category usage without double counting.

    Accepted raw keys: ``total_input``, ``cached_input``, ``uncached_input``,
    ``total_output``, ``reasoning`` (each a finite non-negative number, None,
    or ``"n/a"``), ``reasoning_inside_output`` (bool, default True),
    ``cache_write``, and ``cache_write_additive`` (bool, default False).

    Conventions: cached input already inside total input is not added twice;
    reasoning already inside output is not added twice; ``cache_write`` is
    retained raw and added to input only when explicitly declared additive.
    Absent components stay unknown: a lone ``uncached_input`` without its
    cached counterpart, or lone ``reasoning`` without an output total, leaves
    that side None rather than treating the missing part as zero.
    Contradictory components (parts exceeding their whole, or parts that do
    not sum to a stated total) are rejected instead of silently combined.
    Anything unknown stays None (visibly unknown, never invented zero).
    """
    total_in = _num(raw.get("total_input"), "total_input")
    cached = _num(raw.get("cached_input"), "cached_input")
    uncached = _num(raw.get("uncached_input"), "uncached_input")
    if total_in is not None:
        if cached is not None and cached > total_in and not _close_enough(cached, total_in):
            raise EpisodeError("cached input exceeds total input")
        if uncached is not None and uncached > total_in and not _close_enough(uncached, total_in):
            raise EpisodeError("uncached input exceeds total input")
        if (cached is not None and uncached is not None
                and not _close_enough(total_in, cached + uncached)):
            raise EpisodeError("input parts do not sum to total input")
        norm_input: Optional[float] = total_in
    elif uncached is not None and cached is not None:
        norm_input = uncached + cached
    else:
        norm_input = None

    total_out = _num(raw.get("total_output"), "total_output")
    reasoning = _num(raw.get("reasoning"), "reasoning")
    inside = raw.get("reasoning_inside_output", True)
    if not isinstance(inside, bool):
        raise EpisodeError("reasoning_inside_output must be a bool")
    if total_out is not None:
        if inside:
            if reasoning is not None and reasoning > total_out \
                    and not _close_enough(reasoning, total_out):
                raise EpisodeError("reasoning exceeds total output it claims to sit inside")
            norm_output: Optional[float] = total_out
        else:
            if reasoning is None:
                norm_output = None
            else:
                norm_output = total_out + reasoning
    else:
        norm_output = None

    additive = raw.get("cache_write_additive", False)
    if not isinstance(additive, bool):
        raise EpisodeError("cache_write_additive must be a bool")
    if additive:
        cache_write = _num(raw.get("cache_write"), "cache_write")
        if cache_write is None or norm_input is None:
            norm_input = None
        else:
            norm_input += cache_write

    if norm_input is not None and norm_output is not None:
        norm_total: Optional[float] = norm_input + norm_output
    else:
        norm_total = None
    return {"input": norm_input, "output": norm_output, "total": norm_total}


def _credit_name(credit: dict[str, Any]) -> str:
    """Canonical milestone name for one predeclared work-credit entry."""
    name = credit.get("milestone", credit.get("m", credit.get("name", "")))
    return str(name) if name is not None else ""


def usage_from_model_fit_tokens(tokens_field: str) -> dict[str, Any]:
    """Convert a model-fit ``tokens`` field to a raw usage dict, keeping evidence.

    The model-fit schema has no totals: ``uncached_input``/``cache_read`` are
    disjoint parts, ``output`` is the provider total, ``reasoning`` overlaps
    it for providers that nest reasoning inside output.
    """
    parsed = model_fit.parse_tokens(tokens_field)
    raw: dict[str, Any] = {
        "uncached_input": parsed["uncached_input"],
        "cached_input": parsed["cache_read"],
        "total_input": None,
        "total_output": parsed["output"],
        "reasoning": parsed["reasoning"],
        "reasoning_inside_output": True,
        "cache_write": parsed["cache_write"],
        "model_fit_tokens": tokens_field,
    }
    return raw


# --------------------------------------------------------------------------
# Internal event machinery


def _record_event(conn: sqlite3.Connection, event_id: str, kind: str, payload: dict[str, Any]) -> bool:
    """Insert the event row. True if new; True (no-op) if identical replay.

    Raises ConflictError when the id is reused with a different payload.
    """
    body = _dump(payload)
    try:
        conn.execute(
            "INSERT INTO events (event_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_id, kind, body, _now()),
        )
        return True
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT type, payload FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is not None and row["type"] == kind and row["payload"] == body:
            return False
        raise ConflictError(f"event id {event_id!r} reused with conflicting payload")


def _episode_or_raise(conn: sqlite3.Connection, episode_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    if row is None:
        raise EpisodeError(f"unknown episode {episode_id!r}")
    return row


@contextmanager
def _write_txn(conn: sqlite3.Connection):
    """One serialized write transaction, safe to nest.

    Plain ``with conn:`` begins (at best) lazily at the first write, so
    two connections can both read stale state and then both write: duplicate
    owner sequences, over-allocated shares, double-counted caps. Every
    read-check-write unit here takes the write lock up front with BEGIN
    IMMEDIATE instead; a nested call inside an open transaction simply
    joins it, with the outermost block committing.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _evidence_fingerprint(conn: sqlite3.Connection, episode_id: str) -> str:
    """Count-based fingerprint of an episode's evidence rows for closure binding."""
    counts = []
    for table in ("launches", "usage_segments", "spend_ledger", "acceptances", "attempts"):
        counts.append(str(conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE episode_id=?",
            (episode_id,)).fetchone()["n"]))
    return ":".join(counts)


def _reopen(conn: sqlite3.Connection, episode_id: str) -> None:
    """Reopen a finalized episode when new evidence lands.

    Corrections, new usage, new launches, and new spend all invalidate a
    prior closure: the episode returns to open and must be re-finalized
    before it can be inference-ready again. The event log keeps the full
    closure history; only the current projection reopens.
    """
    conn.execute("UPDATE episodes SET status='open', usage_complete=0,"
                 " closed_by=NULL, closure_ref=NULL"
                 " WHERE episode_id=? AND status='closed'", (episode_id,))


# --------------------------------------------------------------------------
# Episode + proposal + reservation


def create_episode(
    store: Store,
    episode_id: str,
    execution_client: str,
    task_type: str,
    work_credits: list[dict[str, Any]],
    recipe_version: str,
    master_client: str = "",
    context: Optional[dict[str, Any]] = None,
    material_context: str = "",
    route: str = "",
    verification_required: bool = True,
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Declare an episode with fixed predeclared work credits totalling <= 1.

    Milestones are named once here; every acceptance must name the
    predeclared milestones it credits, so the same milestone can never be
    counted twice. No release is declared: the owner cell's actual release
    is fixed by the first launch's observed model id.
    """
    if execution_client not in model_fit.CLIENTS:
        raise EpisodeError(f"unknown execution client {execution_client!r}")
    if task_type not in model_fit.TASK_TYPES:
        raise EpisodeError(f"unknown task type {task_type!r}")
    if not isinstance(verification_required, bool):
        raise EpisodeError("verification_required must be a bool")
    total = 0.0
    names: list[str] = []
    for credit in work_credits:
        amount = float(credit.get("credit", 0))
        if amount < 0:
            raise EpisodeError("work credit must not be negative")
        name = _credit_name(credit)
        if not name:
            raise EpisodeError("every predeclared work credit must name its milestone")
        if name in names:
            raise EpisodeError(f"duplicate predeclared milestone {name!r}")
        names.append(name)
        total += amount
    if total > 1.0 + 1e-9:
        raise EpisodeError(f"predeclared work credits total {total} > 1")
    descriptor = material_descriptor(context, material_context)
    generation = generation_key("unlaunched", route, recipe_version, descriptor)
    conn = store.conn
    with _write_txn(conn):
        fresh = _record_event(conn, event_id or f"episode:{episode_id}", "episode.create", {
            "episode_id": episode_id, "execution_client": execution_client,
            "task_type": task_type, "work_credits": work_credits,
            "recipe_version": recipe_version, "master_client": master_client,
            "context": context or {}, "generation": generation,
            "verification_required": verification_required,
        })
        if not fresh:
            return get_episode(store, episode_id)
        try:
            conn.execute(
                "INSERT INTO episodes (episode_id, execution_client, master_client, task_type,"
                " context, work_credits, work_total, recipe_version, material_band, generation,"
                " verification_required, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (episode_id, execution_client, master_client, task_type, _dump(context or {}),
                 _dump(work_credits), total, recipe_version, material_context, generation,
                 1 if verification_required else 0, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"episode {episode_id!r} already exists with different payload")
    return get_episode(store, episode_id)


def get_episode(store: Store, episode_id: str) -> dict[str, Any]:
    row = _episode_or_raise(store.conn, episode_id)
    return {key: row[key] for key in row.keys()}


def propose(
    store: Store, proposal_id: str, episode_id: str, candidate: str,
    predicted_cost: Optional[float] = None, event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record a proposal (selection intent); distinct from any actual launch."""
    conn = store.conn
    with _write_txn(conn):
        row = _episode_or_raise(conn, episode_id)
        client = model_fit.CLIENTS[row["execution_client"]]
        if candidate not in client.options():
            raise EpisodeError(f"unknown candidate {candidate!r} for {client.name}")
        fresh = _record_event(conn, event_id or f"propose:{proposal_id}", "proposal", {
            "proposal_id": proposal_id, "episode_id": episode_id,
            "candidate": candidate, "predicted_cost": predicted_cost,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            return dict(existing)
        try:
            conn.execute(
                "INSERT INTO proposals (proposal_id, episode_id, candidate, predicted_cost, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (proposal_id, episode_id, candidate,
                 _dump(predicted_cost) if predicted_cost is not None else None, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"proposal {proposal_id!r} already exists with different payload")
    return {"proposal_id": proposal_id, "episode_id": episode_id, "candidate": candidate,
            "status": "pending", "predicted_cost": predicted_cost}


def reserve(
    store: Store, reservation_id: str, episode_id: str,
    amount_predicted: Optional[float] = None, detail: str = "",
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Reserve a spending allowance for an episode (advisory, never a cap proof)."""
    conn = store.conn
    with _write_txn(conn):
        _episode_or_raise(conn, episode_id)
        fresh = _record_event(conn, event_id or f"reserve:{reservation_id}", "reservation.open", {
            "reservation_id": reservation_id, "episode_id": episode_id,
            "amount_predicted": amount_predicted, "detail": detail,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            return dict(existing)
        try:
            conn.execute(
                "INSERT INTO reservations (reservation_id, episode_id, amount_predicted, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (reservation_id, episode_id,
                 _dump(amount_predicted) if amount_predicted is not None else None, detail, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"reservation {reservation_id!r} already exists with different payload")
    return {"reservation_id": reservation_id, "episode_id": episode_id, "status": "open"}


def release_reservation(
    store: Store, reservation_id: str, status: str = "released",
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Release or cancel a reservation; the ledger keeps the full history."""
    if status not in ("released", "cancelled", "consumed"):
        raise EpisodeError(f"unknown reservation status {status!r}")
    conn = store.conn
    with _write_txn(conn):
        row = conn.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        if row is None:
            raise EpisodeError(f"unknown reservation {reservation_id!r}")
        _record_event(conn, event_id or f"reservation:{reservation_id}:{status}", "reservation.close", {
            "reservation_id": reservation_id, "status": status,
        })
        conn.execute("UPDATE reservations SET status=? WHERE reservation_id=?", (status, reservation_id))
    updated = conn.execute(
        "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
    return dict(updated)


# --------------------------------------------------------------------------
# Launch registration (proposal separate from actual launch)


def register_launch(
    store: Store,
    run_id: str,
    episode_id: str,
    family: str,
    effort: str,
    route: str,
    harness_version: str,
    eligible_options: list[str],
    capability_exclusions: Optional[list[dict[str, str]]] = None,
    override_reason: str = "",
    release: Optional[str] = None,
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Register an actual launch; rejects unsupported settings before launch.

    Capability exclusions are decided before ranking and recorded here. A
    rejection raises CapabilityError and records nothing attributable to the
    model: it is not a failure and earns no cost or credit.

    ``release`` is the adapter-observed effective model id and is required:
    the store never synthesizes a release. Sol-family launches must carry
    exactly ``GPT-6.1``.

    One episode has one pre-outcome owner: its first (initial) launch fixes
    the owner option and owner generation (initial setting plus the
    episode's fixed recovery recipe), takes the next stable sequence in
    that owner cell, and carries the episode's cost and credit exactly
    once. Later launches in the same episode are fallbacks: their actual
    configuration is kept as diagnostics, but they take no sequence and
    earn no separate cell credit.
    """
    conn = store.conn
    episode = _episode_or_raise(conn, episode_id)
    execution_client = episode["execution_client"]
    ok, reason = check_capability(execution_client, family, effort, route)
    if not ok:
        # Committed on its own: the rejection is evidence, and raising inside
        # the launch transaction below would roll it back.
        with _write_txn(conn):
            _record_event(conn, event_id or f"launch-rejected:{run_id}", "launch.rejected", {
                "run_id": run_id, "episode_id": episode_id, "option": f"{family}/{effort}",
                "route": route, "reason": reason, "release": release,
            })
        raise CapabilityError(reason)
    if not release or not str(release).strip():
        raise EpisodeError("an actual launch must carry its adapter-observed effective release id")
    if family == "sol":
        if release in SOL_RETIRED_IDS:
            raise EpisodeError(
                f"release {release!r} is retired for Sol; current evidence requires"
                f" one of {sorted(SOL_CURRENT_IDS)}")
        if release not in SOL_CURRENT_IDS:
            raise EpisodeError(
                f"a sol-family launch must carry an observed current catalogue id"
                f" {sorted(SOL_CURRENT_IDS)}, not {release!r}")
    option = f"{family}/{effort}"
    descriptor = material_descriptor(_load(episode["context"]) or {},
                                     episode["material_band"] or "")
    generation = generation_key(release, route, episode["recipe_version"],
                                descriptor, harness_version)
    with _write_txn(conn):
        prior = conn.execute("SELECT * FROM launches WHERE run_id=?", (run_id,)).fetchone()
        if prior is not None:
            # Replay: reuse the stored order/fallback flags so the event
            # payload is identical; anything else differing is a conflict.
            # A missing event row with a matching launch row (e.g. a
            # migrated store) still recovers: the event is re-recorded
            # below and the stored row is returned.
            payload = {
                "run_id": run_id, "episode_id": episode_id,
                "option": option, "release": release, "route": route,
                "harness_version": harness_version, "eligible_options": eligible_options,
                "excluded": capability_exclusions or [], "override_reason": override_reason,
                "generation": generation, "candidate_seq": prior["candidate_seq"],
                "is_fallback": prior["is_fallback"],
            }
            fresh = _record_event(conn, event_id or f"launch:{run_id}", "launch", payload)
            if not fresh:
                return dict(prior)
            if (prior["episode_id"] == episode_id and prior["option"] == option
                    and prior["release"] == release and prior["route"] == route
                    and prior["harness_version"] == harness_version
                    and prior["eligibility"] == _dump(eligible_options)
                    and prior["excluded"] == _dump(capability_exclusions or [])
                    and (prior["override_reason"] or "") == override_reason
                    and prior["generation"] == generation):
                return dict(prior)
            raise ConflictError(f"run {run_id!r} already registered with different payload")
        first = conn.execute("SELECT 1 FROM launches WHERE episode_id=?",
                             (episode_id,)).fetchone() is None
        if first:
            counter = conn.execute(
                "SELECT next_seq FROM candidate_counters"
                " WHERE execution_client = ? AND task_type = ? AND option = ? AND generation = ?",
                (execution_client, episode["task_type"], option, generation)).fetchone()
            seq: Optional[int] = int(counter["next_seq"]) if counter is not None else 1
            is_fallback = 0
        else:
            counter = None
            seq = None
            is_fallback = 1
        payload = {
            "run_id": run_id, "episode_id": episode_id,
            "option": option, "release": release, "route": route,
            "harness_version": harness_version, "eligible_options": eligible_options,
            "excluded": capability_exclusions or [], "override_reason": override_reason,
            "generation": generation, "candidate_seq": seq, "is_fallback": is_fallback,
        }
        _record_event(conn, event_id or f"launch:{run_id}", "launch", payload)
        try:
            conn.execute(
                "INSERT INTO launches (run_id, episode_id, family, effort, option, release, route,"
                " harness_version, eligibility, excluded, override_reason, generation,"
                " candidate_seq, is_fallback, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, episode_id, family, effort, option, release,
                 route, harness_version, _dump(eligible_options),
                 _dump(capability_exclusions or []), override_reason, generation,
                 seq, is_fallback, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"run {run_id!r} already registered with different payload")
        if not first:
            # A later fallback launch invalidates any prior closure.
            _reopen(conn, episode_id)
        if first:
            assert seq is not None
            conn.execute("UPDATE episodes SET owner_option=?, owner_generation=?, owner_seq=?"
                         " WHERE episode_id=?", (option, generation, seq, episode_id))
            if counter is None:
                conn.execute(
                    "INSERT INTO candidate_counters"
                    " (execution_client, task_type, option, generation, next_seq)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (execution_client, episode["task_type"], option, generation, seq + 1))
            else:
                conn.execute(
                    "UPDATE candidate_counters SET next_seq = ?"
                    " WHERE execution_client = ? AND task_type = ? AND option = ?"
                    " AND generation = ?",
                    (seq + 1, execution_client, episode["task_type"], option, generation))
    row = conn.execute("SELECT * FROM launches WHERE run_id=?", (run_id,)).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# Usage segments (raw retained, cumulative via unique segments/deltas)


def record_usage(
    store: Store, segment_key: str, episode_id: str, raw: dict[str, Any],
    run_id: Optional[str] = None, kind: str = "run", purpose: str = "",
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record one unique usage segment; duplicates are idempotent no-ops.

    ``purpose="verification"`` marks the row as master-verification cost
    without requiring a second row: a shared verification allocation
    carries the same purpose instead of double-charging a separate row.
    """
    if purpose not in ("", "verification"):
        raise EpisodeError("usage purpose must be '' or 'verification'")
    norm = normalize_usage(raw)
    missing = 1 if norm["total"] is None else 0
    conn = store.conn
    with _write_txn(conn):
        _episode_or_raise(conn, episode_id)
        if run_id is not None and conn.execute(
                "SELECT 1 FROM launches WHERE run_id=? AND episode_id=?",
                (run_id, episode_id)).fetchone() is None:
            raise EpisodeError(f"run {run_id!r} does not belong to episode {episode_id!r}")
        fresh = _record_event(conn, event_id or f"usage:{segment_key}", "usage", {
            "segment_key": segment_key, "episode_id": episode_id, "run_id": run_id,
            "kind": kind, "purpose": purpose, "raw": raw,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
            return dict(existing)
        try:
            conn.execute(
                "INSERT INTO usage_segments (segment_key, run_id, episode_id, kind, raw,"
                " norm_input, norm_output, norm_total, purpose, missing, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (segment_key, run_id, episode_id, kind, _dump(raw),
                 _dump(norm["input"]), _dump(norm["output"]), _dump(norm["total"]),
                 purpose, missing, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"usage segment {segment_key!r} already recorded with different payload")
        conn.execute(
            "INSERT INTO spend_ledger (entry_id, episode_id, run_id, amount_tokens, kind, created_at)"
            " VALUES (?, ?, ?, ?, 'usage', ?)",
            (f"spend:{segment_key}", episode_id, run_id, _dump(norm["total"]), _now()),
        )
        _reopen(conn, episode_id)
    row = conn.execute("SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
    return dict(row)


def record_cumulative_delta(
    store: Store, segment_key: str, episode_id: str, cumulative: dict[str, Any],
    source_key: str, sequence: int, run_id: Optional[str] = None,
    initial_base: Optional[float] = None, event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record one snapshot of a cumulative provider counter; the store computes the delta.

    Thread-cumulative reports (resumed sessions, rolling token counters) must
    enter as stored snapshots keyed by (``source_key``, ``sequence``), never
    by a caller-supplied prior: the delta is recomputed transactionally
    against the persisted latest snapshot for that source, so a resumed base
    is never counted twice. A ``source_key`` is owned by the episode and run
    that first use it; snapshots under the same key for any other
    episode/run are rejected, so one counter can never straddle two books.
    A brand-new source for a resumed session may declare its
    ``initial_base`` once, explicitly and raw; every later delta is still
    store-computed.

    Late, duplicate-sequence, decreasing, or unknown snapshots are
    preserved raw as non-applied rows with ``missing=1``: they add no
    spend, keep the old known spend as the lower bound, and block
    inference and finalization until explicitly reconciled with
    ``resolve_cumulative_snapshot``. They are never silently sufficient
    evidence.
    """
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise EpisodeError("cumulative sequence must be a non-negative integer")
    if not source_key or not str(source_key).strip():
        raise EpisodeError("a cumulative snapshot must name its counter source")
    if initial_base is not None and (
            isinstance(initial_base, bool) or not isinstance(initial_base, (int, float))
            or not math.isfinite(float(initial_base)) or float(initial_base) < 0):
        raise EpisodeError("initial_base must be a finite non-negative number or None")
    norm = normalize_usage(cumulative)
    current = norm["total"]
    raw = dict(cumulative)
    raw["cumulative_snapshot_total"] = current
    conn = store.conn
    with _write_txn(conn):
        _episode_or_raise(conn, episode_id)
        if run_id is not None and conn.execute(
                "SELECT 1 FROM launches WHERE run_id=? AND episode_id=?",
                (run_id, episode_id)).fetchone() is None:
            raise EpisodeError(f"run {run_id!r} does not belong to episode {episode_id!r}")
        fresh = _record_event(conn, event_id or f"usage:{segment_key}", "usage.delta", {
            "segment_key": segment_key, "episode_id": episode_id, "run_id": run_id,
            "source_key": source_key, "sequence": sequence,
            "initial_base": initial_base, "raw": raw,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
            if existing is None:
                raise EpisodeError(f"cumulative snapshot {segment_key!r} replayed but its row is missing")
            return dict(existing)
        source = conn.execute("SELECT * FROM cumulative_sources WHERE source_key=?",
                              (source_key,)).fetchone()
        if source is not None and (
                source["episode_id"] != episode_id or (source["run_id"] or None) != run_id):
            raise EpisodeError(
                f"cumulative source {source_key!r} is owned by episode"
                f" {source['episode_id']!r} run {source['run_id']!r}; it cannot back"
                f" episode {episode_id!r} run {run_id!r}")
        if source is not None and initial_base is not None:
            raise EpisodeError("initial_base is only declared on a source's first snapshot")
        last_total = _load(source["last_total"]) if source is not None else None
        if current is None:
            kind, delta, applied, advance = "run-delta-unknown", None, 0, False
        elif source is None:
            base = float(initial_base) if initial_base is not None else 0.0
            raw["initial_base"] = base
            if current < base and not _close_enough(current, base):
                kind, delta, applied, advance = "run-delta-nonmonotonic", None, 0, False
            else:
                kind, delta, applied, advance = "run-delta", current - base, 1, True
        elif last_total is None:
            # The stored base was unknown, so this snapshot cannot yield a
            # delta; it becomes the new base for later snapshots instead.
            kind, delta, applied, advance = "run-delta-unknown-base", None, 0, True
        elif sequence <= int(source["last_sequence"]):
            kind, delta, applied, advance = "run-delta-late", None, 0, False
        elif current < float(last_total) and not _close_enough(current, float(last_total)):
            kind, delta, applied, advance = "run-delta-nonmonotonic", None, 0, False
        else:
            kind = "run-delta"
            delta = current - float(last_total)
            applied, advance = 1, True
        raw["applied_delta"] = delta
        missing = 0 if (applied and delta is not None) else 1
        if source is None:
            conn.execute(
                "INSERT INTO cumulative_sources (source_key, episode_id, run_id,"
                " last_sequence, last_total, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (source_key, episode_id, run_id, sequence, _dump(current), _now()))
        elif advance:
            conn.execute("UPDATE cumulative_sources SET last_sequence=?, last_total=?"
                         " WHERE source_key=?",
                         (sequence, _dump(current), source_key))
        try:
            conn.execute(
                "INSERT INTO usage_segments (segment_key, run_id, episode_id, kind, raw,"
                " norm_input, norm_output, norm_total, purpose, missing, created_at)"
                " VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, '', ?, ?)",
                (segment_key, run_id, episode_id, kind, _dump(raw),
                 _dump(delta), missing, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"usage segment {segment_key!r} already recorded with different payload")
        try:
            conn.execute(
                "INSERT INTO cumulative_snapshots (segment_key, source_key, sequence, raw,"
                " delta, applied, resolved, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (segment_key, source_key, sequence, _dump(raw), _dump(delta), applied, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"cumulative snapshot {segment_key!r} already recorded with different payload")
        if applied and delta is not None:
            conn.execute(
                "INSERT INTO spend_ledger (entry_id, episode_id, run_id, amount_tokens, kind, created_at)"
                " VALUES (?, ?, ?, ?, 'usage', ?)",
                (f"spend:{segment_key}", episode_id, run_id, _dump(delta), _now()),
            )
        _reopen(conn, episode_id)
    row = conn.execute("SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
    return dict(row)


def resolve_cumulative_snapshot(
    store: Store, segment_key: str, resolution: str, resolved_by: str,
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Reconcile one non-applied cumulative snapshot so it stops blocking readiness.

    ``superseded`` drops the block for stale information (a late duplicate
    the counter already advanced past); the raw row stays. ``adopt``
    promotes the snapshot to the source's new base (a genuine counter
    reset epoch) and is only allowed for a known total at a sequence past
    the stored one; later deltas then compute from the adopted base. Any
    other state is rejected, and already-applied snapshots need no
    resolution.
    """
    if resolution not in ("superseded", "adopt"):
        raise EpisodeError("cumulative resolution must be 'superseded' or 'adopt'")
    if not resolved_by or not str(resolved_by).strip():
        raise EpisodeError("snapshot resolution must name who reconciled it")
    conn = store.conn
    with _write_txn(conn):
        snap = conn.execute("SELECT * FROM cumulative_snapshots WHERE segment_key=?",
                            (segment_key,)).fetchone()
        if snap is None:
            raise EpisodeError(f"unknown cumulative snapshot {segment_key!r}")
        if int(snap["applied"]):
            raise EpisodeError(f"snapshot {segment_key!r} already applied; nothing to resolve")
        if int(snap["resolved"]):
            existing = conn.execute(
                "SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
            return dict(existing)
        source = conn.execute("SELECT * FROM cumulative_sources WHERE source_key=?",
                              (snap["source_key"],)).fetchone()
        total = (_load(snap["raw"]) or {}).get("cumulative_snapshot_total")
        _record_event(conn, event_id or f"snapshot-resolve:{segment_key}", "snapshot.resolve", {
            "segment_key": segment_key, "resolution": resolution, "resolved_by": resolved_by,
        })
        if resolution == "adopt":
            if total is None:
                raise EpisodeError(f"snapshot {segment_key!r} has unknown total and cannot be adopted")
            if source is not None and int(snap["sequence"]) <= int(source["last_sequence"]):
                raise EpisodeError(
                    f"snapshot {segment_key!r} predates the stored base and cannot be adopted;"
                    " mark it superseded instead")
            if source is None:
                conn.execute(
                    "INSERT INTO cumulative_sources (source_key, episode_id, run_id,"
                    " last_sequence, last_total, updated_at)"
                    " SELECT source_key, episode_id, run_id, sequence, ?, ?"
                    " FROM usage_segments WHERE segment_key=?",
                    (_dump(total), _now(), segment_key))
            else:
                conn.execute("UPDATE cumulative_sources SET last_sequence=?, last_total=?,"
                             " updated_at=? WHERE source_key=?",
                             (int(snap["sequence"]), _dump(total), _now(), snap["source_key"]))
        conn.execute("UPDATE cumulative_snapshots SET resolved=1 WHERE segment_key=?",
                     (segment_key,))
        conn.execute("UPDATE usage_segments SET missing=0 WHERE segment_key=?", (segment_key,))
    row = conn.execute("SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
    return dict(row)


def _window_of(raw: dict[str, Any], source_hash: Optional[str],
               start_offset: Optional[int],
               end_offset: Optional[int]) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Extract and validate a measured-window identity from explicit args or raw fields.

    Adapters may pass Codex measured-review windows either as explicit
    arguments or inside the raw record (``source``/``source_hash`` with
    ``start_offset``/``end_offset``). A half-declared window is rejected:
    where present, all three fields are required, with integer offsets
    satisfying ``0 <= start < end``.
    """
    if source_hash is None and start_offset is None and end_offset is None and isinstance(raw, dict):
        source_hash = raw.get("source_hash", raw.get("source"))
        start_offset = raw.get("start_offset")
        end_offset = raw.get("end_offset")
    if source_hash is None and start_offset is None and end_offset is None:
        return None, None, None
    if not source_hash or not str(source_hash).strip():
        raise EpisodeError("a measured window must name its source hash")
    for name, value in (("start_offset", start_offset), ("end_offset", end_offset)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EpisodeError(f"window {name} must be a non-negative integer")
    assert start_offset is not None and end_offset is not None
    if start_offset >= end_offset:
        raise EpisodeError("window start_offset must be below end_offset")
    return str(source_hash), start_offset, end_offset


def record_master_segment(
    store: Store, master_segment_key: str, master_client: str, raw: dict[str, Any],
    source_hash: Optional[str] = None, start_offset: Optional[int] = None,
    end_offset: Optional[int] = None, event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record one shared master turn once; episodes reference it by allocation.

    Measured windows (Codex cumulative-token review windows) carry their
    source hash and [start, end) offsets, explicitly or inside ``raw``.
    Distinct windows overlapping on one source are rejected even when
    their segment ids differ, so overlapping measurements can never both
    be charged; exact idempotent replay of the same segment still
    succeeds. Adjacent windows on one source, and identical windows on
    different sources, are accepted.
    """
    norm = normalize_usage(raw)
    window = _window_of(raw, source_hash, start_offset, end_offset)
    conn = store.conn
    with _write_txn(conn):
        fresh = _record_event(conn, event_id or f"master-segment:{master_segment_key}",
                              "master.segment", {
                                  "master_segment_key": master_segment_key,
                                  "master_client": master_client, "raw": raw,
                                  "window": window})
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM master_segments WHERE master_segment_key=?",
                (master_segment_key,)).fetchone()
            if existing is None:
                raise EpisodeError(
                    f"master segment {master_segment_key!r} replayed but its row is missing")
            return dict(existing)
        source, start, end = window
        if source is not None:
            assert start is not None and end is not None
            clash = conn.execute(
                "SELECT master_segment_key FROM master_segments WHERE source_hash=?"
                " AND start_offset IS NOT NULL"
                " AND NOT (end_offset <= ? OR start_offset >= ?)",
                (source, start, end)).fetchone()
            if clash is not None:
                raise EpisodeError(
                    f"window [{start}, {end}) on source {source!r} overlaps already"
                    f" recorded segment {clash['master_segment_key']!r}; overlapping"
                    " windows must not both be charged")
        try:
            conn.execute(
                "INSERT INTO master_segments (master_segment_key, master_client, raw, norm_total,"
                " source_hash, start_offset, end_offset, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (master_segment_key, master_client, _dump(raw), _dump(norm["total"]),
                 source, start, end, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(
                f"master segment {master_segment_key!r} already recorded with different payload")
    row = conn.execute("SELECT * FROM master_segments WHERE master_segment_key=?",
                       (master_segment_key,)).fetchone()
    return dict(row)


def allocate_shared(
    store: Store, segment_key: str, episode_id: str, master_segment_key: str,
    weight: float, purpose: str = "", event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Allocate part of a shared master segment to one episode.

    Allocation weights across all episodes for one master segment must sum
    to at most 1, so a shared turn is never counted in multiple episodes.
    ``purpose="verification"`` lets a shared verification turn satisfy the
    episode's declared verification cost with this single row: no separate
    verification row is charged alongside it.
    """
    if purpose not in ("", "verification"):
        raise EpisodeError("allocation purpose must be '' or 'verification'")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
            or not math.isfinite(float(weight)) or not 0.0 <= float(weight) <= 1.0:
        raise EpisodeError("allocation weight must be a finite number in [0, 1]")
    weight = float(weight)
    conn = store.conn
    with _write_txn(conn):
        _episode_or_raise(conn, episode_id)
        master = conn.execute("SELECT * FROM master_segments WHERE master_segment_key=?",
                              (master_segment_key,)).fetchone()
        if master is None:
            raise EpisodeError(f"unknown master segment {master_segment_key!r}")
        # Replay check precedes the cap check: re-recording an existing
        # allocation must succeed even when the cap is already full.
        fresh = _record_event(conn, event_id or f"alloc:{segment_key}", "usage.alloc", {
            "segment_key": segment_key, "episode_id": episode_id,
            "master_segment_key": master_segment_key, "weight": weight,
            "purpose": purpose,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
            if existing is None:
                raise EpisodeError(f"allocation {segment_key!r} replayed but its row is missing")
            return dict(existing)
        used = conn.execute(
            "SELECT COALESCE(SUM(alloc_weight), 0) AS used FROM usage_segments"
            " WHERE master_segment_key=? AND kind='master-alloc'",
            (master_segment_key,)).fetchone()["used"]
        if float(used) + weight > 1.0 + 1e-9:
            raise EpisodeError(
                f"allocations for {master_segment_key!r} would total {float(used) + weight} > 1")
        master_total = _load(master["norm_total"])
        share = None if master_total is None else float(master_total) * weight
        raw = {"master_segment": master_segment_key, "weight": weight}
        try:
            conn.execute(
                "INSERT INTO usage_segments (segment_key, run_id, episode_id, kind, raw,"
                " norm_input, norm_output, norm_total, alloc_weight, master_segment_key,"
                " purpose, missing, created_at)"
                " VALUES (?, NULL, ?, 'master-alloc', ?, NULL, NULL, ?, ?, ?, ?, ?, ?)",
                (segment_key, episode_id, _dump(raw), _dump(share), weight, master_segment_key,
                 purpose, 1 if share is None else 0, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"usage segment {segment_key!r} already recorded with different payload")
        conn.execute(
            "INSERT INTO spend_ledger (entry_id, episode_id, run_id, amount_tokens, kind, created_at)"
            " VALUES (?, ?, NULL, ?, 'master-alloc', ?)",
            (f"spend:{segment_key}", episode_id, _dump(share), _now()),
        )
        _reopen(conn, episode_id)
    row = conn.execute("SELECT * FROM usage_segments WHERE segment_key=?", (segment_key,)).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# Attempt completion vs master acceptance


def record_attempt(
    store: Store, attempt_id: str, run_id: str, status: str, detail: str = "",
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record an attempt ending; distinct from master acceptance."""
    if status not in ATTEMPT_ENDS:
        raise EpisodeError(f"unknown attempt status {status!r}")
    conn = store.conn
    with _write_txn(conn):
        launch = conn.execute("SELECT * FROM launches WHERE run_id=?", (run_id,)).fetchone()
        if launch is None:
            raise EpisodeError(f"unknown run {run_id!r}")
        fresh = _record_event(conn, event_id or f"attempt:{attempt_id}", "attempt", {
            "attempt_id": attempt_id, "run_id": run_id, "status": status, "detail": detail,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            return dict(existing)
        try:
            conn.execute(
                "INSERT INTO attempts (attempt_id, run_id, episode_id, status, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (attempt_id, run_id, launch["episode_id"], status, detail, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"attempt {attempt_id!r} already recorded with different payload")
        if status in ("interrupted", "cancelled"):
            conn.execute("UPDATE launches SET status=? WHERE run_id=?", (status, run_id))
        else:
            conn.execute("UPDATE launches SET status='completed' WHERE run_id=?", (run_id,))
        _reopen(conn, launch["episode_id"])
    row = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    return dict(row)


def _live_acceptances(conn: sqlite3.Connection, episode_id: str) -> list[sqlite3.Row]:
    """Acceptances no later correction supersedes (the chain tips)."""
    return conn.execute(
        "SELECT * FROM acceptances WHERE episode_id=? AND accept_id NOT IN"
        " (SELECT correction_of FROM acceptances WHERE episode_id=? AND correction_of IS NOT NULL)",
        (episode_id, episode_id)).fetchall()


def record_acceptance(
    store: Store, accept_id: str, episode_id: str, verdict: str, accepted_work: float,
    verifier: str, worker_ref: str, run_id: Optional[str] = None,
    correction_of: Optional[str] = None, milestones: Optional[list[str]] = None,
    verification_ref: str = "",
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record the master's verdict. A worker's own verdict is never acceptance.

    Credit must name predeclared milestones: ``milestones`` lists the episode
    milestones this verdict credits, and ``accepted_work`` must equal their
    predeclared sum, so the same milestone can never be counted twice across
    live acceptances. Corrections cite the acceptance they supersede and form
    a single chain: a superseded acceptance cannot be corrected again (no
    forks). Incurred cost and accepted credit survive interruption: this row
    never deletes usage or spend rows.
    """
    if verdict not in VERDICTS:
        raise EpisodeError(f"unknown verdict {verdict!r}")
    if isinstance(accepted_work, bool) or not isinstance(accepted_work, (int, float)) \
            or not math.isfinite(float(accepted_work)) or float(accepted_work) < 0:
        raise EpisodeError("accepted work must be a finite non-negative number")
    accepted_work = float(accepted_work)
    if not verifier or verifier == worker_ref:
        raise EpisodeError("acceptance requires a verifier distinct from the worker")
    if verdict in ("fail", "unknown") and accepted_work != 0:
        raise EpisodeError(f"a {verdict} verdict accepts no work credit")
    milestones = list(milestones or [])
    if verdict in ("pass", "partial") and not milestones:
        raise EpisodeError(f"a {verdict} verdict must name the predeclared milestones it credits")
    if any(not m or not str(m).strip() for m in milestones):
        raise EpisodeError("credited milestones must be non-empty names")
    if len(set(milestones)) != len(milestones):
        raise EpisodeError("an acceptance must not name the same milestone twice")
    if verdict in ("fail", "unknown") and milestones:
        raise EpisodeError(f"a {verdict} verdict credits no milestones")
    conn = store.conn
    with _write_txn(conn):
        episode = _episode_or_raise(conn, episode_id)
        if run_id is not None and conn.execute(
                "SELECT 1 FROM launches WHERE run_id=? AND episode_id=?",
                (run_id, episode_id)).fetchone() is None:
            raise EpisodeError(f"run {run_id!r} does not belong to episode {episode_id!r}")
        # Replay check precedes the cap checks: re-recording a full
        # acceptance must succeed instead of tripping its own ceiling.
        fresh = _record_event(conn, event_id or f"accept:{accept_id}", "acceptance", {
            "accept_id": accept_id, "episode_id": episode_id, "run_id": run_id,
            "verdict": verdict, "accepted_work": accepted_work, "verifier": verifier,
            "worker_ref": worker_ref, "correction_of": correction_of,
            "milestones": milestones, "verification_ref": verification_ref,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM acceptances WHERE accept_id=?", (accept_id,)).fetchone()
            if existing is None:
                raise EpisodeError(f"acceptance {accept_id!r} replayed but its row is missing")
            return dict(existing)
        declared = {_credit_name(c): float(c.get("credit", 0))
                    for c in (_load(episode["work_credits"]) or [])}
        for name in milestones:
            if name not in declared:
                raise EpisodeError(f"milestone {name!r} was not predeclared for episode {episode_id!r}")
        named_total = sum(declared[name] for name in milestones)
        if not _close_enough(accepted_work, named_total):
            raise EpisodeError(
                f"accepted work {accepted_work} does not equal the predeclared sum "
                f"{named_total} of milestones {milestones}")
        prior = 0.0
        if correction_of is not None:
            cited = conn.execute("SELECT * FROM acceptances WHERE accept_id=?",
                                 (correction_of,)).fetchone()
            if cited is None:
                raise EpisodeError(f"unknown acceptance {correction_of!r}")
            if cited["episode_id"] != episode_id:
                raise EpisodeError("a correction must stay in its own episode")
            fork = conn.execute("SELECT accept_id FROM acceptances WHERE correction_of=?",
                                (correction_of,)).fetchone()
            if fork is not None:
                raise EpisodeError(
                    f"acceptance {correction_of!r} already has successor {fork['accept_id']!r};"
                    " corrections form a single chain and cannot fork")
            prior = float(cited["accepted_work"])
        live = _live_acceptances(conn, episode_id)
        live_milestones = {m for row in live for m in (_load(row["milestones"]) or [])}
        if correction_of is not None:
            live_milestones -= set(_load(conn.execute(
                "SELECT milestones FROM acceptances WHERE accept_id=?",
                (correction_of,)).fetchone()["milestones"]) or [])
        for name in milestones:
            if name in live_milestones:
                raise EpisodeError(
                    f"milestone {name!r} is already credited by a live acceptance")
        net_accepted = (sum(float(r["accepted_work"]) for r in live) - prior + accepted_work)
        if net_accepted > float(episode["work_total"]) + 1e-9:
            raise EpisodeError(
                f"net accepted work {net_accepted} exceeds predeclared {episode['work_total']}")
        try:
            conn.execute(
                "INSERT INTO acceptances (accept_id, episode_id, run_id, verdict, accepted_work,"
                " verifier, worker_ref, correction_of, milestones, verification_ref, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (accept_id, episode_id, run_id, verdict, accepted_work, verifier,
                 worker_ref, correction_of, _dump(milestones), verification_ref, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"acceptance {accept_id!r} already recorded with different payload")
        _reopen(conn, episode_id)
    row = conn.execute("SELECT * FROM acceptances WHERE accept_id=?", (accept_id,)).fetchone()
    return dict(row)


def total_accepted_work(conn: sqlite3.Connection, episode_id: str) -> float:
    """Net accepted work: only chain tips count, superseded rows do not."""
    return sum(float(r["accepted_work"]) for r in _live_acceptances(conn, episode_id))


def mark_run_usage_final(
    store: Store, run_id: str, event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record the adapter's statement that one run's usage is final and known.

    The mark requires at least one known-total usage row for the run and
    binds to the run's current usage-row count: any later usage row for
    the run invalidates the mark (finalize compares counts), and replaying
    the old mark event after new usage is a no-op that leaves the stale
    count in place, never a fresh validation. The default event id
    carries the count, so each revision marks under a new identity;
    callers reusing an explicit id across revisions get a conflict, not a
    silent pass. Marking final usage never reopens anything: it completes
    evidence rather than invalidating it.
    """
    conn = store.conn
    with _write_txn(conn):
        launch = conn.execute("SELECT * FROM launches WHERE run_id=?", (run_id,)).fetchone()
        if launch is None:
            raise EpisodeError(f"unknown run {run_id!r}")
        count = conn.execute("SELECT COUNT(*) AS n FROM usage_segments WHERE run_id=?",
                             (run_id,)).fetchone()["n"]
        fresh = _record_event(conn, event_id or f"run-usage-final:{run_id}:{count}",
                              "run.usage_final", {"run_id": run_id, "usage_rows": count})
        if not fresh:
            return dict(conn.execute("SELECT * FROM launches WHERE run_id=?",
                                     (run_id,)).fetchone())
        known = conn.execute("SELECT 1 FROM usage_segments WHERE run_id=? AND missing=0",
                             (run_id,)).fetchone()
        if known is None:
            raise EpisodeError(
                f"run {run_id!r} has no known-total usage row; final usage cannot be marked")
        conn.execute("UPDATE launches SET usage_final_count=? WHERE run_id=?", (count, run_id))
    row = conn.execute("SELECT * FROM launches WHERE run_id=?", (run_id,)).fetchone()
    return dict(row)


def _verification_known(conn: sqlite3.Connection, episode_id: str) -> bool:
    """Known verification cost: a non-missing verification row or known spend."""
    row = conn.execute(
        "SELECT 1 FROM usage_segments WHERE episode_id=? AND missing=0"
        " AND (kind='verification' OR purpose='verification')", (episode_id,)).fetchone()
    if row is not None:
        return True
    spend = conn.execute("SELECT 1 FROM spend_ledger WHERE episode_id=? AND kind='verification'"
                         " AND amount_tokens != 'null'", (episode_id,)).fetchone()
    return spend is not None


def finalize_episode(
    store: Store, episode_id: str, closed_by: str, closure_ref: str = "",
    usage_complete: bool = True, event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Master closure of an episode: the explicit usage-complete marker.

    The flag alone never suffices: finalization validates records, not
    assertions. Every linked run must carry the adapter's final-usage
    mark plus at least one known-total usage row (a run with only unknown
    rows, or a fallback with no rows at all, blocks closure); declared
    verification cost must be present and known (a shared verification
    allocation satisfies this with its single row, no second row is
    charged); no unknown usage row, unresolved snapshot, or unknown
    spend entry may remain; and at least one master acceptance must
    exist. Only finalized episodes are inference-ready; partial cost
    stays visible in accounting throughout, finalized or not. The replay
    check runs before every other check, so re-closing an already closed
    episode with the identical record is a no-op, while a second
    distinct closure is rejected.
    """
    if not closed_by or not str(closed_by).strip():
        raise EpisodeError("episode closure must name the closing master")
    if not isinstance(usage_complete, bool):
        raise EpisodeError("usage_complete must be a bool")
    conn = store.conn
    with _write_txn(conn):
        episode = _episode_or_raise(conn, episode_id)
        revision = _evidence_fingerprint(conn, episode_id)
        try:
            fresh = _record_event(
                conn, event_id or f"finalize:{episode_id}:{revision}", "episode.finalize", {
                    "episode_id": episode_id, "closed_by": closed_by,
                    "closure_ref": closure_ref, "usage_complete": usage_complete,
                    "evidence_revision": revision,
                })
        except ConflictError:
            # A caller reusing one explicit closure id across evidence
            # revisions collides with the earlier closure's event: that is
            # a stale replay, never a new validation.
            if episode["status"] == "closed":
                raise EpisodeError(f"episode {episode_id!r} is already closed")
            raise ConflictError(
                f"episode {episode_id!r}: closure id already used for older evidence;"
                " close the new revision under a fresh event id")
        if not fresh:
            if episode["status"] != "closed":
                raise EpisodeError(
                    f"episode {episode_id!r}: replayed closure predates new evidence;"
                    " it validates nothing and closes nothing")
            return get_episode(store, episode_id)
        if episode["status"] == "closed":
            raise EpisodeError(f"episode {episode_id!r} is already closed")
        if not usage_complete:
            raise EpisodeError(
                f"episode {episode_id!r} cannot finalize with usage_complete=false;"
                " partial cost remains in accounting, but inference needs final usage")
        runs = conn.execute("SELECT * FROM launches WHERE episode_id=?",
                            (episode_id,)).fetchall()
        problems: list[str] = []
        for run in runs:
            attempt = conn.execute("SELECT 1 FROM attempts WHERE run_id=?",
                                   (run["run_id"],)).fetchone()
            if attempt is None:
                problems.append(f"run {run['run_id']!r} lacks a terminal attempt")
            current_rows = conn.execute("SELECT COUNT(*) AS n FROM usage_segments"
                                        " WHERE run_id=?", (run["run_id"],)).fetchone()["n"]
            marked = run["usage_final_count"]
            if marked is None:
                problems.append(f"run {run['run_id']!r} lacks adapter-marked final usage")
            elif int(marked) != int(current_rows):
                problems.append(
                    f"run {run['run_id']!r} has new usage since its final mark"
                    f" ({marked} marked vs {current_rows} present); re-mark before closing")
            known = conn.execute("SELECT 1 FROM usage_segments WHERE run_id=?"
                                 " AND missing=0", (run["run_id"],)).fetchone()
            if known is None:
                problems.append(f"run {run['run_id']!r} has no known-total usage row")
        if problems:
            raise EpisodeError(f"episode {episode_id!r} cannot finalize: "
                               + "; ".join(sorted(problems)))
        if int(episode["verification_required"]) and not _verification_known(conn, episode_id):
            raise EpisodeError(
                f"episode {episode_id!r} declares verification but records no known"
                " verification cost")
        leftover = conn.execute("SELECT COUNT(*) AS n FROM usage_segments"
                                " WHERE episode_id=? AND missing=1", (episode_id,)).fetchone()["n"]
        pending_snaps = conn.execute(
            "SELECT COUNT(*) AS n FROM cumulative_snapshots s JOIN usage_segments u"
            " ON u.segment_key = s.segment_key"
            " WHERE u.episode_id=? AND s.applied=0 AND s.resolved=0",
            (episode_id,)).fetchone()["n"]
        unknown_spend = conn.execute("SELECT COUNT(*) AS n FROM spend_ledger"
                                     " WHERE episode_id=? AND amount_tokens='null'",
                                     (episode_id,)).fetchone()["n"]
        if leftover or pending_snaps or unknown_spend:
            raise EpisodeError(
                f"episode {episode_id!r} cannot finalize with {leftover} unknown usage rows,"
                f" {pending_snaps} unreconciled snapshots, and {unknown_spend} unknown spend entries")
        if conn.execute("SELECT 1 FROM acceptances WHERE episode_id=?",
                        (episode_id,)).fetchone() is None:
            raise EpisodeError(f"episode {episode_id!r} cannot finalize without a master acceptance")
        conn.execute("UPDATE episodes SET status='closed', usage_complete=1,"
                     " closed_by=?, closure_ref=? WHERE episode_id=?",
                     (closed_by, closure_ref, episode_id))
    return get_episode(store, episode_id)


# --------------------------------------------------------------------------
# Combined launch-registration/acceptance interface


def register_launch_and_acceptance(
    store: Store, episode_id: str, run_id: str, family: str, effort: str, route: str,
    harness_version: str, eligible_options: list[str], release: str,
    verifier: str, worker_ref: str, verdict: str, accepted_work: float,
    milestones: Optional[list[str]] = None, usage: Optional[dict[str, Any]] = None,
    verification_usage: Optional[dict[str, Any]] = None,
    capability_exclusions: Optional[list[dict[str, str]]] = None,
    override_reason: str = "", verification_ref: str = "",
    accept_id: Optional[str] = None, event_prefix: str = "",
) -> dict[str, Any]:
    """One supported call for native adapters: launch + attempt + usage + acceptance.

    Brokers with streaming usage should prefer the granular calls; this
    wrapper exists so a native route without per-turn hooks still produces
    complete joined evidence from a single integration point. ``release``
    is the adapter-observed effective model id. Pass ``verification_usage``
    (recorded under kind ``verification``) when the episode declares
    verification; without it the episode cannot finalize.
    """
    prefix = event_prefix or run_id
    launch = register_launch(
        store, run_id, episode_id, family, effort, route, harness_version,
        eligible_options, capability_exclusions, override_reason, release,
        event_id=f"{prefix}:launch")
    record_attempt(store, f"{prefix}:attempt", run_id, "completed",
                   event_id=f"{prefix}:attempt:event")
    if usage is not None:
        record_usage(store, f"{prefix}:usage", episode_id, usage, run_id=run_id,
                     event_id=f"{prefix}:usage:event")
    if verification_usage is not None:
        record_usage(store, f"{prefix}:verification", episode_id, verification_usage,
                     run_id=run_id, kind="verification",
                     event_id=f"{prefix}:verification:event")
    acceptance = record_acceptance(
        store, accept_id or f"{prefix}:accept", episode_id, verdict, accepted_work,
        verifier, worker_ref, run_id=run_id, milestones=milestones,
        verification_ref=verification_ref, event_id=f"{prefix}:accept:event")
    return {"launch": launch, "acceptance": acceptance}


# --------------------------------------------------------------------------
# Ledger, views, reconciliation


def record_spend(
    store: Store, entry_id: str, episode_id: str, amount_tokens: Optional[float],
    kind: str, run_id: Optional[str] = None, event_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append to the uncapped observed-spend ledger (retry/fallback/verification stay here)."""
    if isinstance(amount_tokens, bool) or (
            amount_tokens is not None
            and (not isinstance(amount_tokens, (int, float))
                 or not math.isfinite(float(amount_tokens)) or float(amount_tokens) < 0)):
        raise EpisodeError("spend amounts must be finite non-negative numbers or None (unknown)")
    if not kind or not str(kind).strip():
        raise EpisodeError("a spend entry must name its kind")
    conn = store.conn
    with _write_txn(conn):
        _episode_or_raise(conn, episode_id)
        if run_id is not None and conn.execute(
                "SELECT 1 FROM launches WHERE run_id=? AND episode_id=?",
                (run_id, episode_id)).fetchone() is None:
            raise EpisodeError(f"run {run_id!r} does not belong to episode {episode_id!r}")
        fresh = _record_event(conn, event_id or f"spend-direct:{entry_id}", "spend", {
            "entry_id": entry_id, "episode_id": episode_id, "run_id": run_id,
            "amount_tokens": amount_tokens, "kind": kind,
        })
        if not fresh:
            existing = conn.execute(
                "SELECT * FROM spend_ledger WHERE entry_id=?", (entry_id,)).fetchone()
            return dict(existing)
        try:
            conn.execute(
                "INSERT INTO spend_ledger (entry_id, episode_id, run_id, amount_tokens, kind, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, episode_id, run_id, _dump(amount_tokens), kind, _now()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"spend entry {entry_id!r} already recorded with different payload")
        _reopen(conn, episode_id)
    row = conn.execute("SELECT * FROM spend_ledger WHERE entry_id=?", (entry_id,)).fetchone()
    return dict(row)


def episode_accounting(store: Store, episode_id: str) -> dict[str, Any]:
    """Work, cost, and quality accounting for one episode (retry/fallback included)."""
    conn = store.conn
    _episode_or_raise(conn, episode_id)
    usage = conn.execute(
        "SELECT norm_total, missing, kind FROM usage_segments WHERE episode_id=?", (episode_id,)).fetchall()
    tokens: Optional[float] = 0.0
    missing = 0
    for row in usage:
        # Missing is flag-driven, not null-driven: a resolved base marker
        # carries no total yet blocks nothing, while every unknown row
        # keeps missing=1 until reconciled.
        if int(row["missing"]):
            missing += 1
            continue
        value = _load(row["norm_total"])
        assert tokens is not None
        tokens += float(value) if value is not None else 0.0
    ledger = conn.execute(
        "SELECT amount_tokens, kind FROM spend_ledger WHERE episode_id=?", (episode_id,)).fetchall()
    spend: Optional[float] = 0.0
    spend_missing = 0
    for row in ledger:
        value = _load(row["amount_tokens"])
        if value is None:
            spend_missing += 1
        else:
            assert spend is not None
            spend += float(value)
    acceptances = conn.execute(
        "SELECT verdict FROM acceptances WHERE episode_id=?", (episode_id,)).fetchall()
    attempts = conn.execute(
        "SELECT status FROM attempts WHERE episode_id=?", (episode_id,)).fetchall()
    reservations = conn.execute(
        "SELECT status FROM reservations WHERE episode_id=?", (episode_id,)).fetchall()
    return {
        "episode_id": episode_id,
        "accepted_work": total_accepted_work(conn, episode_id),
        "usage_tokens": tokens,
        "usage_missing_segments": missing,
        "spend_uncapped_tokens": spend,
        "spend_missing_entries": spend_missing,
        "verdicts": [r["verdict"] for r in acceptances],
        "attempts": [r["status"] for r in attempts],
        "reservations": [r["status"] for r in reservations],
        "pending": not acceptances,
    }


def cell_summary(
    store: Store, execution_client: str, task_type: Optional[str] = None,
    generation: Optional[str] = None, include_archived: bool = False,
) -> dict[str, dict[str, Any]]:
    """Cumulative per-cell episode work/cost/quality/accounting views.

    Cells key on (execution_client, task_type, owner option, owner
    generation): the selected execution client's policy only. Each episode
    is attributed exactly once, to its pre-outcome owner cell (initial
    setting plus fixed recovery recipe); fallback launches keep their
    actual-configuration diagnostics on the launch rows and are counted in
    ``fallback_runs``, but their cost and credit stay in the owner's single
    accounting. No confidence arithmetic here.
    """
    conn = store.conn
    episode_filter = "e.execution_client = ?"
    episode_args: list[Any] = [execution_client]
    if task_type is not None:
        episode_filter += " AND e.task_type = ?"
        episode_args.append(task_type)
    if not include_archived:
        episode_filter += " AND e.archived = 0"
    episodes = conn.execute(
        f"SELECT * FROM episodes e WHERE {episode_filter}", episode_args).fetchall()
    launches = conn.execute(
        "SELECT l.episode_id, l.run_id, l.is_fallback FROM launches l"
        " JOIN episodes e ON e.episode_id = l.episode_id"
        f" WHERE {episode_filter}", episode_args).fetchall()
    cells: dict[str, dict[str, Any]] = {}

    def cell_for(task: str, option: Optional[str], gen: str) -> dict[str, Any]:
        return cells.setdefault(f"{task} × {option or 'unlaunched'} @ {gen}", {
            "episodes": 0, "runs": 0, "fallback_runs": 0, "verified_work": 0.0,
            "tokens": 0.0, "missing_usage": 0, "pending": 0, "censored": 0,
            "pass": 0, "partial": 0, "fail": 0, "unknown": 0,
            "spend_uncapped": 0.0, "reservations_open": 0,
        })

    owner_of = {e["episode_id"]: e for e in episodes}
    for launch in launches:
        owner = owner_of.get(launch["episode_id"])
        if owner is None or owner["owner_option"] is None:
            continue
        if generation is not None and owner["owner_generation"] != generation:
            continue
        cell = cell_for(owner["task_type"], owner["owner_option"], owner["owner_generation"])
        if int(launch["is_fallback"]):
            cell["fallback_runs"] += 1
        else:
            cell["runs"] += 1
    for episode in episodes:
        if episode["owner_option"] is None:
            if generation is not None:
                continue
            cell = cell_for(episode["task_type"], None, episode["generation"])
        else:
            if generation is not None and episode["owner_generation"] != generation:
                continue
            cell = cell_for(episode["task_type"], episode["owner_option"],
                            episode["owner_generation"])
        accounting = episode_accounting(store, episode["episode_id"])
        cell["episodes"] += 1
        cell["verified_work"] += accounting["accepted_work"]
        cell["tokens"] += accounting["usage_tokens"] or 0.0
        cell["missing_usage"] += accounting["usage_missing_segments"]
        cell["spend_uncapped"] += accounting["spend_uncapped_tokens"] or 0.0
        if accounting["pending"]:
            cell["pending"] += 1
        for verdict in accounting["verdicts"]:
            cell[verdict] += 1
        if any(s in ("interrupted", "cancelled") for s in accounting["attempts"]):
            cell["censored"] += 1
        cell["reservations_open"] += sum(1 for s in accounting["reservations"] if s == "open")
    return cells


def cell_stats(
    store: Store, execution_client: str, task_type: Optional[str] = None,
    generation: Optional[str] = None, include_archived: bool = False,
) -> dict[str, dict[str, Any]]:
    """Sufficient-stat per-cell aggregates computed in SQL, without row loading.

    Same cells and same attribution as :func:`cell_summary` (each episode
    exactly once in its owner cell; fallbacks counted, never credited),
    but the database sums everything: normal dispatch reads these instead
    of loading episode, launch, and history rows. Verified work sums only
    live (unsuperseded) acceptances; known-only sums skip unknown rows
    while the unknown counts stay visible.
    """
    conn = store.conn
    filt = "e.execution_client = ?"
    args: list[Any] = [execution_client]
    if task_type is not None:
        filt += " AND e.task_type = ?"
        args.append(task_type)
    if generation is not None:
        filt += " AND COALESCE(e.owner_generation, e.generation) = ?"
        args.append(generation)
    if not include_archived:
        filt += " AND e.archived = 0"
    rows = conn.execute(
        f"""SELECT e.task_type AS task,
            COALESCE(e.owner_option, 'unlaunched') AS opt,
            COALESCE(e.owner_generation, e.generation) AS gen,
            COUNT(*) AS episodes,
            COALESCE(SUM((SELECT COUNT(*) FROM launches l WHERE l.episode_id = e.episode_id
                          AND l.is_fallback = 0)), 0) AS runs,
            COALESCE(SUM((SELECT COUNT(*) FROM launches l WHERE l.episode_id = e.episode_id
                          AND l.is_fallback != 0)), 0) AS fallback_runs,
            COALESCE(SUM((SELECT COALESCE(SUM(a.accepted_work), 0) FROM acceptances a
                          WHERE a.episode_id = e.episode_id
                          AND NOT EXISTS (SELECT 1 FROM acceptances c
                                          WHERE c.correction_of = a.accept_id))), 0)
                AS verified_work,
            COALESCE(SUM((SELECT COALESCE(SUM(CASE WHEN u.missing = 0
                          THEN CAST(u.norm_total AS REAL) ELSE 0 END), 0)
                          FROM usage_segments u WHERE u.episode_id = e.episode_id)), 0) AS tokens,
            COALESCE(SUM((SELECT COUNT(*) FROM usage_segments u
                          WHERE u.episode_id = e.episode_id AND u.missing != 0)), 0)
                AS missing_usage,
            COALESCE(SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM acceptances a
                          WHERE a.episode_id = e.episode_id) THEN 1 ELSE 0 END), 0) AS pending,
            COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM attempts t
                          WHERE t.episode_id = e.episode_id
                          AND t.status IN ('interrupted', 'cancelled'))
                          THEN 1 ELSE 0 END), 0) AS censored,
            COALESCE(SUM((SELECT COUNT(*) FROM acceptances a
                          WHERE a.episode_id = e.episode_id AND a.verdict = 'pass')), 0) AS pass,
            COALESCE(SUM((SELECT COUNT(*) FROM acceptances a
                          WHERE a.episode_id = e.episode_id AND a.verdict = 'partial')), 0)
                AS partial,
            COALESCE(SUM((SELECT COUNT(*) FROM acceptances a
                          WHERE a.episode_id = e.episode_id AND a.verdict = 'fail')), 0) AS fail,
            COALESCE(SUM((SELECT COUNT(*) FROM acceptances a
                          WHERE a.episode_id = e.episode_id AND a.verdict = 'unknown')), 0)
                AS unknown,
            COALESCE(SUM((SELECT COALESCE(SUM(CASE WHEN s.amount_tokens != 'null'
                          THEN CAST(s.amount_tokens AS REAL) ELSE 0 END), 0)
                          FROM spend_ledger s WHERE s.episode_id = e.episode_id)), 0)
                AS spend_uncapped,
            COALESCE(SUM((SELECT COUNT(*) FROM reservations r
                          WHERE r.episode_id = e.episode_id AND r.status = 'open')), 0)
                AS reservations_open
            FROM episodes e WHERE {filt} GROUP BY task, opt, gen""", args).fetchall()
    cells: dict[str, dict[str, Any]] = {}
    for row in rows:
        cells[f"{row['task']} × {row['opt']} @ {row['gen']}"] = {
            "episodes": row["episodes"], "runs": row["runs"],
            "fallback_runs": row["fallback_runs"], "verified_work": row["verified_work"],
            "tokens": row["tokens"], "missing_usage": row["missing_usage"],
            "pending": row["pending"], "censored": row["censored"],
            "pass": row["pass"], "partial": row["partial"], "fail": row["fail"],
            "unknown": row["unknown"], "spend_uncapped": row["spend_uncapped"],
            "reservations_open": row["reservations_open"],
        }
    return cells


def _episode_join_state(conn: sqlite3.Connection, episode: sqlite3.Row,
                       usage_missing: int = 0, spend_missing: int = 0) -> dict[str, Any]:
    """Evidence-join state for one episode: acceptance, usage, finalization."""
    episode_id = episode["episode_id"]
    has_acceptance = conn.execute(
        "SELECT 1 FROM acceptances WHERE episode_id = ?", (episode_id,)).fetchone() is not None
    has_usage = conn.execute(
        "SELECT 1 FROM usage_segments WHERE episode_id = ?", (episode_id,)).fetchone() is not None
    finalized = episode["status"] == "closed" and int(episode["usage_complete"]) == 1
    joined = has_acceptance and has_usage
    return {
        "has_acceptance": has_acceptance,
        "has_usage": has_usage,
        # Any single unknown row taints the episode: all() would let one
        # known row mask an unknown verification tail.
        "usage_missing": usage_missing > 0,
        "spend_missing": spend_missing > 0,
        "joined": joined,
        "finalized": finalized,
        # Inference-ready needs joined evidence AND closure AND complete
        # known cost: finalized episodes passed these checks at closure,
        # and the flags re-derive here so post-closure drift can never
        # slip into the prefix without revalidation.
        "ready": joined and finalized and usage_missing == 0 and spend_missing == 0,
    }


def candidate_order(
    store: Store, execution_client: str, task_type: str, option: str,
    generation: Optional[str] = None, limit: Optional[int] = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Stable pre-outcome episode order for one owner cell, with join state.

    Entries sort by the owner sequence fixed at each episode's initial
    launch, before any outcome is observed; fallback launches take no
    sequence of their own. Each entry carries the exact observation the
    selector needs: accepted work, total uncapped cost, owner
    candidate/generation, and the stable sequence — not only aggregates.
    ``limit``/``offset`` page at SQL so callers never load a whole cell;
    pass neither to walk the full order (prefix computation does).
    No confidence arithmetic here: this is the metadata later inference
    needs to use the fully joined prefix.
    """
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise EpisodeError("order limit must be a non-negative integer or None")
    if not isinstance(offset, int) or offset < 0:
        raise EpisodeError("order offset must be a non-negative integer")
    conn = store.conn
    query = ("SELECT * FROM episodes WHERE execution_client = ? AND task_type = ?"
             " AND owner_option = ?")
    args: list[Any] = [execution_client, task_type, option]
    if generation is not None:
        query += " AND owner_generation = ?"
        args.append(generation)
    query += " ORDER BY owner_seq"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        args.extend([limit, offset])
    elif offset:
        query += " LIMIT -1 OFFSET ?"
        args.append(offset)
    ordered = []
    for episode in conn.execute(query, args).fetchall():
        initial = conn.execute("SELECT run_id FROM launches WHERE episode_id=?"
                               " AND is_fallback=0", (episode["episode_id"],)).fetchone()
        accounting = episode_accounting(store, episode["episode_id"])
        entry = {
            "candidate_seq": episode["owner_seq"],
            "episode_id": episode["episode_id"],
            "run_id": initial["run_id"] if initial is not None else None,
            "owner_option": episode["owner_option"],
            "generation": episode["owner_generation"],
            "accepted_work": accounting["accepted_work"],
            "usage_tokens": accounting["usage_tokens"],
            "spend_uncapped_tokens": accounting["spend_uncapped_tokens"],
            "usage_missing_segments": accounting["usage_missing_segments"],
            "spend_missing_entries": accounting["spend_missing_entries"],
        }
        entry.update(_episode_join_state(
            conn, episode, accounting["usage_missing_segments"],
            accounting["spend_missing_entries"]))
        ordered.append(entry)
    return ordered


def prefix_ready(
    store: Store, execution_client: str, task_type: str, option: str,
    generation: Optional[str] = None,
) -> dict[str, Any]:
    """Finalized ready prefix plus unresolved earlier gaps for one owner cell.

    ``prefix_len`` counts leading ready entries by owner sequence; ``gaps``
    lists unready seqs below the highest ready seq; later finalized records
    stay immediately visible in spend/accounting views and are listed under
    ``completed_beyond_prefix``. ``ready_observations`` carries the exact
    per-episode work and uncapped cost the selector consumes.
    """
    ordered = candidate_order(store, execution_client, task_type, option, generation)
    prefix_len = 0
    for entry in ordered:
        if entry["ready"]:
            prefix_len += 1
        else:
            break
    ready_max = max((e["candidate_seq"] for e in ordered if e["ready"]), default=0)
    gaps = [e["candidate_seq"] for e in ordered
            if not e["ready"] and e["candidate_seq"] < ready_max]
    beyond = [e["candidate_seq"] for e in ordered
              if e["ready"] and e["candidate_seq"] > prefix_len]
    return {
        "execution_client": execution_client,
        "task_type": task_type,
        "option": option,
        "total": len(ordered),
        "prefix_len": prefix_len,
        "ready_episode_ids": [e["episode_id"] for e in ordered[:prefix_len]],
        "ready_observations": [
            {k: e[k] for k in ("episode_id", "candidate_seq", "owner_option", "generation",
                               "accepted_work", "usage_tokens", "spend_uncapped_tokens",
                               "usage_missing_segments")}
            for e in ordered[:prefix_len]
        ],
        "gaps": gaps,
        "completed_beyond_prefix": beyond,
        "entries": ordered,
    }


def ready_observations(
    store: Store, execution_client: str, task_type: str, option: str,
    generation: Optional[str] = None, limit: Optional[int] = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Exact inference-ready episode observations for one owner cell.

    Only finalized episodes with joined evidence and complete known cost
    appear here, each with its accepted work, total uncapped cost, owner
    candidate/generation, and stable sequence. ``limit``/``offset`` page
    the ready prefix for bounded dispatch. All spend — including
    incomplete, unready, and unknown rows — stays separately visible in
    accounting views and is never silently folded into these
    observations.
    """
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise EpisodeError("observations limit must be a non-negative integer or None")
    if not isinstance(offset, int) or offset < 0:
        raise EpisodeError("observations offset must be a non-negative integer")
    view = prefix_ready(store, execution_client, task_type, option, generation)
    return view["ready_observations"][offset:(offset + limit) if limit is not None else None]


def reconcile(store: Store, limit: int = 100) -> dict[str, Any]:
    """Capability/health reconciliation: pending, missing, and gap data.

    Lists are bounded at ``limit`` rows with explicit truncation flags
    and exact totals, so health checks never load whole tables.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise EpisodeError("reconcile limit must be a non-negative integer")
    conn = store.conn

    def bounded(query: str, args: tuple = ()) -> tuple[list[dict[str, Any]], int, bool]:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({query})", args).fetchone()["n"]
        rows = [dict(r) for r in conn.execute(
            f"{query} LIMIT {limit + 1}", args).fetchall()]
        return rows[:limit], total, len(rows) > limit

    pending, pending_total, pending_cut = bounded(
        "SELECT * FROM reservations WHERE status='open'")
    episodes_open, open_total, open_cut = bounded(
        "SELECT e.* FROM episodes e LEFT JOIN acceptances a ON a.episode_id = e.episode_id"
        " WHERE a.accept_id IS NULL")
    missing_usage, missing_total, missing_cut = bounded(
        "SELECT l.run_id, l.episode_id FROM launches l LEFT JOIN usage_segments u"
        " ON u.run_id = l.run_id WHERE u.segment_key IS NULL AND l.status = 'completed'")
    rejected, rejected_total, rejected_cut = bounded(
        "SELECT event_id, payload FROM events WHERE type='launch.rejected'")
    late, late_total, late_cut = bounded(
        "SELECT segment_key, source_key, sequence, delta, applied, resolved"
        " FROM cumulative_snapshots WHERE applied=0 AND resolved=0")
    unknown_usage = conn.execute(
        "SELECT COUNT(*) AS n FROM usage_segments WHERE missing=1").fetchone()["n"]
    unfinalized = conn.execute(
        "SELECT COUNT(*) AS n FROM episodes WHERE status != 'closed'").fetchone()["n"]
    return {
        "pending_reservations": pending,
        "pending_reservations_total": pending_total,
        "episodes_without_acceptance": episodes_open,
        "episodes_without_acceptance_total": open_total,
        "completed_runs_missing_usage": missing_usage,
        "completed_runs_missing_usage_total": missing_total,
        "pre_launch_rejections": [(_load(r["payload"])) for r in rejected],
        "pre_launch_rejections_total": rejected_total,
        "unknown_usage_segments": unknown_usage,
        "non_applied_snapshots": late,
        "non_applied_snapshots_total": late_total,
        "unfinalized_episodes": unfinalized,
        "truncated": pending_cut or open_cut or missing_cut or rejected_cut or late_cut,
        "event_count": conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"],
    }


def close_generation(store: Store, generation: str, event_id: Optional[str] = None) -> int:
    """Archive a superseded generation; history stays intact and queryable.

    Archiving follows each episode's owner generation (fixed by its initial
    launch's observed release), falling back to the provisional generation
    for episodes that never launched.
    """
    conn = store.conn
    with _write_txn(conn):
        _record_event(conn, event_id or f"generation:close:{generation}",
                      "generation.close", {"generation": generation})
        cursor = conn.execute(
            "UPDATE episodes SET archived=1 WHERE COALESCE(owner_generation, generation)=?",
            (generation,))
        return cursor.rowcount


def refuse_legacy_import(kind: str = "") -> None:
    """Refuse to import historical records as unbiased observations.

    The exceptions-only tables have no dispatch denominator and the old
    policy JSON has no lifetime ledger; neither can become calibrated
    evidence for the replacement. Preserve them as archived history only.
    """
    raise MigrationError(
        f"legacy {kind or 'state'} must not be imported as evidence: no unbiased "
        "denominator exists; let ordinary new runs accumulate complete data")


def event_count(store: Store) -> int:
    return int(store.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])
