# Independent C12 review

Review date: 2026-08-31 (Asia/Seoul)

## Exact candidate and verdict

The audited code candidate is local Creme `main` at
`c8df90ffde564d82d2d46bbe7df0d5600c6b2911`, tree
`1019b7a74943cde256bd902d82e2945f64ab4e56`.

The review began on `codex/creme-v0.1-agent-infrastructure` at
`cd32139dd4ec21f1f6e55a57de93c74f66b21253`; that commit and the named
candidate had the same tree and an empty diff. During the review, the branch
advanced to lead-authored evidence commit
`9fd74d60a13ae5502cf9f0281130d364f965f7de`. Its diff from the audited tree is
limited to `acceptance/macos.md`; it updates the exercised commit to `c8df90f`,
the cheap-suite count to 44, and representative sibling-edit evidence. It does
not change runtime code or configuration. This report therefore audits the
exact `c8df90f` code tree and separately checks `9fd74d6` for that scope.

**Verdict: BLOCKED; do not publish or call C12 closed.** The candidate has no
detected credential or tracked personal-path leak, and the complete cheap suite
passes, but the license, public-remote/CI/clean-room, Claude/client, Linux, and
macOS-approval gates remain open. One additional code-level capability-contract
blocker is recorded below.

## Blocking findings

### B1 — No license grant; exact and derived private-source material is present

No reachable Creme revision contains `LICENSE`, `LICENCE`, `COPYING`, or
`NOTICE`, and no SPDX or copyright/license-grant header was found. The ledger
identifies four byte-exact Elanc copies and many derived artifacts. Both pinned
private source snapshots are themselves recorded as lacking a license grant.
`docs/provenance.md` and `scripts/extraction-manifest.json` correctly say
publication is blocked pending the owner's license selection; that disclosure
does not supply permission to publish. C1 and C12 remain blocked.

The provenance structure and every recorded source digest were independently
checked against local Git objects at Elanc
`629f7f4bb545d5f8aee7cd2b4cabfd240ae904ab` and Plans
`59f58a81e33ff50fd1c955b1e1468ae42253f916`; all six manifest tests passed.
This validates the ledger, not the missing license.

### B2 — No public remote, public Ubuntu run, or public clean-room clone exists

`git remote -v` is empty. A workflow definition exists for `ubuntu-latest` and
`macos-latest` on Python 3.9/3.12, but there is no public remote on which it
could have run and no run URL/status is recorded. Consequently a stranger
cannot execute the documented `git clone "$CREME_URL" creme`, and the required
public-only clean-room world cannot yet exist. This blocks C1, C7, C9, C11, and
the publication aspect of C12. Remote creation, public visibility, and first
push remain owner actions.

### B3 — Required client acceptance is incomplete, including Claude

`acceptance/client-discovery.md` is still an unfilled template. The older
Codex checkpoint proves static discovery only and expressly says it did not
prove MCP liveness, a Lean edit, or a fresh empty client configuration root.
The exact-candidate macOS record adds direct Lean CLI sibling edits, not a
fresh-client Lean MCP invocation. Claude remains explicitly `OPEN`; no fresh
Claude Creme-root session has demonstrated instructions, both skills, MCP
approval/liveness, sibling access, wrong-root controls, and a representative
edit. `acceptance/self-hosting.md` therefore correctly keeps the bootstrap
Plans/Elanc control plane active and does not deprecate Elanc. C2, C9, C11,
C13, and C12 remain blocked.

The committed static Claude/Codex model is not itself the problem. Current
official documentation supports `@AGENTS.md`, project `.mcp.json`, project
skill directories and directory symlinks, workspace trust, Codex project
configuration, and the distinction between sibling file access and project
configuration discovery. Static documentation agreement cannot replace the
required live acceptance.

### B4 — Linux safety acceptance and Ubuntu evidence are explicitly open

`acceptance/linux.md` says `Status: open pending a conventional Linux host and
public Ubuntu CI`. The local forced-adapter tests are useful negative controls,
but no conventional Linux run on the exact candidate records platform facts,
temp/copy behavior, structured `UNAVAILABLE` results, fresh client behavior,
or a sibling Lean/MCP edit. A workflow matrix is not evidence of a workflow
run. C7, C11, and C12 remain blocked; no Linux parity claim is accepted.

### B5 — macOS preservation still awaits the named owner decision

The lead-authored evidence-only update records the exact `c8df90f` candidate,
44 cheap tests, live host capabilities, and representative Jaune/Blanc edits.
It still says the material behavior differences require owner approval before
they become the final v0.1 preservation claim, and Claude remains open. That
keeps C6 open even though the recorded technical checks pass.

### B6 — Cache-copy execution can escape the documented structured result

`docs/capabilities.md` says every host operation reports a structured status
and describes clone/reflink followed by portable recursive-copy fallback.
`Adapter.copy_cache`, however, calls `shutil.copytree` without translating an
`OSError` into `CapabilityResult(status="ERROR", ...)`. The Darwin and Linux
adapters call that method after an optimized copy fails. If the optimized
command created a partial destination before returning nonzero, the fallback
immediately rejects the now-existing destination; other copy failures can
raise out of the CLI entirely. The tests cover preview and a successful macOS
clone, but not optimized-command failure, partial destination, or recursive
copy failure. This is fail-safe with respect to not widening a process target,
but it contradicts the public capability/error contract and leaves Linux's
required fallback unproven. Close it with exception-to-`ERROR` handling,
partial-destination policy that never deletes unproven user data, and focused
Darwin/Linux failure tests before C5/C7/C12 acceptance.

## Non-blocking findings and residual risks

- No high-confidence AWS, GitHub, OpenAI, Anthropic, Google, Slack, private-key,
  credential-bearing URL, or generic assigned-secret pattern was found in any
  reachable snapshot. No tracked `.env`, credential store, client database,
  memory, or session history exists.
- No tracked current personal/absolute host path was found. Historical commits
  contain the macOS home-root token only as a test regex, not a user path. Elanc,
  Plans, and installed-helper names occur in provenance, migration, and honest
  acceptance statements; runtime/configuration files do not reach those
  repositories. Commit metadata does contain the author's public-facing name
  and Gmail address; the owner should knowingly accept that identity disclosure
  before publishing history.
- The only executable files are the exact-copy executable
  `.agents/skills/lean-prover/tools/check-drift.py` plus the intended entry
  points `scripts/check.sh` and `scripts/creme`. The exact-copy source is also
  mode `100755`. No later executable/type change was found. The only symlinks
  are the two relative per-skill Claude links; both resolve inside Creme and
  match the manifest. Current Claude documentation explicitly supports a skill
  entry that is a symlink to another directory.
- OS-specific executable literals are confined to `creme/adapters/`.
  Non-adapter subprocesses invoke portable/project tools (`git`, `lake`, the
  Python interpreter, or a caller-supplied command). `procmon.py` still says
  the exact Darwin-shaped `ps -axo ...` command is read each sample although
  it now dispatches through adapters and Linux uses `ps -eo ...`; this is a
  documentation inaccuracy, not a cross-OS invocation.
- The generated Codex profile grants write access to exactly the three reviewed
  workspace roots and explicitly grants their `.git` directories, with network
  limited to three GitHub domains. Those are consequential permissions, but
  they match the documented coordinated-editing scope, are preview-first, and
  are not installed implicitly. Current Codex documentation confirms that
  extending `:workspace` does not itself make `.git` writable and that network
  domain rules require the proxy feature, so these settings are deliberate.
- `render_codex_profile` interpolates POSIX paths into TOML quoted keys without
  escaping quotes, backslashes, or newlines. An ordinary path renders as
  intended, while a legal path containing `"` renders invalid TOML. Preview
  makes this visible and the user must explicitly select `--write`, so this is
  not treated as silent permission widening, but robust TOML serialization and
  a hostile-path test are recommended.
- `cmd_client_profile` writes through a predictable `.tmp.<pid>` name rather
  than `mkstemp`, unlike the host-profile writer. The destination directory is
  normally user-owned and the operation is explicit, so this is hardening
  rather than a demonstrated privilege boundary break; use an exclusive
  mode-0600 temporary file and cleanup on failure.
- Semaphore corruption is safely never reset, but `_validate` validates only
  outer shape and labels. A shape-valid hold missing `renewed_at` or
  `lease_seconds` passes validation and later raises `KeyError` instead of a
  clear `SemaphoreError`. Extend field/type validation and test it. The state
  directory also relies on the user's umask; mode `0700` for the directory and
  `0600` for state/log files would better protect free-form notes on shared
  hosts.
- `quadrant-bench.py` intentionally rewrites the real target module for Q1 and
  restores it in `finally`. A kill or host loss can leave a reversible dirty
  proof file. The edit protocol is described in the tool, but the operational
  warning should be more prominent and callers should use a disposable
  worktree.
- GitHub Actions dependencies use mutable major tags (`actions/checkout@v4`,
  `actions/setup-python@v5`) rather than immutable commit SHAs. This is common
  but leaves avoidable CI supply-chain drift. Also, `git diff --check` is
  generally vacuous in a clean CI checkout; use a range-aware committed-tree
  whitespace check if that is intended as a gate.
- `git fsck --full --no-reflogs` found four dangling blobs and no reachable
  corruption. `git count-objects -v` also reports an empty worktree-admin
  `refs` directory as garbage. Neither is reachable from a branch and neither
  would be transferred by an ordinary push, but the repository owner may prune
  local unreachable objects after retaining anything intentionally recoverable.

## Authority and claim review

The committed authority split is internally coherent: Creme owns reusable
workflow/client/host coordination; Jaune and Blanc retain their gate catalogues
and technical doctrine; concrete goal state remains optional and external.
Jaune/Blanc remain build-independent. Current references to private sources are
provenance or explicit migration/acceptance status, not runtime dependencies.

The tree does not falsely claim Linux feature parity and explicitly documents
`UNAVAILABLE` behavior. It does, however, describe Creme as the public launch
root while the publication notice says publication is incomplete, and the
self-hosting record says the live goal still uses bootstrap Plans/Elanc
authority. Those statements are compatible only as a staged target/current
state distinction. Until Claude, public remote, license, clean-room, Linux, and
deprecation gates close, documentation must continue to preserve that
distinction and must not present C1-C13 as complete.

## Commands and verdicts

No repository content was mutated except the required creation of this report
and its eventual report-only commit; tests used disposable temporary roots and
the configured temporary bytecode prefix. No Lean command, client, build,
dependency fetch, remote mutation, or network mutation was run.

- `git rev-parse c8df90f^{tree}` and `git rev-parse cd32139^{tree}` -> both
  `1019b7a74943cde256bd902d82e2945f64ab4e56`; initial candidate diff empty.
- `git diff --name-status c8df90f..9fd74d6` -> only
  `M acceptance/macos.md`; manual diff scope confirmed evidence-only.
- `git ls-files`, `git ls-files -s`, full reads of all 64 tracked paths, and
  JSON enumeration of every manifest source/artifact -> complete current-tree
  inventory; only three `100755` entries and two in-repository `120000` links.
- `git log --all --graph --decorate --oneline`, `git for-each-ref`,
  `git rev-list --objects --all`, per-revision `git grep`, and
  `git fsck --full --no-reflogs` -> all reachable refs/history inspected; no
  high-confidence secret or personal path; four unreachable dangling blobs.
- Per-revision license/SPDX enumeration -> no license/grant in any reachable
  revision.
- Candidate `git grep` for personal/private paths and Elanc/Plans/helper names
  -> only ledger, migration, acceptance, or test-pattern references; no runtime
  dependency.
- Candidate subprocess/platform-literal enumeration plus manual reachability
  review -> OS commands confined to adapters; generic `git`/`lake`/Python and
  caller commands outside them.
- `./scripts/check.sh` under Python 3.9.6 -> PASS, 44/44 unit tests; compileall
  PASS; `git diff --check` PASS.
- `CREME_PROVENANCE_ELANC_ROOT="$ELANC_ROOT"
  CREME_PROVENANCE_PLANS_ROOT="$PLANS_ROOT" python3 -m unittest
  scripts.tests.test_extraction_manifest -v` -> PASS, 6/6 including every
  pinned local source digest. `ELANC_ROOT` and `PLANS_ROOT` were reviewer-local
  paths and are deliberately not recorded in the public report.
- Direct semaphore probes -> `_validate` accepted a hold with only `label`, and
  `_expired` then raised `KeyError`, confirming the residual validation defect.
- `git remote -v` -> no output; public remote gate open.

Current client-claim review used official documentation only:

- Codex: `https://learn.chatgpt.com/docs/agent-configuration/agents-md`,
  `https://learn.chatgpt.com/docs/config-file/config-basic`,
  `https://learn.chatgpt.com/docs/config-file/config-advanced`, and
  `https://learn.chatgpt.com/docs/permissions`.
- Claude Code: `https://code.claude.com/docs/en/memory`,
  `https://code.claude.com/docs/en/settings`,
  `https://code.claude.com/docs/en/skills`,
  `https://code.claude.com/docs/en/mcp`, and
  `https://code.claude.com/docs/en/permissions`.

## Close conditions

Do not change this verdict to accepted until, on one exact integrated
candidate: the owner-selected license is committed and provenance compatibility
is approved; the owner-approved public remote exists; public Ubuntu CI and a
fresh conventional Linux run pass; fresh Codex and Claude sessions complete
the full discovery/MCP/sibling/wrong-root/edit matrix; public-only clean-room
onboarding passes; the macOS behavior differences are owner-approved; the
cache-copy structured-error/fallback defect is closed and tested; Elanc is
actually deprecated/archival; and a final history/secret/mode/path diff review
finds no new blocker.
