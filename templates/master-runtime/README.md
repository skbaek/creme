# Generic master runtime layout

This tracked directory documents the reusable shape of Creme's private master
runtime. It is a reference, not a specimen record: do not copy it into a goal
store or add host values here. `python3 -m creme master init --apply` creates
the real owner-only runtime beneath the goal store resolved from the ignored
host profile.

```text
master/
├── README.md
├── .record.lock
├── events.jsonl
├── board.json
├── intent/
├── briefs/
└── audits/
```

- `events.jsonl` is the authoritative append-ordered event log.
- `board.json` is its deterministic, replaceable projection.
- `.record.lock` serializes authorized record transactions.
- `intent/` contains user-owned intent statements.
- `briefs/` contains private instantiated worker briefs. Start from the
  tracked [generic worker brief](worker-brief.md), then store the filled copy
  only in the ignored runtime.
- `audits/` contains independently owned reports and findings.
- Observations use structured `note` events, while `audits/` remains
  independently owned. Nodes left by the retired pre-master migration belong
  in `master-archive/` beside this directory (see `master retire-migration`).
- One empty `.record-transaction-v1.*` description and one matching
  `*.record-tmp` file may exist transiently during an authorized publication.
  The description binds source and target digests so readers can project a
  crash-left state without mutation and the next renewed writer can recover
  it. A name alone is never accepted as a transaction or lease authority.

Every runtime directory is mode `0700`; every runtime file is mode `0600`.
Files written by a client tool often start `0644`: the lease holder's
`master event` tightens owned record files and directories under the
record lock (never loosening, never following a symlink, never touching a
foreign-owned node) and lists them in `modes_normalized`, while read-only
commands refuse with the offending path, its mode, and the `chmod` that
fixes it.
The whole `master/` subtree must be ignored and untracked. See the
[master guide](../../docs/guides/master.md) for authority and recovery rules.
