#!/usr/bin/env python3
"""A scripted stand-in for the Muse CLI: ``--version``, ``exec`` (echo bootstrap), and ``serve`` (MSP subset).

The scenario file named by ``FAKE_MUSE_SCENARIO`` drives it:

    home            directory for session logs and the call log
    served_model    model the host reports on setModel/tokenUsage (default: the requested one)
    usage           usage/read ``usage`` object, or absent
    turn            {"text": final text, "wait_for_steer": bool, "sleep": seconds,
                     "approvals": [subject, ...], "tools": [tool name, ...], "log_model": id}
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

SCENARIO = json.loads(Path(os.environ["FAKE_MUSE_SCENARIO"]).read_text())
HOME = Path(SCENARIO["home"])
HOME.mkdir(parents=True, exist_ok=True)


def log_call(entry):
    with open(HOME / "calls.jsonl", "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def session_log(session_id):
    path = HOME / "sessions" / session_id / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_log(session_id, record):
    with open(session_log(session_id), "a") as handle:
        handle.write(json.dumps(record) + "\n")


def meta_path(session_id):
    return HOME / "sessions" / session_id / "meta.json"


args = sys.argv[1:]
if args == ["--version"]:
    print("Muse Code 9.9.9 (fake)")
    sys.exit(0)

if args and args[0] == "exec":
    log_call({"argv": args, "env_model": os.environ.get("MUSE_MODEL"),
              "no_update": os.environ.get("MUSE_NO_AUTO_UPDATE")})
    session_id = args[args.index("--session-id") + 1]
    workspace = args[args.index("--workspace") + 1]
    if SCENARIO.get("exec_fail"):
        print("boom", file=sys.stderr)
        sys.exit(3)
    meta_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    meta_path(session_id).write_text(json.dumps({
        "workspace": workspace, "profile": args[args.index("--permission-profile") + 1]}))
    append_log(session_id, {"payload_type": "runtime.session.metadata", "payload": {"provider_id": "echo"}})
    append_log(session_id, {"payload_type": "runtime.model_selection.initialized",
                            "payload": {"model_id": SCENARIO.get("initial_model", "muse-spark-1.3-contributor")}})
    print(json.dumps({"stream": {"kind": "session", "id": session_id}, "payload_type": "run.terminal.completed",
                      "payload": {"text": "echo: bootstrap"}}))
    sys.exit(0)

if not args or args[0] != "serve":
    print(f"fake muse: unsupported {args}", file=sys.stderr)
    sys.exit(2)

log_call({"argv": args})
write_lock = threading.Lock()
state = {"session": None, "model": None, "mode": "onRequest", "effort": None, "turn": None, "steers": [],
         "decisions": {}, "interrupted": False, "cursor": 0}
steered = threading.Event()
decided = threading.Condition()


def send(message):
    message = {"jsonrpc": "2.0", **message}
    with write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def note(method, params):
    state["cursor"] += 1
    send({"method": method, "params": {"sessionId": state["session"], "viewCursor": f"v:{state['cursor']}", **params}})


def session_object():
    meta = json.loads(meta_path(state["session"]).read_text())
    return {"sessionId": state["session"], "path": str(session_log(state["session"])), "status": "idle",
            "activeTurnId": state["turn"], "workspaceRoot": meta["workspace"], "providerId": "meta",
            "modelId": state["model"], "turnCount": 0, "approvalMode": {"mode": state["mode"], "source": "startup",
                                                                          "lastCommandId": None}}


def served():
    return SCENARIO.get("served_model") or state["model"]


def run_turn(turn_id, text):
    turn = SCENARIO.get("turn") or {}
    note("turn/started", {"turnId": turn_id, "commandId": turn_id})
    for index, subject in enumerate(turn.get("approvals") or []):
        approval_id = f"ap-{index}"
        choices = [{"choiceId": "allow_once", "decision": "approved", "scope": "once", "label": "once"},
                   {"choiceId": "allow_prefix", "decision": "approvedPolicyAmendment", "scope": "localPersistent",
                    "label": "always"},
                   {"choiceId": "abort", "decision": "abort", "scope": "once", "label": "abort"}]
        params = {"approvalId": approval_id, "availableChoices": choices,
                  "currentRequirementId": {"approvalId": approval_id, "sourceIndex": 0}, "itemId": f"it-{index}",
                  "judgeEscalated": False, "protectedWrite": False, "rawArgs": "{}", "sessionId": state["session"],
                  "subject": subject, "taskId": "t", "toolCallId": "c", "toolName": subject.get("toolName", "bash"),
                  "turnId": turn_id, "viewCursor": "v"}
        send({"id": f"srv-{index}", "method": "approval/request", "params": params})
        note("approval/requested", params)
        with decided:
            decided.wait_for(lambda: approval_id in state["decisions"] or state["interrupted"], timeout=30)
    for tool in turn.get("tools") or []:
        note("item/started", {"item": {"itemId": f"tool-{tool}", "kind": "toolCall", "tool": tool,
                                       "status": "inProgress", "turnId": turn_id, "revision": 1}})
        note("item/completed", {"item": {"itemId": f"tool-{tool}", "kind": "toolCall", "tool": tool,
                                         "status": "completed", "turnId": turn_id, "revision": 2}})
    if turn.get("wait_for_steer"):
        steered.wait(timeout=30)
    deadline = time.time() + float(turn.get("sleep") or 0)
    while time.time() < deadline and not state["interrupted"]:
        time.sleep(0.05)
    if state["interrupted"]:
        note("turn/completed", {"turnId": turn_id, "terminal": "cancelled"})
        state["turn"] = None
        return
    model = served()
    append_log(state["session"], {"payload_type": "run.model.configured",
                                  "payload": {"model_id": turn.get("log_model") or model}})
    append_log(state["session"], {"payload_type": "reminder", "payload": {"reminder_roster": {
        "agents": [{"id": "verify-reminder", "model": "same-as-main"}]}}})
    note("session/tokenUsage", {"turnId": turn_id, "modelId": model,
                                "usage": {"inputTokens": 100, "outputTokens": 7, "cachedTokens": 40,
                                          "reasoningTokens": 3, "cacheReadTokens": 0, "cacheWriteTokens": 0},
                                "promptTokens": 100, "totalTokens": 107,
                                "cumulative": {"promptTokens": 100, "outputTokens": 7, "totalTokens": 107}})
    final = turn.get("text", "STATUS: DONE")
    if state["steers"]:
        final += "\nSTEERED: " + " | ".join(state["steers"])
    note("item/completed", {"item": {"itemId": f"msg-{turn_id}", "kind": "agentMessage", "turnId": turn_id,
                                     "status": "completed", "revision": 2, "text": final}})
    if turn.get("touch"):
        Path(turn["touch"]).write_text("written by the fake\n")
    state["turn"] = None
    note("turn/completed", {"turnId": turn_id, "terminal": "completed", "durationMs": 12})


def respond(message, result=None, error=None):
    reply = {"id": message["id"]}
    if error is not None:
        reply["error"] = error
    else:
        reply["result"] = result if result is not None else {}
    send(reply)


for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    log_call({"in": message})
    method = message.get("method")
    params = message.get("params") or {}
    if "id" not in message or method is None:
        continue   # a notification or a receipt
    if method == "initialize":
        respond(message, {"serverInfo": {"name": "muse", "version": "9.9.9"}, "userAgent": "fake",
                          "museHome": str(HOME), "platformFamily": "unix", "platformOs": "macos",
                          "schema": {"version": 1, "fingerprint": "sha256:fake"}, "grantedCapabilities": [],
                          "experimentalApi": False, "sessionDurability": "durable"})
    elif method == "model/list":
        rows = []
        for model_id, default in (("muse-spark-1.3", False), ("muse-spark-1.3-contributor", True)):
            rows.append({"modelId": model_id, "isDefault": default, "isActive": model_id == served()
                         and params.get("sessionId") == state["session"] and state["session"] is not None,
                         "variants": ["minimal", "low", "medium", "high", "xhigh", "max"], "providerId": "meta"})
        respond(message, {"models": rows, "providerId": "meta", "profileId": "tbh", "source": "providerCatalog"})
    elif method == "usage/read":
        respond(message, {"usage": SCENARIO["usage"]} if SCENARIO.get("usage") else {})
    elif method == "session/resume":
        state["session"] = params["sessionId"]
        state["model"] = SCENARIO.get("initial_model", "muse-spark-1.3-contributor")
        respond(message, {"session": session_object(), "viewCursor": "v:0", "history": {"mode": "none"},
                          "pendingRequests": []})
    elif method == "session/setModel":
        state["model"] = params["model"]["modelId"]
        respond(message, {"commandId": params["commandId"], "status": "accepted"})
        note("session/modelChanged", {"modelId": served(), "source": "user"})
    elif method == "session/setApprovalMode":
        state["mode"] = params["mode"]
        respond(message, {"commandId": params["commandId"], "status": "accepted",
                          "effectiveMode": {"mode": state["mode"]}})
    elif method == "session/setReasoningEffort":
        state["effort"] = params["reasoningEffort"]
        respond(message, {"commandId": params["commandId"], "status": "accepted"})
    elif method == "session/read":
        session = session_object()
        session["modelId"] = served()
        respond(message, {"session": session, "history": {"mode": "none"}, "pendingRequests": [], "viewCursor": "v"})
    elif method == "turn/start":
        if state["turn"] is not None:
            respond(message, error={"code": -32030, "message": "busy"})
            continue
        turn_id = params["commandId"]
        state["turn"] = turn_id
        state["interrupted"] = False
        state["steers"] = []
        steered.clear()
        respond(message, {"commandId": turn_id, "status": "accepted", "turnId": turn_id, "startedNewTurn": True,
                          "disposition": "started"})
        text = " ".join(part.get("text", "") for part in params.get("input") or [])
        threading.Thread(target=run_turn, args=(turn_id, text), daemon=True).start()
    elif method == "turn/steer":
        if params.get("expectedTurnId") != state["turn"]:
            respond(message, error={"code": -32030, "message": "not the active turn"})
            continue
        text = " ".join(part.get("text", "") for part in params.get("input") or [])
        state["steers"].append(text)
        respond(message, {"commandId": params["commandId"], "status": "accepted", "turnId": state["turn"]})
        note("item/completed", {"item": {"itemId": f"steer-{len(state['steers'])}", "kind": "userMessage",
                                         "turnId": state["turn"], "status": "completed", "revision": 1,
                                         "text": text, "steered": True}})
        steered.set()
    elif method == "turn/interrupt":
        state["interrupted"] = True
        steered.set()
        with decided:
            decided.notify_all()
        respond(message, {"commandId": params["commandId"], "status": "accepted"})
    elif method == "approval/decide":
        with decided:
            state["decisions"][params["approvalId"]] = params["choiceId"]
            decided.notify_all()
        respond(message, {"approvalId": params["approvalId"], "commandId": params["commandId"],
                          "status": "accepted", "terminal": True})
    else:
        respond(message, error={"code": -32601, "message": f"fake: {method}"})
