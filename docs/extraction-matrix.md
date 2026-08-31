# Creme extraction matrix

This is the human review view of `scripts/extraction-manifest.json`. The JSON
ledger is normative for hashes and destination membership. The source refs are
Elanc `629f7f4bb545d5f8aee7cd2b4cabfd240ae904ab` and Plans
`59f58a81e33ff50fd1c955b1e1468ae42253f916`; no source history is imported.

Classification means:

- **move** — byte-for-byte content is present in Creme;
- **split** — reviewed content is rewritten or distributed across public homes;
- **remain** — the source stays private and is not a Creme runtime dependency;
- **retire** — the source is intentionally not carried forward;
- **compatibility notice** — only a client/path compatibility contract survives.

License `L1` means the owner approved the carried content under Creme's tracked
MIT License on 2026-08-31; it covers both byte-exact copies and derived or
re-expressed material. `L0` means the source was not copied and has no Creme
license impact. Evidence `E1` means private transitive evidence was reviewed
but is neither copied nor required at runtime; `E2` marks reviewed behavioral
input with public tests present but final named-case parity review pending;
`E0` means no transitive evidence is needed. These codes are expansions of,
not substitutes for, the machine ledger.

## Elanc

| Source path | Class | Creme destination(s) | Decision and evidence | License / evidence |
| --- | --- | --- | --- | --- |
| `.agents/mcp_config.json` | split | `.agents/mcp_config.json` | Keep the Antigravity-compatible pinned MCP surface; remove host wording. | L1 / E0 |
| `.agents/skills/lean-inspector/SKILL.md` | split | same path | Keep inspection workflow; replace private-root assumptions with Creme/sibling authority. | L1 / E0 |
| `.agents/skills/lean-prover/SKILL.md` | split | same path | Keep reusable proof workflow; point project doctrine back to Jaune/Blanc. | L1 / E0 |
| `.agents/skills/lean-prover/resources/proof-checklist.md` | split | same path | Keep generic tactic hygiene; leave dated Blanc proof-performance evidence in Blanc. | L1 / E1 |
| `.agents/skills/lean-prover/tools/README.md` | split | same path | Remove private paths and the superseded cost-only selector; retain lifecycle guidance. | L1 / E1 |
| `.agents/skills/lean-prover/tools/check-drift.py` | move | same path | Exact copy; SHA-256 `9d2277aa…4b09f6`. | L1 / E0 |
| `.agents/skills/lean-prover/tools/check-fidelity.py` | split | same path | Retain semantic-fidelity checks under the revised verdict-first method. | L1 / E1 |
| `.agents/skills/lean-prover/tools/leanlex.py` | move | same path | Exact copy; SHA-256 `f61910bd…97516`. | L1 / E0 |
| `.agents/skills/lean-prover/tools/lsp-probe.py` | move | same path | Exact copy; SHA-256 `6c4af367…d32ca`. | L1 / E0 |
| `.agents/skills/lean-prover/tools/mk-prefix.py` | move | same path | Exact copy; SHA-256 `7feaf211…1b7af`. Private experiments justify use but are not runtime inputs. | L1 / E1 |
| `.agents/skills/lean-prover/tools/procmon.py` | split | same path | Route process discovery through the OS adapter; remove host timing constants. | L1 / E1 |
| `.agents/skills/lean-prover/tools/quadrant-bench.py` | split | same path | Keep measurements advisory; remove policy authority from the refuted model. | L1 / E1 |
| `.agents/skills/lean-prover/tools/rule.py` | split | same path | Keep the engine; require caller-supplied reviewed model constants. | L1 / E1 |
| `.agents/skills/lean-prover/tools/sample-targets.py` | split | same path | Remove private fit targets and host/corpus cost constants. | L1 / E1 |
| `.agents/skills/lean-prover/tools/score.py` | retire | — | It promotes the cost-only selection rule contradicted by later experiments. | L0 / E1 |
| `.agents/skills/lean-prover/tools/selftest.py` | retire | — | It tests only the retired scoring policy. | L0 / E1 |
| `.claude/agents/worker-high.md` | remain | — | Current public core does not need client-specific worker presets. | L0 / E0 |
| `.claude/agents/worker-max.md` | remain | — | Current public core does not need client-specific worker presets. | L0 / E0 |
| `.claude/agents/worker-xhigh.md` | remain | — | Current public core does not need client-specific worker presets. | L0 / E0 |
| `.claude/settings.json` | split | same path | Retain relative sibling access only; remove absolute paths and broad grants. | L1 / E0 |
| `.claude/skills` | compatibility notice | `.claude/skills/lean-inspector`, `.claude/skills/lean-prover` | Replace one directory link with documented per-skill links. | L1 / E0 |
| `.codex/config.toml` | split | same path | Keep the pinned project MCP; machine permissions move to a previewed local profile. | L1 / E0 |
| `.codex/rules/host-semaphore.rules` | retire | — | Private absolute-helper approval is replaced by the Creme capability CLI. | L0 / E0 |
| `.codex/rules/host-telemetry.rules` | retire | — | Private absolute-helper approval is replaced by the Creme capability CLI. | L0 / E0 |
| `.codex/rules/reclaim-lean.rules` | retire | — | Private absolute-helper approval is replaced by the Creme capability CLI. | L0 / E0 |
| `.github/workflows/ci.yml` | split | `.github/workflows/ci.yml`, `scripts/check.sh` | Reauthor CI around Creme's own cross-platform static gate. | L1 / E0 |
| `.gitignore` | split | `.gitignore` | Carry relevant scratch exclusions and add Creme host-local state. | L1 / E0 |
| `.mcp.json` | split | `.mcp.json` | Keep the pinned Claude project server with explicit stdio/public-safe descriptions. | L1 / E0 |
| `AGENTS.md` | split | `AGENTS.md`, `docs/guides/execution.md` | Extract generic workflow; repository doctrine and concrete state remain with their owners. | L1 / E0 |
| `CLAUDE.md` | compatibility notice | `CLAUDE.md` | Preserve the one-line `@AGENTS.md` import; trailing-byte difference prevents an exact-copy claim. | L1 / E0 |
| `README.md` | split | `AGENTS.md`, `README.md`, `docs/client-discovery.md`, `docs/migration.md` | Preserve launch/setup concepts, not Elanc branding or private layout prose. | L1 / E0 |
| `docs/migration.md` | split | `AGENTS.md`, `docs/client-discovery.md`, `acceptance/client-discovery.md`, `docs/migration.md`, two client-surface tests | Preserve permission-is-not-discovery with fresh public evidence. | L1 / E0 |
| `docs/portability-acceptance-2026-07-24.md` | remain | — | Dated private acceptance stays historical and non-runtime. | L0 / E1 |
| `docs/portability-plan.md` | remain | — | Superseded plan stays historical; current capability truth comes from Creme. | L0 / E1 |
| `docs/setup.md` | split | instructions/README, client/host/migration docs, `creme/cli.py`, `scripts/creme` | Convert setup into a portable, preview-first flow without implicit global mutation. | L1 / E0 |
| `scripts/codex-host-semaphore` | split | semaphore/CLI modules, capability/execution docs, semaphore tests | Reimplement reconciled lease/manual-lock semantics behind the capability boundary. | L1 / E0 |
| `scripts/codex-host-telemetry` | split | Darwin/Linux adapters, CLI, capability docs, adapter tests | Dispatch by OS and provide honest Linux behavior. | L1 / E0 |
| `scripts/codex-reclaim-lean` | split | reclaim/Darwin/CLI modules, capability docs, reclaim test | Preserve current ownership proof and frozen-plan safeguards, not the stale installed copy. | L1 / E0 |
| `scripts/elanc_doctor.py` | split | doctor/profile/Darwin/CLI modules, host docs, adapter/doctor/profile tests | Generalize launch, sibling, adapter, and profile diagnostics. | L1 / E0 |
| `scripts/tests/test_elanc_doctor.py` | split | doctor/profile modules and tests | Reuse behavioral cases; Elanc-name/layout assertions are not copied. | L1 / E2 |
| `scripts/tests/test_host_semaphore.py` | split | semaphore module and tests | Reuse current lease/manual-lock contract; package-level tests are reauthored. | L1 / E2 |
| `scripts/versions.json` | split | same path | Retain public pins/platform facts; remove private repository assumptions. | L1 / E0 |

## Plans

| Source path | Class | Creme destination(s) | Decision and evidence | License / evidence |
| --- | --- | --- | --- | --- |
| `creme-agent-infrastructure-goal.md` | split | instructions/README, client surfaces, architecture/capability/host/migration docs, host package, CI/tests | Implement reviewed product boundaries without publishing the private goal or depending on Plans at runtime. | L1 / E1 |
| `creme-memory-extraction.md` | split | `AGENTS.md`, capability/execution/host docs, host-profile files, adapter/profile modules | Incorporate reviewed Tier A/B content only; opaque client memory and state do not migrate. | L1 / E1 |
| `guide/goal.md` | split | `AGENTS.md`, `docs/guides/{goal,execution}.md` | Extract goal identity/readiness/evidence rules; concrete goals stay in the goal store. | L1 / E1 |
| `guide/lead.md` | split | `AGENTS.md`, `docs/guides/execution.md`, semaphore module/tests | Extract accountable execution and host-resource discipline; concrete lead state stays private. | L1 / E1 |
| `guide/lean-edit-loops.md` | split | `AGENTS.md`, `docs/guides/lean-edit-loops.md`, prover skill and selected tools | Extract the verdict-first method; dated measurements remain provenance. | L1 / E1 |
| `evidence/elab-cure-4-edit-loop-experiments.md` | remain | — | Supports the revised method; dated measurements are not runtime authority. | L0 / E1 |
| `evidence/elab-cure-3-ceiling-debt/disposition-ledger.json` | remain | — | Supports fail-closed ceiling lessons; private evidence is not copied. | L0 / E1 |
| `evidence/elab-cure-3-ceiling-debt/g7-admission-policy.md` | remain | — | Project gate policy remains with its owning repository. | L0 / E1 |
| `evidence/lean-edit-loop/fabrication.md` | remain | — | Supports fidelity safeguards; no public runtime dependency. | L0 / E1 |
| `evidence/lean-edit-loop/divergence-mechanism-source.md` | remain | — | Supports observer-divergence safeguards; no public runtime dependency. | L0 / E1 |

## Destination completeness

The manifest lists each extracted destination once. It includes every current
Creme destination that contains reviewed Elanc/Plans content: client configs,
skills and tools, root instructions/shims, client-discovery evidence, the host
profile and capability implementation, relevant entry points, and current
tests. Thin package initializers and wholly new glue are not provenance claims.

The exact-copy set is intentionally small: four Python tools (`check-drift.py`,
`leanlex.py`, `lsp-probe.py`, and `mk-prefix.py`). All other carried content is
marked derived or compatibility-only, even where the semantic change is small.

## Cutover consequences

Plans remote `main`, Jaune, Blanc, and Elanc now contain the reviewed authority
transition or compatibility surfaces. Those changes do not copy Plans goals or
historical evidence into Creme. The shared local Plans checkout was
intentionally left on its older dirty commit to preserve unrelated work; until
its goal-owned compatibility paths are reconciled safely, it must not be
treated as the operational control plane. Historical evidence stays untouched.
