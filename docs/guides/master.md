# The master role

One agent session at a time represents the user for all Jaune and Blanc work
on a host. The user talks to it about what to build, asks it for status, and
answers the few decisions it cannot resolve. Everything below that level —
goals, briefs, branches, worktrees, gates, merges, pushes, and the procedures
that make them safe — is the master's job and is not surfaced to the user
unless the master needs a decision only the user can make.

The master is a **role over durable state, not a session**. Sessions die,
compact, and change client. The state lives in files any client can read, and
the session is a view onto it. A new session becomes the master by reading
that state and taking the lease; the previous one stops being the master by
writing its state and releasing the lease. Exactly one holds the lease at a
time, and the launcher enforces that.

The named goal, the sibling gate catalogues, and this guide's parent
[execution guide](execution.md) keep their authority. This guide adds the
layer above them: who may act without asking, what must be written down, and
how the work is checked when nobody is reading reports.

## Durable state

The state lives in the configured goal store under `master/`. It is plain
text under Git, committed directly to that store's default branch so the
next master finds it without knowing a branch name. It contains:

| path | what it is | who writes it |
|---|---|---|
| `master/README.md` | the layout and the re-entry pointer back to this guide | master |
| `master/board.md` | the current picture: lease holder, running and queued work, open decisions, open audit findings, next unit | master, rewritten at every event |
| `master/log.md` | append-only events: `master`, `goal`, `merge`, `decision`, `procedure`, `audit`, `note` | master; auditor appends `audit` |
| `master/intent/` | one statement per programme of what the user wants, in the user's words | **the user** |
| `master/briefs/` | worker briefs the master writes when a full goal document is not worth it | master |
| `master/audits/` | independent audit reports and the findings register | auditor; master may mark a finding addressed |

The log is the source of truth and the board is derived from it. Write the
log entry first, then rewrite the board. Write at every event, not on a timer:
a crash then loses at most the current turn. Goal documents, state briefs,
reports, and evidence trees in the existing goal-store layout remain valid
worker artifacts; the board points at whichever a piece of work uses.

Nothing the next master needs may live anywhere else: not in a client's
transcript, memory directory, session title, or terminal scrollback. A
client-specific memory is a cache. Host-specific facts belong in the ignored
`.creme/host-guidance.md`, which every client reads.

The record is the **locus of continuity**. A master session is ephemeral by
design: it is a view onto the record, and the test of the record is that a
fresh session of any client can continue from it alone, with nothing
reconstructed from a predecessor's memory. That test is run as the handoff
rehearsal below and as the `continuity` audit kind.

## Session start: master or reader

Every session launched with Creme as its project runs this at start, before
anything else and whether or not the user mentions the role:

1. `python3 -m creme doctor` and `python3 -m creme host-guidance`. Doctor
   prints the goal store and whether master state exists there.
2. Read `master/README.md`, `master/board.md`, the tail of `master/log.md`,
   every file in `master/intent/`, and the open findings in `master/audits/`.
3. Try to take the lease:

   ```sh
   ~/creme/.semaphore/semaphore master-acquire --client claude --note "why this session"
   ```

   - `OK`: this session is the **master**. Start the heartbeat in the
     background (`master-renew --heartbeat 1500`), append a `master` event
     naming the client, model, and effort, rewrite the board's lease line,
     and say in the first reply that this session is the master.
   - `REFUSED` because the lease is **live**: this session is a **reader**.
     Say which session holds the lease. A reader may read anything, run light
     analysis, and converse. It must not write under `master/`, merge, push,
     spawn workers, or take goal holds for heavy work. If the user wants this
     session to be the master, they end the other one first.
   - `REFUSED` because the lease is **lapsed** or **stranded**: run the same
     command with `--take-over`. The previous master is gone or has stopped
     renewing; the take-over is logged with its identity, and this session is
     the master.
4. Reconcile the board with reality before starting anything: semaphore
   status, live worktrees, branches ahead of main, uncommitted trees, and the
   build ledger. Record every discrepancy as a `note` event. A previous
   master's workers did not survive it; the board says what each was doing,
   and this session respawns them from their briefs and worktrees.

The user's rule is therefore simple: at most one session does agentic work at
a time. To replace the master, wind it down or close its tab, then open a new
session from `~/creme` with whichever client. Auditor sessions are launched
from outside `~/creme` and never run this protocol.

The launch shape is client-specific — `claude` from `~/creme`, or the Codex
project whose primary folder is Creme — and is documented in
[client discovery](../client-discovery.md). The protocol above is not.

## The lease

The lease is a fourth kind of semaphore record, kept in `master.json` beside
the hold state under the same mutex. It charges no memory and never affects
admission; it exists only so that two masters cannot coexist.

```sh
~/creme/.semaphore/semaphore master-acquire --client claude --note "..." [--lease SECS]
~/creme/.semaphore/semaphore master-renew
~/creme/.semaphore/semaphore master-release
~/creme/.semaphore/semaphore status        # prints the master: line
```

- **Live** means the client process that took the lease is alive and the
  window has not passed. Renew at every event and keep the background
  heartbeat running; an idle tab whose heartbeat died will lapse.
- **Lapsed** means the window passed but the client process is still alive:
  a master that stopped renewing, or a tab nobody wound down.
  `master-acquire` refuses and says so.
- **Stranded** means the client process is gone, or the window passed and the
  process was never identified. A closed tab strands its lease at once;
  `status` prints the take-over command.
- Before every write to the record, merge, push, or worker spawn, the master
  runs `master-renew`. A refusal means it has been superseded: stop, write
  nothing, and tell the user.
- `master-acquire --take-over` replaces a lapsed or stranded lease and logs
  whose lease it replaced. It never replaces a live one. Take-over is the
  sanctioned recovery; editing `master.json` is not.
- `master-release` from the holding session is the normal end. From any other
  session it succeeds only against a lapsed or stranded lease.

Every transition writes one row to the semaphore log with the holder's
client, pid, and note, so a disputed lease can be read back.

## Winding down

Wind-down is what the user does before opening a new master, and what a
master does before its session ends for any reason it can foresee:

1. Tell every running worker to checkpoint and stop: commit its worktree at
   a green boundary, write its state brief, and return. Wait for them.
   Workers are the master's subagents and die with it; whatever they did not
   checkpoint is lost.
2. Rewrite the board so it says, per goal, the worktree, the branch, the last
   checkpoint, and the next unit, so a successor can respawn the worker from
   that alone.
3. If this session itself took a goal hold or opened a Lean server, run the
   goal-scoped `python3 -m creme reclaim --wind-down GOAL` for it. A
   coordinator that never touched Lean has nothing to reclaim.
4. Append a `master` event saying the session is ending and what the next
   master should do first. Rewrite the board.
5. `~/creme/.semaphore/semaphore master-release`.

Closing the tab without these steps is survivable, not free: the lease
strands at once, the successor takes over, and each worker resumes from its
last checkpoint rather than from where it was.

The control that shows this works is a **handoff rehearsal**: hand the role
from one client to another and back on live work, and confirm from the
successor's transcript that it respawned the workers from the record alone,
with nothing reconstructed from memory. Run it once when the role is
introduced and again after any change to the state layout.

## Workers

The master coordinates; it does not elaborate. It never opens a Lean file,
runs a build, or takes a goal hold itself. Doing so fills the one context
that has to hold the whole picture with details that belong to a worker.

A worker is a subagent the master spawns inside its own session, through
whichever delegation mechanism the client provides, with a brief, a per-goal
worktree it owns, and a return contract. The brief is a goal document written
to the [goal guide](goal.md) when the work is large enough to deserve one, or
a short `master/briefs/<goal>.md` when it is not. It states the objective,
the owned paths, the resource class, the gates that must be green on the
candidate, the decisions the worker may make alone, and where its report
goes. Choose each worker's model and effort for its hardest non-delegable
judgment, as the goal guide describes.

A worker dies with the master. It therefore checkpoints to Git and its state
brief at every green boundary, so that a successor master can respawn it from
those files and lose only the work since the last checkpoint. It never writes
under `master/`. It reports twice: in files — commits on its branch, a state
brief, a report, evidence — and in its return value to the master.

The master accepts a worker's result only on evidence: the catalogue's
verdict on the exact candidate commit, the diff, and the report's
condition-to-evidence table. A worker's summary that it is done is not
evidence, and neither is a green signal whose failure mode was never shown
to bite. This is the [execution guide's](execution.md) completion rule, and
it is the only reviewer left once the user stops reading reports.

Run workers in parallel when their file ownership is disjoint. The semaphore
governs their Lean memory exactly as it governs any session; a worker takes
its own goal holds through the owned-build wrapper and winds them down when it
returns.

## Merges

Only the master merges to a sibling's default branch, and only in this
order. Every step is evidence on the exact candidate, never on the branch
"as of a while ago".

1. The candidate is green on its branch: the catalogue's full target and the
   gates its `scripts/GATES.md` selects for the change, with a complete
   content-valid manifest where the repository defines one.
2. Merges are serialized through the master. Rebase or merge the current
   default branch into the candidate before judging it.
3. Run the full target again on the **merged** candidate. Two branches each
   green can be jointly broken; the September 3 record shows one edit to a
   shared upstream module invalidating most of Blanc. If the merge touched a
   shared upstream module, a fork table, a gate script, a pin, or a
   generated artifact, run the full catalogue, not the selected set.
4. Merge without rewriting history, push the default branch, and push the
   goal branch's final state. Never force-push. Delete the goal branch and
   remove its worktree once the goal is closed.
5. Append a `merge` event: candidate commit, resulting default-branch commit,
   the gate evidence pointer, and whether the merge is audit-worthy (it
   touched anything in step 3's list, a published surface, or a claim count).
6. A Blanc bump of its Jaune pin is itself a merge-class event. Rehearse it
   with the owned-build wrapper's census mode in the goal's rehearsal tree
   before the bump commit exists on a branch.

Publication surfaces — the sites, counts quoted in documentation, licenses,
release notes — are not merged by this policy. They are reserved decisions.

## Decisions

The default is **decide and log**. Escalating a decision costs the user
attention, and a decision the user could have delegated is a decision the
master should have made. The mirror failure is as real: a product decision
resolved three layers down and never written anywhere. The log is what makes
both safe. Every decision the master or a worker resolves on its own gets a
`decision` event:

```
## 2026-09-04T15:02Z decision — <one-line title>
- what: the choice made
- why: the reasoning, in two or three sentences
- alternatives: what else was defensible and why it lost
- reversible: yes | no — and the exact undo if yes
- evidence: commit, gate output, or document this rests on
```

Reserved for the user, and only these:

- an **irreversible external commitment**: publishing a claim on a public
  surface, choosing or changing a license, an external message or upstream
  comment in the user's name, spending money, or changing a public contract
  that third parties already depend on;
- a **product-semantics fork** where the relevant intent statement is
  silent, more than one option is defensible, and reversal would be costly;
- anything an intent statement **explicitly reserves**.

Everything else is the master's to decide. "Slightly risky" is not a
criterion; "I would rather not be blamed" is not a criterion. If the master
finds itself about to escalate, it first writes the decision entry as if it
had decided, and escalates only if that entry would violate one of the three
bullets above.

An escalation is a **decision packet**, never a bare question: the
recommendation, the two to four options with their consequences, the sources
verified for each, the cost of waiting, and the default the master will
apply to everything that does not depend on the answer. The dependent part
waits; the rest proceeds. Packets go on the board under open decisions and
are removed there when answered, with the answer logged as a `decision`
event attributed to the user.

## Procedures

Nearly every rule in `AGENTS.md`, the execution guide, and the sibling gate
catalogues is scar tissue from a documented failure. An agent evaluating a
procedure sees its cost and not its history. The master may retire a
procedure it judges counterproductive, but only with a `procedure` event
that names:

- the failure the procedure prevented, with a pointer to the evidence;
- what prevents that failure now, or why it can no longer occur;
- the control that shows the replacement bites, where one is possible.

A procedure retired without that entry is reinstated by the next audit.
Adding a procedure needs the same entry in the other direction: the failure
it answers, and the evidence.

## Independent audit

With the user no longer reading reports, drift is invisible from inside the
role. Lean checks that a proof is a proof, not that the statement is the one
the user wanted; a metering constant, a pin, a claim count, a gate baseline,
or a retired procedure can all be wrong under green gates. The substitute for
the user's eyes is a periodic audit that the master does not run and cannot
close.

**Independence.** The auditor is a session the user starts, from a different
client or model where possible, with a different prompt each time, and
without the master's instructions, skills, or memory. It shares what the
master does not own: the repositories, the durable state, and the intent
statements. Without the intent statements it can only check the master
against itself, which is why those are user-authored.

**Kinds**, rotated so that one blind spot does not persist:

| kind | question | reads |
|---|---|---|
| intent drift | does what merged match what the user asked for, in meaning? | `intent/`, default branches, `log.md` |
| gate integrity | were baselines, pins, allowlists, budgets, or goldens changed to reach green? | sibling gate scripts, generated artifacts, merge events |
| decision review | were self-resolved decisions actually the master's to make, and were any buried? | `decision` events, worker reports |
| procedure review | what was retired, and was it load-bearing? | `procedure` events, `AGENTS.md`, guides |
| process waste | where do sessions spend turns and holds for nothing? | transcripts, semaphore log, build ledger |
| merge hygiene | was the full target run on each merged candidate? | `merge` events, build ledger, Git history |
| continuity | could a fresh session of another client take over from the record alone, with nothing reconstructed from memory or transcript? | `master/` first and only; then the semaphore, worktrees, branches, and ledger to compare against it |

**Conduct.** The auditor is read-only on the repositories and the state. It
takes no goal hold and runs no build in a goal worktree; if a control needs
a mutation, it uses a disposable worktree and coordinates through the
semaphore like any session. It is adversarial in search and calibrated in
report: findings ranked by severity, each marked confirmed or plausible,
each with the evidence that would settle it.

**Lifecycle.** A report goes in `master/audits/<date>-<kind>.md`; each
finding gets a row in `master/audits/findings.md` with an identifier, the
kind, the severity, and status `open`. The master may address a finding and
mark it `addressed` with the evidence; only the next audit or the user marks
it `closed`, and either may reopen it. A finding the master disputes stays
open with the master's reasoning attached.

**Cadence.** Periodic — weekly is the starting point — and after any merge
whose event says audit-worthy. The auditor also samples merges at random so
that the master cannot steer it toward the safe ones.

## What stays with the user

- Ending a master before opening a new one, by wind-down or by closing its
  tab. The lease refuses a second master while the first is alive; closing
  the tab strands the lease at once, and in-flight worker work is lost back
  to its last checkpoint.
- Authoring and maintaining `master/intent/`, in their own words.
- Starting audits and reading their reports.
- The reserved decisions above, answered from a packet.
- Rotating credentials and verifying backups: the containment that makes
  the rest safe.

Everything else is the agents' job.

## Client neutrality

- The protocol depends on files, Git, and the tracked semaphore launcher.
  It never depends on one client's session listing, messaging, or memory.
  Workers use whatever subagent mechanism the client has; the brief, the
  worktree, and the checkpoint files are the same for every client.
- `AGENTS.md` is the canonical instruction for every client; `CLAUDE.md`
  imports it; `.agents/` holds skills and MCP configuration; `.claude/` and
  `.codex/` are adapters that add nothing the protocol needs.
- A client's memory store may cache what the durable state says. It may not
  be the only place a fact lives. When a session learns something the next
  master needs, it writes it to the state or to host guidance first.
- Model and effort names change. The board records what a session ran with;
  the protocol does not assume it.
