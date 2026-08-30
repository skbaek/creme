# Linux safety acceptance

Status: open pending a conventional Linux host and public Ubuntu CI.

The portable suite contains forced Linux selection, unsupported-capability,
wrong-OS executable, profile, copy-preview, semaphore, client, and public-path
controls. `.github/workflows/ci.yml` defines Python 3.9 and 3.12 jobs on
`ubuntu-latest` and `macos-latest`.

These static/unit controls are not a substitute for C7. The release candidate
still needs, on a fresh Linux checkout with no private Plans/Elanc and no prior
client registration:

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

No Linux parity or acceptance claim is made until that evidence and the public
Ubuntu workflow exist on the exact candidate.
