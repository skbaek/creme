from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import master_runtime


RECONCILIATION_SCHEMA_VERSION = 1
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class ReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


GitRunner = Callable[[Path, Sequence[str]], GitResult]


@dataclass
class WorktreeFact:
    path: Path
    head: Optional[str]
    branch: Optional[str]
    detached: bool
    primary: bool = False
    tracked_dirty: bool = False
    untracked_data: bool = False
    upstream: Optional[str] = None
    upstream_missing: bool = False
    ahead: Optional[int] = None
    behind: Optional[int] = None
    inaccessible: bool = False
    goal_ids: tuple[str, ...] = ()
    public_id: str = ""


@dataclass
class RepositoryFact:
    repository: str
    status: str
    worktrees: list[WorktreeFact]

    def summary(self) -> dict[str, Any]:
        primary = next((item for item in self.worktrees if item.primary), None)
        return {
            "repository": self.repository,
            "status": self.status,
            "head": primary.head if primary is not None else None,
            "branch": primary.branch if primary is not None else None,
            "upstream": primary.upstream if primary is not None else None,
            "ahead": primary.ahead if primary is not None else None,
            "behind": primary.behind if primary is not None else None,
            "worktree_count": len(self.worktrees),
            "recorded_worktrees": sum(bool(item.goal_ids) for item in self.worktrees),
            "extra_worktrees": sum(
                not item.primary and not item.goal_ids for item in self.worktrees
            ),
            "detached_worktrees": sum(item.detached for item in self.worktrees),
            "tracked_dirty_worktrees": sum(item.tracked_dirty for item in self.worktrees),
            "untracked_worktrees": sum(item.untracked_data for item in self.worktrees),
            "inaccessible_worktrees": sum(item.inaccessible for item in self.worktrees),
        }


@dataclass(frozen=True)
class ReconciliationResult:
    repositories: tuple[dict[str, Any], ...]
    discrepancies: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "repositories": list(self.repositories),
            "discrepancies": list(self.discrepancies),
        }


def run_git(root: Path, arguments: Sequence[str]) -> GitResult:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "maintenance.auto=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                *arguments,
            ],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconciliationError(f"Git fact is inaccessible: {exc}") from exc
    return GitResult(completed.returncode, completed.stdout, completed.stderr)


def _logical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _safe_directory(path: Path) -> tuple[Optional[Path], str]:
    logical = _logical_absolute(path)
    try:
        if path.resolve(strict=False) != logical:
            return None, "symlinked-boundary"
        info = logical.lstat()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "inaccessible"
    if not logical.is_dir() or os.path.islink(logical):
        return None, "wrong-type"
    if info.st_uid != os.geteuid():
        return None, "inaccessible"
    return logical, "OK"


def _decode(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReconciliationError("Git returned a non-UTF-8 public fact") from exc


def _discrepancy(
    repository: str,
    kind: str,
    subject: str,
    *,
    recorded: Optional[str],
    observed: Optional[str],
    detail: str,
) -> dict[str, Any]:
    return {
        "repository": repository,
        "kind": kind,
        "subject": subject,
        "recorded": recorded,
        "observed": observed,
        "detail": detail,
    }


def _parse_worktrees(data: bytes, root: Path) -> list[WorktreeFact]:
    worktrees: list[WorktreeFact] = []
    for record in data.split(b"\0\0"):
        fields: dict[str, bytes] = {}
        flags: set[str] = set()
        for raw in record.split(b"\0"):
            if not raw:
                continue
            key, separator, value = raw.partition(b" ")
            name = _decode(key)
            if separator:
                fields[name] = value
            else:
                flags.add(name)
        raw_path = fields.get("worktree")
        if raw_path is None:
            continue
        path = _logical_absolute(Path(os.fsdecode(raw_path)))
        raw_branch = fields.get("branch")
        branch = _decode(raw_branch) if raw_branch is not None else None
        if branch is not None and branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        head = _decode(fields["HEAD"]) if "HEAD" in fields else None
        worktrees.append(
            WorktreeFact(
                path=path,
                head=head if head and _COMMIT.fullmatch(head) else None,
                branch=branch,
                detached="detached" in flags or branch is None,
                primary=path == root,
            )
        )
    worktrees.sort(key=lambda item: str(item.path))
    for index, item in enumerate(worktrees):
        item.public_id = "primary" if item.primary else f"worktree-{index + 1}"
    return worktrees


def _status_flags(result: GitResult) -> tuple[bool, bool]:
    if result.returncode != 0:
        raise ReconciliationError("Git worktree status is inaccessible")
    tracked = False
    untracked = False
    fields = result.stdout.split(b"\0")
    index = 0
    while index < len(fields):
        row = fields[index]
        index += 1
        if not row:
            continue
        if len(row) < 3:
            raise ReconciliationError("Git returned malformed porcelain status")
        code = row[:2]
        if code == b"??":
            untracked = True
        else:
            tracked = True
        if code[:1] in {b"R", b"C"} or code[1:2] in {b"R", b"C"}:
            index += 1
    return tracked, untracked


def _branch_upstream(
    root: Path,
    branch: Optional[str],
    runner: GitRunner,
) -> tuple[Optional[str], bool, Optional[int], Optional[int]]:
    if branch is None:
        return None, False, None, None
    ref = f"refs/heads/{branch}"
    result = runner(
        root,
        [
            "for-each-ref",
            "--format=%(upstream)%00%(upstream:track)",
            "--count=1",
            ref,
        ],
    )
    if result.returncode != 0:
        raise ReconciliationError("Git upstream fact is inaccessible")
    values = result.stdout.rstrip(b"\n").split(b"\0", 1)
    upstream = _decode(values[0]) if values and values[0] else None
    tracking = _decode(values[1]) if len(values) == 2 else ""
    missing = "gone" in tracking
    if upstream is None or missing:
        return upstream, missing, None, None
    counts = runner(root, ["rev-list", "--left-right", "--count", f"{ref}...{upstream}"])
    if counts.returncode != 0:
        return upstream, True, None, None
    parts = _decode(counts.stdout).split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ReconciliationError("Git returned malformed ahead/behind counts")
    ahead, behind = (int(part) for part in parts)
    return upstream, False, ahead, behind


def _inspect_repository(
    repository: str,
    configured_root: Path,
    runner: GitRunner,
) -> tuple[RepositoryFact, list[dict[str, Any]]]:
    root, state = _safe_directory(configured_root)
    if root is None:
        kind = "missing-repository" if state in {"missing", "wrong-type"} else "inaccessible-fact"
        return RepositoryFact(repository, "missing" if kind == "missing-repository" else "inaccessible", []), [
            _discrepancy(
                repository,
                kind,
                "repository",
                recorded="configured",
                observed=None,
                detail=(
                    "configured repository is absent or not a directory"
                    if kind == "missing-repository"
                    else "configured repository cannot be inspected without following an unsafe boundary"
                ),
            )
        ]
    try:
        identity = runner(root, ["rev-parse", "--is-inside-work-tree", "--show-toplevel"])
    except ReconciliationError:
        return RepositoryFact(repository, "inaccessible", []), [
            _discrepancy(
                repository,
                "inaccessible-fact",
                "repository",
                recorded="configured",
                observed=None,
                detail="Git repository identity could not be inspected",
            )
        ]
    try:
        lines = _decode(identity.stdout).splitlines()
        folded = _decode(identity.stderr).casefold()
    except ReconciliationError:
        lines = []
        folded = ""
    if identity.returncode != 0 or len(lines) != 2 or lines[0] != "true":
        missing = "not a git repository" in folded
        kind = "missing-repository" if missing else "inaccessible-fact"
        return RepositoryFact(repository, "missing" if missing else "inaccessible", []), [
            _discrepancy(
                repository,
                kind,
                "repository",
                recorded="configured",
                observed=None,
                detail=(
                    "configured directory is not a Git repository"
                    if missing
                    else "Git repository identity is unknown"
                ),
            )
        ]
    if _logical_absolute(Path(lines[1])) != root:
        return RepositoryFact(repository, "inaccessible", []), [
            _discrepancy(
                repository,
                "inaccessible-fact",
                "repository-root",
                recorded="configured-root",
                observed="different-root",
                detail="Git resolved a different top-level directory",
            )
        ]
    try:
        listing = runner(root, ["worktree", "list", "--porcelain", "-z"])
    except ReconciliationError:
        listing = GitResult(1, b"", b"")
    if listing.returncode != 0:
        return RepositoryFact(repository, "inaccessible", []), [
            _discrepancy(
                repository,
                "inaccessible-fact",
                "worktree-list",
                recorded="registered-worktrees",
                observed=None,
                detail="registered Git worktrees could not be inspected",
            )
        ]
    try:
        worktrees = _parse_worktrees(listing.stdout, root)
    except ReconciliationError:
        return RepositoryFact(repository, "inaccessible", []), [
            _discrepancy(
                repository,
                "inaccessible-fact",
                "worktree-list",
                recorded="registered-worktrees",
                observed=None,
                detail="registered Git worktree facts are not safely representable",
            )
        ]
    fact = RepositoryFact(repository, "OK", worktrees)
    discrepancies: list[dict[str, Any]] = []
    for worktree in fact.worktrees:
        safe_path, safe_state = _safe_directory(worktree.path)
        if safe_path is None:
            worktree.inaccessible = True
            kind = "missing-worktree" if safe_state in {"missing", "wrong-type"} else "inaccessible-fact"
            discrepancies.append(
                _discrepancy(
                    repository,
                    kind,
                    worktree.public_id,
                    recorded="registered",
                    observed=None,
                    detail=(
                        "registered worktree is absent"
                        if kind == "missing-worktree"
                        else "registered worktree crosses an unsafe or inaccessible boundary"
                    ),
                )
            )
            continue
        try:
            status = runner(
                safe_path,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )
            worktree.tracked_dirty, worktree.untracked_data = _status_flags(status)
            (
                worktree.upstream,
                worktree.upstream_missing,
                worktree.ahead,
                worktree.behind,
            ) = _branch_upstream(root, worktree.branch, runner)
        except ReconciliationError:
            worktree.inaccessible = True
            discrepancies.append(
                _discrepancy(
                    repository,
                    "inaccessible-fact",
                    worktree.public_id,
                    recorded="registered",
                    observed=None,
                    detail="one or more worktree facts could not be inspected",
                )
            )
            continue
        if worktree.detached:
            discrepancies.append(
                _discrepancy(
                    repository,
                    "detached-head",
                    worktree.public_id,
                    recorded="branch",
                    observed="detached",
                    detail="registered worktree HEAD is detached",
                )
            )
        if worktree.tracked_dirty:
            discrepancies.append(
                _discrepancy(
                    repository,
                    "tracked-dirt",
                    worktree.public_id,
                    recorded="clean",
                    observed="tracked-changes",
                    detail="Git reports tracked modifications without reading them into the digest",
                )
            )
        if worktree.untracked_data:
            discrepancies.append(
                _discrepancy(
                    repository,
                    "untracked-data",
                    worktree.public_id,
                    recorded="none",
                    observed="present",
                    detail="Git reports untracked paths without exposing their names or contents",
                )
            )
        if worktree.upstream_missing:
            discrepancies.append(
                _discrepancy(
                    repository,
                    "missing-ref",
                    f"{worktree.public_id}:upstream",
                    recorded="configured-upstream",
                    observed=None,
                    detail="configured upstream ref is missing",
                )
            )
        elif worktree.ahead or worktree.behind:
            if worktree.ahead and worktree.behind:
                observed = f"diverged:ahead={worktree.ahead},behind={worktree.behind}"
            elif worktree.ahead:
                observed = f"ahead:{worktree.ahead}"
            else:
                observed = f"behind:{worktree.behind}"
            discrepancies.append(
                _discrepancy(
                    repository,
                    "upstream-drift",
                    f"{worktree.public_id}:upstream",
                    recorded="ahead=0,behind=0",
                    observed=observed,
                    detail="worktree branch differs from its configured upstream",
                )
            )
    return fact, discrepancies


def _candidate_path(root: Path, recorded: str) -> Optional[Path]:
    value = Path(recorded)
    candidate = _logical_absolute(value if value.is_absolute() else root / value)
    if not value.is_absolute():
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
    return candidate


def _ref_exists(root: Path, ref: str, runner: GitRunner) -> Optional[bool]:
    try:
        result = runner(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{ref}"])
    except ReconciliationError:
        return None
    return result.returncode == 0


def _commit_exists(root: Path, commit: str, runner: GitRunner) -> Optional[bool]:
    if _COMMIT.fullmatch(commit) is None:
        return False
    try:
        result = runner(root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    except ReconciliationError:
        return None
    return result.returncode == 0


def reconcile_record(
    record_root: Path,
    repository_roots: Mapping[str, Path],
    *,
    runner: GitRunner = run_git,
) -> ReconciliationResult:
    view = master_runtime.read_record(record_root)
    discrepancies: list[dict[str, Any]] = []
    if not view.board_current:
        discrepancies.append(
            _discrepancy(
                "master-record",
                "stale-board",
                "board",
                recorded=view.board["source"]["log_digest"],
                observed=view.expected_board["source"]["log_digest"],
                detail="derived board does not represent the authoritative event log",
            )
        )

    inspected: list[tuple[Path, RepositoryFact]] = []
    for repository, configured_root in sorted(repository_roots.items()):
        fact, findings = _inspect_repository(repository, configured_root, runner)
        discrepancies.extend(findings)
        inspected.append((_logical_absolute(configured_root), fact))

    for goal in view.expected_board["goals"]:
        matches: list[tuple[Path, RepositoryFact, WorktreeFact]] = []
        for root, fact in inspected:
            candidate = _candidate_path(root, goal["worktree"])
            if candidate is None:
                continue
            matches.extend(
                (root, fact, worktree)
                for worktree in fact.worktrees
                if worktree.path == candidate
            )
        if not matches:
            discrepancies.append(
                _discrepancy(
                    "workspace",
                    "missing-worktree",
                    f"goal:{goal['goal_id']}",
                    recorded=goal["goal_id"],
                    observed=None,
                    detail="recorded goal worktree is not registered in any configured repository",
                )
            )
            continue
        for root, fact, worktree in matches:
            worktree.goal_ids = tuple(sorted({*worktree.goal_ids, goal["goal_id"]}))
            branch_subject = f"goal:{goal['goal_id']}:branch"
            branch_exists = _ref_exists(root, goal["branch"], runner)
            if branch_exists is None:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "inaccessible-fact",
                        branch_subject,
                        recorded=goal["branch"],
                        observed=None,
                        detail="recorded goal branch ref could not be inspected",
                    )
                )
            elif not branch_exists:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "missing-ref",
                        branch_subject,
                        recorded=goal["branch"],
                        observed=None,
                        detail="recorded goal branch ref is missing",
                    )
                )
            elif worktree.branch != goal["branch"]:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "head-drift",
                        branch_subject,
                        recorded=goal["branch"],
                        observed=worktree.branch or "detached",
                        detail="registered worktree branch differs from the board claim",
                    )
                )
            checkpoint_subject = f"goal:{goal['goal_id']}:checkpoint"
            checkpoint_exists = _commit_exists(root, goal["checkpoint"], runner)
            if checkpoint_exists is None:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "inaccessible-fact",
                        checkpoint_subject,
                        recorded=goal["checkpoint"],
                        observed=None,
                        detail="recorded goal checkpoint could not be inspected",
                    )
                )
            elif not checkpoint_exists:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "missing-ref",
                        checkpoint_subject,
                        recorded=goal["checkpoint"],
                        observed=None,
                        detail="recorded goal checkpoint commit is missing",
                    )
                )
            elif worktree.head != goal["checkpoint"]:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "head-drift",
                        checkpoint_subject,
                        recorded=goal["checkpoint"],
                        observed=worktree.head,
                        detail="registered worktree HEAD differs from the board checkpoint",
                    )
                )

    for _, fact in inspected:
        for worktree in fact.worktrees:
            if not worktree.primary and not worktree.goal_ids:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "missing-worktree",
                        f"unrecorded:{worktree.public_id}",
                        recorded=None,
                        observed="registered",
                        detail="registered non-primary worktree has no current board goal claim",
                    )
                )

    discrepancies.sort(
        key=lambda row: (
            row["repository"],
            row["kind"],
            row["subject"],
            row["recorded"] or "",
            row["observed"] or "",
        )
    )
    repositories = tuple(
        fact.summary() for _, fact in sorted(inspected, key=lambda item: item[1].repository)
    )
    return ReconciliationResult(repositories, tuple(discrepancies))
