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

## Sizing a worker

Choose each worker's model and effort for the **hardest non-delegable judgment**
in its brief, not for the total volume of mechanical work. Volume is what more
workers are for; only judgment justifies a higher rung.

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
that the user does not select. As reconciled on 2026-08-31, this workflow's
Codex choices are **Sol**, **Terra**, and **Luna**; its Claude Code choices are
**Fable** and **Opus**. Recheck those names at launch rather than treating this
snapshot as a permanent product catalogue.

Two tiers matter for dispatch, whatever a client calls them:

| tier | what it is for | Codex today | Claude Code today |
|---|---|---|---|
| the standard model | the balanced everyday worker and reviewer: bounded engineering, gate runs, document authoring, routine proof repair | Terra, with Luna for efficient bounded work whose route and falsifier are already clear | Opus |
| the frontier model | the hardest quality-first judgment that cannot be packetized | Sol | Fable |

**The default is the standard model.** A Claude session strongly prefers Opus
workers and escalates to Fable only when the added power is clearly necessary
for a non-delegable judgment, and only after the effort ladder above has been
walked within Opus; a Fable dispatch is justified in one line in the master
log. This is a cost-of-power rule, not a capability ranking: do not claim a
fixed Fable-versus-Opus ordering without a current representative comparison,
and do not read the default as a reason to under-size a genuinely frontier
judgment.

Model diversity is a separate reason to choose a model. An independent review
of work produced by one model is more useful from another, and the audit and
reviewer roles should be dispatched accordingly.

## The six-selector model

Codex and Claude Code currently expose six user-visible selector positions.
The first five form the intelligence ladder. The sixth is an orchestration
mode, not a higher intelligence effort:

| Position | Codex label | Claude Code label | Use |
|---|---|---|---|
| 1 | Light | `low` | Fast bounded work: inventories, direct edits, routine checks, and other tasks with a short falsifiable route. |
| 2 | Medium | `medium` | Ordinary multi-step implementation with clear semantics and modest ambiguity. |
| 3 | High | `high` | Complex implementation or proof repair along a known architecture; latency is secondary to reliability. |
| 4 | Extra High | `xhigh` / extra | Hard architecture, proof strategy, integration, or ambiguous diagnosis that benefits from sustained reasoning. |
| 5 | Max | `max` | The intelligence ceiling: the hardest quality-first, non-delegable reasoning chains. Compare against position 4; more effort can add latency or overthinking without a measured gain. |
| orchestration | Ultra: Max reasoning with automatic task delegation | `ultracode` / Ultra: automatic multi-agent orchestration; recheck its effective lead effort at launch | Choose an orchestration mode when automatic decomposition across genuinely independent workstreams is itself desired. Do not infer one client's exact lead-reasoning semantics from the other's label. |

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
