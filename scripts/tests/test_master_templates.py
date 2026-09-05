from __future__ import annotations

import re
import unittest
from pathlib import Path

from creme import master_runtime


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = ROOT / "templates/master-runtime"
WORKER_TEMPLATE = TEMPLATE_ROOT / "worker-brief.md"
LAYOUT_TEMPLATE = TEMPLATE_ROOT / "README.md"
MASTER_GUIDE = ROOT / "docs/guides/master.md"
EXECUTION_GUIDE = ROOT / "docs/guides/execution.md"

PROVENANCE_START = "<!-- provenance-rule:start -->"
PROVENANCE_END = "<!-- provenance-rule:end -->"
TABLE_START = "<!-- provenance-table:start -->"
TABLE_END = "<!-- provenance-table:end -->"

EXPECTED_RULE = (
    "Registered-provenance exception: The master may approve a registered "
    "generator's identity/provenance output caused solely by an already-authorized "
    "source/input change if and only if the registered check is green, a relevant "
    "falsifier bites, the diff is exact generator output, and no semantic reference "
    "changes; generation never makes a reserved change autonomous, and an ambiguous "
    "mixed diff must be separated or escalated as a decision packet."
)

EXPECTED_CLASSIFICATIONS = {
    "registered-provenance": "autonomous",
    "pin-reference": "reserved",
    "baseline": "reserved",
    "budget": "reserved",
    "allowlist": "reserved",
    "golden": "reserved",
    "timeout": "reserved",
    "publication": "reserved",
    "public-claim-count": "reserved",
    "license": "reserved",
    "external-message": "reserved",
    "spend": "reserved",
    "dependent-contract": "reserved",
    "ambiguous-mixed": "separate-or-escalate",
}

REQUIRED_SECTIONS = (
    "## Objective",
    "## Exact starting refs",
    "## Read-first sources",
    "## Owned repositories and paths",
    "## Per-goal worktrees and branches",
    "## Resource class and coordination",
    "## Convergence gate",
    "## Autonomous and reserved decisions",
    "## Expected checkpoints",
    "## State, report, and evidence paths",
    "## Pause and reacquisition",
    "## Return contract",
)

REQUIRED_PLACEHOLDERS = {
    "ACQUIRE_OR_WAIT_COMMAND",
    "ADDITIONAL_READ_FIRST_SOURCE",
    "AUTONOMOUS_DECISIONS",
    "BRANCH",
    "CHECKPOINT_ARTIFACT",
    "CHECKPOINT_BOUNDARY",
    "CHECKPOINT_EVIDENCE",
    "CONDITION_TO_EVIDENCE_RULE",
    "CONTROL_THAT_BITES",
    "CONVERGENCE_GATE",
    "DEPENDENCY_RELATIONSHIP",
    "EVIDENCE_TREE",
    "EXCLUDED_PATHS",
    "FINAL_REPORT",
    "FULL_CHECKPOINT_COMMAND",
    "GATE_CATALOGUE",
    "GOAL_ID",
    "MASTER_DECISIONS",
    "MEMORY_GIB",
    "OBJECTIVE",
    "OWNED_PATHS",
    "PRIMARY_AUTHORITY",
    "REACQUISITION_CONDITION",
    "RELEASE_OR_WIND_DOWN_COMMAND",
    "RENEW_COMMAND",
    "REPOSITORY",
    "REPOSITORY_INSTRUCTIONS",
    "RESOURCE_CLASS",
    "START_COMMIT",
    "STATE_BRIEF",
    "UPSTREAM_REF",
    "USER_RESERVED_DECISIONS",
    "WORKTREE",
}


def marked_block(text: str, start: str, end: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise AssertionError(f"expected exactly one {start}/{end} block")
    return text.split(start, 1)[1].split(end, 1)[0].strip()


def normalized_rule(text: str) -> str:
    block = marked_block(text, PROVENANCE_START, PROVENANCE_END)
    words = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith(">"):
            raise AssertionError("the registered-provenance rule must be one blockquote")
        words.append(stripped.removeprefix(">").strip())
    return " ".join(" ".join(words).split())


def decision_rows(text: str) -> dict[str, str]:
    block = marked_block(text, TABLE_START, TABLE_END)
    lines = [line for line in block.splitlines() if line.startswith("|")]
    if len(lines) < 3:
        raise AssertionError("the provenance table is incomplete")
    headings = [cell.strip() for cell in lines[0].strip("|").split("|")]
    if headings != ["ID", "Change", "Classification", "Required evidence or action"]:
        raise AssertionError("the provenance table headings changed")
    rows: dict[str, str] = {}
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            raise AssertionError("the provenance table contains a malformed row")
        identifier = cells[0].strip("`")
        classification = cells[2].strip("`")
        if identifier in rows:
            raise AssertionError(f"duplicate provenance row {identifier}")
        rows[identifier] = classification
    return rows


def assert_decision_table(text: str) -> None:
    if decision_rows(text) != EXPECTED_CLASSIFICATIONS:
        raise AssertionError("the provenance authority table differs from its contract")


class MasterTemplateTest(unittest.TestCase):
    def setUp(self):
        self.worker = WORKER_TEMPLATE.read_text(encoding="utf-8")
        self.layout = LAYOUT_TEMPLATE.read_text(encoding="utf-8")
        self.master = MASTER_GUIDE.read_text(encoding="utf-8")
        self.execution = EXECUTION_GUIDE.read_text(encoding="utf-8")

    def test_generic_layout_matches_the_current_runtime_names_and_privacy(self):
        self.assertEqual(
            {path.name for path in TEMPLATE_ROOT.iterdir()},
            {"README.md", "worker-brief.md"},
        )
        for name in (
            master_runtime.README_NAME,
            master_runtime.LOCK_NAME,
            master_runtime.EVENTS_NAME,
            master_runtime.BOARD_NAME,
            *master_runtime.PRIVATE_DIRECTORIES,
        ):
            self.assertIn(name, self.layout)
        self.assertIn("mode `0700`", self.layout)
        self.assertIn("mode `0600`", self.layout)
        self.assertIn("ignored and untracked", self.layout)
        self.assertIn("a reference, not a specimen record", self.layout)
        self.assertNotRegex(self.layout + self.worker, r"/Users/|/home/|[0-9a-f]{40}")

    def test_worker_brief_has_every_required_field_in_reader_order(self):
        offsets = [self.worker.index(section) for section in REQUIRED_SECTIONS]
        self.assertEqual(offsets, sorted(offsets))
        placeholders = set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", self.worker))
        self.assertEqual(placeholders, REQUIRED_PLACEHOLDERS)
        for phrase in (
            "Dependency relationship and required ancestry",
            "Preserve unrelated state",
            "Light work takes no hold",
            "Required false-positive or mutation control",
            "At every coherent green boundary",
            "A chat summary is not acceptance evidence",
        ):
            self.assertIn(phrase, self.worker)

    def test_pause_and_return_contract_are_explicit_and_non_authoritative(self):
        pause = self.worker.split("## Pause and reacquisition", 1)[1].split(
            "## Return contract", 1
        )[0]
        normalized_pause = " ".join(pause.split())
        self.assertIn("commit the coherent owned checkpoint", normalized_pause)
        self.assertIn("update the state brief with the exact next unit", normalized_pause)
        self.assertIn("reclaim --wind-down {{GOAL_ID}}", normalized_pause)
        self.assertIn("Do not reacquire a hold or resume work until", normalized_pause)
        self.assertIn("{{REACQUISITION_CONDITION}}", normalized_pause)

        returned = self.worker.split("## Return contract", 1)[1]
        self.assertIn("bounded condition/evidence digest", returned)
        self.assertIn("every exact command run and its terminal verdict", returned)
        self.assertIn("never acceptance evidence", returned)

    def test_placeholder_walkthrough_produces_a_complete_neutral_brief(self):
        filled = self.worker
        for placeholder in REQUIRED_PLACEHOLDERS:
            filled = filled.replace(f"{{{{{placeholder}}}}}", f"synthetic-{placeholder.lower()}")
        self.assertNotRegex(filled, r"\{\{[A-Z0-9_]+\}\}")
        self.assertEqual(
            [heading for heading in REQUIRED_SECTIONS if heading in filled],
            list(REQUIRED_SECTIONS),
        )
        self.assertIn("synthetic-reacquisition_condition", filled)
        self.assertIn("bounded condition/evidence digest", filled)

    def test_master_guide_and_template_carry_one_identical_exact_rule(self):
        self.assertEqual(normalized_rule(self.master), EXPECTED_RULE)
        self.assertEqual(normalized_rule(self.worker), EXPECTED_RULE)
        self.assertEqual(normalized_rule(self.master), normalized_rule(self.worker))

    def test_normative_table_covers_the_autonomous_reserved_and_mixed_boundary(self):
        assert_decision_table(self.master)
        rows = decision_rows(self.master)
        self.assertEqual(rows["registered-provenance"], "autonomous")
        self.assertEqual(rows["ambiguous-mixed"], "separate-or-escalate")
        self.assertEqual(
            {identifier for identifier, value in rows.items() if value == "reserved"},
            set(EXPECTED_CLASSIFICATIONS) - {"registered-provenance", "ambiguous-mixed"},
        )

    def test_swapping_any_classification_row_makes_the_table_control_fail(self):
        table = marked_block(self.master, TABLE_START, TABLE_END)
        for identifier, expected in EXPECTED_CLASSIFICATIONS.items():
            with self.subTest(identifier=identifier):
                line = next(
                    candidate
                    for candidate in table.splitlines()
                    if candidate.startswith(f"| `{identifier}` |")
                )
                replacement = "reserved" if expected != "reserved" else "autonomous"
                mutated_line = line.replace(
                    f"| `{expected}` |", f"| `{replacement}` |", 1
                )
                mutated = self.master.replace(line, mutated_line, 1)
                with self.assertRaisesRegex(AssertionError, "authority table"):
                    assert_decision_table(mutated)

    def test_guides_link_the_tracked_template_and_all_new_links_resolve(self):
        reference = "../../templates/master-runtime/worker-brief.md"
        self.assertIn(reference, self.master)
        self.assertIn(reference, self.execution)
        documents = (WORKER_TEMPLATE, LAYOUT_TEMPLATE, MASTER_GUIDE, EXECUTION_GUIDE)
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", text):
                if "://" not in target:
                    self.assertTrue(
                        (document.parent / target).resolve().is_file(),
                        f"broken Markdown link in {document}: {target}",
                    )


if __name__ == "__main__":
    unittest.main()
