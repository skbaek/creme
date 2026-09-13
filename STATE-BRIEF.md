# State brief — creme-admission-fallback-v1

Worker: muse subagent (model muse-spark, effort as routed). Master merges/pushes default branches and writes the master record; this worker does none of those and never writes under the goal store.

Goal: fix the admission fallback sibling-pollution demonstrated by the TWG reachability worker (light Python-only unit; no Lean, no holds, no MCP).

- Worktree: `/Users/agent/creme/.worktrees/creme-admission-fallback-v1`
- Branch: `muse/creme-admission-fallback-v1`
- Base: `origin/main` @ `7ffb5f32225c76fbce4bd3a8b28edba5270db69b` (freshly fetched 2026-09-13; `Briefs: name the hold the gates themselves take`)
- Canonical `/Users/agent/creme` is NEVER edited (three heavy workers import admission code from it live).
- Owned paths: `creme/build_ownership.py` (fallback + messaging only), one new test file `scripts/tests/test_admission_fallback.py`.

## Read-first record (all read in full before editing)

1. `lido-twg-pinned-target-reachability-landing-v1.md` §3–§4 + `polluting-row.json` (8-module row, aggregate 9523.4 MiB = 9.30 GiB, per-module peaks incl. Authorization 1379.9, PauseFor 1454.2, Authority 1936.4, Access 3035.9 MiB) + `authorization-never-fits-build.log` (`memory_gib` 11 with `source` "narrow default 4 GiB: …").
2. `/Users/agent/creme/AGENTS.md`, `docs/guides/execution.md` (light class: no hold; evidence discipline).
3. `module_cost_evidence` (build_ownership.py:2063) + `size_stale_set` (:2248) + `_estimate_note` (:2740) → `fit:` provenance line (semaphore.py:1620); `scripts/check.sh` is the repo's own invocation (`python3 -m unittest discover -s scripts/tests`).

## Baselines (pristine base, from the worktree)

- `python3 -m unittest scripts.tests.test_semaphore` → 166 tests, OK.
  (Brief says 176/176: that count matches the canonical ahead-commit `b4b58f4`, which adds 10 test defs to test_semaphore.py. Mandated base is origin/main tip, where the measured baseline is 166.)
- `python3 -m unittest scripts.tests.test_admission_accuracy` → 83 tests, OK.
- Full `discover -s scripts/tests` → 677 tests, 1 failure + 1 skipped: the failure is `LiveStateCompatibilityTest.test_the_live_files_validate_under_the_candidate_reader`, which passes in isolation (83/83 file-green) — a pre-existing live-host-state/order interaction on the pristine base, outside owned paths.

## Design (decided, implements the brief's constraint set jointly)

- Fix 1: in `module_cost_evidence`, a multi-module narrow row's whole-build aggregate is NOT attributed to a member that has its own recorded `module_peak_mib` — that direct peak (already in `fallback_peaks`) is the row's conservative floor. Single-module rows keep the aggregate floor (no siblings exist; preserves the documented 7.32-vs-6.74 intent and `test_changed_source_preserves_the_prior_whole_build_floor`). Narrow rows without per-module peaks are unchanged. `peaks`/exact logic, `unmeasured`, and contention untouched.
- Fix 2: `module_cost_evidence` returns `fallback_origin` (per-module peak/row-time/kind); `size_stale_set` appends `; fallback prices {module} at {gib} GiB from row {time} ({kind})` to `source` in the narrow/heavy/profile/broader branches exactly when the fallback term alone sets the ask. The `fit:` line quotes `source` via `_estimate_note`, so both surfaces are fixed by the one string. Prefixes preserved (histogram buckets intact).

## Boundaries

- [x] Worktree + branch created at recorded base; read-first complete; baselines measured.
- [x] Fix 1 + Fix 2 implemented, documented at site (`creme/build_ownership.py` +126/-15).
- [x] New tests green (9/9); neighboring suites green (357/357); full gate green (686, 1 pre-existing skip); branch pushed.
- [ ] Wind-down run; return to master.

## Results (2026-09-13, all from this worktree)

- `python3 -m unittest scripts.tests.test_admission_fallback` → 9 tests, OK.
- `python3 -m unittest scripts.tests.test_semaphore scripts.tests.test_admission_accuracy scripts.tests.test_build_ownership` → 357 tests, OK (166 + 83 + 108; matches baselines, no regressions).
- `python3 -m unittest discover -s scripts/tests` → 686 tests, OK (skipped=1; the baseline's flaky LiveState order-failure passed this run — environmental, unrelated).
- `compileall` on `creme` + prover tools → OK; `git diff --check` → clean.
- Behavior change on the polluting scenario: Authorization/PauseFor/Authority singles 11 → 4 GiB (narrow default, own-peak terms below it); Access single 11 → 8 GiB (unchanged heavy rule: 42 s ≥ 20 s keeps the profile default; own-peak term is 4). All four fit now. A fallback-driven ask now appends `; fallback prices {module} at {gib} GiB from row {time} ({kind})` to `source` (and therefore to the `fit:` line).
