# Episode requests and adapters

The [model-fit guide](model-fit.md) owns semantics and coverage. Requests below
are private task artifacts. Reuse immutable IDs on retries; do not regenerate
IDs merely to retry the same event. The CLI accepts JSON files or standard input.

Configure once for a fixed authorized population (`configure --from FILE`):

```json
{
  "policy_id": "bounded-review-v1",
  "mode": "shadow",
  "config": {
    "execution_client": "codex",
    "task_type": "review-audit",
    "context_version": "bounded-python-review-v1",
    "harness_version": "native-subagent-v1",
    "default": "sol/high",
    "seed_allowance": 100000,
    "learning_fraction": 0.1,
    "candidates": [
      {
        "option": "sol/high",
        "release": "gpt-6.1-sol",
        "route": "codex-subagent",
        "recipe_version": "review-and-master-verify-v1",
        "prior_tokens": 100000,
        "bound_assumption": "unavailable"
      }
    ]
  }
}
```

This minimal one-candidate example captures evidence but cannot compare models.
Populate all feasible authorized candidates for a learning population. Do not
mistake this illustrative allowance or prior for a measured model cost. Optional
`token_bound` with `bound_assumption: "assumed"` declares a substantive finite-tail
assumption, not a provider-enforced limit. Cost violations retain uncapped spend
and invalidate confidence. Unsupported routes/efforts and retired Sol releases
refuse before ranking. `context_features`, when present, fixes pre-outcome size
or difficulty bands; every prepared task must supply matching `features`.

Prepare as part of the ordinary task brief (`prepare --from FILE`):

```json
{
  "episode_id": "review-parser-1",
  "policy_id": "bounded-review-v1",
  "milestones": [{"milestone": "verified-review", "credit": 1}],
  "master_client": "codex",
  "opportunity_ref": "goal: parser integration; review candidate commit"
}
```

Launch the returned `actual` setting. `selected` is the shadow recommendation;
`actual_exploration` identifies a real reserved trial. Decisions include the
eligibility snapshot, reason, deterministic selection probability, uncertainty,
local opportunity number and budget. `table` takes `{"policy_id":"bounded-review-v1"}`.
Do not prepare fictitious tasks to advance the clock.

One normal acceptance can import a native run (`accept --from FILE`):

```json
{
  "receipt_id": "review-parser-1:accept",
  "episode_id": "review-parser-1",
  "verdict": "pass",
  "milestones": ["verified-review"],
  "verifier": "master-session-identity",
  "worker_ref": "actual-worker-id",
  "verification_ref": "goal evidence: checks and reviewed findings",
  "codex_sources": [{
    "path": "/absolute/path/to/owned-fresh-worker-rollout.jsonl",
    "episode_id": "review-parser-1",
    "run_id": "actual-worker-id",
    "family": "sol",
    "route": "codex-subagent",
    "harness_version": "native-subagent-v1",
    "terminal": "completed",
    "attempt_index": 1
  }],
  "master_segments": [{
    "id": "measured-master-window-id",
    "client": "codex",
    "usage": {"total_input": 1200, "total_output": 300},
    "weight": 1
  }]
}
```

The numbers above demonstrate the shape only; real acceptance must use measured
usage, not these example values. `codex_sources` extracts worker totals and actual
release/effort from the named source. A resumed counter needs its saved `before`
snapshot. Never charge a resumed main chat from zero. `codex_master_windows`
accepts `{before, after, client, weight}` objects and generates the corresponding
shared segment from measured endpoints. `snapshot --from FILE` reads
`{path, previous?}` and returns an incremental metadata-only snapshot. A normal
client hook can call the same Python functions without another result-writing
step. No message body is imported into a usage record.

A normalized `runs` list can be supplied instead. Each entry contains
`receipt_id`, `episode_id`, `run_id`, `option`, observed `release`, `route`,
`harness_version`, `attempt_index`, `terminal`, `segments: [{id, usage}]`,
`usage_complete`, and `usage_evidence`. Cache/reasoning conventions are explicit
in `usage`; missing values are unknown. Set finality only when the adapter covers
the whole run, including auxiliary work. `override_reason` is required when an
actual setting differs from the proposal, including stronger fallback attempts.

If a recorded segment initially lacked totals, a later segment with the same
`id` can carry `resolves_missing: true` and `evidence: "provider evidence reference"`
alongside definitive `usage`. This fills only genuinely unknown totals, preserves
the original raw event, and cannot change known components or the run/window
identity. It is not a way to revise known measured costs. Run finality still
requires evidence covering all auxiliary work.

The initial attempt has index1; each recovery attempt has its fixed next index.
Feedback arriving for a later attempt remains durable/pending until earlier
identity arrives. It cannot become the strategy owner merely by completing
first. Normal native acceptance imports launch and completion together; inference
uses declaration order from `prepare`, not receipt arrival order. Optional
`receipt` calls let native tools record launch and partial evidence sooner.

A failed or interrupted attempt still needs an honest normal master disposition.
Use `verdict: "fail"` with no credited milestones for rejected work; use
`"unknown"` for an unjudged interruption. Accepted independent milestones may
receive partial credit. Diagnostic terminal status and useful work are separate.
All spend survives either verdict. A worker may not identify itself as verifier.

To correct a previously applied acceptance, supply a new `receipt_id` and
`correction_of` naming its current predecessor. Credit replaces that acceptance's
credit; it does not add another successful task. For an invalid unapplied inbox
receipt, use `supersedes` instead. Corrections cannot fork. Leave unrelated
pending feedback intact; a new valid receipt is not permission to discard it.

Other operations:

```json
{"policy_id":"bounded-review-v1","mode":"off","reason":"roll back selection; retain evidence","event_id":"rollback-1"}
```

Pass this to `mode`; `shadow` and `active` use the same shape. `cancel` takes
`{episode_id, reason}` for a never-launched proposal. Launched work must retain
its actual terminal and spend. `reconcile` can take
`{"broker_records":["/absolute/path/to/session.json"]}` to replay a durable
broker record after capture failure. `health` reports integrity, pending inbox
items, reservations, missing usage and unfinalized episodes. Broker capture
errors are also stored in the broker's `model_fit_capture` field; they never
rewrite the worker's protocol verdict.
