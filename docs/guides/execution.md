# Accountable execution

This guide is the shared lead method for substantial Jaune/Blanc work. The
named goal defines product semantics; sibling `scripts/GATES.md` files define
repository verification; Creme defines execution and evidence discipline. The
layer above it — one master session that owns goals, workers, merges, and
pushes on a host — is [the master guide](master.md).

## Start and reconcile

Read the goal and relevant authorities in full. Confirm stable goal identity,
`ready` status, explicit completion criteria, current repository commits,
dirty-tree ownership, and open user decisions. Create per-goal branches and
worktrees; never repurpose shared main clones or overwrite unrelated changes.
A worktree for the repository at `PATH` belongs in `PATH/.worktrees/<goal>`,
ignored by that repository. Keeping it inside the repository it belongs to
assumes nothing about the surrounding layout and needs no access beyond the
repository already in use.

Write a compact state brief for multi-session work. It should name exact
commits, owned paths, last green evidence, active packets, open decisions, and
the next coherent unit. State may live in any configured goal store; it never
becomes a hidden dependency of the public workflow.

Maintain that brief by replacement at checkpoints, following
[compact continuity](master.md#compact-continuity). Keep detailed commands and
verdicts in evidence and link them; retain all open obligations and qualifying
failures. Do not prepend repeated historical status to current documents.

Before launching the planned units, apply the [escalation procedure](escalation.md)
to their access and capability needs as a group. Preserve the resulting coverage
inventory in the state brief and update it when operations or policy change.

## Delegate by ownership

Delegate only bounded packets with disjoint file ownership, stated authority,
resource class, required checks, and a clear return contract; under the master
model each packet is a written brief, per [the briefs guide](briefs.md). The lead retains
integration, conflict reconciliation, final verification, and user-only
decisions. Parallel work is useful only when it leaves evidence that can be
merged without overlapping authority. The tracked
[generic worker brief](../../templates/master-runtime/worker-brief.md) lists
the fields a packet may need; fill the ones that apply and leave general
policy in the guides. A filled brief is private runtime state; the tracked
template remains placeholders only.

## Resource classes

- Light: inventories, docs, static scans, link checks, and unit tests without
  Lean elaboration. No hold; prefer this class while heavy work is deferred.
- Elaboration: ordinary Lean MCP steps, focused builds, and compiling gates.
  Request adaptive admission with your best evidence of the unit's peak
  memory. It may grant a soft or hard hold.
- Contention-sensitive: work you know must not overlap anything — commands
  that create multiple Lean workers outside the wrapper, previously observed
  spikes, long indivisible work that could make the interactive host unusable.
  Request `sensitive` explicitly; the wrapper never derives it.
- Exclusive: timing, whole-tree sweeps, and mutation campaigns. Request
  `exclusive` and verify the host is quiet when the repository gate requires
  that stronger condition.

Classify from what the unit actually does, not from the nearest example:

| unit | class |
|---|---|
| fixture, static, link, schema, and other non-elaborating gates | light — no hold at all |
| a unit test suite that does not elaborate Lean | light |
| any owned build (`creme lake-build`) without a stated class | `tolerant`, its need sized from the stale closure |
| a warm **full target** whose stale closure is small | `tolerant` — it is judged by its closure, not by naming no target |
| a warm **package or library target** (`jaune`, `Blanc`) | `tolerant` — the target resolves to its Lake roots first |
| a focused language-server proof loop | `tolerant` |
| a full or package target whose Lake configuration cannot be read | `tolerant`, unproven — "roots unresolved" |
| a build whose probe reports every artifact current (`FRESH`) | no hold at all — it elaborates nothing |
| a stale set with a module that has no peak evidence at all | `tolerant`, **unproven**, sized at a default (4 GiB narrow, else 8 GiB) |
| Lean workers started outside the wrapper, or a previously observed spike | `sensitive` |
| a long indivisible command that cannot reach a renewal boundary | `sensitive` |
| timing, whole-tree sweeps, mutation campaigns, dependency censuses | `exclusive` |

A CPU-only gate does not need host exclusivity because it is slow. Holding one
for twenty minutes at half a gibibyte locks out every proof session on the
host for no safety benefit.

```sh
~/creme/.semaphore/semaphore adaptive-acquire GOAL \
  --note "focused proof loop" --memory-gib 4 --contention tolerant
~/creme/.semaphore/semaphore adaptive-acquire GOAL \
  --note "cold or broad rebuild" --memory-gib 10 --contention sensitive
~/creme/.semaphore/semaphore adaptive-acquire GOAL \
  --note "timing control" --memory-gib 8 --contention exclusive
~/creme/.semaphore/semaphore adaptive-acquire GOAL \
  --note "next proof unit" --memory-gib 4 --contention tolerant --wait 600
~/creme/.semaphore/semaphore renew GOAL
~/creme/.semaphore/semaphore release GOAL
```

`ADMITTED_SOFT` and `ADMITTED_HARD` authorize the named heavy unit.
`DEFER_FOR_HARD` means another hold, or the needs already admitted, leave no
room now; `LIGHT_ONLY` means the need does not fit what is available now;
`DEFER_UNPROVEN` means another unit without peak evidence is already running.
All three are waitable. Do not retry in a loop. Reorder independent light
work, wait for an existing heavy unit to wind down, or split the planned work.
The explicit `soft-acquire` and `hard-acquire` compatibility commands are also
pressure-gated; they cannot bypass a low-memory refusal. `release` removes
whichever hold kind adaptive admission selected. Explicit soft/hard releases
remain available for compatibility.

### Wait in one call; never poll by hand

`--wait SECS` on `adaptive-acquire` and on `creme lake-build` queues the
request under the same mutex and returns when it is admitted, when `SECS`
elapses (`WAIT_TIMEOUT`, nonzero exit, no hold), or immediately on a verdict
waiting cannot change — a manual human hold, swap/compressor pressure, or a
need that exceeds physical memory less the floor (`NEVER_FITS`). A live
headroom shortfall remains waitable. Among the waiters that
currently fit, the oldest goes first; a large request refused for headroom
never blocks a smaller one behind it, and a waiter whose process dies is
dropped. Waiting can only postpone a request. It never admits one past a
floor, and it never changes a verdict you would have received without it.

#### Launch and watch: what "currently fit" means

Admission refuses only the obviously infeasible; a watchdog answers the rest
while the build runs (decision launch-and-watch-admission-20260923).

```
need      = the unit's best peak evidence, no margin:
            own measured peak -> recorded floors -> an unproven default
it fits when   need + needs already admitted <= available - 2 GiB floor
NEVER_FITS when need > physical RAM - 2 GiB floor
at most one unproven unit (a default, not evidence) runs at a time
```

"Available" is the platform adapter's figure. On macOS it is
`memory_pressure`'s free percentage, which counts memory the compressor could
reclaim as available and barely moves while a build allocates. Concurrent
admitted needs are counted in full even though part of them is already out of
"available" (conservative). There is no multiplier, no estimate margin, and no
reserve: on this 24 GiB host a measured 14.8 GiB build is admitted whenever
16.8 GiB is available. A larger estimate than the evidence supports still only
makes a request harder to schedule, so state one only when you know it.

While Lake runs, the owned-build wrapper samples the host about once a
second, at two levels. At the **drain** level — the swap/compressor pressure
cause, or available memory below the floor — it reclaims the goal's own
language-server workers idle for two minutes or more, in the background, and
nothing more; admission already refuses new heavy work there. Only a
**retraction** signal stops a build: the kernel's VM pressure level at warning
or worse (`kern.memorystatus_vm_pressure_level` >= 2, macOS) held for 10 s,
swap in use rising by 1 GiB within 10 s, or on Linux `MemAvailable` below the
floor or PSI memory "full" avg10 at 10% or more. On macOS the live retraction
signals are therefore the kernel level and swap growth; the free percentage
never gets there first. A build that has already exited is never retracted.
If a retraction signal persists three seconds, the youngest
unproven admitted build retracts — its process group is terminated through
the normal interruption path — and if it persists, older builds follow,
youngest first, five seconds apart. A retracted build exits **75** with JSON
`"status": "RETRACTED"`, a `hint`, and a ledger row (`outcome: retracted`)
whose observed peak floors the retry: the in-flight modules' own peaks when
the sampler saw them, otherwise the whole run's peak as a floor for any stale
set that still contains every unfinished module, so a plain `--wait` retry is
never priced as before. `--walk` re-queues a retracted unit once at
that peak and stops if it retracts again. Holds taken with `adaptive-acquire`
(language-server loops, gate runners) are admitted by the same arithmetic and
keep the renewal verdicts below; they are never retracted.

Before it blocks, a `--wait` prints its own arithmetic, says where the need
came from, and says what would fit if this one does not:

```
fit: need 10.2 GiB + admitted 4.0 GiB + floor 2.0 GiB = 16.2 GiB; 15.4 GiB available now (64% free) -> does not fit now
fit: at this instant a need of at most 9.4 GiB would fit; a larger one is queued in arrival order but passed over by every request that fits
fit: need 10.2 GiB is derived, not explicit (derived: broader rebuild: …)
```

The second line is the one to read when the number surprises you. **Derived**
means the wrapper sized the build from the ledger's evidence about the modules
that are stale right now, and the remedy is to narrow the stale set or to wait
for the host, never to pass a smaller `--memory-gib`; **explicit** means you
passed the number, and the line names the estimate the evidence supports
instead. In B11 a session read a derived 12 GiB as "an explicit --memory-gib
12" and escalated the wrong cause twice; the line now says which it is.

`semaphore status` prints, under every waiter, the verdict the queue would give
it right now — computed by the same function the queue uses — with the same
arithmetic. If it says `LIGHT_ONLY` while the host looks free, the need is
the problem; if it says `DEFER_HEAVY`, something is holding and waiting is the
right answer.

#### Sizing the wait, and working while it runs

`status` prints each hold's class and how long it has been held
(`contention=exclusive held=1180s`), and a `WAIT_TIMEOUT` prints the same for
the holder it timed out behind, together with the verdict that *dominated* the
wait rather than the one on the final pass. Size `SECS` to the unit ahead: in
B9 calibration and campaign units ran 15–25 minutes, so a literal `--wait 600`
behind one of them buys nothing but a requeue and a spent turn.

A wait blocks the command that issued it. Either line up light work first and
issue the wait when you have nothing else to do, or issue it in the background
and read its result when the client hands it back.

**Claude Code: the foreground ceiling is 600 s.** The Claude client kills a
foreground tool call at 600 s when that is the timeout it was given, and only
backgrounds a call whose stated timeout was shorter; B11 lost two ten-minute
waits and a head-of-queue place to exactly that, and a foreground `--wait
1500` cannot complete in any case. Run every `--wait`, every gate run that
may pass ten minutes, and every `acquire && gate; release` script with the
Bash tool's `run_in_background`, and after a call moves to the background
confirm that your own waiter line is still in `semaphore status`. A chain of
`acquire; gate` with `;` starts the gate uncoordinated on a timeout: write
`acquire && gate; release` under `set -e` with a `trap` that releases.

Never write a shell loop
around `semaphore status`, and never write one around your own backgrounded
process either: `while kill -0 PID; do sleep 20; done` is the same stall with
the poll target renamed. A hand-rolled poll cannot hold a place in the queue,
it spends a foreground turn per iteration, and the verdict it eventually reads
is the one `--wait` would have returned. `status` is for looking at the host,
not for waiting on it.

#### A gate runner that cannot release between rows

Split it if you can. If you cannot, take **one** hold sized for its Lean rows,
not for the whole script, and release the instant the last Lean row finishes.
Rows that do not elaborate — fixtures, static scans, link and schema checks —
take no hold at all. One lease across sixty-one sequential gate rows blocks
every proof session on the host for the duration, and `status` will say so.

A hold covers **one elaborating command**, not a whole gate script. Release
between gates and reacquire — with `--wait` there is no race to lose by
letting go. `status` and `renew` print `IDLE_HOLD` when a holder has had no
`lake` or `lean` process for two minutes — neither among the holder's own
children nor working inside the goal's worktrees, which is how a gate launched
from a different shell call than the one that took the hold is still counted as
work — `STRANDED` with the exact wind-down command when a hold's process is
gone and its lease has lapsed, and `IDLE_WORKERS` when reclaimable
language-server memory is resident. A process is `lake` or `lean` by its
executable — the basename, or an elan toolchain path — never because the word
`lean` occurs in its name: a macOS daemon called `CleanupPreparePathService`
once suppressed the signal host-wide for a whole window. When a `lake`/`lean`
process cannot be placed, the line says `ATTRIBUTION_UNAVAILABLE`, no hold is
called idle, and one `attribution` row in `.semaphore/state/log.jsonl` names
the process and the cause, with an `ATTRIBUTION_RESTORED` row when it clears;
the same cause is logged once, not once per `status`.

An `exclusive` hold is **not judged idle**. A timing gate or a t8n/Python lane
runs under exclusivity with no `lake` or `lean` process at all, so "no Lean
work" is not idleness for that class; the line reads `IDLE_HOLD not judged:
… the hold is exclusive` and says how long, and releasing it the moment the
timed run ends is the holder's job. A `sensitive` or `tolerant` hold over a
non-Lean lane is still `IDLE_HOLD` after two minutes — take no hold for it.
`STRANDED` is unaffected by class.

Ownership is the goal worktree. Under the master model every worker session
is a subagent of one client process, so a client pid names everyone on the
host and therefore no one. `IDLE_HOLD` places a process by its working
directory under the goal's `.worktrees/GOAL` (and its `-control`,
`-mutation`, `-rehearsal` trees); `IDLE_WORKERS` names each idle worker's
owner as `goal GOAL` by the same rule; and a `LIGHT_ONLY` refusal for headroom
names that memory and its owner. Reclaim your own with
`python3 -m creme reclaim --idle-workers MIN --goal GOAL`, which signals only
workers working inside that goal's worktrees and reports every other one
with the command its owner should run; without `--goal` a worker inside any
goal worktree is reported, never signalled.

A hold's recorded pid is the launcher's and is normally gone the moment the
hold exists, and `reclaim --dry-run` sees only the calling session's own Lean
processes. Neither is evidence that another goal's hold is stale: only its
owner's `wind-down` row in the log or a `STRANDED` line frees it. Between two
`status` calls a changing pid or note means a live session moving between
phases. For your own holds the same fact makes `STRANDED` and `IDLE_HOLD`
false positives when you acquire in one shell call and work in another; keep
the acquiring process alive across the unit or use the wrapper, which does.

`python3 -m creme memory-headroom` is a read-only planning sample. It can
justify moving light packets ahead of heavy ones, but only `adaptive-acquire`
re-samples under the mutex and authorizes a heavy start.

Pressure is not only the free percentage. On macOS nearly exhausted swap or a
saturated compressor (`memory_pressure_cause`, see the capability contract)
refuses new heavy work, drains renewals, and turns the build watchdog red even
when the aggregate probe reads healthy; refusals and `status` say
`swap/compressor pressure` with the numbers.
Separately, `status`, `renew`, and headroom refusals list every `lean --worker`
or `lean --server` whose physical footprint (compressed pages included, not
RSS) is 8 GiB or more as `HEAVY_LEAN_WORKER`, with its pid, owning goal (from
its working directory, else its document URI), and whether a hold covers it;
at most one `worker_pressure` row per five minutes goes to the semaphore log.
This is a report only: nothing is signalled. A language server is never
admitted by the semaphore, so a heavy worker named there is its owner's to
checkpoint and wind down.

Renewal is both a lease heartbeat and an in-session pressure check. Call it
before the next elaboration/build unit and at least every five minutes during
an interactive MCP session. Under moderate pressure—or when the worker count
or admitted needs are already above what the host holds—non-priority soft
holders receive `YIELD_HEAVY`, leaving the oldest live coherent unit priority.
At the drain threshold every holder receives `DRAIN_HEAVY`. A wrapper-owned
build renews as `CONTINUE_WATCHED` instead: its watchdog, not renewal,
answers memory pressure. In either case, launch no new
heavy action: checkpoint, wind down, and move to light work. A long command
that cannot reach a renewal boundary belongs in the sensitive class before it
starts.

The launcher is tracked in the canonical Creme checkout and is shared by Codex,
Claude Code, other local agents, and humans. Always use the canonical launcher,
not a copy inside a goal worktree; linked worktrees resolve back to its single
ignored `.semaphore/state` directory.

On an upgraded host, `migrate-state` copies validated live holds under the old
and new mutexes, activates `.semaphore/state`, and leaves the legacy files
untouched. Run it once from a trusted human shell after the neutral-semaphore
change is deployed:

```sh
~/creme/.semaphore/semaphore migrate-state
```

Retire any pre-neutral delegate and legacy state only after every session
launched before the cutover has wound down.

On limited hosts, adaptive admission uses one hard heavy operation at a time
and asks for frequent checkpoints. Missing full telemetry is not a pressure
signal, and the aggregate headroom probe is intentionally independent of
process discovery. Missing headroom forces serialization rather than an
optimistic soft hold. Never edit semaphore state or use a bare process kill.

## Wind down Lean work

Before yielding to a requested pause or restart, handing off the execution, or
reporting completion, every task that opened a Lean MCP server must use one
wind-down operation:

```sh
python3 -m creme reclaim --wind-down GOAL
```

If the direct host operation is sandbox-denied and `doctor` validates the
installed delegates, use the existing reclamation delegate:

```sh
~/.codex/bin/codex-reclaim-lean --wind-down GOAL
```

Wind-down resolves the goal's real Jaune/Blanc `.worktrees/GOAL` roots from the
validated host layout and holds the semaphore mutex across its full
transaction. Other labels may remain: the adapter samples the current working
directory of every same-client Lean candidate and signals only processes inside
the caller's resolved goal worktrees. Processes in another goal worktree remain
foreign and their holds are untouched. If any same-client candidate's working
directory is uninspectable, no process is signalled. Wind-down performs
ordinary reclamation, never hard-pressure reclamation; verifies with a fresh
goal-scoped dry-run that no owned or protected Lean roots remain; and only then
removes the caller's soft or hard hold. A missing or ambiguous worktree scope,
unavailable scan, protected root, survivor, failed verification, or semaphore
state-write failure leaves the matching hold intact. The operation is
idempotent when the goal already has no hold and its scoped process scan is
clear.

Ordinary `soft-release` and `hard-release` remain valid at intermediate
boundaries where retaining an MCP cache is intentional. They are not evidence
that a task is fully wound down. Do not report a Lean-using task safe, idle,
transferred, or complete until wind-down returns structured `OK`. If reclaim is
`UNAVAILABLE`, checkpoint and leave the hold intact while restarting the client
as directed by the capability result; do not substitute a platform command or
bare signal.

## Edit and verify

Choose the cheapest test that can falsify the current claim. Run from the
worktree under test and record exact command, exit status, relevant terminal
verdict, and commit. Do not weaken gates, baselines, manifests, budgets,
timeouts, allowlists, or generated artifacts to obtain green.

### One compilation owner

Every agent-started Lake build has one goal owner and starts through Creme.
This is an enforced client/agent boundary, not a claim that an interactive
human shell cannot execute an absolute toolchain binary:

```sh
~/creme/scripts/creme lake-build GOAL --probe -- Narrow.Target
~/creme/scripts/creme lake-build GOAL --wait 600 -- Narrow.Target
~/creme/scripts/creme lake-build GOAL --wait 900 --                      # the full target
~/creme/scripts/creme lake-build GOAL --threads 1 --wait 900 -- Narrow.Target
~/creme/scripts/creme lake-build GOAL --contention sensitive --wait 1800 -- Cold.Or.Broad.Target
```

The third and fourth commands carry no `--memory-gib`: a full or package target
is classified and sized from its own stale closure like any other, and an
estimate above what the evidence supports only makes the request harder to
schedule. State a class when you know something the ledger cannot — a cold
worktree, a rebuild you expect to be broad — and state an estimate only when
you also know the peak.

`--threads` accepts `1` or `2` and defaults to `2`; it sets
`LEAN_NUM_THREADS` for that owned invocation. A one-thread choice does not
change target interpretation, admission, sizing, measurement, or release, and
does not by itself prove that only one compiler process ran. Wrapper options
belong before `--`; every token after it remains a Lake target. The Linux
contained-build broker keeps its separate restricted interface and does not
gain this option.

Probe first. Exit 0 means the selected artifacts are current; exit 3 means
stale and authorizes no work by itself. The second command requests adaptive
admission, applies `nice -n 10` and the calibrated `LEAN_NUM_THREADS`, records
the ignored host-local ledger, and releases its hold. Use the narrowest target
that can falsify the current edit inside the loop and the repository
catalogue's full target at checkpoints. Bare `lake build`, MCP `lean_build`,
`lean_profile_proof` (which shells to `lake env lean`), language-server
dependency builds, and startup cache downloads are not compilation owners and
are refused or disabled.

Creme supplies `LAKE_CACHE_DIR` as the canonical checkout's
`.creme/lake-cache/` for owned builds, probes, and guarded MCP processes,
overriding inherited shell or toolchain defaults. Linked Creme worktrees use
that same ignored directory. Jaune and Blanc share it: Lake addresses artifacts
by content hash and output mappings by scope and input hash. Repository names
alone do not select a cached artifact. No shell startup file or host-wide
user-manager setting is required. Host containment wrappers that launch gates
must explicitly pass this same absolute setting into the contained process;
they cannot rely on the calling shell's environment surviving containment.

Existing caches are not migrated automatically. Copy or move them only at a
quiet checkpoint using the host's reviewed migration procedure, preserving
hard links where relevant, then verify the selected cache before retiring the
old location. Changes to Creme's runtime invalidate a pinned contained-build
broker; review and regenerate its capability bundle before using that broker.

**Let the wrapper classify.** Omit `--contention` and `--memory-gib` and it
derives both from evidence about the *modules that are stale right now*, not
from the name of the target list. The probe resolves the targets to their
roots, names every module in the stale closure, and the ledger supplies each
module's own measured `lean` peak — recorded per module on every build row
from now on, and read from an older narrow row's largest `lean` process
before that — on the same repository, configuration/execution context, and
exact module input identity. A
broad row without per-module peaks measures no single module: its peak is its
breadth. The build's peak is then the Lake overhead plus the peaks that can
elaborate at the same time, which the import order decides (two modules in
one chain never overlap). That modelled peak is the need, charged as it is.
So `-- Blanc` with one stale root module is
priced from that module, whatever a 376-module rebuild of `-- Blanc` peaked
at an hour earlier, and a two-target list inherits its members' rows.

A module without an exact measurement is priced by a recorded floor when one
exists — its peak on a drifted or other-toolchain row, or a failed or
retracted attempt — and the result is still evidence (`floor evidence`). Only
when some stale module has no peak evidence at all is the need a default, and
the build **unproven**: a large set is bounded by the tightest broader
successful rebuild that included its members; otherwise a small set of short
modules takes the narrow default (`narrow_default_gib`, 4 GiB) and a set with
a heavy member or above the narrow count takes the profile default. The
estimate's `source` names the rule and the row, and `need_gib`/`unproven` are
in the JSON and the ledger row.

A probe that reports every selected artifact current means the build
elaborates nothing, and the wrapper then **takes no hold**: the row says
`NOT_REQUIRED_FRESH`. `--probe` prints a `stale:` line naming the whole stale
closure — every module a build would elaborate, not only the frontier Lake
stops at — so a broad rebuild can be planned as one build of the top of its
import chain instead of walked a layer at a time.

When the whole stale closure cannot be admitted — refused `LIGHT_ONLY`, or
`NEVER_FITS` because its need exceeds even an idle host — pass `--walk`
instead of walking the stale set by hand. The wrapper first asks for the whole
closure and builds it in one invocation if admitted; otherwise it builds the
stale modules one at a time, imports first, each as an ordinary owned build
with its own probe, estimate, admission, hold, ledger row, and log, then builds
the requested targets. A unit the watchdog retracts is re-queued once at its
observed peak. It stops at the first unit that fails, is refused, or retracts
twice, and names it and the modules left unbuilt. `--wait` applies to each unit.

The wrapper prints only failed and warning jobs (bounded) and Lake's verdict,
then a JSON summary with a per-target verdict, the failed modules, and the
`log:` path holding Lake's full stream from the first line; `--full-output`
prints the whole stream. On completion it lists the modules it rebuilt on a
`restart:` line of its own; a file's language-server worker keeps the imports it loaded until
it is refreshed. The edit loop — diagnostics first, the build is not a
type-checker, how to refresh a worker — is in the `lean-prover` skill.

When a build exits 1 and the previous build of the same targets also failed
within the repeat window, the JSON carries `hint: REPEAT_FAIL` naming the
diagnostics tool. It is a hint, never a refusal: the wrapper does not decide
how you work. But two failing builds in a row on the same targets is the
signature of using compilation to enumerate errors one at a time.

The server guard rewrites every `lake setup-file` to include `--no-build
--no-cache`; stale imports therefore remain an explicit `Imports are out of
date` diagnostic. A refusal is a request for an owner to probe, classify, and
run the wrapper, never permission for a tool to build automatically.

Build artifacts and verdicts may be reused by identity rather than location
when the identity covers every verdict-relevant input, the object is immutable
once written, and reuse remains within one host, user, and toolchain. Symlinks
to mutable state and remote or cross-host stores do not satisfy that trust
boundary. The build ledger uses Lake input hashes only to measure duplicate
elaboration; it is performance state and never gate evidence.

When a repository's gate catalogue defines content-addressed verdict reuse, a
checkpoint or merge candidate owes a **complete content-valid manifest**: each
catalogue row is freshly green or is backed by successful evidence with an
identical verdict-relevant identity. A routine draft push does not become an
all-fresh campaign merely because it is a push. Use an explicitly fresh run
when freshness itself is the subject under test or the named goal requires it.

### Build-measurement identity

The host-local build ledger is performance evidence, not a verdict manifest.
Its newer measured rows can reuse an exact module cost across linked worktrees
of one Git repository only when they share a complete input identity: the
repository's Git common directory, the toolchain executables Elan actually
resolved (including an effective `ELAN_TOOLCHAIN` override), the selected
toolchain file and manifest, Lake
configuration, clean Git-pinned dependency checkout revisions, effective
`LEAN_`/`LAKE_` and allocator settings, thread count, and the module's transitive local
source closure. The source collector reads each header and its imports from
the same bytes; an unsupported header form, missing known-local source, dirty
or differently checked-out dependency, missing configuration, or unknown
thread setting leaves the identity unknown. It does not infer an identity from
a `.trace` input hash or an old artifact.

The wrapper snapshots those inputs when it sizes a real stale closure and
checks them again after admission before launching Lake. A change there
returns `SOURCE_CHANGED_REPROBE` and releases the hold, so a new source is not
run under an old estimate. A change during the build records a non-exact row;
only a successful elaborating row with usable samples can be exact evidence.
A fresh/no-elaboration build still takes no hold.

Older rows are never migrated or retrospectively asserted exact. When Git can
still prove a removed linked worktree belonged to the same repository, legacy
peaks and durations may remain conservative fallback floors; otherwise they
are excluded from cross-worktree selection. Fallback can keep a known costly
module expensive, but it never overrides a newer exact cohort. New exact reuse begins only with newly recorded applicable
measurements. A whole-build aggregate floors a module only when its row ran on
the current toolchain; another toolchain's direct module peak remains a floor
only until the current toolchain has measured that module. A failed build
floors a failed module by its own recorded peak, or by the run's whole peak
only when it was the run's sole failed module.

A legacy row is labelled a `legacy own-singleton aggregate` only when the
selector revalidates its Git repository, toolchain and manifest and the row
itself records the same thread count, sensitive execution, one
target/root/rebuilt module, one concurrent Lean process, and a complete usable
sample. It remains a floor, never exact evidence.

This is a same-host, same-user performance boundary. The guarded resolver
checks one coherent Lean/Lake sysroot and the exact identity stores only a
digest of its resolved paths; it assumes the host's managed toolchain store is
immutable for the duration of a run. The ledger is not cross-host evidence.

For a non-vacuity or enforcement claim, show all three controls: the surrounding
tree still works, the control fails at the intended boundary, and removing only
that control restores green. Use disposable worktrees for destructive
mutations. Preserve source artifacts and fail on stale generated output.

Capture control evidence when each command or tool call completes: save the
exact submitted source or mutation, command/tool request, and unnormalized
result, bound to the source candidate. Retain the positive run, intended
rejection, exact restoration, and restored green result. This includes LSP
controls and cleanup receipts; a later diagnostic summary is not the original
response. Save the raw result once in the goal's evidence directory and link
it from the report rather than copying it into every checkpoint.

If an original result was not retained, record that gap explicitly; do not
reconstruct a transcript from memory. Schedule any necessary replacement
control with the next appropriate validation unit. Reuse retained evidence
when its identity and coverage satisfy the goal and repository catalogue;
missing provenance must not become either a false acceptance or a reason to
repeat unrelated green work.

A disposable tree for goal `GOAL` belongs at `.worktrees/GOAL-control`,
`.worktrees/GOAL-mutation`, or `.worktrees/GOAL-rehearsal`; the build owner
accepts those three suffixes as that goal's own and refuses any other. Never
run a destructive mutation in the goal worktree because the owner would refuse
the control tree. A dependency census — updating one Git-pinned Lake
dependency and rebuilding the full target to see what moves — runs only in the
rehearsal tree:

```sh
~/creme/scripts/creme lake-build GOAL --census --dependency jaune --wait 900 --
```

It takes host exclusivity, keeps the dependency Git-pinned, and records the
resolved revision on its ledger row.

Inspect the full diff and status, stage only owned paths, commit coherent green
checkpoints. Never force-push. A worker's return is its local commits; the
master pushes branches at coordinated durability or integration checkpoints
(a local commit is not an off-host backup), and default/protected branch
merges belong to it under the merge policy in [the master guide](master.md).

## Context and completion

Handoff when crossing a clean expertise or self-hosting boundary, when a hard
resource trigger requires restart, or when context no longer supports the next
coherent unit. Record what was verified rather than relying on client memory.

Completion is a condition-to-evidence proof on one exact candidate, not a
completed task list. Re-run drift-prone checks, close independent review
findings, account for compatibility paths, and report branch/worktree/push
state. Where the repository supports it, the verification evidence is a
complete content-valid manifest rather than a claim that every body happened
to re-execute. If a user-owned publication or license gate remains, or the master has not
yet merged the candidate under its policy, the goal remains open.
