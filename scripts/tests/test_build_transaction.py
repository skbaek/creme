"""Real lifetime invariant controls, always using temporary semaphore state.

The cancellation/publication controls also run unchanged on rejected source.
Fixture finalizers are installed before readiness assertions and record every
created child. Driver-level controls retain their own additional finally reaper.
"""
from __future__ import annotations

import contextlib
import gc
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from creme import build_ownership as owned, semaphore
from scripts.tests.test_build_ownership import UNPROBED
from scripts.tests.test_semaphore import HeadroomAdapter


ROOT = Path(__file__).resolve().parents[2]
POLICY = {"task_memory_gib": 2, "heavy_workers": 4, "light_workers": 4,
          "physical_memory_gib": 32.0, "profile_status": "VALID"}


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("fixture synchronization timed out")
        time.sleep(.01)


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="build-transaction-")))
        self.stack.enter_context(patch.dict(os.environ, {
            "CREME_SEMAPHORE_DIR": str(self.root / "state"),
            "CREME_BUILD_LEDGER": str(self.root / "ledger"),
        }))
        self.stack.enter_context(patch.object(semaphore, "get_adapter", return_value=HeadroomAdapter()))
        self.stack.enter_context(patch.object(semaphore, "_runtime_admission_policy", return_value=POLICY))
        self.children = []
        self.fixture_log = os.environ.get("CREME_TRANSACTION_FIXTURE_LOG")
        # Register the finalizer before the first process can exist.
        self.stack.callback(self.reap_all)
        popen = subprocess.Popen
        def track(*args, **kwargs):
            proc = popen(*args, **kwargs)
            self.children.append((proc, kwargs.get("start_new_session", False)))
            if self.fixture_log:
                with open(self.fixture_log, "a") as out:
                    out.write(json.dumps({"pid": proc.pid, "group": bool(kwargs.get("start_new_session")), "event": "created"}) + "\n")
            return proc
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=track))
        self.handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.stack.callback(self.restore_handlers)

    def restore_handlers(self):
        for signum, handler in self.handlers.items():
            signal.signal(signum, handler)

    def reap_all(self):
        failures = []
        for proc, group in reversed(self.children):
            try:
                if group:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                owned._close_process_pipes(proc)
                if group:
                    until(lambda: owned._process_group_alive(proc.pid) is False)
                self.assertFalse(alive(proc.pid))
                if self.fixture_log:
                    with open(self.fixture_log, "a") as out:
                        out.write(json.dumps({"pid": proc.pid, "event": "dead"}) + "\n")
            except BaseException as exc:
                # Continue the remaining owned reapers even when one fails.
                failures.append(exc)
        if failures:
            raise failures[0]

    def executable(self, name, body):
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)
        return path

    def build_fixture(self):
        lake = self.executable("lake", "print('Built T (1s)', flush=True)\n")
        nice = self.root / "nice"
        nice.write_text('#!/bin/sh\nif [ "$1" = "--preflight" ]; then exit 0; fi\nshift 2\nexec "$@"\n')
        nice.chmod(0o700)
        for name, value in {
            "_worktree_identity": (self.root, "g"),
            "resolve_toolchain": (lake, Path("/bin/sh"), Path("/")),
            "guard_bin": self.root,
            "stale_evidence": UNPROBED,
            "worktree_digests": (None, None),
        }.items():
            self.stack.enter_context(patch.object(owned, name, return_value=value))
        self.stack.enter_context(patch.object(owned, "_process_snapshot", return_value={}))
        return lake

    def run_build(self, **kwargs):
        return owned.run_lake_build("g", ["T"], memory_gib=2, contention="sensitive",
                                    stdout=kwargs.pop("stdout", io.StringIO()), **kwargs)

    def assert_absent(self):
        self.assertIsNone(semaphore.snapshot()["hard"])
        self.assertEqual(semaphore.snapshot()["soft"], [])
        for proc, group in self.children:
            self.assertIsNotNone(proc.poll())
            if group:
                self.assertIs(owned._process_group_alive(proc.pid), False)


class TransactionTest(FixtureTest):
    def test_repeated_interrupt_cannot_escape_preflight_cleanup(self):
        ready, term = self.root / "ready", self.root / "term"
        launcher = self.executable("preflight", f"""import os, pathlib, signal, time
signal.signal(signal.SIGTERM, lambda *args: pathlib.Path({str(term)!r}).touch())
pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))
time.sleep(20)
""")
        stop = threading.Event()
        failures = []
        def send():
            try:
                until(lambda: ready.exists() or stop.is_set())
                if stop.is_set():
                    return
                os.kill(os.getpid(), signal.SIGTERM)
                until(lambda: term.exists() or stop.is_set())
                if not stop.is_set():
                    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
                        os.kill(os.getpid(), sig)
            except BaseException as exc:
                failures.append(str(exc))
        sender = threading.Thread(target=send)
        def finish_sender():
            stop.set()
            sender.join(5)
            self.assertFalse(sender.is_alive())
        self.stack.callback(finish_sender)
        def prior_term(*_args):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, prior_term)
        sender.start()
        terminate = owned._terminate_process_group
        with patch.object(owned, "_terminate_process_group", side_effect=lambda p: terminate(p, timeout=.4)):
            with self.assertRaises(KeyboardInterrupt):
                owned._preflight_priority_launcher(launcher, cwd=self.root, env=dict(os.environ), timeout_seconds=4)
            child = int(ready.read_text())
            self.assertFalse(alive(child), "interrupt propagated before the actual child was reaped")
        self.assertEqual(failures, [])
        self.assertIs(signal.getsignal(signal.SIGTERM), prior_term)

    def test_late_preflight_cancellation_never_becomes_success(self):
        launcher = self.executable("preflight", "pass\n")
        close = owned._close_process_pipes
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig):
                def prior(*_args):
                    raise KeyboardInterrupt
                signal.signal(sig, prior)
                def late(proc):
                    answer = close(proc)
                    signal.raise_signal(sig)
                    return answer
                with patch.object(owned, "_close_process_pipes", side_effect=late):
                    with self.assertRaises(KeyboardInterrupt):
                        owned._preflight_priority_launcher(launcher, cwd=self.root, env=dict(os.environ))
                self.assertIs(signal.getsignal(sig), prior)
                signal.signal(sig, self.handlers[sig])

    def test_handler_install_failure_never_leaves_an_admitted_hold(self):
        self.build_fixture()
        self.stack.enter_context(patch.object(owned, "_preflight_priority_launcher", return_value=(True, "fixture")))
        install = signal.signal
        def fail(sig, handler):
            if sig == signal.SIGHUP and getattr(handler, "__name__", "") == "_interrupt":
                raise OSError("fixture partial handler install")
            return install(sig, handler)
        with patch.object(signal, "signal", side_effect=fail):
            with self.assertRaises(OSError):
                self.run_build()
        self.assert_absent()
        self.assertEqual({s: signal.getsignal(s) for s in self.handlers}, self.handlers)

    def test_publication_exception_reconciles_exact_owned_admission(self):
        self.build_fixture()
        save = semaphore._save
        for wait in (None, 1):
            with self.subTest(wait=wait):
                injected = []
                def publish_then_raise(path, state):
                    save(path, state)
                    if (state["hard"] or state["soft"]) and not injected:
                        injected.append(True)
                        raise KeyboardInterrupt
                with patch.object(semaphore, "_save", side_effect=publish_then_raise):
                    self.assertEqual(self.run_build(wait_seconds=wait), 130)
                self.assertEqual(injected, [True])
                self.assert_absent()

    def test_acquisition_failure_cannot_release_a_preexisting_same_label_hold(self):
        self.build_fixture()
        self.assertTrue(semaphore.adaptive_acquire("g", "other operation", memory_gib=2)[0])
        before = (self.root / "state" / "state.json").read_bytes()
        with patch.object(semaphore, "adaptive_acquire", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_build(), 130)
        self.assertEqual((self.root / "state" / "state.json").read_bytes(), before)

    def test_admission_wait_is_excluded_from_build_wall_measurement(self):
        self.build_fixture()
        acquire = semaphore.adaptive_acquire
        now = time.monotonic
        offset = [0]
        rows = []
        def delayed(*args, **kwargs):
            answer = acquire(*args, **kwargs)
            offset[0] += 1000
            return answer
        with patch.object(semaphore, "adaptive_acquire", side_effect=delayed), patch.object(time, "monotonic", side_effect=lambda: now() + offset[0]), patch.object(owned, "append_ledger", side_effect=rows.append):
            self.assertEqual(self.run_build(), 0)
        self.assertLess(rows[0]["wall_seconds"], 10)

    def test_completed_census_group_is_never_signalled_again(self):
        self.build_fixture()
        self.stack.enter_context(patch.object(owned, "_preflight_priority_launcher", return_value=(True, "fixture")))
        self.stack.enter_context(patch.object(owned, "_apparent_goal", return_value="g-rehearsal"))
        self.stack.enter_context(patch.object(owned, "_dependency_revision", return_value=("fixture-rev", "present")))
        terminate = owned._terminate_process_group
        cleaned = []
        def once(proc):
            self.assertNotIn(proc.pid, cleaned, "proved-absent census PGID was used again")
            answer = terminate(proc)
            self.assertTrue(answer)
            cleaned.append(proc.pid)
            return answer
        with patch.object(owned, "_terminate_process_group", side_effect=once):
            self.assertEqual(self.run_build(census=True, dependency="fixture"), 0)
        self.assertEqual(len(cleaned), 2)
        self.assert_absent()

    def test_completed_renewal_cleanup_is_never_signalled_again(self):
        lake = self.build_fixture()
        lake.write_text(f"#!{sys.executable}\nimport time\nprint('ready', flush=True)\ntime.sleep(20)\n")
        self.stack.enter_context(patch.object(owned, "_preflight_priority_launcher", return_value=(True, "fixture")))
        renewal = owned.RenewalThread
        self.stack.enter_context(patch.object(owned, "RenewalThread", side_effect=lambda goal, proc: renewal(goal, proc, interval=.05)))
        self.stack.enter_context(patch.object(semaphore, "renew", return_value=(False, "fixture refusal")))
        terminate = owned._terminate_process_group
        cleaned = []
        def once(proc):
            self.assertNotIn(proc.pid, cleaned, "renewal already proved this numeric group absent")
            answer = terminate(proc)
            self.assertTrue(answer)
            cleaned.append(proc.pid)
            return answer
        with patch.object(owned, "_terminate_process_group", side_effect=once):
            self.assertNotEqual(self.run_build(), 0)
        self.assertEqual(len(cleaned), 1)
        self.assert_absent()

    def test_transient_handler_restore_exception_follows_finalization(self):
        self.build_fixture()
        self.stack.enter_context(patch.object(owned, "_preflight_priority_launcher", return_value=(True, "fixture")))
        install = signal.signal
        failed = []
        def restore(sig, handler):
            if sig == signal.SIGHUP and handler == self.handlers[sig] and not failed:
                self.assert_absent()
                failed.append(True)
                raise KeyboardInterrupt
            return install(sig, handler)
        with patch.object(signal, "signal", side_effect=restore):
            with self.assertRaises(KeyboardInterrupt):
                self.run_build()
        self.assert_absent()
        self.assertEqual({s: signal.getsignal(s) for s in self.handlers}, self.handlers)

    def test_late_telemetry_cancellation_follows_the_hold_decision(self):
        self.build_fixture()
        calls = []
        def sample():
            calls.append(True)
            if len(calls) == 2:
                self.assert_absent()
                signal.raise_signal(signal.SIGINT)
            return None
        with patch.object(owned, "_swap_gib", side_effect=sample):
            self.assertEqual(self.run_build(), 130)
        self.assert_absent()

    def test_late_and_repeated_admitted_signals_cannot_interrupt_finalization(self):
        self.build_fixture()
        self.stack.enter_context(patch.object(owned, "_preflight_priority_launcher", return_value=(True, "fixture")))
        close = owned._close_process_pipes
        for first in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(first=first):
                def interrupt_cleanup(proc):
                    answer = close(proc)
                    for sig in (first, signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                        signal.raise_signal(sig)
                    return answer
                with patch.object(owned, "_close_process_pipes", side_effect=interrupt_cleanup):
                    self.assertEqual(self.run_build(), 128 + first)
                self.assert_absent()
                self.assertEqual({s: signal.getsignal(s) for s in self.handlers}, self.handlers)

    def test_failed_hash_output_and_receipt_sinks_cannot_strand_absent_resources(self):
        self.build_fixture()
        for boundary in ("hash", "output", "ledger", "telemetry"):
            with self.subTest(boundary=boundary), contextlib.ExitStack() as stack:
                output = io.StringIO()
                if boundary == "hash":
                    stack.enter_context(patch.object(owned, "_module_hashes", side_effect=OSError("fixture hash")))
                elif boundary == "ledger":
                    stack.enter_context(patch.object(owned, "append_ledger", side_effect=BrokenPipeError("fixture receipt")))
                elif boundary == "telemetry":
                    stack.enter_context(patch.object(owned, "_swap_gib", side_effect=[None, OSError("fixture telemetry")]))
                else:
                    class Broken(io.StringIO):
                        def write(self, text):
                            raise BrokenPipeError("fixture closed sink")
                    output = Broken()
                try:
                    self.run_build(stdout=output)
                except (OSError, ValueError):
                    pass
                self.assert_absent()

    def test_partial_native_start_is_uncertain_until_bootstrap_finishes(self):
        entered, finish, finished = threading.Event(), threading.Event(), threading.Event()
        class Delayed(owned.ProcessSampler):
            def _bootstrap_inner(self):
                entered.set()
                try:
                    if not finish.wait(5):
                        raise RuntimeError("fixture bootstrap timeout")
                    super()._bootstrap_inner()
                finally:
                    finished.set()
        worker = Delayed(os.getpid())
        def finalizer():
            finish.set()
            self.assertTrue(worker._started.wait(5))
            worker.stop()
            self.assertTrue(finished.wait(5))
            self.assertFalse(worker.is_alive())
        self.stack.callback(finalizer)
        wait = threading.Event.wait
        def interrupt_native_wait(event, timeout=None):
            if event is worker._started:
                self.assertTrue(wait(entered, 5))
                raise KeyboardInterrupt
            return wait(event, timeout)
        with patch.object(threading.Event, "wait", interrupt_native_wait):
            with self.assertRaises(KeyboardInterrupt):
                worker.start()
        self.assertFalse(worker._started.is_set())
        self.assertFalse(worker.is_alive())
        ok, _detail = owned._stop_background(worker, "sampler thread")
        self.assertFalse(ok, "unacknowledged native bootstrap is not absence")
        self.assertTrue(worker.stop_event.is_set())
        self.assertFalse(finished.is_set())

    def test_join_failure_preserves_and_stop_request_survives(self):
        from creme.build_lifecycle import BuildTransaction
        tx = BuildTransaction("g")
        self.assertTrue(semaphore.adaptive_acquire("g", "fixture", memory_gib=2, operation_id=tx.id)[0])
        worker = owned.ProcessSampler(os.getpid())
        self.stack.enter_context(patch.object(owned, "_process_snapshot", return_value={}))
        self.stack.callback(lambda: (worker.stop_event.set(), worker.join(5)))
        tx.helpers.append((worker, "sampler"))
        worker.start()
        with patch.object(worker, "join", side_effect=KeyboardInterrupt):
            tx.finalize(owned._terminate_process_group, owned._close_process_pipes, owned._stop_background, False)
        self.assertFalse(tx.cleanup_proved)
        self.assertTrue(worker.stop_event.is_set())
        self.assertEqual(semaphore.snapshot()["soft"][0]["operation_id"], tx.id)

    def test_preserved_helper_cannot_be_released_by_lean_only_wind_down(self):
        from creme.task_wind_down import wind_down
        self.build_fixture()
        entered, finish = threading.Event(), threading.Event()
        helpers = []
        sampler = owned.ProcessSampler
        def construct(*args, **kwargs):
            helper = sampler(*args, **kwargs)
            helpers.append(helper)
            start = helper.start
            def acknowledged_start():
                start()
                self.assertTrue(entered.wait(5))
            helper.start = acknowledged_start
            return helper
        def blocked():
            entered.set()
            finish.wait(15)
            return {}
        def finish_helpers():
            finish.set()
            for helper in helpers:
                helper.stop()
                self.assertFalse(helper.is_alive())
        self.stack.callback(finish_helpers)
        class EmptyLeanScope(HeadroomAdapter):
            def reclaim(self, arguments):
                return self.result("lean_reclaim", "OK", "empty Lean fixture scope", {"owned": [], "survivors": []})
        with patch.object(owned, "ProcessSampler", side_effect=construct), patch.object(owned, "_process_snapshot", side_effect=blocked):
            self.assertEqual(self.run_build(), 2)
        self.assertTrue(entered.is_set())
        self.assertTrue(helpers[0].is_alive())
        with patch("creme.task_wind_down._goal_worktree_roots", return_value=(self.root,)):
            result = wind_down("g", EmptyLeanScope())
        self.assertEqual(result.status, "REFUSED", "Lean-only recovery released a hold while its ordinary helper lived")
        self.assertTrue(semaphore.snapshot()["hard"] or semaphore.snapshot()["soft"])


class RecoveryTest(FixtureTest):
    def writer(self, resource=False):
        script = '''import gc, json, os, sys, threading
from pathlib import Path
from unittest.mock import patch
from creme import semaphore, build_ownership as owned
from creme.build_lifecycle import BuildTransaction
from scripts.tests.test_semaphore import HeadroomAdapter
root = Path(sys.argv[1])
tx = BuildTransaction('g')
with patch.object(semaphore, 'get_adapter', return_value=HeadroomAdapter()), patch.object(semaphore, '_runtime_admission_policy', return_value=POLICY):
    assert semaphore.adaptive_acquire('g', 'fixture', memory_gib=2, operation_id=tx.id)[0]
if sys.argv[2] == 'resource':
    proc = tx.launch([sys.executable, '-c', "import os, sys; from pathlib import Path; Path(sys.argv[1]).touch(); os.execv('/bin/sleep', ['/bin/sleep', '20'])", str(root / 'resource.ready')], stdout=-3, stderr=-3)
    (root / 'resource.pid').write_text(str(proc.pid))
    if os.environ.get('CREME_TRANSACTION_FIXTURE_LOG'):
        with open(os.environ['CREME_TRANSACTION_FIXTURE_LOG'], 'a') as log:
            log.write(json.dumps({'pid': proc.pid, 'group': True, 'event': 'created'}) + '\\n')
    tx.finalize(lambda p: False, lambda p: True, owned._stop_background, False)
else:
    entered, finish = threading.Event(), threading.Event()
    def snapshot():
        entered.set()
        finish.wait(20)
        return {}
    with patch.object(owned, '_process_snapshot', side_effect=snapshot):
        worker = owned.ProcessSampler(os.getpid())
        worker.start()
        assert entered.wait(5)
        tx.helpers.append((worker, 'sampler'))
        tx.finalize(owned._terminate_process_group, owned._close_process_pipes, owned._stop_background, False)
identity = tx.id
del tx
gc.collect()
(root / 'operation').write_text(identity)
sys.stdin.read(1)
os._exit(0)
'''.replace("POLICY", repr(POLICY))
        proc = subprocess.Popen([sys.executable, "-c", script, str(self.root), "resource" if resource else "helper"],
                                cwd=ROOT, env=dict(os.environ), start_new_session=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def reap_resource():
            path = self.root / "resource.pid"
            if path.exists():
                pid = int(path.read_text())
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                until(lambda: not alive(pid))
        self.stack.callback(reap_resource)
        until(lambda: (self.root / "operation").exists() or proc.poll() is not None)
        if not (self.root / "operation").exists():
            self.fail(proc.communicate(timeout=5)[1])
        return proc, (self.root / "operation").read_text()

    def leave_writer(self, proc):
        proc.stdin.write("x")
        proc.stdin.flush()
        proc.wait(timeout=5)

    def test_recovery_requires_original_wrapper_exit_even_after_gc(self):
        from creme.build_lifecycle import recover
        proc, identity = self.writer()
        self.assertFalse(recover(identity)[0])
        for release in (
            lambda: semaphore.adaptive_release("g"),
            lambda: semaphore.release("soft", "g"),
            lambda: semaphore.release_after_cleanup("g", lambda: (True, "empty Lean scope"), goal_scoped=True),
        ):
            ok, detail = release()
            self.assertFalse(ok)
            self.assertIn("build-recover " + identity, detail)
        self.leave_writer(proc)
        self.assertTrue(recover(identity)[0])
        self.assertTrue(recover(identity)[0])
        self.assert_absent()

    def test_recovery_checks_actual_build_group_not_only_helper_departure(self):
        from creme.build_lifecycle import recover
        proc, identity = self.writer(resource=True)
        until(lambda: (self.root / "resource.ready").exists())
        self.leave_writer(proc)
        ok, detail = recover(identity)
        self.assertFalse(ok)
        self.assertIn("recorded process group is present", detail,
                      "registered executable must not inherit the wrapper lifetime lock")
        pid = int((self.root / "resource.pid").read_text())
        self.assertTrue(alive(pid))
        os.killpg(pid, signal.SIGTERM)
        until(lambda: not alive(pid))
        self.assertTrue(recover(identity)[0])

    def test_record_identity_and_unrelated_hold_recovery_negatives(self):
        from creme.build_lifecycle import recover
        proc, identity = self.writer()
        self.leave_writer(proc)
        root = self.root / "state" / "build-operations"
        path = root / (identity + ".json")
        original = path.read_bytes()
        for corruption in ("id", "lock", "uid", "groups", "unknown", "duplicate", "missing", "symlink", "mode"):
            with self.subTest(corruption=corruption):
                try:
                    row = json.loads(original)
                    if corruption == "id": row["id"] = "0" * 32
                    elif corruption == "lock": row["lock"]["inode"] += 1
                    elif corruption == "uid": row["uid"] += 1
                    elif corruption == "groups": row["groups"] = [-1]
                    elif corruption == "unknown": row["unknown"] = True
                    path.write_text(json.dumps(row))
                    if corruption == "duplicate":
                        path.write_text(path.read_text().replace('"groups": []', '"groups": [123], "groups": []'))
                    if corruption in {"missing", "symlink"}:
                        path.unlink()
                        if corruption == "symlink":
                            target = root / "foreign"
                            target.write_bytes(original)
                            path.symlink_to(target)
                    elif corruption == "mode": path.chmod(0o644)
                    self.assertFalse(recover(identity)[0])
                    self.assertEqual(semaphore.snapshot()["soft"][0]["operation_id"], identity)
                finally:
                    if path.is_symlink(): path.unlink()
                    path.write_bytes(original)
                    path.chmod(0o600)
        self.assertFalse(recover("../" + identity)[0])
        self.assertTrue(semaphore.adaptive_release("g", operation_id=identity)[0])
        self.assertTrue(semaphore.adaptive_acquire("g", "later unrelated", memory_gib=2)[0])
        before = (self.root / "state" / "state.json").read_bytes()
        self.assertFalse(recover(identity)[0])
        self.assertEqual((self.root / "state" / "state.json").read_bytes(), before)

    def test_reused_numeric_group_fails_closed_without_signalling(self):
        from creme.build_lifecycle import recover
        proc, identity = self.writer()
        self.leave_writer(proc)
        unrelated = subprocess.Popen(["/bin/sleep", "20"], start_new_session=True)
        path = self.root / "state" / "build-operations" / (identity + ".json")
        row = json.loads(path.read_text())
        row["groups"] = [unrelated.pid]
        path.write_text(json.dumps(row))
        self.assertFalse(recover(identity)[0])
        self.assertIsNone(unrelated.poll())
        self.assertEqual(semaphore.snapshot()["soft"][0]["operation_id"], identity)

    def test_missing_replaced_and_nonprivate_lifetime_locks_refuse(self):
        from creme.build_lifecycle import recover
        proc, identity = self.writer()
        self.leave_writer(proc)
        root = self.root / "state" / "build-operations"
        path = root / (identity + ".lock")
        backup = root / "original.lock"
        before = (self.root / "state" / "state.json").read_bytes()
        for corruption in ("missing", "symlink", "replacement", "mode", "hardlink"):
            with self.subTest(corruption=corruption):
                path.rename(backup)
                try:
                    if corruption == "symlink": path.symlink_to(backup)
                    elif corruption in {"replacement", "mode"}:
                        path.touch(mode=0o600 if corruption == "replacement" else 0o644)
                    elif corruption == "hardlink": os.link(backup, path)
                    self.assertFalse(recover(identity)[0])
                    self.assertEqual((self.root / "state" / "state.json").read_bytes(), before)
                finally:
                    if path.exists() or path.is_symlink(): path.unlink()
                    backup.rename(path)
        self.assertTrue(recover(identity)[0])

    def test_crash_before_group_registration_cannot_open_executable_gate(self):
        from creme.build_lifecycle import recover
        marker = self.root / "executed"
        # Force the exact post-Popen/pre-journal crash. The real native child
        # inherits the lock and the unopened gate; executable work stays gated.
        script = '''import json, os, sys
from pathlib import Path
from unittest.mock import patch
from creme import semaphore
from creme.build_lifecycle import BuildTransaction
from scripts.tests.test_semaphore import HeadroomAdapter
root = Path(sys.argv[1])
tx = BuildTransaction('g')
with patch.object(semaphore, 'get_adapter', return_value=HeadroomAdapter()), patch.object(semaphore, '_runtime_admission_policy', return_value=POLICY):
    assert semaphore.adaptive_acquire('g', 'fixture', memory_gib=2, operation_id=tx.id)[0]
(root / 'operation').write_text(tx.id)
save = tx.save
def crash():
    if tx.record['groups']:
        (root / 'resource.pid').write_text(str(tx.record['groups'][0]))
        if os.environ.get('CREME_TRANSACTION_FIXTURE_LOG'):
            with open(os.environ['CREME_TRANSACTION_FIXTURE_LOG'], 'a') as log:
                log.write(json.dumps({'pid': tx.record['groups'][0], 'group': True, 'event': 'created'}) + '\\n')
        os._exit(77)
    save()
tx.save = crash
tx.launch([sys.executable, '-c', 'import sys; from pathlib import Path; Path(sys.argv[1]).touch()', str(root / 'executed')])
'''.replace("POLICY", repr(POLICY))
        proc = subprocess.Popen([sys.executable, "-c", script, str(self.root)], cwd=ROOT,
                                env=dict(os.environ), start_new_session=True)
        def finish_startup():
            if (self.root / "resource.pid").exists():
                pid = int((self.root / "resource.pid").read_text())
                if alive(pid):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                until(lambda: not alive(pid))
        self.stack.callback(finish_startup)
        self.assertEqual(proc.wait(timeout=5), 77)
        identity = (self.root / "operation").read_text()
        pid = int((self.root / "resource.pid").read_text())
        until(lambda: not alive(pid))
        self.assertFalse(marker.exists())
        self.assertTrue(recover(identity)[0])

    def test_release_write_uncertainty_stays_recoverable_after_wrapper_exit(self):
        from creme.build_lifecycle import BuildTransaction, recover
        tx = BuildTransaction("g")
        self.assertTrue(semaphore.adaptive_acquire("g", "fixture", memory_gib=2, operation_id=tx.id)[0])
        with patch.object(semaphore, "_save", side_effect=OSError("fixture release write")):
            tx.finalize(owned._terminate_process_group, owned._close_process_pipes, owned._stop_background, False)
        self.assertTrue(tx.cleanup_proved)
        self.assertFalse(tx.release_result[0])
        self.assertFalse(recover(tx.id)[0], "same-process API return cannot retire its lifetime lock")
        self.assertEqual(semaphore.snapshot()["soft"][0]["operation_id"], tx.id)

    def test_finalization_is_single_use_and_cannot_retire_uncertain_lifetime(self):
        from creme.build_lifecycle import BuildTransaction, recover
        tx = BuildTransaction("g")
        self.assertFalse(os.get_inheritable(tx.fd), "ordinary exec must close the lifetime descriptor")
        self.assertTrue(semaphore.adaptive_acquire("g", "fixture", memory_gib=2, operation_id=tx.id)[0])
        with patch.object(semaphore, "_save", side_effect=OSError("fixture release uncertainty")):
            tx.finalize(owned._terminate_process_group, owned._close_process_pipes, owned._stop_background, False)
        with self.assertRaises(RuntimeError):
            tx.finalize(owned._terminate_process_group, owned._close_process_pipes, owned._stop_background, False)
        with self.assertRaises(RuntimeError):
            tx.launch(["/bin/true"])
        identity = tx.id
        del tx
        gc.collect()
        self.assertFalse(recover(identity)[0])

    def test_recovery_reads_journal_only_after_exclusive_lifetime_lock(self):
        from creme import build_lifecycle as life
        proc, identity = self.writer()
        self.leave_writer(proc)
        unrelated = subprocess.Popen(["/bin/sleep", "20"], start_new_session=True)
        path = self.root / "state" / "build-operations" / (identity + ".json")
        flock = life.fcntl.flock
        changed = []
        def last_publication(fd, mode):
            if mode & life.fcntl.LOCK_NB and not changed:
                changed.append(True)
                row = json.loads(path.read_text())
                row["groups"] = [unrelated.pid]
                path.write_text(json.dumps(row))
            return flock(fd, mode)
        with patch.object(life.fcntl, "flock", side_effect=last_publication):
            self.assertFalse(life.recover(identity)[0])
        self.assertEqual(changed, [True])
        self.assertIsNone(unrelated.poll())


if __name__ == "__main__":
    unittest.main()
