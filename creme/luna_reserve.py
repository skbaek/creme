"""Guarded Codex Luna reserve pseudo-subagent runs.

The capability has one invariant: a run is charged to the Codex *reserve*
bucket (model slug ``gpt-reserve``) and never to the regular bucket or paid
credits. Every run goes through ``codex app-server`` (see
``creme.codex_app_server``) with user plugins, MCP servers, sub-agents, and
fast tier disabled, the model pinned on every request, live attribution of
each rate-limit update, and a rollout audit afterwards. When the guard cannot
show reserve attribution the run is an attribution failure: the command says
so loudly, records a tripwire that refuses further runs, and the caller must
stop and tell the user.

Bucket state is read through the same server without model calls. The only
model string this module passes to Codex is ``RESERVE_MODEL``; it never
retries or falls back.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .adapters import get_adapter
from .codex_app_server import (
    AppServerError,
    AppServerProcess,
    GuardedSession,
    PERMITTED_EFFORTS,
    PinViolation,
    decline_server_requests,
    foreign_skill_paths,
    isolation_arguments,
    isolation_failures,
    read_all_features,
)


RESERVE_MODEL = "gpt-reserve"
BINARY_ENV = "CREME_LUNA_RESERVE_CODEX"
STATE_ENV = "CREME_LUNA_RESERVE_STATE"
PREAMBLE_RELATIVE = Path("templates/luna-reserve/preamble.md")
STATE_RELATIVE = Path(".creme/luna-reserve")
TRIPWIRE_NAME = "ATTRIBUTION_FAILURE"

EXIT_OK = 0
EXIT_PREFLIGHT_REFUSED = 10
EXIT_CODEX_FAILED = 11
EXIT_ATTRIBUTION_FAILED = 12

DEFAULT_EFFORT = "medium"
DEFAULT_MIN_REMAINING_PERCENT = 0.0
DEFAULT_JITTER_SECONDS = 300
DEFAULT_DISCRIMINATION_SECONDS = 3600
DEFAULT_TIMEOUT_SECONDS = 1800
MIN_RESET_LEAD_SECONDS = 900
MAX_BRIEF_BYTES = 64 * 1024
FINAL_MESSAGE_MAX_LINES = 60
PERMITTED_SERVICE_TIERS = (None, "default")

STOP_MESSAGE = (
    "ATTRIBUTION FAILURE: this run could not be shown to be charged to the "
    "Luna reserve bucket. Stop using Luna reserve and tell the user."
)

# Options that would change model, profile, configuration, provider, service
# tier, or sandbox. The run parser accepts them only to refuse them loudly.
FORBIDDEN_OVERRIDE_FLAGS = (
    "-m", "--model", "-p", "--profile", "-c", "--config", "--oss",
    "--local-provider", "--service-tier", "--fast", "--enable", "--disable",
    "--dangerously-bypass-approvals-and-sandbox", "--full-auto",
    "--approve-for-me", "--add-dir", "--sandbox", "-s",
)

# Environment that could redirect authentication or billing. It is removed
# from the child environment; CODEX_HOME is kept because auth lives there.
_SCRUBBED_ENV_PREFIXES = ("OPENAI_", "CODEX_")
_KEPT_ENV = ("CODEX_HOME",)


class LunaReserveError(RuntimeError):
    """A fail-closed condition with a user-facing reason."""


# ---------------------------------------------------------------------------
# Binary and state resolution


def resolve_binary(environ: Optional[dict] = None, adapter: Any = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(BINARY_ENV)
    if override:
        path = Path(override).expanduser()
    else:
        found = (adapter or get_adapter()).codex_binary()
        if found.status != "OK":
            raise LunaReserveError(f"{found.detail}; set {BINARY_ENV} to a reviewed codex path")
        path = Path(found.data["path"])
    if not path.is_file() or not os.access(path, os.X_OK):
        raise LunaReserveError(f"Codex binary missing or not executable: {path}")
    return path


def state_root(module_root: Path, environ: Optional[dict] = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(STATE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    from .semaphore import canonical_creme_root

    return canonical_creme_root(module_root) / STATE_RELATIVE


def child_environment(environ: Optional[dict] = None) -> tuple[dict, list[str]]:
    environ = dict(os.environ if environ is None else environ)
    removed = sorted(
        key for key in environ
        if key.startswith(_SCRUBBED_ENV_PREFIXES) and key not in _KEPT_ENV
    )
    for key in removed:
        environ.pop(key)
    return environ, removed


# ---------------------------------------------------------------------------
# Zero-token reads


def zero_token_read(process: AppServerProcess, initialized: dict, cwd: Optional[Path] = None) -> dict:
    """Account, catalogue, bucket table, and effective config; no model call."""
    account = process.request("account/read", {"refreshToken": False})
    models = process.request("model/list", {"includeHidden": True})
    limits = process.request("account/rateLimits/read", None)
    config = process.request("config/read", {"includeLayers": True, "cwd": str(cwd) if cwd else None})
    if isinstance(limits, dict):
        limits.pop("accountId", None)
    inner = (account or {}).get("account") if isinstance(account, dict) else None
    inner = inner if isinstance(inner, dict) else {}
    return {
        "codex_home": (initialized or {}).get("codexHome"),
        # The e-mail address is deliberately not retained.
        "account": {"type": inner.get("type"), "plan": inner.get("planType")},
        "models": models,
        "limits": limits,
        "config": config,
        "read_at": int(time.time()),
    }


def mcp_servers(read: dict) -> dict:
    config = ((read.get("config") or {}).get("config") or {})
    return dict(config.get("mcp_servers") or {})


def launch_root(module_root: Path) -> Path:
    """Pseudo-subagent threads start in the canonical Creme checkout."""
    from .semaphore import canonical_creme_root

    return canonical_creme_root(module_root)


def open_server(binary: Path, env: dict, effort: str, servers: dict,
                transcript: Any = None, stderr: Any = None,
                skill_paths: tuple[str, ...] = (), approval_policy: str = "never",
                server_request_handler: Any = None,
                kept_mcp_servers: Optional[dict] = None) -> tuple[AppServerProcess, dict]:
    process = AppServerProcess(
        binary, isolation_arguments(RESERVE_MODEL, effort, servers, skill_paths, approval_policy, kept_mcp_servers),
        env, transcript,
        server_request_handler=server_request_handler or decline_server_requests, stderr=stderr,
    )
    try:
        initialized = process.start("creme-luna-reserve")
    except AppServerError:
        process.close()
        raise
    return process, initialized


# ---------------------------------------------------------------------------
# Bucket classification


@dataclass
class Bucket:
    limit_id: str
    used_percent: float
    window_minutes: Optional[int]
    resets_at: Optional[int]
    has_secondary: bool
    credits: Any
    reached: Optional[str]
    spend_control_reached: Any
    model_slug: Optional[str] = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    def to_dict(self) -> dict:
        return {
            "limit_id": self.limit_id,
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "resets_at_utc": _utc(self.resets_at),
            "resets_at_local": _local(self.resets_at),
            "has_secondary": self.has_secondary,
            "credits": self.credits,
            "reached": self.reached,
            "spend_control_reached": self.spend_control_reached,
            "model_slug": self.model_slug,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bucket":
        return cls(
            limit_id=data["limit_id"],
            used_percent=float(data["used_percent"]),
            window_minutes=data.get("window_minutes"),
            resets_at=data.get("resets_at"),
            has_secondary=bool(data.get("has_secondary")),
            credits=data.get("credits"),
            reached=data.get("reached"),
            spend_control_reached=data.get("spend_control_reached"),
            model_slug=data.get("model_slug"),
        )


def _bucket(limit_id: str, snapshot: dict) -> Bucket:
    primary = snapshot.get("primary") or {}
    used = primary.get("usedPercent")
    return Bucket(
        limit_id=limit_id,
        # A missing or malformed usage figure counts as exhausted (fail closed).
        used_percent=float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else 100.0,
        window_minutes=primary.get("windowDurationMins"),
        resets_at=primary.get("resetsAt"),
        has_secondary=snapshot.get("secondary") is not None,
        credits=snapshot.get("credits"),
        reached=snapshot.get("rateLimitReachedType"),
        spend_control_reached=snapshot.get("spendControlReached"),
        model_slug=(snapshot.get("normalModelSlug")
                    if isinstance(snapshot.get("normalModelSlug"), str) and snapshot.get("normalModelSlug")
                    else None),
    )


def classify_buckets(limits: dict) -> tuple[Optional[Bucket], Optional[Bucket]]:
    """Return (reserve, regular). The reserve is found by its limit name."""
    by_id = (limits or {}).get("rateLimitsByLimitId") or {}
    reserve_ids = [
        key for key, snapshot in by_id.items()
        if isinstance(snapshot, dict) and snapshot.get("limitName") == RESERVE_MODEL
    ]
    if len(reserve_ids) > 1:
        raise LunaReserveError(f"more than one bucket is named {RESERVE_MODEL}: {reserve_ids}")
    reserve = _bucket(reserve_ids[0], by_id[reserve_ids[0]]) if reserve_ids else None
    top = (limits or {}).get("rateLimits") or {}
    regular_id = top.get("limitId") if top.get("limitName") != RESERVE_MODEL else None
    regular_id = regular_id or "codex"
    regular_snapshot = by_id.get(regular_id) or (top if top.get("limitId") == regular_id else None)
    regular = _bucket(regular_id, regular_snapshot) if regular_snapshot else None
    return reserve, regular


def _utc(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    return _dt.datetime.fromtimestamp(timestamp, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _local(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    return _dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


# ---------------------------------------------------------------------------
# Preflight


@dataclass
class Policy:
    min_remaining_percent: float = DEFAULT_MIN_REMAINING_PERCENT
    jitter_seconds: int = DEFAULT_JITTER_SECONDS
    discrimination_seconds: int = DEFAULT_DISCRIMINATION_SECONDS
    # Retired 2026-09-20 and kept only for compatibility: reading it changes nothing.
    # It used to lift the refusal below, which existed solely because no run had shown
    # that a reserve turn leaves an *available* regular bucket untouched. Run
    # 20260920T073150Z-cff27d showed it. The field stays so that call sites and broker
    # session records written before the retirement still load.
    allow_regular_available: bool = False


def admission(read: dict, policy: Policy, effort: Optional[str] = None,
              now: Optional[int] = None) -> dict:
    """Decide from one zero-token read whether a reserve run may start."""
    refusals: list[str] = []
    warnings: list[str] = []
    now = int(time.time()) if now is None else now
    if policy.discrimination_seconds <= 2 * policy.jitter_seconds:
        refusals.append("discrimination threshold must exceed twice the jitter tolerance")

    account = read.get("account") or {}
    if account.get("type") != "chatgpt":
        refusals.append(
            f"Codex account type is {account.get('type')!r}, not a ChatGPT login; "
            "API-key billing is never used"
        )

    catalogue = [
        entry for entry in ((read.get("models") or {}).get("data") or [])
        if isinstance(entry, dict) and (entry.get("model") == RESERVE_MODEL or entry.get("id") == RESERVE_MODEL)
    ]
    if not catalogue:
        refusals.append(f"{RESERVE_MODEL} is absent from the Codex model catalogue")
    elif catalogue[0].get("defaultServiceTier") not in PERMITTED_SERVICE_TIERS:
        refusals.append(
            f"{RESERVE_MODEL} defaults to service tier {catalogue[0].get('defaultServiceTier')!r}; "
            "only the default tier is permitted"
        )
    if catalogue and effort is not None:
        supported = [
            level.get("reasoningEffort") if isinstance(level, dict) else level
            for level in (catalogue[0].get("supportedReasoningEfforts") or [])
        ]
        if supported and effort not in supported:
            refusals.append(f"effort {effort!r} is not supported by {RESERVE_MODEL}: {supported}")

    limits = read.get("limits") or {}
    try:
        reserve, regular = classify_buckets(limits)
    except LunaReserveError as exc:
        reserve, regular = None, None
        refusals.append(str(exc))
    if reserve is None:
        refusals.append(f"no rate-limit bucket named {RESERVE_MODEL}")
    else:
        if reserve.reached or reserve.used_percent >= 100:
            refusals.append(f"reserve bucket {reserve.limit_id} is reached")
        if reserve.spend_control_reached:
            refusals.append("reserve bucket reports a spend control")
        if reserve.remaining_percent < policy.min_remaining_percent:
            refusals.append(
                f"reserve remaining {reserve.remaining_percent:g}% is below the "
                f"{policy.min_remaining_percent:g}% floor"
            )
        if reserve.resets_at is None or reserve.window_minutes is None:
            refusals.append("reserve bucket has no primary window to attribute against")
        elif reserve.resets_at - now < MIN_RESET_LEAD_SECONDS:
            refusals.append("reserve window resets within 15 minutes; wait for the reset")
    if regular is None:
        refusals.append("no regular Codex bucket to discriminate against")
    elif reserve is not None:
        if regular.resets_at is None or reserve.resets_at is None or abs(
            regular.resets_at - reserve.resets_at
        ) <= policy.discrimination_seconds:
            refusals.append(
                "reserve and regular reset times cannot be discriminated "
                f"(threshold {policy.discrimination_seconds}s)"
            )
        if reserve.window_minutes == regular.window_minutes and reserve.credits == regular.credits \
                and regular.resets_at == reserve.resets_at:
            refusals.append("reserve and regular buckets are indistinguishable")
    # An available regular bucket is reported, not refused. Until 2026-09-20 this was a
    # refusal (liftable with --allow-regular-available) because no run had shown that a
    # reserve turn leaves an available regular bucket untouched; run 20260920T073150Z-cff27d
    # showed it, with the regular bucket unmoved at 37.0% used across the turn. What the run
    # did not certify stays guarded elsewhere: every turn re-checks attribution against the
    # reserve window by resets_at, a regular or credit movement across the run is a failure
    # (regular_delta_failures), and a misattributed turn still exits ATTRIBUTION_FAILURE.
    regular_available = bool(limits.get("ordinaryUsageAllowed")) or (
        regular is not None and not regular.reached and regular.used_percent < 100
    )
    if regular is not None and isinstance(regular.credits, dict) and (
        regular.credits.get("hasCredits") or regular.credits.get("unlimited")
    ):
        warnings.append("the regular bucket has paid credits; a misattributed run could spend them")
    return {
        "admitted": not refusals,
        "refusals": refusals,
        "warnings": warnings,
        "reserve": reserve.to_dict() if reserve else None,
        "regular": regular.to_dict() if regular else None,
        "regular_available": regular_available,
        "ordinary_usage_allowed": limits.get("ordinaryUsageAllowed"),
        "banner": ((limits.get("rateLimitUpsell") or {}).get("banner_type")),
        "account": account,
        "policy": policy.__dict__,
    }


def target_refusals(target: Path, write: bool, codex_home: Optional[str], root: Path,
                    home: Optional[Path] = None) -> list[str]:
    """Refuse a target directory that is too broad or protected."""
    refusals: list[str] = []
    if not target.is_dir():
        return [f"target directory does not exist: {target}"]
    home = (home or Path.home()).resolve()
    codex_home_path = Path(codex_home).resolve() if codex_home else home / ".codex"
    if target == Path(target.anchor) or target == home or target in home.parents:
        refusals.append(f"target directory is too broad: {target}")
    if target == codex_home_path or codex_home_path in target.parents:
        refusals.append("target directory is inside CODEX_HOME")
    if write and (target == root or target in root.parents):
        refusals.append("write mode refuses the Creme launch checkout itself or an ancestor; use a worktree")
    if write and "master" in target.parts:
        refusals.append("write mode refuses a target under a 'master' directory")
    return refusals


# ---------------------------------------------------------------------------
# Rollout audit


def _within(value: Any, target: Optional[int], tolerance: int) -> bool:
    return isinstance(value, (int, float)) and target is not None and abs(value - target) <= tolerance


def snapshot_matches_reserve(rate_limits: Any, reserve: Bucket, regular: Bucket,
                             jitter: int) -> tuple[bool, str]:
    """Attribute one token snapshot by window shape, reset time, and credits."""
    if not isinstance(rate_limits, dict):
        return False, "snapshot has no rate_limits"
    primary = rate_limits.get("primary")
    if not isinstance(primary, dict):
        return False, "snapshot has no primary window"
    # Rollout records use snake_case; live app-server notifications camelCase.
    resets_at = primary.get("resets_at", primary.get("resetsAt"))
    window = primary.get("window_minutes", primary.get("windowDurationMins"))
    if _within(resets_at, regular.resets_at, jitter):
        return False, f"resets_at {resets_at} matches the regular bucket ({regular.resets_at})"
    if not _within(resets_at, reserve.resets_at, jitter):
        return False, f"resets_at {resets_at} does not match the reserve bucket ({reserve.resets_at})"
    if window != reserve.window_minutes:
        return False, f"window {window} differs from reserve {reserve.window_minutes}"
    if (rate_limits.get("secondary") is not None) != reserve.has_secondary:
        return False, "secondary window shape differs from the reserve bucket"
    credits = rate_limits.get("credits")
    if (credits is None) != (reserve.credits is None):
        return False, f"credits shape {credits!r} differs from the reserve bucket"
    return True, "reserve"


def model_pinned(model: Any) -> bool:
    return model == RESERVE_MODEL


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def audit_records(records: list[dict], reserve: Bucket, regular: Bucket,
                  jitter: int = DEFAULT_JITTER_SECONDS) -> dict:
    failures: list[str] = []
    thread_ids: set[str] = set()
    turn_models: list[Any] = []
    snapshots = attributed = 0
    limit_ids: dict[str, int] = {}
    usage: Optional[dict] = None
    if not records:
        failures.append("rollout is empty")
    for index, record in enumerate(records):
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        payload_type = payload.get("type")
        if kind == "session_meta" and payload.get("id"):
            thread_ids.add(str(payload["id"]))
        if kind == "turn_context":
            turn_models.append(payload.get("model"))
            if not model_pinned(payload.get("model")):
                failures.append(f"record {index}: turn model {payload.get('model')!r} is not {RESERVE_MODEL}")
            if payload.get("service_tier") not in PERMITTED_SERVICE_TIERS:
                failures.append(f"record {index}: service tier {payload.get('service_tier')!r}")
        if payload_type == "thread_settings_applied":
            settings = payload.get("thread_settings") or {}
            if "model" in settings and not model_pinned(settings.get("model")):
                failures.append(f"record {index}: thread model {settings.get('model')!r} is not {RESERVE_MODEL}")
            if settings.get("service_tier") not in PERMITTED_SERVICE_TIERS:
                failures.append(f"record {index}: service tier {settings.get('service_tier')!r}")
        if payload_type in ("model_reroute", "model_rerouted") or "from_model" in set(_walk_keys(payload)):
            failures.append(f"record {index}: model reroute record")
        text = json.dumps(payload) if payload_type in ("warning", "error", "stream_error") else ""
        if "model rerouted" in text.lower():
            failures.append(f"record {index}: model reroute warning")
        if payload_type == "token_count":
            snapshots += 1
            rate_limits = payload.get("rate_limits")
            if isinstance(rate_limits, dict):
                label = str(rate_limits.get("limit_id"))
                limit_ids[label] = limit_ids.get(label, 0) + 1
            ok, reason = snapshot_matches_reserve(rate_limits, reserve, regular, jitter)
            if ok:
                attributed += 1
            else:
                failures.append(f"record {index}: {reason}")
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                usage = info["total_token_usage"]
    if records and not turn_models:
        failures.append("rollout has no turn_context record")
    if records and snapshots == 0:
        failures.append("rollout has no token snapshot to attribute")
    return {
        "verdict": "PASS" if not failures else "FAIL",
        "failures": failures,
        "thread_ids": sorted(thread_ids),
        "turn_models": sorted({str(model) for model in turn_models}),
        "turns": len(turn_models),
        "token_snapshots": snapshots,
        "attributed_snapshots": attributed,
        "snapshot_limit_id_labels": limit_ids,
        "token_usage": usage,
        "reference": {"reserve": reserve.to_dict(), "regular": regular.to_dict(), "jitter_seconds": jitter},
    }


def load_rollout(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise LunaReserveError(f"{path}:{number}: malformed rollout line")
            if isinstance(record, dict):
                records.append(record)
    return records


_THREAD_ID = re.compile(r"^[0-9a-f][0-9a-f-]{15,}$")


def find_rollout(codex_home: Path, thread_id: str) -> Optional[Path]:
    if not _THREAD_ID.match(thread_id):
        raise LunaReserveError(f"not a thread id: {thread_id!r}")
    matches = sorted((codex_home / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    return matches[-1] if matches else None


def regular_delta_failures(before: dict, after: dict) -> list[str]:
    """Compare two bucket reads; any regular or credit movement is a failure."""
    failures = []
    regular_before, regular_after = before.get("regular") or {}, after.get("regular") or {}
    if not regular_after:
        failures.append("post-run bucket read has no regular bucket")
        return failures
    if regular_before.get("resets_at") == regular_after.get("resets_at") and float(
        regular_after.get("used_percent", 0)
    ) > float(regular_before.get("used_percent", 0)):
        failures.append(
            f"regular bucket usage rose from {regular_before.get('used_percent')}% "
            f"to {regular_after.get('used_percent')}%"
        )

    def balance(bucket: dict) -> Optional[float]:
        credits = bucket.get("credits")
        try:
            return float(credits.get("balance")) if isinstance(credits, dict) else None
        except (TypeError, ValueError):
            return None

    if balance(regular_before) is not None and balance(regular_after) is not None and \
            balance(regular_after) < balance(regular_before):
        failures.append("regular credit balance decreased")
    return failures


# ---------------------------------------------------------------------------
# Run


@dataclass
class RunRequest:
    brief: str
    workdir: Path
    effort: str = DEFAULT_EFFORT
    write: bool = False
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    policy: Policy = field(default_factory=Policy)
    overrides: list[str] = field(default_factory=list)
    preflight_only: bool = False


def tripwire_path(state: Path) -> Path:
    return state / TRIPWIRE_NAME


def developer_instructions(module_root: Path, request: RunRequest, root: Path) -> str:
    template = (module_root / PREAMBLE_RELATIVE).read_text(encoding="utf-8")
    return (
        template.replace("{launch_root}", str(root))
        .replace("{target}", str(request.workdir))
        .replace("{mode}", "write" if request.write else "read-only")
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def early_refusals(state: Path, effort: str, overrides: Iterable[str] = (),
                   brief: Optional[str] = None) -> list[str]:
    """Refusals that need no Codex process: overrides, tripwire, effort, brief."""
    overrides = list(overrides)
    if overrides:
        return [f"override refused: {', '.join(overrides)}; the model is pinned to {RESERVE_MODEL}"]
    if tripwire_path(state).exists():
        return [
            f"an earlier attribution failure is recorded at {tripwire_path(state)}; "
            "only the user may clear it after reviewing that run"
        ]
    if effort not in PERMITTED_EFFORTS:
        return [f"effort must be one of {', '.join(PERMITTED_EFFORTS)}"]
    if brief is not None:
        if not brief.strip():
            return ["the brief is empty"]
        if len(brief.encode("utf-8")) > MAX_BRIEF_BYTES:
            return [f"the brief exceeds {MAX_BRIEF_BYTES} bytes"]
    return []


def approval_policy_for(write: bool, broker: bool) -> str:
    """Read-only work never asks; a broker write session routes approvals to the master."""
    return "on-request" if (write and broker) else "never"


def attribution_for(decision: dict, jitter: int):
    """The live attribution predicate for one admission read."""
    reserve = Bucket.from_dict(decision["reserve"])
    regular = Bucket.from_dict(decision["regular"])

    def attribution(snapshot: dict) -> tuple[bool, str]:
        return snapshot_matches_reserve(snapshot, reserve, regular, jitter)

    return attribution


class ReserveServer:
    """One isolated, admitted ``codex app-server`` with its preflight proof.

    ``prepare`` runs the probe server, admission, target refusals, and the
    isolation proof, and records ``preflight.json`` in ``run_dir``. The
    one-shot ``run`` and every broker session start and resume threads only
    through a prepared server.
    """

    def __init__(self, module_root: Path, root: Path, run_dir: Path, workdir: Path, write: bool,
                 effort: str, policy: Policy, environ: dict, approval_policy: str = "never",
                 lean: Any = None) -> None:
        self.module_root = module_root
        self.root = root
        self.run_dir = run_dir
        self.workdir = workdir
        self.write = write
        self.effort = effort
        self.policy = policy
        self.environ = environ
        self.approval_policy = approval_policy
        self.process: Optional[AppServerProcess] = None
        self.before_read: dict = {}
        self.before: dict = {}
        self.first: dict = {}
        self.early: dict = {}
        self.servers: dict = {}
        self.skill_paths: tuple[str, ...] = ()
        self.scrubbed: list[str] = []
        self.transcript: Any = None
        self.stderr: Any = None
        self.codex_home: Optional[Path] = None
        # A ``luna_lean.LeanMode`` keeps the tracked lean-lsp-mcp server running;
        # ``None`` is the unchanged non-Lean isolation.
        self.lean = lean
        if lean is not None and not (write and approval_policy == "on-request"):
            raise PinViolation("Lean mode needs a brokered write session")

    def kept_mcp_servers(self) -> Optional[dict]:
        if self.lean is None:
            return None
        from .luna_lean import LEAN_MCP_SERVER, launch_override

        return {LEAN_MCP_SERVER: launch_override(self.lean.definition)}

    # -- stage 1: probe (no run directory yet) --------------------------
    def probe(self) -> list[str]:
        try:
            binary = resolve_binary(self.environ)
        except LunaReserveError as exc:
            return [str(exc)]
        self.binary = binary
        self.env, self.scrubbed = child_environment(self.environ)
        if not self.workdir.is_dir():
            return [f"target directory does not exist: {self.workdir}"]
        # A first isolated server names the user's MCP servers and foreign
        # skills so the run server can disable each one; it also gives an
        # early admission verdict.
        try:
            probe, initialized = open_server(binary, self.env, self.effort, {},
                                             approval_policy=self.approval_policy)
            try:
                self.first = zero_token_read(probe, initialized, self.root)
                first_skills = probe.request("skills/list", {"cwds": [str(self.root)], "forceReload": True})
            finally:
                probe.close()
        except (AppServerError, PinViolation) as exc:
            return [str(exc)]
        self.early = admission(self.first, self.policy, self.effort)
        refusals = self.early["refusals"] + target_refusals(
            self.workdir, self.write, self.first.get("codex_home"), self.root,
        )
        self.servers = mcp_servers(self.first)
        self.skill_paths = foreign_skill_paths(first_skills, self.root)
        if self.lean is not None:
            from . import luna_lean

            if luna_lean.LEAN_MCP_SERVER not in self.servers:
                refusals.append(f"the launch root defines no {luna_lean.LEAN_MCP_SERVER} server for Lean mode")
            try:
                self.lean.definition = luna_lean.launch_definition(self.lean.tracked(self.root))
            except luna_lean.LeanModeError as exc:
                refusals.append(str(exc))
                return refusals
        try:
            # Validate the isolation arguments before anything is recorded, so
            # an unrepresentable server name or skill path is a refusal.
            isolation_arguments(RESERVE_MODEL, self.effort, self.servers, self.skill_paths, self.approval_policy,
                                self.kept_mcp_servers())
        except PinViolation as exc:
            refusals.append(str(exc))
        self.codex_home = Path(self.first.get("codex_home") or Path.home() / ".codex")
        return refusals

    # -- stage 2: the run server and its isolation proof ----------------
    def open(self, server_request_handler: Any = None) -> list[str]:
        """Launch the run server in ``run_dir``; raises AppServerError/PinViolation."""
        self.transcript = (self.run_dir / "transcript.jsonl").open("a", encoding="utf-8")
        self.stderr = (self.run_dir / "app-server.stderr").open("a", encoding="utf-8")
        self.process, initialized = open_server(
            self.binary, self.env, self.effort, self.servers, self.transcript, self.stderr, self.skill_paths,
            approval_policy=self.approval_policy, server_request_handler=server_request_handler,
            kept_mcp_servers=self.kept_mcp_servers(),
        )
        process = self.process
        self.before_read = zero_token_read(process, initialized, self.root)
        features = read_all_features(process)
        skills = process.request("skills/list", {"cwds": [str(self.root)], "forceReload": True})
        self.before = admission(self.before_read, self.policy, self.effort)
        kept = tuple(self.kept_mcp_servers() or ())
        isolation = isolation_failures(self.before_read["config"], features, RESERVE_MODEL, self.root, skills,
                                       self.approval_policy, kept)
        lean_record = None
        if self.lean is not None:
            from . import luna_lean

            isolation += luna_lean.definition_failures(self.before_read["config"], self.lean.definition)
            lean_record = {**self.lean.summary(), "definition": self.lean.definition}
        config = self.before_read["config"] or {}
        _write_json(self.run_dir / "preflight.json", {
            "read": {key: value for key, value in self.before_read.items() if key != "config"},
            "admission": self.before,
            "isolation": {
                "arguments": isolation_arguments(RESERVE_MODEL, self.effort, self.servers, self.skill_paths,
                                                 self.approval_policy, self.kept_mcp_servers()),
                "failures": isolation,
                "enabled_features": sorted(item.get("name") for item in features if item.get("enabled")),
                "config_layers": [layer.get("name") for layer in config.get("layers") or []],
                "mcp_servers": {
                    name: {"enabled": (server or {}).get("enabled"), "command": (server or {}).get("command")}
                    for name, server in ((config.get("config") or {}).get("mcp_servers") or {}).items()
                },
                "skills": [
                    {"name": skill.get("name"), "scope": skill.get("scope"), "enabled": skill.get("enabled"),
                     "plugin": skill.get("pluginId"), "path": skill.get("path")}
                    for entry in (skills or {}).get("data") or [] for skill in entry.get("skills") or []
                ],
            },
            **({"lean": lean_record} if lean_record is not None else {}),
        })
        return self.before["refusals"] + isolation + target_refusals(
            self.workdir, self.write, self.before_read.get("codex_home"), self.root,
        )

    def session(self, developer_instructions_text: str) -> GuardedSession:
        lean_servers: tuple[str, ...] = ()
        forbidden: tuple[str, ...] = ()
        if self.lean is not None:
            from . import luna_lean

            lean_servers = (luna_lean.LEAN_MCP_SERVER,)
            forbidden = luna_lean.OPEN_WORLD_TOOLS + luna_lean.FORBIDDEN_TOOLS
        return GuardedSession(
            self.process, RESERVE_MODEL, attribution_for(self.before, self.policy.jitter_seconds), self.root,
            "workspace-write" if self.write else "read-only", self.effort,
            writable_roots=(self.workdir,) if self.write else (),
            developer_instructions=developer_instructions_text,
            approval_policy=self.approval_policy,
            mcp_servers=lean_servers, forbidden_mcp_tools=forbidden,
        )

    def admission_read(self) -> dict:
        """A fresh zero-token read on this server, judged by the same admission."""
        read = zero_token_read(self.process, {"codexHome": self.before_read.get("codex_home")}, self.root)
        return admission(read, self.policy, self.effort)

    def postflight(self, reference: dict) -> tuple[dict, dict, list[str]]:
        """Bucket read after work; any regular or credit movement is a failure."""
        limits = self.process.request("account/rateLimits/read", None)
        after = admission({**self.before_read, "limits": limits}, self.policy)
        return limits, after, regular_delta_failures(reference, after)

    def close(self) -> None:
        if self.process is not None:
            self.process.close()
        for handle in (self.transcript, self.stderr):
            if handle is not None and not handle.closed:
                handle.close()


def rollout_records(path: Optional[Path], wait_seconds: float) -> Optional[list[dict]]:
    """Load a rollout, waiting briefly for it to appear and for a partial line to finish."""
    if path is None:
        return None
    deadline = time.monotonic() + wait_seconds
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.25)
    if not path.is_file():
        return None
    for attempt in range(3):
        try:
            return load_rollout(path)
        except LunaReserveError:
            if attempt == 2:
                raise
            time.sleep(0.5)
    return None


def audit_turn(records: Optional[list[dict]], start: int, reference: dict, jitter: int,
               turn_status: Optional[str], token_usage: Any, thread_id: Optional[str]) -> tuple[Optional[dict], list[str]]:
    """Audit the rollout records a turn produced (from ``start``) against its reference read."""
    if records is None:
        return None, ["no rollout found for the turn"]
    audit = audit_records(
        records[start:], Bucket.from_dict(reference["reserve"]), Bucket.from_dict(reference["regular"]), jitter,
    )
    if token_usage is None and turn_status != "completed":
        audit["failures"] = [
            failure for failure in audit["failures"] if failure != "rollout has no token snapshot to attribute"
        ]
        audit["verdict"] = "PASS" if not audit["failures"] else "FAIL"
    audit["rollout_start"] = start
    failures = list(audit["failures"])
    if audit["thread_ids"] and thread_id and thread_id not in audit["thread_ids"]:
        failures.append("rollout session id does not match the thread")
    return audit, failures


def record_tripwire(state: Path, run_id: str, thread_id: Optional[str], failures: list[str]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    path = tripwire_path(state)
    if path.exists():
        return
    path.write_text(json.dumps({
        "run_id": run_id, "thread_id": thread_id, "failures": failures,
    }, indent=2) + "\n", encoding="utf-8")


def run(module_root: Path, request: RunRequest, environ: Optional[dict] = None,
        rollout_wait_seconds: float = 10.0, settle_seconds: float = 2.0,
        root: Optional[Path] = None) -> tuple[int, dict]:
    environ = dict(os.environ if environ is None else environ)
    root = (root or launch_root(module_root)).resolve()
    state = state_root(module_root, environ)
    started = _dt.datetime.now(_dt.timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    run_dir = state / "runs" / run_id
    summary: dict[str, Any] = {
        "run_id": run_id,
        "mode": "write" if request.write else "read-only",
        "effort": request.effort,
        "target": str(request.workdir),
        "launch_root": str(root),
        "model": RESERVE_MODEL,
    }

    def refuse(reasons: list[str], extra: Optional[dict] = None) -> tuple[int, dict]:
        summary.update({"verdict": "REFUSED", "exit_code": EXIT_PREFLIGHT_REFUSED, "refusals": reasons})
        summary.update(extra or {})
        if run_dir.is_dir():
            summary["run_dir"] = str(run_dir)
            _write_json(run_dir / "verdict.json", summary)
        return EXIT_PREFLIGHT_REFUSED, summary

    refusals = early_refusals(state, request.effort, request.overrides, request.brief)
    if refusals:
        return refuse(refusals)
    request.workdir = request.workdir.expanduser().resolve()
    server = ReserveServer(module_root, root, run_dir, request.workdir, request.write, request.effort,
                           request.policy, environ, approval_policy_for(request.write, broker=False))
    refusals = server.probe()
    summary["scrubbed_environment"] = server.scrubbed
    summary["target"] = str(request.workdir)
    if refusals:
        return refuse(refusals, {"reserve_before": server.early.get("reserve")} if server.early else None)

    run_dir.mkdir(parents=True, exist_ok=False)
    summary["run_dir"] = str(run_dir)
    user_config = server.codex_home / "config.toml"
    config_digest_before = _digest(user_config)
    attribution_failures: list[str] = []
    codex_errors: list[str] = []
    outcome = None
    session: Optional[GuardedSession] = None
    after: Optional[dict] = None
    try:
        refusals = server.open()
        if refusals:
            server.close()
            return refuse(refusals, {"reserve_before": server.before.get("reserve")})
        if request.preflight_only:
            summary.update({"verdict": "PREFLIGHT_OK", "exit_code": EXIT_OK,
                            "reserve_before": server.before.get("reserve")})
            _write_json(run_dir / "verdict.json", summary)
            return EXIT_OK, summary
        instructions = developer_instructions(module_root, request, root)
        (run_dir / "developer-instructions.md").write_text(instructions, encoding="utf-8")
        (run_dir / "brief.md").write_text(request.brief, encoding="utf-8")
        session = server.session(instructions)
        session.start_thread(settle_seconds)
        summary["thread_id"] = session.thread_id
        summary["rollout"] = session.rollout_path
        summary["instruction_sources"] = session.instruction_sources
        if session.guard_failures:
            attribution_failures.extend(session.guard_failures)
        else:
            outcome = session.run_turn(request.brief.strip(), request.timeout_seconds)
            attribution_failures.extend(outcome.guard_failures)
            codex_errors.extend(outcome.errors)
        after_limits, after, delta = server.postflight(server.before)
        _write_json(run_dir / "postflight.json", {"limits": after_limits, "admission": after})
        attribution_failures.extend(delta)
        if session.thread_id:
            try:
                items = server.process.request("thread/items/list", {"threadId": session.thread_id, "limit": 200})
                _write_json(run_dir / "items.json", items)
            except AppServerError as exc:
                codex_errors.append(f"transcript read failed: {exc}")
    except PinViolation as exc:
        attribution_failures.append(f"pin violation: {exc}")
    except AppServerError as exc:
        codex_errors.append(str(exc))
        if outcome is None and session is not None and session.active_turn:
            attribution_failures.append("the server failed during a turn; attribution cannot be shown")
    finally:
        server.close()

    before = server.before
    audit: Optional[dict] = None
    if outcome is not None:
        rollout = Path(session.rollout_path) if session and session.rollout_path else None
        audit, failures = audit_turn(
            rollout_records(rollout, rollout_wait_seconds), 0, before, request.policy.jitter_seconds,
            outcome.status, outcome.token_usage, session.thread_id if session else None,
        )
        if audit is not None:
            _write_json(run_dir / "audit.json", audit)
        attribution_failures.extend(failures)

    warnings: list[str] = list(before.get("warnings") or [])
    if _digest(user_config) != config_digest_before:
        warnings.append(f"Codex modified the user configuration {user_config} during the run")
    last_message = run_dir / "last-message.md"
    if outcome is not None and outcome.final_message is not None:
        last_message.write_text(outcome.final_message.rstrip("\n") + "\n", encoding="utf-8")
        lines = outcome.final_message.splitlines()
        summary["last_message_lines"] = len(lines)
        summary["last_message_format_ok"] = bool(lines) and lines[0].startswith("STATUS:") and \
            len(lines) <= FINAL_MESSAGE_MAX_LINES
    process = server.process
    summary.update({
        "turn_status": outcome.status if outcome else None,
        "timed_out": outcome.timed_out if outcome else False,
        "interrupted_by_guard": outcome.interrupted_by_guard if outcome else False,
        "live_snapshots": f"{outcome.attributed_snapshots}/{outcome.snapshots}" if outcome else None,
        "token_usage": (outcome.token_usage if outcome else None) or (audit or {}).get("token_usage"),
        "codex_errors": codex_errors,
        "warnings": warnings,
        "server_requests": process.server_requests if process is not None else [],
        "last_message_path": str(last_message) if last_message.exists() else None,
        "reserve_before": before.get("reserve"),
        "reserve_after": (after or {}).get("reserve"),
    })

    if attribution_failures:
        summary.update({
            "verdict": "ATTRIBUTION_FAILED",
            "exit_code": EXIT_ATTRIBUTION_FAILED,
            "attribution_failures": attribution_failures,
            "message": STOP_MESSAGE,
        })
        record_tripwire(state, run_id, summary.get("thread_id"), attribution_failures)
        code = EXIT_ATTRIBUTION_FAILED
    elif outcome is None or outcome.status != "completed" or codex_errors:
        summary.update({"verdict": "CODEX_FAILED", "exit_code": EXIT_CODEX_FAILED})
        code = EXIT_CODEX_FAILED
    else:
        summary.update({"verdict": "PASS", "exit_code": EXIT_OK})
        code = EXIT_OK
    _write_json(run_dir / "verdict.json", summary)
    return code, summary


def _static_read(environ: dict, cwd: Optional[Path] = None) -> dict:
    """One isolated zero-token read (no MCP stubs are needed without a thread)."""
    binary = resolve_binary(environ)
    env, _ = child_environment(environ)
    try:
        process, initialized = open_server(binary, env, "low", {})
    except (AppServerError, PinViolation) as exc:
        raise LunaReserveError(str(exc))
    try:
        return {**zero_token_read(process, initialized, cwd), "binary": str(binary)}
    except AppServerError as exc:
        raise LunaReserveError(str(exc))
    finally:
        process.close()


def audit_target(module_root: Path, target: str, policy: Policy,
                 environ: Optional[dict] = None) -> tuple[int, dict]:
    """Re-audit a rollout path or thread id against the run's own preflight."""
    environ = dict(os.environ if environ is None else environ)
    state = state_root(module_root, environ)
    path: Optional[Path] = Path(target).expanduser()
    reference: Optional[dict] = None
    reference_source = None
    thread_id = None
    if path is not None and path.suffix == ".jsonl" and path.is_file():
        match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path.name)
        thread_id = match.group(1) if match else None
    else:
        thread_id = target
        path = None
    if thread_id:
        for verdict_path in sorted((state / "runs").glob("*/verdict.json")):
            try:
                verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
                preflight = json.loads((verdict_path.parent / "preflight.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if verdict.get("thread_id") != thread_id:
                continue
            reference = preflight["admission"]
            reference_source = str(verdict_path.parent / "preflight.json")
            if path is None and verdict.get("rollout"):
                path = Path(verdict["rollout"])
    if reference is None or path is None:
        try:
            read = _static_read(environ)
        except LunaReserveError as exc:
            return EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [str(exc)]}
        if reference is None:
            reference = admission(read, policy)
            reference_source = "live bucket table (valid only if neither window has reset since the run)"
        if path is None and thread_id:
            path = find_rollout(Path(read.get("codex_home") or Path.home() / ".codex"), thread_id)
    if path is None or not path.is_file():
        return EXIT_PREFLIGHT_REFUSED, {"verdict": "REFUSED", "refusals": [f"no rollout found for {target}"]}
    if not reference.get("reserve") or not reference.get("regular"):
        return EXIT_PREFLIGHT_REFUSED, {
            "verdict": "REFUSED", "refusals": ["reference bucket table lacks a reserve or regular bucket"],
        }
    result = audit_records(
        load_rollout(path),
        Bucket.from_dict(reference["reserve"]),
        Bucket.from_dict(reference["regular"]),
        policy.jitter_seconds,
    )
    result.update({"rollout": str(path), "reference_source": reference_source})
    if result["verdict"] != "PASS":
        result["message"] = STOP_MESSAGE
        return EXIT_ATTRIBUTION_FAILED, result
    return EXIT_OK, result


def status(policy: Policy, environ: Optional[dict] = None,
           module_root: Optional[Path] = None) -> tuple[int, dict]:
    environ = dict(os.environ if environ is None else environ)
    report: dict[str, Any] = {"model": RESERVE_MODEL}
    report["scrubbed_environment"] = child_environment(environ)[1]
    try:
        read = _static_read(environ, launch_root(module_root) if module_root is not None else None)
    except LunaReserveError as exc:
        return EXIT_PREFLIGHT_REFUSED, {**report, "admitted": False, "refusals": [str(exc)]}
    decision = admission(read, policy)
    if module_root is not None:
        tripwire = tripwire_path(state_root(module_root, environ))
        if tripwire.exists():
            decision["admitted"] = False
            decision["refusals"].append(f"attribution failure tripwire present: {tripwire}")
    report.update(decision)
    report["binary"] = read.get("binary")
    report["codex_home"] = read.get("codex_home")
    report["mcp_servers_to_disable"] = sorted(mcp_servers(read))
    return (EXIT_OK if decision["admitted"] else EXIT_PREFLIGHT_REFUSED), report


# ---------------------------------------------------------------------------
# Human-readable output sized for a calling agent


def format_status(report: dict) -> str:
    lines = [f"Luna reserve ({RESERVE_MODEL}): {'ADMITTED' if report.get('admitted') else 'NOT ADMITTED'}"]
    for name in ("reserve", "regular"):
        bucket = report.get(name)
        if bucket:
            model = f" model={bucket['model_slug']}" if bucket.get("model_slug") is not None else ""
            lines.append(
                f"  {name}: id={bucket['limit_id']}{model} used={bucket['used_percent']:g}% "
                f"reached={bucket['reached']} credits={json.dumps(bucket['credits'])} "
                f"resets={bucket['resets_at_local']} ({bucket['resets_at_utc']})"
            )
    if "ordinary_usage_allowed" in report:
        lines.append(f"  ordinaryUsageAllowed={report.get('ordinary_usage_allowed')} banner={report.get('banner')}")
    for refusal in report.get("refusals") or []:
        lines.append(f"  refused: {refusal}")
    for warning in report.get("warnings") or []:
        lines.append(f"  warning: {warning}")
    return "\n".join(lines)


def format_run(summary: dict) -> str:
    lines = [f"verdict={summary.get('verdict')} exit={summary.get('exit_code')} run={summary.get('run_id')}"]
    if summary.get("verdict") == "REFUSED":
        lines.extend(f"refused: {reason}" for reason in summary.get("refusals") or [])
        return "\n".join(lines)
    if summary.get("verdict") == "PREFLIGHT_OK":
        lines.append(
            f"mode={summary.get('mode')} effort={summary.get('effort')} no thread started, no tokens spent "
            f"reserve_used%={(summary.get('reserve_before') or {}).get('used_percent')}"
        )
        lines.append(f"run_dir={summary.get('run_dir')}")
        return "\n".join(lines)
    usage = summary.get("token_usage") or {}
    before = (summary.get("reserve_before") or {}).get("used_percent")
    after = (summary.get("reserve_after") or {}).get("used_percent")
    lines.append(
        f"thread={summary.get('thread_id')} mode={summary.get('mode')} effort={summary.get('effort')} "
        f"turn={summary.get('turn_status')} live_attributed={summary.get('live_snapshots')}"
    )
    lines.append(
        f"tokens input={usage.get('inputTokens', usage.get('input_tokens'))} "
        f"cached={usage.get('cachedInputTokens', usage.get('cached_input_tokens'))} "
        f"output={usage.get('outputTokens', usage.get('output_tokens'))} reserve_used%={before}->{after}"
    )
    lines.append(f"last_message={summary.get('last_message_path')} format_ok={summary.get('last_message_format_ok')}")
    lines.append(f"run_dir={summary.get('run_dir')}")
    for item in summary.get("codex_errors") or []:
        lines.append(f"codex: {item}")
    for item in summary.get("warnings") or []:
        lines.append(f"warning: {item}")
    for failure in summary.get("attribution_failures") or []:
        lines.append(f"attribution: {failure}")
    if summary.get("message"):
        lines.append(summary["message"])
    return "\n".join(lines)
