# Model selection from complete episodes

Select within the execution client the master has already chosen. The objective
is aggregate independently verified useful work divided by aggregate episode
tokens. There is no cross-client ranking, family multiplier, price conversion,
or separate success-rate floor. A cheaper first attempt followed by recovery is
one strategy: all attempt, briefing, verification, repair and fallback costs
belong to its original episode. Worker completion never grants useful-work credit.

Creme stores lossless events and projections in the configured goal store at
`model-fit/runtime/episodes-v2.sqlite3`. Keep this mutable SQLite/WAL directory
Git-ignored; export acceptance/evaluation evidence at checkpoints. The public
code resolves the goal store through the host profile and embeds no private
host paths. Back up a live database using SQLite backup, not a copy of its main
file without the WAL.

## Normal work

An episode is an ordinary independently useful task, declared before its outcome.
Its normal brief names milestones whose total credit is at most one and fixes
the recovery recipe. Accept only the milestones actually verified. A failure or
interruption retains its spend; accepted partial work can retain its declared
credit. Unmeasured usage remains unknown, never zero.

Task types retain the shared vocabulary; context bands refine these rows:

| Identifier | Work |
|---|---|
| `fact-finding` | read-only fact-finding: inventories, searches, state reconnaissance |
| `interface-design` | interface or architecture design |
| `statement-freezing` | statement freezing and Lean-free design (statements, proof transcripts from donors) |
| `lean-elaboration` | Lean elaboration from a frozen design |
| `open-proof` | open-shape proof discovery (no proof to mirror) |
| `proof-repair` | proof repair and diagnosis |
| `mutation-controls` | mutation controls (apply, build, restore, report the biting site) |
| `gate-integration` | gate runs, landings, and integration |
| `code-change` | code change with tests (non-Lean) |
| `doc-authoring` | document authoring |
| `review-audit` | hostile review or audit |
| `mechanical-edit` | mechanical edits and hoists |

Use the structured episode interface:

```sh
python3 -m creme model-fit episode configure --from population.json
python3 -m creme model-fit episode prepare --from brief.json
# Launch the returned actual setting through the selected client's normal tool.
python3 -m creme model-fit episode accept --from acceptance.json
python3 -m creme model-fit episode table --from population-id.json
python3 -m creme model-fit episode health
```

Every command accepts `--dir DIRECTORY` to select another private model-fit
store. JSON `--from -` reads standard input. Exit 3 means a receipt was durably
saved but still needs reconciliation; exit 2 is an invalid request. `health`
reports incomplete joins independently of whether a particular receipt applied.
See [request examples and adapters](model-fit-episodes.md) for complete schemas.

`prepare` both records the predeclared task and selects its setting. Repeating
it with the same episode ID and identical request returns the same decision,
without advancing a clock. A read of `table` or `health` is not a new opportunity.
The execution client owns the clock: a Codex master dispatching Muse work updates
Muse evidence, not Codex's clock. Features and context bands are declared before
outcomes; do not cherry-pick easier tasks for probes and generalize to a full row.

For a brokered task pass `--episode ID` to `muse start` or `luna-reserve start`
(and `--fit-dir` when using a nondefault store). The binding is persisted before
the first turn, follows resumed sessions, and captures actual run identity and
terminal evidence automatically. Each bound session is one task, including its
repair turns; use another episode/session for unrelated work. Existing broker
pin, permission and admission guards still govern the launch. Effort changes on
resume are recorded as actual overrides.

Native adapters use the same normal master acceptance to import run receipts
and measured usage. There is no second per-task model-fit verdict or narrative.
Use measured master windows for briefing, verification, recovery and recording;
allocate a shared window across tasks at most once in total. Cumulative endpoints
must belong to the same source. Overlapping worker/master windows refuse.
Bookkeeping and fallbacks are not free. Preserve provider raw categories:
cached input and reasoning already included in totals are not added again.

## Coverage and unresolved evidence

| Route | Current capture boundary |
|---|---|
| Native Codex | Combined acceptance imports an owned fresh rollout or an explicit resumed counter window, actual release/effort, and separately measured master windows. A source changing model/effort requires separate attempt windows. |
| Muse broker | Automatic launch, terminal and measured parent-turn costs. Reminder-agent costs are not exposed in those totals (Muse's own durable log records them as `usage_family: reminder` with `reported: false` and zero counts), and compaction usage is reported separately from the parent completions, so usage stays incomplete and cannot promote a model. |
| Luna reserve broker | Automatic launch and terminal identity. Thread-cumulative usage requires adjacent rollout windows; the broker record alone is incomplete. |
| Claude Code Agent tool | Combined acceptance imports a subagent transcript (`claude_code_sources`) with its nested Agent-tool children: release from `message.model` (one known release; a fallback refuses), effort from the agent profile's `effort` 1..5 (low..max). Final usage per response comes from Claude Code's OpenTelemetry export (`creme claude-telemetry serve`, default `runtime/claude-otel` of the model-fit directory) joined by `request_id`; an event and a span of one request count once. Complete only when every response of every included transcript has a telemetry record; telemetry requests carrying an `agent_id` of the run's agents but no transcript entry are added (their model must be that agent's release). A transcript final usage or model that differs from telemetry refuses. Missing telemetry, a missing child transcript, a compaction, or a session request that cannot be attributed (an event without its span, or a record without `request_id`) is a gap, never zero. |
| Claude Code master | `claude_code_master_windows` measure the master's own top-level transcript between two UTC times, joined to telemetry the same way, and also charge the session's own telemetry requests in the window with no transcript entry and no `agent_id` (auxiliary requests, any model, listed in `observed_models`). A response without telemetry, an unattributable session request in the window, or a compaction leaves the window unknown. Requests the receiver never got (it was down) are invisible; see the episode guide. |
| Antigravity run | Combined acceptance imports a `creme antigravity run` record (`antigravity_runs`): release is the init model slug, input plus disjoint cache reads, thinking inside output. Final only when the result event is present and no checkpoint or invoked-subagent step consumed unreported usage. |
| Other native clients and one-shot routes | Normalized combined receipts are supported; automatic provider extraction is not claimed. Missing actual identity, completion or total usage remains a capability gap. |

Do not turn these capability gaps into zero-cost observations. A route can still
perform useful authorized work while its incomplete episode is excluded from
statistical promotion. The accounting/health view retains its observed costs and
missing evidence. A protocol PASS is never a master quality verdict.

`episode reconcile` retries durable out-of-order receipts and closes joins whose
normal master acceptance and final usage have both arrived. Retry order rotates,
so old gaps do not starve later completed tasks. Nothing is evicted at a fixed
pending count. Receipt IDs and measured evidence are immutable and idempotent.
An explicitly evidenced missing-only usage resolution can fill unknown totals;
the original raw event remains, known counts cannot change, and spend is charged once.
An invalid unapplied receipt is replaced with an explicit `supersedes` reference;
an applied acceptance is corrected through `correction_of`. Both retain history.
New evidence invalidates earlier closure until the new revision is joined.

## Population and selection

Every dispatch, by every master client, goes through this runtime (user
directive 2026-10-04). Selection needs reusable populations: configure one per
execution client, task type and context band, listing every feasible authorized
candidate, and prepare each task against it. A population made for one task
with one candidate records spend but never selects, explores or learns. Move a
population to `active` once its route's capture closes joins on ordinary use;
a route whose capture is still a gap gets that adapter work first.

Configure a finite list of feasible authorized settings, their observed release,
route, effort, harness, material context and fixed recovery recipe. A material
change gets a new population identity; historical evidence remains inspectable.
Incidental brief wording is not a new population. Actual launch differs from
proposal only with an explicit override reason, and statistics follow actual
identity. Sol means `gpt-6.1-sol`; retired `gpt-6-sol` data is not replacement data.

A cold default is provisional. Cumulative moments use only the fully joined
prefix in each candidate's pre-outcome launch order. Later completions count
immediately for accounting but do not skip an earlier unresolved observation in
inference. This prevents fast-success/slow-failure joining from manufacturing a
favorable sample. Corrections recompute the affected evidence; incumbent switches
do not erase other candidates' histories.

The pure selector uses simultaneous empirical-Bernstein work and cost intervals,
with an error allocation summable over every sample count and candidate. It
compares lower-work/upper-cost against upper-work/lower-cost; a zero lower cost
bound gives an uninformative upper efficiency bound. Keep the incumbent until a
challenger's lower efficiency exceeds its upper efficiency. All-zero observed
work is reported as no demonstrated productive route.

Learning uses both uncertainty-guided trials and protected fair coverage.
`ceil(.25 log²(1+t))` is the minimum growing coverage target; every other learning
slot protects coverage independently of favorable priors. Focused trials have a
`ceil(4 log³(1+t))` per-setting limit. At most one trial is proposed per ten local
ordinary opportunities and at most one trial is pending. Cancelled proposals
release reservations and do not count as completed samples. Unfunded coverage
debt persists. Priors prioritize learning; they never permanently blacklist an
expensive or statistically weak eligible setting.

A seed allowance avoids cold-start deadlock; at most ten percent of observed
ordinary expenditure accrues further learning allowance. Incurred trial cost is
uncapped and the remaining predicted reservation is charged once. An overrun
stops new trials until the allowance permits them. This runtime's reservations
are **expected-cost controls**, not strict token caps: no provider enforces an
entire master-plus-worker episode limit. `enforced` bounds therefore refuse in
runtime configuration. An explicitly assumed finite bound supports conditional
confidence statements; unavailable or violated bounds do not support promotion.

For the conditional convergence argument and practical limits, see
[selection guarantees](model-fit-guarantees.md). Neither stationarity alone nor
a handful of successful real tasks establishes representative sampling, finite
cost tails, convergence speed, or model superiority.

## Rollout, rollback and legacy records

Start a population in `shadow`: record decisions and normal outcomes, but launch
its explicit default. Use `active` only after capture and joins work on ordinary
use. `off` or `shadow` immediately disables active selection for future prepared
tasks without deleting evidence, pending work, reservations or history. Already
launched tasks still need their terminal evidence and normal acceptance.

The old Markdown exception narratives at `$GOAL_STORE/model-fit/<client>.md`
(`codex.md`, `muse.md`, `claude-code.md`, `antigravity.md`) and JSON streak state
remain historical.
They are not all-run denominators and must never seed calibrated moments. A prior
recommendation may be chosen explicitly as a provisional cold default. Once the
new runtime exists, legacy `model-fit recommend` refuses and points to `episode
prepare`; there is no competing active selector. Legacy `add`, `summarize`,
`validate`, `policy` and old-outcome tools remain for inspecting or closing that
history, not for ranking new episodes. Routine successful work needs no prose
entry. Keep unusual qualitative observations when they explain a future decision.
