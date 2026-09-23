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

## What the goal does not carry

Lead, model, effort, delegation, branch, merge cadence, and state/recovery
locations belong to the master's brief ([briefs](briefs.md)) and merge policy
([master](master.md#merges)), not the goal; an older goal in the fuller shape
stays valid, with those sections read as advisory. The rationale for retiring
them is archived in
[`docs/archive/goal-pre-master.md`](../archive/goal-pre-master.md).

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
- Ask finite evidence (fixtures, differentials, oracles) only for what no
  theorem states: statement-to-spec fidelity, agreement with an external
  referent, and measurements. Never require finite coverage of a property a
  named theorem proves; see [evidence economy](master.md#evidence-economy).

The executing worker may adapt internal implementation, packet boundaries, and
cheap test selection. Changes to objective, public claim, security boundary,
supported platforms, license, or remote require the authority named in the
goal; a protected/default merge follows the master's merge policy in
[the master guide](master.md).
