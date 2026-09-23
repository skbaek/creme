# Clean-room acceptance

Date: 2026-08-31 (Asia/Seoul)

Status: **actual public clone/onboarding PASS; fresh-client/edit portion of C9
remains open.**

## Actual public-remote run

An empty disposable parent received only ordinary HTTPS clones from the three
actual public repositories. It contained no Elanc, Plans, inherited client
home, or pre-existing project checkout.

| Repository | Exact public commit |
|---|---|
| Creme | `d2bd6ea29e306f5284b30a8b89470616bbad14b4` |
| Jaune | `ae1b7d51f79205a15fc946034b4fb18085dcddad` |
| Blanc | `18ca2b4310688465300378067b3a76f9bfadf4a5` |

All paths below are represented relative to `<public-root>`; no machine-private
path is part of the public record.

| Check | Verdict | Evidence |
|---|---|---|
| public cloneability | PASS | all three default branches cloned from their documented GitHub URLs; Creme defaulted to exact public `main` |
| portable suite | PASS | `./scripts/check.sh`: 53 tests plus compile and working/committed-tree whitespace checks |
| platform/init/profile | PASS | Darwin selected; preview wrote nothing; reviewed write produced the ignored profile; validation returned `VALID` |
| root and sibling doctor | PASS | every check `OK`, including public origins, sibling access, standalone boundaries, MCP pin, Claude shims/skills, portable paths, and secret scan |
| isolated Codex profile | PASS | preview followed by a write outside the client home; mode `0600`; no private-repository or secret content |
| wrong-root control | PASS | invocation from Jaune exited 1 with `WRONG_ROOT`; sibling permission did not substitute for Creme discovery |
| cache and semaphore | PASS | APFS-clone cache copy matched by recursive diff; one soft hold acquired, appeared live, and released cleanly |
| repository hygiene | PASS | Creme, Jaune, and Blanc remained clean; generated host state was ignored |

This proves actual public packaging and the non-client onboarding path. It does
not claim a fresh Codex/Claude trust ceremony, MCP diagnostics, or a
client-mediated Lean edit.

## Earlier offline precursor

The preflight began from an empty disposable parent containing no Elanc,
Plans, client home, or pre-existing Creme checkout. Creme, Jaune, and Blanc
were cloned with `git clone --no-local` from the exact local candidates below.
The three disposable `origin` URLs were then rewritten to their expected public
URLs solely so the doctor could exercise the published-origin contract.

| Repository | Exact commit |
|---|---|
| Creme | `1c0014984841c87c8b43aa2f62811575bbb7bce1` |
| Jaune | `92b2b1eca27a569942175c2647de2b41d7402765` |
| Blanc | `162b84020f1462bc490e4f7793ce01cbf4807b1b` |

This earlier run was deliberately **not** a network clone. Rewriting a
disposable remote URL was a contract simulation, not publication evidence; the
actual public run above supersedes that limitation while preserving the
precursor as historical migration evidence.

## Results

All paths in this record are expressed relative to `<offline-root>` so the
evidence contains no machine-private path.

| Check | Verdict | Evidence |
|---|---|---|
| three-repository world | PASS | `<offline-root>` initially contained only `creme/`, `jaune/`, and `blanc/`; no Elanc, Plans, `.codex`, or `.claude` tree was present |
| portable suite | PASS | `./scripts/check.sh`: 52 tests before license recording; the license-ledger test raises the candidate suite to 53, plus compile and working/committed-tree whitespace checks |
| platform selection | PASS | `python3 -m creme platform` selected Darwin |
| onboarding preview | PASS | `python3 -m creme init` printed the profile plan without writing |
| onboarding write | PASS | `python3 -m creme init --write` created only the ignored local host profile |
| profile validation | PASS | `python3 -m creme validate-profile` returned `VALID` |
| root and sibling doctor | PASS | `python3 -m creme doctor --json` reported every check `OK`, including expected public origins, standalone repository boundaries, the pinned MCP surface, Claude shims/skills, relative sibling access, portable paths, and secret scanning |
| isolated client profile | PASS | preview followed by `--write` to `<offline-root>/client-state/codex/creme.config.toml`; the file mode was `0600` and the content contained neither private absolute paths, Elanc/Plans references, nor secrets |
| wrong-root control | PASS | from Jaune, `PYTHONPATH=../creme python3 -m creme doctor` exited 1 with `WRONG_ROOT`; all unrelated checks remained `OK` |
| portable cache copy | PASS | preview then execute copied tracked `config/` to `<offline-root>/cache-copy`; APFS clone was selected, `diff -r` matched, and no owned stage remained |
| repository hygiene | PASS | all three disposable Git worktrees remained clean; the generated host profile was ignored as designed |
| cleanup | PASS | the cache copy, client state, clones, and complete `<offline-root>` fixture were removed |

## Gates still open

C9 still requires a fresh client trust/approval ceremony, Lean MCP liveness,
live Claude Code, and representative sibling edits under the public-only
layout. Public CI is green. Conventional Linux client/edit acceptance remains
a separate mandatory gate; the owner has already approved the recorded macOS
behavior differences.
