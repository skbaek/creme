# Escalation procedure

Routine work should proceed under the user's existing authorization. A client
approval request, a memory refusal, and a repository decision are different
conditions with different remedies. Do not turn each into a new question about
whether the user still wants the task done.

## Identify the boundary

Before requesting user input, classify the actual operation:

| Boundary | Evidence to inspect | Next action |
|---|---|---|
| Task authorization | Current request, prior answers, goal and intent | Proceed with authorized implementation choices. Ask only for an unresolved reserved decision under the master protocol. |
| Client access or command approval | Tool result and effective sandbox/approval policy | Use the client's required approval mechanism for the concrete operation. Do not add a duplicate conversational permission question. |
| Host resource admission | Capability refusal, live admission result, owned job state | Wait through the supported mechanism, narrow the work when valid, or perform independent light work. A memory refusal is not a request for broader filesystem permissions. |
| Repository evidence restriction | Exact applicable goal or gate rule and changed artifact | Preserve the restriction; prepare the concrete reconciliation packet when required. Command approval cannot authorize changing a frozen baseline. |

Follow higher-priority platform instructions on sandbox retries and approval
requests. Never route a denied operation through another tool, shell, MCP
server, or writable wrapper to evade the boundary. Conversely, do not request
host execution merely because a command is unfamiliar, slow, or uses a sibling
repository already inside the granted workspace.

If a local instruction requires a user decision, name and link the source,
quote the applicable instruction, and explain its application. Distinguish an
explicit requirement from an agent's interpretation. Prior user authorization
can settle a task decision; it cannot override managed platform restrictions.
If automatic approval review rejects an action, identify the action and the
reported reason explicitly instead of presenting it as the agent's preference.

## Inventory coverage before execution

At the first execution checkpoint, record one compact inventory in the goal's
state brief. Cover the operations the goal actually needs: status and telemetry,
Lean inspection, builds, selected and full gates, fixture validation and
registered generation, replay, and final publication if applicable. For each,
record the canonical entry point, required paths, resource class, available
capability, and any observed approval restriction. Unknown is a valid finding;
it is not evidence of permission or of denial.

Use doctor and host guidance for the installed capability facts. Inspect the
client's effective policy when that information is available; a config file is
only one input and does not prove the running configuration. Keep these facts
separate:

- Installed delegates and rules match the expected files on disk.
- The running client loaded those rules and selected the intended permission
  profile. A pending restart means these facts have not been established.
- A tool's command matches a rule. Shell composition, an alternate executable
  path, or a different operation may change the match.
- Managed requirements and MCP tool approval policies may impose additional
  restrictions. An execution-command allow rule does not authorize an MCP tool.

Record the exact source of observed prompts without copying secrets or full
client configuration into reports. Group repeated prompts by cause. Once a
capability gap is known, plan a coherent workflow-level remedy; do not spend
successive turns discovering the same gap with different script prefixes.

A generic runner that accepts an arbitrary executable after `--` remains a
broad host execution capability. Cgroup limits do not turn it into filesystem
isolation. Do not persist its prefix, a shell/interpreter prefix, or the prefix
of a temporary or worktree script as a substitute for a reviewed capability.
One-time execution approval remains available when required by client policy.

## Repair the correct layer

First remove agent-generated redundant confirmations and choose existing
supported capabilities. For eligible platform prompts, examine whether the
installed client and managed policy support automatic approval review with the
existing sandbox retained. Treat reviewer routing, task authorization, sandbox
access, and tool approval as distinct settings. Do not silently change the
user's or organization's approval policy, infer support from an example config,
or claim automatic review guarantees approval.

If routine operations still require missing host capabilities, prepare one
reviewable implementation covering the required operation families, with typed
arguments, validated paths, and controls that reject out-of-scope inputs. A
restricted operation parser alone does not make execution of writable project
code harmless; document its trust boundary and actual isolation. Review the
complete implementation and its tests before the final installation approval.
Batch compatible capability changes and verify the complete installed bundle
before the required client restart. These guidance changes themselves remove
no platform prompts and authorize no new host operations.

## Preserve execution ownership

For each launched unit retain its goal, exact command, client execution handle,
service identifier when supplied, log location, and terminal result. Distinguish
pending approval, running, terminal, and unknown. Do not describe a pending
request as an executing job or a timeout as a completed job.

Before retrying after interruption, recheck the original handle and, through an
authorized capability, any service that can outlive the launcher. Missing client
handles alone do not prove an external service stopped. Do not launch duplicate
work, release its resource ownership, or claim a quiet host from an unrelated
process snapshot. If inspection is unavailable, preserve the uncertainty and
continue independent light work. A restart requirement blocks the affected
execution path, not all unrelated work; ask for necessary confirmation once and
retain it as a pending dependency until evidence resolves it.
