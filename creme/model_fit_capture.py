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
