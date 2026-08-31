# Self-hosting checkpoint

Date: 2026-08-31 (Asia/Seoul)

Minimum spine branch commit: `b070556eb7a6941a1ebbb150a6709b752b35f20c`
Pre-public local `main` merge: `10e1b96cca71230b13ade9371ad64a82543d4cfa`
Latest exact code candidate exercised:
`df2c2b431f3a67438a1eca8ff4f743f0c585c148`

The merge was the explicit pre-public exception authorized by the goal. At
that checkpoint no remote existed and nothing had been pushed. The owner later
created the empty public `skbaek/creme` repository and, on 2026-08-31, approved
the MIT license and first push. Exact-candidate review still precedes that push.

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

## Post-Proxy trusted-client MCP liveness

After Proxy Pair publication, ephemeral Codex task
`01a055c2-4f29-7b73-934c-c1137b267b61` launched from exact root
`<workspace>/creme` at Creme
`2c4511e272e3d7cddd07a7d5156777e7f856f938`, with explicit read-only access
to Jaune `ae1b7d51f79205a15fc946034b4fb18085dcddad` and Blanc
`18ca2b4310688465300378067b3a76f9bfadf4a5`. The client was Codex CLI
0.151.0-alpha.7.1 using the current trusted configuration; it did not build or
edit either sibling.

The task invoked the registered `lean-lsp-mcp` server directly. Required
`lean_diagnostic_messages` calls succeeded for both
`Jaune/Basic.lean` and `Blanc/Basic.lean`, each returning `success: true`,
`timed_out: false`, an empty diagnostics list, and no failed dependencies.
Ordinary read-only outline/declaration calls also succeeded on both siblings.
The task was stopped after these required checks when it began optional extra
probing; that interruption does not qualify any incomplete optional probe as
acceptance evidence.

This closes current-trusted-client MCP liveness on the post-Proxy target. It
does not close the fresh-user trust/approval ceremony, Claude Code, or the
client-mediated representative edit required by the full clean-room matrix.

## Ignored-user-config trust control

Ephemeral Codex task `01a055d7-b0c4-74a2-b517-e614d566409c` launched at exact
Creme `d66db65d288884c2596b8db5bf547998e377704a` with user configuration
ignored and an invocation-only trust override for the reviewed Creme root. It
correctly loaded the Creme instruction marker and `lean-inspector` skill, but
exposed no callable MCP tool. It therefore made neither requested sibling
diagnostic call, and no MCP-liveness claim is taken from it.

This is a fail-closed negative control. It shows that an ephemeral trust
override is not a substitute for the client's genuine fresh project
registration/approval path; no persisted trust, user configuration, or client
database was modified.

## Authority verdict

The exact candidate is self-discovering in Codex and registers its 22-tool MCP
surface in the current trusted client. The owner explicitly authorized the
repository authority transition after the completed Beacon deposit work was
merged. The reconciled default branches were then verified and published:

| Repository | Activation commit / tree | Transition evidence |
|---|---|---|
| Elanc | `a6640f4e470f447fd4df0e3ac40104f2533c76a9` | compatibility surface is fail-closed; legacy MCP, skills, Claude agents, and Codex rules are inactive; live macOS probe tests skip on Linux CI |
| Plans | default merge `3ee039b99dc89d89285ec77f0b272ef98ca038fc` | reusable workflow guides point to Creme; Plans retains private goals, state, reports, and evidence; later Plans-only ledger commits record the transition |
| Jaune | `ae1b7d51f79205a15fc946034b4fb18085dcddad` | onboarding and gate catalogue use Creme coordination while Jaune retains proof authority |
| Blanc | `c4795430427b4c1a0eec9f01d9c277728ca69613` | Beacon-complete main plus Creme portability changes; the exact candidate passed `GATES OK: 47 rows, 47 executed, 0 reused from valid evidence, 2633.8s` |

The Blanc reconciliation preserved Beacon's two-regime, four-mutant model gate
and used Creme's capability-selected cache-copy wording. Elanc's 32 tests,
Jaune hygiene/integrity plus its 1,780-job build, and the Plans content/whitespace
checks also passed. The dirty and divergent shared Plans checkout was not
moved; only its verified remote default branch advanced.

## Post-activation downstream integration

Proxy Pair subsequently reconciled onto the activated Blanc base
`c4795430427b4c1a0eec9f01d9c277728ca69613` and advanced public Blanc `main`
to `18ca2b4310688465300378067b3a76f9bfadf4a5`. Its merge commit
`76c9863dcc28b002b55ab6456d76a1cab1a8e9e0` has exact parents Proxy
`1c54d09601766a0710e87cf2672a32a22d95098c` and Creme
`c4795430427b4c1a0eec9f01d9c277728ca69613`; the descendant `18ca2b4`
changes only the claim-gate verdict. The ordered post-merge close passed 52
rows, with 3 executed and 49 reused from fingerprint-valid evidence, and both
integration reviews accepted the result.

The remaining fresh-client/MCP and representative-edit acceptance therefore
uses Blanc `18ca2b4` (or a later reviewed `main` descendant) as its minimum
target and records the exact commit actually exercised. The earlier `162b840`
direct-CLI smoke remains migration evidence; it is not silently reused as the
post-Proxy client-mediated edit.

Creme is therefore the active repository control plane for new agent-assisted
work. New sessions must launch from the Creme root, not Elanc or Plans. This
activation does **not** close full v0.1 self-hosting acceptance: a fresh Codex
trust/approval ceremony and representative edit, a fresh Claude Code run, the
license and first-public-push gates, public-only clean-room evidence, and
conventional Linux acceptance remain open.
