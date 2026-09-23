# Independent C12 review — publication follow-up

Review date: 2026-08-31 (Asia/Seoul)

## Exact scope and verdict

An independent read-only reviewer audited exact licensed candidate
`5619d297f530c83f2c39ba0bf74b44a32d2b96a5` and all 30 reachable commits.
The review covered secrets, paths, private dependencies, licensing/provenance,
modes and symlinks, global configuration safety, CI/platform claims,
instruction consistency, and the failed post-Proxy edit disposition. It held
publication only for four stale owner-gate sentences. Exact descendant
`d2bd6ea29e306f5284b30a8b89470616bbad14b4` changed only those three
acceptance documents (11 insertions, 11 deletions); the reviewer rechecked the
delta and explicitly approved that exact commit for the first public push.

**Verdict: publication review ACCEPTED; full C12 remains BLOCKED until the
mandatory client/Linux gates close and the exact final candidate receives the
completion review.** No code/configuration security blocker was found.

## Blocking findings and retained gates

### B1 — License grant (closed)

The owner selected MIT on 2026-08-31 and explicitly applied it to both the four
byte-exact Elanc copies and the derived/re-expressed private-source material.
The reviewed tree contains `LICENSE` and the updated normative manifest. The
private provenance suite passed 7/7, including all source and exact-copy
digests, destinations, symlinks, and the MIT-ledger assertion.

### B2 — Public evidence partial; fresh-client evidence open

The reviewed commit is now public `main`. Public CI run `33355230556` passed
all five jobs, including the fresh Ubuntu sibling-layout runtime job. An actual
public-only three-repository clone also passed the 53-test suite and non-client
onboarding matrix.

Current-trusted-client Codex MCP liveness passes on Jaune `ae1b7d5` and Blanc
`18ca2b4`. The ignored-user-config controls expose zero MCP tools, including an
invocation-only trust override, so a genuine fresh Codex project
registration/approval and representative edit remain open. Live Claude Code
and the separate fresh conventional Linux client/edit run also remain open.

### B3 — Owner dispositions (closed)

On 2026-08-31 the owner approved the four named macOS safety/portability changes
and publication of the existing Git author name/email. These release decisions
no longer block C6/C12; the final reviewer must confirm that the public record
matches them exactly.

## Closed stale findings

- The public remote exists, defaults to `main`, and contains the reviewed
  history. MIT licensing, author disclosure, and the first push were approved.
- Creme authority activation is complete. Elanc fails closed, Jaune/Blanc point
  enhanced agent work to Creme, and Plans owns concrete state rather than the
  reusable execution method.
- The preserved dirty shared Plans checkout now has only its clean
  compatibility paths and goal-owned Creme state/report synchronized as a
  working-tree overlay. Its branch/index and unrelated work were not moved.
- Post-Proxy current-trust MCP diagnostics were actually invoked and passed;
  they are not misrepresented as a fresh trust ceremony or edit.

## Public-surface and history review

- No high-confidence credential, private key, assigned secret, personal
  `/Users/...` or `/home/...` path was found in any reachable snapshot.
- Runtime/configuration has no Elanc or Plans dependency. Their names occur in
  provenance, migration, optional-goal wording, acceptance status, or
  private-path rejection tests.
- Only the original three executables and two relative in-repository Claude
  skill symlinks exist. No post-review executable, symlink, or file-mode change
  was found.
- Post-`df2c2b4`, host-runtime Python and project client configuration are
  unchanged. Later executable behavior is limited to the reviewed CI workflow.
- Four dangling blobs observed before publication were unreachable and did not
  transfer in the ordinary push. Commit history exposes the recorded author
  identity, whose publication the owner approved on 2026-08-31.

## CI review

The workflow grants only `contents: read`. Its nested Creme/Jaune/Blanc layout,
working directory, shell block, expected Linux reclamation failure check, and
final clean-tree assertion are coherent. It invokes no Darwin-only command.
Candidate `d66db65` pins Jaune
`ae1b7d51f79205a15fc946034b4fb18085dcddad` and Blanc
`18ca2b4310688465300378067b3a76f9bfadf4a5`, closing the reviewer's
non-blocking reproducibility note. GitHub Actions dependencies still follow
mutable maintained major tags; this is a disclosed non-blocking supply-chain
drift risk.

## Principal review commands

```sh
git log --reverse --format=fuller 5619d29
git rev-list --count 5619d29
git ls-tree -r 5619d29
git diff --stat --summary --name-status c663814..5619d29
git diff --check c663814..5619d29
git grep <secret/path/private-runtime patterns> <each reachable revision>
git fsck --full --no-reflogs
git log --all --summary
sed <linux-runtime shell block> | bash -n
git diff --stat --name-status 5619d29..d2bd6ea
git diff --check 5619d29..d2bd6ea
```

## Close condition

Require fresh Codex and Claude Code matrices, conventional Linux acceptance,
and one final independent history/secret/path/mode/claim review of the exact
published candidate. Only that final review may change full C12 to accepted.
