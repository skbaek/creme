# Lean edit-loop tooling

Supporting tools for `../../../../docs/guides/lean-edit-loops.md`, which is the authority
on **when** to use any of this. Nothing here should be reached for before the
guide's pre-edit question has been answered — for almost every Lean edit the
answer is "edit directly and verify", and none of these tools apply.

All are Python 3, no third-party dependencies, and none of them writes outside
the paths you give it.

## The loop

| tool | what it does |
|---|---|
| `mk-prefix.py` | fabricates a scratch prefix: a byte-identical copy of a module through one target declaration, suffix truncated, open scopes closed |
| `check-fidelity.py` | the three-leg gate every fabricated prefix must pass before a verdict from it counts |
| `check-drift.py` | verifies the real file's context zone still matches what the candidate was validated against; `--strict` at transfer time |
| `leanlex.py` | the Lean lexical scanner the others import (nesting block comments, string literals with escapes and gaps, line comments, char literals) |

Usage:

```sh
python3 mk-prefix.py --file Blanc/X.lean --decl NAME --out /path/scratch.lean
python3 check-fidelity.py --workdir <worktree> --real Blanc/X.lean \
        --prefix /path/scratch.lean --decl NAME
python3 check-drift.py --real Blanc/X.lean --prefix /path/scratch.lean --decl NAME [--strict]
```

**Truncation is the default and the settled choice.** Splice mode exists behind
a warning; the guide explains why it is not the default, and the short version
is that a two-leg fidelity check cannot see a broken splice.

`check-fidelity.py` exit codes: `0` faithful · `2` usage · `3` in-range
diagnostics disagree · `4` anchor proof state disagrees · `5` indeterminate ·
`6` faithful but collateral errors outside the preserved range.

`check-fidelity.py --workdir` is required. This prevents a developer-machine
path from silently selecting the wrong tree.

## The measurement instruments

These instruments support a new reviewed measurement packet. Creme does not
ship private host/corpus constants as portable policy.

| tool | what it does |
|---|---|
| `lsp-probe.py` | direct JSON-RPC client to `lake serve`; the only sound way to time a language-server iteration, because MCP tool calls carry no timestamps |
| `procmon.py` | peak-RSS sampler over a process subtree |
| `quadrant-bench.py` | runs one declaration through all four loop quadrants and emits a JSON record |
| `sample-targets.py` | stratified seeded draw over a repository's declarations |
| `rule.py` | the decision rule itself, executable |

**`lsp-probe.py` carries one load-bearing subtlety.** It decides a verdict is
final on the `textDocument/waitForDiagnostics` response, not on file-progress
completion and not on the first `publishDiagnostics`. Both of those are wrong
and were measured reporting **green on a file with two errors**. That rule is
read out of the Lean server's source and is pinned to the toolchain; a
toolchain bump requires re-deriving it.

## Re-measuring the guide's constants

```sh
python3 sample-targets.py --workdir <worktree> --seed <N> -n <COUNT> \
        --tier-bounds <LOW>,<HIGH> --max-baseline <SECONDS> --out draw.json
python3 quadrant-bench.py --workdir <worktree> --module Blanc/X.lean --decl NAME \
        -k 3 --separate-lsp-servers --fidelity full --out rec.json
```

The private campaign scorer and corpus-specific self-test are intentionally not
part of Creme v0.1. Recompute public decision constants with reviewed records
and a separately reviewed scoring packet before publishing them.

Any run whose output is a timing takes the host **exclusively** — no other
agent, gate, build, or sweep. Concurrent elaboration invalidates attribution.
