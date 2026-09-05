"""Runtime record writes under the published schema-4 lease identities.

These controls compose the accepted record transaction with the published
upstream lease. They exercise a newly acquired schema-4 lease with a recorded
process witness, migrated legacy schema-2 and schema-3 records that must not
acquire a witness retroactively, an old single-alias Codex digest that is not
the new combined identity, and a renewal transaction that meets a gone
process witness. Real process-lifetime locks, isolated lease state, and a
synthetic private record are used; no live host record is read.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import master_runtime, semaphore
from creme.adapters.linux import LinuxAdapter


def note_payload(title: str) -> dict[str, str]:
    return {
        "title": title,
        "note": "bounded operational observation",
        "evidence": "synthetic-evidence.json",
        "next_unit": "next",
    }


def acquisition_digest(lease_id: str) -> str:
    return hashlib.sha256(
        master_runtime._ACQUISITION_DOMAIN + lease_id.encode("ascii")
    ).hexdigest()


class WitnessRuntimeTest(unittest.TestCase):
    """Real lifetime locks, identical local PID 1, one synthetic private record."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.record_root = self.root / "record"
        master_runtime.initialize_empty_record(self.record_root)
        self.adapter = LinuxAdapter()
        patchers = (
            mock.patch.dict(os.environ, {
                "CREME_SEMAPHORE_DIR": str(self.root / "state"),
                "CREME_MASTER_SESSION_ID": "",
                "CREME_MASTER_LIVENESS_SOCKET": "",
            }),
            mock.patch(
                "creme.semaphore._client_process",
                return_value=(1, "codex", "client codex pid 1"),
            ),
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

    def as_session(self, number):
        directory, _process = self.sessions[number]
        os.environ["PATH"] = str(directory) + os.pathsep + os.defpath
        os.environ["CODEX_SESSION_ID"] = f"session-{number}"
        os.environ["CODEX_THREAD_ID"] = f"thread-{number}"

    def acquire(self, note="owner", **kwargs):
        return semaphore.master_acquire("codex", note, adapter=self.adapter, **kwargs)

    def renew(self):
        return semaphore.master_renew(adapter=self.adapter)

    def transaction(self):
        return semaphore.master_authority_transaction(adapter=self.adapter)

    def writer(self, *, renew=None):
        return master_runtime.RecordWriter(
            self.record_root,
            renew=renew or self.renew,
            lease_snapshot=semaphore.master_snapshot,
            authority_transaction=self.transaction,
        )

    def core_bytes(self) -> dict[str, bytes]:
        return {
            name: (self.record_root / name).read_bytes()
            for name in (master_runtime.EVENTS_NAME, master_runtime.BOARD_NAME)
        } | {"master.json": semaphore.master_path().read_bytes()}

    def persisted_lease(self) -> dict:
        return json.loads(semaphore.master_path().read_text(encoding="utf-8"))

    def assert_event_bound(self, event, lease) -> None:
        self.assertEqual(event["actor"]["client"], "codex")
        self.assertEqual(event["actor"]["acquisition_digest"], acquisition_digest(lease["lease_id"]))
        self.assertNotIn(lease["lease_id"], json.dumps(event))
        self.assertNotIn("process_witness", json.dumps(event))

    def test_write_under_newly_acquired_schema_four_identity_keeps_the_witness(self):
        ok, detail = self.acquire()
        self.assertTrue(ok, detail)
        acquired = semaphore.master_snapshot()
        self.assertEqual(acquired["schema_version"], semaphore.MASTER_SCHEMA_VERSION)
        witness = acquired["lease"]["process_witness"]
        self.assertIsNotNone(witness)
        result = self.writer().append("note", note_payload("first"))
        self.assert_event_bound(result.event, acquired["lease"])
        self.assertFalse(result.already_present)
        view = master_runtime.read_record(self.record_root)
        self.assertEqual(len(view.events), 1)
        self.assertTrue(view.board_current)
        persisted = self.persisted_lease()
        self.assertEqual(persisted["schema_version"], semaphore.MASTER_SCHEMA_VERSION)
        self.assertEqual(persisted["lease"]["process_witness"], witness)
        self.assertEqual(persisted["lease"]["lease_id"], acquired["lease"]["lease_id"])
        self.assertGreaterEqual(persisted["lease"]["renewed_at"], acquired["lease"]["renewed_at"])
        self.assertEqual(persisted["lease"]["heartbeat_renewals"], 0)
        self.assertEqual(persisted["lease"]["direct_activity_at"], persisted["lease"]["renewed_at"])

    def test_write_under_migrated_legacy_identity_does_not_invent_a_witness(self):
        ok, detail = self.acquire()
        self.assertTrue(ok, detail)
        acquired = semaphore.master_snapshot()
        events = 0
        for version in (3, 2):
            with self.subTest(schema_version=version):
                legacy = json.loads(json.dumps(acquired))
                legacy["schema_version"] = version
                del legacy["lease"]["process_witness"]
                if version == 2:
                    del legacy["lease"]["heartbeat_launch_digest"]
                    del legacy["lease"]["heartbeat_launch_expires_at"]
                raw = json.dumps(legacy)
                semaphore.master_path().write_text(raw, encoding="utf-8")
                upgraded = semaphore.master_snapshot()
                self.assertEqual(upgraded["schema_version"], semaphore.MASTER_SCHEMA_VERSION)
                self.assertIsNone(upgraded["lease"]["process_witness"])
                self.assertEqual(semaphore.master_path().read_text(encoding="utf-8"), raw)
                digest = master_runtime.read_record(self.record_root)
                self.assertEqual(len(digest.events), events)
                self.assertEqual(semaphore.master_path().read_text(encoding="utf-8"), raw)
                result = self.writer().append("note", note_payload(f"schema-{version}"))
                events += 1
                self.assert_event_bound(result.event, acquired["lease"])
                view = master_runtime.read_record(self.record_root)
                self.assertEqual(len(view.events), events)
                self.assertTrue(view.board_current)
                persisted = self.persisted_lease()
                self.assertEqual(persisted["schema_version"], semaphore.MASTER_SCHEMA_VERSION)
                self.assertIsNone(persisted["lease"]["process_witness"])
                self.assertEqual(persisted["lease"]["lease_id"], acquired["lease"]["lease_id"])
                self.assertEqual(persisted["lease"]["session_digest"], acquired["lease"]["session_digest"])
                self.assertFalse(persisted["lease"]["legacy_unbound"])
                self.assertEqual(set(persisted["lease"]), semaphore.MASTER_KEYS)

    def test_old_alias_digest_cannot_write_the_record(self):
        ok, detail = self.acquire()
        self.assertTrue(ok, detail)
        self.writer().append("note", note_payload("before"))
        legacy = semaphore.master_snapshot()
        legacy["schema_version"] = 3
        del legacy["lease"]["process_witness"]
        legacy["lease"]["session_digest"] = semaphore._digest_session_value(
            semaphore.MASTER_SESSION_DIGEST_DOMAIN, os.environ["CODEX_SESSION_ID"])
        semaphore.master_path().write_text(json.dumps(legacy), encoding="utf-8")
        before = self.core_bytes()
        with self.assertRaises(master_runtime.RenewalRefused):
            self.writer().append("note", note_payload("refused"))
        self.assertEqual(self.core_bytes(), before)
        with self.assertRaises(master_runtime.RenewalRefused):
            self.writer(renew=lambda: (True, "stale pre-lock renewal")).append(
                "note", note_payload("refused inside the transaction"))
        self.assertEqual(self.core_bytes(), before)
        self.assertEqual(len(master_runtime.read_record(self.record_root).events), 1)

    def test_renewal_transaction_meeting_a_gone_witness_refuses_without_writing(self):
        ok, detail = self.acquire()
        self.assertTrue(ok, detail)
        original = semaphore.master_snapshot()["lease"]
        self.writer().append("note", note_payload("while alive"))
        before = self.core_bytes()
        self.stop_client(self.sessions[0][1])

        with self.assertRaises(master_runtime.RenewalRefused) as first:
            self.writer().append("note", note_payload("after departure"))
        self.assertIn("witness is gone", str(first.exception))
        self.assertEqual(self.core_bytes(), before)

        # A pre-lock renewal that already passed cannot carry authority into
        # the transaction: the witness is re-checked under the record lock and
        # the semaphore mutex, before any lease or record byte changes.
        with self.assertRaises(master_runtime.RenewalRefused) as second:
            self.writer(renew=lambda: (True, "stale pre-lock renewal")).append(
                "note", note_payload("after departure"))
        self.assertIn("witness is gone", str(second.exception))
        self.assertIsInstance(second.exception.__cause__, semaphore.MasterAuthorityRefused)
        self.assertEqual(self.core_bytes(), before)

        with self.assertRaises(semaphore.MasterAuthorityRefused):
            with self.transaction():
                self.fail("a gone witness must not yield a snapshot")
        self.assertEqual(self.core_bytes(), before)
        self.assertEqual(len(master_runtime.read_record(self.record_root).events), 1)

        self.as_session(1)
        ok, detail = self.acquire("successor", take_over=True)
        self.assertTrue(ok, detail)
        self.assertIn("stranded", detail)
        successor = semaphore.master_snapshot()["lease"]
        self.assertNotEqual(successor["lease_id"], original["lease_id"])
        result = self.writer().append("note", note_payload("successor"))
        self.assert_event_bound(result.event, successor)
        view = master_runtime.read_record(self.record_root)
        self.assertEqual(len(view.events), 2)
        self.assertTrue(view.board_current)
        self.assertEqual(
            {event["actor"]["acquisition_digest"] for event in view.events},
            {acquisition_digest(original["lease_id"]), acquisition_digest(successor["lease_id"])},
        )


if __name__ == "__main__":
    unittest.main()
