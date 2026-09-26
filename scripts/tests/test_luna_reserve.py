from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

from creme import luna_reserve as L
from creme.cli import main


ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "scripts/tests/fixtures/luna_reserve/fake_codex.py"
RESERVE_RESET = 1790183837
REGULAR_RESET = 1789838141
THREAD = "01a0aef3-327d-72e3-bfd5-1fca3c552bd0"
NOW = 1789640000


def limits(regular_used=100, regular_reached="rate_limit_reached", reserve_used=1,
           reserve_reached=None, reserve_reset=RESERVE_RESET, regular_reset=REGULAR_RESET,
           ordinary=False, reserve=True, regular_credits=None):
    regular = {
        "limitId": "codex", "limitName": None,
        "primary": {"usedPercent": regular_used, "windowDurationMins": 10080, "resetsAt": regular_reset},
        "secondary": None,
        "credits": regular_credits or {"hasCredits": False, "unlimited": False, "balance": "0"},
        "rateLimitReachedType": regular_reached,
    }
    table = {"codex": regular}
    if reserve:
        table["base_model_inference"] = {
            "limitId": "base_model_inference", "limitName": "gpt-reserve",
            "normalModelSlug": "gpt-5.6-luna",
            "primary": {"usedPercent": reserve_used, "windowDurationMins": 10080, "resetsAt": reserve_reset},
            "secondary": None, "credits": None, "rateLimitReachedType": reserve_reached,
        }
    return {"ordinaryUsageAllowed": ordinary, "rateLimits": regular, "rateLimitsByLimitId": table}


def read(limits_value=None, account="chatgpt", models=("gpt-reserve", "gpt-5.6-luna")):
    return {
        "codex_home": "/nonexistent/codex-home",
        "account": {"type": account, "plan": "pro"},
        "models": {"data": [
            {"id": slug, "model": slug,
             "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max")]}
            for slug in models
        ]},
        "limits": limits() if limits_value is None else limits_value,
    }


def snapshot(resets_at=RESERVE_RESET, credits=None, limit_id="codex", window=10080):
    return {"type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 5}},
        "rate_limits": {"limit_id": limit_id, "primary": {
            "used_percent": 1.0, "window_minutes": window, "resets_at": resets_at,
        }, "secondary": None, "credits": credits},
    }}


def rollout(model="gpt-reserve", snapshots=None, extra=()):
    return [
        {"type": "session_meta", "payload": {"id": THREAD}},
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "event_msg", "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {"model": model, "service_tier": "default"},
        }},
        *(snapshots if snapshots is not None else [snapshot(RESERVE_RESET + 14), snapshot()]),
        *extra,
    ]


def buckets(limits_value=None):
    reserve, regular = L.classify_buckets(limits_value or limits())
    return reserve, regular


class AuditTest(unittest.TestCase):
    def audit(self, records):
        reserve, regular = buckets()
        return L.audit_records(records, reserve, regular)

    def test_reserve_rollout_passes_despite_codex_limit_id_label(self):
        result = self.audit(rollout())
        self.assertEqual(result["verdict"], "PASS", result["failures"])
        self.assertEqual(result["attributed_snapshots"], 2)
        self.assertEqual(result["snapshot_limit_id_labels"], {"codex": 2})

    def test_snapshot_matching_regular_reset_time_fails(self):
        result = self.audit(rollout(snapshots=[snapshot(), snapshot(REGULAR_RESET + 3)]))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(any("regular bucket" in failure for failure in result["failures"]))

    def test_snapshot_matching_neither_bucket_fails(self):
        result = self.audit(rollout(snapshots=[snapshot(RESERVE_RESET + 5000)]))
        self.assertEqual(result["verdict"], "FAIL")

    def test_regular_credit_shape_fails(self):
        credits = {"hasCredits": False, "unlimited": False, "balance": "0"}
        self.assertEqual(self.audit(rollout(snapshots=[snapshot(credits=credits)]))["verdict"], "FAIL")

    def test_window_shape_mismatch_fails(self):
        self.assertEqual(self.audit(rollout(snapshots=[snapshot(window=300)]))["verdict"], "FAIL")

    def test_model_mismatch_fails(self):
        result = self.audit(rollout(model="gpt-5.6-luna"))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(any("is not gpt-reserve" in failure for failure in result["failures"]))

    def test_turn_context_model_mismatch_alone_fails(self):
        records = rollout()
        records[1] = {"type": "turn_context", "payload": {"model": "gpt-6-astra"}}
        self.assertEqual(self.audit(records)["verdict"], "FAIL")

    def test_reroute_record_fails(self):
        reroute = {"type": "event_msg", "payload": {
            "type": "model_reroute", "from_model": "gpt-reserve", "to_model": "gpt-5.6-luna", "reason": "x",
        }}
        result = self.audit(rollout(extra=[reroute]))
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(any("reroute" in failure for failure in result["failures"]))

    def test_nested_from_model_fails(self):
        nested = {"type": "event_msg", "payload": {"type": "other", "detail": {"from_model": "a"}}}
        self.assertEqual(self.audit(rollout(extra=[nested]))["verdict"], "FAIL")

    def test_fast_service_tier_fails(self):
        records = rollout()
        records[2]["payload"]["thread_settings"]["service_tier"] = "priority"
        self.assertEqual(self.audit(records)["verdict"], "FAIL")

    def test_missing_rate_limits_and_empty_rollouts_fail(self):
        bare = {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": None}}
        self.assertEqual(self.audit(rollout(snapshots=[bare]))["verdict"], "FAIL")
        self.assertEqual(self.audit(rollout(snapshots=[]))["verdict"], "FAIL")
        self.assertEqual(self.audit([])["verdict"], "FAIL")

    def test_regular_delta_detects_usage_and_credit_movement(self):
        before = L.admission(read(limits(regular_used=40, regular_reached=None)), L.Policy(allow_regular_available=True), now=NOW)
        after = L.admission(read(limits(regular_used=41, regular_reached=None)), L.Policy(allow_regular_available=True), now=NOW)
        self.assertTrue(L.regular_delta_failures(before, after))
        self.assertEqual(L.regular_delta_failures(before, before), [])
        rich = {"hasCredits": True, "unlimited": False, "balance": "10"}
        poorer = {"hasCredits": True, "unlimited": False, "balance": "9"}
        before = L.admission(read(limits(regular_credits=rich)), L.Policy(), now=NOW)
        after = L.admission(read(limits(regular_credits=poorer)), L.Policy(), now=NOW)
        self.assertIn("regular credit balance decreased", L.regular_delta_failures(before, after))


class AdmissionTest(unittest.TestCase):
    def decide(self, value, policy=None, effort="low"):
        return L.admission(value, policy or L.Policy(), effort, now=NOW)

    def assertRefused(self, decision, fragment):
        self.assertFalse(decision["admitted"])
        self.assertTrue(any(fragment in refusal for refusal in decision["refusals"]), decision["refusals"])

    def test_exhausted_regular_and_available_reserve_is_admitted(self):
        decision = self.decide(read())
        self.assertTrue(decision["admitted"], decision["refusals"])
        self.assertEqual(decision["reserve"]["limit_id"], "base_model_inference")
        self.assertEqual(decision["regular"]["limit_id"], "codex")

    def test_missing_or_malformed_reserve_usage_counts_as_exhausted(self):
        for value in (None, "1", True):
            with self.subTest(value=value):
                table = limits()
                if value is None:
                    del table["rateLimitsByLimitId"]["base_model_inference"]["primary"]["usedPercent"]
                else:
                    table["rateLimitsByLimitId"]["base_model_inference"]["primary"]["usedPercent"] = value
                decision = L.admission(read(table), L.Policy(), now=NOW)
                self.assertFalse(decision["admitted"])
                self.assertTrue(any("reached" in reason for reason in decision["refusals"]))

    def test_reserve_is_found_by_limit_name_not_id(self):
        value = limits()
        value["rateLimitsByLimitId"]["renamed"] = value["rateLimitsByLimitId"].pop("base_model_inference")
        decision = self.decide(read(value))
        self.assertTrue(decision["admitted"], decision["refusals"])
        self.assertEqual(decision["reserve"]["limit_id"], "renamed")

    def test_missing_model_is_refused(self):
        self.assertRefused(self.decide(read(models=("gpt-5.6-luna",))), "absent from the Codex model catalogue")

    def test_missing_reserve_bucket_is_refused(self):
        self.assertRefused(self.decide(read(limits(reserve=False))), "no rate-limit bucket named gpt-reserve")

    def test_reached_reserve_is_refused(self):
        self.assertRefused(self.decide(read(limits(reserve_reached="rate_limit_reached"))), "is reached")
        self.assertRefused(self.decide(read(limits(reserve_used=100))), "is reached")

    def test_default_has_no_floor(self):
        self.assertTrue(self.decide(read(limits(reserve_used=99.5)))["admitted"])

    def test_reserve_below_an_explicit_floor_is_refused(self):
        policy = L.Policy(min_remaining_percent=1.0)
        self.assertRefused(self.decide(read(limits(reserve_used=99.5)), policy), "below the 1% floor")

    def test_indistinguishable_reset_times_are_refused(self):
        value = limits(reserve_reset=REGULAR_RESET + 60)
        self.assertRefused(self.decide(read(value)), "cannot be discriminated")

    def test_jitter_must_be_smaller_than_discrimination(self):
        policy = L.Policy(jitter_seconds=2000, discrimination_seconds=3600)
        self.assertRefused(self.decide(read(), policy), "twice the jitter")

    def test_api_key_account_is_refused(self):
        self.assertRefused(self.decide(read(account="apiKey")), "not a ChatGPT login")

    def test_unsupported_effort_is_refused(self):
        self.assertRefused(self.decide(read(), effort="ultra"), "not supported")

    def test_imminent_reserve_reset_is_refused(self):
        self.assertRefused(self.decide(read(limits(reserve_reset=NOW + 60))), "resets within 15 minutes")

    def test_available_regular_bucket_is_admitted_without_a_flag(self):
        # Retired 2026-09-20. The refusal existed only because no run had shown that a
        # reserve turn leaves an available regular bucket untouched; run
        # 20260920T073150Z-cff27d showed it (report: Plans f61cf6d1). The flag is retained
        # as a no-op, so passing it must change nothing about the decision.
        value = limits(regular_used=3, regular_reached=None, ordinary=True, regular_reset=RESERVE_RESET + 86400 * 3)
        decision = self.decide(read(value))
        self.assertTrue(decision["admitted"], decision["refusals"])
        self.assertTrue(decision["regular_available"])
        self.assertEqual(decision["warnings"], [])
        with_flag = self.decide(read(value), L.Policy(allow_regular_available=True))
        self.assertTrue(with_flag["admitted"], with_flag["refusals"])
        self.assertEqual(with_flag["warnings"], decision["warnings"])
        self.assertEqual(with_flag["refusals"], decision["refusals"])

    def test_available_regular_bucket_keeps_every_other_guard(self):
        # The verification discharged one refusal and nothing adjacent: the reserve floor,
        # reset discrimination, and the paid-credit warning still apply when the regular
        # bucket is available.
        available = dict(regular_used=3, regular_reached=None, ordinary=True,
                         regular_reset=RESERVE_RESET + 86400 * 3)
        self.assertRefused(self.decide(read(limits(reserve_used=99.5, **available)), L.Policy(min_remaining_percent=1.0)), "below the 1% floor")
        self.assertRefused(self.decide(read(limits(reserve_used=100, **available))), "is reached")
        close = dict(available, regular_reset=RESERVE_RESET + 60)
        self.assertRefused(self.decide(read(limits(**close))), "cannot be discriminated")
        credited = limits(regular_credits={"hasCredits": True, "unlimited": False, "balance": "10"}, **available)
        decision = self.decide(read(credited))
        self.assertTrue(decision["admitted"], decision["refusals"])
        self.assertIn("the regular bucket has paid credits; a misattributed run could spend them",
                      decision["warnings"])

    def test_child_environment_scrubs_billing_overrides(self):
        env, removed = L.child_environment({"OPENAI_API_KEY": "x", "CODEX_API_KEY": "y", "CODEX_HOME": "/h", "PATH": "/bin"})
        self.assertEqual(removed, ["CODEX_API_KEY", "OPENAI_API_KEY"])
        self.assertEqual(env, {"CODEX_HOME": "/h", "PATH": "/bin"})

    def test_only_the_reserve_slug_appears_in_the_module(self):
        import re

        source = (ROOT / "creme/luna_reserve.py").read_text(encoding="utf-8")
        slugs = set(re.findall(r"gpt-[0-9a-z.\-]+", source))
        self.assertEqual(slugs, {"gpt-reserve"})

    def test_bucket_model_slug_missing_from_snapshot_is_optional(self):
        value = limits()
        del value["rateLimitsByLimitId"]["base_model_inference"]["normalModelSlug"]
        reserve, regular = L.classify_buckets(value)
        self.assertIsNone(reserve.model_slug)
        report = {
            "admitted": True,
            "reserve": reserve.to_dict(),
            "regular": regular.to_dict(),
        }
        self.assertNotIn("model=", L.format_status(report))

    def test_bucket_model_slug_round_trips(self):
        reserve, _ = L.classify_buckets(limits())
        b = reserve
        self.assertEqual(L.Bucket.from_dict(b.to_dict()).model_slug, b.model_slug)


    def test_only_the_reserve_slug_appears_in_the_capability(self):
        import re

        client = (ROOT / "creme/codex_app_server.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"gpt-[0-9a-z.\-]+", client), [])

    def test_target_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            home = base / "home"
            (home / ".codex").mkdir(parents=True)
            root = home / "creme"
            worktree = root / ".worktrees" / "goal"
            worktree.mkdir(parents=True)
            (home / "plans" / "master").mkdir(parents=True)
            codex_home = str(home / ".codex")
            self.assertEqual(L.target_refusals(worktree, True, codex_home, root, home), [])
            self.assertTrue(L.target_refusals(root, True, codex_home, root, home))
            self.assertEqual(L.target_refusals(root, False, codex_home, root, home), [])
            self.assertTrue(L.target_refusals(home, False, codex_home, root, home))
            self.assertTrue(L.target_refusals(home / ".codex", False, codex_home, root, home))
            self.assertTrue(L.target_refusals(home / "plans" / "master", True, codex_home, root, home))
            self.assertTrue(L.target_refusals(base / "missing", False, codex_home, root, home))

    def test_binary_resolution_fails_closed(self):
        from creme.adapters import get_adapter

        with self.assertRaises(L.LunaReserveError):
            L.resolve_binary({}, adapter=get_adapter("Linux"))
        with self.assertRaises(L.LunaReserveError):
            L.resolve_binary({L.BINARY_ENV: "/nonexistent/codex"})
        self.assertEqual(get_adapter("Plan9").codex_binary().status, "UNAVAILABLE")


class SessionPolicyTest(unittest.TestCase):
    def session(self, sandbox="read-only", roots=(), effort="low"):
        from creme.codex_app_server import GuardedSession

        reserve, regular = buckets()
        return GuardedSession(
            process=None, pinned_model="gpt-reserve",
            attribution=lambda snap: L.snapshot_matches_reserve(snap, reserve, regular, 300),
            cwd=Path("/launch/creme"), sandbox=sandbox, effort=effort, writable_roots=roots,
            developer_instructions="contract",
        )

    def test_built_parameters_pass_their_own_policy(self):
        read_only = self.session()
        read_only.check_params("thread/start", read_only.thread_parameters())
        write = self.session("workspace-write", (Path("/work/tree"),))
        params = write.thread_parameters()
        write.check_params("thread/start", params)
        self.assertNotIn("sandbox", params)
        profile = params["config"]["permissions"][params["config"]["default_permissions"]]
        self.assertEqual(profile["extends"], ":read-only")
        self.assertEqual(profile["filesystem"], {"/work/tree": "write"})
        self.assertEqual(params["model"], "gpt-reserve")

    def test_overrides_are_pin_violations(self):
        from creme.codex_app_server import PinViolation

        session = self.session()
        session.thread_id, session.active_turn = "t", "u"
        base = session.thread_parameters()
        pins = {"approvalPolicy": "never", "approvalsReviewer": "user"}
        attempts = [
            ("thread/start", {**base, "model": "gpt-5.6-luna"}),
            ("thread/start", {key: value for key, value in base.items() if key != "model"}),
            ("thread/start", {**base, "serviceTier": "priority"}),
            ("thread/start", {**base, "profile": "fast"}),
            ("thread/start", {**base, "config": {"model": "x"}}),
            ("thread/start", {**base, "modelProvider": "oss"}),
            ("thread/start", {**base, "ephemeral": True}),
            ("thread/start", {**base, "sandbox": "danger-full-access"}),
            ("thread/start", {**base, "approvalPolicy": "on-request"}),
            ("thread/start", {**base, "developerInstructions": "you are the master"}),
            ("thread/start", {**base, "approvalsReviewer": "auto_review"}),
            ("thread/start", {key: value for key, value in base.items() if key != "approvalsReviewer"}),
            ("thread/start", {**base, "allowProviderModelFallback": True}),
            ("thread/start", {key: value for key, value in base.items() if key != "allowProviderModelFallback"}),
            ("turn/start", {"threadId": "t", "input": [], "model": "gpt-reserve", "approvalPolicy": "never",
                            "approvalsReviewer": "auto_review"}),
            ("turn/start", {"threadId": "t", "input": [], "model": "gpt-reserve", "approvalPolicy": "never"}),
            ("turn/start", {"threadId": "t", "input": [], "model": "gpt-reserve", "approvalPolicy": "on-request",
                            "approvalsReviewer": "user"}),
            ("thread/resume", {"threadId": "t", "model": "gpt-reserve", "approvalPolicy": "never",
                               "approvalsReviewer": "guardian_subagent"}),
            ("turn/start", {"threadId": "t", "input": [], "model": "gpt-reserve", "effort": "ultra", **pins}),
            ("turn/start", {"threadId": "t", "input": [], "model": "gpt-reserve", "serviceTierForTurn": "priority"}),
            ("turn/start", {"threadId": "t", "input": [], "model": "gpt-5.6-luna", **pins}),
            ("turn/start", {"threadId": "t", "input": [], **pins}),
            ("thread/resume", {"threadId": "t", "model": "gpt-6-astra", **pins}),
            ("turn/steer", {"threadId": "t", "expectedTurnId": "u", "input": [], "model": "gpt-5.6-luna"}),
            ("config/value/write", {"keyPath": "model", "value": "x"}),
            ("plugin/install", {}),
            ("command/exec", {}),
        ]
        for method, params in attempts:
            # Only a steer needs an active turn; a turn/start case must fail on its own pin.
            session.active_turn = "u" if method == "turn/steer" else None
            with self.subTest(method=method, params=params):
                with self.assertRaises(PinViolation):
                    session.check_params(method, params)
        session.check_params("thread/items/list", {"threadId": "t"})

    def test_every_catalogue_effort_is_permitted_and_others_are_pin_violations(self):
        from creme.codex_app_server import PinViolation, isolation_arguments

        pins = {"approvalPolicy": "never", "approvalsReviewer": "user"}
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                isolation_arguments("gpt-reserve", effort, {})
                session = self.session(effort=effort)
                session.thread_id, session.active_turn = "t", None
                session.check_params("turn/start", {"threadId": "t", "input": [], "model": "gpt-reserve",
                                                    "effort": effort, **pins})
        for effort in ("ultra", "minimal", "none", ""):
            with self.subTest(effort=effort):
                with self.assertRaises(PinViolation):
                    isolation_arguments("gpt-reserve", effort, {})
                with self.assertRaises(PinViolation):
                    self.session(effort=effort)

    def test_thread_response_checks(self):
        session = self.session("workspace-write", (Path("/work/tree"),))
        good = {
            "thread": {"id": "t", "path": "/r.jsonl", "ephemeral": False}, "model": "gpt-reserve",
            "serviceTier": None, "modelProvider": "openai", "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "activePermissionProfile": {"id": "creme_pseudo_subagent_write", "extends": ":read-only"},
            "sandbox": {"type": "workspaceWrite", "writableRoots": ["/work/tree"], "networkAccess": False,
                        "excludeSlashTmp": True, "excludeTmpdirEnvVar": True},
        }
        self.assertEqual(session.check_thread_response(good), [])
        for change in (
            {"model": "gpt-6-astra"}, {"serviceTier": "priority"}, {"activePermissionProfile": None},
            {"sandbox": {**good["sandbox"], "writableRoots": ["/work/tree", "/launch/creme"]}},
            {"sandbox": {**good["sandbox"], "networkAccess": True}},
            {"sandbox": {**good["sandbox"], "excludeSlashTmp": False}},
            {"thread": {"id": "t", "path": None}}, {"approvalPolicy": "on-request"},
            {"approvalsReviewer": "auto_review"},
        ):
            with self.subTest(change=change):
                self.assertTrue(session.check_thread_response({**good, **change}))

    def test_live_guard_observations(self):
        session = self.session()
        live_ok = {"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 1, "windowDurationMins": 10080, "resetsAt": RESERVE_RESET},
            "secondary": None, "credits": None}}}
        self.assertEqual(session.observe(live_ok), [])
        self.assertEqual(session.observe({"method": "remoteControl/status/changed",
                                          "params": {"status": "disabled"}}), [])
        live_regular = json.loads(json.dumps(live_ok))
        live_regular["params"]["rateLimits"]["primary"]["resetsAt"] = REGULAR_RESET
        for message in (
            live_regular,
            {"method": "model/rerouted", "params": {"fromModel": "gpt-reserve", "toModel": "x"}},
            {"method": "mcpServer/startupStatus/updated", "params": {"name": "node_repl", "status": "starting"}},
            {"method": "item/started", "params": {"item": {"type": "collabAgentToolCall", "model": "x"}}},
            {"method": "item/started", "params": {"item": {"type": "mcpToolCall"}}},
            {"method": "thread/settings/updated", "params": {"threadSettings": {"model": "gpt-6-astra"}}},
            {"method": "thread/settings/updated", "params": {"threadSettings": {"serviceTier": "priority"}}},
            {"method": "thread/settings/updated", "params": {"threadSettings": {"approvalsReviewer": "auto_review"}}},
            {"method": "remoteControl/status/changed", "params": {"status": "connected"}},
        ):
            with self.subTest(method=message["method"]):
                self.assertTrue(session.observe(message))

    def test_isolation_failures(self):
        from creme.codex_app_server import DISABLED_FEATURES, isolation_failures

        root = Path("/launch/creme")
        config = {"config": {
            "model": "gpt-reserve", "review_model": "gpt-reserve", "service_tier": "default",
            "web_search": "disabled", "notify": [], "approval_policy": "never", "approvals_reviewer": "user",
            "mcp_servers": {"lean-lsp-mcp": {"command": "/usr/bin/false", "enabled": False}},
        }, "layers": [{"name": {"type": "sessionFlags"}},
                      {"name": {"type": "project", "dotCodexFolder": "/launch/creme/.codex"}},
                      {"name": {"type": "user", "file": "/h/.codex/config.toml"}}]}
        features = [{"name": name, "enabled": False} for name in DISABLED_FEATURES] + [
            {"name": "shell_tool", "enabled": True}]
        self.assertEqual(isolation_failures(config, features, "gpt-reserve", root), [])
        bad = json.loads(json.dumps(config))
        bad["config"]["mcp_servers"]["node_repl"] = {"command": "node", "enabled": True}
        bad["layers"].append({"name": {"type": "project", "dotCodexFolder": "/elsewhere/.codex"}})
        bad["config"]["model"] = "gpt-6-astra"
        failures = isolation_failures(bad, features[:1] + [{"name": "multi_agent", "enabled": True}], "gpt-reserve", root)
        self.assertTrue(any("node_repl" in failure for failure in failures))
        self.assertTrue(any("foreign project" in failure for failure in failures))
        self.assertTrue(any("effective model" in failure for failure in failures))
        self.assertTrue(any("multi_agent" in failure for failure in failures))
        plugin_skills = {"data": [{"skills": [{"name": "documents", "enabled": True, "pluginId": "p"}]}]}
        self.assertTrue(isolation_failures(config, features, "gpt-reserve", root, plugin_skills))
        repo_skill = {"name": "lean-prover", "enabled": True, "scope": "repo",
                      "path": "/launch/creme/.agents/skills/lean-prover/SKILL.md"}
        self.assertEqual(isolation_failures(config, features, "gpt-reserve", root,
                                            {"data": [{"skills": [repo_skill]}]}), [])
        for foreign in ({"name": "hatch-pet", "enabled": True, "scope": "user", "path": "/h/.codex/skills/p/SKILL.md"},
                        {"name": "imagegen", "enabled": True, "scope": "system", "path": "/h/.codex/skills/.system/i/SKILL.md"},
                        {**repo_skill, "path": "/elsewhere/.agents/skills/x/SKILL.md"}):
            with self.subTest(skill=foreign["name"]):
                self.assertTrue(isolation_failures(config, features, "gpt-reserve", root,
                                                   {"data": [{"skills": [foreign]}]}))
        for key, value in (("approvals_reviewer", "auto_review"), ("approvals_reviewer", None),
                           ("approval_policy", "on-request")):
            with self.subTest(key=key, value=value):
                changed = json.loads(json.dumps(config))
                changed["config"][key] = value
                self.assertTrue(any(key.split("_")[1] in failure
                                    for failure in isolation_failures(changed, features, "gpt-reserve", root)))

    def test_skill_and_reviewer_isolation_arguments(self):
        from creme.codex_app_server import PinViolation, foreign_skill_paths, isolation_arguments

        root = Path("/launch/creme")
        listing = {"data": [{"skills": [
            {"name": "lean-prover", "scope": "repo", "enabled": True, "path": "/launch/creme/.agents/skills/l/SKILL.md"},
            {"name": "hatch-pet", "scope": "user", "enabled": True, "path": "/h/.codex/skills/hatch-pet/SKILL.md"},
            {"name": "imagegen", "scope": "system", "enabled": True, "path": "/h/.codex/skills/.system/i/SKILL.md"},
            {"name": "docs", "scope": "user", "enabled": True, "pluginId": "p", "path": "/p/SKILL.md"},
        ]}]}
        paths = foreign_skill_paths(listing, root)
        self.assertEqual(paths, ("/h/.codex/skills/hatch-pet/SKILL.md",))
        arguments = isolation_arguments("gpt-reserve", "low", {}, paths)
        for expected in ('approvals_reviewer="user"', 'approval_policy="never"', "skills.bundled.enabled=false",
                         'skills.config=[{path="/h/.codex/skills/hatch-pet/SKILL.md",enabled=false}]'):
            self.assertIn(expected, arguments)
        for unsafe in ('/h/"x"/SKILL.md', "relative/SKILL.md", "/h/a\\nb"):
            with self.subTest(unsafe=unsafe), self.assertRaises(PinViolation):
                isolation_arguments("gpt-reserve", "low", {}, (unsafe,))


class FakeAppServerRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.base = base
        self.codex_home = base / "codex-home"
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text("", encoding="utf-8")
        self.state = base / "state"
        self.root = base / "creme"
        self.target = self.root / ".worktrees" / "goal"
        self.target.mkdir(parents=True)
        self.log = base / "log.jsonl"
        binary = base / "codex"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        binary.chmod(0o755)
        self.scenario_path = base / "scenario.json"
        self.environ = {
            **os.environ,
            L.BINARY_ENV: str(binary),
            L.STATE_ENV: str(self.state),
            "FAKE_CODEX_SCENARIO": str(self.scenario_path),
            "OPENAI_API_KEY": "must-not-reach-child",
        }
        now = int(time.time())
        self.reserve_reset = now + 86400 * 6
        self.regular_reset = now + 86400 * 2
        self.scenario = {
            "log": str(self.log),
            "state": str(base / "executed"),
            "codex_home": str(self.codex_home),
            "models": ["gpt-reserve", "gpt-5.6-luna"],
            "limits": limits(reserve_reset=self.reserve_reset, regular_reset=self.regular_reset),
            "config": {"model": "gpt-6-astra", "service_tier": "default", "mcp_servers": {
                "node_repl": {"command": "node", "enabled": True},
                "lean-lsp-mcp": {"command": "/usr/bin/python3", "enabled": True},
            }},
            "enabled_features": ["shell_tool", "multi_agent", "apps", "plugins", "fast_mode"],
            "layers": [{"name": {"type": "sessionFlags"}},
                       {"name": {"type": "project", "dotCodexFolder": str(self.root / ".codex")}},
                       {"name": {"type": "user", "file": "/fake/.codex/config.toml"}}],
            "records": rollout(snapshots=[snapshot(self.reserve_reset)]),
            "skills": [{"cwd": str(self.root), "skills": [
                {"name": "lean-prover", "scope": "repo", "enabled": True,
                 "path": str(self.root / ".agents/skills/lean-prover/SKILL.md")},
                {"name": "hatch-pet", "scope": "user", "enabled": True, "path": "/fake/.codex/skills/hatch-pet/SKILL.md"},
                {"name": "imagegen", "scope": "system", "enabled": True,
                 "path": "/fake/.codex/skills/.system/imagegen/SKILL.md"},
            ]}],
            "turn_notifications": [self.live(self.reserve_reset),
                                   {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {
                                       "inputTokens": 10, "cachedInputTokens": 5, "outputTokens": 2}}}}],
        }

    def live(self, resets_at):
        return {"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 1, "windowDurationMins": 10080, "resetsAt": resets_at},
            "secondary": None, "credits": None}}}

    def tearDown(self):
        self.tmp.cleanup()

    def run_request(self, **overrides):
        self.scenario_path.write_text(json.dumps(self.scenario), encoding="utf-8")
        request = L.RunRequest(
            brief=overrides.pop("brief", "Say OK."), workdir=overrides.pop("target", self.target),
            effort="low", timeout_seconds=overrides.pop("timeout_seconds", 20), **overrides,
        )
        return L.run(ROOT, request, environ=self.environ, rollout_wait_seconds=0.5,
                     settle_seconds=0.3, root=self.root)

    def entries(self, kind, method=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["kind"] == kind and (method is None or row.get("method") == method)]

    def test_pass_run_is_isolated_pinned_and_audited(self):
        code, summary = self.run_request()
        self.assertEqual(code, L.EXIT_OK, summary)
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["live_snapshots"], "1/1")
        self.assertEqual(self.entries("forbidden-invocation"), [])
        launches = self.entries("launch")
        self.assertEqual(len(launches), 2)
        run_argv = launches[1]["argv"]
        self.assertIn('model="gpt-reserve"', run_argv)
        self.assertIn('service_tier="default"', run_argv)
        for feature in ("multi_agent", "apps", "plugins", "fast_mode", "computer_use"):
            self.assertIn(feature, run_argv)
        self.assertIn('mcp_servers.lean-lsp-mcp={command="/usr/bin/false",args=[],enabled=false}', run_argv)
        self.assertIn('mcp_servers.node_repl={command="/usr/bin/false",args=[],enabled=false}', run_argv)
        self.assertIn('approvals_reviewer="user"', run_argv)
        self.assertIn("skills.bundled.enabled=false", run_argv)
        self.assertIn('skills.config=[{path="/fake/.codex/skills/hatch-pet/SKILL.md",enabled=false}]', run_argv)
        self.assertEqual(self.entries("request", "thread/start")[0]["params"]["approvalsReviewer"], "user")
        self.assertFalse(self.entries("request", "thread/start")[0]["params"]["allowProviderModelFallback"])
        self.assertEqual(launches[1]["env_keys"], [])
        (thread_start,) = self.entries("request", "thread/start")
        params = thread_start["params"]
        self.assertEqual(params["model"], "gpt-reserve")
        self.assertEqual(params["cwd"], str(self.root))
        self.assertEqual(params["sandbox"], "read-only")
        self.assertFalse(params["ephemeral"])
        self.assertIn("You are a worker under the current Creme master session", params["developerInstructions"])
        self.assertIn(str(self.target), params["developerInstructions"])
        (turn_start,) = self.entries("request", "turn/start")
        self.assertEqual(turn_start["params"]["model"], "gpt-reserve")
        self.assertEqual(turn_start["params"]["effort"], "low")
        self.assertEqual(turn_start["params"]["approvalsReviewer"], "user")
        self.assertEqual(turn_start["params"]["approvalPolicy"], "never")
        self.assertEqual(turn_start["params"]["input"], [{"type": "text", "text": "Say OK."}])
        run_dir = Path(summary["run_dir"])
        for name in ("verdict.json", "preflight.json", "postflight.json", "audit.json", "transcript.jsonl",
                     "last-message.md", "developer-instructions.md"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertNotIn("someone@example.invalid", (run_dir / "transcript.jsonl").read_text(encoding="utf-8"))
        self.assertIn("verdict=PASS", L.format_run(summary))
        code, audited = L.audit_target(ROOT, THREAD, L.Policy(), environ=self.environ)
        self.assertEqual(code, L.EXIT_OK, audited)
        self.assertTrue(audited["reference_source"].endswith("preflight.json"))

    def test_write_mode_confines_writes_to_the_target(self):
        code, summary = self.run_request(write=True)
        self.assertEqual(code, L.EXIT_OK, summary)
        (thread_start,) = self.entries("request", "thread/start")
        params = thread_start["params"]
        self.assertNotIn("sandbox", params)
        profile = params["config"]["permissions"][params["config"]["default_permissions"]]
        self.assertEqual(profile["filesystem"], {str(self.target): "write"})
        self.assertEqual(profile["extends"], ":read-only")

    def test_write_mode_refuses_the_launch_checkout(self):
        code, summary = self.run_request(write=True, target=self.root)
        self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED, summary)
        self.assertEqual(self.entries("request", "thread/start"), [])

    def test_live_regular_snapshot_interrupts_and_trips(self):
        self.scenario["turn_notifications"] = [self.live(self.regular_reset)]
        self.scenario["hang"] = True
        code, summary = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED, summary)
        self.assertTrue(summary["interrupted_by_guard"])
        self.assertEqual(len(self.entries("request", "turn/interrupt")), 1)
        self.assertEqual(summary["message"], L.STOP_MESSAGE)
        self.assertTrue((self.state / L.TRIPWIRE_NAME).exists())
        code, summary = self.run_request()
        self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED)
        self.assertTrue(any("attribution failure" in reason for reason in summary["refusals"]))
        self.assertEqual(len(self.entries("request", "turn/start")), 1)

    def test_live_reroute_is_attribution_failure(self):
        self.scenario["turn_notifications"] = [{"method": "model/rerouted", "params": {
            "fromModel": "gpt-reserve", "toModel": "gpt-5.6-luna"}}]
        code, _ = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED)

    def test_mcp_startup_after_thread_start_blocks_the_turn(self):
        self.scenario["thread_notifications"] = [{"method": "mcpServer/startupStatus/updated", "params": {
            "name": "cua_repl", "status": "starting"}}]
        code, _ = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED)
        self.assertEqual(self.entries("request", "turn/start"), [])

    def test_thread_response_on_another_model_blocks_the_turn(self):
        self.scenario["thread_response"] = {"model": "gpt-6-astra"}
        code, _ = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED)
        self.assertEqual(self.entries("request", "turn/start"), [])

    def test_rollout_model_mismatch_is_attribution_failure(self):
        self.scenario["records"] = rollout(model="gpt-5.6-luna", snapshots=[snapshot(self.reserve_reset)])
        code, _ = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED)

    def test_rollout_regular_snapshot_is_attribution_failure(self):
        self.scenario["records"] = rollout(snapshots=[snapshot(self.regular_reset)])
        code, _ = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED)

    def test_missing_rollout_is_attribution_failure(self):
        self.scenario["records"] = None
        code, _ = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED)

    def test_regular_credit_decrease_is_attribution_failure(self):
        after = json.loads(json.dumps(self.scenario["limits"]))
        after["rateLimitsByLimitId"]["codex"]["credits"] = {"hasCredits": True, "unlimited": False, "balance": "-1"}
        self.scenario["limits_after"] = after
        code, _ = self.run_request()
        self.assertEqual(code, L.EXIT_ATTRIBUTION_FAILED)

    def test_failed_turn_is_codex_failure(self):
        self.scenario["turn_status"] = "failed"
        self.scenario["turn_notifications"] = []
        self.scenario["records"] = rollout(snapshots=[])
        code, summary = self.run_request()
        self.assertEqual(code, L.EXIT_CODEX_FAILED, summary)

    def test_timeout_interrupts_and_is_codex_failure(self):
        self.scenario["hang"] = True
        self.scenario["turn_notifications"] = [self.live(self.reserve_reset)]
        code, summary = self.run_request(timeout_seconds=1)
        self.assertEqual(code, L.EXIT_CODEX_FAILED, summary)
        self.assertTrue(summary["timed_out"])
        self.assertEqual(len(self.entries("request", "turn/interrupt")), 1)

    def test_isolation_failures_refuse_before_any_thread(self):
        cases = {
            "unstubbable mcp": {"unstubbable_mcp": ["cua_repl"]},
            "sticky multi agent": {"sticky_features": ["multi_agent"]},
            "foreign project layer": {"layers": [{"name": {"type": "project", "dotCodexFolder": "/else/.codex"}}]},
            "plugin skill": {"skills": [{"cwd": "x", "skills": [{"name": "docs", "enabled": True, "pluginId": "p"}]}]},
            "sticky user skill": {"sticky_skills": ["/fake/.codex/skills/hatch-pet/SKILL.md"]},
            "auto review reviewer": {"config_after_launch": {"approvals_reviewer": "auto_review"}},
            "unsafe mcp name": {"config": {"model": "x", "mcp_servers": {"bad name": {"command": "node"}}}},
        }
        for name, change in cases.items():
            with self.subTest(name):
                saved = json.loads(json.dumps(self.scenario))
                self.scenario.update(change)
                code, summary = self.run_request()
                self.scenario = saved
                self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED, summary)
                self.assertEqual(self.entries("request", "thread/start"), [])

    def test_preflight_refusals_never_start_a_thread(self):
        now = int(time.time())
        cases = {
            "reserve reached": {"limits": limits(reserve_reached="rate_limit_reached")},
            "no reserve bucket": {"limits": limits(reserve=False)},
            "model missing": {"models": ["gpt-5.6-luna"]},
            "indistinguishable": {"limits": limits(reserve_reset=now + 86400, regular_reset=now + 86400 + 30)},
            "fast default tier": {"default_service_tier": "priority"},
            "api key account": {"account_type": "apiKey"},
            "server launch fails": {"launch_exit": 1},
        }
        for name, change in cases.items():
            with self.subTest(name):
                saved = json.loads(json.dumps(self.scenario))
                self.scenario.update(change)
                code, summary = self.run_request()
                self.scenario = saved
                self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED, summary)
                self.assertEqual(self.entries("request", "thread/start"), [])
        code, _ = self.run_request(brief="   ")
        self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED)

    def test_overrides_are_refused_before_any_codex_process(self):
        code, _ = self.run_request(overrides=["--model gpt-5.6-luna"])
        self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED)
        self.assertFalse(self.log.exists())

    def test_cli_refuses_each_override_flag(self):
        brief = self.base / "brief.md"
        brief.write_text("Say OK.\n", encoding="utf-8")
        self.scenario_path.write_text(json.dumps(self.scenario), encoding="utf-8")
        attempts = (
            ["-m", "gpt-5.6-luna"], ["--model=gpt-5.6-luna"], ["-p", "fast"], ["--profile", "x"],
            ["-c", 'model="x"'], ["--config", 'service_tier="priority"'], ["--oss"],
            ["--service-tier", "priority"], ["--fast"], ["--local-provider", "ollama"],
            ["--dangerously-bypass-approvals-and-sandbox"], ["--enable", "multi_agent"],
            ["--unknown-codex-flag"],
        )
        old = dict(os.environ)
        os.environ.update(self.environ)
        try:
            for attempt in attempts:
                with self.subTest(attempt=attempt):
                    output = StringIO()
                    with redirect_stdout(output), redirect_stderr(StringIO()):
                        code = main([
                            "luna-reserve", "run", "--brief", str(brief), "--target", str(self.target),
                            *attempt,
                        ])
                    self.assertEqual(code, L.EXIT_PREFLIGHT_REFUSED, output.getvalue())
                    self.assertIn("override refused", output.getvalue())
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertFalse(self.log.exists())

    def test_run_passes_with_an_available_regular_bucket_and_no_flag(self):
        # End-to-end counterpart of the admission unit test: before 2026-09-20 this run was
        # refused at preflight (exit 10) unless --allow-regular-available was passed.
        self.scenario["limits"] = limits(
            regular_used=37, regular_reached=None, ordinary=True,
            reserve_reset=self.reserve_reset, regular_reset=self.regular_reset,
        )
        code, summary = self.run_request()
        self.assertEqual(code, L.EXIT_OK, summary)
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["warnings"], [])
        admission = json.loads((Path(summary["run_dir"]) / "preflight.json").read_text(encoding="utf-8"))["admission"]
        self.assertTrue(admission["regular_available"])
        self.assertEqual(admission["refusals"], [])
        self.assertEqual(admission["warnings"], [])
        self.assertEqual(self.entries("forbidden-invocation"), [])
        # The retained flag is still accepted and still admits.
        code, summary = self.run_request(policy=L.Policy(allow_regular_available=True))
        self.assertEqual(code, L.EXIT_OK, summary)
        self.assertEqual(summary["verdict"], "PASS")

    def test_status_reports_admission_without_a_thread(self):
        self.scenario_path.write_text(json.dumps(self.scenario), encoding="utf-8")
        code, report = L.status(L.Policy(), environ=self.environ)
        self.assertEqual(code, L.EXIT_OK, report)
        self.assertEqual(report["reserve"]["limit_id"], "base_model_inference")
        self.assertEqual(report["reserve"]["model_slug"], "gpt-5.6-luna")
        self.assertNotIn("someone@example.invalid", json.dumps(report))
        self.assertIn("ADMITTED", L.format_status(report))
        self.assertIn("model=gpt-5.6-luna", L.format_status(report))
        self.assertEqual(self.entries("request", "thread/start"), [])


if __name__ == "__main__":
    unittest.main()
