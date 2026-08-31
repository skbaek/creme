# Jaune/Blanc agent development from Creme

Creme is the public launch root for agent-assisted work on the sibling Jaune
and Blanc Lean projects. Start the agent with this repository as its project
and current working directory. Filesystem access to a sibling repository does
not load these instructions, skills, or MCP configuration.

## Workspace and authority

The default layout is one parent directory containing `creme/`, `jaune/`, and
`blanc/`. An ignored host profile may select another layout. Run
`python3 -m creme doctor` before relying on it.

- Creme owns reusable agent workflow, client shims, host capabilities, and the
  goal-execution method.
- Jaune and Blanc own their source architecture, technical doctrine, gate
  catalogues, and pass criteria. Read the affected repository's
  `scripts/GATES.md` before selecting or running a gate.
- A concrete goal, state brief, or report belongs in the configured goal store,
  which is optional and need not be Plans.
- Blanc consumes Jaune through its Git-pinned Lake dependency. Never replace
  that dependency with a sibling path or symlink.

If authorities conflict, platform safety and access constraints govern how work
is performed, repository gate catalogues govern their commands and verdicts,
and the named goal governs product semantics. Reconcile contradictions instead
of choosing the convenient source.

## Before changing a sibling

1. Confirm the client trusts Creme and has the required sibling read/write
   access. Do not work around a real permission boundary.
2. Read `docs/guides/execution.md` for substantial work and the named goal in
   full. A goal must have stable identity and status `ready`.
3. Use per-goal worktrees. A worktree for the repository at `PATH` belongs
   in `PATH/.worktrees/<goal>`, which assumes nothing about the layout above
   that repository and needs no write access outside it; add `/.worktrees/`
   to that repository's `.gitignore`. Shared main clones stay on their
   default branches.
4. Read Jaune's or Blanc's `scripts/GATES.md` before editing or testing there.
5. For Blanc, follow `docs/COMMON_API.md` and `docs/PROOF_RECIPES.md` in the
   Blanc repository. Generic-shaped definitions, lemmas, tactics, and instances
   go through Blanc's common-library-first and discoverability workflow.

## Lean proof work

Use the `lean-inspector` skill for proof-state analysis and `lean-prover` when
writing, finishing, or repairing a proof. Skills live in `.agents/skills/`.

- Inspect the exact goal before editing. Verify names locally before broad
  search. Use `lean_multi_attempt` only when at least two genuine candidates
  remain unresolved; it evaluates every submitted candidate.
- Make the smallest coherent edit. After it, inspect the resulting goal and
  diagnostics. An empty goal is not success unless the cursor and artifacts
  are current and diagnostics are clean.
- Do not intentionally break a theorem to inspect state, and do not use a build
  as an inner proof-state loop. The command line belongs at loop boundaries.
- If MCP is unavailable or stale, repair or restart it. Do not substitute blind
  edits. Follow `docs/guides/lean-edit-loops.md` for the exceptional iterative
  cases.
- In Blanc, consult `docs/PROOF_RECIPES.md` before a manual multi-step walk or
  inversion, and read the proof-performance conventions before raising a
  resource ceiling or building a large walk.

## Verification and evidence

Run commands from the repository worktree under test and record exact commands
and terminal verdicts. Never weaken, silently rebase, or hand-edit a baseline,
budget, manifest, allowlist, generated artifact, timeout, or golden merely to
make a gate green.

Jaune's authoritative catalogue is `../jaune/scripts/GATES.md`; Blanc's is
`../blanc/scripts/GATES.md`. Use their current selection, concurrency, runtime,
and pass rules rather than copied summaries. Generated artifacts come only
from the registered generators.

A control must be shown to bite: the surrounding tree still builds, failure
lands at the control, and removing only the control restores green. Run
mutation campaigns in disposable worktrees.

## Host capabilities and coordination

Shared workflow calls Creme capabilities; it does not invoke platform binaries
directly. Use:

```sh
python3 -m creme platform
python3 -m creme telemetry
python3 -m creme semaphore status
python3 -m creme reclaim --dry-run
python3 -m creme reclaim --wind-down GOAL
```

If the client sandbox denies a host operation, use only a generated delegate
that `python3 -m creme doctor` reports as current:

```sh
~/.codex/bin/codex-host-telemetry
~/.codex/bin/codex-host-semaphore status
~/.codex/bin/codex-reclaim-lean --dry-run
~/.codex/bin/codex-reclaim-lean --wind-down GOAL
```

These stable approval targets dispatch back into the canonical Creme checkout.
Never use a copied standalone helper or a delegate that `doctor` marks stale;
preview and regenerate the complete set with `python3 -m creme host-wrappers`.

The capability contract and limited-mode results are in
`docs/capabilities.md`. A missing capability is not permission to run another
OS's command. Missing telemetry reduces concurrency and strengthens
checkpointing; it is not evidence that the host is under pressure.

Coordinate memory-heavy Lean work with the host semaphore described in
`docs/guides/execution.md`: builds and elaboration use a soft hold; timing,
whole-tree, and mutation work use the exclusive hard hold. Never edit semaphore
state, use a bare `kill`, or treat a quiet-looking snapshot as a lease.

Any task that opened a Lean MCP server must run `reclaim --wind-down GOAL`
before yielding to a requested pause or restart, transferring the task, or
reporting completion. The command verifies reclamation before atomically
releasing that goal's hold. A bare soft/hard release is an intermediate
operation, not wind-down evidence; never claim the task is safe or idle after
Lean work unless wind-down reports `OK`.

## Git and completion

Preserve unrelated work. Inspect the complete diff and status, stage explicit
owned paths, commit coherent green checkpoints, and push dedicated
non-protected branches when authorized. Never rewrite history or force-push.
Merging a protected or default branch remains user-owned unless a named goal
explicitly says otherwise.

Completion means every mandatory goal condition maps to inspectable evidence
on the exact candidate. Update the state brief at green boundaries and write a
final report; a completed plan is not by itself a completed goal.
