# Luna reserve pseudo-subagents

Codex offers a reserve allowance of its Luna model, selected by the hidden
model slug `gpt-reserve`. It is metered on its own rate-limit bucket, separate
from the regular Codex bucket and from paid credits. A Creme master can hand a
bounded task to it as a *pseudo-subagent* and keep its own tokens for judgment.
`python3 -m creme luna-reserve` is the only supported way to do that. It keeps
every run on the reserve bucket, or stops and says loudly that it could not.

## Policy

- **Opt-in only.** A master uses Luna reserve when the user instructs it to,
  for the scope the user names. It is not a default worker tier and not a
  fallback when another client is busy or out of quota.
- **Never the regular bucket.** Running on the regular Codex bucket or on paid
  credits is a failure, not a degraded success. Nothing in this workflow
  retries on another model, falls back, or buys credits.
- **A result is a worker summary, not evidence.** The master verifies every
  claim that matters on the files, commands, and gates themselves, exactly as
  it would for any other worker.

## Task fit

Luna is a fast, economical model for simpler work. Good fits:

- inventories and searches across a repository or a sibling;
- reading and summarising large logs, diffs, gate output, or transcripts;
- mechanical edits whose correct result is easy to check;
- first-pass checks whose findings the master then confirms.

Poor fits: Lean proof or elaboration work, architecture or proof strategy,
anything whose correctness the master cannot cheaply confirm, and anything
that needs network access, builds, or the semaphore. Lean MCP access is out
of scope for version 1 (see [Lean work (planned)](#lean-work-planned)).

## Commands

```sh
python3 -m creme luna-reserve status [--json]
python3 -m creme luna-reserve run --brief FILE|- --target DIR [--write] \
    [--effort low|medium|high] [--timeout-seconds N] [--preflight-only] [--json]
python3 -m creme luna-reserve audit ROLLOUT.jsonl|THREAD_ID [--json]
```

`status` reads the bucket table without model tokens. It shows the reserve
and regular buckets (usage, credits, reset time in local time and UTC) and
whether a run would be admitted.

`run` sends one brief as one turn on a new thread and follows it to completion.
The default effort is `medium`: the reserve is a separate allowance, and a
weaker answer costs the master more to verify than a slightly larger reserve
charge. Use `low` for trivial mechanical briefs. `xhigh` and `max` are refused.
`--preflight-only` does everything up to thread creation, including the
isolation proof, and then stops without spending tokens.

`audit` re-runs the attribution check on a finished rollout, using the run's
recorded preflight bucket table when the thread came from `run`.

Exit codes are distinct: `0` success, `10` preflight refusal (nothing was
spent), `11` Codex failure (the turn failed, timed out, or the server broke),
and `12` **attribution failure**. Argument errors keep the parser's usual `2`.

Each run writes an ignored run directory under the canonical checkout's
`.creme/luna-reserve/runs/` (override with `CREME_LUNA_RESERVE_STATE`):
`verdict.json`, `preflight.json` (bucket table, admission, isolation proof),
`postflight.json`, `audit.json`, the redacted two-way `transcript.jsonl`,
`items.json`, `developer-instructions.md`, `brief.md`, and `last-message.md`.
The printed summary is a few lines: verdict, thread id, turn status, live
attribution count, token usage, reserve usage before and after, the
last-message path, and any failure lines.

The Codex binary is the one bundled with the ChatGPT desktop app on macOS; set
`CREME_LUNA_RESERVE_CODEX` to use another reviewed binary. Other operating
systems have no default and fail closed. Host-specific facts (binary version,
bucket identifiers) belong in the ignored host guidance, not here.

## How a run stays on the reserve

The capability drives `codex app-server` over JSON-RPC rather than
`codex exec`. The app server exposes live rate-limit updates, steering, and
interruption, and it lets each thread carry its own permission profile.
`codex exec` has no live accounting, and on this workflow's first host it
wrote a project-trust entry into the user configuration.

**Preflight** refuses, before any thread exists, when: the binary is missing;
the account is not a ChatGPT login; `gpt-reserve` is absent from the model
catalogue or defaults to a non-default service tier; no bucket is named
`gpt-reserve` (the reserve is identified by that limit name, never by a
hard-coded id); the reserve is reached, under a spend control, below the
remaining-share floor (default 10%), or about to reset; the reserve and regular
reset times are too close to tell apart; the regular bucket is available
(see [Open verification](#open-verification)); an earlier attribution failure
is recorded; or any model, profile, configuration, provider, service-tier, or
sandbox override is attempted on the command line. `OPENAI_*` and `CODEX_*`
variables other than `CODEX_HOME` are removed from the Codex environment.

**Isolation.** `codex app-server` has no `--ignore-user-config`, and the
user configuration must never be edited or copied (a copied `auth.json` risks
refresh-token rotation). The run's app server therefore starts with launch
overrides only: plugins, apps, sub-agents, computer and browser use, image
generation, fast tier, automatic approval review, hooks, goals, memories,
remote control, and the JavaScript REPL disabled; model, review model, and
service tier pinned; approval policy `never` with approvals routed to the
client (`approvals_reviewer="user"`); web search and notify programs off;
bundled system skills off, and every other skill that does not come from the
launch root (for example user skills under `CODEX_HOME`) disabled by path;
and every MCP server visible from the launch root (user and project layers)
replaced by a disabled stub. A bare `enabled=false` is not enough for a server
defined only in a project layer: Codex then rejects the configuration. A first
isolated zero-token server lists those MCP servers and skills. Before the
thread starts, the run server's own effective configuration, feature list,
and skills list must confirm all of it, and only repository skills under the
launch root may remain enabled; the proof is saved in `preflight.json`. A
server name or skill path that cannot be written as a safe override is a
preflight refusal.

**Pin.** `creme.codex_app_server.GuardedSession` builds every `thread/start`,
`thread/resume`, `turn/start`, and `turn/steer` request from an allowlist of
parameters. Each `thread/start`, `thread/resume`, and `turn/start` must carry
model `gpt-reserve`, approval policy `never`, and approvals reviewer `user`;
`thread/start` must also set `allowProviderModelFallback: false`. Any other
model, service tier, profile, configuration, reviewer (`auto_review` or the
legacy `guardian_subagent`), ephemeral thread, or method outside a read-only
allowlist is refused before it is sent. The `thread/start` response must
report `gpt-reserve`, the default tier, approval policy `never`, reviewer
`user`, the expected sandbox and writable roots, no network, and a rollout
path.

Approval review stays on the client route because Codex's automatic review
runs on its own reviewer model, whose billing bucket is unverified. With
approval policy `never`, a sandboxed command that would need approval simply
fails inside the turn; the default server-request handler also declines any
approval request that does arrive.

**Live guard.** Every `account/rateLimits/updated` notification is attributed
as it arrives. The notification's `limitId` label is not reliable (reserve
responses arrive labelled `codex`), so attribution compares the window length,
the reset time (within a jitter tolerance, default 300 s) against the
preflight table, and the credits shape. A snapshot matching the regular
bucket, a model reroute, a thread-settings change of model, tier, or reviewer,
remote control leaving `disabled`, an MCP server starting, or any sub-agent or
MCP tool item interrupts the turn at once.

**Rollout audit.** After the turn, the rollout must show every
`turn_context.model` as `gpt-reserve`, every token snapshot attributed to the
reserve, no reroute record, and no fast tier. A final bucket read must show no
rise in regular usage and no drop in credit balance.

**Attribution failure** (exit `12`) prints: *stop using Luna reserve and tell
the user.* It also writes `.creme/luna-reserve/ATTRIBUTION_FAILURE`, which makes
every later `run` refuse. Stop dispatching to Luna reserve, report the run
directory and thread id to the user, and let the user decide. Only the user
clears the tripwire, after reviewing that run.

## Launch root, context, and sandbox

Every thread's working directory is the canonical Creme checkout, the same
launch root Creme sessions use, so Codex loads Creme's `AGENTS.md` and
repository skills. The target directory is named in the developer
instructions and, in write mode, is the only writable root: the thread runs
under a per-thread permission profile that extends `:read-only` and grants
write access to exactly that directory, with no network and no temporary
directories. Read-only mode may read the target, the launch root, and its
siblings. Write mode refuses the launch checkout itself and any `master`
directory; give it a per-goal worktree.

`AGENTS.md` says that workers and pseudo-subagents a master dispatches never
enter the master role. The tracked contract in
`templates/luna-reserve/preamble.md` is sent as developer instructions: it
states that the thread is a worker under the current master, and it sets the
pseudo-subagent rules (no push, merge, or history rewrite; no writes under a
goal store's `master/`; no Lean elaboration or builds; no network installs or
other model clients; stay in the target; a bounded final-message format).

What loads is recorded per run: `thread/start` reports the instruction
sources (on the first host, only the launch root's `AGENTS.md`), and
`preflight.json` lists the effective configuration layers, the stubbed MCP
servers, and every skill with its scope, path, and enabled state (Creme's
repository skills stay enabled; they are Lean skills the contract forbids
using in version 1).

## Writing a pseudo-subagent brief

The brief is the user message of the turn. Keep it short and falsifiable:

- the question or the edit, naming exact files or directories;
- what to return, in the preamble's format, and how long it may be;
- what not to touch, when that is not obvious from the target;
- the evidence you want quoted (commands and their results, line numbers).

Do not put secrets, the master record, or user-reserved decisions in a brief.
Give a write-mode run the smallest target that contains the edit.

## Calling it

From **Claude Code**, run the command through the shell. A run can exceed the
client's 600-second foreground limit, so start anything that might take more
than a few minutes as a background command and watch for completion (for
example with the Monitor tool), then read the printed summary and, only if
needed, `last-message.md`. `status` and `--preflight-only` are quick.

From **Codex** or **Muse**, run the same command through the shell from the
Creme checkout. A Codex session must not substitute its own model selector or
`codex exec` for this command.

Pass `--json` when a program consumes the result.

## Verifying results

Treat `last-message.md` as a worker's claim. Check each load-bearing fact on
the target, re-run any command that decides something, inspect the diff of a
write-mode run before staging it, and run the owning repository's gates as
usual. Verify the verdict too: `verdict=PASS` and exit `0` together.

## Open verification

This workflow was first exercised while the regular bucket was exhausted, so
no run has yet shown that a reserve run leaves an *available* regular bucket
untouched. Until that is shown, `run` refuses while the regular bucket is
available. After the regular reset, the master performs one tiny read-only run
with `--allow-regular-available --effort low`, confirms exit `0`, a live and
rollout attribution to the reserve, and unchanged regular usage before and
after, and records the result. Only then may routine runs use that flag.

## Broker (planned)

Version 1 runs one turn per invocation. A later broker keeps
`GuardedSession`s alive per thread behind one-shot CLI calls that any client
can drive and watch (Claude Code through background commands and its Monitor
tool): `start`, `send` (a new turn), `steer` (`turn/steer` on the active
turn), `interrupt`, `tail` (the redacted transcript), `read`
(`thread/items/list`), `approve` (queued server requests, answered by the
master, never by automatic review), and `stop`. The session's request policy,
live guard, transcript, and server-request handler are already shaped for
that; every broker call must go through the same pins, preflight, and
postflight audit, and an attribution failure trips the same tripwire.

## Lean work (planned)

Version 1 forbids Lean. A Lean-capable pseudo-subagent is a separate,
master-scoped extension, not a flag:

- **Selective isolation.** Keep the version 1 disables (plugins, apps, other
  MCP servers, computer use, fast tier, native sub-agents, automatic review),
  but keep the project trust that Creme's project layer needs, and keep the
  user's own Lean relay permission profile and execpolicy rules (host
  guidance names them) instead of the version 1 write profile.
- **Guarded Lean server.** Replace the disabled stub for Creme's project
  `lean-lsp-mcp` with Creme's guarded launcher, started from cwd `~/creme` as
  the project layer defines it. No other MCP server returns.
- **Approvals.** MCP and sandbox approvals go to the broker's `approve` call,
  answered by the master; never `auto_review`.
- **Memory.** Each Lean pseudo-subagent is a language-server worker and counts
  against the host's one-language-server-worker memory rule; the master
  schedules it like any other Lean worker.
- **Builds.** Only through `~/creme/scripts/creme lake-build GOAL -- TARGETS`
  under semaphore admission; never bare `lake build`.
- **Stop.** `python3 -m creme reclaim --wind-down GOAL` runs at stop, and the
  run is not reported idle until wind-down reports `OK`.
