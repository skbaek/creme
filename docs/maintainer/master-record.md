# Master record internals

Maintainer reference for Creme's master-record writer. An operating master does
not need this to use the record; it is here for changing or debugging
`creme/master_runtime.py`, `creme/master_operations.py`, and
`creme/master_migrate.py`. The operating contract is in
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

Explicit legacy migration also recognizes the optional root-level
`observations.md` sidecar used by the manual workflow. Migration records its
exact bytes, size, and SHA-256 in the verified backup and report, retains the
obsolete root file, and seals it against later changes. Its prose never
becomes board facts. After migration, ongoing workflow observations are
`note` events in `events.jsonl`; `audits/` remains reserved for independent
audit reports.

A published backup directory contains exactly `manifest.json` and
`originals/`; any other child makes both migration planning and ordinary
record reads refuse unchanged. An interrupted staging backup is removable
only after every remaining node is verified as an exact publication prefix of
the current legacy snapshot. Cleanup removes that prefix in reverse
publication order and syncs each parent, so repeated process deaths leave a
smaller verified prefix that an authorized retry can continue.
