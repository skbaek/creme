# Host profile

The tracked schema is `config/host-profile.schema.json`; the tracked example is
`config/host-profile.example.json`. The live profile is
`.creme/host-profile.json` and is ignored.

```sh
python3 -m creme init                 # preview
python3 -m creme init --write         # first reviewed write
python3 -m creme validate-profile
python3 -m creme doctor
python3 -m creme doctor --heavy-workers 1 --task-memory-gib 2
```

The profile records only static facts: OS, architecture, logical cores, and
physical memory. A fingerprint makes stale profiles deterministic. Current
free memory, swap, disk availability, process RSS, and pressure are dynamic
samples and are rejected as durable facts.

Validation results are:

- `MISSING`: use conservative behavior and warn;
- `INVALID`: refuse the profile and identify the structural error;
- `STALE`: refuse its policy until regenerated and reviewed;
- `LIMITED`: shape is valid but the adapter cannot verify freshness;
- `VALID`: shape and current static fingerprint agree.

Policy keys are `task_memory_gib`, `heavy_workers`, and `light_workers`.
Explicit CLI values win over host overrides, then recorded policy, then OS
defaults, then shared conservative defaults. Profile generation is local; do
not commit a live profile or use one machine's values as public baselines.
Adaptive admission uses `task_memory_gib` as the default declared peak and
`heavy_workers` as a ceiling, then tightens both with live aggregate headroom,
a usability reserve, and simultaneous peak reservations. An agent may provide
a larger conservative `--memory-gib` estimate for one operation; that dynamic
intent is stored with the private hold, not persisted as a host fact.

`workspace.root` identifies the parent of the sibling checkouts.
`workspace.goal_store` may name a private goal location or remain null. No
Creme runtime path requires that store to exist.

## Local host guidance

Machine-specific operational findings that do not fit the static JSON profile
live in the optional ignored `.creme/host-guidance.md` file. This includes a
reproduced resource hazard, measured bounds, and the local wrapper that safely
runs an otherwise authoritative repository command. It must not contain
credentials, transient pressure snapshots presented as durable facts, or
changes to a sibling repository's pass criteria.

Every Creme-root agent run checks this file through:

```sh
python3 -m creme doctor
python3 -m creme host-guidance
```

`MISSING` is supported on a host with no local findings. A symlink,
non-regular file, invalid UTF-8, NUL-containing, empty, or oversized guidance
file fails the doctor check. The 64-KiB ceiling keeps this safety layer
reviewable. `AGENTS.md` requires agents to read valid local guidance before
host-intensive work.
