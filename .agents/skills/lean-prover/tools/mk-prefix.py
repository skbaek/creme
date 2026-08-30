#!/usr/bin/env python3
"""Fabricate a scratch *prefix file* from a real Lean module.

A prefix file is byte-identical to the real module from line 1 through the last
source line of a chosen target declaration, with the remainder either dropped
(``--mode truncate``) or wrapped in a block comment (``--mode splice``), and
with the scopes that are still open at that point explicitly closed.

Usage:
  mk-prefix.py --file <path.lean> --decl <name>
               [--mode truncate|splice] [--out <path>] [--print-anchor]
               [--json]

Exit codes: 0 success, 2 usage/target error.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leanlex import line_start_states, delimiters_hidden_in_strings  # noqa: E402

# Column-0 tokens that begin a new top-level command region.
MODIFIERS = {
    "private", "protected", "noncomputable", "partial", "unsafe", "nonrec",
    "scoped", "local", "meta",
}

DECL_KEYWORDS = {
    "theorem", "lemma", "def", "abbrev", "instance", "example", "structure",
    "inductive", "class", "opaque", "axiom", "constant",
}

OTHER_COMMANDS = {
    "import", "open", "namespace", "section", "end", "variable", "variables",
    "universe", "set_option", "attribute", "export", "include", "omit",
    "alias", "mutual", "initialize", "builtin_initialize",
    "run_cmd", "run_elab", "register_simp_attr", "macro", "macro_rules",
    "elab", "elab_rules", "syntax", "notation", "infix", "infixl", "infixr",
    "prefix", "postfix", "declare_syntax_cat", "binder_predicate",
    "register_builtin_option", "builtin_dsimproc", "simproc", "dsimproc",
    "recommended_spelling",
}

# Column-0 tokens that CONTINUE the preceding declaration rather than starting
# a new one.  Listing them is documentation, not behaviour: what matters is
# that they are absent from COMMAND_TOKENS.
CONTINUATION_TOKENS = {
    "where", "in", "with", "deriving", "termination_by", "decreasing_by",
    "match", "fun", "do", "then", "else",
}

COMMAND_TOKENS = MODIFIERS | DECL_KEYWORDS | OTHER_COMMANDS

# Column-0 starts that open a comment region rather than a command.
COMMENT_STARTS = ("/--", "/-!", "/-", "--")

_TOKEN = re.compile(r"[A-Za-z_#][A-Za-z0-9_!?']*")
_IDENT = re.compile(r"[A-Za-z_À-￿][A-Za-z0-9_!?'À-￿]*"
                    r"(?:\.[A-Za-z0-9_!?'À-￿]+)*")


class TargetError(Exception):
    pass


def classify_lines(text):
    """For each 0-indexed line return a dict describing its column-0 role."""
    lines = text.split("\n")
    states = line_start_states(text)
    out = []
    for i, raw in enumerate(lines):
        st = states[i] if i < len(states) else (0, False)
        depth, in_string = st
        info = {
            "n": i,
            "raw": raw,
            "in_comment_at_start": depth > 0,
            "in_string_at_start": in_string,
            "blank": raw.strip() == "",
            "kind": None,      # "command" | "comment" | None
            "token": None,
            "attr": False,
        }
        if depth == 0 and not in_string and raw[:1] not in ("", " ", "\t"):
            if raw.startswith("@["):
                info["kind"] = "command"
                info["attr"] = True
            elif any(raw.startswith(c) for c in COMMENT_STARTS):
                info["kind"] = "comment"
            else:
                m = _TOKEN.match(raw)
                if m and m.group(0) in COMMAND_TOKENS:
                    info["kind"] = "command"
                    info["token"] = m.group(0)
                elif m and m.group(0).startswith("#"):
                    info["kind"] = "command"
                    info["token"] = m.group(0)
        out.append(info)
    return lines, out


def declared_name(raw):
    """Given a column-0 declaration line, return (keyword, written name)."""
    rest = raw
    kw = None
    while True:
        m = _TOKEN.match(rest)
        if not m:
            return None, None
        tok = m.group(0)
        if tok in MODIFIERS:
            rest = rest[m.end():].lstrip()
            continue
        if tok in DECL_KEYWORDS:
            kw = tok
            rest = rest[m.end():].lstrip()
            break
        return None, None
    if kw == "example":
        return kw, None
    m = _IDENT.match(rest)
    if not m:
        return kw, None
    return kw, m.group(0)


def namespace_stack_at(infos, upto):
    """Scopes open at the START of 0-indexed line ``upto``.

    Each entry is ``("namespace"|"section"|"mutual", name_or_None)``.
    """
    stack = []
    for info in infos[:upto]:
        if info["kind"] != "command":
            continue
        tok = info["token"]
        raw = info["raw"]
        if tok in ("namespace", "section"):
            # ``section`` may be anonymous; ``namespace`` never is.
            rest = raw[len(tok):].strip()
            # A trailing ``--`` comment on the same line is not part of it.
            rest = rest.split("--")[0].strip()
            m = _IDENT.match(rest) if rest else None
            stack.append((tok, m.group(0) if m else None))
        elif tok == "mutual":
            stack.append(("mutual", None))
        elif tok == "end":
            if stack:
                stack.pop()
    return stack


def find_target(text, decl):
    lines, infos = classify_lines(text)
    hits = []
    for info in infos:
        if info["kind"] != "command" or info["attr"]:
            continue
        kw, name = declared_name(info["raw"])
        if not kw or not name:
            continue
        stack = namespace_stack_at(infos, info["n"])
        ns = [nm for kind, nm in stack if kind == "namespace" and nm]
        qualified = ".".join(ns + [name]) if ns else name
        if name == decl or qualified == decl or qualified.endswith("." + decl):
            hits.append((info["n"], kw, name, qualified))
    if not hits:
        raise TargetError(f"no top-level declaration named {decl!r} found")
    exact = [h for h in hits if h[2] == decl or h[3] == decl]
    if exact:
        hits = exact
    if len(hits) > 1:
        names = ", ".join(f"{h[3]}@line{h[0]+1}" for h in hits)
        raise TargetError(f"ambiguous target {decl!r}; candidates: {names}")
    return lines, infos, hits[0]


def target_extent(infos, start_idx):
    """Return (last_source_idx, next_cmd_idx_or_None), 0-indexed.

    ``last_source_idx`` is the last line preserved byte-identically.
    """
    n = len(infos)
    nxt = None
    for i in range(start_idx + 1, n):
        if infos[i]["kind"] == "command":
            nxt = i
            break
    if nxt is None:
        last = n - 1
    else:
        # Give back to the NEXT declaration any contiguous run of column-0
        # comments / blank lines immediately preceding it.  Comments in the
        # middle of the target's own proof therefore do not truncate it.
        j = nxt - 1
        while j > start_idx:
            info = infos[j]
            if (info["blank"] or info["in_comment_at_start"]
                    or info["kind"] == "comment"):
                j -= 1
                continue
            break
        last = j
    # Trim trailing blank lines.
    while last > start_idx and infos[last]["blank"]:
        last -= 1
    return last, nxt


def leading_modifier_start(infos, start_idx):
    """First line of the target's own region: walk back over ``... in``
    modifier lines, ``@[...]`` attributes and its docstring."""
    i = start_idx
    while i > 0:
        p = infos[i - 1]
        raw = p["raw"]
        if p["kind"] == "command" and (
                p["attr"] or re.search(r"\bin\s*$", raw)):
            i -= 1
            continue
        if p["in_comment_at_start"] or (
                p["kind"] == "comment" and raw.startswith("/--")):
            # Walk to the top of the docstring block.
            k = i - 1
            while k > 0 and infos[k]["in_comment_at_start"]:
                k -= 1
            if infos[k]["kind"] == "comment" and infos[k]["raw"].startswith("/--"):
                i = k
                continue
            break
        break
    return i


def closing_ends(stack):
    out = []
    for kind, name in reversed(stack):
        out.append(f"end {name}" if name else "end")
    return out


def compute_anchor(lines, start_idx, last_idx):
    """Ranked candidate tactic positions inside the target's proof.

    Returns ``(primary, candidates)`` where each entry is
    ``{"line": L, "col": C, "indent": I}``, 1-indexed, meaning "a
    ``trace_state`` inserted on its own line immediately BEFORE line L at
    indentation I".

    Candidacy is a heuristic, not a parse, so the list is RANKED and the
    fidelity checker verifies a candidate by elaborating the real file with the
    probe inserted.  Lines beginning with ``|`` are excluded outright: those are
    ``induction ... with`` / ``match`` alternatives, and a tactic cannot be
    inserted between them.
    """
    by_idx = None
    for i in range(start_idx, last_idx + 1):
        s = lines[i].rstrip()
        if re.search(r"(^|[\s(\[])by\s*$", s):
            by_idx = i
            break
    if by_idx is None or by_idx >= last_idx:
        return None, []
    cands = []
    for i in range(by_idx + 1, last_idx + 1):
        s = lines[i]
        if s.strip() == "":
            continue
        stripped = s.lstrip()
        if stripped.startswith("|"):
            continue
        if stripped[:1] in (")", "]", "}", ","):
            continue
        indent = len(s) - len(stripped)
        cands.append({"line": i + 1, "col": indent + 1, "indent": indent})
    if not cands:
        return None, []
    # Deepest interior state first: latest line, then shallowest indent as a
    # tie-break (a shallower line is more likely a real sequence element).
    ranked = sorted(cands, key=lambda c: (-c["line"], c["indent"]))
    return ranked[0], ranked


def build(text, decl, mode):
    lines, infos, (start_idx, kw, name, qualified) = find_target(text, decl)
    last_idx, next_idx = target_extent(infos, start_idx)
    region_start = leading_modifier_start(infos, start_idx)
    stack = namespace_stack_at(infos, last_idx + 1)
    ends = closing_ends(stack)

    prefix_lines = lines[: last_idx + 1]
    suffix_lines = lines[last_idx + 1:]
    # Drop a single trailing empty element produced by a final newline.
    trailing_nl = bool(suffix_lines) and suffix_lines[-1] == ""
    if trailing_nl:
        suffix_lines = suffix_lines[:-1]
    suffix_empty = all(s.strip() == "" for s in suffix_lines)

    body = list(prefix_lines)
    body.append("")
    body.extend(ends)
    if mode == "splice" and not suffix_empty:
        body.append("/-")
        body.extend(suffix_lines)
        body.append("-/")
    out = "\n".join(body) + "\n"

    # --- structural self-check on the fabricated output -------------------
    out_lines, out_infos = classify_lines(out)
    residual = namespace_stack_at(out_infos, len(out_infos))
    end_depth, end_str = line_start_states(out)[-1]
    problems = []
    if residual:
        problems.append(f"scopes still open at EOF: {residual}")
    if end_depth != 0:
        problems.append(f"unterminated block comment at EOF (depth {end_depth})")
    if end_str:
        problems.append("unterminated string literal at EOF")

    # Splice mode only: a ``/-`` or ``-/`` hidden inside a string literal in the
    # suffix becomes a REAL comment delimiter once the suffix is wrapped.
    hazards = []
    if mode == "splice" and not suffix_empty:
        suffix_text = "\n".join(suffix_lines)
        for (l, c, tok) in delimiters_hidden_in_strings(suffix_text):
            hazards.append({"line": last_idx + 1 + l, "col": c, "token": tok})

    anchor, anchor_candidates = compute_anchor(lines, start_idx, last_idx)
    meta = {
        "decl": decl,
        "keyword": kw,
        "written_name": name,
        "qualified_name": qualified,
        "decl_line": start_idx + 1,
        "region_start_line": region_start + 1,
        "last_source_line": last_idx + 1,
        "next_command_line": (next_idx + 1) if next_idx is not None else None,
        "open_scopes": [list(s) for s in stack],
        "end_lines": ends,
        "mode": mode,
        "suffix_empty": suffix_empty,
        "anchor": anchor,
        "anchor_candidates": anchor_candidates,
        "structure_problems": problems,
        "splice_hazards": hazards,
    }
    return out, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--decl", required=True)
    ap.add_argument("--mode", choices=["truncate", "splice"], default="truncate")
    ap.add_argument("--out")
    ap.add_argument("--print-anchor", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        out, meta = build(text, args.decl, args.mode)
    except TargetError as e:
        print(f"mk-prefix: {e}", file=sys.stderr)
        return 2

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
    else:
        sys.stdout.write(out)

    for pb in meta["structure_problems"]:
        print(f"mk-prefix: WARNING: {pb}", file=sys.stderr)
    for hz in meta["splice_hazards"]:
        print(f"mk-prefix: WARNING: splice hazard: {hz['token']!r} hidden in a "
              f"string literal at {hz['line']}:{hz['col']} of the original file",
              file=sys.stderr)

    if args.json:
        print(json.dumps(meta), file=sys.stderr)
    elif args.print_anchor:
        a = meta["anchor"]
        print(f"mk-prefix: target {meta['qualified_name']} "
              f"lines {meta['region_start_line']}..{meta['last_source_line']} "
              f"(decl at {meta['decl_line']})", file=sys.stderr)
        print(f"mk-prefix: closing scopes: {meta['end_lines'] or '(none)'}",
              file=sys.stderr)
        if a:
            print(f"mk-prefix: anchor {a['line']}:{a['col']} "
                  f"({len(meta['anchor_candidates'])} candidates)",
                  file=sys.stderr)
        else:
            print("mk-prefix: anchor (none found)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
