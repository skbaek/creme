"""Model-pinned, guarded client for the Muse Session Protocol (MSP) over ``muse serve``.

Two layers, as for Codex (``creme.codex_app_server``):

``MuseServeProcess`` is transport. It reuses ``AppServerProcess`` (line
JSON-RPC over the child's stdio) with the MSP envelope (``"jsonrpc": "2.0"``)
and answers the server's must-answer presentation requests
(``approval/request``, ``userInput/request``) with an empty receipt; the
decision itself travels later as ``approval/decide``.

``MuseGuard`` is policy. It is the only way to attach, reconfigure, start,
steer, or interrupt model work. Every request is built from an allowlist:
``session/setModel`` may name only the pinned model (never a ``contributor``
variant), ``turn/start`` and ``turn/steer`` carry the session's explicit
effort, a steer names the session's active turn, and after the first guard
failure only ``turn/interrupt`` is still permitted. Every notification is
checked: a model other than the pin on ``session/modelChanged`` or
``session/tokenUsage``, any ``session/modelRouteUnserved``, a forbidden tool,
or a native subagent is a guard failure. ``decide_approval`` is the broker's
approval allowlist; ``audit_session_log`` re-reads the durable session log
after each turn.

The module names the pinned model once (``PINNED_MODEL``); nothing here falls
back to a server default, because the server's default is a contributor model.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from . import luna_lean
from .codex_app_server import AppServerError, AppServerProcess, PinViolation

PINNED_MODEL = "muse-spark-1.3"
PINNED_PROVIDER = "meta"
PINNED_PROFILE = "tbh"
# The catalogue's effort ladder for the pinned model; admission re-checks it
# against the live ``model/list`` variants.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
CLIENT_NAME = "creme_muse"

# Session approval modes (MSP ``ApprovalMode``). Every pseudo-subagent session
# uses ``promptUnmatched`` so that each unmatched action reaches the broker's
# allowlist; ``allowAll`` and ``onRequest`` would let actions run unasked.
APPROVAL_MODE = "promptUnmatched"

# Native tools a pseudo-subagent never uses: web access, the user's personal
# memory store, scheduled jobs, and workflows that can spawn children.
FORBIDDEN_TOOLS = frozenset({
    "web_fetch", "web_search", "add_memory", "edit_memory", "cron_create", "cron_delete", "workflow",
})
LEAN_TOOL_PREFIX = "mcp__lean_lsp_mcp__"

READ_ONLY_METHODS = frozenset({"model/list", "usage/read", "session/read", "approval/listPending"})

_CONTRIBUTOR = re.compile(r"contributor", re.IGNORECASE)


def uuid7() -> str:
    """A UUIDv7 (MSP requires one for every commandId)."""
    milliseconds = int(time.time() * 1000)
    raw = bytearray(milliseconds.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def model_failures(model: Any, where: str) -> list[str]:
    """Why a served or requested model id is not the pin (empty when it is)."""
    if model == PINNED_MODEL:
        return []
    if isinstance(model, str) and _CONTRIBUTOR.search(model):
        return [f"{where}: contributor model {model!r} (never permitted)"]
    return [f"{where}: model {model!r} is not the pinned {PINNED_MODEL}"]


# ---------------------------------------------------------------------------
# Transport


def receipt_handler(method: str, params: Any, request_id: Any) -> Optional[dict]:
    """Answer MSP server requests with the presentation receipt; the decision is a later command."""
    if method in ("approval/request", "userInput/request"):
        return {"result": {}}
    return {"error": {"code": -32601, "message": f"{method} is not supported by this Creme client"}}


class MuseServeProcess(AppServerProcess):
    subcommand = "serve"
    label = "muse serve"
    jsonrpc = "2.0"

    def initialize_params(self, client_name: str) -> dict:
        # userInputDialogs=false: the host withholds userInput/request, so a
        # model question can never wait on a surface nobody answers.
        return {"clientInfo": {"name": CLIENT_NAME, "version": "1"},
                "capabilities": {"userInputDialogs": False}}


# ---------------------------------------------------------------------------
# Approval allowlist


# Read-only commands a Lean session (whose host runs unsandboxed so that the
# owned build can lower its priority) may run without the master's decision.
READ_ONLY_COMMANDS = frozenset({"ls", "cat", "head", "tail", "wc", "rg", "grep", "pwd", "stat", "file"})
READ_ONLY_GIT = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "blame"})
_SHELL_META = re.compile(r"[;&|<>`$(){}\\\n\r]")
_BUILD = re.compile(r"^(?P<launcher>\S+) lake-build (?P<goal>\S+)(?: --wait (?P<wait>[0-9]+))? -- "
                    r"(?P<modules>[A-Za-z0-9_.]+(?: [A-Za-z0-9_.]+)*)$")


def _stages(subject: dict) -> list[list[str]]:
    stages = []
    for stage in subject.get("stages") or []:
        if not isinstance(stage, dict) or not stage.get("argvComplete"):
            return []
        argv = stage.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            return []
        stages.append(argv)
    return stages


def _read_only_argv(argv: list[str]) -> bool:
    name = PurePosixPath(argv[0]).name
    if any(_SHELL_META.search(item) for item in argv):
        return False
    if name == "git":
        rest = [item for item in argv[1:] if not item.startswith("-")]
        return bool(rest) and rest[0] in READ_ONLY_GIT and "--output" not in " ".join(argv)
    return name in READ_ONLY_COMMANDS


def build_command_failure(command: str, goal: str, workspace: Optional[str], target: Path) -> Optional[str]:
    """The failed rule of the exact owned-build command, or ``None`` when it is allowed."""
    if _SHELL_META.search(command):
        return "the command has shell metacharacters"
    match = _BUILD.fullmatch(command.strip())
    if match is None:
        return "the command is not `~/creme/scripts/creme lake-build GOAL [--wait N] -- MODULES`"
    launchers = {"~/creme/scripts/creme", str(Path.home() / "creme/scripts/creme")}
    if match.group("launcher") not in launchers:
        return "the launcher is not ~/creme/scripts/creme"
    if match.group("goal") != goal:
        return f"the goal label is not {goal}"
    if match.group("wait") is not None and not 1 <= int(match.group("wait")) <= 900:
        return "--wait must be an integer from 1 through 900"
    try:
        if workspace is None or Path(workspace).resolve() != target.resolve():
            return "the command's workspace is not the session target"
    except (OSError, RuntimeError):
        return "the workspace cannot be resolved"
    return None


@dataclass
class ApprovalDecision:
    action: str                 # "approve", "abort", or "master"
    reason: str
    choice: Optional[str] = None


def _choice(params: dict, decision: str) -> Optional[str]:
    for choice in params.get("availableChoices") or []:
        if isinstance(choice, dict) and choice.get("decision") == decision and choice.get("scope") == "once":
            return choice.get("choiceId")
    return None


def decide_approval(params: dict, mode: str, lean_goal: Optional[str], target: Path) -> ApprovalDecision:
    """The broker's approval allowlist. Never an LLM judge, never a session-wide or persistent choice.

    * read-only and write sessions run shell commands inside Muse's sandbox
      (read-only, or writes confined to the workspace and temporary
      directories, no network), so a shell command is approved once and the
      sandbox is the control;
    * a Lean session's host is unsandboxed: only the exact owned build, a
      narrow read-only command set, and the non-open-world Lean tools are
      approved; the semaphore and reclamation commands are aborted; any other
      command goes to the master;
    * every other subject (MCP outside Lean mode, network, file access outside
      the workspace, protected writes, unknown kinds) is aborted.
    """
    approve, abort = _choice(params, "approved"), _choice(params, "abort")
    subject = params.get("subject") if isinstance(params.get("subject"), dict) else {}
    kind = subject.get("kind")
    if abort is None:
        return ApprovalDecision("master", "no one-shot abort choice is offered")

    def denied(reason: str) -> ApprovalDecision:
        return ApprovalDecision("abort", reason, abort)

    if params.get("protectedWrite"):
        return denied("a protected write is never approved for a pseudo-subagent")
    if params.get("subagentOrigin"):
        return denied("an approval from a native subagent is never approved")
    if kind == "tool":
        name = str(subject.get("toolName") or params.get("toolName") or "")
        if lean_goal is None or not name.startswith(LEAN_TOOL_PREFIX):
            return denied(f"tool {name} is not available to this session")
        tool = name[len(LEAN_TOOL_PREFIX):]
        if tool in luna_lean.OPEN_WORLD_TOOLS or tool in luna_lean.FORBIDDEN_TOOLS:
            return denied(f"Lean tool {tool} reaches the network or builds; never approved")
        if approve is None:
            return denied("no one-shot approval choice is offered")
        return ApprovalDecision("approve", f"Lean tool {tool}", approve)
    if kind != "shell":
        return denied(f"approval subject {kind!r} is never approved")
    command = str(subject.get("command") or "")
    if approve is None:
        return denied("no one-shot approval choice is offered")
    if lean_goal is None:
        return ApprovalDecision("approve", f"sandboxed {mode} shell command", approve)
    reason = luna_lean.forbidden_command(command)
    if reason is not None:
        return denied(reason)
    if build_command_failure(command, lean_goal, subject.get("workspaceRoot"), target) is None:
        return ApprovalDecision("approve", "the owned narrow build", approve)
    stages = _stages(subject)
    if stages and all(_read_only_argv(argv) for argv in stages):
        return ApprovalDecision("approve", "read-only command", approve)
    return ApprovalDecision("master", "unsandboxed command outside the Lean allowlist")


# ---------------------------------------------------------------------------
# Guarded session


@dataclass
class TurnOutcome:
    turn_id: str
    status: Optional[str] = None             # completed | failed | cancelled | lost
    error: Optional[dict] = None
    final_message: Optional[str] = None
    tokens: dict = field(default_factory=lambda: {"prompt": 0, "output": 0, "total": 0, "cached": 0,
                                                  "reasoning": 0, "completions": 0})
    models: dict = field(default_factory=dict)
    steered: int = 0
    steer_items: list = field(default_factory=list)
    tool_calls: int = 0
    guard_failures: list = field(default_factory=list)
    started: float = field(default_factory=time.time)
    completed: Optional[float] = None
    duration_ms: Optional[int] = None


class MuseGuard:
    """Pins and checks every MSP request and notification of one Muse session."""

    def __init__(self, process: AppServerProcess, session_id: str, effort: str, mode: str,
                 lean_goal: Optional[str] = None) -> None:
        if effort not in EFFORTS:
            raise PinViolation(f"effort {effort!r} is not one of {EFFORTS}")
        self.process = process
        self.session_id = session_id
        self.effort = effort
        self.mode = mode
        self.lean_goal = lean_goal
        self.active_turn: Optional[str] = None
        self.outcome: Optional[TurnOutcome] = None
        self.guard_failures: list[str] = []
        self.models_seen: dict[str, int] = {}

    # -- requests -------------------------------------------------------
    def check(self, method: str, params: dict) -> None:
        if self.guard_failures and method != "turn/interrupt":
            raise PinViolation(f"{method} refused after a guard failure")
        if method in READ_ONLY_METHODS:
            return
        if params.get("sessionId") != self.session_id:
            raise PinViolation(f"{method} names another session")
        if method == "session/setModel":
            model = (params.get("model") or {}).get("modelId")
            failures = model_failures(model, "session/setModel")
            if failures:
                raise PinViolation(failures[0])
        elif method == "session/setApprovalMode":
            if params.get("mode") != APPROVAL_MODE:
                raise PinViolation(f"approval mode {params.get('mode')!r} is not {APPROVAL_MODE}")
        elif method in ("session/setReasoningEffort", "turn/start", "turn/steer"):
            if params.get("reasoningEffort") != self.effort:
                raise PinViolation(f"{method} must carry the session effort {self.effort}")
            if method == "turn/start" and self.active_turn is not None:
                raise PinViolation("turn/start while a turn is active")
            if method == "turn/start" and params.get("ifBusy") not in (None, "queue"):
                raise PinViolation("turn/start may not steer or replace; use turn/steer")
            if method == "turn/steer" and (self.active_turn is None or params.get("expectedTurnId") != self.active_turn):
                raise PinViolation("turn/steer must name the session's active turn")
        elif method in ("turn/interrupt", "approval/decide", "session/resume"):
            pass
        else:
            raise PinViolation(f"{method} is not an allowed method")

    def request(self, method: str, params: dict, timeout: float = 60.0) -> Any:
        self.check(method, params)
        return self.process.request(method, params, timeout=timeout)

    def command(self, method: str, params: dict, timeout: float = 60.0) -> Any:
        return self.request(method, {"commandId": uuid7(), "sessionId": self.session_id, **params}, timeout)

    def pin(self) -> list[str]:
        """Select the pinned model, the approval mode, and the effort; return failures of the read-back."""
        self.command("session/setModel", {"model": {"modelId": PINNED_MODEL, "providerId": PINNED_PROVIDER,
                                                    "profileId": PINNED_PROFILE}})
        self.command("session/setApprovalMode", {"mode": APPROVAL_MODE})
        self.command("session/setReasoningEffort", {"reasoningEffort": self.effort})
        return self.verify_pins()

    def verify_pins(self) -> list[str]:
        failures: list[str] = []
        listing = self.request("model/list", {"sessionId": self.session_id}) or {}
        active = [row for row in listing.get("models") or [] if isinstance(row, dict) and row.get("isActive")]
        if len(active) != 1:
            failures.append(f"model/list shows {len(active)} active models; exactly the pin is required")
        for row in active:
            failures += model_failures(row.get("modelId"), "model/list active row")
            if self.effort not in (row.get("variants") or []):
                failures.append(f"effort {self.effort} is not offered for {row.get('modelId')}")
        read = self.request("session/read", {"sessionId": self.session_id}) or {}
        session = read.get("session") or {}
        failures += model_failures(session.get("modelId"), "session/read")
        mode = (session.get("approvalMode") or {}).get("mode")
        if mode != APPROVAL_MODE:
            failures.append(f"session approval mode is {mode!r}, not {APPROVAL_MODE}")
        return failures

    def begin_turn(self, text: str) -> TurnOutcome:
        result = self.command("turn/start", {"input": [{"type": "text", "text": text}],
                                             "reasoningEffort": self.effort}) or {}
        if result.get("disposition") != "started" or not result.get("turnId"):
            raise AppServerError(f"turn/start did not start a turn: {json.dumps(result)[:200]}")
        self.active_turn = result["turnId"]
        self.outcome = TurnOutcome(turn_id=self.active_turn)
        return self.outcome

    def steer(self, text: str) -> Any:
        return self.command("turn/steer", {"expectedTurnId": self.active_turn,
                                           "input": [{"type": "text", "text": text}],
                                           "reasoningEffort": self.effort})

    def interrupt(self) -> Any:
        params: dict = {"commandId": uuid7(), "sessionId": self.session_id}
        if self.active_turn:
            params["turnId"] = self.active_turn
        return self.request("turn/interrupt", params, timeout=30)

    def decide(self, params: dict, choice: str) -> Any:
        return self.command("approval/decide", {"approvalId": params["approvalId"], "choiceId": choice,
                                                "requirementId": params["currentRequirementId"]}, timeout=30)

    def finish_turn(self) -> Optional[TurnOutcome]:
        outcome, self.outcome, self.active_turn = self.outcome, None, None
        return outcome

    # -- notifications --------------------------------------------------
    def fail(self, failure: str) -> list[str]:
        self.guard_failures.append(failure)
        if self.outcome is not None:
            self.outcome.guard_failures.append(failure)
        return [failure]

    def see_model(self, model: Any, where: str) -> list[str]:
        if model is None:
            return []
        self.models_seen[str(model)] = self.models_seen.get(str(model), 0) + 1
        if self.outcome is not None:
            self.outcome.models[str(model)] = self.outcome.models.get(str(model), 0) + 1
        failures = model_failures(model, where)
        for failure in failures:
            self.fail(failure)
        return failures

    def observe(self, message: dict) -> list[str]:
        """Check one notification; return new guard failures."""
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if params.get("sessionId") not in (None, self.session_id):
            return []
        outcome = self.outcome
        if method == "session/modelChanged":
            return self.see_model(params.get("modelId"), "session/modelChanged")
        if method == "session/modelRouteUnserved":
            return self.fail(f"session/modelRouteUnserved: the pinned route {params.get('modelId')!r} "
                             f"is unserved (installed {params.get('installedProviderId')!r})")
        if method == "session/tokenUsage":
            failures = self.see_model(params.get("modelId"), "session/tokenUsage")
            if params.get("modelId") is None:
                failures += self.fail("session/tokenUsage without a model id; attribution cannot be shown")
            if outcome is not None and params.get("turnId") == outcome.turn_id:
                usage = params.get("usage") or {}
                outcome.tokens["prompt"] += int(params.get("promptTokens") or 0)
                outcome.tokens["output"] += int(usage.get("outputTokens") or 0)
                outcome.tokens["total"] += int(params.get("totalTokens") or 0)
                outcome.tokens["cached"] += int(usage.get("cachedTokens") or 0)
                outcome.tokens["reasoning"] += int(usage.get("reasoningTokens") or 0)
                outcome.tokens["completions"] += 1
            return failures
        if method in ("item/started", "item/completed"):
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            kind = item.get("kind")
            if kind == "subagent":
                return self.fail(f"a native subagent was started ({item.get('agentPath') or item.get('role')})")
            if kind == "toolCall":
                tool = str(item.get("tool") or "")
                if method == "item/started" and outcome is not None:
                    outcome.tool_calls += 1
                if tool in FORBIDDEN_TOOLS:
                    return self.fail(f"forbidden tool {tool} was called")
                if tool.startswith("mcp__") and method == "item/completed" and item.get("status") == "completed":
                    lean_tool = tool[len(LEAN_TOOL_PREFIX):] if tool.startswith(LEAN_TOOL_PREFIX) else None
                    if self.lean_goal is None or lean_tool is None or lean_tool in luna_lean.OPEN_WORLD_TOOLS \
                            or lean_tool in luna_lean.FORBIDDEN_TOOLS:
                        return self.fail(f"MCP tool {tool} ran outside what this session allows")
            if kind == "userMessage" and item.get("steered") and outcome is not None \
                    and method == "item/completed":
                outcome.steer_items.append(item.get("itemId"))
            if kind == "agentMessage" and method == "item/completed" and outcome is not None \
                    and item.get("turnId") == outcome.turn_id and item.get("text") is not None:
                outcome.final_message = item.get("text")
            return []
        if method == "turn/completed" and outcome is not None and params.get("turnId") == outcome.turn_id:
            outcome.status = params.get("terminal")
            outcome.error = params.get("error")
            outcome.completed = time.time()
            outcome.duration_ms = params.get("durationMs")
        return []


# ---------------------------------------------------------------------------
# Durable session-log audit


_MODEL_KEYS = ("model_id", "modelId", "model")


def _models_in(value: Any, found: list) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _MODEL_KEYS and isinstance(item, str):
                found.append(item)
            elif isinstance(item, str) and item.startswith("{") and len(item) < 2_000_000 and '"model' in item:
                try:
                    _models_in(json.loads(item), found)
                except ValueError:
                    pass
            else:
                _models_in(item, found)
    elif isinstance(value, list):
        for item in value:
            _models_in(item, found)


def log_length(path: Optional[Path]) -> int:
    if path is None or not path.is_file():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def audit_session_log(path: Optional[Path], start_line: int) -> dict:
    """Every model id the durable log names after ``start_line`` must be the pin.

    ``same-as-main`` (the reminder roster's model) is accepted only as that
    literal, which the runtime resolves to the session's model.
    """
    result: dict = {"verdict": "PASS", "failures": [], "models": {}, "lines": 0, "start_line": start_line}
    if path is None or not path.is_file():
        result.update(verdict="FAIL", failures=[f"session log {path} is not readable"])
        return result
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle):
            if number < start_line:
                continue
            result["lines"] += 1
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            found: list = []
            _models_in(record, found)
            for model in found:
                result["models"][model] = result["models"].get(model, 0) + 1
    for model in result["models"]:
        if model == "same-as-main":
            continue
        result["failures"] += model_failures(model, "session log")
    if result["failures"]:
        result["verdict"] = "FAIL"
    return result


def describe_approval(params: dict) -> str:
    subject = params.get("subject") if isinstance(params.get("subject"), dict) else {}
    kind = subject.get("kind")
    if kind == "shell":
        text = f"shell `{subject.get('command')}` in {subject.get('workspaceRoot')}"
    elif kind == "tool":
        text = f"tool {subject.get('toolName') or params.get('toolName')}"
    else:
        text = f"{kind} {json.dumps({k: v for k, v in subject.items() if k != 'stages'})[:200]}"
    return text.replace("\n", " ")[:300]


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(item) for item in argv)
