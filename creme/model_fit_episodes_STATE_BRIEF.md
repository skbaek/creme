# Model-fit episodes store — stages 1/2 foundation checkpoint

Module: `creme/model_fit_episodes.py`. Tests:
`scripts/tests/test_model_fit_episodes.py` (54 tests, all passing).
No existing file was modified; compatibility is import-only
(`model_fit.CLIENTS`, `TASK_TYPES`, `parse_tokens`, route maps).
Selector mathematics files (`creme/model_fit_efficiency.py`,
`scripts/tests/test_model_fit_efficiency.py`) are the master's and were
not touched.

## What was built

SQLite-backed (stdlib only) episode evidence/lifecycle store: one atomic
transaction per mutation, append-preserved `events` log with
replay-before-cap idempotency / conflicting-reuse-fails semantics, no
eviction, restart-safe. Covers: named predeclared milestones (total ≤ 1)
and recipe version; proposal vs actual launch; capability gate before
ranking; observed-only release identity (sol must be GPT-6.1); strict
validated normalization; sourced/sequenced cumulative snapshots with
store-computed deltas; shared master allocation via exact FK column;
attempt vs acceptance separation; partial/unknown/interrupted;
milestone-named credit with single-chain no-fork corrections; worker
self-verification refused; uncapped spend ledger + advisory reservations;
single pre-outcome owner cell per episode with fallback diagnostics;
generation boundaries on release/route/recipe/context/harness with
archive-intact history; master finalization as the usage-complete marker;
per-cell cumulative views with missing/pending/censored coverage; exact
ready-episode observations for the selector; `reconcile()` health data;
`register_launch_and_acceptance()` combined interface;
`refuse_legacy_import()` migration guard.

## Design choices

- SQLite over JSON: atomic multi-row transactions, UNIQUE idempotency
  keys, ordered replay, restart preservation from one stdlib file. A
  second handwritten JSON projection + replay-on-dispatch was rejected
  per the brief.
- Releases are observed, never synthesized: `register_launch` requires
  the adapter-seen model id; sol validates exactly GPT-6.1.
- One owner per episode: the initial launch fixes owner option +
  generation (initial setting + fixed recipe) and takes the owner-cell
  sequence; fallbacks (`is_fallback=1`, no seq) keep diagnostics only.
  Cheap-first + expensive fallback charges once to the cheap recipe;
  no free standalone strong success exists.
- Normalization validates (finite, non-negative, non-boolean),
  leaves absent parts unknown, honors `cache_write_additive`, and
  rejects contradictions (parts exceeding wholes, parts ≠ total).
- Cumulative counters key on (source, sequence) with transactional
  deltas; late/decreasing/unknown snapshots are preserved raw with no
  spend effect and no source corruption.
- Credit names milestones and must equal their predeclared sum;
  corrections cite a tip acceptance (no forks); milestones cannot
  double-count across live acceptances.
- `ready` = joined evidence AND master finalization (all runs terminal,
  declared verification usage present, acceptance recorded). Accounting
  always shows partial cost; inference sees only `ready_observations`.
- Counters partition by (client, task, option, generation); replays
  reuse stored order; dev-era stores migrate deterministically.

## Exact commands / verdicts

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest scripts.tests.test_model_fit_episodes`
  → 54 tests, OK.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest scripts.tests.test_model_fit
  scripts.tests.test_model_fit_policy` → all pass (no existing file
  touched; run to confirm before acceptance).
- Real failure encoded: `muse-spark/none` on `muse-broker` raises
  `CapabilityError`, records `launch.rejected`, and attributes nothing to
  the model; verified in
  `test_muse_none_on_broker_rejected_before_launch`.

## Limitations

- No selection mathematics, no confidence formulas, no backoff/streak
  state (parent freezes these next against this API).
- No CLI wiring and no broker/adapter hooks yet (next seam below).
- Reservations are advisory; no strict cost-cap enforcement is claimed.
- Generation filter on `prefix_ready` slices seqs; cross-generation
  prefix semantics are the selector's decision, not this module's.
- Statistical/product questions not fixed by the report are stated, not
  weakened: finite-horizon budget constants, fair-coverage schedule, and
  drift-detector design remain open for stages 3–5.

## Next integration seam (exact)

1. Broker/native launch paths (`creme/muse_broker.py::cmd_start`,
   reserve/Codex equivalents): after the existing preflight, call
   `check_capability()`; on success `register_launch()` with the
   eligibility snapshot + exclusions + override reason; stream per-turn
   `record_usage()` / `record_cumulative_delta()`.
2. Master acceptance action: one `record_acceptance()` call with the
   verification reference and a verifier ≠ worker; corrections via
   `correction_of`.
3. Reconciler: periodic `reconcile()` surfacing launched-but-unjoined
   runs, missing usage, and open reservations; capability gaps reported
   explicitly per route.
4. Selector (stage 3, master-owned files only): consume `cell_summary()` +
   `candidate_order()`/`prefix_ready()`/`ready_observations()`; fair-coverage
   counters must advance only on the execution client's own opportunities.
