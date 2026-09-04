# Lean edit loops

Most Lean edits should be made directly after inspecting the exact goal, then
verified with a current goal and clean diagnostics. Do not build a scratch
loop merely because one exists.

## Choose the loop before editing

Use the persistent Lean language server for goal inspection and ordinary proof
repair. Use a one-shot CLI check at loop boundaries or when the result being
tested is explicitly command-line behavior. Use a fabricated prefix only when
repeated whole-module work is genuinely expensive and the target's preserved
environment can be proven faithful.

### The loop is edit, diagnostics, goal — not edit, build

The default inner loop never builds:

1. edit;
2. `lean_diagnostic_messages` on the edited file — it reports every error in
   the file at once, where a build reports the first and stops;
3. `lean_goal` at the tactic and `lean_hover_info` on the symbol for a type
   mismatch, which is the most common error and the one a build explains
   worst;
4. repeat from the exact position of whatever remains.

Build only when a module is registered or an import changes, at a checkpoint
or commit, or when the repository catalogue requires it. **A clean
`lean_diagnostic_messages` pass on a file whose imports are current is loop
evidence**; it does not need confirming with a build, and confirming it with
one costs a hold that another session is waiting for.

There is one compilation owner. When diagnostics report stale imports, do not
ask the language server or MCP to build: `lean_build` and the shelling proof
profiler are deliberately absent.
From the goal worktree, probe the narrow target with `~/creme/scripts/creme
lake-build GOAL --probe -- TARGET`; on exit 3, run the same narrow target
through the admitted wrapper, refresh the file's worker, and recheck the exact
goal and diagnostics. Refreshing is two tool calls: query two other Lean files,
then the file you care about; under `LEAN_LSP_MAX_OPEN_FILES=2` that evicts its
worker and reloads the rebuilt `.olean`s. Editing the file's own body does not
do it, and neither does `reclaim --idle-workers`, which frees the memory while
the MCP layer keeps answering from its cache. The wrapper prints the modules it
rebuilt on its own `restart:` line; a stale `.olean` left by a neighbour's
build is the usual reason diagnostics stop looking trustworthy. Use the repository catalogue's full
target at a green checkpoint, also through the wrapper. Never use bare `lake
build` in either loop.

Two failing builds of the same targets in a row raise `hint: REPEAT_FAIL` in
the wrapper's JSON. It never refuses the build; it names the diagnostics tool
that would have shown every error in one pass.

Before opening or advancing an elaborating loop, obtain adaptive admission
with an honest peak estimate — or omit `--contention` and `--memory-gib` and
let the wrapper derive both from the probe's stale closure and the ledger's
measured peaks. When something else holds the host, add `--wait SECS` and let
the request queue: one call, in arrival order, no `semaphore status` loop.
Renew before each next proof attempt or build boundary and at least every five
minutes. `YIELD_HEAVY` or `DRAIN_HEAVY` ends the heavy loop at the current
safe boundary: checkpoint, wind down, then work on non-elaborating inventory,
prose, or static analysis. A cold/broad rebuild or indivisible spike is
contention-sensitive and should request a hard hold before it starts. A hold
covers one elaborating command, not a whole gate script; release between gates
and reacquire.

The `lean-prover` skill carries the operational proof workflow. Its tools live
under `.agents/skills/lean-prover/tools/`.

## Fidelity before speed

A fabricated prefix is evidence only after:

1. diagnostics agree over the preserved source range;
2. a source-level anchor produces the same proof state; and
3. the fabricated file has no collateral error outside that range.

`mk-prefix.py` truncation is the default. Splice mode is exceptional because a
local comparison can miss a broken suffix. `check-fidelity.py` requires an
explicit worktree; no developer path is inferred. At transfer time,
`check-drift.py --strict` proves the real context did not change.

## Measurement rules

Measure the complete interaction being optimized, keep success and failure
signatures explicit, separate startup from steady state, and repeat enough to
see host modes. Time the language server through its protocol completion—not
the first diagnostic notification or an un-timestamped client wrapper. Any
timing-authoritative run owns the hard semaphore and records host telemetry.

The measurement tools are instruments, not public constants. Repository- and
host-specific campaign values stay with reviewed evidence. A toolchain change
requires re-deriving protocol assumptions.

## Transfer and teardown

Make the smallest real-file edit, inspect the resulting goal and diagnostics,
run the relevant cheap repository gate, and confirm source/artifact freshness.
Remove scratch outputs. Ordinary hold releases may retain a useful server at an
intermediate boundary. Before pause, restart, transfer, or completion, use
`python3 -m creme reclaim --wind-down GOAL`; this verifies that servers inside
the configured per-goal worktree are gone before releasing only the matching
hold. Other goals' holds and servers do not block or join that scoped
transaction. A bare release is not teardown evidence. If reclamation is
unavailable, keep the hold, checkpoint, and restart the client instead.
