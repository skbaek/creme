from __future__ import annotations

import copy
import ctypes
import errno
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import cli, semaphore
from creme.adapters.base import Adapter
from creme.adapters.darwin import DarwinAdapter
from creme.adapters.linux import LinuxAdapter
from creme.adapters.session import codex_identity, lock_alive, valid_identity


class KernelLockHandoffTest(unittest.TestCase):
    """Real client lifetime locks, isolated lease state, identical local PID 1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = LinuxAdapter()
        self.environ = mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": str(self.root / "state")})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        client = mock.patch("creme.semaphore._client_process", return_value=(1, "codex", "client codex pid 1"))
        client.start()
        self.addCleanup(client.stop)
        self.sessions = []
        for name in ("Alpha", "Beta"):
            directory = self.root / "tmp" / "arg0" / ("codex-arg0" + name)
            directory.mkdir(parents=True)
            # A real subprocess owns the exclusive lock, like Arg0PathEntryGuard.
            process = subprocess.Popen(
                [sys.executable, "-c", "import fcntl,os,sys; f=open(sys.argv[1], 'w'); "
                 "fcntl.flock(f,fcntl.LOCK_EX); print('READY',flush=True); "
                 "sys.stdin.readline(); os._exit(0)", str(directory / ".lock")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(process.stdout.readline().strip(), "READY")
            self.sessions.append((directory, process))
            self.addCleanup(self.stop_client, process)
        self.as_session(0)

    @staticmethod
    def stop_client(process):
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()

    def as_session(self, number, *, thread=None):
        directory, _process = self.sessions[number]
        os.environ["PATH"] = str(directory) + os.pathsep + os.defpath
        os.environ["CODEX_SESSION_ID"] = f"session-{number}"
        os.environ["CODEX_THREAD_ID"] = thread or f"thread-{number}"

    def acquire(self, note="owner", **kwargs):
        return semaphore.master_acquire("codex", note, adapter=self.adapter, **kwargs)

    def test_two_live_sessions_with_pid_one_cannot_renew_release_or_take_over(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()
        self.as_session(1)
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertFalse(self.acquire("competitor", take_over=True)[0])
        self.assertEqual(semaphore.master_snapshot(), original)

    def test_crash_and_restart_with_pid_one_takes_over_without_expiry(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()["lease"]
        self.stop_client(self.sessions[0][1])
        self.as_session(1)
        ok, detail = self.acquire("restart", take_over=True)
        self.assertTrue(ok, detail)
        self.assertIn("stranded", detail)
        self.assertNotEqual(semaphore.master_snapshot()["lease"]["generation"], original["generation"])

    def test_same_owner_renews_in_separate_cli_invocations_then_reacquires(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()["lease"]
        for _ in range(2):
            result = subprocess.run([sys.executable, "-m", "creme", "semaphore", "master-renew"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(semaphore.master_release(adapter=self.adapter)[0])
        self.assertTrue(self.acquire("reacquired")[0])
        self.assertNotEqual(semaphore.master_snapshot()["lease"]["generation"], original["generation"])

    def test_threads_sharing_application_guard_are_distinct_owners(self):
        self.assertTrue(self.acquire()[0])
        self.as_session(0, thread="another-thread")
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertFalse(self.acquire("other thread", take_over=True)[0])

    def test_heartbeat_cannot_adopt_successor_between_beats(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()["lease"]
        successor = []

        def replace(_seconds):
            self.assertTrue(semaphore.master_release(adapter=self.adapter)[0])
            self.assertTrue(self.acquire("successor")[0])
            successor.append(semaphore.master_snapshot())

        ok, detail = semaphore.master_heartbeat(
            1, adapter=self.adapter, sleep=replace,
            expected_generation=original["generation"], expected_identity=original["identity"],
        )
        self.assertTrue(ok, detail)
        self.assertIn("superseded", detail)
        self.assertEqual(semaphore.master_snapshot(), successor[0])

    def test_detached_launch_captures_generation_before_child_is_scheduled(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()["lease"]
        with mock.patch("creme.semaphore.subprocess.Popen") as popen:
            popen.return_value.pid = 123
            self.assertTrue(semaphore.master_heartbeat_detached(1, adapter=self.adapter)[0])
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--generation") + 1], original["generation"])
        self.assertEqual(json.loads(command[command.index("--identity") + 1]), original["identity"])
        self.assertTrue(semaphore.master_release(adapter=self.adapter)[0])
        self.assertTrue(self.acquire("same owner, new generation")[0])
        successor = semaphore.master_snapshot()
        # Use candidate code, never the canonical production lease or launcher.
        result = subprocess.run([sys.executable, "-m", "creme", "semaphore", *command[2:]],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("superseded", result.stdout)
        self.assertEqual(semaphore.master_snapshot(), successor)

    def test_generation_is_rechecked_under_renewal_mutex(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()["lease"]
        self.assertTrue(semaphore.master_release(adapter=self.adapter)[0])
        self.assertTrue(self.acquire("replacement")[0])
        successor = semaphore.master_snapshot()
        ok, detail = semaphore.master_renew(adapter=self.adapter,
            expected_generation=original["generation"], expected_identity=original["identity"])
        self.assertFalse(ok)
        self.assertIn("superseded", detail)
        self.assertEqual(semaphore.master_snapshot(), successor)

    def test_missing_or_unreadable_first_guard_does_not_use_parent_guard(self):
        current = self.sessions[0][0]
        parent = self.sessions[1][0]
        os.environ["PATH"] = str(current) + os.pathsep + str(parent)
        with mock.patch("creme.adapters.session.Path.lstat", side_effect=PermissionError("denied")):
            self.assertIsNone(codex_identity())
        os.environ["PATH"] = str(current.parent / "codex-arg0Missing") + os.pathsep + str(parent)
        self.assertIsNone(codex_identity())

    def test_lock_permissions_failure_is_unknown_and_file_replacement_is_dead(self):
        identity = codex_identity()
        self.assertTrue(lock_alive(identity))
        with mock.patch("creme.adapters.session.os.open", side_effect=PermissionError("denied")):
            self.assertIsNone(lock_alive(identity))
        replaced = dict(identity, inode=identity["inode"] + 1)
        self.assertFalse(lock_alive(replaced))

    def test_unknown_identity_cannot_renew_or_start_heartbeat(self):
        self.assertTrue(self.acquire()[0])
        unknown = Adapter.unsupported("test")
        original = semaphore.master_snapshot()
        self.assertFalse(semaphore.master_renew(adapter=unknown)[0])
        self.assertFalse(semaphore.master_heartbeat_detached(1, adapter=unknown)[0])
        self.assertFalse(semaphore.master_heartbeat(1, adapter=unknown, max_beats=1)[0])
        self.assertEqual(semaphore.master_snapshot(), original)

    def test_legacy_pid_one_is_unknown_until_expiry_and_never_renews(self):
        self.assertTrue(self.acquire()[0])
        data = semaphore.master_snapshot()
        data["schema_version"] = 1
        del data["lease"]["generation"], data["lease"]["identity"]
        path = semaphore.master_path()
        path.write_text(json.dumps(data))
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertFalse(self.acquire("migration", take_over=True)[0])
        self.assertEqual(semaphore.master_snapshot(), data)
        data["lease"]["acquired_at"] = data["lease"]["renewed_at"] = 1.0
        path.write_text(json.dumps(data))
        self.assertTrue(self.acquire("migration", take_over=True)[0])
        self.assertEqual(semaphore.master_snapshot()["schema_version"], 2)

    def test_corrupt_identity_is_refused_without_state_replacement(self):
        self.assertTrue(self.acquire()[0])
        data = semaphore.master_snapshot()
        bad = [dict(data["lease"]["identity"], path="/tmp/\x00/tmp/arg0/codex-arg0Bad/.lock"),
               {"kind": "linux-process", "pid": 1, "scope": "garbage", "start": "garbage", "uid": os.getuid()},
               dict(data["lease"]["identity"], inode=True)]
        for identity in bad:
            with self.subTest(identity=identity):
                data["lease"]["identity"] = identity
                raw = json.dumps(data)
                semaphore.master_path().write_text(raw)
                with self.assertRaises(semaphore.SemaphoreError):
                    self.acquire("must fail", take_over=True)
                self.assertEqual(semaphore.master_path().read_text(), raw)


class ProcessIncarnationTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux /proc capability")
    def test_current_linux_process_and_pid_reuse(self):
        adapter = LinuxAdapter()
        identity = adapter.process_identity(os.getpid()).data
        self.assertTrue(valid_identity(identity))
        self.assertTrue(adapter.session_alive(identity).data["alive"])
        changed = dict(identity, start=str(int(identity["start"]) + 1))
        self.assertFalse(adapter.session_alive(changed).data["alive"])
        foreign = dict(identity, scope=identity["scope"].split(":", 1)[0] + ":pid:[0]")
        self.assertEqual(adapter.session_alive(foreign).status, "UNAVAILABLE")
        for error in (PermissionError("denied"), FileNotFoundError("hidden scope")):
            with mock.patch.object(adapter, "_process_scope", side_effect=error):
                self.assertEqual(adapter.session_alive(identity).status, "UNAVAILABLE")

    @unittest.skipUnless(sys.platform == "linux", "Linux /proc capability")
    def test_linux_exited_process_is_dead(self):
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        adapter = LinuxAdapter()
        identity = adapter.process_identity(process.pid).data
        process.stdin.close()
        process.wait(timeout=5)
        self.assertFalse(adapter.session_alive(identity).data["alive"])

    def test_darwin_birth_time_boot_and_observability(self):
        adapter = DarwinAdapter()
        boot = "00000000-0000-0000-0000-000000000000"
        with mock.patch.object(adapter, "_process_scope", return_value=boot), \
             mock.patch.object(adapter, "_process_record", return_value=(os.getuid(), "123.000001", False)) as record:
            identity = adapter.process_identity(17).data
            self.assertTrue(valid_identity(identity))
            self.assertTrue(adapter.session_alive(identity).data["alive"])
            record.return_value = (os.getuid(), "123.000002", False)
            self.assertFalse(adapter.session_alive(identity).data["alive"])
            record.side_effect = PermissionError("denied")
            self.assertEqual(adapter.session_alive(identity).status, "UNAVAILABLE")
            record.side_effect = ProcessLookupError("gone")
            self.assertFalse(adapter.session_alive(identity).data["alive"])
        with mock.patch.object(adapter, "_process_scope", side_effect=FileNotFoundError("hidden")):
            self.assertEqual(adapter.session_alive(identity).status, "UNAVAILABLE")

    def test_darwin_proc_pidinfo_decodes_public_abi_and_refuses_partial_reads(self):
        def query(pid, flavor, arg, buffer, size):
            self.assertEqual((pid, flavor, arg, size), (17, 3, 0, 136))
            struct.pack_into("=I", buffer, 4, 2)
            struct.pack_into("=I", buffer, 12, 17)
            struct.pack_into("=I", buffer, 20, 42)
            struct.pack_into("=QQ", buffer, 120, 123, 45)
            return size
        with mock.patch("creme.adapters.darwin.ctypes.CDLL") as library:
            library.return_value.proc_pidinfo.side_effect = query
            self.assertEqual(DarwinAdapter._process_record(17), (42, "123.000045", False))
            library.return_value.proc_pidinfo.side_effect = None
            library.return_value.proc_pidinfo.return_value = 12
            with self.assertRaises(OSError):
                DarwinAdapter._process_record(17)
            library.return_value.proc_pidinfo.return_value = 0
            with mock.patch("creme.adapters.darwin.ctypes.get_errno", return_value=errno.ESRCH):
                with self.assertRaises(ProcessLookupError):
                    DarwinAdapter._process_record(17)

    def test_cli_rejects_partial_heartbeat_transport(self):
        with mock.patch("builtins.print"):
            self.assertNotEqual(cli.main(["semaphore", "master-renew", "--generation", "abc"]), 0)


if __name__ == "__main__":
    unittest.main()
