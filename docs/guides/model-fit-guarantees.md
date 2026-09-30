# Conditional selection argument

This is an argument about the declared finite policy class and implemented
scheduler, not a theorem about arbitrary real model behavior. The selector is
`creme/model_fit_efficiency.py`; runtime identity, costs and delayed joins are
in `model_fit_runtime.py`. Numerical evaluation uses floating-point arithmetic,
not a formally verified real-arithmetic implementation.

Fix one execution client, task/context population, harness and finite set of K
eligible setting/recovery recipes. For each candidate i, assume its episodes in
pre-outcome declaration order are independent representative draws from a fixed
joint distribution of credited work W in [0,1] and total tokens C in [0,B_i],
with a valid finite B_i and E[C]>0. W and C may be correlated. Adaptive candidate
choice may depend on past observations, but may not select unobserved outcomes
or a different task distribution for the candidate. Stationary marginal
histograms alone are insufficient. A new release/material context is a different
population; legacy exception narratives are not samples from this distribution.

Assume genuine tasks recur indefinitely for this population, active selection
continues, admitted trials actually launch, pending reservations resolve, and
every launched episode eventually receives final usage and master disposition.
The growing joined prefix must reach every fixed episode index. User pauses,
infinite cancellations, missing usage, permanently unjudged interruptions, or
permanent host refusals do not satisfy these assumptions. No routing obligation
to another execution client is implied.

## Simultaneous uncertainty

For each candidate and sample count n>=2, allocate
`alpha_n = delta / (4 K n(n+1))` to each of four one-sided errors: work lower and
upper bounds, and scaled cost lower and upper bounds. With sample variance s²,
use radius `sqrt(2 s² log(2/alpha_n)/n) + 7 log(2/alpha_n)/(3(n-1))`, clipped to
[0,1]. This is Theorem4 applied to a bounded variable and its complement in
[Maurer and Pontil, 2009](https://www.cs.mcgill.ca/~colt2009/papers/012.pdf).

Summing the four allocations over K candidates and all n>=2 costs at most delta
(the sum of 1/(n(n+1)) is at most1). Thus repeated inspection, including an
adaptively chosen prefix length, is covered by one simultaneous event with
probability at least1-delta. The means need not be independent of each other.
On this event the efficiency E[W]/E[C] lies between lower-work/upper-cost and
upper-work/lower-cost. If the lower cost bound is zero, the upper efficiency
bound is infinite; fewer than two samples are likewise uninformative.

A delayed prefix does not choose only fast successes: its order is fixed before
outcomes, and it contains every earlier episode. Runtime uses declaration order
even when native launch receipts arrive after completion. A fallback cannot
claim initial ownership by arriving first: attempt positions must join in order.
Corrections invalidate closure and recompute moments from the corrected prefix.
These mechanisms enforce ordering and bookkeeping; they do not establish the
representative-sample assumptions themselves.

## Sampling and budget conditions

The fair target q(t)=ceil(.25 log²(1+t)) diverges. A protected learning slot picks
an affordable under-target candidate with smallest accumulated launch/pending
count. Every second admitted learning slot protects this rule; if focused
learning has no candidate, fairness can use the other slots too. A wrong high
cost prior affects focused priority but does not remove fair debt.

The proof needs sufficiently recurrent **local affordable protected slots**:
a candidate whose count stays bounded must be affordable at enough such slots,
and pending work must not remain a permanent substitute for its samples.
This is stronger than saying the host occasionally admits some unrelated work.
Under this condition, a finite-count candidate cannot starve: eventually q(t)
exceeds that count, and only finitely many smaller/equal counts can be served
before it is selected, since K is finite and each actual launch increments a
count. Hence every candidate's sample count, and by eventual joining its usable
prefix, tends to infinity.

Learning opportunities are spaced by at least ten local ordinary opportunities;
only one trial can be pending. Fair launches per candidate are bounded by its
largest fair target plus pending overshoot; focused launches are bounded by
ceil(4 log³(1+t)) plus pending overshoot. Ordinary launches also satisfy these
quotas and can only reduce exploratory demand. Therefore total exploratory
launches are O(K log³ t), which is o(t) for fixed K.

With a common finite maximum episode cost, exploration consumes o(t) tokens.
If observed ordinary expenditure grows at least linearly with t, its fixed
positive allowance fraction eventually exceeds sublinear exploration spend and
finite reservations. Together with finite pending delays and recurrent host
admission, this supplies the local affordability condition. Alternatively,
state that condition directly. Merely positive expected cost with arbitrarily
late unobserved expenditure does not by itself guarantee timely funding.

The runtime cannot enforce a full master-plus-worker token bound. Reservations
are advisory; predicted overruns remain fully charged and block additional trials
until funded. An explicitly assumed B_i supports a conditional statistical
claim, not a strict spending promise. Unavailable bounds prohibit confidence
promotion; violated bounds remove that candidate from admission until its recipe
or assumption is reconciled. Under an actually valid bound, that violation case
does not occur. Accounting is never clipped to manufacture the premise.

## Convergence and practical limits

Under the sample assumptions, empirical work/cost means converge and the radii
shrink as n grows: log(1/alpha_n)=O(log n). Since E[C]>0, each efficiency interval
shrinks to its true ratio. With a uniquely separated best candidate, eventually
its lower bound exceeds every inferior incumbent's upper bound; `choose` then
promotes a supported challenger. On the simultaneous-coverage event, each
promotion improves true efficiency, so finitely many such improvements reach
the best candidate. Exploratory tokens are a vanishing share under the budget
conditions above. This is a conditional high-probability result over this fixed
policy class, not an unconditional convergence guarantee for deployed models.
Exact ties need not select a unique incumbent; an all-zero-work population has
no demonstrated productive route, regardless of cheap failures.

At practical horizons, broad intervals, rare task classes, loose cost bounds,
large catalogues and delayed verification can make discovery slow. The seeded
equal-token-budget evaluation reports these costs instead of assuming asymptotic
behavior is already useful. The one-per-ten envelope and constants are engineering
choices; evidence must show their finite-horizon tradeoffs. A few real rollout
tasks validate capture and joins, not comparative model performance. Changes to
this argument's premises require a new stated claim, not silently weaker checks.
