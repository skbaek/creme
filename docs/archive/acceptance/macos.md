# macOS preservation acceptance

Date: 2026-08-31 (Asia/Seoul)
Candidate exercised: local Creme `main` at
`df2c2b431f3a67438a1eca8ff4f743f0c585c148`

Host facts are reported only at public-safe granularity: Darwin arm64, 10
logical cores, 24 GiB physical memory. The ignored live profile fingerprinted
those static facts and validated `VALID`; no live profile is tracked.

| Workflow | Command/control | Verdict |
|---|---|---|
| portable suite | `./scripts/check.sh` | PASS before license recording, 52 tests plus compile and working/committed-tree whitespace checks; the license-ledger test raises the candidate suite to 53 |
| adapter selection | `python3 -m creme platform` | PASS, Darwin selected |
| host initialization | preview, reviewed `init --write`, `validate-profile` | PASS, `VALID` |
| doctor | `python3 -m creme doctor --workspace-root <workspace> --json` | PASS, no failed checks |
| live telemetry | `python3 -m creme telemetry` outside the command sandbox | PASS, structured sample; no Lean processes |
| reclaim safety | `python3 -m creme reclaim --dry-run` outside the command sandbox | PASS, zero owned/foreign targets and no signals |
| cache copy | copied tracked `config/` to a disposable destination, then `diff -r`; repeated after staged-copy hardening | PASS, APFS clone selected, bytes matched, and no owned stage remained |
| temp directory | `python3 -m creme tempdir --create --prefix creme-acceptance-` | PASS, valid Darwin runtime temp root |
| semaphore X/Y | two soft holds; hard refused; release X; Y converts to hard; release | PASS, expected refusal and transitions |
| wrong root | doctor invoked from Jaune with Creme on `PYTHONPATH` | PASS negative control, exit 1 with `WRONG_ROOT` |
| Codex discovery and trusted MCP | see `acceptance/self-hosting.md` | PASS for exact-root discovery and current-trusted-client MCP diagnostics on Jaune `ae1b7d5` and Blanc `18ca2b4`; fresh trust ceremony and representative edit remain open |
| fresh Claude discovery | installed Desktop app was detected, but desktop was locked | OPEN |

## Representative sibling edits

Both edits ran serially under the shared soft semaphore. Each used a detached,
disposable worktree at the exact public-onboarding branch commit, copied the
corresponding main checkout's `.lake` directory through Creme's preview-first
cache-copy command, added only an untracked `CremeSmoke.lean`, and was removed
after verification.

| Repository | Exact commit | Edit and compilation | Cheap repository gate | Verdict |
|---|---|---|---|---|
| Jaune | `92b2b1eca27a569942175c2647de2b41d7402765` | `import Jaune`; `example (n : Nat) : n = n := by rfl`; `lake env lean CremeSmoke.lean` | `scripts/check-hygiene.sh`; `scripts/check-integrity.sh` | PASS; 0 unallowlisted hygiene findings and 0 pending integrity findings |
| Blanc | `162b84020f1462bc490e4f7793ce01cbf4807b1b` | `import Blanc`; same theorem; `lake env lean CremeSmoke.lean` | `scripts/check-doc-counts.sh`; `scripts/check-layering.sh` | PASS; 12/12 counts and 229 modules with no layering violation |

The first Jaune invocation intentionally demonstrated the blank-worktree
failure mode: dependencies were fetched but the project module was not yet
built, so `Jaune` was unavailable. The documented worktree cache-copy step was
then applied and the exact command passed. This is prerequisite evidence, not
a suppressed failure. Both cache copies selected APFS clone; no tracked source
or build dependency was changed.

These direct-CLI edits predate the default-branch transition and are preserved
as migration evidence. The still-open fresh Codex/Claude MCP acceptance must
repeat the representative Blanc edit against exact public `main`
`18ca2b4310688465300378067b3a76f9bfadf4a5` (or a later reviewed descendant),
which includes Creme followed by the reconciled Proxy Pair integration.

## Post-Proxy client-edit resource control

A current-trusted Codex task (`01a055dd-aae5-71a0-9edf-2ebd716a1b3e`)
launched from Creme and targeted an isolated Blanc clone at exact
`18ca2b4310688465300378067b3a76f9bfadf4a5`. It created only an
untracked `CremeSmoke.lean` with `import Blanc` and `n = n` by `rfl`, then
requested Lean MCP diagnostics before the two documented cheap gates.

The MCP unexpectedly rebuilt most of the 1,355-unit environment despite the
copied cache. It reached approximately 1,263 units without returning terminal
`success: true`; swap rose from about 1.47 GiB to 13.37 GiB, crossing the hard
host-pressure trigger. The client was interrupted, the exact isolated process
group was verified by its clone-local working directory and paths, and that
group alone was terminated. No Lean process remained and swap recovered to
about 2.98 GiB. The canonical Blanc checkout was untouched and the Creme
semaphore hold was released.

Verdict: **FAILED SAFELY / NOT ACCEPTANCE EVIDENCE**. No cheap gate ran and the
representative client-mediated edit remains open. This attempt will not be
retried automatically on the same host; the earlier post-Proxy read-only MCP
diagnostics remain valid evidence for liveness only.

All disposable cache/semaphore/temp directories and sibling smoke worktrees
were removed after their contents and state transitions were verified. No live
global client config or client database was mutated.

## Intentional differences from the private helpers

- Semaphore state uses the standard user state directory (or an explicit test
  override), not a private repository path.
- Reclamation output redacts full commands. Ordinary mode protects an entire
  server root when any non-server descendant is active; ambiguous/foreign
  trees are left alone. Linux reports reclamation unavailable.
- Cache copy uses capability-selected clone/reflink with a portable recursive
  fallback instead of assuming APFS.
- Host policy is generated from static facts; current pressure and swap remain
  live samples.

These are safety/portability changes, not gate or proof-semantic changes. The
owner approved all four named differences on 2026-08-31 for the v0.1 macOS
preservation claim.
