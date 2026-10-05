# Muse pseudo-subagents

`python3 -m creme muse` hands a bounded brief to Muse (Meta's coding agent,
`~/.local/bin/muse`, override `CREME_MUSE_BIN`) and keeps the run bounded,
model-pinned, and steerable. It drives `muse serve` over the Muse Session
Protocol (MSP, JSON-RPC over stdio) through the same broker plumbing as the
[Luna reserve](luna-reserve.md) broker (`creme/pseudo_broker.py`), so a master
uses one recipe for both.

## Policy

- **On user instruction, like Luna reserve and Antigravity.** The user
  instructed use on 2026-09-28: for model-fit calibration and to save Claude
  tokens. It is not a fallback when another client is busy.
- **One model: `muse-spark-1.3`.** Every session is pinned to it explicitly.
  A model id containing `contributor` is never selected, and the server's
  catalogue default *is* the contributor variant (its content may be used for
  product improvement), so an omitted model anywhere is a bug. Any served-model
  evidence of another model (`session/modelChanged`, `session/tokenUsage`,
  `session/modelRouteUnserved`, the durable session log) fails the turn closed,
  records the `MODEL_PIN_FAILURE` tripwire, and stops every session. Clearing
  it is an explicit master action after reviewing the run (see
  [Clearing the tripwire](#clearing-the-tripwire)).
- **A result is a worker summary, not evidence.** Verify every claim that
  matters on the files, commands, and gates themselves.
- **Never modify the user's Muse configuration**, auth, or trust files. Nothing
  here writes, copies, or links them; the Lean-mode check reads
  `settings.json` only.

## Task fit

Good fits: inventories and searches, summarising logs or diffs, mechanical
edits whose result is easy to check, first-pass checks, and mechanical Lean
work in a Lean-mode session (a named edit, a diagnostics sweep, a narrow
build). Poor fits: anything needing the network, hard proof strategy, or work
whose correctness the master cannot cheaply confirm. Effort comes from the
[episode runtime](model-fit.md) (`start --episode ID`; routes `muse-broker`,
`muse-run`); `$GOAL_STORE/model-fit/muse.md` is historical. As a cold default,
start read-only work at `low` or `medium` and write or Lean work at `high`.

## Commands

```sh
python3 -m creme muse status [--json]
python3 -m creme muse run --brief FILE|- --target DIR [--write] \
    [--effort minimal|low|medium|high|xhigh|max] [--timeout-seconds N] [--json]
python3 -m creme muse start --brief FILE|- --target DIR [--write | --lean GOAL] \
    [--effort E] [--detail silent|summary|live] [--timeout-seconds N]
python3 -m creme muse send SESSION (--text TEXT | --brief FILE|-)
python3 -m creme muse steer SESSION (--text TEXT | --brief FILE|-)
python3 -m creme muse interrupt SESSION
python3 -m creme muse wait SESSION [--timeout SECONDS]
python3 -m creme muse events SESSION [--follow] [--last N] [--since SEQ]
python3 -m creme muse read SESSION [--lines N]
python3 -m creme muse approve SESSION APPROVAL accept|decline
python3 -m creme muse detail SESSION silent|summary|live
python3 -m creme muse sessions [--limit N]          # alias: list
python3 -m creme muse stop SESSION
python3 -m creme muse resume MUSE_SESSION_ID [--effort E]
python3 -m creme muse shutdown
```

The names and semantics are Luna's: `send` starts a new turn on an idle session
and steers a running one, `steer` only steers, `wait` blocks until idle, an
approval is pending, or the session ended, and every command prints a few
bounded lines (`--json` prints the full record). `resume` attaches a new broker
session to a Muse session this state directory recorded, with the same target,
mode (its permission profile was fixed at bootstrap), and pins; `--effort`
may change the effort. `run` is one turn with no master to ask and has no
Lean mode.

Exit codes: `0` pass, `10` refused before any model call, `11` Muse failure
(turn failed, timed out, host lost, or a read-only target changed), `12`
model-pin failure (tripwire recorded: stop and tell the user), and for `wait`
also `20` (approval pending), `21` (interrupted), `124` (timeout).

`status` spends no model tokens: binary and version, `model/list` (the pin
present with every effort, and which row is the catalogue default, never used),
`usage/read`, the Lean MCP definition check, and the fixed posture of each mode.

## How a session is built

Measured on the first host (Muse 1.4.0, 2026-09-28; the probes are in the goal
store report `reports/creme-muse-pseudo-v1.md`):

1. **Bootstrap with the echo provider.** `muse serve` composes a new session's
   permission profile from the user's saved `permissions.default_profile`. That
   is `:auto-review` for the user's interactive sessions, and a serve host
   refuses it ("the automated reviewer is unavailable on this host") in every
   approval mode. A Creme-owned config root (`XDG_CONFIG_HOME`) starts sessions
   but has no credentials (`authRequired`), and copying or linking `auth.json`
   is out of bounds. So the session is created by
   `muse exec --json --provider echo --session-id UUIDv7 --permission-profile P
   --no-foreign-personal-context --disable-web-tools --workspace TARGET`: the
   echo provider makes no model call, and the session's permission bootstrap is
   durable, so `session/resume` on a serve host reuses it.
2. **Attach and pin.** A `muse serve` host with the mode's sandbox flags
   resumes the session. The guard then sends `session/setModel`
   (`muse-spark-1.3`, provider `meta`), `session/setApprovalMode`
   (`promptUnmatched`), and `session/setReasoningEffort`, and reads them back:
   `model/list` for the session must show exactly the pin active and offering
   the effort, and `session/read` must report the pin and the approval mode.
   Only then may a turn start. `turn/start` and `turn/steer` carry the effort
   explicitly (MSP turns carry no model field; the model is the session's).
3. **Environment.** The Muse child's environment drops `MUSE_*` (including
   `MUSE_MODEL`), `TBH_*`, `META_*`, and other providers' keys, and sets
   `MUSE_NO_AUTO_UPDATE=1` so the launcher never swaps the binary under a
   session. It also sets `TBH_STREAM_IDLE_TIMEOUT_SECS=900`: Muse aborts a
   model call whose stream is silent for 3 minutes ("model stream idle
   timeout"), which killed long-context Lean turns, so every child (bootstrap
   `exec` and `serve`) gets the longer idle budget. Override with
   `CREME_MUSE_STREAM_IDLE_TIMEOUT_SECS` (a positive integer of seconds); the
   first-event timeout is left unset.

The pseudo-subagent never uses the user's `musec` launcher and never relies on
saved Muse state for its sandbox: each serve host's posture is chosen per
process from the mode.

Every contract also carries the shared-library rule from `templates/shared/library-first.md` (check the shared library first; hoist generically applicable results into it).

## Sandbox and approvals

| mode | profile | `muse serve` flags | what the sandbox allows |
|---|---|---|---|
| read-only | `:read-only` | `--disable-write --sandbox-network restricted` | shell reads anywhere; no writes; no network; no `os.nice` |
| write | `:ask-me` | `--sandbox-network restricted` | writes in the workspace and temporary directories only; no network |
| lean | `:ask-me` | `--disable-sandbox` | unsandboxed shell; file tools still confined to the workspace |

Every session uses approval mode `promptUnmatched`, so every shell command, MCP
tool call, and unmatched access reaches the broker, which decides it from a
fixed allowlist (`creme/muse_client.py` `decide_approval`). It is never an LLM
approval judge and never `:auto-review`, and it only ever picks a one-shot
choice (`scope: once`): session-wide and `localPersistent` choices (which
would write standing rules) are never taken.

- read-only and write: a shell command is approved once (the sandbox is the
  control); every MCP tool, network, file-access, protected-write, subagent, or
  unknown subject is aborted.
- lean: the only shell command approved by rule is the exact owned build
  `~/creme/scripts/creme lake-build GOAL [--walk] [--wait N] -- MODULES` (N 1–900, no
  sizing flags, no shell metacharacters, workspace = target). Semaphore,
  reclaim, and wind-down commands are aborted (the Luna Lean guard, reused).
  **Every other shell command, including plain reads such as `git status`,
  waits for the master** (`approve SESSION aN accept|decline`): the host is
  unsandboxed, and an argv allowlist of "read-only" commands was shown to be
  bypassable (`git diff -o FILE` writes anywhere, `git --exec-path=DIR` and
  `rg --pre CMD` run programs, `cat`/`git -C` read outside the target; review
  of 2026-09-28). Reading and searching go through Muse's own file tools,
  which stay confined to the workspace on the unsandboxed host too (see
  below). The `lean-lsp-mcp` tools are approved once except the
  network-reaching search tools and `lean_build`/`lean_profile_proof`.
  Answer a master approval only after reading the whole command: it runs
  unsandboxed with the user's privileges.

Why Lean mode is unsandboxed: the owned build's priority launcher calls
`os.nice(10)`, which Muse's sandbox denies in both profiles (measured:
`PermissionError: [Errno 1] Operation not permitted`), exactly as Codex's does.
With `--disable-sandbox`, `os.nice` works, the network is reachable from the
shell, and a shell command can write outside the target; each shell command
still raises an approval, so the allowlist (the exact build only) and the
master's decisions are what bound the session's shell. The file tools refuse
paths outside the workspace in every mode, the unsandboxed Lean host included
(measured: writes and reads; see the report). What the allowlist does not
bound: Lean elaboration itself. A file the model writes in the target can run
`#eval` with `IO` when the language server or the owned build elaborates it,
and `lean_run_code` elaborates arbitrary code; this is inherent to Lean work
(Luna's Lean mode has the same exposure), so review the diff before any build
you approve by hand. MCP servers run outside
Muse's sandbox in every mode (measured: a search tool reached the network under
`:read-only`), which is why MCP tools are aborted outside Lean mode.

Reading `~/creme/.semaphore` works in every mode; writing it (and the build
ledger) needs the unsandboxed Lean host.

## Guards and verdict

Besides the pins above, the guard fails the turn closed on: a notification or
token usage naming another model (any turn); `session/modelRouteUnserved`; a
native subagent; a forbidden native tool (`web_fetch`, `web_search`,
`add_memory`, `edit_memory`, `cron_create`, `cron_delete`, `workflow`); or an
MCP tool that ran outside what the mode allows. After a guard failure only
`turn/interrupt` may be sent. Each turn ends with an audit of the durable
session log after the attach point:

- every model id recorded must be the pin, except the literal `same-as-main`
  of the reminder agents, which is accepted only when the log links those
  child sessions to this session (`*child_session_linked` with this session as
  parent) or holds a pinned model completion of it;
- every `model_completed` record of the turn's run must name the pin; one that
  names no model fails the turn.

A `session/tokenUsage` with **no** model id is not a failure by itself (the
schema allows it, and the echo bootstrap's zero-token completion has none; a
replay from the view start on 2026-09-28 met exactly that and falsely tripped
`4b254f7`). For the current turn it is counted as unattributed, and the turn
then needs positive attribution: at least one `model_completed` of the turn
naming the pin, and none naming anything else or nothing. Usage of another
turn without an id is ignored. So the alarm trips on: an explicit non-pin or
contributor id anywhere; `session/modelRouteUnserved`; a completion of the turn
with no model; model-less usage of a turn whose completions the log cannot
attribute; `same-as-main` without a pinned parent; an unreadable session log;
and the other guard failures above. Anything else ambiguous stays fail-closed:
a completed turn with no model evidence at all is `FAILED` (not passed), and a
failed pin read-back refuses the session before any turn.

`PASS` requires the turn to complete, every observed model to be the pin with
at least one attributed model call, the session-log audit to pass, no guard
failure or error, and, in read-only mode, an unchanged `HEAD` and
`git status --porcelain --ignored` for a Git target.

**Usage admission.** A start or turn is refused when `usage/read` shows the
5-hour window or the weekly block at or above 99% used (the user wants the
allowance used, not left idle). A fresh serve host reports no usage until its
first model call, so admission falls back to the last observation of an
earlier host while its window has not reset (`usage-last.json`), and records
`unobserved` otherwise. Usage is recorded before and after every turn.

## When the live view is lost

The live view stream is best-effort. On first real use (2026-09-28), six
sessions each got one `session/viewHealthChanged` (`unavailable`,
`projectionUnavailable`) two to five minutes into a turn, and the host pushed
nothing after it: no items, no approvals, no `turn/completed`, although Muse
kept running and waited on an approval nobody saw. The broker (and `run`)
therefore reconciles from the server on that notification, on `view/gap`,
every `CREME_MUSE_RECONCILE_SECONDS` (default 20) while a turn runs, and before
every `send` or `steer`:

1. `view/page` from the last durable view cursor processed: every missed
   durable event goes through the same path as a live one (guard, records,
   turn completion). Immutable `sourceRange` plus event/item revision identifies
   processed events; `viewCursor` is only a paging position. Folded reminder
   records can reuse a cursor later assigned to a genuine message or usage
   event, so cursors must never serve as event identities;
2. `approval/listPending`: every still-pending approval goes through the same
   allowlist, and one left to the master appears as an `approval` line that
   `approve` answers (replayed approval events are skipped, so a resolved one is
   never re-decided);
3. `session/read`: a replayed `turn/completed` is applied only when Muse
   reports the turn over (session idle, or running another turn), and only to
   the turn it names. A `view/page` read while a turn runs folds that turn as
   `failed` with reason `incomplete` although it goes on (measured
   2026-09-28, the regression of commit `7562211`), so a replayed terminal is
   never trusted alone. When Muse reports the session idle and the turn's
   terminal is still not found (a second page, then a backward page from the
   head), the turn ends as `lost` (verdict FAILED), so `send` starts a new
   turn instead of steering a finished one. If a turn the broker ended as
   failed or lost is still running in Muse, the next reconcile reopens it
   (keeping its counted tokens) and records its genuine terminal;
4. `view/subscribe` after the cursor, to try to re-attach the live stream.

A reconcile that recovered an approval or ended a turn prints a `reconcile`
attention event.

**When the projection itself is gone.** Once Muse's materialized view is
unavailable, `approval/listPending`, `session/read`, and `view/page` can all
fail (`-32603 ... materialized session view is unavailable`, measured
2026-09-28), while Muse's durable session log keeps every record and
`approval/decide` still works. Each reconcile source is then tried on its own,
and a failing one falls back to tailing the durable log
(`creme/muse_log.py`): an `approval` `requested` record with no
`decision_applied` becomes a pending approval (at its first unresolved stage)
that goes through the same allowlist and master queue and is answered with
`approval/decide`; the run's `terminal` record (run id = turn id) ends the
turn, with the last `assistant_message_committed` text as its final message.
Every terminal, including a live `turn/completed`, receives bounded durable
finalization. It recovers the committed final assistant text and replaces live
usage counters with sums of `model_completed` records whose `payload.kind` is
`run` and whose `run_id` is exactly the parent turn. Every counted completion
must name the pinned model and carry valid input/output usage; child/reminder
records are excluded. Repeated finalization does not add the totals twice.
Missing, unreadable, corrupt, or incomplete terminal evidence fails the turn
with an explicit diagnostic. The full session model audit still checks all
observed model identities and the contributor tripwire remains active.
The durable run terminal is authoritative whenever it is present. After a
source fails, reconciles run every 5 seconds. A failing reconcile source is
recorded as a turn **warning** and never by itself fails a turn or a `run`.

**Multi-stage approvals.** Muse approves a compound shell command
(`a; b | c`) stage by stage: `approval/decide` answers `terminal: false` and
the next stage has the next `sourceIndex`. One decision covers the whole
command: after the allowlist or the master decides the first stage, the
remaining stages of the same approval are answered with the same one-shot
choice at once (and any stage that arrives later is answered from that
decision), so the master sees one `aN` per command. Every stage decision is
recorded in the turn's `approvals.json`.

A retryable `approval/decide` durability-fence error is reconciled by bounded
read-only log polling. It is accepted only when `decision_applied` records the
exact outgoing `commandId`, approval id, parent run, and requested decision.
The RPC error and durable confirmation remain recorded; an unresolved or
mismatched decision fails the turn. The wrapper never retries the approval
command or replays its shell effects.

**Dispatch and visibility.** A master should include the applicable proof skill
instructions in the Muse brief, or authorize exact reads, when those files are
outside Muse's target workspace. Native file-tool confinement also applies in
Lean mode; a missing skill read is not permission to edit proofs blind. In a
Codex workspace sandbox, local broker/socket operations may be denied or appear
unavailable even when the broker exists. Diagnose the execution context and
socket visibility; request the host delegate/escalation for the same authorized
`python3 -m creme muse ...` command when necessary. Do not change Muse auth or
settings, or create alternative sockets to bypass the restriction.

## Clearing the tripwire

```sh
python3 -m creme muse clear-tripwire --reason TEXT
```

Only the master runs it, after reviewing the tripped run (its `session.json`,
`transcript.jsonl`, and Muse's session log) and deciding the alarm was false or
is resolved. It refuses without a non-empty reason and while any broker session
is live, and it moves `MODEL_PIN_FAILURE` to
`.creme/muse/tripwire-records/MODEL_PIN_FAILURE-<UTC stamp>.json` with the
reason, time, and user added. A true pin failure is a user matter: tell the
user and do not clear it.

## Records

Under the canonical checkout's `.creme/muse/` (override `CREME_MUSE_STATE`),
all private to the user:

- `runs/<id>/` for `run`: `brief.md`, `bootstrap.json`, `transcript.jsonl` (the
  MSP wire), `events.jsonl`, `approvals.json`, `usage-before.json`,
  `usage-after.json`, `audit.json`, `last-message.md`, `messages.md`,
  `verdict.json`, and `git-before.txt`/`git-after.txt` for a Git target.
- `sessions/<id>/` for the broker: `session.json` (the registry record, with
  the Muse session id and the session-log path), `events.jsonl`,
  `transcript.jsonl`, `bootstrap.json`, `stop-audit.json`, `wind-down.json`
  (Lean), and `turns/<n>/` with `brief.md`, `steer-<k>.md`,
  `usage-before.json`, `usage-after.json`, `audit.json`, `approvals.json`,
  `last-message.md`, and `messages.md`. Token usage per turn
  (`session/tokenUsage`) is in `session.json`.
- `broker/` (socket, info, log), `usage-last.json`, and the tripwire.

Muse's own session log stays where Muse keeps it
(`~/.local/share/muse/sessions/...`, path recorded).

Every turn also writes `messages.md` beside `last-message.md`: every committed
assistant message of the turn's run, in order, separated by a `---` line, with
its path recorded next to `last_message`. When chatter without a `STATUS:`
header follows the contract block, `last-message.md` holds that contract
message followed by the later message(s), so the header block is never hidden.
The contract's 60-line bound covers that header block only; when the brief
asks for a report in the final message, the worker appends a `REPORT:` section
after `NOT VERIFIED` in one single final message.

## Steering

Steering is `turn/steer` naming the running turn (`expectedTurnId`), so a steer
can never land in a later turn. Demonstrated live on 2026-09-28 (session
`ms-20260928-010324-4ec352`): the brief ran `sleep 30` and was to report
codeword ALPHA; a `steer` sent while the command ran was absorbed mid-turn
(`userMessage` item with `steered: true`), and the final message reported
BRAVO. A steer that arrives after the turn ends is refused; use `send`, which
starts a new turn on the same session.

## Lean work

`start --lean GOAL` requires the target to be exactly the goal's worktree
`<jaune|blanc>/.worktrees/GOAL` (or a sanctioned `-control`/`-mutation`/
`-rehearsal` tree), refuses when host headroom or the semaphore would not admit
heavy work or a `lean`/`lake` process already runs in the target, holds at most
two live Lean sessions with distinct goal labels (`CREME_MUSE_MAX_LEAN_SESSIONS`
1–4), and runs `python3 -m creme reclaim --wind-down GOAL` on every end of the
session (stop, idle close, shutdown, tripwire, refusal or failure after the host
opened, a lost host, and crash recovery by a successor broker), recording
`wind_down=OK` only when wind-down reports `OK` and no in-target `lean`/`lake`
process remains. These are Luna's Lean-mode rules and code (`creme/luna_lean.py`).

The Lean MCP server is the user's Muse `lean-lsp-mcp` definition, which must
keep Creme's guarded launcher (`/usr/bin/python3 -m creme lean-mcp -- uvx
lean-lsp-mcp==PIN`, `LEAN_MCP_DISABLED_TOOLS` covering `lean_build` and
`lean_profile_proof`, `LEAN_LSP_MAX_OPEN_FILES=2`); its `env.PYTHONPATH` must
also resolve the Creme checkout root, since Muse starts the server with the
session workspace as cwd. A drift refuses the start.
Brief a Lean session as the Luna guide says, and verify the build from the
wrapper's own records (the ledger row and a `FRESH` probe), not the transcript.

## Calling it

From **Claude Code**: `start` returns in seconds; run `wait SESSION --timeout
3600` in the background (or the Monitor tool on `events SESSION --follow` after
`detail SESSION live`), steer or follow up with `send`, answer an `approval`
line with `approve`, read with `read SESSION` only when a command cannot settle
the question, and finish with `stop SESSION` (check `stop_audit=PASS`, and
`wind_down=OK` for Lean). A `run` longer than a few minutes goes in the
background. From **Codex** or a **Muse** master, run the same commands through
the shell from the Creme checkout.

## Not isolated

- The user's global Muse MCP server (`lean-lsp-mcp`) is started in every session,
  including read-only and write ones: a session-level MCP override cannot
  replace a host server (`session_mcp_name_conflict`). Its tools are refused by
  the allowlist and the guard outside Lean mode.
- Bundled Muse skills and one plugin skill are listed to the model; no
  foreign-scope (Claude or Codex) skill or rule was observed, but the
  durability of `--no-foreign-personal-context` across resume was not proven
  separately.
- Web tools reappear in an unsandboxed (Lean) host although the bootstrap
  disabled them; the guard fails the turn if one is called.
- Write mode may write the temporary directories as well as the target.
- Reminder agents (verify, skill, todo reminders) run as child sessions on
  `same-as-main`; their tokens ride the parent's items and are not in the
  per-turn token totals.
- Memory tools (`add_memory`, `edit_memory`) and scheduling tools are listed
  to the model; the guard fails a turn that calls one.
