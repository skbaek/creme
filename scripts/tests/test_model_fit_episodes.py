"""Stage 1/2 negative controls for the episode evidence store.

Every test checks external behavior and invariants (idempotent replay,
no double counting, single-owner attribution, partition correctness),
not mirrored implementation fields. Stores live in temporary
directories; nothing touches live state.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from creme import model_fit_episodes as ep


def raw(total_in=None, cached=None, uncached=None, total_out=None, reasoning=None,
        inside=True, cache_write=None, **extra):
    body = {"total_input": total_in, "cached_input": cached, "uncached_input": uncached,
            "total_output": total_out, "reasoning": reasoning,
            "reasoning_inside_output": inside, "cache_write": cache_write}
    body.update(extra)
    return body


MUSE_RELEASE = "muse-spark-obs-001"
LUNA_RELEASE = "luna-obs-001"


class EpisodeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ep.open_store(Path(self._tmp.name) / "episodes.sqlite3")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def make_episode(self, eid="ep-1", client="muse", task="code-change",
                     credits=None, master="codex", recipe="r1", route="muse-broker",
                     verification_required=True):
        return ep.create_episode(
            self.store, eid, client, task,
            credits if credits is not None else [{"milestone": "done", "credit": 1.0}],
            recipe, master_client=master, route=route,
            verification_required=verification_required)

    def launch(self, run_id, episode_id, family="muse-spark", effort="high",
               route="muse-broker", release=MUSE_RELEASE, harness="h1",
               eligible=None, **kwargs):
        return ep.register_launch(
            self.store, run_id, episode_id, family, effort, route, harness,
            eligible if eligible is not None else [f"{family}/{effort}"],
            release=release, **kwargs)

    def accept(self, accept_id, episode_id, verdict="pass", work=1.0,
               milestones=None, **kwargs):
        return ep.record_acceptance(
            self.store, accept_id, episode_id, verdict, work,
            verifier="master:x", worker_ref="w1",
            milestones=["done"] if milestones is None and work else milestones,
            **kwargs)

    def verify(self, key, episode_id, run_id, amount=10.0):
        return ep.record_usage(
            self.store, key, episode_id,
            raw(total_in=amount / 2, total_out=amount / 2),
            run_id=run_id, kind="verification")

    def join_episode(self, eid, run_id, effort="high", release=MUSE_RELEASE):
        """Drive one episode to finalized: usage, verification, attempt, verdict, closure."""
        self.make_episode(eid=eid)
        self.launch(run_id, eid, effort=effort, release=release)
        ep.record_usage(self.store, f"{eid}:usage", eid,
                        raw(total_in=60, total_out=40), run_id=run_id)
        self.verify(f"{eid}:verification", eid, run_id)
        ep.record_attempt(self.store, f"{eid}:attempt", run_id, "completed")
        ep.mark_run_usage_final(self.store, run_id)
        self.accept(f"{eid}:accept", eid, run_id=run_id)
        return ep.finalize_episode(self.store, eid, "master:x", f"events#{eid}")

    # ------------------------------------------------------------ identity

    def test_credits_over_one_rejected(self):
        with self.assertRaises(ep.EpisodeError):
            self.make_episode(credits=[{"milestone": "a", "credit": 0.6},
                                       {"milestone": "b", "credit": 0.6}])

    def test_milestones_must_be_named_once(self):
        with self.assertRaises(ep.EpisodeError):
            self.make_episode(credits=[{"credit": 1.0}])
        with self.assertRaises(ep.EpisodeError):
            self.make_episode(credits=[{"milestone": "a", "credit": 0.5},
                                       {"m": "a", "credit": 0.5}])

    def test_unknown_client_or_task_rejected(self):
        with self.assertRaises(ep.EpisodeError):
            self.make_episode(client="nope")
        with self.assertRaises(ep.EpisodeError):
            self.make_episode(task="vibes")

    def test_proposal_is_not_a_launch(self):
        self.make_episode()
        ep.propose(self.store, "p1", "ep-1", "muse-spark/high")
        # No launch, no attempt, no acceptance exists for the proposal alone.
        rec = ep.reconcile(self.store)
        self.assertEqual(rec["completed_runs_missing_usage"], [])
        self.assertEqual(len(rec["episodes_without_acceptance"]), 1)

    # ------------------------------------------------------------ capability + release

    def test_muse_none_on_broker_rejected_before_launch(self):
        """The real 2026-09-30 failure: recommended none, broker refuses."""
        self.make_episode()
        with self.assertRaises(ep.CapabilityError):
            ep.register_launch(self.store, "run-none", "ep-1", "muse-spark", "none",
                               "muse-broker", "harness-v1", ["muse-spark/none"],
                               capability_exclusions=[])
        # Not a model failure: no attempt, no acceptance, no cost attributed.
        rec = ep.reconcile(self.store)
        self.assertEqual(len(rec["pre_launch_rejections"]), 1)
        self.assertEqual(
            rec["pre_launch_rejections"][0]["option"], "muse-spark/none")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["attempts"], [])
        self.assertEqual(acc["verdicts"], [])
        # The actual high run is credited to high, never to none.
        self.launch("run-high", "ep-1", effort="high", harness="harness-v1")
        launch = self.store.conn.execute(
            "SELECT * FROM launches WHERE run_id='run-high'").fetchone()
        self.assertEqual(launch["option"], "muse-spark/high")
        self.assertNotIn("none", launch["option"])

    def test_luna_reserve_route_confusion_rejected(self):
        self.make_episode(client="codex", route="codex-subagent")
        with self.assertRaises(ep.CapabilityError):
            ep.register_launch(self.store, "r1", "ep-1", "luna-reserve", "high",
                               "codex-subagent", "h1", [])
        with self.assertRaises(ep.CapabilityError):
            ep.register_launch(self.store, "r2", "ep-1", "luna", "high",
                               "luna-reserve-broker", "h1", [])

    def test_unsupported_option_rejected(self):
        self.make_episode(client="codex", route="codex-subagent")
        with self.assertRaises(ep.CapabilityError):
            ep.register_launch(self.store, "r3", "ep-1", "opus", "high",
                               "codex-subagent", "h1", [])

    def test_actual_launch_requires_observed_release(self):
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            self.launch("r1", "ep-1", release=None)
        with self.assertRaises(ep.EpisodeError):
            self.launch("r1", "ep-1", release="  ")
        launch = self.launch("r1", "ep-1", release="muse-spark-adapter-seen-abc123")
        self.assertEqual(launch["release"], "muse-spark-adapter-seen-abc123")

    def test_sol_release_must_be_observed_catalogue_id(self):
        self.make_episode(client="codex", route="codex-subagent")
        launch = ep.register_launch(self.store, "r-sol", "ep-1", "sol", "high",
                                    "codex-subagent", "h1", ["sol/high"],
                                    release="gpt-6.1-sol")
        self.assertEqual(launch["release"], "gpt-6.1-sol")
        # The retired native id is rejected for current Sol...
        with self.assertRaises(ep.EpisodeError):
            ep.register_launch(self.store, "r-old", "ep-1", "sol", "high",
                               "codex-subagent", "h1", ["sol/high"], release="gpt-6-sol")
        # ...and so is the human label: never substituted, never stored.
        with self.assertRaises(ep.EpisodeError):
            ep.register_launch(self.store, "r-label", "ep-1", "sol", "high",
                               "codex-subagent", "h1", ["sol/high"], release="GPT-6.1")
        with self.assertRaises(ep.EpisodeError):
            ep.register_launch(self.store, "r-other", "ep-1", "sol", "high",
                               "codex-subagent", "h1", ["sol/high"], release="sol-2026-09")

    def test_legacy_import_refused(self):
        with self.assertRaises(ep.MigrationError):
            ep.refuse_legacy_import("policy JSON")
        with self.assertRaises(ep.MigrationError):
            ep.refuse_legacy_import("exceptions table")

    # ------------------------------------------------------------ usage normalization

    def test_reasoning_inside_output_not_doubled(self):
        norm = ep.normalize_usage(raw(total_in=100, total_out=50, reasoning=30, inside=True))
        self.assertEqual(norm, {"input": 100.0, "output": 50.0, "total": 150.0})

    def test_reasoning_outside_output_added_once(self):
        norm = ep.normalize_usage(raw(total_in=100, total_out=50, reasoning=30, inside=False))
        self.assertEqual(norm["total"], 180.0)

    def test_cached_inside_total_not_doubled(self):
        norm = ep.normalize_usage(raw(total_in=100, cached=40))
        self.assertEqual(norm["input"], 100.0)
        norm = ep.normalize_usage(raw(uncached=60, cached=40))
        self.assertEqual(norm["input"], 100.0)

    def test_lone_uncached_without_cached_stays_unknown(self):
        norm = ep.normalize_usage(raw(uncached=60, total_out=10))
        self.assertIsNone(norm["input"])
        self.assertIsNone(norm["total"])

    def test_lone_reasoning_without_output_stays_unknown(self):
        norm = ep.normalize_usage(raw(total_in=10, reasoning=60))
        self.assertIsNone(norm["output"])
        self.assertIsNone(norm["total"])

    def test_absent_additive_reasoning_stays_unknown(self):
        norm = ep.normalize_usage(raw(total_in=10, total_out=50, inside=False))
        self.assertIsNone(norm["output"])
        self.assertIsNone(norm["total"])

    def test_cache_write_additive_flag_honored(self):
        ignored = ep.normalize_usage(raw(total_in=100, total_out=10, cache_write=25))
        self.assertEqual(ignored["total"], 110.0)
        added = ep.normalize_usage(raw(total_in=100, total_out=10, cache_write=25,
                                       cache_write_additive=True))
        self.assertEqual(added, {"input": 125.0, "output": 10.0, "total": 135.0})
        missing = ep.normalize_usage(raw(total_in=100, total_out=10,
                                         cache_write_additive=True))
        self.assertIsNone(missing["input"])
        self.assertIsNone(missing["total"])

    def test_invalid_numerics_rejected(self):
        for bad in (-1, True, float("inf"), float("nan"), "10"):
            with self.assertRaises(ep.EpisodeError, msg=f"{bad!r}"):
                ep.normalize_usage(raw(total_in=bad, total_out=10))
            with self.assertRaises(ep.EpisodeError, msg=f"{bad!r}"):
                ep.record_master_segment(self.store, f"m-{bad}", "codex",
                                         raw(total_in=10, total_out=bad))

    def test_contradictory_components_rejected(self):
        with self.assertRaises(ep.EpisodeError):  # cached exceeds its total
            ep.normalize_usage(raw(total_in=100, cached=140, total_out=10))
        with self.assertRaises(ep.EpisodeError):  # parts do not sum to total
            ep.normalize_usage(raw(total_in=100, cached=40, uncached=30, total_out=10))
        with self.assertRaises(ep.EpisodeError):  # reasoning exceeds its whole
            ep.normalize_usage(raw(total_in=10, total_out=50, reasoning=60, inside=True))

    def test_missing_usage_stays_unknown(self):
        norm = ep.normalize_usage(raw())
        self.assertEqual(norm, {"input": None, "output": None, "total": None})
        self.make_episode()
        seg = ep.record_usage(self.store, "s-missing", "ep-1", raw(), run_id=None)
        self.assertEqual(seg["missing"], 1)
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_missing_segments"], 1)
        self.assertEqual(acc["usage_tokens"], 0.0)  # unknown, not zero-valued

    def test_model_fit_token_string_reused_without_double_count(self):
        raw_usage = ep.usage_from_model_fit_tokens(
            "uncached_input=10 cache_read=2000 cache_write=300 output=400 reasoning=n/a")
        norm = ep.normalize_usage(raw_usage)
        # 10 + 2000 input parts, 400 output; cache_write excluded by default.
        self.assertEqual(norm["input"], 2010.0)
        self.assertEqual(norm["output"], 400.0)
        self.assertEqual(norm["total"], 2410.0)
        # Reasoning nested in output is not added again.
        raw2 = ep.usage_from_model_fit_tokens(
            "uncached_input=0 cache_read=0 cache_write=0 output=100 reasoning=60")
        self.assertEqual(ep.normalize_usage(raw2)["total"], 100.0)

    def test_usage_run_must_belong_to_episode(self):
        self.make_episode(eid="ep-1")
        self.make_episode(eid="ep-2")
        self.launch("r1", "ep-1")
        with self.assertRaises(ep.EpisodeError):
            ep.record_usage(self.store, "s-x", "ep-2", raw(total_in=1, total_out=1),
                            run_id="r1")
        with self.assertRaises(ep.EpisodeError):
            ep.record_cumulative_delta(self.store, "s-y", "ep-2",
                                       raw(total_in=1, total_out=1),
                                       source_key="src", sequence=1, run_id="r1")
        with self.assertRaises(ep.EpisodeError):
            ep.record_spend(self.store, "sp-x", "ep-2", 5.0, "usage", run_id="r1")

    def test_cumulative_source_recomputes_and_blocks_until_reconciled(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        ep.record_cumulative_delta(self.store, "seg-base", "ep-1",
                                   raw(total_in=1000, total_out=100),
                                   source_key="thread", sequence=1, run_id="r1")
        ep.record_cumulative_delta(self.store, "seg-next", "ep-1",
                                   raw(total_in=1600, total_out=160),
                                   source_key="thread", sequence=2, run_id="r1")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 1100.0 + 660.0)
        # A late duplicate-sequence snapshot is preserved raw, adds nothing,
        # and blocks readiness as missing until reconciled.
        late = ep.record_cumulative_delta(
            self.store, "seg-late", "ep-1", raw(total_in=1600, total_out=160),
            source_key="thread", sequence=1, run_id="r1")
        self.assertEqual(late["kind"], "run-delta-late")
        self.assertEqual(late["missing"], 1)
        # A decreasing snapshot (counter reset/error) never subtracts spend.
        down = ep.record_cumulative_delta(
            self.store, "seg-down", "ep-1", raw(total_in=100, total_out=50),
            source_key="thread", sequence=3, run_id="r1")
        self.assertEqual(down["kind"], "run-delta-nonmonotonic")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 1100.0 + 660.0)  # old known spend kept
        self.assertEqual(acc["usage_missing_segments"], 2)
        rec = ep.reconcile(self.store)
        self.assertEqual(len(rec["non_applied_snapshots"]), 2)
        # Reconcile the stale duplicate; the old spend stands as the bound.
        resolved = ep.resolve_cumulative_snapshot(
            self.store, "seg-late", "superseded", "master:x")
        self.assertEqual(resolved["missing"], 0)
        # Adopting a predating snapshot as the base is refused: the stored
        # base already advanced past it, so only supersede applies.
        ep.record_cumulative_delta(self.store, "seg-late2", "ep-1",
                                   raw(total_in=1600, total_out=160),
                                   source_key="thread", sequence=2, run_id="r1")
        with self.assertRaises(ep.EpisodeError):
            ep.resolve_cumulative_snapshot(self.store, "seg-late2", "adopt", "master:x")
        ep.resolve_cumulative_snapshot(self.store, "seg-late2", "superseded", "master:x")
        # The source still advances on the next good snapshot.
        ep.record_cumulative_delta(self.store, "seg-after", "ep-1",
                                   raw(total_in=2000, total_out=200),
                                   source_key="thread", sequence=4, run_id="r1")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 1100.0 + 660.0 + 440.0)

    def test_cumulative_unknown_base_resumes_cleanly(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        first = ep.record_cumulative_delta(
            self.store, "seg-u", "ep-1", raw(), source_key="t", sequence=1, run_id="r1")
        self.assertEqual(first["kind"], "run-delta-unknown")
        second = ep.record_cumulative_delta(
            self.store, "seg-k", "ep-1", raw(total_in=500, total_out=50),
            source_key="t", sequence=2, run_id="r1")
        self.assertEqual(second["kind"], "run-delta-unknown-base")
        third = ep.record_cumulative_delta(
            self.store, "seg-k2", "ep-1", raw(total_in=700, total_out=70),
            source_key="t", sequence=3, run_id="r1")
        self.assertEqual(third["kind"], "run-delta")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 220.0)
        self.assertEqual(acc["usage_missing_segments"], 2)

    def test_cumulative_source_binds_episode_and_run(self):
        self.make_episode(eid="ep-1")
        self.make_episode(eid="ep-2")
        self.launch("r1", "ep-1")
        self.launch("r2", "ep-2")
        self.launch("r1b", "ep-1", effort="low")
        ep.record_cumulative_delta(self.store, "s1", "ep-1",
                                   raw(total_in=100, total_out=10),
                                   source_key="shared", sequence=1, run_id="r1")
        # One counter can never straddle two books...
        with self.assertRaises(ep.EpisodeError):
            ep.record_cumulative_delta(self.store, "s2", "ep-2",
                                       raw(total_in=200, total_out=20),
                                       source_key="shared", sequence=2, run_id="r2")
        # ...nor two runs.
        with self.assertRaises(ep.EpisodeError):
            ep.record_cumulative_delta(self.store, "s3", "ep-1",
                                       raw(total_in=200, total_out=20),
                                       source_key="shared", sequence=2, run_id="r1b")

    def test_cumulative_resumed_source_declares_base_once(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        first = ep.record_cumulative_delta(
            self.store, "seg-r", "ep-1", raw(total_in=1600, total_out=160),
            source_key="resumed", sequence=1, run_id="r1", initial_base=1100.0)
        self.assertEqual(first["kind"], "run-delta")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 660.0)  # resumed base not double-counted
        with self.assertRaises(ep.EpisodeError):
            ep.record_cumulative_delta(
                self.store, "seg-r2", "ep-1", raw(total_in=1700, total_out=170),
                source_key="resumed", sequence=2, run_id="r1", initial_base=1100.0)

    def test_cumulative_adopt_new_epoch_then_deltas_resume(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        ep.record_cumulative_delta(self.store, "s1", "ep-1",
                                   raw(total_in=1600, total_out=160),
                                   source_key="t", sequence=1, run_id="r1")
        down = ep.record_cumulative_delta(self.store, "s2", "ep-1",
                                          raw(total_in=100, total_out=50),
                                          source_key="t", sequence=2, run_id="r1")
        self.assertEqual(down["kind"], "run-delta-nonmonotonic")
        ep.resolve_cumulative_snapshot(self.store, "s2", "adopt", "master:x")
        nxt = ep.record_cumulative_delta(self.store, "s3", "ep-1",
                                         raw(total_in=300, total_out=60),
                                         source_key="t", sequence=3, run_id="r1")
        self.assertEqual(nxt["kind"], "run-delta")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 1760.0 + 210.0)
        self.assertEqual(acc["usage_missing_segments"], 0)

    def test_duplicate_segment_idempotent(self):
        self.make_episode()
        first = ep.record_usage(self.store, "s1", "ep-1", raw(total_in=10, total_out=5))
        again = ep.record_usage(self.store, "s1", "ep-1", raw(total_in=10, total_out=5))
        self.assertEqual(first["segment_key"], again["segment_key"])
        self.assertEqual(ep.event_count(self.store), 2)  # episode + one usage
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 15.0)  # counted once

    def test_conflicting_identity_fails(self):
        self.make_episode()
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=10, total_out=5))
        with self.assertRaises(ep.ConflictError):
            ep.record_usage(self.store, "s1", "ep-1", raw(total_in=999, total_out=5))

    def test_shared_master_segment_never_double_counted(self):
        self.make_episode(eid="ep-a")
        self.make_episode(eid="ep-b")
        ep.record_master_segment(self.store, "m1", "codex", raw(total_in=80, total_out=20))
        ep.allocate_shared(self.store, "a1", "ep-a", "m1", 0.6)
        ep.allocate_shared(self.store, "b1", "ep-b", "m1", 0.4)
        with self.assertRaises(ep.EpisodeError):
            ep.allocate_shared(self.store, "b2", "ep-b", "m1", 0.1)  # would total 1.1
        self.assertEqual(ep.episode_accounting(self.store, "ep-a")["usage_tokens"], 60.0)
        self.assertEqual(ep.episode_accounting(self.store, "ep-b")["usage_tokens"], 40.0)

    def test_allocation_replay_precedes_cap_check(self):
        self.make_episode(eid="ep-a")
        self.make_episode(eid="ep-b")
        ep.record_master_segment(self.store, "m1", "codex", raw(total_in=80, total_out=20))
        ep.allocate_shared(self.store, "a1", "ep-a", "m1", 1.0)
        # Replaying the existing full allocation must succeed, not trip the cap.
        again = ep.allocate_shared(self.store, "a1", "ep-a", "m1", 1.0)
        self.assertEqual(again["segment_key"], "a1")
        self.assertEqual(ep.episode_accounting(self.store, "ep-a")["usage_tokens"], 100.0)
        with self.assertRaises(ep.ConflictError):
            ep.allocate_shared(self.store, "a1", "ep-a", "m1", 0.5)

    # ------------------------------------------------------------ acceptance

    def test_worker_self_verification_rejected(self):
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            ep.record_acceptance(self.store, "a1", "ep-1", "pass", 1.0,
                                 verifier="worker:run-1", worker_ref="worker:run-1",
                                 milestones=["done"])

    def test_fail_accepts_no_credit(self):
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            ep.record_acceptance(self.store, "a1", "ep-1", "fail", 0.5,
                                 verifier="master:x", worker_ref="worker:run-1",
                                 milestones=["done"])

    def test_credit_cannot_exceed_predeclared(self):
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            ep.record_acceptance(self.store, "a1", "ep-1", "pass", 2.0,
                                 verifier="master:x", worker_ref="worker:run-1",
                                 milestones=["done"])

    def test_credit_must_name_predeclared_milestones(self):
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            self.accept("a1", "ep-1", milestones=["ghost"])
        with self.assertRaises(ep.EpisodeError):
            self.accept("a1", "ep-1", work=0.5, milestones=["done"])
        with self.assertRaises(ep.EpisodeError):
            self.accept("a1", "ep-1", work=1.0, milestones=["done", "done"])

    def test_correction_supersedes_without_fork(self):
        self.make_episode(credits=[{"milestone": "a", "credit": 0.5},
                                   {"milestone": "b", "credit": 0.5}])
        self.accept("a1", "ep-1", work=1.0, milestones=["a", "b"])
        ep.record_acceptance(self.store, "a2", "ep-1", "partial", 0.5,
                             verifier="master:x", worker_ref="w1",
                             correction_of="a1", milestones=["a"])
        conn = self.store.conn
        self.assertEqual(ep.total_accepted_work(conn, "ep-1"), 0.5)
        # History retained: both rows present.
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM acceptances").fetchone()["n"], 2)
        # A superseded acceptance cannot be corrected again: no forks.
        with self.assertRaises(ep.EpisodeError):
            ep.record_acceptance(self.store, "a3", "ep-1", "partial", 0.5,
                                 verifier="master:x", worker_ref="w1",
                                 correction_of="a1", milestones=["b"])
        # Nor can a live milestone be credited twice by a fresh verdict.
        with self.assertRaises(ep.EpisodeError):
            self.accept("a4", "ep-1", work=0.5, milestones=["a"])

    def test_acceptance_replay_precedes_cap_check(self):
        self.make_episode()
        self.accept("a1", "ep-1")
        again = self.accept("a1", "ep-1")
        self.assertEqual(again["accept_id"], "a1")
        self.assertEqual(ep.total_accepted_work(self.store.conn, "ep-1"), 1.0)

    def test_interrupted_keeps_cost_and_credit(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=100, total_out=50), run_id="r1")
        ep.record_attempt(self.store, "att1", "r1", "interrupted", "capacity limit")
        ep.record_acceptance(self.store, "a1", "ep-1", "unknown", 0.0,
                             verifier="master:x", worker_ref="w1")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 150.0)  # incurred cost survives
        cells = ep.cell_summary(self.store, "muse")
        key = next(iter(cells))
        self.assertEqual(cells[key]["censored"], 1)
        self.assertEqual(cells[key]["unknown"], 1)

    def test_partial_acceptance(self):
        self.make_episode(credits=[{"milestone": "a", "credit": 0.5},
                                   {"milestone": "b", "credit": 0.5}])
        ep.record_acceptance(self.store, "a1", "ep-1", "partial", 0.5,
                             verifier="master:x", worker_ref="w1", milestones=["a"])
        self.assertEqual(ep.episode_accounting(self.store, "ep-1")["accepted_work"], 0.5)

    def test_late_out_of_order_records_reconcile(self):
        """Acceptance arriving before usage still joins the same episode."""
        self.make_episode()
        self.launch("r1", "ep-1")
        self.accept("a1", "ep-1", run_id="r1")
        ep.record_usage(self.store, "s-late", "ep-1", raw(total_in=60, total_out=40), run_id="r1")
        ep.record_attempt(self.store, "att-late", "r1", "completed")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual((acc["accepted_work"], acc["usage_tokens"]), (1.0, 100.0))
        self.assertEqual(acc["pending"], False)

    def test_out_of_order_arrivals_rejected_then_recovered(self):
        """Usage/completion before launch is rejected, never silently held."""
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            ep.record_usage(self.store, "s-early", "ep-1",
                            raw(total_in=60, total_out=40), run_id="r1")
        with self.assertRaises(ep.EpisodeError):
            ep.record_attempt(self.store, "att-early", "r1", "completed")
        self.launch("r1", "ep-1")
        ep.record_usage(self.store, "s-early", "ep-1",
                        raw(total_in=60, total_out=40), run_id="r1")
        ep.record_attempt(self.store, "att-early", "r1", "completed")
        # Crash recovery: identical resubmission after reopen is a no-op.
        path = self.store.path
        self.store.close()
        reopened = ep.open_store(path)
        try:
            again = ep.record_usage(reopened, "s-early", "ep-1",
                                    raw(total_in=60, total_out=40), run_id="r1")
            self.assertEqual(again["segment_key"], "s-early")
            acc = ep.episode_accounting(reopened, "ep-1")
            self.assertEqual(acc["usage_tokens"], 100.0)
        finally:
            reopened.close()
            self.store = ep.open_store(path)

    def test_cheap_first_fallback_charged_once_to_owner(self):
        """Cheap attempt + expensive fallback: full cost once to the recipe."""
        self.make_episode(recipe="cheap-then-strong")
        low = self.launch("r-cheap", "ep-1", effort="low")
        high = self.launch("r-strong", "ep-1", effort="high")
        self.assertEqual(low["is_fallback"], 0)
        self.assertEqual((low["candidate_seq"], high["candidate_seq"]), (1, None))
        self.assertEqual(high["is_fallback"], 1)
        ep.record_usage(self.store, "s-cheap", "ep-1",
                        raw(total_in=900, total_out=100), run_id="r-cheap")
        ep.record_attempt(self.store, "att-cheap", "r-cheap", "failed")
        ep.mark_run_usage_final(self.store, "r-cheap")
        ep.record_usage(self.store, "s-strong", "ep-1",
                        raw(total_in=9000, total_out=1000), run_id="r-strong")
        self.verify("s-verify", "ep-1", "r-strong")
        ep.record_attempt(self.store, "att-strong", "r-strong", "completed")
        ep.mark_run_usage_final(self.store, "r-strong")
        self.accept("a1", "ep-1", run_id="r-strong")
        ep.finalize_episode(self.store, "ep-1", "master:x")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["accepted_work"], 1.0)
        self.assertEqual(acc["usage_tokens"], 1000.0 + 10000.0 + 10.0)
        cells = ep.cell_summary(self.store, "muse")
        owned = [c for k, c in cells.items() if "muse-spark/low" in k]
        self.assertEqual(len(owned), 1)
        self.assertEqual(
            (owned[0]["episodes"], owned[0]["runs"], owned[0]["fallback_runs"]), (1, 1, 1))
        self.assertEqual(owned[0]["verified_work"], 1.0)
        self.assertEqual(owned[0]["tokens"], 1000.0 + 10000.0 + 10.0)
        # No free standalone strong success anywhere: no high-owned cell exists.
        self.assertFalse([k for k in cells if "muse-spark/high" in k])
        self.assertEqual(sum(c["verified_work"] for c in cells.values()), 1.0)
        self.assertEqual(
            ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high"), [])

    # ------------------------------------------------------------ lifecycle

    def test_reservation_release_cancel(self):
        self.make_episode()
        ep.reserve(self.store, "res1", "ep-1", 5000.0)
        ep.reserve(self.store, "res2", "ep-1", 5000.0)
        ep.release_reservation(self.store, "res1", "released")
        ep.release_reservation(self.store, "res2", "cancelled")
        rows = {r["reservation_id"]: r["status"] for r in self.store.conn.execute(
            "SELECT reservation_id, status FROM reservations").fetchall()}
        self.assertEqual(rows, {"res1": "released", "res2": "cancelled"})
        # Reservations are advisory: releasing never deletes the ledger.
        self.assertIn("res1", [r["reservation_id"] for r in
                               self.store.conn.execute("SELECT reservation_id FROM reservations")])

    def test_restart_replay_preserves_attribution(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=100, total_out=50), run_id="r1")
        self.accept("a1", "ep-1", run_id="r1")
        before = ep.episode_accounting(self.store, "ep-1")
        count = ep.event_count(self.store)
        path = self.store.path
        self.store.close()
        reopened = ep.open_store(path)
        try:
            after = ep.episode_accounting(reopened, "ep-1")
            self.assertEqual(before, after)
            self.assertEqual(ep.event_count(reopened), count)
        finally:
            reopened.close()
            self.store = ep.open_store(path)

    def test_release_change_starts_new_generation_history_intact(self):
        self.make_episode(eid="ep-old", client="codex", route="codex-subagent", recipe="r1")
        ep.register_launch(self.store, "r-old", "ep-old", "luna", "high",
                           "codex-subagent", "h1", ["luna/high"], release="luna-obs-old")
        old_gen = self.store.conn.execute(
            "SELECT generation FROM launches WHERE run_id='r-old'").fetchone()["generation"]
        n = ep.close_generation(self.store, old_gen)
        self.assertEqual(n, 1)
        self.make_episode(eid="ep-new", client="codex", route="codex-subagent", recipe="r1")
        launch = ep.register_launch(self.store, "r-new", "ep-new", "luna", "high",
                                    "codex-subagent", "h1", ["luna/high"],
                                    release="luna-obs-new")
        self.assertEqual(launch["release"], "luna-obs-new")
        self.assertNotEqual(launch["generation"], old_gen)
        current = ep.cell_summary(self.store, "codex")
        self.assertTrue(all("luna-obs-new" in k or "unlaunched" in k for k in current))
        archived = ep.cell_summary(self.store, "codex", include_archived=True)
        self.assertGreater(len(archived), len(current))

    def test_harness_change_partitions_cells(self):
        self.make_episode(eid="ep-h1", recipe="r1")
        self.launch("r-h1", "ep-h1", harness="h1")
        self.make_episode(eid="ep-h2", recipe="r1")
        self.launch("r-h2", "ep-h2", harness="h2")
        cells = ep.cell_summary(self.store, "muse")
        owned = [k for k in cells if "muse-spark/high" in k]
        self.assertEqual(len(owned), 2)
        for key in owned:
            gen = key.split(" @ ", 1)[1]
            order = ep.candidate_order(self.store, "muse", "code-change",
                                       "muse-spark/high", generation=gen)
            self.assertEqual(len(order), 1)

    def test_cross_master_muse_belongs_to_muse_only(self):
        """A Codex master dispatching to Muse advances only Muse's books."""
        ep.create_episode(self.store, "ep-x", "muse", "code-change",
                          [{"milestone": "done", "credit": 1.0}], "r1",
                          master_client="codex", route="muse-broker")
        self.launch("r-x", "ep-x")
        ep.record_usage(self.store, "s-x", "ep-x", raw(total_in=100, total_out=50), run_id="r-x")
        ep.record_acceptance(self.store, "a-x", "ep-x", "pass", 1.0,
                             verifier="master:codex", worker_ref="muse:w1", run_id="r-x",
                             milestones=["done"])
        muse_cells = ep.cell_summary(self.store, "muse")
        codex_cells = ep.cell_summary(self.store, "codex")
        self.assertEqual(sum(c["episodes"] for c in muse_cells.values()), 1)
        self.assertEqual(sum(c["episodes"] for c in codex_cells.values()), 0)

    def test_reconcile_surfaces_gaps(self):
        self.make_episode()
        ep.reserve(self.store, "res1", "ep-1", 100.0)
        self.launch("r1", "ep-1")
        ep.record_attempt(self.store, "att1", "r1", "completed")
        rec = ep.reconcile(self.store)
        self.assertEqual(len(rec["pending_reservations"]), 1)
        self.assertEqual(len(rec["episodes_without_acceptance"]), 1)
        self.assertEqual(len(rec["completed_runs_missing_usage"]), 1)

    def test_combined_interface_end_to_end(self):
        self.make_episode()
        out = ep.register_launch_and_acceptance(
            self.store, "ep-1", "r1", "muse-spark", "high", "muse-broker", "h1",
            ["muse-spark/high"], MUSE_RELEASE, verifier="master:x", worker_ref="w1",
            verdict="pass", accepted_work=1.0, milestones=["done"],
            usage=raw(total_in=100, total_out=50),
            verification_usage=raw(total_in=5, total_out=5),
            verification_ref="events#1")
        self.assertEqual(out["launch"]["option"], "muse-spark/high")
        self.assertEqual(out["acceptance"]["verdict"], "pass")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual((acc["accepted_work"], acc["usage_tokens"]), (1.0, 160.0))
        ep.mark_run_usage_final(self.store, "r1")
        finalized = ep.finalize_episode(self.store, "ep-1", "master:x", "events#1")
        self.assertEqual(finalized["status"], "closed")

    def test_finalize_gates(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        # Open run: no attempt, no marked final usage yet.
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=60, total_out=40),
                        run_id="r1")
        # Usage present but the adapter has not marked it final.
        with self.assertRaises(ep.EpisodeError) as ctx:
            ep.finalize_episode(self.store, "ep-1", "master:x")
        self.assertIn("adapter-marked final usage", str(ctx.exception))
        ep.record_attempt(self.store, "att1", "r1", "completed")
        ep.mark_run_usage_final(self.store, "r1")
        # Still missing acceptance and verification usage.
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x")
        self.accept("a1", "ep-1", run_id="r1")
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x")
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x", usage_complete=False)
        # Unknown verification cost does not satisfy the declaration...
        ep.record_usage(self.store, "s-vu", "ep-1", raw(), run_id="r1",
                        kind="verification")
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x")
        self.verify("s-v", "ep-1", "r1")
        # ...and the unknown row itself still blocks: it is evidence, so it
        # cannot be deleted or zeroed away. Closure stays blocked, honestly.
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x")

    def test_finalize_replay_and_double_close(self):
        self.join_episode("ep-1", "r1")
        closed = ep.finalize_episode(self.store, "ep-1", "master:x", "events#ep-1")
        self.assertEqual((closed["status"], closed["usage_complete"]), ("closed", 1))
        # Identical re-closure is a no-op; a distinct second closure fails.
        again = ep.finalize_episode(self.store, "ep-1", "master:x", "events#ep-1")
        self.assertEqual(again["status"], "closed")
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:y", "events#10",
                                event_id="finalize:ep-1:second")

    def test_mark_run_usage_final_needs_known_usage(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        with self.assertRaises(ep.EpisodeError):
            ep.mark_run_usage_final(self.store, "r1")
        with self.assertRaises(ep.EpisodeError):
            ep.mark_run_usage_final(self.store, "nope")
        ep.record_usage(self.store, "s-u", "ep-1", raw(), run_id="r1")
        with self.assertRaises(ep.EpisodeError):
            ep.mark_run_usage_final(self.store, "r1")
        ep.record_usage(self.store, "s-k", "ep-1", raw(total_in=1, total_out=1),
                        run_id="r1")
        marked = ep.mark_run_usage_final(self.store, "r1")
        self.assertEqual(marked["usage_final_count"], 2)
        again = ep.mark_run_usage_final(self.store, "r1")
        self.assertEqual(again["usage_final_count"], 2)
        # New usage invalidates the mark: replaying the old mark event id
        # with the new count conflicts loudly instead of revalidating.
        ep.record_usage(self.store, "s-k2", "ep-1", raw(total_in=2, total_out=2),
                        run_id="r1")
        with self.assertRaises(ep.ConflictError):
            ep.mark_run_usage_final(self.store, "r1", event_id="run-usage-final:r1:2")
        current = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM usage_segments WHERE run_id='r1'").fetchone()["n"]
        self.assertEqual(current, 3)
        row = self.store.conn.execute(
            "SELECT usage_final_count FROM launches WHERE run_id='r1'").fetchone()
        self.assertEqual(row["usage_final_count"], 2)  # stale mark untouched
        fresh = ep.mark_run_usage_final(self.store, "r1")
        self.assertEqual(fresh["usage_final_count"], 3)

    def test_candidate_seq_assigned_per_episode_in_launch_order(self):
        self.make_episode(eid="ep-1")
        self.make_episode(eid="ep-2")
        l1 = self.launch("r1", "ep-1")
        l2 = self.launch("r2", "ep-2")
        self.assertEqual((l1["candidate_seq"], l2["candidate_seq"]), (1, 2))
        # A fallback launch in an owned episode takes no sequence.
        fb = self.launch("r1b", "ep-1", effort="low")
        self.assertIsNone(fb["candidate_seq"])
        self.assertEqual(fb["is_fallback"], 1)
        # Replay returns the same seq, never a new one.
        again = self.launch("r1", "ep-1")
        self.assertEqual(again["candidate_seq"], 1)

    def test_sequences_partitioned_per_owner_cell(self):
        self.make_episode(eid="ep-1")
        self.make_episode(eid="ep-2")
        self.launch("r1", "ep-1", effort="high")
        other = self.launch("r2", "ep-2", effort="low")
        self.assertEqual(other["candidate_seq"], 1)

    def test_prefix_needs_finalized_order_not_completion_order(self):
        for eid in ("ep-1", "ep-2", "ep-3"):
            self.make_episode(eid=eid)
            self.launch(f"r-{eid}", eid)
        # Fast job fully joined first, but nothing is finalized yet.
        ep.record_usage(self.store, "s3", "ep-3", raw(total_in=60, total_out=40),
                        run_id="r-ep-3")
        self.verify("s3v", "ep-3", "r-ep-3")
        ep.record_attempt(self.store, "att3", "r-ep-3", "completed")
        ep.mark_run_usage_final(self.store, "r-ep-3")
        self.accept("a3", "ep-3", run_id="r-ep-3")
        view = ep.prefix_ready(self.store, "muse", "code-change", "muse-spark/high")
        self.assertEqual(view["prefix_len"], 0)
        self.assertEqual(view["ready_episode_ids"], [])
        self.assertEqual(view["completed_beyond_prefix"], [])
        # Later completion stays immediately visible in accounting anyway.
        self.assertEqual(ep.episode_accounting(self.store, "ep-3")["accepted_work"], 1.0)
        # Finalizing seq 3 alone leaves a gap at 1-2, listed as beyond-prefix.
        ep.finalize_episode(self.store, "ep-3", "master:x")
        view = ep.prefix_ready(self.store, "muse", "code-change", "muse-spark/high")
        self.assertEqual(view["prefix_len"], 0)
        self.assertEqual(view["gaps"], [1, 2])
        self.assertEqual(view["completed_beyond_prefix"], [3])
        # Finalizing seq 1 advances the prefix; seq 2 shows as the gap.
        self.join_episode("ep-1", "r-ep-1")
        view = ep.prefix_ready(self.store, "muse", "code-change", "muse-spark/high")
        self.assertEqual(view["prefix_len"], 1)
        self.assertEqual(view["ready_episode_ids"], ["ep-1"])
        self.assertEqual(view["gaps"], [2])
        self.assertEqual(view["completed_beyond_prefix"], [3])
        # Closing seq 2 completes the prefix through the already-ready seq 3.
        self.join_episode("ep-2", "r-ep-2")
        view = ep.prefix_ready(self.store, "muse", "code-change", "muse-spark/high")
        self.assertEqual((view["prefix_len"], view["gaps"],
                          view["completed_beyond_prefix"]), (3, [], []))
        obs = view["ready_observations"][0]
        self.assertEqual(
            (obs["episode_id"], obs["candidate_seq"], obs["owner_option"],
             obs["accepted_work"], obs["usage_tokens"], obs["spend_uncapped_tokens"]),
            ("ep-1", 1, "muse-spark/high", 1.0, 110.0, 110.0))
        self.assertIn(obs["generation"],
                      self.store.conn.execute(
                          "SELECT owner_generation FROM episodes WHERE episode_id='ep-1'"
                      ).fetchone()["owner_generation"])
        self.assertEqual(
            ep.ready_observations(self.store, "muse", "code-change", "muse-spark/high"),
            view["ready_observations"])

    def test_joined_but_unfinalized_is_not_ready(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        self.accept("a1", "ep-1", run_id="r1")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=60, total_out=40),
                        run_id="r1")
        ordered = ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high")
        self.assertEqual(ordered[0]["joined"], True)
        self.assertEqual(ordered[0]["ready"], False)
        self.assertEqual(
            ep.prefix_ready(self.store, "muse", "code-change", "muse-spark/high")["prefix_len"], 0)
        self.assertEqual(
            ep.ready_observations(self.store, "muse", "code-change", "muse-spark/high"), [])

    def test_known_row_does_not_mask_unknown_tail(self):
        """One known row plus one unknown row is missing, never ready."""
        self.make_episode()
        self.launch("r1", "ep-1")
        ep.record_usage(self.store, "s-known", "ep-1",
                        raw(total_in=60, total_out=40), run_id="r1")
        ep.record_usage(self.store, "s-tail", "ep-1", raw(), run_id="r1")
        self.accept("a1", "ep-1", run_id="r1")
        ordered = ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high")
        self.assertEqual(ordered[0]["joined"], True)
        self.assertEqual(ordered[0]["usage_missing"], True)
        self.assertEqual(ordered[0]["ready"], False)

    def test_fallback_without_usage_blocks_finalize(self):
        self.make_episode()
        self.launch("r1", "ep-1", effort="low")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=90, total_out=10),
                        run_id="r1")
        ep.record_attempt(self.store, "att1", "r1", "failed")
        ep.mark_run_usage_final(self.store, "r1")
        self.launch("r2", "ep-1", effort="high")
        ep.record_attempt(self.store, "att2", "r2", "completed")
        self.verify("s-v", "ep-1", "r2")
        self.accept("a1", "ep-1", run_id="r2")
        with self.assertRaises(ep.EpisodeError) as ctx:
            ep.finalize_episode(self.store, "ep-1", "master:x")
        self.assertIn("r2", str(ctx.exception))
        ep.record_usage(self.store, "s2", "ep-1", raw(total_in=900, total_out=100),
                        run_id="r2")
        ep.mark_run_usage_final(self.store, "r2")
        closed = ep.finalize_episode(self.store, "ep-1", "master:x")
        self.assertEqual(closed["status"], "closed")

    def test_shared_verification_allocation_satisfies_alone(self):
        """A shared verification turn counts once, with no second row charged."""
        self.make_episode()
        self.launch("r1", "ep-1")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=60, total_out=40),
                        run_id="r1")
        ep.record_attempt(self.store, "att1", "r1", "completed")
        ep.mark_run_usage_final(self.store, "r1")
        ep.record_master_segment(self.store, "mv", "codex",
                                 raw(total_in=80, total_out=20))
        ep.allocate_shared(self.store, "av", "ep-1", "mv", 0.5, purpose="verification")
        self.accept("a1", "ep-1", run_id="r1")
        closed = ep.finalize_episode(self.store, "ep-1", "master:x")
        self.assertEqual(closed["status"], "closed")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["usage_tokens"], 100.0 + 50.0)  # single shared row
        kinds = [r["kind"] for r in self.store.conn.execute(
            "SELECT kind FROM usage_segments WHERE episode_id='ep-1'").fetchall()]
        self.assertEqual(kinds.count("verification"), 0)
        self.assertEqual(
            sum(1 for r in self.store.conn.execute(
                "SELECT purpose FROM usage_segments WHERE episode_id='ep-1'").fetchall()
                if r["purpose"] == "verification"), 1)

    def test_pass_verdict_must_name_milestones(self):
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            ep.record_acceptance(self.store, "a1", "ep-1", "pass", 1.0,
                                 verifier="master:x", worker_ref="w1")

    def test_post_finalize_evidence_reopens(self):
        self.join_episode("ep-1", "r1")
        self.assertEqual(
            ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high"
                               )[0]["ready"], True)
        ep.record_usage(self.store, "s-extra", "ep-1",
                        raw(total_in=10, total_out=10), run_id="r1")
        state = ep.candidate_order(
            self.store, "muse", "code-change", "muse-spark/high")[0]
        self.assertEqual(state["ready"], False)
        self.assertEqual(state["finalized"], False)
        # The new row invalidated the run's mark: re-closing without a
        # fresh mark fails, with one it succeeds.
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x")
        ep.mark_run_usage_final(self.store, "r1")
        ep.finalize_episode(self.store, "ep-1", "master:x")
        self.assertEqual(
            ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high"
                               )[0]["ready"], True)

    def test_post_finalize_correction_reopens_and_revalidates(self):
        self.make_episode(credits=[{"milestone": "a", "credit": 0.5},
                                   {"milestone": "b", "credit": 0.5}])
        self.launch("r1", "ep-1")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=60, total_out=40),
                        run_id="r1")
        self.verify("s-v", "ep-1", "r1")
        ep.record_attempt(self.store, "att1", "r1", "completed")
        ep.mark_run_usage_final(self.store, "r1")
        ep.record_acceptance(self.store, "a1", "ep-1", "pass", 1.0,
                             verifier="master:x", worker_ref="w1", run_id="r1",
                             milestones=["a", "b"])
        ep.finalize_episode(self.store, "ep-1", "master:x")
        ep.record_acceptance(self.store, "a2", "ep-1", "partial", 0.5,
                             verifier="master:x", worker_ref="w1",
                             correction_of="a1", milestones=["a"])
        episode = ep.get_episode(self.store, "ep-1")
        self.assertEqual(episode["status"], "open")
        self.assertEqual(
            ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high"
                               )[0]["ready"], False)
        ep.finalize_episode(self.store, "ep-1", "master:x")
        self.assertEqual(ep.episode_accounting(self.store, "ep-1")["accepted_work"], 0.5)

    def test_post_finalize_fallback_reopens(self):
        self.join_episode("ep-1", "r1")
        self.launch("r2", "ep-1", effort="low")
        episode = ep.get_episode(self.store, "ep-1")
        self.assertEqual(episode["status"], "open")
        self.assertEqual(
            ep.prefix_ready(self.store, "muse", "code-change", "muse-spark/high"
                            )["prefix_len"], 0)

    def test_windowed_master_segments_reject_overlap(self):
        first = ep.record_master_segment(
            self.store, "w1", "codex", raw(total_in=100, total_out=20),
            source_hash="abc123", start_offset=0, end_offset=10)
        self.assertEqual(
            (first["source_hash"], first["start_offset"], first["end_offset"]),
            ("abc123", 0, 10))
        # Exact replay of the same segment succeeds.
        again = ep.record_master_segment(
            self.store, "w1", "codex", raw(total_in=100, total_out=20),
            source_hash="abc123", start_offset=0, end_offset=10)
        self.assertEqual(again["master_segment_key"], "w1")
        # Adjacent windows on one source are accepted.
        ep.record_master_segment(self.store, "w2", "codex",
                                 raw(total_in=50, total_out=10),
                                 source_hash="abc123", start_offset=10, end_offset=20)
        # Overlapping windows with different ids are rejected, never charged.
        for key, start, end in (("wx", 5, 15), ("wy", 0, 10), ("wz", 8, 12)):
            with self.assertRaises(ep.EpisodeError, msg=key):
                ep.record_master_segment(self.store, key, "codex",
                                         raw(total_in=10, total_out=2),
                                         source_hash="abc123",
                                         start_offset=start, end_offset=end)
        # Identical windows on a different source are independent.
        ep.record_master_segment(self.store, "w3", "codex",
                                 raw(total_in=100, total_out=20),
                                 source_hash="def456", start_offset=0, end_offset=10)
        # Raw-embedded window fields (the adapter shape) are validated too.
        ep.record_master_segment(
            self.store, "codex-window:abc123:20:30", "codex",
            {"total_input": 40, "total_output": 8, "reasoning_inside_output": True,
             "source": "abc123", "start_offset": 20, "end_offset": 30})
        with self.assertRaises(ep.EpisodeError):
            ep.record_master_segment(
                self.store, "codex-window:abc123:25:35", "codex",
                {"total_input": 40, "total_output": 8, "source": "abc123",
                 "start_offset": 25, "end_offset": 35})
        count = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM master_segments").fetchone()["n"]
        self.assertEqual(count, 4)

    def test_window_fields_validated(self):
        with self.assertRaises(ep.EpisodeError):
            ep.record_master_segment(self.store, "b1", "codex",
                                     raw(total_in=1, total_out=1),
                                     source_hash="s", start_offset=10, end_offset=10)
        with self.assertRaises(ep.EpisodeError):
            ep.record_master_segment(self.store, "b2", "codex",
                                     raw(total_in=1, total_out=1),
                                     source_hash="s", start_offset=-1, end_offset=5)
        with self.assertRaises(ep.EpisodeError):
            ep.record_master_segment(self.store, "b3", "codex",
                                     raw(total_in=1, total_out=1),
                                     source_hash="s", start_offset=True, end_offset=5)
        with self.assertRaises(ep.EpisodeError):
            ep.record_master_segment(self.store, "b4", "codex",
                                     raw(total_in=1, total_out=1),
                                     source_hash="s", start_offset=0, end_offset=None)

    def test_windowed_segment_allocates_once(self):
        self.make_episode(eid="ep-a")
        self.make_episode(eid="ep-b")
        ep.record_master_segment(self.store, "w1", "codex",
                                 raw(total_in=100, total_out=20),
                                 source_hash="abc", start_offset=0, end_offset=10)
        ep.allocate_shared(self.store, "a1", "ep-a", "w1", 0.6)
        ep.allocate_shared(self.store, "b1", "ep-b", "w1", 0.4)
        self.assertEqual(ep.episode_accounting(self.store, "ep-a")["usage_tokens"], 72.0)
        self.assertEqual(ep.episode_accounting(self.store, "ep-b")["usage_tokens"], 48.0)

    def test_cell_stats_matches_cell_summary(self):
        """Falsifier: the SQL sufficient stats must equal the row-built view."""
        self.make_episode(eid="ep-1", credits=[{"milestone": "a", "credit": 0.5},
                                               {"milestone": "b", "credit": 0.5}])
        self.make_episode(eid="ep-2")
        self.make_episode(eid="ep-3")
        self.launch("r1", "ep-1", effort="low")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=90, total_out=10),
                        run_id="r1")
        ep.record_attempt(self.store, "att1", "r1", "failed")
        self.launch("r1b", "ep-1", effort="high")
        ep.record_usage(self.store, "s1b", "ep-1", raw(total_in=180, total_out=20),
                        run_id="r1b")
        ep.record_attempt(self.store, "att1b", "r1b", "completed")
        ep.record_acceptance(self.store, "a1", "ep-1", "pass", 1.0,
                             verifier="master:x", worker_ref="w1", run_id="r1b",
                             milestones=["a", "b"])
        ep.record_acceptance(self.store, "a2", "ep-1", "partial", 0.5,
                             verifier="master:x", worker_ref="w1",
                             correction_of="a1", milestones=["a"])
        ep.reserve(self.store, "res1", "ep-2", 100.0)
        self.launch("r2", "ep-2", effort="low")
        summary = ep.cell_summary(self.store, "muse")
        stats = ep.cell_stats(self.store, "muse")
        self.assertEqual(set(stats), set(summary))
        for key in summary:
            for field, value in summary[key].items():
                self.assertAlmostEqual(stats[key][field], value, msg=f"{key}.{field}")
        filtered = ep.cell_stats(self.store, "muse", task_type="code-change")
        self.assertEqual(set(filtered), set(summary))

    def test_order_and_observations_paginate(self):
        for i in range(1, 4):
            self.join_episode(f"ep-{i}", f"r-{i}")
        page = ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high",
                                  limit=2)
        self.assertEqual([e["candidate_seq"] for e in page], [1, 2])
        page = ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high",
                                  limit=2, offset=2)
        self.assertEqual([e["candidate_seq"] for e in page], [3])
        obs = ep.ready_observations(self.store, "muse", "code-change", "muse-spark/high",
                                    limit=1, offset=1)
        self.assertEqual([o["episode_id"] for o in obs], ["ep-2"])
        with self.assertRaises(ep.EpisodeError):
            ep.candidate_order(self.store, "muse", "code-change", "muse-spark/high",
                               limit=-1)

    def test_reconcile_is_bounded(self):
        for i in range(3):
            self.make_episode(eid=f"ep-{i}")
            ep.reserve(self.store, f"res-{i}", f"ep-{i}", 10.0)
        rec = ep.reconcile(self.store, limit=2)
        self.assertEqual(rec["pending_reservations_total"], 3)
        self.assertEqual(len(rec["pending_reservations"]), 2)
        self.assertEqual(rec["truncated"], True)
        full = ep.reconcile(self.store)
        self.assertEqual(full["truncated"], False)

    def test_generation_descriptor_canonical_and_collision_free(self):
        same_a = ep.create_episode(
            self.store, "ep-a", "muse", "code-change",
            [{"milestone": "done", "credit": 1.0}], "r1",
            context={"band": "small|open", "frozen": True})
        same_b = ep.create_episode(
            self.store, "ep-b", "muse", "code-change",
            [{"milestone": "done", "credit": 1.0}], "r1",
            context={"frozen": True, "band": "small|open"})
        self.assertEqual(same_a["generation"], same_b["generation"])
        other = ep.create_episode(
            self.store, "ep-c", "muse", "code-change",
            [{"milestone": "done", "credit": 1.0}], "r1",
            context={"band": "small", "frozen": "open|x"})
        self.assertNotEqual(same_a["generation"], other["generation"])
        for row in (same_a, same_b, other):
            json.loads(row["generation"])
        with self.assertRaises(ep.EpisodeError):
            ep.create_episode(self.store, "ep-d", "muse", "code-change",
                              [{"milestone": "done", "credit": 1.0}], "r1",
                              context={"bad": object()})

    def test_concurrent_registrations_serialize_ownership(self):
        """Two-connection race: one initial owner, unique seqs, one allocation winner."""
        import threading
        path = Path(self._tmp.name) / "race.sqlite3"
        seed = ep.open_store(path)
        try:
            seed_ep = lambda eid: ep.create_episode(
                seed, eid, "muse", "code-change",
                [{"milestone": "done", "credit": 1.0}], "r1", route="muse-broker")
            seed_ep("ep-a")
            seed_ep("ep-b")
            ep.record_master_segment(seed, "m1", "codex",
                                     raw(total_in=80, total_out=20))
        finally:
            seed.close()
        barrier = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def worker(name: str, episode_id: str, run_id: str, alloc_key: str):
            store = ep.open_store(path)
            try:
                barrier.wait(timeout=30)
                launch = ep.register_launch(
                    store, run_id, episode_id, "muse-spark", "high", "muse-broker",
                    "h1", ["muse-spark/high"], release=MUSE_RELEASE)
                try:
                    ep.allocate_shared(store, alloc_key, episode_id, "m1", 0.6)
                    alloc = "won"
                except ep.EpisodeError:
                    alloc = "lost"
                outcomes[name] = (launch["is_fallback"], launch["candidate_seq"], alloc)
            except Exception as exc:  # noqa: BLE001 - re-raised on the main thread
                outcomes[name] = exc
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=("t1", "ep-a", "r-a", "aa")),
                   threading.Thread(target=worker, args=("t2", "ep-b", "r-b", "ab"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for name in ("t1", "t2"):
            self.assertNotIsInstance(outcomes[name], Exception, outcomes[name])
        # Same episode would elect exactly one initial owner; here two
        # episodes share one cell, so the sequences are unique across them.
        seqs = sorted(o[1] for o in outcomes.values())
        self.assertEqual(seqs, [1, 2])
        inits = [o for o in outcomes.values() if o[0] == 0]
        self.assertEqual(len(inits), 2)
        # Both allocations cannot win: weights would total 1.2 > 1.
        self.assertEqual(sorted(o[2] for o in outcomes.values()), ["lost", "won"])

    def test_concurrent_first_launches_elect_single_owner(self):
        import threading
        path = Path(self._tmp.name) / "race2.sqlite3"
        seed = ep.open_store(path)
        try:
            ep.create_episode(seed, "ep-1", "muse", "code-change",
                              [{"milestone": "done", "credit": 1.0}], "r1",
                              route="muse-broker")
        finally:
            seed.close()
        barrier = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def worker(name: str, run_id: str):
            store = ep.open_store(path)
            try:
                barrier.wait(timeout=30)
                launch = ep.register_launch(
                    store, run_id, "ep-1", "muse-spark", "high", "muse-broker",
                    "h1", ["muse-spark/high"], release=MUSE_RELEASE)
                outcomes[name] = (launch["is_fallback"], launch["candidate_seq"])
            except Exception as exc:  # noqa: BLE001 - re-raised on the main thread
                outcomes[name] = exc
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=("t1", "r-1")),
                   threading.Thread(target=worker, args=("t2", "r-2"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for name in ("t1", "t2"):
            self.assertNotIsInstance(outcomes[name], Exception, outcomes[name])
        # Exactly one initial owner; the loser is a fallback with no sequence.
        self.assertEqual(sorted(outcomes.values()), [(0, 1), (1, None)])
        check = ep.open_store(path)
        try:
            row = check.conn.execute(
                "SELECT owner_option, owner_seq FROM episodes WHERE episode_id='ep-1'"
            ).fetchone()
            self.assertEqual((row["owner_option"], row["owner_seq"]),
                             ("muse-spark/high", 1))
        finally:
            check.close()

    def test_stale_closure_replay_never_closes_new_evidence(self):
        self.make_episode()
        self.launch("r1", "ep-1")
        ep.record_usage(self.store, "s1", "ep-1", raw(total_in=60, total_out=40),
                        run_id="r1")
        self.verify("s-v", "ep-1", "r1")
        ep.record_attempt(self.store, "att1", "r1", "completed")
        ep.mark_run_usage_final(self.store, "r1")
        self.accept("a1", "ep-1", run_id="r1")
        ep.finalize_episode(self.store, "ep-1", "master:x", "events#1",
                            event_id="close-1")
        # New evidence reopens the episode...
        ep.record_usage(self.store, "s-tail", "ep-1",
                        raw(total_in=5, total_out=5), run_id="r1")
        self.assertEqual(ep.get_episode(self.store, "ep-1")["status"], "open")
        # ...and reusing the old closure id on the new revision conflicts
        # instead of validating: no mutation, still open.
        with self.assertRaises(ep.ConflictError):
            ep.finalize_episode(self.store, "ep-1", "master:x", "events#1",
                                event_id="close-1")
        self.assertEqual(ep.get_episode(self.store, "ep-1")["status"], "open")
        # The old mark is stale for the new row; only a fresh mark re-closes.
        with self.assertRaises(ep.EpisodeError):
            ep.finalize_episode(self.store, "ep-1", "master:x")
        ep.mark_run_usage_final(self.store, "r1")
        closed = ep.finalize_episode(self.store, "ep-1", "master:x")
        self.assertEqual(closed["status"], "closed")

    def test_foreign_keys_enforced(self):
        self.assertEqual(
            self.store.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_unknown_store_layout_refused_without_mutation(self):
        import sqlite3 as _sqlite3
        foreign = Path(self._tmp.name) / "foreign.sqlite3"
        handle = _sqlite3.connect(str(foreign))
        handle.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        handle.commit()
        handle.close()
        with self.assertRaises(ep.EpisodeError):
            ep.open_store(foreign)
        reopened = _sqlite3.connect(str(foreign))
        try:
            tables = {r[0] for r in reopened.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertEqual(tables, {"other"})
        finally:
            reopened.close()

    def test_prefix_survives_restart(self):
        self.make_episode(eid="ep-1")
        self.make_episode(eid="ep-2")
        self.launch("r1", "ep-1")
        path = self.store.path
        self.store.close()
        reopened = ep.open_store(path)
        try:
            launch = ep.register_launch(
                reopened, "r2", "ep-2", "muse-spark", "high", "muse-broker", "h1",
                ["muse-spark/high"], release=MUSE_RELEASE)
            self.assertEqual(launch["candidate_seq"], 2)
        finally:
            reopened.close()
            self.store = ep.open_store(path)

    def test_spend_entries_validated(self):
        self.make_episode()
        with self.assertRaises(ep.EpisodeError):
            ep.record_spend(self.store, "sp-neg", "ep-1", -5.0, "usage")
        with self.assertRaises(ep.EpisodeError):
            ep.record_spend(self.store, "sp-bool", "ep-1", True, "usage")
        with self.assertRaises(ep.EpisodeError):
            ep.record_spend(self.store, "sp-inf", "ep-1", math.inf, "usage")
        with self.assertRaises(ep.EpisodeError):
            ep.record_spend(self.store, "sp-kind", "ep-1", 5.0, " ")
        ep.record_spend(self.store, "sp-ok", "ep-1", None, "verification")
        acc = ep.episode_accounting(self.store, "ep-1")
        self.assertEqual(acc["spend_missing_entries"], 1)

    def test_no_confidence_or_backoff_surface(self):
        """Stage 1/2 exposes evidence only; selection math is frozen later."""
        for name in ("confidence", "backoff", "promote", "demote", "incumbent",
                     "explore", "streak", "interval", "efficiency"):
            self.assertFalse(hasattr(ep, name), name)


class AntigravityClaudeOptionTest(unittest.TestCase):
    """Claude models served through agy are Antigravity options, not Claude Code ones."""

    def test_agy_claude_families_are_separate_options(self) -> None:
        for family in ("claude-opus-5-5", "claude-sonnet-5-5"):
            for effort in ("low", "medium", "high"):
                self.assertEqual(
                    ep.check_capability("antigravity", family, effort, "antigravity-run"),
                    (True, "eligible"),
                )
            self.assertFalse(ep.check_capability("antigravity", family, "xhigh", "antigravity-run")[0])
            self.assertFalse(ep.check_capability("claude-code", family, "high", "claude-agent-tool")[0])
        self.assertFalse(ep.check_capability("antigravity", "opus", "high", "antigravity-run")[0])


if __name__ == "__main__":
    unittest.main()
