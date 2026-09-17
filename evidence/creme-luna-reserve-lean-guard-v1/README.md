# Lean-session approval guard — evidence

Goal label `creme-luna-reserve-v1`; branch `claude/creme-luna-reserve-lean-guard-v1`.
No Plans worktree exists for `creme-luna-reserve-v1`, so the evidence sits here,
under the Creme worktree.

## What the guard does

In a Lean-mode brokered session, a command-execution approval request
(`item/commandExecution/requestApproval` or `execCommandApproval`) is declined by
the broker itself, and never offered to the master, when
`creme.luna_lean.forbidden_command` names it. The rule, on the tokens of the
resolved command (argv list or command string, with a nested `sh -c` /
`bash -lc` script re-split to bounded depth):

1. any token equal to `--wind-down` or starting `--wind-down=`;
2. any token whose POSIX basename is `semaphore` or `codex-reclaim-lean`;
3. any token `reclaim` or `semaphore` that follows a `creme` invocation
   (a token whose basename is `creme`, or `-m creme`), and `-m creme.reclaim`
   / `-m creme.semaphore` directly.

`creme lake-build GOAL -- <targets>` is untouched and still escalates to the
master. Non-Lean sessions are untouched.

Known gap, stated rather than claimed away: the rule is textual and fail-closed,
not airtight. A rephrasing that reaches the same effect without those tokens —
`python3 -c "import creme.reclaim; ..."`, a helper script written into the
worktree and then executed, an env-var indirection — still reaches the master,
who declines it as before. The guard removes the recurring easy case, not every
case. It is also deliberately over-broad in the other direction: any escalated
command carrying a token whose basename is `semaphore` is declined, which costs
one declined escalation with a printed reason.

## Files

- `check-sh.txt` — `./scripts/check.sh` from this worktree. Final lines:
  `Ran 842 tests in 87.226s` / `OK (skipped=1)`, exit `0`.
- `control-guard-removed.txt` — the four-line guard hook removed from
  `BrokerSession.on_server_request` and nothing else; the two new integration
  tests fail, the other 34 stay green.
- `control-guard-restored.txt` — the hook restored, all 36 green.
