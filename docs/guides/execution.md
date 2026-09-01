# Accountable execution

This guide is the shared lead method for substantial Jaune/Blanc work. The
named goal defines product semantics; sibling `scripts/GATES.md` files define
repository verification; Creme defines execution and evidence discipline.

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
~/creme/.semaphore/semaphore soft-acquire GOAL --note "build"
~/creme/.semaphore/semaphore renew GOAL
~/creme/.semaphore/semaphore soft-release GOAL
~/creme/.semaphore/semaphore hard-acquire GOAL --note "timing control"
~/creme/.semaphore/semaphore hard-release GOAL
```

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

On limited hosts, run one heavy operation at a time and checkpoint first.
Missing telemetry is not a pressure signal. Never edit semaphore state or use
a bare process kill.

## Wind down Lean work

Before yielding to a requested pause or restart, handing off the execution, or
reporting completion, every task that opened a Lean MCP server must use one
wind-down operation:

```sh
python3 -m creme reclaim --wind-down GOAL
```

If the direct host operation is sandbox-denied and `doctor` validates the
installed delegates, use the existing reclamation delegate:

```sh
~/.codex/bin/codex-reclaim-lean --wind-down GOAL
```

Wind-down holds the semaphore mutex across its full transaction. It refuses to
start while another label or a manual session holds the host; performs ordinary
ownership-verifying reclamation, never hard-pressure reclamation; verifies with
a fresh dry-run that no owned or protected Lean roots remain; and only then
removes the goal's soft or hard hold. Any unavailable scan, protected root,
survivor, failed verification, or semaphore state-write failure leaves the matching
hold intact. The operation is idempotent when the goal already has no hold and
the process scan is clear.

Ordinary `soft-release` and `hard-release` remain valid at intermediate
boundaries where retaining an MCP cache is intentional. They are not evidence
that a task is fully wound down. Do not report a Lean-using task safe, idle,
transferred, or complete until wind-down returns structured `OK`. If reclaim is
`UNAVAILABLE`, checkpoint and leave the hold intact while restarting the client
as directed by the capability result; do not substitute a platform command or
bare signal.

## Edit and verify

Choose the cheapest test that can falsify the current claim. Run from the
worktree under test and record exact command, exit status, relevant terminal
verdict, and commit. Do not weaken gates, baselines, manifests, budgets,
timeouts, allowlists, or generated artifacts to obtain green.

When a repository's gate catalogue defines content-addressed verdict reuse, a
checkpoint or merge candidate owes a **complete content-valid manifest**: each
catalogue row is freshly green or is backed by successful evidence with an
identical verdict-relevant identity. A routine draft push does not become an
all-fresh campaign merely because it is a push. Use an explicitly fresh run
when freshness itself is the subject under test or the named goal requires it.

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
state. Where the repository supports it, the verification evidence is a
complete content-valid manifest rather than a claim that every body happened
to re-execute. If a user-owned publication, license, or protected-merge gate
remains, the goal remains open.
