# Lean edit loops

Reference for two uncommon cases: a Lean file the language server cannot hold,
and measuring an edit loop. Ordinary proof work needs neither. What a proof
session needs is in the `lean-prover` skill; builds, admission, and wind-down
are in [execution](execution.md).

## Keep concrete machine images out of unfolding

Never let a concrete memory image, storage map, or long write chain reach
definitional unfolding or kernel evaluation: a kernel `decide` over concrete
writes, or a `set`/`rw`/`simp` whose defeq check unfolds a concrete `Mem` term,
drove single language-server workers and owned builds to 13–16.7 GiB on
2026-09-29 (Blanc creation-code proofs). State such facts symbolically instead
(size lemmas, read images, lemmas over a variable memory, an opaque `∃ post`),
and evaluate only small code windows. A missing import can hide the same
blow-up behind a slow elaboration instead of a fast error; check imports first
when a module's memory climbs.

## A file the language server cannot hold

Try the plain routes first: edit with a placeholder for the heavy term and
verify through a narrow owned build, or split the heavy check into its own
module. A faithful prefix, a copy of the module through one target declaration
with its suffix truncated, is for a file where repeated whole-module work stays
expensive after that. Its tools live under `.agents/skills/lean-prover/tools/`.

A fabricated prefix is evidence only after:

1. diagnostics agree over the preserved source range;
2. a source-level anchor produces the same proof state; and
3. the fabricated file has no collateral error outside that range.

`mk-prefix.py` truncation is the default. Splice mode is exceptional because a
local comparison can miss a broken suffix. `check-fidelity.py` requires an
explicit worktree; no developer path is inferred. At transfer time,
`check-drift.py --strict` proves the real context did not change.

To transfer, apply the candidate to the real file, check its goal and
diagnostics there, run the relevant cheap repository gate, and confirm
source/artifact freshness. Remove scratch outputs. Wind-down is the same as for
any Lean work; see [execution](execution.md#wind-down-lean-work).

## Measurement rules

Measure the complete interaction being optimized, keep success and failure
signatures explicit, separate startup from steady state, and repeat enough to
see host modes. Time the language server through its protocol completion—not
the first diagnostic notification or an un-timestamped client wrapper. Any
timing-authoritative run owns the hard semaphore and records host telemetry.

No committed resource ceiling or boundary claim may rest on language-server
output.

The measurement tools are instruments, not public constants. Repository- and
host-specific campaign values stay with reviewed evidence. A toolchain change
requires re-deriving protocol assumptions.
