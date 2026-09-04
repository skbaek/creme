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

## Delegate by ownership

Delegate only bounded packets with disjoint file ownership, stated authority,
resource class, required checks, and a clear return contract. The lead retains
integration, conflict reconciliation, final verification, and user-only
decisions. Parallel work is useful only when it leaves evidence that can be
merged without overlapping authority.

## Resource classes

- Light: inventories, docs, static scans, link checks, and unit tests without
  Lean elaboration. No hold; prefer this class while heavy work is deferred.
- Elaboration: ordinary Lean MCP steps, focused builds, and compiling gates.
  Request adaptive admission with a conservative peak-memory estimate. It may
  grant a soft or hard hold.
- Contention-sensitive: cold or unexpectedly broad rebuilds, commands that
  create multiple Lean workers, previously observed spikes, and long
  indivisible work that could make the interactive host unusable under
  contention. Request `sensitive`; correctness under contention does not make
  soft coordination wise.
- Exclusive: timing, whole-tree sweeps, and mutation campaigns. Request
  `exclusive` and verify the host is quiet when the repository gate requires
  that stronger condition.

Classify from what the unit actually does, not from the nearest example:

| unit | class |
|---|---|
| fixture, static, link, schema, and other non-elaborating gates | light — no hold at all |
| a unit test suite that does not elaborate Lean | light |
| a warm narrow build whose stale set is small and whose measured peak is modest | `tolerant` |
| a warm **full target** whose stale closure is small and whose measured peak is modest | `tolerant` — it is judged by its closure, not by naming no target |
| a warm **package or library target** (`jaune`, `Blanc`) under the same conditions | `tolerant` — the target resolves to its Lake roots first |
| a focused language-server proof loop | `tolerant` |
| a full or package target whose Lake configuration cannot be read | `sensitive` — "roots unresolved" |
| a cold worktree, an unmeasured target, or a broad closure | `sensitive` |
| anything that creates several Lean workers, or a previously observed spike | `sensitive` |
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
`DEFER_FOR_HARD` means another hold or the parallel peak budget makes
serialization safer; `LIGHT_ONLY` means current headroom cannot preserve the
host usability reserve. Do not retry in a loop. Reorder independent light
work, wait for an existing heavy unit to wind down, or split the planned work.
The explicit `soft-acquire` and `hard-acquire` compatibility commands are also
pressure-gated; they cannot bypass a low-memory refusal. `release` removes
whichever hold kind adaptive admission selected. Explicit soft/hard releases
remain available for compatibility.

### Wait in one call; never poll by hand

`--wait SECS` on `adaptive-acquire` and on `creme lake-build` queues the
request under the same mutex and returns when it is admitted, when `SECS`
elapses (`WAIT_TIMEOUT`, nonzero exit, no hold), or immediately on a verdict
waiting cannot change — a manual human hold, the drain floor, or an estimate
whose charged peak exceeds the whole heavy-work budget. Among the waiters that
currently fit, the oldest goes first; a large request refused for headroom
never blocks a smaller one behind it, and a waiter whose process dies is
dropped. Waiting can only postpone a request. It never admits one past a
floor, and it never changes a verdict you would have received without it.

#### What "currently fit" means, in numbers

Arrival order decides between the requests that fit *at that pass*. Fit is:

```
charged  = ceil(1.25 x estimate)            # the peak multiplier
reserve  = max(2 GiB, 25% of physical RAM)  # the host usability reserve
it fits when   available >= charged + reserve
```

On this 24 GiB host the reserve is 6.0 GiB, so an 8 GiB estimate needs
16.0 GiB available and a **10 GiB estimate needs 19.0 GiB** — about 79% free.
With three sessions running, this host offered 16.5–19.0 GiB all through the
B9 window, so a 10 GiB request was unschedulable and was passed over 26 times
in 107 minutes while smaller requests behind it were admitted in 0.0 s. That
is the policy working, not a fault — **and a larger estimate than the evidence
supports is the surest way to starve.** State one only when you know the build
is cold or broad; otherwise omit `--memory-gib` and let the wrapper derive it.

You never have to guess which case you are in. Before it blocks, a `--wait`
prints its own arithmetic, and says what would fit if this one does not:

```
fit: estimate 10 GiB -> charged 13 GiB (x1.25) + reserve 6.0 GiB = 19.0 GiB needed;
     18.7 GiB available now (77% free) -> does not fit now
fit: at this instant an estimate of at most 10 GiB would fit; a larger one is
     queued in arrival order but passed over by every request that fits
```

`semaphore status` prints, under every waiter, the verdict the queue would give
it right now — computed by the same function the queue uses — with the same
arithmetic. If it says `LIGHT_ONLY` with a reserve line while the host looks
free, the estimate is the problem; if it says `DEFER_HEAVY`, something is
holding and waiting is the right answer.

#### Sizing the wait, and working while it runs

`status` prints each hold's class and how long it has been held
(`contention=exclusive held=1180s`), and a `WAIT_TIMEOUT` prints the same for
the holder it timed out behind, together with the verdict that *dominated* the
wait rather than the one on the final pass. Size `SECS` to the unit ahead: in
B9 calibration and campaign units ran 15–25 minutes, so a literal `--wait 600`
behind one of them buys nothing but a requeue and a spent turn.

A wait blocks the command that issued it. Either line up light work first and
issue the wait when you have nothing else to do, or issue it in the background
and read its result when the client hands it back. Never write a shell loop
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
language-server memory is resident. When the host cannot be attributed at all,
the line says `ATTRIBUTION_UNAVAILABLE` and no hold is called idle. A `LIGHT_ONLY` refusal for headroom names that memory and its owner;
reclaim your own with `python3 -m creme reclaim --idle-workers MIN`, which
reports every worker outside your ownership boundary instead of killing it.

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

Renewal is both a lease heartbeat and an in-session pressure check. Call it
before the next elaboration/build unit and at least every five minutes during
an interactive MCP session. Under moderate pressure—or when recorded worker
count/peak reservations are already unsafe—non-priority soft holders receive
`YIELD_HEAVY`, leaving the oldest live coherent unit priority. At the drain
threshold every holder receives `DRAIN_HEAVY`. In either case, launch no new
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
~/creme/scripts/creme lake-build GOAL --contention sensitive --wait 1800 -- Cold.Or.Broad.Target
```

The third and fourth commands carry no `--memory-gib`: a full or package target
is classified and sized from its own stale closure like any other, and an
estimate above what the evidence supports only makes the request harder to
schedule. State a class when you know something the ledger cannot — a cold
worktree, a rebuild you expect to be broad — and state an estimate only when
you also know the peak.

Probe first. Exit 0 means the selected artifacts are current; exit 3 means
stale and authorizes no work by itself. The second command requests adaptive
admission, applies `nice -n 10` and the calibrated `LEAN_NUM_THREADS`, records
the ignored host-local ledger, and releases its hold. Use the narrowest target
that can falsify the current edit inside the loop and the repository
catalogue's full target at checkpoints. Bare `lake build`, MCP `lean_build`,
`lean_profile_proof` (which shells to `lake env lean`), language-server
dependency builds, and startup cache downloads are not compilation owners and
are refused or disabled.

**Let the wrapper classify.** Omit `--contention` and `--memory-gib` and it
derives both from evidence: `tolerant` only when the probe's stale closure is
small *and* the ledger holds a measured peak below the configured threshold
for the same worktree, the same targets, and the same toolchain and Lake
manifest digests. Anything missing, drifted, or unreadable keeps `sensitive`.
State a class explicitly when you know something the ledger cannot — a cold
worktree, a rebuild you expect to be broad, a command that will spawn several
workers. The JSON records both the class you asked for and the class the
evidence supports, so a disagreement is visible afterwards.

On completion the wrapper lists the modules it rebuilt on a `restart:` line
of its own. A file worker keeps the imports it loaded when it started, so
neither the rebuild nor an edit to your own file changes what it reports about
them. To refresh one file: **query two other Lean files, then that file
again.** With `LEAN_LSP_MAX_OPEN_FILES=2` the second query evicts its worker
and the third starts a fresh one against the rebuilt `.olean`s. `reclaim
--idle-workers` frees that memory but does **not** refresh diagnostics — it
terminates the worker while the MCP layer keeps answering from its cache. A
stale `.olean` from a neighbour's build is the usual reason an agent stops
believing the language server.

Never filter the wrapper's output. `hint:` and `restart:` are printed as their
own lines precisely so a pipeline that keeps only `^error` and `Build complete`
still sees them; a filter that drops the JSON line drops them too.

### The build is not a type-checker

A wrapper build is for artifacts and boundaries. It is not how you find out
whether an edit compiles.

1. Make the edit.
2. Run `lean_diagnostic_messages` on the edited file. It reports **every**
   error in the file at once; a build reports the first one and stops.
3. For a type mismatch, `lean_goal` at the tactic and `lean_hover_info` on the
   symbol tell you what the two types actually are. Reading them is faster
   than guessing and rebuilding.
4. Build only when a module is registered or an import changes, when a
   checkpoint or commit is due, or when the repository catalogue requires it.

**A clean `lean_diagnostic_messages` pass on a file whose imports are current
is loop evidence.** It does not need confirming with a build. If diagnostics
say `Imports are out of date`, they are not current: probe, build the narrow
target, refresh that file's worker (query two other Lean files, then it again),
and read them again — that is the repair, not a reason to distrust the tool.

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

For a non-vacuity or enforcement claim, show all three controls: the surrounding
tree still works, the control fails at the intended boundary, and removing only
that control restores green. Use disposable worktrees for destructive
mutations. Preserve source artifacts and fail on stale generated output.

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
checkpoints, and push the goal's non-protected branch. Never force-push.
Default/protected branch merges belong to the master under the merge policy in
[the master guide](master.md); a worker hands its green candidate to the
master rather than merging it.

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
