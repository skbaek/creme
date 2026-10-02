from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from creme.adapters.base import Adapter
from creme.profile import (effective_policy, fingerprint, load, propose, validate_data, write_reviewed,
                           load_containment_memory_max, load_lsp_aggregate_ceiling)


class FakeAdapter(Adapter):
    def __init__(self, system="Linux", memory_gib=12, cores=6, available=True):
        self.system = system
        self.memory_gib = memory_gib
        self.cores = cores
        self.available = available

    def static_facts(self):
        if not self.available:
            return self.result("static_facts", "UNAVAILABLE", "forced test failure")
        return self.result("static_facts", "OK", "fixture", {
            "system": self.system,
            "machine": "fixture-machine",
            "logical_cores": self.cores,
            "physical_memory_bytes": self.memory_gib * 1024 ** 3,
        })


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = FakeAdapter()

    def candidate(self):
        return propose(self.root / "creme", self.root, self.adapter)

    def test_missing_malformed_valid_and_stale(self):
        path = self.root / "profile.json"
        self.assertEqual(load(path, self.adapter).status, "MISSING")
        path.write_text("{no", encoding="utf-8")
        self.assertEqual(load(path, self.adapter).status, "INVALID")
        write_reviewed(path, self.candidate())
        self.assertEqual(load(path, self.adapter).status, "VALID")
        self.assertEqual(load(path, FakeAdapter(memory_gib=24)).status, "STALE")

    def test_dynamic_fact_is_rejected_even_if_fingerprint_is_recomputed(self):
        data = self.candidate()
        data["facts"]["memory_free_percent"] = 88
        data["fingerprint"] = fingerprint(data["facts"])
        checked = validate_data(data)
        self.assertEqual(checked.status, "INVALID")

    def test_unavailable_freshness_is_limited_not_fabricated(self):
        path = self.root / "profile.json"
        write_reviewed(path, self.candidate())
        checked = load(path, FakeAdapter(available=False))
        self.assertEqual(checked.status, "LIMITED")

    def test_precedence_cli_over_host_over_os_over_shared(self):
        data = self.candidate()
        data["policy"] = {"task_memory_gib": 3, "heavy_workers": 2, "light_workers": 3}
        data["overrides"] = {"task_memory_gib": 4, "heavy_workers": None, "light_workers": 4}
        actual = effective_policy(
            data,
            FakeAdapter(memory_gib=6, cores=2),
            {"task_memory_gib": 5, "heavy_workers": 3},
        )
        self.assertEqual(actual, {"task_memory_gib": 5, "heavy_workers": 3, "light_workers": 4})

    def test_profile_shape_has_no_unexpected_keys(self):
        data = self.candidate()
        data["credential"] = "not allowed"
        self.assertEqual(validate_data(data).status, "INVALID")

    def test_containment_cap_requires_integer_and_physical_reserve(self):
        for setting in [None, {}, {"memory_max_gib": True}, {"memory_max_gib": 0},
                        {"memory_max_gib": 11}, {"memory_max_gib": 4, "swap": 1}]:
            data = self.candidate()
            data["containment"] = setting
            self.assertEqual(validate_data(data).status, "INVALID", setting)
        data = self.candidate()
        data["containment"] = {"memory_max_gib": 10}
        self.assertEqual(validate_data(data).status, "VALID")
        self.assertEqual(validate_data(data, FakeAdapter(memory_gib=8).static_facts().data).status, "STALE")

    def test_containment_loading_pins_reviewed_value_and_refuses_stale_or_invalid(self):
        self.assertEqual(load_containment_memory_max(self.root, self.adapter), 8)
        data = self.candidate()
        data["containment"] = {"memory_max_gib": 10}
        path = self.root / ".creme/host-profile.json"
        write_reviewed(path, data)
        self.assertEqual(load_containment_memory_max(self.root, self.adapter), 10)
        self.assertEqual(load_lsp_aggregate_ceiling(self.root, self.adapter), 10)
        with self.assertRaises(ValueError):
            load_containment_memory_max(self.root, FakeAdapter(memory_gib=24))
        path.write_text("{}")
        with self.assertRaises(ValueError):
            load_containment_memory_max(self.root, self.adapter)

    def test_aggregate_ceiling_scales_and_is_bounded_by_reviewed_containment(self):
        self.assertEqual(load_lsp_aggregate_ceiling(self.root, FakeAdapter(memory_gib=64)), 62)
        self.assertEqual(load_lsp_aggregate_ceiling(self.root, FakeAdapter(available=False)), 8)
        data = propose(self.root, self.root, FakeAdapter(memory_gib=64))
        data["containment"] = {"memory_max_gib": 48}
        write_reviewed(self.root / ".creme/host-profile.json", data)
        self.assertEqual(load_lsp_aggregate_ceiling(self.root, FakeAdapter(memory_gib=64)), 48)


if __name__ == "__main__":
    unittest.main()
