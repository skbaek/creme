import math
import unittest

from creme.model_fit_efficiency import (
    Budget, Candidate, EfficiencyError, Moments, choose, efficiency_interval, quota,
)


class EfficiencyTests(unittest.TestCase):
    def test_aggregate_ratio_includes_expensive_failure(self):
        m = Moments().add(1, 10).add(0, 990)
        self.assertEqual(m.efficiency, 0.001)
        self.assertEqual(m.tokens, 1000)
        self.assertAlmostEqual(m.tokens_m2, 480200)

    def test_incomplete_and_invalid_usage_cannot_be_samples(self):
        for value in (None, -1, math.inf, math.nan, True):
            with self.assertRaises(EfficiencyError):
                Moments().add(1, value)
        with self.assertRaises(EfficiencyError):
            Moments().add(1.1, 100)

    def test_three_lucky_successes_cannot_certify_promotion(self):
        m = Moments()
        for _ in range(3):
            m = m.add(1, 1)
        i = efficiency_interval(Candidate("cheap", 100, 1, "enforced"), m, 2)
        self.assertEqual(i.lower, 0)
        self.assertEqual(i.upper, math.inf)

    def test_cost_violation_is_charged_and_invalidates_confidence(self):
        c = Candidate("a", 100, 10, "assumed")
        m = Moments().add(1, 1001)
        interval = efficiency_interval(c, m, 1)
        self.assertEqual(m.tokens, 1001)
        self.assertEqual(interval.status, "cost-bound-violated")
        self.assertEqual(interval.upper, math.inf)

    def test_unknown_bound_never_becomes_confidence(self):
        c = Candidate("a", None, 10)
        m = Moments()
        for _ in range(10000):
            m = m.add(1, 10)
        self.assertEqual(efficiency_interval(c, m, 1).status, "cost-bound-unavailable")

    def test_shrinks_around_ratio_without_work_cost_independence(self):
        c = Candidate("a", 100, 20, "assumed")
        m = Moments()
        for i in range(100000):
            m = m.add(i % 2, 10 if i % 2 else 90)
        interval = efficiency_interval(c, m, 3)
        self.assertLess(interval.lower, .01)
        self.assertGreater(interval.upper, .01)
        self.assertLess(interval.upper - interval.lower, .003)

    def decision(self, **overrides):
        args = dict(
            candidates=[Candidate("default", 100, 100, "enforced"),
                        Candidate("expensive-prior", 100, 100000, "enforced")],
            moments={}, incumbent="default", opportunity=10, last_exploration=0,
            exploration_count=0, trials={"default": 9}, pending={},
            budget=Budget(100, 0, 0, 0),
        )
        args.update(overrides)
        return choose(**args)

    def test_expensive_prior_gets_protected_trial(self):
        result = self.decision()
        self.assertEqual(result.selected, "expensive-prior")
        self.assertTrue(result.exploration)
        self.assertFalse(result.demonstrated_productive_route)
        # Protected debt must win even when focused priority has a cheaper
        # unresolved alternative. Two candidates alone cannot distinguish the
        # protected scheduler from an incidental focused choice.
        candidates = [Candidate(key, 100, prior, "enforced") for key, prior in
                      (("default", 100), ("cheap", 1), ("expensive-prior", 100000))]
        result = self.decision(candidates=candidates, trials={"default": 9, "cheap": 2})
        self.assertEqual(result.selected, "expensive-prior")

    def test_budget_pending_unknown_and_spacing_defer_without_erasing_debt(self):
        for budget in (Budget(99, 0, 0, 0), Budget(100, 1, 0, 0),
                       Budget(100, 0, 100, 1), Budget(100, 0, 0, 0, unknown_spend=True)):
            self.assertFalse(self.decision(budget=budget).exploration)
        self.assertFalse(self.decision(opportunity=9).exploration)
        self.assertFalse(self.decision(pending={"expensive-prior": 1}).exploration)
        # No internal state is mutated by refusal; the same debt becomes
        # actionable once the external reservation/budget is available.
        self.assertTrue(self.decision().exploration)

    def test_prediction_only_is_not_a_strict_cap(self):
        c = [Candidate("default", None, 100), Candidate("expensive-prior", None, 10)]
        result = self.decision(candidates=c)
        self.assertEqual(result.spending_guarantee, "expected-only")
        self.assertEqual(result.reservation, 10)

    def test_foreign_cell_cannot_claim_productivity(self):
        result = self.decision(moments={"other-client": Moments().add(1, 10)})
        self.assertFalse(result.demonstrated_productive_route)

    def test_violated_cap_is_not_reserved_again(self):
        result = self.decision(moments={"expensive-prior": Moments().add(1, 1001)})
        self.assertEqual(result.selected, "default")
        self.assertFalse(result.exploration)

    def test_violated_incumbent_gets_explicit_operational_fallback(self):
        result = self.decision(moments={"default": Moments().add(1, 1001)}, opportunity=1)
        self.assertEqual(result.incumbent, "expensive-prior")
        self.assertIn("operational-fallback", result.reason)
        self.assertIn("provisional", result.reason)
        with self.assertRaisesRegex(EfficiencyError, "every candidate"):
            self.decision(moments={key: Moments().add(1, 1001)
                                   for key in ("default", "expensive-prior")})

    def test_unknown_focused_candidates_still_use_soft_cost_prior(self):
        cs = [Candidate(key, 100, prior, "enforced") for key, prior in
              (("default", 100), ("a-cheap", 1), ("b-pricey", 1000000))]
        result = self.decision(candidates=cs, exploration_count=1)
        self.assertEqual(result.selected, "a-cheap")

    def test_boolean_clocks_and_fractional_pending_are_refused(self):
        for override in ({"last_exploration": True}, {"exploration_count": False},
                         {"budget": Budget(100, 0, 0, .5)}):
            with self.assertRaises(EfficiencyError):
                self.decision(**override)

    def test_sampling_targets_are_growing_and_sublinear(self):
        for focused in (False, True):
            self.assertGreater(quota(10**12, focused), quota(10**6, focused))
            self.assertLess(quota(10**12, focused) / 10**12, 1e-6)

    def test_wrong_prior_native_sol_optimum_is_discovered(self):
        candidates = [Candidate("astra/medium", 100, 100, "enforced"),
                      Candidate("sol6.1/high", 100, 100000, "enforced")]
        moments = {c.identity: Moments() for c in candidates}
        trials = {key: 0 for key in moments}
        incumbent = "astra/medium"
        last = explorations = switches = 0
        spent = 0
        for opportunity in range(1, 20001):
            decision = choose(
                candidates, moments, incumbent, opportunity=opportunity,
                last_exploration=last, exploration_count=explorations,
                trials=trials, pending={}, budget=Budget(10000000, spent, 0, 0),
            )
            switches += decision.incumbent != incumbent
            incumbent = decision.incumbent
            cost = 10 if decision.selected == "sol6.1/high" else 100
            if decision.exploration:
                self.assertGreaterEqual(opportunity - last, 10)
                last = opportunity
                explorations += 1
                spent += cost
            trials[decision.selected] += 1
            moments[decision.selected] = moments[decision.selected].add(1, cost)
        self.assertEqual(incumbent, "sol6.1/high")
        self.assertEqual(switches, 1)
        self.assertLessEqual(explorations, 2000)
        self.assertGreater(trials["sol6.1/high"], trials["astra/medium"])
        self.assertEqual(sum(m.n for m in moments.values()), 20000)


if __name__ == "__main__":
    unittest.main()
