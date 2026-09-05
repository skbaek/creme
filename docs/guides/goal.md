# Executable goal contracts

A goal is a durable contract for autonomous execution. It carries what nothing
else in the workflow can supply: the user-reviewed product semantics, the
decisions already fixed and the ones reserved, and the definition of done with
the evidence that settles it. It is specific enough to prove complete, explicit
about boundaries, and independent of disposable client memory.

A goal document is one of the two forms of instruction a worker can be handed.
The other is the short worker brief described in
[the briefs guide](briefs.md). Write a goal document when the work has product
semantics worth reviewing, a completion claim someone will audit, or decisions
that must outlive every session that touches them; write a brief when it does
not. The goal is also the contract an independent audit judges the delivered
work against, which is why it states what must become true rather than the
coordination method [the master](master.md) already owns.

## Required shape

1. Stable ID, title, date, owner, and status.
2. Exact objective and the honest claim: what the finished work asserts and,
   explicitly, what it does not.
3. Mandatory outcome table: each condition has inspectable acceptance evidence,
   named with the negative control that would fail if the outcome were absent.
4. Scope and non-goals.
5. Fixed decisions, invariants, and authority order.
6. Verified starting state with dated commits and the remaining uncertainty.
7. Workstreams with dependencies, ownership, and resource class — a dependency
   graph and its convergence gates, not a schedule.
8. Verification sources and goal-specific controls.
9. Decisions the executing worker may make alone, and decisions reserved for
   the user.
10. Completion-report requirements: where the report goes and what its
    condition-to-evidence table must contain.

Use `ready` only when a competent worker can begin without inventing product
semantics. Use `active` while a live execution owns the goal, `blocked` only at
a genuine impasse, and `complete` only when every mandatory outcome has
evidence on the exact delivered candidate.

For a repository with content-addressed gate evidence, write checkpoint and
merge-candidate closure as a **complete content-valid manifest**: every
catalogue row is freshly green or has successful evidence with an identical
verdict-relevant identity. Require all-fresh execution only when freshness is
itself an acceptance subject; routine draft pushes should run the affected set
and required cheap invariants without inheriting a blanket freshness claim.

## What the goal no longer carries

Earlier goal documents also configured the execution lead and the mechanics of
running the work. Under the master-led workflow those belong elsewhere, and a
goal that restates them creates a second authority that drifts:

- **Lead, model, and effort selection** moves to the master's brief, under
  [the briefs guide](briefs.md). The master sizes each worker at dispatch,
  against current client offerings, and records what it ran with. A goal owner
  who genuinely needs a fixed configuration may still fix it in the goal as a
  **reserved decision** in item 9; then it binds the master.
- **Delegation posture and packet boundaries** move to the brief. The goal
  gives the dependency graph and file ownership (item 7); how that is cut into
  packets, and by whom, is a dispatch decision.
- **Branch target and merge cadence** move to the master's merge policy in
  [the master guide](master.md#merges). A worker hands over a green candidate;
  it never chooses the merge.
- **State and recovery locations** move to the master record — the board says,
  per goal, the worktree, branch, last checkpoint, and next unit — and to the
  worker's own state brief. The goal still names where its completion report
  goes, because that is an acceptance artifact.

## Why this shape

Each retired section answered a real failure, and each failure now has a
different answer. The lead configuration existed so that an under-powered
session would not silently invent product semantics; that is now prevented by
the goal's own fixed decisions and reserved-decision split, and by the master
choosing the worker at dispatch with the task in front of it. The branch and
merge instructions existed so that a finished candidate would not sit unowned
or be merged by whoever happened to hold it; merges are now serialized through
one master under a written policy, with a `merge` event per merge. The
state-and-recovery section existed so that a handoff would not lose in-flight
work; continuity is now the master record's job, tested by the handoff
rehearsal and the `continuity` audit. What is left in the goal is what none of
those mechanisms can supply: the semantics, the fixed and reserved decisions,
and the evidence that completion is real. Creme requires a retired procedure to
name the failure it prevented and what prevents it now; this paragraph is that
statement for readers on any host, and the master logs the matching `procedure`
event.

Existing goal documents written in the fuller shape remain valid: a master
reads their lead-configuration, delegation, branch, and recovery sections as
advisory, and their identity, semantics, decisions, and acceptance sections as
binding.

## Quality rules

- Express outcomes rather than implementation theater. Commands are evidence,
  not the objective.
- Name negative controls for likely false positives: stale output, wrong root,
  missing discovery, disabled enforcement, unsupported OS, or an absent
  dependency.
- Separate public facts from current-host observations and private strategy.
- Give every required artifact a canonical owner and location.
- Keep repository technical truth with the repository that owns it.
- Treat estimates as measured ranges in comparable units; do not compare a
  serialized wall-clock observation with an idealized parallel sum.
- Never mark completion based on a plan, a silent command, or a green signal
  whose failure mode was not shown to bite.

The executing worker may adapt internal implementation, packet boundaries, and
cheap test selection. Changes to objective, public claim, security boundary,
supported platforms, license, or remote require the authority named in the
goal; a protected/default merge follows the master's merge policy in
[the master guide](master.md).
