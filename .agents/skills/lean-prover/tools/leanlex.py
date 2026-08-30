"""Minimal Lean 4 lexical scanner: enough structure to tell code from
comments/strings, and to find column-0 top-level commands.

Not a parser.  It tracks exactly four things:
  * ``--`` line comments
  * ``/- ... -/`` block comments, which NEST in Lean, including the
    ``/--`` docstring and ``/-!`` module-doc forms
  * ``"..."`` string literals with ``\\"`` escapes and line gaps
  * ``'c'`` character literals (only when they match a strict char-literal
    shape, so that primed identifiers like ``h'`` are not mistaken for one)

The scan is LINE-ORIENTED on purpose.  A character-oriented scan that advances
two positions for ``--``/``/-``/``-/``/``\\x`` can step over a newline and lose
a line boundary; Blanc really does contain string gaps (a ``\\`` as the last
character of a line inside a string literal), which triggers exactly that.
"""

import re

# A character literal, and nothing that looks like a primed identifier.
_CHARLIT = re.compile(r"'(\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4,6}|.)|[^\\'\n])'")
_IDENT_CHAR = re.compile(r"[0-9A-Za-z_'!?À-￿]")


def scan(text, on_delim_in_string=None):
    """Scan ``text`` line by line.

    Returns ``states``: ``states[i]`` is ``(comment_depth, in_string)`` at the
    very start of 0-indexed line ``i``.  ``(0, False)`` means "ordinary code".

    If ``on_delim_in_string`` is given it is called as
    ``on_delim_in_string(line_1indexed, col_1indexed, token)`` for every ``/-``
    or ``-/`` that occurs INSIDE a string literal -- a delimiter Lean does not
    see, but that a naive block-comment wrapper would expose.
    """
    lines = text.split("\n")
    states = []
    depth = 0
    in_string = False
    for ln, line in enumerate(lines):
        states.append((depth, in_string))
        i = 0
        n = len(line)
        while i < n:
            c = line[i]
            if in_string:
                if c == "\\":
                    i = min(i + 2, n)
                    continue
                if line.startswith("/-", i) or line.startswith("-/", i):
                    if on_delim_in_string is not None:
                        on_delim_in_string(ln + 1, i + 1, line[i:i + 2])
                    i += 2
                    continue
                if c == '"':
                    in_string = False
                i += 1
                continue
            if depth > 0:
                # Inside a block comment.  Only nesting delimiters matter;
                # a string literal inside a comment is just text to Lean too.
                if line.startswith("/-", i):
                    depth += 1
                    i += 2
                    continue
                if line.startswith("-/", i):
                    depth -= 1
                    i += 2
                    continue
                i += 1
                continue
            # Ordinary code.
            if line.startswith("/-", i):
                depth += 1
                i += 2
                continue
            if line.startswith("--", i):
                # A line comment hides delimiters just as a string does: once
                # the region is wrapped in /- ... -/ the `--` has no meaning
                # any more, so a `-/` in here really does close the wrapper.
                if on_delim_in_string is not None:
                    j = i
                    while j < n - 1:
                        if line.startswith("/-", j) or line.startswith("-/", j):
                            on_delim_in_string(ln + 1, j + 1, line[j:j + 2])
                            j += 2
                            continue
                        j += 1
                break  # line comment runs to end of line
            if c == '"':
                in_string = True
                i += 1
                continue
            if c == "'":
                prev = line[i - 1] if i > 0 else ""
                if not (prev and _IDENT_CHAR.match(prev)):
                    m = _CHARLIT.match(line, i)
                    if m:
                        i = m.end()
                        continue
            i += 1
    return states


def line_start_states(text):
    return scan(text)


def delimiters_hidden_in_strings(text):
    """``/-`` and ``-/`` occurrences Lean currently does NOT read as comment
    delimiters -- because they sit inside a string literal or inside a ``--``
    line comment.

    These are the splice-mode hazard: wrapping a suffix in ``/- ... -/`` makes
    every one of them live, so a hidden ``-/`` becomes a real terminator and a
    hidden ``/-`` a real opener.  Returns ``(line, col, token)``, 1-indexed.
    """
    found = []
    scan(text, on_delim_in_string=lambda l, c, t: found.append((l, c, t)))
    return found
