# macOS preservation acceptance

Date: 2026-08-31 (Asia/Seoul)
Candidate exercised: local Creme `main` at
`10e1b96cca71230b13ade9371ad64a82543d4cfa`

Host facts are reported only at public-safe granularity: Darwin arm64, 10
logical cores, 24 GiB physical memory. The ignored live profile fingerprinted
those static facts and validated `VALID`; no live profile is tracked.

| Workflow | Command/control | Verdict |
|---|---|---|
| portable suite | `./scripts/check.sh` | PASS, 35 tests |
| adapter selection | `python3 -m creme platform` | PASS, Darwin selected |
| host initialization | preview, reviewed `init --write`, `validate-profile` | PASS, `VALID` |
| doctor | `python3 -m creme doctor --workspace-root <workspace> --json` | PASS, no failed checks |
| live telemetry | `python3 -m creme telemetry` outside the command sandbox | PASS, structured sample; no Lean processes |
| reclaim safety | `python3 -m creme reclaim --dry-run` outside the command sandbox | PASS, zero owned/foreign targets and no signals |
| cache copy | copied tracked `config/` to a disposable destination, then `diff -r` | PASS, APFS clone selected and bytes matched |
| temp directory | `python3 -m creme tempdir --create --prefix creme-acceptance-` | PASS, valid Darwin runtime temp root |
| semaphore X/Y | two soft holds; hard refused; release X; Y converts to hard; release | PASS, expected refusal and transitions |
| wrong root | doctor invoked from Jaune with Creme on `PYTHONPATH` | PASS negative control, exit 1 with `WRONG_ROOT` |
| fresh Codex discovery | see `acceptance/self-hosting.md` | PASS for discovery; write-mode task not launched |
| fresh Claude discovery | installed Desktop app was detected, but desktop was locked | OPEN |

The three disposable cache/semaphore/temp directories were removed after their
contents and state transitions were verified. No live global client config or
client database was mutated.

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
owner-approval question is still required before treating any material behavior
difference as the final v0.1 preservation claim.
