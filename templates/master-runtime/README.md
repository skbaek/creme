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
- `migration.json` and `migration-backups/` appear only after explicit legacy
  migration. They bind the verified conversion and byte-identical originals.

Every runtime directory is mode `0700`; every runtime file is mode `0600`.
The whole `master/` subtree must be ignored and untracked. See the
[master guide](../../docs/guides/master.md) for authority and recovery rules.
