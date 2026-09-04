"""creme-admission-accuracy-v1: estimates keyed on the stale set, not the target.

Each test here fails on the code B11 observed and passes on the candidate.
The replay class rebuilds prorata's B11 waits from a ledger fixture cut from
the real ledger of that window and shows the verdict each request now gets.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from creme import build_ownership as owned
from creme import idle_workers, reclaim, semaphore
from creme.cli import cmd_idle_workers, cmd_reclaim
from creme.profile import ADMISSION_DEFAULTS, validate_data

try:
    from test_semaphore import ProcessAdapter
except ImportError:  # invoked as scripts.tests.test_admission_accuracy
    from scripts.tests.test_semaphore import ProcessAdapter


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SETTINGS = dict(ADMISSION_DEFAULTS)
LIVE_STATE = Path("/Users/agent/creme/.semaphore/state")
HOST_POLICY = {
    "task_memory_gib": 8, "heavy_workers": 2, "light_workers": 5,
    "physical_memory_gib": 24.0, "profile_status": "VALID",
}


@contextlib.contextmanager
def _isolated():
    """A scratch ledger and a scratch semaphore state directory."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = root / "state"
        state.mkdir()
        (state / "log.jsonl").touch()
        with patch.dict(os.environ, {
            "CREME_BUILD_LEDGER": str(root / "ledger.jsonl"),
            "CREME_SEMAPHORE_DIR": str(state),
        }):
            yield root


def _row(time: str, rebuilt, peak_gib: float, *, lean_gib=None, concurrency=1,
         seconds=None, module_peaks=None, targets=("T",), worktree="/w",
         exit_code=0) -> dict:
    rebuilt = list(rebuilt)
    row = {
        "schema_version": 1, "time": time, "kind": "build", "goal": "g",
        "worktree": worktree, "targets": list(targets), "command": ["lake", "build"],
        "exit": exit_code, "wall_seconds": 1.0, "threads": 2, "probe": False,
        "admission": "ADMITTED_HARD", "contention": "sensitive",
        "modules_rebuilt": rebuilt, "modules_restored": [], "module_hashes": {},
        "module_seconds": dict(seconds or {module: 1.0 for module in rebuilt}),
        "peak_rss_mib": peak_gib * 1024.0, "max_concurrent_lean": concurrency,
        "toolchain_digest": "tc", "manifest_digest": "mf",
    }
    if lean_gib is not None:
        row["peak_lean_rss_mib"] = lean_gib * 1024.0
    if module_peaks is not None:
        row["module_peak_mib"] = {module: gib * 1024.0 for module, gib in module_peaks.items()}
    return row


def _sample(available_gib: float, total_gib: float = 24.0):
    return SimpleNamespace(status="OK", data={
        "memory_free_percent": int(round(100 * available_gib / total_gib)),
        "memory_available_bytes": int(available_gib * 1024 ** 3),
        "physical_memory_bytes": int(total_gib * 1024 ** 3),
    })


class StaleSetSizingTest(unittest.TestCase):
    """Item 1 and 2: the estimate and the class come from the stale set."""

    def test_a_single_measured_module_is_sized_from_its_own_lean_peak(self) -> None:
        rows = [_row("2026-09-04T00:00:00Z", ["A"], 2.1, lean_gib=1.5)]
        sizing = owned.size_stale_set(["A"], {"A": set()}, rows, SETTINGS, 8)
        self.assertEqual(sizing["kind"], "measured")
        self.assertAlmostEqual(sizing["overhead_gib"], 0.6, places=2)
        self.assertAlmostEqual(sizing["peak_gib"], 2.1, places=2)
        self.assertEqual(sizing["estimate_gib"], 4)      # ceil(2.1) + 1

    def test_the_spelling_of_a_target_list_is_not_evidence(self) -> None:
        """B11 F1: one broad rebuild pinned every later `-- Blanc` at 12 GiB."""
        broad = [f"Pkg.M{index}" for index in range(376)] + ["Pkg"]
        rows = [
            _row("2026-09-04T01:26:43Z", broad, 10.17, lean_gib=7.98, concurrency=2,
                 targets=("Pkg",)),
            _row("2026-09-04T01:30:00Z", ["Pkg"], 2.2, lean_gib=1.55, targets=("Pkg",)),
        ]
        sizing = owned.size_stale_set(["Pkg"], {"Pkg": set()}, rows, SETTINGS, 8)
        self.assertEqual(sizing["kind"], "measured")
        self.assertEqual(sizing["estimate_gib"], 4)
        with _isolated() as root:
            (root / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            keyed, evidence = owned._target_keyed_estimate(
                Path("/w"), ["Pkg"], SETTINGS, ("tc", "mf"), 8, 1
            )
        # The keying B11 ran under: the maximum peak of every row spelled `Pkg`.
        self.assertEqual(keyed, 12)
        self.assertIn("target rows", evidence["source"])

    def test_a_chain_elaborates_one_at_a_time_and_an_antichain_together(self) -> None:
        rows = [
            _row("2026-09-04T00:00:00Z", ["A"], 2.1, lean_gib=1.5),
            _row("2026-09-04T00:01:00Z", ["B"], 2.1, lean_gib=1.5),
            _row("2026-09-04T00:02:00Z", ["C", "D"], 3.6, lean_gib=1.5, concurrency=2),
        ]
        chain = owned.size_stale_set(["A", "B"], {"B": {"A"}, "A": set()}, rows, SETTINGS, 8)
        self.assertEqual(chain["width"], 1)
        self.assertAlmostEqual(chain["peak_gib"], 2.1, places=2)
        independent = owned.size_stale_set(["A", "B"], {"A": set(), "B": set()}, rows, SETTINGS, 8)
        self.assertEqual(independent["width"], 2)
        self.assertAlmostEqual(independent["peak_gib"], 3.6, places=2)
        self.assertEqual(independent["estimate_gib"], 5)

    def test_the_width_is_the_largest_antichain_of_the_import_order(self) -> None:
        graph = {"Top": {"Mid"}, "Mid": {"Leaf"}, "Leaf": set(), "Side": set()}
        self.assertEqual(owned.stale_set_width(["Top", "Mid", "Leaf"], graph, 4), 1)
        self.assertEqual(owned.stale_set_width(["Top", "Side"], graph, 4), 2)
        # Reachability through a module outside the set still orders the set.
        self.assertEqual(owned.stale_set_width(["Top", "Leaf"], graph, 4), 1)
        self.assertEqual(owned.stale_set_width(["Top", "Side"], graph, 1), 1)
        self.assertEqual(owned.stale_set_width([f"M{i}" for i in range(40)], None, 2), 2)

    def test_a_broad_row_measures_no_single_module(self) -> None:
        broad = [f"M{index}" for index in range(40)]
        rows = [_row("2026-09-04T00:00:00Z", broad, 9.0, lean_gib=6.0, concurrency=2)]
        sizing = owned.size_stale_set(["M3"], {"M3": set()}, rows, SETTINGS, 8)
        self.assertEqual(sizing["kind"], "narrow default")
        self.assertEqual(sizing["unmeasured"], ["M3"])
        self.assertEqual(sizing["estimate_gib"], SETTINGS["narrow_default_gib"])

    def test_a_member_that_elaborated_for_long_keeps_the_profile_default(self) -> None:
        """B11 02:44:47: two modules, one of them 261 s, peaked at 7.4 GiB."""
        broad = [f"M{index}" for index in range(40)] + ["Code"]
        seconds = {module: 1.0 for module in broad}
        seconds["Code"] = 108.0
        rows = [
            _row("2026-09-04T00:00:00Z", broad, 9.0, lean_gib=6.0, concurrency=2, seconds=seconds),
            _row("2026-09-04T00:01:00Z", ["Arith"], 2.0, lean_gib=1.3),
        ]
        sizing = owned.size_stale_set(["Arith", "Code"], {"Arith": set(), "Code": set()}, rows, SETTINGS, 8)
        self.assertEqual(sizing["kind"], "heavy module")
        self.assertEqual(sizing["heavy"], ["Code"])
        self.assertEqual(sizing["estimate_gib"], 8)
        self.assertIn("108s", sizing["source"])

    def test_an_unmeasured_small_set_takes_the_narrow_default(self) -> None:
        """B11 F2: a fresh worktree's first narrow build started at 8 GiB."""
        sizing = owned.size_stale_set(["Jaune.Fork", "Jaune.Machine"], None, [], SETTINGS, 8)
        self.assertEqual(sizing["kind"], "narrow default")
        self.assertEqual(sizing["estimate_gib"], 4)
        tuned = dict(SETTINGS, narrow_default_gib=3)
        self.assertEqual(owned.size_stale_set(["A"], None, [], tuned, 8)["estimate_gib"], 3)

    def test_a_partly_measured_small_set_never_sizes_below_its_measured_part(self) -> None:
        rows = [_row("2026-09-04T00:00:00Z", ["A"], 5.5, lean_gib=4.9)]
        sizing = owned.size_stale_set(["A", "B"], None, rows, SETTINGS, 8)
        self.assertEqual(sizing["kind"], "narrow default")
        self.assertEqual(sizing["estimate_gib"], 7)     # ceil(5.5) + 1 > 4

    def test_an_unmeasured_broad_set_is_bounded_by_the_tightest_broader_rebuild(self) -> None:
        stale = [f"W{index}" for index in range(20)]
        rows = [
            _row("2026-09-04T00:00:00Z", stale + [f"X{i}" for i in range(280)], 10.2,
                 lean_gib=8.0, concurrency=2),
            _row("2026-09-04T00:10:00Z", stale + [f"Y{i}" for i in range(10)], 5.9,
                 lean_gib=4.8, concurrency=2),
        ]
        sizing = owned.size_stale_set(stale, None, rows, SETTINGS, 8)
        self.assertEqual(sizing["kind"], "broader rebuild")
        self.assertAlmostEqual(sizing["peak_gib"], 5.9, places=2)
        self.assertEqual(sizing["estimate_gib"], 7)
        # A rebuild that missed a tenth of the set is not a cover.
        rows[1]["modules_rebuilt"] = stale[:17] + [f"Y{i}" for i in range(13)]
        sizing = owned.size_stale_set(stale, None, rows, SETTINGS, 8)
        self.assertAlmostEqual(sizing["peak_gib"], 10.2, places=2)

    def test_an_unmeasured_broad_set_with_no_cover_is_the_profile_default(self) -> None:
        stale = [f"W{index}" for index in range(20)]
        rows = [_row("2026-09-04T00:00:00Z", [f"X{i}" for i in range(300)], 10.2, lean_gib=8.0)]
        sizing = owned.size_stale_set(stale, None, rows, SETTINGS, 8)
        self.assertEqual(sizing["kind"], "profile default")
        self.assertEqual(sizing["estimate_gib"], 8)

    def test_recorded_module_peaks_are_preferred_to_narrow_row_bounds(self) -> None:
        rows = [_row("2026-09-04T00:00:00Z", ["A", "B"], 4.0, lean_gib=3.0, concurrency=2,
                     module_peaks={"A": 1.0, "B": 3.0})]
        sizing = owned.size_stale_set(["A"], {"A": set()}, rows, SETTINGS, 8)
        self.assertAlmostEqual(sizing["peak_gib"], 1.0 + owned.DEFAULT_LAKE_OVERHEAD_GIB, places=2)
        evidence = owned.module_cost_evidence(rows, SETTINGS)
        self.assertEqual(evidence["lean_peak_gib"], {"A": 1.0, "B": 3.0})
        self.assertFalse(evidence["overhead_measured"])

    def test_only_the_most_recent_rows_size_a_module(self) -> None:
        rows = [_row(f"2026-09-04T00:0{index}:00Z", ["A"], 9.0, lean_gib=8.0) for index in range(3)]
        rows += [_row(f"2026-09-04T01:0{index}:00Z", ["A"], 2.0, lean_gib=1.4) for index in range(5)]
        sizing = owned.size_stale_set(["A"], {"A": set()}, rows, SETTINGS, 8)
        self.assertLess(sizing["peak_gib"], 3.0)

    def test_an_empty_stale_set_is_fresh(self) -> None:
        sizing = owned.size_stale_set([], None, [], SETTINGS, 8)
        self.assertEqual(sizing["kind"], "fresh")
        self.assertIn("takes no hold", sizing["source"])

    def _classify(self, stale_set, rows, graph=None, **settings):
        with _isolated() as root:
            (root / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            return owned.classify_contention(
                Path("/w"), ["T"], Path("/lake"), dict(SETTINGS, **settings), ("tc", "mf"),
                {"roots": ["T"], "package_roots": ["T"], "resolution": "T (module)",
                 "stale": len(stale_set), "detail": "fixture",
                 "stale_set": list(stale_set), "graph": graph},
            )

    def test_the_class_is_decided_by_the_same_sizing(self) -> None:
        rows = [_row("2026-09-04T00:00:00Z", ["A"], 2.1, lean_gib=1.5)]
        verdict, evidence = self._classify(["A"], rows)
        self.assertEqual(verdict, "tolerant", evidence)
        self.assertEqual(evidence["sizing"], "measured")
        verdict, evidence = self._classify(["A", "B"], rows)
        self.assertEqual(verdict, "sensitive")
        self.assertIn("1 of 2 stale module(s) unmeasured", evidence["reason"])
        heavy = [_row("2026-09-04T00:00:00Z", ["A"], 6.0, lean_gib=5.4)]
        verdict, evidence = self._classify(["A"], heavy)
        self.assertEqual(verdict, "sensitive")
        self.assertIn("modelled peak 6.00 GiB is not below", evidence["reason"])
        self.assertEqual(self._classify(["A"], [])[0], "sensitive")

    def test_a_drifted_digest_is_still_no_evidence(self) -> None:
        rows = [_row("2026-09-04T00:00:00Z", ["A"], 2.1, lean_gib=1.5)]
        rows[0]["manifest_digest"] = "other"
        verdict, evidence = self._classify(["A"], rows)
        self.assertEqual(verdict, "sensitive")
        self.assertIn("unmeasured", evidence["reason"])

    def test_the_estimate_follows_the_named_stale_set(self) -> None:
        rows = [_row("2026-09-04T00:00:00Z", ["A"], 2.1, lean_gib=1.5, targets=("OTHER",))]
        with _isolated() as root:
            (root / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            estimate, evidence = owned.derive_memory_gib(
                Path("/w"), ["T"], SETTINGS, ("tc", "mf"), 8, 1,
                {"roots": ["T"], "stale": 1, "stale_set": ["A"], "graph": {"A": set()}},
            )
        self.assertEqual(estimate, 4)
        self.assertEqual(evidence["kind"], "measured")
        self.assertTrue(evidence["source"].startswith("measured stale set"))

    def test_the_target_keyed_fallback_unions_the_members_rows(self) -> None:
        rows = [
            _row("2026-09-04T00:00:00Z", ["A"], 2.0, targets=("A",)),
            _row("2026-09-04T00:01:00Z", ["B"], 2.3, targets=("B",)),
        ]
        with _isolated() as root:
            (root / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            estimate, evidence = owned.derive_memory_gib(
                Path("/w"), ["A", "B"], SETTINGS, ("tc", "mf"), 8, None,
                {"roots": ["A", "B"], "stale": None, "stale_set": None, "graph": None},
            )
        self.assertEqual(estimate, 4)
        self.assertIn("2 from member targets", evidence["source"])

    def test_the_narrow_default_and_heavy_seconds_are_profile_tunables(self) -> None:
        self.assertEqual(ADMISSION_DEFAULTS["narrow_default_gib"], 4)
        self.assertEqual(ADMISSION_DEFAULTS["heavy_module_seconds"], 30)
        facts = {"system": "Darwin", "machine": "arm64", "logical_cores": 10,
                 "physical_memory_bytes": 25769803776}
        from creme.profile import fingerprint
        data = {
            "schema_version": 1, "fingerprint": fingerprint(facts), "facts": facts,
            "workspace": {"root": "/x", "jaune": "jaune", "blanc": "blanc", "goal_store": None},
            "policy": {"task_memory_gib": 8, "heavy_workers": 2, "light_workers": 5},
            "overrides": {"task_memory_gib": None, "heavy_workers": None, "light_workers": None},
            "admission": {"narrow_default_gib": 3, "heavy_module_seconds": 60},
        }
        self.assertEqual(validate_data(data).status, "VALID")
        data["admission"]["narrow_default_gib"] = 0
        self.assertEqual(validate_data(data).status, "INVALID")

    def test_a_row_with_module_peaks_validates_and_a_malformed_one_does_not(self) -> None:
        row = _row("2026-09-04T00:00:00Z", ["A"], 2.0, module_peaks={"A": 1.4})
        self.assertTrue(owned._valid_ledger_row(row))
        row["module_peak_mib"] = {"A": "big"}
        self.assertFalse(owned._valid_ledger_row(row))


class B11ReplayTest(unittest.TestCase):
    """prorata's B11 waits, replayed from rows cut from the real ledger."""

    WORKTREE = Path("/Users/agent/blanc/.worktrees/prorata-erc4626-port-v1")
    DIGESTS = ("8e3538e0ab5f81a3", "6170c288489d1fb9")
    BLANC = "Blanc"
    MESSAGE = "Blanc.Composition.ProrataWethVaultMessage"
    BACKING = "Blanc.Composition.ProrataWethVaultBacking"
    GRAPH = {
        "Blanc": {"Blanc.Prorata", "Blanc.ProrataWethVault", MESSAGE, BACKING},
        MESSAGE: {BACKING},
        BACKING: {"Blanc.ProrataWethVaultDust"},
        "Blanc.ProrataWethVaultFunctional": {"Blanc.ProrataWethVaultArithmetic"},
        "Blanc.ProrataWethVaultDust": {"Blanc.ProrataWethVaultArithmetic"},
        "Blanc.ProrataArithmetic": {"Blanc.Prorata"},
        "Blanc.ProrataWethVaultCode": {"Blanc.ProrataWethVault"},
        "Blanc.Prorata": set(), "Blanc.ProrataWethVault": set(),
        "Blanc.ProrataWethVaultArithmetic": set(),
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ledger = Path(self.tmp.name) / "ledger.jsonl"
        shutil.copyfile(FIXTURES / "b11-prorata-ledger.jsonl", ledger)
        patcher = patch.dict(os.environ, {
            "CREME_BUILD_LEDGER": str(ledger),
            "CREME_SEMAPHORE_DIR": str(Path(self.tmp.name) / "state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rows, _ = owned._evidence_rows(self.WORKTREE, *self.DIGESTS)
        self.assertGreater(len(self.rows), 50)

    def rows_before(self, moment: str) -> list[dict]:
        return [row for row in self.rows if row["time"] < moment]

    def old_estimate(self, targets: list[str], moment: str) -> int:
        """The keying B11 ran under: the exact target list, elaborating rows."""
        matching = [
            row for row in self.rows_before(moment)
            if list(row["targets"]) == list(targets) and row["modules_rebuilt"]
        ]
        if not matching:
            return 8
        peak = max(float(row["peak_rss_mib"]) for row in matching[-5:]) / 1024.0
        return max(2, int(peak) + (1 if peak != int(peak) else 0) + 1)

    def new_estimate(self, stale_set: list[str], moment: str) -> dict:
        graph = self.GRAPH if len(stale_set) < 12 else None
        return owned.size_stale_set(stale_set, graph, self.rows_before(moment), SETTINGS, 8)

    def fits(self, estimate_gib: int, available_gib: float) -> bool:
        return bool(semaphore.fit_arithmetic(_sample(available_gib), HOST_POLICY, estimate_gib)["fits"])

    def test_the_package_target_with_one_stale_root_module(self) -> None:
        """01:34, 02:11, 02:59: `-- Blanc` priced 12 GiB while only `Blanc` was stale."""
        for moment, available in (("2026-09-04T01:34:04Z", 17.76),
                                  ("2026-09-04T02:11:38Z", 15.4),
                                  ("2026-09-04T02:59:46Z", 18.0)):
            old = self.old_estimate(["Blanc"], moment)
            new = self.new_estimate([self.BLANC], moment)
            self.assertEqual(old, 12, moment)
            self.assertFalse(self.fits(old, available), moment)
            self.assertEqual(new["kind"], "measured", (moment, new["source"]))
            self.assertEqual(new["estimate_gib"], 4, moment)
            self.assertTrue(self.fits(new["estimate_gib"], available), moment)

    def test_the_two_target_list_inherits_its_members_measurements(self) -> None:
        """02:00: `Message Backing` was unmeasured as a pair; each had thirty rows."""
        moment = "2026-09-04T02:00:02Z"
        old = self.old_estimate([self.MESSAGE, self.BACKING], moment)
        new = self.new_estimate([self.MESSAGE, self.BACKING], moment)
        self.assertEqual(old, 8)
        self.assertFalse(self.fits(old, 14.4))
        self.assertEqual(new["kind"], "measured")
        self.assertEqual(new["width"], 1)            # Backing is in Message's closure
        self.assertEqual(new["estimate_gib"], 4)
        self.assertTrue(self.fits(new["estimate_gib"], 14.4))

    def test_two_modules_known_only_from_a_broad_rebuild_take_the_narrow_default(self) -> None:
        """02:21:48: a 2-module `-- Blanc` priced from the 376-module row."""
        moment = "2026-09-04T02:21:48Z"
        stale = ["Blanc.Prorata", "Blanc.ProrataWethVault"]
        self.assertEqual(self.old_estimate(["Blanc"], moment), 12)
        new = self.new_estimate(stale, moment)
        self.assertEqual(new["kind"], "narrow default")
        self.assertEqual(new["estimate_gib"], 4)
        self.assertTrue(self.fits(4, 15.5))
        # The build that ran at 02:23:56 peaked at 3.57 GiB: covered.
        actual = next(row for row in self.rows if row["time"].startswith("2026-09-04T02:23"))
        self.assertLess(float(actual["peak_rss_mib"]) / 1024.0, 4.0)

    def test_a_heavy_module_seen_in_a_broad_rebuild_keeps_the_profile_default(self) -> None:
        """02:24–02:44: `ProrataWethVaultCode` (108 s in the 376-row) peaked at 7.2 GiB."""
        moment = "2026-09-04T02:24:17Z"
        new = self.new_estimate(["Blanc.ProrataArithmetic", "Blanc.ProrataWethVaultCode"], moment)
        self.assertEqual(new["kind"], "heavy module")
        self.assertEqual(new["heavy"], ["Blanc.ProrataWethVaultCode"])
        self.assertEqual(new["estimate_gib"], 8)
        actual = next(row for row in self.rows if row["time"].startswith("2026-09-04T02:44"))
        self.assertLess(float(actual["peak_rss_mib"]) / 1024.0, 8.0)

    def test_the_all_modules_list_is_priced_like_the_rebuild_it_is(self) -> None:
        """02:45:54: 294 stale modules spelled as 386 targets slipped in at 8 GiB."""
        moment = "2026-09-04T02:45:54Z"
        landed = next(row for row in self.rows if len(row["modules_rebuilt"]) == 294)
        stale = list(landed["modules_rebuilt"])
        old = self.old_estimate(landed["targets"], moment)
        self.assertEqual(old, 8)
        self.assertTrue(self.fits(old, 16.3))           # admitted, then 10.1 GiB
        self.assertGreater(float(landed["peak_rss_mib"]) / 1024.0, old)
        new = self.new_estimate(stale, moment)
        self.assertEqual(new["kind"], "broader rebuild")
        self.assertEqual(new["estimate_gib"], 12)
        self.assertFalse(self.fits(new["estimate_gib"], 16.3))
        self.assertGreaterEqual(new["estimate_gib"], float(landed["peak_rss_mib"]) / 1024.0)

    def test_the_same_list_with_two_stale_modules_is_sized_from_those_two(self) -> None:
        """03:04:30: the list inherited its own 10 GiB row; two modules were stale."""
        moment = "2026-09-04T03:04:30Z"
        landed = next(row for row in self.rows if len(row["modules_rebuilt"]) == 294)
        old = self.old_estimate(landed["targets"], moment)
        self.assertEqual(old, 11)
        self.assertFalse(self.fits(old, 17.5))          # 50 minutes, then WAIT_TIMEOUT
        stale = ["Blanc.ProrataWethVaultFunctional", "Blanc.ProrataWethVaultDust"]
        new = self.new_estimate(stale, moment)
        self.assertEqual(new["kind"], "measured")
        self.assertEqual(new["width"], 2)
        self.assertEqual(new["estimate_gib"], 5)
        self.assertTrue(self.fits(new["estimate_gib"], 17.5))
        actual = next(row for row in self.rows if row["time"].startswith("2026-09-04T03:55:19"))
        self.assertLessEqual(float(actual["peak_rss_mib"]) / 1024.0, new["estimate_gib"])

    def test_a_fresh_worktree_starts_at_the_narrow_default(self) -> None:
        """02:02:27: jaune's first narrow build refused at 63% free on an 8 GiB default."""
        new = owned.size_stale_set(["Jaune.Fork", "Jaune.Machine"], None, [], SETTINGS, 8)
        self.assertEqual(new["estimate_gib"], 4)
        self.assertFalse(self.fits(8, 15.1))
        self.assertTrue(self.fits(4, 15.1))


class _Harness:
    """A `run_lake_build` with Lake, the semaphore, and the sampler replaced."""

    def __init__(self, *, probe, exit_code=0, rebuilt=("A",), peak_mib=2100.0,
                 lake_run=None):
        self.probe = probe
        self.exit_code = exit_code
        self.rebuilt = list(rebuilt)
        self.peak_mib = peak_mib
        self.lake_run = lake_run
        self.rows: list[dict] = []
        self.acquire = mock.Mock(return_value=(True, "ADMITTED_SOFT"))
        self.release = mock.Mock(return_value=(True, "released"))
        self.output = io.StringIO()

    def run(self, **kwargs) -> int:
        harness = self

        class FakeProc:
            pid = 4321
            stdout = iter([])

            def wait(self, timeout=None):
                return harness.exit_code

        class FakeSampler:
            def __init__(self, _pid, worktree=None):
                self.peak_rss_mib = harness.peak_mib
                self.peak_lean_rss_mib = harness.peak_mib - 600.0
                self.max_concurrent_lean = 1
                self.module_peak_mib = {m: harness.peak_mib - 600.0 for m in harness.rebuilt}
                self.samples = 4
                self.unavailable_samples = 0

            def start(self):
                pass

            def stop(self):
                pass

        class FakeRenewer:
            def __init__(self, *_args, **_kwargs):
                self.verdicts: list[str] = []
                self.refused = False
                self.cleanup_proved = True

            def start(self):
                pass

            def stop(self):
                pass

        patches = [
            patch("creme.build_ownership._worktree_identity", return_value=(Path.cwd(), "g")),
            patch("creme.build_ownership.resolve_toolchain",
                  return_value=(Path("/tool/lake"), Path("/tool/lean"), Path("/tool"))),
            patch("creme.build_ownership.stale_evidence", return_value=self.probe),
            patch("creme.build_ownership.worktree_digests", return_value=("tc", "mf")),
            patch("creme.build_ownership.semaphore.adaptive_acquire", self.acquire),
            patch("creme.build_ownership.semaphore.adaptive_release", self.release),
            patch("creme.build_ownership.guard_bin", return_value=Path("/guard")),
            patch("creme.build_ownership.subprocess.Popen", return_value=FakeProc()),
            patch("creme.build_ownership.ProcessSampler", FakeSampler),
            patch("creme.build_ownership.RenewalThread", FakeRenewer),
            patch("creme.build_ownership._process_group_alive", return_value=False),
            patch("creme.build_ownership._module_hashes", return_value={}),
            patch("creme.build_ownership._parse_build_output",
                  return_value=(self.rebuilt, [], {m: 1.0 for m in self.rebuilt})),
            patch("creme.build_ownership._swap_gib", return_value=1.0),
            patch("creme.build_ownership.append_ledger", side_effect=self.rows.append),
        ]
        if self.lake_run is not None:
            patches.append(patch("creme.build_ownership.subprocess.run", return_value=self.lake_run))
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            return owned.run_lake_build("g", ["T"], stdout=self.output, **kwargs)


class WrapperSurfaceTest(unittest.TestCase):
    """Items 2, 3, and 5 at the wrapper: FRESH, the estimate's provenance, the closure."""

    def probe(self, stale_set):
        return {
            "roots": ["T"], "package_roots": ["T"], "resolution": "T (module)",
            "stale": None if stale_set is None else len(stale_set),
            "detail": "probe frontier ['T']; fixture", "stale_set": stale_set,
            "graph": {name: set() for name in (stale_set or [])},
        }

    def test_the_probe_prints_the_full_stale_closure(self) -> None:
        harness = _Harness(
            probe=self.probe(["Lib.A", "Lib.B", "Lib.C"]),
            lake_run=SimpleNamespace(returncode=3, stdout="frontier\n", stderr=""),
        )
        with _isolated():
            code = harness.run(probe=True)
        self.assertEqual(code, 3)
        text = harness.output.getvalue()
        stale_lines = [line for line in text.splitlines() if line.startswith("stale: ")]
        self.assertEqual(len(stale_lines), 1, text)
        self.assertIn("3 module(s)", stale_lines[0])
        self.assertIn("Lib.A, Lib.B, Lib.C", stale_lines[0])
        row = harness.rows[0]
        self.assertTrue(row["probe"])
        self.assertEqual(row["stale_modules"], 3)
        self.assertIn("Lib.C", row["stale_detail"])
        summary = json.loads(text.splitlines()[-1])
        self.assertEqual(summary["stale_set"], ["Lib.A", "Lib.B", "Lib.C"])

    def test_an_unmeasurable_closure_says_so_on_the_probe(self) -> None:
        harness = _Harness(
            probe=self.probe(None),
            lake_run=SimpleNamespace(returncode=3, stdout="frontier\n", stderr=""),
        )
        with _isolated():
            harness.run(probe=True, contention="sensitive")
        self.assertIn("stale: unmeasured", harness.output.getvalue())

    def test_a_fresh_probe_takes_no_hold(self) -> None:
        harness = _Harness(probe=self.probe([]), rebuilt=())
        with _isolated():
            code = harness.run(contention="sensitive")
        self.assertEqual(code, 0)
        harness.acquire.assert_not_called()
        harness.release.assert_not_called()
        self.assertEqual(harness.rows[0]["admission"], "NOT_REQUIRED_FRESH")
        self.assertIn("takes no hold", harness.output.getvalue())

    def test_a_stale_build_still_takes_a_hold_and_releases_it(self) -> None:
        harness = _Harness(probe=self.probe(["A"]))
        with _isolated():
            harness.run()
        harness.acquire.assert_called_once()
        harness.release.assert_called_once()
        self.assertEqual(harness.rows[0]["admission"], "ADMITTED_SOFT")

    def test_the_row_records_each_modules_lean_peak(self) -> None:
        harness = _Harness(probe=self.probe(["A", "B"]), rebuilt=("A", "B"), peak_mib=2600.0)
        with _isolated():
            harness.run()
        self.assertEqual(harness.rows[0]["module_peak_mib"], {"A": 2000.0, "B": 2000.0})
        self.assertTrue(owned._valid_ledger_row({
            "schema_version": 1, "time": "2026-09-04T00:00:00Z", **harness.rows[0],
        }))

    def test_the_queue_is_told_whether_the_estimate_was_derived(self) -> None:
        harness = _Harness(probe=self.probe(["A"]))
        with _isolated():
            harness.run(wait_seconds=5)
        source = harness.acquire.call_args.kwargs["estimate_source"]
        self.assertTrue(source.startswith("derived: narrow default"), source)
        explicit = _Harness(probe=self.probe(["A"]))
        with _isolated():
            explicit.run(memory_gib=6, wait_seconds=5)
        source = explicit.acquire.call_args.kwargs["estimate_source"]
        self.assertTrue(source.startswith("explicit --memory-gib 6"), source)
        self.assertIn("the evidence supports 4 GiB", source)

    def test_an_explicit_class_still_probes_for_freshness(self) -> None:
        harness = _Harness(probe=self.probe([]), rebuilt=())
        with _isolated():
            harness.run(contention="exclusive", memory_gib=8)
        harness.acquire.assert_not_called()
        self.assertEqual(harness.rows[0]["admission"], "NOT_REQUIRED_FRESH")


class SamplerAttributionTest(unittest.TestCase):
    def test_a_lean_command_line_names_its_module(self) -> None:
        worktree = Path("/w/blanc/.worktrees/g")
        self.assertEqual(
            owned._lean_module("/tc/bin/lean ./Blanc/Foo.lean -o ./.lake/build/lib/lean/Blanc/Foo.olean --json", worktree),
            "Blanc.Foo",
        )
        self.assertEqual(
            owned._lean_module(f"/tc/bin/lean {worktree}/Blanc/Bar/Baz.lean -o x.olean", worktree),
            "Blanc.Bar.Baz",
        )
        self.assertEqual(
            owned._lean_module("/tc/bin/lean /elsewhere/Q.lean -o /w/.lake/build/lib/lean/Pkg/Q.olean", worktree),
            "Pkg.Q",
        )
        self.assertIsNone(owned._lean_module("/tc/bin/lake build", worktree))
        self.assertIsNone(owned._lean_module("/tc/bin/lean --worker", worktree))

    def test_the_sampler_records_a_peak_per_module(self) -> None:
        snapshots = iter([
            {1: (0, 1000, "python3"), 2: (1, 500, "/tc/bin/lake build"),
             3: (2, 1_048_576, "/tc/bin/lean ./P/A.lean -o ./.lake/build/lib/lean/P/A.olean")},
            {1: (0, 1000, "python3"), 2: (1, 500, "/tc/bin/lake build"),
             3: (2, 2_097_152, "/tc/bin/lean ./P/A.lean -o ./.lake/build/lib/lean/P/A.olean"),
             4: (2, 524_288, "/tc/bin/lean ./P/B.lean -o ./.lake/build/lib/lean/P/B.olean")},
        ])
        sampler = owned.ProcessSampler(1, interval=0.001, worktree=Path("/w"))

        def snapshot():
            try:
                return next(snapshots)
            except StopIteration:
                sampler.stop_event.set()
                return None

        with patch("creme.build_ownership._process_snapshot", side_effect=snapshot):
            sampler.run()
        self.assertEqual(sampler.module_peak_mib, {"P.A": 2048.0, "P.B": 512.0})
        self.assertEqual(sampler.max_concurrent_lean, 2)
        self.assertEqual(sampler.peak_lean_rss_mib, 2048.0)


class ExecutableAttributionTest(unittest.TestCase):
    """Item 4: `lake`/`lean` by executable, never by the substring `lean`."""

    def test_the_apple_daemon_is_not_elaborating(self) -> None:
        self.assertFalse(semaphore._is_elaborating(
            "com.apple.MobileSoftwareUpdate.CleanupPreparePathService"
        ))
        self.assertFalse(semaphore._is_elaborating("CleanupPreparePathService"))
        self.assertFalse(semaphore._is_elaborating("/bin/zsh -c pgrep -f 'lean --worker'"))
        self.assertFalse(semaphore._is_elaborating("lean-lsp-mcp"))
        self.assertFalse(semaphore._is_elaborating(""))

    def test_lean_and_lake_are_recognised_by_name_or_toolchain_path(self) -> None:
        self.assertTrue(semaphore._is_elaborating("lean"))
        self.assertTrue(semaphore._is_elaborating("lake"))
        self.assertTrue(semaphore._is_elaborating(
            "/Users/agent/.elan/toolchains/leanprover--lean4---v4.32.1/bin/lean --worker"
        ))
        self.assertTrue(semaphore._is_elaborating(
            "/Users/a b/.elan/toolchains/leanprover--lean4---v4.32.1/bin/lake serve"
        ))

    def test_reclaim_classifies_by_executable_too(self) -> None:
        loop = reclaim.Process(
            1, 0, 0, "x",
            '/bin/zsh -c for i in $(seq 1 200); do pgrep -f "lean --worker|lean --server|lake serve" >/dev/null || break; sleep 15; done',
        )
        self.assertEqual(loop.kind, "zsh")
        self.assertFalse(reclaim.is_candidate(loop))
        self.assertFalse(reclaim.is_lean_worker(loop.command))
        serve = reclaim.Process(2, 0, 0, "x", "/x/.elan/toolchains/t/bin/lake serve")
        self.assertEqual(serve.kind, "lake-serve")
        self.assertTrue(reclaim.is_candidate(serve))
        worker = reclaim.Process(3, 0, 0, "x", "/Users/a b/.elan/toolchains/t/bin/lean --worker /f.lean")
        self.assertEqual(worker.kind, "lean-worker")
        self.assertTrue(reclaim.is_candidate(worker))
        self.assertTrue(reclaim.is_lean_worker(worker.command))
        server = reclaim.Process(4, 0, 0, "x", "/tc/bin/lean --server")
        self.assertEqual(server.kind, "lean-server")
        self.assertEqual(reclaim.Process(5, 0, 0, "x", "").kind, "process")

    def test_goal_of_directory(self) -> None:
        self.assertEqual(idle_workers.goal_of_directory("/Users/a/blanc/.worktrees/g1/Blanc"), "g1")
        self.assertEqual(idle_workers.goal_of_directory("/Users/a/jaune/.worktrees/g1-control"), "g1")
        self.assertIsNone(idle_workers.goal_of_directory("/Users/a/blanc"))
        self.assertIsNone(idle_workers.goal_of_directory(None))


class _SignalBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = ProcessAdapter(free_percent=80, total_gib=32)
        self.policy = {
            "task_memory_gib": 2, "heavy_workers": 2, "light_workers": 4,
            "physical_memory_gib": 32.0, "profile_status": "VALID",
        }
        for patcher in (
            patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": self.tmp.name}, clear=False),
            patch("creme.semaphore.get_adapter", return_value=self.adapter),
            patch("creme.semaphore._runtime_admission_policy", return_value=self.policy),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scope = self.root / "blanc" / ".worktrees" / "gate"
        self.other = self.root / "blanc" / ".worktrees" / "peer"
        self.scope.mkdir(parents=True)
        self.other.mkdir(parents=True)
        self.scopes = {"gate": (self.scope,), "peer": (self.other,)}
        patcher = patch(
            "creme.semaphore._goal_scope_roots",
            side_effect=lambda label, adapter: self.scopes.get(label, ()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def hold(self, label, contention="tolerant"):
        ok, detail = semaphore.adaptive_acquire(
            label, "fixture", memory_gib=2, contention=contention,
            adapter=self.adapter, policy=self.policy,
        )
        assert ok, detail

    def make_idle(self, label):
        semaphore.refresh_signals(self.adapter)
        queue = semaphore._load_queue(self.root)[0]
        queue["activity"][label] = semaphore._now() - (semaphore.IDLE_HOLD_SECONDS + 30)
        semaphore._save_queue(self.root, queue)

    def log_rows(self, action=None):
        path = self.root / "log.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [row for row in rows if action is None or row["action"] == action]


class HoldSignalTest(_SignalBase):
    def test_the_apple_daemon_no_longer_suspends_idleness_host_wide(self) -> None:
        self.hold("gate")
        self.adapter.processes = [
            {"pid": 45840, "ppid": 1, "rss_kib": 4000,
             "command": "com.apple.MobileSoftwareUpdate.CleanupPreparePathService"},
        ]
        self.adapter.cwds = {}
        self.make_idle("gate")
        text = semaphore.status_text(self.adapter)
        self.assertIn("IDLE_HOLD", text)
        self.assertNotIn("ATTRIBUTION_UNAVAILABLE", text)

    def test_an_exclusive_hold_is_not_judged_idle_and_says_why(self) -> None:
        self.hold("gate", contention="exclusive")
        self.adapter.processes = []
        self.make_idle("gate")
        text = semaphore.status_text(self.adapter)
        self.assertNotIn("IDLE_HOLD:", text)
        self.assertIn("IDLE_HOLD not judged", text)
        self.assertIn("exclusive", text)
        self.assertIn("timing and non-Lean lanes", text)

    def test_a_sensitive_hold_over_a_python_lane_is_still_idle(self) -> None:
        self.hold("gate", contention="sensitive")
        self.adapter.processes = [
            {"pid": 999200, "ppid": 1, "rss_kib": 4000, "command": "python3"},
        ]
        self.make_idle("gate")
        self.assertIn("IDLE_HOLD:", semaphore.status_text(self.adapter))

    def test_an_exclusive_hold_can_still_be_stranded(self) -> None:
        self.hold("gate", contention="exclusive")
        path = self.root / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        dead = os.fork()
        if dead == 0:
            os._exit(0)
        os.waitpid(dead, 0)
        state["hard"]["pid"] = dead
        state["hard"]["acquired_at"] = 1
        state["hard"]["renewed_at"] = 1
        path.write_text(json.dumps(state), encoding="utf-8")
        self.adapter.processes = []
        self.assertIn("STRANDED", semaphore.status_text(self.adapter))

    def test_attribution_failure_is_logged_once_with_its_reason(self) -> None:
        self.hold("gate")
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {}            # the sample answers for no pid
        for _ in range(3):
            text = semaphore.status_text(self.adapter)
        self.assertIn("ATTRIBUTION_UNAVAILABLE", text)
        rows = self.log_rows("attribution")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["verdict"], "REFUSED")
        self.assertEqual(rows[0]["label"], "gate")
        self.assertIn("pid 999100 (lean)", rows[0]["detail"])
        self.assertIn("did not answer", rows[0]["detail"])
        # A different cause is a new row; the same cause again is not.
        self.adapter.cwds = None
        semaphore.status_text(self.adapter)
        semaphore.status_text(self.adapter)
        rows = self.log_rows("attribution")
        self.assertEqual(len(rows), 2, rows)
        self.assertIn("sample is unavailable", rows[1]["detail"])
        # Attribution coming back closes the episode with one OK row.
        self.adapter.cwds = {999100: str(self.scope / "Blanc")}
        semaphore.status_text(self.adapter)
        semaphore.status_text(self.adapter)
        rows = self.log_rows("attribution")
        self.assertEqual(len(rows), 3, rows)
        self.assertEqual(rows[2]["verdict"], "OK")
        self.assertIn("ATTRIBUTION_RESTORED", rows[2]["detail"])

    def test_the_audit_reads_only_the_tail_of_a_long_log(self) -> None:
        self.hold("gate")
        filler = json.dumps({"time": "2026-09-04T00:00:00Z", "action": "renew", "label": "x",
                             "verdict": "OK", "detail": "x" * 200})
        with (self.root / "log.jsonl").open("a", encoding="utf-8") as handle:
            for _ in range(600):
                handle.write(filler + "\n")
        self.adapter.processes = [
            {"pid": 999100, "ppid": 1, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {}
        semaphore.status_text(self.adapter)
        semaphore.status_text(self.adapter)
        self.assertEqual(len(self.log_rows("attribution")), 1)

    def test_two_goals_sharing_one_client_pid_are_attributed_by_working_directory(self) -> None:
        """The master model: every worker is a subagent of one client process."""
        self.hold("gate")
        self.hold("peer")
        client = 74739
        self.adapter.processes = [
            {"pid": client, "ppid": 1, "rss_kib": 500_000, "command": "claude"},
            {"pid": 999301, "ppid": client, "rss_kib": 4000, "command": "lean"},
        ]
        self.adapter.cwds = {999301: str(self.scope / "Blanc")}
        self.make_idle("gate")
        self.make_idle("peer")
        lines = semaphore.status_text(self.adapter).splitlines()
        following = {
            line.split()[0]: (lines[index + 1] if index + 1 < len(lines) else "")
            for index, line in enumerate(lines)
            if line.startswith("  gate (") or line.startswith("  peer (")
        }
        self.assertNotIn("IDLE_HOLD", following["gate"])   # its lean is in its worktree
        self.assertIn("IDLE_HOLD", following["peer"])      # nothing of its own anywhere
        self.assertIn(str(self.other), following["peer"])

    def test_idle_workers_are_owned_by_the_goal_worktree_not_the_client(self) -> None:
        client = 74739
        self.adapter.processes = []
        self.adapter.workers = [
            {"pid": 999401, "ppid": client, "rss_kib": 2 * 1024 * 1024, "cpu_seconds": 5.0,
             "command": "lean --worker", "ancestry": [{"pid": client, "command": "claude"}]},
            {"pid": 999402, "ppid": client, "rss_kib": 1024 * 1024, "cpu_seconds": 5.0,
             "command": "lean --worker", "ancestry": [{"pid": client, "command": "claude"}]},
        ]
        self.adapter.cwds = {999401: str(self.scope / "Blanc"), 999402: str(self.root / "blanc")}
        self.adapter.client_pattern = re.compile(r"claude")
        semaphore.refresh_signals(self.adapter)
        report = semaphore.refresh_signals(self.adapter)["lean_workers"]
        owners = {worker["pid"]: worker["owner"] for worker in report["workers"]}
        self.assertEqual(owners[999401], "goal gate")
        self.assertEqual(owners[999402], f"client claude pid {client}")
        self.assertIn("goal gate", semaphore.status_text(self.adapter))
        self.assertIn("--goal GOAL", semaphore.status_text(self.adapter))


class _ReclaimingAdapter(ProcessAdapter):
    """Answers reclaim dry-runs by cwd when scoped, by client ancestry when not."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls: list[list[str]] = []

    def reclaim(self, options):
        self.calls.append(list(options))
        roots = [Path(options[i + 1]) for i, opt in enumerate(options) if opt == "--scope-root"]
        candidates = [worker["pid"] for worker in (self.workers or [])]
        if roots:
            owned = [
                pid for pid in candidates
                if self.cwds and pid in self.cwds
                and any(Path(self.cwds[pid]).is_relative_to(root) for root in roots)
            ]
        else:
            owned = candidates
        return self.result("lean_reclaim", "OK", "fixture", {
            "owned": [{"pid": pid} for pid in owned],
            "termination_order": [int(options[i + 1]) for i, opt in enumerate(options) if opt == "--only-pid"],
        })


class IdleWorkerReclaimScopeTest(_SignalBase):
    def setUp(self) -> None:
        super().setUp()
        self.adapter = _ReclaimingAdapter(free_percent=80, total_gib=32)
        for patcher in (
            patch("creme.semaphore.get_adapter", return_value=self.adapter),
            patch("creme.cli.get_adapter", return_value=self.adapter),
            patch("creme.cli._goal_worktree_roots",
                  side_effect=lambda label, adapter: self.scopes[label]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        client = 74739
        self.adapter.processes = []
        self.adapter.workers = [
            {"pid": 999401, "ppid": client, "rss_kib": 2 * 1024 * 1024, "cpu_seconds": 5.0,
             "command": "lean --worker", "ancestry": [{"pid": client, "command": "claude"}]},
            {"pid": 999402, "ppid": client, "rss_kib": 1024 * 1024, "cpu_seconds": 5.0,
             "command": "lean --worker", "ancestry": [{"pid": client, "command": "claude"}]},
        ]
        self.adapter.cwds = {999401: str(self.scope / "Blanc"), 999402: str(self.other / "Jaune")}
        semaphore.refresh_signals(self.adapter)
        queue = semaphore._load_queue(self.root)[0]
        for pid in ("999401", "999402"):
            queue["workers"][pid]["seen_at"] -= 3600
        semaphore._save_queue(self.root, queue)

    def run_idle_workers(self, goal=None, dry_run=True):
        arguments = SimpleNamespace(idle_workers=1, goal=goal, dry_run=dry_run,
                                    hard_pressure=False, wind_down=None)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cmd_reclaim(arguments)
        return code, json.loads(buffer.getvalue())

    def test_without_a_goal_no_worker_inside_a_goal_worktree_is_a_target(self) -> None:
        code, report = self.run_idle_workers()
        self.assertEqual(code, 0)
        self.assertEqual(report["owned_targets"], [])
        reported = {item["pid"]: item for item in report["reported_not_owned"]}
        self.assertEqual(set(reported), {999401, 999402})
        self.assertIn("--goal", reported[999401]["reason"])
        self.assertEqual(reported[999401]["owner"], "goal gate")
        self.assertTrue(reported[999401]["owner_should_run"].endswith("--goal gate"))
        self.assertTrue(reported[999402]["owner_should_run"].endswith("--goal peer"))

    def test_a_goal_reclaims_only_inside_its_own_worktrees(self) -> None:
        code, report = self.run_idle_workers(goal="gate", dry_run=False)
        self.assertEqual(code, 0)
        self.assertEqual(report["owned_targets"], [999401])
        self.assertEqual([item["pid"] for item in report["reported_not_owned"]], [999402])
        self.assertIn("goal peer's worktree, not gate's", report["reported_not_owned"][0]["reason"])
        scoped = [call for call in self.adapter.calls if "--scope-root" in call]
        self.assertEqual(len(scoped), 2)          # the dry-run and the signalling call
        self.assertIn(str(self.scope), scoped[-1])
        self.assertEqual(report["reclaim"]["data"]["termination_order"], [999401])

    def test_a_goal_without_a_worktree_is_refused(self) -> None:
        from creme.task_wind_down import WorktreeScopeError
        with patch("creme.cli._goal_worktree_roots", side_effect=WorktreeScopeError("no scope")):
            code, report = self.run_idle_workers(goal="ghost")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "REFUSED")

    def test_goal_is_only_meaningful_with_idle_workers(self) -> None:
        arguments = SimpleNamespace(idle_workers=None, goal="gate", dry_run=True,
                                    hard_pressure=False, wind_down=None)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cmd_reclaim(arguments), 2)
        self.assertIn("only meaningful", buffer.getvalue())


class FitLineProvenanceTest(_SignalBase):
    """Item 3: the fit line says whether the estimate was derived."""

    def announce(self, memory_gib, estimate_source):
        announced: list[str] = []
        semaphore._waiting_admit(
            "queued", "waiting", 600, memory_gib=memory_gib, contention="tolerant",
            adapter=self.adapter, policy=self.policy, wait_seconds=1, poll_seconds=0.01,
            announce=announced.append, estimate_source=estimate_source,
        )
        return "\n".join(announced)

    def test_a_derived_estimate_is_never_called_explicit(self) -> None:
        text = self.announce(12, "derived: measured stale set: 1 module(s) all measured")
        self.assertIn("estimate 12 GiB is derived, not explicit", text)
        self.assertIn("measured stale set", text)
        self.assertNotIn("an explicit --memory-gib", text)

    def test_an_explicit_estimate_is_called_explicit_with_the_evidence(self) -> None:
        text = self.announce(12, "explicit --memory-gib 12; the evidence supports 4 GiB (x)")
        self.assertIn("estimate 12 GiB is explicit", text)
        self.assertIn("the evidence supports 4 GiB", text)
        self.assertIn("an explicit --memory-gib 12 exceeds this host's default", text)

    def test_the_command_line_keeps_its_meaning(self) -> None:
        text = self.announce(12, None)
        self.assertIn("estimate 12 GiB is explicit", text)
        self.assertIn("an explicit --memory-gib 12 exceeds", text)
        self.assertNotIn("is explicit", self.announce(None, None))

    def test_adaptive_acquire_forwards_the_source(self) -> None:
        announced: list[str] = []
        semaphore.adaptive_acquire(
            "queued", "waiting", memory_gib=3, contention="tolerant",
            adapter=self.adapter, policy=self.policy, wait_seconds=1, poll_seconds=0.01,
            announce=announced.append, estimate_source="derived: narrow default 3 GiB",
        )
        self.assertTrue(any("derived, not explicit" in line for line in announced), announced)


class LiveStateCompatibilityTest(unittest.TestCase):
    """The on-disk schema of the live semaphore state is unchanged.

    Reads copies of the live files only; the live directory is never opened
    by the code under test.
    """

    NAMES = ("state.json", "queue.json", "master.json")

    def setUp(self) -> None:
        if not all((LIVE_STATE / name).is_file() for name in self.NAMES):
            self.skipTest("no live semaphore state on this host")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in self.NAMES:
            shutil.copyfile(LIVE_STATE / name, self.root / name)
        (self.root / "log.jsonl").touch()
        self.raw = {name: json.loads((self.root / name).read_text(encoding="utf-8")) for name in self.NAMES}
        self.adapter = ProcessAdapter(free_percent=80, total_gib=24, processes=[], workers=[])
        self.policy = dict(HOST_POLICY)
        for patcher in (
            patch.dict(os.environ, {"CREME_SEMAPHORE_DIR": str(self.root)}, clear=False),
            patch("creme.semaphore.get_adapter", return_value=self.adapter),
            patch("creme.semaphore._runtime_admission_policy", return_value=self.policy),
            patch("creme.semaphore._goal_scope_roots", side_effect=lambda label, adapter: ()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_live_files_validate_under_the_candidate_reader(self) -> None:
        state = semaphore._validate(self.raw["state.json"])
        self.assertEqual(set(state), set(semaphore._empty_state()))
        for hold in ([state["hard"]] if state["hard"] else []) + state["soft"]:
            self.assertEqual(set(hold), semaphore.HOLD_KEYS)
        queue, notes = semaphore._load_queue(self.root)
        self.assertEqual(notes, [], notes)
        self.assertEqual(set(self.raw["queue.json"]), set(semaphore._empty_queue()))
        self.assertEqual(len(queue["waiters"]), len(self.raw["queue.json"]["waiters"]))
        for waiter in self.raw["queue.json"]["waiters"]:
            self.assertEqual(set(waiter), semaphore.WAITER_KEYS)
        master = semaphore._validate_master(self.raw["master.json"])
        self.assertEqual(set(master), set(semaphore._empty_master()))

    def test_a_status_pass_leaves_the_schema_as_it_found_it(self) -> None:
        before = (self.root / "state.json").read_bytes()
        text = semaphore.status_text(self.adapter)
        self.assertIn("hard:", text)
        self.assertEqual((self.root / "state.json").read_bytes(), before)
        after = json.loads((self.root / "queue.json").read_text(encoding="utf-8"))
        self.assertEqual(set(after), set(semaphore._empty_queue()))
        self.assertEqual(after["schema_version"], semaphore.QUEUE_SCHEMA_VERSION)
        for waiter in after["waiters"]:
            self.assertEqual(set(waiter), semaphore.WAITER_KEYS)
        for observation in after["workers"].values():
            self.assertEqual(set(observation), idle_workers.OBSERVATION_KEYS)
        self.assertEqual(
            json.loads((self.root / "master.json").read_text(encoding="utf-8")),
            self.raw["master.json"],
        )

    def test_the_candidates_writers_keep_the_hold_and_waiter_shapes(self) -> None:
        hold = semaphore._hold("g", "note", 300, memory_gib=4, contention="tolerant")
        self.assertEqual(set(hold), semaphore.HOLD_KEYS)
        note, gib, contention = semaphore._decode_admission_note(hold["note"], 8)
        self.assertEqual((note, gib, contention), ("note", 4, "tolerant"))
        self.assertEqual(set(semaphore._empty_queue()), {"schema_version", "waiters", "activity", "workers"})


if __name__ == "__main__":
    unittest.main()
