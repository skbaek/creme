"""estimator-evidence-scope-v1: evidence floors stay scoped to what they measured.

Reproduces the 2026-09-23 over-pricing on this host from synthetic ledgers:

* a failed row naming two failed modules and no per-module peak charged its
  whole-closure peak (9.53 GiB) to each of them, pricing both at 11 GiB;
* an old-toolchain single-module aggregate (9.81 GiB) kept pricing a module
  at 11 GiB although a newer current-toolchain measurement of it existed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from creme import build_ownership as owned

from test_admission_fallback import SETTINGS, _current, _identity, _isolated, _recent_time, _row


OLD_TC, NEW_TC = "tc-old", "tc"


def _failed(time, rebuilt, failed, peak_gib, module_peaks=None) -> dict:
    row = _row(time, rebuilt, peak_gib, exit_code=1, module_peaks=module_peaks)
    row["modules_failed"] = list(failed)
    return row


class FailedFloorAttributionTest(unittest.TestCase):
    """A shared whole-process peak is not attributed to several failures."""

    def test_multi_failure_row_without_own_peaks_floors_no_module(self) -> None:
        # The 2026-09-23T01:36:15 shape: 10 rebuilt deps, two failed modules,
        # a 9.53 GiB closure peak, no module_peak_mib entry for either failure.
        deps = [f"D{index}" for index in range(10)]
        row = _failed(
            "2026-09-23T01:36:15Z", deps, ["A", "B"], 9.53,
            module_peaks={dep: 1.5 for dep in deps},
        )
        for module in ("A", "B"):
            with self.subTest(module=module):
                sizing = owned.size_stale_set(
                    [module], {module: set()}, [], SETTINGS, 8, failed_rows=[row],
                )
                self.assertEqual(sizing["failed_attempt_modules"], [])
                self.assertEqual(sizing["kind"], "narrow default")
                self.assertEqual(sizing["estimate_gib"], 4)
                self.assertNotIn("failed attempt", sizing["source"])

    def test_multi_failure_row_uses_a_failed_modules_own_peak(self) -> None:
        row = _failed(
            "2026-09-23T01:36:15Z", ["D"], ["A", "B"], 9.53, module_peaks={"D": 1.5, "A": 5.2},
        )
        sizing = owned.size_stale_set(["A"], {"A": set()}, [], SETTINGS, 8, failed_rows=[row])
        self.assertEqual(sizing["failed_attempt_modules"], ["A"])
        self.assertEqual(sizing["estimate_gib"], 7)            # ceil(5.2) + 1 GiB margin
        self.assertIn("own peak", sizing["source"])
        self.assertIn("2026-09-23T01:36:15Z", sizing["source"])
        other = owned.size_stale_set(["B"], {"B": set()}, [], SETTINGS, 8, failed_rows=[row])
        self.assertEqual(other["failed_attempt_modules"], [])
        self.assertEqual(other["estimate_gib"], 4)

    def test_single_failure_join_row_still_floors_by_the_closure_peak(self) -> None:
        # The Join case that motivated the floor: four deps rebuilt, one failure.
        row = _failed("2026-09-20T00:00:00Z", ["D1", "D2", "D3", "D4"], ["Join"], 5.4)
        sizing = owned.size_stale_set(
            ["Join"], {"Join": set()}, [], SETTINGS, 8, failed_rows=[row],
        )
        self.assertEqual(sizing["failed_attempt_modules"], ["Join"])
        self.assertEqual(sizing["estimate_gib"], 7)            # ceil(5.4) + 1 GiB margin
        self.assertIn("sole failed module", sizing["source"])
        self.assertIn("2026-09-20T00:00:00Z", sizing["source"])


class ToolchainScopeTest(unittest.TestCase):
    """Fallback aggregates come from the current toolchain; newer evidence supersedes."""

    def old_singleton(self) -> dict:
        # 2026-09-18T22:44:09 shape: old toolchain, one module, 9.81 GiB aggregate.
        row = _row(
            "2026-09-18T22:44:09Z", ["A"], 9.81, lean_gib=9.17, module_peaks={"A": 9.17},
            identity=_identity(["A"], "old"), samples=3,
        )
        row["toolchain_digest"] = OLD_TC
        return row

    def new_pair(self) -> dict:
        # 2026-09-22T23:57:40 shape: current toolchain, the facade plus one fragment.
        return _row(
            "2026-09-22T23:57:40Z", ["A", "A.Frag"], 5.52, lean_gib=4.84,
            module_peaks={"A": 0.52, "A.Frag": 4.84},
            identity=_identity(["A", "A.Frag"], "mid"), samples=3,
        )

    def test_without_a_toolchain_the_old_behaviour_is_unchanged(self) -> None:
        sizing = owned.size_stale_set(
            ["A"], {"A": set()}, [self.old_singleton(), self.new_pair()], SETTINGS, 8,
            _current(["A"]),
        )
        self.assertEqual(sizing["estimate_gib"], 11)

    def test_newer_current_toolchain_measurement_supersedes_old_toolchain_floors(self) -> None:
        sizing = owned.size_stale_set(
            ["A"], {"A": set()}, [self.old_singleton(), self.new_pair()], SETTINGS, 8,
            _current(["A"]), toolchain_digest=NEW_TC,
        )
        self.assertEqual(sizing["fallback_build_modules"], [])
        self.assertAlmostEqual(sizing["fallback_peak_gib"], 0.52, places=2)
        self.assertEqual(sizing["kind"], "narrow default")
        self.assertEqual(sizing["estimate_gib"], 4)

    def test_old_toolchain_aggregate_never_floors_but_own_peak_remains_alone(self) -> None:
        # With no current-toolchain evidence, the old module peak stays a
        # conservative cross-toolchain floor; the old aggregate does not.
        evidence = owned.module_cost_evidence(
            [self.old_singleton()], SETTINGS, _current(["A"]), NEW_TC,
        )
        self.assertNotIn("A", evidence["fallback_build_peak_gib"])
        self.assertAlmostEqual(evidence["fallback_peak_gib"]["A"], 9.17, places=2)
        sizing = owned.size_stale_set(
            ["A"], {"A": set()}, [self.old_singleton()], SETTINGS, 8, _current(["A"]),
            toolchain_digest=NEW_TC,
        )
        self.assertEqual(sizing["estimate_gib"], 11)           # ceil(9.17) + 1 GiB margin
        self.assertIn("2026-09-18T22:44:09Z", sizing["source"])
        self.assertIn("module peak", sizing["source"])

    def test_current_toolchain_aggregate_still_floors(self) -> None:
        row = _row(
            "2026-09-20T09:57:33Z", ["A"], 7.13, lean_gib=6.45, module_peaks={"A": 6.45},
            identity=_identity(["A"], "old"), samples=3,
        )
        sizing = owned.size_stale_set(
            ["A"], {"A": set()}, [self.old_singleton(), row], SETTINGS, 8, _current(["A"]),
            toolchain_digest=NEW_TC,
        )
        self.assertEqual(sizing["fallback_build_modules"], ["A"])
        self.assertAlmostEqual(sizing["fallback_peak_gib"], 7.13, places=2)
        self.assertIn("2026-09-20T09:57:33Z", sizing["source"])

    def test_the_ledger_path_passes_the_current_toolchain(self) -> None:
        old = self.old_singleton()
        new = self.new_pair()
        old["time"] = _recent_time()
        new["time"] = _recent_time()
        with _isolated() as root:
            (root / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in (old, new)), encoding="utf-8",
            )
            estimate, evidence = owned.derive_memory_gib(
                Path("/w"), ["A"], SETTINGS, (NEW_TC, "mf"), 8,
                stale={"stale": 1, "stale_set": ["A"], "graph": {"A": set()}},
                input_identity=_current(["A"]),
            )
        self.assertEqual(estimate, 4, evidence["source"])


if __name__ == "__main__":
    unittest.main()
