#!/usr/bin/env python3
"""Parameterised Lean edit-loop quadrant decision rule.

The reusable engine contains no fitted host/corpus constants. Supply a reviewed
protocol floor and fidelity multiplier from evidence for the current toolchain,
repository, and host.
"""

from __future__ import annotations

import argparse
import json


def decide(
    baseline_s: float,
    suffix_fraction: float,
    expected_iterations: int,
    protocol_floor_s: float,
    fidelity_multiplier: float,
):
    if baseline_s < 0 or not 0 <= suffix_fraction <= 1:
        raise ValueError("baseline must be non-negative and suffix fraction must be in [0, 1]")
    if expected_iterations <= 1:
        return "Q2", "one-shot: no loop to amortize", None
    suffix_cost = baseline_s * suffix_fraction
    if suffix_cost < protocol_floor_s:
        return "Q2", "suffix cost is below the supplied protocol floor", None
    if suffix_fraction == 0:
        return "Q2", "empty suffix: nothing for a prefix to remove", None
    breakeven = (fidelity_multiplier - suffix_fraction) / suffix_fraction
    if expected_iterations > breakeven:
        return "Q4", "expected iterations exceed the supplied break-even model", breakeven
    return "Q2", "expected iterations do not exceed the supplied break-even model", breakeven


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline_s", type=float)
    parser.add_argument("suffix_fraction", type=float)
    parser.add_argument("expected_iterations", type=int)
    parser.add_argument("--protocol-floor-s", type=float, required=True)
    parser.add_argument("--fidelity-multiplier", type=float, required=True)
    args = parser.parse_args()
    try:
        quadrant, reason, breakeven = decide(
            args.baseline_s,
            args.suffix_fraction,
            args.expected_iterations,
            args.protocol_floor_s,
            args.fidelity_multiplier,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "quadrant": quadrant,
        "reason": reason,
        "breakeven_n": breakeven,
        "inputs": vars(args),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
