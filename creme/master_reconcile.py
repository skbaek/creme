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

# A recorded checkpoint is free prose as often as it is a bare object name, so a
# ref claim inside prose is recognized conservatively: only a standalone
# full-length lowercase SHA-1 object name counts. That length is what Git prints
# for a complete object name and is the one shape this record's prose does not
# also use for other data. Shorter runs are words ("effaced"), decimal data,
# abbreviated merkle roots, and 32-hex record event ids; 64-hex runs are content
# digests; longer runs are a name glued to a neighbouring word by compaction.
# `0x` data is EVM, never a Git object.
_PROSE_OBJECT_NAME = re.compile(r"(?<![0-9a-f])(?<!0x)(?<!0X)[0-9a-f]{40}(?![0-9a-f])")
CENSUS_REPOSITORY = "master-record"
CENSUS_SUBJECT = "reconciliation-census"


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


@dataclass(frozen=True)
class CheckpointClaim:
    """What a recorded goal checkpoint asserts about Git objects."""

    commit: Optional[str]
    candidates: tuple[str, ...]


def _is_object_name_claim(token: str) -> bool:
    # An all-decimal run is a count, a date, or a quantity; no object name Git
    # printed is free of a-f at this length.
    return any(character in "abcdef" for character in token)


def checkpoint_claim(checkpoint: str) -> CheckpointClaim:
    """Classify a recorded checkpoint into the ref claims it actually makes.

    A field that is exactly one object name is a bare commit claim. Anything
    else is prose, which claims only that the full object names it cites exist
    somewhere in the configured workspace. Prose that cites no full object name
    makes no ref claim at all and is never reported as a missing ref.
    """
    text = checkpoint.strip()
    if _COMMIT.fullmatch(text) is not None:
        return CheckpointClaim(text, ())
    candidates: list[str] = []
    for match in _PROSE_OBJECT_NAME.finditer(text):
        token = match.group(0)
        if not _is_object_name_claim(token) or token in candidates:
            continue
        candidates.append(token)
    return CheckpointClaim(None, tuple(candidates))


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
        # A nonzero rev-list is not evidence that the upstream disappeared.
        # Recheck the exact ref with Git's quiet presence contract so permission,
        # corruption, timeout-like, and other failures remain inaccessible.
        presence = runner(root, ["show-ref", "--verify", "--quiet", upstream])
        upstream_exists = _quiet_presence(presence, "Git upstream ref")
        if not upstream_exists:
            return upstream, True, None, None
        raise ReconciliationError("Git ahead/behind counts are inaccessible")
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


def _quiet_presence(result: GitResult, what: str) -> bool:
    if result.returncode == 0 and not result.stdout and not result.stderr:
        return True
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return False
    raise ReconciliationError(f"{what} presence is inaccessible")


def _ref_exists(root: Path, ref: str, runner: GitRunner) -> Optional[bool]:
    try:
        result = runner(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{ref}"])
    except ReconciliationError:
        return None
    try:
        return _quiet_presence(result, "Git branch ref")
    except ReconciliationError:
        return None


def _commit_exists(root: Path, commit: str, runner: GitRunner) -> Optional[bool]:
    if _COMMIT.fullmatch(commit) is None:
        return False
    try:
        result = runner(root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    except ReconciliationError:
        return None
    if result.returncode == 0 and not result.stdout and not result.stderr:
        return True
    expected_missing = f"fatal: Not a valid object name {commit}^{{commit}}\n".encode("ascii")
    if result.returncode == 128 and not result.stdout and result.stderr == expected_missing:
        return False
    return None


def _object_exists(root: Path, token: str, runner: GitRunner) -> Optional[bool]:
    """Whether `token` names an object this repository holds.

    A prose candidate may abbreviate a commit, so the peeling form used for a
    bare checkpoint is deliberately not used here: the question is only whether
    Git knows the named object. Only Git's own quiet contracts decide absence;
    anything else, including an ambiguous abbreviation, stays unknown.
    """
    try:
        result = runner(root, ["cat-file", "-e", token])
    except ReconciliationError:
        return None
    if result.stdout:
        return None
    if result.returncode == 0 and not result.stderr:
        return True
    if result.returncode == 1 and not result.stderr:
        # A well-formed object name that this repository does not hold.
        return False
    missing = f"fatal: Not a valid object name {token}\n".encode("ascii")
    if result.returncode == 128 and result.stderr == missing:
        # No object in this repository matches the named prefix.
        return False
    return None


class _WorkspaceObjects:
    """Memoized `exists anywhere in the configured workspace` resolution."""

    def __init__(self, roots: Sequence[Path], runner: GitRunner) -> None:
        self._roots = tuple(roots)
        self._runner = runner
        self._objects: dict[str, Optional[bool]] = {}
        self._commits: dict[str, Optional[bool]] = {}

    def _resolve(
        self,
        token: str,
        probe: Callable[[Path, str, GitRunner], Optional[bool]],
        cache: dict[str, Optional[bool]],
    ) -> Optional[bool]:
        if token in cache:
            return cache[token]
        verdict: Optional[bool] = False if self._roots else None
        for root in self._roots:
            present = probe(root, token, self._runner)
            if present:
                verdict = True
                break
            if present is None:
                verdict = None
        cache[token] = verdict
        return verdict

    def resolve_object(self, token: str) -> Optional[bool]:
        return self._resolve(token, _object_exists, self._objects)

    def resolve_commit(self, commit: str) -> Optional[bool]:
        return self._resolve(commit, _commit_exists, self._commits)


def _claim_text(candidates: Sequence[str], *, limit: int = 8) -> str:
    shown = list(candidates[:limit])
    text = " ".join(shown)
    remaining = len(candidates) - len(shown)
    if remaining:
        text = f"{text} (+{remaining} further candidates)"
    return text


def _sort_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        row["repository"],
        row["kind"],
        row["subject"],
        row["recorded"] or "",
        row["observed"] or "",
    )


def _census(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, int]]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["repository"], row["kind"])
        counts[key] = counts.get(key, 0) + 1
    return [(repository, kind, counts[(repository, kind)]) for repository, kind in sorted(counts)]


def _census_row(rows: Sequence[Mapping[str, Any]], retained: int, limit: int) -> dict[str, Any]:
    total = len(rows)
    census = _census(rows)
    head = (
        f"observed reconciliation exceeds the {limit}-row record cap; "
        f"{total} discrepancies observed and {retained} representative rows recorded, "
        f"covering every observed repository and kind; the census follows and is "
        f"authoritative for the counts"
    )
    budget = master_runtime.MAX_TEXT_BYTES - 256
    detail = head
    shown = 0
    for repository, kind, count in census:
        entry = f"; {repository}/{kind}={count}"
        if len(detail.encode("utf-8")) + len(entry.encode("utf-8")) > budget:
            break
        detail += entry
        shown += 1
    omitted = len(census) - shown
    if omitted:
        detail += f"; census lists {shown} of {len(census)} groups, {omitted} omitted"
    return {
        "repository": CENSUS_REPOSITORY,
        "kind": "inaccessible-fact",
        "subject": CENSUS_SUBJECT,
        "recorded": f"observed={total}",
        "observed": f"recorded={retained}",
        "detail": detail,
    }


def _representative_selection(
    rows: Sequence[Mapping[str, Any]],
    budget: int,
) -> list[dict[str, Any]]:
    """Round-robin one row per (repository, kind) group until the budget is spent."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["repository"], row["kind"]), []).append(row)
    order = sorted(groups)
    selected: list[dict[str, Any]] = []
    index = 0
    while len(selected) < budget:
        progressed = False
        for key in order:
            bucket = groups[key]
            if index >= len(bucket):
                continue
            selected.append(dict(bucket[index]))
            progressed = True
            if len(selected) >= budget:
                break
        if not progressed:
            break
        index += 1
    selected.sort(key=_sort_key)
    return selected


def summarize_for_record(
    result: ReconciliationResult,
    *,
    limit: int = master_runtime.MAX_LIST_ITEMS,
) -> list[dict[str, Any]]:
    """Bound observed discrepancies to what one record event may carry.

    Under the cap the observed rows are recorded unchanged. Over it, the event
    carries an explicit census row stating the observed total and every
    repository/kind count, followed by a representative selection that shows at
    least one row of every observed group. The count is therefore never
    understated and the record never silently drops what was observed.
    """
    if limit < 1:
        raise ReconciliationError("record reconciliation limit must be positive")
    rows = [dict(row) for row in result.discrepancies]
    if len(rows) <= limit:
        return rows
    selected = _representative_selection(rows, limit - 1)
    return [_census_row(rows, len(selected), limit), *selected]


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

    objects = _WorkspaceObjects(
        [root for root, fact in inspected if fact.status == "OK"], runner
    )

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
        claim = checkpoint_claim(goal["checkpoint"])
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
            if claim.commit is None:
                continue
            checkpoint_subject = f"goal:{goal['goal_id']}:checkpoint"
            checkpoint_exists = _commit_exists(root, claim.commit, runner)
            if checkpoint_exists is not True:
                # A goal may be recorded with worktrees in several configured
                # repositories while its checkpoint names a commit in one of
                # them, so absence is only asserted workspace-wide. A commit the
                # workspace holds elsewhere is HEAD drift here, never a missing
                # ref.
                elsewhere = objects.resolve_commit(claim.commit)
                if elsewhere is True:
                    # This repository does not hold the commit, so it cannot be
                    # this worktree HEAD.
                    discrepancies.append(
                        _discrepancy(
                            fact.repository,
                            "head-drift",
                            checkpoint_subject,
                            recorded=claim.commit,
                            observed=worktree.head,
                            detail=(
                                "board checkpoint commit is held by another configured "
                                "repository and is not this worktree HEAD"
                            ),
                        )
                    )
                    continue
                if elsewhere is None or checkpoint_exists is None:
                    discrepancies.append(
                        _discrepancy(
                            fact.repository,
                            "inaccessible-fact",
                            checkpoint_subject,
                            recorded=claim.commit,
                            observed=None,
                            detail="recorded goal checkpoint could not be inspected",
                        )
                    )
                    continue
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "missing-ref",
                        checkpoint_subject,
                        recorded=claim.commit,
                        observed=None,
                        detail=(
                            "recorded goal checkpoint commit is missing from every "
                            "configured repository"
                        ),
                    )
                )
            elif worktree.head != claim.commit:
                discrepancies.append(
                    _discrepancy(
                        fact.repository,
                        "head-drift",
                        checkpoint_subject,
                        recorded=claim.commit,
                        observed=worktree.head,
                        detail="registered worktree HEAD differs from the board checkpoint",
                    )
                )
        if claim.commit is None and claim.candidates:
            # A prose checkpoint names no repository, so each full object name
            # it cites is asserted against the whole configured workspace. Every
            # cited name is judged on its own: one name that resolves does not
            # excuse another that resolves nowhere.
            verdicts = [objects.resolve_object(token) for token in claim.candidates]
            absent = [
                token
                for token, verdict in zip(claim.candidates, verdicts)
                if verdict is False
            ]
            unknown = any(verdict is None for verdict in verdicts)
            subject = f"goal:{goal['goal_id']}:checkpoint"
            recorded = _claim_text(absent or claim.candidates)
            if not absent and not unknown:
                pass
            elif not absent:
                discrepancies.append(
                    _discrepancy(
                        "workspace",
                        "inaccessible-fact",
                        subject,
                        recorded=recorded,
                        observed=None,
                        detail=(
                            "recorded goal checkpoint cites a full object name that "
                            "could not be inspected in any configured repository"
                        ),
                    )
                )
            else:
                discrepancies.append(
                    _discrepancy(
                        "workspace",
                        "missing-ref",
                        subject,
                        recorded=recorded,
                        observed=None,
                        detail=(
                            "recorded goal checkpoint cites a full object name that "
                            "resolves in no configured repository"
                        ),
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

    discrepancies.sort(key=_sort_key)
    repositories = tuple(
        fact.summary() for _, fact in sorted(inspected, key=lambda item: item[1].repository)
    )
    return ReconciliationResult(repositories, tuple(discrepancies))
