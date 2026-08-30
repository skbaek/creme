#!/usr/bin/env python3
"""Decide whether a fabricated prefix file is still ANCHORED to its real module.

The scratch-prefix edit loop validates a candidate against an *environment*:
every line of the real module that precedes the target declaration.  If that
region changes while the loop runs -- another session's commit, a rebase, or
the agent's own upstream edit -- the candidate was validated against an
environment that no longer exists, and transferring it is unsound even though
the scratch file is green.

This tool compares the real module and the prefix file over two zones, whose
boundary is taken from ``mk-prefix.py`` rather than reimplemented:

  ZONE A -- CONTEXT: line 1 .. (target's ``region_start_line`` - 1).
      Everything the target elaborates *in*: imports, ``open``s, ``variable``s,
      ``set_option``s, namespaces, and every earlier declaration.  This must be
      byte-identical.  A difference here is DRIFT: the verdict is void.

  ZONE B -- TARGET: ``region_start_line`` .. ``last_source_line``.
      The declaration under edit, with its docstring/attributes.  In a live
      loop this is *expected* to differ -- that difference is the candidate.
      It is reported, and only enforced under ``--strict``, which is the
      transfer-time check: "does the real file still hold the baseline text I
      am about to replace?"

Zone A is checked against the REAL file's own boundary AND the PREFIX file's
own boundary; if the target moved, or vanished, or became ambiguous in either
file, that is itself drift and is reported as such.

Usage:
  check-drift.py --real <module.lean> --prefix <prefix.lean> --decl <name>
                 [--strict] [--json]

Exit codes:
  0  anchored (zone A identical; under --strict, zone B identical too)
  2  usage / target error (the target could not be located in a file)
  3  DRIFT -- the region is not byte-identical
"""

import argparse
import difflib
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "mkprefix", os.path.join(_HERE, "mk-prefix.py"))
mkprefix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mkprefix)


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def bounds(text, decl, label):
    """(region_start_line, last_source_line), 1-indexed inclusive."""
    lines, infos, (start_idx, kw, name, qualified) = mkprefix.find_target(
        text, decl)
    last_idx, _ = mkprefix.target_extent(infos, start_idx)
    region_start = mkprefix.leading_modifier_start(infos, start_idx)
    return {
        "where": label,
        "region_start_line": region_start + 1,
        "decl_line": start_idx + 1,
        "last_source_line": last_idx + 1,
        "qualified_name": qualified,
        "keyword": kw,
    }


def zone(text, lo, hi):
    """1-indexed inclusive line range as a list of lines."""
    lines = text.split("\n")
    return lines[lo - 1:hi]


def first_diff(a, b, lo):
    """First differing 1-indexed line number, or None."""
    for i in range(max(len(a), len(b))):
        av = a[i] if i < len(a) else None
        bv = b[i] if i < len(b) else None
        if av != bv:
            return lo + i, av, bv
    return None


def report_zone(name, real_lines, pfx_lines, lo, out):
    if real_lines == pfx_lines:
        out[name] = {"identical": True, "lines": len(real_lines)}
        return True
    d = first_diff(real_lines, pfx_lines, lo)
    entry = {
        "identical": False,
        "real_lines": len(real_lines),
        "prefix_lines": len(pfx_lines),
        "first_diff_line": d[0] if d else None,
        "real_text": d[1] if d else None,
        "prefix_text": d[2] if d else None,
        "unified": list(difflib.unified_diff(
            pfx_lines, real_lines, fromfile="prefix", tofile="real",
            lineterm="", n=1))[:40],
    }
    out[name] = entry
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--decl", required=True)
    ap.add_argument("--strict", action="store_true",
                    help="also require the TARGET zone to be byte-identical "
                         "(the transfer-time check)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    real = read(args.real)
    pfx = read(args.prefix)

    try:
        rb = bounds(real, args.decl, "real")
    except mkprefix.TargetError as e:
        print(f"check-drift: DRIFT: target {args.decl!r} is no longer locatable "
              f"in the real file {args.real}: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"check-drift: {args.real}: {e}", file=sys.stderr)
        return 2
    try:
        pb = bounds(pfx, args.decl, "prefix")
    except mkprefix.TargetError as e:
        print(f"check-drift: target {args.decl!r} is not locatable in the "
              f"prefix file {args.prefix}: {e}", file=sys.stderr)
        return 2

    out = {"real": args.real, "prefix": args.prefix, "decl": args.decl,
           "real_bounds": rb, "prefix_bounds": pb, "strict": args.strict}

    ok = True
    moved = rb["region_start_line"] != pb["region_start_line"]
    out["target_moved"] = moved

    # ---- ZONE A: context ------------------------------------------------
    a_hi_real = rb["region_start_line"] - 1
    a_hi_pfx = pb["region_start_line"] - 1
    ra = zone(real, 1, a_hi_real) if a_hi_real >= 1 else []
    pa = zone(pfx, 1, a_hi_pfx) if a_hi_pfx >= 1 else []
    if not report_zone("context", ra, pa, 1, out):
        ok = False

    # ---- ZONE B: target -------------------------------------------------
    rt = zone(real, rb["region_start_line"], rb["last_source_line"])
    pt = zone(pfx, pb["region_start_line"], pb["last_source_line"])
    same_target = report_zone("target", rt, pt, rb["region_start_line"], out)
    if args.strict and not same_target:
        ok = False

    out["verdict"] = "ANCHORED" if ok else "DRIFT"

    if args.json:
        print(json.dumps(out, indent=1))
    else:
        c = out["context"]
        if c["identical"]:
            print(f"check-drift: context zone OK: lines 1..{a_hi_real} of "
                  f"{args.real} are byte-identical to the prefix "
                  f"({c['lines']} lines).")
        else:
            print(f"check-drift: DRIFT in the CONTEXT zone (lines 1.."
                  f"{a_hi_real}), which the candidate was validated against.",
                  file=sys.stderr)
            print(f"  first difference at real line {c['first_diff_line']}:",
                  file=sys.stderr)
            print(f"    real  : {c['real_text']!r}", file=sys.stderr)
            print(f"    prefix: {c['prefix_text']!r}", file=sys.stderr)
            print(f"  real context is {c['real_lines']} lines, prefix context "
                  f"is {c['prefix_lines']} lines", file=sys.stderr)
            for ln in c["unified"]:
                print("  " + ln, file=sys.stderr)
        if moved:
            print(f"check-drift: target region moved: real "
                  f"{rb['region_start_line']}..{rb['last_source_line']} vs "
                  f"prefix {pb['region_start_line']}..{pb['last_source_line']}",
                  file=sys.stderr)
        t = out["target"]
        if t["identical"]:
            print(f"check-drift: target zone identical "
                  f"({rb['qualified_name']}, {t['lines']} lines).")
        else:
            msg = ("DRIFT in the TARGET zone" if args.strict
                   else "target zone differs (expected during a live loop; "
                        "re-run with --strict at transfer time)")
            stream = sys.stderr if args.strict else sys.stdout
            print(f"check-drift: {msg}: first difference at real line "
                  f"{t['first_diff_line']}", file=stream)
            if args.strict:
                print(f"    real  : {t['real_text']!r}", file=sys.stderr)
                print(f"    prefix: {t['prefix_text']!r}", file=sys.stderr)
        print(f"check-drift: {out['verdict']}")

    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
