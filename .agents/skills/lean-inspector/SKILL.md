---
name: lean-inspector
description: Inspects Lean 4 proof states, term goals, diagnostics, declarations, and file structure through lean-lsp-mcp. Use for questions about Lean files, goals, tactics, declarations, diagnostics, or a `sorry`, and before choosing a proof edit.
---

# Lean 4 inspection

A goal read off the source is a guess; the language server holds the state
Lean has. What each tool returns, and when that answer is stale or empty for
another reason, is in the tool's own description.

- A session that may not build reports a stale state as stale.
- An inspected state is reported verbatim.
