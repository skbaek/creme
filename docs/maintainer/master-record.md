# Master record internals

Maintainer reference for Creme's master-record writer. An operating master does
not need this to use the record; it is here for changing or debugging
`creme/master_runtime.py`, `creme/master_operations.py`, and
`creme/master_retire.py`. The operating contract is in
[the master guide](../guides/master.md#durable-state).

During one authorized publication, the writer may also create one empty
private `.record-transaction-v1.*` description and one matching
`*.record-tmp` file. The atomically created description binds the operation,
nonce, source-log digest, target-log digest, and target-board digest. Readers
wait for an active writer, validate a crash-left description against the
authoritative log, and render the resulting projection without changing any
byte. Only a renewed writer holding the record lock removes a verified
interrupted publication. A temporary-looking name without the exact
description and source/target relationship is unknown data and causes
refusal; neither description nor digest grants lease authority.

The writer first renews before waiting on private serialization. Once it
holds the record lock, it enters a semaphore-owned authority transaction that
authenticates and renews the same lease while retaining the public lease mutex
through recovery and publication. Release or succession therefore completes
before that transaction and makes it refuse without a core write, or waits
until the authorized transaction finishes. The cross-subsystem lock order is
always private record serialization followed by the semaphore mutex.

The pre-master legacy migrator (`creme/master_migrate.py`, last carried by
Creme `1cc5c48`) is retired. The layout refuses its retained root nodes with a
pointer to `master retire-migration`. Retirement takes the renewed lease and
the exclusive record lock, re-checks the migration's own seals (complete
report, backup manifest digest, every backup file, retained root files equal
to their originals), reads the structured record with only those nodes
admitted, copies them to a staging directory beside the archive, verifies the
staging copy against its canonical manifest, renames it into place, and only
then removes the nodes, `migration.json` last. A retry after a crash resumes
from the verified archive; the `procedure` event is appended once, keyed by
the manifest digest. Restore verifies the archive, copies with `O_EXCL`, and
leaves the archive in place.
