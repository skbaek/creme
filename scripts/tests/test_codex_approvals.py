from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme.codex_approvals import _read_config, _routing_records, approval_checks


SESSION = "12345678-abcd-1234-abcd-123456789abc"
OTHER = "87654321-abcd-1234-abcd-123456789abc"


class CodexApprovalsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()

    def config(self, text):
        (self.home / "config.toml").write_text(text)

    def rollout(self, reviewer="user", session=SESSION, **extra):
        path = self.home / "sessions" / "2026" / f"rollout-date-{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "approvals_reviewer": reviewer,
            "approval_policy": "on-request",
            "active_permission_profile": {"id": "creme-relay", "extends": ":workspace"},
            "sandbox_policy": {"type": "workspace-write", "writable_roots": ["PRIVATE_PATH"]},
            "permission_profile": {"type": "managed", "file_system": {"type": "restricted"}},
            "user_instructions": "PRIVATE_INSTRUCTIONS",
            "turn_id": "PRIVATE_TURN_ID",
            **extra,
        }
        with path.open("a") as handle:
            handle.write(json.dumps({"timestamp": "2026-09-06T00:00:00Z", "type": "turn_context", "payload": payload}) + "\n")
        return path

    def checks(self, session=SESSION):
        return approval_checks(self.root, home=self.home, session=session)

    def settings(self, path, reviewer="auto_review", session=SESSION, **extra):
        payload = {
            "type": "thread_settings_applied", "thread_id": session,
            "thread_settings": {
                "approvals_reviewer": reviewer, "approval_policy": "on-request",
                "active_permission_profile": {"id": "creme-relay", "extends": ":workspace"},
                "permission_profile": {"file_system": {"type": "restricted", "entries": ["PRIVATE_ROOT"]}},
                "developer_instructions": "PRIVATE_SETTINGS_INSTRUCTIONS",
            },
            **extra,
        }
        with path.open("a") as handle:
            handle.write(json.dumps({"timestamp": "2026-09-06T00:00:01Z", "ordinal": 5,
                                     "type": "event_msg", "payload": payload}) + "\n")

    def test_newer_thread_settings_make_old_context_uncertain_not_active(self):
        self.config('approvals_reviewer = "auto_review"\n')
        path = self.rollout("user")
        self.assertEqual(self.checks()[-1][1], "fail")
        self.settings(path)
        rows = self.checks()
        self.assertEqual(rows[-1][1], "warn")
        self.assertIn("CONTEXT_PREDATES_SETTINGS", rows[-1][2])
        self.assertNotIn("AUTO_REVIEW_INACTIVE", rows[-1][2])
        self.assertIn("reviewer=user", rows[-1][2])
        settings = next(row for row in rows if row[0] == "Codex: recorded thread settings")
        self.assertIn("reviewer=auto_review", settings[2])
        self.assertIn("does not prove the active turn", settings[2])

    def test_subsequent_context_regains_authority_by_record_order(self):
        self.config('approvals_reviewer = "auto_review"\n')
        path = self.rollout("user")
        self.settings(path)
        # The fixture's context timestamp is older, but append order establishes
        # which observation the recorder produced after the settings update.
        self.rollout("user")
        self.assertEqual(self.checks()[-1][1], "fail")
        self.assertNotIn("CONTEXT_PREDATES_SETTINGS", self.checks()[-1][2])
        self.rollout("auto_review")
        self.assertEqual(self.checks()[-1][1], "ok")

    def test_newer_human_thread_defaults_do_not_claim_current_turn_changed(self):
        path = self.rollout("auto_review")
        self.settings(path, reviewer="user")
        self.assertEqual(self.checks()[-1][1], "warn")
        self.assertIn("reviewer=auto_review", self.checks()[-1][2])
        self.assertIn("reviewer=user", self.checks()[-2][2])

    def test_foreign_thread_settings_cannot_supersede_own_context(self):
        self.config('approvals_reviewer = "auto_review"\n')
        path = self.rollout("user")
        self.settings(path, session=OTHER)
        rows = self.checks()
        self.assertEqual(rows[-1][1], "fail")
        self.assertFalse(any(row[0] == "Codex: recorded thread settings" for row in rows))

    def test_malformed_settings_invalidates_earlier_settings_evidence(self):
        path = self.rollout("user")
        self.settings(path)
        self.settings(path, thread_settings=["PRIVATE_MALFORMED_SETTINGS"])
        rows = self.checks()
        self.assertEqual(rows[-2][1], "warn")
        self.assertIn("UNVERIFIED", rows[-2][2])
        self.assertNotIn("reviewer=auto_review", rows[-2][2])
        self.assertIn("CONTEXT_PREDATES_SETTINGS", rows[-1][2])
        self.assertNotIn("PRIVATE_MALFORMED_SETTINGS", json.dumps(rows))

    def test_truncated_and_unknown_settings_envelopes_are_unverified(self):
        path = self.rollout("user")
        with path.open("a") as handle:
            handle.write('{"type":"event_msg","payload":{"type":"thread_settings_applied",')
        self.assertIsNone(_routing_records(path, SESSION).settings)
        self.assertEqual(self.checks()[-1][1], "warn")
        self.assertIn("CONTEXT_PREDATES_SETTINGS", self.checks()[-1][2])
        path.write_text(json.dumps({"unknown_envelope": True, "type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_id": SESSION,
            "thread_settings": {"approvals_reviewer": "auto_review"},
        }}) + "\n")
        self.assertIn("UNVERIFIED", self.checks()[-2][2])
        self.assertIn("UNVERIFIED", self.checks()[-1][2])

    def test_missing_thread_identity_cannot_prove_thread_settings(self):
        path = self.rollout("user")
        self.settings(path, session=None)
        self.assertIn("UNVERIFIED", self.checks()[-2][2])
        self.assertEqual(self.checks()[-1][1], "warn")

    def test_thread_settings_are_redacted_and_unrelated_events_not_decoded(self):
        path = self.rollout("user")
        self.settings(path, reviewer="guardian_subagent")
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {
                "type": "diagnostic_message", "message": "PRIVATE_UNRELATED_EVENT",
            }}) + "\n")
        original = json.loads

        def allowed_records_only(value, *args, **kwargs):
            self.assertNotIn(b"PRIVATE_UNRELATED_EVENT", value if isinstance(value, bytes) else value.encode())
            return original(value, *args, **kwargs)

        with mock.patch("creme.codex_approvals.json.loads", side_effect=allowed_records_only):
            rows = self.checks()
            records = _routing_records(path, SESSION)
        self.assertIn("reviewer=auto_review", rows[-2][2])
        rendered = json.dumps(rows) + repr(records)
        for secret in (SESSION, "PRIVATE_ROOT", "PRIVATE_SETTINGS_INSTRUCTIONS", "PRIVATE_UNRELATED_EVENT"):
            self.assertNotIn(secret, rendered)

    def test_named_profile_and_saved_auto_mode_cannot_hide_actual_user(self):
        self.config('default_permissions = "creme-relay"\n')
        (self.home / ".codex-global-state.json").write_text(json.dumps({
            "electron-persisted-atom-state": {
                "permission-selection-by-host-id:local": {
                    "kind": "agent-mode", "agentMode": "guardian-approvals",
                },
            },
        }))
        self.rollout()
        rows = self.checks()
        self.assertIn("unset (Codex default=user", rows[0][2])
        self.assertEqual(rows[-1][1], "fail")
        self.assertIn("AUTO_REVIEW_INACTIVE", rows[-1][2])
        self.assertIn("permission profile=creme-relay", rows[-1][2])

    def test_control_runtime_change_clears_disagreement(self):
        self.config('approvals_reviewer = "auto_review"\n')
        self.rollout("user")
        self.assertEqual(self.checks()[-1][1], "fail")
        self.rollout("auto_review")
        row = self.checks()[-1]
        self.assertEqual(row[1], "ok")
        self.assertIn("not a live settings query", row[2])

    def test_guardian_alias_normalizes_disk_intent_and_recorded_reviewer(self):
        self.config('approvals_reviewer = "guardian_subagent"\n')
        self.rollout("user")
        rows = self.checks()
        self.assertIn("reviewer=auto_review", rows[0][2])
        self.assertEqual(rows[-1][1], "fail")
        self.rollout("guardian_subagent")
        row = self.checks()[-1]
        self.assertEqual(row[1], "ok")
        self.assertIn("reviewer=auto_review", row[2])

    def test_builtin_permission_profile_ids_remain_visible(self):
        for name in (":workspace", ":read-only"):
            with self.subTest(name=name):
                self.rollout("auto_review", active_permission_profile={"id": name})
                self.assertIn(f"permission profile={name};", self.checks()[-1][2])

    def test_profile_name_redaction_survives_builtin_prefix_support(self):
        for name in (":abcdefab-abcd-1234-abcd-123456789abc", "PRIVATE NAME\nINSTRUCTION"):
            with self.subTest(name=name):
                self.rollout("auto_review", active_permission_profile={"id": name})
                detail = self.checks()[-1][2]
                self.assertNotIn(name, detail)
                self.assertIn("permission profile=unverified;", detail)

    def test_config_alone_and_other_sessions_never_prove_activation(self):
        self.config('approvals_reviewer = "auto_review"\n')
        self.rollout("auto_review", session=OTHER)
        for session in (SESSION, "", "../sessions/*", "old-unknown-session"):
            with self.subTest(session=session):
                row = self.checks(session)[-1]
                self.assertEqual(row[1], "warn")
                self.assertIn("UNVERIFIED", row[2])

    def test_latest_older_metadata_does_not_fall_back_to_previous_reviewer(self):
        self.rollout("auto_review")
        self.rollout(None)
        row = self.checks()[-1]
        self.assertEqual(row[1], "warn")
        self.assertIn("reviewer=unverified", row[2])

    def test_partial_context_invalidates_earlier_success(self):
        path = self.rollout("auto_review")
        with path.open("a") as handle:
            handle.write('{"type":"turn_context","payload":')
        self.assertIsNone(_routing_records(path, SESSION).context)

    def test_current_client_ordinal_metadata_is_supported(self):
        path = self.rollout("auto_review")
        record = json.loads(path.read_text())
        path.write_text(json.dumps({"timestamp": record["timestamp"], "ordinal": 123,
                                    "type": "turn_context", "payload": record["payload"]}) + "\n")
        self.assertEqual(self.checks()[-1][1], "ok")

    def test_unknown_context_envelope_invalidates_earlier_success(self):
        path = self.rollout("auto_review")
        with path.open("a") as handle:
            handle.write(json.dumps({"new_envelope": True, "type": "turn_context",
                                     "payload": {"approvals_reviewer": "user"}}) + "\n")
        self.assertIn("UNVERIFIED", self.checks()[-1][2])

    def test_exact_current_session_index_is_read_only(self):
        path = self.rollout("auto_review")
        # Index lookup supports rollout names that do not encode the session.
        renamed = path.with_name("current.jsonl")
        path.rename(renamed)
        index = self.home / "state_5.sqlite"
        with sqlite3.connect(index) as db:
            db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?)", (SESSION, str(renamed)))
        before = index.read_bytes()
        self.assertEqual(self.checks()[-1][1], "ok")
        self.assertEqual(index.read_bytes(), before)
        self.assertIn("UNVERIFIED", self.checks(OTHER)[-1][2])

    def test_rollout_symlink_outside_sessions_is_not_read(self):
        path = self.rollout("auto_review")
        outside = self.root / "outside.jsonl"
        path.rename(outside)
        path.symlink_to(outside)
        self.assertIn("UNVERIFIED", self.checks()[-1][2])

    def test_transcripts_not_decoded_and_all_outputs_redacted(self):
        self.config('approvals_reviewer = "auto_review"\napi_key = "PRIVATE_CREDENTIAL"\n')
        path = self.rollout("auto_review")
        response = {"type": "response_item", "payload": {
            "content": "PRIVATE_TRANSCRIPT", "approvals_reviewer": "user",
        }}
        with path.open("a") as handle:
            handle.write(json.dumps(response) + "\n")
        original = json.loads

        def context_only(value, *args, **kwargs):
            self.assertNotIn(b"PRIVATE_TRANSCRIPT", value if isinstance(value, bytes) else value.encode())
            return original(value, *args, **kwargs)

        with mock.patch("creme.codex_approvals.json.loads", side_effect=context_only):
            rendered = json.dumps(self.checks())
        for secret in (SESSION, "PRIVATE_CREDENTIAL", "PRIVATE_TRANSCRIPT", "PRIVATE_PATH",
                       "PRIVATE_INSTRUCTIONS", "PRIVATE_TURN_ID", str(path)):
            self.assertNotIn(secret, rendered)

    def test_auto_review_does_not_make_disabled_boundary_healthy(self):
        variants = (
            {"approval_policy": "never"},
            {"sandbox_policy": {"type": "danger-full-access"}},
            {"permission_profile": {"file_system": {"type": "unrestricted"}}},
        )
        for extra in variants:
            with self.subTest(extra=extra):
                self.rollout("auto_review", **extra)
                self.assertEqual(self.checks()[-1][1], "fail")
                self.assertIn("AUTO_REVIEW_BOUNDARY_MISSING", self.checks()[-1][2])

    def test_config_profile_override_and_invalid_metadata_are_explicit(self):
        self.config('profile = "creme"\n[profiles.creme]\napprovals_reviewer = "auto_review"\n')
        self.assertEqual(_read_config(self.home / "config.toml")["reviewer"], "auto_review")
        self.config('approvals_reviewer = "PRIVATE_BAD_VALUE" trailing\n')
        row = self.checks()[0]
        self.assertEqual(row[1], "warn")
        self.assertNotIn("PRIVATE_BAD_VALUE", row[2])

    def test_explicit_project_human_review_is_not_silent_auto_review_drift(self):
        self.config('approvals_reviewer = "auto_review"\n')
        project = self.root / ".codex" / "config.toml"
        project.parent.mkdir()
        project.write_text('approvals_reviewer = "user"\n')
        self.rollout("user")
        self.assertEqual(self.checks()[-1][1], "warn")

    def test_missing_toml_reader_is_unverified_not_guessed(self):
        original = __import__

        def without_toml(name, *args, **kwargs):
            if name in {"tomllib", "tomli"}:
                raise ImportError(name)
            return original(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=without_toml):
            state = _read_config(self.home / "config.toml")
        self.assertEqual(state["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
