"""``python3 -m creme attribution``: private commit attribution commands."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import attribution as attr

ROOT = Path(__file__).resolve().parents[1]


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _location():
    from . import master_operations

    return master_operations.resolve_runtime_location(ROOT)


def _host() -> Optional[str]:
    from . import goal_stack_store

    return goal_stack_store.local_host_name()


def _repos(location, names: Optional[list] = None) -> dict:
    repos = attr.repositories(location)
    if names:
        unknown = sorted(set(names) - set(repos))
        if unknown:
            raise attr.AttributionError(f"unknown repositories {unknown}; one of {sorted(repos)}")
        repos = {name: repos[name] for name in names}
    return repos


def _refused(detail: str, status: str = "refused") -> int:
    _json({"status": status, "detail": detail})
    return 2


def _guarded(function):
    def run(arguments: argparse.Namespace) -> int:
        from . import master_operations, master_runtime

        try:
            return function(arguments)
        except (attr.AttributionError, master_operations.MasterOperationError,
                master_runtime.MasterRecordError, OSError, ValueError) as exc:
            return _refused(str(exc))
    return run


def _identity(lease) -> dict:
    from . import master_runtime as runtime

    actor = runtime._actor_from_snapshot(lease)
    return {"client": actor["client"], "acquisition_digest": actor["acquisition_digest"],
            "lease_state": "live"}


@_guarded
def cmd_install(arguments: argparse.Namespace) -> int:
    from . import semaphore

    location = _location()
    shared_root = semaphore.canonical_creme_root(ROOT)
    script = Path(arguments.script).resolve() if arguments.script else \
        shared_root / "creme" / "attribution_hook.py"
    if not script.is_file():
        return _refused(f"hook entry point {script} is missing")
    repos = {k: v for k, v in _repos(location, arguments.repo).items() if v.is_dir()}
    result = attr.install(repos, python=arguments.python, script=script,
                          state_dir=semaphore.state_root().resolve(),
                          profile=shared_root / ".creme" / "host-profile.json")
    _json(result)
    return 0


@_guarded
def cmd_uninstall(arguments: argparse.Namespace) -> int:
    _json(attr.uninstall({k: v for k, v in _repos(_location(), arguments.repo).items()
                          if v.is_dir()}))
    return 0


@_guarded
def cmd_status(arguments: argparse.Namespace) -> int:
    repos = _repos(_location())
    view = {name: (attr.install_status(path) if path.is_dir() else {"status": "missing"})
            for name, path in repos.items()}
    if arguments.json:
        _json({"status": "OK", "repositories": view})
    else:
        for name, row in view.items():
            print(f"{name:10} {row['status']:10} {row.get('detail', '')} "
                  f"adopted={row.get('adopted_at')}")
    return 0


@_guarded
def cmd_publish(arguments: argparse.Namespace) -> int:
    location = _location()
    result = attr.publish(location.goal_store,
                          {k: v for k, v in attr.repositories(location).items() if v.is_dir()},
                          host=_host(), record_root=location.record_root,
                          public_repos=attr.public_repositories(location))
    _json(result)
    return 0


def _current_session_start(location) -> Optional[float]:
    from . import master_runtime, semaphore

    try:
        digest = master_runtime._actor_from_snapshot(semaphore.master_snapshot())["acquisition_digest"]
        started = attr.session_started_at(master_runtime.read_record(location.record_root).events,
                                          digest)
    except Exception:
        return None
    return attr.parse_time(started) if started else None


@_guarded
def cmd_census(arguments: argparse.Namespace) -> int:
    location = _location()
    since = attr.parse_time(arguments.since) if arguments.since else _current_session_start(location)
    if since is None:
        return _refused("no live master session to take the window from; pass --since")
    until = attr.parse_time(arguments.until) if arguments.until else time.time()
    repos = {k: v for k, v in attr.repositories(location).items() if v.is_dir()}
    rows = attr.census(repos, since, until)
    _json({"status": "OK", "since": attr.iso(since), "until": attr.iso(until),
           "unattributed": len(rows), "census": rows})
    return 0


@_guarded
def cmd_merge_check(arguments: argparse.Namespace) -> int:
    location = _location()
    repos = {k: v for k, v in attr.repositories(location).items() if v.is_dir()}

    def publish_first() -> dict:
        return attr.publish(location.goal_store, repos, host=_host(),
                            record_root=location.record_root,
                            public_repos=attr.public_repositories(location))

    result = attr.merge_check(repos, arguments.repo, arguments.range, publish_first=publish_first)
    _json(result)
    return 2 if result["status"] == "refused" else 0


@_guarded
def cmd_adopt(arguments: argparse.Namespace) -> int:
    from . import git_channel as channels

    location = _location()
    repos = attr.repositories(location)

    def action(lease) -> dict:
        return attr.adopt(repos, arguments.repo, arguments.sha, reason=arguments.reason,
                          host=_host(), identity=_identity(lease))

    _json(channels.authorized(location.record_root, None, action, attr._spec(), pass_lease=True))
    return 0


@_guarded
def cmd_report(arguments: argparse.Namespace) -> int:
    location = _location()
    host = _host()
    repos = {k: v for k, v in attr.repositories(location).items() if v.is_dir()}
    ledger = attr.read_ledger(location.goal_store, host, fetch=not arguments.no_fetch) \
        if host else attr.Ledger(attached=False)
    local = []
    for repo in repos.values():
        local.extend(dict(entry.record, host=entry.record.get("host") or host)
                     for entry in attr.journal(repo) if not entry.published)
    filters = attr.Filters(
        session=arguments.session, host=arguments.host, goal=arguments.goal,
        since=attr.parse_time(arguments.since) if arguments.since else None,
        until=attr.parse_time(arguments.until) if arguments.until else None)
    report = attr.build_report(ledger, repos, filters, local=local)
    if arguments.json:
        _json(report)
    else:
        sys.stdout.write(attr.render_report(report) or "no attributed commits match\n")
    return 0


@_guarded
def cmd_privacy_check(arguments: argparse.Namespace) -> int:
    location = _location()
    repos = attr.repositories(location)
    target = repos.get(arguments.repo) or Path(arguments.repo).expanduser()
    if not target.is_dir():
        return _refused(f"{arguments.repo!r} is neither one of {sorted(repos)} nor a directory")
    digests = attr.known_digests(repos, location.goal_store)
    hits = attr.privacy_check(target, arguments.base, host=_host(),
                              digests=digests, head=arguments.head)
    _json({"status": "flagged" if hits else "OK", "hits": hits,
           "known_digests": len(digests)})
    return 2 if hits else 0


def register(commands) -> None:
    root = commands.add_parser(
        "attribution",
        help="private commit attribution: which master session made each commit",
    )
    sub = root.add_subparsers(dest="attribution_action", required=True)

    install = sub.add_parser("install", help="install the recording hooks (refuses to displace any hook)")
    install.add_argument("--repo", action="append", metavar="NAME")
    install.add_argument("--python", metavar="PATH", help="interpreter the hooks run (default: this one)")
    install.add_argument("--script", metavar="PATH", help=argparse.SUPPRESS)
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser("uninstall", help="remove only hooks this installer wrote")
    uninstall.add_argument("--repo", action="append", metavar="NAME")
    uninstall.set_defaults(func=cmd_uninstall)

    status = sub.add_parser("status", help="hook install state of the configured repositories")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    publish = sub.add_parser("publish", help="publish the journal to the private ledger (master only)")
    publish.set_defaults(func=cmd_publish)

    census = sub.add_parser("census", help="locally created commits in a window with no record")
    census.add_argument("--since", metavar="TIME", help="default: the live master session's start")
    census.add_argument("--until", metavar="TIME")
    census.set_defaults(func=cmd_census)

    check = sub.add_parser(
        "merge-check", help="refuse unrecorded commits created here (run before a default-branch merge)")
    check.add_argument("--repo", required=True, metavar="NAME")
    check.add_argument("range", metavar="RANGE", help="e.g. origin/main..CANDIDATE")
    check.set_defaults(func=cmd_merge_check)

    adopt = sub.add_parser("adopt", help="record commits the hooks missed, with a reason (master only)")
    adopt.add_argument("--repo", required=True, metavar="NAME")
    adopt.add_argument("sha", nargs="+", metavar="SHA")
    adopt.add_argument("--reason", required=True, metavar="TEXT")
    adopt.set_defaults(func=cmd_adopt)

    report = sub.add_parser("report", help="commits by session, host, goal or time window")
    report.add_argument("--session", metavar="DIGEST", help="acquisition digest or a prefix")
    report.add_argument("--host", metavar="HOST")
    report.add_argument("--goal", metavar="ID")
    report.add_argument("--since", metavar="TIME")
    report.add_argument("--until", metavar="TIME")
    report.add_argument("--no-fetch", action="store_true", help="read this host's ledger copy only")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    privacy = sub.add_parser("privacy-check",
                             help="flag host names, digests or session fields in BASE..HEAD")
    privacy.add_argument("--repo", required=True, metavar="NAME|PATH",
                         help="a configured repository or a worktree path")
    privacy.add_argument("--base", required=True, metavar="REV")
    privacy.add_argument("--head", default="HEAD", metavar="REV")
    privacy.set_defaults(func=cmd_privacy_check)
