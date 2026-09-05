from __future__ import annotations

import hashlib
import multiprocessing
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from creme import master_runtime


class SyntheticFault(RuntimeError):
    pass


def tree_snapshot(root: Path):
    rows = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                data = None
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    data = path.read_bytes()
                elif stat.S_ISLNK(info.st_mode):
                    data = os.readlink(path).encode("utf-8")
                rows.append(
                    (
                        path.relative_to(root).as_posix(),
                        info.st_mode,
                        info.st_nlink,
                        data,
                    )
                )
    return sorted(rows)


def process_append(root: str, index: int, start, results) -> None:
    try:
        if not start.wait(5):
            raise RuntimeError("process append start timed out")
        writer = master_runtime.RecordWriter(
            Path(root),
            renew=lambda: (True, "synthetic holder verified"),
            lease_snapshot=lambda: {
                "schema_version": 4,
                "lease": {"client": "codex", "lease_id": "a" * 32},
            },
        )
        writer.append("goal", {
            "goal_id": f"process-goal-{index:02d}",
            "status": "active",
            "worktree": f".worktrees/process-goal-{index:02d}",
            "branch": f"codex/process-goal-{index:02d}",
            "checkpoint": f"{index + 1:040x}",
            "next_unit": f"process-unit-{index:02d}",
        })
        results.put(None)
    except Exception as exc:
        results.put(repr(exc))


def kill_append_at(root: str, stage: str) -> None:
    writer = master_runtime.RecordWriter(
        Path(root),
        renew=lambda: (True, "synthetic holder verified"),
        lease_snapshot=lambda: {
            "schema_version": 4,
            "lease": {"client": "codex", "lease_id": "a" * 32},
        },
    )
    writer.append(
        "note",
        {
            "title": "process-death",
            "note": "synthetic publication boundary",
            "evidence": "synthetic-process-death.json",
            "next_unit": "recover",
        },
        fault=lambda current: os._exit(71) if current == stage else None,
    )
    os._exit(72)


class MasterRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "master"
        master_runtime.initialize_empty_record(self.root)
        self.renewals = 0
        self.lease_id = "a" * 32

    def renew(self):
        self.renewals += 1
        return True, "holder verified"

    def snapshot(self):
        return {
            "schema_version": 4,
            "lease": {"client": "codex", "lease_id": self.lease_id},
        }

    def writer(self, **kwargs):
        return master_runtime.RecordWriter(
            self.root,
            renew=kwargs.pop("renew", self.renew),
            lease_snapshot=kwargs.pop("lease_snapshot", self.snapshot),
            **kwargs,
        )

    @staticmethod
    def goal_payload(goal_id="goal-one", status="active", next_unit="unit two"):
        return {
            "goal_id": goal_id,
            "status": status,
            "worktree": ".worktrees/goal-one",
            "branch": "codex/goal-one",
            "checkpoint": "1" * 40,
            "next_unit": next_unit,
        }

    @staticmethod
    def note_payload(title="observation", next_unit="next"):
        return {
            "title": title,
            "note": "bounded operational observation",
            "evidence": "synthetic-evidence.json",
            "next_unit": next_unit,
        }

    def core_bytes(self):
        return {
            name: (self.root / name).read_bytes()
            for name in (master_runtime.EVENTS_NAME, master_runtime.BOARD_NAME)
        }

    def test_initialization_is_private_and_idempotent(self):
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for name in (
            master_runtime.EVENTS_NAME,
            master_runtime.BOARD_NAME,
            master_runtime.LOCK_NAME,
            master_runtime.README_NAME,
        ):
            path = self.root / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for name in master_runtime.PRIVATE_DIRECTORIES:
            path = self.root / name
            self.assertTrue(path.is_dir(), name)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        before = self.core_bytes()
        self.assertFalse(master_runtime.initialize_empty_record(self.root))
        self.assertEqual(self.core_bytes(), before)

    def test_initialization_refuses_relative_symlinked_and_legacy_roots(self):
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "absolute"):
            master_runtime.initialize_empty_record(Path("relative/master"))

        target = Path(self.temporary.name).resolve() / "target"
        target.mkdir(mode=0o700)
        link = Path(self.temporary.name).resolve() / "linked"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "symlinked"):
            master_runtime.initialize_empty_record(link / "master")

        legacy = Path(self.temporary.name).resolve() / "legacy"
        legacy.mkdir(mode=0o700)
        (legacy / "log.md").write_text("legacy\n", encoding="utf-8")
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "migration"):
            master_runtime.initialize_empty_record(legacy)

    def test_all_event_kinds_validate_and_reduce_deterministically(self):
        payloads = [
            ("master", {
                "action": "start",
                "model": "synthetic-model",
                "effort": "high",
                "note": "start synthetic master",
                "next_unit": "first unit",
                "reconciliation": [{
                    "repository": "creme",
                    "kind": "tracked-dirt",
                    "subject": "worktree",
                    "recorded": "clean",
                    "observed": "modified",
                    "detail": "synthetic tracked modification",
                }],
            }),
            ("goal", self.goal_payload()),
            ("merge", {
                "goal_id": "goal-one",
                "candidate": "1" * 40,
                "result": "2" * 40,
                "evidence": "synthetic-gates.json",
                "audit_worthy": True,
            }),
            ("decision", {
                "decision_id": "decision-one",
                "status": "open",
                "title": "synthetic choice",
                "choice": "option-a",
                "reason": "bounded reason",
                "alternatives": ["option-b"],
                "reversible": True,
                "undo": "restore option-b",
                "evidence": "synthetic-decision.json",
                "authority": "master",
            }),
            ("procedure", {
                "procedure_id": "procedure-one",
                "action": "replace",
                "failure": "synthetic failure",
                "replacement": "synthetic replacement",
                "control": "synthetic control",
                "evidence": "synthetic-procedure.json",
            }),
            ("audit", {
                "audit_id": "audit-one",
                "audit_kind": "continuity",
                "verdict": "REJECT",
                "report": "synthetic-audit.md",
                "findings": [{
                    "finding_id": "finding-one",
                    "status": "open",
                    "severity": "high",
                    "summary": "synthetic finding",
                    "evidence": "synthetic-audit.json",
                }],
            }),
            ("note", self.note_payload()),
        ]
        writer = self.writer()
        for kind, payload in payloads:
            writer.append(kind, payload)
        view = master_runtime.read_record(self.root)
        self.assertTrue(view.board_current)
        self.assertEqual(len(view.events), len(payloads))
        self.assertEqual(view.board["goals"][0]["goal_id"], "goal-one")
        self.assertEqual(view.board["open_decisions"][0]["decision_id"], "decision-one")
        self.assertEqual(
            view.board["open_audit_findings"][0]["finding_id"], "finding-one"
        )
        self.assertEqual(view.board["next_unit"], "next")
        self.assertEqual(
            view.board["source"]["log_digest"],
            hashlib.sha256(view.log_bytes).hexdigest(),
        )
        self.assertEqual(
            master_runtime.render_board(view.events),
            master_runtime.render_board(tuple(view.events)),
        )
        self.assertEqual(self.renewals, 2 * len(payloads))

    def test_goal_decision_and_finding_updates_supersede_only_current_rows(self):
        writer = self.writer()
        writer.append("goal", self.goal_payload(status="active"))
        writer.append("goal", self.goal_payload(status="complete", next_unit="archive"))
        decision = {
            "decision_id": "decision-one",
            "status": "open",
            "title": "choice",
            "choice": "a",
            "reason": "reason",
            "alternatives": ["b"],
            "reversible": False,
            "undo": None,
            "evidence": "evidence",
            "authority": "user",
        }
        writer.append("decision", decision)
        writer.append("decision", {**decision, "status": "resolved", "choice": "b"})
        finding = {
            "audit_id": "audit-one",
            "audit_kind": "merge-hygiene",
            "verdict": "REJECT",
            "report": "audit.md",
            "findings": [{
                "finding_id": "finding-one",
                "status": "open",
                "severity": "medium",
                "summary": "finding",
                "evidence": "evidence",
            }],
        }
        writer.append("audit", finding)
        closed = dict(finding)
        closed["verdict"] = "ACCEPT"
        closed["findings"] = [{**finding["findings"][0], "status": "closed"}]
        writer.append("audit", closed)
        board = master_runtime.read_record(self.root).board
        self.assertEqual(len(board["goals"]), 1)
        self.assertEqual(board["goals"][0]["status"], "complete")
        self.assertEqual(board["open_decisions"], [])
        self.assertEqual(board["open_audit_findings"], [])
        self.assertEqual(board["source"]["event_count"], 6)

    def test_schema_rejects_unknown_wrong_non_utc_non_finite_and_oversize_values(self):
        writer = self.writer()
        invalid = [
            ("goal", {**self.goal_payload(), "unknown": "field"}),
            ("goal", {**self.goal_payload(), "status": True}),
            ("note", {**self.note_payload(), "note": float("nan")}),
            ("note", {**self.note_payload(), "note": "x" * (master_runtime.MAX_TEXT_BYTES + 1)}),
        ]
        before = self.core_bytes()
        for kind, payload in invalid:
            with self.subTest(kind=kind, payload=list(payload)):
                with self.assertRaises(master_runtime.MasterRecordError):
                    writer.append(kind, payload)
                self.assertEqual(self.core_bytes(), before)

        event = {
            "schema_version": 1,
            "event_id": "1" * 32,
            "timestamp": "2026-01-01T00:00:00.000000+00:00",
            "kind": "note",
            "actor": {
                "client": "codex",
                "acquisition_digest": "2" * 64,
            },
            "payload": self.note_payload(),
        }
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "UTC RFC-3339"):
            master_runtime.validate_event(event)

    def test_renewal_refusal_precedes_private_record_access_and_preserves_bytes(self):
        before = self.core_bytes()
        os.chmod(self.root / master_runtime.LOCK_NAME, 0)
        self.addCleanup(os.chmod, self.root / master_runtime.LOCK_NAME, 0o600)
        writer = self.writer(renew=lambda: (False, "successor owns the lease"))
        with self.assertRaisesRegex(master_runtime.RenewalRefused, "successor"):
            writer.append("note", self.note_payload())
        self.assertEqual(self.core_bytes(), before)

    def test_second_renewal_closes_lock_wait_race(self):
        before = self.core_bytes()
        results = iter([(True, "holder verified"), (False, "lease changed")])
        writer = self.writer(renew=lambda: next(results))
        with self.assertRaisesRegex(master_runtime.RenewalRefused, "lease changed"):
            writer.append("note", self.note_payload())
        self.assertEqual(self.core_bytes(), before)

    def test_actor_is_bound_to_a_domain_separated_acquisition_digest(self):
        event = self.writer().append("note", self.note_payload()).event
        expected = hashlib.sha256(
            b"creme-master-record-acquisition-v1\0" + self.lease_id.encode("ascii")
        ).hexdigest()
        self.assertEqual(
            event["actor"],
            {"client": "codex", "acquisition_digest": expected},
        )
        serialized = (self.root / master_runtime.EVENTS_NAME).read_text(encoding="utf-8")
        self.assertNotIn(self.lease_id, serialized)

    def test_duplicate_generated_id_refuses_without_mutation(self):
        event_id = "d" * 32
        writer = self.writer(event_id=lambda: event_id)
        writer.append("note", self.note_payload("first"))
        before = self.core_bytes()
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "duplicate event ID"):
            writer.append("note", self.note_payload("second"))
        self.assertEqual(self.core_bytes(), before)

    def test_corrupt_or_partial_authoritative_log_is_preserved(self):
        writer = self.writer()
        writer.append("note", self.note_payload())
        log = self.root / master_runtime.EVENTS_NAME
        log.write_bytes(log.read_bytes() + b'{"partial":')
        corrupt = self.core_bytes()
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "partial final record"):
            writer.append("note", self.note_payload("later"))
        self.assertEqual(self.core_bytes(), corrupt)

    def test_stale_but_valid_board_is_repaired_by_the_next_authorized_append(self):
        writer = self.writer()
        writer.append("note", self.note_payload("first"))
        (self.root / master_runtime.BOARD_NAME).write_bytes(master_runtime.render_board(()))
        stale = master_runtime.read_record(self.root)
        self.assertFalse(stale.board_current)
        result = writer.append("note", self.note_payload("second"))
        self.assertTrue(result.repaired_stale_board)
        current = master_runtime.read_record(self.root)
        self.assertTrue(current.board_current)
        self.assertEqual(current.board["source"]["event_count"], 2)

    def test_symlinked_or_hardlinked_core_paths_refuse(self):
        board = self.root / master_runtime.BOARD_NAME
        outside = Path(self.temporary.name).resolve() / "outside"
        outside.write_bytes(board.read_bytes())
        os.chmod(outside, 0o600)
        board.unlink()
        board.symlink_to(outside)
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "non-symlink"):
            self.writer().append("note", self.note_payload())

        board.unlink()
        os.link(outside, board)
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "hard links"):
            self.writer().append("note", self.note_payload())

    def test_non_private_layout_component_refuses_before_a_core_write(self):
        before = self.core_bytes()
        os.chmod(self.root / "intent", 0o755)
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "mode 0700"):
            self.writer().append("note", self.note_payload())
        self.assertEqual(self.core_bytes(), before)

    def test_unsafe_nested_private_nodes_refuse_current_without_mutation(self):
        cases = ("symlink", "file-mode", "directory-mode", "hardlink", "fifo")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                master_runtime.initialize_empty_record(root)
                nested = root / "intent" / "nested"
                nested.mkdir(mode=0o700)
                outside = root.parent / f"outside-{case}"
                outside.write_bytes(b"private\n")
                os.chmod(outside, 0o600)
                candidate = nested / "candidate"
                if case == "symlink":
                    candidate.symlink_to(outside)
                elif case == "file-mode":
                    candidate.write_bytes(b"private\n")
                    os.chmod(candidate, 0o644)
                elif case == "directory-mode":
                    candidate.mkdir(mode=0o755)
                elif case == "hardlink":
                    os.link(outside, candidate)
                else:
                    os.mkfifo(candidate, 0o600)

                def snapshot():
                    rows = []
                    for path in [root, *sorted(root.rglob("*"))]:
                        info = path.lstat()
                        data = None
                        if stat.S_ISREG(info.st_mode):
                            data = path.read_bytes()
                        elif stat.S_ISLNK(info.st_mode):
                            data = os.readlink(path).encode("utf-8")
                        rows.append(
                            (
                                path.relative_to(root).as_posix(),
                                info.st_mode,
                                info.st_nlink,
                                data,
                            )
                        )
                    return rows

                before = snapshot()
                with self.assertRaises(master_runtime.MasterRecordError):
                    master_runtime.read_record(root)
                self.assertEqual(snapshot(), before)

    def test_boolean_board_version_is_not_an_integer_schema(self):
        board_path = self.root / master_runtime.BOARD_NAME
        malformed = board_path.read_bytes().replace(b'"schema_version":1', b'"schema_version":true')
        board_path.write_bytes(malformed)
        before = self.core_bytes()
        with self.assertRaisesRegex(master_runtime.MasterRecordError, "explicit migration"):
            self.writer().append("note", self.note_payload())
        self.assertEqual(self.core_bytes(), before)

    def test_concurrent_authorized_writers_form_one_total_order(self):
        count = 24
        barrier = threading.Barrier(count)
        errors = []

        def append(index):
            try:
                barrier.wait()
                self.writer().append(
                    "goal",
                    self.goal_payload(
                        goal_id=f"goal-{index:02d}",
                        next_unit=f"unit-{index:02d}",
                    ),
                )
            except Exception as exc:  # pragma: no cover - retained for failure detail
                errors.append(exc)

        threads = [threading.Thread(target=append, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        view = master_runtime.read_record(self.root)
        self.assertTrue(view.board_current)
        self.assertEqual(len(view.events), count)
        self.assertEqual(len({event["event_id"] for event in view.events}), count)
        self.assertEqual(len(view.board["goals"]), count)

    def test_cross_process_lock_prevents_lost_or_duplicated_events(self):
        context = multiprocessing.get_context("fork")
        count = 8
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=process_append,
                args=(str(self.root), index, start, results),
            )
            for index in range(count)
        ]
        for process in processes:
            process.start()
        start.set()
        messages = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
        self.assertEqual(messages, [None] * count)
        self.assertEqual([process.exitcode for process in processes], [0] * count)
        view = master_runtime.read_record(self.root)
        self.assertTrue(view.board_current)
        self.assertEqual(len(view.events), count)
        self.assertEqual(len({event["event_id"] for event in view.events}), count)
        self.assertEqual(len(view.board["goals"]), count)

    def test_synchronized_writer_waits_for_live_publication(self):
        paused = threading.Event()
        release = threading.Event()
        contender_done = threading.Event()
        results = {}

        def pause(stage):
            if stage == "board:after-temp-create":
                paused.set()
                if not release.wait(5):
                    raise AssertionError("owner publication was not released")

        def owner():
            results["owner"] = self.writer().append(
                "note", self.note_payload("owner"), fault=pause
            )

        def contender():
            results["contender"] = self.writer().append(
                "note", self.note_payload("contender")
            )
            contender_done.set()

        first = threading.Thread(target=owner)
        first.start()
        self.assertTrue(paused.wait(5))
        second = threading.Thread(target=contender)
        second.start()
        self.assertFalse(contender_done.wait(0.2))
        release.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(set(results), {"owner", "contender"})
        view = master_runtime.read_record(self.root)
        self.assertEqual(len(view.events), 2)
        self.assertTrue(view.board_current)

    def test_process_death_at_every_publication_boundary_is_projected_and_recovered(self):
        before_log = {
            "transaction-description:before-create",
            "transaction-description:after-create",
            "transaction-description:before-dir-fsync",
            "transaction-description:after-dir-fsync",
            "log:before-temp-create",
            "log:after-temp-create",
            "log:after-write",
            "log:after-flush",
            "log:after-fsync",
            "log:before-replace",
        }
        after_log = {
            "log:after-replace",
            "log:before-dir-fsync",
            "log:after-dir-fsync",
            "transaction:after-log-commit",
            "board:before-temp-create",
            "board:after-temp-create",
            "board:after-write",
            "board:after-flush",
            "board:after-fsync",
            "board:before-replace",
        }
        after_board = {
            "board:after-replace",
            "board:before-dir-fsync",
            "board:after-dir-fsync",
            "transaction-description:before-remove-temporaries",
            "transaction-description:after-remove-temporaries",
            "transaction-description:after-remove",
            "transaction-description:after-remove-dir-fsync",
        }
        context = multiprocessing.get_context("fork")
        for stage in sorted(before_log | after_log | after_board):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                master_runtime.initialize_empty_record(root)
                process = context.Process(target=kill_append_at, args=(str(root), stage))
                process.start()
                process.join(10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 71)

                before_read = tree_snapshot(root)
                view = master_runtime.read_record(root)
                self.assertEqual(tree_snapshot(root), before_read)
                accepted = 0 if stage in before_log else 1
                self.assertEqual(len(view.events), accepted)
                if stage in after_log:
                    self.assertFalse(view.board_current)
                else:
                    self.assertTrue(view.board_current)

                writer = master_runtime.RecordWriter(
                    root,
                    renew=self.renew,
                    lease_snapshot=self.snapshot,
                )
                writer.append("note", self.note_payload("successor"))
                final = master_runtime.read_record(root)
                self.assertEqual(len(final.events), accepted + 1)
                self.assertTrue(final.board_current)
                self.assertFalse(
                    any(
                        path.name.startswith(".record-transaction-v1.")
                        or path.name.endswith(".record-tmp")
                        for path in root.iterdir()
                    )
                )

    def test_forged_publication_nodes_refuse_unchanged(self):
        cases = ("un described", "wrong source", "forged target", "marker child")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "master"
                master_runtime.initialize_empty_record(root)
                source = hashlib.sha256(b"").hexdigest()
                nonce = "1" * 16
                target = "2" * 64
                board = "3" * 64
                temporary_name = master_runtime._publication_temp_name(
                    master_runtime.EVENTS_NAME, nonce
                )
                if case == "un described":
                    path = root / temporary_name
                    path.write_bytes(b"forged bytes\n")
                    path.chmod(0o600)
                else:
                    if case == "wrong source":
                        source = "4" * 64
                    marker_name = master_runtime._publication_marker_name(
                        "append", nonce, source, target, board
                    )
                    marker = root / marker_name
                    marker.mkdir(mode=0o700)
                    if case == "marker child":
                        child = marker / "forged"
                        child.write_bytes(b"forged bytes\n")
                        child.chmod(0o600)
                    else:
                        path = root / temporary_name
                        path.write_bytes(b"forged bytes\n")
                        path.chmod(0o600)
                before = tree_snapshot(root)
                with self.assertRaises(master_runtime.MasterRecordError):
                    master_runtime.read_record(root)
                with self.assertRaises(master_runtime.MasterRecordError):
                    master_runtime.RecordWriter(
                        root,
                        renew=self.renew,
                        lease_snapshot=self.snapshot,
                    ).append("note", self.note_payload())
                self.assertEqual(tree_snapshot(root), before)

    def test_faults_leave_only_old_or_log_authoritative_or_fully_new_state(self):
        before_log_stages = {
            "log:before-temp-create",
            "log:after-temp-create",
            "log:after-write",
            "log:after-flush",
            "log:after-fsync",
            "log:before-replace",
        }
        split_stages = {
            "log:after-replace",
            "log:before-dir-fsync",
            "log:after-dir-fsync",
            "transaction:after-log-commit",
            "board:before-temp-create",
            "board:after-temp-create",
            "board:after-write",
            "board:after-flush",
            "board:after-fsync",
            "board:before-replace",
        }
        new_stages = {
            "board:after-replace",
            "board:before-dir-fsync",
            "board:after-dir-fsync",
        }
        stages = before_log_stages | split_stages | new_stages
        for stage in sorted(stages):
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary).resolve() / "master"
                    master_runtime.initialize_empty_record(root)
                    writer = master_runtime.RecordWriter(
                        root,
                        renew=self.renew,
                        lease_snapshot=self.snapshot,
                    )

                    def inject(current):
                        if current == stage:
                            raise SyntheticFault(stage)

                    with self.assertRaisesRegex(SyntheticFault, re_escape(stage)):
                        writer.append("note", self.note_payload(), fault=inject)
                    view = master_runtime.read_record(root)
                    if stage in before_log_stages:
                        self.assertEqual(len(view.events), 0)
                        self.assertTrue(view.board_current)
                    elif stage in split_stages:
                        self.assertEqual(len(view.events), 1)
                        self.assertFalse(view.board_current)
                    else:
                        self.assertEqual(len(view.events), 1)
                        self.assertTrue(view.board_current)

    def test_split_state_from_fault_is_repaired_on_the_following_append(self):
        writer = self.writer()

        def inject(stage):
            if stage == "transaction:after-log-commit":
                raise SyntheticFault(stage)

        with self.assertRaises(SyntheticFault):
            writer.append("note", self.note_payload("accepted"), fault=inject)
        split = master_runtime.read_record(self.root)
        self.assertEqual(len(split.events), 1)
        self.assertFalse(split.board_current)
        result = writer.append("note", self.note_payload("repair"))
        self.assertTrue(result.repaired_stale_board)
        final = master_runtime.read_record(self.root)
        self.assertTrue(final.board_current)
        self.assertEqual(len(final.events), 2)

    def test_once_per_acquisition_retry_repairs_without_a_duplicate_event(self):
        payload = {
            "action": "start",
            "model": "synthetic-model",
            "effort": "high",
            "note": "start",
            "next_unit": "unit",
            "reconciliation": [],
        }
        writer = self.writer()

        def inject(stage):
            if stage == "transaction:after-log-commit":
                raise SyntheticFault(stage)

        with self.assertRaises(SyntheticFault):
            writer.append(
                "master", payload, fault=inject, once_per_acquisition=True
            )
        split = master_runtime.read_record(self.root)
        self.assertEqual(len(split.events), 1)
        self.assertFalse(split.board_current)
        retried = writer.append(
            "master",
            {**payload, "action": "resume", "note": "retry"},
            once_per_acquisition=True,
        )
        self.assertTrue(retried.already_present)
        self.assertTrue(retried.repaired_stale_board)
        final = master_runtime.read_record(self.root)
        self.assertTrue(final.board_current)
        self.assertEqual(len(final.events), 1)
        self.assertEqual(final.events[0]["payload"]["action"], "start")


def re_escape(value: str) -> str:
    """Keep subtest regexes literal without importing another global module."""
    return value.replace("-", "\\-")


if __name__ == "__main__":
    unittest.main()
