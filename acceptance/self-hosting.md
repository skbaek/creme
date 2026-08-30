# Self-hosting checkpoint

Date: 2026-08-31 (Asia/Seoul)

Minimum spine branch commit: `b070556eb7a6941a1ebbb150a6709b752b35f20c`
Pre-public local `main` merge: `10e1b96cca71230b13ade9371ad64a82543d4cfa`
Latest exact code candidate exercised:
`df2c2b431f3a67438a1eca8ff4f743f0c585c148`

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

## Exact-candidate Codex recheck

Two additional ephemeral Codex CLI 0.151.0-alpha.7.1 processes launched with
`<workspace>/creme` as the exact root, a read-only sandbox, explicit read-only
sibling access, and no Lean/MCP invocation.

| Task | Configuration control | Verdict |
|---|---|---|
| `01a05474-11b3-7f22-b111-4b24e884d02f` | `--ignore-user-config` | Exact root/HEAD, Creme heading, both skills, and both sibling gate files passed. The MCP namespace exposed zero tools, so clean-client project trust/registration is **not accepted** and remains an explicit onboarding gate. |
| `01a05474-c013-7130-8f7f-534b8593b77c` | current trusted client, `--ephemeral` | Exact HEAD and both skills passed; `mcp__lean_lsp_mcp__` exposed 22 named tools. No MCP tool was invoked. |

The contrast is the desired fail-closed trust behavior: a fresh untrusted client
does not silently activate project MCP configuration. It also means the second
row cannot substitute for the required clean-room trust/approval ceremony.
MCP liveness was deferred because another coordinated session held the
host-exclusive semaphore; starting a Lean environment would have violated host
coordination. Direct disposable Jaune/Blanc CLI edits are separately recorded
in `acceptance/macos.md` and do not satisfy this MCP row.

## Authority verdict

The exact candidate is self-discovering in Codex and registers its 22-tool MCP
surface in the current trusted client. The full C2
authority transition remains open because a fresh Claude Code session has not
yet run and the locked macOS desktop prevented controlled use of the installed
Claude Code Desktop app. Clean-client MCP approval/liveness is also open. Until
those checks complete, this goal continues to use its bootstrap Plans/Elanc
authority and does not declare Elanc deprecated.
