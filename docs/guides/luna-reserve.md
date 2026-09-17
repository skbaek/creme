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

Brokered sessions (see [Broker](#broker)) add `start`, `send`, `steer`,
`interrupt`, `wait`, `events`, `read`, `approve`, `detail`, `list`, `stop`,
`resume`, and `shutdown`.

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
model `gpt-reserve`, the session's approval policy (`never`, or `on-request`
for a brokered write session), and approvals reviewer `user`;
`thread/start` must also set `allowProviderModelFallback: false`. A follow-up
`turn/start` must carry the session's effort and may not start while a turn is
active; a `turn/steer` must name the session's active turn and carries no
model, tier, or policy key; `turn/start`, `turn/steer`, `turn/interrupt`, and
`thread/resume` must name the session's own thread; and once any guard failure
is recorded, only `turn/interrupt` is still permitted. Any other
model, service tier, profile, configuration, reviewer (`auto_review` or the
legacy `guardian_subagent`), ephemeral thread, or method outside a read-only
allowlist is refused before it is sent. The `thread/start` response must
report `gpt-reserve`, the default tier, approval policy `never`, reviewer
`user`, the expected sandbox and writable roots, no network, and a rollout
path.

Approval review stays on the client route because Codex's automatic review
runs on its own reviewer model, whose billing bucket is unverified. With
approval policy `never` (every `run`, and every read-only brokered session), a
sandboxed command that would need approval simply fails inside the turn; the
default server-request handler also declines any approval request that does
arrive. A brokered write session uses `on-request` instead, and its approvals
go to the master (see [Approvals](#approvals)).

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
needed, `last-message.md`. `status` and `--preflight-only` are quick. For
anything that may need steering, follow-up, or approvals, use a brokered
session instead (see the [Claude Code recipe](#claude-code-recipe)).

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

## Broker

`run` is one turn per invocation. A brokered session keeps a guarded Codex
thread alive between one-shot commands, so a master in any client can start a
task, steer it, give follow-up orders, interrupt or stop it, answer its
approval requests, and read its transcript on demand.

```sh
python3 -m creme luna-reserve start --brief FILE|- --target DIR [--write] \
    [--effort low|medium|high] [--detail silent|summary|live] [--timeout-seconds N]
python3 -m creme luna-reserve send SESSION (--text TEXT | --brief FILE|-)
python3 -m creme luna-reserve steer SESSION (--text TEXT | --brief FILE|-)
python3 -m creme luna-reserve interrupt SESSION
python3 -m creme luna-reserve wait SESSION [--timeout SECONDS]
python3 -m creme luna-reserve events SESSION [--follow] [--last N] [--since SEQ]
python3 -m creme luna-reserve read SESSION [--lines N | --items N]
python3 -m creme luna-reserve approve SESSION APPROVAL accept|decline|cancel
python3 -m creme luna-reserve detail SESSION silent|summary|live
python3 -m creme luna-reserve list [--limit N]
python3 -m creme luna-reserve stop SESSION
python3 -m creme luna-reserve resume THREAD_ID [--target DIR] [--write] [--effort E]
python3 -m creme luna-reserve shutdown
```

- `start` prints a session id and returns as soon as the first turn is
  accepted. Preflight, isolation proof, and admission run first, so a refusal
  (exit `10`) still spends nothing.
- `send` starts a new turn on an idle session and steers a running one;
  `steer` only steers. Every new turn first makes its own zero-token
  admission read and is refused (exit `10`) when the reserve is no longer
  admitted.
- `wait` blocks until the session is idle, needs attention (an approval), or
  has ended, then prints a few lines: state, last turn status and verdict,
  thread token totals (cumulative for the thread, not per turn), pending
  approvals with the decisions the server offers, and the first lines of the
  final message. Its
  exit code is `0` (turn passed), `10` (refused), `11` (Codex failure or lost
  session), `12` (attribution failure), `20` (approval pending), `21`
  (interrupted), or `124` (timeout).
- `events` prints one line per event at the session's detail level; with
  `--follow` it keeps printing and exits when the session ends.
- `read` prints the latest final message (at most 60 lines), or the last N
  thread items (at most 50). The full redacted transcript stays on disk.
- `stop` interrupts a running turn, cancels pending approvals, re-audits the
  whole rollout turn by turn, and closes the app server. Records are kept.
- `resume` attaches a new session to a recorded thread, after a broker
  restart, a stop, or a master succession. Target, mode, and effort default to
  the thread's latest record. It runs a new preflight and `thread/resume`
  under the same pins; send the follow-up with `send`.

Every command prints a bounded result of a few lines; `--json` prints the full
record instead.

**Detail levels.** `silent` (the default) shows only attention events: a turn
completed, failed, or was interrupted; an approval is needed; a billing alarm;
a session refused, failed, or stopped. `summary` adds the final message's path
and a one-line excerpt. `live` adds filtered progress: turn start, steer,
command start and exit, file changes, and completed agent messages. Token
deltas never appear. `detail` changes the level mid-run; `events` applies the
level current when it prints.

**Guards.** The broker adds no guard logic of its own. Each session runs
through the same `ReserveServer` preflight (probe, admission, isolation
proof) and `GuardedSession` pins as `run`, with one app server per session.
Every `account/rateLimits/updated` is attributed live against the current
turn's admission read; a mismatch, reroute, or other guard failure interrupts
the turn at once, records the shared `ATTRIBUTION_FAILURE` tripwire, and
stops every session in the broker. A tripwire recorded by any other run also
stops every session within a second. The rollout audit runs on each turn's
own slice of the rollout at turn end and again for every turn at `stop`;
before `resume` continues a thread, any turn that ended with a crashed broker
is audited first. A lost app server during a turn is an attribution failure,
as in `run`.

### Approvals

A read-only session keeps the `run` policy (`never`; any approval request is
declined). A write session uses approval policy `on-request` with reviewer
`user`: work inside the target runs under the write profile without asking,
and a command or patch that needs to leave the sandbox (for example a write
under `.git`, or network access) is queued as an attention event with an id
such as `a1` and a bounded description. The turn waits until the master
answers with `approve SESSION a1 accept|decline|cancel`. Nothing is answered
automatically. Session-wide and policy-amending decisions
(`acceptForSession`, execpolicy or network rules) are not offered, because
they would approve later commands the master has not seen or change standing
configuration. Permission-profile grants and other server requests are
refused as in `run`. `untrusted` was not chosen: it would ask for every
ordinary command inside the target and spend the master's attention on
routine work.

### Process, socket, and records

The broker is a long-lived process started on first use by `start` or
`resume`. Its state lives under the Luna reserve state directory
(`.creme/luna-reserve/`, or `CREME_LUNA_RESERVE_STATE`):

- `broker/broker.sock`, mode `0600` in a `0700` directory; each connection's
  peer uid is checked where the platform reports it (macOS
  `LOCAL_PEERCRED`, Linux `SO_PEERCRED`), and a different uid is refused;
- `broker/broker.json` (pid and a random instance token) and `broker.log`;
- `sessions/<id>/`: `session.json` (the registry record: thread id, target,
  mode, state, turns with verdicts, pending approvals, last event),
  `events.jsonl`, `transcript.jsonl`, `preflight.json`, and
  `turns/<n>/` with `brief.md`, `preflight.json`, `postflight.json`,
  `audit.json`, `items.json`, and `last-message.md`.

A client uses a broker only when a ping returns the recorded pid and instance,
and `start` or `resume` also requires that the broker runs the client's own
checkout and code digest; an idle broker on other code is replaced, and one
holding open sessions is refused.
Otherwise it replaces the broker: it signals a recorded pid only if that
process's command line carries the recorded instance token, removes a stale
socket, and starts a new broker. The broker exits on `shutdown`, and after ten
minutes with no open session; a session idle for thirty minutes is stopped
(records kept, resumable). A new broker marks sessions of a dead broker
`lost` and stops their app server only if it is orphaned and carries this
capability's pinned launch arguments. It never signals or reuses any other
`codex app-server`, including the ChatGPT desktop app's. Nothing a successor
needs lives only in broker memory: `list`, `events`, `wait`, and `read` work
from the records, and `resume` continues a thread.

### Claude Code recipe

1. `python3 -m creme luna-reserve start --brief brief.md --target DIR --effort low`
   returns a session id in a second or so.
2. For one completion notification, run `python3 -m creme luna-reserve wait
   SESSION --timeout 3600` as a background command. For live progress, start
   the Monitor tool on `python3 -m creme luna-reserve events SESSION --follow`
   after `detail SESSION live`; each line is one notification, and the command
   exits when the session ends.
3. Redirect with `send SESSION --text "..."` (steers a running turn, or starts
   the next turn), stop work with `interrupt SESSION`, and answer an approval
   line with `approve SESSION a1 accept` or `decline`.
4. Verify with the master's own commands (`git diff`, a test, a grep on the
   target) rather than reading the transcript; read `read SESSION` only when
   a command cannot settle the question.
5. `stop SESSION` when done, then check that its line says
   `stop_audit=PASS`.

Codex and Muse masters use the same commands through their shell. The run
directory of a session is never a substitute for verification: its final
message is a worker summary.

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
