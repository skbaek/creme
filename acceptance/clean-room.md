# Offline clean-room preflight

Date: 2026-08-31 (Asia/Seoul)

Status: **PASS as an offline packaging and onboarding precursor; not C9 public
clean-room acceptance.**

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

This was deliberately **not** a network clone, did not prove that published
Creme history is cloneable, and did not trigger public CI. Rewriting a
disposable remote URL is a contract simulation, not publication evidence. The
owner subsequently created the empty public repository, which still contains
no candidate history.

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

C9 still requires three fresh clones from the actual public remotes after the
owner approves the license and the first push. That run must
also exercise a fresh client trust/approval ceremony, Lean MCP liveness, live
Claude Code, and the representative sibling edits under the public-only
layout. Public CI, conventional Linux acceptance, and the owner decision on
the recorded macOS behavior differences remain separate mandatory gates.
