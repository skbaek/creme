# Worker brief — `{{GOAL_ID}}`

Replace every placeholder before dispatch and store the filled brief under the
goal store's ignored `master/briefs/`. General policy lives in `AGENTS.md` and
the guides it links; this brief states only what is specific to the work.

## Objective

{{OBJECTIVE}}

## Exact starting refs

| Repository | Starting commit | Upstream or dependency ref |
|---|---|---|
| `{{REPOSITORY}}` | `{{START_COMMIT}}` | `{{UPSTREAM_REF}}` |

Dependency relationship and required ancestry:
{{DEPENDENCY_RELATIONSHIP}}

## Read-first sources

Read in full before acting, in this authority order: `{{PRIMARY_AUTHORITY}}`,
`{{REPOSITORY_INSTRUCTIONS}}`, `{{GATE_CATALOGUE}}`.

## Owned repositories and paths

| Repository | Owned paths | Explicit exclusions |
|---|---|---|
| `{{REPOSITORY}}` | `{{OWNED_PATHS}}` | `{{EXCLUDED_PATHS}}` |

Preserve unrelated state. Do not edit, stage, or commit outside this
allocation.

## Per-goal worktrees and branches

| Repository | Worktree | Branch |
|---|---|---|
| `{{REPOSITORY}}` | `{{WORKTREE}}` | `{{BRANCH}}` |

Return local commits on this branch; the master pushes and merges.

## Resource class and coordination

Resource class: `{{RESOURCE_CLASS}}`. Light work takes no hold. Builds go
through the owned-build wrapper, which sizes and admits them; follow
[execution](../../docs/guides/execution.md#resource-classes) for waits and
renewal.

## Convergence gate

- Required candidate gate: `{{CONVERGENCE_GATE}}`
- Exact full-checkpoint command: `{{FULL_CHECKPOINT_COMMAND}}`
- Required false-positive or mutation control: `{{CONTROL_THAT_BITES}}`
- Evidence acceptance rule: `{{CONDITION_TO_EVIDENCE_RULE}}`

## Autonomous and reserved decisions

- Worker-autonomous decisions: {{AUTONOMOUS_DECISIONS}}
- Master-only decisions: {{MASTER_DECISIONS}}
- User-reserved decisions: {{USER_RESERVED_DECISIONS}}

Generated output never makes a reserved change autonomous; see the master
guide's [registered-provenance rule](../../docs/guides/master.md#registered-provenance-is-a-narrow-exception).

## Expected checkpoints

| Boundary | Required commit/state update | Required evidence |
|---|---|---|
| `{{CHECKPOINT_BOUNDARY}}` | `{{CHECKPOINT_ARTIFACT}}` | `{{CHECKPOINT_EVIDENCE}}` |

At every coherent green boundary, inspect the complete diff, stage explicit
owned paths, commit, and update the state brief in place.

## State, report, and evidence paths

- State brief: `{{STATE_BRIEF}}`
- Final report: `{{FINAL_REPORT}}`
- Evidence tree: `{{EVIDENCE_TREE}}`

A chat summary is not acceptance evidence.

## Pause and reacquisition

On a pause request, stop at a safe boundary, commit the coherent owned checkpoint
(unfinished work goes on a labeled recovery branch), update the state brief with
the exact next unit, and return. If this worker opened a Lean server or took a
goal hold, run `python3 -m creme reclaim --wind-down {{GOAL_ID}}` first. Do not
reacquire a hold or resume work until `{{REACQUISITION_CONDITION}}` is true.

## Return contract

Return a bounded condition/evidence digest: exact commits and changed owned
paths; each required condition and its evidence; every exact command run and
its terminal verdict; open findings, decisions, and blockers; the next coherent
unit. The worker's statement that the task is complete is never acceptance
evidence.
