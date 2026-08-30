from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from creme.adapters.base import Adapter
from creme.profile import effective_policy, fingerprint, load, propose, validate_data, write_reviewed


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


if __name__ == "__main__":
    unittest.main()
