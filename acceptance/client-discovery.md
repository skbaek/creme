# Client-discovery acceptance

This acceptance proves that fresh Codex and Claude Code sessions launched from
Creme discover Creme's instructions, Lean skills, and pinned Lean MCP server,
can read the configured sibling repositories, and do not confuse permission
with project discovery.

Record the date, client version, Creme commit, exact command and current working
directory, trust and MCP approval outcomes, and every verdict below. Never copy
global client configuration, opaque trust state, approval databases, or
credentials into the evidence bundle.

## Static checks

From the Creme worktree under test:

```sh
python3 -m unittest \
  scripts.tests.test_client_surface \
  scripts.tests.test_permission_is_not_discovery \
  -v
```

These tests parse the committed surface and construct isolated temporary Git
repositories and config roots. They do not invoke an agent, contact a service,
or mutate user/global configuration.

## Preconditions

1. `creme/`, `jaune/`, and `blanc/` are siblings, or the previewed machine-local
   profile resolves the equivalent absolute layout.
2. `uvx` and the pinned `lean-lsp-mcp` version are available through the
   reviewed setup path.
3. The generated Codex `creme` profile has been previewed in full before it is
   installed. If it claims domain-restricted network access, it sets
   `features.network_proxy = true`.
4. The user is prepared to accept Creme workspace trust and the exact pinned
   project MCP server. No test edits or fabricates client trust state.
5. The sibling diagnostic fixtures and ordinary readable canary files are
   identified before launching a client.

For the post-activation run, record the actual sibling commits before launch.
The minimum reviewed Blanc target is public `main` at
`18ca2b4310688465300378067b3a76f9bfadf4a5`, which contains both the Creme
authority transition and the reconciled Proxy Pair integration. A later
reviewed `main` descendant is acceptable only when its exact identity is
recorded. Earlier direct-CLI evidence at the pre-merge onboarding branch does
not satisfy the remaining client-mediated representative edit.

## Codex positive control

```sh
cd "${CREME_ROOT:?set CREME_ROOT}"
codex --profile creme
```

In the fresh session:

1. Inspect `/mcp` and record the `lean-lsp-mcp` status and configured version.
2. Invoke `$lean-inspector` explicitly.
3. Submit this no-write prompt, replacing fixture paths only:

```text
State the active project root and the canonical launch-root rule from the
instructions. List the available Lean skills and MCP servers. Read the named
ordinary canary in ../jaune and ../blanc, then use lean_diagnostic_messages on
the named sibling Lean fixtures. Make no edits and do not run a build.
```

Pass: the answer identifies Creme as the project root, reflects Creme's
`AGENTS.md`, exposes the required Lean skills, reports the pinned MCP server,
reads both sibling canaries, and returns MCP diagnostics for both fixtures.

Fail closed: a skipped trusted project config, absent skill, unavailable MCP
server, wrong root, or denied sibling read is a failure, not a limited pass.

## Claude Code positive control

```sh
cd "${CREME_ROOT:?set CREME_ROOT}"
claude doctor
claude
```

Accept workspace trust for the exact Creme root. In the fresh session inspect
`/context`, `/memory`, `/skills`, `/mcp`, and `/permissions`. Approve only the
pinned `lean-lsp-mcp` project server, invoke `/lean-inspector`, and submit the
same no-write prompt used for Codex. After approval, record the read-only CLI
view as well:

```sh
cd "${CREME_ROOT:?set CREME_ROOT}"
claude mcp list
```

Pass criteria are the same as Codex. The permission view must show Jaune and
Blanc as relative additional directories derived from Creme's shared settings.

## Muse positive control

Muse is experimental; its evidence column records observed behavior and does
not satisfy either required v0.1 client column. The user-global `mcpServers`
entry from the setup guide must already be installed: a new muse process
reads it at startup.

```sh
cd "${CREME_ROOT:?set CREME_ROOT}"
muse exec --reasoning-effort minimal "Do not call any tools. Reply with \
exactly three lines: (1) PROJECT-ROOT: <your current project root>, \
(2) LEAN-TOOLS: <comma-separated lean-lsp-mcp tool names, or NONE>, \
(3) SKILLS: <lean skill ids you see, or NONE>."
muse exec --reasoning-effort low "Call lean_diagnostic_messages exactly once \
on <absolute Blanc fixture path> and reply with two lines: DIAG-COUNT: \
<number of messages>, DIAG-FIRST: <first 100 chars of the first message, or \
EMPTY>. Make no edits and do not run a build."
```

Pass: the first answer identifies Creme as the project root, lists the exact
20 enabled Lean tools with `lean_build` and `lean_profile_proof` absent, and
names both Lean skills. The second returns MCP diagnostics for the named
sibling fixture. A skipped trust decision, absent skill, unavailable MCP
server, wrong root, or denied sibling read is a failure, not a limited pass.
An interactive master session covers the trust ceremony and the
`CREME_MASTER_SESSION_ID` lease path, which headless probes cannot.

Approval behavior is part of the record. The listing probe calls no tools and
runs under default approval. A headless diagnostics probe parks on a human
approval request for the MCP tool call (choices: allow once, allow for the
session, abort); add `--disable-approval` to that probe only, and record that
the call ran approval-isolated. There is no user-configurable standing
selective rule for MCP tools: no tool-rule keys exist in user settings, the
reviewer is human, and the managed-policy planes are absent.

## Wrong-root controls

Start fresh sessions from a projectless temporary Git repository, Jaune, and
Blanc, using otherwise equivalent machine-local permissions. Do not use Claude
`--add-dir`, `/add-dir`, or
`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`.

Pass: an ordinary Creme file can be read when permission is deliberately
granted, but the session does not report Creme's canonical instruction marker,
Creme-only Lean skills, or Creme project MCP configuration. A wrong-root run
must never be reported as satisfying the Creme launch contract.

## Synthetic permission-is-not-discovery control

Create the following two sibling temporary Git repositories under one isolated
temporary directory:

```text
control-root/
  AGENTS.md or CLAUDE.md: CONTROL_ROOT_CANARY
granted-only/
  ordinary.txt: READ_ACCESS_CANARY
  AGENTS.md and CLAUDE.md: FORBIDDEN_INSTRUCTION_CANARY
  .agents/skills/forbidden-sibling-skill/SKILL.md
  .claude/skills/forbidden-sibling-skill/SKILL.md
  .codex/config.toml: forbidden-sibling-mcp
  .mcp.json: forbidden-sibling-mcp
```

Codex uses an isolated temporary `CODEX_HOME`; its temporary profile trusts
only `control-root` and grants `granted-only` as a workspace root. Claude uses
an isolated `CLAUDE_CONFIG_DIR` whose user settings grant `granted-only` via
`permissions.additionalDirectories`. Do not place `granted-only` in the launch
path and do not use any Claude additional-directory discovery flag.

Negative pass:

- `READ_ACCESS_CANARY` is readable.
- `CONTROL_ROOT_CANARY` is active.
- `FORBIDDEN_INSTRUCTION_CANARY`, `forbidden-sibling-skill`, and
  `forbidden-sibling-mcp` are absent from the active client views.

Positive fixture check: close the session, launch a separate fresh session with
`granted-only` as its project and current working directory, accept its isolated
trust/MCP prompts, and verify that all forbidden canaries become discoverable.
This shows that the negative result came from project-root selection rather
than a broken fixture.

Delete only the temporary fixture and config roots after recording verdicts.
Do not alter the real client home or global settings during this control.

## Evidence record

| Check | Codex | Claude Code | Muse |
| --- | --- | --- | --- |
| Client version recorded | CLI 0.151.0-alpha.7.1 | Desktop 1.34493.1; CLI absent | muse-bin-1.1.1-R2514.1 |
| Exact Creme commit recorded | `2c4511e272e3d7cddd07a7d5156777e7f856f938` for latest trusted-client liveness | OPEN | `ebeee65` (probe-time main) |
| CWD/project is Creme | PASS, four ephemeral tasks | OPEN; desktop locked | PASS, headless exec |
| Root instructions observed | PASS | OPEN | PASS (`AGENTS.md`; client-emitted `CLAUDE.md`-shadowed warning) |
| `lean-inspector` observed and invoked | PASS in current trusted client | OPEN | Observed PASS; explicit invocation OPEN |
| `lean-prover` observed | PASS | OPEN | PASS |
| Pinned Lean MCP observed | 22 tools in current trusted client; zero with ignored user trust | OPEN | PASS, exact 20 enabled tools, disabled pair absent |
| Lean MCP diagnostics returned | PASS for Jaune `ae1b7d5` and post-Proxy Blanc `18ca2b4` in current trusted client | OPEN | PASS, 0 messages on Blanc `ForwardMstore8.lean` at `a3d23af`, approval-isolated |
| Jaune ordinary read succeeded | PASS | OPEN | OPEN headless control (interactive reads proven) |
| Blanc ordinary read succeeded | PASS | OPEN | OPEN headless control (interactive reads proven) |
| Representative sibling edit | FAILED SAFELY under hard host pressure; no acceptance claimed | OPEN | OPEN (not attempted; setup-only session) |
| Wrong-root control omitted Creme discovery | synthetic PASS; live client matrix OPEN | OPEN | OPEN |
| Synthetic access-without-discovery control passed | PASS | PASS static fixture only | OPEN |
| Trust and approvals explicitly recorded | OPEN; ignored-user-config plus an invocation-only trust override still exposed zero MCP tools | OPEN | PASS: trust user-config; first MCP tool call requests human approval (allow once/session/abort); no standing selective rule; headless parks without `--disable-approval` |

Task identifiers, the two Codex configuration controls, and the later direct
MCP task are recorded in `acceptance/self-hosting.md`. This table is
deliberately incomplete: direct liveness under inherited trust is not a fresh
approval ceremony, the interrupted post-Proxy edit did not return a terminal
MCP success or run its cheap gates, and the static Claude fixture is not a live
Claude session. The resource-control disposition is recorded in
`acceptance/macos.md`.

Antigravity is recorded separately as retained experimental compatibility. It
does not satisfy either required v0.1 client column until a dedicated live
Creme-root matrix is approved and completed.
