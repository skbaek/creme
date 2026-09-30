"""Pure within-client efficiency estimates and bounded exploration decisions.

Persistence, actual launch, eligibility and usage reconciliation belong to the
episode/adapter layer. This module never interprets an absent observation as a
zero-cost failure. Inputs are cumulative moments of a *complete prefix* of
episodes in pre-outcome candidate order, within one fixed release/context and
recovery recipe. Partial episode spend is additionally charged by Budget.

The empirical Bernstein bound is Maurer--Pontil (COLT 2009), Theorem 4,
applied to work and bounded cost and to their complements. Spending
delta/(4*K*n*(n+1)) on each one-sided bound makes simultaneous repeated
inspection valid by a union bound. This requires independent representative
bounded episodes per candidate; it does not establish those assumptions.
Raw cost is never clipped. An exceeded or undeclared bound gives no confidence
claim. Predicted reservations never become strict spending guarantees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence


class EfficiencyError(ValueError):
    pass


def finite(value: float, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EfficiencyError(f"{name} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise EfficiencyError(f"{name} must be finite and >= {minimum}")
    return float(value)


@dataclass(frozen=True)
class Moments:
    """Welford sufficient statistics; rebuild on an evidence correction."""

    n: int = 0
    work: float = 0.0
    tokens: float = 0.0
    work_m2: float = 0.0
    tokens_m2: float = 0.0
    max_tokens: float = 0.0

    def add(self, work: float, tokens: float) -> Moments:
        work = finite(work, "verified work")
        tokens = finite(tokens, "episode tokens")
        if work > 1:
            raise EfficiencyError("episode work exceeds its predeclared unit")
        n = self.n + 1
        dw = work - self.work / self.n if self.n else work
        dc = tokens - self.tokens / self.n if self.n else tokens
        return Moments(
            n, self.work + work, self.tokens + tokens,
            self.work_m2 + dw * (work - (self.work + work) / n),
            self.tokens_m2 + dc * (tokens - (self.tokens + tokens) / n),
            max(self.max_tokens, tokens),
        )

    @property
    def efficiency(self) -> float | None:
        return self.work / self.tokens if self.tokens > 0 else None


@dataclass(frozen=True)
class Candidate:
    """An already authorized and feasible setting + fixed recovery recipe.

    identity includes effective releases/route/harness/context/recipe. The
    adapter must start a new population when material identities change.
    bound_assumption is explicit: 'enforced', 'assumed', or 'unavailable'.
    The latter cannot support confidence-based promotion.
    """

    identity: str
    token_bound: float | None
    prior_tokens: float
    bound_assumption: str = "unavailable"

    def validate(self) -> None:
        if not self.identity:
            raise EfficiencyError("candidate has no identity")
        if finite(self.prior_tokens, "prior tokens") <= 0:
            raise EfficiencyError("prior tokens must be positive")
        if self.bound_assumption not in {"enforced", "assumed", "unavailable"}:
            raise EfficiencyError("unknown cost-bound assumption")
        if self.token_bound is not None and finite(self.token_bound, "bound") <= 0:
            raise EfficiencyError("token bound must be positive")
        if self.bound_assumption != "unavailable" and self.token_bound is None:
            raise EfficiencyError("declared bound assumption requires a bound")


@dataclass(frozen=True)
class Interval:
    lower: float
    upper: float
    estimate: float | None
    observations: int
    status: str


def _bounded_mean(mean: float, m2: float, n: int, alpha: float) -> tuple[float, float]:
    if n < 2:
        return 0.0, 1.0
    variance = max(0.0, m2 / (n - 1))
    log = math.log(2.0 / alpha)
    radius = math.sqrt(2.0 * variance * log / n) + 7.0 * log / (3.0 * (n - 1))
    return max(0.0, mean - radius), min(1.0, mean + radius)


def efficiency_interval(
    candidate: Candidate, moments: Moments, population: int, delta: float = 0.05,
) -> Interval:
    candidate.validate()
    if isinstance(population, bool) or not isinstance(population, int) or population < 1:
        raise EfficiencyError("population must be a positive integer")
    if not 0 < finite(delta, "delta") < 1:
        raise EfficiencyError("delta must lie strictly between zero and one")
    if candidate.token_bound is None or candidate.bound_assumption == "unavailable":
        return Interval(0.0, math.inf, moments.efficiency, moments.n, "cost-bound-unavailable")
    bound = candidate.token_bound
    if moments.max_tokens > bound:
        return Interval(0.0, math.inf, moments.efficiency, moments.n, "cost-bound-violated")
    if moments.n < 2:
        return Interval(0.0, math.inf, moments.efficiency, moments.n, "insufficient-evidence")
    n = moments.n
    alpha = delta / (4 * population * n * (n + 1))
    wl, wu = _bounded_mean(moments.work / n, moments.work_m2, n, alpha)
    cl, cu = _bounded_mean(moments.tokens / (n * bound), moments.tokens_m2 / bound**2, n, alpha)
    lower = wl / (cu * bound) if cu > 0 else 0.0
    upper = wu / (cl * bound) if cl > 0 else math.inf
    return Interval(lower, upper, moments.efficiency, n, candidate.bound_assumption)


@dataclass(frozen=True)
class Budget:
    """All exploration spend, including unfinished episodes, plus reservations.

    allowance is externally accrued from unique ordinary opportunities and
    normal-work expenditure; it must not increase on repeated recommend calls.
    An unknown incurred cost closes admission until reconciliation. Enforced
    reservation bounds are necessary for a strict actual-spend promise.
    """

    allowance: float
    spent: float
    reserved: float
    pending: int
    max_pending: int = 1
    unknown_spend: bool = False

    def admits(self, reservation: float) -> bool:
        for name in ("allowance", "spent", "reserved"):
            finite(getattr(self, name), name)
        finite(reservation, "reservation")
        if self.pending < 0 or self.max_pending < 1:
            raise EfficiencyError("invalid pending-trial limit")
        return (not self.unknown_spend and self.pending < self.max_pending
                and self.spent + self.reserved + reservation <= self.allowance)


@dataclass(frozen=True)
class Decision:
    selected: str
    incumbent: str
    reason: str
    exploration: bool
    reservation: float
    spending_guarantee: str
    demonstrated_productive_route: bool
    intervals: Mapping[str, Interval]


def quota(opportunity: int, focused: bool = False) -> int:
    if isinstance(opportunity, bool) or not isinstance(opportunity, int) or opportunity < 0:
        raise EfficiencyError("opportunity must be a nonnegative integer")
    x = math.log1p(opportunity)
    # Both grow unboundedly and sublinearly, including when tied best routes
    # keep uncertainty-driven exploration open forever.
    return math.ceil(4 * x**3) if focused else math.ceil(0.25 * x**2)


def choose(
    candidates: Sequence[Candidate], moments: Mapping[str, Moments], incumbent: str,
    *, opportunity: int, last_exploration: int, exploration_count: int,
    trials: Mapping[str, int], pending: Mapping[str, int], budget: Budget,
    delta: float = 0.05,
) -> Decision:
    """Recommend without mutating clocks, reservations, or evidence.

    Caller atomically persists the decision/reservation for its unique real
    opportunity and registers actual launch separately. ``trials`` includes
    all *launched episodes* per candidate (ordinary and exploratory); pending
    holds not yet launched are additional. Cancelled proposals release their
    holds and cannot satisfy a candidate's lifetime sample quota.
    """
    by_id = {c.identity: c for c in candidates}
    if not candidates or len(by_id) != len(candidates) or incumbent not in by_id:
        raise EfficiencyError("need distinct feasible candidates and an eligible incumbent")
    fair = quota(opportunity)
    focused = quota(opportunity, True)
    if not 0 <= last_exploration <= opportunity or exploration_count < 0:
        raise EfficiencyError("invalid exploration clock")
    for table in (trials, pending):
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in table.values()):
            raise EfficiencyError("trial counts must be nonnegative integers")
    intervals = {key: efficiency_interval(c, moments.get(key, Moments()), len(candidates), delta)
                 for key, c in by_id.items()}
    challengers = [key for key, interval in intervals.items()
                   if interval.lower > intervals[incumbent].upper]
    reason = "retain-provisional-incumbent"
    if challengers:
        incumbent = max(challengers, key=lambda key: (intervals[key].lower, key))
        reason = "supported-efficiency-advantage"
    productive = any(m.work > 0 for m in moments.values())

    def result(selected: str, why: str, exploration: bool = False) -> Decision:
        candidate = by_id[selected]
        reserve = (candidate.token_bound if candidate.token_bound is not None
                   else candidate.prior_tokens) if exploration else 0.0
        guarantee = ("bounded" if candidate.bound_assumption == "enforced"
                     else "expected-only") if exploration else "not-a-trial"
        return Decision(selected, incumbent, why, exploration, reserve, guarantee,
                        productive, intervals)

    if opportunity - last_exploration < 10:
        return result(incumbent, reason)
    counts = {key: trials.get(key, 0) + pending.get(key, 0) for key in by_id}
    def affordable(key: str) -> bool:
        c = by_id[key]
        return pending.get(key, 0) == 0 and budget.admits(
            c.token_bound if c.token_bound is not None else c.prior_tokens)
    debt = [key for key in by_id if counts[key] < fair and affordable(key)]
    possible = [key for key in by_id if key != incumbent and counts[key] < focused
                and intervals[key].upper > intervals[incumbent].lower and affordable(key)]
    # Every other learning slot protects fair coverage. Smallest accumulated
    # count wins; a currently expensive prior never removes a coverage debtor.
    if debt and (exploration_count % 2 == 0 or not possible):
        key = min(debt, key=lambda key: (counts[key], key))
        return result(key, "protected-fair-coverage", True)
    if possible:
        def priority(key: str) -> tuple[float, int, str]:
            c = by_id[key]
            interval = intervals[key]
            gain = interval.upper - intervals[incumbent].lower
            return gain / c.prior_tokens, -counts[key], key
        return result(max(possible, key=priority), "focused-efficiency-learning", True)
    if debt:
        return result(min(debt, key=lambda key: (counts[key], key)), "protected-fair-coverage", True)
    return result(incumbent, reason + "; exploration-deferred-or-covered")
