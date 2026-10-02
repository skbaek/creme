# Shared library first

Before writing a definition, lemma, tactic, or instance that is
generic-shaped (not specific to one contract), search the target
repository's shared library: in Blanc, `docs/COMMON_API.md` (the need-first
map of shared modules) and `docs/PROOF_RECIPES.md` (the recipe lookup), plus
`lean_local_search`. Reuse what exists instead of writing a private copy.

When you prove something generically applicable, put it in a shared module
rather than a contract-local file: in Blanc, register the module as SHARED
in `scripts/check-layering.py` and cite it in `docs/COMMON_API.md`. When you
find a private copy of a shared fact, replace it with the shared one.

Place helpers locally while proving, hoist at the green boundary. While
working toward a result `X` in a file `F`, put each new intermediate
definition or lemma in `F`, just above its first use, instead of editing
widely imported modules mid-loop: the language server sees it at once and
nothing upstream rebuilds. Once `X` is proved, make one closure pass before
the checkpoint commit: hoist the generically applicable helpers to shared
modules as above, delete the ones nothing uses, and leave contract-specific
ones in place. Extending an existing shared definition (for example a new
opcode case in a lifter transfer function) has no local alternative; make
such edits deliberately and few.

The duplication gates only catch byte-identical copies; concept-level
duplicates are yours to avoid. Say in your report what you reused and what
you hoisted.
