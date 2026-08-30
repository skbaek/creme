#!/usr/bin/env python3
"""The Lean edit-loop quadrant decision rule.

DERIVED from the nine-target fit set on 2026-08-26 and COMMITTED BEFORE the
24-target randomized holdout was measured.  Its inputs are restricted to what
an agent can know *before* doing any work:

    B  module baseline elaboration seconds   (scripts/baseline-elab.txt)
    a  suffix fraction = decls_after / decls_total   (countable from the file)
    n  the agent's own estimate of iterations

Nothing here consults P, T or S.  A rule needing those would cost more to
evaluate than the loop it chooses.

Quadrants:  Q1 in-situ CLI · Q2 in-situ LSP · Q3 scratch CLI-prefix
            Q4 scratch LSP-prefix
"""

# --- constants, dated 2026-08-26, revisable by re-measurement ---------------

FLOOR_S = 1.0   # a suffix cheaper than this is below the ~0.2s protocol floor
                # plus noise; the scratch prefix cannot recover a saving there.
FID_MULT = 4.0  # fabrication + full fidelity check, as a multiple of B.
                # Measured range across the fit set: 2.3x - 4.4x.  The high end
                # is used deliberately: it biases toward in-situ, i.e. AGAINST
                # the mode this goal was commissioned to recommend.


def decide(baseline_s, suffix_fraction, expected_iterations):
    """Return (quadrant, reason, breakeven_n_or_None)."""
    B, a, n = baseline_s, suffix_fraction, expected_iterations

    # 1. One-shot work is not a loop.  Nothing below applies.
    if n <= 1:
        return "Q2", "one-shot: no loop to amortize anything over", None

    # 2. The suffix is what the scratch prefix removes.  If it is cheap, there
    #    is nothing to win, at any iteration count.
    suffix_cost = B * a
    if suffix_cost < FLOOR_S:
        return ("Q2",
                f"suffix costs only {suffix_cost:.2f}s (< {FLOOR_S}s floor): "
                f"nothing for a scratch prefix to remove", None)

    # 3. Otherwise trade the one-time fabrication+fidelity charge against the
    #    per-iteration suffix saving.  Q4's warm-up is cheaper than Q2's by
    #    roughly the suffix too, which is why the saving appears on both sides.
    #       n * (B*a)  >  FID_MULT*B - B*a
    #       n          >  (FID_MULT - a) / a
    breakeven = (FID_MULT - a) / a
    if n > breakeven:
        return ("Q4",
                f"suffix {suffix_cost:.1f}s/iteration; break-even at "
                f"{breakeven:.1f} iterations and {n} expected", breakeven)
    return ("Q2",
            f"break-even at {breakeven:.1f} iterations but only {n} expected",
            breakeven)


# Q1 and Q3 are never selected for iteration.  Across every fit target, in-situ
# LSP beat in-situ CLI by between 1.06x and 12x, for a one-time warm-up of
# 2-38s, and the CLI quadrants re-elaborate the prefix on every single pass.
# The command line stays a loop BOUNDARY: controls, batch discovery, boundary
# confirmation under the oracle rule, and integration.

if __name__ == "__main__":
    import sys, json
    if len(sys.argv) == 4:
        q, why, be = decide(float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]))
        print(json.dumps({"quadrant": q, "reason": why, "breakeven_n": be}, indent=2))
    else:
        print(__doc__)
        print("usage: rule.py <baseline_s> <suffix_fraction> <expected_iterations>")
        for a in (1.0, 0.9, 0.8, 0.5, 0.25, 0.1):
            print(f"  a={a:4.2f} -> break-even n = {(FID_MULT-a)/a:5.1f}")
