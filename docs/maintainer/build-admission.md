# Build admission and ledger internals

Maintainer reference for Creme's semaphore, owned-build wrapper, and build
ledger (`creme/semaphore.py`, `creme/build_ownership.py`). An agent running a
build or gate does not need this: the tools enforce it, and the operating
contract is [the execution guide](../guides/execution.md). Moved here verbatim
from that guide on 2026-09-23 (goal `creme-execution-guide-cut-20260923`).

Observed behaviour as of that date: in the first Phase B gate catalogue after
launch-and-watch merged (Creme 9a39ebe), every gate row took its own soft hold
at a 3–8 GiB need and was admitted at about 79% free, one row took an
exclusive hard hold, one `--wait` build was admitted after 0 s at a 3.56 GiB
need, and fresh builds took no hold. No `LIGHT_ONLY`, `WAIT_TIMEOUT`, drain,
or retraction occurred; the only retraction rows in the live log that day came
from unit tests.

## Launch and watch: what "currently fit" means

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
16.8 GiB is available.

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
keep the renewal verdicts of the execution guide; they are never retracted.

Before it blocks, a `--wait` prints its own arithmetic, says where the need
came from, and says what would fit if this one does not:

```
fit: need 10.2 GiB + admitted 4.0 GiB + floor 2.0 GiB = 16.2 GiB; 15.4 GiB available now (64% free) -> does not fit now
fit: at this instant a need of at most 9.4 GiB would fit; a larger one is queued in arrival order but passed over by every request that fits
fit: need 10.2 GiB is derived, not explicit (derived: broader rebuild: …)
```

## Status-line attribution

`IDLE_HOLD` counts a holder idle when no `lake` or `lean` process has worked
for two minutes, among the holder's own children or inside the goal's
worktrees. A process is `lake` or `lean` by its
executable — the basename, or an elan toolchain path — never because the word
`lean` occurs in its name: a macOS daemon called `CleanupPreparePathService`
once suppressed the signal host-wide for a whole window. When a `lake`/`lean`
process cannot be placed, the line says `ATTRIBUTION_UNAVAILABLE`, no hold is
called idle, and one `attribution` row in `.semaphore/state/log.jsonl` names
the process and the cause, with an `ATTRIBUTION_RESTORED` row when it clears;
the same cause is logged once, not once per `status`.

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

## Pressure signals

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

On limited hosts, adaptive admission uses one hard heavy operation at a time
and asks for frequent checkpoints. Missing full telemetry is not a pressure
signal, and the aggregate headroom probe is intentionally independent of
process discovery. Missing headroom forces serialization rather than an
optimistic soft hold. Never edit semaphore state or use a bare process kill.

## Wind-down transaction

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

## Wrapper options, cache, and sizing

`--threads` accepts `1` or `2` and defaults to `2`; it sets
`LEAN_NUM_THREADS` for that owned invocation. A one-thread choice does not
change target interpretation, admission, sizing, measurement, or release, and
does not by itself prove that only one compiler process ran. Wrapper options
belong before `--`; every token after it remains a Lake target. The Linux
contained-build broker keeps its separate restricted interface and does not
gain this option.

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

Without `--contention` and `--memory-gib` the wrapper derives both from
evidence about the *modules that are stale right now*, not from the name of
the target list. The probe resolves the targets to their
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

## Build-measurement identity

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

Build artifacts and verdicts may be reused by identity rather than location
when the identity covers every verdict-relevant input, the object is immutable
once written, and reuse remains within one host, user, and toolchain. Symlinks
to mutable state and remote or cross-host stores do not satisfy that trust
boundary. The build ledger uses Lake input hashes only to measure duplicate
elaboration; it is performance state and never gate evidence.
