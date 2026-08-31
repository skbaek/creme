# Provenance and publication disposition

Creme's public tree is a reviewed content extraction, not a publication of the
private Elanc or Plans repositories. The machine-readable record is
`scripts/extraction-manifest.json`; `docs/extraction-matrix.md` is its review
view. No private Git history, client memory database, session history,
credential, approval state, trust record, host profile, or measured host state
is part of the extraction.

## Pinned source snapshots

| Source | Selected ref | Compared ref | Observed divergence |
| --- | --- | --- | --- |
| Elanc | `629f7f4bb545d5f8aee7cd2b4cabfd240ae904ab` | `origin/main` `1cbe5af41390d46b27fedffa2c26ff09da6b5876` | Selected ref is one commit ahead. Commit `629f7f4` changes `AGENTS.md`, `scripts/codex-host-semaphore`, and `scripts/tests/test_host_semaphore.py` to add the guarded manual semaphore. The extraction intentionally uses this reconciled local ref. |
| Plans | `59f58a81e33ff50fd1c955b1e1468ae42253f916` | `origin/main` `3c50a515629cc882d06b97a799bb4ef9e83899f1` | Selected ref is two commits ahead and eleven behind; merge base is `b695afd71287b285a64a1f7ee73fafb7cfaee8a1`. The two local commits amend `guide/lead.md` for the manual semaphore and add the Creme goal/memory-extraction inputs. Named files are hashed from the selected ref, so unrelated working-tree changes and remote-only commits cannot leak into this packet. |

Elanc was clean during the census. Plans had unrelated modified and untracked
work, so every recorded Plans digest was computed from `git show` at the pinned
ref, never from the working tree. A Git author census found one author identity
across the selected Elanc history and selected Plans paths. That is useful
ownership evidence but is not a license grant.

Creme's reviewed history begins with `ec0972b` and the self-hosting spine; it
does not share or merge the private source histories. Exact provenance is
content-level and limited to the four files named below.

## Exact copies and derived content

Four artifacts are byte-for-byte moves from Elanc:

- `.agents/skills/lean-prover/tools/check-drift.py`
- `.agents/skills/lean-prover/tools/leanlex.py`
- `.agents/skills/lean-prover/tools/lsp-probe.py`
- `.agents/skills/lean-prover/tools/mk-prefix.py`

For each, the destination SHA-256 equals the recorded source SHA-256. All other
artifacts are explicitly `derived`, `derived-symlink`, or
`derived-compatibility-shim`; semantic similarity is never presented as an
exact copy. The source and destination membership for every derived artifact is
normalized in the manifest so each destination appears once.

Thin new package glue (`creme/__init__.py`, `creme/__main__.py`) is not an
extraction claim. It is ordinary Creme-authored code around the inventoried
capabilities.

## Installed-helper divergence

The installed helpers were evidence inputs, not source-of-truth inputs. They
were hashed without executing them or inspecting process arguments.

| Helper | Elanc selected source SHA-256 | Installed SHA-256 | Disposition |
| --- | --- | --- | --- |
| telemetry | `98476361b4621c46728248e3fd54a4c35e4b8866b3fda5e73a014375c2cd9189` | `b9ca36c8f94150ce94eb6c880a00bfe23a92a60d3724d257f4f4f9b29905a16e` | Divergent; the installed bytes do not match any version of this path reachable in the inspected Elanc refs. Creme derives from the selected source and dispatches through adapters. |
| semaphore | `51d572f7e757e7fb9344caba2880393c4b2350151118e74b507b11cdc7164dc0` | same | Current installed helper matches the reconciled local source, including guarded manual-lock behavior. Creme reimplements that contract portably. |
| Lean reclamation | `20b87ccc806fb4c412fbc37df2d4e3fdde355fad5823cdba6d5d49d7213bfb98` | `5cd4ec2f01f006c506fa50f1242243f4cfeb769ee335d672d62b679e2f02fcbb` | Stale; installed bytes match historical Elanc commit `9b55ce6db4778353f9bcc7534f8e73d3559ed77d`, before the current hard-pressure/frozen-plan closure. Creme derives from the selected source. |

This divergence is why the public code does not shell out to host-global helper
paths or treat installed copies as provenance authorities.

## Private paths, active references, and transitive evidence

The private sources contain home-relative/absolute repository paths, direct
calls to installed helpers, client-global configuration assumptions, and dated
host measurements. Public client configuration is rewritten to relative
project paths; host facts move to an ignored validated profile; direct OS
commands are confined to adapters. The public runtime has no Elanc or Plans
dependency. Provenance documentation necessarily names those repositories but
does not make them executable inputs.

Plans still contains active, non-historical documents that point at
`guide/goal.md`, `guide/lead.md`, `guide/lean-edit-loops.md`, and sometimes
Elanc. The census found references in Plans `AGENTS.md` and current goal,
portfolio, proposal, and backlog documents, not just archives. Those references
must be updated or supported by concise compatibility notices at cutover. They
are not copied into Creme, and historical records are not rewritten.

The edit-loop guide cites five private evidence inputs recorded in the
manifest: the controlled edit-loop experiments, the ceiling disposition
ledger, the G7 admission policy, the scratch-fabrication study, and the
observer-divergence study. They were reviewed to decide which generic rules
survive. They remain private, are not public runtime dependencies, and do not
grant project-specific gate policy to Creme.

`creme-memory-extraction.md` is the durable reviewed carrier for selected
client-memory lessons. The opaque memory itself was not read into Creme and is
not migrated. Each carried Tier A/B theme is merged into a named public home;
the manifest records the document as a derived source and marks client memory
as non-migrated.

## License disposition

Publication is blocked pending the user's license selection. Neither pinned
source snapshot contains a tracked `LICENSE`, `LICENCE`, `COPYING`, or `NOTICE`
file. A targeted source scan also found no SPDX identifier, copyright notice,
or license-grant header in the inventoried paths. The single observed Git author
identity is provenance evidence only and cannot substitute for permission.

Before the first public push occurs, the user must select and record Creme's
license and confirm that it covers both the exact moves and the derived content
in the manifest. Until then, the machine ledger's status remains
`publication-blocked-pending-user-selected-license`.

## Public repository state

On 2026-08-31 the owner created
`https://github.com/skbaek/creme` and granted the publication token the same
repository permissions used for the sibling public projects. A read-only API
check found the repository public and empty, with no default branch. The local
canonical checkout records it as `origin`; no Creme history has been pushed.
Repository creation is therefore closed, while license recording and the first
public push remain distinct open gates.

## Reproduction commands

Set `ELANC_ROOT`, `PLANS_ROOT`, and `CREME_ROOT` to the three checkouts. These
commands enumerate the reviewed populations and pin the comparison:

```sh
git -C "$ELANC_ROOT" status --short --branch
git -C "$ELANC_ROOT" rev-parse HEAD origin/main
git -C "$ELANC_ROOT" ls-tree -r --name-only 629f7f4bb545d5f8aee7cd2b4cabfd240ae904ab
git -C "$ELANC_ROOT" diff --stat origin/main..629f7f4bb545d5f8aee7cd2b4cabfd240ae904ab

git -C "$PLANS_ROOT" status --short --branch
git -C "$PLANS_ROOT" rev-parse HEAD origin/main
git -C "$PLANS_ROOT" merge-base HEAD origin/main
git -C "$PLANS_ROOT" ls-tree -r --name-only 59f58a81e33ff50fd1c955b1e1468ae42253f916 -- \
  guide/goal.md guide/lead.md guide/lean-edit-loops.md \
  creme-agent-infrastructure-goal.md creme-memory-extraction.md evidence

git -C "$PLANS_ROOT" grep -n -E \
  'guide/(goal|lead|lean-edit-loops)\.md|creme-memory-extraction\.md' \
  59f58a81e33ff50fd1c955b1e1468ae42253f916 -- '*.md' \
  ':!archive/**' ':!reports/**' ':!evidence/**' ':!state/**'

find "$CREME_ROOT" -maxdepth 5 \( -type f -o -type l \) -print
git -C "$CREME_ROOT" log --all --oneline --decorate --stat
```

For a source digest, use `git show <ref>:<path>` and SHA-256 the exact bytes.
The test automates this when the optional private checkout variables are set:

```sh
CREME_PROVENANCE_ELANC_ROOT="$ELANC_ROOT" \
CREME_PROVENANCE_PLANS_ROOT="$PLANS_ROOT" \
python3 -m unittest scripts.tests.test_extraction_manifest -v
```

Without those variables, the same test remains public/clean-room safe: it
checks the schema, source/ref/hash shape, one-to-one destination ledger,
destination presence, symlink targets, exact-copy destination hashes, source
digest equality for exact-copy claims, and the absence of placeholder
dispositions.

## Remaining decisions

- **Blocking:** select and record the public license.
- **Migration:** update active private references or install compatibility
  notices after self-hosting is proven; do not edit historical evidence.
- **Behavioral coverage:** the Elanc doctor and semaphore tests are classified
  as reexpressed behavioral inputs. Their public counterparts exist, but final
  integration should compare named edge cases before declaring C6 closed.
- **Installed helpers:** retain the hash table above in the final review so a
  later machine-local reinstall cannot silently rewrite provenance.
