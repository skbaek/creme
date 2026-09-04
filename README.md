# Creme

Creme is the public agent-development launch root for the sibling
[Jaune](https://github.com/skbaek/jaune) and
[Blanc](https://github.com/skbaek/blanc) Lean projects. It supplies shared
instructions, Lean skills, client shims, host-safe coordination, and an
accountable goal-execution method. Jaune and Blanc remain standalone libraries
and retain authority over their own APIs, proofs, and gates.

Creme v0.1 supports macOS and Linux. Linux deliberately has fewer optional
host capabilities; an explicit `UNAVAILABLE` is a supported result.

> Public `main` is available at `https://github.com/skbaek/creme` under the
> [MIT License](LICENSE). The first public macOS/Ubuntu matrix and fresh Ubuntu
> sibling-layout runtime run passed. Fresh client and conventional-Linux
> acceptance remain explicit v0.1 release gates.

## Blank-host setup

For a new macOS or Linux account, start with the complete, preview-first
[first-machine setup guide](docs/setup.md). It covers system prerequisites,
Codex and Claude Code installation/trust, the Lean toolchain, disk planning,
public cloning, host initialization, sibling builds, optional fixtures, and a
representative client check.

The shortest path after prerequisites are present is:

```sh
cd ~
git clone https://github.com/skbaek/creme.git creme
git clone https://github.com/skbaek/jaune.git jaune
git clone https://github.com/skbaek/blanc.git blanc
cd ~/creme
python3 -m creme platform
python3 -m creme init --workspace-root ..
python3 -m creme init --workspace-root .. --write
python3 -m creme doctor --workspace-root ..
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

All supported local agents and humans share the tracked semaphore launcher:

```sh
./.semaphore/semaphore status
```

Fresh hosts keep its private runtime state in ignored `.semaphore/state/`.
Upgraded hosts retain and use legacy state until the explicit, non-destructive
`migrate-state` cutover documented in [the setup guide](docs/setup.md).

Codex installations that authorize stable executable paths can install thin
delegates for telemetry and Lean reclamation. Semaphore coordination always
uses the tracked client-neutral launcher. Preview the exact files first:

```sh
python3 -m creme host-wrappers --output-dir ~/.codex/bin
python3 -m creme host-wrappers --output-dir ~/.codex/bin --write
```

The generated files contain no capability implementation; each delegates to
this checkout's `scripts/creme`. Regenerate them after moving the checkout.
Use `--replace` only after reviewing a changed preview. If either generated
path exists, `doctor` requires the complete installed set to match.

Before real work, read [AGENTS.md](AGENTS.md), the appropriate sibling's
`scripts/GATES.md`, and [the execution guide](docs/guides/execution.md).
New goal documents use [the public goal-writing guide](docs/guides/goal.md),
even when a concrete goal is stored in a private repository. One session at a
time holds the master role described in
[the master guide](docs/guides/master.md); it owns goals, workers, merges, and
pushes, and it is the only session that takes the `master-*` lease.
Client discovery and its negative control are documented in
[docs/client-discovery.md](docs/client-discovery.md).

## Commands

```sh
python3 -m creme --help
python3 -m creme doctor --json
python3 -m creme host-guidance
python3 -m creme host-wrappers --output-dir ~/.codex/bin
python3 -m creme memory-headroom
python3 -m creme telemetry
python3 -m creme python-runtime 3.11.9
./.semaphore/semaphore status
./.semaphore/semaphore adaptive-acquire GOAL --note "proof loop" --memory-gib 4
./.semaphore/semaphore adaptive-acquire GOAL --note "queued" --wait 600
./.semaphore/semaphore master-acquire --client codex --note "master session"
./.semaphore/semaphore master-renew --heartbeat 1500 --detach
./.semaphore/semaphore master-release
python3 -m creme tempdir
python3 -m creme cache-copy SOURCE DESTINATION
python3 -m creme reclaim --dry-run
python3 -m creme reclaim --idle-workers 10 --goal GOAL
python3 -m creme reclaim --wind-down GOAL
python3 -m creme build-ledger --since 2026-09-03 --until 2026-09-03T05:35:00Z
python3 -m unittest discover -s scripts/tests -v
```

Mutating commands are preview-first where practical. `cache-copy` needs
`--execute`; reclamation proves same-client ancestry from a frozen process
snapshot and refuses ambiguous subtrees. `--idle-workers MIN --goal GOAL` narrows
that proven plan to Lean workers measured idle across two CPU samples and
working inside the named goal's worktrees, and reports every other worker
with its owner instead of signalling it — under the master model every
session shares one client process, so the goal worktree is the boundary.
`--wait SECS` queues a request under the same mutex and returns in one call
rather than inviting a hand-rolled `status` polling loop; it can postpone a
request but never admits one past a safety floor. Task wind-down additionally scopes
every candidate to the caller's configured `.worktrees/GOAL` roots, so
concurrent goals can wind down independently without releasing or killing one
another.

## Design and migration

- [Architecture and authority](docs/architecture.md)
- [First-machine setup](docs/setup.md)
- [Capability contract](docs/capabilities.md)
- [Host profile](docs/host-profile.md)
- [Goal contracts](docs/guides/goal.md)
- [Lean edit loops](docs/guides/lean-edit-loops.md)
- [Migration from Elanc/Plans](docs/migration.md)
- [Provenance](docs/provenance.md)
