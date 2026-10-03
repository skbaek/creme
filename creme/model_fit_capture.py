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
                                       "input": None, "final": None, "request_id": None,
                                       "session": row.get("sessionId")}
            if call.get("outside"):
                continue
            request = row.get("requestId")
            if request is not None:
                if not isinstance(request, str) or call["request_id"] not in {None, request}:
                    raise CaptureError(f"{path.name}:{number}: response {ident} carries two request ids")
                call["request_id"] = request
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
            "sessions": sorted({call["session"] for call in real.values()} - {None}),
            "responses": [{"request_id": call["request_id"], "model": call["model"], "final": call["final"],
                           "input": call["input"]} for call in real.values()],
            "children": {key: value for key, value in children.items() if key not in errored}}


# Claude Code OpenTelemetry (`creme claude-telemetry serve`, daily
# `otlp-YYYYMMDD.jsonl` lines `{"received","signal","body"}` with raw OTLP/JSON
# bodies). Measured 2026-10-03/04: every API request is a `logs` event
# `api_request` and a `traces` span `claude_code.llm_request`, both carrying
# `request_id`, `session.id`, `model` and final input/output/cache counts in the
# transcript's disjoint convention; only the span carries `agent_id` (absent for
# the main session). Transcript assistant entries carry the same `requestId`.
# One request is counted once however many records report it.

CLAUDE_TELEMETRY_KEYS = {"input": "input_tokens", "cache_read": "cache_read_tokens",
                         "cache_creation": "cache_creation_tokens", "output": "output_tokens"}
_TELEMETRY_CACHE: dict[tuple, dict[str, Any]] = {}


def _otlp_value(value: Any) -> Any:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    kind, inner = next(iter(value.items()))
    if kind == "intValue":
        return int(inner)
    return inner if kind in {"stringValue", "doubleValue", "boolValue"} else None


def _otlp_records(body: dict[str, Any], signal: str):
    """(attributes, time_ms, is_span) for every api_request event / llm_request span."""
    if signal == "logs":
        outer, inner, leaf = "resourceLogs", "scopeLogs", "logRecords"
    elif signal == "traces":
        outer, inner, leaf = "resourceSpans", "scopeSpans", "spans"
    else:
        return
    for resource in body.get(outer) or []:
        for scope in resource.get(inner) or []:
            for record in scope.get(leaf) or []:
                attributes = {item.get("key"): _otlp_value(item.get("value"))
                              for item in record.get("attributes") or [] if isinstance(item, dict)}
                if signal == "logs" and attributes.get("event.name") != "api_request":
                    continue
                if signal == "traces" and record.get("name") != "claude_code.llm_request":
                    continue
                stamp = record.get("startTimeUnixNano") if signal == "traces" else record.get("timeUnixNano")
                yield attributes, int(stamp) // 1_000_000 if stamp is not None else None, signal == "traces"


def claude_telemetry(directory: Path) -> dict[str, Any]:
    """API requests reported by Claude Code telemetry, merged by `request_id`.

    Event and span of one request must agree on session, model and counts; the
    span contributes `agent_id`. A record without `request_id` but with tokens
    is kept apart as unkeyed. A partial final line is still being written.
    """
    directory = Path(directory).resolve()
    files = sorted(directory.glob("otlp-*.jsonl")) if directory.is_dir() else []
    key = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in files)
    if (str(directory), key) in _TELEMETRY_CACHE:
        return _TELEMETRY_CACHE[(str(directory), key)]
    requests: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for path in files:
        with path.open("rb") as handle:
            for number, line in enumerate(handle, 1):
                if not line.endswith(b"\n"):
                    break
                try:
                    row = json.loads(line)
                    body, signal = row["body"], row["signal"]
                except (ValueError, UnicodeError, KeyError, TypeError) as exc:
                    raise CaptureError(f"{path.name}:{number}: malformed telemetry record") from exc
                for attributes, moment, span in _otlp_records(body, signal):
                    where = f"{path.name}:{number}"
                    if attributes.get("success") is False and \
                            all(attributes.get(field) is None for field in CLAUDE_TELEMETRY_KEYS.values()):
                        continue  # a failed attempt reporting no usage
                    usage = {name: _count(attributes.get(field), f"{where} {field}")
                             for name, field in CLAUDE_TELEMETRY_KEYS.items()}
                    agent = attributes.get("agent_id")
                    record = {"request_id": attributes.get("request_id"), "session": attributes.get("session.id"),
                              "model": attributes.get("model"), "usage": usage, "time_ms": moment,
                              "agent_id": agent if span else None, "span": span,
                              "query_source": attributes.get("query_source") or attributes.get("query_source_safe")}
                    ident = record["request_id"]
                    if not isinstance(ident, str) or not ident:
                        if any(usage.values()):
                            unkeyed.append(record)
                        continue
                    known = requests.get(ident)
                    if known is None:
                        requests[ident] = record
                        continue
                    for field in ("session", "model", "usage"):
                        if known[field] != record[field]:
                            raise CaptureError(f"{where}: telemetry records of {ident} disagree on {field}")
                    if span:
                        if known["span"] and known["agent_id"] != record["agent_id"]:
                            raise CaptureError(f"{where}: telemetry spans of {ident} disagree on agent")
                        known.update(span=True, agent_id=record["agent_id"], time_ms=moment)
                    known["query_source"] = known["query_source"] or record["query_source"]
    result = {"directory": str(directory), "files": len(files), "requests": requests, "unkeyed": unkeyed}
    _TELEMETRY_CACHE.clear()
    _TELEMETRY_CACHE[(str(directory), key)] = result
    return result


def _transcript_request_ids(paths: list[Path]) -> set[str]:
    found: set[str] = set()
    for path in paths:
        try:
            with path.open("rb") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(row, dict) and isinstance(row.get("requestId"), str):
                        found.add(row["requestId"])
        except OSError:
            continue
    return found


def claude_join(parts: list[dict[str, Any]], owners: list[str | None], telemetry: dict[str, Any] | None,
                windows: dict[str | None, tuple[int | None, int | None]],
                siblings: list[Path] | None = None) -> dict[str, Any]:
    """Final usage of every response from telemetry, plus untranscribed requests.

    `owners[i]` is the agent id of `parts[i]` (None: the session's main thread).
    A response's transcript final usage, when present, must equal telemetry,
    and the telemetry model must equal the transcript's. Telemetry requests of
    an owner in its `windows` entry with no transcript entry are added. An
    event without its span cannot be attributed; one that could be an owner's
    is a gap unless `siblings` transcripts account for it.
    """
    gaps: list[str] = []
    sessions = {session for part in parts for session in part["sessions"]}
    if len(sessions) > 1:
        raise CaptureError(f"transcripts span sessions {sorted(sessions)}")
    session = next(iter(sessions), None)
    requests = (telemetry or {}).get("requests", {})
    totals = {"input": 0, "cache_read": 0, "cache_creation": 0, "output": 0}
    seen: dict[str, str | None] = {}
    joined = missing = unfinished = added = 0
    models: dict[str | None, set[str]] = {}
    for part, owner in zip(parts, owners):
        models.setdefault(owner, set()).update(part["models"])
        for response in part["responses"]:
            ident = response["request_id"]
            record = requests.get(ident) if ident else None
            if ident is not None:
                if ident in seen:
                    raise CaptureError(f"request {ident} appears in two transcript responses")
                seen[ident] = owner
            if record is None:
                missing += 1
                final = response["final"]
                if final is None:
                    unfinished += 1
                    for key, value in zip(("input", "cache_read", "cache_creation"), response["input"]):
                        totals[key] += value
                else:
                    for key in totals:
                        totals[key] += final[key]
                continue
            if record["model"] != response["model"]:
                raise CaptureError(f"request {ident}: telemetry model {record['model']} is not the "
                                   f"transcript release {response['model']}")
            if record["session"] != session:
                raise CaptureError(f"request {ident}: telemetry session {record['session']} is not {session}")
            if record["span"] and record["agent_id"] != owner:
                raise CaptureError(f"request {ident}: telemetry agent {record['agent_id']} is not {owner}")
            if response["final"] is not None and response["final"] != record["usage"]:
                raise CaptureError(f"request {ident}: transcript final usage {response['final']} differs "
                                   f"from telemetry {record['usage']}")
            joined += 1
            for key in totals:
                totals[key] += record["usage"][key]
    if telemetry is None:
        gaps.append("no telemetry directory")
    elif missing:
        gaps.append(f"{missing} of {joined + missing} responses have no telemetry record")
    if session is None:
        gaps.append("transcripts record no session id; untranscribed requests cannot be matched")
    observed: set[str] = set()
    ambiguous = 0
    sibling_ids: set[str] | None = None
    master = None in owners

    def inside(record, owner):
        start, end = windows.get(owner, (None, None))
        moment = record["time_ms"]
        if start is None and end is None:
            return True
        return moment is not None and (start is None or moment >= start) and (end is None or moment < end)

    candidates = [(ident, record) for ident, record in requests.items() if ident not in seen]
    candidates += [(None, record) for record in (telemetry or {}).get("unkeyed", [])]
    for ident, record in candidates:
        if session is None or record["session"] != session:
            continue
        if record["span"]:
            owner = record["agent_id"]
            if owner not in models or not inside(record, owner):
                continue
            if ident is None:
                ambiguous += 1
                continue
            if not master and record["model"] not in models[owner]:
                raise CaptureError(f"request {ident} of agent {owner} used {record['model']}, not its "
                                   f"release {sorted(models[owner])}; register separate attempts")
            added += 1
            observed.add(record["model"])
            for key in totals:
                totals[key] += record["usage"][key]
            continue
        agentish = str(record["query_source"] or "").startswith("agent:")
        if master:
            if agentish or not inside(record, None):
                continue
            ambiguous += 1
        elif agentish:
            if ident is not None:
                if sibling_ids is None:
                    sibling_ids = _transcript_request_ids(siblings or [])
                if ident in sibling_ids:
                    continue
            ambiguous += 1
    if ambiguous:
        gaps.append(f"{ambiguous} telemetry requests of this session cannot be attributed "
                    "(no span or no request id)")
    return {"totals": totals, "joined": joined, "missing": missing, "unfinished": unfinished,
            "added": added, "added_models": sorted(observed), "gaps": gaps,
            "session": session, "telemetry": (telemetry or {}).get("directory")}


def claude_usage(parts: list[dict[str, Any]], gaps: list[str],
                 join: dict[str, Any] | None = None) -> dict[str, Any]:
    """Model-fit usage for transcript parts; any gap leaves the affected total unknown."""
    if join is None:
        totals = {key: sum(part["totals"][key] for part in parts)
                  for key in ("input", "cache_read", "cache_creation", "output")}
        unfinished = sum(part["unfinished_calls"] for part in parts)
    else:
        totals, unfinished, gaps = join["totals"], join["unfinished"], gaps + join["gaps"]
    compactions = sum(part["compactions"] for part in parts)
    known_input = totals["input"] + totals["cache_read"] + totals["cache_creation"]
    raw = {"convention": "anthropic: input, cache read and cache creation are disjoint; "
                         "cache creation is inside total_input, never added again",
           "observed": totals, "calls": sum(part["calls"] for part in parts),
           "unfinished_calls": unfinished, "compactions": compactions, "gaps": gaps}
    if join is not None:
        raw["telemetry"] = {key: join[key] for key in ("telemetry", "session", "joined", "missing", "added",
                                                       "added_models")}
    return {"total_input": None if compactions else known_input,
            "cached_input": totals["cache_read"], "cache_write": totals["cache_creation"],
            "total_output": None if unfinished or compactions or gaps else totals["output"],
            "reasoning": None, "reasoning_inside_output": True, "provider_raw": raw}


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
