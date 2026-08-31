# Creme

Creme is the public agent-development launch root for the sibling
[Jaune](https://github.com/skbaek/jaune) and
[Blanc](https://github.com/skbaek/blanc) Lean projects. It supplies shared
instructions, Lean skills, client shims, host-safe coordination, and an
accountable goal-execution method. Jaune and Blanc remain standalone libraries
and retain authority over their own APIs, proofs, and gates.

Creme v0.1 supports macOS and Linux. Linux deliberately has fewer optional
host capabilities; an explicit `UNAVAILABLE` is a supported result.

> Publication is not complete yet. The owner has created the empty public
> repository at `https://github.com/skbaek/creme`; the license grant must be
> recorded before the first public push.

## Blank-host setup

Prerequisites are Git, Python 3.9 or newer, and a current Codex or Claude Code
client. Lean work additionally needs `uvx` and the sibling repositories'
documented Lean/Lake toolchain.

```sh
mkdir agent-workspace
cd agent-workspace
git clone https://github.com/skbaek/creme.git creme
git clone https://github.com/skbaek/jaune.git jaune
git clone https://github.com/skbaek/blanc.git blanc
cd creme
python3 -m creme platform
python3 -m creme init
python3 -m creme init --write
python3 -m creme doctor
```

`init` previews by default. Its live output is `.creme/host-profile.json`, which
is ignored. Review before `--write`; use `--replace` only after reviewing a
changed host fingerprint or policy.

Codex users who need sibling writes should also preview a least-privilege
profile, then explicitly install the reviewed result:

```sh
python3 -m creme client-profile --workspace-root ..
python3 -m creme client-profile --workspace-root .. \
  --output ~/.codex/creme.config.toml --write
codex --profile creme
```

The profile file is an overlay selected with `--profile`; Creme never changes
the main user configuration implicitly. Claude users launch `claude` from this
directory after reviewing the project MCP prompt. Both clients must be
launched with Creme as the project/current directory. Merely granting sibling
filesystem access does not load Creme's instructions, skills, or MCP config.

Before real work, read [AGENTS.md](AGENTS.md), the appropriate sibling's
`scripts/GATES.md`, and [the execution guide](docs/guides/execution.md).
Client discovery and its negative control are documented in
[docs/client-discovery.md](docs/client-discovery.md).

## Commands

```sh
python3 -m creme --help
python3 -m creme doctor --json
python3 -m creme telemetry
python3 -m creme semaphore status
python3 -m creme tempdir
python3 -m creme cache-copy SOURCE DESTINATION
python3 -m creme reclaim --dry-run
python3 -m unittest discover -s scripts/tests -v
```

Mutating commands are preview-first where practical. `cache-copy` needs
`--execute`; reclamation proves same-client ancestry from a frozen process
snapshot and refuses ambiguous subtrees.

## Design and migration

- [Architecture and authority](docs/architecture.md)
- [Capability contract](docs/capabilities.md)
- [Host profile](docs/host-profile.md)
- [Goal contracts](docs/guides/goal.md)
- [Lean edit loops](docs/guides/lean-edit-loops.md)
- [Migration from Elanc/Plans](docs/migration.md)
- [Provenance](docs/provenance.md)
