---
name: lean-inspector
description: Inspects Lean 4 proof states, term goals, diagnostics, declarations, and file structure through lean-lsp-mcp. Use for questions about Lean files, goals, tactics, declarations, diagnostics, or a `sorry`, and before choosing a proof edit.
---

# Lean 4 LSP inspection

1. Locate the target `.lean` file and the exact proof or term position.
2. When an edit may follow, capture the current diagnostics with
   `lean_diagnostic_messages` so later checks can distinguish new failures.
   If that edit is expected to need many iterations rather than one, the loop
   is chosen before the first edit, not after the third failure — see
   `../../../docs/guides/lean-edit-loops.md`, which also carries the rule that no
   committed resource ceiling or boundary claim may rest on language-server
   output. Follow it there rather than restating it.
   If diagnostics say `Imports are out of date`, the guarded server will not
   build them and `lean_build` is intentionally unavailable. From the goal
   worktree, run `python3 -m creme lake-build GOAL --probe -- TARGET`; exit 3
   means stale. Run that narrow target through the same command without
   `--probe`, restart the server, and inspect again. Use the repository's full
   target only at checkpoints and only through this admitted wrapper; never
   run bare `lake build`. `lean_profile_proof` is also absent because it shells
   out to an unowned `lake env lean` compilation.
3. Query the state without guessing:
   - Use `lean_goal` at the start of the relevant tactic or `sorry`.
   - Lines and columns are 1-indexed.
   - Omit the column to compare the goals before and after a tactic line.
   - An empty response from a misplaced cursor is not evidence that a proof is
     complete; recheck the position and diagnostics.
4. Use the narrowest supporting tool needed:
   - `lean_term_goal` for a term-mode expected type.
   - `lean_hover_info` for a symbol's type and documentation.
   - `lean_file_outline` for a compact view of imports and declarations.
   - `lean_local_search` before broader Lean or Mathlib searches.
5. Report the returned state verbatim when the user asks to inspect it.
   Otherwise, use it to guide the requested work without adding unnecessary
   transcript detail.

If the MCP server or language-server state is unavailable or stale, repair or
restart that integration or report the blocker. Do not infer the goal from
source text alone and do not run a build merely to obtain a tactic state.
