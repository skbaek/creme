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

| Check | Codex | Claude Code |
| --- | --- | --- |
| Client version recorded |  |  |
| Exact Creme commit recorded |  |  |
| CWD/project is Creme |  |  |
| Root instructions observed |  |  |
| `lean-inspector` observed and invoked |  |  |
| `lean-prover` observed |  |  |
| Pinned Lean MCP observed |  |  |
| Lean MCP diagnostics returned |  |  |
| Jaune ordinary read succeeded |  |  |
| Blanc ordinary read succeeded |  |  |
| Wrong-root control omitted Creme discovery |  |  |
| Synthetic access-without-discovery control passed |  |  |
| Trust and approvals explicitly recorded |  |  |

Antigravity is recorded separately as retained experimental compatibility. It
does not satisfy either required v0.1 client column until a dedicated live
Creme-root matrix is approved and completed.
