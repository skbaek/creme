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
`python3 -m creme reclaim --wind-down GOAL`; this verifies that owned servers
are gone before releasing the goal hold. A bare release is not teardown
evidence. If reclamation is unavailable, keep the hold, checkpoint, and restart
the client instead.
