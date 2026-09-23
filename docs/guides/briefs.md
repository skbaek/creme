# Worker briefs, model, and effort

A worker is a subagent the master spawns inside its own session, with a brief,
a per-goal worktree it owns, and a return contract. The brief is either a full
goal document written to [the goal guide](goal.md), when the work has product
semantics worth reviewing, or a short `master/briefs/<goal>.md` when it does
not. This guide covers the brief itself and the sizing decision that goes with
it; [the master guide](master.md#workers) covers how workers are run.

## What a brief states

- the **objective**, in one paragraph, and the honest boundary of the claim;
- the **owned paths** — the files this worker may change, disjoint from every
  other running worker;
- the **resource class** and any hold the work is expected to take;
- the **gates** that must be green on the exact candidate, by their entry in
  the owning repository's `scripts/GATES.md`;
- the **decisions the worker may make alone**, and what it must return
  unresolved;
- where its **report and state brief** go, and what its return value must
  contain.

A brief is not a substitute for a goal document when the work fixes product
semantics. If writing the brief requires inventing an answer the user has not
given, write or amend a goal instead.

### Name the hold the gates themselves take

A brief that names a gate must also say whether the worker may hold the
semaphore *around* it. A repository's gate runner may take its own inner hold,
and an outer hold the master or worker took under a different label does not
satisfy it: admission is keyed on exact label equality, so the inner request
arrives as a distinct competitor and is refused. The gate runner then reports a
prerequisite failure having verified nothing, and a long host-exclusive slot is
spent at row zero.

State in the brief which of these applies — that the worker takes no outer hold
and lets the runner coordinate itself, that the outer hold uses the label the
runner will ask for, or that the runner is told to inherit the caller's hold
through whatever mechanism the repository provides. Read the owning
repository's `scripts/GATES.md` for which label its runner derives and which
mechanism it offers; the guide does not restate a repository's catalogue.

### Liveness: bounded waits and heartbeats

For every client: a long gate or build runs as one tracked command, with output
going to a log file from the first attempt — never detached behind a
`sleep N; check` loop or `nohup`. A timeout is not completion and never
authorizes restarting a process that may still be live. How a master learns
that a worker is alive differs by client, so the liveness clauses a brief
carries do too:

When a worker appears stalled, inspect its actual live command or task handle;
frozen artifacts and an unanswered status pulse do not establish that it ended.
Use supported controls to stop or interrupt a genuinely stalled owned worker,
then confirm terminal status and cleanup before assigning replacement
ownership. An observation timeout alone never authorizes a restart.

- **Claude Code.** The harness does the liveness work: `run_in_background`
  and `Monitor` notify on completion, `sleep N; cmd` is blocked, contexts
  auto-compact, and a stalled subagent returns as a failed task that a message
  to the same agent resumes. A worker that idles on a background command
  returns an interim result and is resumed by message. Briefs carry no poll
  caps, heartbeat files or hand-off rule.
- **Codex.** Native collaboration messages and worker-completion delivery
  reach the master. For a healthy tracked worker, briefs do not require
  periodic master polling, heartbeat-file churn, or a fixed poll cap that
  forces checkpoint and re-dispatch. Use the notifications the active harness
  actually exposes; if it is quiet or lacks them, use bounded status checks
  and escalate only when the worker appears stalled. A long command still runs
  as one tracked operation with a durable log, and a timeout never means that
  the process ended. A Codex brief opens with one compaction-safe line naming
  the brief's own absolute path and the worktree's `STATE-BRIEF.md`; re-read
  those after every compaction before the next action. Keep durable green
  checkpoints and hand off when the context no longer supports the next
  coherent unit.
- **Muse.** No notification channel reaches the master, so briefs state all
  three: bound every poll (interval at most 10 minutes, at most 6 rounds; at
  the cap, checkpoint, write the state brief and report back for re-dispatch,
  never extend in place); refresh an uncommitted `STATE-BRIEF.md` at the
  worktree root at least every 30 minutes during a gate, without moving HEAD,
  so the master can tell stuck from slow by mtime; and hand off when the
  context no longer supports the next coherent unit.

## Sizing a worker

Choose each worker's model and effort for the **hardest non-delegable judgment**
in its brief, not for the total volume of mechanical work. Volume is what more
workers are for; only judgment justifies a higher rung.

**Consult the model fit tables first.** Read the summary grid of your
client's file in the goal store (`$GOAL_STORE/model-fit/<client>.md`). A cell
with at least three master-verified runs for the brief's task type informs the
choice; otherwise the rules below decide. Record only the exceptions
[the model fit guide](model-fit.md) lists.

Sizing is a dispatch decision made against the client's currently available
offerings, not a property of the goal. Recheck the installed client's models
and selectors at launch. A renamed or retired model may be replaced by its
closest current capability equivalent, but record the substitution and the
effective effort in the board and the state brief. Never fall back silently to
a client default. A goal may fix a configuration as a reserved decision; then
it binds, and the master records that it is honouring it.

Older goal documents carry a **Recommended lead configuration** section from
the shape that preceded this guide. That section is required but advisory
where it appears: it records the author's sizing judgment at authoring time,
not a permanent dependency on one vendor release. Reconcile it against the
offerings available at dispatch and record what was actually run. Only a
configuration the goal fixes as a reserved decision binds.

A client may split the two axes across different mechanisms, and the sizing
record must name both whatever the mechanism. In Claude Code a subagent
profile fixes the effort and the Agent tool's `model` parameter chooses the
model, overriding the profile; so the profiles are one per role and effort
(`worker-high`, `reader-xhigh`, `reviewer-max`) and every model is available at
every rung without a profile per pairing. Pass `model` explicitly: an omitted
one inherits a default, which is a dispatch nobody chose and an observation
that cannot be recorded.

**Escalate effort before model.** When a standard-model worker looks
insufficient — thrashing, repeating an approach, not converging — the first
escalation is effort within the same model, one rung at a time:
`medium` → `high` → `xhigh`. Only after that ladder is exhausted does the
frontier model become the answer, and that dispatch is justified in one line
in the master log. The same ordering runs backwards: after the hard boundary
in a piece of work becomes mechanical, de-escalate the effort rather than the
model, and re-dispatch the remainder to smaller workers.

## Model choice

Use the client-visible model name, not a redundant API-family prefix or suffix
that the user does not select. As reconciled on 2026-09-23, the workflow's
Codex choices are **Astra**, **Sol**, and **Luna** (all GPT-6; Terra is
retired); its Claude Code choices are **Fable**, **Opus** (Opus 5.5), and
**Sonnet**; its Muse choice, reconciled 2026-09-11, is
**muse-spark**, selected at launch with `--model`. Recheck those names at
launch rather than treating this snapshot as a permanent product catalogue.

Two tiers matter for dispatch, whatever a client calls them:

| tier | what it is for | Codex today | Claude Code today | Muse today |
|---|---|---|---|---|
| the standard model | the balanced everyday worker and reviewer: bounded engineering, gate runs, document authoring, routine proof repair | Sol, with Luna for efficient bounded work whose route and falsifier are already clear | Opus | muse-spark at the session's effort |
| the frontier model | the hardest quality-first judgment that cannot be packetized | Astra | Fable | muse-spark at ceiling effort (no second family observed) |

**Codex dispatch coverage.** Codex's native spawn tool selects model and reasoning effort directly; a
worker, reader or reviewer role comes from its brief. It therefore needs no
model-by-effort profile grid like a client whose effort is fixed in profile
files. Check the live tool schema and supported model settings before dispatch,
and specify both axes when the brief's sizing decision chooses them. With the
`collaboration.spawn_agent` interface, an explicit override requires
`fork_turns="none"` or a finite history count; a full-history fork inherits the
parent settings. Follow another interface's actual contract rather than copying
these parameter names blindly.

Before assigning a Codex worker an operation likely to need sandbox
escalation, check what task authorization its context will retain. For a
model/effort override, prefer a finite fork that includes the applicable user
instructions when available, and verify what the worker inherited; do not
assume a `fork_turns="none"` brief carries the parent's conversation. If that
context cannot be preserved, plan for the master to launch the authorized
escalation-prone unit from its own task from the outset, while the worker owns
candidate preparation and analysis. This is a dispatch choice before any
denial, not a route around one. Existing authorization may cover the whole
task; no separate worker or per-gate user grant is implied.

Keep dispatch availability separate from fit evidence. A `no data` cell means
no verified observation, not an unavailable setting. Conversely, a table row
does not establish that a current client can launch that option. Check supported
settings and role constraints, then verify the effective configuration on real
work. Do not manufacture an exhaustive grid of empty tasks or count a successful
launch as a successful task. A reader or reviewer brief restricts its work; that
alone does not establish a harness-enforced read-only sandbox.

A Muse worker inherits its master's model and effort route; the client offers
no per-worker model or effort selector. Size a Muse worker by launching (or
relaunching) the session at the route its hardest brief needs, record that
session route in the board and the state brief instead of a per-worker
selection, and get model diversity from a session running a different route
rather than from a dispatch flag.

**The default is the standard model.** A Claude session strongly prefers Opus
workers and escalates to Fable only when the added power is clearly necessary
for a non-delegable judgment, and only after the effort ladder above has been
walked within Opus; a Fable dispatch is justified in one line in the master
log. This is a cost-of-power rule, not a capability ranking: do not claim a
fixed Fable-versus-Opus ordering without a current representative comparison,
and do not read the default as a reason to under-size a genuinely frontier
judgment. A Muse session has no per-worker choice to prefer: its workers
inherit its route, so the session itself is launched at the effort its hardest
brief needs.

Model diversity is a separate reason to choose a model. An independent review
of work produced by one model is more useful from another, and the audit and
reviewer roles should be dispatched accordingly.

**Luna reserve is an external token-saving tier, not a rung on this ladder.**
When the user instructs it, a master may dispatch a bounded task whose result
it can cheaply verify (an inventory, a log or diff summary, a mechanical edit,
a first-pass check) to Codex Luna reserve as a pseudo-subagent through
`python3 -m creme luna-reserve run`, saving its own and its workers' tokens.
In Lean mode it also elaborates a unit whose statements a Claude or frontier
designer has frozen, and runs mutation-control campaigns, with the master
checking headers at each build approval; its effort fit is recorded in the
Codex fit table's `luna-reserve` columns. It never replaces a worker for
architecture, interface design, or a non-delegable judgment, and it is never a
fallback when a client is busy or out of quota. Its brief states the question, the target directory, read-only
or write mode, and the evidence to quote; the master verifies the answer
before relying on it. The guarded path, the attribution guard, and the stop
rule are in the [Luna reserve guide](luna-reserve.md).

## The six-selector model

Codex, Claude Code, and Muse currently expose six user-visible selector positions.
The first five form the intelligence ladder. The sixth is an orchestration
mode, not a higher intelligence effort:

| Position | Codex label | Claude Code label | Muse label | Use |
|---|---|---|---|---|
| 1 | Light | `low` | `low` | Fast bounded work: inventories, direct edits, routine checks, and other tasks with a short falsifiable route. |
| 2 | Medium | `medium` | `medium` | Ordinary multi-step implementation with clear semantics and modest ambiguity. |
| 3 | High | `high` | `high` | Complex implementation or proof repair along a known architecture; latency is secondary to reliability. |
| 4 | Extra High | `xhigh` / extra | `xhigh` | Hard architecture, proof strategy, integration, or ambiguous diagnosis that benefits from sustained reasoning. |
| 5 | Max | `max` | `max` | The intelligence ceiling: the hardest quality-first, non-delegable reasoning chains. Compare against position 4; more effort can add latency or overthinking without a measured gain. |
| orchestration | Ultra: Max reasoning with automatic task delegation | `ultracode` / Ultra: automatic multi-agent orchestration; recheck its effective lead effort at launch | `ultra`; recheck its exact orchestration semantics at launch | Choose an orchestration mode when automatic decomposition across genuinely independent workstreams is itself desired. Do not infer one client's exact lead-reasoning semantics from the other's label. |

Muse also accepts `minimal` and `none` below position 1 for trivial bounded
calls, and its launch default is `high`. Its effort flag is
`--reasoning-effort`; it applies to the whole session, which its workers
inherit.

Current OpenAI model guidance describes Codex Max as maximum reasoning for one
task and Codex Ultra as maximum reasoning with automatic task delegation. The
latter can reduce wall-clock time on work that divides cleanly. Recheck the
[current Codex model guidance](https://learn.chatgpt.com/docs/models) and
[API model guidance](https://developers.openai.com/api/docs/guides/latest-model)
when dispatching, because model support and defaults drift.

Ultra is therefore not a sixth intelligence rung and must not be chosen as
shorthand for "strongest." Creme already specifies explicit delegation,
ownership, and host-headroom rules, and the master already owns decomposition.
Prefer Extra High when that is enough reasoning, Max when the worker's own
judgment needs the ceiling, and Ultra only when its automatic orchestration is
a deliberate benefit rather than duplicate policy.

Do not choose Max reflexively. Use it when the hard part is one coupled chain
that cannot be safely packetized — for example, freezing a novel invariant or
integration boundary. De-escalate after that boundary becomes mechanical.
Bounded discovery, fixture generation, routine proof repair, gate execution,
and independent review normally belong to appropriately sized workers rather
than to an inflated setting.
