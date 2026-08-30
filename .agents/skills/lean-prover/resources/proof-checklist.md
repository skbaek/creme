# Lean 4 Tactic Guardrails

Use these guardrails when selecting or reviewing a proof strategy:

- Prefer structured tactics (`rcases`, `obtain`, `gcongr`) over chaotic, unmaintainable strings of rewrites when breaking down structures.
- Prefer a readable, robust proof over the shortest tactic string.
- If automation may close the goal and at least two focused candidates remain
  genuinely unresolved, compare only those candidates with
  `lean_multi_attempt`. If one closure is already known, apply it directly and
  verify the edit. Do not pad the snippets list: every submitted candidate is
  evaluated, even after an earlier one succeeds.
- Do not repeat a tactic that leaves the goal unchanged; change strategy or
  inspect the missing premise.
- Do not invent lemma names. Verify declarations with `lean_local_search` or
  another Lean search tool before using them.

## Defeq blowup guardrails (measured, 2026-08-17)

A definition whose right-hand side uses its own state/accumulator argument
more than once (a "wide-recursion state constructor") makes k-layer nesting
unfold geometrically. The hazard on such a tower is any step that leaves a
definitional-equality obligation **spanning a layer the tactic did not name**:

- Implicit defeq across layers: bare `exact`/`assumption`/`change`/`show`/
  `rfl` where the goal and the supplied type differ by tower unfolding.
- A **partial** `simp only`/`dsimp only` — unfolding some layers while
  leaving an inner one (often a `let`-bound intermediate state) opaque, so
  the closing `exact` must cross it by defeq.
- Instantiating a lemma's abstract state variable with a concrete tower and
  letting unification normalize it.

**The number of definitions in a simp set is not the signal; completeness
relative to the tower is.** Measured: a site naming all seven layers and
closing with `exact` compiles in ~3 s, while deleting one intermediate name
from that same set took the enclosing module from 161 s to still running past
400 s. Do not "simplify" a working simp set by dropping names from it.

Safe discipline, preferred first: state one-layer projection lemmas over an
**abstract** base (they are `rfl`-provable precisely because one layer over a
variable stays small) and apply them by `rw` after zeta-unfolding only the
local `let`. `rw` chains compose additively; defeq composes multiplicatively;
and this route does not depend on remembering every layer name. A complete
unfold naming every layer also works, but is fragile — adding a layer later
silently makes the set partial again. "`rfl`-provable" does not mean "cheap to
use by defeq".

Note that inside simp's defeq discharging the work is not heartbeat-metered,
and the kernel ignores `maxHeartbeats` entirely, so a generous budget that has
never fired is not evidence of health.

If a file's whole-file diagnostics time out (a `success: false` result with
empty items is a wait timeout, not a verdict), verify by compiling an
importable prefix olean plus per-segment probe modules with wall-clock caps,
truncating inside a slow theorem with `sorry` to isolate the guilty tactic.

To catch this class before it becomes a hang, profile the module. Ask which
declaration first: `lake env lean -Dtrace.profiler=true
-Dtrace.profiler.threshold=2000 <file>` prints `[Elab.async]` lines naming each
declaration and its total time, ranking every proof in one run. Prefer it to
`-Dprofiler=true`, whose flat tactic list carries no attribution. Then read the
per-tactic view by name: `exact`, `assumption`, `rfl`, `apply` and `change` do
no search, so a multi-second entry naming one of them is defeq work — the
subcritical form of the trap, costing tens of seconds per site instead of
hanging. `simp`, `omega`, `decide` and `congr` are expected to take real time.
A repeated identical cluster of timings is one defect with several call sites.
Cost tracks neither line count nor declaration count.

**A failed alternative is not free.** `first | t₁ | t₂ | …`, `try` and
`all_goals` charge full elaboration for every alternative *attempted*, not just
the one that succeeds — harmless when failure is cheap, ruinous when failing
needs an expensive unification. Measured: four goals closed by `all_goals first
| exact lemma (n := 0) rfl … | exact lemma (n := 544) h₁ … | …` cost 46.4 s,
because each goal unified `N.size = n` against a nested write tower for every
earlier `n` and discarded it; dispatching by goal tag in emission order
(`case h_ext => exact lemma (n := 0) …`, repeated — same-tagged goals are
consumed in order) gave the identical proof in 5.1 s. When a `first`'s
alternatives differ only in an implicit argument that must unify against a
large term, name the goal instead of letting the combinator guess.

Do not infer proof interactivity from source-line count. When considering a
module split, compute the candidate partition from the declaration dependency
graph and measure the same practical edit before and after; do not claim a
performance benefit merely because a stable prefix moved behind an `.olean`.
In Blanc, follow the current *Proof-module size and partition* evidence in its
`README.md` rather than retaining a historical line-count threshold.
