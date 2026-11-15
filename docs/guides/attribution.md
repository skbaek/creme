# Commit attribution: which master session made each commit

After a master session ends, `python3 -m creme attribution report` on either
host lists which master session (host, client, model, effort, start and end)
made each commit in Creme, Jaune, Blanc and the goal store, grouped by
repository and goal. It supports an after-action review, including sessions
in which two hosts worked in parallel.

**What it claims.** Every commit created on a host that has adopted this,
while a hook could run, is attributed to the master lease held on that host
at that moment, or to `none` when no lease was held, and the attribution
reaches the private ledger readable from both hosts. Commits the hooks missed
are reported as `unattributed`, never silently assigned.

**What it does not claim.** Nothing finer than the master session: a worker's
commits belong to the master session live on its host (the goal label and the
`Co-Authored-By` model line narrow it further). Nothing about commits made
before adoption. No usage or quota accounting.

## Privacy

Which host or session did something is private. None of it may appear in
Creme, Jaune or Blanc: not in commit messages, authors, trailers, notes,
tracked files or any public surface. It lives only in each repository's
host-local journal (inside its Git directory, never tracked or pushed) and on
the private goal store's `attribution` branch. Creme tracks only this code,
its schema and this guide.

- Publication refuses a remote that any public repository uses (the Creme,
  Jaune and Blanc remotes, and their public GitHub origins).
- Never copy ledger or journal contents into a public repository. Where a
  public artifact needs a fact, restate it without host or session.
- `attribution privacy-check --repo NAME|PATH --base REV` flags, in the
  added lines and the commit metadata of `REV..HEAD`, this host's name as a
  word, any acquisition digest this host knows, and a session field carrying
  a hex value. Test fixtures under `scripts/tests/` may use synthetic host
  names; known digests are flagged there too. `doctor` fails when a known
  digest appears in a tracked file of Creme, Jaune or Blanc.

## How it works

**Capture.** Four local hooks in each repository's Git common directory
(shared by every worktree) run `creme/attribution_hook.py` after the commit
exists: `post-commit`, `post-merge` (only when a merge commit was made, not a
fast-forward or squash), `post-rewrite` (amend and rebase, with the old SHA)
and `post-applypatch` (`git am`). The hook never changes the commit, never
uses the network and always exits 0. Errors go to the host-local `hook.log`;
the census catches what was missed. The hook imports only the standard
library and runs on Python 3.9 and later, under the interpreter recorded at
install time (falling back to `python3`).

Each record is one write-once JSON file in
`<git-common-dir>/creme-attribution/journal/` (directories `0700`, files
`0600`): host (from the host profile), client and `acquisition_digest` of the
lease in `.semaphore/state/master.json` (the same digest master events carry,
so records join `master/events.jsonl`; `none` without a lease) and whether
that lease was still within its window, repository, SHA, parents, patch-id
(non-merge commits), goal label (`.worktrees/GOAL` and its `-control`,
`-mutation` and `-rehearsal` trees give `GOAL`; a primary checkout gives its
branch), commit and record times, hook, and for `post-rewrite` the rewritten
SHA. Raw session identifiers are never written. Commits in the ledger's own
worktree are not recorded.

Measured on Git 2.50 (Apple Git-155), which hooks fire:

| Operation | Hooks that fire | Recorded |
|---|---|---|
| `commit` | `post-commit` | yes |
| `commit --amend` | `post-commit`, `post-rewrite amend` | both SHAs |
| `merge` making a merge commit | `post-merge 0` | yes |
| `merge` fast-forward (`--ff-only` too) | `post-merge 0` | no (no commit) |
| `merge --squash` then `commit` | `post-merge 1`, then `post-commit` | the commit |
| `cherry-pick`, `revert` | `post-commit` | yes |
| `rebase`, `rebase -i` | `post-commit` per pick, `post-rewrite rebase` (squash adds `post-rewrite amend`) | yes |
| `am` | `post-applypatch` | yes |
| plumbing (`commit-tree`, `update-ref`) | none | census only if a reflog marks it |

**Ledger.** A channel-only branch (`attribution`) of the goal store's origin,
sharing no history with its default branch, checked out at
`$GOAL_STORE/.attribution/` and excluded through `.git/info/exclude`, exactly
like [host comms](host-comms.md). Each host writes only under
`hosts/<host>/`, every file once:

- `hosts/<host>/batches/<UTC-stamp>-<8 hex>.json`: published records;
- `hosts/<host>/sessions/<digest>/start-<stamp>-<4 hex>.json` and `end-...`:
  client, model, effort, start and end of one acquisition; the end carries
  the session's census.

Publication commits new files and pushes without force; a rejected push
fetches, rebases the never-published commit and retries a bounded number of
times. A journal file moves to `published/` only after its batch reached the
origin, so records reach the ledger or stay in the journal.

**Publication points.** `master start` (which also publishes any journal a
dead predecessor left, with that predecessor's digest), the `master` event
with `action: end`, `attribution publish`, and `attribution merge-check`,
which publishes before it reads. Publication authenticates through the
master lease as host comms writes do. A failed publication never fails
`master start` or the end event; the result says so and the next point
retries.

**Census.** The census lists commits this clone created in a window (from
its reflogs: commits, amends, merge commits, cherry-picks, reverts, `am`,
rebase picks; never checkouts, resets or fast-forwards) that have no journal
record, as `unattributed`. The end event runs it over the session window and
publishes the rows in the end record; its output names the count.

**Merge check.** Before merging to or pushing a default branch the master
runs `attribution merge-check --repo NAME RANGE`. It publishes, then refuses
(exit 2) every commit in the range created on this host since adoption that
has no record. Commits created on other hosts or before adoption pass. Attribute a
refused commit with `attribution adopt --repo NAME SHA... --reason TEXT`,
which records it under the current lease with `source: census`, `hook: adopt`
and the reason, then run the check again. The check is never advisory on an
adopted repository; adoption time survives `uninstall`.

**Report.** `attribution report` reads the ledger (fetching it; `--no-fetch`
reads this host's copy) plus this host's unpublished journal, and groups by
session, repository and goal, resolving subjects from local Git at report
time. With a time window (`--since/--until`, or a session's start and end),
commits in the window that no host recorded are listed as `unattributed`,
except those matching a recorded commit's patch-id (rebased or merged
elsewhere), which are listed under that session as `matched: patch-id`.

## Commands

```sh
python3 -m creme attribution install [--repo NAME]... [--python PATH]
python3 -m creme attribution uninstall [--repo NAME]...
python3 -m creme attribution status [--json]
python3 -m creme attribution publish
python3 -m creme attribution census [--since TIME] [--until TIME]
python3 -m creme attribution merge-check --repo NAME RANGE
python3 -m creme attribution adopt --repo NAME SHA... --reason TEXT
python3 -m creme attribution report [--session DIGEST] [--host HOST] [--goal ID] [--since TIME] [--until TIME] [--no-fetch] [--json]
python3 -m creme attribution privacy-check --repo NAME|PATH --base REV [--head REV]
```

Repository names are the configured ones: `creme`, `jaune`, `blanc` and
`goal-store`. `TIME` is `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM[:SS]Z`. `install`
refuses, changing nothing, when `core.hooksPath` is set or a hook it would
write exists and was not written by it; reinstalling over its own hooks is
allowed. `doctor` reports the install state of the four repositories.

## Adoption

A host adopts once, from the canonical Creme checkout, after the code is on
its default branch: `attribution install`, then `doctor` (the
`attribution hooks` row reads `installed` for each repository). Commits made
before adoption keep the older fallback: local reflogs plus the start and end
times in `master/events.jsonl`. Until a host adopts, its commits in a report
window read `unattributed` on every host.
