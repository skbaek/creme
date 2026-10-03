"""Independent adapter/CLI controls using metadata-only temporary evidence."""
from __future__ import annotations

import copy
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from creme import cli
from creme import model_fit_adapters as A
from creme import model_fit_capture as C
from creme import model_fit_episodes as E
from creme import model_fit_runtime as R
from creme.pseudo_broker import SessionRecord
from scripts.tests.test_model_fit_runtime import base_config, accept_receipt


def context(model="gpt-6.1-sol", effort="high", shape="effort"):
    payload = {"model": model}
    if shape == "collaboration":
        payload["collaboration_mode"] = {"settings": {"reasoning_effort": effort}}
    else:
        payload[shape] = effort
    return {"type": "turn_context", "payload": payload}


def counter(incoming, outgoing):
    return {"type": "event_msg", "timestamp": "fixture-time", "payload": {
        "type": "token_count", "info": {"total_token_usage": {
            "input_tokens": incoming, "cached_input_tokens": incoming // 2,
            "output_tokens": outgoing, "reasoning_output_tokens": outgoing // 2,
            "total_tokens": incoming + outgoing}}}}


class AdapterControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.store = R.open_runtime(self.directory)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def rollout(self, name, *rows):
        path = self.directory / (name + ".jsonl")
        self.append(path, *rows)
        return path

    def append(self, path, *rows):
        with path.open("a") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def native(self, path, episode="ep", run="run", **extra):
        return A.codex_run(path, episode, run, "sol", "codex-subagent", "h1",
                           "completed", **extra)

    def prepare(self, client="codex", episode="ep"):
        muse = client == "muse"
        option = "muse-spark/high" if muse else "sol/high"
        config = base_config(execution_client=client, default=option, candidates=[{
            "option": option, "prior_tokens": 100,
            "release": "muse-spark-1.3" if muse else "gpt-6.1-sol",
            "recipe_version": "r1", "route": "muse-broker" if muse else "codex-subagent"}])
        R.configure(self.store, "policy", config, "active")
        R.prepare(self.store, episode, "policy", [{"milestone": "done", "credit": 1}],
                  "codex", "fixture-opportunity")
        return config

    def acceptance(self, episode="ep", **extra):
        receipt = accept_receipt(episode, **extra)
        for segment in receipt["master_segments"]:
            segment["client"] = "codex"
        return receipt

    def session(self):
        self.prepare("muse")
        session = SessionRecord()
        session.dir = self.directory / "broker"
        session.dir.mkdir()
        session.data_lock = threading.RLock()
        session.seq = 0
        session.record = {"id": "session", "model": "muse-spark-1.3", "effort": "high",
                          "model_fit": A.binding(self.directory, "ep"), "turns": [{
                              "n": 1, "turn_id": "turn", "status": "completed", "verdict": "PASS",
                              "models": {"muse-spark-1.3": 1}, "tokens": {
                                  "prompt": 100, "cached": 90, "output": 20,
                                  "reasoning": 15, "total": 120}}]}
        return session

    def test_native_effective_release_and_effort_are_observed(self):
        for shape in ("effort", "reasoning_effort", "collaboration"):
            with self.subTest(shape=shape):
                path = self.rollout(shape, context(shape=shape), counter(100, 20))
                receipt = self.native(path)
                self.assertEqual((receipt["release"], receipt["option"]),
                                 ("gpt-6.1-sol", "sol/high"))
                usage = receipt["segments"][0]["usage"]
                self.assertEqual((usage["total_input"], usage["total_output"]), (100, 20))
                self.assertEqual(usage["observed_effort"], "high")
                self.assertEqual(receipt["usage_evidence"], str(path.resolve()))
                self.assertTrue(receipt["usage_complete"])

    def test_native_metadata_gaps_and_family_mismatch_refuse(self):
        for name, metadata in (("no-context", None), ("no-effort", {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}),
                               ("retired", context("gpt-6-sol")), ("wrong-family", context("gpt-6-astra"))):
            with self.subTest(name=name):
                path = self.rollout(name, *([metadata] if metadata else []), counter(100, 20))
                with self.assertRaises(C.CaptureError):
                    self.native(path)

    def test_luna_reserve_rollout_imports_its_observed_reserve_release(self):
        path = self.rollout("reserve", context("gpt-reserve", "medium"), counter(100, 20))
        receipt = A.codex_run(path, "ep", "run", "luna-reserve", "luna-reserve-broker", "h1",
                              "completed")
        self.assertEqual((receipt["release"], receipt["option"]),
                         ("gpt-reserve", "luna-reserve/medium"))
        with self.assertRaises(C.CaptureError):
            self.native(path)

    def test_native_identity_drift_within_measured_window_refuses(self):
        for name, changed in (("model", context("gpt-6-astra")), ("effort", context(effort="low"))):
            with self.subTest(name=name):
                path = self.rollout(name, context(), counter(100, 20))
                before = C.codex_snapshot(path)
                self.append(path, changed, counter(110, 25))
                for base in (None, before):
                    with self.subTest(resumed=base is not None), self.assertRaisesRegex(C.CaptureError, "changed model/effort"):
                        self.native(path, before=base)

    def test_resumed_window_ignores_identity_change_before_saved_base(self):
        path = self.rollout("historical", context("gpt-6-astra", "low"), counter(80, 10),
                            context(), counter(100, 20))
        before = C.codex_snapshot(path)
        self.assertTrue(before["identity_changed"])
        self.append(path, context(), counter(110, 25))
        receipt = self.native(path, before=before)
        usage = receipt["segments"][0]["usage"]
        self.assertEqual((receipt["release"], receipt["option"]), ("gpt-6.1-sol", "sol/high"))
        self.assertEqual((usage["total_input"], usage["total_output"]), (10, 5))
        self.assertTrue(before["identity_changed"], "capture must not mutate saved historical evidence")

    def test_resumed_base_excludes_prior_cost_and_adjacent_review_joins(self):
        self.prepare()
        path = self.rollout("resumed", context(), counter(1000, 200))
        before = C.codex_snapshot(path)
        self.append(path, counter(1010, 204))
        receipt = self.native(path, before=before)
        self.assertEqual(R.submit(self.store, receipt)["status"], "applied")
        middle = C.codex_snapshot(path, before)
        self.append(path, counter(1013, 206))
        after = C.codex_snapshot(path, middle)
        acceptance = self.acceptance(master_segments=[A.master_window(middle, after, "codex")])
        self.assertEqual(R.submit(self.store, acceptance)["status"], "applied")
        self.assertEqual(E.get_episode(self.store, "ep")["status"], "closed")
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 19)

    def test_adapter_measured_overlap_cannot_fund_another_worker_or_master(self):
        self.prepare()
        path = self.rollout("overlap", context(), counter(100, 20))
        before = C.codex_snapshot(path)
        self.append(path, counter(110, 25))
        original = self.native(path, before=before)
        self.assertEqual(R.submit(self.store, original)["status"], "applied")
        self.append(path, counter(120, 30))
        wider = self.native(path, before=before)
        result = R.submit(self.store, wider)
        self.assertEqual(result["status"], "pending")
        self.assertIn("overlaps", result["error"])
        after = C.codex_snapshot(path, before)
        acceptance = self.acceptance(master_segments=[A.master_window(before, after, "codex")])
        result = R.submit(self.store, acceptance)
        self.assertEqual(result["status"], "pending")
        self.assertIn("overlaps", result["error"])
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 15)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM master_segments").fetchone()[0], 0)

    def test_persist_automatically_captures_binding_and_retains_muse_auxiliary_gap(self):
        session = self.session()
        session.persist()
        durable = json.loads((session.dir / "session.json").read_text())
        self.assertEqual(durable["model_fit"], session.record["model_fit"])
        self.assertEqual(durable["model_fit_capture"], {"status": "captured", "inbox_pending": 0})
        launch = dict(self.store.conn.execute("SELECT * FROM launches").fetchone())
        self.assertEqual((launch["run_id"], launch["release"], launch["status"]),
                         ("session:turn", "muse-spark-1.3", "completed"))
        self.assertIsNone(launch["usage_final_count"])
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 120)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM acceptances").fetchone()[0], 0)
        count = self.store.conn.execute("SELECT COUNT(*) FROM fit_inbox").fetchone()[0]
        from unittest.mock import patch
        with patch.object(A, "broker_capture", wraps=A.broker_capture) as capture:
            session.persist()
            capture.assert_not_called()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM fit_inbox").fetchone()[0], count)
        restarted = SessionRecord()
        restarted.dir = session.dir
        restarted.record = durable
        restarted.data_lock = threading.RLock()
        restarted.seq = 0
        with patch.object(A, "broker_capture", wraps=A.broker_capture) as capture:
            restarted.persist()
            capture.assert_called_once()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM fit_inbox").fetchone()[0], count)
        R.submit(self.store, self.acceptance())
        self.assertNotEqual(E.get_episode(self.store, "ep")["status"], "closed")
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 130)
        config = R._policy(self.store, "policy")[1]
        self.assertEqual(R.statistics(self.store, config, config["candidates"][0]).n, 0)

    def test_persist_records_sqlite_capture_gap_without_losing_session(self):
        from unittest.mock import patch
        session = self.session()
        for error in (sqlite3.OperationalError("fixture locked"), E.EpisodeError("fixture conflict")):
            with self.subTest(error=type(error).__name__):
                with patch.object(A, "broker_capture", side_effect=error):
                    session.persist()
                durable = json.loads((session.dir / "session.json").read_text())
                self.assertEqual(durable["turns"][0]["verdict"], "PASS")
                self.assertEqual(durable["model_fit_capture"], {"status": "gap", "error": str(error)})
        session.persist()
        durable = json.loads((session.dir / "session.json").read_text())
        self.assertEqual(durable["model_fit_capture"], {"status": "captured", "inbox_pending": 0})
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 120)

    def test_missing_auxiliary_usage_recovers_with_evidence_without_reacceptance(self):
        session = self.session()
        session.persist()
        parent = json.loads(self.store.conn.execute(
            "SELECT payload FROM fit_inbox WHERE json_extract(payload,'$.kind')='run'").fetchone()[0])
        unknown = copy.deepcopy(parent)
        unknown.update(receipt_id="auxiliary-gap", segments=[{"id": "auxiliary", "usage": {}}])
        self.assertEqual(R.submit(self.store, unknown)["status"], "applied")
        R.submit(self.store, self.acceptance())
        self.assertEqual(E.episode_accounting(self.store, "ep")["usage_missing_segments"], 1)
        definitive = copy.deepcopy(parent)
        definitive.update(receipt_id="auxiliary-final", usage_complete=True,
                          usage_evidence="fixture-parent-plus-auxiliary",
                          segments=[{"id": "auxiliary", "usage": {"total_input": 6, "total_output": 4},
                                     "resolves_missing": True, "evidence": "fixture-auxiliary-provider"}])
        self.assertEqual(R.submit(self.store, definitive)["status"], "applied")
        self.assertEqual(E.get_episode(self.store, "ep")["status"], "closed")
        accounting = E.episode_accounting(self.store, "ep")
        self.assertEqual((accounting["usage_missing_segments"], accounting["spend_missing_entries"],
                          accounting["spend_uncapped_tokens"]), (0, 0, 140))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM acceptances").fetchone()[0], 1)
        retained = json.loads(self.store.conn.execute(
            "SELECT payload FROM fit_inbox WHERE receipt_id='auxiliary-gap'").fetchone()[0])
        self.assertEqual(retained["segments"][0]["usage"], {})
        self.assertEqual(R.submit(self.store, definitive)["status"], "applied")
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 140)

    def test_cli_one_normal_acceptance_imports_actual_worker_and_review_cost(self):
        config = self.prepare()
        worker = self.rollout("worker", context(shape="collaboration"), counter(100, 20))
        master = self.rollout("master", context("gpt-6-astra"), counter(1000, 200))
        before = C.codex_snapshot(master)
        self.append(master, counter(1005, 202))
        after = C.codex_snapshot(master, before)
        request = self.acceptance(master_segments=[])
        request.pop("kind")
        request.update(codex_sources=[{"path": str(worker), "episode_id": "ep", "run_id": "worker-run",
                                      "family": "sol", "route": "codex-subagent", "harness_version": "h1",
                                      "terminal": "completed"}],
                       codex_master_windows=[{"before": before, "after": after, "client": "codex"}])
        path = self.directory / "accept.json"
        path.write_text(json.dumps(request))
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["model-fit", "episode", "accept", "--dir", str(self.directory), "--from", str(path)])
        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertEqual(json.loads(output.getvalue())["status"], "applied")
        self.assertEqual(E.get_episode(self.store, "ep")["status"], "closed")
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 127)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM acceptances").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM fit_inbox").fetchone()[0], 1)
        moments = R.statistics(self.store, config, config["candidates"][0])
        self.assertEqual((moments.n, moments.work, moments.tokens), (1, 1, 127))

    def test_schema3_additive_upgrade_preserves_evidence_and_pending_payload(self):
        self.prepare()
        path = self.rollout("migration", context(), counter(100, 20))
        R.submit(self.store, self.native(path))
        R.submit(self.store, self.acceptance())
        pending = self.acceptance(receipt_id="legacy-pending", milestones=["undeclared"])
        self.assertEqual(R.submit(self.store, pending)["status"], "pending")
        db = self.store.path
        self.store.close()
        # Only the temporary fixture is downgraded to the historical two-column gap.
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE fit_inbox DROP COLUMN attempted_at")
        conn.execute("ALTER TABLE fit_opportunities DROP COLUMN reconciled_at")
        conn.execute("UPDATE meta SET value='3' WHERE key='schema'")
        conn.commit()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        baseline = {}
        columns = {}
        for table in tables:
            columns[table] = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            baseline[table] = sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
        conn.close()
        self.store = E.open_store(db)
        for table in tables:
            if table in ("events", "meta"):
                continue
            selected = ",".join(columns[table])
            rows = sorted((tuple(r) for r in self.store.conn.execute(f"SELECT {selected} FROM {table}")), key=repr)
            self.assertEqual(rows, baseline[table], table)
        old_events = {r[0]: r for r in baseline["events"]}
        new_events = {r[0]: tuple(r) for r in self.store.conn.execute("SELECT * FROM events")}
        self.assertEqual({k: new_events[k] for k in old_events}, old_events)
        self.assertEqual(set(new_events) - set(old_events), {"schema:3:4"})
        self.assertEqual(json.loads(new_events["schema:3:4"][2]),
                         {"from": 3, "to": 4, "evidence_preserved": True})
        self.assertEqual(self.store.conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], "4")
        self.assertEqual(self.store.conn.execute("SELECT attempted_at FROM fit_inbox WHERE receipt_id='legacy-pending'").fetchone()[0], "")
        self.assertEqual(self.store.conn.execute("SELECT reconciled_at FROM fit_opportunities").fetchone()[0], "")
        self.assertEqual(E.episode_accounting(self.store, "ep")["spend_uncapped_tokens"], 130)
        self.store.close()
        self.store = E.open_store(db)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM events WHERE event_id='schema:3:4'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
