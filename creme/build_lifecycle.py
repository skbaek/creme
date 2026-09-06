"""Owned build transactions. No policy decisions or general process reclaimer.

The journal precedes admission. A random operation ID, carried by the hold,
settles ambiguous publication without ever releasing by label. A kernel lock
outlives uncertain API returns; a child inherits it until its recorded launch
gate opens. Recovery only observes absence and never signals a numeric PID.
"""
from __future__ import annotations

import codecs
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional

from . import semaphore


SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
# Integers have no destructor. Deliberately retain these descriptors until
# process exit on every uncertain path, including a same-process API return.
_LIFETIME_FDS: set[int] = set()


class TerminationSignals:
    """Remember cancellation in *every* phase; raise only at safe checkpoints.

    Handlers never raise asynchronously. Polling child I/O and queue waits call
    check(); finalization does not. Thus a first or repeated signal in any
    finalizer bytecode cannot unwind cleanup. Installation/restoration occurs
    under the calling thread's original POSIX mask, before/after resources.
    """
    def __init__(self) -> None:
        self.signum: Optional[int] = None
        self._prior: dict[int, Any] = {}

    def _interrupt(self, signum: int, _frame: Any) -> None:
        if self.signum is None:
            self.signum = signum

    def __enter__(self) -> "TerminationSignals":
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("owned build cancellation requires the main thread")
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        try:
            try:
                for signum in SIGNALS:
                    self._prior[signum] = signal.getsignal(signum)
                    signal.signal(signum, self._interrupt)
            except BaseException:
                self._restore()
                raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        return self

    def check(self) -> None:
        if self.signum is not None:
            raise KeyboardInterrupt

    def defer(self) -> None:
        # Compatibility for callers: handlers already defer all signals.
        pass

    def _restore(self) -> None:
        error: Optional[BaseException] = None
        # Finish the other restorations, then retry a transient API exception
        # once. Still report it; resource finalization has already happened.
        for _attempt in range(2):
            for signum, handler in list(self._prior.items()):
                try:
                    signal.signal(signum, handler)
                    del self._prior[signum]
                except BaseException as exc:
                    if error is None:
                        error = exc
            if not self._prior:
                break
        if error is not None:
            raise error

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        try:
            self._restore()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)


class OwnedThread(threading.Thread):
    """A start attempt is uncertainty until the native bootstrap completes."""
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.start_attempted = False
        self.bootstrap_finished = threading.Event()

    def start(self) -> None:
        self.start_attempted = True
        super().start()

    def _bootstrap_inner(self) -> None:
        try:
            super()._bootstrap_inner()
        finally:
            self.bootstrap_finished.set()


def stop_thread(worker: Any, name: str, timeout: float = 2.0) -> tuple[bool, str]:
    # Set the request before any join: it must survive a late native bootstrap.
    try:
        event = getattr(worker, "stop_event", None)
        if event is not None:
            event.set()
        if isinstance(worker, OwnedThread):
            if not worker.start_attempted:
                return True, f"{name} never attempted startup"
            if not worker._started.wait(timeout):
                return False, f"{name} startup has not acknowledged termination"
        worker.stop()
        if isinstance(worker, OwnedThread):
            if worker.is_alive():
                return False, f"{name} remained alive after stop"
            if not worker.bootstrap_finished.wait(timeout):
                return False, f"{name} native bootstrap has not finished"
            worker.join(timeout)
        alive = getattr(worker, "is_alive", None)
        if callable(alive) and alive():
            return False, f"{name} remained alive after stop"
    except BaseException as exc:
        return False, f"{name} termination unproved: {type(exc).__name__}: {exc}"
    return True, f"{name} stopped"


def communicate(proc: subprocess.Popen[str], cancellation: TerminationSignals,
                timeout: Optional[float] = None) -> tuple[str, str]:
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        cancellation.check()
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            return proc.communicate(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass


def output_lines(proc: subprocess.Popen[str], cancellation: TerminationSignals):
    """Keep live output while allowing cancellation of a silent child."""
    assert proc.stdout is not None
    try:
        fd = proc.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        # In-memory test streams have no descriptor.
        for line in proc.stdout:
            cancellation.check()
            yield line
        return
    decoder = codecs.getincrementaldecoder(proc.stdout.encoding or "utf-8")(errors="replace")
    pending = ""
    while True:
        cancellation.check()
        if not select.select([fd], [], [], 0.1)[0]:
            continue
        chunk = os.read(fd, 65536)
        pending += decoder.decode(chunk, final=not chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line + "\n"
        if not chunk:
            if pending:
                yield pending
            return


def _private(path: Path, directory: bool = False) -> os.stat_result:
    value = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(value.st_mode) or value.st_uid != os.getuid()
            or value.st_mode & 0o077 or (not directory and value.st_nlink != 1)):
        raise ValueError("operation path is not a private owned regular file/directory")
    return value


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for name, value in pairs:
        if name in record:
            raise ValueError("duplicate operation record field")
        record[name] = value
    return record


def _root(*, create: bool = True) -> Path:
    root = semaphore.state_root()
    if root.is_symlink():
        raise ValueError("semaphore root cannot be a symlink")
    # Existing state roots may have ordinary mkdir permissions in fixtures;
    # the semaphore's own mutex initialization secures this canonical root.
    if create:
        with semaphore.locked_state():
            pass
    _private(root, True)
    directory = root / "build-operations"
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    _private(directory, True)
    return directory


_GATE = """import os, sys
gate, lifetime = int(sys.argv[1]), int(sys.argv[2])
go = os.read(gate, 1)
os.close(gate)
if go != b'G':
    os._exit(125)
os.close(lifetime)
os.execv(sys.argv[3], sys.argv[3:])
"""


class BuildTransaction:
    def __init__(self, goal: str) -> None:
        self.id = uuid.uuid4().hex
        self.goal = goal
        self.root = _root()
        self.path = self.root / (self.id + ".json")
        self.lock_path = self.root / (self.id + ".lock")
        self.processes: list[subprocess.Popen[str]] = []
        self.retired_processes: list[subprocess.Popen[str]] = []
        self.helpers: list[tuple[Any, str]] = []
        self.errors: list[str] = []
        self.cleanup_proved = False
        self.release_result: Optional[tuple[bool, str]] = None
        self.finalization_started = False
        self.record: dict[str, Any] = {
            "schema_version": 1, "id": self.id, "goal": goal,
            "uid": os.getuid(), "wrapper_pid": os.getpid(), "groups": [],
            "pending_launch": False, "status": "active", "lock": {},
        }
        fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        # Never give this descriptor to an object whose destructor closes it.
        _LIFETIME_FDS.add(fd)
        self.fd = fd
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        value = os.fstat(fd)
        self.record["lock"] = {"device": value.st_dev, "inode": value.st_ino}
        self.save()

    @property
    def recovery(self) -> str:
        return f"python3 -m creme build-recover {self.id}"

    def save(self) -> None:
        semaphore._write_json(self.path, self.record)
        directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def launch(self, args: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        """Publish before executable work. EOF on an unopened gate means exit.

        A pending native fork inherits the lifetime lock, so recovery cannot
        mistake an unregistered startup child for absence on parent death.
        No preexec_fn is used in this threaded wrapper.
        """
        if self.finalization_started:
            raise RuntimeError("operation finalization has already started")
        self.record["pending_launch"] = True
        self.save()
        reader, writer = os.pipe()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _GATE, str(reader), str(self.fd), *args],
                pass_fds=(reader, self.fd), start_new_session=True, **kwargs,
            )
            self.processes.append(proc)
            self.record["groups"].append(proc.pid)
            self.save()
            os.write(writer, b"G")
            self.record["pending_launch"] = False
            self.save()
            return proc
        finally:
            os.close(writer)
            os.close(reader)

    def retire(self, proc: subprocess.Popen[str]) -> None:
        """Retire a proved-absent group before optional work or another launch.

        Keep its journal identity for conservative recovery, but never signal
        the old numeric group again during this transaction's finalization.
        """
        self.processes.remove(proc)
        self.retired_processes.append(proc)

    def finalize(self, terminate: Callable[[Any], bool], close: Callable[[Any], bool],
                 stop: Callable[[Any, str], tuple[bool, str]], fresh: bool) -> None:
        """One unconditional decision. Diagnostic failure cannot skip it."""
        if self.finalization_started:
            raise RuntimeError("operation finalization may only run once")
        self.finalization_started = True
        proved = not self.record["pending_launch"]
        try:
            # Stop renewal before the group is observed absent: no later helper
            # action may signal the old group or touch a subsequent hold.
            for helper, name in self.helpers:
                try:
                    ok, detail = stop(helper, name)
                    proved = ok and proved
                    if not ok:
                        self.errors.append(detail)
                except BaseException:
                    proved = False
            for proc in self.processes:
                try:
                    proved = terminate(proc) and proved
                except BaseException:
                    proved = False
            for proc in self.processes + self.retired_processes:
                # Pipe/diagnostic failures are not process-liveness evidence.
                try:
                    close(proc)
                except BaseException:
                    pass
        finally:
            self.cleanup_proved = proved
            if proved:
                try:
                    self.release_result = ((True, "no hold was taken") if fresh else
                        semaphore.adaptive_release(self.goal, operation_id=self.id))
                except BaseException as exc:
                    self.release_result = (False, f"release uncertainty: {type(exc).__name__}")
            self.record["status"] = (
                "finalized" if self.release_result and self.release_result[0] else "preserved"
            )
            # This receipt is useful even with a broken output sink. Its failure
            # cannot reverse or prevent an already proved hold decision.
            try:
                self.save()
            except BaseException:
                pass
            if self.record["status"] == "finalized":
                os.close(self.fd)
                _LIFETIME_FDS.discard(self.fd)


def recover(operation_id: str) -> tuple[bool, str]:
    """Non-signalling recovery; identity drift or any live group refuses."""
    if OPERATION_ID.fullmatch(operation_id) is None:
        return False, "operation ID must be exactly 32 lowercase hexadecimal characters"
    try:
        root = _root(create=False)
        lock_path = root / (operation_id + ".lock")
        _private(lock_path)
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            value = os.fstat(fd)
            if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                    or value.st_mode & 0o077 or value.st_nlink != 1):
                return False, "operation lifetime lock is not private"
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False, "original wrapper/helpers or unopened startup child remain alive; wait for wrapper exit"
            path = root / (operation_id + ".json")
            _private(path)
            journal_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(journal_fd, "r", encoding="utf-8") as journal:
                checked = os.fstat(journal.fileno())
                if (not stat.S_ISREG(checked.st_mode) or checked.st_uid != os.getuid()
                        or checked.st_mode & 0o077 or checked.st_nlink != 1):
                    raise ValueError("operation journal is not private")
                record = json.load(journal, object_pairs_hook=_unique_fields)
            keys = {"schema_version", "id", "goal", "uid", "wrapper_pid", "groups",
                    "pending_launch", "status", "lock"}
            if (not isinstance(record, dict) or set(record) != keys
                    or type(record["schema_version"]) is not int
                    or record["schema_version"] != 1 or record["id"] != operation_id
                    or type(record["uid"]) is not int or record["uid"] != os.getuid()
                    or not isinstance(record["goal"], str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", record["goal"]) is None
                    or type(record["wrapper_pid"]) is not int or not 1 <= record["wrapper_pid"] <= 2**31 - 1
                    or type(record["pending_launch"]) is not bool
                    or not isinstance(record["status"], str)
                    or record["status"] not in {"active", "preserved", "finalized"}
                    or not isinstance(record["groups"], list)
                    or any(type(pid) is not int or not 2 <= pid <= 2**31 - 1 for pid in record["groups"])
                    or len(set(record["groups"])) != len(record["groups"])
                    or not isinstance(record["lock"], dict)
                    or set(record["lock"]) != {"device", "inode"}
                    or any(type(v) is not int or v < 0 for v in record["lock"].values())):
                return False, "operation record is malformed or mismatched"
            if record["lock"] != {"device": value.st_dev, "inode": value.st_ino}:
                return False, "operation lifetime lock identity changed"
            # No actor retaining the original lock can still update this
            # record or open a startup gate. Match/release under the hold mutex.
            def absent() -> tuple[bool, str]:
                for pgid in record["groups"]:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        continue
                    except OSError:
                        return False, "owned process-group absence is uninspectable"
                    return False, "recorded process group is present (including possible PID reuse); hold retained"
                return True, "original lifetime lock free and every recorded group absent"
            return semaphore.release_operation_after_cleanup(record["goal"], operation_id, absent)
        finally:
            os.close(fd)
    except (OSError, ValueError, semaphore.SemaphoreError) as exc:
        return False, f"operation recovery refused: {exc}"
