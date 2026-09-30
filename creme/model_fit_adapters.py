"""Automatic broker receipts and one-action native/master usage capture.

Adapters expose capability gaps rather than inventing total episode costs.
Muse parent totals do not cover hidden reminder agents. Luna counters are
thread-cumulative and therefore require explicit adjacent rollout windows.
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
    observed_family = {"gpt-6.1-sol": "sol", "gpt-6-astra": "astra", "gpt-6-luna": "luna"}.get(after["model"])
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
