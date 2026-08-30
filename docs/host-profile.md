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

`workspace.root` identifies the parent of the sibling checkouts.
`workspace.goal_store` may name a private goal location or remain null. No
Creme runtime path requires that store to exist.
