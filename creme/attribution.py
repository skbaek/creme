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
- ``hosts/<host>/sessions/<digest>/<start|end>-<stamp>-<4 hex>.json``:
  client, model, effort, start and end of one master acquisition, the end
  carrying the census of unattributed commits.

The journal is the source of truth until publication is confirmed: a record
or session file moves to ``published/`` only after the fetched origin holds
it, and a local ledger commit the origin lacks is discarded and rebuilt.

Privacy: attribution data lives only in the host-local journal and on the
private goal store's ledger branch. Publication pushes only to the goal
store's own origin, named explicitly, and refuses when any push URL differs
from it or when it is any public repository's URL. Nothing here writes to a
tracked file of Creme, Jaune or Blanc.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import attribution_hook as hook
from . import git_channel as channels, git_urls, goal_stack_git as shared

LEDGER_DIR = ".attribution"
DEFAULT_BRANCH = "attribution"
DEFAULT_REMOTE = shared.DEFAULT_REMOTE
LOCK_NAME = "creme-attribution.lock"
HOSTS = "hosts"
BATCHES = "batches"
SESSIONS = "sessions"
CENSUS = "census"
QUARANTINE = "quarantine"
PENDING = "pending"
RETIRED_PREFIX = "retired-"
UNKNOWN = "unknown"
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
# Reflog entries that move HEAD to an existing commit, though their command
# name looks like a creation: ``cherry-pick --ff``, ``am --abort`` and kin.
NOT_CREATION = re.compile(r"fast-forward|--abort|--quit|--skip", re.IGNORECASE)
SESSION_FIELD = re.compile(
    r"(acquisition_digest|session_digest|liveness_digest|lease_id)[\"']?\s*[:=]\s*[\"']?[0-9a-f]{32,64}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
SESSION_ID_RE = re.compile(r"(start|end)-\d{8}T\d{6}Z-[0-9a-f]{4,8}")
README = (
    "# Commit attribution ledger\n\n"
    "Which master session made each commit, published by each host under\n"
    "`hosts/<host>/`. Private: never copy anything here into a public\n"
    "repository. Never edit or delete a file; use `python3 -m creme attribution`.\n"
)


class AttributionError(ValueError):
    """An attribution operation refused."""


def _untracked_ok(path: str) -> bool:
    """Stray files outside ``hosts/`` (or Finder litter) never block publication."""
    return not path.startswith(HOSTS + "/") or Path(path).name == ".DS_Store"


def _spec(remote: str = DEFAULT_REMOTE, tracking: Optional[str] = None) -> channels.Spec:
    """The ledger channel; ``remote`` may be a validated literal URL named ``tracking``."""
    return channels.Spec(directory=LEDGER_DIR, branch=DEFAULT_BRANCH, lock_name=LOCK_NAME,
                         readme=README.encode(), opening="attribution: open the ledger",
                         remedy=f"move them out of {LEDGER_DIR}/{HOSTS}/ (only `attribution publish` writes)",
                         error=AttributionError, remote=remote, explicit_remote=True,
                         tracking_remote=tracking,
                         untracked_ok=_untracked_ok, extra_excludes=(".DS_Store",))


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


def _write_json_once(path: Path, value: Any) -> bool:
    """Create ``path`` (0600, missing parents 0700) unless it exists; True if written."""
    hook.ensure_dir(str(path.parent))
    try:
        hook.write_new(str(path), (json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))
    except FileExistsError:
        return False
    return True


# -- install and adoption -----------------------------------------------------------


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


def _worktree_hooks_paths(repo: Path) -> list[str]:
    """Worktrees whose own ``config.worktree`` sets ``core.hooksPath``."""
    listed = _git(repo, "worktree", "list", "--porcelain", check=False).stdout.decode(
        "utf-8", "replace")
    found = []
    for line in listed.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):])
        if not path.is_dir():
            continue
        value = _git(path, "config", "--worktree", "--get", "core.hooksPath", check=False)
        text = value.stdout.decode("utf-8", "replace").strip()
        if value.returncode == 0 and text:
            found.append(f"{path.name}: core.hooksPath={text}")
    return found


def _ours(path: Path) -> bool:
    try:
        return HOOK_MARKER in path.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
    except OSError:
        return False


def _our_hooks(repo: Path) -> list[str]:
    hooks_dir = _hooks_dir(repo)
    return [name for name in hook.HOOKS if (hooks_dir / name).is_file() and _ours(hooks_dir / name)]


def _installed_at(repo: Path) -> Optional[float]:
    try:
        data = json.loads((journal_root(repo) / hook.INSTALLED).read_text(encoding="utf-8"))
        return parse_time(data["installed_at"])
    except (OSError, ValueError, KeyError, TypeError, AttributionError,
            shared.SharedStackError):
        return None


def adoption(repo: Path) -> tuple[str, Optional[float]]:
    """``adopted`` (with its time), ``unmarked`` or ``not-adopted``.

    ``unmarked``: this installer's hooks are present but the adoption marker
    is gone; every check that relies on adoption refuses until reinstalled.
    """
    try:
        adopted = _installed_at(repo)
        if adopted is not None:
            return "adopted", adopted
        return ("unmarked", None) if _our_hooks(repo) else ("not-adopted", None)
    except (AttributionError, shared.SharedStackError):
        return "not-adopted", None


def install_status(repo: Path) -> dict:
    """Install state of one repository, for ``status`` and ``doctor``.

    ``installed``, ``absent``, ``partial``, ``foreign``, ``hooks-path`` or
    ``unmarked`` (hooks without the adoption marker).
    """
    try:
        hooks_dir = _hooks_dir(repo)
    except (AttributionError, shared.SharedStackError) as exc:
        return {"status": "unavailable", "detail": str(exc)}
    setting = _hooks_path_setting(repo)
    ours = _our_hooks(repo)
    foreign = [name for name in hook.HOOKS
               if (hooks_dir / name).exists() and name not in ours]
    state, adopted = adoption(repo)
    view = {"hooks": ours, "foreign": foreign, "adoption": state,
            "adopted_at": iso(adopted) if adopted is not None else None,
            "worktree_hooks_paths": _worktree_hooks_paths(repo)}
    if setting is not None:
        return {**view, "status": "hooks-path", "detail": f"core.hooksPath={setting}"}
    if foreign:
        return {**view, "status": "foreign", "detail": f"repository-owned hooks: {foreign}"}
    if ours and state == "unmarked":
        return {**view, "status": "unmarked",
                "detail": "hooks present but the adoption marker is missing; "
                          "run `attribution install` to restore it"}
    if len(ours) == len(hook.HOOKS):
        return {**view, "status": "installed", "detail": "all hooks installed"}
    if ours:
        return {**view, "status": "partial", "detail": f"only {ours}"}
    return {**view, "status": "absent", "detail": "no attribution hooks"}


def _evidence_time(repo: Path) -> Optional[float]:
    """Earliest sign of an earlier adoption: our hook files or journal records."""
    times = []
    for name in _our_hooks(repo):
        try:
            times.append((_hooks_dir(repo) / name).stat().st_mtime)
        except OSError:
            continue
    for entry in journal(repo):
        try:
            times.append(parse_time(entry.record["recorded_at"]))
        except (KeyError, AttributionError):
            continue
    return min(times) if times else None


def install(repos: dict, *, python: Optional[str] = None, script: Path = SCRIPT,
            state_dir: Path, profile: Path, now: Optional[float] = None) -> dict:
    """Install the hooks in every repository, or refuse and change nothing.

    Refuses when ``core.hooksPath`` is set or any hook it would write exists
    and was not written by this installer (a repository-owned hook). An
    existing adoption time is never reset: a missing marker is restored from
    the earliest evidence of the earlier installation.
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
        root = journal_root(repo)
        hook.ensure_dir(str(root))
        marker = root / hook.INSTALLED
        if not marker.exists():
            earlier = _evidence_time(repo)
            adopted_at = min(moment, earlier) if earlier is not None else moment
            hook.write_new(str(marker), (json.dumps(
                {"schema": hook.SCHEMA, "repository": name, "installed_at": iso(adopted_at)},
                sort_keys=True) + "\n").encode())
        hooks_dir = _hooks_dir(repo)
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for item in hook.HOOKS:
            text = render_hook(item, python=python, script=script, repository=name,
                               state_dir=state_dir, profile=profile)
            temporary = hooks_dir / f".{item}.creme-{secrets.token_hex(4)}"
            temporary.write_text(text, encoding="utf-8")
            temporary.chmod(0o755)
            os.replace(temporary, hooks_dir / item)
        installed[name] = install_status(repo)
    return {"status": "OK", "repositories": installed}


def uninstall(repos: dict, *, retire_adoption: bool = False, now: Optional[float] = None) -> dict:
    """Remove only hooks this installer wrote.

    Adoption is kept, so the merge check still judges commits made since it.
    ``retire_adoption`` ends it deliberately: the marker becomes
    ``retired-<stamp>.json`` recording when, and the repository reads as not
    adopted afterwards.
    """
    moment = time.time() if now is None else now
    removed, retired = {}, {}
    for name, repo in repos.items():
        hooks_dir = _hooks_dir(repo)
        removed[name] = []
        for item in hook.HOOKS:
            path = hooks_dir / item
            if path.is_file() and _ours(path):
                path.unlink()
                removed[name].append(item)
        marker = journal_root(repo) / hook.INSTALLED
        if retire_adoption and marker.exists():
            data = json.loads(marker.read_text(encoding="utf-8"))
            data["retired_at"] = iso(moment)
            target = marker.parent / f"{RETIRED_PREFIX}{_stamp(moment)}.json"
            hook.write_new(str(target), (json.dumps(data, sort_keys=True) + "\n").encode())
            marker.unlink()
            retired[name] = data
    return {"status": "OK", "removed": removed, "retired": retired}


# -- journal ----------------------------------------------------------------------


def valid_record(record: Any) -> bool:
    return (isinstance(record, dict) and record.get("schema") == hook.SCHEMA
            and isinstance(record.get("id"), str) and isinstance(record.get("sha"), str)
            and SHA_RE.fullmatch(record["sha"]) is not None
            and isinstance(record.get("repository"), str)
            and isinstance(record.get("acquisition_digest"), str)
            and (record["acquisition_digest"] in (hook.NONE, UNKNOWN)
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


def _sessions_dir(goal_store: Path, published: bool) -> Path:
    return journal_root(goal_store) / SESSIONS / (hook.PUBLISHED if published else PENDING)


def write_session(goal_store: Path, record: dict, now: Optional[float] = None) -> Path:
    """Journal a session start or end before any publication attempt."""
    moment = time.time() if now is None else now
    if record.get("kind") not in {"start", "end"}:
        raise AttributionError("session kind must be start or end")
    if DIGEST_RE.fullmatch(record.get("acquisition_digest") or "") is None:
        raise AttributionError("session record needs the acquisition digest")
    ident = f"{record['kind']}-{time.strftime(hook.STAMP, time.gmtime(moment))}-{secrets.token_hex(2)}"
    path = _sessions_dir(goal_store, False) / f"{ident}.json"
    _write_json_once(path, {**record, "id": ident, "schema": hook.SCHEMA})
    return path


def _pending_sessions(goal_store: Path) -> list[tuple[Path, dict]]:
    directory = _sessions_dir(goal_store, False)
    found = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(value, dict) and value.get("id") == path.stem
                    and SESSION_ID_RE.fullmatch(path.stem)
                    and DIGEST_RE.fullmatch(value.get("acquisition_digest") or "")):
                found.append((path, value))
    return found


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
            checkout = _admin_checkout(admin)
            if checkout is not None and checkout.name == LEDGER_DIR:
                continue
            if (admin / "logs" / "HEAD").is_file():
                files.append(admin / "logs" / "HEAD")
    return files


def _admin_checkout(admin: Path) -> Optional[Path]:
    try:
        return Path((admin / "gitdir").read_text(encoding="utf-8").strip()).parent
    except OSError:
        return None


def _creation_goal(common: Path, log: str) -> str:
    """The goal label of the worktree whose reflog shows the creation, if known."""
    parts = log.split("/")
    if len(parts) == 4 and parts[0] == "worktrees" and parts[2:] == ["logs", "HEAD"]:
        checkout = _admin_checkout(common / "worktrees" / parts[1])
        if checkout is not None:
            label = hook.goal_label(str(checkout), "")
            return label if label != "unknown" else UNKNOWN
    return UNKNOWN


def creations(repo: Path, since: float, until: float) -> dict[str, dict]:
    """Commits this clone created in [since, until], from its reflogs.

    Reflog entries that create a commit (commit, amend, merge commit,
    cherry-pick, revert, am, rebase picks) are kept; checkouts, resets,
    fast-forwards and aborts are not. The ledger's own branch is skipped.
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
            if CREATION.search(message) is None or NOT_CREATION.search(message):
                continue
            log = path.relative_to(common).as_posix()
            goal = _creation_goal(common, log)
            if new not in found or moment < found[new]["at"]:
                earlier_goal = found.get(new, {}).get("goal", UNKNOWN)
                found[new] = {"at": moment, "reflog": message[:120], "log": log,
                              "goal": goal if goal != UNKNOWN else earlier_goal}
            elif found[new]["goal"] == UNKNOWN and goal != UNKNOWN:
                found[new]["goal"] = goal  # the worktree reflog names the goal
    return found


def known_creations(repo: Path, since: float, until: float, *, persist: bool = True) -> dict[str, dict]:
    """Fresh reflog creations plus those an earlier scan persisted.

    Each scan persists what it sees under ``census/`` in the journal area, so
    a creation stays known after its worktree or branch (and so its reflog)
    is removed, provided some census or merge check ran before the removal.
    """
    fresh = creations(repo, since, until)
    directory = journal_root(repo) / CENSUS
    if persist and fresh:
        for sha, seen in fresh.items():
            if not (directory / f"{sha}.json").exists():
                _write_json_once(directory / f"{sha}.json", {"sha": sha, **seen})
    merged = dict(fresh)
    if directory.is_dir():
        for path in directory.glob("*.json"):
            if path.stem in merged or SHA_RE.fullmatch(path.stem) is None:
                continue
            try:
                seen = json.loads(path.read_text(encoding="utf-8"))
                at = float(seen["at"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if since <= at <= until:
                merged[path.stem] = {"at": at, "reflog": seen.get("reflog", ""),
                                     "log": seen.get("log", ""), "goal": seen.get("goal", UNKNOWN),
                                     "persisted": True}
    return merged


def judged(repos: dict) -> dict:
    """Repositories adopted on this host (or with unmarked hooks); only these are judged."""
    return {name: Path(repo) for name, repo in repos.items()
            if Path(repo).is_dir() and adoption(Path(repo))[0] != "not-adopted"}


def census(repos: dict, since: float, until: float) -> list[dict]:
    """Commits created here in the window, since adoption, with no journal record."""
    missing = []
    for name, repo in judged(repos).items():
        state, adopted_at = adoption(repo)
        start = max(since, adopted_at) if adopted_at is not None else since
        recorded = recorded_shas(repo)
        for sha, seen in sorted(known_creations(repo, start, until).items(),
                                key=lambda item: item[1]["at"]):
            if sha not in recorded:
                missing.append({"repository": name, "sha": sha, "created_at": iso(seen["at"]),
                                "reflog": seen["reflog"], "goal": seen.get("goal", UNKNOWN),
                                "status": "unattributed"})
    return missing


def adopt(repos: dict, repository: str, shas: Iterable[str], *, reason: str,
          host: Optional[str], adopter: dict, now: Optional[float] = None) -> dict:
    """Record commits this host's census lists, with a reason and ``source: census``.

    The creation-time session is not known, so the record's session is
    ``unknown``; ``adopted_by`` names the adopting session. Only commits this
    host created since adoption and has no record of are accepted.
    """
    if repository not in repos:
        raise AttributionError(f"unknown repository {repository!r}; one of {sorted(repos)}")
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason:
        raise AttributionError("--reason must be one nonempty line")
    repo = Path(repos[repository])
    state, adopted_at = adoption(repo)
    if state != "adopted":
        raise AttributionError(f"{repository} is {state} on this host; nothing to adopt")
    moment = time.time() if now is None else now
    candidates = known_creations(repo, adopted_at, moment + 1)
    recorded = recorded_shas(repo)
    resolved_shas = []
    for sha in shas:
        resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
        full = resolved.stdout.decode().strip() if resolved.returncode == 0 else sha
        if full not in candidates or full in recorded:
            raise AttributionError(
                f"{sha} is not in this host's census for {repository} (created here since adoption "
                "and unrecorded); only those can be adopted; nothing adopted")
        resolved_shas.append(full)
    where = hook.context(str(repo))
    identity = {"client": None, "acquisition_digest": UNKNOWN, "lease_state": UNKNOWN}
    written = []
    for full in resolved_shas:
        seen = candidates[full]
        record = hook.build_record(
            cwd=str(repo), sha=full, hook="adopt", repository=repository, host=host,
            identity=identity, where=where, now=moment, source="census",
            extra={"reason": reason.strip(), "created_at": iso(seen["at"]),
                   "adopted_by": {"acquisition_digest": adopter.get("acquisition_digest"),
                                  "client": adopter.get("client")}})
        record["goal"] = seen.get("goal") or UNKNOWN
        record["branch"] = None
        hook.write_record(where["common"], record)
        written.append({"sha": full, "id": record["id"]})
    return {"status": "OK", "repository": repository, "adopted": written}


# -- remote guard ---------------------------------------------------------------------


def _urls(repo: Path, remote: str, *, push: bool) -> list[str]:
    args = ["remote", "get-url", "--all"] + (["--push"] if push else []) + [remote]
    result = _git(repo, *args, check=False)
    if result.returncode:
        return []
    return [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def _remotes(repo: Path) -> list[str]:
    result = _git(repo, "remote", check=False)
    return result.stdout.decode("utf-8", "replace").split() if result.returncode == 0 else []


def _local_identities(url: str, base: Path) -> set[str]:
    """Canonical forms of every repository a local URL can reach.

    The path itself, ``PATH.git`` (Git's fallback), and the Git common
    directory and its work tree when the target is a repository, so a linked
    worktree, a work tree and its ``.git`` all name the same repository.
    """
    path = git_urls.local_path(url, base)
    found = {git_urls.canonical(path)}
    for candidate in (path, path + ".git"):
        if not os.path.exists(candidate):
            continue
        found.add(git_urls.canonical(candidate))
        if os.path.isdir(candidate):
            result = _git(Path(candidate), "rev-parse", "--path-format=absolute", "--git-common-dir",
                          check=False)
            common = result.stdout.decode("utf-8", "replace").strip()
            if result.returncode == 0 and common:
                found.add(git_urls.canonical(common))
                if Path(common).name == ".git":
                    found.add(git_urls.canonical(str(Path(common).parent)))
    return found


def _identities(url: str, base: Path) -> set[str]:
    return _local_identities(url, base) if git_urls.is_local(url) else {git_urls.canonical(url, base)}


def public_urls(public_repos: Iterable[Path]) -> set[str]:
    """Canonical forms a public repository is reached by: its remotes and its own paths."""
    public = set(PUBLIC_ORIGINS)  # already canonical
    for repo in public_repos:
        repo = Path(repo)
        if not repo.is_dir():
            continue
        public |= _local_identities(str(repo), repo)
        for remote in _remotes(repo):
            for url in _urls(repo, remote, push=False) + _urls(repo, remote, push=True):
                public |= _identities(url, repo)
    return public


def _rewrite_rules(cwd: Path, kind: str) -> list[tuple[str, str]]:
    """``url.<base>.<kind>`` rules as (prefix, base), as Git reads them in ``cwd``."""
    # -z: records end in NUL and key and value are split by a newline, so a
    # base or prefix containing spaces is read exactly as Git reads it.
    result = _git(cwd, "config", "-z", "--get-regexp", rf"^url\..*\.{kind}$", check=False)
    rules = []
    suffix = "." + kind
    for record in result.stdout.decode("utf-8", "replace").split("\0"):
        key, _, prefix = record.partition("\n")
        if key.lower().startswith("url.") and key.lower().endswith(suffix):
            rules.append((prefix, key[len("url."):-len(suffix)]))
    return rules


def _redirects(url: str, rules: list[tuple[str, str]]) -> bool:
    """Whether any rule could rewrite ``url`` to something else.

    Git's choice among matching rules (longest prefix, ties by first
    appearance of the base) is not emulated: any matching rule that is not
    the identity refuses, whichever Git would pick.
    """
    return any(url.startswith(prefix) and base + url[len(prefix):] != url
               for prefix, base in rules)


def resolve_target(cwd: Path, goal_store: Path, remote: str, public_repos: Iterable[Path]) -> str:
    """The one literal URL to fetch from and push to, validated as Git sees it in ``cwd``.

    Publication never pushes to a remote name: a name's push URLs can differ
    by worktree (``config.worktree``) or by branch (``includeIf.onbranch``).
    The target is ``remote.<remote>.url`` with ``insteadOf`` applied, read in
    the same working directory as the push that will use it. It must be the
    only URL of that remote, Git must not rewrite it again (``insteadOf``,
    ``pushInsteadOf``), no remote may be named after it, and it must not
    reach any public repository. Push URLs (``pushurl``) are never used.
    """
    raw = _git(cwd, "config", "--get-all", f"remote.{remote}.url", check=False)
    urls = [line for line in raw.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if len(urls) != 1:
        raise AttributionError(f"remote {remote!r} must have exactly one URL here; nothing published")
    fetch_rules = _rewrite_rules(cwd, "insteadof")
    push_rules = _rewrite_rules(cwd, "pushinsteadof")
    # Git's own answer for the fetch URL with insteadOf applied.
    got = _git(cwd, "ls-remote", "--get-url", remote, check=False)
    target = got.stdout.decode("utf-8", "replace").strip()
    if got.returncode != 0 or not target:
        raise AttributionError(f"cannot resolve the URL of remote {remote!r} here; nothing published")
    if git_urls.is_local(target):
        target = git_urls.local_path(target, goal_store)
    if _redirects(target, fetch_rules) or _redirects(target, push_rules):
        raise AttributionError(
            f"URL rewriting (insteadOf/pushInsteadOf) would redirect the publication target "
            f"of remote {remote!r}; nothing published")
    names = _git(cwd, "config", "--name-only", "--get-regexp", r"^remote\.", check=False)
    for key in names.stdout.decode("utf-8", "replace").splitlines():
        if key.startswith(f"remote.{target}."):
            raise AttributionError("a remote is named after the publication URL; nothing published")
    if _identities(target, goal_store) & public_urls(public_repos):
        raise AttributionError(
            f"remote {remote!r} of the goal store is a public repository's remote; "
            "attribution data stays on the private goal store only; nothing published")
    return target


def require_private_remote(goal_store: Path, remote: str, public_repos: Iterable[Path]) -> str:
    """The validated publication URL as the goal store's own checkout resolves it."""
    return resolve_target(goal_store, goal_store, remote, public_repos)


# -- ledger -----------------------------------------------------------------------


@dataclass
class Ledger:
    records: list = field(default_factory=list)
    sessions: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    attached: bool = True
    paths: set = field(default_factory=set)


def _tree_files(worktree: Path, rev: str) -> Iterable[tuple[str, bytes]]:
    """Every file under ``hosts/`` at ``rev``: one ``ls-tree`` and one ``cat-file --batch``.

    Anything that is not a regular blob of bounded size yields empty bytes,
    which the loader reports as a problem.
    """
    listed = _git(worktree, "ls-tree", "-r", "-z", "--long", rev, "--", HOSTS, check=False)
    entries, wanted = [], []
    for entry in listed.stdout.decode("utf-8", "replace").split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        ok = (len(fields) >= 4 and fields[1] == "blob" and fields[0] in {"100644", "100755"}
              and fields[3].isdigit() and int(fields[3]) <= MAX_LEDGER_FILE)
        entries.append((path, fields[2] if ok else None))
        if ok:
            wanted.append(fields[2])
    blobs: dict[str, bytes] = {}
    if wanted:
        output = _git(worktree, "cat-file", "--batch",
                      stdin=("\n".join(dict.fromkeys(wanted)) + "\n").encode()).stdout
        position = 0
        while position < len(output):
            newline = output.index(b"\n", position)
            header = output[position:newline].decode("ascii", "replace").split()
            position = newline + 1
            if len(header) < 3 or header[1] != "blob":
                continue
            size = int(header[2])
            blobs[header[0]] = output[position:position + size]
            position += size + 1
    for path, oid in entries:
        yield path, blobs.get(oid, b"") if oid else b""


def load_ledger(worktree: Path, rev: str = "HEAD") -> Ledger:
    """Parse the ledger as committed at ``rev``; malformed files become problems."""
    ledger = Ledger()
    for relative, data in _tree_files(worktree, rev):
        ledger.paths.add(relative)
        parts = relative.split("/")
        try:
            if not data:
                raise AttributionError("not a regular file of bounded size")
            value = json.loads(data.decode("utf-8"))
            host = parts[1]
            if len(parts) == 4 and parts[2] == BATCHES:
                if not isinstance(value, dict) or value.get("host") != host:
                    raise AttributionError("batch host does not match its path")
                for record in value.get("records", []):
                    if not valid_record(record) or record.get("host") != host:
                        raise AttributionError("batch holds a malformed or foreign record")
                ledger.records.extend(value["records"])
            elif len(parts) == 5 and parts[2] == SESSIONS:
                kind = parts[4].split("-", 1)[0]
                if (not isinstance(value, dict) or value.get("host") != host
                        or value.get("acquisition_digest") != parts[3]
                        or kind not in {"start", "end"}):
                    raise AttributionError("session file does not match its path")
                session = ledger.sessions.setdefault((host, parts[3]), {
                    "host": host, "acquisition_digest": parts[3]})
                for key in ("client", "model", "effort", "started_at"):
                    if value.get(key) is not None and session.get(key) is None:
                        session[key] = value[key]
                if kind == "end":
                    session["ended_at"] = max(session.get("ended_at") or "", value.get("ended_at") or "")
                    session.setdefault("census", []).extend(value.get("census", []))
            else:
                raise AttributionError("unexpected ledger path")
        except (ValueError, AttributionError, AttributeError, TypeError, UnicodeError) as exc:
            ledger.problems.append((relative, str(exc)))
    return ledger


def read_ledger(goal_store: Path, host: str, *, remote: str = DEFAULT_REMOTE,
                branch: str = DEFAULT_BRANCH, fetch: bool = True) -> Ledger:
    """Fetch and read the ledger without acquiring the master lease."""
    spec = _spec(remote)
    try:
        with channels.lock(goal_store, spec):
            path = channels.worktree_path(goal_store, spec)
            if fetch:
                worktree = channels.attach(goal_store, spec, create=False, host=host,
                                           remote=remote, branch=branch)
                if worktree is not None:
                    channels.reset_to_remote(worktree, spec)
            else:
                worktree = path if path.is_dir() else None
            if worktree is None:
                return Ledger(attached=False)
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


def _move(path: Path, folder: str) -> None:
    target = path.parent.parent / folder
    hook.ensure_dir(str(target))
    try:
        os.replace(path, target / path.name)
    except OSError:
        pass  # the next publication finds it on the origin and moves it then


def publish(goal_store: Path, repos: dict, *, host: str, record_root: Path, writer=None,
            public_repos: Iterable[Path] = (), now: Optional[float] = None,
            remote: str = DEFAULT_REMOTE, branch: str = DEFAULT_BRANCH) -> dict:
    """Publish every unpublished journal record and session file to the ledger.

    Master only. Includes anything a dead predecessor left. "Already
    published" is decided from the fetched origin, never the local tree; a
    local ledger commit the origin lacks is discarded and rebuilt from the
    journal; journal files move to ``published/`` only once the fetched
    origin holds them. A record naming another host is quarantined.
    """
    if hook.HOST_RE.fullmatch(host or "") is None:
        raise AttributionError("this host has no valid name; attribution refuses unnamed hosts "
                               "(python3 -m creme set-host-name NAME)")
    public_repos = list(public_repos)
    # The opening push (if any) runs in the goal store's checkout: validate there.
    root_target = resolve_target(goal_store, goal_store, remote, public_repos)
    root_spec = _spec(root_target, remote)
    moment = time.time() if now is None else now

    def action() -> dict:
        with channels.lock(goal_store, root_spec):
            worktree = channels.attach(goal_store, root_spec, create=True, host=host,
                                       remote=root_target, branch=branch)
            # Every later fetch and push runs in the ledger worktree, whose own
            # configuration (config.worktree, includeIf.onbranch) can differ.
            target = resolve_target(worktree, goal_store, remote, public_repos)
            if target != root_target:
                raise AttributionError(
                    "the ledger worktree resolves the publication URL differently from the goal "
                    "store's checkout; nothing published")
            spec = _spec(target, remote)
            channels.reset_to_remote(worktree, spec)
            published = load_ledger(worktree)
            present = {r["id"] for r in published.records if r.get("host") == host}
            pending: list[JournalEntry] = []
            for repo in repos.values():
                if Path(repo).is_dir():
                    pending.extend(entry for entry in journal(Path(repo)) if not entry.published)
            quarantined, fresh = [], []
            for entry in pending:
                if entry.record.get("host") not in (None, host):
                    _move(entry.path, QUARANTINE)
                    quarantined.append({"id": entry.record["id"], "host": entry.record.get("host")})
                    continue
                if entry.record["id"] not in present:
                    fresh.append({**entry.record, "host": host})
            pending = [e for e in pending if e.record["id"] not in {q["id"] for q in quarantined}]
            files: dict[str, bytes] = {}
            for index in range(0, len(fresh), MAX_BATCH):
                relative = f"{HOSTS}/{host}/{BATCHES}/{_stamp(moment)}-{secrets.token_hex(4)}.json"
                files[relative] = _dump({"schema": hook.SCHEMA, "host": host,
                                         "published_at": iso(moment),
                                         "records": fresh[index:index + MAX_BATCH]})
            sessions = _pending_sessions(goal_store)
            session_paths = {}
            for path, value in sessions:
                relative = f"{HOSTS}/{host}/{SESSIONS}/{value['acquisition_digest']}/{value['id']}.json"
                session_paths[relative] = path
                if relative not in published.paths:
                    files[relative] = _dump({**value, "host": host})
            commit = None
            if files:
                session_count = sum(1 for relative in files if f"/{SESSIONS}/" in relative)
                commit = channels.publish(
                    worktree, files,
                    f"attribution: {host} publishes {len(fresh)} record(s) and "
                    f"{session_count} session file(s)", spec)
                remote_head = channels.fetch(worktree, spec)
                if not shared.is_ancestor(worktree, commit, remote_head):
                    raise AttributionError("the origin does not hold the published commit; "
                                           "journal kept for the next publication")
            confirmed = load_ledger(worktree, shared.tracking_ref(remote, branch)) if files else published
            on_origin = {r["id"] for r in confirmed.records if r.get("host") == host}
            for entry in pending:
                if entry.record["id"] in on_origin:
                    _move(entry.path, hook.PUBLISHED)
            for relative, path in session_paths.items():
                if relative in confirmed.paths:
                    _move(path, hook.PUBLISHED)
            return {"status": "OK", "published": len(fresh), "commit": commit,
                    "session_files": sorted(r for r in session_paths if r in files),
                    "quarantined": quarantined, "host": host}

    try:
        return channels.authorized(record_root, writer, action, root_spec)
    except shared.SharedStackError as exc:
        raise AttributionError(str(exc)) from exc


def session_record(kind: str, *, digest: str, client: str, model: str, effort: str,
                   started_at: Optional[str], ended_at: Optional[str] = None,
                   census_rows: Optional[list] = None) -> dict:
    record = {"kind": kind, "acquisition_digest": digest, "client": client, "model": model,
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
    are outside the claim and pass. Hooks without the adoption marker refuse.
    """
    if repository not in repos:
        raise AttributionError(f"unknown repository {repository!r}; one of {sorted(repos)}")
    repo = Path(repos[repository])
    state, adopted = adoption(repo)
    if state == "unmarked":
        return {"status": "refused", "repository": repository,
                "detail": "attribution hooks are installed but the adoption marker is missing",
                "remedy": "python3 -m creme attribution install (restores the marker)"}
    if state == "not-adopted":
        return {"status": "not-adopted", "repository": repository,
                "detail": "attribution is not adopted in this repository on this host"}
    published = publish_first()
    listed = _out(repo, "rev-list", rev_range).split()
    moment = time.time() if now is None else now
    created = known_creations(repo, adopted, moment + 1)
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
    return hook.safe_patch_id(str(repo), sha)


def _session_key(record: dict) -> tuple:
    return (record.get("host"), record["acquisition_digest"])


def build_report(ledger: Ledger, repos: dict, filters: Filters, *, local: Iterable[dict] = (),
                 now: Optional[float] = None, host: Optional[str] = None) -> dict:
    """Group ledger records (plus local unpublished ones) by session, repository, goal.

    Subjects come from local Git at report time. A rewritten commit shows its
    final SHA with the superseded ones folded under it. When the report has a
    time window (given, or a session's start and end), commits in the window
    in repositories adopted here that no host recorded are listed as
    ``unattributed``; those whose patch-id equals a recorded commit's in the
    same repository are listed separately as matched, never attributed.
    """
    moment = time.time() if now is None else now
    warnings = []
    if not ledger.attached:
        warnings.append("no ledger is attached on this host; only this host's journal was read")
    if host is None:
        warnings.append("this host has no name; the ledger was not read")
    published_ids = {record["id"] for record in ledger.records}
    records = list(ledger.records) + [dict(record, unpublished=True) for record in local
                                      if record["id"] not in published_ids]
    successor = {(r["repository"], r["rewritten_from"]): r["sha"] for r in records
                 if r.get("rewritten_from")}
    seen: dict[tuple, dict] = {}
    for record in sorted(records, key=lambda r: (r.get("recorded_at", ""), r["id"])):
        seen.setdefault((record.get("host"), record["repository"], record["sha"]), record)
    records = list(seen.values())
    sessions = ledger.sessions
    since, until = filters.since, filters.until
    if filters.session:
        matches = {key for key in {_session_key(r) for r in records} | set(sessions)
                   if key[1].startswith(filters.session)}
        if not matches:
            raise AttributionError(f"no session matches {filters.session!r}")
        times = []
        for key in matches:
            meta = sessions.get(key, {})
            times += [parse_time(meta[k]) for k in ("started_at", "ended_at") if meta.get(k)]
            times += [parse_time(r["recorded_at"]) for r in records if _session_key(r) == key]
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
    chosen_keys = {(r["repository"], r["sha"]) for r in chosen}

    def final(repository: str, sha: str) -> str:
        visited = {sha}
        while (repository, sha) in successor and successor[(repository, sha)] not in visited:
            sha = successor[(repository, sha)]
            visited.add(sha)
        return sha

    by_key = {(r["repository"], r["sha"]): r for r in records}
    superseded: dict[tuple, list] = {}
    shown = []
    for record in chosen:
        end = final(record["repository"], record["sha"])
        if end == record["sha"]:
            shown.append(record)
            continue
        successor_record = by_key.get((record["repository"], end))
        same_session = (successor_record is not None
                        and _session_key(successor_record) == _session_key(record))
        # Fold only within one session: a later session that rebased an earlier
        # one's commits must not take them over. Adopted rows are never folded.
        if (same_session and record.get("source") != "census"
                and (record["repository"], end) in chosen_keys):
            superseded.setdefault((record["repository"], end), []).append(record["sha"])
        else:
            shown.append(dict(record, superseded_by=end))
    unattributed: list[dict] = []
    matched: list[dict] = []
    if since is not None and until is not None and not filters.goal:
        known = {(r["repository"], r["sha"]) for r in records}
        by_patch: dict[tuple, dict] = {}
        for record in records:
            if record.get("patch_id"):
                by_patch.setdefault((record["repository"], record["patch_id"]), record)
        for name, repo in judged(repos).items():
            for sha, at in _window_commits(repo, since, until):
                if (name, sha) in known:
                    continue
                pid = _patch_id(repo, sha) if by_patch else None
                origin = by_patch.get((name, pid)) if pid else None
                if origin is not None:
                    matched.append({"repository": name, "sha": sha, "committed_at": iso(at),
                                    "matches": origin["sha"], "matched_host": origin.get("host"),
                                    "status": "matched by patch-id, not attributed"})
                else:
                    unattributed.append({"repository": name, "sha": sha,
                                         "committed_at": iso(at), "status": "unattributed"})
    subjects: dict[tuple, str] = {}
    for name, repo in repos.items():
        shas = [r["sha"] for r in shown if r["repository"] == name] + \
               [u["sha"] for u in unattributed + matched if u["repository"] == name]
        for sha, subject in _subjects(Path(repo), shas).items():
            subjects[(name, sha)] = subject
    grouped: dict[tuple, dict] = {}
    for record in sorted(shown, key=lambda r: (r.get("recorded_at", ""), r["sha"])):
        key = _session_key(record)
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
        for extra in ("unpublished", "reason", "lease_state", "stale_lease", "adopted_by",
                      "created_at", "superseded_by"):
            if record.get(extra) is not None:
                row[extra] = record[extra]
        folded = superseded.get((record["repository"], record["sha"]))
        if folded:
            row["supersedes"] = sorted(set(folded))
        goals.setdefault(record.get("goal") or "(none)", []).append(row)
    for row in unattributed + matched:
        row["subject"] = subjects.get((row["repository"], row["sha"]), "(not in the local clone)")
    census_rows = [dict(row, host=key[0]) for key, meta in sessions.items()
                   if key in grouped for row in meta.get("census", [])]
    return {
        "schema": 1,
        "filters": {"session": filters.session, "host": filters.host, "goal": filters.goal,
                    "since": iso(since) if since is not None else None,
                    "until": iso(until) if until is not None else None},
        "sessions": sorted(grouped.values(), key=lambda s: (s.get("started_at") or "", s["host"] or "",
                                                            s["acquisition_digest"])),
        "unattributed": unattributed,
        "matched_by_patch_id": matched,
        "session_census": census_rows,
        "warnings": warnings,
        "problems": [{"path": p, "reason": r} for p, r in ledger.problems],
    }


def _row_flags(row: dict) -> str:
    flags = []
    if row.get("reason") is not None and row.get("source") == "census":
        flags.append(f"adopted: {row['reason']}")
    if row.get("stale_lease"):
        flags.append("stale lease")
    if row.get("unpublished"):
        flags.append("unpublished")
    if row.get("supersedes"):
        flags.append("supersedes " + ", ".join(sha[:12] for sha in row["supersedes"]))
    if row.get("superseded_by"):
        flags.append(f"superseded by {row['superseded_by'][:12]}")
    return "".join(f" [{flag}]" for flag in flags)


def render_report(report: dict) -> str:
    lines = [f"warning: {text}" for text in report.get("warnings", [])]
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
                    lines.append(f"    {row['sha'][:12]} {row['subject']}{_row_flags(row)}")
    if report["unattributed"]:
        lines.append(f"unattributed: {len(report['unattributed'])} commit(s)")
        for row in report["unattributed"]:
            lines.append(f"  {row['repository']} {row['sha'][:12]} {row['committed_at']} {row['subject']}")
    if report.get("matched_by_patch_id"):
        lines.append(f"matched by patch-id, not attributed: {len(report['matched_by_patch_id'])} commit(s)")
        for row in report["matched_by_patch_id"]:
            lines.append(f"  {row['repository']} {row['sha'][:12]} {row['subject']} "
                         f"(same patch as {row['matches'][:12]} recorded by {row['matched_host']})")
    for problem in report["problems"]:
        lines.append(f"problem {problem['path']}: {problem['reason']}")
    return "\n".join(lines) + ("\n" if lines else "")


# -- privacy ----------------------------------------------------------------------


def known_digests(repos: dict, goal_store: Optional[Path] = None) -> set[str]:
    """Acquisition digests this host knows from journals and its ledger copy."""
    digests = set()
    for repo in repos.values():
        try:
            for entry in journal(Path(repo)):
                record = entry.record
                digests.add(record["acquisition_digest"])
                digests.add(record.get("stale_lease") or hook.NONE)
                digests.add((record.get("adopted_by") or {}).get("acquisition_digest") or hook.NONE)
        except (AttributionError, shared.SharedStackError):
            continue
    if goal_store is not None and (Path(goal_store) / LEDGER_DIR).is_dir():
        try:
            ledger = load_ledger(Path(goal_store) / LEDGER_DIR)
            digests.update(r["acquisition_digest"] for r in ledger.records)
            digests.update(key[1] for key in ledger.sessions)
        except (AttributionError, shared.SharedStackError):
            pass
    return {d for d in digests if isinstance(d, str) and DIGEST_RE.fullmatch(d)}


def _word(host: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(host) + r"(?![A-Za-z0-9_-])", re.IGNORECASE)


TEST_TREE = "scripts/tests/"


def privacy_check(repo: Path, base: str, *, host: Optional[str], digests: Iterable[str],
                  head: str = "HEAD") -> list[dict]:
    """Private attribution data in ``base..head``: added lines and commit metadata.

    Flags this host's name as a word in any letter case, any known
    acquisition digest, and a session field carrying a hex value. Test
    fixtures (``scripts/tests/``) may use synthetic host names and
    identifiers, so only known digests are flagged there.
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
    return bool(judged(repos))


def _local_host(host: Optional[str]) -> Optional[str]:
    if host is not None:
        return host
    from . import goal_stack_store

    return goal_stack_store.local_host_name()


def _current_digest(location, writer) -> str:
    from . import master_runtime as runtime

    writer = writer or runtime.RecordWriter(location.record_root)
    return runtime._actor_from_snapshot(writer.lease_snapshot())["acquisition_digest"]


def on_master_start(location, *, client: str, model: str, effort: str, writer=None,
                    host: Optional[str] = None, now: Optional[float] = None) -> dict:
    """At ``master start``: journal the session start, then publish everything pending."""
    repos = {k: v for k, v in repositories(location).items() if v.is_dir()}
    if not adopted(repos):
        return {"status": "not-adopted"}
    moment = time.time() if now is None else now
    write_session(location.goal_store, session_record(
        "start", digest=_current_digest(location, writer), client=client, model=model,
        effort=effort, started_at=iso(moment)), now=moment)
    return publish(location.goal_store, repos, host=_local_host(host),
                   record_root=location.record_root, writer=writer,
                   public_repos=public_repositories(location), now=moment)


def session_started_at(events: Iterable[dict], digest: str) -> Optional[str]:
    """The earliest master-event timestamp of one acquisition."""
    stamps = [event["timestamp"] for event in events
              if isinstance(event, dict) and event.get("actor", {}).get("acquisition_digest") == digest]
    return min(stamps) if stamps else None


def on_master_end(location, *, digest: str, client: str, model: str, effort: str,
                  events: Iterable[dict], writer=None, host: Optional[str] = None,
                  now: Optional[float] = None) -> dict:
    """At ``master end``: census the session, journal the end record, then publish.

    The end record is journaled before publication, so a failed publication
    is retried by any later one (``attribution publish``, the next start).
    """
    repos = {k: v for k, v in repositories(location).items() if v.is_dir()}
    if not adopted(repos):
        return {"status": "not-adopted"}
    moment = time.time() if now is None else now
    started = session_started_at(events, digest)
    since = parse_time(started) if started else moment
    rows = census(repos, since, moment)
    write_session(location.goal_store, session_record(
        "end", digest=digest, client=client, model=model, effort=effort, started_at=started,
        ended_at=iso(moment), census_rows=rows), now=moment)
    summary = {"unattributed": len(rows), "census": rows}
    try:
        result = publish(location.goal_store, repos, host=_local_host(host),
                         record_root=location.record_root, writer=writer,
                         public_repos=public_repositories(location), now=moment)
    except Exception as exc:
        return {"status": "error", "detail": str(exc), "end_record": "journaled; publish later",
                **summary}
    return {**result, **summary}
