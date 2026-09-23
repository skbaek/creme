# Lean work, builds, and evidence

The contract for a session that elaborates Lean, builds, or runs a gate: which
hold to take, what to do with each verdict the host returns, how to wind down,
and what counts as evidence. `AGENTS.md` states the invariants and host
guidance states this machine's hazards; this guide does not repeat them. How
the semaphore sizes, watches, and records work is maintainer reference in
[`docs/maintainer/build-admission.md`](../maintainer/build-admission.md); the
pre-master single-lead sections are in
[`docs/archive/execution-pre-master.md`](../archive/execution-pre-master.md).

## Resource classes

Decide whether the unit needs a hold, and of which class. Classify from what
the unit does, not from the nearest example:

| unit | class |
|---|---|
| fixture, static, link, schema, and other non-elaborating gates; unit tests that do not elaborate Lean | light — no hold at all |
| any owned build (`creme lake-build`) without a stated class | `tolerant`, sized from its stale closure |
| a warm **full target** whose stale closure is small | `tolerant` — it is judged by its closure, not by naming no target |
| a warm **package or library target** (`jaune`, `Blanc`) | `tolerant` — the target resolves to its Lake roots first |
| a full or package target whose Lake configuration cannot be read | `tolerant`, unproven — "roots unresolved" |
| a build whose probe reports every artifact current (`FRESH`) | no hold — it elaborates nothing |
| a focused language-server proof loop | `tolerant` |
| Lean workers started outside the wrapper, a previously observed spike, or a long indivisible command that cannot reach a renewal boundary | `sensitive` — state it yourself; the wrapper never derives it |
| timing, whole-tree sweeps, mutation campaigns, dependency censuses | `exclusive` — and verify the host is quiet when the gate requires it |

A CPU-only gate does not need exclusivity because it is slow: holding one for
twenty minutes at half a gibibyte locks out every proof session for no safety
benefit. Builds take their hold through the wrapper
([below](#one-compilation-owner)); other Lean work takes it directly, and
`release` removes whichever hold kind admission selected:

```sh
~/creme/.semaphore/semaphore adaptive-acquire GOAL \
  --note "focused proof loop" --memory-gib 4 --contention tolerant
~/creme/.semaphore/semaphore adaptive-acquire GOAL \
  --note "timing control" --memory-gib 8 --contention exclusive --wait 1500
~/creme/.semaphore/semaphore renew GOAL
~/creme/.semaphore/semaphore release GOAL
```

### What to do with each verdict

| verdict | meaning | do |
|---|---|---|
| `ADMITTED_SOFT`, `ADMITTED_HARD` | the named unit may run | run it, renew, release at its end |
| `DEFER_FOR_HARD`, `DEFER_HEAVY` | another hold, or needs already admitted, leave no room now | light work first, or `--wait` |
| `LIGHT_ONLY` | this need does not fit what is available now | light work, `--wait`, or split the unit; if the host looks free, the need is the problem |
| `DEFER_UNPROVEN` | another unit without peak evidence is already running | `--wait` |
| `NEVER_FITS` | the need exceeds physical memory less the floor | build with `--walk`, or split the unit |
| `WAIT_TIMEOUT` | `--wait SECS` elapsed; no hold | resize the wait to the holder it names, or do light work |
| `ALREADY_HELD` | this label already holds | `renew`, or release first; for a gate runner, see [the briefs guide](briefs.md#name-the-hold-the-gates-themselves-take) |
| `RETRACTED` (exit 75) | the build watchdog stopped your build under memory pressure | retry with `--wait`; its observed peak already prices the retry |
| `SOURCE_CHANGED_REPROBE` | sources changed between sizing and launch; hold released | rerun the same command |
| `NOT_REQUIRED_FRESH` | nothing is stale; no hold taken | nothing |

Waitable refusals are never retried in a loop and never downgraded: do not
pass a smaller `--memory-gib` or a weaker class to get admitted. A manual human
hold or swap/compressor pressure returns at once even under `--wait`; do light
work.

### Wait in one call; never poll by hand

`--wait SECS` on `adaptive-acquire` and on `creme lake-build` queues the
request and returns when it is admitted, when `SECS` elapses, or at once on a
verdict waiting cannot change. Among waiters that currently fit, the oldest
goes first; a large request never blocks a smaller one behind it. Waiting only
postpones: it never admits past a floor or changes a verdict.

Before it blocks, a `--wait` prints `fit:` lines with its arithmetic and the
largest need that would fit now. **Derived** means the wrapper sized the build
from the ledger; the remedy is to narrow the stale set or wait, never to pass
a smaller number. **Explicit** means you passed the number, and the line names
what the evidence supports instead. `semaphore status` shows each waiter's
current verdict by the same arithmetic, and each hold's class and age
(`contention=exclusive held=1180s`); a `WAIT_TIMEOUT` names the holder it
waited behind. Size `SECS` to that unit: calibration and campaign units run
15–25 minutes, so `--wait 600` behind one buys only a requeue.

A wait blocks the command that issued it: line up light work first, or issue
the wait in the background and read its result when the client hands it back.
In Claude Code a foreground call dies at 600 s, so run every `--wait`, every
gate that may pass ten minutes, and every acquire-and-gate script with
`run_in_background`, then confirm your waiter line is still in `semaphore
status`. Write `acquire && gate; release` under `set -e` with a `trap` that
releases; `acquire; gate` starts the gate uncoordinated on a timeout.

Never write a shell loop around `semaphore status`, and never write one around
your own backgrounded process either: `while kill -0 PID; do sleep 20; done`
is the same stall with the poll target renamed. A hand-rolled poll holds no
place in the queue and spends a turn per iteration. `status` is for looking at
the host, not for waiting on it.

### Gate runners

A hold covers **one elaborating command**, not a whole gate script. Release
between gates and reacquire; with `--wait` there is no race to lose. If a
runner cannot release between rows, take **one** hold sized for its Lean rows,
not for the whole script, and release the instant the last Lean row finishes.
Rows that do not elaborate take no hold at all.

### Reading `status`

| line | means | do |
|---|---|---|
| `IDLE_HOLD` | a `tolerant`/`sensitive` hold has had no `lake`/`lean` process in its goal's worktrees for two minutes | release it if it is yours; take no hold for a non-Lean lane |
| `IDLE_HOLD not judged … exclusive` | exclusive holds are never judged idle | release yours the moment the timed run ends |
| `STRANDED` | the hold's process is gone and its lease lapsed | run the wind-down command it prints, if the goal is yours |
| `IDLE_WORKERS` | reclaimable language-server memory is resident | `python3 -m creme reclaim --idle-workers MIN --goal GOAL` for your own |
| `HEAVY_LEAN_WORKER` | a `lean` worker or server at 8 GiB or more (report only) | if yours, checkpoint and wind down |
| `ATTRIBUTION_UNAVAILABLE` | a `lake`/`lean` process could not be placed; nothing is called idle | nothing |

Ownership is the goal worktree (`.worktrees/GOAL` and its `-control`,
`-mutation`, `-rehearsal` trees), not a pid. A hold's recorded pid is the
launcher's and is normally gone at once, and `reclaim --dry-run` sees only
your own processes, so neither is evidence that another goal's hold is stale:
only its owner's `wind-down` row in `.semaphore/state/log.jsonl` or a
`STRANDED` line frees it. The same fact makes `STRANDED` and `IDLE_HOLD` false
positives on your own hold when you acquire in one shell call and work in
another; keep the acquiring process alive across the unit, or use the wrapper.

## Renewal and pressure

Renew before the next elaboration or build unit, and during an interactive
MCP session at least every five minutes or by passing `--heartbeat SECS
--detach` to `adaptive-acquire`: one renewer per hold, bound to the agent
client, that stops by itself on release, on the client's exit, or on a
`YIELD_HEAVY`/`DRAIN_HEAVY` verdict, which it logs without killing anything.
Renewal is the lease heartbeat and the pressure check.

| renewal verdict | do |
|---|---|
| `CONTINUE_HEAVY` | continue |
| `CONTINUE_WATCHED` | continue: a wrapper-owned build's watchdog, not renewal, answers pressure |
| `YIELD_HEAVY` | moderate pressure and you are not the priority holder: start no heavy action; checkpoint, wind down, move to light work |
| `DRAIN_HEAVY` | drain threshold, every holder: same as `YIELD_HEAVY` |

A long command that cannot reach a renewal boundary belongs in the sensitive
class before it starts. `python3 -m creme memory-headroom` is a read-only
planning sample; only `adaptive-acquire` authorizes a heavy start. The
language server is never admitted by the semaphore, so a heavy file worker is
its owner's to checkpoint and wind down.

## Wind down Lean work

Before yielding to a requested pause or restart, handing off the work, or
reporting completion, every task that opened a Lean MCP server runs:

```sh
python3 -m creme reclaim --wind-down GOAL
```

If the direct operation is sandbox-denied and `doctor` validates the installed
delegates, use `~/.codex/bin/codex-reclaim-lean --wind-down GOAL`.

Wind-down signals only Lean processes inside the goal's own worktrees,
verifies with a fresh scoped scan that none remain, and only then removes the
goal's hold. Any doubt — an ambiguous scope, an uninspectable process, a
survivor, a failed write — leaves the matching hold intact. It is idempotent.

A plain `release` is valid at an intermediate boundary where keeping the MCP
cache is intentional, but it is not wind-down evidence. Do not report a
Lean-using task safe, idle, transferred, or complete until wind-down returns
`OK`. If it returns `UNAVAILABLE`, checkpoint and leave the hold intact while
restarting the client as the result directs; never substitute a platform
command or a bare signal.

## One compilation owner

Every agent-started Lake build has one goal owner and starts through Creme:

```sh
~/creme/scripts/creme lake-build GOAL --probe -- Narrow.Target
~/creme/scripts/creme lake-build GOAL --wait 600 -- Narrow.Target
~/creme/scripts/creme lake-build GOAL --wait 900 --                      # the full target
~/creme/scripts/creme lake-build GOAL --walk --wait 900 -- Top.Target
~/creme/scripts/creme lake-build GOAL --contention sensitive --wait 1800 -- Cold.Or.Broad.Target
```

Probe first: exit 0 means current, exit 3 means stale and authorizes nothing;
its `stale:` line names the whole stale closure. Use the narrowest target that
can falsify the current edit inside the loop and the catalogue's full target
at checkpoints. **Let the wrapper classify**: omit `--contention` and
`--memory-gib`, and it prices the modules stale right now from the ledger.
State a class only when you know something the ledger cannot, such as a cold
worktree or an expected broad rebuild, and an estimate only when you also know
the peak. `--threads 1` lowers compiler threads without changing admission.

When the whole stale closure is refused `LIGHT_ONLY` or `NEVER_FITS`, pass
`--walk` instead of walking the set by hand: the wrapper builds the closure in
one invocation if admitted, otherwise one owned unit per stale module, imports
first, then the targets. It re-queues a retracted unit once at its observed
peak and stops at the first unit that fails, is refused, or retracts twice,
naming the modules left unbuilt.

The wrapper prints failed and warning jobs (bounded), Lake's verdict, and a
JSON summary with per-target verdicts, failed modules, and the `log:` path of
the full stream (`--full-output` prints it all). Its `restart:` line lists
rebuilt modules; a file's language-server worker keeps its old imports until
refreshed. The edit loop — diagnostics first, the build is not a type-checker,
how to refresh a worker — is in the `lean-prover` skill. `hint: REPEAT_FAIL`
after two failing builds of the same targets is the signature of compiling to
enumerate errors one at a time; switch to diagnostics.

Bare `lake build`, MCP `lean_build`, `lean_profile_proof`, language-server
dependency builds, and startup cache downloads are refused or disabled. The
server guard makes stale imports an explicit `Imports are out of date`
diagnostic; that is a request to probe and run the wrapper, never permission
for a tool to build.

## Evidence and controls

Choose the cheapest test that can falsify the current claim, and record the
commit with each command and verdict. When a catalogue defines
content-addressed verdict reuse, a checkpoint or merge candidate owes a
**complete content-valid manifest**: each row freshly green or backed by
successful evidence with an identical verdict-relevant identity. A routine
push does not become an all-fresh campaign; run fresh only when freshness is
the subject or the goal requires it. The build ledger is performance state,
never gate evidence.

Capture control evidence as each command or tool call completes: the exact
submitted source or mutation, the request, and the unnormalized result, bound
to the source candidate — the positive run, the intended rejection, the exact
restoration, and the restored green (for a script or data control, byte
identity with the green baseline, not a rerun), including LSP controls and
cleanup receipts. Save each raw result once in the goal's evidence directory and link
it from the report. If an original result was not retained, record the gap;
never reconstruct a transcript from memory, and schedule a replacement control
with the next validation unit.

A disposable tree for goal `GOAL` is `.worktrees/GOAL-control`,
`GOAL-mutation`, or `GOAL-rehearsal`; the build owner accepts only those
suffixes as the goal's own. A dependency census — updating one Git-pinned Lake
dependency and rebuilding the full target — runs only in the rehearsal tree,
under host exclusivity, and records the resolved revision:

```sh
~/creme/scripts/creme lake-build GOAL --census --dependency jaune --wait 900 --
```

## Completion

Completion is a condition-to-evidence proof on one exact candidate, not a
completed task list. Re-run drift-prone checks, close independent review
findings, account for compatibility paths, and report branch, worktree, and
push state. Where the repository supports it, the verification evidence is a
complete content-valid manifest, not a claim that every body re-executed. A
worker's summary that it is done is not evidence. If a user-owned publication
or license gate remains, or the master has not merged the candidate under
[its policy](master.md#merges), the goal remains open.
