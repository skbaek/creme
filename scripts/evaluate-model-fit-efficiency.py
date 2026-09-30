#!/usr/bin/env python3
"""Seeded equal-token-budget evaluation with known synthetic populations.

No model calls. Work/cost outcomes belong to complete fixed recovery recipes.
Every policy pays attempt, verification and fallback costs; no free recovery.
Results are engineering experiments, not measurements of real model quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creme.model_fit_efficiency import Budget, Candidate, Moments, choose


@dataclass(frozen=True)
class Recipe:
    name: str
    attempt: int
    verification: int
    success: float
    fallback: int = 0
    fallback_success: float = 1.0
    prior: int = 100
    latency: float = 1.0

    @property
    def bound(self):
        return self.attempt + self.verification + self.fallback

    @property
    def efficiency(self):
        work = self.success + (1 - self.success) * self.fallback_success if self.fallback else self.success
        cost = self.attempt + self.verification + (1 - self.success) * self.fallback
        return work / cost

    def outcome(self, seed, opportunity):
        # Candidate-independent task draw: fixed task stream across policies.
        salt = hashlib.sha256(f"{seed}:{opportunity}".encode()).digest()
        rng = random.Random(int.from_bytes(salt, "big"))
        first = rng.random() < self.success
        recovered = not first and self.fallback and rng.random() < self.fallback_success
        return (float(first or recovered), self.attempt + self.verification
                + (self.fallback if not first else 0), not first)


SCENARIOS = {
    "wrong-expensive-prior": [Recipe("initial", 90, 10, 1), Recipe("hidden", 8, 2, 1, prior=100000)],
    "equal-priors": [Recipe("initial", 90, 10, 1), Recipe("hidden", 8, 2, 1)],
    "lower-first-success-better-episode": [Recipe("direct", 90, 10, 1), Recipe("cheap-first", 8, 2, .6, 90)],
    "poor-yield": [Recipe("initial", 90, 10, 1), Recipe("cheap-failure", 8, 2, .01)],
    "zero-work": [Recipe("initial", 90, 10, 0), Recipe("cheap-failure", 8, 2, 0)],
    "rare-expensive-recovery": [Recipe("initial", 40, 10, 1), Recipe("cheap-first", 8, 2, .99, 5000)],
    "native-sol-high": [Recipe("astra/medium", 90, 10, 1), Recipe("sol6.1/high", 8, 2, 1, prior=100000)],
    # This exercises delayed inference with Muse-shaped identities; actual
    # cross-client clock/attribution is an adapter integration test, not this
    # pure simulation's claim.
    "muse-delayed-feedback": [Recipe("muse/medium", 90, 10, 1), Recipe("muse/high", 8, 2, 1, prior=100000)],
}


def simulate(recipes, seed, token_budget, policy="learn", delay=0):
    by_id = {r.name: r for r in recipes}
    # Common declared cap: do not hand the learner the hidden optimum's
    # smaller true maximum as an oracle-provided confidence advantage.
    max_bound = max(r.bound for r in recipes)
    candidates = [Candidate(r.name, max_bound, r.prior, "enforced") for r in recipes]
    moments = {r.name: Moments() for r in recipes}
    trials = dict.fromkeys(by_id, 0)
    ready = {r.name: {} for r in recipes}
    next_seq = dict.fromkeys(by_id, 1)
    pending = []
    incumbent = recipes[0].name
    oracle = max(recipes, key=lambda r: r.efficiency).name
    spent = work = learning_spent = normal_spent = elapsed = 0
    last_explore = explorations = switches = failures = opportunities = 0
    reserved = 0
    first_switch = None
    # Reserve the complete worst-case episode before launch for all policies.
    # This leaves at most max_bound unspent; actual work is compared at the
    # same budget, with unspent money explicitly retained in the report.
    while spent + max_bound <= token_budget:
        opportunities += 1
        delivered = [p for p in pending if p[0] <= opportunities]
        pending = [p for p in pending if p[0] > opportunities]
        for _, key, seq, accepted, cost, trial, allowance in delivered:
            ready[key][seq] = (accepted, cost)
            if trial:
                reserved -= allowance
        for key in by_id:
            while next_seq[key] in ready[key]:
                accepted, cost = ready[key].pop(next_seq[key])
                moments[key] = moments[key].add(accepted, cost)
                next_seq[key] += 1
        trial = False
        if policy == "oracle":
            selected = oracle
        elif policy == "default":
            selected = recipes[0].name
        else:
            pending_trials = sum(p[5] for p in pending)
            # Small finite seed allowance avoids cold-start deadlock. Beyond
            # it, at most 10% of incurred ordinary spend funds exploration.
            budget = Budget(2 * max_bound + .1 * normal_spent, learning_spent,
                            reserved, pending_trials, max_pending=1)
            decision = choose(candidates, moments, incumbent,
                              opportunity=opportunities, last_exploration=last_explore,
                              exploration_count=explorations, trials=trials,
                              pending={}, budget=budget)
            selected = decision.selected
            if incumbent != decision.incumbent:
                switches += 1
                if first_switch is None:
                    first_switch = spent
            incumbent = decision.incumbent
            trial = decision.exploration
        recipe = by_id[selected]
        accepted, cost, failed = recipe.outcome(seed, opportunities)
        work += accepted
        spent += cost
        elapsed += recipe.latency
        failures += int(failed)
        if trial:
            explorations += 1
            last_explore = opportunities
            learning_spent += cost
        else:
            normal_spent += cost
        trials[selected] += 1
        # Costs are known at completion here, but evidence delay simulates
        # out-of-order joining. Account observed spend immediately. Retain
        # no remaining cost reservation once the full observed cost is paid.
        due = opportunities + (delay * (2 if failed else 1))
        pending.append((due, selected, trials[selected], accepted, cost, trial, 0))
    return dict(work=work, tokens=spent, unused_tokens=token_budget-spent,
                work_per_token=work/spent if spent else 0, switches=switches,
                exploration_tokens=learning_spent, exploratory_episodes=explorations,
                failures=failures, opportunities=opportunities, latency=elapsed,
                unjoined_at_end=len(pending), first_switch_tokens=first_switch,
                final_incumbent=incumbent, expected_best=oracle, trials=trials)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, default=1000000)
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.budget <= 0 or args.seeds <= 0:
        parser.error("budget and seeds must be positive")
    output = {"synthetic_only": True, "budget_per_policy": args.budget, "seeds": args.seeds,
              "scenarios": {}}
    for name, recipes in SCENARIOS.items():
        rows = []
        for seed in range(args.seeds):
            learn = simulate(recipes, seed, args.budget, delay=3 if name == "muse-delayed-feedback" else 0)
            oracle = simulate(recipes, seed, args.budget, "oracle")
            default = simulate(recipes, seed, args.budget, "default")
            rows.append(dict(seed=seed, learn=learn, oracle=oracle, default=default,
                             work_gain_over_default=learn["work"]-default["work"],
                             work_shortfall_to_oracle=oracle["work"]-learn["work"]))
        output["scenarios"][name] = dict(
            mean_work_gain=statistics.mean(r["work_gain_over_default"] for r in rows),
            mean_oracle_shortfall=statistics.mean(r["work_shortfall_to_oracle"] for r in rows),
            rows=rows)
    rendered = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
        for name, result in output["scenarios"].items():
            print(f"{name}: gain={result['mean_work_gain']:.1f}, oracle_shortfall={result['mean_oracle_shortfall']:.1f}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
