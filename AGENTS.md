# Jaune/Blanc agent development from Creme

Creme is the public launch root for agent-assisted work on the sibling Jaune
and Blanc Lean projects. Start the agent with this repository as its project
and current working directory. Filesystem access to a sibling repository does
not load these instructions, skills, or MCP configuration.

## Workspace and authority

The default layout is one parent directory containing `creme/`, `jaune/`, and
`blanc/`. An ignored host profile may select another layout. Run
`python3 -m creme doctor` before relying on it. Then run
`python3 -m creme host-guidance`. If machine-local guidance is present, read
it in full before any Lean elaboration, build, timing run, or mutation
campaign; its safety constraints govern how repository-prescribed gates run on
this host.

- Creme owns reusable agent workflow, client shims, host capabilities, and the
  goal-execution method.
- Jaune and Blanc own their source architecture, technical doctrine, gate
  catalogues, and pass criteria. Read the affected repository's
  `scripts/GATES.md` before selecting or running a gate.
- A concrete goal, state brief, or report belongs in the configured goal store,
  which is optional and need not be Plans.
- The enduring master-session record is always `$GOAL_STORE/master/`, where
  the ignored host profile resolves `GOAL_STORE`. That whole runtime directory
  is host-local, Git-ignored, and never staged, committed, or pushed. Creme
  owns the client-neutral protocol, validation, and any reusable templates;
  it never tracks a host's board, log, intent, briefs, audits, paths, or
  capacity.
- Blanc consumes Jaune through its Git-pinned Lake dependency. Never replace
  that dependency with a sibling path or symlink.

If authorities conflict, platform safety and access constraints govern how work
is performed, repository gate catalogues govern their commands and verdicts,
and the named goal governs product semantics. Reconcile contradictions instead
of choosing the convenient source.

## The master role

One session at a time is the user's representative for all Jaune/Blanc work
on this host and holds the master lease. Every session launched with Creme as
its project resolves the configured goal store, verifies its `master/` runtime
directory is ignored and untracked, reads that record, and then tries to take
the lease at start, before anything else:
`~/creme/.semaphore/semaphore master-acquire --client codex --note "..."`
(substitute the actual client label).
`OK` makes it the master, and it says so in its first reply; a refusal for a
live lease makes it a reader, which says so in its first reply, naming the
holder, and may read, analyse, and converse but never writes
under `master/`, merges, pushes, spawns workers, or takes heavy goal holds; a
refusal for a lapsed or stranded lease is answered with `--take-over`. Read
`docs/guides/master.md` for the protocol. The master owns goals, briefs,
workers, merges, and pushes; the user owns the intent statements, the
reserved decisions, and independent audits. Workers are the master's own
subagents, each in a per-goal worktree, checkpointing to Git and a state
brief because they die with the master.

The default for any decision is decide and log. Only an irreversible external
commitment, a product-semantics fork the intent statements leave open, or a
decision an intent statement explicitly reserves goes to the user, and then as
a decision packet with a recommendation. Retiring a procedure requires a logged
`procedure` event naming the failure it prevented and what prevents that
failure now.

## Before changing a sibling

1. Confirm the client trusts Creme and has the required sibling read/write
   access. Do not work around a real permission boundary.
2. Read `docs/guides/execution.md` for substantial work and the named goal or
   the master's brief in full. A goal must have stable identity and status
   `ready`; a brief must name its objective, owned paths, gates, and report
   location.
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
There is one compilation owner: probe and build only through
`~/creme/scripts/creme lake-build GOAL -- <narrow-targets>`, never through bare
`lake build` or the removed MCP `lean_build`/`lean_profile_proof` tools. Keep narrow targets in the
proof loop, build the catalogue-required full target at checkpoints, and
refresh a file's language-server worker after a build that rebuilt something it
imports — query two other Lean files, then that file again, which evicts and
reloads it under `LEAN_LSP_MAX_OPEN_FILES=2`.

The inner loop is edit → `lean_diagnostic_messages` on the edited file →
`lean_goal`/`lean_hover_info` for a type mismatch → repeat. Build only when a
module is registered, an import changes, or a checkpoint or commit is due. A
clean diagnostics pass on a file whose imports are current is loop evidence
and does not need confirming with a build.

- Inspect the exact goal before editing. Verify names locally before broad
  search. Use `lean_multi_attempt` only when at least two genuine candidates
  remain unresolved; it evaluates every submitted candidate.
- Make the smallest coherent edit. After it, inspect the resulting goal and
  diagnostics. An empty goal is not success unless the cursor and artifacts
  are current and diagnostics are clean.
- Do not intentionally break a theorem to inspect state, and do not use a build
  as an inner proof-state loop. The command line belongs at loop boundaries.
- Let the wrapper classify: omit `--contention` and `--memory-gib` and it
  derives both from the probe's stale closure and the ledger's measured peaks
  of the modules in it — never from the name of the target list — keeping
  `sensitive` whenever the evidence is missing or drifted, sizing an
  unmeasured small stale set at the narrow default, and taking no hold when
  the probe reports everything current. State a class yourself when you know
  the build is cold or broad.
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
python3 -m creme memory-headroom
python3 -m creme telemetry
~/creme/.semaphore/semaphore status
~/creme/.semaphore/semaphore adaptive-acquire GOAL --note "proof loop" --memory-gib 8
python3 -m creme reclaim --dry-run
python3 -m creme reclaim --wind-down GOAL
```

The tracked `.semaphore/semaphore` launcher is the client-neutral coordination
entry point for Codex, Claude Code, future local agents, and humans. Invoke the
canonical `~/creme` launcher even while working in a per-goal worktree; it
resolves every worktree to the same ignored `.semaphore/state` directory.

If the client sandbox denies telemetry or reclamation, use only a generated
host delegate that `python3 -m creme doctor` reports as current:

```sh
~/.codex/bin/codex-host-telemetry
~/.codex/bin/codex-reclaim-lean --dry-run
~/.codex/bin/codex-reclaim-lean --wind-down GOAL
```

These stable approval targets dispatch back into the canonical Creme checkout.
Their generated Codex rules allow only telemetry, reclamation `--dry-run`, and
goal-scoped `--wind-down`; stronger reclamation remains approval-gated. Never
persist a prefix for a shell script under `/tmp`, a goal worktree, or another
sandbox-writable path. Repeated host execution needs a generated stable
delegate with an argument-rejecting surface. Never use a copied standalone
helper or a bundle that `doctor` marks stale; preview and regenerate the
complete delegates-and-rules set with `python3 -m creme host-wrappers`, then
fully restart Codex because rules load only at process startup.

If that bundle includes `codex-creme-contained-build`, use it for a host-
contained owned build instead of persistently approving `systemd-run`, a
current-checkout safe runner, or a temporary shell script. Its profile and goal
derive the worktree and its parser permits only probe/wait/exclusive build
options and Lake targets. A drift refusal means preview and reinstall the
bundle after review; never bypass the pin or downgrade its cgroup.

The capability contract and limited-mode results are in
`docs/capabilities.md`. A missing capability is not permission to run another
OS's command. Full telemetry may be unavailable while the narrower aggregate
memory-headroom probe still works. If headroom itself is unavailable, adaptive
admission serializes heavy work under a hard hold; it does not infer pressure.

Coordinate every memory-heavy Lean unit with the adaptive semaphore described
in `docs/guides/execution.md`. Supply a conservative whole-GiB peak estimate,
or let the owned-build wrapper derive one from measurement. Use
`contention=sensitive` for cold or unexpectedly broad rebuilds, multiple Lean
workers, known memory spikes, and any operation whose concurrency could make
the host unresponsive even when results remain valid; use `exclusive` for
timing, whole-tree, mutation, and dependency-census work. A non-elaborating
fixture or static gate is light and needs no hold at all. The decision may
admit soft, admit hard, or refuse with `DEFER_FOR_HARD`/`LIGHT_ONLY`. Never
downgrade it.

When a refusal is one another session will lift, add `--wait SECS` to
`adaptive-acquire` or `creme lake-build`: the request queues under the same
mutex and returns admitted, timed out, or refused for a reason waiting cannot
change. Never write a shell loop around `semaphore status`. On a refusal that
waiting cannot fix, reorder and do light work rather than starting the command
uncoordinated. A hold covers one elaborating command; release between gates.

Run `semaphore renew GOAL` before each further heavy unit and at least every
five minutes in an interactive proof session. `YIELD_HEAVY` gives the older
soft holder priority; `DRAIN_HEAVY` means start no further Lean action,
checkpoint, and wind down. Classify a long indivisible command conservatively
before it starts because renewal cannot interrupt an already-running command.
Never edit semaphore state, use a bare `kill`, or treat a quiet-looking
snapshot as a lease.

Any task that opened a Lean MCP server must run `reclaim --wind-down GOAL`
before yielding to a requested pause or restart, transferring the task, or
reporting completion. The command verifies reclamation before atomically
releasing that goal's hold. A bare soft/hard release is an intermediate
operation, not wind-down evidence; never claim the task is safe or idle after
Lean work unless wind-down reports `OK`.

Wind-down is scoped to the configured Jaune/Blanc `.worktrees/GOAL` roots.
Other goal labels may remain while it reclaims and releases only the caller's
work. An unresolved worktree or uninspectable same-client Lean candidate fails
closed before signalling; never bypass that refusal with another owner's
release or a bare kill.

The ignored `.creme/host-guidance.md` records hazards and safe command paths
learned on this particular machine. It does not change Jaune or Blanc pass
criteria. If it marks a raw command unsafe, use its local safe wrapper while
preserving the gate's authoritative final command and verdict.

## Git and completion

Preserve unrelated work. Inspect the complete diff and status, stage explicit
owned paths, commit coherent green checkpoints, and push dedicated
non-protected branches when authorized. Never rewrite history or force-push.
Merging to or pushing a protected or default branch belongs to the master
under the merge policy in `docs/guides/master.md`; a worker never does it.
Publication surfaces and licenses remain user decisions.

Completion means every mandatory goal condition maps to inspectable evidence
on the exact candidate. Update the state brief at green boundaries and write a
final report; a completed plan is not by itself a completed goal.
