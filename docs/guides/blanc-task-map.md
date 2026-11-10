# Blanc task map

Where to look in Blanc, by task, and how a Creme session or worker works
there. Read it when Blanc work comes up; nothing loads it at startup.

Jaune and Blanc track no agent instruction file (`doctor`'s "standalone agent
boundary" row checks that). What is addressed to a session is stated here;
what is a fact about Blanc stays in the Blanc document that owns it, and this
guide only points there. Paths are relative to the Blanc checkout under test.
A pointer names a document and a section, never a line. When this map and a
Blanc document disagree, the Blanc document governs: follow it and report the
drift.

Blanc is standalone and Creme is optional; Blanc's README says so where it
points at Creme. Commands and pass criteria for every gate are in Blanc's
`scripts/GATES.md`. This guide routes to gates and never restates them.

## Before writing a lemma, definition, or tactic

- **An existing lemma or definition.** `docs/COMMON_API.md`: one root
  question, then a branch (E execution, I invariance, S state, M memory,
  T settlement, C compilation and deployment), then named declarations. The
  file is large: grep a keyword and open its branch; do not read it top to
  bottom.
- **Goal-shaped advice.** `blanc_suggest` at the goal (README, "Proof recipe
  lookup"). The tactic lives in `Blanc/ProofRecipeTactic.lean`, a leaf that
  no production module imports: add `import Blanc.ProofRecipeTactic` to the
  file under edit (nothing downstream rebuilds) and remove the import before
  you commit. It is advice only, and a no-match is not evidence that nothing
  exists (`docs/COMMON_API.md`, "Common-library-first workflow", step 1).
- **The recipe lookup.** `docs/PROOF_RECIPES.md`, generated from
  `scripts/proof-recipes.toml`: edit the TOML and regenerate as README "Proof
  recipe lookup" says, never the generated files. Few recipes match
  lifted-program goals (`docs/LIFT_WORKFLOW.md` says so in its opening
  paragraph), so under `Blanc/Lift/` expect `blanc_suggest` to print nothing
  for a run or walk goal (`SFunc.RunExact`, `SProg.RunExact`, `RunP`) and
  use the next two entries.
- **Lifting or certifying deployed bytecode.** `docs/LIFT_WORKFLOW.md`
  (producer, registry rows, assembly modes, gates), then `docs/COMMON_API.md`
  section C9 for the proof kits.
- **An authoring tool or a tactic.** `docs/TOOLS.md`: the simp-migration
  pipeline, the discharge trace, the census tools, and a tactic table
  (`kernel_rfl`, `show_nth`, the walk step tactics and their name clashes).
- **An EVM-generic execution fact.** Search Jaune first: its `API.md`
  (section 6 lists the lemma families for downstream proof libraries). A new
  generic execution fact belongs in Jaune (`docs/COMMON_API.md`, the root
  section). A worker whose brief owns only Blanc paths returns that need to
  the master: the Jaune edit and the pin move that delivers it are outside a
  Blanc brief.

## Hitting a wall

- **A heartbeat or recursion-depth limit, or a slow `simp`/`decide`.** README
  "Proof-performance conventions", then `docs/offender-catalogue.md`. Raising
  `maxHeartbeats` or `maxRecDepth` is not the fix: `scripts/GATES.md` says
  what `scripts/check-proof-debt.sh` rejects.
- **Kernel deep recursion in a `decide +kernel` over an artifact.**
  `docs/COMMON_API.md` C8, and `kernel_rfl_and` in `docs/TOOLS.md`.
- **A gate you do not know how to run.** `scripts/GATES.md`, "Which gate to
  run first": the table that begins "Choose the gate by what you changed,
  cheapest falsifier first" maps a change to the gate to run first, then to
  the full set.
- **A published number or claim.** `scripts/check-doc-counts.sh` and
  `docs/DEPLOYED_BYTECODE_CLAIM_MAP.md`. Never edit the claim map,
  `scripts/ClaimCheck.lean`, `scripts/leaf-count.json`, or a pinned statement
  to reach green: these are Blanc's instances of the
  [never-weaken rule](../../AGENTS.md#verification-and-evidence) (Blanc
  `scripts/GATES.md`, "Rules": "Never weaken a gate to make it green").
- **Published wording.** The README's lift bullet, the claim map, and the
  site are published wording (`docs/LIFT_WORKFLOW.md` lists them with their
  gates). A worker does not change them on its own judgment: it returns the
  proposed wording to the master, and a public claim or count is never
  autonomous.

## Which gates for which change

Cheap, Lean-free gates first. Every row below is a row of the `scripts/GATES.md`
table named above, which governs; this list is only the order to reach for
them.

- **Any change:** `scripts/check-doc-counts.sh` and
  `scripts/check-layering.sh`; add `scripts/check-trust-surface.sh` for an
  import or imported source.
- **Lean proofs:** `scripts/check-proof-recipes.sh --base main`,
  `scripts/check-proof-debt.sh`, `scripts/check-proof-module-size.sh`,
  `scripts/check-proof-duplication.sh`, `scripts/check-explicit-simp.sh`;
  then the [owned build](execution.md#one-compilation-owner); then
  `scripts/check.sh --no-build`.
- **A lifted certificate, its input, or the producer:**
  `scripts/check-lift-certificates.sh`, then the owned build of its
  `cert_check`.
- **A claim-map row or a pinned headline:**
  `scripts/check-deployed-claim-map.sh` and `scripts/check-claims.sh`.
- **A new or moved module:** `scripts/check-layering.sh` classifies every
  module; register it as "House rules" says.

Where the catalogue writes `lake build`, a Creme session runs the owned-build
wrapper. Blanc's elaborating gates ask the host for their own hold, named
after the goal worktree, so settle the outer hold as
[the briefs guide](briefs.md#name-the-hold-the-gates-themselves-take) says
before running one. Timing runs, mutation campaigns, and long gates follow
[execution](execution.md#timing-authoritative-runs-and-mutation-campaigns).

## House rules

- **Common library first.** Search before you write (`docs/COMMON_API.md`,
  the recipes, `lean_local_search`). Prove a generic-shaped statement once,
  in a shared module, and cite it in the right need branch of
  `docs/COMMON_API.md` in the same change. The standing workflow is the
  section "Common-library-first workflow" at the end of that file; Creme's
  [`templates/shared/library-first.md`](../../templates/shared/library-first.md)
  is the short form every worker carries.
- **Placement.** New shared code goes in the narrowest shared owner: a new
  low module imported only by its consumers, registered in `SHARED` in
  `scripts/check-layering.py` (a contract-neutral module names no contract;
  contracts stay siblings). A hub is a module most of the tree imports;
  `docs/COMMON_API.md` defines it in step 4 of the workflow, and that
  definition, not a list here, says which modules are hubs. Add to a hub
  only by design and with the master's approval, after
  [counting its reverse dependencies](execution.md#builds-and-throughput):
  an edit to a hub rebuilds most of the tree.
- **Leaf rule.** A declaration nothing uses is a leaf, and every leaf must
  be independently valuable or deleted (`scripts/GATES.md`, "Leaf audit").
  Where a new shared declaration is born is step 4 of the workflow in
  `docs/COMMON_API.md`; a declaration your change leaves with no user is a
  leaf under this rule: delete it or justify it. The published leaf count is
  generated ("Leaf audit" names the generator), never hand-edited.
- **Generated files** (`Blanc/Lift/**/Cert.lean`, registered `Check*.lean`,
  the recipe lookup, counts) come only from their registered generator
  (`scripts/GATES.md`, "Rules": "Generated artifacts come from their
  generators"). Change the generator and regenerate.
- **Statements stay byte-for-byte** for every declaration named in
  `scripts/ClaimCheck.lean`, the claim map, or a gate's pinned list. Grep
  the name before you touch a statement.
- **Build narrow.** Elaborate the module you changed and its direct
  consumers, not the whole tree; see
  [builds and throughput](execution.md#builds-and-throughput). Keep a heavy
  kernel `decide`/`rfl` over large data out of the language server: edit
  with a placeholder and check through a narrow owned build
  ([Lean edit loops](lean-edit-loops.md#a-file-the-language-server-cannot-hold)).

## Host-coordinated maintenance

- **Elaboration timing and the offender census.** `scripts/check-elab.sh`
  and a fresh census under `docs/offender-catalogue.md` are
  timing-authoritative: follow
  [execution](execution.md#timing-authoritative-runs-and-mutation-campaigns)
  for the hold, the language servers, and the telemetry around the run.
- **Cache garbage collection** (`scripts/gc-lake-artifact-cache.py`; README
  and `scripts/GATES.md` describe what it keeps and refuses). Run it only
  from a goal that holds the `exclusive` hold, and only when the brief or
  the master asks for it: read the default dry run first, then pass
  `--execute --ack-exclusive-semaphore`, and release the hold when it ends.
