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
        index += 2 if flag in ("--disable", "-c") else 1
    for name in scenario.get("sticky_features", []):
        features[name] = True
    for name in scenario.get("unstubbable_mcp", []):
        config.setdefault("mcp_servers", {})[name] = {"command": "/bin/sh", "enabled": True}
    config.update(scenario.get("config_after_launch", {}))
    return {"config": config, "features": features}


def serve(scenario: dict, log: Path, argv: list) -> int:
    state = Path(scenario["state"])
    effective = launch_config(scenario, argv)
    thread_id = scenario.get("thread_id", "01a0aef3-327d-72e3-bfd5-1fca3c552bd0")
    rollout = Path(scenario["codex_home"]) / "sessions/2026/09/17" / f"rollout-2026-09-17T10-00-00-{thread_id}.jsonl"
    turn_id = "turn-1"
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
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
            emit({"id": ident, "result": {"data": scenario.get("skills", [])}})
        elif method == "thread/start":
            response = {
                "thread": {"id": thread_id, "path": str(rollout), "ephemeral": False},
                "model": params.get("model"), "modelProvider": "openai", "serviceTier": None,
                "approvalPolicy": params.get("approvalPolicy"), "instructionSources": ["/fake/AGENTS.md"],
                "sandbox": {"type": "readOnly", "networkAccess": False}, "activePermissionProfile": None,
            }
            if params.get("config"):
                profile = params["config"]["default_permissions"]
                roots = list(params["config"]["permissions"][profile]["filesystem"])
                response["sandbox"] = {"type": "workspaceWrite", "writableRoots": roots, "networkAccess": False,
                                       "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}
                response["activePermissionProfile"] = {"id": profile, "extends": ":read-only"}
            response.update(scenario.get("thread_response", {}))
            emit({"id": ident, "result": response})
            for note in scenario.get("thread_notifications", []):
                emit(note)
        elif method == "turn/start":
            state.write_text("executed", encoding="utf-8")
            emit({"id": ident, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            if scenario.get("records") is not None:
                rollout.parent.mkdir(parents=True, exist_ok=True)
                with rollout.open("w", encoding="utf-8") as handle:
                    for record in scenario["records"]:
                        handle.write(json.dumps(record) + "\n")
            for note in scenario.get("turn_notifications", []):
                emit(note)
            if not scenario.get("hang"):
                emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
                    "id": turn_id, "status": scenario.get("turn_status", "completed"),
                    "items": [{"type": "agentMessage", "phase": "final_answer",
                               "text": scenario.get("final_message", "STATUS: DONE\nSUMMARY:\n- ok")}],
                }}})
        elif method == "turn/interrupt":
            emit({"id": ident, "result": {}})
            emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
                "id": turn_id, "status": "interrupted", "items": []}}})
        elif method == "thread/items/list":
            emit({"id": ident, "result": {"data": [], "nextCursor": None}})
        else:
            emit({"id": ident, "error": {"code": -32601, "message": f"fake does not implement {method}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
