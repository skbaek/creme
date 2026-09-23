# Archived: pre-master goal-guide prose

Before the master role (2026-09-04) a goal document also configured the
execution lead and the mechanics of running the work; `docs/guides/goal.md`
then spent two sections explaining what goals no longer carried and why. On
2026-09-23 (goal `execution-guide-refocus-v1`, the pre-master archive pass of
the capability-drift review) those sections were reduced to a short pointer.
The removed prose is kept below verbatim from Creme commit `9a39ebe`
(`git show 9a39ebe:docs/guides/goal.md` has the exact prior bytes). It is
history, not instructions.

````markdown
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
````
