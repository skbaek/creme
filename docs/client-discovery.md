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
