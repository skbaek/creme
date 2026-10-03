"""Automatic broker receipts and one-action native/master usage capture.

Adapters expose capability gaps rather than inventing total episode costs.
Muse parent totals do not cover hidden reminder agents. Luna counters are
thread-cumulative and therefore require explicit adjacent rollout windows.
Claude Code subagent transcripts usually lack final output usage, so Claude
Code usage comes from its OpenTelemetry export joined by request id; without a
telemetry record per response it stays incomplete. Antigravity checkpoint
steps report none. Both stay explicit gaps, never zero.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import model_fit_capture as C
from . import model_fit_runtime as R


def codex_run(path, episode_id, run_id, family, route, harness_version,
              terminal, before=None, override_reason="", attempt_index=1):
    """Import an owned native rollout, including all its measured input/output.

    A resumed source requires its saved `before` snapshot. For a fresh worker
    source the counter starts at zero; this is never appropriate for a resumed
    main chat. Effective release and effort must be present in turn metadata.
    """
    after = C.codex_snapshot(Path(path), before)
    if after.get("identity_changed"):
        raise C.CaptureError("native source changed model/effort; register its separately measured attempts")
    if not after.get("model") or not after.get("effort"):
        raise C.CaptureError("native rollout lacks effective release/effort metadata")
    # A Luna reserve rollout's turn context names the reserve catalogue model
    # (`gpt-reserve`), the same id the broker records as its observed release.
    observed_family = {"gpt-6.1-sol": "sol", "gpt-6-astra": "astra", "gpt-6-luna": "luna",
                       "gpt-reserve": "luna-reserve"}.get(after["model"])
    if observed_family != family and not (family == "luna-reserve" and observed_family == "luna"):
        raise C.CaptureError("observed native release does not match its family")
    if before is None:
        before = {**after, "cursor": 0, "usage_offset": 0,
                  "raw": {"input_tokens": 0, "output_tokens": 0,
                          "cached_input_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0}}
    usage = C.codex_window(before, after)
    return {"receipt_id": "native:" + run_id + ":" + R.digest(after), "kind": "run",
            "episode_id": episode_id, "run_id": run_id, "attempt_index": attempt_index,
            "option": family + "/" + after["effort"], "release": after["model"],
            "route": route, "harness_version": harness_version, "override_reason": override_reason,
            "terminal": terminal, "segments": [{"id": usage["segment_key"], "usage": usage}],
            "usage_complete": True, "usage_evidence": str(Path(path).resolve())}


def master_window(before, after, client, weight=1.0):
    usage = C.codex_window(before, after)
    return {"id": usage["segment_key"], "client": client, "weight": weight, "usage": usage}


def broker_capture(record):
    """Called after durable broker persistence; never changes a worker verdict.

    Binding is installed before launch and survives restarts. Every turn of
    that session belongs to its declared episode, including repair/fallback
    turns. Separate tasks use separate bindings/sessions. Unknown auxiliary
    usage leaves finality false even when the parent run completed.
    """
    binding = record.get("model_fit")
    if not binding:
        return
    store = R.open_runtime(Path(binding["directory"]))
    try:
        for turn in record.get("turns", []):
            if not turn.get("turn_id"):
                continue
            run_id = record["id"] + ":" + str(turn["turn_id"])
            muse = record.get("model") == "muse-spark-1.3"
            actual = {"episode_id": binding["episode_id"], "run_id": run_id,
                      "attempt_index": binding.get("attempt_offset", 0) + turn["n"],
                      "option": ("muse-spark" if muse else "luna-reserve") + "/" + record["effort"],
                      "release": record.get("model"),
                      "route": "muse-broker" if muse else "luna-reserve-broker",
                      "harness_version": binding["harness_version"],
                      "override_reason": binding.get("override_reason", "")}
            R.submit(store, {**actual, "receipt_id": "broker-launch:" + run_id, "kind": "launch"})
            if turn.get("status") not in {"completed", "interrupted", "cancelled", "failed"}:
                continue
            terminal = turn["status"]
            receipt = {**actual, "kind": "run", "receipt_id": "broker-result:" + run_id,
                       "terminal": terminal, "usage_complete": False, "segments": [],
                       "detail": "provider terminal; quality requires master verification"}
            if muse:
                try:
                    captured = C.muse_turn(record, turn["n"])
                    receipt["segments"] = [{"id": "muse-parent:" + run_id, "usage": captured["usage"]}]
                    receipt["detail"] += "; capability gap: reminder-agent usage is not in parent totals"
                except C.CaptureError as exc:
                    receipt["detail"] += "; usage gap: " + str(exc)
            else:
                receipt["detail"] += "; capability gap: cumulative Luna counter needs adjacent native rollout windows"
            # Revision identity keeps changed provider evidence inspectable.
            # The immutable parent segment refuses changed totals rather than
            # silently adding a revised cumulative total as another segment.
            receipt["receipt_id"] += ":" + R.digest(receipt)
            R.submit(store, receipt)
        return R.health(store, 5)
    finally:
        store.close()


def binding(directory, episode_id):
    store = R.open_runtime(Path(directory))
    try:
        _, config, decision = R._context(store, episode_id)
        return {"directory": str(Path(directory).resolve()), "episode_id": episode_id,
                "harness_version": config["harness_version"], "option": decision["actual"]}
    finally:
        store.close()


PROFILES = Path(__file__).resolve().parents[1] / ".claude" / "agents"


def claude_profile_effort(agent_type, profiles=None):
    """Effort of a Claude Code agent profile: frontmatter `effort` 1..5 -> low..max.

    Returns None when no project profile of that name exists (a built-in agent
    type); refuses a profile without a valid effort, or whose name ends in a
    different effort word than its number.
    """
    if not isinstance(agent_type, str) or not agent_type or "/" in agent_type:
        raise C.CaptureError("subagent meta has no valid agentType")
    path = Path(profiles or PROFILES) / (agent_type + ".md")
    if not path.is_file():
        return None
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---" or "---" not in [line.strip() for line in lines[1:]]:
        raise C.CaptureError(f"profile {agent_type} has no frontmatter")
    front = lines[1:1 + [line.strip() for line in lines[1:]].index("---")]
    values = [line.split(":", 1)[1].strip() for line in front if line.split(":", 1)[0].strip() == "effort"]
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) not in C.CLAUDE_PROFILE_EFFORTS:
        raise C.CaptureError(f"profile {agent_type} has no effort 1..5")
    effort = C.CLAUDE_PROFILE_EFFORTS[int(values[0])]
    named = agent_type.rsplit("-", 1)[-1]
    if named in C.CLAUDE_PROFILE_EFFORTS.values() and named != effort:
        raise C.CaptureError(f"profile {agent_type} names effort {named} but sets {effort}")
    return effort


def _claude_tree(transcript, start_ms, end_ms, seen, owner):
    """Parent transcript plus every Agent-tool child in its directory, recursively.

    Returns (parts, owners, gaps): owners[i] is the agent id of parts[i].
    """
    part = C.claude_transcript(transcript, start_ms, end_ms)
    parts, owners, gaps = [part], [owner], []
    directory = Path(transcript).parent
    metas = {}
    for meta_path in sorted(directory.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and isinstance(meta.get("toolUseId"), str):
            metas.setdefault(meta["toolUseId"], []).append(meta_path.name[len("agent-"):-len(".meta.json")])
    for tool_use, agent in sorted(part["children"].items()):
        ids = set(metas.get(tool_use, [])) | ({agent} if agent else set())
        if not ids:
            gaps.append(f"Agent call {tool_use} has no recorded result or child transcript")
            continue
        for child in sorted(ids):
            path = directory / f"agent-{child}.jsonl"
            if child in seen:
                continue
            seen.add(child)
            if not path.is_file():
                gaps.append(f"child agent {child} transcript is missing")
                continue
            child_parts, child_owners, child_gaps = _claude_tree(path, None, None, seen, child)
            parts += child_parts
            owners += child_owners
            gaps += child_gaps
    return parts, owners, gaps


def _telemetry(telemetry):
    """Parsed telemetry of an explicit directory, or None when no directory is named."""
    return None if telemetry is None else C.claude_telemetry(Path(telemetry).expanduser())


def claude_code_run(path, episode_id, harness_version, terminal, run_id=None,
                    route="claude-agent-tool", family=None, since=None, until=None,
                    profiles=None, override_reason="", attempt_index=1, telemetry=None):
    """Import one Claude Code subagent (`subagents/agent-<id>.jsonl` + `.meta.json`).

    Release is the single observed `message.model`; effort is the agent
    profile's (or, for a built-in agent type, the uniform observed per-turn
    effort). Nested Agent-tool children are part of the run. Final usage per
    response comes from `telemetry` (a `claude-otel` directory) joined by
    request id; telemetry requests of the run's agents without a transcript
    entry are added. Usage is final only when every response of every included
    transcript has a telemetry record and no session request is unattributable.
    `since`/`until` (UTC ISO) bound a resumed continuation measured separately.
    """
    path = Path(path).resolve()
    if not path.name.startswith("agent-") or path.suffix != ".jsonl":
        raise C.CaptureError("Claude Code run source must be a subagents/agent-<id>.jsonl transcript")
    agent = path.name[len("agent-"):-len(".jsonl")]
    try:
        meta = json.loads(path.with_name(f"agent-{agent}.meta.json").read_text())
    except (OSError, ValueError) as exc:
        raise C.CaptureError(f"subagent meta is missing or unreadable: {exc}") from exc
    start_ms = C.utc_millis(since, "since") if since is not None else None
    end_ms = C.utc_millis(until, "until") if until is not None else None
    parts, owners, gaps = _claude_tree(path, start_ms, end_ms, {agent}, agent)
    own = parts[0]
    if not own["calls"]:
        raise C.CaptureError("subagent transcript has no model response in the window")
    if len(own["models"]) != 1:
        raise C.CaptureError(f"subagent changed release {own['models']}; register separate attempts")
    release = own["models"][0]
    observed_family = C.CLAUDE_FAMILIES.get(release)
    if observed_family is None:
        raise C.CaptureError(f"unknown Claude release {release!r}; extend the family map deliberately")
    if family is not None and family != observed_family:
        raise C.CaptureError("observed Claude release does not match its family")
    if meta.get("model") and C.CLAUDE_FAMILIES.get(release) != meta["model"]:
        raise C.CaptureError(f"meta model alias {meta['model']!r} disagrees with observed {release}")
    effort = claude_profile_effort(meta.get("agentType"), profiles)
    if len(own["efforts"]) > 1:
        raise C.CaptureError(f"subagent changed effort {own['efforts']}; register separate attempts")
    if effort is None:
        if not own["efforts"]:
            raise C.CaptureError(f"agent type {meta.get('agentType')!r} has no profile effort and no observed effort")
        effort = own["efforts"][0]
    elif own["efforts"] and own["efforts"] != [effort]:
        raise C.CaptureError(f"observed effort {own['efforts']} disagrees with profile effort {effort}")
    siblings = sorted(path.parent.glob("agent-*.jsonl"))
    session_dir = path.parent.parent
    siblings.append(session_dir.parent / (session_dir.name + ".jsonl"))
    join = C.claude_join(parts, owners, _telemetry(telemetry), {agent: (start_ms, end_ms)}, siblings)
    usage = C.claude_usage(parts, gaps, join)
    usage.update(source=own["source"], start_offset=own["first_ms"], end_offset=own["last_ms"] + 1,
                 transcripts=[part["path"] for part in parts], agent_type=meta.get("agentType"),
                 child_models=sorted({m for part in parts[1:] for m in part["models"]}))
    complete = usage["total_input"] is not None and usage["total_output"] is not None
    run_id = run_id or "claude-agent:" + agent + (f":{start_ms}" if start_ms is not None else "")
    receipt = {"receipt_id": "claude-code:" + run_id + ":" + R.digest(usage), "kind": "run",
               "episode_id": episode_id, "run_id": run_id, "attempt_index": attempt_index,
               "option": observed_family + "/" + effort, "release": release, "route": route,
               "harness_version": harness_version, "override_reason": override_reason,
               "terminal": terminal,
               "segments": [{"id": f"claude-code:{own['source']}:{usage['start_offset']}:{usage['end_offset']}",
                             "usage": usage}],
               "usage_complete": complete, "usage_evidence": str(path)}
    if not complete:
        raw = usage["provider_raw"]
        receipt["detail"] = (f"usage gap: {raw['unfinished_calls']} of {raw['calls']} responses lack final "
                             f"output usage; {raw['compactions']} compactions; "
                             + "; ".join(raw["gaps"])).rstrip("; ")
    return receipt


def claude_master_window(path, start, end, client="claude-code", weight=1.0, telemetry=None):
    """A master session window [start, end) (UTC ISO) of its own top-level transcript.

    Each response's usage is joined to `telemetry` by request id; the session's
    own telemetry requests in the window without a transcript entry and without
    an agent id (auxiliary requests) are charged too.
    """
    start_ms, end_ms = C.utc_millis(start, "start"), C.utc_millis(end, "end")
    if start_ms >= end_ms:
        raise C.CaptureError("master window start must precede its end")
    import time
    if end_ms > time.time() * 1000:
        raise C.CaptureError("master window ends in the future; measure it after it closes")
    part = C.claude_transcript(Path(path), start_ms, end_ms, main_only=True)
    # The master's own Agent calls are separate runs, not master window cost.
    join = C.claude_join([part], [None], _telemetry(telemetry), {None: (start_ms, end_ms)})
    usage = C.claude_usage([part], [], join)
    usage.update(source=part["source"], start_offset=start_ms, end_offset=end_ms,
                 observed_models=sorted(set(part["models"]) | set(join["added_models"])), path=part["path"])
    return {"id": f"claude-code-window:{part['source']}:{start_ms}:{end_ms}", "client": client,
            "weight": weight, "usage": usage}


def antigravity_run(run_dir, episode_id, harness_version, terminal=None, run_id=None,
                    route="antigravity-run", override_reason="", attempt_index=1):
    """Import one `creme antigravity run` record as a run of option family/effort.

    Release is the init event's model slug. Usage is final only when the result
    event is present and no step (checkpoint, subagent) consumed unreported usage.
    """
    record = C.antigravity_record(Path(run_dir))
    verdict, init = record["verdict"], record["init"]
    if init is None or not init.get("model"):
        raise C.CaptureError("Antigravity run has no init model")
    family, effort = verdict.get("family"), verdict.get("effort")
    if not family or not effort or init["model"] != f"{family}-{effort}":
        raise C.CaptureError(f"init model {init['model']!r} is not {family}-{effort}")
    usage = C.antigravity_usage(record)
    complete = record["result"] is not None and not record["gaps"] and usage["total_input"] is not None
    name = verdict.get("run") or Path(run_dir).name
    run_id = run_id or "antigravity:" + name
    if terminal is None:
        status = (record["result"] or {}).get("status")
        terminal = "completed" if status == "SUCCESS" else "failed"
    conversation = (record["result"] or {}).get("conversation_id") or init.get("conversation_id") or "none"
    receipt = {"receipt_id": "antigravity:" + run_id + ":" + R.digest(usage), "kind": "run",
               "episode_id": episode_id, "run_id": run_id, "attempt_index": attempt_index,
               "option": family + "/" + effort, "release": init["model"], "route": route,
               "harness_version": harness_version, "override_reason": override_reason,
               "terminal": terminal,
               "segments": [{"id": f"antigravity:{name}:{conversation}", "usage": usage}],
               "usage_complete": complete, "usage_evidence": record["run_dir"]}
    if not complete:
        receipt["detail"] = "usage gap: " + "; ".join(
            record["gaps"] + ([] if record["result"] is not None else ["no result event"]))
    return receipt
