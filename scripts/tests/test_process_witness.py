from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import semaphore
from creme.adapters.base import Adapter
from creme.adapters.linux import LinuxAdapter
from creme.adapters.session import codex_process_witness, lock_alive, valid_process_witness


class ProcessWitnessTest(unittest.TestCase):
    """Real process lifetime locks, isolated lease state, identical local PID 1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = LinuxAdapter()
        patchers = (
            mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": str(self.root / "state"),
                "CREME_MASTER_SESSION_ID": "", "CREME_MASTER_LIVENESS_SOCKET": ""}),
            mock.patch("creme.semaphore._client_process", return_value=(1, "codex", "client codex pid 1")),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sessions = []
        for name in ("Alpha", "Beta"):
            directory = self.root / "tmp" / "arg0" / ("codex-arg0" + name)
            directory.mkdir(parents=True)
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
        if not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()

    def as_session(self, number, *, task=None):
        directory, _process = self.sessions[number]
        os.environ["PATH"] = str(directory) + os.pathsep + os.defpath
        os.environ["CODEX_SESSION_ID"] = task or f"session-{number}"
        os.environ["CODEX_THREAD_ID"] = task or f"thread-{number}"

    def acquire(self, note="owner", **kwargs):
        return semaphore.master_acquire("codex", note, adapter=self.adapter, **kwargs)

    def test_crash_restart_pid_one_takes_over_without_expiry(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()["lease"]
        self.assertIsNotNone(original["process_witness"])
        self.stop_client(self.sessions[0][1])
        self.as_session(1)
        ok, detail = self.acquire("restart", take_over=True)
        self.assertTrue(ok, detail)
        self.assertIn("stranded", detail)
        self.assertNotEqual(semaphore.master_snapshot()["lease"]["lease_id"], original["lease_id"])

    def test_two_live_sessions_with_pid_one_cannot_renew_release_or_take_over(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()
        self.as_session(1)
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertFalse(self.acquire("competitor", take_over=True)[0])
        self.assertEqual(semaphore.master_snapshot(), original)

    def test_same_task_id_after_process_replacement_cannot_renew(self):
        self.as_session(0, task="reused-task")
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()
        self.as_session(1, task="reused-task")
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertFalse(semaphore._prepare_master_heartbeat_launch(self.adapter)[0])
        self.assertEqual(semaphore.master_snapshot(), original)

    def test_same_owner_renews_in_separate_cli_invocations_then_reacquires(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()["lease"]
        for _ in range(2):
            result = subprocess.run([sys.executable, "-m", "creme", "semaphore", "master-renew"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(semaphore.master_release(adapter=self.adapter)[0])
        self.assertTrue(self.acquire("reacquired")[0])
        self.assertNotEqual(semaphore.master_snapshot()["lease"]["lease_id"], original["lease_id"])

    def test_distinct_tasks_sharing_application_guard_cannot_impersonate(self):
        self.assertTrue(self.acquire()[0])
        self.as_session(0, task="another-task")
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertFalse(self.acquire("other task", take_over=True)[0])

    def test_same_session_alias_distinct_threads_cannot_impersonate(self):
        self.assertTrue(self.acquire()[0])
        original = semaphore.master_snapshot()
        os.environ["CODEX_THREAD_ID"] = "different-thread"
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertFalse(semaphore._prepare_master_heartbeat_launch(self.adapter)[0])
        self.assertEqual(semaphore.master_snapshot(), original)

    def test_explicit_neutral_identity_takes_precedence_over_compatibility_aliases(self):
        os.environ["CREME_MASTER_SESSION_ID"] = "explicit-neutral-task"
        self.assertTrue(self.acquire()[0])
        os.environ["CODEX_THREAD_ID"] = "other-alias"
        self.assertTrue(semaphore.master_renew(adapter=self.adapter)[0])

    def test_held_application_guard_does_not_remove_task_heartbeat_budget(self):
        self.assertTrue(self.acquire()[0])
        self.assertTrue(semaphore._bounded_master_heartbeat(semaphore.master_snapshot()["lease"]))
        clock = [1000.0]
        naps = []

        def nap(seconds):
            naps.append(seconds)
            clock[0] += seconds
            if len(naps) == 3:
                current = semaphore.master_snapshot()["lease"]
                self.assertEqual(current["heartbeat_renewals"], 2)
                self.assertTrue(lock_alive(current["process_witness"]))
                self.assertTrue(semaphore.master_release(adapter=self.adapter)[0])

        ok, detail = semaphore.master_heartbeat(1, adapter=self.adapter, sleep=nap, clock=lambda: clock[0])
        self.assertTrue(ok, detail)
        self.assertEqual(naps, [1, 1, float(semaphore.HEARTBEAT_SLICE_SECONDS)])

    def test_authenticated_orphan_stops_on_departed_process(self):
        self.assertTrue(self.acquire()[0])
        ok, detail, prepared = semaphore._prepare_master_heartbeat_launch(self.adapter)
        self.assertTrue(ok, detail)
        self.stop_client(self.sessions[0][1])
        before = semaphore.master_snapshot()["lease"]["renewed_at"]
        ok, detail = semaphore.master_heartbeat(1, adapter=self.adapter, launch_capability=prepared.capability)
        self.assertTrue(ok, detail)
        self.assertIn("witness is gone", detail)
        self.assertEqual(semaphore.master_snapshot()["lease"]["renewed_at"], before)

    def test_original_witness_is_rechecked_inside_renewal_transaction(self):
        self.assertTrue(self.acquire()[0])
        ok, detail, binding = semaphore._authenticate_master_heartbeat_start(self.adapter)
        self.assertTrue(ok, detail)
        self.stop_client(self.sessions[0][1])
        before = semaphore.master_snapshot()
        self.assertFalse(semaphore._renew_master_heartbeat(binding, adapter=self.adapter)[0])
        self.assertEqual(semaphore.master_snapshot(), before)

    def test_unobservable_witness_preserves_live_lease_and_refuses_direct_renewal(self):
        self.assertTrue(self.acquire()[0])
        before = semaphore.master_snapshot()
        unavailable = Adapter.unsupported("unknown")
        self.assertEqual(semaphore._master_view(before["lease"], semaphore._now(), unavailable)["state"], "live")
        self.assertFalse(semaphore.master_renew(adapter=unavailable)[0])
        self.assertFalse(semaphore.master_acquire("codex", "unobservable", take_over=True, adapter=unavailable)[0])
        self.assertEqual(semaphore.master_snapshot(), before)

    def test_missing_or_unreadable_first_guard_does_not_use_parent_guard(self):
        current, parent = self.sessions[0][0], self.sessions[1][0]
        os.environ["PATH"] = str(current) + os.pathsep + str(parent)
        with mock.patch("creme.adapters.session.Path.lstat", side_effect=PermissionError("denied")):
            self.assertIsNone(codex_process_witness())
        os.environ["PATH"] = str(current.parent / "codex-arg0Missing") + os.pathsep + str(parent)
        self.assertIsNone(codex_process_witness())

    def test_lock_permission_failure_is_unknown_and_file_replacement_is_dead(self):
        witness = codex_process_witness()
        self.assertTrue(lock_alive(witness))
        with mock.patch("creme.adapters.session.os.open", side_effect=PermissionError("denied")):
            self.assertIsNone(lock_alive(witness))
        self.assertFalse(lock_alive(dict(witness, inode=witness["inode"] + 1)))

    def test_schema_three_migration_does_not_invent_an_old_witness(self):
        self.assertTrue(self.acquire()[0])
        data = semaphore.master_snapshot()
        data["schema_version"] = 3
        del data["lease"]["process_witness"]
        raw = json.dumps(data)
        semaphore.master_path().write_text(raw)
        upgraded = semaphore.master_snapshot()
        self.assertEqual(upgraded["schema_version"], 4)
        self.assertIsNone(upgraded["lease"]["process_witness"])
        self.assertEqual(semaphore.master_path().read_text(), raw)
        self.assertTrue(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertIsNone(semaphore.master_snapshot()["lease"]["process_witness"])

    def test_old_alias_digest_is_not_silently_adopted_during_migration(self):
        self.assertTrue(self.acquire()[0])
        data = semaphore.master_snapshot()
        data["schema_version"] = 3
        del data["lease"]["process_witness"]
        data["lease"]["session_digest"] = semaphore._digest_session_value(
            semaphore.MASTER_SESSION_DIGEST_DOMAIN, os.environ["CODEX_SESSION_ID"])
        raw = json.dumps(data)
        semaphore.master_path().write_text(raw)
        self.assertFalse(semaphore.master_renew(adapter=self.adapter)[0])
        self.assertFalse(semaphore.master_release(adapter=self.adapter)[0])
        self.assertEqual(semaphore.master_path().read_text(), raw)
        data["lease"]["acquired_at"] = data["lease"]["renewed_at"] = data["lease"]["direct_activity_at"] = 1.0
        semaphore.master_path().write_text(json.dumps(data))
        self.assertTrue(self.acquire("new identity", take_over=True)[0])
        self.assertIsNotNone(semaphore.master_snapshot()["lease"]["process_witness"])

    def test_corrupt_witness_is_refused_without_state_replacement(self):
        self.assertTrue(self.acquire()[0])
        data = semaphore.master_snapshot()
        witness = data["lease"]["process_witness"]
        bad = [dict(witness, path="/tmp/\x00/tmp/arg0/codex-arg0Bad/.lock"),
               dict(witness, inode=True), dict(witness, uid=os.getuid() + 1),
               dict(witness, path="/tmp/../tmp/arg0/codex-arg0Bad/.lock")]
        for value in bad:
            with self.subTest(witness=value):
                data["lease"]["process_witness"] = value
                raw = json.dumps(data)
                semaphore.master_path().write_text(raw)
                with self.assertRaises(semaphore.SemaphoreError):
                    self.acquire("must fail", take_over=True)
                self.assertEqual(semaphore.master_path().read_text(), raw)
        self.assertFalse(valid_process_witness(bad[0]))
        self.assertIsNone(lock_alive(bad[0]))


if __name__ == "__main__":
    unittest.main()
