"""creme-admission-fallback-v1: a module's own peak beats the sibling aggregate.

Proves the TWG-reachability sibling-pollution fix.  A narrow 8-module row
with aggregate peak 9523.4 MiB (9.30 GiB) used to price every member's
single-module rebuild at ceil(9.30) + 1 = 11 GiB, while the estimate source
still printed "narrow default 4 GiB".  Members with a recorded per-module
peak are now floored by their own direct evidence, and whenever a fallback
contribution alone sets the ask the source — quoted verbatim by the fit:
line — names the module, the fallback GiB, and the originating row.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from creme import build_ownership as owned
from creme.profile import ADMISSION_DEFAULTS


SETTINGS = dict(ADMISSION_DEFAULTS)

# The demonstrating row, cut from the TWG worker's polluting-row.json.
POLLUTING_TIME = "2026-09-13T07:25:28.959958Z"
POLLUTING_MODULES = [
    "Blanc.DripStackSafetyRegion576",
    "Blanc.LidoCircuitBreakerAccess",
    "Blanc.LidoCircuitBreakerAuthority",
    "Blanc.LidoCircuitBreakerDeploymentTrace",
    "Blanc.LidoCircuitBreakerOwnerClosure",
    "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization",
    "Blanc.LidoTriggerableWithdrawalsGatewayPauseFor",
    "Blanc.LidoTriggerableWithdrawalsGatewayPauseUntilResume",
]
POLLUTING_PEAKS_MIB = {
    "Blanc.DripStackSafetyRegion576": 6092.1,
    "Blanc.LidoCircuitBreakerAccess": 3035.9,
    "Blanc.LidoCircuitBreakerAuthority": 1936.4,
    "Blanc.LidoCircuitBreakerDeploymentTrace": 5788.8,
    "Blanc.LidoCircuitBreakerOwnerClosure": 1593.0,
    "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization": 1379.9,
    "Blanc.LidoTriggerableWithdrawalsGatewayPauseFor": 1454.2,
    "Blanc.LidoTriggerableWithdrawalsGatewayPauseUntilResume": 1471.5,
}
POLLUTING_SECONDS = {
    "Blanc.DripStackSafetyRegion576": 41.0,
    "Blanc.LidoCircuitBreakerAccess": 42.0,
    "Blanc.LidoCircuitBreakerAuthority": 10.0,
    "Blanc.LidoCircuitBreakerDeploymentTrace": 18.0,
    "Blanc.LidoCircuitBreakerOwnerClosure": 3.5,
    "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization": 0.932,
    "Blanc.LidoTriggerableWithdrawalsGatewayPauseFor": 0.948,
    "Blanc.LidoTriggerableWithdrawalsGatewayPauseUntilResume": 1.2,
}
# The four atomic singles the TWG worker proved NEVER_FITS at the polluted ask.
SINGLES = [
    "Blanc.LidoCircuitBreakerAccess",
    "Blanc.LidoCircuitBreakerAuthority",
    "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization",
    "Blanc.LidoTriggerableWithdrawalsGatewayPauseFor",
]
POLLUTING_AGGREGATE_MIB = 9523.4


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


def _row(time, rebuilt, peak_gib, *, lean_gib=None, concurrency=1,
         seconds=None, module_peaks=None, targets=("T",), worktree="/w",
         exit_code=0, identity=None, samples=None) -> dict:
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
    if samples is not None:
        row["sampling_samples"] = samples
        row["sampling_unavailable"] = 0
    if identity is not None:
        row.update({"identity_status": "exact", **identity})
    return row


def _recent_time() -> str:
    """A ledger-window-safe row time: fixed dates rot out of the 30d cohort."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def _identity(modules, source: str) -> dict:
    return {
        "repository_identity": "repo-a",
        "input_context": "tc-mf-config-threads2",
        "module_inputs": {module: source for module in modules},
    }


def _polluting_row(time: str = POLLUTING_TIME) -> dict:
    return _row(
        time, POLLUTING_MODULES, POLLUTING_AGGREGATE_MIB / 1024.0,
        lean_gib=POLLUTING_PEAKS_MIB["Blanc.DripStackSafetyRegion576"] / 1024.0,
        concurrency=2, seconds=dict(POLLUTING_SECONDS),
        module_peaks={module: mib / 1024.0 for module, mib in POLLUTING_PEAKS_MIB.items()},
        identity=_identity(POLLUTING_MODULES, "old"), samples=3,
    )


def _current(modules) -> dict:
    return _identity(modules, "new")


class SiblingPollutionTest(unittest.TestCase):
    """Fix 1: a multi-module narrow row no longer taxes each member its aggregate."""

    def test_the_fixture_row_is_the_polluting_shape(self) -> None:
        # Guards the premise: narrow (8 <= 8) with an aggregate that prices 11.
        self.assertEqual(len(POLLUTING_MODULES), SETTINGS["tolerant_module_count"])
        self.assertEqual(
            math.ceil(POLLUTING_AGGREGATE_MIB / 1024.0) + SETTINGS["estimate_margin_gib"], 11,
        )

    def test_polluting_row_prices_each_single_from_its_own_peak(self) -> None:
        row = _polluting_row()
        current = _current(POLLUTING_MODULES)
        # Access elaborated 42 s in the polluting row, so the unchanged heavy
        # rule keeps it at the profile default — still priced from its own
        # 2.96 GiB peak (term 4, below the default), never the 9.30 aggregate.
        expected = {
            "Blanc.LidoCircuitBreakerAccess": ("heavy module", 8),
            "Blanc.LidoCircuitBreakerAuthority": ("narrow default", 4),
            "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization": ("narrow default", 4),
            "Blanc.LidoTriggerableWithdrawalsGatewayPauseFor": ("narrow default", 4),
        }
        for module in SINGLES:
            with self.subTest(module=module):
                sizing = owned.size_stale_set(
                    [module], {module: set()}, [row], SETTINGS, 8, current,
                )
                own_gib = POLLUTING_PEAKS_MIB[module] / 1024.0
                kind, estimate = expected[module]
                self.assertEqual(sizing["kind"], kind)
                self.assertEqual(sizing["unmeasured"], [module])
                self.assertEqual(sizing["fallback_build_modules"], [])
                self.assertEqual(sizing["fallback_modules"], [module])
                self.assertAlmostEqual(sizing["fallback_peak_gib"], round(own_gib, 2), places=2)
                self.assertEqual(sizing["estimate_gib"], estimate)

    def test_fallback_origin_records_the_module_peak_and_its_row(self) -> None:
        evidence = owned.module_cost_evidence(
            [_polluting_row()], SETTINGS, _current(POLLUTING_MODULES),
        )
        module = "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization"
        origin = evidence["fallback_origin"][module]
        self.assertAlmostEqual(origin["peak_gib"], POLLUTING_PEAKS_MIB[module] / 1024.0)
        self.assertEqual(origin["kind"], "module peak")
        self.assertEqual(origin["row_time"], POLLUTING_TIME)
        self.assertNotIn(module, evidence["fallback_build_peak_gib"])

    def test_single_module_row_keeps_the_whole_build_floor(self) -> None:
        # Boundary of the sibling rule: one rebuilt module means no siblings,
        # so the aggregate stays the floor (the documented 7.32-vs-6.74 case).
        time = "2026-09-13T08:00:00Z"
        row = _row(
            time, ["A"], 7.32, lean_gib=6.74, module_peaks={"A": 6.74},
            identity=_identity(["A"], "old"), samples=3,
        )
        sizing = owned.size_stale_set(["A"], {"A": set()}, [row], SETTINGS, 8, _current(["A"]))
        self.assertEqual(sizing["kind"], "narrow default")
        self.assertEqual(sizing["estimate_gib"], 9)
        self.assertEqual(sizing["fallback_build_modules"], ["A"])
        self.assertIn("A", sizing["source"])
        self.assertIn("7.32", sizing["source"])
        self.assertIn("whole-build aggregate", sizing["source"])

    def test_narrow_row_without_recorded_peaks_keeps_the_aggregate_floor(self) -> None:
        # No per-module peak anywhere: there is nothing more specific, so the
        # aggregate still floors each member — and is now named as such.
        time = "2026-09-13T08:10:00Z"
        row = _row(
            time, ["A", "B"], 7.5, lean_gib=6.0, concurrency=2,
            identity=_identity(["A", "B"], "old"), samples=3,
        )
        sizing = owned.size_stale_set(["A"], {"A": set()}, [row], SETTINGS, 8, _current(["A", "B"]))
        self.assertEqual(sizing["estimate_gib"], 9)
        self.assertIn("fallback prices A at 7.50 GiB", sizing["source"])
        self.assertIn(time, sizing["source"])
        self.assertIn("whole-build aggregate", sizing["source"])


class FallbackMessagingTest(unittest.TestCase):
    """Fix 2: a fallback-driven ask names the module, peak, and row."""

    def test_dominating_module_peak_fallback_is_named(self) -> None:
        time = "2026-09-13T08:20:00Z"
        row = _row(
            time, ["A", "B"], 9.3, lean_gib=6.0, concurrency=2,
            module_peaks={"A": 6.5, "B": 1.0},
            identity=_identity(["A", "B"], "old"), samples=3,
        )
        sizing = owned.size_stale_set(["A"], {"A": set()}, [row], SETTINGS, 8, _current(["A", "B"]))
        # The 9.3 GiB sibling aggregate is not attributed; A's own 6.5 GiB
        # peak sets the ask at ceil(6.5) + 1 = 8 instead of 11.
        self.assertEqual(sizing["estimate_gib"], 8)
        self.assertIn("fallback prices A at 6.50 GiB", sizing["source"])
        self.assertIn(time, sizing["source"])
        self.assertIn("module peak", sizing["source"])

    def test_quiet_source_when_fallback_does_not_set_the_ask(self) -> None:
        module = "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization"
        sizing = owned.size_stale_set(
            [module], {module: set()}, [_polluting_row()], SETTINGS, 8,
            _current(POLLUTING_MODULES),
        )
        self.assertEqual(sizing["estimate_gib"], 4)
        self.assertTrue(sizing["source"].startswith("narrow default 4 GiB"))
        self.assertNotIn("fallback prices", sizing["source"])

    def test_fit_note_quotes_the_named_fallback(self) -> None:
        time = "2026-09-13T08:20:00Z"
        row = _row(
            time, ["A", "B"], 9.3, lean_gib=6.0, concurrency=2,
            module_peaks={"A": 6.5, "B": 1.0},
            identity=_identity(["A", "B"], "old"), samples=3,
        )
        sizing = owned.size_stale_set(["A"], {"A": set()}, [row], SETTINGS, 8, _current(["A", "B"]))
        # The semaphore wait path prints this note verbatim on its fit: line,
        # so naming here is naming on fit:.
        note = owned._estimate_note({"source": sizing["source"]})
        self.assertTrue(note.startswith("derived: "))
        self.assertIn("fallback prices A at 6.50 GiB", note)
        self.assertIn(time, note)


class DriftedEvidenceStaysConservativeTest(unittest.TestCase):
    """The fix prices less; it never promotes drifted rows to measured."""

    def test_drifted_polluting_row_stays_unmeasured_and_sensitive(self) -> None:
        module = "Blanc.LidoTriggerableWithdrawalsGatewayAuthorization"
        row = _polluting_row(_recent_time())
        current = _current(POLLUTING_MODULES)
        stale = {
            "roots": [module], "package_roots": [module], "resolution": f"{module} (module)",
            "stale": 1, "detail": "fixture", "stale_set": [module], "graph": {module: set()},
        }
        with _isolated() as root:
            (root / "ledger.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            estimate, evidence = owned.derive_memory_gib(
                Path("/current"), [module], SETTINGS, ("tc", "mf"), 8,
                stale=stale, input_identity=current,
            )
            verdict, classification = owned.classify_contention(
                Path("/current"), [module], Path("/lake"), SETTINGS, ("tc", "mf"),
                stale, current,
            )
        self.assertEqual(estimate, 4)
        self.assertEqual(evidence["kind"], "narrow default")
        self.assertEqual(evidence["unmeasured_modules"], [module])
        self.assertEqual(verdict, "sensitive")
        self.assertIn("unmeasured", classification["reason"])


if __name__ == "__main__":
    unittest.main()
