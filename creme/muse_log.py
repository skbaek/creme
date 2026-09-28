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
        self.applied: set[str] = set()               # approval ids with a final decision
        self.terminals: dict[str, dict] = {}         # run id -> {"terminal", "reason"}
        self.messages: dict[str, str] = {}           # run id -> last committed assistant text
        self.error: Optional[str] = None
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
        count = 0
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break
            self.offset += len(raw)
            count += 1
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                self._take(record)
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
        elif payload.get("kind") == "run" and isinstance(run_id, str):
            if kind == "terminal":
                self.terminals[run_id] = {"terminal": event.get("terminal"), "reason": event.get("reason")}
            elif kind == "assistant_message_committed" and isinstance(event.get("text"), str):
                self.messages[run_id] = event["text"]

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
