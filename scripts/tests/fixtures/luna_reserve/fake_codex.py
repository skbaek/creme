"""Fake ``codex app-server`` for luna-reserve tests; it never uses a network.

The scenario JSON named by FAKE_CODEX_SCENARIO supplies every answer and the
turn's scripted notifications. Every launch and request is appended to the
scenario's log so tests can assert exact launch arguments and parameters.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    scenario = json.loads(Path(os.environ["FAKE_CODEX_SCENARIO"]).read_text(encoding="utf-8"))
    log = Path(scenario["log"])
    if sys.argv[1:2] != ["app-server"]:
        append(log, {"kind": "forbidden-invocation", "argv": sys.argv[1:]})
        return 2
    append(log, {"kind": "launch", "argv": sys.argv[2:],
                 "env_keys": sorted(k for k in os.environ if k.startswith(("OPENAI_", "CODEX_")))})
    if scenario.get("launch_exit") is not None:
        return int(scenario["launch_exit"])
    return serve(scenario, log, sys.argv[2:])


def append(log: Path, entry: dict) -> None:
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def launch_config(scenario: dict, argv: list) -> dict:
    """Apply the subset of -c/--disable the client uses to the fake config."""
    config = json.loads(json.dumps(scenario["config"]))
    features = {name: True for name in scenario.get("enabled_features", [])}
    stubbed = set()
    bundled = True
    disabled_skills = set()
    index = 0
    while index < len(argv):
        flag = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else ""
        if flag == "--disable":
            features[value] = False
        elif flag == "-c":
            key, _, raw = value.partition("=")
            if key.startswith("mcp_servers."):
                name = key.split(".", 1)[1]
                if "enabled=false" in raw:
                    stubbed.add(name)
                    config.setdefault("mcp_servers", {})[name] = {"command": "/usr/bin/false", "enabled": False}
            elif key in ("model", "review_model", "service_tier", "model_reasoning_effort", "web_search"):
                config[key] = raw.strip('"')
            elif key == "notify":
                config[key] = []
            elif key in ("approval_policy", "approvals_reviewer"):
                config[key] = raw.strip('"')
            elif key == "skills.bundled.enabled":
                bundled = raw != "false"
            elif key == "skills.config":
                disabled_skills.update(part.split('"')[1] for part in raw.split("path=")[1:])
        index += 2 if flag in ("--disable", "-c") else 1
    for name in scenario.get("sticky_features", []):
        features[name] = True
    for name in scenario.get("unstubbable_mcp", []):
        config.setdefault("mcp_servers", {})[name] = {"command": "/bin/sh", "enabled": True}
    config.update(scenario.get("config_after_launch", {}))
    skills = []
    for entry in scenario.get("skills", []):
        kept = []
        for skill in entry.get("skills", []):
            if skill.get("scope") == "system" and not bundled:
                continue
            skill = dict(skill)
            if skill.get("path") in disabled_skills and skill.get("path") not in scenario.get("sticky_skills", []):
                skill["enabled"] = False
            kept.append(skill)
        skills.append({**entry, "skills": kept})
    return {"config": config, "features": features, "skills": skills}


def thread_response(scenario: dict, params: dict, thread_id: str, rollout: Path) -> dict:
    response = {
        "thread": {"id": thread_id, "path": str(rollout), "ephemeral": False},
        "model": params.get("model"), "modelProvider": "openai", "serviceTier": None,
        "approvalPolicy": params.get("approvalPolicy"), "instructionSources": ["/fake/AGENTS.md"],
        "approvalsReviewer": params.get("approvalsReviewer"),
        "sandbox": {"type": "readOnly", "networkAccess": False}, "activePermissionProfile": None,
    }
    if params.get("config"):
        profile = params["config"]["default_permissions"]
        roots = list(params["config"]["permissions"][profile]["filesystem"])
        response["sandbox"] = {"type": "workspaceWrite", "writableRoots": roots, "networkAccess": False,
                               "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}
        response["activePermissionProfile"] = {"id": profile, "extends": ":read-only"}
    response.update(scenario.get("thread_response", {}))
    return response


def serve(scenario: dict, log: Path, argv: list) -> int:
    """Answer one client. A turn may stay pending until a steer, an interrupt, or an approval reply.

    ``scenario["turns"]`` optionally overrides, per turn number (1-based list), the keys
    ``turn_notifications``, ``hang``, ``final_message``, ``turn_status``, ``records``,
    ``approval`` (a server request to send), ``complete_on_steer``, and ``crash``.
    """
    state = Path(scenario["state"])
    effective = launch_config(scenario, argv)
    thread_id = scenario.get("thread_id", "01a0aef3-327d-72e3-bfd5-1fca3c552bd0")
    turn_number = 0
    pending = None  # {"id", "turn", "final"}

    def rollout_for(identifier: str) -> Path:
        return Path(scenario["codex_home"]) / "sessions/2026/09/17" / f"rollout-2026-09-17T10-00-00-{identifier}.jsonl"

    def complete(turn: dict, status: str, final: str) -> None:
        items = [] if status == "interrupted" else [{"type": "agentMessage", "phase": "final_answer", "text": final}]
        for note in turn.get("completion_notifications", []):
            emit(note)
        emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
            "id": turn["id"], "status": status, "items": items}}})

    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if "method" not in message:
            append(log, {"kind": "server-request-reply", "id": message["id"],
                         "result": message.get("result"), "error": message.get("error")})
            if pending is not None and pending.get("approval_id") == message["id"]:
                decision = (message.get("result") or {}).get("decision")
                turn, pending = pending, None
                complete(turn, "completed", f"{turn['final']} APPROVAL={decision}")
            continue
        method, params, ident = message.get("method"), message.get("params"), message["id"]
        append(log, {"kind": "request", "method": method, "params": params})
        executed = state.exists()
        if method == "initialize":
            emit({"id": ident, "result": {"codexHome": scenario["codex_home"]}})
        elif method == "account/read":
            emit({"id": ident, "result": {"account": {
                "type": scenario.get("account_type", "chatgpt"), "planType": "pro",
                "email": "someone@example.invalid"}}})
        elif method == "model/list":
            emit({"id": ident, "result": {"data": [
                {"id": slug, "model": slug, "hidden": slug == "gpt-reserve",
                 "defaultServiceTier": scenario.get("default_service_tier"),
                 "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high")]}
                for slug in scenario["models"]]}})
        elif method == "account/rateLimits/read":
            limits = scenario["limits_after"] if executed and "limits_after" in scenario else scenario["limits"]
            emit({"id": ident, "result": limits})
        elif method == "config/read":
            emit({"id": ident, "result": {"config": effective["config"], "layers": scenario.get("layers", [
                {"name": {"type": "sessionFlags"}},
                {"name": {"type": "user", "file": "/fake/.codex/config.toml"}},
            ])}})
        elif method == "experimentalFeature/list":
            emit({"id": ident, "result": {"data": [
                {"name": name, "enabled": enabled} for name, enabled in effective["features"].items()
            ], "nextCursor": None}})
        elif method == "skills/list":
            emit({"id": ident, "result": {"data": effective["skills"]}})
        elif method in ("thread/start", "thread/resume"):
            if method == "thread/resume":
                thread_id = params.get("threadId")
                if scenario.get("resume_error"):
                    emit({"id": ident, "error": {"code": -32600, "message": "no such thread"}})
                    continue
            emit({"id": ident, "result": thread_response(scenario, params, thread_id, rollout_for(thread_id))})
            for note in scenario.get("thread_notifications", []):
                emit(note)
        elif method == "turn/start":
            turn_number += 1
            state.write_text("executed", encoding="utf-8")
            overrides = (scenario.get("turns") or [])
            spec = {**scenario, **(overrides[turn_number - 1] if turn_number <= len(overrides) else {})}
            turn_id = f"turn-{turn_number}"
            emit({"id": ident, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            if spec.get("crash"):
                return 3
            rollout = rollout_for(thread_id)
            if spec.get("records") is not None:
                rollout.parent.mkdir(parents=True, exist_ok=True)
                fresh = not rollout.exists()
                with rollout.open("a", encoding="utf-8") as handle:
                    for record in spec["records"]:
                        if fresh or record.get("type") != "session_meta":
                            handle.write(json.dumps(record) + "\n")
            emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id}}})
            for note in spec.get("turn_notifications", []):
                emit(note)
            final = spec.get("final_message", "STATUS: DONE\nSUMMARY:\n- ok")
            turn = {"id": turn_id, "final": final, "complete_on_steer": spec.get("complete_on_steer"),
                    "completion_notifications": spec.get("completion_notifications", [])}
            if spec.get("approval"):
                request = dict(spec["approval"])
                turn["approval_id"] = request.get("id", "srv-1")
                emit({"id": turn["approval_id"], "method": request["method"],
                      "params": {"threadId": thread_id, "turnId": turn_id, **request.get("params", {})}})
                pending = turn
            elif spec.get("hang") or spec.get("complete_on_steer"):
                pending = turn
            else:
                complete(turn, spec.get("turn_status", "completed"), final)
        elif method == "turn/steer":
            if pending is None or params.get("expectedTurnId") != pending["id"]:
                emit({"id": ident, "error": {"code": -32600, "message": "no active turn"}})
                continue
            emit({"id": ident, "result": {"turnId": pending["id"]}})
            if pending.get("complete_on_steer"):
                turn, pending = pending, None
                text = " ".join(part.get("text", "") for part in params.get("input") or [])
                complete(turn, "completed", f"{turn['final']} STEERED: {text}")
        elif method == "turn/interrupt":
            emit({"id": ident, "result": {}})
            turn = pending or {"id": params.get("turnId"), "final": ""}
            pending = None
            complete(turn, "interrupted", "")
        elif method == "thread/items/list":
            emit({"id": ident, "result": {"data": [
                {"turnId": "turn-1", "item": {"type": "agentMessage", "text": "STATUS: DONE"}}], "nextCursor": None}})
        else:
            emit({"id": ident, "error": {"code": -32601, "message": f"fake does not implement {method}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
