#!/usr/bin/env python3
"""Four-quadrant edit-loop benchmark for one (module, declaration) pair.

           | in situ (real file)          | scratch prefix
  ---------+------------------------------+-----------------------------
  CLI      | Q1  lake env lean <module>   | Q3  lake env lean <prefix>
  LSP      | Q2  persistent server, real  | Q4  persistent server, prefix

Edit protocol (fixed by validation-design.md; do not "improve" it):
  iteration i (i = 1..k) inserts i copies of the tactic ``skip`` as the FIRST
  tactic line of the target's proof, at the proof's own indentation.  The same
  edit sequence is replayed in all four quadrants.  Insertion is at the START
  of the proof on purpose: Lean's tactic-level incrementality lets an edit near
  the END reuse the elaboration of everything before it, which would flatter
  Q2/Q4 -- the very modes this experiment exists to test.

Applicability is mechanical and pre-registered: the proof must be a ``by`` block
whose ``by`` ends a line, with an insertable first tactic line.  Anything else
returns a record with ``applicable: false`` and a reason.

Fidelity gate: the fabricated prefix is checked with ``check-fidelity.py`` before
any Q3/Q4 number is produced.  A non-faithful prefix means Q3/Q4 are UNAVAILABLE
for that target; the refusal is recorded as a result, never worked around.

Verdict classification for Q1/Q3 reuses ``check-fidelity.py``'s
``parse_output``/``classify_run``, which alternate over BOTH success and failure
signatures and know that Lean emits ``error:`` as well as coded forms like
``error(lean.unknownIdentifier):``.  A filter matching only success signatures
cannot tell a clean pass from a command that never ran.

Usage:
  quadrant-bench.py --workdir DIR --module Blanc/X.lean --decl NAME
                    [-k 3] [--out record.json] [--scratch DIR]
                    [--edit-tactic skip] [--quadrants Q1,Q2,Q3,Q4]
                    [--lsp-order q2q4|q4q2] [--fidelity full|leg1|skip]
                    [--separate-lsp-servers]
                    [--rss-interval 0.15] [--label TEXT]

Exit codes: 0 record produced (including a not-applicable or refusal record),
2 usage/target error.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
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
fidelity = _load("fidelity", "check-fidelity.py")
lspprobe = _load("lspprobe", "lsp-probe.py")
from procmon import ProcMon  # noqa: E402

_CREME_ROOT = os.path.abspath(os.path.join(_HERE, "../../../.."))
TELEMETRY_COMMAND = (sys.executable, "-m", "creme", "telemetry")

# Stripped-line prefixes that make the first proof line un-insertable-before.
_NOT_INSERTABLE = ("|", "<;>", ".", ")", "]", "}", ",", ";", "·")


# --------------------------------------------------------------- helpers ---

def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def host_telemetry(when):
    """Compact host sample.  Taken immediately before/after a measured command
    so an outlier has a sample of the window it came from."""
    rec = {"when": when, "t": time.time()}
    try:
        run = subprocess.run(
            TELEMETRY_COMMAND,
            cwd=_CREME_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(run.stdout)
    except Exception as e:
        rec["error"] = str(e)
        return rec
    if run.returncode != 0 or payload.get("status") != "OK":
        rec["error"] = payload.get("detail", "telemetry unavailable")
        return rec
    data = payload.get("data") or {}
    rec["mem_free_pct"] = data.get("memory_free_percent")
    rec["swap_used_mb"] = data.get("swap_used_mib")
    families = data.get("client_family_rss_kib") or {}
    rec["codex_family_rss_kib"] = families.get("codex")
    rec["lean_rss_kib_total"] = sum(
        row.get("rss_kib", 0) for row in data.get("lean_processes", [])
    )
    return rec


def count_decls(infos):
    n = 0
    idx = []
    for info in infos:
        if info["kind"] != "command" or info["attr"]:
            continue
        kw, name = mkprefix.declared_name(info["raw"])
        if kw and (name or kw == "example"):
            idx.append(info["n"])
            n += 1
    return n, idx


def locate_edit_point(lines, start_idx, last_idx):
    """(line_1indexed, indent) of the FIRST tactic line of the ``by`` block,
    or (None, reason)."""
    by_idx = None
    for i in range(start_idx, last_idx + 1):
        if re.search(r"(^|[\s(\[])by\s*$", lines[i].rstrip()):
            by_idx = i
            break
    if by_idx is None:
        return None, "proof is not a `by` block ending a line"
    if by_idx >= last_idx:
        return None, "`by` is the last line of the declaration"
    first = None
    for i in range(by_idx + 1, last_idx + 1):
        if lines[i].strip() == "":
            continue
        first = i
        break
    if first is None:
        return None, "no tactic line after `by`"
    stripped = lines[first].lstrip()
    for bad in _NOT_INSERTABLE:
        if stripped.startswith(bad):
            return None, ("first tactic line starts with %r; inserting before "
                          "it is not a valid tactic position" % bad)
    if last_idx - by_idx < 2:
        return None, "`by` block is shorter than the pre-registered 3-line floor"
    indent = len(lines[first]) - len(stripped)
    return (first + 1, indent), None


_END_CLAUSES = ("where", "termination_by", "decreasing_by")


def locate_edit_point_end(lines, start_idx, last_idx, base_indent):
    """(line_1indexed, indent) for an insertion as the LAST tactic line of the
    declaration, i.e. immediately after its final preserved source line, at the
    ``by`` block's own base indentation.

    Inserting at the base indentation (rather than the final line's own, which
    may sit inside a focus dot or a `with` alternative) closes any nested block
    and appends a new top-level tactic to the sequence.  ``skip`` is `pure ()`
    and so is legal even when every goal is already closed.

    Refused when the declaration carries a trailing `where` / `termination_by` /
    `decreasing_by` clause, because the last source line is then not inside the
    tactic block at all.
    """
    for i in range(start_idx, last_idx + 1):
        st = lines[i].strip()
        for cl in _END_CLAUSES:
            if st == cl or st.startswith(cl + " ") or st.startswith(cl + "\n"):
                return None, ("declaration carries a trailing `%s` clause; the "
                              "last source line is not a tactic position" % cl)
    return (last_idx + 2, base_indent), None


def apply_edit(text, line_1indexed, indent, tactic, copies):
    lines = text.split("\n")
    ins = [" " * indent + tactic] * copies
    return "\n".join(lines[:line_1indexed - 1] + ins + lines[line_1indexed - 1:])


def run_cli(workdir, rel_path, rss_interval, label):
    """One ``lake env lean <rel_path>`` with peak-RSS sampling and telemetry."""
    before = host_telemetry("before:" + label)
    t0 = time.perf_counter()
    proc = subprocess.Popen(["lake", "env", "lean", rel_path], cwd=workdir,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    mon = ProcMon(proc.pid, rss_interval, label=label).start()
    out, err = proc.communicate()
    rss = mon.stop()
    wall = time.perf_counter() - t0
    after = host_telemetry("after:" + label)
    trace, diags = fidelity.parse_output(out, rel_path)
    verdict = fidelity.classify_run(proc.returncode, out, err, diags, trace)
    errs = [d for d in diags if d["sev"].startswith("error")]
    return {
        "label": label,
        "wall_s": round(wall, 4),
        "returncode": proc.returncode,
        "verdict": verdict,
        "green": verdict == "OK",
        "n_diagnostics": len(diags),
        "n_errors": len(errs),
        "first_error": (fidelity.fmt_diag(errs[0]) if errs else None),
        "peak_tree_rss_kib": rss["peak_tree_rss_kib"],
        "peak_tree_rss_gib": rss["peak_tree_rss_gib"],
        "rss_samples": rss["samples_with_live_tree"],
        "rss_interval_s": rss["interval_s"],
        "stderr_tail": err[-400:] if err else "",
        "telemetry_before": before,
        "telemetry_after": after,
    }


def trim_lsp(rec, label):
    """LSP step record, without the full diagnostic payload."""
    keep = ("elapsed_s", "version", "green", "n_diagnostics", "n_errors",
            "first_error", "n_publishes", "timed_out", "fatal_progress",
            "first_publish_elapsed_s", "first_publish_n_errors",
            "first_publish_green", "naive_first_publish_differs",
            "progress_done_elapsed_s", "publishes_after_progress_done",
            "grace_extra_publishes", "grace_changed_answer",
            "publish_offsets_s", "publish_counts")
    out = {k: rec.get(k) for k in keep}
    out["label"] = label
    out["wall_s"] = round(rec["elapsed_s"], 4)
    return out


# ------------------------------------------------------------------ main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--module", required=True, help="path relative to workdir")
    ap.add_argument("--decl", required=True)
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--out")
    ap.add_argument("--scratch", default=None,
                    help="directory for fabricated files; default <workdir>/_editloop")
    ap.add_argument("--edit-tactic", default="skip")
    ap.add_argument("--edit-position", choices=["start", "end"], default="start",
                    help="insert the edit as the FIRST tactic line of the proof "
                         "(start, the pre-registered conservative default) or as "
                         "the LAST one (end, which lets Lean's tactic-level "
                         "incrementality reuse the whole proof before it)")
    ap.add_argument("--quadrants", default="Q1,Q2,Q3,Q4")
    ap.add_argument("--lsp-order", choices=["q2q4", "q4q2"], default="q2q4")
    ap.add_argument("--fidelity", choices=["full", "leg1", "skip"], default="full")
    ap.add_argument("--rss-interval", type=float, default=0.15)
    ap.add_argument("--baseline", default="scripts/baseline-elab.txt")
    ap.add_argument("--label", default=None)
    ap.add_argument("--lsp-grace", type=float, default=0.0)
    ap.add_argument("--separate-lsp-servers", action="store_true",
                    help="one fresh server per LSP quadrant, so peak "
                         "RSS is attributable; costs a second warm-up")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    real_rel = args.module
    real_path = os.path.join(workdir, real_rel)
    want = set(x.strip().upper() for x in args.quadrants.split(",") if x.strip())
    scratch = args.scratch or os.path.join(workdir, "_editloop")
    os.makedirs(scratch, exist_ok=True)
    # Fabricated prefixes live inside the worktree so `lake serve` resolves
    # them; a self-ignoring scratch dir keeps `git status` clean during a
    # campaign without touching anything tracked.
    gi = os.path.join(scratch, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write("*\n")

    rec = {
        "tool": "quadrant-bench.py",
        "schema": 1,
        "label": args.label,
        "workdir": workdir,
        "module": real_rel,
        "decl": args.decl,
        "k": args.k,
        "edit_tactic": args.edit_tactic,
        "edit_position": ("start-of-proof" if args.edit_position == "start"
                          else "end-of-proof"),
        "lsp_order": args.lsp_order,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lsp_report_delay_ms": 200,
        "lsp_report_delay_note": (
            "server.reportDelayMs defaults to 200 ms; the reporter sleeps for "
            "it before publishing anything, so every Q2/Q4 per-iteration figure "
            "carries a ~0.20 s floor.  A Q4 reading at ~0.21 s is the floor, "
            "not Lean's elaboration cost."),
        "applicable": False,
        "refusals": [],
    }

    with open(real_path, "r", encoding="utf-8") as f:
        original = f.read()
    rec["module_sha"] = sha(original)

    # baseline row
    bl = os.path.join(workdir, args.baseline)
    rec["baseline_module_s"] = None
    if os.path.exists(bl):
        for line in open(bl, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) == 3 and p[2] == real_rel:
                rec["baseline_module_s"] = float(p[1])

    # ---- target location and position ------------------------------------
    try:
        lines, infos, (start_idx, kw, name, qualified) = mkprefix.find_target(
            original, args.decl)
    except mkprefix.TargetError as e:
        rec["reason"] = "target error: %s" % e
        _emit(rec, args.out)
        return 0
    last_idx, next_idx = mkprefix.target_extent(infos, start_idx)
    total_decls, decl_idx = count_decls(infos)
    rec.update({
        "qualified_name": qualified,
        "keyword": kw,
        "decl_line": start_idx + 1,
        "last_source_line": last_idx + 1,
        "lines_total": len(lines),
        "lines_before": start_idx,
        "lines_after": len(lines) - (last_idx + 1),
        "decls_total": total_decls,
        "decls_before": sum(1 for i in decl_idx if i < start_idx),
        "decls_after": sum(1 for i in decl_idx if i > start_idx),
    })
    rec["suffix_fraction"] = (rec["decls_after"] / total_decls) if total_decls else None

    # `set_option` ceiling wrapper is a separate experiment (pre-registered).
    for i in range(mkprefix.leading_modifier_start(infos, start_idx), start_idx):
        if infos[i]["kind"] == "command" and infos[i]["token"] == "set_option":
            rec["reason"] = "target sits under a `set_option` ceiling wrapper"
            _emit(rec, args.out)
            return 0

    loc, why = locate_edit_point(lines, start_idx, last_idx)
    if loc is None:
        rec["reason"] = why
        _emit(rec, args.out)
        return 0
    edit_line, edit_indent = loc
    if args.edit_position == "end":
        eloc, ewhy = locate_edit_point_end(lines, start_idx, last_idx, edit_indent)
        if eloc is None:
            rec["reason"] = ewhy
            _emit(rec, args.out)
            return 0
        rec["edit_line_start_variant"] = edit_line
        edit_line, edit_indent = eloc
    rec["applicable"] = True
    rec["edit_line"] = edit_line
    rec["edit_indent"] = edit_indent

    # ---- fabricate the prefix --------------------------------------------
    prefix_rel = None
    t_fab0 = time.perf_counter()
    prefix_text, meta = mkprefix.build(original, args.decl, "truncate")
    fab_s = time.perf_counter() - t_fab0
    rec["one_time"] = {"fabricate_s": round(fab_s, 4)}
    rec["prefix_structure_problems"] = meta["structure_problems"]
    base = os.path.splitext(os.path.basename(real_rel))[0]
    prefix_name = "EL_%s_%s.lean" % (base, re.sub(r"[^A-Za-z0-9]", "_", args.decl))[:120]
    prefix_path = os.path.join(scratch, prefix_name)
    prefix_rel = os.path.relpath(prefix_path, workdir)
    with open(prefix_path, "w", encoding="utf-8") as f:
        f.write(prefix_text)
    rec["prefix_path"] = prefix_rel
    rec["prefix_lines"] = len(prefix_text.split("\n"))

    # ---- fidelity gate ----------------------------------------------------
    scratch_ok = True
    if args.fidelity == "skip":
        rec["fidelity"] = {"mode": "skip", "exit": None,
                           "note": "NOT CHECKED -- Q3/Q4 numbers are unwarranted"}
        scratch_ok = False
        rec["refusals"].append("fidelity check skipped; Q3/Q4 suppressed")
    else:
        cmd = [sys.executable, os.path.join(_HERE, "check-fidelity.py"),
               "--workdir", workdir, "--real", real_rel,
               "--prefix", prefix_rel, "--decl", args.decl]
        if args.fidelity == "leg1":
            cmd.append("--no-anchor")
        t0 = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True, text=True)
        rec["fidelity"] = {
            "mode": args.fidelity,
            "exit": p.returncode,
            "seconds": round(time.perf_counter() - t0, 3),
            "meaning": {0: "faithful", 2: "usage/target error",
                        3: "UNFAITHFUL diagnostics", 4: "UNFAITHFUL anchor state",
                        5: "INDETERMINATE", 6: "faithful but DEGRADED"}.get(
                            p.returncode, "unknown"),
            "stdout_tail": p.stdout[-1200:],
            "stderr_tail": p.stderr[-600:],
        }
        if p.returncode != 0:
            scratch_ok = False
            rec["refusals"].append(
                "prefix failed the fidelity check (exit %d = %s); Q3 and Q4 "
                "REFUSED for this target" % (p.returncode,
                                             rec["fidelity"]["meaning"]))

    # ---- build the edit sequence -----------------------------------------
    real_versions = [apply_edit(original, edit_line, edit_indent,
                                args.edit_tactic, i) for i in range(1, args.k + 1)]
    prefix_versions = [apply_edit(prefix_text, edit_line, edit_indent,
                                  args.edit_tactic, i) for i in range(1, args.k + 1)]
    rec["edit_shas"] = [sha(t) for t in real_versions]

    results = {"Q1": [], "Q2": [], "Q3": [], "Q4": []}
    rec["quadrants"] = results
    rec["one_time"].update({})

    try:
        # ---------------- Q1: CLI, real file ------------------------------
        if "Q1" in want:
            rec["one_time"]["q1_unedited_s"] = None
            r0 = run_cli(workdir, real_rel, args.rss_interval, "Q1-unedited")
            rec["q1_unedited"] = r0
            rec["one_time"]["q1_unedited_s"] = r0["wall_s"]
            for i, text in enumerate(real_versions, 1):
                with open(real_path, "w", encoding="utf-8") as f:
                    f.write(text)
                results["Q1"].append(
                    run_cli(workdir, real_rel, args.rss_interval, "Q1-i%d" % i))
            with open(real_path, "w", encoding="utf-8") as f:
                f.write(original)

        # ---------------- Q3: CLI, scratch prefix -------------------------
        if "Q3" in want and scratch_ok:
            r0 = run_cli(workdir, prefix_rel, args.rss_interval, "Q3-unedited")
            rec["q3_unedited"] = r0
            rec["one_time"]["q3_unedited_s"] = r0["wall_s"]
            for i, text in enumerate(prefix_versions, 1):
                with open(prefix_path, "w", encoding="utf-8") as f:
                    f.write(text)
                results["Q3"].append(
                    run_cli(workdir, prefix_rel, args.rss_interval, "Q3-i%d" % i))
            with open(prefix_path, "w", encoding="utf-8") as f:
                f.write(prefix_text)

        # ---------------- Q2 / Q4: language-server quadrants --------------
        # Server-reuse policy, and why it is a knob rather than a constant:
        # sharing ONE server across Q2 and Q4 is what makes the sweep
        # affordable because the import environment is paid once. But a
        # shared server has ONE resident footprint, so its peak RSS cannot be
        # attributed to a quadrant.  If the scoring needs per-quadrant peak
        # memory, --separate-lsp-servers buys it at the price of a second
        # warm-up.  Whichever is used is recorded in the record.
        if ("Q2" in want) or ("Q4" in want and scratch_ok):
            order = ([("Q2", real_rel, real_versions, original),
                      ("Q4", prefix_rel, prefix_versions, prefix_text)]
                     if args.lsp_order == "q2q4" else
                     [("Q4", prefix_rel, prefix_versions, prefix_text),
                      ("Q2", real_rel, real_versions, original)])
            order = [o for o in order
                     if not (o[0] == "Q2" and "Q2" not in want)
                     and not (o[0] == "Q4" and ("Q4" not in want or not scratch_ok))]
            lsp_one_time = {}
            servers = []

            def run_on(cli, qname, rel, versions, base_text):
                first = "lsp_first_opened_quadrant" not in lsp_one_time
                op = cli.did_open(rel, text=base_text, grace=args.lsp_grace)
                lsp_one_time["%s_didopen_s" % qname.lower()] = round(
                    op["elapsed_s"], 4)
                # NOT a per-quadrant warm-up when the server is shared: for the
                # SECOND quadrant this elapsed figure contains the whole of the
                # first quadrant's run.  Named for what it is so nobody reads
                # it as W.
                lsp_one_time["%s_open_done_since_spawn_s" % qname.lower()] = \
                    round(time.perf_counter() - cli.t_spawn, 4)
                if first or args.separate_lsp_servers:
                    lsp_one_time.setdefault("lsp_first_opened_quadrant", qname)
                    key = ("w_fresh_server_s" if not args.separate_lsp_servers
                           else "w_fresh_server_%s_s" % qname.lower())
                    lsp_one_time[key] = round(
                        cli.warmup["spawn_to_initialized_s"] + op["elapsed_s"], 4)
                rec.setdefault("lsp_open", {})[qname] = trim_lsp(
                    op, "%s-didOpen" % qname)
                for i, text in enumerate(versions, 1):
                    ch = cli.did_change(rel, text, grace=args.lsp_grace)
                    results[qname].append(trim_lsp(ch, "%s-i%d" % (qname, i)))
                cli.did_close(rel)

            def new_server(label):
                tel_b = host_telemetry("before:" + label)
                cli = lspprobe.LeanLspClient(workdir, timeout=900.0)
                mon = ProcMon(cli.proc.pid, args.rss_interval, label=label).start()
                lsp_one_time.setdefault(
                    "spawn_to_initialized_s",
                    round(cli.warmup["spawn_to_initialized_s"], 4))
                return cli, mon, tel_b, label

            def close_server(cli, mon, tel_b, label, covers):
                rss = mon.stop()
                sd = cli.shutdown()
                servers.append({
                    "label": label,
                    "covers_quadrants": covers,
                    "peak_tree_rss_kib": rss["peak_tree_rss_kib"],
                    "peak_tree_rss_gib": rss["peak_tree_rss_gib"],
                    "rss_samples": rss["samples_with_live_tree"],
                    "rss_interval_s": rss["interval_s"],
                    "peak_members": rss["peak_members"],
                    "shutdown": sd,
                    "telemetry_before": tel_b,
                    "telemetry_after": host_telemetry("after:" + label),
                })

            if args.separate_lsp_servers:
                for qname, rel, versions, base_text in order:
                    cli, mon, tel_b, label = new_server("LSP-" + qname)
                    try:
                        run_on(cli, qname, rel, versions, base_text)
                    finally:
                        close_server(cli, mon, tel_b, label, [qname])
            else:
                cli, mon, tel_b, label = new_server("LSP-shared")
                try:
                    for qname, rel, versions, base_text in order:
                        run_on(cli, qname, rel, versions, base_text)
                finally:
                    close_server(cli, mon, tel_b, label,
                                 [o[0] for o in order])

            rec["one_time"].update(lsp_one_time)
            rec["lsp_servers"] = servers
            rec["lsp_server_shared"] = not args.separate_lsp_servers
            # back-compat / convenience: the peak a quadrant should be charged
            rec["lsp_peak_by_quadrant"] = {
                q: srv["peak_tree_rss_gib"]
                for srv in servers for q in srv["covers_quadrants"]}
            rec["lsp_peak_note"] = (
                "shared server: the SAME peak is charged to Q2 and Q4 because "
                "one process served both; rerun with --separate-lsp-servers "
                "for per-quadrant peaks"
                if not args.separate_lsp_servers else
                "separate servers: peaks are per-quadrant, and each quadrant "
                "paid its own warm-up")

    finally:
        # never leave the worktree mutated
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(original)
        with open(real_path, "r", encoding="utf-8") as f:
            rec["module_restored_ok"] = sha(f.read()) == rec["module_sha"]

    # ---- per-quadrant summary (all iterations, not a mean) ---------------
    summ = {}
    for q in ("Q1", "Q2", "Q3", "Q4"):
        rows = results[q]
        if not rows:
            summ[q] = {"available": False}
            continue
        walls = [r["wall_s"] for r in rows]
        summ[q] = {
            "available": True,
            "per_iteration_s": walls,
            "median_s": sorted(walls)[len(walls) // 2],
            "green": [r["green"] for r in rows],
            "n_errors": [r.get("n_errors") for r in rows],
            "peak_tree_rss_gib": (
                max(r["peak_tree_rss_gib"] for r in rows)
                if q in ("Q1", "Q3") else
                rec.get("lsp_peak_by_quadrant", {}).get(q)),
        }
    rec["summary"] = summ
    rec["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _emit(rec, args.out)
    return 0


def _emit(rec, out):
    text = json.dumps(rec, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("quadrant-bench: wrote %s" % out, file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
