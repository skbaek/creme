from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
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
