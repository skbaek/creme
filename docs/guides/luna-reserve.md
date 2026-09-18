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

Poor fits: hard proof strategy, architecture, anything whose correctness the
master cannot cheaply confirm, and anything that needs network access.
Mechanical Lean work (a named edit, a diagnostics sweep, a narrow build) is a
fit only in a Lean-mode session (see [Lean work](#lean-work)); every other
session forbids Lean, builds, and the semaphore.

## Commands

```sh
python3 -m creme luna-reserve status [--json]
python3 -m creme luna-reserve run --brief FILE|- --target DIR [--write] \
    [--effort low|medium|high|xhigh|max] [--timeout-seconds N] [--preflight-only] [--json]
python3 -m creme luna-reserve audit ROLLOUT.jsonl|THREAD_ID [--json]
```

Brokered sessions (see [Broker](#broker)) add `start`, `send`, `steer`,
`interrupt`, `wait`, `events`, `read`, `approve`, `detail`, `list`, `stop`,
`resume`, and `shutdown`.

`status` reads the bucket table without model tokens. It shows the reserve
and regular buckets (usage, credits, reset time in local time and UTC) and
whether a run would be admitted.

`run` sends one brief as one turn on a new thread and follows it to completion.
Effort is `low`, `medium` (default), `high`, `xhigh`, or `max`: every level
the `gpt-reserve` catalogue lists, and admission re-checks the live catalogue.
**Erring high is cheap, and that part is now measured.** A read-only `xhigh`
review consuming 4,721,198 input tokens moved the reserve bucket from 17% to
19%; a 889,070-token `xhigh` probe did not move it at all. There is no economic
reason to economize on effort, so reserve cost does not decide the level; wall time
does, and so does over-elaboration.

**Read-only design and review: `medium`** (controlled runs, 2026-09-18). On a
design question with known ground truth, run at every level with a replicate and a
target that could not leak the answer: every level found the deciding structural
point (7 of 7 runs); `medium` reached the correct verdict 2 of 2 at roughly half of
`high`'s tokens and wall time; `low` was right only 1 of 2 — it found the obstacle
and then stopped instead of resolving it. No correctness gain was observed above
`medium`, and the proposed designs grew more elaborate as effort rose. Run-to-run
spread at a FIXED effort was 1.3–1.9x in tokens, so compare levels only with
replicates. **Write mode and Lean mode have no controlled runs — keep `high` there**;
every failure seen in real use so far happened while writing. Details and the
pre-registered next experiments: the master record's
`briefs/luna-effort-calibration-20260918.md`.

**Field observations, second day of real use (2026-09-18; single runs, no
replicates — a working default, not a measurement).** By mode:

| Mode | Default | What was seen |
|---|---|---|
| read-only, existence or location only | `low` | right locations; ignored "quote verbatim"; one false `MISSING` for a `private` declaration |
| read-only, anything the master will rely on | `medium` | no fabricated signature in about sixty citations; two lemmas placed in the wrong same-named directory |
| write, document synthesis into a fixed template | `medium` | faithful and sourced; over-uses placeholders; did not notice a cross-source impossibility |
| write, code whose result a command checks | `high` | followed a long exact specification and a self-check against a proved bound |
| Lean, edits and small units from frozen statements | `high` | closed a new 92-line module (dependent record, inductive relation, four lemmas), a change to a public inductive with its consumers, and a three-lemma de-duplication across two modules; `xhigh` on a similar task showed no gain |
| Lean, a proof by mirroring a named template | `xhigh` (only level tried) | proved a 200-line shared lemma whose statement was only sketched, by mirroring an existing proof that establishes the fact internally; about 50 minutes and four reserve points — the first run whose reserve cost was visible |

Lean mode is therefore a fit for more than named edits: **a small unit whose
statements are already frozen** is within reach, and that is the pattern to
prefer — a frontier worker freezes the statements, Luna elaborates them, the
master reads `git diff` at the build-approval prompt, before accepting the
build that would certify the edit. A proof whose shape is given by a named
proof to mirror is also within reach. Nothing here shows that Luna can find a
proof with no template. For edits, try `xhigh` only after a `high` attempt has
failed; for a mirrored proof, try `high` first too and record the difference,
because only `xhigh` has been run.

**Third day: whole units from frozen designs (2026-09-18/19; single runs).**
One master session used Luna for most of a DRIP/vault wave: 13 sessions, about
4,000 elaborated lines across ten Blanc modules, four control campaigns (34
controls), and three Creme code changes. Every Lean run was at `high` and none
needed a retry for depth. What was seen:

| Mode | Default | What was seen |
|---|---|---|
| Lean, a whole unit from a frozen Claude design (statements AND transcribed proofs, donor lines named, fallbacks listed) | `high` | 11 multi-turn runs (613-line generic module; its DRIP instance; an 11-rung re-derivation keeping every statement byte-identical; 774-, 680-, 391- and 1035-line leaves). Zero statement drift across all of them (the master diffed every header against the design at each build approval); elaboration fallbacks rarely needed. A long session carried three dependent units over 8 turns without losing context. |
| Lean, mutation controls in a `-mutation` worktree | `high` | 34 controls; every one applied, built, restored byte-identically and reported "at predicted site: yes/no" honestly. Quality is set by the MUTANT LIST: lists that name the site where the removed or falsified fact is CONSUMED bit there 29 of 29 times (15 + 6 + 8); a list whose edit breaks an earlier reference first fails only mechanically ("unknown identifier"/"invalid field") — 4 of 5 in one campaign — and one mutant hit a heartbeat timeout (inconclusive). |
| Lean, a model-level inhabitant by mirroring a named theorem (statement given as meaning + spelling latitude) | `high` | exact first time; read the statement at the approval prompt |
| read-only, an interface for several consumers | `medium` | facts and citations accurate; the proposed interface was unusable (hooks quantified over all states/messages, so unsatisfiable). A Lean-free Claude designer did this well; use Luna for the fact inventory only |
| write, code + tests in Creme | `high` | clean when the brief enumerated the test cases; once returned without the required tests while reporting "added regression test" — check the test list, not the summary |

The division of labour that paid most: **a Lean-free Claude designer freezes
statements and transcribes proofs from named donors (satisfiability of every
hook shown per consumer), Luna elaborates, the master checks headers at each
build approval.** Two Lean sessions run concurrently (distinct goals); use
`approve-builds` with `--header-base` to take the per-build round trips off the
master. `max` has still not been run on real work: no unit with an open proof
shape arose. Run it (against `high`, same brief) on the first such unit, and on
any Luna proof failure retry at `xhigh` then `max` before re-routing.

**But effort is NOT the main lever, and the ladder below `high` is
uncalibrated.** Across the first twelve real uses (2026-09-18) the efforts
actually run were one `low`, eight `high` and three `xhigh` — `medium` was never
exercised, and no task was ever run at two efforts, so nothing here is a
controlled comparison. Two things the evidence does show:

* `high` closed a genuinely nontrivial Lean task — 31 `#print axioms` entries
  plus their imports, correct, with the audited count predicted in advance and a
  non-standard axiom set flagged unprompted. So "use `xhigh`/`max` for Lean"
  overstates what Lean work needs.
* Raising effort did not prevent the failures that actually occurred. Both
  `xhigh` read-only reviews were directionally right and quantitatively wrong —
  one inflated a count, the other proposed an interface with roughly twice the
  fields the proofs needed.

Every failure observed so far has been a **scope or verification** failure, not
a depth failure: a registration whose census was `0` and therefore checked
nothing; audit entries printed but not pinned, because the author did not know a
second list existed; a count measured against a stale commit. None of these
reads as "did not think hard enough", and none would have been fixed by more
effort. The levers that do work are a brief that names exactly what to check and
in what form, and a master who verifies the result rather than quoting it.

Higher effort takes longer per turn, so raise `--timeout-seconds` (default 1800)
for long `xhigh`/`max` turns; the broker interrupts a turn that outlives it.
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
A Lean-mode session gets `templates/luna-reserve/lean-preamble.md` instead
(see [Lean work](#lean-work)).

What loads is recorded per run: `thread/start` reports the instruction
sources (on the first host, only the launch root's `AGENTS.md`), and
`preflight.json` lists the effective configuration layers, the stubbed MCP
servers, and every skill with its scope, path, and enabled state (Creme's
repository skills stay enabled; they are Lean skills that only a Lean-mode
session may use).

## Writing a pseudo-subagent brief

The brief is the user message of the turn. Keep it short and falsifiable:

- the question or the edit, naming exact files or directories;
- what to return, in the preamble's format, and how long it may be;
- what not to touch, when that is not obvious from the target;
- the evidence you want quoted (commands and their results, line numbers).

Do not put secrets, the master record, or user-reserved decisions in a brief.
Give a write-mode run the smallest target that contains the edit.

Habits that paid for themselves in real use:

- **Ask for facts, not verdicts.** Inventories came back accurate; the
  concluding inference was the weakest part of each. Keep the conclusion for
  the master or a frontier worker.
- **Have it quote every definition it relies on, with `file:line`, and list
  the ambiguities it resolved.** This once exposed an error in the master's
  own brief in a single read, and it turns a wrong result into a diagnosable
  one.
- **Give it a self-check against something already proved or known** ("this
  bound must hold on these cases; if it fails, your model is wrong"). A "none
  found" is worth little without one.
- **In every inventory, ask for each declaration's visibility and the path
  exactly as the search printed it**, and give a pattern that admits a
  visibility prefix, for example
  `rg -n '^(private |protected )?(theorem|lemma|def|structure|inductive) NAME'`.
  Both read-only runs of 2026-09-18 missed `private` declarations, and a
  sibling keeps same-named files in two directories, so a wrong path fails
  silently.
- **Say which items are not open.** Told to mark unknowns, it marks too much.
  Keep structural sanity checks explicit ("can this be proved in the file you
  name? check the import direction").
- **Correct or extend through `send` on the same session** rather than a new
  brief: the thread keeps its context.

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
python3 -m creme luna-reserve start --brief FILE|- --target DIR [--write | --lean GOAL] \
    [--effort low|medium|high|xhigh|max] [--detail silent|summary|live] [--timeout-seconds N]
python3 -m creme luna-reserve send SESSION (--text TEXT | --brief FILE|-)
python3 -m creme luna-reserve steer SESSION (--text TEXT | --brief FILE|-)
python3 -m creme luna-reserve interrupt SESSION
python3 -m creme luna-reserve wait SESSION [--timeout SECONDS]
python3 -m creme luna-reserve events SESSION [--follow] [--last N] [--since SEQ]
python3 -m creme luna-reserve read SESSION [--lines N | --items N]
python3 -m creme luna-reserve approve SESSION APPROVAL accept|decline|cancel
python3 -m creme luna-reserve approve-builds SESSION [--header-base REF] [--header-file PATH ...] [--allow-removed NAME ...] [--timeout SECONDS]
python3 -m creme luna-reserve detail SESSION silent|summary|live
python3 -m creme luna-reserve list [--limit N]
python3 -m creme luna-reserve stop SESSION
python3 -m creme luna-reserve resume THREAD_ID [--target DIR] [--write | --lean GOAL] [--effort E]
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

`approve-builds` is opt-in per call and accepts only a Lean session's command
approval in its target, exactly `/bin/zsh -lc '<that>'` with `<that>` exactly
`~/creme/scripts/creme lake-build GOAL [--wait N] -- M1 M2 ...`, N integer 1–900
and each M a Lean module name `[A-Za-z0-9_.]+`. With `--header-base`, every
declaration header from every `--header-file` at REF must be byte-identical in
the worktree except `--allow-removed` names. `--wait N` is within this rule;
with a second Lean lane, transient `DEFER_FOR_HARD` is normal. Anything else
prints its id, summary, and failed rule, exits 20, and is not answered.

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
   For the opt-in narrow rule, run `approve-builds SESSION` instead.
4. Verify with the master's own commands (`git diff`, a test, a grep on the
   target) rather than reading the transcript; read `read SESSION` only when
   a command cannot settle the question.
5. `stop SESSION` when done, then check that its line says
   `stop_audit=PASS`.

Codex and Muse masters use the same commands through their shell. The run
directory of a session is never a substitute for verification: its final
message is a worker summary.

## Lean work

`start --lean GOAL` (and `resume --lean GOAL`; `run` has no Lean mode) opens a
Lean pseudo-subagent: an ordinary brokered write session with the tooling and
host discipline of any other Lean agent on the host. It is opt-in and changes
nothing for other sessions. No billing guard changes: `gpt-reserve` only,
reviewer `user`, never `auto_review`, live and rollout attribution, and the
tripwire all apply unchanged.

### What the broker enforces

- **Target.** `--target` must be exactly `<repo>/.worktrees/GOAL` or
  `<repo>/.worktrees/GOAL-<suffix>` where `<suffix>` is `control`, `mutation`,
  or `rehearsal`, for the Jaune or Blanc repository the Creme host profile
  resolves (a plain directory with a `.git` file, not a symlink). The goal
  label therefore names the worktree that `creme lake-build GOAL` and
  `reclaim --wind-down GOAL` scope on.
  Anything else is refused before any process starts.
- **One MCP server.** Every MCP server stays a disabled stub except
  `lean-lsp-mcp`. Its definition comes from the launch root's tracked
  `.codex/config.toml`, which must equal the file at Git `HEAD` and keep its
  pins (the guarded `/usr/bin/python3 -m creme lean-mcp -- uvx
  lean-lsp-mcp==PIN` launcher, `LEAN_MCP_DISABLED_TOOLS` covering `lean_build`
  and `lean_profile_proof`, `LEAN_LSP_MAX_OPEN_FILES=2`,
  `default_tools_approval_mode = "writes"`, and only the reviewed
  `lean_verify` approval exception). The session launches that definition as
  one `-c` override with `disabled_tools` added for the five open-world search
  tools (`lean_leansearch`, `lean_loogle`, `lean_leanfinder`,
  `lean_state_search`, `lean_hammer_premise`): Codex does not sandbox MCP
  servers, so those tools would reach the network. A nested override is not
  possible, because a project-only server then has no transport at launch.
  Before the thread starts, the effective definition must equal the launched
  one exactly (drift is a refusal). Before the first turn,
  `mcpServer/startupStatus/updated` must show `lean-lsp-mcp` ready and no
  other server starting, and `mcpServerStatus/list` must show it connected
  without a search, build, or profiler tool while every other server is
  disabled. `preflight.json` keeps the definition; `session.json` keeps the
  startup statuses and the tool list. During turns, a tool call on another
  server, a forbidden tool, or a plugin tool is an isolation failure, as in
  version 1.
- **Host discipline.** The broker holds at most `MAX_LEAN_SESSIONS` live Lean
  sessions by default (2; `CREME_LUNA_MAX_LEAN_SESSIONS` may set 1--4, while
  invalid values use the default), including one that is still stopping.
  Live sessions must have distinct goal labels: a goal and its `-control`,
  `-mutation`, or `-rehearsal` worktree share the scope of
  `reclaim --wind-down GOAL`, so admitting both could reclaim the other
  session's language server. The headroom floor and semaphore remain the
  resource safeguards. Before starting, it refuses when
  memory headroom is unavailable, below the semaphore's 20% drain floor
  (`DRAIN_HEAVY`/`LIGHT_ONLY`), or below the host-guidance floor of 30%; when
  another label holds the hard semaphore or a manual hold is active; and when
  a `lean` or `lake` process already runs with its working directory inside
  the target. It takes no hold itself: builds are admitted by the wrapper.
- **Wind-down.** Every end of a Lean session (`stop`, idle close, `shutdown`,
  a tripwire stop, a refusal or failure after the app-server opened, a lost
  app-server, and crash-recovery reconciliation by a successor broker) closes
  the app-server first, waits for in-target `lean`/`lake` processes to exit,
  runs `python3 -m creme reclaim --wind-down GOAL`, and scans the target again.
  The verdict is `OK` only when wind-down reports structured `OK` and no
  in-target `lean`/`lake` process remains; it is recorded under
  `session.json` `lean.wind_down` and `wind-down.json`, and printed as
  `wind_down=OK`. A session whose wind-down is not `OK` ends in state
  `unclean` (exit `11`), never `stopped`. The residual scan exists because the
  broker is not a client process: reclaim alone would call the app-server's
  language servers foreign and report `OK` while they ran. While a successor
  broker's reconciliation runs, a Lean session is refused.

### Sandbox, approvals, and network

The thread runs under the same write profile as any write session: it extends
`:read-only`, grants write access to the target worktree only, and has no
network and no temporary directories. Nothing else is writable from the
sandbox, deliberately: the build ledger, guard launchers, semaphore state,
and Lake cache stay out of the model's reach. Two facts were measured on the
first host with zero model tokens (`codex sandbox` and a thread-only
`thread/start`):

- The Lean MCP server runs outside the sandbox (Codex starts MCP servers
  itself), so the language server works under this profile.
- `creme lake-build` cannot run inside it: the owned-build priority launcher's
  `os.nice(10)` is denied and the wrapper exits `64` before any work, and even
  its `--probe` needs to append to the build ledger. The session therefore
  requests escalation for each `creme lake-build` command, and the master
  answers it with `approve`.

A typical edit, diagnostics, and narrow-build cycle raises **one** approval:
the escalated `creme lake-build` command (offered `accept|cancel`). File edits
inside the worktree and `lean_diagnostic_messages`, `lean_goal`, and the other
read-only-marked tools raise none. `lean_verify` is pre-approved by the
tracked definition. Any other MCP tool approval arrives as an
`mcpServer/elicitation/request` and is queued like a command approval (the
answer is `accept`, `decline`, or `cancel`; a form that requires content is
offered only `decline` or `cancel`); requests from any other server, URL
elicitations, and device verifications are declined by policy.

Answer a build approval only when the command is exactly
`~/creme/scripts/creme lake-build GOAL [--wait N] -- <narrow targets>` with no
`--memory-gib` or `--contention`, N an integer from 1 through 900, its working
directory the target worktree, and the host with room; `decline` or `cancel`
anything else.
The approved command runs outside the sandbox and takes its own semaphore
admission.

The broker declines one class of command itself, without spending the master's
attention on it: in a Lean session, a command-execution approval whose tokens
name the semaphore launcher or the generated `codex-reclaim-lean` delegate
(matched on the basename, so any path spelling counts), carry `--wind-down`, or
put `reclaim` or `semaphore` after a `creme` invocation. A nested
`sh -c`/`bash -lc` script is read as tokens too. The preamble already forbids
these because the broker owns wind-down, so a request for one is the model
ignoring its contract, not a decision. The refusal takes the next approval id,
is recorded under `session.json` `refused_approvals`, and is printed as an
attention event (`approval-refused`) in `events` and as a `refused a1 by the
broker:` line in the session record. Nothing else changes: `creme lake-build`
still escalates to the master, and a non-Lean session's approvals are
untouched. The rule is deliberately blunt and fail-closed; a command that
reaches the same effect without those tokens (for example a Python `-c`
snippet importing `creme.reclaim`) still gets through to the master, who
declines it as before.

### Briefing a Lean pseudo-subagent

Use it for mechanical Lean work whose result the master can check cheaply: a
named lemma or example to add, a rename across one module, diagnostics over a
list of files, a narrow build, a local search. Do not use it for hard proof
strategy, a proof whose shape is open, a broad rebuild, or anything that needs
the search tools. Brief it as any other pseudo-subagent, and also:

- name the files, the exact edit or goal, and the narrow build target;
- say whether to build at all, and name the module target;
- ask for the diagnostics counts and the wrapper's final status line;
- keep one objective per turn and use `send` for the next step.

The Lean contract (`templates/luna-reserve/lean-preamble.md`) already carries
the `AGENTS.md` Lean rules: the edit, `lean_diagnostic_messages`, `lean_goal`
loop; builds only through `creme lake-build GOAL -- <narrow targets>` with no
memory or contention flags and with an escalation request; never bare `lake
build`, `lean_build`, or `lean_profile_proof`; refresh the language server
after a rebuild; stop on `YIELD_HEAVY`, `DRAIN_HEAVY`, or any refusal; no
push, merge, or history rewrite; and no semaphore, reclaim, or wind-down
commands, because the broker owns wind-down.

### Verifying Lean results

The final message is a claim. The master checks the worktree diff itself
(`git -C TARGET diff`), and the build verdict from the wrapper's own records,
not from the transcript: the build ledger row for the goal
(`.creme/lean-build-ownership/ledger.jsonl`, with `exit`, `modules_rebuilt`,
and peaks), the semaphore log's acquire and release rows, and a fresh
`creme lake-build GOAL --probe -- TARGET` reporting `FRESH`. After `stop`,
check `wind_down=OK` and that no `lean`/`lake` process runs in the target. On
the first live proof the model appended an unrequested blank line while
reporting "exactly" the requested lines, and reported a failed wind-down it
had never run; both were caught only by these checks.
