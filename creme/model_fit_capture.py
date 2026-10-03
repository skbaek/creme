"""Read-only provider evidence adapters; no quality verdicts or model calls.

Codex snapshots stream only local event metadata, retaining an incremental byte
cursor. Windows charge a cumulative counter difference once; a changed source,
counter reset, incomplete usage or malformed log is an explicit capture gap.
Raw usage remains available for provider-specific audits. Cached input and
reasoning are subsets of the provider totals, never additional charges.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class CaptureError(ValueError):
    pass


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise CaptureError(f"{name} is missing or is not a nonnegative integer")
    return value


def provider_totals(raw: dict[str, Any], *, input_key: str, output_key: str,
                    cache_key: str, reasoning_key: str, total_key: str) -> dict[str, Any]:
    """Translate explicit provider totals, preserving absence of subcategories."""
    incoming = _count(raw.get(input_key), input_key)
    outgoing = _count(raw.get(output_key), output_key)
    cached = raw.get(cache_key)
    reasoning = raw.get(reasoning_key)
    if cached is not None and _count(cached, cache_key) > incoming:
        raise CaptureError("cached input exceeds total input")
    if reasoning is not None and _count(reasoning, reasoning_key) > outgoing:
        raise CaptureError("reasoning exceeds total output")
    if raw.get(total_key) is not None and _count(raw[total_key], total_key) != incoming + outgoing:
        raise CaptureError("provider total does not equal input plus output")
    return {"total_input": incoming, "total_output": outgoing,
            "cached_input": cached, "reasoning": reasoning,
            "reasoning_inside_output": True, "provider_raw": raw}


def codex_snapshot(path: Path, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Incrementally read the latest complete usage event from one own rollout.

    Caller chooses the authorized rollout. This does not search other chats or
    read any message bodies into the result. A partial final line is retried
    next time. A complete malformed line is a gap, not silently skipped data.
    """
    path = Path(path).resolve()
    with path.open("rb") as handle:
        stat = path.stat()
        identity = hashlib.sha256(f"{path}:{stat.st_dev}:{stat.st_ino}".encode()).hexdigest()
        if previous and previous["source"] != identity:
            raise CaptureError("rollout source changed; cannot join a cumulative usage window")
        result = dict(previous or {"source": identity, "cursor": 0, "usage_offset": None,
                                   "raw": None, "model": None, "effort": None, "timestamp": None})
        # Identity changes describe this measured window, not all prior windows.
        result["identity_changed"] = False
        if stat.st_size < result["cursor"]:
            raise CaptureError("rollout truncated; cumulative window needs reconciliation")
        handle.seek(result["cursor"])
        while True:
            offset = handle.tell()
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                break
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise CaptureError(f"malformed rollout record at byte {offset}") from exc
            if not isinstance(row, dict):
                raise CaptureError(f"non-object rollout record at byte {offset}")
            payload = row.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            if row.get("type") == "turn_context":
                observed = payload.get("model")
                if observed and result.get("model") and observed != result["model"]:
                    result["identity_changed"] = True
                result["model"] = observed or result["model"]
                settings = (payload.get("collaboration_mode") or {}).get("settings") or {}
                effort = (settings.get("reasoning_effort") or payload.get("effort")
                          or payload.get("reasoning_effort") or result["effort"])
                if result.get("effort") and effort != result["effort"]:
                    result["identity_changed"] = True
                result["effort"] = effort
            if row.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                if not isinstance(info, dict):
                    raise CaptureError(f"invalid usage metadata at byte {offset}")
                raw = info.get("total_token_usage")
                old_raw = result.get("raw")
                if isinstance(raw, dict) and isinstance(old_raw, dict):
                    for name in ("input_tokens", "output_tokens"):
                        if raw.get(name) is not None and old_raw.get(name) is not None:
                            if _count(raw[name], name) < _count(old_raw[name], name):
                                raise CaptureError(f"counter reset at byte {offset}; reconcile source epoch")
                # An explicitly incomplete later counter invalidates this
                # snapshot; don't quietly return an older known total.
                result.update(raw=raw, usage_offset=offset, timestamp=row.get("timestamp"))
            result["cursor"] = handle.tell()
    return result


def codex_window(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before["source"] != after["source"]:
        raise CaptureError("window spans distinct rollout counters")
    totals = []
    for snapshot in (before, after):
        if not isinstance(snapshot.get("raw"), dict):
            raise CaptureError("usage window has an unobserved endpoint")
        totals.append(provider_totals(
            snapshot["raw"], input_key="input_tokens", output_key="output_tokens",
            cache_key="cached_input_tokens", reasoning_key="reasoning_output_tokens",
            total_key="total_tokens"))
    delta_in = totals[1]["total_input"] - totals[0]["total_input"]
    delta_out = totals[1]["total_output"] - totals[0]["total_output"]
    if after["cursor"] < before["cursor"] or delta_in < 0 or delta_out < 0:
        raise CaptureError("counter decreased or window is reversed; reconcile before attribution")
    return {"segment_key": f"codex-window:{before['source']}:{before['usage_offset']}:{after['usage_offset']}",
            "total_input": delta_in, "total_output": delta_out,
            "reasoning_inside_output": True,
            "source": before["source"], "start_offset": before["usage_offset"],
            "end_offset": after["usage_offset"], "before_raw": before["raw"], "after_raw": after["raw"],
            "observed_model": after["model"], "observed_effort": after["effort"]}


def muse_turn(record: dict[str, Any], number: int) -> dict[str, Any]:
    """Read exact recorded parent-turn usage; auxiliary coverage is separate.

    The broker's protocol PASS is never promoted to master acceptance here.
    This function does not assert that unreported auxiliary agents cost zero.
    """
    if not record.get("id"):
        raise CaptureError("Muse record has no session identity")
    turns = [t for t in record.get("turns", []) if t.get("n") == number]
    if len(turns) != 1:
        raise CaptureError("no unique recorded Muse turn")
    turn = turns[0]
    if not turn.get("turn_id"):
        raise CaptureError("Muse turn has no identity")
    tokens = turn.get("tokens")
    if not isinstance(tokens, dict):
        raise CaptureError("Muse turn has no recorded usage")
    raw = provider_totals(tokens, input_key="prompt", output_key="output",
                          cache_key="cached", reasoning_key="reasoning", total_key="total")
    models = turn.get("models") or {}
    if not isinstance(models, dict):
        raise CaptureError("invalid Muse model attribution")
    observed = [model for model, count in models.items() if _count(count, "model completions")]
    if len(observed) != 1 or observed[0] != record.get("model"):
        raise CaptureError("Muse actual-model attribution is missing or ambiguous")
    return {"run_id": f"{record['id']}:{turn['turn_id']}", "execution_client": "muse",
            "release": observed[0], "effort": record.get("effort"), "route": "muse-broker",
            "status": turn.get("status"), "protocol_verdict": turn.get("verdict"),
            "usage": raw, "usage_scope": "parent-turn", "quality_verdict": None,
            "started": turn.get("started"), "completed": turn.get("completed")}


# ---------------------------------------------------------------------------
# Claude Code transcripts
#
# Measured 2026-10-04 (Claude Code 2.1.284-2.1.286). One API response is
# streamed into several assistant entries sharing `message.id`. Every entry
# repeats the message_start input counters; only an entry with a non-null
# `stop_reason` carries the final usage (it alone has `iterations`). Master
# session transcripts record a final entry for every response; subagent
# transcripts usually do not (e.g. 7 of 107 responses), and their remaining
# `output_tokens` are streaming placeholders (8 tokens for 23 kB of content).
# A placeholder is never charged as output: such a response leaves output unknown.
# Anthropic input categories are disjoint: input, cache read and cache creation
# sum to total input; cache read is reported as its cached subset. A
# `fallback_message` iteration is a second billed request under another model.

CLAUDE_FAMILIES = {"claude-fable-5-1": "fable", "claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet"}
CLAUDE_PROFILE_EFFORTS = {1: "low", 2: "medium", 3: "high", 4: "xhigh", 5: "max"}
CLAUDE_AGENT_TOOLS = ("Agent", "Task")
_SYNTHETIC = "<synthetic>"


def utc_millis(value: Any, name: str) -> int:
    """Epoch milliseconds of an explicit UTC ISO timestamp (trailing Z or +00:00)."""
    import datetime as _dt
    if not isinstance(value, str) or not (value.endswith("Z") or value.endswith("+00:00")):
        raise CaptureError(f"{name} must be an explicit UTC ISO timestamp")
    try:
        moment = _dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise CaptureError(f"{name} is not an ISO timestamp") from exc
    return int(moment.timestamp() * 1000)


def source_identity(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha256(f"{path}:{stat.st_dev}:{stat.st_ino}".encode()).hexdigest()


def _call_usage(usage: Any, where: str) -> dict[str, int]:
    if not isinstance(usage, dict):
        raise CaptureError(f"{where}: assistant entry has no usage")
    return {"input": _count(usage.get("input_tokens"), "input_tokens"),
            "cache_read": _count(usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens"),
            "cache_creation": _count(usage.get("cache_creation_input_tokens", 0), "cache_creation_input_tokens"),
            "output": _count(usage.get("output_tokens"), "output_tokens")}


def claude_transcript(path: Path, start_ms: int | None = None, end_ms: int | None = None,
                      main_only: bool = False) -> dict[str, Any]:
    """Deduplicated API responses of one transcript, by first-entry time in [start, end).

    Returns metadata only: per-response usage, observed models and efforts,
    Agent-tool children, and the gaps that keep usage incomplete. Message
    bodies are not retained.
    """
    path = Path(path).resolve()
    calls: dict[str, dict[str, Any]] = {}
    efforts: set[str] = set()
    children: dict[str, str | None] = {}     # Agent tool_use id -> launched agent id (None: no result yet)
    errored: set[str] = set()
    compactions = synthetic = 0
    with path.open("rb") as handle:
        for number, line in enumerate(handle, 1):
            if not line.endswith(b"\n"):
                break  # a partial final line is still being written
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise CaptureError(f"{path.name}:{number}: malformed transcript record") from exc
            if not isinstance(row, dict) or (main_only and row.get("isSidechain")):
                continue
            stamp = row.get("timestamp")
            moment = utc_millis(stamp, f"{path.name}:{number} timestamp") if isinstance(stamp, str) else None
            inside = moment is not None and (start_ms is None or moment >= start_ms) and \
                (end_ms is None or moment < end_ms)
            if row.get("type") == "system" and row.get("subtype") == "compact_boundary" and inside:
                compactions += 1
            if row.get("type") == "user":
                content = (row.get("message") or {}).get("content")
                result = row.get("toolUseResult")
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_result" \
                            and block.get("tool_use_id") in children:
                        if block.get("is_error"):
                            errored.add(block["tool_use_id"])
                        elif isinstance(result, dict) and isinstance(result.get("agentId"), str):
                            children[block["tool_use_id"]] = result["agentId"]
            if row.get("type") != "assistant":
                continue
            message = row.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise CaptureError(f"{path.name}:{number}: assistant entry has no message id")
            ident = message["id"]
            call = calls.get(ident)
            if call is None:
                if not inside:
                    calls[ident] = {"outside": True}
                    continue
                call = calls[ident] = {"model": message.get("model"), "first_ms": moment,
                                       "input": None, "final": None}
            if call.get("outside"):
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" \
                        and block.get("name") in CLAUDE_AGENT_TOOLS and isinstance(block.get("id"), str):
                    children.setdefault(block["id"], None)
            if message.get("model") != call["model"]:
                raise CaptureError(f"{path.name}:{number}: response changed model "
                                   f"{call['model']} -> {message.get('model')} (fallback); "
                                   "register separate attempts")
            if call["model"] == _SYNTHETIC:
                continue
            if row.get("perTurnEffort") is not None:
                efforts.add(row["perTurnEffort"])
            usage = _call_usage(message.get("usage"), f"{path.name}:{number}")
            start = (usage["input"], usage["cache_read"], usage["cache_creation"])
            iterations = message["usage"].get("iterations")
            if call["input"] is None:
                call["input"] = start
            elif call["input"] != start and not iterations:
                raise CaptureError(f"{path.name}:{number}: streamed entries disagree on input usage")
            if message.get("stop_reason") is not None:
                if iterations:
                    parts = [_call_usage(item, f"{path.name}:{number} iteration") for item in iterations]
                    models = {item.get("model") for item in iterations if isinstance(item, dict)} - {None}
                    if models - {call["model"]}:
                        raise CaptureError(f"{path.name}:{number}: response fell back across models "
                                           f"{sorted(models | {call['model']})}; register separate attempts")
                    final = {key: sum(part[key] for part in parts) for key in usage}
                else:
                    final = usage
                if call["final"] is not None and call["final"] != final:
                    raise CaptureError(f"{path.name}:{number}: two different final usages for one response")
                call["final"] = final
    real = {key: call for key, call in calls.items()
            if not call.get("outside") and call["model"] != _SYNTHETIC}
    synthetic = sum(1 for call in calls.values() if call.get("model") == _SYNTHETIC)
    totals = {"input": 0, "cache_read": 0, "cache_creation": 0, "output": 0}
    unfinished = 0
    for call in real.values():
        final = call["final"]
        if final is None:
            unfinished += 1
            totals["input"] += call["input"][0]
            totals["cache_read"] += call["input"][1]
            totals["cache_creation"] += call["input"][2]
        else:
            for key in totals:
                totals[key] += final[key]
    stamps = [call["first_ms"] for call in real.values()]
    return {"path": str(path), "source": source_identity(path), "calls": len(real),
            "unfinished_calls": unfinished, "compactions": compactions, "synthetic": synthetic,
            "models": sorted({call["model"] for call in real.values()}), "efforts": sorted(efforts),
            "totals": totals, "first_ms": min(stamps) if stamps else None,
            "last_ms": max(stamps) if stamps else None,
            "children": {key: value for key, value in children.items() if key not in errored}}


def claude_usage(parts: list[dict[str, Any]], gaps: list[str]) -> dict[str, Any]:
    """Model-fit usage for transcript parts; any gap leaves the affected total unknown."""
    totals = {key: sum(part["totals"][key] for part in parts)
              for key in ("input", "cache_read", "cache_creation", "output")}
    unfinished = sum(part["unfinished_calls"] for part in parts)
    compactions = sum(part["compactions"] for part in parts)
    known_input = totals["input"] + totals["cache_read"] + totals["cache_creation"]
    return {"total_input": None if compactions else known_input,
            "cached_input": totals["cache_read"], "cache_write": totals["cache_creation"],
            "total_output": None if unfinished or compactions or gaps else totals["output"],
            "reasoning": None, "reasoning_inside_output": True,
            "provider_raw": {"convention": "anthropic: input, cache read and cache creation are disjoint; "
                                           "cache creation is inside total_input, never added again",
                             "observed": totals, "calls": sum(part["calls"] for part in parts),
                             "unfinished_calls": unfinished, "compactions": compactions,
                             "gaps": gaps}}


# ---------------------------------------------------------------------------
# Antigravity run records
#
# Measured 2026-10-04 over 110 `creme antigravity run` records: the result
# event's usage equals the sum of per-step usage; `total_tokens` is input plus
# output; cache reads are disjoint from input (cold claude-opus-5-5 prompt
# 23087 input; warm claude-sonnet-5-5 9010 input + 14086 cache read; Gemini
# cache reads routinely exceed input) and thinking sits inside output (step
# with 9291 visible characters: 12131 output, 9162 thinking). `checkpoint`
# steps (25 of 110 runs, 9-74 s each) and invoked subagents carry no usage.


def antigravity_record(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    try:
        verdict = json.loads((run_dir / "verdict.json").read_text())
    except (OSError, ValueError) as exc:
        raise CaptureError(f"Antigravity run has no readable verdict.json: {exc}") from exc
    init = result = None
    steps: dict[Any, dict] = {}
    gaps: list[str] = []
    with (run_dir / "events.jsonl").open("rb") as handle:
        for number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise CaptureError(f"events.jsonl:{number} is malformed") from exc
            if not isinstance(event, dict):
                raise CaptureError(f"events.jsonl:{number} is not an object")
            if event.get("event") == "init" and init is None:
                init = event.get("init") or {}
            elif event.get("event") == "result" and result is None:
                result = event.get("result") or {}
            update = event.get("step_update")
            if isinstance(update, dict):
                if isinstance(update.get("usage"), dict):
                    steps[update.get("step_index")] = update["usage"]
                if update.get("step_type") == "checkpoint":
                    gaps.append(f"checkpoint step {update.get('step_index')} has no reported usage")
                if update.get("step_type") == "subagent" and \
                        ((update.get("subagent_info") or {}).get("subagents") or update.get("state") != "ERROR"):
                    gaps.append(f"subagent step {update.get('step_index')} usage is not evidenced")
    return {"verdict": verdict, "init": init, "result": result, "steps": steps,
            "gaps": list(dict.fromkeys(gaps)), "run_dir": str(run_dir)}


def antigravity_usage(record: dict[str, Any]) -> dict[str, Any]:
    result = record["result"]
    if result is None or not isinstance(result.get("usage"), dict):
        observed = {key: sum(step.get(key, 0) for step in record["steps"].values())
                    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens")}
        return {"total_input": None, "total_output": None,
                "provider_raw": {"gap": "no result event", "observed_steps": observed}}
    raw = result["usage"]
    totals = provider_totals(raw, input_key="input_tokens", output_key="output_tokens",
                             cache_key="__absent__", reasoning_key="thinking_tokens",
                             total_key="total_tokens")
    cached = _count(raw.get("cache_read_tokens", 0), "cache_read_tokens")
    for key in raw:
        if key in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens") \
                and sum(_count(step.get(key, 0), key) for step in record["steps"].values()) != raw[key]:
            raise CaptureError(f"result {key} differs from the sum of step usage")
    return {"total_input": totals["total_input"] + cached, "cached_input": cached,
            "total_output": totals["total_output"], "reasoning": totals["reasoning"],
            "reasoning_inside_output": True,
            "provider_raw": {"convention": "agy: cache reads disjoint from input_tokens; "
                                           "thinking inside output; provider total excludes cache reads",
                             "usage": raw}}
