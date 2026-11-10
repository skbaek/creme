---
name: lean-prover
description: Writes, finishes, and repairs Lean 4 proofs with a language-server feedback loop. Use when asked to prove, finish, solve, refactor, or fill a lemma, theorem, tactic block, or `sorry`.
---

# Lean 4 proof work

How you reach a proof is your call: how much to write before checking it,
which tool checks it, and whether a candidate is tried in the file, at a
position, or in a scratch module. This skill holds only what nothing else tells
you at the moment it matters: what a check costs on a shared host, and what its
answer is worth as evidence. What a tool's result means is in that tool's
description, and the build wrapper explains its own output.

## What a check costs

- Every tool call replays the session's whole context, so a turn costs more
  than the seconds its tool takes, and more the longer the session has run.
- `lean_diagnostic_messages` returns every error in a file in seconds and takes
  no host hold.
- A build takes a host hold that another session may be waiting for, and it
  checks nothing downstream of a module that fails. While a file's imports are
  current, diagnostics answer the same question sooner; a build is needed when
  a module is registered, an import changes, or a checkpoint or commit is due.
- A failed tactic alternative still incurs elaboration work. When alternatives
  would repeatedly unify against a large term, goal-directed dispatch is
  cheaper.
- In Blanc, a raised heartbeat or recursion ceiling is rejected by the
  proof-debt gate, so a slow proof has to be made cheaper; the
  proof-performance conventions in its README say how.

## What an answer is worth

A clean `lean_diagnostic_messages` pass on a file whose imports are current is
loop evidence. It is not checkpoint evidence: on Lean 4.34 the server has
reported a module clean that the build then failed. A checkpoint or commit
needs the module's narrow owned build green, run from the goal worktree:

```sh
~/creme/scripts/creme lake-build GOAL --probe -- TARGET       # exit 0 current, exit 3 stale
~/creme/scripts/creme lake-build GOAL --wait SECS -- TARGET   # queues behind other sessions' holds
```

Verdicts, classes the wrapper cannot derive, walks, background rebuilds after
shared edits, and landing builds are in
[execution](../../../docs/guides/execution.md#one-compilation-owner).

## What the language server cannot hold

The host semaphore never admits the language server, so what a file costs in
memory is its editor's to watch. A worker past the host's ceiling is
[stopped](../../../docs/guides/execution.md#language-server-worker-watchdog),
and the elaboration it was running is lost.

- A kernel `decide` or `rfl` over a large artifact, or a `set`, `rw`, or `simp`
  whose definitional check unfolds a concrete memory image, storage map, or
  long write chain, has driven single workers past 13 GiB. State such facts
  symbolically, or edit with a placeholder and verify through a narrow build;
  see [machine images](../../../docs/guides/lean-edit-loops.md#keep-concrete-machine-images-out-of-unfolding).
- A missing import can hide the same growth behind a slow elaboration instead
  of a fast error.
- For a file the server cannot hold at all, the tools in `tools/` beside this
  file fabricate a faithful truncated copy; the same guide says when a verdict
  from one counts.
