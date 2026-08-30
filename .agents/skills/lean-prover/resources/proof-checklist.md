# Lean 4 tactic guardrails

Use these generic guardrails when selecting or reviewing a proof strategy:

- Prefer structured tactics such as `rcases`, `obtain`, and `gcongr` over an
  opaque string of rewrites when breaking down structures.
- Prefer a readable, robust proof over the shortest tactic string.
- If automation may close the goal and at least two focused candidates remain
  genuinely unresolved, compare only those candidates with
  `lean_multi_attempt`. If one closure is known, apply and verify it directly.
- Do not repeat a tactic that leaves the goal unchanged; inspect the missing
  premise or change strategy.
- Do not invent lemma names. Verify declarations locally before using them.
- A failed tactic alternative still incurs elaboration work. Prefer explicit,
  goal-directed dispatch when alternatives would repeatedly unify against a
  large term.
- Profile before attributing a slowdown. Never raise a resource ceiling merely
  because it has not fired, and do not infer interactivity from line count.

Proof-performance thresholds, known definitional-equality hazards, profiler
interpretation, and module-partition evidence depend on a repository and its
current toolchain. In Blanc, read the current proof-performance sections of the
sibling repository's `README.md` and `docs/PROOF_RECIPES.md`; do not substitute
dated measurements copied into a generic skill.
