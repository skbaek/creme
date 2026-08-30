"""Structural and hash checks for Creme's private-source extraction ledger.

The default test is self-contained: exact-copy claims are checked against the
committed destination and the recorded source digest.  Maintainers with the
private snapshots can additionally set CREME_PROVENANCE_ELANC_ROOT and
CREME_PROVENANCE_PLANS_ROOT; every recorded source digest is then checked
against ``git show <ref>:<path>`` without making either checkout a public CI
dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "scripts/extraction-manifest.json"
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
HEX_40 = re.compile(r"^[0-9a-f]{40}$")

CLASSIFICATIONS = {"move", "split", "remain", "retire", "compatibility_notice"}
RELATIONSHIPS = {
    "exact-copy",
    "derived",
    "derived-symlink",
    "derived-compatibility-shim",
}
LICENSE_DISPOSITIONS = {
    "publication-blocked-pending-user-selected-license",
    "not-copied-no-creme-license-impact",
}
EVIDENCE_DISPOSITIONS = {
    "none",
    "reviewed-private-evidence-not-copied",
    "remain-private-not-runtime-dependency",
    "remain-private-goal-not-runtime-dependency",
    "remain-private-concrete-goals-not-runtime-dependency",
    "remain-private-concrete-state-not-runtime-dependency",
    "durable-reviewed-input-only-client-memory-not-migrated",
    "behavior-reexpressed-public-tests-present-final-parity-review-pending",
}
SOURCE_ROOT_ENV = {
    "elanc": "CREME_PROVENANCE_ELANC_ROOT",
    "plans": "CREME_PROVENANCE_PLANS_ROOT",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _is_safe_relative(path: str) -> bool:
    value = PurePosixPath(path)
    return bool(path) and not value.is_absolute() and ".." not in value.parts


def _walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


class ExtractionManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _load()
        cls.repositories = {
            item["id"]: item for item in cls.manifest["source_repositories"]
        }
        cls.sources = {item["id"]: item for item in cls.manifest["sources"]}

    def test_top_level_schema_and_repository_snapshots(self) -> None:
        self.assertEqual(
            set(self.manifest),
            {
                "schema_version",
                "scope",
                "source_repositories",
                "license_disposition",
                "sources",
                "artifacts",
            },
        )
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(set(self.repositories), {"elanc", "plans"})
        self.assertEqual(
            len(self.repositories), len(self.manifest["source_repositories"])
        )
        for repository in self.repositories.values():
            self.assertEqual(
                set(repository),
                {
                    "id",
                    "ref",
                    "origin_main_ref",
                    "availability",
                    "history_imported",
                    "license_file_disposition",
                },
            )
            self.assertRegex(repository["ref"], HEX_40)
            self.assertRegex(repository["origin_main_ref"], HEX_40)
            self.assertEqual(repository["availability"], "private-source-snapshot")
            self.assertIs(repository["history_imported"], False)
            self.assertEqual(repository["license_file_disposition"], "absent-at-ref")

        license_disposition = self.manifest["license_disposition"]
        self.assertEqual(
            set(license_disposition), {"status", "evidence", "required_action"}
        )
        self.assertEqual(
            license_disposition["status"],
            "publication-blocked-pending-user-selected-license",
        )
        self.assertTrue(license_disposition["evidence"].strip())
        self.assertTrue(license_disposition["required_action"].strip())

    def test_source_schema_hashes_and_reviewed_dispositions(self) -> None:
        self.assertEqual(len(self.sources), len(self.manifest["sources"]))
        artifact_links = {
            (source_id, artifact["destination"])
            for artifact in self.manifest["artifacts"]
            for source_id in artifact["source_ids"]
        }
        source_links: set[tuple[str, str]] = set()

        for source_id, source in self.sources.items():
            with self.subTest(source=source_id):
                self.assertEqual(
                    set(source),
                    {
                        "id",
                        "repository",
                        "ref",
                        "path",
                        "sha256",
                        "classification",
                        "destination_paths",
                        "rationale",
                        "license_disposition",
                        "transitive_evidence_disposition",
                    },
                )
                self.assertTrue(source_id)
                self.assertIn(source["repository"], self.repositories)
                self.assertEqual(
                    source["ref"], self.repositories[source["repository"]]["ref"]
                )
                self.assertTrue(_is_safe_relative(source["path"]))
                self.assertRegex(source["sha256"], HEX_64)
                self.assertIn(source["classification"], CLASSIFICATIONS)
                self.assertTrue(source["rationale"].strip())
                self.assertIn(source["license_disposition"], LICENSE_DISPOSITIONS)
                self.assertIn(
                    source["transitive_evidence_disposition"], EVIDENCE_DISPOSITIONS
                )
                self.assertEqual(
                    len(source["destination_paths"]),
                    len(set(source["destination_paths"])),
                )
                for destination in source["destination_paths"]:
                    self.assertTrue(_is_safe_relative(destination))
                    source_links.add((source_id, destination))

                if source["classification"] in {"move", "split", "compatibility_notice"}:
                    self.assertTrue(source["destination_paths"])
                    self.assertEqual(
                        source["license_disposition"],
                        "publication-blocked-pending-user-selected-license",
                    )
                else:
                    self.assertFalse(source["destination_paths"])
                    self.assertEqual(
                        source["license_disposition"],
                        "not-copied-no-creme-license-impact",
                    )

        self.assertEqual(source_links, artifact_links)

    def test_artifact_destinations_are_unique_present_and_safe(self) -> None:
        artifacts = self.manifest["artifacts"]
        destinations = [artifact["destination"] for artifact in artifacts]
        self.assertEqual(len(destinations), len(set(destinations)))

        for artifact in artifacts:
            destination = artifact["destination"]
            with self.subTest(destination=destination):
                allowed_keys = {"destination", "relationship", "source_ids"}
                if artifact["relationship"] == "exact-copy":
                    allowed_keys.add("sha256")
                if artifact["relationship"] == "derived-symlink":
                    allowed_keys.add("symlink_target")
                self.assertEqual(set(artifact), allowed_keys)
                self.assertTrue(_is_safe_relative(destination))
                self.assertIn(artifact["relationship"], RELATIONSHIPS)
                self.assertTrue(artifact["source_ids"])
                self.assertEqual(
                    len(artifact["source_ids"]), len(set(artifact["source_ids"]))
                )
                self.assertTrue(
                    all(source_id in self.sources for source_id in artifact["source_ids"])
                )
                path = ROOT / destination
                self.assertTrue(path.exists() or path.is_symlink(), path)

                if artifact["relationship"] == "derived-symlink":
                    self.assertTrue(path.is_symlink(), path)
                    self.assertEqual(os.readlink(path), artifact["symlink_target"])

    def test_exact_copy_claims_match_destination_and_source_digest(self) -> None:
        exact = [
            artifact
            for artifact in self.manifest["artifacts"]
            if artifact["relationship"] == "exact-copy"
        ]
        self.assertTrue(exact)
        for artifact in exact:
            with self.subTest(destination=artifact["destination"]):
                self.assertRegex(artifact["sha256"], HEX_64)
                self.assertEqual(
                    _sha256((ROOT / artifact["destination"]).read_bytes()),
                    artifact["sha256"],
                )
                self.assertTrue(
                    any(
                        self.sources[source_id]["sha256"] == artifact["sha256"]
                        for source_id in artifact["source_ids"]
                    ),
                    "an exact copy must name at least one source with the same digest",
                )

    def test_optional_private_snapshots_match_all_recorded_source_hashes(self) -> None:
        checked = 0
        for source in self.sources.values():
            env_name = SOURCE_ROOT_ENV[source["repository"]]
            root_text = os.environ.get(env_name)
            if not root_text:
                continue
            root = Path(root_text).resolve()
            self.assertTrue((root / ".git").exists(), f"{env_name} is not a Git checkout")
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "show",
                    f"{source['ref']}:{source['path']}",
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(_sha256(result.stdout), source["sha256"], source["id"])
            checked += 1
        if any(os.environ.get(name) for name in SOURCE_ROOT_ENV.values()):
            self.assertGreater(checked, 0)

    def test_no_placeholder_dispositions(self) -> None:
        prohibited = {"unknown", "unreviewed"}
        for value in _walk_strings(self.manifest):
            words = set(re.findall(r"[a-z]+", value.lower()))
            self.assertTrue(
                prohibited.isdisjoint(words),
                f"placeholder disposition in manifest value: {value!r}",
            )


if __name__ == "__main__":
    unittest.main()
