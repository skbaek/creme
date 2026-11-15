"""Private commit attribution: which master session made each commit.

Capture (:mod:`creme.attribution_hook`) writes one host-local journal record
per new commit in the Git common directory of each configured repository.
This module installs those hooks, publishes the journal to a private ledger,
finds commits the hooks missed (the census), refuses unrecorded commits before
a default-branch merge, and reports by session, host, goal or time window.

The ledger is a channel-only branch (default ``attribution``) of the goal
store's origin, checked out at ``$GOAL_STORE/.attribution`` and ignored in
that clone, exactly like host comms (:mod:`creme.git_channel`). Each host
writes only under ``hosts/<host>/``, every file once:

- ``hosts/<host>/batches/<stamp>-<8 hex>.json``: published journal records;
- ``hosts/<host>/sessions/<digest>/start-<stamp>.json`` and
  ``end-<stamp>.json``: client, model, effort, start and end of one master
  acquisition, the end carrying the census of unattributed commits.

Privacy: attribution data lives only in the host-local journal and on the
private goal store's ledger branch. Publication refuses a remote that is a
public repository's, and nothing here writes to a tracked file of Creme,
Jaune or Blanc.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import attribution_hook as hook
from . import git_channel as channels, goal_stack_git as shared

LEDGER_DIR = ".attribution"
DEFAULT_BRANCH = "attribution"
DEFAULT_REMOTE = shared.DEFAULT_REMOTE
LOCK_NAME = "creme-attribution.lock"
HOSTS = "hosts"
BATCHES = "batches"
SESSIONS = "sessions"
MAX_BATCH = 2000
MAX_LEDGER_FILE = 8 * 1024 * 1024
HOOK_MARKER = "# creme-attribution-hook v1"
SCRIPT = Path(__file__).resolve().parent / "attribution_hook.py"
PUBLIC_ORIGINS = frozenset({
    "github.com/skbaek/creme", "github.com/skbaek/jaune", "github.com/skbaek/blanc",
})
CHANNEL_BRANCHES = (DEFAULT_BRANCH,)
CREATION = re.compile(
    r"^(commit( \((amend|initial|merge)\))?|cherry-pick|revert|am)\b"
    r"|^(pull --rebase|rebase( -i| -m| \(-i\))?)[^:]*\((pick|reword|squash|fixup|edit|continue)\):"
    r"|Merge made by"
)
SESSION_FIELD = re.compile(
    r"(acquisition_digest|session_digest|liveness_digest|lease_id)[\"']?\s*[:=]\s*[\"']?[0-9a-f]{32,64}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
README = (
    "# Commit attribution ledger\n\n"
    "Which master session made each commit, published by each host under\n"
    "`hosts/<host>/`. Private: never copy anything here into a public\n"
    "repository. Never edit or delete a file; use `python3 -m creme attribution`.\n"
)


class AttributionError(ValueError):
    """An attribution operation refused."""


def _spec() -> channels.Spec:
    return channels.Spec(directory=LEDGER_DIR, branch=DEFAULT_BRANCH, lock_name=LOCK_NAME,
                         readme=README.encode(), opening="attribution: open the ledger",
                         remedy=f"move them out of {LEDGER_DIR}/ (only `attribution publish` writes)",
                         error=AttributionError, remote=DEFAULT_REMOTE)


def iso(seconds: float) -> str:
    return hook.iso(seconds)


def parse_time(text: str) -> float:
    """``2026-11-15``, ``2026-11-15T10:00:00Z`` or a master-event timestamp."""
    value = text.strip()
    for form in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d"):
        try:
            return _utc_epoch(value, form)
        except ValueError:
            continue
    raise AttributionError(f"time {text!r} is not YYYY-MM-DD[THH:MM[:SS]Z]")


def _utc_epoch(value: str, form: str) -> float:
    from datetime import datetime, timezone

    return datetime.strptime(value, form).replace(tzinfo=timezone.utc).timestamp()


def _git(repo: Path, *args: str, stdin: Optional[bytes] = None,
         check: bool = True) -> subprocess.CompletedProcess:
    result = shared.git(Path(repo), *args, check=False, input_bytes=stdin)
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise AttributionError(f"git {' '.join(args[:2])} in {repo}: {detail}")
    return result


def _out(repo: Path, *args: str, stdin: Optional[bytes] = None) -> str:
    return _git(repo, *args, stdin=stdin).stdout.decode("utf-8", "replace")


def common_dir(repo: Path) -> Path:
    value = Path(_out(repo, "rev-parse", "--git-common-dir").strip())
    return (value if value.is_absolute() else Path(repo) / value).resolve()


def journal_root(repo: Path) -> Path:
    return common_dir(repo) / hook.JOURNAL_ROOT


# -- install --------------------------------------------------------------------


def render_hook(name: str, *, python: str, script: Path, repository: str, state_dir: Path,
                profile: Path) -> str:
    """The shell hook: run the recorder, discard its output, always exit 0."""
    command = " ".join(shlex.quote(part) for part in (
        str(script), "--repo", repository, "--state-dir", str(state_dir),
        "--profile", str(profile), name))
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        "# Written by `python3 -m creme attribution install`; it records the new\n"
        "# commit in this repository's host-local journal, never changes the\n"
        "# commit, never uses the network, and always exits 0. Never edit it.\n"
        f"py={shlex.quote(python)}\n"
        "[ -x \"$py\" ] || py=python3\n"
        f"\"$py\" -I -S {command} \"$@\" >/dev/null 2>&1\n"
        "exit 0\n"
    )


def _hooks_dir(repo: Path) -> Path:
    return common_dir(repo) / "hooks"


def _hooks_path_setting(repo: Path) -> Optional[str]:
    result = _git(repo, "config", "--get", "core.hooksPath", check=False)
    value = result.stdout.decode("utf-8", "replace").strip()
    return value or None


def _ours(path: Path) -> bool:
    try:
        return HOOK_MARKER in path.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
    except OSError:
        return False


def install_status(repo: Path) -> dict:
    """``installed``, ``absent``, ``partial``, ``foreign`` or ``hooks-path`` for one repo."""
    try:
        hooks_dir = _hooks_dir(repo)
    except (AttributionError, shared.SharedStackError) as exc:
        return {"status": "unavailable", "detail": str(exc)}
    setting = _hooks_path_setting(repo)
    ours = [name for name in hook.HOOKS if (hooks_dir / name).is_file() and _ours(hooks_dir / name)]
    foreign = [name for name in hook.HOOKS
               if (hooks_dir / name).exists() and name not in ours]
    adopted = _installed_at(repo)
    view = {"hooks": ours, "foreign": foreign,
            "adopted_at": iso(adopted) if adopted is not None else None}
    if setting is not None:
        return {**view, "status": "hooks-path", "detail": f"core.hooksPath={setting}"}
    if foreign:
        return {**view, "status": "foreign", "detail": f"repository-owned hooks: {foreign}"}
    if len(ours) == len(hook.HOOKS):
        return {**view, "status": "installed", "detail": "all hooks installed"}
    if ours:
        return {**view, "status": "partial", "detail": f"only {ours}"}
    return {**view, "status": "absent", "detail": "no attribution hooks"}


def install(repos: dict, *, python: Optional[str] = None, script: Path = SCRIPT,
            state_dir: Path, profile: Path, now: Optional[float] = None) -> dict:
    """Install the hooks in every repository, or refuse and change nothing.

    Refuses when ``core.hooksPath`` is set or any hook it would write exists
    and was not written by this installer (a repository-owned hook).
    """
    python = python or sys.executable
    moment = time.time() if now is None else now
    refusals = []
    for name, repo in repos.items():
        setting = _hooks_path_setting(repo)
        if setting is not None:
            refusals.append(f"{name}: core.hooksPath is set ({setting}); not displacing it")
            continue
        hooks_dir = _hooks_dir(repo)
        for item in hook.HOOKS:
            path = hooks_dir / item
            if (path.exists() or path.is_symlink()) and not (path.is_file() and _ours(path)):
                refusals.append(f"{name}: {item} exists and was not written by this installer")
    if refusals:
        raise AttributionError("install refused; nothing changed: " + "; ".join(refusals))
    installed = {}
    for name, repo in repos.items():
        hooks_dir = _hooks_dir(repo)
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for item in hook.HOOKS:
            text = render_hook(item, python=python, script=script, repository=name,
                               state_dir=state_dir, profile=profile)
            temporary = hooks_dir / f".{item}.creme-{secrets.token_hex(4)}"
            temporary.write_text(text, encoding="utf-8")
            temporary.chmod(0o755)
            os.replace(temporary, hooks_dir / item)
        root = journal_root(repo)
        hook.ensure_dir(str(root))
        marker = root / hook.INSTALLED
        if not marker.exists():
            hook.write_new(str(marker), (json.dumps(
                {"schema": hook.SCHEMA, "repository": name, "installed_at": iso(moment)},
                sort_keys=True) + "\n").encode())
        installed[name] = install_status(repo)
    return {"status": "OK", "repositories": installed}


def uninstall(repos: dict) -> dict:
    """Remove only hooks this installer wrote; adoption time is kept."""
    removed = {}
    for name, repo in repos.items():
        hooks_dir = _hooks_dir(repo)
        removed[name] = []
        for item in hook.HOOKS:
            path = hooks_dir / item
            if path.is_file() and _ours(path):
                path.unlink()
                removed[name].append(item)
    return {"status": "OK", "removed": removed}


def _installed_at(repo: Path) -> Optional[float]:
    try:
        data = json.loads((journal_root(repo) / hook.INSTALLED).read_text(encoding="utf-8"))
        return parse_time(data["installed_at"])
    except (OSError, ValueError, KeyError, TypeError, AttributionError,
            shared.SharedStackError):
        return None


# -- journal ----------------------------------------------------------------------


def valid_record(record: Any) -> bool:
    return (isinstance(record, dict) and record.get("schema") == hook.SCHEMA
            and isinstance(record.get("id"), str) and isinstance(record.get("sha"), str)
            and SHA_RE.fullmatch(record["sha"]) is not None
            and isinstance(record.get("repository"), str)
            and isinstance(record.get("acquisition_digest"), str)
            and (record["acquisition_digest"] == hook.NONE
                 or DIGEST_RE.fullmatch(record["acquisition_digest"]) is not None))


@dataclass
class JournalEntry:
    path: Path
    record: dict
    published: bool


def journal(repo: Path) -> list[JournalEntry]:
    """Every readable journal record of one repository (published or not)."""
    root = journal_root(repo)
    entries = []
    for folder, published in ((hook.JOURNAL, False), (hook.PUBLISHED, True)):
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if valid_record(record) and record["id"] == path.stem:
                entries.append(JournalEntry(path, record, published))
    return entries


def recorded_shas(repo: Path) -> set[str]:
    return {entry.record["sha"] for entry in journal(repo)}


def adopt(repos: dict, repository: str, shas: Iterable[str], *, reason: str,
          host: Optional[str], identity: dict, now: Optional[float] = None) -> dict:
    """Record commits the hooks missed, with a reason and ``source: census``."""
    if repository not in repos:
        raise AttributionError(f"unknown repository {repository!r}; one of {sorted(repos)}")
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason:
        raise AttributionError("--reason must be one nonempty line")
    repo = Path(repos[repository])
    moment = time.time() if now is None else now
    where = hook.context(str(repo))
    written = []
    for sha in shas:
        resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
        if resolved.returncode:
            raise AttributionError(f"{sha} is not a commit in {repository}")
        full = resolved.stdout.decode().strip()
        record = hook.build_record(cwd=str(repo), sha=full, hook="adopt", repository=repository,
                                   host=host, identity=identity, where=where, now=moment,
                                   source="census", extra={"reason": reason.strip()})
        hook.write_record(where["common"], record)
        written.append({"sha": full, "id": record["id"]})
    return {"status": "OK", "repository": repository, "adopted": written}


# -- census -----------------------------------------------------------------------


def _reflog_files(common: Path) -> list[Path]:
    files = []
    if (common / "logs" / "HEAD").is_file():
        files.append(common / "logs" / "HEAD")
    heads = common / "logs" / "refs" / "heads"
    if heads.is_dir():
        files.extend(path for path in sorted(heads.rglob("*"))
                     if path.is_file() and path.relative_to(heads).as_posix() not in CHANNEL_BRANCHES)
    worktrees = common / "worktrees"
    if worktrees.is_dir():
        for admin in sorted(worktrees.iterdir()):
            try:
                checkout = Path((admin / "gitdir").read_text(encoding="utf-8").strip()).parent
            except OSError:
                checkout = None
            if checkout is not None and checkout.name == LEDGER_DIR:
                continue
            if (admin / "logs" / "HEAD").is_file():
                files.append(admin / "logs" / "HEAD")
    return files


def creations(repo: Path, since: float, until: float) -> dict[str, dict]:
    """Commits this clone created in [since, until], from its reflogs.

    Reflog entries that create a commit (commit, amend, merge commit,
    cherry-pick, revert, am, rebase picks) are kept; checkouts, resets and
    fast-forwards are not. The attribution ledger's own branch is skipped.
    """
    common = common_dir(repo)
    if (common / "reftable").exists():
        raise AttributionError(f"{repo} uses the reftable backend; the census reads files reflogs")
    found: dict[str, dict] = {}
    for path in _reflog_files(common):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            head, _, message = line.partition("\t")
            parts = head.split()
            if len(parts) < 4:
                continue
            new = parts[1]
            try:
                moment = float(parts[-2])
            except ValueError:
                continue
            if not since <= moment <= until or SHA_RE.fullmatch(new) is None or new == "0" * 40:
                continue
            if CREATION.search(message) is None:
                continue
            if new not in found or moment < found[new]["at"]:
                found[new] = {"at": moment, "reflog": message[:120],
                              "log": path.relative_to(common).as_posix()}
    return found


def census(repos: dict, since: float, until: float) -> list[dict]:
    """Locally created commits in the window with no journal record."""
    missing = []
    for name, repo in repos.items():
        recorded = recorded_shas(Path(repo))
        for sha, seen in sorted(creations(Path(repo), since, until).items(),
                                key=lambda item: item[1]["at"]):
            if sha not in recorded:
                missing.append({"repository": name, "sha": sha, "created_at": iso(seen["at"]),
                                "reflog": seen["reflog"], "status": "unattributed"})
    return missing


# -- ledger -----------------------------------------------------------------------


def _normalize(url: str, base: Path) -> str:
    from .doctor import _normalize_origin

    value = url.strip()
    if "://" not in value and not re.match(r"^[^/]+@[^:]+:", value) and not re.match(r"^[^/:]+:", value):
        candidate = Path(value).expanduser()
        candidate = candidate if candidate.is_absolute() else base / candidate
        return str(candidate.resolve())
    if value.startswith("file://"):
        return str(Path(value[len("file://"):]).resolve())
    return _normalize_origin(value)


def _remote_urls(repo: Path) -> list[str]:
    result = _git(repo, "config", "--get-regexp", r"^remote\..*\.(url|pushurl)$", check=False)
    return [line.split(" ", 1)[1] for line in result.stdout.decode("utf-8", "replace").splitlines()
            if " " in line]


def require_private_remote(goal_store: Path, remote: str, public_repos: Iterable[Path]) -> str:
    """Refuse to publish to any remote a public repository uses."""
    own = _git(goal_store, "remote", "get-url", "--push", remote, check=False)
    if own.returncode:
        raise AttributionError(f"goal store has no remote {remote!r}; nothing published")
    target = _normalize(own.stdout.decode().strip(), goal_store)
    public = set(PUBLIC_ORIGINS)
    for repo in public_repos:
        try:
            public.update(_normalize(url, Path(repo)) for url in _remote_urls(Path(repo)))
        except (AttributionError, shared.SharedStackError):
            continue
    if target in public:
        raise AttributionError(
            f"remote {remote!r} of the goal store is a public repository's remote; "
            "attribution data stays on the private goal store only; nothing published")
    return target


@dataclass
class Ledger:
    records: list = field(default_factory=list)
    sessions: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    attached: bool = True


def _ledger_files(worktree: Path) -> Iterable[tuple[str, Path]]:
    base = worktree / HOSTS
    if not base.is_dir() or base.is_symlink():
        return
    for path in sorted(base.rglob("*")):
        if path.is_file() or path.is_symlink():
            yield path.relative_to(worktree).as_posix(), path


def load_ledger(worktree: Path) -> Ledger:
    """Parse a ledger copy; malformed files become problems, never crashes."""
    ledger = Ledger()
    for relative, path in _ledger_files(worktree):
        parts = relative.split("/")
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LEDGER_FILE:
                raise AttributionError("not a regular file of bounded size")
            data = json.loads(path.read_text(encoding="utf-8"))
            host = parts[1]
            if len(parts) == 4 and parts[2] == BATCHES:
                if not isinstance(data, dict) or data.get("host") != host:
                    raise AttributionError("batch host does not match its path")
                for record in data.get("records", []):
                    if not valid_record(record) or record.get("host") != host:
                        raise AttributionError("batch holds a malformed or foreign record")
                ledger.records.extend(data["records"])
            elif len(parts) == 5 and parts[2] == SESSIONS:
                kind = parts[4].split("-", 1)[0]
                if (not isinstance(data, dict) or data.get("host") != host
                        or data.get("acquisition_digest") != parts[3]
                        or kind not in {"start", "end"}):
                    raise AttributionError("session file does not match its path")
                session = ledger.sessions.setdefault((host, parts[3]), {
                    "host": host, "acquisition_digest": parts[3]})
                for key in ("client", "model", "effort", "started_at"):
                    if data.get(key) is not None and session.get(key) is None:
                        session[key] = data[key]
                if kind == "end":
                    session["ended_at"] = max(session.get("ended_at") or "", data.get("ended_at") or "")
                    session.setdefault("census", []).extend(data.get("census", []))
            else:
                raise AttributionError("unexpected ledger path")
        except (OSError, ValueError, AttributionError, AttributeError, TypeError) as exc:
            ledger.problems.append((relative, str(exc)))
    return ledger


def read_ledger(goal_store: Path, host: str, *, remote: str = DEFAULT_REMOTE,
                branch: str = DEFAULT_BRANCH, fetch: bool = True) -> Ledger:
    """Fetch and read the ledger without acquiring the master lease."""
    spec = _spec()
    try:
        with channels.lock(goal_store, spec):
            worktree = channels.attach(goal_store, spec, create=False, host=host,
                                       remote=remote, branch=branch) if fetch else \
                (channels.worktree_path(goal_store, spec)
                 if channels.worktree_path(goal_store, spec).is_dir() else None)
            if worktree is None:
                return Ledger(attached=False)
            if fetch:
                channels.sync(worktree, spec)
            problems = channels.changed_paths(worktree)
            ledger = load_ledger(worktree)
    except shared.SharedStackError as exc:
        raise AttributionError(str(exc)) from exc
    ledger.problems = problems + ledger.problems
    return ledger


def _stamp(moment: float) -> str:
    return time.strftime(hook.STAMP, time.gmtime(moment))


def _dump(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")


def publish(goal_store: Path, repos: dict, *, host: str, record_root: Path, writer=None,
            public_repos: Iterable[Path] = (), session: Optional[dict] = None,
            now: Optional[float] = None, remote: str = DEFAULT_REMOTE,
            branch: str = DEFAULT_BRANCH) -> dict:
    """Publish every unpublished journal record (and a session file) to the ledger.

    Master only. Includes any journal a dead predecessor left. Records reach
    the ledger or stay in the journal; a journal file moves to ``published/``
    only after its batch is on the origin.
    """
    if hook.HOST_RE.fullmatch(host or "") is None:
        raise AttributionError("this host has no valid name; attribution refuses unnamed hosts "
                               "(python3 -m creme set-host-name NAME)")
    require_private_remote(goal_store, remote, public_repos)
    spec = _spec()
    moment = time.time() if now is None else now

    def action(lease) -> dict:
        from . import master_runtime as runtime

        digest = runtime._actor_from_snapshot(lease)["acquisition_digest"]
        with channels.lock(goal_store, spec):
            worktree = channels.attach(goal_store, spec, create=True, host=host,
                                       remote=remote, branch=branch)
            channels.sync(worktree, spec)
            present = {record["id"] for record in load_ledger(worktree).records
                       if record.get("host") == host}
            pending: list[JournalEntry] = []
            for repo in repos.values():
                pending.extend(entry for entry in journal(Path(repo)) if not entry.published)
            files: dict[str, bytes] = {}
            fresh = []
            for entry in pending:
                if entry.record["id"] in present:
                    continue
                if entry.record.get("host") not in (None, host):
                    raise AttributionError(f"journal record {entry.record['id']} names host "
                                           f"{entry.record.get('host')!r}, not {host!r}")
                fresh.append({**entry.record, "host": host})
            for index in range(0, len(fresh), MAX_BATCH):
                relative = f"{HOSTS}/{host}/{BATCHES}/{_stamp(moment)}-{secrets.token_hex(4)}.json"
                files[relative] = _dump({"schema": hook.SCHEMA, "host": host,
                                         "published_at": iso(moment),
                                         "records": fresh[index:index + MAX_BATCH]})
            session_path = None
            if session is not None:
                session_path = (f"{HOSTS}/{host}/{SESSIONS}/{digest}/"
                                f"{session['kind']}-{_stamp(moment)}-{secrets.token_hex(2)}.json")
                files[session_path] = _dump({**session, "acquisition_digest": digest,
                                             "host": host, "schema": hook.SCHEMA})
            commit = None
            if files:
                commit = channels.publish(
                    worktree, files,
                    f"attribution: {host} publishes {len(fresh)} record(s)"
                    + (f" and a session {session['kind']}" if session else ""), spec)
            for entry in pending:
                target = entry.path.parent.parent / hook.PUBLISHED
                hook.ensure_dir(str(target))
                try:
                    os.replace(entry.path, target / entry.path.name)
                except OSError:
                    pass  # the next publication skips it by id
            return {"status": "OK", "published": len(fresh), "commit": commit,
                    "session_file": session_path, "host": host}

    try:
        return channels.authorized(record_root, writer, action, spec, pass_lease=True)
    except shared.SharedStackError as exc:
        raise AttributionError(str(exc)) from exc


def session_record(kind: str, *, client: str, model: str, effort: str,
                   started_at: Optional[str], ended_at: Optional[str] = None,
                   census_rows: Optional[list] = None) -> dict:
    """A session start or end; publication binds it to the current acquisition."""
    record = {"kind": kind, "client": client, "model": model,
              "effort": effort, "started_at": started_at}
    if kind == "end":
        record["ended_at"] = ended_at
        record["census"] = census_rows or []
        record["unattributed"] = len(census_rows or [])
    return record


# -- merge check ------------------------------------------------------------------


def merge_check(repos: dict, repository: str, rev_range: str, *, publish_first, now=None) -> dict:
    """Refuse commits in ``rev_range`` created here since adoption without a record.

    ``publish_first`` publishes the journal (it raises on failure, which
    refuses the check). Commits created on other hosts or before adoption
    are outside the claim and pass.
    """
    if repository not in repos:
        raise AttributionError(f"unknown repository {repository!r}; one of {sorted(repos)}")
    repo = Path(repos[repository])
    adopted = _installed_at(repo)
    if adopted is None:
        return {"status": "not-adopted", "repository": repository,
                "detail": "attribution is not adopted in this repository on this host"}
    published = publish_first()
    listed = _out(repo, "rev-list", rev_range).split()
    moment = time.time() if now is None else now
    created = creations(repo, adopted, moment + 1)
    recorded = recorded_shas(repo)
    refused = [{"sha": sha, "created_at": iso(created[sha]["at"]), "reflog": created[sha]["reflog"]}
               for sha in listed if sha in created and sha not in recorded]
    result = {"repository": repository, "range": rev_range, "commits": len(listed),
              "created_here": sum(1 for sha in listed if sha in created),
              "published": published}
    if refused:
        return {**result, "status": "refused", "unrecorded": refused,
                "remedy": ("python3 -m creme attribution adopt --repo "
                           f"{repository} SHA... --reason TEXT")}
    return {**result, "status": "OK"}


# -- report -----------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    session: Optional[str] = None
    host: Optional[str] = None
    goal: Optional[str] = None
    since: Optional[float] = None
    until: Optional[float] = None


def _subjects(repo: Path, shas: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(shas))
    if not wanted:
        return {}
    result = _git(repo, "log", "--no-walk=unsorted", "--ignore-missing", "--stdin",
                  "--format=%H%x1f%s", stdin=("\n".join(wanted) + "\n").encode(), check=False)
    found = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        sha, _, subject = line.partition("\x1f")
        found[sha] = subject
    return found


def _window_commits(repo: Path, since: float, until: float) -> list[tuple[str, float]]:
    excludes = []
    for branch in CHANNEL_BRANCHES:
        excludes += [f"--exclude=refs/heads/{branch}", f"--exclude=refs/remotes/*/{branch}"]
    result = _git(repo, "log", "--single-worktree", *excludes, "--all",
                  f"--since=@{int(since)}", f"--until=@{int(until) + 1}", "--format=%H %ct",
                  check=False)
    rows = []
    for line in result.stdout.decode().splitlines():
        sha, _, moment = line.partition(" ")
        if SHA_RE.fullmatch(sha):
            rows.append((sha, float(moment)))
    return rows


def _patch_id(repo: Path, sha: str) -> Optional[str]:
    try:
        return hook.patch_id(str(repo), sha)
    except Exception:
        return None


def build_report(ledger: Ledger, repos: dict, filters: Filters, *, local: Iterable[dict] = (),
                 now: Optional[float] = None) -> dict:
    """Group ledger records (plus local unpublished ones) by session, repository, goal.

    Subjects come from local Git at report time. When the report has a time
    window (given, or a session's start and end), commits in the window that
    no host recorded are listed as ``unattributed`` (or matched by patch-id).
    """
    moment = time.time() if now is None else now
    published_ids = {record["id"] for record in ledger.records}
    records = list(ledger.records) + [dict(record, unpublished=True) for record in local
                                      if record["id"] not in published_ids]
    seen: dict[tuple, dict] = {}
    for record in sorted(records, key=lambda r: (r.get("recorded_at", ""), r["id"])):
        seen.setdefault((record.get("host"), record["repository"], record["sha"]), record)
    records = list(seen.values())
    sessions = ledger.sessions
    since, until = filters.since, filters.until
    if filters.session:
        matches = {key for key in {(r.get("host"), r["acquisition_digest"]) for r in records}
                   | set(sessions) if key[1].startswith(filters.session)}
        if not matches:
            raise AttributionError(f"no session matches {filters.session!r}")
        times = []
        for key in matches:
            meta = sessions.get(key, {})
            times += [parse_time(meta[k]) for k in ("started_at", "ended_at") if meta.get(k)]
            times += [parse_time(r["recorded_at"]) for r in records
                      if (r.get("host"), r["acquisition_digest"]) == key]
        if times and since is None:
            since = min(times)
        if times and until is None:
            ended = all(sessions.get(key, {}).get("ended_at") for key in matches)
            until = max(times) if ended else moment

    def selected(record: dict) -> bool:
        if filters.session and not record["acquisition_digest"].startswith(filters.session):
            return False
        if filters.host and record.get("host") != filters.host:
            return False
        if filters.goal and record.get("goal") != filters.goal:
            return False
        at = parse_time(record["recorded_at"]) if record.get("recorded_at") else None
        if since is not None and (at is None or at < since):
            return False
        if until is not None and (at is None or at > until):
            return False
        return True

    chosen = [record for record in records if selected(record)]
    unattributed: list[dict] = []
    if since is not None and until is not None and not filters.goal:
        known = {(r["repository"], r["sha"]) for r in records}
        by_patch: dict[str, dict] = {}
        for record in records:
            if record.get("patch_id"):
                by_patch.setdefault(record["patch_id"], record)
        for name, repo in repos.items():
            for sha, at in _window_commits(Path(repo), since, until):
                if (name, sha) in known:
                    continue
                pid = _patch_id(Path(repo), sha) if by_patch else None
                origin = by_patch.get(pid) if pid else None
                if origin is not None and selected(origin):
                    chosen.append({**origin, "sha": sha, "repository": name,
                                   "matched": "patch-id", "matched_from": origin["sha"]})
                    known.add((name, sha))
                elif origin is None:
                    unattributed.append({"repository": name, "sha": sha,
                                         "committed_at": iso(at), "status": "unattributed"})
    subjects: dict[tuple, str] = {}
    for name, repo in repos.items():
        shas = [r["sha"] for r in chosen if r["repository"] == name] + \
               [u["sha"] for u in unattributed if u["repository"] == name]
        for sha, subject in _subjects(Path(repo), shas).items():
            subjects[(name, sha)] = subject
    grouped: dict[tuple, dict] = {}
    for record in sorted(chosen, key=lambda r: (r.get("recorded_at", ""), r["sha"])):
        key = (record.get("host"), record["acquisition_digest"])
        meta = sessions.get(key, {})
        session = grouped.setdefault(key, {
            "host": key[0], "acquisition_digest": key[1],
            "client": meta.get("client") or record.get("client"),
            "model": meta.get("model"), "effort": meta.get("effort"),
            "started_at": meta.get("started_at"), "ended_at": meta.get("ended_at"),
            "repositories": {}})
        goals = session["repositories"].setdefault(record["repository"], {})
        row = {"sha": record["sha"],
               "subject": subjects.get((record["repository"], record["sha"]),
                                       "(not in the local clone)"),
               "recorded_at": record.get("recorded_at"), "hook": record.get("hook"),
               "source": record.get("source")}
        for extra in ("matched", "matched_from", "unpublished", "reason", "lease_state"):
            if record.get(extra) is not None:
                row[extra] = record[extra]
        goals.setdefault(record.get("goal") or "(none)", []).append(row)
    for row in unattributed:
        row["subject"] = subjects.get((row["repository"], row["sha"]), "(not in the local clone)")
    census_rows = [dict(row, host=key[0]) for key, meta in sessions.items()
                   if any(key == s for s in grouped) for row in meta.get("census", [])]
    return {
        "schema": 1,
        "filters": {"session": filters.session, "host": filters.host, "goal": filters.goal,
                    "since": iso(since) if since is not None else None,
                    "until": iso(until) if until is not None else None},
        "sessions": sorted(grouped.values(), key=lambda s: (s.get("started_at") or "", s["host"] or "",
                                                            s["acquisition_digest"])),
        "unattributed": unattributed,
        "session_census": census_rows,
        "problems": [{"path": p, "reason": r} for p, r in ledger.problems],
    }


def render_report(report: dict) -> str:
    lines = []
    for session in report["sessions"]:
        digest = session["acquisition_digest"]
        lines.append(
            f"session {digest[:12]} host={session['host']} client={session.get('client')} "
            f"model={session.get('model')} effort={session.get('effort')} "
            f"start={session.get('started_at')} end={session.get('ended_at')}")
        for repository, goals in sorted(session["repositories"].items()):
            for goal, rows in sorted(goals.items()):
                lines.append(f"  {repository} / {goal}: {len(rows)} commit(s)")
                for row in rows:
                    flags = "".join(f" [{flag}]" for flag in ("matched", "unpublished")
                                    if row.get(flag))
                    lines.append(f"    {row['sha'][:12]} {row['subject']}{flags}")
    if report["unattributed"]:
        lines.append(f"unattributed: {len(report['unattributed'])} commit(s)")
        for row in report["unattributed"]:
            lines.append(f"  {row['repository']} {row['sha'][:12]} {row['committed_at']} {row['subject']}")
    for problem in report["problems"]:
        lines.append(f"problem {problem['path']}: {problem['reason']}")
    return "\n".join(lines) + ("\n" if lines else "")


# -- privacy ----------------------------------------------------------------------


def known_digests(repos: dict, goal_store: Optional[Path] = None) -> set[str]:
    """Acquisition digests this host knows from journals and its ledger copy."""
    digests = set()
    for repo in repos.values():
        try:
            digests.update(e.record["acquisition_digest"] for e in journal(Path(repo)))
        except (AttributionError, shared.SharedStackError):
            continue
    if goal_store is not None and (Path(goal_store) / LEDGER_DIR).is_dir():
        ledger = load_ledger(Path(goal_store) / LEDGER_DIR)
        digests.update(r["acquisition_digest"] for r in ledger.records)
        digests.update(key[1] for key in ledger.sessions)
    digests.discard(hook.NONE)
    return digests


def _word(host: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(host) + r"(?![A-Za-z0-9_-])")


TEST_TREE = "scripts/tests/"


def privacy_check(repo: Path, base: str, *, host: Optional[str], digests: Iterable[str],
                  head: str = "HEAD") -> list[dict]:
    """Private attribution data in ``base..head``: added lines and commit metadata.

    Flags this host's name as a word, any known acquisition digest, and a
    session field carrying a hex value. Test fixtures (``scripts/tests/``)
    may use synthetic host names and identifiers, so only known digests are
    flagged there.
    """
    digests = sorted(set(digests))
    word = _word(host) if host else None
    hits = []

    def scan(where: str, text: str, fixture: bool) -> None:
        for digest in digests:
            if digest in text:
                hits.append({"where": where, "rule": "acquisition digest"})
        if fixture:
            return
        if word is not None and word.search(text):
            hits.append({"where": where, "rule": "host name"})
        if SESSION_FIELD.search(text):
            hits.append({"where": where, "rule": "session field"})

    diff = _out(repo, "diff", "--no-color", "--unified=0", "--no-ext-diff", f"{base}..{head}")
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and current is not None:
            scan(f"diff {current}", line[1:], current.startswith(TEST_TREE))
    log = _out(repo, "log", "--format=%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e", f"{base}..{head}")
    for entry in log.split("\x1e"):
        fields = entry.strip("\n").split("\x1f")
        if len(fields) == 4:
            sha, author, committer, body = fields
            scan(f"commit {sha[:12]} author", author, False)
            scan(f"commit {sha[:12]} committer", committer, False)
            scan(f"commit {sha[:12]} message", body, False)
    return hits


def tracked_digest_hits(repo: Path, digests: Iterable[str]) -> list[str]:
    """Tracked files of ``repo`` containing any known acquisition digest."""
    patterns = sorted(set(digests))
    if not patterns:
        return []
    args = ["grep", "-l", "-F", "-I"]
    for digest in patterns:
        args += ["-e", digest]
    result = _git(repo, *args, check=False)
    return sorted(result.stdout.decode("utf-8", "replace").split())


# -- master integration -------------------------------------------------------------


def repositories(location) -> dict:
    """The four configured repositories, by name, from a master runtime location."""
    return {name: Path(path) for name, path in location.repository_roots}


def public_repositories(location) -> list:
    return [Path(path) for name, path in location.repository_roots if name != "goal-store"]


def adopted(repos: dict) -> bool:
    return any(_installed_at(Path(repo)) is not None for repo in repos.values()
               if Path(repo).is_dir())


def _local_host(host: Optional[str]) -> Optional[str]:
    if host is not None:
        return host
    from . import goal_stack_store

    return goal_stack_store.local_host_name()


def on_master_start(location, *, client: str, model: str, effort: str, writer=None,
                    host: Optional[str] = None, now: Optional[float] = None) -> dict:
    """At ``master start``: publish the session start and any journal left behind."""
    repos = {k: v for k, v in repositories(location).items() if v.is_dir()}
    if not adopted(repos):
        return {"status": "not-adopted"}
    moment = time.time() if now is None else now
    return publish(location.goal_store, repos, host=_local_host(host),
                   record_root=location.record_root, writer=writer,
                   public_repos=public_repositories(location),
                   session=session_record("start", client=client, model=model, effort=effort,
                                          started_at=iso(moment)), now=moment)


def session_started_at(events: Iterable[dict], digest: str) -> Optional[str]:
    """The earliest master-event timestamp of one acquisition."""
    stamps = [event["timestamp"] for event in events
              if isinstance(event, dict) and event.get("actor", {}).get("acquisition_digest") == digest]
    return min(stamps) if stamps else None


def on_master_end(location, *, digest: str, client: str, model: str, effort: str,
                  events: Iterable[dict], writer=None, host: Optional[str] = None,
                  now: Optional[float] = None) -> dict:
    """At ``master end``: census the session window, then publish the end record."""
    repos = {k: v for k, v in repositories(location).items() if v.is_dir()}
    if not adopted(repos):
        return {"status": "not-adopted"}
    moment = time.time() if now is None else now
    started = session_started_at(events, digest)
    since = parse_time(started) if started else moment
    rows = census(repos, since, moment)
    result = publish(location.goal_store, repos, host=_local_host(host),
                     record_root=location.record_root, writer=writer,
                     public_repos=public_repositories(location),
                     session=session_record("end", client=client, model=model, effort=effort,
                                            started_at=started, ended_at=iso(moment),
                                            census_rows=rows), now=moment)
    return {**result, "unattributed": len(rows), "census": rows}
