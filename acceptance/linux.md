# Linux safety acceptance

Status: **public Ubuntu CI PASS; conventional Linux client/edit OPEN.**

The portable suite contains forced Linux selection, unsupported-capability,
wrong-OS executable, profile, copy-preview, semaphore, client, and public-path
controls. `.github/workflows/ci.yml` defines Python 3.9 and 3.12 jobs on
`ubuntu-latest` and `macos-latest`, plus a fresh Ubuntu sibling-layout job that
checks out public Jaune and Blanc and exercises platform detection, profile
preview/write/validation, doctor, telemetry, temporary-directory creation,
reflink-or-copy, semaphore acquire/status/release, structured unavailable
reclamation, the portable suite, and final repository cleanliness.

Public push [run `33355230556`](https://github.com/skbaek/creme/actions/runs/33355230556)
exercised exact Creme
`d2bd6ea29e306f5284b30a8b89470616bbad14b4`, Jaune
`ae1b7d51f79205a15fc946034b4fb18085dcddad`, and Blanc
`18ca2b4310688465300378067b3a76f9bfadf4a5`. All five jobs passed: the four
portable OS/Python combinations and `linux-runtime`. The runtime job performed
real public sibling checkouts, returned structured `UNAVAILABLE` plus restart
guidance for Linux reclamation, and left the Creme checkout clean. GitHub's
Node 20 deprecation annotation for the maintained major action tags is a
non-blocking dependency warning, not a test failure.

The public CI closes the automated half of C7. The release candidate still
needs, on a separate fresh conventional Linux checkout with no private
Plans/Elanc and no prior client registration:

```sh
python3 -m creme platform
python3 -m creme init
python3 -m creme init --write
python3 -m creme validate-profile
python3 -m creme doctor
python3 -m creme telemetry
python3 -m creme reclaim --dry-run
./scripts/check.sh
```

Record exact commit, distribution/kernel, architecture, memory, cores,
filesystem/copy result, client versions, and every `UNAVAILABLE` result. Verify
that no Darwin binary is attempted, the effective heavy-worker count is
conservative, temporary creation succeeds, cache copy uses reflink-auto or the
portable fallback, and reclamation clearly directs the user to restart the
client. Then run the documented sibling Lean/MCP edit and cheap gate.

No Linux parity or full acceptance claim is made until the separate
conventional-host/client evidence above is recorded.
