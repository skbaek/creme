# Archived: pre-master execution-guide prose

`docs/guides/execution.md` was written on 2026-08-31 as the method for a single
lead agent, before the master role (2026-09-04) took over goals, briefs,
workers, and merges. On 2026-09-23 (goal `execution-guide-refocus-v1`, slate
item 6 of the capability-drift review) it was refocused as the worker's
Lean-and-build contract, because the lead-era sections duplicated
`docs/guides/master.md` and `docs/guides/briefs.md` and cost every worker the
reading without preventing anything. The prose removed then is kept below
verbatim, grouped by where it stood, from Creme commit `9a39ebe`
(`git show 9a39ebe:docs/guides/execution.md` has the exact prior bytes). It is
history, not instructions: nothing here is current policy.

## Original introduction

````markdown
This guide is the shared lead method for substantial Jaune/Blanc work. The
named goal defines product semantics; sibling `scripts/GATES.md` files define
repository verification; Creme defines execution and evidence discipline. The
layer above it — one master session that owns goals, workers, merges, and
pushes on a host — is [the master guide](master.md).
````

## Lead-era sections

````markdown
## Start and reconcile

Read the goal and relevant authorities in full. Confirm stable goal identity,
`ready` status, explicit completion criteria, current repository commits,
dirty-tree ownership, and open user decisions. Create per-goal branches and
worktrees; never repurpose shared main clones or overwrite unrelated changes.
A worktree for the repository at `PATH` belongs in `PATH/.worktrees/<goal>`,
ignored by that repository. Keeping it inside the repository it belongs to
assumes nothing about the surrounding layout and needs no access beyond the
repository already in use.

Write a compact state brief for multi-session work. It should name exact
commits, owned paths, last green evidence, active packets, open decisions, and
the next coherent unit. State may live in any configured goal store; it never
becomes a hidden dependency of the public workflow.

Maintain that brief by replacement at checkpoints, following
[compact continuity](master.md#compact-continuity). Keep detailed commands and
verdicts in evidence and link them; retain all open obligations and qualifying
failures. Do not prepend repeated historical status to current documents.

Before launching the planned units, apply the [escalation procedure](escalation.md)
to their access and capability needs as a group. Preserve the resulting coverage
inventory in the state brief and update it when operations or policy change.

## Delegate by ownership

Delegate only bounded packets with disjoint file ownership, stated authority,
resource class, required checks, and a clear return contract; under the master
model each packet is a written brief, per [the briefs guide](briefs.md). The lead retains
integration, conflict reconciliation, final verification, and user-only
decisions. Parallel work is useful only when it leaves evidence that can be
merged without overlapping authority. The tracked
[generic worker brief](../../templates/master-runtime/worker-brief.md) lists
the fields a packet may need; fill the ones that apply and leave general
policy in the guides. A filled brief is private runtime state; the tracked
template remains placeholders only.
````

## Pre-master semaphore compatibility commands (from "Resource classes")

````markdown
The explicit `soft-acquire` and `hard-acquire` compatibility commands are also
pressure-gated; they cannot bypass a low-memory refusal. `release` removes
whichever hold kind adaptive admission selected. Explicit soft/hard releases
remain available for compatibility.
````

## Incident note (from "Launch and watch")

````markdown
In B11 a session read a derived 12 GiB as "an explicit --memory-gib
12" and escalated the wrong cause twice; the line now says which it is.
````

## Duplicated estimate advice (from "Launch and watch"; kept under "One compilation owner")

````markdown
A larger estimate than the evidence supports still only
makes a request harder to schedule, so state one only when you know it.
````

## Launcher and state migration (canonical homes: AGENTS.md, docs/setup.md, docs/capabilities.md)

````markdown
The launcher is tracked in the canonical Creme checkout and is shared by Codex,
Claude Code, other local agents, and humans. Always use the canonical launcher,
not a copy inside a goal worktree; linked worktrees resolve back to its single
ignored `.semaphore/state` directory.

On an upgraded host, `migrate-state` copies validated live holds under the old
and new mutexes, activates `.semaphore/state`, and leaves the legacy files
untouched. Run it once from a trusted human shell after the neutral-semaphore
change is deployed:

```sh
~/creme/.semaphore/semaphore migrate-state
```

Retire any pre-neutral delegate and legacy state only after every session
launched before the cutover has wound down.
````

## Compatibility releases (from "Wind down Lean work")

````markdown
Ordinary `soft-release` and `hard-release` remain valid at intermediate
boundaries where retaining an MCP cache is intentional.
````

## Lead-era hand-off rule (from "Context and completion"; now per client in docs/guides/briefs.md)

````markdown
## Context and completion

Handoff when crossing a clean expertise or self-hosting boundary, when a hard
resource trigger requires restart, or when context no longer supports the next
coherent unit. Record what was verified rather than relying on client memory.
````
