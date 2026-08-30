#!/usr/bin/env python3
"""Decide whether a fabricated prefix file is FAITHFUL to its real module.

Two legs:

  1. Diagnostics agreement.  ``lake env lean`` is run on the real file and on
     the prefix file; the diagnostic sets restricted to the PRESERVED line
     range (line <= the target's last source line) must be identical in line,
     column, severity and message text.  Diagnostics after the preserved region
     in the real file are excluded by construction.

  2. Anchor proof-state agreement.  ``lake env lean`` cannot report a goal, so
     the state is obtained at source level: a ``trace_state`` tactic is
     inserted at the SAME anchor line and indentation in both files, both are
     elaborated, and the resulting trace text must be byte-identical.  The
     anchor is not guessed: mk-prefix emits ranked candidates and the first one
     that elaborates cleanly IN THE REAL FILE is used.  Probe files are
     temporary and are removed afterwards.

Exit codes:
  0  faithful
  2  usage / target error
  3  UNFAITHFUL -- diagnostics disagree in the preserved range
  4  UNFAITHFUL -- anchor proof state disagrees
  5  INDETERMINATE -- lean did not run, or no anchor could be established
  6  DEGRADED -- target is faithful, but the prefix file is broken OUTSIDE the
     preserved range, so `lake env lean` rejects a file the real module accepts.
     This is not a fidelity disagreement under the two specified legs; it is
     reported separately because a prefix Lean rejects is useless as an edit
     loop, and exiting 0 on it would be the very "cannot tell success from
     failure" trap the two legs are meant to avoid.  --lenient restores
     spec-only (leg 1 + leg 2) exit semantics.
"""

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "mkprefix", os.path.join(_HERE, "mk-prefix.py"))
mkprefix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mkprefix)

SEVERITIES = ("error", "warning", "information")


# --------------------------------------------------------------------------
# Running Lean


def run_lean(workdir, rel_path):
    proc = subprocess.run(
        ["lake", "env", "lean", rel_path],
        cwd=workdir, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_output(stdout, rel_path):
    """Split lean's stdout into (trace_text, diagnostics).

    ``trace_text`` is everything printed before the first diagnostic header --
    where header-less ``information`` output such as ``trace_state`` lands.
    A diagnostic's message runs until the next header.
    """
    # Lean 4 emits BOTH bare severities ("error:") and severities carrying a
    # diagnostic code ("error(lean.unknownIdentifier):").  A filter that only
    # matches the bare form silently drops a whole class of real errors, which
    # would make leg 1 blind.  The code is kept as part of the diagnostic's
    # identity so a change of code counts as a disagreement.
    header = re.compile(
        r"^" + re.escape(rel_path)
        + r":(\d+):(\d+): (" + "|".join(SEVERITIES)
        + r")(\([A-Za-z0-9_.\-]*\))?: ?(.*)$")
    diags = []
    preamble = []
    cur = None
    for line in stdout.split("\n"):
        m = header.match(line)
        if m:
            if cur is not None:
                diags.append(cur)
            cur = {
                "line": int(m.group(1)),
                "col": int(m.group(2)),
                "sev": m.group(3) + (m.group(4) or ""),
                "msg": [m.group(5)],
            }
            continue
        if cur is None:
            preamble.append(line)
        else:
            cur["msg"].append(line)
    if cur is not None:
        diags.append(cur)
    for d in diags:
        d["msg"] = "\n".join(d["msg"]).rstrip()
    return "\n".join(preamble).strip("\n"), diags


def classify_run(rc, stdout, stderr, diags, trace_text):
    """Alternation over BOTH success and failure signatures.

    A filter that only matches success signatures cannot tell success from a
    command that never ran, so every outcome is named explicitly.
    """
    errors = [d for d in diags if d["sev"].startswith("error")]
    if rc != 0 and not diags and not trace_text.strip():
        return "DID-NOT-RUN"
    if errors:
        return "ELABORATION-ERRORS"
    if rc != 0:
        # Lean failed but this parser did not account for the failure.  Fail
        # closed: never let an unexplained nonzero exit read as success.
        return "UNPARSED-FAILURE"
    return "OK"


def fmt_diag(d):
    first = d["msg"].split("\n")[0]
    extra = "" if "\n" not in d["msg"] else " [+more]"
    return f"{d['line']}:{d['col']}: {d['sev']}: {first}{extra}"


# --------------------------------------------------------------------------


def insert_probe(text, line_1indexed, indent):
    lines = text.split("\n")
    lines.insert(line_1indexed - 1, " " * indent + "trace_state")
    return "\n".join(lines)


def shift_diags(diags, at_line):
    out = []
    for d in diags:
        e = dict(d)
        if e["line"] >= at_line:
            e["line"] += 1
        out.append(e)
    return out


def key(d):
    return (d["line"], d["col"], d["sev"], d["msg"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True,
                    help="Lean worktree root; never inferred from a developer path")
    ap.add_argument("--real", required=True,
                    help="path to the real module, relative to --workdir")
    ap.add_argument("--prefix", required=True,
                    help="path to the fabricated prefix, relative to --workdir")
    ap.add_argument("--decl", required=True)
    ap.add_argument("--max-anchor-tries", type=int, default=6)
    ap.add_argument("--no-anchor", action="store_true")
    ap.add_argument("--keep-probes", action="store_true")
    ap.add_argument("--lenient", action="store_true",
                    help="report leg 3 but do not let it change the exit code")
    args = ap.parse_args()

    wd = args.workdir
    real_rel, pre_rel = args.real, args.prefix
    real_abs = os.path.join(wd, real_rel)
    pre_abs = os.path.join(wd, pre_rel)

    real_text = open(real_abs, encoding="utf-8").read()
    try:
        _out, meta = mkprefix.build(real_text, args.decl, "truncate")
    except mkprefix.TargetError as e:
        print(f"check-fidelity: {e}", file=sys.stderr)
        return 2
    last = meta["last_source_line"]

    print(f"== target {meta['qualified_name']} "
          f"({meta['keyword']}) in {real_rel}")
    print(f"== preserved range: lines 1..{last}")

    # ---- Leg 1: diagnostics agreement -----------------------------------
    rc_r, so_r, se_r = run_lean(wd, real_rel)
    tr_r, dg_r = parse_output(so_r, real_rel)
    cls_r = classify_run(rc_r, so_r, se_r, dg_r, tr_r)

    rc_p, so_p, se_p = run_lean(wd, pre_rel)
    tr_p, dg_p = parse_output(so_p, pre_rel)
    cls_p = classify_run(rc_p, so_p, se_p, dg_p, tr_p)

    print(f"== leg1 real   : exit={rc_r} class={cls_r} "
          f"diags={len(dg_r)} (in-range {len([d for d in dg_r if d['line'] <= last])})")
    print(f"== leg1 prefix : exit={rc_p} class={cls_p} "
          f"diags={len(dg_p)} (in-range {len([d for d in dg_p if d['line'] <= last])})")

    if cls_r in ("DID-NOT-RUN", "UNPARSED-FAILURE") or \
            cls_p in ("DID-NOT-RUN", "UNPARSED-FAILURE"):
        print("INDETERMINATE: lake env lean failed in a way this parser did "
              "not account for")
        print("--- real stderr ---\n" + se_r[:800])
        print("--- prefix stderr ---\n" + se_p[:800])
        return 5

    in_r = sorted([d for d in dg_r if d["line"] <= last], key=key)
    in_p = sorted([d for d in dg_p if d["line"] <= last], key=key)
    only_r = [d for d in in_r if key(d) not in {key(x) for x in in_p}]
    only_p = [d for d in in_p if key(d) not in {key(x) for x in in_r}]

    if only_r or only_p:
        print("LEG1 DISAGREEMENT")
        for d in only_r:
            print(f"  only in REAL   : {fmt_diag(d)}")
        for d in only_p:
            print(f"  only in PREFIX : {fmt_diag(d)}")
        print("VERDICT: UNFAITHFUL")
        return 3
    print("== leg1 AGREE")

    # ---- Leg 3: collateral damage outside the preserved range -----------
    out_r = [d for d in dg_r if d["line"] > last and d["sev"].startswith("error")]
    out_p = [d for d in dg_p if d["line"] > last and d["sev"].startswith("error")]
    degraded = (cls_r == "OK" and cls_p != "OK") or len(out_p) > len(out_r)
    print(f"== leg3 collateral: real out-of-range errors={len(out_r)} "
          f"prefix out-of-range errors={len(out_p)} "
          f"-> {'DEGRADED' if degraded else 'CLEAN'}")
    if degraded:
        for d in out_p[:4]:
            print(f"  prefix-only (outside preserved range): {fmt_diag(d)}")

    if args.no_anchor:
        print("VERDICT: FAITHFUL (leg1 only; leg2 skipped by request)")
        if degraded and not args.lenient:
            print("VERDICT: FAITHFUL-BUT-DEGRADED")
            return 6
        return 0

    # ---- Leg 2: anchor proof-state agreement ----------------------------
    cands = meta["anchor_candidates"][: args.max_anchor_tries]
    if not cands:
        print("INDETERMINATE: no anchor candidate (target has no `by` block?)")
        return 5

    probe_dir = os.path.join(wd, "_probe")
    os.makedirs(probe_dir, exist_ok=True)
    pre_text = open(pre_abs, encoding="utf-8").read()
    chosen = None
    try:
        for cand in cands:
            L, ind = cand["line"], cand["indent"]
            rp = os.path.join("_probe", "real_probe.lean")
            open(os.path.join(wd, rp), "w", encoding="utf-8").write(
                insert_probe(real_text, L, ind))
            rc, so, se = run_lean(wd, rp)
            tr, dg = parse_output(so, rp)
            cls = classify_run(rc, so, se, dg, tr)
            base_shifted = sorted(shift_diags(dg_r, L), key=key)
            same_diags = ({key(d) for d in dg} == {key(d) for d in base_shifted})
            usable = (cls == "OK" and same_diags
                      and ("⊢" in tr or "no goals" in tr))
            # ``diags`` is printed because leg 2 reads the trace from the
            # header-less PREAMBLE of stdout.  With zero diagnostics the
            # preamble is unambiguously the whole trace output; with
            # diagnostics present it relies on lean printing header-less
            # trace output before header-carrying diagnostics.
            print(f"== leg2 anchor try {L}:{cand['col']} -> exit={rc} "
                  f"class={cls} same_diags={same_diags} diags={len(dg)} "
                  f"trace_chars={len(tr)} usable={usable}")
            if usable:
                chosen = (cand, tr, rc)
                break
        if chosen is None:
            print("INDETERMINATE: no anchor candidate elaborated cleanly "
                  "in the real file; leg2 not established")
            return 5

        cand, tr_real, rc_real = chosen
        L, ind = cand["line"], cand["indent"]
        pp = os.path.join("_probe", "prefix_probe.lean")
        open(os.path.join(wd, pp), "w", encoding="utf-8").write(
            insert_probe(pre_text, L, ind))
        rc2, so2, se2 = run_lean(wd, pp)
        tr_pre, dg2 = parse_output(so2, pp)
        cls2 = classify_run(rc2, so2, se2, dg2, tr_pre)
        print(f"== leg2 prefix probe: exit={rc2} class={cls2} "
              f"diags={len(dg2)} trace_chars={len(tr_pre)}")

        if cls2 in ("DID-NOT-RUN", "UNPARSED-FAILURE"):
            print("INDETERMINATE: probed prefix produced no parseable output")
            return 5
        if not tr_pre.strip() or not ("⊢" in tr_pre or "no goals" in tr_pre):
            print("LEG2 DISAGREEMENT: probed prefix emitted no proof state")
            for d in dg2[:5]:
                print(f"  prefix diag: {fmt_diag(d)}")
            print("VERDICT: UNFAITHFUL")
            return 4
        if tr_real != tr_pre:
            print(f"LEG2 DISAGREEMENT at anchor {L}:{cand['col']}")
            import difflib
            for ln in list(difflib.unified_diff(
                    tr_real.split("\n"), tr_pre.split("\n"),
                    "REAL", "PREFIX", lineterm=""))[:40]:
                print("  " + ln)
            print("VERDICT: UNFAITHFUL")
            return 4
        print(f"== leg2 AGREE at anchor {L}:{cand['col']} "
              f"({len(tr_real)} chars of proof state, byte-identical)")
    finally:
        if not args.keep_probes:
            shutil.rmtree(os.path.join(wd, "_probe"), ignore_errors=True)

    if degraded and not args.lenient:
        print("VERDICT: FAITHFUL-BUT-DEGRADED "
              "(target faithful; prefix file rejected by lean)")
        return 6
    print("VERDICT: FAITHFUL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
