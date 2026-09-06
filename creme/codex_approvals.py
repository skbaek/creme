"""Read-only approval routing observations; never a permission authority.

Only the invoking session's turn_context and thread_settings_applied records
are decoded. Conversation, reviewer output, credentials, session identifiers,
and rollout paths are never returned. Thread defaults are not active-turn proof.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_ENVELOPE = (
    rb'^\s*\{\s*(?:(?:"timestamp"\s*:\s*"[^"\r\n]*"|'
    rb'"ordinal"\s*:\s*[0-9]+)\s*,\s*)*'
)
_CONTEXT = re.compile(
    _ENVELOPE +
    rb'"type"\s*:\s*"turn_context"\s*,'
)
_THREAD_SETTINGS = re.compile(
    _ENVELOPE + rb'"type"\s*:\s*"event_msg"\s*,\s*"payload"\s*:\s*\{\s*'
    rb'"type"\s*:\s*"thread_settings_applied"\s*,'
)
_CONTEXT_MARKER = re.compile(rb'(?<!\\)"type"\s*:\s*"turn_context"')
_SETTINGS_MARKER = re.compile(rb'(?<!\\)"type"\s*:\s*"thread_settings_applied"')
_LIMIT = 4 * 1024 * 1024


def _enum(value: Any, choices: set[str]) -> str:
    return value if isinstance(value, str) and value in choices else "unverified"


def _reviewer(value: Any) -> str:
    # Accepted by current clients for compatibility; generated config uses
    # the canonical spelling. Normalize both disk and recorded observations.
    if value == "guardian_subagent":
        return "auto_review"
    return _enum(value, {"user", "auto_review"})


def _policy(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("granular"), dict):
        return "granular"
    return _enum(value, {"on-request", "never", "untrusted", "on-failure"})


def _routing_fields(metadata: dict[str, Any]) -> dict[str, str]:
    """Immediately discard every field except normalized approval metadata."""
    sandbox = metadata.get("sandbox_policy")
    sandbox = _enum(sandbox.get("type") if isinstance(sandbox, dict) else None,
                    {"workspace-write", "read-only", "danger-full-access", "external-sandbox"})
    profile = metadata.get("active_permission_profile")
    name = profile.get("id") if isinstance(profile, dict) else None
    name = name if (isinstance(name, str) and not _UUID.fullmatch(name.lstrip(":"))
                    and re.fullmatch(r":?[A-Za-z][A-Za-z0-9_.:-]{0,63}", name)) else "unverified"
    permissions = metadata.get("permission_profile")
    filesystem = permissions.get("file_system") if isinstance(permissions, dict) else None
    filesystem = _enum(filesystem.get("type") if isinstance(filesystem, dict) else None,
                       {"restricted", "unrestricted"})
    return {
        "reviewer": _reviewer(metadata.get("approvals_reviewer")),
        "policy": _policy(metadata.get("approval_policy")),
        "sandbox": sandbox, "profile": name, "filesystem": filesystem,
    }


def _routing_summary(fields: dict[str, str]) -> str:
    return (f"reviewer={fields['reviewer']}; approval_policy={fields['policy']}; "
            f"permission profile={fields['profile']}; sandbox={fields['sandbox']}; "
            f"filesystem={fields['filesystem']}. ")


def _read_config(path: Path) -> dict[str, str]:
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        return {"status": "unverified", "detail": "TOML reader unavailable on this Python"}
    try:
        with path.open("rb") as handle:
            raw = handle.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            return {"status": "unverified", "detail": "configuration exceeds diagnostic size limit"}
        data = tomllib.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return {"status": "absent", "detail": "configuration absent"}
    except (OSError, UnicodeError, ValueError):
        return {"status": "unverified", "detail": "configuration unreadable or invalid"}
    # Legacy inline profiles can override top-level keys. Current CLI profiles
    # may be separate <name>.config.toml layers; these disk observations do not
    # resolve CLI selection or claim to reconstruct the client's merged config.
    profile = data.get("profile")
    profiles = data.get("profiles", {})
    if isinstance(profile, str) and isinstance(profiles, dict):
        override = profiles.get(profile)
        if isinstance(override, dict):
            data.update(override)
    reviewer = _reviewer(data.get("approvals_reviewer"))
    if "approvals_reviewer" not in data:
        reviewer = "unset"
    return {
        "status": "read", "reviewer": reviewer,
        "policy": _policy(data.get("approval_policy")),
        "named_profile": "yes" if isinstance(data.get("default_permissions"), str) else "no",
    }


def _saved_ui(home: Path) -> str:
    try:
        with (home / ".codex-global-state.json").open("rb") as handle:
            raw = handle.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            return "unverified"
        data = json.loads(raw)
        state = data.get("electron-persisted-atom-state", {})
        selected = state.get("permission-selection-by-host-id:local", {})
        if selected.get("kind") == "agent-mode":
            mode = selected.get("agentMode")
        else:
            mode = state.get("agent-mode-by-host-id", {}).get("local")
        return _enum(mode, {"guardian-approvals", "full-access", "default"})
    except (OSError, ValueError, AttributeError):
        return "unverified"


def _own_rollout(home: Path, session: str) -> Optional[Path]:
    if not _UUID.fullmatch(session):
        return None
    # The current index avoids scanning unrelated transcripts. Older clients
    # without this index can still be found by their exact session filename.
    index = home / "state_5.sqlite"
    if index.is_file():
        try:
            with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT rollout_path FROM threads WHERE id = ?", (session,)).fetchone()
            if row and isinstance(row[0], str):
                candidate = Path(row[0]).resolve()
                for directory in ("sessions", "archived_sessions"):
                    try:
                        candidate.relative_to((home / directory).resolve())
                    except ValueError:
                        continue
                    if candidate.is_file():
                        return candidate
        except (OSError, sqlite3.Error):
            pass
    try:
        matches = list((home / "sessions").glob(f"**/rollout-*-{session}.jsonl"))
        if len(matches) == 1:
            candidate = matches[0].resolve()
            candidate.relative_to((home / "sessions").resolve())
            return candidate
        return None
    except (OSError, ValueError):
        return None


@dataclass
class RoutingRecords:
    context: Optional[dict[str, str]] = None
    context_position: int = 0
    settings: Optional[dict[str, str]] = None
    settings_position: int = 0

    @property
    def context_predates_settings(self) -> bool:
        return self.settings_position > self.context_position


def _routing_records(path: Path, session: str) -> RoutingRecords:
    records = RoutingRecords()
    position = 0
    try:
        with path.open("rb") as handle:
            while True:
                line = handle.readline(_LIMIT + 1)
                if not line:
                    break
                position += 1
                context = bool(_CONTEXT.match(line))
                settings = bool(_THREAD_SETTINGS.match(line))
                if not context and _CONTEXT_MARKER.search(line):
                    # An unknown envelope must not leave earlier green evidence
                    # looking current. Do not decode the unrecognized record.
                    records.context = None
                    records.context_position = position
                if not settings and _SETTINGS_MARKER.search(line):
                    records.settings = None
                    records.settings_position = position
                oversized = len(line) > _LIMIT
                if oversized:
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(_LIMIT + 1)
                if not context and not settings:
                    continue
                payload = None
                if not oversized:
                    try:
                        payload = json.loads(line).get("payload")
                    except (ValueError, UnicodeError):
                        pass
                if context:
                    # A partial/newer malformed context invalidates older evidence.
                    records.context_position = position
                    records.context = _routing_fields(payload) if isinstance(payload, dict) else None
                else:
                    if isinstance(payload, dict) and isinstance(payload.get("thread_id"), str):
                        if payload["thread_id"] != session:
                            continue
                        metadata = payload.get("thread_settings")
                    else:
                        metadata = None
                    records.settings_position = position
                    records.settings = _routing_fields(metadata) if isinstance(metadata, dict) else None
    except OSError:
        return RoutingRecords()
    return records


def approval_checks(root: Path, *, home: Optional[Path] = None,
                    session: Optional[str] = None) -> list[tuple[str, str, str]]:
    """Return redacted check rows without querying or changing a live client."""
    home = home or Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")).expanduser()
    session = session if session is not None else (
        os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID", "")
    )
    rows = []
    requested_auto = False
    for label, path in (("user config", home / "config.toml"),
                        ("project config", root / ".codex" / "config.toml")):
        config = _read_config(path)
        if config["status"] != "read":
            rows.append(("Codex: " + label, "warn", config["detail"] + "; not runtime evidence"))
            continue
        reviewer = config["reviewer"]
        if reviewer in {"user", "auto_review"}:
            requested_auto = reviewer == "auto_review"
        status = "warn" if reviewer in {"user", "unverified"} else "ok"
        if reviewer == "unset":
            if label == "user config":
                reviewer = "unset (Codex default=user; other layers may override)"
                status = "warn"
            else:
                reviewer = "unset (inherits other layers)"
        rows.append(("Codex: " + label, status,
                     f"reviewer={reviewer}; approval_policy={config['policy']}; "
                     f"named permission profile={config['named_profile']}; disk intent only"))
    mode = _saved_ui(home)
    requested_auto = requested_auto or mode == "guardian-approvals"
    rows.append(("Codex: saved desktop mode", "warn" if mode == "unverified" else "ok",
                 f"local mode={mode}; saved UI preference, not runtime evidence"))
    path = _own_rollout(home, session)
    records = _routing_records(path, session) if path else RoutingRecords()
    if records.settings_position:
        settings = records.settings
        if settings is None:
            status = "warn"
            detail = "UNVERIFIED: incomplete or unsupported thread_settings_applied record. "
        else:
            status = "ok" if settings["reviewer"] != "unverified" else "warn"
            detail = "latest recorded thread_settings_applied: " + _routing_summary(settings)
        rows.append(("Codex: recorded thread settings", status, detail +
                     "Thread defaults govern subsequent turns; this record does not prove "
                     "the active turn's approval routing changed."))
    metadata = records.context
    if metadata is None:
        rows.append(("Codex: recorded approval routing", "warn",
                     "UNVERIFIED: own-session turn_context unavailable; no inference from other "
                     "sessions, disk config, or saved UI. Check the running client's permissions."))
        return rows
    reviewer, policy = metadata["reviewer"], metadata["policy"]
    sandbox, filesystem = metadata["sandbox"], metadata["filesystem"]
    detail = "latest recorded turn_context: " + _routing_summary(metadata)
    if records.context_predates_settings:
        status = "warn"
        detail += ("CONTEXT_PREDATES_SETTINGS: a later thread-settings record leaves the "
                   "active turn's routing UNVERIFIED by these records; confirm native "
                   "current-turn settings and actual approval receipts. ")
    elif reviewer == "user":
        status = "fail" if requested_auto else "warn"
        detail += ("AUTO_REVIEW_INACTIVE: recorded requests route to a person. "
                   "Select native auto review in the running client; use `client-profile "
                   "--auto-review` for an explicit configuration preview. ")
    elif reviewer == "unverified":
        status = "warn"
        detail += "UNVERIFIED: this client did not record a recognized reviewer. "
    elif policy == "never" or sandbox == "danger-full-access" or filesystem == "unrestricted":
        status = "fail"
        detail += "AUTO_REVIEW_BOUNDARY_MISSING: review needs interactive approvals and a sandbox. "
    elif policy not in {"on-request", "granular"} or (
        sandbox not in {"workspace-write", "read-only"} and filesystem != "restricted"
    ):
        status = "warn"
        detail += "UNVERIFIED: interactive review and restricted boundary are not established. "
    else:
        status = "ok"
        detail += "Native auto review is recorded with a restricted boundary. "
    detail += ("This is recorded context, not a live settings query or proof that an action was reviewed; "
               "in-turn settings changes may not yet be recorded.")
    rows.append(("Codex: recorded approval routing", status, detail))
    return rows
