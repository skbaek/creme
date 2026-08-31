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
the first push on 2026-08-31. Publication remains a separate state until the
reviewed history is actually pushed to the public remote.

Historical documents remain historical. A compatibility notice must fail
informatively on wrong-root use and point to Creme; it must not keep a second
copy of operational policy alive.

Creme is optional for building Jaune or Blanc. Users who do not want the
enhanced agent workflow continue to clone and build either library normally.
