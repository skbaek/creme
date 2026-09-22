# Native Codex approval review

Use Codex's separate reviewer for risk-based decisions on eligible escalation
requests. The explicit configuration is:

```toml
approval_policy = "on-request"
approvals_reviewer = "auto_review"
```

`python3 -m creme client-profile --auto-review` previews these keys together
with Creme's existing restricted workspace permission profile. Omitting the
option preserves the ordinary generator output. Select the generated profile
with a supported `--profile` option, or merge the reviewed keys into the
client's active configuration; a preview alone does not change the client.
See the version-sensitive [CLI setup](../setup.md#codex-cli). Keep the sandbox,
sibling roots, and host memory containment in place. This feature does not
require new command allow rules or a custom execution broker.

The native reviewer assesses actions that already require approval. It can
approve or reject them; a rejection directs the agent to a safer alternative
or a human decision. It does not guarantee zero human decisions. Managed
requirements still apply. See the [official OpenAI auto-review
documentation](https://learn.chatgpt.com/docs/sandboxing/auto-review).

## Verify the running conversation

Run `python3 -m creme doctor` from the active Creme session. Its approval rows
keep the following evidence separate:

- User and project configuration: reviewer intent on disk, including legacy
  inline profiles selected within those files. CLI-selected
  `<name>.config.toml` layers, managed configuration, and running-client
  overrides are not resolved by these disk rows.
- Saved desktop mode: the local desktop preference, when this client exposes
  it. A saved `guardian-approvals` preference does not prove the conversation
  uses automatic review.
- Recorded approval routing: the last `turn_context` for the invoking
  `CODEX_THREAD_ID` (or compatibility `CODEX_SESSION_ID`), including its reviewer
  and restricted boundary. The diagnostic never chooses somebody else's
  latest conversation as a substitute.
- Recorded thread settings: an own-thread `thread_settings_applied` event,
  when present. These defaults govern subsequent turns; they do not prove the
  active turn adopted them. Only the approval fields are retained.

`AUTO_REVIEW_INACTIVE` fails when disk or desktop state requests automatic
review but the recorded reviewer is `user`. The mismatch can occur with a
named custom permission profile whose configuration omitted
`approvals_reviewer`: the default reviewer is `user`. Explicitly select native
automatic review in the running client, or start a new conversation using the
updated configuration, then check again. A changed saved preference alone
does not close the mismatch.

An `OK` context row establishes what the latest context says. It is not a
live settings query: a current-turn settings update may not yet have produced
a new context record. If a thread-settings event follows that context in the
record, doctor reports `CONTEXT_PREDATES_SETTINGS` as a warning instead of
treating the old reviewer as current or claiming the new defaults are active.
A subsequently recorded context restores the normal context verdict. Malformed
settings records leave the evidence unverified. Runtime acceptance requires
native confirmation that the current-turn update applied, plus an actual
harmless, non-allowlisted escalation to receive native automatic approval,
with the intended permission profile still active. Use a safe rejection
control to verify the reviewer can still refuse inappropriate actions; do
not run a destructive action to test refusal.

Missing, unsupported, inaccessible, malformed, or older metadata produces
`UNVERIFIED`, never a guessed activation. Python versions without `tomllib`
or `tomli` cannot parse configuration for this optional diagnostic; session
evidence remains independently available. These observations do not authorize
execution or change settings. The diagnostic decodes only recognized context
and thread-setting envelopes in the invoking session's record; unrelated event
messages are not decoded. It emits allowlisted fields; conversation content,
reviewer reasoning, credentials, session IDs, and rollout paths are excluded.
The supported compatibility spelling `guardian_subagent` is normalized to
`auto_review`; generated configuration uses the canonical `auto_review` name.
Built-in permission-profile IDs such as `:workspace` remain visible.

## Host delegates

If the Codex sandbox denies telemetry or reclamation, use only a generated
host delegate that `python3 -m creme doctor` reports as current:

```sh
~/.codex/bin/codex-host-telemetry
~/.codex/bin/codex-reclaim-lean --dry-run
~/.codex/bin/codex-reclaim-lean --wind-down GOAL
```

These stable approval targets dispatch back into the canonical Creme checkout.
Their generated rules allow only telemetry, reclamation `--dry-run`, and
goal-scoped `--wind-down`; stronger reclamation remains approval-gated. Never
persist a prefix for a shell script under `/tmp`, a goal worktree, or another
sandbox-writable path. Never use a copied standalone helper or a bundle that
`doctor` marks stale; preview and regenerate the complete delegates-and-rules
set with `python3 -m creme host-wrappers`, then fully restart Codex, because
rules load only at process startup.

If the bundle includes `codex-creme-contained-build`, use it for a
host-contained owned build instead of persistently approving `systemd-run`, a
safe runner, or a temporary shell script; a drift refusal means preview and
reinstall the bundle after review, never bypass the pin. If it includes
`codex-creme-contained-workflow`, use its reviewed operations for registered
gates and fixture work under [the recipe contract](contained-workflow.md); the
generic `lean-safe-run -- COMMAND` path is for humans only.
