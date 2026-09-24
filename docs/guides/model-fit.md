# Model fit tables

A master choosing a subagent's model and effort should be able to see how each
option of **its own client** has actually done on this kind of task on this
host's Jaune, Blanc, and Creme work. The model fit tables hold that record.
This guide is the method: what the tables are, how an observation is recorded,
how a cell is summarised, and how a dispatch uses them. Creme carries no
observation and no judgement about any model; the tables live in the goal
store.

## Where the tables are

The tables are in the goal store by default, at
`$GOAL_STORE/model-fit/<client>.md`, one file per client:

| file | client | options (columns) |
|---|---|---|
| `claude-code.md` | Claude Code | `fable`, `opus`, `sonnet` × `low` `medium` `high` `xhigh` `max` |
| `codex.md` | Codex | `astra`, `sol`, `luna` × `low` … `max`, and `luna-reserve` × `low` … `max` |
| `muse.md` | Muse | `muse-spark` × `none` `minimal` `low` `medium` `high` `xhigh` `max` |
| `antigravity.md` | Antigravity | `gemini-3.8-flash` × `low` `medium` `high` |

`luna-reserve` is a separate column group because it is a different route to
the Luna model (the `gpt-reserve` allowance through the Creme broker, usually
driven by a non-Codex master), and its fit need not equal that of a Luna
worker under a Codex master. It can also serve an older release than
regular Luna (`luna-reserve status` prints the model); the header says which. The option list is the one
`creme/model_fit.py` fixes; when a client adds, renames, or retires a
selectable model or effort, change that list and this table together.

**An option names its family's current release.** Each table's header states
which release every family denotes and since when. When a release is
superseded (a new Opus, a new Sol) or a family is retired, move that family's
observations to `$GOAL_STORE/model-fit/archive/<client>-<release>.md` with a
line saying what superseded them, and let its cells restart empty: a run on
the old release is not evidence about the new one. The archive is history,
never selection evidence; `validate` does not read it, and `add` numbers new
observations past every archived id.

Only the goal store holds the files, because their evidence links resolve only
there and in the host-local master record, and because a public table would
be a published vendor judgement, which is the user's decision. Moving the
tables out of the goal store is reserved for the user.

**One file per client, and options are compared only within a file.** There
is no cross-client table and no cross-client ranking. Most dispatches start
from a fixed client, and a shared table would invite each vendor's models to
tilt the comparison. A model may edit another client's file when it recorded
the run (for example a Claude master recording a Luna reserve run); such a
commit names itself in its message with `cross-client edit:` and the reason.

## Rows: the task-type vocabulary

Every file has the same rows. Classify a run by the **hardest non-delegable
judgment its brief asked for**, the same axis the briefs guide sizes by:

| task type | what the brief asked for |
|---|---|
| `fact-finding` | read-only fact-finding: inventories, searches, state reconnaissance |
| `interface-design` | interface or architecture design for consumers the brief names |
| `statement-freezing` | statement freezing and Lean-free design: statements, proof transcripts from named donors |
| `lean-elaboration` | Lean elaboration from a frozen design (statements, and usually a proof route) |
| `open-proof` | open-shape proof discovery: no proof to mirror, the route is the work |
| `proof-repair` | proof repair and diagnosis of a failing or drifted proof |
| `mutation-controls` | mutation controls: apply, build, restore, report the biting site |
| `gate-integration` | gate runs, landings, reconciliation, integration of finished units |
| `code-change` | code change with tests, not Lean |
| `doc-authoring` | document authoring: guides, reports, goal documents |
| `review-audit` | hostile review or audit of a candidate |
| `mechanical-edit` | mechanical edits and hoists whose result is easy to check |

A run that did two of these is recorded under the harder one; say the other in
`notes`. Change the vocabulary only in Creme, and only with a migration of the
existing observations.

## Observations

An observation is one dispatched run, recorded **by the master after it has
verified the result**. The worker's own report is never the verdict. Each
observation is a `### <prefix>-NNNN` entry under `## Observations` (prefix
`cc`, `cx`, or `mu`) with these `- key: value` fields:

| field | content |
|---|---|
| `task_type` | a row identifier above |
| `option` | `family/effort` from this client's columns |
| `route` | a route tag, then free detail: `claude-agent-tool`, `claude-session`; `codex-subagent`, `codex-session`, `luna-reserve-broker`, `luna-reserve-run`; `muse-worker`, `muse-session`; `antigravity-run`. Detail names the harness, e.g. `luna-reserve-broker (Lean mode, Claude Opus master)` |
| `goal` | goal id, or `n/a` |
| `date` | `YYYY-MM-DD` of the run's start |
| `run` | the run's identifier (agent id, Luna session id, rollout id) |
| `source` | evidence link to the run record (transcript, session directory) |
| `verdict` | `pass`, `partial`, `fail`, or `unknown` |
| `verdict_source` | link to the master's verification record (an event id in `master/events.jsonl`, field notes, a report or review) for `pass`/`partial`/`fail`; for `unknown`, `none — <why no join>` or the record that left it open |
| `failure_modes` | what went wrong, in a phrase, or `none` |
| `tokens` | `uncached_input=N cache_read=N cache_write=N output=N reasoning=N`, each an integer or `n/a` |
| `wall_time` | `<seconds>s` or `n/a` |
| `turns` | assistant turns (Claude API responses, Luna turns, Codex turns), or `n/a` |
| `retries` | re-dispatches of the same brief the master made, or `n/a` |
| `rework` | what the master had to redo or repair after the run, or `none` |
| `recorded_by` | the recording master (client, model/effort) or the backfill that joined it |
| `notes` | optional |

Verdicts: `pass` is accepted as briefed on the master's checks; `partial` is
accepted after rework, or accepted for part of its scope; `fail` is rejected or
redone; `unknown` is every run whose master-verified verdict cannot be joined.
Never infer a verdict from the worker's summary, from silence, or from a later
merge whose record does not name the run.

A run ended or truncated by something outside its work — a client capacity or
usage limit, a user- or master-ordered wind-down or pause, a master error, a
reassignment, or a scope cut by a user decision — says nothing about the
option's quality: record it `unknown` with `verdict_source: none —
interrupted: <cause> (<record>)`, unless the master's record judged the part it
finished, in which case judge only that part and name the interruption in
`notes`. `fail` is for work rejected or redone on its merits, and `partial`
only when the missing part was the run's own shortfall.

Cost fields are observables, never dollars. Tokens by category are not a
price: categories are billed differently and differently by client. A
weekly-limit or bucket delta may be quoted in `notes` only when the record
shows the run was the bucket's only consumer in that window. Wall time
includes waits for host admission and approvals.

### Recording one

```sh
python3 -m creme model-fit add "$GOAL_STORE/model-fit/<client>.md" \
  --from-claude-transcript ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl \
  --task-type lean-elaboration --option opus/high \
  --route "claude-agent-tool (profile worker-high, model opus)" --goal <goal> \
  --verdict pass --verdict-source "\$GOAL_STORE/master/events.jsonl#<event_id>" \
  --failure-modes none --rework none --recorded-by "master claude opus/xhigh"
```

In Claude Code the two axes come from different places, so record both. A
subagent profile fixes only the **effort**; the **model** is the Agent tool's
`model` parameter, which overrides whatever a profile's frontmatter says. So
`--option` names the pairing that actually ran (`opus/high`), and the route
detail names the profile and the model passed (`profile worker-high, model
opus`). A dispatch that omitted `model` inherited a default rather than
choosing one and is not a recordable observation; the effective model is
recoverable from the subagent transcript if you need to check.

`--from-luna-session <session dir>` and `--from-codex-rollout <rollout.jsonl>`
fill `tokens`, `wall_time`, `turns`, `date`, `run`, and `source` the same way;
any explicit flag overrides what the extractor found. Luna token counts are
thread-cumulative, so a resumed session is recorded net of the session it
resumed. `--dry-run` prints the observation without writing. `add` refuses an
invalid observation and regenerates the summary. Commit the table in the goal
store with the verification it cites.

## Cells and the derived summary

The route tags include `antigravity-run` for the one-shot Antigravity route.

The block between the `model-fit:summary` markers is generated by
`python3 -m creme model-fit summarize FILE` (and by every `add`) and is never
edited by hand. For each family it shows the task-type × effort grid; a cell
reads `no data`, or `Nv: pP aA fF +uU` — N master-verified runs, their pass,
partial, and fail counts, and u runs with an unknown verdict. A cell with at
least three verified runs is marked `guides`. Under the grids, one line per
populated cell gives its verdicts, up to three most recent failure modes, and
the median output tokens and wall time. Everything else stays in the
observations below the summary, read only when a cell decides a choice.
Quality comes first in every cell; cost is second and carries the cautions
above.

Every observation is a single uncontrolled run whose outcome also depends on
its brief and on the master's verification. A cell is a record, not a
benchmark. Controlled comparisons, where they exist, are cited from `notes`.

## Selection rule

Before sizing a worker, read the summary grid of your client's file (the block
between the summary markers), not its observations; open a cell's observations
only when that cell decides the choice.

- A cell marked `guides` (at least three verified runs) for the brief's task
  type informs the choice: prefer an option whose verified runs pass, avoid
  one whose failure modes match the brief's hardest judgment, and compare cost
  observables only between options whose quality is comparable.
- Otherwise the [briefs guide's](briefs.md#sizing-a-worker) sizing rules
  decide: the standard model first, the effort ladder before the frontier
  model.

**Record exceptions, not every run** (user decision, 2026-09-23). Record an
observation only when it would change a future choice:

- a `fail`, or a `partial` whose shortfall was the run's own;
- a run that contradicts the guiding cell it was sized by;
- a deliberate test of an option on a cell with no guidance, such as a first
  trial of a new release or a cheaper rung;
- a cost observation that would change the choice between options of
  comparable quality.

A run that went as its sizing expected is not recorded. Batch recordings and
commit them with the verification they cite at a real checkpoint, not as a
transaction per run. The per-run rule this replaces cost more reading and
bookkeeping than its uncontrolled evidence was worth.

## Validation

`python3 -m creme model-fit validate [DIR]` (DIR defaults to the goal store's
`model-fit/`) refuses: a missing or unknown client file; an unknown task type;
an option from another client or an unknown model or effort; a route that does
not serve the option; an observation without a `source` link, or a
`pass`/`partial`/`fail` without a `verdict_source` link; a malformed cost
field; an observation recorded by the worker; a duplicate id; and a summary
that differs from what the observations derive, naming any cell that claims
data no observation supports. `python3 -m creme model-fit init [DIR]` creates
missing skeletons with every row and column present.
