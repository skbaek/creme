"""Command-line surface of ``python3 -m creme muse`` (registered by ``creme.cli``)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from . import muse as M
from . import muse_broker as MB
from .muse_client import EFFORTS

ROOT = Path(__file__).resolve().parents[1]


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _read_brief(value: str) -> tuple[Optional[str], Optional[str]]:
    if value == "-":
        return sys.stdin.read(), None
    try:
        return Path(value).expanduser().read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read brief: {exc}"


def _print(arguments: argparse.Namespace, code: int, lines: list[str], record: Any) -> int:
    if getattr(arguments, "json", False):
        print(json.dumps({"exit_code": code, **(record or {})}, indent=2, sort_keys=True, default=str))
    else:
        for line in lines:
            print(line)
    if code == M.EXIT_PIN_FAILED:
        print(M.STOP_MESSAGE, file=sys.stderr)
    return code


def _refused(arguments: argparse.Namespace, reason: str) -> int:
    return _print(arguments, M.EXIT_PREFLIGHT_REFUSED,
                  [f"verdict=REFUSED exit={M.EXIT_PREFLIGHT_REFUSED}", f"refused: {reason}"], {})


def cmd_status(arguments: argparse.Namespace) -> int:
    code, report = M.status(ROOT)
    return _print(arguments, code, M.format_status(report).splitlines(), report)


def cmd_run(arguments: argparse.Namespace) -> int:
    brief, error = _read_brief(arguments.brief)
    if error:
        return _refused(arguments, error)
    request = M.RunRequest(brief=brief, target=Path(arguments.target).expanduser(),
                           mode="write" if arguments.write else "read-only", effort=arguments.effort,
                           timeout_seconds=arguments.timeout_seconds, lean_goal=arguments.lean)
    code, record = M.run(ROOT, request)
    return _print(arguments, code, M.format_run(record).splitlines(), record)


def cmd_start(arguments: argparse.Namespace) -> int:
    brief, error = _read_brief(arguments.brief)
    if error:
        return _refused(arguments, error)
    fit = None
    if getattr(arguments, "episode", None):
        from . import model_fit, model_fit_adapters
        directory = Path(arguments.fit_dir).expanduser() if arguments.fit_dir else model_fit.default_dir(ROOT)
        fit = model_fit_adapters.binding(directory, arguments.episode)
    code, lines, record = MB.cmd_start(ROOT, dict(os.environ), brief, arguments.target, arguments.write,
                                       arguments.effort, arguments.detail, arguments.timeout_seconds,
                                       lean=arguments.lean, model_fit=fit)
    return _print(arguments, code, lines, record)


def cmd_resume(arguments: argparse.Namespace) -> int:
    code, lines, record = MB.cmd_resume(ROOT, dict(os.environ), arguments.muse_session, arguments.target,
                                        arguments.write, arguments.effort, arguments.detail,
                                        arguments.timeout_seconds, lean=arguments.lean)
    return _print(arguments, code, lines, record)


def cmd_send(arguments: argparse.Namespace) -> int:
    if arguments.text is not None:
        text = arguments.text
    else:
        text, error = _read_brief(arguments.brief or "-")
        if error:
            return _refused(arguments, error)
    code, lines, record = MB.cmd_simple(ROOT, dict(os.environ), "send", arguments.session, text=text,
                                        steer=arguments.muse_action == "steer")
    return _print(arguments, code, lines, record)


def cmd_session_op(arguments: argparse.Namespace) -> int:
    extra = {}
    if arguments.muse_action == "approve":
        extra = {"approval": arguments.approval, "decision": arguments.decision}
    code, lines, record = MB.cmd_simple(ROOT, dict(os.environ), arguments.muse_action, arguments.session, **extra)
    return _print(arguments, code, lines, record)


def cmd_detail(arguments: argparse.Namespace) -> int:
    code, lines, record = MB.cmd_detail(ROOT, dict(os.environ), arguments.session, arguments.level)
    return _print(arguments, code, lines, record)


def cmd_wait(arguments: argparse.Namespace) -> int:
    code, lines, record = MB.cmd_wait(ROOT, dict(os.environ), arguments.session, arguments.timeout)
    return _print(arguments, code, lines, record)


def cmd_events(arguments: argparse.Namespace) -> int:
    def emit(line: str) -> None:
        print(line, flush=True)

    return MB.cmd_events(ROOT, dict(os.environ), arguments.session, arguments.follow, arguments.last,
                         arguments.since, emit, timeout=arguments.timeout)


def cmd_read(arguments: argparse.Namespace) -> int:
    code, lines, record = MB.cmd_read(ROOT, dict(os.environ), arguments.session, arguments.lines)
    return _print(arguments, code, lines, record)


def cmd_sessions(arguments: argparse.Namespace) -> int:
    code, lines, record = MB.cmd_sessions(ROOT, dict(os.environ), arguments.limit)
    return _print(arguments, code, lines, record)


def cmd_shutdown(arguments: argparse.Namespace) -> int:
    code, lines, record = MB.cmd_shutdown(ROOT, dict(os.environ))
    return _print(arguments, code, lines, record)


def cmd_clear_tripwire(arguments: argparse.Namespace) -> int:
    code, lines, record = MB.cmd_clear_tripwire(ROOT, dict(os.environ), arguments.reason)
    return _print(arguments, code, lines, record)


def cmd_broker_serve(arguments: argparse.Namespace) -> int:
    return MB.serve_main(ROOT, arguments.instance)


def register(commands: Any) -> None:
    muse = commands.add_parser("muse", help="guarded, steerable Muse (muse-spark-1.3) pseudo-subagent sessions")
    actions = muse.add_subparsers(dest="muse_action", required=True)

    def sub(name: str, text: str) -> argparse.ArgumentParser:
        item = actions.add_parser(name, help=text)
        item.add_argument("--json", action="store_true", help="print the full JSON record")
        return item

    status = sub("status", "binary, pinned model, usage windows, and postures; zero model tokens")
    status.set_defaults(func=cmd_status)

    run = sub("run", "run one bounded brief as one turn")
    run.add_argument("--brief", required=True, help="brief file, or - for stdin")
    run.add_argument("--target", required=True)
    run.add_argument("--write", action="store_true", help="writes confined to the target (sandboxed)")
    run.add_argument("--lean", metavar="GOAL", help=argparse.SUPPRESS)  # refused: Lean work is brokered only
    run.add_argument("--effort", choices=EFFORTS, default=M.DEFAULT_EFFORT)
    run.add_argument("--timeout-seconds", type=_positive, default=M.DEFAULT_TIMEOUT_SECONDS)
    run.set_defaults(func=cmd_run)

    for name, text in (("start", "open a brokered session with a first brief"),
                       ("resume", "attach a new brokered session to a recorded Muse session")):
        item = sub(name, text)
        if name == "start":
            item.add_argument("--episode", help="predeclared model-fit episode; automatic run capture")
            item.add_argument("--fit-dir", help="model-fit directory; defaults to configured goal store")
            item.add_argument("--brief", required=True, help="brief file, or - for stdin")
            item.add_argument("--target", required=True)
        else:
            item.add_argument("muse_session", help="the recorded Muse session id")
            item.add_argument("--target")
        mode = item.add_mutually_exclusive_group()
        mode.add_argument("--write", action="store_true")
        mode.add_argument("--lean", metavar="GOAL")
        item.add_argument("--effort", choices=EFFORTS, default=M.DEFAULT_EFFORT if name == "start" else None)
        item.add_argument("--detail", choices=("silent", "summary", "live"), default="silent")
        item.add_argument("--timeout-seconds", type=_positive, default=M.DEFAULT_TIMEOUT_SECONDS,
                          help="per-turn bound; the broker interrupts a turn that outlives it")
        item.set_defaults(func=cmd_start if name == "start" else cmd_resume)

    for name, text in (("send", "new turn if idle, steer if running"), ("steer", "steer the running turn only")):
        item = sub(name, text)
        item.add_argument("session")
        source = item.add_mutually_exclusive_group(required=True)
        source.add_argument("--text")
        source.add_argument("--brief")
        item.set_defaults(func=cmd_send)

    for name, text in (("interrupt", "interrupt the running turn"), ("stop", "stop the session (records kept)")):
        item = sub(name, text)
        item.add_argument("session")
        item.set_defaults(func=cmd_session_op)

    approve = sub("approve", "answer an approval the allowlist left to the master")
    approve.add_argument("session")
    approve.add_argument("approval")
    approve.add_argument("decision", choices=MB.DECISIONS)
    approve.set_defaults(func=cmd_session_op)

    wait = sub("wait", "block until idle, attention, or end")
    wait.add_argument("session")
    wait.add_argument("--timeout", type=float, default=3600)
    wait.set_defaults(func=cmd_wait)

    events = actions.add_parser("events", help="print the session's filtered event feed")
    events.add_argument("session")
    events.add_argument("--follow", action="store_true")
    events.add_argument("--last", type=_positive, default=20)
    events.add_argument("--since", type=int, default=0)
    events.add_argument("--timeout", type=float)
    events.set_defaults(func=cmd_events)

    read = sub("read", "print the latest final message")
    read.add_argument("session")
    read.add_argument("--lines", type=_positive, default=60)
    read.set_defaults(func=cmd_read)

    detail = sub("detail", "change the event detail level")
    detail.add_argument("session")
    detail.add_argument("level", choices=("silent", "summary", "live"))
    detail.set_defaults(func=cmd_detail)

    for name in ("sessions", "list"):
        item = sub(name, "list recorded sessions")
        item.add_argument("--limit", type=_positive, default=10)
        item.set_defaults(func=cmd_sessions)

    clear = sub("clear-tripwire", "master only: clear MODEL_PIN_FAILURE with a recorded reason")
    clear.add_argument("--reason", required=True)
    clear.set_defaults(func=cmd_clear_tripwire)

    shutdown = sub("shutdown", "stop every session and the broker")
    shutdown.set_defaults(func=cmd_shutdown)

    serve = actions.add_parser("broker-serve", help="internal: run the broker (started by clients)")
    serve.add_argument("--instance", required=True)
    serve.set_defaults(func=cmd_broker_serve)
