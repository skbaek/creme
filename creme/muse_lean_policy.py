"""Rule-based approval policy for shell commands in a Muse Lean-mode session.

A Lean session's ``muse serve`` host is unsandboxed (the owned build must lower
its priority), so ``muse_client.decide_approval`` used to approve only the
exact owned build and send every other shell command to the master. This module
approves, by rule and never by judgment, the class of commands a Lean unit runs
all day: reads and searches inside configured read roots, a few in-workspace
edits, read-only ``git``, restricted Python and ``awk``, the repository's static
gate scripts, and the owned build inside a pipeline.

Fail-closed in three layers:

1. **Own lexer over the raw command.** Muse's ``stages`` drop redirections
   (``2>&1``, ``> FILE``) and environment prefixes and say nothing about
   heredoc bodies, so the decision is taken on what the shell will execute: a
   deliberately small bash subset (words, single and double quotes, ``;``
   ``&&`` ``||`` ``|`` and newline, ``N>&M``, ``>``/``>>``/``<`` to a file,
   quoted ``<<`` heredocs, ``$?`` and variables the command itself assigned).
   Anything else (command substitution, backticks, subshells, braces,
   background jobs, process substitution, here-strings, other expansions) is
   refused.
2. **Cross-check with Muse's stages.** Muse's own parse must agree on the
   number of simple commands and their program names, and on the full argv of
   every stage Muse marks ``argvComplete`` (backslashes ignored: Muse keeps the
   backslash of ``\\"`` inside double quotes). Muse sends no stages when a word
   uses a variable; that is accepted only when this lexer saw one.
3. **Per-program rules** with every path argument resolved (``realpath``,
   from every directory the shell may be in) inside the read roots, and every
   write inside the workspace.

Anything not matched returns a reason and the caller leaves the command to the
master, as before. Semaphore and reclamation commands are refused earlier by
``luna_lean.forbidden_command`` and are aborted, as before.
"""

from __future__ import annotations

import ast
import glob as _glob
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .build_ownership import RUNTIME_RELATIVE

POLICY_NAME = "lean-shell-policy-v1"

# Default read roots besides the workspace, relative to the home directory.
DEFAULT_HOME_READ_ROOTS = (".elan/toolchains", ".local/share/muse/sessions")
# Build logs the owned-build wrapper writes (only the session's own label is readable by default).
BUILD_LOG_DIR = Path("creme") / RUNTIME_RELATIVE / "logs"
_BUILD_LOG_NAME = r"-\d{8}T\d{6}(?:\.\d+)?Z-\d+\.log"
# A build's output may go to /tmp/NAME.log; the same top-level /tmp/NAME.log files are readable.
_TMP_LOG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.log")
TMP_DIR = Path("/tmp")

GATE_SCRIPTS = ("scripts/check-hygiene.sh", "scripts/check-integrity.sh", "scripts/check-assurance-manifest.py")
PYTHON_MODULES = frozenset({"re", "json", "collections", "textwrap"})
PYTHON_BUILTINS = frozenset({
    "print", "len", "range", "enumerate", "sorted", "reversed", "set", "frozenset", "dict", "list", "tuple",
    "str", "int", "float", "bool", "min", "max", "sum", "any", "all", "zip", "map", "filter", "isinstance",
    "abs", "repr", "next", "iter", "round", "ord", "chr", "divmod", "hex", "format", "slice",
    "Exception", "ValueError", "KeyError", "IndexError", "AssertionError", "StopIteration", "RuntimeError",
    "TypeError", "True", "False", "None",
})
GIT_READ_VERBS = frozenset({"log", "show", "diff", "status", "grep", "ls-files", "ls-tree", "merge-base",
                            "rev-parse", "blame", "rev-list", "cat-file", "show-ref"})
_GIT_FORBIDDEN_PREFIXES = ("--output", "-O", "--open-files-in-pager", "--exec-path", "--ext-diff", "--no-index",
                           "--upload-pack", "--exec")
READ_TOOLS = frozenset({"ls", "cat", "head", "tail", "grep", "wc", "sort", "uniq", "cut", "tr", "nl", "diff",
                        "date"})
INERT_TOOLS = frozenset({"echo", "true", "pwd"})
_VAR_DENY = frozenset({"PATH", "IFS", "ENV", "HOME", "CDPATH", "PWD", "OLDPWD", "SHELL", "SHELLOPTS", "BASHOPTS",
                       "GLOBIGNORE", "PS4", "PROMPT_COMMAND", "TMPDIR", "TMP", "TEMP", "PAGER", "EDITOR",
                       "VISUAL", "MANPAGER", "TERM", "USER", "LOGNAME", "MAIL"})
_VAR_DENY_PREFIXES = ("LD_", "PYTHON", "GIT_", "LC_", "LANG", "LESS", "BASH", "DYLD_", "LEAN", "LAKE", "ELAN")
_VAR_VALUE = re.compile(r"[A-Za-z0-9_./+@%:,=-]+")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNQUOTED_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:=,+-@%^~*?[]#")
_GLOB = set("*?[")


class Refusal(Exception):
    """A command (or part of it) is outside the policy; the message says why."""


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


# ---------------------------------------------------------------------------
# Context


def gate_scripts_failure(workspace: Path) -> Optional[str]:
    """Why the workspace's ``scripts/`` cannot be trusted to run unsandboxed, or ``None``.

    The model can write the workspace, so a gate script is run by rule only
    when ``scripts/`` equals ``HEAD``: no tracked change, no untracked file,
    and no ignored file other than bytecode under ``__pycache__/``.
    """
    failure = git_repository_failure(workspace, workspace)
    if failure is not None:
        return failure
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all", "--ignored",
             "--", "scripts"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git status of scripts/ failed: {exc}"
    if completed.returncode != 0:
        return "git status of scripts/ failed"
    dirty = [line for line in completed.stdout.splitlines()
             if line.strip() and not re.fullmatch(r"!! scripts/(?:[^/]+/)*__pycache__/(?:[^/]+\.pyc)?", line)]
    if dirty:
        return f"scripts/ differs from HEAD ({dirty[0].strip()[:80]})"
    return None


@dataclass(frozen=True)
class PolicyContext:
    workspace: Path                        # resolved session target (the shell's starting directory)
    goal: str                              # the session's Lean goal label
    read_roots: tuple[Path, ...]           # resolved; includes the workspace
    home: Path
    build_log_dir: Path
    scripts_check: Callable[[Path], Optional[str]] = gate_scripts_failure


def default_read_roots(workspace: Path, home: Path) -> tuple[Path, ...]:
    return (workspace, *(home / relative for relative in DEFAULT_HOME_READ_ROOTS))


def context(workspace: Path, goal: str, extra_read_roots: Iterable[str | Path] = (), home: Optional[Path] = None,
            scripts_check: Callable[[Path], Optional[str]] = gate_scripts_failure) -> PolicyContext:
    home = Path(os.path.realpath(home or Path.home()))
    workspace = Path(os.path.realpath(workspace))
    roots: list[Path] = []
    for root in (*default_read_roots(workspace, home), *(Path(str(r)).expanduser() for r in extra_read_roots)):
        real = Path(os.path.realpath(root))
        if real not in roots:
            roots.append(real)
    return PolicyContext(workspace, goal, tuple(roots), home, Path(os.path.realpath(home / BUILD_LOG_DIR)),
                         scripts_check)


def read_root_failure(value: str) -> Optional[str]:
    """Why ``value`` cannot be an extra read root (``muse start --read-root``), or ``None``."""
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        return f"read root {value!r} is not an absolute path"
    real = Path(os.path.realpath(path))
    if not real.is_dir():
        return f"read root {value!r} is not a directory"
    home = Path(os.path.realpath(Path.home()))
    if real in (Path("/"), home) or real in home.parents:
        return f"read root {value!r} is too broad (the filesystem root or the home directory)"
    return None


# ---------------------------------------------------------------------------
# Lexer


@dataclass
class Word:
    parts: list = field(default_factory=list)   # ("lit", text, quoted) | ("var", name, quoted) | ("status",)
    glob: bool = False                          # an unquoted * ? [
    tilde: bool = False                         # an unquoted leading ~ (alone or before /)
    raw: str = ""                               # the source text, for messages

    def compare(self) -> str:
        """The text Muse's parser reports (variables unexpanded), without backslashes."""
        out = []
        for part in self.parts:
            if part[0] == "lit":
                out.append(part[1])
            elif part[0] == "var":
                out.append("$" + part[1])
            else:
                out.append("$?")
        return "".join(out).replace("\\", "")

    def has_vars(self) -> bool:
        return any(part[0] != "lit" for part in self.parts)


@dataclass
class Redirect:
    fd: Optional[int]
    op: str                       # ">", ">>", "&>", "<", "dup", "<<"
    target: Optional[Word] = None
    dup: Optional[int] = None
    delimiter: str = ""
    quoted: bool = False
    body: Optional[str] = None


@dataclass
class Simple:
    words: list = field(default_factory=list)
    redirects: list = field(default_factory=list)

    def heredoc(self) -> Optional[Redirect]:
        found = [r for r in self.redirects if r.op == "<<"]
        return found[0] if found else None


_OPERATOR_CHARS = set(";&|<>()\n")


def _lex(command: str) -> list:
    """Tokens: ``("word", Word)``, ``("op", text)``, ``("redir", Redirect)``. Raises ``Refusal``."""
    tokens: list = []
    pending_heredocs: list[Redirect] = []
    i, n = 0, len(command)

    def read_var(j: int, quoted: bool, word: Word) -> int:
        # command[j] == "$"
        if j + 1 < n and command[j + 1] == "?":
            word.parts.append(("status",))
            return j + 2
        if j + 1 < n and command[j + 1] == "{":
            end = command.find("}", j + 2)
            _need(end != -1, "unterminated ${")
            name = command[j + 2:end]
            _need(bool(_NAME.fullmatch(name)), f"expansion ${{{name}}} is not a plain variable")
            word.parts.append(("var", name, quoted))
            return end + 1
        match = _NAME.match(command, j + 1)
        _need(match is not None, "a $ expansion other than $?, $NAME or ${NAME} (command substitution, "
                                 "arithmetic, positional or special parameters)")
        word.parts.append(("var", match.group(0), quoted))
        return match.end()

    def read_word(j: int) -> tuple[Word, int]:
        word = Word()
        start = j
        if command[j] == "~" and (j + 1 == n or command[j + 1] in "/ \t\n;&|<>"):
            word.tilde = True
        _need(command[j] != "#", "a shell comment")
        while j < n and command[j] not in " \t" and command[j] not in _OPERATOR_CHARS:
            c = command[j]
            if c == "'":
                end = command.find("'", j + 1)
                _need(end != -1, "unterminated single quote")
                word.parts.append(("lit", command[j + 1:end], True))
                j = end + 1
            elif c == '"':
                j += 1
                buf = []
                while True:
                    _need(j < n, "unterminated double quote")
                    d = command[j]
                    if d == '"':
                        j += 1
                        break
                    if d == "\\":
                        _need(j + 1 < n, "trailing backslash")
                        nxt = command[j + 1]
                        _need(nxt != "\n", "a line continuation")
                        # bash drops the backslash only before $ ` " \; compare() drops every backslash
                        buf.append(nxt if nxt in '$`"\\' else d + nxt)
                        j += 2
                    elif d == "$" and (j + 1 >= n or command[j + 1] in '" \t'):
                        buf.append(d)            # a lone $ before the closing quote or a blank is literal
                        j += 1
                    elif d == "$":
                        if buf:
                            word.parts.append(("lit", "".join(buf), True))
                            buf = []
                        j = read_var(j, True, word)
                    elif d == "`":
                        raise Refusal("a backtick command substitution")
                    else:
                        buf.append(d)
                        j += 1
                word.parts.append(("lit", "".join(buf), True))
            elif c == "\\":
                _need(j + 1 < n and command[j + 1] != "\n", "a line continuation or trailing backslash")
                word.parts.append(("lit", command[j + 1], True))
                j += 2
            elif c == "$":
                j = read_var(j, False, word)
            elif c == "`":
                raise Refusal("a backtick command substitution")
            else:
                _need(c in _UNQUOTED_SAFE, f"the unquoted character {c!r}")
                if c in _GLOB:
                    word.glob = True
                word.parts.append(("lit", c, False))
                j += 1
        word.raw = command[start:j]
        return word, j

    while i < n:
        c = command[i]
        if c in " \t":
            i += 1
            continue
        if c == "\n":
            tokens.append(("op", "\n"))
            i += 1
            for heredoc in pending_heredocs:
                lines = []
                while True:
                    _need(i < n, f"heredoc {heredoc.delimiter} is not terminated")
                    end = command.find("\n", i)
                    line = command[i:] if end == -1 else command[i:end]
                    i = n if end == -1 else end + 1
                    if line == heredoc.delimiter:
                        break
                    lines.append(line)
                heredoc.body = "\n".join(lines)
            pending_heredocs = []
            continue
        if c == ";":
            _need(command[i:i + 2] != ";;", "a ;; case terminator")
            tokens.append(("op", ";"))
            i += 1
            continue
        if c == "&":
            if command.startswith("&&", i):
                tokens.append(("op", "&&"))
                i += 2
                continue
            if command.startswith("&>", i):
                _need(not command.startswith("&>>", i), "an &>> redirection")
                target, i = _redirect_target(command, i + 2, read_word)
                tokens.append(("redir", Redirect(None, "&>", target)))
                continue
            raise Refusal("a background job (&)")
        if c == "|":
            if command.startswith("||", i):
                tokens.append(("op", "||"))
                i += 2
                continue
            _need(not command.startswith("|&", i), "a |& pipe")
            tokens.append(("op", "|"))
            i += 1
            continue
        if c in "()":
            raise Refusal("a subshell or grouping parenthesis")
        match = re.compile(r"(\d*)(>>|>&|>\||<<<|<<-|<<|<&|<>|>\(|<\(|>|<)").match(command, i)
        if match:
            fd = int(match.group(1)) if match.group(1) else None
            op = match.group(2)
            i = match.end()
            _need(op not in (">|", "<<<", "<<-", "<&", "<>", ">(", "<("), f"the redirection {op}")
            if op == ">&":
                dup = re.compile(r"\d+").match(command, i)
                _need(dup is not None and (dup.end() == n or command[dup.end()] in " \t;&|\n"),
                      "a >& redirection to something other than a file descriptor")
                tokens.append(("redir", Redirect(fd, "dup", dup=int(dup.group(0)))))
                i = dup.end()
                continue
            if op == "<<":
                while i < n and command[i] in " \t":
                    i += 1
                delim = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\"|([A-Za-z_][A-Za-z0-9_]*)")
                found = delim.match(command, i)
                _need(found is not None, "a heredoc delimiter that is not a plain word")
                quoted = found.group(3) is None
                redirect = Redirect(fd, "<<", delimiter=found.group(1) or found.group(2) or found.group(3),
                                    quoted=quoted)
                pending_heredocs.append(redirect)
                tokens.append(("redir", redirect))
                i = found.end()
                continue
            target, i = _redirect_target(command, i, read_word)
            tokens.append(("redir", Redirect(fd, op, target)))
            continue
        word, i = read_word(i)
        tokens.append(("word", word))
    _need(not pending_heredocs, "a heredoc without a body")
    return tokens


def _redirect_target(command: str, i: int, read_word: Callable) -> tuple[Word, int]:
    while i < len(command) and command[i] in " \t":
        i += 1
    _need(i < len(command) and command[i] not in _OPERATOR_CHARS, "a redirection without a target")
    word, i = read_word(i)
    _need(not word.has_vars() and not word.glob, "a redirection target with an expansion")
    return word, i


def _parse(command: str) -> list[tuple[Optional[str], list[Simple]]]:
    """Statements as ``(connector, pipeline)``; the connector joins it to the previous statement."""
    statements: list[tuple[Optional[str], list[Simple]]] = []
    pipeline: list[Simple] = [Simple()]
    connector: Optional[str] = None
    for kind, value in _lex(command):
        if kind == "word":
            pipeline[-1].words.append(value)
        elif kind == "redir":
            pipeline[-1].redirects.append(value)
        elif value == "|":
            _need(bool(pipeline[-1].words), "an empty pipeline stage")
            pipeline.append(Simple())
        else:
            if not pipeline[-1].words and not pipeline[-1].redirects and len(pipeline) == 1:
                _need(value in (";", "\n"), f"an empty command before {value}")
                if value == ";":
                    _need(False, "an empty command before ;")
                continue          # blank line
            _need(bool(pipeline[-1].words), "an empty command")
            statements.append((connector, pipeline))
            connector, pipeline = value, [Simple()]
    if pipeline[-1].words or pipeline[-1].redirects or len(pipeline) > 1:
        _need(bool(pipeline[-1].words), "an empty command")
        statements.append((connector, pipeline))
    elif connector in ("&&", "||"):
        raise Refusal(f"a command ending in {connector}")
    _need(bool(statements), "an empty command")
    return statements


# ---------------------------------------------------------------------------
# Evaluation


@dataclass
class Value:
    text: str
    glob: bool
    tilde: bool


class _Evaluator:
    def __init__(self, ctx: PolicyContext, logvar: Optional[str]) -> None:
        self.ctx = ctx
        self.cwds: set[Path] = {ctx.workspace}
        self.bindings: dict[str, str] = {}
        self.labels: list[str] = []
        if logvar is not None:
            self.bindings[logvar] = str(ctx.build_log_dir / f"{ctx.goal}-00000000T000000Z-0.log")

    # -- words ------------------------------------------------------------
    def value(self, word: Word, allow_status: bool = False) -> Value:
        out = []
        for part in word.parts:
            if part[0] == "lit":
                out.append(part[1])
            elif part[0] == "var":
                _need(part[1] in self.bindings, f"${part[1]} is not assigned earlier in this command")
                out.append(self.bindings[part[1]])
            else:
                _need(allow_status, "$? outside echo")
                out.append("0")
        return Value("".join(out), word.glob, word.tilde)

    def resolve(self, value: Value, cwd: Path) -> str:
        text = value.text
        if value.tilde:
            text = str(self.ctx.home) + text[1:]
        else:
            _need(not text.startswith("~"), f"{value.text!r}: only ~ and ~/ expand")
        return os.path.normpath(os.path.join(str(cwd), text))

    def _inside(self, real: str, roots: Iterable[Path]) -> bool:
        return any(real == str(root) or real.startswith(str(root).rstrip("/") + "/") for root in roots)

    def _special_readable(self, real: str) -> bool:
        path = Path(real)
        if path.parent == Path(os.path.realpath(TMP_DIR)) and _TMP_LOG.fullmatch(path.name):
            return True
        return (path.parent == self.ctx.build_log_dir
                and re.fullmatch(re.escape(self.ctx.goal) + _BUILD_LOG_NAME, path.name) is not None)

    def readable(self, value: Value) -> None:
        if value.text in ("/dev/null", "-"):
            return
        for cwd in self.cwds:
            lexical = self.resolve(value, cwd)
            if value.glob:
                for component in Path(lexical).parts:
                    _need(not (component.startswith(".") and set(component) & _GLOB),
                          f"{value.text!r}: a glob that can match dot entries")
            candidates = [lexical] + (_glob.glob(lexical) if value.glob else [])
            for candidate in candidates:
                real = os.path.realpath(candidate)
                _need(self._inside(real, self.ctx.read_roots) or self._special_readable(real),
                      f"{value.text!r} is outside the read roots")

    def writable(self, value: Value) -> str:
        _need(not value.glob, f"{value.text!r}: a glob in a write target")
        _need(".." not in Path(value.text).parts, f"{value.text!r}: .. in a write target")
        for cwd in self.cwds:
            real = os.path.realpath(self.resolve(value, cwd))
            workspace = str(self.ctx.workspace)
            _need(real.startswith(workspace.rstrip("/") + "/"), f"{value.text!r} is outside the workspace")
            _need(".git" not in Path(real[len(workspace):]).parts, f"{value.text!r} is inside .git")
        return value.text

    def build_log_target(self, value: Value) -> bool:
        real = os.path.realpath(self.resolve(value, self.ctx.workspace))
        path = Path(real)
        return (not value.glob and path.parent == Path(os.path.realpath(TMP_DIR))
                and _TMP_LOG.fullmatch(path.name) is not None and not path.is_symlink())

    # -- statements ---------------------------------------------------------
    def statement(self, pipeline: list[Simple]) -> None:
        for simple in pipeline:
            self.simple(simple, in_pipeline=len(pipeline) > 1)

    def simple(self, simple: Simple, in_pipeline: bool) -> None:
        first = simple.words[0]
        name_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", first.compare())
        if name_match and first.parts and first.parts[0][0] == "lit" and not first.parts[0][2]:
            self.assignment(simple, name_match.group(1), in_pipeline)
            return
        argv = [self.value(word, allow_status=self.value(simple.words[0]).text == "echo") for word in simple.words]
        program = argv[0].text
        _need(not argv[0].glob and not argv[0].tilde or program.startswith("~/"), "a globbed program name")
        is_build = self.is_build(argv)
        for redirect in simple.redirects:
            self.redirect(redirect, is_build, program)
        if simple.heredoc() is not None:
            _need(program == "python3", "a heredoc feeding something other than python3")
        if is_build:
            self.labels.append("owned build")
            return
        handler = _HANDLERS.get(program)
        if handler is None and program in GATE_SCRIPTS + tuple("./" + g for g in GATE_SCRIPTS):
            handler = _gate
        _need(handler is not None, f"{program!r} is not a command the Lean policy runs")
        if program == "cd":
            _need(not in_pipeline, "cd inside a pipeline")
        self.labels.append(handler(self, argv, simple))

    def assignment(self, simple: Simple, name: str, in_pipeline: bool) -> None:
        _need(len(simple.words) == 1 and not simple.redirects and not in_pipeline,
              "an environment prefix or an assignment with other words")
        _need(name not in _VAR_DENY and not name.startswith(_VAR_DENY_PREFIXES),
              f"assigning {name}, which can change how programs run")
        value = self.value(simple.words[0])
        text = value.text[len(name) + 1:]
        _need(not value.glob and not value.tilde and _VAR_VALUE.fullmatch(text) is not None,
              f"{name}= must be a plain path")
        self.readable(Value(text, False, False))
        self.bindings[name] = text
        self.labels.append(f"assign {name}")

    def redirect(self, redirect: Redirect, is_build: bool, program: str) -> None:
        if redirect.op == "dup":
            _need(redirect.dup in (1, 2) and redirect.fd in (None, 1, 2), "a redirection of another descriptor")
            return
        if redirect.op == "<<":
            _need(redirect.fd in (None, 0), "a heredoc on another descriptor")
            if not redirect.quoted:
                _need(not re.search(r"[$`\\]", redirect.body or ""), "an unquoted heredoc with $, ` or \\")
            return
        target = self.value(redirect.target)
        if redirect.op == "<":
            _need(redirect.fd in (None, 0), "an input redirection of another descriptor")
            self.readable(target)
            return
        _need(redirect.fd in (None, 1, 2), "a redirection of another descriptor")
        if target.text == "/dev/null":
            return
        if is_build and self.build_log_target(target):
            return
        self.writable(target)

    def is_build(self, argv: list[Value]) -> bool:
        if len(argv) < 2 or argv[1].text != "lake-build":
            return False
        if argv[0].text not in ("~/creme/scripts/creme", str(self.ctx.home / "creme/scripts/creme")):
            return False
        from .muse_client import build_command_failure
        joined = " ".join(value.text for value in argv)
        failure = build_command_failure(joined, self.ctx.goal, str(self.ctx.workspace), self.ctx.workspace)
        _need(failure is None, f"owned build: {failure}")
        return True


# ---------------------------------------------------------------------------
# Program rules (each returns a short label for the audit or raises Refusal)


def _options_and_operands(argv: list[Value]) -> tuple[list[str], list[Value]]:
    options, operands, done = [], [], False
    for value in argv[1:]:
        if not done and value.text == "--":
            done = True
        elif not done and value.text.startswith("-") and value.text != "-":
            options.append(value.text)
        else:
            operands.append(value)
    return options, operands


def _check_options(ev: _Evaluator, options: list[str]) -> None:
    for option in options:
        if option.startswith("--") and "=" in option:
            ev.readable(Value(option.split("=", 1)[1], False, False))
        elif not option.startswith("--"):
            _need("/" not in option and "~" not in option, f"{option!r}: a path glued to a short option")


def _read_tool(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    program = argv[0].text
    options, operands = _options_and_operands(argv)
    if program == "grep":
        _need(not any(o in ("-R", "--dereference-recursive") or (not o.startswith("--") and "R" in o)
                      for o in options), "grep -R follows symlinks out of the read roots")
    if program == "sort":
        _need(not any(o.startswith(("-o", "--output", "--compress-program", "-T", "--temporary-directory"))
                      or (not o.startswith("--") and ("o" in o or "T" in o)) for o in options),
              "sort with an output file, a temporary directory, or a compress program")
    if program == "uniq":
        _need(len(operands) <= 1, "uniq with an output operand")
    if program == "date":
        _need(not any(o.startswith(("-s", "--set")) for o in options), "date --set")
    if program == "tail":
        _need(not any(o in ("-f", "-F", "--follow") or o.startswith("--follow") for o in options), "tail -f")
    _check_options(ev, options)
    for operand in operands:
        ev.readable(operand)
    return f"read {program}"


def _inert(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    return argv[0].text


def _sleep(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    _need(len(argv) == 2 and re.fullmatch(r"\d{1,3}(?:\.\d+)?", argv[1].text) is not None
          and float(argv[1].text) <= 600, "sleep takes one duration of at most 600 seconds")
    return "sleep"


def _cd(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    _need(len(argv) == 2 and not argv[1].glob and argv[1].text not in ("-",), "cd takes one directory")
    ev.readable(argv[1])
    new = {Path(os.path.realpath(ev.resolve(argv[1], cwd))) for cwd in ev.cwds}
    ev.cwds |= new      # cd may fail: later commands may run in either directory
    return "cd"


def _mkdir(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    options, operands = _options_and_operands(argv)
    _need(all(option == "-p" for option in options) and operands, "mkdir takes -p and directories only")
    for operand in operands:
        ev.writable(operand)
    return "mkdir (workspace)"


_SED_SIMPLE = set("pPdDnN=qQlgGhHxz{}")


def _sed_script_failure(script: str) -> Optional[str]:
    """Accept a sed script made only of addresses and p/d/n/q/hold-space/s/y commands (no e, w, r)."""
    i, n = 0, len(script)

    def skip(j: int) -> int:
        while j < n and script[j] in " \t":
            j += 1
        return j

    def regex_end(j: int, delim: str) -> int:
        while j < n:
            if script[j] == "\\":
                j += 2
                continue
            if script[j] == delim:
                return j
            if script[j] == "\n":
                return -1
            j += 1
        return -1

    def address(j: int) -> int:
        number = re.compile(r"\d+(?:~\d+)?|\$").match(script, j)
        if number:
            return number.end()
        if j < n and script[j] in "/\\":
            delim = "/"
            if script[j] == "\\":
                if j + 1 >= n:
                    return -1
                delim, j = script[j + 1], j + 1
            end = regex_end(j + 1, delim)
            if end == -1:
                return -1
            j = end + 1
            while j < n and script[j] in "IM":
                j += 1
            return j
        return j

    while True:
        i = skip(i)
        if i >= n:
            return None
        if script[i] in ";\n":
            i += 1
            continue
        start = i
        i = address(i)
        if i == -1:
            return "an unterminated address"
        if i > start:
            i = skip(i)
            if i < n and script[i] == ",":
                i = skip(i + 1)
                second = re.compile(r"[+~]\d+").match(script, i)
                if second:
                    i = second.end()
                else:
                    after = address(i)
                    if after in (-1, i):
                        return "a malformed address range"
                    i = after
        i = skip(i)
        if i < n and script[i] == "!":
            i = skip(i + 1)
        if i >= n:
            return "an address without a command"
        command = script[i]
        if command in _SED_SIMPLE:
            i += 1
            if command in "qQ":
                exit_code = re.compile(r"\s*\d*").match(script, i)
                i = exit_code.end()
        elif command in "sy":
            if i + 1 >= n or script[i + 1] in "\\\n ":
                return f"a malformed {command} command"
            delim = script[i + 1]
            first = regex_end(i + 2, delim)
            if first == -1:
                return f"an unterminated {command} command"
            second = regex_end(first + 1, delim)
            if second == -1:
                return f"an unterminated {command} command"
            i = second + 1
            if command == "s":
                flags = re.compile(r"[gpiImM0-9]*").match(script, i)
                i = flags.end()
        else:
            return f"the sed command {command!r} (only p d n q = l g h x z s y and braces are allowed)"
        i = skip(i)
        if i < n and script[i] not in ";\n}":
            return f"unexpected text after a sed command: {script[i:i + 10]!r}"


def _sed(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    scripts: list[str] = []
    files: list[Value] = []
    in_place = False
    words = argv[1:]
    k = 0
    done = False
    while k < len(words):
        text = words[k].text
        if not done and text == "--":
            done = True
        elif not done and text == "-i":
            in_place = True
        elif not done and text in ("-e", "--expression"):
            _need(k + 1 < len(words), "sed -e without a script")
            scripts.append(words[k + 1].text)
            k += 1
        elif not done and text.startswith("--expression="):
            scripts.append(text.split("=", 1)[1])
        elif not done and text in ("--posix", "--regexp-extended", "--quiet", "--silent", "--separate",
                                   "--null-data", "--unbuffered", "--debug", "--sandbox"):
            pass
        elif not done and re.fullmatch(r"-[nErszu]+e?", text):
            if text.endswith("e"):
                _need(k + 1 < len(words), "sed -e without a script")
                scripts.append(words[k + 1].text)
                k += 1
        elif not done and text.startswith("-") and text != "-":
            raise Refusal(f"sed option {text!r}")
        elif not scripts and not any(w.text in ("-e", "--expression") for w in words[:k]) and not _has_e(words[:k]):
            scripts.append(text)
        else:
            files.append(words[k])
        k += 1
    _need(bool(scripts), "sed without a script")
    for script in scripts:
        failure = _sed_script_failure(script)
        _need(failure is None, f"sed script: {failure}")
    if in_place:
        _need(bool(files), "sed -i without files")
        for value in files:
            ev.writable(value)
        return "sed -i (workspace)"
    for value in files:
        ev.readable(value)
    return "sed"


def _has_e(words: list[Value]) -> bool:
    return any(re.fullmatch(r"-[nErszu]*e", w.text) or w.text.startswith("--expression") for w in words)


_AWK_FORBIDDEN = re.compile(r"\b(system|getline|close|fflush|ENVIRON|PROCINFO|nextfile)\b|@|\|(?!\|)|(?<!\|)\|")


def _awk_program_failure(program: str) -> Optional[str]:
    # Drop string literals and regex literals, then refuse calls that run commands or open files.
    stripped, i, n = [], 0, len(program)
    previous = ""
    while i < n:
        c = program[i]
        if c == '"':
            j = i + 1
            while j < n and program[j] != '"':
                j += 2 if program[j] == "\\" else 1
            if j >= n:
                return "an unterminated awk string"
            stripped.append('""')
            i = j + 1
            previous = '"'
            continue
        if c == "/" and previous in ("", "(", ",", "{", "}", ";", "!", "~", "&", "|", "\n", "?", ":"):
            j = i + 1
            in_class = False
            while j < n and (program[j] != "/" or in_class):
                if program[j] == "\\":
                    j += 2
                    continue
                if program[j] == "[":
                    in_class = True
                elif program[j] == "]":
                    in_class = False
                j += 1
            if j >= n:
                return "an unterminated awk regex"
            stripped.append("//")
            i = j + 1
            previous = "/"
            continue
        stripped.append(c)
        if c not in " \t":
            previous = c
        i += 1
    code = "".join(stripped)
    code_no_or = code.replace("||", "  ")
    if "|" in code_no_or:
        return "an awk pipe"
    if re.search(r"\b(system|getline|close|fflush|ENVIRON|PROCINFO)\b", code) or "@" in code:
        return "an awk call that runs a command, reads another file, or loads code"
    for statement in re.split(r"[;{}\n]", code):
        if re.match(r"\s*printf?\b", statement) and ">" in statement:
            return "an awk output redirection"
    return None


def _awk(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    words = argv[1:]
    k, program = 0, None
    while k < len(words):
        text = words[k].text
        if program is None and text in ("-v", "-F"):
            _need(k + 1 < len(words), f"awk {text} without a value")
            if text == "-v":
                _need(_NAME.match(words[k + 1].text.split("=", 1)[0]) is not None and "=" in words[k + 1].text,
                      "awk -v takes NAME=VALUE")
            k += 2
            continue
        if program is None and re.fullmatch(r"-F.+", text):
            k += 1
            continue
        if program is None and text == "--":
            k += 1
            continue
        if program is None:
            _need(not text.startswith("-"), f"awk option {text!r}")
            program = text
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", text, re.S):
            pass
        else:
            ev.readable(words[k])
        k += 1
    _need(program is not None, "awk without a program")
    failure = _awk_program_failure(program)
    _need(failure is None, f"awk program: {failure}")
    return "awk (restricted)"


_FIND_FORBIDDEN = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf",
                             "-fls", "-files0-from", "-L", "-follow", "-H"})


def _find(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    for value in argv[1:]:
        _need(value.text not in _FIND_FORBIDDEN, f"find {value.text}")
    for value in argv[1:]:
        if not value.text.startswith("-") and value.text not in ("(", ")", "!", ","):
            ev.readable(value)
    return "find"


def _git(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    k = 1
    directories: list[Value] = []
    while k < len(argv) and argv[k].text.startswith("-"):
        if argv[k].text == "-C":
            _need(k + 1 < len(argv), "git -C without a directory")
            directories.append(argv[k + 1])
            k += 2
        elif argv[k].text == "--no-pager":
            k += 1
        else:
            raise Refusal(f"git global option {argv[k].text!r}")
    _need(k < len(argv), "git without a verb")
    verb = argv[k].text
    _need(verb in GIT_READ_VERBS, f"git {verb} is not a read-only verb")
    if directories:
        _need(len(directories) == 1, "more than one git -C")
        _need(not directories[0].glob, "a glob in git -C")
        ev.readable(directories[0])
        starts = {Path(os.path.realpath(ev.resolve(directories[0], cwd))) for cwd in ev.cwds}
    else:
        starts = set(ev.cwds)
    for start in starts:
        failure = git_repository_failure(start, ev.ctx.workspace)
        _need(failure is None, failure or "")
    for value in argv[k + 1:]:
        text = value.text
        _need(not text.startswith(_GIT_FORBIDDEN_PREFIXES), f"git {verb} {text}")
        if verb in ("diff", "log", "show") and text.startswith("-o") and not text.startswith("--"):
            raise Refusal(f"git {verb} {text} writes a file")
        if text.startswith("--") and "=" in text:
            ev.readable(Value(text.split("=", 1)[1], False, False))
        elif text.startswith("-"):
            _need("/" not in text, f"{text!r}: a path glued to a short option")
        elif directories:
            # Paths are relative to the -C directory; a revision or pathspec must not leave it.
            base = {Path(os.path.realpath(ev.resolve(directories[0], cwd))) for cwd in ev.cwds}
            saved, ev.cwds = ev.cwds, base
            try:
                ev.readable(value)
            finally:
                ev.cwds = saved
        else:
            ev.readable(value)
    return f"git {verb}"


def git_repository_failure(start: Path, workspace: Path) -> Optional[str]:
    """Refuse a repository whose git directory the session could have written.

    Git runs programs named in its config (``core.fsmonitor``, ``core.pager``,
    ``diff.external``), so the repository git discovers from ``start`` must
    keep its git directory outside the workspace: a worktree's ``.git`` file
    must point outside it, and a ``.git`` directory must not be inside it.
    """
    directory = start
    workspace_text = str(workspace).rstrip("/") + "/"
    while True:
        dot_git = directory / ".git"
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return f"cannot read {dot_git}"
            match = re.fullmatch(r"gitdir: (.+)", text)
            if match is None:
                return f"{dot_git} is not a gitdir pointer"
            gitdir = os.path.realpath(os.path.join(str(directory), match.group(1)))
            if (gitdir + "/").startswith(workspace_text):
                return f"{dot_git} points inside the workspace"
            return None
        if dot_git.exists():
            real = os.path.realpath(dot_git)
            if (real + "/").startswith(workspace_text):
                return f"the git directory {dot_git} is inside the workspace"
            return None
        if directory.parent == directory:
            return None
        directory = directory.parent


def _python(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    texts = [value.text for value in argv]
    heredoc = simple.heredoc()
    if texts in (["python3", "scripts/check-assurance-manifest.py"],
                 ["python3", "-B", "scripts/check-assurance-manifest.py"]) and heredoc is None:
        return _gate(ev, argv[1:] if texts[1] != "-B" else argv[2:], simple)
    if len(texts) == 3 and texts[1] == "-c" and heredoc is None:
        code = texts[2]
        label = "python3 -c (restricted)"
    elif texts in (["python3", "-"], ["python3"]) and heredoc is not None:
        code = heredoc.body or ""
        label = "python3 heredoc (restricted)"
    else:
        raise Refusal("python3 other than -c CODE, a heredoc, or the assurance-manifest gate")
    for cwd in ev.cwds:
        failure = python_shadow_failure(cwd)
        _need(failure is None, failure or "")
    failure = python_code_failure(code, ev)
    _need(failure is None, f"python: {failure}")
    return label


def python_shadow_failure(directory: Path) -> Optional[str]:
    """Python puts the current directory first on ``sys.path``: refuse one that can shadow a module."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return f"cannot list {directory} to rule out shadowing Python modules"
    for entry in entries:
        name = entry.name
        if name.endswith((".py", ".pyc", ".pyo", ".so", ".pyd", ".pth")):
            return f"{directory} contains {name}, which python3 would import ahead of the standard library"
        if entry.is_dir() and (Path(entry.path) / "__init__.py").exists():
            return f"{directory} contains the package {name}, which python3 would import ahead of the standard library"
    return None


def python_code_failure(code: str, ev: _Evaluator) -> Optional[str]:
    """Accept Python that only computes, reads files inside the roots, and writes workspace files."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"does not parse ({exc.msg})"
    local: set[str] = set()
    constants: dict[str, list] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                if node.module not in PYTHON_MODULES or node.level:
                    return f"import from {node.module}"
                for alias in node.names:
                    if alias.name == "*" or alias.name.startswith("_"):
                        return f"from {node.module} import {alias.name}"
                    local.add(alias.asname or alias.name)
            else:
                for alias in node.names:
                    if alias.name not in PYTHON_MODULES:
                        return f"import {alias.name}"
                    local.add(alias.asname or alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            local.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            local.add(node.name)
        elif isinstance(node, ast.arg):
            local.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            local.update(node.names)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.comprehension, ast.With,
                               ast.NamedExpr)):
            for sub in ast.walk(node.target if not isinstance(node, ast.With) else ast.Tuple(
                    elts=[item.optional_vars for item in node.items if item.optional_vars], ctx=ast.Store())):
                if isinstance(sub, ast.Name):
                    constants.setdefault(sub.id, []).append(None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in ast.walk(node.args):
                if isinstance(arg, ast.arg):
                    constants.setdefault(arg.arg, []).append(None)
    if "open" in local:
        return "rebinding open"
    open_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith, ast.Await)):
            return "async code"
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return f"the attribute {node.attr}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            open_calls.add(id(node.func))
            failure = _open_failure(node, constants, ev)
            if failure:
                return failure
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id == "open":
                if id(node) not in open_calls:
                    return "open used other than as a direct call"
            elif node.id not in local and node.id not in PYTHON_BUILTINS:
                return f"the name {node.id}"
            if node.id.startswith("__"):
                return f"the name {node.id}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("__") \
                and node.value.endswith("__"):
            return f"the dunder string {node.value!r}"
    return None


def _string_constant(node: ast.AST, constants: dict[str, list]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        values = constants.get(node.id) or []
        if len(values) == 1 and isinstance(values[0], ast.Constant) and isinstance(values[0].value, str):
            return values[0].value
    return None


def _open_failure(call: ast.Call, constants: dict[str, list], ev: _Evaluator) -> Optional[str]:
    if not call.args or len(call.args) > 2:
        return "open() without a path, or with positional arguments past the mode"
    path = _string_constant(call.args[0], constants)
    if path is None:
        return "open() of a path that is not a string constant"
    mode_node = call.args[1] if len(call.args) == 2 else None
    for keyword in call.keywords:
        if keyword.arg == "mode" and mode_node is None:
            mode_node = keyword.value
        elif keyword.arg not in ("encoding", "errors", "newline") or not isinstance(keyword.value, ast.Constant):
            return f"open(..., {keyword.arg}=...)"
    mode = "r" if mode_node is None else _string_constant(mode_node, constants)
    if mode is None or not re.fullmatch(r"[rwaxbt+]+", mode):
        return "open() with a mode that is not a string constant"
    value = Value(path, False, False)
    try:
        if set(mode) & set("wax+"):
            _need(not path.startswith(("/", "~")), f"open({path!r}, {mode!r}): writes take a relative path")
            ev.writable(value)
        else:
            ev.readable(value)
    except Refusal as exc:
        return str(exc)
    return None


def _gate(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    texts = [value.text for value in argv]
    if texts and texts[0] == "bash":
        texts = texts[1:]
    _need(len(texts) == 1, "a gate script with arguments")
    script = texts[0][2:] if texts[0].startswith("./") else texts[0]
    _need(script in GATE_SCRIPTS, f"{texts[0]} is not one of the static gate scripts")
    for cwd in ev.cwds:
        _need(Path(os.path.realpath(cwd / script)) == ev.ctx.workspace / script,
              f"{script} is not the workspace's own gate")
    failure = ev.ctx.scripts_check(ev.ctx.workspace)
    _need(failure is None, f"gate {script}: {failure}")
    failure = python_shadow_failure(ev.ctx.workspace)
    _need(failure is None, f"gate {script}: {failure}")
    return f"gate {script}"


def _bash(ev: _Evaluator, argv: list[Value], simple: Simple) -> str:
    return _gate(ev, argv, simple)


_HANDLERS: dict[str, Callable[[_Evaluator, list[Value], Simple], str]] = {
    **{name: _read_tool for name in READ_TOOLS},
    **{name: _inert for name in INERT_TOOLS},
    "sleep": _sleep, "cd": _cd, "mkdir": _mkdir, "sed": _sed, "awk": _awk, "find": _find, "git": _git,
    "python3": _python, "bash": _bash,
}


# ---------------------------------------------------------------------------
# Entry point


_LOG_IDIOM = re.compile(r"\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)=\$\(ls -t (?P<dir>\S+)/(?P<label>[A-Za-z0-9_.-]+)-\*\.log"
                        r" \| head -1\)\s*(?:;|&&)")


def _stages_failure(stages: Any, simples: list[Simple], used_variables: bool) -> Optional[str]:
    if not isinstance(stages, list):
        return "Muse sent no parsed stages"
    if not stages:
        return None if used_variables else "Muse sent no parsed stages for a command without variables"
    candidates = [simples, [s for s in simples if not _is_assignment(s)]]
    for mine in candidates:
        if len(mine) != len(stages):
            continue
        for stage, simple in zip(stages, mine):
            argv = stage.get("argv") if isinstance(stage, dict) else None
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                return "a Muse stage without an argv"
            ours = [word.compare() for word in simple.words]
            theirs = [a.replace("\\", "") for a in argv]
            if stage.get("argvComplete") is True and simple.heredoc() is None:
                if ours != theirs:
                    return f"Muse parsed {argv[:3]} where the policy parsed {ours[:3]}"
            elif ours[0] != theirs[0]:
                return f"Muse parsed program {argv[0]!r} where the policy parsed {ours[0]!r}"
        return None
    return f"Muse parsed {len(stages)} stage(s) where the policy parsed {len(simples)}"


def _is_assignment(simple: Simple) -> bool:
    return len(simple.words) == 1 and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", simple.words[0].compare()) is not None


def evaluate(command: str, stages: Any, ctx: PolicyContext) -> tuple[bool, str]:
    """``(True, reason)`` when the policy approves the whole command, else ``(False, why not)``."""
    try:
        logvar = None
        rest = command
        idiom = _LOG_IDIOM.match(command)
        if idiom:
            directory = os.path.realpath(os.path.expanduser(idiom.group("dir")))
            _need(Path(directory) == ctx.build_log_dir and idiom.group("label") == ctx.goal,
                  "the newest-build-log idiom names another directory or label")
            logvar = idiom.group("var")
            rest = command[idiom.end():]
        statements = _parse(rest)
        simples = [simple for _, pipeline in statements for simple in pipeline]
        used_variables = logvar is not None or any(
            word.has_vars() and any(p[0] == "var" for p in word.parts) for simple in simples for word in simple.words)
        failure = _stages_failure(stages, simples, used_variables)
        _need(failure is None, failure or "")
        evaluator = _Evaluator(ctx, logvar)
        if logvar is not None:
            evaluator.labels.append(f"newest own build log into {logvar}")
        for _, pipeline in statements:
            evaluator.statement(pipeline)
    except Refusal as exc:
        return False, str(exc)
    labels = list(dict.fromkeys(evaluator.labels))
    return True, f"{POLICY_NAME}: " + ", ".join(labels)
