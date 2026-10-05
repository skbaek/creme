"""Tail of Muse's durable session log: the source of truth when the view projection is gone.

Measured 2026-09-28 (Muse 1.4.0): once a session's materialized view is
unavailable, ``approval/listPending``, ``session/read`` and ``view/page`` can
all fail (``-32603 ... materialized session view is unavailable``), while the
durable log (``~/.local/share/muse/sessions/.../session.jsonl``) keeps every
record and ``approval/decide`` still works. The records used here:

* ``payload.kind == "approval"``, ``event.kind == "requested"``: a pending
  action (``pending_action_id`` = the MSP ``approvalId``) with
  ``approval_subject`` (``shell_command`` with ``raw_command`` and ``stages``,
  or ``tool_action`` with ``tool_name``);
* ``event.kind == "stage_requirement_resolved"``: one stage decided;
* ``event.kind == "decision_applied"``: the approval's final decision;
* ``payload.kind == "run"``, ``event.kind == "terminal"``: the run's (turn's)
  terminal, with ``terminal`` and ``reason``; the run id is the turn id;
* ``event.kind == "assistant_message_committed"``: committed assistant text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

ONCE_CHOICES = [
    {"choiceId": "allow_once", "decision": "approved", "scope": "once", "label": "allow once"},
    {"choiceId": "abort", "decision": "abort", "scope": "once", "label": "abort"},
]


class DurableLog:
    """Incremental reader of one session log from a line offset (only complete lines are consumed)."""

    def __init__(self, path: Optional[Path], start_line: int = 0) -> None:
        self.path = path
        self.offset = 0
        self.requested: dict[str, dict] = {}         # approval id -> requested event (+ run_id)
        self.resolved: dict[str, set[int]] = {}      # approval id -> resolved source indexes
        self.decisions: dict[str, dict] = {}          # exact command id -> authoritative applied decision
        self.completions: dict[str, list[dict]] = {}  # parent run id -> durable model completion events
        self.applied: set[str] = set()               # approval ids with a final decision
        self.terminals: dict[str, dict] = {}         # run id -> {"terminal", "reason"}
        self.messages: dict[str, str] = {}           # run id -> last committed assistant text
        self.message_list: dict[str, list[str]] = {}  # run id -> every committed assistant text, in order
        self.error: Optional[str] = None
        self.corrupt = False
        if path is not None and path.is_file():
            with path.open("rb") as handle:
                for _ in range(start_line):
                    if not handle.readline():
                        break
                self.offset = handle.tell()

    def poll(self) -> int:
        """Read new complete records; return how many were read."""
        if self.path is None:
            return 0
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError as exc:
            self.error = str(exc)
            return 0
        if not self.corrupt:
            self.error = None
        count = 0
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break
            self.offset += len(raw)
            count += 1
            try:
                record = json.loads(raw)
            except ValueError:
                self.corrupt = True
                self.error = "invalid JSON in durable session log"
                continue
            if isinstance(record, dict):
                self._take(record)
            else:
                self.corrupt = True
                self.error = "non-object record in durable session log"
        return count

    def _take(self, record: dict) -> None:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        kind = event.get("kind")
        run_id = payload.get("run_id")
        if payload.get("kind") == "approval":
            action = event.get("pending_action_id")
            if not isinstance(action, str):
                return
            if kind == "requested":
                self.requested[action] = {**event, "run_id": run_id}
            elif kind == "stage_requirement_resolved":
                index = (event.get("requirement_id") or {}).get("source_index")
                if isinstance(index, int):
                    self.resolved.setdefault(action, set()).add(index)
            elif kind == "decision_applied":
                self.applied.add(action)
                command = event.get("decided_by_command_id")
                if isinstance(command, str):
                    applied = {**event, "run_id": run_id}
                    if command in self.decisions and self.decisions[command] != applied:
                        self.corrupt = True
                        self.error = "conflicting durable decisions for one command id"
                    self.decisions[command] = applied
        elif payload.get("kind") == "run" and isinstance(run_id, str):
            if kind == "terminal":
                self.terminals[run_id] = dict(event)
            elif kind == "model_completed":
                self.completions.setdefault(run_id, []).append(event)
            elif kind == "assistant_message_committed" and isinstance(event.get("text"), str):
                self.message_list.setdefault(run_id, []).append(event["text"])
                self.messages[run_id] = event["text"]

    def confirms_decision(self, request: dict, decision: str, run_id: Optional[str]) -> bool:
        event = self.decisions.get(request.get("commandId")) or {}
        stream = event.get("session_stream") or {}
        if not isinstance(stream, dict):
            return False
        return not self.error and bool(event) and event.get("pending_action_id") == request.get("approvalId") \
            and event.get("decision") == decision and event.get("run_id") == run_id \
            and (not stream or stream.get("id") == request.get("sessionId"))

    def usage(self, run_id: str, model: str) -> tuple[Optional[dict], Optional[str]]:
        """Exact parent-run totals, never reminder/child totals or live-plus-durable sums."""
        totals = dict.fromkeys(("prompt", "output", "total", "cached", "reasoning", "completions"), 0)
        events = self.completions.get(run_id) or []
        if self.error:
            return None, self.error
        if not events:
            return None, "no durable model completions for parent run"
        for event in events:
            if event.get("model") != model:
                return None, "durable completion is not attributed to the pinned model"
            usage = event.get("usage")
            if not isinstance(usage, dict):
                return None, "missing or invalid durable model usage"
            fields = {"prompt": usage.get("input_tokens"), "output": usage.get("output_tokens"),
                      "cached": usage.get("cached_tokens", 0), "reasoning": usage.get("reasoning_tokens", 0)}
            if any(type(value) is not int or value < 0 for value in fields.values()):
                return None, "incomplete or invalid durable model usage"
            for key, value in fields.items():
                totals[key] += value
            totals["completions"] += 1
        totals["total"] = totals["prompt"] + totals["output"]
        return totals, None

    def pending(self, session_id: str, workspace: Path, run_id: Optional[str] = None) -> list[dict]:
        """Still-undecided approvals as MSP ``approval/requested``-shaped params (next unresolved stage)."""
        found = []
        for action, event in self.requested.items():
            if action in self.applied or (run_id is not None and event.get("run_id") != run_id):
                continue
            if event.get("run_id") in self.terminals:
                continue
            subject = event.get("approval_subject") if isinstance(event.get("approval_subject"), dict) else {}
            stages = [stage for stage in subject.get("stages") or [] if isinstance(stage, dict)]
            done = self.resolved.get(action, set())
            indexes = [((stage.get("requirement_id") or {}).get("source_index")) for stage in stages]
            open_indexes = [index for index in indexes if isinstance(index, int) and index not in done]
            if stages and not open_indexes:
                continue
            index = open_indexes[0] if open_indexes else 0
            found.append({
                "approvalId": action, "sessionId": session_id, "turnId": event.get("run_id"),
                "currentRequirementId": {"approvalId": action, "sourceIndex": index},
                "availableChoices": [dict(choice) for choice in ONCE_CHOICES],
                "subject": msp_subject(subject, event, workspace), "toolName": event.get("tool_name"),
                "protectedWrite": bool(event.get("protected_write")), "fromDurableLog": True,
            })
        return found

    def terminal(self, run_id: str) -> Optional[dict]:
        return self.terminals.get(run_id)


def msp_subject(subject: dict, event: dict, workspace: Path) -> dict[str, Any]:
    """The MSP ``ApprovalSubject`` shape the allowlist reads, from the durable ``approval_subject``."""
    kind = subject.get("kind")
    if kind == "shell_command":
        return {"kind": "shell", "command": subject.get("raw_command"), "workspaceRoot": str(workspace),
                "stages": [{"argv": stage.get("argv"), "argvComplete": bool(stage.get("argv_complete"))}
                           for stage in subject.get("stages") or [] if isinstance(stage, dict)]}
    if kind == "tool_action":
        return {"kind": "tool", "toolName": subject.get("tool_name") or event.get("tool_name")}
    return {"kind": str(kind)}
