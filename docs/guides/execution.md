# Accountable execution

This guide is the shared lead method for substantial Jaune/Blanc work. The
named goal defines product semantics; sibling `scripts/GATES.md` files define
repository verification; Creme defines execution and evidence discipline.

## Start and reconcile

Read the goal and relevant authorities in full. Confirm stable goal identity,
`ready` status, explicit completion criteria, current repository commits,
dirty-tree ownership, and open user decisions. Create per-goal branches and
worktrees; never repurpose shared main clones or overwrite unrelated changes.

Write a compact state brief for multi-session work. It should name exact
commits, owned paths, last green evidence, active packets, open decisions, and
the next coherent unit. State may live in any configured goal store; it never
becomes a hidden dependency of the public workflow.

## Delegate by ownership

Delegate only bounded packets with disjoint file ownership, stated authority,
resource class, required checks, and a clear return contract. The lead retains
integration, conflict reconciliation, final verification, and user-only
decisions. Parallel work is useful only when it leaves evidence that can be
merged without overlapping authority.

## Resource classes

- Light: inventories, docs, static scans, link checks, and unit tests without
  Lean elaboration. No hold.
- Elaboration: Lean MCP sessions, builds, and compiling gates. Take a soft hold.
- Exclusive: timing, whole-tree sweeps, and mutation campaigns. Take the hard
  hold and verify the host is quiet.

```sh
python3 -m creme semaphore soft-acquire GOAL --note "build"
python3 -m creme semaphore renew GOAL
python3 -m creme semaphore soft-release GOAL
python3 -m creme semaphore hard-acquire GOAL --note "timing control"
python3 -m creme semaphore hard-release GOAL
```

On a Codex host where the sandbox denies the direct command, first require
`python3 -m creme doctor` to validate the installed delegates, then substitute
the exact stable path without changing the action or arguments:

```sh
~/.codex/bin/codex-host-semaphore soft-acquire GOAL --note "build"
~/.codex/bin/codex-host-semaphore renew GOAL
~/.codex/bin/codex-host-semaphore soft-release GOAL
```

The delegate is an approval boundary, not another semaphore implementation.
Never use one that `doctor` reports as partial or stale. Preview and regenerate
the full set with `python3 -m creme host-wrappers` rather than copying a helper.

On limited hosts, run one heavy operation at a time and checkpoint first.
Missing telemetry is not a pressure signal. Never edit semaphore state or use
a bare process kill.

## Edit and verify

Choose the cheapest test that can falsify the current claim. Run from the
worktree under test and record exact command, exit status, relevant terminal
verdict, and commit. Do not weaken gates, baselines, manifests, budgets,
timeouts, allowlists, or generated artifacts to obtain green.

For a non-vacuity or enforcement claim, show all three controls: the surrounding
tree still works, the control fails at the intended boundary, and removing only
that control restores green. Use disposable worktrees for destructive
mutations. Preserve source artifacts and fail on stale generated output.

Inspect the full diff and status, stage only owned paths, commit coherent green
checkpoints, and push only an authorized non-protected branch. Never
force-push. Default/protected branch merges remain user-owned unless the goal
explicitly authorizes a named exception.

## Context and completion

Handoff when crossing a clean expertise or self-hosting boundary, when a hard
resource trigger requires restart, or when context no longer supports the next
coherent unit. Record what was verified rather than relying on client memory.

Completion is a condition-to-evidence proof on one exact candidate, not a
completed task list. Re-run drift-prone checks, close independent review
findings, account for compatibility paths, and report branch/worktree/push
state. If a user-owned publication, license, or protected-merge gate remains,
the goal remains open.
