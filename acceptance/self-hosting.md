# Self-hosting checkpoint

Date: 2026-08-31 (Asia/Seoul)

Minimum spine branch commit: `b070556eb7a6941a1ebbb150a6709b752b35f20c`
Pre-public local `main` merge: `10e1b96cca71230b13ade9371ad64a82543d4cfa`

The merge was the explicit pre-public exception authorized by the goal. No
remote exists and nothing was pushed.

## Fresh Codex task

A new ephemeral Codex task (`01a05450-570b-7d83-b4d6-41aa9b92d1a9`) launched
with `<workspace>/creme` as its exact root under a deliberately read-only
sandbox. It made no edits, wrote no configuration, and invoked no Lean tool.

| Check | Verdict | Evidence |
|---|---|---|
| exact root and candidate | PASS | Git root `<workspace>/creme`, commit `10e1b96cca71230b13ade9371ad64a82543d4cfa` |
| instructions | PASS | loaded `AGENTS.md`; first heading `Jaune/Blanc agent development from Creme` |
| skills | PASS | `lean-inspector` and `lean-prover` appeared from `.agents/skills` |
| MCP registration | PASS | namespace `mcp__lean_lsp_mcp__` exposed 22 Lean tools; tools were not invoked |
| sibling authority | PASS | both sibling `scripts/GATES.md` files were readable and began `Verification gates` |
| wrong discovery model | PASS | task distinguished added-directory access from Creme-root instruction/config discovery |
| doctor in forced read-only task | EXPECTED CONTROL | root/client/platform checks passed; Jaune/Blanc write checks failed because this acceptance task intentionally selected a read-only sandbox |

Outside that forced read-only task, the exact same `main` candidate ran
`python3 -m creme doctor --workspace-root <workspace>` under the configured
least-privilege sibling profile. After the ignored host profile was generated,
all checks passed, including read/write access to both siblings.

This proves Codex project discovery and demonstrates that discovery and access
are independent. It does **not** by itself prove MCP liveness, a Lean edit, a
fresh empty user-config root, Claude discovery, or Linux acceptance.

## Authority verdict

The minimum Creme spine is technically self-discovering in Codex. The full C2
authority transition remains open because a fresh Claude Code session has not
yet run and the locked macOS desktop prevented controlled use of the installed
Claude Code Desktop app. Until that check is complete, this goal continues to
use its bootstrap Plans/Elanc authority and does not declare Elanc deprecated.
