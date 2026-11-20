"""Adversarial tests for the public Creme model-fit runtime.

Covers configure -> prepare -> receipt submission -> one acceptance ->
ready statistics, plus delayed acceptance, idempotent/conflicting
receipts, failure spending, single fallback ownership, shared master
allocation, mode rollback, cross-master attribution, release partitions,
cancellation, exploration/pending caps, and no legacy import.

Temporary stores only; never touches live state. Uses the public
``creme.model_fit_runtime`` / ``creme.model_fit_episodes`` API.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creme import model_fit_episodes as E
from creme import model_fit_runtime as R

RELEASE = "muse-spark-obs-001"
LOW = "muse-spark/low"
HIGH = "muse-spark/high"
ROUTE = "muse-broker"
HARNESS = "h1"
CREDITS = [{"milestone": "done", "credit": 1.0}]


def usage(total_in=60.0, total_out=40.0):
    return {"total_input": total_in, "total_output": total_out}


def base_config(**overrides):
    config = {
        "execution_client": "muse",
        "task_type": "code-change",
        "context_version": "ctx-1",
        "harness_version": HARNESS,
        "candidates": [
            {"option": LOW, "prior_tokens": 100.0, "release": RELEASE,
             "recipe_version": "r1", "route": ROUTE},
            {"option": HIGH, "prior_tokens": 200.0, "release": RELEASE,
             "recipe_version": "r1", "route": ROUTE},
        ],
        "default": LOW,
        "delta": 0.05,
        "seed_allowance": 100000.0,
        "learning_fraction": 0.1,
    }
    config.update(overrides)
    return config


def launch_receipt(episode_id, run_id, option, release=RELEASE,
                   receipt_id=None, **extra):
    receipt = {
        "receipt_id": receipt_id or f"{episode_id}:launch:{run_id}",
        "kind": "launch",
        "episode_id": episode_id,
        "run_id": run_id,
        "option": option,
        "route": ROUTE,
        "release": release,
        "harness_version": HARNESS,
    }
    receipt.update(extra)
    return receipt


def run_receipt(episode_id, run_id, option, terminal="completed",
                segments=None, receipt_id=None, release=RELEASE,
                final=True, **extra):
    segs = segments if segments is not None else [
        {"id": f"{run_id}:seg", "usage": usage()}]
    receipt = {
        "receipt_id": receipt_id or f"{episode_id}:run:{run_id}",
        "kind": "run",
        "episode_id": episode_id,
        "run_id": run_id,
        "option": option,
        "route": ROUTE,
        "release": release,
        "harness_version": HARNESS,
        "segments": segs,
        "terminal": terminal,
    }
    if final:
        receipt.update({"usage_complete": True,
                        "usage_evidence": f"adapter#{run_id}"})
    receipt.update(extra)
    return receipt


def accept_receipt(episode_id, receipt_id=None, milestones=None,
                   weight=1.0, segment_usage=None, **extra):
    receipt = {
        "receipt_id": receipt_id or f"{episode_id}:accept",
        "kind": "accept",
        "episode_id": episode_id,
        "runs": [],
        "master_segments": [{
            "id": f"{episode_id}:mseg",
            "client": "muse",
            "usage": segment_usage or {"total_input": 5.0,
                                       "total_output": 5.0},
            "weight": weight,
        }],
        "milestones": milestones or ["done"],
        "verification_ref": f"events#{episode_id}",
        "verdict": "pass",
        "verifier": "master:x",
        "worker_ref": "w1",
    }
    receipt.update(extra)
    return receipt


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = R.open_runtime(Path(self._tmp.name))

    def tearDown(self):
        self.store.conn.close()
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------
    def configure(self, policy="pol-1", mode="active", **overrides):
        config = base_config(**overrides)
        R.configure(self.store, policy, config, mode=mode)
        return config

    def prepare(self, episode_id, policy="pol-1", master="muse",
                credits=None):
        return R.prepare(self.store, episode_id, policy,
                         credits if credits is not None else CREDITS,
                         master, f"opp#{episode_id}")

    def test_codex_upgrade_keeps_old_receipts_and_routes_new_work_by_release(self):
        from unittest.mock import patch

        previous = {"sol": "gpt-6.1-sol", "astra": "gpt-6-astra"}
        current = {"sol": "gpt-6.2-sol", "astra": "gpt-6.2-astra"}
        for family, old_release in previous.items():
            option = family + "/high"
            old_id = "old-" + family
            candidate = dict(option=option, release=old_release, route="codex-subagent",
                             recipe_version="review", prior_tokens=100)
            config = base_config(execution_client="codex", default=option, candidates=[candidate])
            with patch.dict(E.CODEX_CURRENT_RELEASES, previous):
                R.configure(self.store, old_id, config)
                prior = self.prepare("past-" + family, old_id)
            self.assertEqual(R.configure(self.store, old_id, config)["policy_id"], old_id)
            with self.assertRaisesRegex(R.RuntimeError, current[family]):
                R.configure(self.store, "fresh-" + old_id, config)
            self.assertEqual(self.prepare("past-" + family, old_id), prior)
            new = self.prepare("next-" + family, old_id)
            self.assertEqual(new["release"], current[family])
            self.assertNotEqual(new["policy_id"], old_id)
            self.assertEqual(self.prepare("next-" + family, old_id), new)
            self.assertEqual(R.refresh_codex(self.store)["successors"][old_id], new["policy_id"])
            self.assertEqual(R._policy(self.store, old_id)[1], config)
            for episode in ("past-", "next-"):
                result = R.submit(self.store, run_receipt(episode + family, episode + "run-" + family,
                                  option, release=old_release, route="codex-subagent"))
                if episode == "past-":
                    self.assertEqual(result["status"], "applied")
                    R.submit(self.store, accept_receipt(episode + family))
                else:
                    self.assertEqual(result["status"], "pending")
            _, updated = R._policy(self.store, new["policy_id"])
            self.assertEqual(R.statistics(self.store, updated, updated["candidates"][0]).n, 0)
            self.assertEqual(R.statistics(self.store, config, candidate).n, 1)

    def test_codex_migration_shares_controls_and_partitions_pending_receipts(self):
        from unittest.mock import patch

        previous = dict(E.CODEX_PREVIOUS_RELEASES)
        option = "sol/high"
        candidates = [dict(option=option, release=previous["sol"], route="codex-subagent",
                           recipe_version="review", prior_tokens=100)]
        config = base_config(execution_client="codex", default=option, candidates=candidates,
                             seed_allowance=100, learning_fraction=0)
        with patch.dict(E.CODEX_CURRENT_RELEASES, previous):
            R.configure(self.store, "legacy", config, mode="active")
            for n in range(1, 10):
                self.prepare(str(n), "legacy")
                R.cancel(self.store, str(n), "fixture cancellation")
            self.assertTrue(self.prepare("10", "legacy")["actual_exploration"])
        successor = R.refresh_codex(self.store)["successors"]["legacy"]
        # Exercise the original durable request shape written before migration.
        self.store.conn.execute("""UPDATE fit_opportunities SET decision=
            json_remove(decision,'$.release','$.codex_releases') WHERE episode_id='10'""")
        self.store.conn.commit()
        for n in range(11, 21):
            decision = self.prepare(str(n), "legacy")
        self.assertEqual(self.prepare("20", successor), decision)
        self.assertEqual(decision["opportunity"], 20)
        self.assertFalse(decision["actual_exploration"])
        budget = R.table(self.store, successor)["budget"]
        self.assertEqual((budget["reserved"], budget["pending"]), (100, 1))
        R.set_mode(self.store, "legacy", "off", "rollback", "rollback-62")
        self.assertEqual(self.prepare("21", successor)["mode"], "off")
        # The old unknown launch cannot hold back an accepted 6.2 owner.
        R.submit(self.store, run_receipt("11", "new-run", option, route="codex-subagent",
                                        release=E.CODEX_CURRENT_RELEASES["sol"]))
        R.submit(self.store, accept_receipt("11"))
        self.assertEqual(R.table(self.store, successor)["candidates"][0]["moments"]["n"], 1)
        result = R.submit(self.store, run_receipt("10", "historical-override", "astra/high",
                          route="codex-subagent", release=previous["astra"],
                          override_reason="historical independent review"))
        self.assertEqual(result["status"], "applied")
        R.submit(self.store, accept_receipt("10"))
        self.assertEqual(R.closure(self.store, "10"), {"episode_status": "closed"})

    def test_current_native_ultra_is_selectable_for_both_new_models(self):
        for family, release in E.CODEX_CURRENT_RELEASES.items():
            option = family + "/ultra"
            config = base_config(execution_client="codex", default=option, candidates=[
                dict(option=option, release=release, route="codex-subagent",
                     recipe_version="review", prior_tokens=100)])
            R.configure(self.store, family, config)
            decision = self.prepare("ultra-" + family, family)
            self.assertEqual((decision["release"], decision["actual"]), (release, option))
        self.assertFalse(E.check_capability("codex", "luna-reserve", "ultra", "luna-reserve-run")[0])

    def candidate(self, config, option=LOW):
        return next(c for c in config["candidates"]
                    if c["option"] == option)

    def drive_episode(self, episode_id, run_id, policy="pol-1",
                      master="muse", option=None, release=RELEASE):
        """Full pipeline: prepare -> launch -> run -> accept -> close."""
        config = base_config()
        decision = self.prepare(episode_id, policy, master)
        actual = option or decision["actual"]
        inbox = R.submit(self.store, launch_receipt(
            episode_id, run_id, actual, release=release))
        self.assertEqual(inbox["status"], "applied")
        inbox = R.submit(self.store, run_receipt(
            episode_id, run_id, actual, release=release))
        self.assertEqual(inbox["status"], "applied")
        inbox = R.submit(self.store, accept_receipt(episode_id))
        self.assertEqual(inbox["status"], "applied")
        return decision

    # -- main pipeline ------------------------------------------------
    def test_full_pipeline_ready_statistics(self):
        config = self.configure()
        decision = self.prepare("ep-1")
        self.assertEqual(decision["actual"], LOW)
        self.assertEqual(decision["mode"], "active")
        self.drive_episode("ep-1", "ep-1:r1")
        row = E.get_episode(self.store, "ep-1")
        self.assertEqual(row["status"], "closed")
        moments = R.statistics(self.store, config,
                               self.candidate(config))
        self.assertEqual(moments.n, 1)
        self.assertAlmostEqual(moments.work, 1.0)
        obs = E.ready_observations(
            self.store, "muse", "code-change", LOW,
            R.generation(config, self.candidate(config)))
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["episode_id"], "ep-1")
        self.assertAlmostEqual(obs[0]["accepted_work"], 1.0)
        self.assertEqual(obs[0]["candidate_seq"], 1)

    # -- delayed acceptance -------------------------------------------
    def test_acceptance_before_launch_joins_later(self):
        self.configure()
        self.prepare("ep-delay")
        inbox = R.submit(self.store, accept_receipt("ep-delay"))
        self.assertEqual(inbox["status"], "applied")
        row = E.get_episode(self.store, "ep-delay")
        self.assertNotEqual(row["status"], "closed")
        R.submit(self.store, launch_receipt("ep-delay", "ep-delay:r1",
                                            LOW))
        R.submit(self.store, run_receipt("ep-delay", "ep-delay:r1",
                                         LOW))
        row = E.get_episode(self.store, "ep-delay")
        self.assertEqual(row["status"], "closed")
        self.assertAlmostEqual(
            E.total_accepted_work(self.store.conn, "ep-delay"), 1.0)

    # -- duplicates ----------------------------------------------------
    def test_duplicate_receipts_idempotent_conflict_refused(self):
        self.configure()
        decision = self.prepare("ep-dup")
        receipt = launch_receipt("ep-dup", "ep-dup:r1",
                                 decision["actual"])
        first = R.submit(self.store, receipt)
        second = R.submit(self.store, dict(receipt))
        self.assertEqual((first["status"], second["status"]),
                         ("applied", "applied"))
        again = self.prepare("ep-dup")
        self.assertEqual(again, decision)
        with self.assertRaises(E.EpisodeError):
            R.submit(self.store, launch_receipt(
                "ep-dup", "ep-dup:r1", HIGH,
                receipt_id=receipt["receipt_id"]))
        with self.assertRaises(E.EpisodeError):
            R.prepare(self.store, "ep-dup", "pol-1",
                      [{"milestone": "other", "credit": 1.0}],
                      "muse", "opp#ep-dup")

    # -- failure / interruption ----------------------------------------
    def test_failure_and_interruption_spend_survives_unclosed(self):
        config = self.configure()
        self.prepare("ep-fail")
        R.submit(self.store, launch_receipt("ep-fail", "ep-fail:r1",
                                            LOW))
        R.submit(self.store, run_receipt("ep-fail", "ep-fail:r1", LOW,
                                         terminal="failed"))
        self.prepare("ep-intr")
        R.submit(self.store, launch_receipt("ep-intr", "ep-intr:r1",
                                            LOW))
        R.submit(self.store, run_receipt("ep-intr", "ep-intr:r1", LOW,
                                         terminal="interrupted"))
        for eid in ("ep-fail", "ep-intr"):
            accounting = E.episode_accounting(self.store, eid)
            self.assertGreater(
                accounting["spend_uncapped_tokens"], 0)
            self.assertNotEqual(
                E.get_episode(self.store, eid)["status"], "closed")
        moments = R.statistics(self.store, config,
                               self.candidate(config))
        self.assertEqual(moments.n, 0)
        self.assertEqual(E.ready_observations(
            self.store, "muse", "code-change", LOW,
            R.generation(config, self.candidate(config))), [])

    # -- fallback -------------------------------------------------------
    def test_fallback_once_original_owner_keeps_credit(self):
        self.configure()
        self.prepare("ep-fb")
        R.submit(self.store, launch_receipt("ep-fb", "ep-fb:r1", LOW))
        R.submit(self.store, run_receipt("ep-fb", "ep-fb:r1", LOW))
        R.submit(self.store, launch_receipt(
            "ep-fb", "ep-fb:r2", HIGH,
            override_reason="retry strong", attempt_index=2))
        R.submit(self.store, run_receipt(
            "ep-fb", "ep-fb:r2", HIGH,
            override_reason="retry strong", attempt_index=2))
        R.submit(self.store, accept_receipt("ep-fb"))
        row = E.get_episode(self.store, "ep-fb")
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["owner_option"], LOW)
        launches = self.store.conn.execute(
            "SELECT run_id, is_fallback FROM launches WHERE episode_id=?",
            ("ep-fb",)).fetchall()
        flags = {r["run_id"]: r["is_fallback"] for r in launches}
        self.assertEqual(flags, {"ep-fb:r1": 0, "ep-fb:r2": 1})
        self.assertAlmostEqual(
            E.total_accepted_work(self.store.conn, "ep-fb"), 1.0)

    # -- shared master allocation ---------------------------------------
    def test_shared_master_segment_weight_capped_at_one(self):
        self.configure()
        self.prepare("ep-a")
        self.prepare("ep-b")
        shared = {"total_input": 8.0, "total_output": 2.0}
        first = accept_receipt("ep-a", segment_usage=shared,
                               weight=0.6)
        first["master_segments"][0]["id"] = "shared-seg"
        second = accept_receipt("ep-b", segment_usage=shared,
                                weight=0.6)
        second["master_segments"][0]["id"] = "shared-seg"
        self.assertEqual(
            R.submit(self.store, first)["status"], "applied")
        # 0.6 + 0.6 exceeds the shared-turn cap: stays pending w/ error.
        outcome = R.submit(self.store, second)
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("would total 1.2 > 1", outcome["error"])
        third = accept_receipt("ep-b", receipt_id="ep-b:accept:2",
                               segment_usage=shared, weight=0.4)
        third["master_segments"][0]["id"] = "shared-seg"
        self.assertEqual(
            R.submit(self.store, third)["status"], "applied")

    # -- mode rollback ---------------------------------------------------
    def test_mode_rollback_retains_data(self):
        config = self.configure()
        self.drive_episode("ep-mode", "ep-mode:r1")
        before = R.statistics(self.store, config,
                              self.candidate(config))
        R.set_mode(self.store, "pol-1", "shadow", "rollback probe",
                   "mode#1")
        self.assertEqual(R.table(self.store, "pol-1")["mode"],
                         "shadow")
        after = R.statistics(self.store, config,
                             self.candidate(config))
        self.assertEqual((before.n, after.n), (1, 1))
        self.assertEqual(E.get_episode(self.store, "ep-mode")["status"],
                         "closed")
        decision = self.prepare("ep-shadow")
        self.assertEqual(decision["actual"], LOW)
        self.assertFalse(decision["actual_exploration"])
        R.set_mode(self.store, "pol-1", "active", "restore",
                   "mode#2")

    # -- cross-master attribution -----------------------------------------
    def test_cross_master_muse_episodes_share_execution_cell(self):
        config = self.configure()
        self.drive_episode("ep-m1", "ep-m1:r1", master="muse")
        self.drive_episode("ep-m2", "ep-m2:r1", master="codex")
        rows = [E.get_episode(self.store, eid)
                for eid in ("ep-m1", "ep-m2")]
        self.assertEqual(
            {r["master_client"] for r in rows}, {"muse", "codex"})
        self.assertTrue(all(r["status"] == "closed" for r in rows))
        moments = R.statistics(self.store, config,
                               self.candidate(config))
        self.assertEqual(moments.n, 2)
        self.assertAlmostEqual(moments.work, 2.0)

    # -- release partition --------------------------------------------------
    def test_release_mismatch_does_not_pollute_population(self):
        config = self.configure()
        self.drive_episode("ep-rel", "ep-rel:r1")
        candidate = self.candidate(config)
        before = R.statistics(self.store, config, candidate)
        self.prepare("ep-rel2")
        R.submit(self.store, launch_receipt(
            "ep-rel2", "ep-rel2:r1", LOW,
            release="muse-spark-obs-002"))
        after = R.statistics(self.store, config, candidate)
        self.assertEqual((before.n, after.n), (1, 1))
        self.assertEqual(len(E.ready_observations(
            self.store, "muse", "code-change", LOW,
            R.generation(config, candidate))), 1)
        row = E.get_episode(self.store, "ep-rel2")
        self.assertNotEqual(row["owner_generation"],
                            R.generation(config, candidate))

    # -- cancellation ---------------------------------------------------------
    def test_cancel_never_launched_blocks_later_launch(self):
        self.configure()
        self.prepare("ep-cancel")
        R.cancel(self.store, "ep-cancel", "no longer needed")
        outcome = R.submit(self.store, launch_receipt(
            "ep-cancel", "ep-cancel:r1", LOW))
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("cancelled before launch", outcome["error"])
        self.prepare("ep-launched")
        R.submit(self.store, launch_receipt(
            "ep-launched", "ep-launched:r1", LOW))
        with self.assertRaises(E.EpisodeError):
            R.cancel(self.store, "ep-launched", "too late")
        with self.assertRaises(E.EpisodeError):
            R.cancel(self.store, "ep-cancel", "")

    # -- exploration / pending caps ----------------------------------------------
    def test_exploration_at_most_one_in_ten_pending_capped(self):
        self.configure()
        decisions = [self.prepare(f"ep-q{i:02d}") for i in range(20)]
        flags = [d["actual_exploration"] for d in decisions]
        self.assertEqual(flags[:9], [False] * 9)
        self.assertLessEqual(sum(flags), 2)
        for entry in R.table(self.store, "pol-1")["candidates"]:
            self.assertLessEqual(entry["pending"], 20)
        concurrent = sum(
            1 for d in decisions if d["actual_exploration"])
        self.assertLessEqual(concurrent, 2)

    # -- legacy ---------------------------------------------------------------------
    def test_no_legacy_import(self):
        self.assertFalse(hasattr(R, "import_legacy"))
        with self.assertRaises(E.EpisodeError):
            E.refuse_legacy_import("samples")
        self.configure()
        health = R.health(self.store)
        self.assertEqual(health["legacy"],
                         "preserved separately; no statistical import")
        config = base_config()
        moments = R.statistics(self.store, config,
                               self.candidate(config))
        self.assertEqual(moments.n, 0)


if __name__ == "__main__":
    unittest.main()
