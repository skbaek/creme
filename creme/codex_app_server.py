"""Isolated, model-pinned client for the Codex ``app-server`` JSON-RPC protocol.

Two layers are provided.

``AppServerProcess`` is transport: it launches ``codex app-server`` with the
isolation arguments, correlates requests and responses, answers server
requests through a pluggable handler, queues notifications, and appends every
message in both directions to a redacted transcript.

``GuardedSession`` is policy: it is the only way to start, resume, steer, or
interrupt model work. Every such request is built from an allowlist of
parameter keys with the model pinned, and every response and notification is
checked. The first guard failure interrupts the active turn. Read-only
methods pass through an explicit allowlist; everything else is refused.

The module names no model itself. Its caller supplies the pinned slug and the
attribution predicate. A later long-lived broker can hold one session per
thread and map send/steer/interrupt/tail/read/approve/stop onto
``turn_start``, ``turn_steer``, ``turn_interrupt``, the transcript,
``request("thread/items/list")``, the server-request handler, and ``close``.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TextIO


# Features whose tools can reach outside the brief: plugins and apps (and the
# MCP servers they bring), computer and browser use, sub-agents that could name
# another model, automatic approval review on another model, fast tier, image
# generation, goals, hooks, memories, and tool suggestion or installation.
DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "plugin_sharing", "tool_suggest",
    "skill_mcp_dependency_install", "multi_agent", "multi_agent_v2",
    "computer_use", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "in_app_browser", "image_generation",
    "fast_mode", "guardian_approval", "hooks", "goals", "memories",
    "standalone_web_search", "remote_control", "js_repl",
)

PERMITTED_SERVICE_TIERS = (None, "default")
PERMITTED_SANDBOXES = ("read-only", "workspace-write")
PERMITTED_EFFORTS = ("low", "medium", "high")
_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

# Read-only methods a session may pass through unchanged.
READ_ONLY_METHODS = frozenset({
    "account/read", "account/rateLimits/read", "model/list", "config/read",
    "experimentalFeature/list", "mcpServerStatus/list", "thread/read",
    "thread/items/list", "thread/turns/list", "thread/loaded/list",
})


class AppServerError(RuntimeError):
    """Transport or protocol failure."""


class PinViolation(RuntimeError):
    """A request or response would leave the pinned, isolated configuration."""


WRITE_PROFILE = "creme_pseudo_subagent_write"


def isolation_arguments(pinned_model: str, effort: str, mcp_servers: dict,
                        foreign_skill_paths: tuple[str, ...] = ()) -> list[str]:
    """Launch arguments that keep user plugins, MCP servers, and tiers out.

    ``mcp_servers`` maps every server name visible from the thread's working
    directory (user and trusted-project layers) to its configuration. A bare
    ``enabled=false`` is not enough for a server defined only in a project
    layer, because the launch-time configuration would then hold a server with
    no transport; each server is therefore replaced by a disabled stub whose
    transport cannot run anything.

    Bundled system skills are switched off, and every other skill that does
    not come from the launch root (``foreign_skill_paths``, such as user
    skills under ``CODEX_HOME``) is disabled by path. Approval requests are
    pinned to the client route: never ``auto_review``, whose reviewer model
    and billing bucket are unverified.
    """
    if effort not in PERMITTED_EFFORTS:
        raise PinViolation(f"effort {effort!r} is not permitted")
    arguments: list[str] = []
    for feature in DISABLED_FEATURES:
        arguments += ["--disable", feature]
    overrides = [
        f'model="{pinned_model}"',
        f'review_model="{pinned_model}"',
        'service_tier="default"',
        f'model_reasoning_effort="{effort}"',
        'web_search="disabled"',
        "notify=[]",
        "include_apps_instructions=false",
        'approval_policy="never"',
        'approvals_reviewer="user"',
        "skills.bundled.enabled=false",
    ]
    if foreign_skill_paths:
        entries = []
        for path in sorted(set(foreign_skill_paths)):
            if not path.startswith("/") or any(ch in path for ch in '"\\\n\r\t'):
                raise PinViolation(f"cannot disable skill with unsafe path {path!r}")
            entries.append(f'{{path="{path}",enabled=false}}')
        overrides.append("skills.config=[" + ",".join(entries) + "]")
    for name in sorted(mcp_servers):
        if not _MCP_NAME.match(name):
            raise PinViolation(f"cannot disable MCP server with unsafe name {name!r}")
        server = mcp_servers[name] if isinstance(mcp_servers[name], dict) else {}
        if server.get("url"):
            overrides.append(f'mcp_servers.{name}={{url="http://127.0.0.1:9/",enabled=false}}')
        else:
            overrides.append(f'mcp_servers.{name}={{command="/usr/bin/false",args=[],enabled=false}}')
    for override in overrides:
        arguments += ["-c", override]
    return arguments


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if key in ("email", "accountId", "access_token", "id_token") else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


ServerRequestHandler = Callable[[str, Any], dict]


def decline_server_requests(method: str, params: Any) -> dict:
    """Default handler: decline approvals, refuse everything else."""
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"result": {"decision": "decline"}}
    if method in ("applyPatchApproval", "execCommandApproval"):
        return {"result": {"decision": "denied"}}
    return {"error": {"code": -32601, "message": f"{method} is not supported by this Creme client"}}


class AppServerProcess:
    def __init__(self, binary: Path, arguments: list[str], env: dict,
                 transcript: Optional[TextIO] = None,
                 server_request_handler: ServerRequestHandler = decline_server_requests,
                 stderr: Optional[TextIO] = None) -> None:
        self.binary = binary
        self.arguments = list(arguments)
        self.env = env
        self.transcript = transcript
        self.stderr = stderr
        self.server_request_handler = server_request_handler
        self.server_requests: list[dict] = []
        self._responses: dict[Any, dict] = {}
        self._condition = threading.Condition()
        self._notifications: "queue.Queue[dict]" = queue.Queue()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._process: Optional[subprocess.Popen] = None
        self._closed = False

    # -- lifecycle -------------------------------------------------------
    def start(self, client_name: str = "creme") -> dict:
        try:
            self._process = subprocess.Popen(
                [str(self.binary), "app-server", *self.arguments],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr if self.stderr is not None else subprocess.DEVNULL,
                text=True, bufsize=1, env=self.env,
            )
        except OSError as exc:
            raise AppServerError(f"cannot start codex app-server: {exc}")
        threading.Thread(target=self._read_loop, daemon=True).start()
        result = self.request("initialize", {
            "clientInfo": {"name": client_name, "title": "Creme", "version": "1"},
        })
        self.notify("initialized", None)
        return result

    def close(self) -> None:
        if self._closed or self._process is None:
            return
        self._closed = True
        process = self._process
        try:
            if process.stdin:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        try:
            if process.stdout:
                process.stdout.close()
        except (OSError, ValueError):
            pass

    def __enter__(self) -> "AppServerProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transcript -----------------------------------------------------
    def _record(self, direction: str, message: dict) -> None:
        if self.transcript is None:
            return
        entry = {"t": round(time.time(), 3), "dir": direction, "msg": _redact(message)}
        with self._write_lock:
            self.transcript.write(json.dumps(entry, sort_keys=True) + "\n")
            self.transcript.flush()

    # -- wire -----------------------------------------------------------
    def _send(self, message: dict) -> None:
        if self._process is None or self._process.stdin is None or self._closed:
            raise AppServerError("codex app-server is not running")
        self._record("out", message)
        try:
            with self._write_lock:
                self._process.stdin.write(json.dumps(message) + "\n")
                self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AppServerError(f"codex app-server write failed: {exc}")

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                self._record("in", message)
                if "method" in message and "id" in message:
                    self.server_requests.append({"method": message["method"], "params": _redact(message.get("params"))})
                    reply = self.server_request_handler(message["method"], message.get("params"))
                    try:
                        self._send({"id": message["id"], **reply})
                    except AppServerError:
                        pass
                elif "id" in message:
                    with self._condition:
                        self._responses[message["id"]] = message
                        self._condition.notify_all()
                elif "method" in message:
                    self._notifications.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self._notifications.put({"method": "creme/transport/closed", "params": {}})
            with self._condition:
                self._condition.notify_all()

    def notify(self, method: str, params: Any) -> None:
        message: dict = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: Any, timeout: float = 60.0) -> Any:
        self._next_id += 1
        identifier = self._next_id
        self._send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._condition:
            while identifier not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError(f"codex app-server did not answer {method}")
                if self._process is not None and self._process.poll() is not None:
                    raise AppServerError(f"codex app-server exited during {method}")
                self._condition.wait(timeout=min(remaining, 0.5))
            message = self._responses.pop(identifier)
        if "error" in message:
            raise AppServerError(f"codex app-server {method} failed: {json.dumps(message['error'])[:400]}")
        return message.get("result")

    def next_notification(self, timeout: float) -> Optional[dict]:
        try:
            return self._notifications.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None


def read_all_features(process: AppServerProcess) -> list[dict]:
    features: list[dict] = []
    cursor = None
    for _ in range(20):
        page = process.request("experimentalFeature/list", {"cursor": cursor, "limit": 500}) or {}
        features.extend(item for item in page.get("data") or [] if isinstance(item, dict))
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return features


def isolation_failures(config_read: dict, features: list[dict], pinned_model: str,
                       launch_root: Path, skills: Optional[dict] = None) -> list[str]:
    """Verify the launched server really runs with the isolation applied."""
    failures: list[str] = []
    config = (config_read or {}).get("config") or {}
    if config.get("model") != pinned_model:
        failures.append(f"effective model is {config.get('model')!r}")
    if config.get("review_model") != pinned_model:
        failures.append(f"effective review model is {config.get('review_model')!r}")
    if config.get("service_tier") not in PERMITTED_SERVICE_TIERS:
        failures.append(f"effective service tier is {config.get('service_tier')!r}")
    if config.get("profile") is not None:
        failures.append(f"a configuration profile is active: {config.get('profile')!r}")
    if config.get("model_provider") not in (None, "openai"):
        failures.append(f"model provider is {config.get('model_provider')!r}")
    if config.get("openai_base_url") is not None:
        failures.append("a custom OpenAI base URL is configured")
    if config.get("web_search") != "disabled":
        failures.append("web search is not disabled")
    if config.get("notify"):
        failures.append("a notify program is configured")
    if config.get("approval_policy") != "never":
        failures.append(f"effective approval policy is {config.get('approval_policy')!r}")
    if config.get("approvals_reviewer") != "user":
        failures.append(f"effective approvals reviewer is {config.get('approvals_reviewer')!r}")
    for name, server in (config.get("mcp_servers") or {}).items():
        if not isinstance(server, dict) or server.get("enabled") is not False:
            failures.append(f"MCP server {name!r} is not disabled")
    for name, server in (config.get("mcp_servers") or {}).items():
        if isinstance(server, dict) and server.get("command") not in (None, "/usr/bin/false"):
            failures.append(f"MCP server {name!r} keeps a runnable command")
    allowed_project = str(launch_root / ".codex")
    for layer in (config_read or {}).get("layers") or []:
        name = (layer or {}).get("name") or {}
        kind = str(name.get("type", ""))
        if kind not in ("sessionFlags", "project", "user", "system"):
            failures.append(f"unexpected configuration layer {json.dumps(name)[:200]}")
        if kind == "project" and name.get("dotCodexFolder") != allowed_project:
            failures.append(f"a foreign project configuration layer applies: {json.dumps(name)[:200]}")
    for entry in (skills or {}).get("data") or []:
        for skill in (entry or {}).get("skills") or []:
            if not skill.get("enabled"):
                continue
            if skill.get("pluginId"):
                failures.append(f"plugin skill {skill.get('name')!r} is still enabled")
            elif skill.get("scope") != "repo" or not _under(skill.get("path"), launch_root):
                failures.append(
                    f"skill {skill.get('name')!r} ({skill.get('scope')}) is enabled outside the launch root"
                )
    enabled = {item.get("name") for item in features if item.get("enabled")}
    for feature in DISABLED_FEATURES:
        if feature in enabled:
            failures.append(f"feature {feature!r} is still enabled")
    if not features:
        failures.append("feature list is empty; isolation cannot be shown")
    return failures


def _under(path: Any, root: Path) -> bool:
    return isinstance(path, str) and Path(path).is_absolute() and (
        Path(path) == root or root in Path(path).parents
    )


def foreign_skill_paths(skills: Optional[dict], launch_root: Path) -> tuple[str, ...]:
    """Paths of enabled non-plugin skills that do not come from the launch root."""
    paths = []
    for entry in (skills or {}).get("data") or []:
        for skill in (entry or {}).get("skills") or []:
            path = skill.get("path")
            if skill.get("pluginId") or not isinstance(path, str):
                continue
            if skill.get("scope") == "repo" and _under(path, launch_root):
                continue
            if skill.get("scope") == "system":
                continue  # removed by skills.bundled.enabled=false
            paths.append(path)
    return tuple(sorted(set(paths)))


# ---------------------------------------------------------------------------


@dataclass
class TurnOutcome:
    thread_id: str
    turn_id: Optional[str]
    status: Optional[str] = None
    final_message: Optional[str] = None
    token_usage: Optional[dict] = None
    errors: list[str] = field(default_factory=list)
    guard_failures: list[str] = field(default_factory=list)
    attributed_snapshots: int = 0
    snapshots: int = 0
    timed_out: bool = False
    interrupted_by_guard: bool = False


Attribution = Callable[[dict], "tuple[bool, str]"]


class GuardedSession:
    """One pinned thread with a live attribution and isolation guard."""

    def __init__(self, process: AppServerProcess, pinned_model: str, attribution: Attribution,
                 cwd: Path, sandbox: str, effort: str,
                 writable_roots: tuple[Path, ...] = (),
                 developer_instructions: Optional[str] = None) -> None:
        if sandbox not in PERMITTED_SANDBOXES:
            raise PinViolation(f"sandbox {sandbox!r} is not permitted")
        if effort not in PERMITTED_EFFORTS:
            raise PinViolation(f"effort {effort!r} is not permitted")
        self.process = process
        self.pinned_model = pinned_model
        self.attribution = attribution
        self.cwd = cwd
        self.sandbox = sandbox
        self.effort = effort
        self.writable_roots = tuple(writable_roots)
        if sandbox == "workspace-write" and not self.writable_roots:
            raise PinViolation("write mode needs an explicit writable root")
        if sandbox == "read-only" and self.writable_roots:
            raise PinViolation("read-only mode takes no writable root")
        self.developer_instructions = developer_instructions
        self.thread_id: Optional[str] = None
        self.rollout_path: Optional[str] = None
        self.active_turn: Optional[str] = None
        self.guard_failures: list[str] = []
        self.instruction_sources: Optional[list] = None
        self._outcome: Optional[TurnOutcome] = None

    # -- request policy -------------------------------------------------
    _ALLOWED_KEYS = {
        "thread/start": {"model", "cwd", "sandbox", "approvalPolicy", "approvalsReviewer",
                         "serviceTier", "ephemeral", "developerInstructions", "config",
                         "allowProviderModelFallback"},
        "thread/resume": {"threadId", "model", "cwd", "sandbox", "approvalPolicy",
                          "approvalsReviewer", "serviceTier", "excludeTurns", "config",
                          "developerInstructions"},
        "turn/start": {"threadId", "input", "model", "effort", "serviceTier", "cwd",
                       "approvalPolicy", "approvalsReviewer", "clientUserMessageId", "outputSchema"},
        "turn/steer": {"threadId", "expectedTurnId", "input", "clientUserMessageId"},
        "turn/interrupt": {"threadId", "turnId"},
    }

    def check_params(self, method: str, params: dict) -> None:
        allowed = self._ALLOWED_KEYS.get(method)
        if allowed is None:
            if method in READ_ONLY_METHODS:
                return
            raise PinViolation(f"method {method} is not permitted in a guarded session")
        unexpected = sorted(set(params) - allowed)
        if unexpected:
            raise PinViolation(f"{method} carries non-permitted parameters: {unexpected}")
        if "model" in params and params["model"] != self.pinned_model:
            raise PinViolation(f"{method} model {params['model']!r} is not {self.pinned_model}")
        if method in ("thread/start", "thread/resume", "turn/start") and params.get("model") != self.pinned_model:
            raise PinViolation(f"{method} must pin model {self.pinned_model}")
        if method in ("thread/start", "thread/resume", "turn/start"):
            if params.get("approvalsReviewer") != "user":
                raise PinViolation(f"{method} must pin approvals reviewer user, never auto_review")
            if params.get("approvalPolicy") != "never":
                raise PinViolation(f"{method} must pin approval policy never")
        if method == "thread/start" and params.get("allowProviderModelFallback") is not False:
            raise PinViolation("thread/start must refuse provider model fallback")
        if params.get("serviceTier") not in PERMITTED_SERVICE_TIERS:
            raise PinViolation(f"{method} service tier {params.get('serviceTier')!r} is not permitted")
        if "effort" in params and params["effort"] not in PERMITTED_EFFORTS:
            raise PinViolation(f"{method} effort {params['effort']!r} is not permitted")
        if "sandbox" in params and (params["sandbox"] != self.sandbox or self.sandbox != "read-only"):
            raise PinViolation(f"{method} sandbox {params['sandbox']!r} differs from the session")
        if "cwd" in params and str(params["cwd"]) != str(self.cwd):
            raise PinViolation(f"{method} cwd differs from the session")
        if "approvalPolicy" in params and params["approvalPolicy"] != "never":
            raise PinViolation(f"{method} approval policy must be never")
        if "approvalsReviewer" in params and params["approvalsReviewer"] != "user":
            raise PinViolation(f"{method} approvals reviewer must be user")
        if params.get("ephemeral"):
            raise PinViolation("an ephemeral thread leaves no rollout to audit")
        if "config" in params and params["config"] != self.thread_config():
            raise PinViolation(f"{method} carries a configuration other than the session's permission profile")
        if "developerInstructions" in params and params["developerInstructions"] != self.developer_instructions:
            raise PinViolation(f"{method} replaces the session's developer instructions")

    def request(self, method: str, params: dict, timeout: float = 60.0) -> Any:
        self.check_params(method, params)
        return self.process.request(method, params, timeout)

    def thread_config(self) -> Optional[dict]:
        """Write mode: a read-only profile plus exactly the writable roots."""
        if self.sandbox != "workspace-write":
            return None
        return {
            "default_permissions": WRITE_PROFILE,
            "permissions": {WRITE_PROFILE: {
                "extends": ":read-only",
                "filesystem": {str(root): "write" for root in self.writable_roots},
                "network": {"enabled": False},
            }},
        }

    def thread_parameters(self) -> dict:
        params: dict = {
            "model": self.pinned_model,
            "cwd": str(self.cwd),
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "serviceTier": "default",
            "ephemeral": False,
            "allowProviderModelFallback": False,
        }
        if self.sandbox == "workspace-write":
            params["config"] = self.thread_config()
        else:
            params["sandbox"] = self.sandbox
        if self.developer_instructions is not None:
            params["developerInstructions"] = self.developer_instructions
        return params

    def check_thread_response(self, result: dict) -> list[str]:
        failures = []
        if (result or {}).get("model") != self.pinned_model:
            failures.append(f"thread model is {(result or {}).get('model')!r}")
        if (result or {}).get("serviceTier") not in PERMITTED_SERVICE_TIERS:
            failures.append(f"thread service tier is {(result or {}).get('serviceTier')!r}")
        if (result or {}).get("modelProvider") not in (None, "openai"):
            failures.append(f"thread model provider is {(result or {}).get('modelProvider')!r}")
        profile = (result or {}).get("activePermissionProfile")
        if self.sandbox == "read-only" and profile is not None:
            failures.append("a permission profile overrides the read-only sandbox")
        if self.sandbox == "workspace-write" and (profile or {}).get("id") != WRITE_PROFILE:
            failures.append(f"write permission profile is not active: {profile!r}")
        if (result or {}).get("approvalPolicy") != "never":
            failures.append(f"approval policy is {(result or {}).get('approvalPolicy')!r}")
        if (result or {}).get("approvalsReviewer") != "user":
            failures.append(f"approvals reviewer is {(result or {}).get('approvalsReviewer')!r}")
        sandbox = (result or {}).get("sandbox") or {}
        expected = {"read-only": "readOnly", "workspace-write": "workspaceWrite"}[self.sandbox]
        if sandbox.get("type") != expected:
            failures.append(f"sandbox is {sandbox.get('type')!r}, expected {expected}")
        if sandbox.get("networkAccess"):
            failures.append("sandbox grants network access")
        allowed = {str(root) for root in self.writable_roots}
        for root in sandbox.get("writableRoots") or []:
            if str(root) not in allowed:
                failures.append(f"sandbox writable root is not the target: {root}")
        if self.sandbox == "workspace-write" and not (sandbox.get("excludeSlashTmp") and sandbox.get("excludeTmpdirEnvVar")):
            failures.append("write sandbox still allows temporary directories")
        thread = (result or {}).get("thread") or {}
        if thread.get("ephemeral"):
            failures.append("thread is ephemeral")
        if not thread.get("path"):
            failures.append("thread has no rollout path")
        return failures

    # -- lifecycle ------------------------------------------------------
    def start_thread(self, settle_seconds: float = 2.0) -> dict:
        result = self.request("thread/start", self.thread_parameters())
        thread = (result or {}).get("thread") or {}
        self.instruction_sources = (result or {}).get("instructionSources")
        self.thread_id = thread.get("id")
        self.rollout_path = thread.get("path")
        failures = self.check_thread_response(result)
        if not self.thread_id:
            failures.append("thread/start returned no thread id")
        self.guard_failures.extend(failures)
        deadline = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            message = self.process.next_notification(deadline - time.monotonic())
            if message is not None:
                self.observe(message)
        return result

    def turn_start(self, text: str) -> str:
        if self.guard_failures:
            raise PinViolation("guard failures are recorded; no further turns may start")
        result = self.request("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": text}],
            "model": self.pinned_model,
            "effort": self.effort,
            "serviceTier": "default",
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
        })
        self.active_turn = ((result or {}).get("turn") or {}).get("id")
        return self.active_turn

    def turn_steer(self, text: str) -> Any:
        return self.request("turn/steer", {
            "threadId": self.thread_id, "expectedTurnId": self.active_turn,
            "input": [{"type": "text", "text": text}],
        })

    def turn_interrupt(self) -> Any:
        if not self.active_turn:
            return None
        return self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.active_turn})

    # -- live guard ------------------------------------------------------
    def observe(self, message: dict) -> list[str]:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        failures: list[str] = []
        outcome = self._outcome
        if method == "account/rateLimits/updated":
            snapshot = params.get("rateLimits") or {}
            ok, reason = self.attribution(snapshot)
            if outcome is not None:
                outcome.snapshots += 1
                outcome.attributed_snapshots += 1 if ok else 0
            if not ok:
                failures.append(f"live rate-limit update not attributed to the reserve: {reason}")
        elif method == "model/rerouted":
            failures.append(f"model rerouted from {params.get('fromModel')!r} to {params.get('toModel')!r}")
        elif method == "mcpServer/startupStatus/updated":
            failures.append(f"MCP server {params.get('name')!r} started despite isolation")
        elif method == "thread/settings/updated":
            settings = params.get("threadSettings") or {}
            if "model" in settings and settings.get("model") != self.pinned_model:
                failures.append(f"thread settings changed model to {settings.get('model')!r}")
            if settings.get("serviceTier") not in PERMITTED_SERVICE_TIERS:
                failures.append(f"thread settings changed service tier to {settings.get('serviceTier')!r}")
            if "approvalsReviewer" in settings and settings.get("approvalsReviewer") != "user":
                failures.append(f"thread settings changed approvals reviewer to {settings.get('approvalsReviewer')!r}")
        elif method == "remoteControl/status/changed":
            if params.get("status") != "disabled":
                failures.append(f"remote control is {params.get('status')!r}; another client could drive the thread")
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            kind = item.get("type")
            if kind in ("collabAgentToolCall", "subAgentActivity"):
                failures.append(f"sub-agent activity ({kind}) despite isolation")
            elif kind in ("mcpToolCall", "dynamicToolCall", "imageGeneration"):
                failures.append(f"isolated tool surface used ({kind})")
            if outcome is not None and method == "item/completed" and kind == "agentMessage":
                if item.get("phase") in (None, "final_answer"):
                    outcome.final_message = item.get("text")
        elif method == "warning" and "reroute" in json.dumps(params).lower():
            failures.append("reroute warning")
        elif method == "thread/tokenUsage/updated" and outcome is not None:
            outcome.token_usage = (params.get("tokenUsage") or {}).get("total")
        elif method == "error" and outcome is not None:
            outcome.errors.append(json.dumps(params)[:400])
        elif method == "turn/completed" and outcome is not None:
            turn = params.get("turn") or {}
            if turn.get("id") == outcome.turn_id:
                outcome.status = turn.get("status")
                for item in turn.get("items") or []:
                    if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                        outcome.final_message = item.get("text")
                if turn.get("error"):
                    outcome.errors.append(json.dumps(turn.get("error"))[:400])
        if failures:
            self.guard_failures.extend(failures)
            if outcome is not None:
                outcome.guard_failures.extend(failures)
        return failures

    def run_turn(self, text: str, timeout_seconds: float) -> TurnOutcome:
        """Start one turn and follow it to completion under the live guard."""
        outcome = TurnOutcome(thread_id=str(self.thread_id), turn_id=None)
        self._outcome = outcome
        outcome.turn_id = self.turn_start(text)
        deadline = time.monotonic() + timeout_seconds
        interrupt_deadline: Optional[float] = None
        while outcome.status is None:
            now = time.monotonic()
            if interrupt_deadline is not None and now > interrupt_deadline:
                break
            if interrupt_deadline is None and now > deadline:
                outcome.timed_out = True
                interrupt_deadline = self._interrupt(now)
                continue
            limit = (interrupt_deadline or deadline) - now
            message = self.process.next_notification(min(limit, 1.0))
            if message is None:
                continue
            if message.get("method") == "creme/transport/closed":
                outcome.errors.append("codex app-server closed during the turn")
                break
            failures = self.observe(message)
            if failures and interrupt_deadline is None and outcome.status is None:
                outcome.interrupted_by_guard = True
                interrupt_deadline = self._interrupt(time.monotonic())
        self.active_turn = None
        self._outcome = None
        return outcome

    def _interrupt(self, now: float) -> float:
        try:
            self.turn_interrupt()
        except (AppServerError, PinViolation) as exc:
            self.guard_failures.append(f"interrupt failed: {exc}")
        return now + 30.0
