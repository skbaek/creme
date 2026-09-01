# Migration from Elanc and private Plans guides

Creme replaces the reusable agent-workflow control plane formerly split across
Elanc and private Plans guides. It does not import either repository's Git
history, private state, host samples, credentials, or client databases.

The extraction matrix and machine-readable manifest identify each reviewed
source and its destination. Generic workflow moved or was rewritten here;
Jaune/Blanc doctrine remains repository-owned; dated/private evidence remains
private; experimental and corpus-specific tools were retired.

Migration is staged:

1. build and test the minimum Creme instruction/client/host spine;
2. launch fresh clients from the exact Creme root and prove discovery;
3. validate macOS preservation and Linux limited mode;
4. update Jaune/Blanc onboarding and current private references;
5. replace old active entry points with concise compatibility notices;
6. independently audit one exact candidate and its full public history;
7. record the owner-approved MIT license, review the exact candidate, and make
   the authorized first public push to the already-created public remote.

The repository-authority activation through step 5 is complete: Jaune and
Blanc point enhanced agent work at Creme, and Elanc's active client surfaces
fail closed with a deprecation notice. The owner approved MIT licensing and
the first push on 2026-08-31. Reviewed commit `d2bd6ea` was published as
Creme's public `main`; the initial public CI and three-repository clone/onboard
checks passed. Full v0.1 acceptance still requires the documented fresh-client
and conventional-Linux runs.

Historical documents remain historical. A compatibility notice must fail
informatively on wrong-root use and point to Creme; it must not keep a second
copy of operational policy alive.

User-local approved helper paths require a separate cutover because they are
outside every repository. A copied pre-Creme helper is not a compatibility
shim: it can retain obsolete state paths and logic. From the canonical Creme
checkout, preview and then replace the telemetry and reclamation helpers with
generated delegates:

```sh
python3 -m creme host-wrappers --output-dir ~/.codex/bin
python3 -m creme host-wrappers --output-dir ~/.codex/bin --write --replace
python3 -m creme doctor
```

The delegates execute Creme's current capability CLI. `doctor` treats an
absent set as optional, but rejects a partial, stale, linked, or non-executable
install so a repository migration cannot silently leave active host behavior
behind.

## Neutral semaphore cutover

Creme now tracks one client-neutral semaphore launcher at
`.semaphore/semaphore`. Fresh installations create ignored private state at
`.semaphore/state/`. Existing installations remain on the former XDG/user-local
state until the owner deliberately runs:

```sh
~/creme/.semaphore/semaphore migrate-state
```

Migration locks both roots, validates and copies active holds, activates the
neutral state, and retains the complete old state directory. It does not
declare a pre-neutral delegate or the legacy files safe to delete. Retire them
only after every session launched before the cutover has wound down. Current
`host-wrappers` output does not install a semaphore delegate.

Creme is optional for building Jaune or Blanc. Users who do not want the
enhanced agent workflow continue to clone and build either library normally.
