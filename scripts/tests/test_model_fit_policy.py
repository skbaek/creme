import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from creme import model_fit_policy as policy


class ModelFitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def recommend(self, task="code-change", default="fable/high"):
        return policy.recommend(self.directory, "claude-code", task, default=default)

    def state(self):
        return json.loads(
            (self.directory / "state" / "claude-code.json").read_text(encoding="utf-8")
        )

    def write_state(self, state):
        path = self.directory / "state" / "claude-code.json"
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def outcome(self, dispatch, verdict="pass", tokens=None):
        return policy.outcome(
            self.directory, "claude-code", dispatch["dispatch_id"], verdict, tokens=tokens
        )

    def test_cold_start_requires_default_and_sets_incumbent(self):
        with self.assertRaisesRegex(policy.PolicyError, "no recommendation yet"):
            self.recommend(default=None)
        result = self.recommend()
        self.assertEqual(result["recommended"], "fable/high")
        state = self.state()
        task = state["task_types"]["code-change"]
        self.assertEqual(task["recommended"], "fable/high")
        self.assertEqual(task["known_good"], "fable/high")
        self.assertEqual(state["weights"]["family"]["fable"], 5.0)

    def test_no_probe_before_min_gap(self):
        results = [self.recommend() for _ in range(3)]
        self.assertEqual([result["probe"] for result in results], [False, False, False])
        self.assertTrue(all(result["option"] == "fable/high" for result in results))

    def test_cheapest_due_candidate_wins_and_expensive_never_candidate(self):
        results = [self.recommend() for _ in range(4)]
        self.assertEqual(results[-1]["option"], "sonnet/low")
        self.assertTrue(results[-1]["probe"])
        self.assertNotIn("opus/low", results[-1]["reason"])
        self.assertEqual(self.outcome(results[-1])["event"], "n-updated")

    def test_starvation_guard_beats_cheaper_due_candidate(self):
        self.recommend()
        state = self.state()
        task = state["task_types"]["code-change"]
        task["since_probe"] = policy.MIN_GAP
        task["cells"]["sonnet/low"]["count"] = 4
        task["cells"]["sonnet/low"]["n"] = 4
        task["cells"]["opus/low"]["count"] = 8
        task["cells"]["opus/low"]["n"] = 4
        self.write_state(state)
        result = self.recommend()
        self.assertEqual(result["option"], "opus/low")
        self.assertIn("starvation guard", result["reason"])

    def test_probe_fail_pass_and_promotion(self):
        self.recommend()
        state = self.state()
        task = state["task_types"]["code-change"]
        task["since_probe"] = policy.MIN_GAP
        task["cells"]["sonnet/low"]["count"] = 3
        task["cells"]["sonnet/low"]["n"] = 4
        self.write_state(state)
        probe = self.recommend()
        failed = self.outcome(probe, "fail")
        self.assertEqual(failed["n"], 64)

        state = self.state()
        task = state["task_types"]["code-change"]
        task["since_probe"] = policy.MIN_GAP
        task["cells"]["sonnet/low"]["count"] = 63
        self.write_state(state)
        probe = self.recommend()
        passed = self.outcome(probe, "pass")
        self.assertEqual(passed["n"], 32)

        state = self.state()
        task = state["task_types"]["code-change"]
        task["since_probe"] = policy.MIN_GAP
        task["cells"]["sonnet/low"]["n"] = 1
        task["cells"]["sonnet/low"]["count"] = 1
        self.write_state(state)
        probe = self.recommend()
        promoted = self.outcome(probe, "pass")
        self.assertEqual(promoted["event"], "promoted")
        self.assertEqual(promoted["recommended"], "sonnet/low")
        state = self.state()
        task = state["task_types"]["code-change"]
        self.assertEqual(task["known_good"], "fable/high")
        self.assertEqual(task["cells"]["opus/low"]["n"], policy.N_AFTER_PROMOTION)
        self.assertEqual(task["cells"]["opus/low"]["count"], 0)

    def _prepare_ordinary_failures(self, known_good):
        self.recommend(default="sonnet/low")
        state = self.state()
        task = state["task_types"]["code-change"]
        task["known_good"] = known_good
        task["recommended"] = "sonnet/low"
        self.write_state(state)

    def test_demotion_returns_to_known_good_or_more_expensive(self):
        self._prepare_ordinary_failures("fable/high")
        first = self.outcome(self.recommend(), "fail")
        self.assertEqual(first["event"], "recorded")
        self.outcome(self.recommend(), "pass")
        demoted = self.outcome(self.recommend(), "fail")
        self.assertEqual(demoted["event"], "demoted")
        self.assertEqual(demoted["recommended"], "fable/high")
        self.assertIsNone(self.state()["task_types"]["code-change"]["known_good"])
        self.assertEqual(self.state()["task_types"]["code-change"]["cells"]["sonnet/low"]["n"], policy.N_AFTER_DEMOTION)

        self._prepare_ordinary_failures(None)
        self.outcome(self.recommend(), "fail")
        self.outcome(self.recommend(), "pass")
        demoted = self.outcome(self.recommend(), "fail")
        self.assertEqual(demoted["event"], "demoted")
        self.assertEqual(demoted["recommended"], "opus/low")

    def test_measured_cost_uses_median_of_last_five(self):
        self.recommend(default="sonnet/low")
        for tokens in (100000, 300000):
            self.outcome(self.recommend(), "pass", tokens)
        state = self.state()
        self.assertEqual(state["task_types"]["code-change"]["cells"]["sonnet/low"]["costs"], [1.0, 3.0])
        self.outcome(self.recommend(), "pass", 200000)
        state = self.state()
        self.assertEqual(
            policy._cost(state, policy.model_fit.CLIENTS["claude-code"], state["task_types"]["code-change"], "sonnet/low"),
            2.0,
        )
        for tokens in (50000, 400000, 600000, 700000):
            self.outcome(self.recommend(), "pass", tokens)
        state = self.state()
        costs = state["task_types"]["code-change"]["cells"]["sonnet/low"]["costs"]
        self.assertEqual(len(costs), policy.COST_WINDOW)
        self.assertEqual(policy.statistics.median(costs), 4.0)

    def test_pending_cap_and_unknown_dispatch(self):
        for _ in range(policy.PENDING_CAP + 8):
            self.recommend()
        task = self.state()["task_types"]["code-change"]
        self.assertEqual(len(task["pending"]), policy.PENDING_CAP)
        with self.assertRaisesRegex(policy.PolicyError, "unknown dispatch"):
            policy.outcome(self.directory, "claude-code", "missing-dispatch", "pass")

    def test_state_remains_bounded_after_cycles(self):
        policy.set_recommendation(self.directory, "claude-code", "code-change", "sonnet/low")
        for _ in range(200):
            dispatch = self.recommend(default="sonnet/low")
            self.outcome(dispatch, "pass", tokens=100000)
        task = self.state()["task_types"]["code-change"]
        self.assertLessEqual(len(task["pending"]), policy.PENDING_CAP)
        self.assertTrue(all(len(cell["costs"]) <= policy.COST_WINDOW for cell in task["cells"].values()))

    def test_cli_recommend_outcome_round_trip(self):
        env = dict(__import__("os").environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[2])
        command = [sys.executable, "-m", "creme", "model-fit"]
        recommended = subprocess.run(
            command + ["recommend", "claude-code", "code-change", "--default", "sonnet/low", "--dir", str(self.directory)],
            text=True, capture_output=True, env=env, check=False,
        )
        self.assertEqual(recommended.returncode, 0, recommended.stderr)
        self.assertIn("use=sonnet/low", recommended.stdout)
        dispatch = recommended.stdout.split("dispatch=", 1)[1].splitlines()[0]
        outcome = subprocess.run(
            command + ["outcome", "claude-code", dispatch, "pass", "--dir", str(self.directory)],
            text=True, capture_output=True, env=env, check=False,
        )
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertIn("event=recorded", outcome.stdout)


if __name__ == "__main__":
    unittest.main()
