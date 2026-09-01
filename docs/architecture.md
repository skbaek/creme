# Architecture and authority

Creme has three configuration tiers and one repository boundary.

| Tier | Owns | Must not own |
|---|---|---|
| Shared | instructions, goal/lead method, evidence discipline, skills, capability calls | platform binaries, personal paths, live pressure values |
| OS adapter | canonical platform/runtime identity, process discovery, telemetry, temporary/copy optimization, GUI-session checks | host thresholds disguised as universal facts |
| Ignored host profile | detected static facts, derived policy, explicit user overrides, workspace layout | credentials, live pressure, repository doctrine |

The precedence chain is: command-line override, host override, OS-derived
default, conservative shared default. Missing information is never invented.

Direct platform calls live only in `creme/adapters/`. Shared code selects one
adapter through `creme.adapters.get_adapter()` and consumes structured
`OK`, `PREVIEW`, `BUSY`, `UNAVAILABLE`, `REFUSED`, or `ERROR` results. Linux
never falls through to a Darwin implementation.

## Repository authority

- Creme owns reusable agent workflow, client integration, coordination, and
  host capability contracts.
- Jaune and Blanc own source architecture, proof doctrine, generated-artifact
  rules, gate commands, budgets, and pass criteria.
- A goal store owns concrete goals, state briefs, reports, and private
  portfolio decisions. It is optional and is not a Creme runtime dependency.
- Blanc consumes Jaune only through its Git-pinned Lake dependency. Sibling
  checkout paths are for coordinated editing, not build substitution.

Creme points to a sibling's canonical documentation instead of copying it.
This prevents two live sources of truth and keeps both libraries standalone.

## Client surfaces

`AGENTS.md` is the canonical project instruction. `CLAUDE.md` imports it.
`.agents/skills` is canonical for skills; `.claude/skills` contains documented
per-skill links. Codex, Claude, and Antigravity MCP shims pin the same server
version, but only Codex and Claude are v0.1 acceptance clients. Antigravity is
retained as an explicitly experimental surface until live discovery is proven.

Host memory coordination has one additional client-neutral surface:
`.semaphore/semaphore`. Its implementation remains in the Creme package, while
its ignored state is anchored to the canonical checkout rather than any linked
goal worktree. User-local Codex delegates remain compatibility shims, not a
second implementation or state authority.

See [client discovery](client-discovery.md) for current official discovery
semantics and the permission-is-not-discovery negative control.
