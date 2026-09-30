# One live goal stack

`$GOAL_STORE/goal-stack.toml` is the scheduling authority. Read it through
`python3 -m creme master stack list`, then `show ID` for the selected entry.
The default list is compact; `--json` exposes structured data. Read the named
goal and context documents before work. The regular master digest combines
this same order with the record's open decisions and findings.

The manifest has `schema_version = 1`, an increasing `revision`, and ordered
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
