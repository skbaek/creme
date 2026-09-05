# Client discovery and trust

Creme is the launch root for supported agent work on the adjacent Jaune and
Blanc repositories. Launching in Creme is what selects Creme's instructions,
skills, and project MCP configuration. Merely allowing a client to read Jaune
or Blanc does not make either sibling part of Creme's project discovery chain.

This document records the supported behavior checked against official client
documentation on 2026-08-31. Client behavior can change; recheck the linked
sources when changing this surface or upgrading a client.

## Supported public surface

| Concern | Codex | Claude Code | Antigravity |
| --- | --- | --- | --- |
| Root instructions | `AGENTS.md` | `CLAUDE.md` imports `@AGENTS.md` | `AGENTS.md` |
| Project skills | `.agents/skills/<name>/SKILL.md` | `.claude/skills/<name>` points to the matching `.agents/skills/<name>` directory | `.agents/skills/<name>/SKILL.md` |
| Lean MCP | `.codex/config.toml` | `.mcp.json` | `.agents/mcp_config.json` |
| Sibling access | Generated machine-local permission profile | `.claude/settings.json` relative `permissions.additionalDirectories` | Not acceptance-supported yet |
| Trust | User accepts Creme as a trusted project; trust state is never committed | User accepts workspace trust and the pinned project MCP server | Not acceptance-supported yet |

The public files contain relative paths and reviewed version pins only. They do
not contain credentials, approval databases, copied trust state, absolute home
paths, or a user's global client configuration.

## Guarded Lean MCP launch

Every supported project shim launches the pinned MCP as `/usr/bin/python3 -m
creme lean-mcp -- uvx lean-lsp-mcp==0.26.1`. Binding the initial interpreter
to the host system path avoids a project-controlled `PATH`; generated guard
launchers then bind the resolved running interpreter by identity. The nested
`uvx` runner is likewise resolved only from reviewed non-writable installation
paths, never from client or project `PATH`. The launcher prepends a Creme-owned
`lake` guard without relying on either client's variable expansion or PATH
ordering. The guard delegates `serve` to the toolchain Lake selected by Elan.
It supplies Lake with an ignored toolchain facade whose `lean` proxy changes
only the final server environment: `LAKE` points to the guard while the real
Lean executable and sysroot are restored. Lake's own workspace environment,
invalid-configuration fallback, and package `moreGlobalServerArgs` therefore
remain authoritative.

All shims set `LEAN_MCP_DISABLED_TOOLS=lean_build,lean_profile_proof`,
`LEAN_LSP_MAX_OPEN_FILES=2`, and `LEAN_LSP_TEST_MODE=1`. A source audit of
`lean-lsp-mcp==0.26.1` found the test-mode variable read only in
`lean_lsp_mcp.client_utils._start_client`, where it becomes
`prevent_cache_get=True` for `LeanLSPClient`; no other behavior is conditional
on it. The proof profiler is also disabled because it shells out to `lake env
lean` and is therefore a second unowned compilation route. Doctor validates
the pin, launcher, and all three settings in every shim. The Antigravity
surface stays configuration-compatible but remains
outside acceptance support as described below.

## Codex discovery

Codex constructs its instruction chain once when a run starts. It finds the
project root (normally the Git root), then searches from that root down to the
current working directory. At each directory it reads at most one instruction
file, preferring `AGENTS.override.md`, then `AGENTS.md`, then configured fallback
names. Instructions nearer the current directory apply later. The default
combined project-instruction limit is 32 KiB.

A trusted project may provide `.codex/config.toml`. Project configuration is
layered from the project root toward the current directory, while an untrusted
project's `.codex` layers are skipped. Repository skills are discovered from
`.agents/skills` at the current directory and its parents up to the repository
root. Codex supports symlinked skill directories.

Creme therefore keeps canonical instructions in root `AGENTS.md`, canonical
skills in `.agents/skills`, and only the pinned Lean MCP definition in
`.codex/config.toml`. Cross-repository filesystem and Git access belongs in a
previewed, generated user profile rather than the public project configuration.
Permission profiles are currently beta. If their network-domain rules are
used, `features.network_proxy = true` is required for those rules to be
enforced; `network.enabled = true` alone allows direct network access.

### Lean verification approval

The project keeps `default_tools_approval_mode = "writes"` and grants only
`mcp_servers.lean-lsp-mcp.tools.lean_verify` an `approval_mode = "approve"`
exception. The reviewed `lean-lsp-mcp==0.26.1` tool advertises
`readOnlyHint=false`: it temporarily appends `#print axioms` to an LSP buffer,
attempts buffer restoration in `finally`, and optionally searches source with
`rg`. It is not a read-only LSP-buffer operation. The exception accepts this
specific verification operation; it does not change the annotation or approve other
MCP writes. Re-review this exception when upgrading the package pin.

Codex documents `writes` as prompting for tools not marked read-only and
supports the per-tool exception above. See the official
[MCP configuration options and examples](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
The guarded launcher, disabled build/profiler tools, sandbox, project trust,
host containment, and semaphore admission remain unchanged.

The 2026-09-05 source audit covered every tool decorator in the pinned 0.26.1
`server.py`, not only verification. There are 22 tools, of which 20 are enabled:

| Tools | Upstream hints (`readOnlyHint`, `openWorldHint`) | Project approval policy |
| --- | --- | --- |
| `lean_file_outline`, `lean_diagnostic_messages`, `lean_goal`, `lean_term_goal`, `lean_hover_info`, `lean_completions`, `lean_declaration_file`, `lean_references` | true, false | Existing `writes` default; no exception |
| `lean_multi_attempt`, `lean_run_code`, `lean_local_search`, `lean_code_actions`, `lean_get_widgets`, `lean_get_widget_source` | true, false | Existing `writes` default; no exception |
| `lean_leansearch`, `lean_loogle`, `lean_leanfinder`, `lean_state_search`, `lean_hammer_premise` | true, true | Existing `writes` default; network policy still applies |
| `lean_verify` | false, false | Single reviewed `approve` exception |
| `lean_build` | false, false | Disabled; no approval exception |
| `lean_profile_proof` | true, false | Disabled; no approval exception |

All 22 advertise `idempotentHint=true`; only `lean_build` explicitly advertises
`destructiveHint=true` (the others omit that hint). These are upstream policy
annotations, not proof that execution has no effects: `lean_run_code` creates
a temporary file and `lean_multi_attempt` edits a temporary LSP buffer. The
usual proof-loop, resource, source-edit, and network restrictions still apply.
The 19 enabled read-only-marked tools need no additional exception to avoid
prompts caused by the `writes` setting. Other client or managed policies can
still require approval; this audit does not promise every action is prompt-free.

The portable regression in `scripts/tests/test_client_surface.py` records this
inventory and checks pin/config scope, policy coverage, and metadata-drift
controls. Its `audit_mcp_annotations(source)` helper compares supplied package
source using Python's AST without importing or starting MCP. Run that source
audit again against the installed package when changing the pin or tools;
ordinary static CI intentionally does not depend on a host package cache.

After updating the trusted project's config, use a supported MCP/config reload
if the installed client exposes one; otherwise restart the client. The official
[app-server API](https://learn.chatgpt.com/docs/app-server) provides
`config/mcpServer/reload` to reload disk config and queue refreshes for loaded
threads; `/mcp` is documented as a status view, not a reload command. Existing
tasks may retain their loaded configuration. Verify the effective project
config and an authorized `lean_verify` call before declaring activation
complete. Static tests prove the committed scope, not prompt-free live
behavior. Do not relax server-wide or global approvals to compensate for a
stale or unsupported client configuration.

Official evidence:

- [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Config basics](https://learn.chatgpt.com/docs/config-file/config-basic)
- [Advanced config](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp)
- [Permissions](https://learn.chatgpt.com/docs/permissions)

## Claude Code discovery

Claude Code reads `CLAUDE.md` through the launch hierarchy. The root
`CLAUDE.md` is deliberately a one-line import of `@AGENTS.md`, the compatibility
shape recommended by Claude's documentation when `AGENTS.md` is canonical.
Project skills live under `.claude/skills/<name>/SKILL.md`; Creme uses documented
per-skill symlinks so each Claude skill resolves to the matching canonical
`.agents/skills` directory.

Claude's shared project settings live in `.claude/settings.json`. The relative
`permissions.additionalDirectories` entries grant access to Jaune and Blanc
after workspace trust is accepted. This setting grants file access but does
not load configuration from those directories.

That last guarantee is specific to the settings key. Do not replace it in a
discovery control with `--add-dir` or `/add-dir`: those mechanisms load skills,
commands, and subagents from additional directories. Setting
`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` also loads their `CLAUDE.md`
and rules. Project `.mcp.json` servers require one-time approval in addition to
workspace trust.

Official evidence:

- [Claude Code memory](https://code.claude.com/docs/en/memory)
- [Claude Code settings](https://code.claude.com/docs/en/settings)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Claude Code permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Debug your Claude Code configuration](https://code.claude.com/docs/en/debug-your-config)

## Access is not discovery

The acceptance control uses two sibling temporary Git repositories. The
client launches in a `control-root` repository and receives read access to a
`granted-only` repository. The granted repository contains an ordinary readable
canary and forbidden instruction, skill, and MCP canaries.

The control passes only when all of the following are true:

1. The ordinary canary in `granted-only` is readable.
2. Instructions active from `control-root` are present.
3. The granted sibling's instruction, skill, and MCP canaries are absent.
4. A positive-control launch rooted in `granted-only` discovers its canaries.

The local test in `scripts/tests/test_permission_is_not_discovery.py` validates
the fixture topology and isolation without launching a client. The live client
run in `acceptance/client-discovery.md` remains required evidence; the static
test is not a substitute for observed Codex or Claude behavior.

## Antigravity disposition

Antigravity's current documentation recognizes active-directory `AGENTS.md`,
workspace `.agents/skills`, and workspace `.agents/mcp_config.json`. Those files
overlap the canonical Codex surface and are retained. Antigravity is nevertheless
**experimental and not a v0.1 acceptance-supported client** until a fresh
Creme-root session demonstrates instruction, skill, MCP, trust, and sibling
access behavior.

Official evidence:

- [Antigravity GCLI migration guide](https://www.antigravity.google/docs/cli/gcli-migration/)
- [Antigravity MCP](https://antigravity.google/docs/mcp)

## Known limitations

- Static tests validate public file shape and the negative-control harness;
  they do not execute Codex, Claude Code, Antigravity, or the Lean MCP server.
- Trust and MCP approval are intentional user actions and cannot be proven by
  committed configuration.
- Codex permission-profile syntax is beta and must be checked against the
  installed client version before a generated profile is accepted.
- A network allowlist is not complete until setup, Git, Lake, and package-fetch
  operations have been tested on a clean representative host.
- Access from Jaune, Blanc, or a projectless directory does not select Creme as
  the client project. Those launch roots are negative controls, not supported
  substitutes.
