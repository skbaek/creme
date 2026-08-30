#!/usr/bin/env python3
"""Stratified random draw of (module, declaration) targets for the quadrant sweep.

Strata (fixed before sampling, per validation-design.md):
  * module weight tier -- light / mid / heavy, from the committed
    ``scripts/baseline-elab.txt``.  Tier boundaries are the TERCILES of the
    committed row set by default, computed mechanically and recorded in the
    output, so the draw is reproducible from the baseline file alone.
    ``--tier-bounds LO,HI`` pins them explicitly instead.
  * suffix fraction bucket -- ``[0,0.1)``, ``[0.1,0.5)``, ``[0.5,0.9)``,
    ``[0.9,1]`` -- the share of the module's top-level declarations that FOLLOW
    the target.

12 strata.  Within each, a seeded RNG draws without replacement; the seed is
recorded so the campaign is reproducible from it.

Exclusions are MECHANICAL and pre-registered only -- never "this one looks
awkward":
  1. the proof must be a ``by`` block whose ``by`` ends a line, with an
     insertable first tactic line and a block of at least 3 lines.  This is
     decided by ``quadrant-bench.py``'s OWN ``locate_edit_point``, imported
     here rather than reimplemented, so a target this tool admits is one the
     bench can actually run;
  2. the target must not sit under a ``set_option`` ceiling wrapper (a separate
     experiment);
  3. the seven fit-set targets are removed, because grading a rule on the
     targets its constants were fitted to is a fit, not a validation.
A fabrication/fidelity failure is NOT an exclusion here: it is a rule input
discovered at measurement time, recorded by the bench as a refusal.

Every excluded declaration is counted by reason, so the eligible population is
auditable rather than asserted.

Usage:
  sample-targets.py --workdir DIR [--baseline scripts/baseline-elab.txt]
                    --seed 20260826 [-n 24] [--out draw.json]
                    [--exclude-fit-set/--no-exclude-fit-set]
                    [--exclude "Blanc/X.lean:Decl" ...]
                    [--tier-bounds 2.5,15.0] [--census census.json]

Exit codes: 0 draw emitted, 2 usage error.
"""

import argparse
import importlib.util
import json
import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mkprefix = _load("mkprefix", "mk-prefix.py")
qbench = _load("qbench", "quadrant-bench.py")

BUCKETS = [("b0_0.1", 0.0, 0.1), ("b0.1_0.5", 0.1, 0.5),
           ("b0.5_0.9", 0.5, 0.9), ("b0.9_1", 0.9, 1.0001)]

# The seven fit targets (evidence/lean-edit-loop/targets.md).
FIT_SET = [
    ("Blanc/ExecutionSettlement.lean", "Frame.raw_commits_of_settlementCommits"),
    ("Blanc/ExecutionSettlement.lean", "Exec.descendantFrames_runOk_create_codeDepositRollback"),
    ("Blanc/ExecutionSettlement.lean", "Frame.settlementCommits_ofCall_of_raw_commits"),
    ("Blanc/LidoCircuitBreakerEnumeration.lean", "RegistryWitness.enumeration_word_arithmetic"),
    ("Blanc/LidoCircuitBreakerEnumeration.lean", "pauserSet_target_zero_no_success"),
    ("Blanc/LidoCircuitBreakerAttainment.lean", "freshWorld_previousPauserZero"),
    ("Blanc/LidoCircuitBreakerAttainment.lean", "replWorld_countDecrement"),
]


def read_baseline(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) == 3 and p[0] == "OK":
            rows.append((p[2], float(p[1])))
    return rows


def bucket_of(frac):
    for name, lo, hi in BUCKETS:
        if lo <= frac < hi:
            return name
    return BUCKETS[-1][0]


def census_module(workdir, rel, verbose_reasons):
    """Every eligible declaration in one module, plus exclusion counts."""
    path = os.path.join(workdir, rel)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    lines, infos = mkprefix.classify_lines(text)
    total_decls, decl_idx = qbench.count_decls(infos)
    eligible = []
    excluded = {}
    for i in decl_idx:
        kw, name = mkprefix.declared_name(infos[i]["raw"])
        if not name:
            excluded["anonymous (example)"] = excluded.get("anonymous (example)", 0) + 1
            continue
        last_idx, _ = mkprefix.target_extent(infos, i)
        # exclusion 2: set_option ceiling wrapper
        wrapped = False
        for j in range(mkprefix.leading_modifier_start(infos, i), i):
            if infos[j]["kind"] == "command" and infos[j]["token"] == "set_option":
                wrapped = True
        if wrapped:
            excluded["set_option ceiling wrapper"] = \
                excluded.get("set_option ceiling wrapper", 0) + 1
            continue
        # exclusion 1: must be an editable `by` block
        loc, why = qbench.locate_edit_point(lines, i, last_idx)
        if loc is None:
            key = why if verbose_reasons else why.split(";")[0]
            excluded[key] = excluded.get(key, 0) + 1
            continue
        # qualified name, for unambiguous targeting
        stack = mkprefix.namespace_stack_at(infos, i)
        ns = [nm for kind, nm in stack if kind == "namespace" and nm]
        qualified = ".".join(ns + [name]) if ns else name
        after = sum(1 for j in decl_idx if j > i)
        eligible.append({
            "module": rel,
            "decl": name,
            "qualified_name": qualified,
            "keyword": kw,
            "decl_line": i + 1,
            "last_source_line": last_idx + 1,
            "edit_line": loc[0],
            "edit_indent": loc[1],
            "decls_total": total_decls,
            "decls_before": sum(1 for j in decl_idx if j < i),
            "decls_after": after,
            "suffix_fraction": round(after / total_decls, 5) if total_decls else 0.0,
            "lines_total": len(lines),
        })
    return eligible, excluded, total_decls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--baseline", default="scripts/baseline-elab.txt")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("-n", type=int, default=24)
    ap.add_argument("--out")
    ap.add_argument("--census", help="write the full eligible population here")
    ap.add_argument("--tier-bounds", default=None,
                    help="LO,HI seconds; default = terciles of the baseline")
    ap.add_argument("--exclude", action="append", default=[],
                    help="'module:decl' to remove; repeatable")
    ap.add_argument("--no-exclude-fit-set", action="store_true")
    ap.add_argument("--verbose-reasons", action="store_true")
    ap.add_argument("--max-baseline", type=float, default=None,
                    help="drop modules whose baseline elaboration exceeds this "
                         "many seconds (pre-registered population cap)")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    rows = read_baseline(os.path.join(workdir, args.baseline))
    if not rows:
        print("sample-targets: no OK rows in baseline", file=sys.stderr)
        return 2

    times = sorted(t for _, t in rows)
    if args.tier_bounds:
        lo, hi = [float(x) for x in args.tier_bounds.split(",")]
    else:
        lo = times[len(times) // 3]
        hi = times[2 * len(times) // 3]

    def tier_of(t):
        return "light" if t < lo else ("mid" if t < hi else "heavy")

    excl = set(FIT_SET) if not args.no_exclude_fit_set else set()
    for e in args.exclude:
        m, _, d = e.partition(":")
        excl.add((m, d))

    t0 = time.perf_counter()
    population = []
    excluded_counts = {}
    missing = []
    capped_modules = []
    for rel, secs in rows:
        p = os.path.join(workdir, rel)
        if not os.path.exists(p):
            missing.append(rel)
            continue
        if args.max_baseline is not None and secs > args.max_baseline:
            excluded_counts["module over --max-baseline"] = \
                excluded_counts.get("module over --max-baseline", 0) + 1
            capped_modules.append({"module": rel, "baseline_s": secs})
            continue
        eligible, exc, _ = census_module(workdir, rel, args.verbose_reasons)
        for k, v in exc.items():
            excluded_counts[k] = excluded_counts.get(k, 0) + v
        for e in eligible:
            if (rel, e["decl"]) in excl or (rel, e["qualified_name"]) in excl:
                excluded_counts["fit-set / caller-excluded"] = \
                    excluded_counts.get("fit-set / caller-excluded", 0) + 1
                continue
            e["baseline_s"] = secs
            e["tier"] = tier_of(secs)
            e["bucket"] = bucket_of(e["suffix_fraction"])
            e["stratum"] = e["tier"] + "|" + e["bucket"]
            # Cost estimate, MEASURED rather than assumed.  validation-design
            # guessed "7-8x the module's whole-file time"; a real k=3
            # four-quadrant run with the default full fidelity check on
            # Blanc/LidoCircuitBreakerEnumeration.lean (baseline 6.776 s) took
            # 83.4 s wall = 12.3x, and there is a fixed floor of roughly 10 s
            # (server spawn, didOpen debounce, telemetry, interpreter start)
            # that dominates on light modules.  Both terms are recorded.
            e["est_sweep_cost_s"] = round(10.0 + 12.3 * secs, 1)
            e["est_cost_model"] = "10 s fixed + 12.3 x baseline (measured, k=3, --fidelity full)"
            population.append(e)
    census_s = time.perf_counter() - t0

    strata = {}
    for e in population:
        strata.setdefault(e["stratum"], []).append(e)

    # Deterministic allocation: as even as possible over NON-EMPTY strata, in a
    # fixed order, with the shortfall of a small stratum redistributed to the
    # next non-exhausted one.  Recorded rather than left implicit.
    order = ["%s|%s" % (t, b[0]) for t in ("light", "mid", "heavy")
             for b in BUCKETS]
    nonempty = [s for s in order if strata.get(s)]
    rng = random.Random(args.seed)
    quota = {s: 0 for s in nonempty}
    for i in range(args.n):
        quota[nonempty[i % len(nonempty)]] += 1

    draw = []
    shortfall = 0
    for s in nonempty:
        pool = sorted(strata[s], key=lambda e: (e["module"], e["decl_line"]))
        want = quota[s]
        take = min(want, len(pool))
        picked = rng.sample(pool, take)
        shortfall += want - take
        for e in picked:
            e = dict(e)
            e["drawn_from_stratum"] = s
            draw.append(e)
    # redistribute shortfall over strata that still have material
    if shortfall:
        remaining = []
        chosen = {(e["module"], e["decl"]) for e in draw}
        for s in nonempty:
            for e in strata[s]:
                if (e["module"], e["decl"]) not in chosen:
                    remaining.append(e)
        remaining.sort(key=lambda e: (e["stratum"], e["module"], e["decl_line"]))
        extra = rng.sample(remaining, min(shortfall, len(remaining)))
        for e in extra:
            e = dict(e)
            e["drawn_from_stratum"] = e["stratum"] + " (redistributed)"
            draw.append(e)

    out = {
        "tool": "sample-targets.py",
        "schema": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workdir": workdir,
        "baseline": args.baseline,
        "seed": args.seed,
        "requested_n": args.n,
        "max_baseline_s": args.max_baseline,
        "capped_modules": capped_modules,
        "tier_bounds_s": {"light_below": lo, "heavy_at_or_above": hi,
                          "source": ("explicit" if args.tier_bounds
                                     else "terciles of committed baseline")},
        "buckets": [[b[0], b[1], b[2]] for b in BUCKETS],
        "fit_set_excluded": (not args.no_exclude_fit_set),
        "caller_exclusions": args.exclude,
        "modules_in_baseline": len(rows),
        "modules_missing_from_worktree": missing,
        "eligible_population": len(population),
        "excluded_counts": excluded_counts,
        "census_seconds": round(census_s, 2),
        "stratum_sizes": {s: len(v) for s, v in sorted(strata.items())},
        "quota": quota,
        "shortfall_redistributed": shortfall,
        "draw_size": len(draw),
        "estimated_sweep_cost_s": round(sum(e["est_sweep_cost_s"] for e in draw), 1),
        "estimated_cost_note": ("10 s fixed + 12.3 x baseline per target, from a "
                                "measured k=3 --fidelity full run; the design's "
                                "7-8x figure understates it"),
        "max_single_target_cost_s": (max((e["est_sweep_cost_s"] for e in draw), default=0)),
        "draw": draw,
    }
    text = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("sample-targets: wrote %s (%d targets, est %.0f s of elaboration)"
              % (args.out, len(draw), out["estimated_sweep_cost_s"]),
              file=sys.stderr)
    else:
        print(text)
    if args.census:
        with open(args.census, "w", encoding="utf-8") as f:
            json.dump({"population": population}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
