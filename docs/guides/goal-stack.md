# One live goal stack

`$GOAL_STORE/goal-stack.toml` is the scheduling authority; once the stack is
[shared across hosts](#one-stack-across-hosts) that file is a stub and the
authority is the shared stack branch checked out at `$GOAL_STORE/.goal-stack/`.
Read it through
`python3 -m creme master stack list`, then `show ID` for the selected entry.
The default list is compact; `--json` exposes structured data. Read the named
goal and context documents before work. The regular master digest combines
this same order with the record's open decisions and findings.

The single-host manifest has `schema_version = 1` (shared: `2`), an increasing `revision`, and ordered
`[[entries]]`. Each entry has `id`, `title`, and `status`, plus either `goal`
(a Plans-relative file) or `done` (an inline completion condition). Optional
`context`, `next`, `reason`, `trigger`, `depends_on`, `worktree`, `branch`, and
`checkpoint` keep the summary small. File references must exist inside the
configured goal store. Documents under `master/` remain private and untracked.
The portable format uses one assignment per line, double-quoted JSON-compatible
strings and arrays, and `[[entries]]` tables; comments are allowed. Other TOML
syntax is refused consistently, including on the supported Python 3.9
system launchers. Commands generate this subset automatically.

## Selection and lifecycle

- `push --from ENTRY.json` inserts at position 1 (LIFO). `--position N`,
  `move ID N`, and `reorder ID...` explicitly change ordering.
- `next` is read-only: first active entry, otherwise first ready entry with
  no live dependencies. A push never preempts active work. Multiple active
  entries remain visible for authorized parallel work; order breaks ties.
- `update ID --from CHANGES.json` changes entry fields. Mark work active only
  when taking it up. A dependency cannot remain on an active entry.
- Blocked entries carry a reason; parked entries carry a reason or restart
  trigger. Both stay visible and are skipped. Parking is not completion.
- `complete ID --evidence TEXT` removes the entry, preserves its exact content
  and evidence in a receipt, and satisfies dependency edges atomically.
- `retire ID --reason TEXT` removes obsolete work but refuses if other live
  goals depend on it. Reconcile those goals explicitly; retirement must not
  pretend their prerequisite was delivered.
- `history ID` retrieves archived transitions and historical goal events.
  This is provenance, never an alternate live queue. A deliberate reopening
  uses `push` with current semantics and context pointing to prior evidence.

For mutations, `--expect-revision N` rejects a stale read before writing.
Malformed fields, duplicate IDs, missing references, self-dependencies and
cycles refuse without changing the stack. Small items need no separate goal
file; substantial work retains its existing contract and evidence owners.

## Authority and recovery

Only an authenticated master writes. Commands renew and recheck lease authority
inside the existing record lock; workers return results to the master. Writes
first publish a bounded `.goal-stack-pending.json` journal, then the immutable
`archive/goal-stack/transitions/REVISION.json` receipt, then atomically replace
the manifest, and finally remove the journal. An interrupted transaction is
finished by `master stack recover` (or the next authorized mutation). Readers
refuse a pending transaction. Conflicting receipts or a manifest changed
outside the pending transaction refuse for explicit reconciliation.

Do not hand-edit receipts, revisions, or the publication journal. Ignore the
journal in Git. The manifest and receipts may be versioned in a private goal
store; the entire `master/` runtime stays ignored and untracked. A missing or
invalid manifest after adoption never falls back to old events or backlogs.

## Adoption and retirement of competing queues

Prepare an ordered TOML candidate and validate every reference. Inventory old
TODOs, programme maps and other scheduling indexes; preserve their exact bytes
in history and replace live entry points with archive pointers. Preserve user
intent, independent audits, detailed goal contracts and evidence. Adjudicate
stale work before importing it; never turn every historical imperative into a
new ready goal. Keep genuine deferred commitments parked with explicit triggers.

Stage the candidate under a different filename until initialization. A file,
directory or symlink at the canonical name claims that scheduling location;
if invalid, it stops scheduling rather than reviving the historical board.
As with the existing master record, configured storage and transaction paths
may not cross symlink boundaries. This is an intentional host policy.

Run `master stack init --from CANDIDATE.toml` once from the configured master
workspace. It requires revision 0 and no existing stack or adoption marker.
It archives the initial snapshot and records the `master-goal-stack-v1`
procedure adoption. Thereafter new legacy `goal` events refuse, board goal
rows disappear, and startup/digest/reconciliation read the stack. Old hosts
remain readable before explicit adoption; they do not gain a second queue.

Finish with a read-only handoff rehearsal from the compact entry points:
recover the next goal, its full contract/context, all active work and all open
decisions/findings without scanning historical backlogs. No reader acquires
master authority to perform this check. Record the migration inventory and
rehearsal evidence in the goal report.

Shared record reads use read-only lock descriptors. If a sandbox cannot inspect
the live semaphore, the digest still exposes validated durable goals, decisions
and findings, with lease state explicitly `unavailable` rather than absent.
That descriptive view grants no master authority; normal lease acquisition and
mutation checks remain mandatory.

## One stack across hosts

There is one canonical goal stack for the whole project. It describes work
on every host, so each host accepts that some of it cannot be verified
locally. Say which host a file is on: "the file X on host Y".

**Host names.** Each host has a stable short name (`[a-z][a-z0-9-]{0,31}`,
e.g. `wsl`, `mac`) in its ignored host profile: `python3 -m creme
set-host-name NAME` (or `init --host-name NAME`). `doctor` reports it and the
shared stack's wiring. A shared stack refuses reads and writes on an unnamed
host. Renaming a host orphans its qualified references, so it needs
`--replace`. Older Creme treats a profile with `host_name` as invalid; pull
Creme before naming a host.

**Schema 2 references.** A path-valued field is either an unqualified
Plans-relative file that every host's goal store has (published on the goal
store's shared default branch), or `HOST:PATH`, a file on host `HOST`:

| Reference | On its own host | On another host |
|---|---|---|
| `goals/x.md` (shared) | must exist (fail closed) | must exist (fail closed) |
| `wsl:states/x.md` | must exist (fail closed) | shape only; shown "unverifiable here" |
| `worktree = "mac:/Users/.../x"` | never existence-checked; reconciled | skipped by reconciliation |

`goal`/`context` paths stay goal-store-relative on their host; `worktree`
must be qualified and absolute. An optional `host` field names the owning
host; otherwise the worktree's qualifier does. `stack list --mine` or
`--host NAME` (and the same options on `master digest`) show work owned by a
host; listings mark entries `@host`. Write a goal file that only one host has
as `HOST:goals/x.md`; publish it on the shared default branch before
referring to it unqualified, or the other host's stack refuses to load.

**Storage.** The stack lives on a stack-only branch (`goal-stack`) of the goal
store's origin that shares no history with its default branch: it holds only
`goal-stack.toml`, `archive/goal-stack/transitions/` and a `.gitignore` for the
journal. Each host checks it out as a Git worktree at `$GOAL_STORE/.goal-stack/`
(ignored on the default branch). Pushing it never pushes the default branch,
so a host whose default branch carries private history still shares the
stack. One commit is one revision with its receipt.

**Writes are compare-and-swap.** Every mutation (under the host's own lease,
lock and journal, as before) fetches the canonical branch, verifies that the
incoming commits form one linear chain with exactly one bound receipt per
revision (merges, skipped or repeated revisions, missing receipts and
rewritten history refuse), fast-forwards, commits revision N+1 on that head
and pushes without force. The write was prepared against the revision this
host last read: `--expect-revision N`, else the local copy's revision. If the
chain advanced since then, the write replays on the new head only when every
entry it touches is field-for-field unchanged (a `push` ID still absent), and
for `move`, `reorder` or a positioned `push` the live order is unchanged;
otherwise it refuses with "stack advanced on host Y (revision A -> B): ...;
re-read". The refusal leaves the local copy synced, so the re-read is local.
A rejected push (another host won the race) drops the never-published local
commit and re-prepares by the same rule. An unreachable origin refuses and
writes nothing. Receipts carry `actor.host` and `prepared_revision`; published
receipts are never edited or renumbered.

**Reads never touch the network.** They show this host's copy at its last
sync. `python3 -m creme master stack sync` (master only) fetches and
fast-forwards; run it before deciding from the stack. Readers also refuse a
copy whose manifest differs from its committed revision.

**Recovery.** A crash leaves the journal in `.goal-stack/`; readers refuse and
`master stack recover` finishes the commit and push. If another host
advanced the chain first, the unpublished commit is preserved under
`refs/goal-stack/unpublished/` and the write refuses for a re-read. Local
commits that are not on the canonical branch (only after a crash) refuse every
write until `master stack sync --preserve-unpublished` sets them aside the
same way.

**Talking to the other host.** Requests, answers and notices between
masters go on the separate [host comms](host-comms.md) channel
(`python3 -m creme master comms`), never in free-text stack fields. Messages
never change goal order or status; decisions and status stay here.

### Migration (the first host, once)

1. Pull Creme; `python3 -m creme set-host-name NAME`; fetch the goal store's
   origin so `origin/HEAD` (the shared ref) is current.
2. Plan without writing anything:
   `python3 -m creme master stack migrate --dry-run --host-map /Users/agent/=mac`
   (repeat `--host-map PREFIX=HOST` for every other host's paths). A ref present
   locally and on the shared ref stays unqualified; one present only locally
   becomes `THIS_HOST:`; anything else needs a host map or the plan refuses
   and lists it. The plan also proves the receipts are exactly
   `00000000..N`, linked, and bound to the live manifest.
3. As the master, run the same command without `--dry-run`. It refuses if the
   branch already exists on the origin (another host migrated: adopt instead).
   It commits the schema-1 manifest and every receipt byte-for-byte as an
   import commit, appends revision N+1 (`migrate`, recording each
   qualification), pushes only that branch, checks it out at `.goal-stack/`,
   adds `/.goal-stack/` to the goal store's `.gitignore`, replaces
   `goal-stack.toml` with the stub, and records the
   `master-goal-stack-multihost-v1` procedure event.
4. Commit the stub and `.gitignore` on the goal store's default branch. The
   default-branch `archive/goal-stack/transitions/` is now frozen history; the
   shared branch holds the canonical chain (including those receipts).
   On a later merge conflict there, keep either side: neither schedules work.

Old Creme refuses both the stub and any schema-2 manifest, so a host that has
not updated Creme stops scheduling rather than reading a stale queue.

### Adoption (every other host, once)

1. Pull Creme and the goal store's default branch (shared goal files);
   `python3 -m creme set-host-name NAME`.
2. Stop writing the local schema-1 stack. As that host's master, run
   `python3 -m creme master stack adopt-shared`. It fetches the shared
   branch, verifies the entire chain from the import commit, and checks that
   the local schema-1 receipts are a byte-identical prefix of it and bind the
   local manifest.
3. If the local stack has revisions the shared chain lacks, it refuses and
   lists them. Rerun with `--set-aside-local`; the result lists each local-only
   revision's action and payload. Re-apply the ones that still matter with
   ordinary stack commands (qualifying this host's paths as `NAME:...`) after
   adoption. Their receipt files stay in the default branch's frozen archive.
4. Adoption validates the canonical head as this host (shared and own-host
   files must exist; pull the default branch if a shared file is missing),
   then writes the stub and `.gitignore` entry and records the procedure
   event. Commit those on the default branch.
