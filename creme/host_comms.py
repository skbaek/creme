"""Cross-host message channel between masters, beside (never inside) the goal stack.

Masters on different hosts send each other requests, answers and notices on
a channel-only branch (default ``host-comms``) of the goal store's origin,
checked out as a worktree at ``$GOAL_STORE/.host-comms``. Messages never
change goal order or status; decisions and status stay in the stack and the
goals.

Every message is one immutable file
``messages/<UTC-timestamp>-<from-host>-<short-id>.md`` (``+++`` front matter
in the stack's portable TOML subset, Markdown body). An acknowledgement is
its own file ``acks/<message-id>/<host>.toml``; nothing is ever edited or
deleted. Because hosts only add distinct paths, a rejected push is resolved
by fetching and rebasing the never-published local commit onto the new head,
which cannot conflict, and pushing again a bounded number of times.

Writes (send, ack) authenticate through the master lease exactly as stack
mutations do. Reads (inbox, show, thread, watch) never acquire; they fetch,
fast-forward this host's copy and attach the worktree locally on first use.
Git plumbing comes from :mod:`creme.goal_stack_git`.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import goal_stack, goal_stack_git as shared, goal_stack_store as store
from . import master_runtime as runtime, semaphore

COMMS_DIR = ".host-comms"
DEFAULT_BRANCH = "host-comms"
DEFAULT_REMOTE = shared.DEFAULT_REMOTE
MESSAGES = "messages"
ACKS = "acks"
LOCK_NAME = "creme-host-comms.lock"
ALL = "all"
DEFAULT_INTERVAL = 60
MAX_PUBLISH_ATTEMPTS = 6
MAX_SUBJECT_LEN = 200
MAX_NOTE_LEN = 2000
MAX_BODY_BYTES = 64 * 1024
MAX_FILE_BYTES = MAX_BODY_BYTES + 8 * 1024
FENCE = "+++"

ID_RE = re.compile(r"(\d{8}T\d{6}Z)-([a-z][a-z0-9-]{0,31})-([0-9a-f]{8})")
STAMP = "%Y%m%dT%H%M%SZ"
CREATED = "%Y-%m-%dT%H:%M:%SZ"

README = (
    "# Host comms\n\n"
    "Messages between the masters of different hosts. One immutable file per\n"
    "message under `messages/`, acknowledgements under `acks/<id>/<host>.toml`.\n"
    "Never edit or delete a file here; use `python3 -m creme master comms`.\n"
    "Goal order and status live in the goal stack, never here.\n"
)


class CommsError(ValueError):
    """A host-comms operation refused."""


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    to: str
    subject: str
    created: str
    body: str
    path: str
    in_reply_to: Optional[str] = None
    goal: Optional[str] = None

    def to_dict(self) -> dict:
        value = {"id": self.id, "from": self.sender, "to": self.to, "subject": self.subject,
                 "created": self.created, "path": self.path, "body": self.body}
        if self.in_reply_to is not None:
            value["in_reply_to"] = self.in_reply_to
        if self.goal is not None:
            value["goal"] = self.goal
        return value


@dataclass(frozen=True)
class Ack:
    message: str
    host: str
    acked: str
    note: Optional[str] = None

    def to_dict(self) -> dict:
        value = {"message": self.message, "host": self.host, "acked": self.acked}
        if self.note is not None:
            value["note"] = self.note
        return value


@dataclass
class Channel:
    """Everything on this host's copy; ``problems`` are (path, reason) pairs."""

    messages: dict = field(default_factory=dict)
    acks: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    attached: bool = True

    def acked_by(self, message_id: str, host: str) -> Optional[Ack]:
        return self.acks.get(message_id, {}).get(host)


# -- names, time and parsing ------------------------------------------------


def require_host(host: Optional[str] = None) -> str:
    host = host if host is not None else store.local_host_name()
    if host is None:
        raise CommsError("this host has no name; host comms refuses unnamed hosts "
                         "(python3 -m creme set-host-name NAME)")
    if host == ALL or goal_stack.HOST_RE.fullmatch(host) is None:
        raise CommsError(f"host name {host!r} is invalid (want [a-z][a-z0-9-]{{0,31}}, not 'all')")
    return host


def _utc(now: Optional[datetime]) -> datetime:
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise CommsError("comms clock must be timezone-aware")
    return now.astimezone(timezone.utc)


def parse_created(text: str) -> datetime:
    return datetime.strptime(text, CREATED).replace(tzinfo=timezone.utc)


def age(created: str, now: Optional[datetime] = None) -> str:
    try:
        seconds = int((_utc(now) - parse_created(created)).total_seconds())
    except ValueError:
        return "?"
    seconds = max(seconds, 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def one_line(text: str) -> str:
    return " ".join(text.split())


def _check_text(name: str, value, *, max_len: int, single_line: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommsError(f"{name} must be a nonempty string")
    if len(value) > max_len:
        raise CommsError(f"{name} exceeds {max_len} characters")
    if single_line and ("\n" in value or "\r" in value):
        raise CommsError(f"{name} must be one line")
    return value


def _front(fields: list[tuple[str, Optional[str]]]) -> str:
    return "".join(f"{key} = {goal_stack._toml_str(value)}\n"
                   for key, value in fields if value is not None)


def render_message(message: Message) -> bytes:
    front = _front([("id", message.id), ("from", message.sender), ("to", message.to),
                    ("subject", message.subject), ("in_reply_to", message.in_reply_to),
                    ("goal", message.goal), ("created", message.created)])
    body = message.body if message.body.endswith("\n") else message.body + "\n"
    return f"{FENCE}\n{front}{FENCE}\n\n{body}".encode("utf-8")


def _table(text: str, what: str) -> dict:
    try:
        table = goal_stack.parse(text)
    except ValueError as exc:
        raise CommsError(f"{what}: {exc}") from exc
    for key, value in table.items():
        if not isinstance(value, str):
            raise CommsError(f"{what}: {key} must be a string")
    return table


def parse_message(relative: str, data: bytes) -> Message:
    """Parse and check one message file; unknown front-matter keys are ignored."""
    stem = Path(relative).name[:-len(".md")] if relative.endswith(".md") else None
    match = ID_RE.fullmatch(stem or "")
    if match is None:
        raise CommsError("file name is not <UTC-timestamp>-<host>-<8 hex>.md")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise CommsError(f"not UTF-8: {exc}") from exc
    lines = text.split("\n")
    if not lines or lines[0] != FENCE or FENCE not in lines[1:]:
        raise CommsError(f"missing {FENCE} front matter")
    end = lines.index(FENCE, 1)
    table = _table("\n".join(lines[1:end]), "front matter")
    missing = sorted({"id", "from", "to", "subject", "created"} - set(table))
    if missing:
        raise CommsError(f"front matter lacks {', '.join(missing)}")
    if table["id"] != stem:
        raise CommsError(f"id {table['id']!r} does not match the file name")
    if table["from"] != match.group(2):
        raise CommsError(f"from {table['from']!r} does not match the file name")
    if table["to"] != ALL and goal_stack.HOST_RE.fullmatch(table["to"]) is None:
        raise CommsError(f"to {table['to']!r} is neither a host name nor 'all'")
    _check_text("subject", table["subject"], max_len=MAX_SUBJECT_LEN, single_line=True)
    try:
        parse_created(table["created"])
    except ValueError as exc:
        raise CommsError(f"created {table['created']!r} is not {CREATED}") from exc
    reply = table.get("in_reply_to")
    if reply is not None and ID_RE.fullmatch(reply) is None:
        raise CommsError(f"in_reply_to {reply!r} is not a message id")
    goal = table.get("goal")
    if goal is not None and goal_stack.ID_RE.fullmatch(goal) is None:
        raise CommsError(f"goal {goal!r} is not a goal ID")
    body = "\n".join(lines[end + 1:])
    body = body[1:] if body.startswith("\n") else body
    return Message(id=stem, sender=table["from"], to=table["to"], subject=table["subject"],
                   created=table["created"], body=body, path=relative,
                   in_reply_to=reply, goal=goal)


def render_ack(item: Ack) -> bytes:
    return _front([("message", item.message), ("host", item.host),
                   ("acked", item.acked), ("note", item.note)]).encode("utf-8")


def parse_ack(relative: str, data: bytes) -> Ack:
    parts = Path(relative).parts
    if (len(parts) != 3 or ID_RE.fullmatch(parts[1]) is None or not parts[2].endswith(".toml")
            or goal_stack.HOST_RE.fullmatch(parts[2][:-len(".toml")]) is None):
        raise CommsError("ack path is not acks/<message-id>/<host>.toml")
    try:
        table = _table(data.decode("utf-8"), "ack")
    except UnicodeError as exc:
        raise CommsError(f"not UTF-8: {exc}") from exc
    if table.get("message") != parts[1] or table.get("host") != parts[2][:-len(".toml")]:
        raise CommsError("ack message/host do not match its path")
    try:
        parse_created(table.get("acked", ""))
    except ValueError as exc:
        raise CommsError(f"acked is not {CREATED}") from exc
    note = table.get("note")
    if note is not None:
        _check_text("note", note, max_len=MAX_NOTE_LEN, single_line=False)
    return Ack(message=parts[1], host=table["host"], acked=table["acked"], note=note)


def _regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise CommsError("not a regular file")
    if info.st_size > MAX_FILE_BYTES:
        raise CommsError(f"exceeds {MAX_FILE_BYTES} bytes")
    return path.read_bytes()


def load(worktree: Path) -> Channel:
    """Parse this host's copy; malformed files become problems, never crashes."""
    channel = Channel()
    directory = worktree / MESSAGES
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.iterdir()):
            relative = f"{MESSAGES}/{path.name}"
            try:
                message = parse_message(relative, _regular(path))
            except (CommsError, OSError) as exc:
                channel.problems.append((relative, str(exc)))
                continue
            channel.messages[message.id] = message
    directory = worktree / ACKS
    if directory.is_dir() and not directory.is_symlink():
        for folder in sorted(directory.iterdir()):
            paths = sorted(folder.iterdir()) if folder.is_dir() and not folder.is_symlink() else [folder]
            for path in paths:
                relative = path.relative_to(worktree).as_posix()
                try:
                    item = parse_ack(relative, _regular(path))
                except (CommsError, OSError) as exc:
                    channel.problems.append((relative, str(exc)))
                    continue
                channel.acks.setdefault(item.message, {})[item.host] = item
    return channel


def _ordered(messages) -> list[Message]:
    return sorted(messages, key=lambda m: (m.created, m.id))


def addressed(message: Message, host: str) -> bool:
    return message.sender != host and message.to in {host, ALL}


def inbox(channel: Channel, host: str, *, include_all: bool = False) -> list[Message]:
    """Unacknowledged messages for ``host`` (or, with ``include_all``, every one)."""
    if include_all:
        return _ordered(channel.messages.values())
    return _ordered(m for m in channel.messages.values()
                    if addressed(m, host) and channel.acked_by(m.id, host) is None)


def thread(channel: Channel, message_id: str) -> list[Message]:
    """The whole conversation containing ``message_id``, oldest first."""
    if message_id not in channel.messages:
        raise CommsError(f"unknown message id: {message_id}")
    root, visited = message_id, {message_id}
    while True:
        parent = channel.messages[root].in_reply_to
        if parent is None or parent not in channel.messages or parent in visited:
            break
        root = parent
        visited.add(root)
    children: dict[str, list[str]] = {}
    for message in channel.messages.values():
        if message.in_reply_to is not None:
            children.setdefault(message.in_reply_to, []).append(message.id)
    found, pending = {root}, [root]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return _ordered(channel.messages[i] for i in found)


# -- worktree, lock and sync ------------------------------------------------


def worktree_path(root: Path) -> Path:
    return Path(root) / COMMS_DIR


@contextlib.contextmanager
def _lock(root: Path) -> Iterator[None]:
    """Serialize this host's git work on the channel (readers fetch too)."""
    common = Path(shared.out(root, "rev-parse", "--git-common-dir"))
    path = common if common.is_absolute() else Path(root) / common
    descriptor = os.open(path / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _ignore(root: Path) -> None:
    """Ignore the worktree in this clone only, so no tracked file changes."""
    if shared.git(root, "check-ignore", "-q", "--no-index", "--",
                  f"{COMMS_DIR}/x", check=False).returncode == 0:
        return
    exclude = Path(shared.out(root, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else Path(root) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_bytes() if exclude.exists() else b""
    prefix = b"" if not existing or existing.endswith(b"\n") else b"\n"
    exclude.write_bytes(existing + prefix + b"/" + COMMS_DIR.encode() + b"/\n")


def _create_branch(root: Path, remote: str, branch: str, host: str) -> bool:
    """Publish the channel's history-free first commit; False if another host won."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        tree = shared.write_tree(root, {"README.md": README.encode()}, env)
    first = shared.commit_tree(root, tree, f"host-comms: open the channel (host {host})")
    pushed = shared.git(root, "push", "--quiet", remote, f"{first}:refs/heads/{branch}", check=False)
    return pushed.returncode == 0


def attach(root: Path, *, create: bool, host: str, remote: str = DEFAULT_REMOTE,
           branch: str = DEFAULT_BRANCH) -> Optional[Path]:
    """This host's channel worktree, attaching (or with ``create``, opening) it.

    Returns None when the channel does not exist yet and ``create`` is false.
    Call with the channel lock held.
    """
    root = Path(root)
    path = worktree_path(root)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise CommsError(f"{path} must be a directory, not a link or file")
    if path.exists():
        current = shared.out(path, "symbolic-ref", "--short", "HEAD")
        if current != branch:
            raise CommsError(f"{COMMS_DIR} has {current!r} checked out, not {branch!r}")
        return path
    if not shared.remote_branch_exists(root, remote, branch):
        if not create:
            return None
        if not _create_branch(root, remote, branch, host) and not \
                shared.remote_branch_exists(root, remote, branch):
            raise CommsError(f"could not publish {remote}/{branch}; channel not opened")
    tracking = shared.tracking_ref(remote, branch)
    shared.git(root, "fetch", "--quiet", remote, f"refs/heads/{branch}:{tracking}")
    canonical = shared.out(root, "rev-parse", "--verify", tracking)
    local = shared.git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if local.returncode == 0:
        if not shared.is_ancestor(root, local.stdout.decode().strip(), canonical):
            raise CommsError(f"local branch {branch!r} diverges from {remote}/{branch}")
        shared.git(root, "branch", "-f", branch, canonical)
    else:
        shared.git(root, "branch", branch, canonical)
    shared.configure_upstream(root, remote, branch)
    _ignore(root)
    shared.git(root, "worktree", "add", "--quiet", COMMS_DIR, branch)
    return path


REMEDY = "move them out of " + COMMS_DIR + "/ (messages are only written by `master comms`)"


def _rebase(worktree: Path, onto: str) -> None:
    result = shared.git(worktree, "-c", "commit.gpgsign=false", "rebase", "--quiet", onto, check=False)
    if result.returncode:
        shared.git(worktree, "rebase", "--abort", check=False)
        raise CommsError("could not rebase unpublished channel commits: "
                         + result.stderr.decode("utf-8", "replace").strip())


def changed_paths(worktree: Path) -> list[tuple[str, str]]:
    """Paths any commit on this copy modified or deleted after adding them."""
    listed = shared.out(worktree, "log", "--format=", "--name-status", "--no-renames",
                        "--diff-filter=MDT", "HEAD")
    problems: dict[str, str] = {}
    for line in listed.splitlines():
        status, _, path = line.partition("\t")
        if path and path not in problems:
            problems[path] = f"changed after publication (git status {status}); files are immutable"
    return sorted(problems.items())


def sync(worktree: Path) -> None:
    """Fetch, then fast-forward or rebase local-only (unpublished) commits."""
    shared.require_clean(worktree, REMEDY)
    remote_head = shared.fetch(worktree)
    local = shared.head(worktree)
    if local == remote_head or shared.is_ancestor(worktree, remote_head, local):
        return
    if shared.is_ancestor(worktree, local, remote_head):
        shared.git(worktree, "merge", "--ff-only", "--quiet", remote_head)
    else:
        _rebase(worktree, remote_head)


def read_channel(root: Path, host: Optional[str] = None, *, remote: str = DEFAULT_REMOTE,
                 branch: str = DEFAULT_BRANCH) -> Channel:
    """Fetch and read the channel without acquiring the master lease."""
    host = require_host(host)
    try:
        with _lock(root):
            worktree = attach(root, create=False, host=host, remote=remote, branch=branch)
            if worktree is None:
                return Channel(attached=False)
            sync(worktree)
            problems = changed_paths(worktree)
            channel = load(worktree)
    except shared.SharedStackError as exc:
        raise CommsError(str(exc)) from exc
    channel.problems = problems + channel.problems
    return channel


# -- authenticated writes ---------------------------------------------------


def _authorized(record_root: Path, writer, action: Callable[[], dict]) -> dict:
    """Run ``action`` inside the master lease transaction stack writes use."""
    writer = writer or runtime.RecordWriter(record_root)
    if writer.root != record_root:
        raise CommsError("writer and channel must use the same master record")
    runtime._renew_or_refuse(writer.renew)
    runtime._normalize_lock_prerequisites(record_root)
    with runtime._locked_record(record_root):
        try:
            with writer.authority_transaction() as lease:
                runtime._actor_from_snapshot(lease)
                return action()
        except semaphore.MasterAuthorityRefused as exc:
            raise runtime.RenewalRefused(f"master renewal refused: {exc}") from exc


def _drop_tip(worktree: Path) -> None:
    shared.git(worktree, "reset", "--hard", "--quiet", "HEAD~1")


def publish(worktree: Path, files: dict[str, bytes], message: str) -> str:
    """Commit new files and push without force, rebasing on rejection.

    The new commit is always the tip; when publication fails it is dropped,
    so a refused write leaves nothing behind.
    """
    for relative in files:
        if (worktree / relative).exists() or (worktree / relative).is_symlink():
            raise CommsError(f"{relative} already exists; channel files are immutable")
    written = []
    try:
        for relative, data in files.items():
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            written.append(path)
        shared.git(worktree, "add", "--", *files)
        shared.git(worktree, "-c", "commit.gpgsign=false", "commit", "--no-verify", "--quiet",
                   "-m", message)
    except shared.SharedStackError:
        shared.git(worktree, "reset", "--quiet", check=False)
        for path in written:
            path.unlink(missing_ok=True)
        raise
    for _ in range(MAX_PUBLISH_ATTEMPTS):
        published, detail = shared.push(worktree)
        if published:
            return shared.head(worktree)
        try:
            remote_head = shared.fetch(worktree)
        except shared.SharedStackError as exc:
            _drop_tip(worktree)
            raise CommsError(f"could not publish; nothing sent: {exc}") from exc
        if shared.is_ancestor(worktree, remote_head, "HEAD"):
            _drop_tip(worktree)
            raise CommsError(f"could not publish; nothing sent: {detail}")
        # Another host published first. Only new, distinct paths exist on
        # either side, so the rebase cannot conflict.
        try:
            _rebase(worktree, remote_head)
        except CommsError:
            _drop_tip(worktree)
            raise
    _drop_tip(worktree)
    raise CommsError("channel kept advancing on other hosts; nothing sent; retry")


def new_id(host: str, now: datetime) -> str:
    return f"{now.strftime(STAMP)}-{host}-{secrets.token_hex(4)}"


def send(record_root: Path, *, to: str, subject: str, body: str, goal: Optional[str] = None,
         in_reply_to: Optional[str] = None, writer=None, host: Optional[str] = None,
         now: Optional[datetime] = None, remote: str = DEFAULT_REMOTE,
         branch: str = DEFAULT_BRANCH) -> dict:
    """Publish one message (master only); opens the channel on first use."""
    host = require_host(host)
    if to != ALL and goal_stack.HOST_RE.fullmatch(to or "") is None:
        raise CommsError(f"--to {to!r} must be a host name or 'all'")
    if to == host:
        raise CommsError("a master does not message its own host")
    _check_text("subject", subject, max_len=MAX_SUBJECT_LEN, single_line=True)
    if not isinstance(body, str) or not body.strip():
        raise CommsError("body must be nonempty")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise CommsError(f"body exceeds {MAX_BODY_BYTES} bytes")
    if goal is not None and goal_stack.ID_RE.fullmatch(goal) is None:
        raise CommsError(f"goal {goal!r} is not a goal ID")
    if in_reply_to is not None and ID_RE.fullmatch(in_reply_to) is None:
        raise CommsError(f"in-reply-to {in_reply_to!r} is not a message id")
    stamp = _utc(now)
    root = record_root.parent

    def action() -> dict:
        with _lock(root):
            worktree = attach(root, create=True, host=host, remote=remote, branch=branch)
            sync(worktree)
            if in_reply_to is not None and in_reply_to not in load(worktree).messages:
                raise CommsError(f"in-reply-to {in_reply_to} is not on the channel")
            message = Message(id=new_id(host, stamp), sender=host, to=to, subject=subject,
                              created=stamp.strftime(CREATED), body=body, path="",
                              in_reply_to=in_reply_to, goal=goal)
            relative = f"{MESSAGES}/{message.id}.md"
            data = render_message(message)
            parse_message(relative, data)
            commit = publish(worktree, {relative: data},
                             f"host-comms: {message.id} to {to}: {one_line(subject)[:72]}")
            return {"status": "OK", "id": message.id, "path": relative, "to": to,
                    "host": host, "commit": commit}

    try:
        return _authorized(record_root, writer, action)
    except shared.SharedStackError as exc:
        raise CommsError(str(exc)) from exc


def ack(record_root: Path, message_id: str, *, note: Optional[str] = None, writer=None,
        host: Optional[str] = None, now: Optional[datetime] = None,
        remote: str = DEFAULT_REMOTE, branch: str = DEFAULT_BRANCH) -> dict:
    """Acknowledge a message addressed to this host (master only)."""
    host = require_host(host)
    if ID_RE.fullmatch(message_id or "") is None:
        raise CommsError(f"{message_id!r} is not a message id")
    if note is not None:
        _check_text("note", note, max_len=MAX_NOTE_LEN, single_line=False)
    stamp = _utc(now)
    root = record_root.parent

    def action() -> dict:
        with _lock(root):
            worktree = attach(root, create=False, host=host, remote=remote, branch=branch)
            if worktree is None:
                raise CommsError("no host-comms channel yet; nothing to acknowledge")
            sync(worktree)
            channel = load(worktree)
            message = channel.messages.get(message_id)
            if message is None:
                raise CommsError(f"unknown message id: {message_id}")
            if not addressed(message, host):
                raise CommsError(f"{message_id} is not addressed to {host}")
            existing = channel.acked_by(message_id, host)
            if existing is not None:
                return {"status": "OK", "changed": False, "id": message_id, "host": host,
                        "ack": existing.to_dict()}
            item = Ack(message=message_id, host=host, acked=stamp.strftime(CREATED), note=note)
            relative = f"{ACKS}/{message_id}/{host}.toml"
            commit = publish(worktree, {relative: render_ack(item)},
                             f"host-comms: {host} acknowledges {message_id}")
            return {"status": "OK", "changed": True, "id": message_id, "host": host,
                    "ack": item.to_dict(), "commit": commit}

    try:
        return _authorized(record_root, writer, action)
    except shared.SharedStackError as exc:
        raise CommsError(str(exc)) from exc


# -- watch --------------------------------------------------------------------


def watch_line(message: Message) -> str:
    return f"COMMS {message.id} from={message.sender} subject={one_line(message.subject)}"


def watch(root: Path, *, host: Optional[str] = None, interval: float = DEFAULT_INTERVAL,
          once: bool = False, emit: Callable[[str], None] = print,
          sleep: Callable[[float], None] = time.sleep,
          remote: str = DEFAULT_REMOTE, branch: str = DEFAULT_BRANCH) -> int:
    """Print one line per newly arrived unacknowledged message for this host.

    The first pass reports every message still unacknowledged. Failures print
    one ``COMMS-ERROR`` line per streak (malformed files once each) and the
    loop continues; only ``once`` returns, nonzero after a failure.
    """
    seen: set[str] = set()
    reported: set[tuple[str, str]] = set()
    failing = False
    while True:
        try:
            channel = read_channel(root, host, remote=remote, branch=branch)
            failing = False
            for message in inbox(channel, require_host(host)):
                if message.id not in seen:
                    seen.add(message.id)
                    emit(watch_line(message))
            for problem in channel.problems:
                if problem not in reported:
                    reported.add(problem)
                    emit(f"COMMS-ERROR malformed {problem[0]}: {one_line(problem[1])}")
            status = 0
        except Exception as exc:  # a watcher outlives every transient failure
            status = 2
            if not failing:
                failing = True
                emit(f"COMMS-ERROR {one_line(str(exc)) or type(exc).__name__}")
        if once:
            return status
        sleep(interval)
