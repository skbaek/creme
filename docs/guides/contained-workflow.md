# Reviewed contained workflows

Hosts with the reviewed Lean preflight may install
`codex-creme-contained-workflow` as part of the complete Creme host bundle.
It runs registered gate and fixture recipes through one stable capability:

```sh
~/.codex/bin/codex-creme-contained-workflow status
~/.codex/bin/codex-creme-contained-workflow blanc GOAL drip-fixtures validate
~/.codex/bin/codex-creme-contained-workflow blanc GOAL drip-fixtures check
~/.codex/bin/codex-creme-contained-workflow blanc GOAL drip-fixtures write
~/.codex/bin/codex-creme-contained-workflow blanc GOAL blanc-gates checkpoint
```

Operation identifiers and modes are examples; the reviewed host recipe file
supplies the installed set. The invocation accepts only profile, goal,
operation, mode, and optional `--purpose goal|control|mutation|rehearsal`.
It accepts no shell command, executable, environment, path, or resource override.
`status` takes no arguments and reports the workflow service, last recorded
execution, host telemetry, and semaphore state. It remains available while the
host circuit breaker blocks execution. Missing service information never proves
an interrupted run completed.

## Recipe contract and review

The ignored canonical `.creme/workflow-recipes.json` contains version 1,
`repositories` with absolute Jaune/Blanc roots, and `operations`. Each operation
has one `profile`, a conservative integer `memory_gib` estimate from 1 to 8,
and `modes`, plus an optional fixed `guard`. Each mode contains an exact `argv` array and an `env` object.
For example:

```json
{
  "version": 1,
  "repositories": {"jaune": "/workspace/jaune", "blanc": "/workspace/blanc"},
  "operations": {
    "fixtures": {
      "profile": "blanc",
      "memory_gib": 4,
      "modes": {
        "validate": {
          "argv": ["/usr/bin/python3", "-B", "{repo}/scripts/gen-example-fixtures.py", "--validate-runtime"],
          "env": {}
        }
      }
    }
  }
}
```

The exact file bytes are hashed into the generated executable. Any recipe drift
refuses execution until the complete bundle is reviewed and regenerated. Recipes
are control-plane authorization inputs, not instructions taken from an unchecked
goal worktree registry. Review each against the repository's authoritative gate
catalogue and registered generator. Keep the original commands, generated-output
rules, and verdicts; do not encode a weaker replacement in a recipe.

Recipe executables are fixed Python interpreters or `/usr/bin/bash` invoking a
fixed script under `{repo}/scripts/`. Python permits only the interpreter flags
`-B` and `-s` before that script. There is no `-c`, module evaluator, or shell
expression. Arguments are passed as an array, not interpolated into a shell.
Only `{repo}` and `{creme}` placeholders are accepted. Environment keys are
limited to HOME, PATH, PYTHONNOUSERSITE, VIRTUAL_ENV, and JAUNE_T8N_TARGET;
values are reviewed constants. Inherited command environment is discarded and
LAKE_CACHE_DIR always selects canonical Creme's ignored cache.

The broker validates the worktree against the reviewed repository's Git common
directory and rejects symlink components in its root and script path. It pins
Creme's clean runtime, launcher, preflight, and recipe definitions. This does
**not** make editable repository code untrusted-code safe: that code and its
imports still execute with host user access. The memory cgroup is not filesystem
or network isolation. The capability approves this specific trusted-project
workflow; it does not claim an external filesystem sandbox exists.

The `blanc-build-certificate` guard runs the repository's certificate-status
check in an isolated interpreter before admission and gate execution. Missing
or stale certificates refuse the checkpoint recipe: refresh through the owned
build capability and the repository's certificate procedure first. An all-fresh
catalogue mode is not exposed while its prerequisite would execute bare Lake.
Do not add such a recipe until the repository preserves compilation ownership.

## Ownership and limits

The systemd service owns the same private lock used by contained builds. It
holds that lock through preflight, exclusive adaptive admission, command exit,
and release. A lost outer client cannot release it. The command inherits the
lock descriptor to preserve exclusion if its supervisor disappears. Systemd
uses control-group termination and the fixed 8 GiB memory limit, zero Blanc swap
or 1 GiB Jaune swap, and the existing Lean slice and OOM group policy.

Admission failure executes no recipe. The prospective owner is fsynced in an `ADMITTING` record before acquisition,
then marked `RUNNING`. Normal completion records the command and
release exit codes; an interrupted supervisor preserves its semaphore hold and
nonterminal record for ownership-aware recovery. Records contain a unique owner,
goal, operation and mode. A collected/missing service with a nonterminal record
is unknown, not successful or idle. Do not relaunch until supported inspection
resolves the prior job and its hold. The fixed service name also refuses a
second workflow launch while the first unit remains active.

## Installation and activation

First prepare and review recipes inside Creme, then preview the entire bundle:

```sh
python3 -m creme host-wrappers --output-dir ~/.codex/bin --rules-dir ~/.codex/rules
```

After reviewing the exact generated executables and rules, install the complete
set using the client's required approval path:

```sh
python3 -m creme host-wrappers --output-dir ~/.codex/bin --rules-dir ~/.codex/rules --write --replace
python3 -m creme doctor
```

Fully restart Codex after the bundle change. Confirm the running session loaded
the rule, then use `status` and the smallest real registered validation operation
as host controls. Source unit tests use mocked containment and do not establish
host readiness, actual memory peaks, effective runtime approvals, or successful
gate execution. Managed restrictions can still override a user rule.

Once this capability covers an operation, routine agents use it. The generic
`lean-safe-run -- COMMAND` path is reserved for human manual use; do not return
to repeated generic-runner approvals or persist its prefix. Missing recipe
coverage is repaired by reviewing the workflow's recipe set as a whole.
