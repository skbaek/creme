# Jaune/Blanc agent development from Creme

Creme is the public launch root for agent-assisted work on the sibling Jaune
and Blanc Lean projects. Start the agent with this repository as its project
and current working directory. Filesystem access to a sibling repository does
not load these instructions, skills, or MCP configuration.

This file is loaded by every session of every client, so it holds only what
every session needs. The guides it links hold the rest; read a guide when its
subject comes up, not at startup.

## Workspace and authority

The default layout is one parent directory containing `creme/`, `jaune/`, and
`blanc/`. An ignored host profile may select another layout. Run
`python3 -m creme doctor` before relying on it. Before any Lean elaboration,
build, timing run, or mutation campaign, read `python3 -m creme host-guidance`
in full: it is the short current contract for this machine, and its safety
constraints govern how repository-prescribed gates run here.

- Creme owns reusable agent workflow, client shims, host capabilities, and the
  goal-execution method.
- Jaune and Blanc own their source architecture, technical doctrine, gate
  catalogues, and pass criteria. Read the affected repository's
  `scripts/GATES.md` before selecting or running a gate.
- Goals, state briefs, and reports belong in the configured goal store. The
  master record is `$GOAL_STORE/master/`: host-local, Git-ignored, and never
  staged, committed, or pushed.
- Blanc consumes Jaune through its Git-pinned Lake dependency. Never replace
  that dependency with a sibling path or symlink.

If authorities conflict, platform safety and access constraints govern how work
is performed, repository gate catalogues govern their commands and verdicts,
and the named goal governs product semantics. Reconcile contradictions instead
of choosing the convenient source.

## The master role

One session at a time is the user's representative for all Jaune/Blanc work
on this host and holds the master lease. A session does not enter the role on
its own: until the user explicitly directs it to start as master it is an
ordinary session with a reader's limits. It may read, analyse, converse, and do
what the user asks, but it never writes under `master/`, merges or pushes a
default branch, spawns workers, or takes heavy goal holds; if a request needs
one of those, it says so and asks. Workers and pseudo-subagents never enter the
role. On the user's direction, follow
[master entry](docs/guides/master.md#master-entry-master-or-reader) and say in
the reply whether the session became the master or a reader.

The master owns goals, briefs, workers, merges, and pushes; the user owns the
intent statements, the reserved decisions, and independent audits. The default
for any decision is decide and log. Only an irreversible external commitment,
a product-semantics fork the intent statements leave open, a decision an intent
statement reserves, or an integrity/provenance change the master guide
classifies as reserved goes to the user, as a decision packet with a
recommendation.

## Before changing a sibling

1. Confirm the client has the required sibling read/write access. Do not work
   around a real permission boundary.
2. Read the named goal or the master's brief in full. A goal must have stable
   identity and status `ready`; a brief names its objective, owned paths,
   gates, and report location.
3. Use per-goal worktrees at `PATH/.worktrees/<goal>` for the repository at
   `PATH`, with `/.worktrees/` in its `.gitignore`. Shared main clones stay on
   their default branches.
4. Read Jaune's or Blanc's `scripts/GATES.md` before editing or testing there.
5. For Blanc, follow its `docs/COMMON_API.md` and `docs/PROOF_RECIPES.md`;
   generic-shaped definitions, lemmas, tactics, and instances go through its
   common-library-first workflow.

Muse sessions only: the edit/create tools admit only this workspace, so edit a
sibling with `scripts/muse-edit-file` (see `--help`).

## Lean proof work

Use the `lean-inspector` skill for proof-state analysis and `lean-prover` when
writing or repairing a proof; they carry the inner loop. There is one
compilation owner: build only through
`~/creme/scripts/creme lake-build GOAL -- <narrow-targets>`, never bare
`lake build`; the wrapper sizes and admits the build. If MCP is unavailable or
stale, repair it rather than editing blind.

## Verification and evidence

Run commands from the repository worktree under test and record exact commands
and terminal verdicts. Never weaken, silently rebase, or hand-edit a baseline,
budget, manifest, allowlist, generated artifact, timeout, or golden merely to
make a gate green. Generated artifacts come only from the registered
generators. Pin/reference movements, weakened baselines or budgets, allowlist
growth, goldens, timeouts, publication, public claims or counts, licenses,
external messages, spend, and dependent public contracts are never autonomous;
see the master guide's registered-provenance rule.

A control must be shown to bite: the surrounding tree still builds, failure
lands at the control, and removing only the control restores green. Run
mutation campaigns in disposable worktrees.

## Host coordination

Every memory-heavy Lean unit is admitted by the host semaphore
(`~/creme/.semaphore/semaphore`, the canonical launcher even from a worktree);
the owned-build wrapper does this for builds. The rules — `adaptive-acquire`,
the `DEFER_FOR_HARD`/`LIGHT_ONLY` refusals, `--wait` instead of a shell loop,
`YIELD_HEAVY`/`DRAIN_HEAVY`, renewal, contention classes — are in
[execution](docs/guides/execution.md#resource-classes). On a refusal that
waiting cannot fix, do light work instead; never downgrade a decision, edit
semaphore state, or use a bare `kill`.

Any task that opened a Lean MCP server runs
`python3 -m creme reclaim --wind-down GOAL` before yielding to a requested
pause or restart, transferring the task, or reporting completion, and never
claims to be idle after Lean work unless it reports `OK`. A bare hold release
is not wind-down evidence.

Separate task authorization, client access, host admission, and repository
evidence restrictions using [the escalation procedure](docs/guides/escalation.md).
A missing capability is not permission to run another OS's command; see
`docs/capabilities.md`. Codex sessions use the generated host delegates in
[Codex approvals](docs/guides/codex-approvals.md#host-delegates).

Luna reserve pseudo-subagents are used only when the user instructs it,
through `python3 -m creme luna-reserve`; read
[the Luna reserve guide](docs/guides/luna-reserve.md) first.

## Git and completion

Preserve unrelated work. Inspect the complete diff and status, stage explicit
owned paths, and commit coherent green checkpoints. Never rewrite history or
force-push. Merging to or pushing a default branch belongs to the master; a
worker returns local commits. Publication surfaces and licenses remain user
decisions.

Completion means every mandatory goal condition maps to inspectable evidence
on the exact candidate. Update the state brief at green boundaries and write a
final report; a completed plan is not by itself a completed goal.
