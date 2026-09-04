from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from creme import cli, semaphore
from creme.adapters.base import Adapter


class HeadroomAdapter(Adapter):
    system = "Linux"

    def __init__(self, free_percent=80, total_gib=32, available=True):
        self.free_percent = free_percent
        self.total_gib = total_gib
        self.available = available

    def static_facts(self):
        return self.result("static_facts", "OK", "fixture", {
            "system": self.system,
            "machine": "fixture",
            "logical_cores": 8,
            "physical_memory_bytes": self.total_gib * 1024 ** 3,
        })

    def memory_headroom(self):
        if not self.available:
            return self.result("memory_headroom", "UNAVAILABLE", "fixture unavailable")
        return self.result("memory_headroom", "OK", "fixture", {
            "memory_free_percent": self.free_percent,
            "memory_available_bytes": int(
                self.total_gib * 1024 ** 3 * self.free_percent / 100
            ),
            "physical_memory_bytes": self.total_gib * 1024 ** 3,
            "swap_used_mib": 0,
        })

    def quiet_host(self):
        return self.result("quiet_host", "OK", "fixture quiet")

    def gui_sessions(self, owner_uid):
        return self.result("human_gui_sessions", "OK", "fixture", {"sessions": []})


def concurrent_admit_worker(state_directory, label, start, results):
    os.environ["CREME_SEMAPHORE_DIR"] = state_directory
    start.wait(5)
    result = semaphore.adaptive_acquire(
        label,
        "concurrent large proof",
        memory_gib=8,
        adapter=HeadroomAdapter(free_percent=80, total_gib=24),
        policy={
            "task_memory_gib": 8,
            "heavy_workers": 2,
            "light_workers": 4,
            "physical_memory_gib": 24.0,
            "profile_status": "VALID",
        },
    )
    results.put((label, *result))


class SemaphoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = HeadroomAdapter()
        self.policy = {
            "task_memory_gib": 2,
            "heavy_workers": 4,
            "light_workers": 4,
            "physical_memory_gib": 32.0,
            "profile_status": "VALID",
        }
        self.runtime_admission_policy = semaphore._runtime_admission_policy
        patcher = mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter_patcher = mock.patch("creme.semaphore.get_adapter", return_value=self.adapter)
        adapter_patcher.start()
        self.addCleanup(adapter_patcher.stop)
        policy_patcher = mock.patch(
            "creme.semaphore._runtime_admission_policy",
            return_value=self.policy,
        )
        policy_patcher.start()
        self.addCleanup(policy_patcher.stop)

    def expire(self, label):
        path = Path(self.tmp.name) / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        holds = ([data["hard"]] if data["hard"] else []) + data["soft"]
        hold = next(item for item in holds if item["label"] == label)
        hold["acquired_at"] = 1
        hold["renewed_at"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_linked_worktree_resolves_the_canonical_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            canonical = temporary / "creme"
            worktree = temporary / "elsewhere" / "goal"
            git_dir = canonical / ".git" / "worktrees" / "goal"
            git_dir.mkdir(parents=True)
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")

            self.assertEqual(semaphore.canonical_creme_root(worktree), canonical.resolve())

    def test_missing_profile_forces_conservative_worker_policy(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "creme.semaphore.canonical_creme_root", return_value=Path(tmp)
        ):
            policy = self.runtime_admission_policy(HeadroomAdapter(total_gib=32))

        self.assertEqual(policy["profile_status"], "MISSING")
        self.assertEqual(policy["task_memory_gib"], 2)
        self.assertEqual(policy["heavy_workers"], 1)
        self.assertEqual(policy["physical_memory_gib"], 32.0)

    def test_state_selection_keeps_existing_install_on_legacy_until_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            neutral = temporary / "creme" / ".semaphore" / "state"
            legacy = temporary / "legacy"

            self.assertEqual(semaphore._select_state_root(neutral, legacy), neutral)
            legacy.mkdir()
            (legacy / "mutex").touch()
            self.assertEqual(semaphore._select_state_root(neutral, legacy), legacy)
            neutral.mkdir(parents=True)
            (neutral / "state.json").write_text(
                json.dumps(semaphore._empty_state()), encoding="utf-8"
            )
            self.assertEqual(semaphore._select_state_root(neutral, legacy), neutral)

    def test_migration_copies_live_holds_and_retains_legacy_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            neutral = temporary / "creme" / ".semaphore" / "state"
            legacy = temporary / "legacy"
            with mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": str(legacy)}):
                self.assertTrue(semaphore.acquire("soft", "live", "existing session")[0])
            legacy_before = (legacy / "state.json").read_bytes()

            ok, detail = semaphore.migrate_legacy_state(neutral, legacy)

            self.assertTrue(ok, detail)
            self.assertIn("legacy state retained", detail)
            self.assertEqual((legacy / "state.json").read_bytes(), legacy_before)
            migrated = semaphore._load_state(neutral / "state.json")
            self.assertEqual([item["label"] for item in migrated["soft"]], ["live"])
            self.assertTrue((neutral / "log.jsonl").is_file())

    def test_migration_refuses_to_replace_corrupt_neutral_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            neutral = temporary / "neutral"
            legacy = temporary / "legacy"
            neutral.mkdir()
            (neutral / "state.json").write_text("not-json", encoding="utf-8")
            before = (neutral / "state.json").read_bytes()

            with self.assertRaises(semaphore.SemaphoreError):
                semaphore.migrate_legacy_state(neutral, legacy)

            self.assertEqual((neutral / "state.json").read_bytes(), before)

    def test_repeated_migration_ignores_retained_legacy_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            neutral = temporary / "neutral"
            legacy = temporary / "legacy"
            neutral.mkdir()
            legacy.mkdir()
            expected = semaphore._empty_state()
            (neutral / "state.json").write_text(json.dumps(expected), encoding="utf-8")
            (legacy / "state.json").write_text("retained legacy drift", encoding="utf-8")

            ok, detail = semaphore.migrate_legacy_state(neutral, legacy)

            self.assertTrue(ok, detail)
            self.assertIn("already active", detail)
            self.assertEqual(semaphore._load_state(neutral / "state.json"), expected)
            self.assertEqual(
                (legacy / "state.json").read_text(encoding="utf-8"),
                "retained legacy drift",
            )

    def test_waiter_reselects_neutral_state_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            neutral = temporary / "neutral"
            legacy = temporary / "legacy"
            neutral.mkdir()
            (neutral / "state.json").write_text(
                json.dumps(semaphore._empty_state()), encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(
                    semaphore, "state_root", side_effect=[legacy, neutral]
                ):
                    with mock.patch.object(
                        semaphore, "legacy_state_root", return_value=legacy
                    ):
                        with semaphore.locked_state() as (path, state):
                            self.assertEqual(path, neutral / "state.json")
                            self.assertEqual(state, semaphore._empty_state())

    def test_xy_interleaving_and_conversion(self):
        self.assertTrue(semaphore.acquire("soft", "X", "build")[0])
        self.assertTrue(semaphore.acquire("soft", "Y", "build")[0])
        self.assertFalse(semaphore.acquire("hard", "Y", "timing")[0])
        self.assertTrue(semaphore.release("soft", "X")[0])
        self.assertTrue(semaphore.acquire("hard", "Y", "timing")[0])
        state = semaphore.snapshot()
        self.assertEqual(state["hard"]["label"], "Y")
        self.assertEqual(state["soft"], [])
        self.assertFalse(semaphore.acquire("soft", "X", "build")[0])

    def test_expired_hold_still_blocks_until_certified_break(self):
        semaphore.acquire("soft", "old", "work", 1)
        self.expire("old")
        self.assertFalse(semaphore.acquire("hard", "new", "timing")[0])
        self.assertTrue(semaphore.break_expired("old", "orphaned", HeadroomAdapter())[0])
        self.assertTrue(semaphore.acquire("hard", "new", "timing")[0])

    def test_adaptive_acquire_chooses_soft_when_parallel_budget_fits(self):
        ok, detail = semaphore.adaptive_acquire("goal", "proof", memory_gib=2)

        self.assertTrue(ok, detail)
        self.assertIn("ADMITTED_SOFT", detail)
        state = semaphore.snapshot()
        self.assertEqual(state["soft"][0]["label"], "goal")
        note, memory_gib, contention = semaphore._decode_admission_note(
            state["soft"][0]["note"], 8
        )
        self.assertEqual((note, memory_gib, contention), ("proof", 2, "tolerant"))

    def test_contention_sensitive_work_chooses_hard(self):
        ok, detail = semaphore.adaptive_acquire(
            "goal", "cold rebuild", memory_gib=4, contention="sensitive"
        )

        self.assertTrue(ok, detail)
        self.assertIn("ADMITTED_HARD", detail)
        self.assertEqual(semaphore.snapshot()["hard"]["label"], "goal")

    def test_adaptive_release_handles_both_selected_hold_kinds(self):
        self.assertTrue(semaphore.adaptive_acquire("soft", "proof")[0])
        self.assertEqual(
            semaphore.adaptive_release("soft"),
            (True, "soft hold released"),
        )
        self.assertTrue(semaphore.adaptive_acquire(
            "hard", "broad build", contention="sensitive"
        )[0])
        self.assertEqual(
            semaphore.adaptive_release("hard"),
            (True, "hard hold released"),
        )
        self.assertEqual(semaphore.snapshot(), semaphore._empty_state())

    def test_contention_sensitive_work_defers_for_existing_soft_hold(self):
        self.assertTrue(semaphore.adaptive_acquire("older", "proof", memory_gib=2)[0])

        ok, detail = semaphore.adaptive_acquire(
            "goal", "cold rebuild", memory_gib=4, contention="sensitive"
        )

        self.assertFalse(ok)
        self.assertIn("DEFER_FOR_HARD", detail)
        self.assertIn("run light work", detail)
        self.assertEqual([item["label"] for item in semaphore.snapshot()["soft"]], ["older"])

    def test_parallel_peak_budget_promotes_second_large_task_to_deferred_hard(self):
        policy = {**self.policy, "task_memory_gib": 8, "heavy_workers": 3, "physical_memory_gib": 24.0}
        adapter = HeadroomAdapter(free_percent=80, total_gib=24)
        self.assertTrue(semaphore.adaptive_acquire(
            "first", "large proof", memory_gib=8, adapter=adapter, policy=policy
        )[0])

        ok, detail = semaphore.adaptive_acquire(
            "second", "large proof", memory_gib=8, adapter=adapter, policy=policy
        )

        self.assertFalse(ok)
        self.assertIn("DEFER_FOR_HARD", detail)
        self.assertIn("peak reservations", detail)

    def test_cross_process_admission_race_is_atomic(self):
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=concurrent_admit_worker,
                args=(self.tmp.name, label, start, results),
            )
            for label in ("concurrent-a", "concurrent-b")
        ]
        for worker in workers:
            worker.start()
        start.set()
        outcomes = [results.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)

        admitted = [outcome for outcome in outcomes if outcome[1]]
        deferred = [outcome for outcome in outcomes if not outcome[1]]
        self.assertEqual(len(admitted), 1, outcomes)
        self.assertIn("ADMITTED_SOFT", admitted[0][2])
        self.assertEqual(len(deferred), 1, outcomes)
        self.assertIn("DEFER_FOR_HARD", deferred[0][2])
        self.assertEqual(len(semaphore.snapshot()["soft"]), 1)

    def test_low_memory_refuses_even_exclusive_start(self):
        adapter = HeadroomAdapter(free_percent=12, total_gib=24)

        ok, detail = semaphore.adaptive_acquire(
            "goal", "proof", contention="exclusive", adapter=adapter, policy=self.policy
        )

        self.assertFalse(ok)
        self.assertIn("LIGHT_ONLY", detail)
        self.assertEqual(semaphore.snapshot(), semaphore._empty_state())

    def test_unavailable_headroom_serializes_one_adaptive_task(self):
        unavailable = HeadroomAdapter(available=False)

        first_ok, first_detail = semaphore.adaptive_acquire(
            "first", "proof", adapter=unavailable, policy=self.policy
        )
        second_ok, second_detail = semaphore.adaptive_acquire(
            "second", "proof", adapter=unavailable, policy=self.policy
        )

        self.assertTrue(first_ok, first_detail)
        self.assertIn("ADMITTED_HARD", first_detail)
        self.assertFalse(second_ok)
        self.assertIn("DEFER_HEAVY", second_detail)

    def test_explicit_soft_acquire_cannot_bypass_low_memory(self):
        ok, detail = semaphore.acquire(
            "soft",
            "goal",
            "proof",
            adapter=HeadroomAdapter(free_percent=10),
            policy=self.policy,
        )

        self.assertFalse(ok)
        self.assertIn("LIGHT_ONLY", detail)

    def test_explicit_hard_acquire_cannot_bypass_low_memory(self):
        ok, detail = semaphore.acquire(
            "hard",
            "goal",
            "timing",
            adapter=HeadroomAdapter(free_percent=10),
            policy=self.policy,
        )

        self.assertFalse(ok)
        self.assertIn("LIGHT_ONLY", detail)

    def test_renew_drains_at_low_memory(self):
        self.assertTrue(semaphore.adaptive_acquire("goal", "proof")[0])

        ok, detail = semaphore.renew(
            "goal", adapter=HeadroomAdapter(free_percent=18)
        )

        self.assertFalse(ok)
        self.assertIn("DRAIN_HEAVY", detail)

    def test_newer_soft_holder_yields_first_under_contention(self):
        self.assertTrue(semaphore.adaptive_acquire("older", "proof")[0])
        self.assertTrue(semaphore.adaptive_acquire("newer", "proof")[0])
        pressure = HeadroomAdapter(free_percent=25)

        newer_ok, newer_detail = semaphore.renew("newer", adapter=pressure)
        older_ok, older_detail = semaphore.renew("older", adapter=pressure)

        self.assertFalse(newer_ok)
        self.assertIn("YIELD_HEAVY", newer_detail)
        self.assertIn("older", newer_detail)
        self.assertTrue(older_ok, older_detail)

    def test_unavailable_renewal_headroom_yields_newer_soft_holder(self):
        self.assertTrue(semaphore.adaptive_acquire("older", "proof")[0])
        self.assertTrue(semaphore.adaptive_acquire("newer", "proof")[0])

        ok, detail = semaphore.renew(
            "newer", adapter=HeadroomAdapter(available=False)
        )

        self.assertFalse(ok)
        self.assertIn("YIELD_HEAVY", detail)

    def test_renew_proactively_serializes_overcommitted_peak_reservations(self):
        permissive = {**self.policy, "physical_memory_gib": 64.0}
        strict = {**self.policy, "physical_memory_gib": 24.0}
        adapter = HeadroomAdapter(free_percent=80, total_gib=24)
        self.assertTrue(semaphore.adaptive_acquire(
            "older", "large proof", memory_gib=8, policy=permissive
        )[0])
        self.assertTrue(semaphore.adaptive_acquire(
            "newer", "large proof", memory_gib=8, policy=permissive
        )[0])

        ok, detail = semaphore.renew("newer", adapter=adapter, policy=strict)

        self.assertFalse(ok)
        self.assertIn("YIELD_HEAVY", detail)
        self.assertIn("peak reservations", detail)

    def test_expired_hold_does_not_take_renewal_priority_from_live_holder(self):
        self.assertTrue(semaphore.adaptive_acquire("expired", "proof")[0])
        self.assertTrue(semaphore.adaptive_acquire("live", "proof")[0])
        self.expire("expired")

        ok, detail = semaphore.renew(
            "live", adapter=HeadroomAdapter(free_percent=25), policy=self.policy
        )

        self.assertTrue(ok, detail)
        self.assertIn("CONTINUE_HEAVY", detail)

    def test_manual_human_hold_blocks_adaptive_heavy_work(self):
        with semaphore.locked_state() as (path, state):
            state["soft"].append(
                semaphore._hold(
                    semaphore.MANUAL_LABEL,
                    "human active",
                    semaphore.MANUAL_GRACE_SECONDS,
                    manual=True,
                )
            )
            semaphore._save(path, state)

        ok, detail = semaphore.adaptive_acquire("goal", "proof")

        self.assertFalse(ok)
        self.assertIn("LIGHT_ONLY", detail)
        self.assertIn("human-session", detail)

    def test_manual_human_hold_makes_existing_heavy_holder_yield(self):
        self.assertTrue(semaphore.adaptive_acquire("goal", "proof")[0])
        with semaphore.locked_state() as (path, state):
            state["soft"].append(
                semaphore._hold(
                    semaphore.MANUAL_LABEL,
                    "human active",
                    semaphore.MANUAL_GRACE_SECONDS,
                    manual=True,
                )
            )
            semaphore._save(path, state)

        ok, detail = semaphore.renew("goal")

        self.assertFalse(ok)
        self.assertIn("YIELD_HEAVY", detail)
        self.assertIn("human-session", detail)

    def test_corrupt_state_is_not_replaced(self):
        path = Path(self.tmp.name) / "state.json"
        path.write_text("not-json", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(semaphore.SemaphoreError):
            semaphore.snapshot()
        self.assertEqual(path.read_bytes(), before)

    def test_shape_valid_but_incomplete_hold_is_rejected_without_replacement(self):
        self.assertTrue(semaphore.acquire("soft", "incomplete", "fixture")[0])
        path = Path(self.tmp.name) / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["soft"][0]["renewed_at"]
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(semaphore.SemaphoreError):
            semaphore.snapshot()
        self.assertEqual(path.read_bytes(), before)

    def test_state_directory_and_files_are_private(self):
        self.assertTrue(semaphore.acquire("soft", "private", "sensitive note")[0])
        root = Path(self.tmp.name)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for name in ("mutex", "state.json", "log.jsonl"):
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)

    def test_reserved_manual_label_is_not_an_agent_hold(self):
        self.assertFalse(semaphore.acquire("soft", semaphore.MANUAL_LABEL, "x")[0])
        self.assertFalse(semaphore.release("soft", semaphore.MANUAL_LABEL)[0])

    def test_cleanup_success_releases_soft_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        cleanup = mock.Mock(return_value=(True, "clean"))

        self.assertEqual(
            semaphore.release_after_cleanup("goal", cleanup),
            (True, "cleanup verified and soft hold released"),
        )
        cleanup.assert_called_once_with()
        self.assertEqual(semaphore.snapshot()["soft"], [])

    def test_cleanup_success_releases_hard_hold(self):
        self.assertTrue(semaphore.acquire("hard", "goal", "timing")[0])

        self.assertTrue(
            semaphore.release_after_cleanup("goal", lambda: (True, "clean"))[0]
        )
        self.assertIsNone(semaphore.snapshot()["hard"])

    def test_cleanup_failure_preserves_matching_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])

        ok, detail = semaphore.release_after_cleanup(
            "goal", lambda: (False, "server survived")
        )

        self.assertFalse(ok)
        self.assertIn("hold retained", detail)
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_cleanup_exception_preserves_matching_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])

        ok, detail = semaphore.release_after_cleanup(
            "goal", mock.Mock(side_effect=RuntimeError("fixture"))
        )

        self.assertFalse(ok)
        self.assertIn("hold retained", detail)
        self.assertEqual(semaphore.snapshot()["soft"][0]["label"], "goal")

    def test_other_hold_blocks_cleanup_before_callback(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        self.assertTrue(semaphore.acquire("soft", "other", "build")[0])
        cleanup = mock.Mock(return_value=(True, "clean"))

        ok, detail = semaphore.release_after_cleanup("goal", cleanup)

        self.assertFalse(ok)
        self.assertIn("other", detail)
        cleanup.assert_not_called()
        self.assertEqual(
            [item["label"] for item in semaphore.snapshot()["soft"]],
            ["goal", "other"],
        )

    def test_goal_scoped_cleanup_releases_only_matching_hold(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])
        self.assertTrue(semaphore.acquire("soft", "other", "build")[0])
        cleanup = mock.Mock(return_value=(True, "goal worktree clear"))

        ok, detail = semaphore.release_after_cleanup(
            "goal",
            cleanup,
            goal_scoped=True,
        )

        self.assertTrue(ok, detail)
        cleanup.assert_called_once_with()
        self.assertEqual(
            [item["label"] for item in semaphore.snapshot()["soft"]],
            ["other"],
        )

    def test_cleanup_is_idempotent_after_matching_hold_was_released(self):
        self.assertEqual(
            semaphore.release_after_cleanup("goal", lambda: (True, "clean")),
            (True, "cleanup verified; no matching hold remained"),
        )
        self.assertEqual(semaphore.snapshot(), semaphore._empty_state())

    def test_successful_state_change_is_not_misreported_when_audit_log_fails(self):
        self.assertTrue(semaphore.acquire("soft", "goal", "proof")[0])

        with mock.patch("creme.semaphore._log", side_effect=OSError("fixture")):
            ok, detail = semaphore.release_after_cleanup(
                "goal", lambda: (True, "clean")
            )

        self.assertTrue(ok)
        self.assertIn("audit log write failed", detail)
        self.assertEqual(semaphore.snapshot()["soft"], [])


if __name__ == "__main__":
    unittest.main()


class ProcessAdapter(HeadroomAdapter):
    """Headroom fixture that can also answer process and worker questions."""

    def __init__(self, processes=None, workers=None, cwds=None, **kwargs):
        super().__init__(**kwargs)
        self.processes = processes
        self.workers = workers
        self.cwds = {} if cwds is None else cwds

    def process_snapshot(self):
        if self.processes is None:
            return self.result("process_snapshot", "UNAVAILABLE", "fixture unavailable")
        return self.result(
            "process_snapshot", "OK", "fixture", {"processes": list(self.processes)}
        )

    def lean_workers(self):
        if self.workers is None:
            return self.result("lean_workers", "UNAVAILABLE", "fixture unavailable")
        return self.result(
            "lean_workers", "OK", "fixture", {"workers": list(self.workers)}
        )

    def process_working_directories(self, pids):
        if self.cwds is None:
            return self.result(
                "process_working_directories", "UNAVAILABLE", "fixture unavailable"
            )
        wanted = sorted({int(pid) for pid in pids})
        answered = {
            str(pid): self.cwds[pid] for pid in wanted if pid in self.cwds
        }
        return self.result(
            "process_working_directories", "OK", "fixture",
            {"working_directories": answered, "requested": wanted},
        )


class QueueTest(unittest.TestCase):
    """B1/B5/B8: blocking admission, idle holds, and stranded holds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = ProcessAdapter(free_percent=80, total_gib=32)
        self.policy = {
            "task_memory_gib": 2,
            "heavy_workers": 2,
            "light_workers": 4,
            "physical_memory_gib": 32.0,
            "profile_status": "VALID",
        }
        patcher = mock.patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter_patcher = mock.patch("creme.semaphore.get_adapter", return_value=self.adapter)
        adapter_patcher.start()
        self.addCleanup(adapter_patcher.stop)
        policy_patcher = mock.patch(
            "creme.semaphore._runtime_admission_policy", return_value=self.policy
        )
        policy_patcher.start()
        self.addCleanup(policy_patcher.stop)

    # -- helpers ---------------------------------------------------------
    def wait_acquire(self, label, seconds=5, memory_gib=2, contention="tolerant", poll=0.05):
        return semaphore.adaptive_acquire(
            label,
            f"waiting {label}",
            memory_gib=memory_gib,
            contention=contention,
            adapter=self.adapter,
            policy=self.policy,
            wait_seconds=seconds,
            poll_seconds=poll,
        )

    def seed_waiter(self, label, *, pid, memory_gib=2, contention="tolerant", age=10.0):
        queue = semaphore._load_queue(self.root)[0]
        now = semaphore._now()
        queue["waiters"].append({
            "id": f"seed-{label}",
            "label": label,
            "pid": pid,
            "uid": os.getuid(),
            "contention": contention,
            "memory_gib": memory_gib,
            "enqueued_at": now - age,
            "heartbeat_at": now,
        })
        semaphore._save_queue(self.root, queue)

    def log_rows(self):
        path = self.root / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # -- positive --------------------------------------------------------
    def test_two_waiters_are_admitted_in_arrival_order(self):
        semaphore.adaptive_acquire(
            "holder", "blocking", memory_gib=4, contention="exclusive",
            adapter=self.adapter, policy=self.policy,
        )
        admitted: list[str] = []
        results: dict[str, tuple] = {}

        def run(label, delay):
            time.sleep(delay)
            outcome = self.wait_acquire(label, seconds=10, contention="exclusive")
            results[label] = outcome
            if outcome[0]:
                admitted.append(label)
                time.sleep(0.3)
                semaphore.adaptive_release(label)

        threads = [
            threading.Thread(target=run, args=("first", 0.0)),
            threading.Thread(target=run, args=("second", 0.4)),
        ]
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        semaphore.adaptive_release("holder")
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(admitted, ["first", "second"], results)
        self.assertTrue(all(outcome[0] for outcome in results.values()), results)

    def test_wait_returns_a_timeout_without_taking_a_hold(self):
        semaphore.adaptive_acquire(
            "holder", "blocking", memory_gib=4, contention="exclusive",
            adapter=self.adapter, policy=self.policy,
        )
        ok, detail = self.wait_acquire("late", seconds=1)
        self.assertFalse(ok)
        self.assertIn("WAIT_TIMEOUT", detail)
        state = semaphore.snapshot()
        self.assertEqual([hold["label"] for hold in state["soft"]], [])
        self.assertEqual(state["hard"]["label"], "holder")

    # -- required controls -----------------------------------------------
    def test_a_waiter_whose_process_is_gone_never_blocks_the_queue(self):
        dead = subprocess.Popen([os.sys.executable, "-c", "pass"])
        dead.wait()
        self.seed_waiter("ghost", pid=dead.pid, memory_gib=2, age=600.0)
        ok, detail = self.wait_acquire("live", seconds=5)
        self.assertTrue(ok, detail)
        remaining = semaphore._load_queue(self.root)[0]["waiters"]
        self.assertEqual(remaining, [])

    def test_a_large_waiter_refused_for_headroom_does_not_block_a_small_one(self):
        # 15 GiB charges 19 GiB, which fits the 24 GiB budget but not the
        # 25.6 GiB currently available once the usability reserve is kept.
        self.seed_waiter("large", pid=os.getpid(), memory_gib=15, age=600.0)
        ok, detail = self.wait_acquire("small", seconds=3, memory_gib=2)
        self.assertTrue(ok, detail)

    def test_a_manual_human_hold_refuses_immediately_instead_of_waiting(self):
        with mock.patch.object(self.adapter, "system", "Darwin"):
            self.assertTrue(semaphore.manual_acquire("human at the keyboard")[0])
        started = time.monotonic()
        ok, detail = self.wait_acquire("blocked", seconds=30)
        self.assertFalse(ok)
        self.assertIn("LIGHT_ONLY", detail)
        self.assertIn("manual human-session hold", detail)
        self.assertLess(time.monotonic() - started, 5)

    def test_waiting_never_admits_below_the_drain_floor(self):
        self.adapter.free_percent = 10
        started = time.monotonic()
        ok, detail = self.wait_acquire("drained", seconds=30)
        self.assertFalse(ok)
        self.assertIn("LIGHT_ONLY", detail)
        self.assertIn("10%", detail)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(semaphore.snapshot()["soft"], [])

    def test_a_charged_peak_above_the_budget_refuses_immediately(self):
        started = time.monotonic()
        ok, detail = self.wait_acquire("enormous", seconds=30, memory_gib=30)
        self.assertFalse(ok)
        self.assertIn("heavy-work budget", detail)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_ten_minute_wait_adds_two_log_rows(self):
        semaphore.adaptive_acquire(
            "holder", "blocking", memory_gib=4, contention="exclusive",
            adapter=self.adapter, policy=self.policy,
        )
        before = len(self.log_rows())
        self.wait_acquire("patient", seconds=1, poll=0.02)
        # The attribution audit writes its own row when the fixture host has
        # no process snapshot; the wait itself still adds exactly two.
        rows = [row for row in self.log_rows()[before:] if row["action"].startswith("wait")]
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual([row["action"] for row in rows], ["wait-enqueue", "wait-acquire"])

    def test_queue_entries_carry_no_free_form_note(self):
        self.seed_waiter("watched", pid=os.getpid(), age=1.0)
        entry = semaphore._load_queue(self.root)[0]["waiters"][0]
        self.assertEqual(set(entry), semaphore.WAITER_KEYS)
        self.assertNotIn("note", entry)

    def test_the_hold_state_file_still_validates_for_a_pre_update_reader(self):
        semaphore.adaptive_acquire(
            "holder", "blocking", memory_gib=4, contention="exclusive",
            adapter=self.adapter, policy=self.policy,
        )
        self.seed_waiter("queued", pid=os.getpid(), age=1.0)
        raw = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        # A pre-update launcher rejects any key it does not know.
        self.assertEqual(set(raw), {"schema_version", "hard", "soft"})
        self.assertEqual(semaphore._validate(raw)["hard"]["label"], "holder")
        self.assertTrue((self.root / "queue.json").exists())

    def test_a_malformed_queue_entry_is_reported_not_reset(self):
        self.seed_waiter("good", pid=os.getpid(), age=1.0)
        queue = json.loads((self.root / "queue.json").read_text(encoding="utf-8"))
        queue["waiters"].append({"id": "bad"})
        (self.root / "queue.json").write_text(json.dumps(queue), encoding="utf-8")
        loaded, notes = semaphore._load_queue(self.root)
        self.assertEqual([item["label"] for item in loaded["waiters"]], ["good"])
        self.assertTrue(any("malformed" in note for note in notes))

    def test_an_unreadable_queue_is_preserved_rather_than_discarded(self):
        (self.root / "queue.json").write_text("{not json", encoding="utf-8")
        loaded, notes = semaphore._load_queue(self.root)
        self.assertEqual(loaded["waiters"], [])
        self.assertTrue(any("preserved" in note for note in notes))
        self.assertTrue(list(self.root.glob("queue.corrupt.*.json")))

    # -- B5 and B8 -------------------------------------------------------
    def _hold_with_children(self, children):
        semaphore.adaptive_acquire(
            "gate", "fixture", memory_gib=2, contention="tolerant",
            adapter=self.adapter, policy=self.policy,
        )
        pid = semaphore.snapshot()["soft"][0]["pid"]
        self.adapter.processes = [
            {"pid": pid, "ppid": 1, "rss_kib": 1000, "command": "python3"},
            *children,
        ]
        return pid

    def test_a_hold_with_no_elaborating_child_reports_idle_hold(self):
        self._hold_with_children([])
        semaphore.refresh_signals(self.adapter)
        queue = semaphore._load_queue(self.root)[0]
        queue["activity"]["gate"] = semaphore._now() - (semaphore.IDLE_HOLD_SECONDS + 30)
        semaphore._save_queue(self.root, queue)
        self.assertIn("IDLE_HOLD", semaphore.status_text(self.adapter))

    def test_a_hold_whose_lean_child_is_merely_sleeping_still_reads_busy(self):
        pid = self._hold_with_children([
            {"pid": 999001, "ppid": 0, "rss_kib": 4000, "command": "lean"},
        ])
        self.adapter.processes[1]["ppid"] = pid
        semaphore.refresh_signals(self.adapter)
        queue = semaphore._load_queue(self.root)[0]
        queue["activity"]["gate"] = semaphore._now() - 10_000
        semaphore._save_queue(self.root, queue)
        text = semaphore.status_text(self.adapter)
        self.assertNotIn("IDLE_HOLD", text)

    def test_a_live_hold_with_a_live_pid_is_never_marked_stranded(self):
        self._hold_with_children([])
        self.assertNotIn("STRANDED", semaphore.status_text(self.adapter))

    def test_an_expired_hold_with_a_dead_pid_and_no_lean_work_is_stranded(self):
        self._hold_with_children([])
        path = self.root / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        dead = subprocess.Popen([os.sys.executable, "-c", "pass"])
        dead.wait()
        data["soft"][0]["pid"] = dead.pid
        data["soft"][0]["acquired_at"] = 1.0
        data["soft"][0]["renewed_at"] = 1.0
        path.write_text(json.dumps(data), encoding="utf-8")
        text = semaphore.status_text(self.adapter)
        self.assertIn("STRANDED", text)
        self.assertIn("reclaim --wind-down gate", text)

    def test_a_headroom_refusal_names_reclaimable_worker_memory(self):
        pid = self._hold_with_children([])
        self.adapter.workers = [{
            "pid": 999002, "ppid": pid, "rss_kib": 3 * 1024 * 1024,
            "cpu_seconds": 12.0, "command": "lean --worker",
            "ancestry": [{"pid": pid, "command": "python3"}],
        }]
        semaphore.refresh_signals(self.adapter)   # first observation
        semaphore.refresh_signals(self.adapter)   # second establishes idleness
        self.adapter.free_percent = 30
        ok, detail = semaphore.adaptive_acquire(
            "hungry", "big", memory_gib=8, contention="sensitive",
            adapter=self.adapter, policy=self.policy,
        )
        self.assertFalse(ok)
        self.assertIn("idle", detail)
        self.assertIn("goal gate", detail)


class QueueVisibilityTest(QueueTest):
    """B1/B3: a waiter can see why it waits, and every enqueue has an outcome."""

    def hold(self, label, memory_gib=4, contention="exclusive", note="blocking"):
        ok, detail = semaphore.adaptive_acquire(
            label, note, memory_gib=memory_gib, contention=contention,
            adapter=self.adapter, policy=self.policy,
        )
        self.assertTrue(ok, detail)

    # -- B1(a): status shows the live verdict and the arithmetic -----------
    def test_status_prints_the_would_be_verdict_and_arithmetic_for_a_waiter(self):
        self.seed_waiter("large", pid=os.getpid(), memory_gib=15, age=600.0)
        text = semaphore.status_text(self.adapter)
        self.assertIn("would: LIGHT_ONLY", text)
        self.assertIn("usability reserve", text)
        self.assertIn("fit: estimate 15 GiB -> charged 19 GiB", text)
        self.assertIn("does not fit now", text)

    def test_the_printed_verdict_is_the_one_the_queue_would_compute(self):
        self.hold("holder")
        self.seed_waiter("queued", pid=os.getpid(), memory_gib=2, age=5.0)
        state = semaphore.snapshot()
        expected = semaphore._admission_decision(
            state, "adaptive", "queued", 2, "tolerant",
            self.adapter, self.policy, self.adapter.memory_headroom(),
        )
        text = semaphore.status_text(self.adapter)
        self.assertIn(f"would: {expected.verdict} — {expected.detail}", text)

    def test_a_waiter_that_fits_is_shown_as_admitted(self):
        self.seed_waiter("small", pid=os.getpid(), memory_gib=2, age=5.0)
        text = semaphore.status_text(self.adapter)
        self.assertIn("would: ADMITTED_", text)
        self.assertIn("fits now", text)

    # -- B1(a) control: privacy -------------------------------------------
    def test_would_be_text_never_repeats_a_hold_note(self):
        self.hold("holder", note="secret-campaign-detail")
        self.seed_waiter("queued", pid=os.getpid(), memory_gib=2, age=5.0)
        text = semaphore.status_text(self.adapter)
        self.assertIn("would: ", text)
        after_waiting = text.split("waiting (W=")[1]
        self.assertNotIn("secret-campaign-detail", after_waiting)

    # -- B1(b): the timeout reports the verdict that dominated -------------
    def test_timeout_reports_the_dominant_verdict_not_the_final_pass(self):
        self.adapter.free_percent = 40           # 12.8 GiB available of 32
        passes = {"n": 0}
        clock = {"t": 1_000.0}

        def scripted_sleep(seconds):
            # A deterministic clock: three passes refused for the usability
            # reserve, then a hold appears and only the closing passes see it.
            clock["t"] += seconds
            passes["n"] += 1
            if passes["n"] == 3:
                self.hold("holder", memory_gib=2, contention="exclusive")

        with mock.patch.object(semaphore, "_now", lambda: clock["t"]):
            ok, detail = semaphore._waiting_admit(
                "large", "waiting", 600,
                memory_gib=12, contention="sensitive",
                adapter=self.adapter, policy=self.policy,
                wait_seconds=1, poll_seconds=0.25, sleep=scripted_sleep,
            )
        self.assertFalse(ok)
        self.assertIn("WAIT_TIMEOUT", detail)
        self.assertIn("dominant verdict LIGHT_ONLY over 3 pass(es)", detail)
        self.assertIn("LIGHT_ONLY 3", detail)
        self.assertIn("DEFER_HEAVY 2", detail)
        self.assertIn("last verdict DEFER_HEAVY", detail)
        self.assertIn("tightest headroom margin", detail)
        row = [r for r in self.log_rows() if r["action"] == "wait-acquire"][-1]
        self.assertIn("dominant verdict LIGHT_ONLY", row["detail"])

    def test_the_dominant_verdict_counts_both_passes(self):
        tally = {
            "LIGHT_ONLY": {"passes": 190, "seconds": 570.0},
            "DEFER_HEAVY": {"passes": 10, "seconds": 30.0},
        }
        summary = semaphore._wait_summary(tally, (19.0, 18.96))
        self.assertIn("dominant verdict LIGHT_ONLY over 190 pass(es)", summary)
        self.assertIn("LIGHT_ONLY 190", summary)
        self.assertIn("DEFER_HEAVY 10", summary)
        self.assertIn("needed 19.0 GiB; most available 18.96 GiB", summary)

    # -- B1(c): the admitted waiter names whom it passed -------------------
    def test_an_admitted_waiter_names_the_older_waiters_it_passed(self):
        self.adapter.total_gib = 24
        self.policy = {**self.policy, "physical_memory_gib": 24.0}
        self.seed_waiter("large", pid=os.getpid(), memory_gib=15, age=600.0)
        ok, detail = self.wait_acquire("small", seconds=3, memory_gib=2)
        self.assertTrue(ok, detail)
        self.assertIn("passed 1 older waiter(s): large(LIGHT_ONLY)", detail)
        row = [r for r in self.log_rows() if r["action"] == "wait-acquire"][-1]
        self.assertIn("passed 1 older waiter(s): large(LIGHT_ONLY)", row["detail"])

    # -- B1(d): the pre-enqueue fit line -----------------------------------
    def test_a_wait_announces_its_fit_arithmetic_before_enqueueing(self):
        announced: list[str] = []
        self.hold("holder")
        semaphore._waiting_admit(
            "queued", "waiting", 600, memory_gib=2, contention="tolerant",
            adapter=self.adapter, policy=self.policy,
            wait_seconds=1, poll_seconds=0.01, announce=announced.append,
        )
        self.assertTrue(announced)
        self.assertIn("fit: estimate 2 GiB -> charged 3 GiB", announced[0])
        self.assertIn("fits now", announced[0])

    def test_an_explicit_estimate_above_the_default_is_called_out(self):
        announced: list[str] = []
        semaphore._waiting_admit(
            "queued", "waiting", 600, memory_gib=12, contention="tolerant",
            adapter=self.adapter, policy=self.policy,
            wait_seconds=1, poll_seconds=0.01, announce=announced.append,
        )
        joined = "\n".join(announced)
        self.assertIn("exceeds this host's default estimate of 2 GiB", joined)

    def test_an_unschedulable_request_is_told_what_would_fit(self):
        announced: list[str] = []
        self.adapter.free_percent = 50           # 16 GiB available of 32
        semaphore._waiting_admit(
            "queued", "waiting", 600, memory_gib=12, contention="sensitive",
            adapter=self.adapter, policy=self.policy,
            wait_seconds=1, poll_seconds=0.01, announce=announced.append,
        )
        joined = "\n".join(announced)
        self.assertIn("does not fit now", joined)
        self.assertIn("an estimate of at most", joined)

    def test_no_fit_line_is_printed_without_wait(self):
        announced: list[str] = []
        semaphore.adaptive_acquire(
            "direct", "no wait", memory_gib=2, contention="tolerant",
            adapter=self.adapter, policy=self.policy, announce=announced.append,
        )
        self.assertEqual(announced, [])

    def test_status_shows_the_holder_class_and_age(self):
        self.hold("holder", memory_gib=4, contention="exclusive")
        text = semaphore.status_text(self.adapter)
        self.assertIn("contention=exclusive held=", text)

    def test_a_timeout_names_the_holder_class_and_age(self):
        self.hold("holder", memory_gib=4, contention="exclusive")
        ok, detail = self.wait_acquire("queued", seconds=1, poll=0.02)
        self.assertFalse(ok)
        self.assertIn("holder at timeout: holder contention=exclusive held=", detail)

    def test_the_timeout_holder_note_carries_no_hold_note(self):
        self.hold("holder", memory_gib=4, contention="exclusive", note="secret-gate-name")
        _ok, detail = self.wait_acquire("queued", seconds=1, poll=0.02)
        self.assertNotIn("secret-gate-name", detail)

    # -- B3: every enqueue has an outcome ----------------------------------
    def test_a_cancelled_wait_writes_exactly_one_outcome_row(self):
        self.hold("holder")

        def cancel(_seconds):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            semaphore._waiting_admit(
                "cancelled", "waiting", 600, memory_gib=2, contention="tolerant",
                adapter=self.adapter, policy=self.policy,
                wait_seconds=600, poll_seconds=0.01, sleep=cancel,
            )
        rows = [r for r in self.log_rows() if r["label"] == "cancelled"]
        self.assertEqual([r["action"] for r in rows], ["wait-enqueue", "wait-acquire"])
        self.assertIn("WAIT_CANCELLED", rows[1]["detail"])
        self.assertIn("dominant verdict DEFER_HEAVY", rows[1]["detail"])
        self.assertEqual(semaphore._load_queue(self.root)[0]["waiters"], [])

    def test_a_terminated_wait_writes_its_row_from_a_real_signal(self):
        self.hold("holder")
        script = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from creme import semaphore\n"
            "from test_semaphore import ProcessAdapter\n"
            "adapter = ProcessAdapter(free_percent=80, total_gib=32)\n"
            "policy = %r\n"
            "semaphore._waiting_admit('terminated', 'waiting', 600, memory_gib=2,\n"
            "    contention='tolerant', adapter=adapter, policy=policy,\n"
            "    wait_seconds=600, poll_seconds=0.05)\n"
        ) % (str(Path(__file__).resolve().parents[2]), self.policy)
        env = {
            **os.environ,
            "CREME_SEMAPHORE_DIR": str(self.root),
            "PYTHONPATH": str(Path(__file__).resolve().parent),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        child = subprocess.Popen([os.sys.executable, "-c", script], env=env)
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(r["label"] == "terminated" for r in self.log_rows()):
                break
            time.sleep(0.05)
        child.terminate()
        child.wait(timeout=10)
        rows = [r for r in self.log_rows() if r["label"] == "terminated"]
        self.assertEqual([r["action"] for r in rows], ["wait-enqueue", "wait-acquire"])
        self.assertIn("WAIT_CANCELLED", rows[1]["detail"])
        self.assertEqual(semaphore._load_queue(self.root)[0]["waiters"], [])
        self.assertEqual(-child.returncode, 15)

    def test_a_killed_waiter_is_dropped_with_one_row(self):
        dead = subprocess.Popen([os.sys.executable, "-c", "pass"])
        dead.wait()
        self.seed_waiter("ghost", pid=dead.pid, memory_gib=2, age=600.0)
        ok, _ = self.wait_acquire("live", seconds=5)
        self.assertTrue(ok)
        dropped = [r for r in self.log_rows() if r["action"] == "wait-dropped"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["label"], "ghost")
        self.assertIn("WAIT_DROPPED", dropped[0]["detail"])

    def test_a_dropped_waiter_is_logged_once_across_repeated_status_reads(self):
        dead = subprocess.Popen([os.sys.executable, "-c", "pass"])
        dead.wait()
        self.seed_waiter("ghost", pid=dead.pid, memory_gib=2, age=600.0)
        semaphore.status_text(self.adapter)
        semaphore.status_text(self.adapter)
        dropped = [r for r in self.log_rows() if r["action"] == "wait-dropped"]
        self.assertEqual(len(dropped), 1)

    def test_a_normal_admission_still_adds_no_extra_row(self):
        self.wait_acquire("patient", seconds=1, poll=0.02)
        rows = [r for r in self.log_rows() if r["label"] == "patient"]
        self.assertEqual([r["action"] for r in rows], ["wait-enqueue", "wait-acquire"])


class HoldAttributionTest(QueueTest):
    """B2: a hold is idle only when nothing attributable to it is elaborating."""

    def setUp(self):
        super().setUp()
        self.scope = Path(self.tmp.name) / "blanc" / ".worktrees" / "gate"
        self.scope.mkdir(parents=True)
        self.other = Path(self.tmp.name) / "blanc" / ".worktrees" / "peer"
        self.other.mkdir(parents=True)
        self.scopes = {"gate": (self.scope,), "peer": (self.other,)}
        patcher = mock.patch(
            "creme.semaphore._goal_scope_roots",
            side_effect=lambda label, adapter: self.scopes.get(label, ()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def acquire_gate(self):
        semaphore.adaptive_acquire(
            "gate", "fixture", memory_gib=2, contention="tolerant",
            adapter=self.adapter, policy=self.policy,
        )
        return semaphore.snapshot()["soft"][0]["pid"]

    def make_idle(self, label="gate"):
        semaphore.refresh_signals(self.adapter)
        queue = semaphore._load_queue(self.root)[0]
        queue["activity"][label] = semaphore._now() - (semaphore.IDLE_HOLD_SECONDS + 30)
        semaphore._save_queue(self.root, queue)

    def test_a_sibling_lean_working_in_the_goal_worktree_reads_busy(self):
        self.acquire_gate()
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {999100: str(self.scope / "Blanc")}
        self.make_idle()
        text = semaphore.status_text(self.adapter)
        self.assertNotIn("IDLE_HOLD", text)

    def test_a_lean_in_another_goals_worktree_leaves_this_hold_idle(self):
        self.acquire_gate()
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {999100: str(self.other / "Blanc")}
        self.make_idle()
        self.assertIn("IDLE_HOLD", semaphore.status_text(self.adapter))

    def test_the_idle_hold_line_names_what_it_looked_for(self):
        self.acquire_gate()
        self.adapter.processes = []
        self.make_idle()
        text = semaphore.status_text(self.adapter)
        self.assertIn("no lake or lean process among this hold's children", text)
        self.assertIn(str(self.scope), text)

    def test_an_unavailable_cwd_sample_is_uninspectable_never_idle(self):
        self.acquire_gate()
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = None
        self.make_idle()
        text = semaphore.status_text(self.adapter)
        self.assertNotIn("IDLE_HOLD", text)
        self.assertIn("ATTRIBUTION_UNAVAILABLE", text)

    def test_an_unanswered_lean_pid_is_uninspectable_never_idle(self):
        self.acquire_gate()
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {}
        self.make_idle()
        text = semaphore.status_text(self.adapter)
        self.assertNotIn("IDLE_HOLD", text)
        self.assertIn("ATTRIBUTION_UNAVAILABLE", text)

    def test_a_sleeping_lean_child_of_the_holder_still_reads_busy(self):
        pid = self.acquire_gate()
        self.adapter.processes = [
            {"pid": pid, "ppid": 1, "rss_kib": 1000, "command": "python3"},
            {"pid": 999101, "ppid": pid, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {999101: "/elsewhere"}
        self.make_idle()
        self.assertNotIn("IDLE_HOLD", semaphore.status_text(self.adapter))

    def test_a_goal_without_a_worktree_is_still_reported_idle(self):
        semaphore.adaptive_acquire(
            "nowhere", "fixture", memory_gib=2, contention="tolerant",
            adapter=self.adapter, policy=self.policy,
        )
        self.adapter.processes = []
        self.make_idle("nowhere")
        text = semaphore.status_text(self.adapter)
        self.assertIn("IDLE_HOLD", text)
        self.assertIn("has no Jaune/Blanc worktree", text)

    def test_an_unreadable_scope_is_uninspectable_never_idle(self):
        self.scopes = {"gate": None}
        self.acquire_gate()
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {999100: "/elsewhere"}
        self.make_idle()
        text = semaphore.status_text(self.adapter)
        self.assertNotIn("IDLE_HOLD", text)
        self.assertIn("ATTRIBUTION_UNAVAILABLE", text)

    def test_a_manual_human_hold_is_never_attributed_or_flagged(self):
        semaphore.manual_acquire  # documented; macOS-only, so seed state directly
        path = self.root / "state.json"
        state = semaphore._empty_state()
        state["soft"] = [
            semaphore._hold(semaphore.MANUAL_LABEL, "human", 300, manual=True)
        ]
        path.write_text(json.dumps(state), encoding="utf-8")
        self.adapter.processes = []
        text = semaphore.status_text(self.adapter)
        self.assertNotIn("IDLE_HOLD", text)
        self.assertNotIn("ATTRIBUTION_UNAVAILABLE", text)

    def test_stranded_uses_the_same_attribution(self):
        self.acquire_gate()
        path = self.root / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        dead = subprocess.Popen([os.sys.executable, "-c", "pass"])
        dead.wait()
        data["soft"][0]["pid"] = dead.pid
        data["soft"][0]["acquired_at"] = 1.0
        data["soft"][0]["renewed_at"] = 1.0
        path.write_text(json.dumps(data), encoding="utf-8")
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {999100: str(self.scope / "Blanc")}
        self.assertNotIn("STRANDED", semaphore.status_text(self.adapter))
        self.adapter.cwds = {999100: str(self.other / "Blanc")}
        self.assertIn("STRANDED", semaphore.status_text(self.adapter))


class MasterLeaseTest(unittest.TestCase):
    """One master at a time: live, lapsed, stranded, and take-over."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = ProcessAdapter(free_percent=80, total_gib=32)
        self.policy = {
            "task_memory_gib": 2,
            "heavy_workers": 2,
            "light_workers": 4,
            "physical_memory_gib": 32.0,
            "profile_status": "VALID",
        }
        for patcher in (
            mock.patch.dict(os.environ, {
                "CREME_SEMAPHORE_DIR": self.tmp.name,
                "CREME_MASTER_SESSION_ID": "",
                "CREME_MASTER_LIVENESS_SOCKET": "",
                "CODEX_SESSION_ID": "",
                "CODEX_THREAD_ID": "",
                "CODEX_APP_TOOLS_PIPE_PATH": "",
            }, clear=False),
            mock.patch("creme.semaphore.get_adapter", return_value=self.adapter),
            mock.patch("creme.semaphore._runtime_admission_policy", return_value=self.policy),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.as_client(os.getpid(), "claude")

    def as_client(self, pid, family):
        if getattr(self, "_client_patch", None):
            self._client_patch.stop()
        value = (pid, family, f"client {family} pid {pid}") if pid else (None, None, "no client")
        self._client_patch = mock.patch("creme.semaphore._client_process", return_value=value)
        self._client_patch.start()
        self.addCleanup(self._client_patch.stop)

    def expire(self):
        path = self.root / "master.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["lease"]["acquired_at"] = 1.0
        data["lease"]["renewed_at"] = 1.0
        data["lease"]["direct_activity_at"] = 1.0
        path.write_text(json.dumps(data), encoding="utf-8")

    def dead_pid(self):
        gone = subprocess.Popen([os.sys.executable, "-c", "pass"])
        gone.wait()
        return gone.pid

    def log_actions(self):
        rows = (self.root / "log.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(row)["action"] for row in rows]

    def test_acquire_then_a_second_acquire_is_refused_and_names_the_holder(self):
        ok, detail = semaphore.master_acquire("claude", "first master")
        self.assertTrue(ok, detail)
        self.as_client(os.getpid() + 100000, "codex")
        ok, detail = semaphore.master_acquire("codex", "second master")
        self.assertFalse(ok)
        self.assertIn("live", detail)
        self.assertIn("client claude", detail)
        self.assertIn("master-release", detail)
        self.assertIn("never edit master.json", detail)
        self.assertEqual(semaphore.master_snapshot()["lease"]["client"], "claude")

    def test_the_holder_renews_and_releases(self):
        semaphore.master_acquire("claude", "master")
        before = semaphore.master_snapshot()["lease"]["renewed_at"]
        time.sleep(0.01)
        ok, detail = semaphore.master_renew(900)
        self.assertTrue(ok, detail)
        self.assertIn("holder verified", detail)
        lease = semaphore.master_snapshot()["lease"]
        self.assertGreater(lease["renewed_at"], before)
        self.assertEqual(lease["lease_seconds"], 900)
        ok, detail = semaphore.master_release()
        self.assertTrue(ok, detail)
        self.assertIn("by its holder", detail)
        self.assertIsNone(semaphore.master_snapshot()["lease"])
        self.assertIn("master: none", semaphore.status_text(self.adapter))
        self.assertEqual(
            self.log_actions(), ["master-acquire", "master-renew", "master-release"]
        )

    def test_another_client_cannot_renew_or_release_a_live_lease(self):
        semaphore.master_acquire("claude", "master")
        self.as_client(os.getpid() + 100000, "codex")
        ok, detail = semaphore.master_renew()
        self.assertFalse(ok)
        self.assertIn("belongs to client claude", detail)
        ok, detail = semaphore.master_release()
        self.assertFalse(ok)
        self.assertIn("--force", detail)
        self.assertIsNotNone(semaphore.master_snapshot()["lease"])
        ok, detail = semaphore.master_release(force=True, reason="hung session")
        self.assertTrue(ok, detail)
        self.assertIn("by force", detail)
        rows = (self.root / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn("hung session", rows)

    def test_a_lapsed_lease_is_replaced_only_by_take_over(self):
        semaphore.master_acquire("claude", "idle tab")
        self.expire()                      # client pid is this test process: alive
        text = semaphore.status_text(self.adapter)
        self.assertIn("(lapsed)", text)
        self.assertIn("LAPSED", text)
        self.assertIn("--take-over", text)
        self.as_client(os.getpid() + 100000, "codex")
        ok, detail = semaphore.master_acquire("codex", "new master")
        self.assertFalse(ok)
        self.assertIn("LAPSED", detail)
        ok, detail = semaphore.master_acquire("codex", "new master", take_over=True)
        self.assertTrue(ok, detail)
        self.assertIn("taken over", detail)
        self.assertEqual(semaphore.master_snapshot()["lease"]["client"], "codex")
        self.assertIn("master-take-over", self.log_actions())

    def test_a_stranded_lease_names_the_take_over_command(self):
        self.as_client(self.dead_pid(), "claude")
        semaphore.master_acquire("claude", "crashed master")
        self.expire()
        text = semaphore.status_text(self.adapter)
        self.assertIn("(stranded)", text)
        self.assertIn("STRANDED", text)
        self.assertIn("master-acquire --take-over", text)
        self.as_client(os.getpid(), "codex")
        ok, _ = semaphore.master_acquire("codex", "successor")
        self.assertFalse(ok)
        ok, detail = semaphore.master_acquire("codex", "successor", take_over=True)
        self.assertTrue(ok, detail)
        self.assertIn("stranded", detail)

    def test_a_live_lease_is_never_taken_over(self):
        semaphore.master_acquire("claude", "master")
        self.as_client(os.getpid() + 100000, "codex")
        ok, detail = semaphore.master_acquire("codex", "impatient", take_over=True)
        self.assertFalse(ok)
        self.assertIn("live", detail)
        self.assertEqual(semaphore.master_snapshot()["lease"]["client"], "claude")

    def test_an_unknown_client_process_is_stranded_once_lapsed(self):
        self.as_client(None, None)
        ok, detail = semaphore.master_acquire(None, "sandboxed")
        self.assertFalse(ok)
        self.assertIn("--client", detail)
        ok, detail = semaphore.master_acquire("human", "sandboxed")
        self.assertTrue(ok, detail)
        self.assertIsNone(semaphore.master_snapshot()["lease"]["client_pid"])
        self.assertIn("(live)", semaphore.status_text(self.adapter))
        self.expire()
        self.assertIn("(stranded)", semaphore.status_text(self.adapter))

    def test_stable_session_identity_replaces_an_unavailable_process_snapshot(self):
        self.as_client(None, None)
        raw = "codex-session-high-entropy-value"
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": raw}, clear=False):
            ok, detail = semaphore.master_acquire("codex", "sandboxed master")
            self.assertTrue(ok, detail)
            self.assertIn("identity", detail)
            lease = semaphore.master_snapshot()["lease"]
            self.assertIsNone(lease["client_pid"])
            self.assertIsNotNone(lease["session_digest"])
            serialized = (self.root / "master.json").read_text(encoding="utf-8")
            self.assertNotIn(raw, serialized)
            self.assertNotIn(raw, semaphore.status_text(self.adapter))
            self.assertNotIn(raw, (self.root / "log.jsonl").read_text(encoding="utf-8"))
            ok, detail = semaphore.master_renew()
            self.assertTrue(ok, detail)
            self.assertIn("holder verified", detail)
            with mock.patch("creme.semaphore.subprocess.Popen") as popen:
                popen.return_value.pid = 424242
                ok, detail = semaphore.master_heartbeat_detached(1500)
            self.assertTrue(ok, detail)
            command = popen.call_args.args[0]
            lease_id = command[command.index("--heartbeat-lease-id") + 1]
            self.assertEqual(
                lease_id,
                semaphore.master_snapshot()["lease"]["lease_id"],
            )
            ok, detail = semaphore.master_heartbeat(
                1500,
                expected_lease_id=lease_id,
                sleep=lambda _seconds: None,
                max_beats=1,
            )
            self.assertTrue(ok, detail)
            self.assertIn("beat limit reached", detail)
            ok, detail = semaphore.master_release()
            self.assertTrue(ok, detail)
            self.assertIn("by its holder", detail)

    def test_a_different_session_identity_cannot_impersonate_the_same_client(self):
        # Desktop tasks can share a visible app ancestor; the per-task digest
        # therefore takes precedence even when process discovery returns the
        # same client pid for both invocations.
        self.as_client(os.getpid(), "codex")
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False):
            semaphore.master_acquire("codex", "first")
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-b"}, clear=False):
            ok, detail = semaphore.master_renew()
            self.assertFalse(ok)
            self.assertIn("belongs to client codex", detail)
            ok, detail = semaphore.master_release()
            self.assertFalse(ok)
            self.assertIn("--force", detail)
            ok, detail = semaphore.master_acquire("codex", "second", take_over=True)
            self.assertFalse(ok)
            self.assertIn("live", detail)

    def test_codex_app_pipe_is_never_task_liveness(self):
        raw_pipe = str(self.root / "app-global-codex-tools.sock")
        listener = semaphore.socket.socket(semaphore.socket.AF_UNIX, semaphore.socket.SOCK_STREAM)
        listener.bind(raw_pipe)
        listener.listen()
        try:
            with mock.patch.dict(os.environ, {
                "CREME_MASTER_SESSION_ID": "",
                "CREME_MASTER_LIVENESS_SOCKET": "",
                "CODEX_SESSION_ID": "session-a",
                "CODEX_THREAD_ID": "",
                "CODEX_APP_TOOLS_PIPE_PATH": raw_pipe,
            }, clear=False):
                session = semaphore._client_session("codex")
        finally:
            listener.close()
        self.assertIsNotNone(session.digest)
        self.assertIsNone(session.liveness_digest)
        self.assertIsNone(session.liveness_socket)
        self.assertNotIn(raw_pipe, session.detail)

    def test_codex_without_identity_never_trusts_a_shared_app_pid(self):
        self.as_client(os.getpid(), "codex")
        ok, detail = semaphore.master_acquire("codex", "no identity")
        self.assertTrue(ok, detail)
        lease = semaphore.master_snapshot()["lease"]
        self.assertIsNone(lease["client_pid"])

        # Another task has the same observable app pid. Direct holder actions
        # must fail closed because that pid is not task identity.
        ok, detail = semaphore.master_renew()
        self.assertFalse(ok)
        self.assertIn("belongs to client codex", detail)
        ok, detail = semaphore.master_release()
        self.assertFalse(ok)
        self.assertIn("--force", detail)

        # The acquisition-bound helper still gets the documented finite grace.
        for _ in range(semaphore.MASTER_UNVERIFIED_HEARTBEAT_RENEWALS):
            ok, detail = semaphore.master_renew(
                expected_lease_id=lease["lease_id"], heartbeat=True
            )
            self.assertTrue(ok, detail)
        ok, detail = semaphore.master_renew(
            expected_lease_id=lease["lease_id"], heartbeat=True
        )
        self.assertFalse(ok)
        self.assertIn("budget exhausted", detail)
        semaphore.master_release(force=True, reason="test cleanup")

        # A cosmetic --client label cannot turn the discovered Codex app
        # ancestor back into task-scoped process evidence.
        ok, detail = semaphore.master_acquire("custom-client", "renamed codex")
        self.assertTrue(ok, detail)
        self.assertIsNone(semaphore.master_snapshot()["lease"]["client_pid"])
        semaphore.master_release(force=True, reason="test cleanup")

    def test_cosmetic_label_preserves_discovered_codex_compatibility_identity(self):
        self.as_client(os.getpid(), "codex")
        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "session-a"}, clear=False):
            ok, detail = semaphore.master_acquire("custom-client", "renamed codex")
            self.assertTrue(ok, detail)
            lease = semaphore.master_snapshot()["lease"]
            self.assertEqual(lease["client"], "custom-client")
            self.assertIsNone(lease["client_pid"])
            self.assertIsNotNone(lease["session_digest"])
            self.assertIn("Codex compatibility alias identity", detail)
            ok, detail = semaphore.master_renew()
            self.assertTrue(ok, detail)
            self.assertIn("holder verified", detail)
            with mock.patch("creme.semaphore.subprocess.Popen") as popen:
                popen.return_value.pid = 424242
                ok, detail = semaphore.master_heartbeat_detached(1500)
            self.assertTrue(ok, detail)
            command = popen.call_args.args[0]
            lease_id = command[command.index("--heartbeat-lease-id") + 1]
            self.assertEqual(
                lease_id,
                semaphore.master_snapshot()["lease"]["lease_id"],
            )
            ok, detail = semaphore.master_heartbeat(
                1500,
                expected_lease_id=lease_id,
                sleep=lambda _seconds: None,
                max_beats=1,
            )
            self.assertTrue(ok, detail)
            self.assertIn("beat limit reached", detail)

    def test_identity_holder_recovers_after_sleep_without_process_or_socket(self):
        self.as_client(None, None)
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False):
            ok, detail = semaphore.master_acquire("codex", "sleeping master")
            self.assertTrue(ok, detail)
            self.expire()
            self.assertIn("(stranded)", semaphore.status_text(self.adapter))
            ok, detail = semaphore.master_renew()
            self.assertTrue(ok, detail)
            self.assertIn("holder verified", detail)
            ok, detail = semaphore.master_release()
            self.assertTrue(ok, detail)

    def test_session_listener_loss_stops_an_orphaned_heartbeat(self):
        self.as_client(None, None)
        session_digest = semaphore._digest_session_value(
            semaphore.MASTER_SESSION_DIGEST_DOMAIN, "session-a"
        )
        liveness_digest = semaphore._digest_session_value(
            semaphore.MASTER_LIVENESS_DIGEST_DOMAIN, "/tmp/session-a.sock"
        )
        session = semaphore._ClientSession(
            session_digest, liveness_digest, "/tmp/session-a.sock", "test session"
        )
        with (
            mock.patch("creme.semaphore._client_session", return_value=session),
            mock.patch("creme.semaphore._session_socket_live", side_effect=[True, False, False, False]),
        ):
            ok, detail = semaphore.master_acquire("codex", "master")
            self.assertTrue(ok, detail)
            self.assertNotIn(
                session.liveness_socket,
                (self.root / "master.json").read_text(encoding="utf-8"),
            )
            self.assertNotIn(session.liveness_socket, semaphore.status_text(self.adapter))
            self.assertNotIn(
                session.liveness_socket,
                (self.root / "log.jsonl").read_text(encoding="utf-8"),
            )
            before = semaphore.master_snapshot()["lease"]["renewed_at"]
            ok, detail = semaphore.master_heartbeat(
                5, sleep=lambda _seconds: None, max_beats=1
            )
        self.assertTrue(ok, detail)
        self.assertIn("session listener is gone", detail)
        self.assertEqual(semaphore.master_snapshot()["lease"]["renewed_at"], before)
        self.expire()
        self.assertIn("(stranded)", semaphore.status_text(self.adapter))

    def test_session_listener_recovers_an_expired_lease_within_one_wake_slice(self):
        self.as_client(None, None)
        session_digest = semaphore._digest_session_value(
            semaphore.MASTER_SESSION_DIGEST_DOMAIN, "session-a"
        )
        liveness_digest = semaphore._digest_session_value(
            semaphore.MASTER_LIVENESS_DIGEST_DOMAIN, "/tmp/session-a.sock"
        )
        session = semaphore._ClientSession(
            session_digest, liveness_digest, "/tmp/session-a.sock", "test session"
        )
        wall = [1_000.0]
        naps = []

        def nap(seconds):
            naps.append(seconds)
            wall[0] += 10_000 if len(naps) == 1 else seconds

        with (
            mock.patch("creme.semaphore._client_session", return_value=session),
            mock.patch("creme.semaphore._session_socket_live", return_value=True),
            mock.patch("creme.semaphore._now", side_effect=lambda: wall[0]),
        ):
            ok, detail = semaphore.master_acquire("codex", "master")
            self.assertTrue(ok, detail)
            ok, detail = semaphore.master_heartbeat(
                1500, sleep=nap, max_beats=2, clock=lambda: wall[0]
            )
        self.assertTrue(ok, detail)
        self.assertEqual(naps, [60])
        lease = semaphore.master_snapshot()["lease"]
        self.assertEqual(lease["renewed_at"], 11_000.0)
        self.assertEqual(lease["direct_activity_at"], 1_000.0)

    def test_unverified_heartbeat_budget_is_reset_only_by_direct_holder_activity(self):
        self.as_client(None, None)
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False):
            semaphore.master_acquire("codex", "master")
            lease = semaphore.master_snapshot()["lease"]
            for _ in range(semaphore.MASTER_UNVERIFIED_HEARTBEAT_RENEWALS):
                ok, detail = semaphore.master_renew(
                    as_session_digest=lease["session_digest"],
                    expected_lease_id=lease["lease_id"],
                    heartbeat=True,
                )
                self.assertTrue(ok, detail)
            ok, detail = semaphore.master_renew(
                as_session_digest=lease["session_digest"],
                expected_lease_id=lease["lease_id"],
                heartbeat=True,
            )
            self.assertFalse(ok)
            self.assertIn("budget exhausted", detail)
            ok, detail = semaphore.master_renew()
            self.assertTrue(ok, detail)
            lease = semaphore.master_snapshot()["lease"]
            self.assertEqual(lease["heartbeat_renewals"], 0)
            self.assertEqual(lease["direct_activity_at"], lease["renewed_at"])

    def test_standard_fallback_expires_within_4800_seconds_of_direct_activity(self):
        self.as_client(None, None)
        wall = [1_000.0]
        with (
            mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False),
            mock.patch("creme.semaphore._now", side_effect=lambda: wall[0]),
        ):
            ok, detail = semaphore.master_acquire("codex", "bounded master")
            self.assertTrue(ok, detail)
            wall[0] = 1_001.0
            ok, detail = semaphore.master_renew()
            self.assertTrue(ok, detail)
            direct = wall[0]
            lease = semaphore.master_snapshot()["lease"]

            # Place each permitted helper beat as late as a 1,500-second
            # schedule allows after the direct renewal.
            for elapsed in (1_499.9, 2_999.9):
                wall[0] = direct + elapsed
                ok, detail = semaphore.master_renew(
                    as_session_digest=lease["session_digest"],
                    expected_lease_id=lease["lease_id"],
                    heartbeat=True,
                )
                self.assertTrue(ok, detail)
            wall[0] = direct + 4_499.9
            ok, detail = semaphore.master_renew(
                as_session_digest=lease["session_digest"],
                expected_lease_id=lease["lease_id"],
                heartbeat=True,
            )
            self.assertFalse(ok)
            self.assertIn("direct-activity deadline passed", detail)

            final = semaphore.master_snapshot()["lease"]
            expires_after = final["renewed_at"] + final["lease_seconds"] - direct
            self.assertLessEqual(expires_after, 4_800)
            self.assertEqual(final["direct_activity_at"], direct)

    def test_unverified_heartbeat_does_not_renew_after_a_10000_second_wake(self):
        self.as_client(None, None)
        wall = [1_000.0]
        naps = []

        def nap(seconds):
            naps.append(seconds)
            wall[0] += 10_000.0 if len(naps) == 1 else seconds

        with (
            mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False),
            mock.patch("creme.semaphore._now", side_effect=lambda: wall[0]),
        ):
            ok, detail = semaphore.master_acquire("codex", "bounded master")
            self.assertTrue(ok, detail)
            lease_id = semaphore.master_snapshot()["lease"]["lease_id"]
            ok, detail = semaphore.master_heartbeat(
                1500,
                expected_lease_id=lease_id,
                sleep=nap,
                max_beats=2,
                clock=lambda: wall[0],
            )
        self.assertFalse(ok)
        self.assertIn("direct-activity deadline passed", detail)
        self.assertEqual(naps, [60])
        lease = semaphore.master_snapshot()["lease"]
        self.assertEqual(lease["heartbeat_renewals"], 1)
        self.assertEqual(lease["renewed_at"], 1_000.0)
        self.assertLessEqual(
            lease["renewed_at"] + lease["lease_seconds"] - lease["direct_activity_at"],
            4_800,
        )

    def test_unverified_heartbeat_cannot_spend_its_budget_after_starting_late(self):
        self.as_client(None, None)
        wall = [1_000.0]
        with (
            mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False),
            mock.patch("creme.semaphore._now", side_effect=lambda: wall[0]),
        ):
            ok, detail = semaphore.master_acquire("codex", "bounded master")
            self.assertTrue(ok, detail)
            lease = semaphore.master_snapshot()["lease"]
            wall[0] = lease["direct_activity_at"] + 3_000.1
            ok, detail = semaphore.master_heartbeat(
                1500,
                expected_lease_id=lease["lease_id"],
                sleep=lambda _seconds: None,
                max_beats=1,
                clock=lambda: wall[0],
            )
        self.assertFalse(ok)
        self.assertIn("direct-activity deadline passed", detail)
        final = semaphore.master_snapshot()["lease"]
        self.assertEqual(final["heartbeat_renewals"], 0)
        self.assertEqual(final["renewed_at"], 1_000.0)

    def test_direct_post_sleep_recovery_resets_the_absolute_deadline(self):
        self.as_client(None, None)
        wall = [1_000.0]
        with (
            mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False),
            mock.patch("creme.semaphore._now", side_effect=lambda: wall[0]),
        ):
            ok, detail = semaphore.master_acquire("codex", "sleeping master")
            self.assertTrue(ok, detail)
            lease = semaphore.master_snapshot()["lease"]
            wall[0] = 11_000.0
            ok, detail = semaphore.master_renew(
                expected_lease_id=lease["lease_id"], heartbeat=True
            )
            self.assertFalse(ok)
            self.assertIn("direct-activity deadline passed", detail)

            ok, detail = semaphore.master_renew()
            self.assertTrue(ok, detail)
            recovered = semaphore.master_snapshot()["lease"]
            self.assertEqual(recovered["direct_activity_at"], 11_000.0)
            self.assertEqual(recovered["heartbeat_renewals"], 0)

            wall[0] = 13_999.9
            ok, detail = semaphore.master_renew(
                expected_lease_id=lease["lease_id"], heartbeat=True
            )
            self.assertTrue(ok, detail)
            final = semaphore.master_snapshot()["lease"]
            self.assertEqual(final["direct_activity_at"], 11_000.0)
            self.assertLessEqual(
                final["renewed_at"] + final["lease_seconds"]
                - final["direct_activity_at"],
                4_800,
            )

    def test_multiple_unverified_helpers_share_one_deadline_and_budget(self):
        self.as_client(None, None)
        wall = [1_000.0]
        with (
            mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False),
            mock.patch("creme.semaphore._now", side_effect=lambda: wall[0]),
        ):
            ok, detail = semaphore.master_acquire("codex", "bounded master")
            self.assertTrue(ok, detail)
            lease = semaphore.master_snapshot()["lease"]
            anchor = lease["direct_activity_at"]
            for elapsed in (1_499.9, 2_999.9):
                wall[0] = anchor + elapsed
                ok, detail = semaphore.master_renew(
                    expected_lease_id=lease["lease_id"], heartbeat=True
                )
                self.assertTrue(ok, detail)
            ok, detail = semaphore.master_renew(
                expected_lease_id=lease["lease_id"], heartbeat=True
            )
            self.assertFalse(ok)
            self.assertIn("budget exhausted", detail)
        final = semaphore.master_snapshot()["lease"]
        self.assertEqual(final["heartbeat_renewals"], 2)
        self.assertEqual(final["direct_activity_at"], anchor)
        self.assertLessEqual(
            final["renewed_at"] + final["lease_seconds"] - anchor,
            4_800,
        )

    def test_predecessor_heartbeat_cannot_renew_a_successor_lease(self):
        self.as_client(None, None)
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False):
            semaphore.master_acquire("codex", "first")
            predecessor = semaphore.master_snapshot()["lease"]
            ok, detail = semaphore.master_release()
            self.assertTrue(ok, detail)
            ok, detail = semaphore.master_acquire("codex", "successor", take_over=True)
            self.assertTrue(ok, detail)
            ok, detail = semaphore.master_renew(
                as_session_digest=predecessor["session_digest"],
                expected_lease_id=predecessor["lease_id"],
                heartbeat=True,
            )
        self.assertFalse(ok)
        self.assertIn("lease changed", detail)
        self.assertEqual(semaphore.master_snapshot()["lease"]["note"], "successor")

    def test_schema_one_live_lease_upgrades_on_holder_renewal(self):
        legacy_lease = {
            "client": "claude",
            "client_pid": os.getpid(),
            "pid": os.getpid(),
            "uid": os.getuid(),
            "note": "legacy live master",
            "acquired_at": time.time() - 10,
            "renewed_at": time.time() - 5,
            "lease_seconds": 1800,
        }
        (self.root / "master.json").write_text(json.dumps({
            "schema_version": 1,
            "lease": legacy_lease,
        }), encoding="utf-8")
        snapshot = semaphore.master_snapshot()
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["lease"]["acquired_at"], legacy_lease["acquired_at"])
        self.assertEqual(snapshot["lease"]["direct_activity_at"], legacy_lease["renewed_at"])
        self.assertTrue(snapshot["lease"]["legacy_unbound"])
        ok, detail = semaphore.master_renew()
        self.assertTrue(ok, detail)
        persisted = json.loads((self.root / "master.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 2)
        self.assertFalse(persisted["lease"]["legacy_unbound"])
        self.assertEqual(persisted["lease"]["client"], "claude")

    def test_schema_one_process_unknown_live_lease_is_preserved_but_unwritable(self):
        self.as_client(None, None)
        legacy_lease = {
            "client": "codex",
            "client_pid": None,
            "pid": os.getpid(),
            "uid": os.getuid(),
            "note": "legacy unknown master",
            "acquired_at": time.time() - 10,
            "renewed_at": time.time() - 5,
            "lease_seconds": 1800,
        }
        path = self.root / "master.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "lease": legacy_lease,
        }), encoding="utf-8")
        snapshot = semaphore.master_snapshot()
        self.assertTrue(snapshot["lease"]["legacy_unbound"])
        self.assertEqual(snapshot["lease"]["renewed_at"], legacy_lease["renewed_at"])
        ok, detail = semaphore.master_renew()
        self.assertFalse(ok)
        ok, detail = semaphore.master_release()
        self.assertFalse(ok)
        # Refusals do not opportunistically persist or invalidate the legacy
        # record; it remains schema 1 until a legitimate write is possible.
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_the_lease_lives_beside_the_hold_state_and_changes_no_verdict(self):
        semaphore.master_acquire("claude", "master")
        path = self.root / "master.json"
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        state_path = self.root / "state.json"
        if state_path.exists():
            semaphore._validate(json.loads(state_path.read_text(encoding="utf-8")))
        ok, detail = semaphore.adaptive_acquire(
            "goal", "worker build", memory_gib=2, contention="tolerant",
            adapter=self.adapter, policy=self.policy,
        )
        self.assertTrue(ok, detail)
        semaphore._validate(json.loads(state_path.read_text(encoding="utf-8")))
        self.assertNotIn("master", json.loads(state_path.read_text(encoding="utf-8")))
        text = semaphore.status_text(self.adapter)
        self.assertIn("master: claude (live)", text)
        self.assertIn("goal (live)", text)

    def test_corrupt_master_state_is_reported_not_reset(self):
        path = self.root / "master.json"
        self.root.mkdir(exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(semaphore.SemaphoreError):
            semaphore.master_acquire("claude", "master")
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")
        self.assertIn("refusing to replace corrupt master lease state", semaphore.status_text(self.adapter))

    def test_invalid_direct_activity_anchor_is_reported_not_reset(self):
        semaphore.master_acquire("claude", "master")
        path = self.root / "master.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["lease"]["direct_activity_at"] = data["lease"]["renewed_at"] + 1
        malformed = json.dumps(data)
        path.write_text(malformed, encoding="utf-8")
        with self.assertRaises(semaphore.SemaphoreError):
            semaphore.master_snapshot()
        self.assertEqual(path.read_text(encoding="utf-8"), malformed)

    def test_the_client_walk_finds_the_session_above_the_launcher(self):
        self._client_patch.stop()
        self.addCleanup(lambda: None)
        adapter = ProcessAdapter(processes=[
            {"pid": 10, "ppid": 1, "rss_kib": 1, "command": "/Applications/Claude.app/Contents/MacOS/Claude"},
            {"pid": 20, "ppid": 10, "rss_kib": 1, "command": "/x/claude.app/Contents/MacOS/claude --model x"},
            {"pid": 30, "ppid": 20, "rss_kib": 1, "command": "/bin/zsh -c semaphore"},
            {"pid": 40, "ppid": 30, "rss_kib": 1, "command": "python3 semaphore master-acquire"},
        ])
        adapter.client_pattern = semaphore.re.compile(r"claude\.app/")
        pid, family, detail = semaphore._client_process(adapter, start_pid=30)
        self.assertEqual((pid, family), (20, "claude"))
        self.assertIn("pid 20", detail)
        unavailable = ProcessAdapter(processes=None)
        self.assertEqual(semaphore._client_process(unavailable, start_pid=30)[:2], (None, None))

    def test_the_heartbeat_renews_while_the_holder_lives_and_stops_when_it_is_gone(self):
        semaphore.master_acquire("claude", "master")
        before = semaphore.master_snapshot()["lease"]["renewed_at"]
        time.sleep(0.01)
        naps = []
        clock = [0.0]

        def nap(seconds):
            naps.append(seconds)
            clock[0] += seconds

        ok, detail = semaphore.master_heartbeat(
            5, sleep=nap, max_beats=2, clock=lambda: clock[0],
        )
        self.assertTrue(ok, detail)
        self.assertEqual(naps, [5])
        self.assertGreater(semaphore.master_snapshot()["lease"]["renewed_at"], before)
        self.assertEqual(self.log_actions().count("master-renew"), 2)
        # An orphaned heartbeat must not keep a dead master alive.
        path = self.root / "master.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["lease"]["client_pid"] = self.dead_pid()
        path.write_text(json.dumps(data), encoding="utf-8")
        ok, detail = semaphore.master_heartbeat(5, sleep=naps.append, max_beats=5)
        self.assertTrue(ok, detail)
        self.assertIn("is gone", detail)
        self.assertEqual(self.log_actions().count("master-renew"), 2)
        semaphore.master_release(force=True, reason="test")
        ok, detail = semaphore.master_heartbeat(5, sleep=naps.append)
        self.assertIn("no master lease", detail)

    def test_the_client_walk_accepts_bare_executable_names(self):
        # Darwin's snapshot is `ps -o comm`: no paths, so the session is `claude`
        # under the desktop app `Claude`; the walk must stop at the session.
        self._client_patch.stop()
        adapter = ProcessAdapter(processes=[
            {"pid": 1, "ppid": 0, "rss_kib": 1, "command": "launchd"},
            {"pid": 10, "ppid": 1, "rss_kib": 1, "command": "Claude"},
            {"pid": 15, "ppid": 10, "rss_kib": 1, "command": "disclaimer"},
            {"pid": 20, "ppid": 15, "rss_kib": 1, "command": "claude"},
            {"pid": 30, "ppid": 20, "rss_kib": 1, "command": "zsh"},
        ])
        adapter.client_pattern = semaphore.re.compile(r"/Applications/Claude\.app/")
        self.assertEqual(semaphore._client_process(adapter, start_pid=30)[:2], (20, "claude"))
        adapter.processes[3]["command"] = "codex"
        self.assertEqual(semaphore._client_process(adapter, start_pid=30)[:2], (20, "codex"))
        adapter.processes[3]["command"] = "python3"
        adapter.processes[1]["command"] = "bash"
        self.assertEqual(semaphore._client_process(adapter, start_pid=30)[:2], (None, None))

    def test_a_closed_session_strands_its_lease_at_once(self):
        self.as_client(self.dead_pid(), "claude")
        semaphore.master_acquire("claude", "tab closed without wind-down")
        # Not expired: the window is intact, but the client process is gone.
        text = semaphore.status_text(self.adapter)
        self.assertIn("(stranded)", text)
        self.assertIn("--take-over", text)
        self.as_client(os.getpid(), "codex")
        ok, detail = semaphore.master_acquire("codex", "successor", take_over=True)
        self.assertTrue(ok, detail)
        self.assertIn("stranded", detail)

    def test_an_orphaned_heartbeat_renews_for_the_holder_it_read(self):
        semaphore.master_acquire("claude", "master")
        self.as_client(None, None)          # orphaned: no client above the heartbeat
        ok, detail = semaphore.master_heartbeat(5, sleep=lambda s: None, max_beats=1)
        self.assertTrue(ok, detail)
        self.assertEqual(self.log_actions().count("master-renew"), 1)
        # A stranger without the holder assertion is still refused once lapsed.
        self.expire()
        ok, detail = semaphore.master_renew()
        self.assertFalse(ok)

    def test_the_detached_heartbeat_starts_its_own_session(self):
        semaphore.master_acquire("claude", "master")
        lease = semaphore.master_snapshot()["lease"]
        with mock.patch("creme.semaphore.subprocess.Popen") as popen:
            popen.return_value.pid = 424242
            ok, detail = semaphore.master_heartbeat_detached(1500)
        self.assertTrue(ok, detail)
        self.assertIn("pid 424242", detail)
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0][-5:],
            [
                "master-renew",
                "--heartbeat",
                "1500",
                "--heartbeat-lease-id",
                lease["lease_id"],
            ],
        )
        self.assertTrue(args[0][1].endswith("/.semaphore/semaphore"))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], semaphore.subprocess.DEVNULL)
        self.assertIn("master-heartbeat", self.log_actions())
        self.assertEqual(stat.S_IMODE((self.root / "heartbeat.log").stat().st_mode), 0o600)
        semaphore.master_release()
        ok, detail = semaphore.master_heartbeat_detached(1500)
        self.assertFalse(ok)

    def test_heartbeat_cli_forwards_the_parent_lease_binding(self):
        lease_id = "a" * 32
        with (
            mock.patch(
                "creme.cli.semaphore.master_heartbeat",
                return_value=(True, "bound heartbeat"),
            ) as heartbeat,
            mock.patch("builtins.print"),
        ):
            code = cli.main([
                "semaphore",
                "master-renew",
                "--heartbeat",
                "1500",
                "--heartbeat-lease-id",
                lease_id,
            ])
        self.assertEqual(code, 0)
        heartbeat.assert_called_once_with(1500, expected_lease_id=lease_id)

    def test_delayed_detached_child_stops_after_release_and_reacquire(self):
        self.as_client(None, None)
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False):
            semaphore.master_acquire("codex", "first")
            first = semaphore.master_snapshot()["lease"]
            with mock.patch("creme.semaphore.subprocess.Popen") as popen:
                popen.return_value.pid = 424242
                ok, detail = semaphore.master_heartbeat_detached(1500)
            self.assertTrue(ok, detail)
            command = popen.call_args.args[0]
            bound_id = (
                command[command.index("--heartbeat-lease-id") + 1]
                if "--heartbeat-lease-id" in command
                else None
            )

            ok, detail = semaphore.master_release()
            self.assertTrue(ok, detail)
            ok, detail = semaphore.master_acquire("codex", "successor")
            self.assertTrue(ok, detail)
            successor = semaphore.master_snapshot()["lease"]
            self.assertNotEqual(successor["lease_id"], bound_id)

            ok, detail = semaphore.master_heartbeat(
                1500,
                expected_lease_id=bound_id,
                sleep=lambda _seconds: None,
                max_beats=1,
            )
        self.assertTrue(ok, detail)
        self.assertIn("master lease changed", detail)
        self.assertEqual(bound_id, first["lease_id"])
        final = semaphore.master_snapshot()["lease"]
        self.assertEqual(final["lease_id"], successor["lease_id"])
        self.assertEqual(final["heartbeat_renewals"], 0)

    def test_delayed_detached_child_stops_after_same_digest_take_over(self):
        self.as_client(None, None)
        with mock.patch.dict(os.environ, {"CREME_MASTER_SESSION_ID": "session-a"}, clear=False):
            semaphore.master_acquire("codex", "first")
            first = semaphore.master_snapshot()["lease"]
            with mock.patch("creme.semaphore.subprocess.Popen") as popen:
                popen.return_value.pid = 424242
                ok, detail = semaphore.master_heartbeat_detached(1500)
            self.assertTrue(ok, detail)
            command = popen.call_args.args[0]
            bound_id = (
                command[command.index("--heartbeat-lease-id") + 1]
                if "--heartbeat-lease-id" in command
                else None
            )

            self.expire()
            ok, detail = semaphore.master_acquire(
                "codex", "successor", take_over=True
            )
            self.assertTrue(ok, detail)
            successor = semaphore.master_snapshot()["lease"]
            self.assertNotEqual(successor["lease_id"], bound_id)

            ok, detail = semaphore.master_heartbeat(
                1500,
                expected_lease_id=bound_id,
                sleep=lambda _seconds: None,
                max_beats=1,
            )
        self.assertTrue(ok, detail)
        self.assertIn("master lease changed", detail)
        self.assertEqual(bound_id, first["lease_id"])
        final = semaphore.master_snapshot()["lease"]
        self.assertEqual(final["lease_id"], successor["lease_id"])
        self.assertEqual(final["heartbeat_renewals"], 0)

    def test_the_heartbeat_renews_within_a_slice_of_waking_from_system_sleep(self):
        semaphore.master_acquire("claude", "master")
        naps = []
        clock = [0.0]

        def nap(seconds):
            naps.append(seconds)
            # The machine slept: the wall clock jumps far past the window
            # while the process itself slept only one slice.
            clock[0] += 10_000 if len(naps) == 1 else seconds

        ok, detail = semaphore.master_heartbeat(
            1500, sleep=nap, max_beats=2, clock=lambda: clock[0],
        )
        self.assertTrue(ok, detail)
        self.assertEqual(naps, [60])
        self.assertEqual(self.log_actions().count("master-renew"), 2)
