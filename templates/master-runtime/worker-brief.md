# Worker brief — `{{GOAL_ID}}`

Replace every placeholder before dispatch. This filled brief is private
runtime state; store it beneath the configured goal store's ignored
`master/briefs/` directory, never in this tracked template.

## Objective

{{OBJECTIVE}}

## Exact starting refs

| Repository | Starting commit | Upstream or dependency ref |
|---|---|---|
| `{{REPOSITORY}}` | `{{START_COMMIT}}` | `{{UPSTREAM_REF}}` |

Dependency relationship and required ancestry:
{{DEPENDENCY_RELATIONSHIP}}

## Read-first sources

Read each source in full before acting, in this authority order:

1. `{{PRIMARY_AUTHORITY}}`
2. `{{REPOSITORY_INSTRUCTIONS}}`
3. `{{GATE_CATALOGUE}}`
4. `{{ADDITIONAL_READ_FIRST_SOURCE}}`

## Owned repositories and paths

| Repository | Owned paths | Explicit exclusions |
|---|---|---|
| `{{REPOSITORY}}` | `{{OWNED_PATHS}}` | `{{EXCLUDED_PATHS}}` |

Preserve unrelated state. Do not edit, stage, commit, merge, or push outside
the allocation above.

## Per-goal worktrees and branches

| Repository | Worktree | Branch |
|---|---|---|
| `{{REPOSITORY}}` | `{{WORKTREE}}` | `{{BRANCH}}` |

Shared default-branch checkouts remain untouched. Commit and push only the
dedicated branches authorized by this brief.

## Resource class and coordination

- Resource class: `{{RESOURCE_CLASS}}`
- Conservative peak estimate when required: `{{MEMORY_GIB}}` GiB
- Acquire or wait command: `{{ACQUIRE_OR_WAIT_COMMAND}}`
- Renewal command: `{{RENEW_COMMAND}}`
- Release or wind-down command: `{{RELEASE_OR_WIND_DOWN_COMMAND}}`

Light work takes no hold. Any elaborating command uses the owned-build wrapper
and the repository's gate catalogue; never substitute an uncoordinated build.

## Convergence gate

- Required candidate gate: `{{CONVERGENCE_GATE}}`
- Exact full-checkpoint command: `{{FULL_CHECKPOINT_COMMAND}}`
- Required false-positive or mutation control: `{{CONTROL_THAT_BITES}}`
- Evidence acceptance rule: `{{CONDITION_TO_EVIDENCE_RULE}}`

## Autonomous and reserved decisions

- Worker-autonomous decisions: {{AUTONOMOUS_DECISIONS}}
- Master-only decisions: {{MASTER_DECISIONS}}
- User-reserved decisions: {{USER_RESERVED_DECISIONS}}

<!-- provenance-rule:start -->
> Registered-provenance exception: The master may approve a registered
> generator's identity/provenance output caused solely by an already-authorized
> source/input change if and only if the registered check is green, a relevant
> falsifier bites, the diff is exact generator output, and no semantic reference
> changes; generation never makes a reserved change autonomous, and an ambiguous
> mixed diff must be separated or escalated as a decision packet.
<!-- provenance-rule:end -->

Pins or references, weakened baselines or budgets, allowlist growth, goldens,
timeouts, publication, public claims or counts, licenses, external messages,
spending, and dependent public contracts remain reserved even when generated.
Consult the decision table in the master guide; do not relabel a mixed diff as
provenance.

## Expected checkpoints

| Boundary | Required commit/state update | Required evidence |
|---|---|---|
| `{{CHECKPOINT_BOUNDARY}}` | `{{CHECKPOINT_ARTIFACT}}` | `{{CHECKPOINT_EVIDENCE}}` |

At every coherent green boundary, inspect the complete diff, stage explicit
owned paths, commit, push when authorized, and update the state brief.

## State, report, and evidence paths

- State brief: `{{STATE_BRIEF}}`
- Final report: `{{FINAL_REPORT}}`
- Evidence tree: `{{EVIDENCE_TREE}}`

These files, exact candidate commits, and terminal verdicts are durable
evidence. A chat summary is not acceptance evidence.

## Pause and reacquisition

On a pause request, finish or stop at a safe boundary, inspect the full diff,
commit the coherent owned checkpoint, update the state brief with the exact
next unit, and return. If this worker opened a Lean server or took a goal hold,
run goal-scoped `python3 -m creme reclaim --wind-down {{GOAL_ID}}` first. Do not
reacquire a hold or resume work until `{{REACQUISITION_CONDITION}}` is true.

## Return contract

Return a bounded condition/evidence digest containing:

1. exact commits and changed owned paths;
2. each required condition and its inspectable evidence;
3. every exact command run and its terminal verdict;
4. open findings, decisions, and blockers; and
5. the next coherent unit.

The digest points to durable evidence; it does not replace that evidence, and
the worker's statement that the task is complete is never acceptance evidence.
