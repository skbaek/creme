---
name: lean-prover
description: Writes, finishes, and repairs Lean 4 proofs with a language-server feedback loop. Use when asked to prove, finish, solve, refactor, or fill a lemma, theorem, tactic block, or `sorry`.
---

# Lean 4 interactive prover

Use `lean-lsp-mcp` throughout. Never edit blindly or introduce invalid syntax
to force compiler feedback about a proof (see `AGENTS.md` for the one narrow
exception: a control whose subject is the tooling itself).

## The loop, in one place

```
edit
  → lean_diagnostic_messages on the edited file      (every error, at once)
  → lean_goal / lean_hover_info for a type mismatch  (what the types are)
  → repeat from the exact position of what remains
```

Build **only** when a module is registered, an import changes, or a checkpoint
or commit is due — not to find out whether an edit compiles. A build reports
the first error and stops; `lean_diagnostic_messages` reports the whole file.
Rebuilding after each edit turns the compiler into a slow error enumerator and
takes a host hold that another session is waiting for.

**A clean `lean_diagnostic_messages` pass on a file whose imports are current
is loop evidence.** Within the loop it is complete on its own: do not follow
each edit with a build "to confirm for real". A checkpoint or commit is
different: it needs the module's narrow owned build green, because on Lean 4.34
the server has reported a module clean that the build then failed. If the diagnostics say `Imports are out of date`, the
imports are not current: probe, build the narrow target, refresh the file's
worker — call any lean tool on two other Lean files and then on this one, which
evicts and reloads it under `LEAN_LSP_MAX_OPEN_FILES=2`; editing the file does
not reload its imports — and read the diagnostics again. That is the repair, and a stale import is the usual reason a
clean pass looked untrustworthy.

## 1. Establish a baseline

- Locate the exact target and capture `lean_diagnostic_messages` before editing.
- Query `lean_goal` at the start of the target tactic or `sorry`. Lines and
  columns are 1-indexed.
- For term-mode holes, use `lean_term_goal`.
- If the guard surfaces `Imports are out of date`, keep compilation ownership
  explicit: run `~/creme/scripts/creme lake-build GOAL --probe -- TARGET` from the
  goal worktree. Exit 3 means stale; run the narrow target through the same
  command without `--probe`, then refresh the file's worker as above and
  repeat the goal and diagnostics checks. `lean_build` is intentionally unavailable and bare
  `lake build` is prohibited. `lean_profile_proof` is also unavailable because
  it shells to unowned compilation. Narrow targets belong in the loop; the
  repository catalogue's full target belongs at green checkpoints and still
  runs through the wrapper.
- Omit `--contention` and `--memory-gib`: the wrapper derives both from the
  probe's stale closure and the ledger's measured peaks, and keeps `sensitive`
  whenever that evidence is missing or has drifted. Pass a class yourself only
  when you know something it cannot — a cold worktree, an expected broad
  rebuild, a command that spawns several workers.
- If another session holds the host, add `--wait SECS` and let the request
  queue; it returns admitted, `WAIT_TIMEOUT`, or refused for a reason waiting
  cannot change. Never write a shell loop around
  `~/creme/.semaphore/semaphore status`.
- The wrapper prints the modules it rebuilt on its own `restart:` line. Before
  trusting diagnostics in a file that imports one of them, refresh its worker
  as above; `reclaim --idle-workers` frees memory without refreshing anything.
- `hint: REPEAT_FAIL` in the wrapper's JSON means the previous build of these
  same targets also failed recently. Nothing is refused; it is telling you the
  next error was already visible in `lean_diagnostic_messages`.

## 2. Explore without modifying the file

- Verify every proposed local declaration with `lean_local_search` before use.
  Use `lean_leansearch`, `lean_loogle`, or other Lean search tools only when
  local search is insufficient.
- Use `lean_multi_attempt` only to compare at least two genuinely unresolved
  candidates after inspecting the exact goal. If a closing lemma or tactic is
  already known, apply it as the smallest coherent edit and recheck it instead.
  Do not pad the snippets list to satisfy candidate-count guidance: the tool
  evaluates every submitted snippet and does not stop after the first success.
- Consult [the proof checklist](resources/proof-checklist.md) when choosing
  between proof strategies or before finalizing a nontrivial proof.
- In Blanc, before beginning a manual multi-step walk or inversion, run
  `blanc_suggest` at the goal or consult the sibling repository's
  `<workspace-root>/blanc/docs/PROOF_RECIPES.md`;
  heed the non-applicability boundary before selecting the route.
- In Blanc, a needed declaration with a generic shape — one that nowhere
  mentions the contract being worked on — falls under the common-library-first
  workflow in `<workspace-root>/blanc/docs/COMMON_API.md`: search shared modules first; use
  or generalize what exists; hoist a contract-local original before using it;
  build a genuinely new generic declaration in the common library. Follow the
  workflow there rather than restating it.

## 3. Make the smallest coherent edit

Apply the shortest maintainable fragment that represents a tested idea. Avoid
batching unrelated or speculative steps, but do not force one file edit per
tactic when a short sequence was validated together.

For a file the language server cannot hold, or a resource-boundary search,
see `../../../docs/guides/lean-edit-loops.md` (tools in `tools/` beside this
file); its verdicts are candidates, discharged in step 4 against the real file.

## 4. Recheck immediately

- Query `lean_goal` on the modified line with the column omitted to inspect its
  before/after states, or query at the exact start of the next tactic.
- Run `lean_diagnostic_messages` and compare it with the baseline. This is the
  check. It does not need a build behind it.
- Repair or revert newly introduced errors. Unrelated pre-existing diagnostics
  should remain visible but are not evidence that this edit caused a regression.
- If goals remain, repeat the exploration-edit-check loop from their exact
  positions. If that loop is going to run many times, revisit the loop choice
  in step 3 before repeating it by hand.
- Treat an empty goal result as completion only after confirming the cursor
  position and that the target proof has no new diagnostic errors.

## 5. Finalize

Declare the target complete only when the intended goals are closed and the
edited proof has no new errors. When the task requires theorem-level assurance
and the fully qualified declaration name is known, use `lean_verify` to check
axioms and the source scan. Its axiom list is advisory, not evidence: it comes
from `#print axioms`, which on Lean v4.30 and later can under-report axioms
reached through an imported inductive
([lean4#15226](https://github.com/leanprover/lean4/issues/15226)). Take
axiom evidence only from the repository's from-scratch audit gate. Run any
project verification selected by the repository's authoritative gate
catalogue separately.

In Blanc, if the work built, generalized, or hoisted a common-library
declaration, the discoverability closure in
`<workspace-root>/blanc/docs/COMMON_API.md`,
*Common-library-first workflow* — the registry branch update and, where a
reliable goal shape exists, the proof-recipe registration — is part of the
same change, not a follow-up.

If Lean MCP is unavailable or stale, repair/restart the integration or report
the blocker rather than substituting blind edits or a build.
