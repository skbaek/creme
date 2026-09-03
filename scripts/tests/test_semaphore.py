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

from creme import semaphore
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

    def __init__(self, processes=None, workers=None, **kwargs):
        super().__init__(**kwargs)
        self.processes = processes
        self.workers = workers

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
        rows = self.log_rows()[before:]
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
