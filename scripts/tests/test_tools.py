from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / ".agents" / "skills" / "lean-prover" / "tools"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicToolTest(unittest.TestCase):
    def test_rule_requires_caller_supplied_model(self):
        rule = load("public_rule", TOOLS / "rule.py")
        self.assertEqual(rule.decide(10, 0.5, 1, 1, 4)[0], "Q2")
        self.assertEqual(rule.decide(10, 0.5, 10, 1, 4)[0], "Q4")

    def test_rule_rejects_invalid_fraction(self):
        rule = load("public_rule_invalid", TOOLS / "rule.py")
        with self.assertRaises(ValueError):
            rule.decide(10, 1.1, 3, 1, 4)

    def test_no_private_fitted_campaign_constants(self):
        selected = [
            TOOLS / "rule.py",
            TOOLS / "sample-targets.py",
            TOOLS / "procmon.py",
            TOOLS / "README.md",
        ]
        forbidden = ("fit-set targets", "83.4 s", "12.3 x", "15.3 ms", "6.5 s beside")
        hits = []
        for path in selected:
            text = path.read_text(encoding="utf-8")
            hits.extend((path.name, token) for token in forbidden if token in text)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
