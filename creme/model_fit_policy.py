"""Adaptive recommendations for the model-fit option tables.

The policy is deliberately small and bounded: each task type has one incumbent,
bounded probe counters and bounded cost history.  State is private to a client
and is protected by a sibling lock file while it is read and written.
"""

from __future__ import annotations

import fcntl
import json
import secrets
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

from . import model_fit


N_INITIAL = 4
N_AFTER_PROMOTION = 4
N_AFTER_DEMOTION = 8
N_CAP = 256
FAIL_FACTOR = 16
PASS_DIVISOR = 2
MIN_GAP = 3
COST_WINDOW = 5
MIN_MEASURED = 3
PENDING_CAP = 32
DEMOTE_FAILS = 2


class PolicyError(Exception):
    """A malformed policy request or unusable policy state."""


def _client(name: str) -> model_fit.Client:
    try:
        return model_fit.CLIENTS[name]
    except KeyError:
        raise PolicyError(
            f"unknown client {name!r} (known: {', '.join(model_fit.CLIENTS)})"
        )


def _task_type(client: model_fit.Client, task_type: str) -> None:
    if task_type not in model_fit.TASK_TYPES:
        raise PolicyError(
            f"unknown task type {task_type!r} (known: {', '.join(model_fit.TASK_TYPES)})"
        )


def _option(client: model_fit.Client, option: str) -> Tuple[str, str]:
    if option not in client.options():
        raise PolicyError(
            f"unknown option {option!r} for {client.name} "
            f"(known: {', '.join(client.options())})"
        )
    family, effort = option.split("/", 1)
    return family, effort


def _state_path(directory: Path, client: model_fit.Client) -> Path:
    return directory / "state" / f"{client.name}.json"


def _new_task(client: model_fit.Client) -> dict[str, Any]:
    return {
        "recommended": None,
        "known_good": None,
        "recent": [],
        "since_probe": 0,
        "cells": {
            option: {"n": N_INITIAL, "count": 0, "costs": []}
            for option in client.options()
        },
        "pending": {},
    }


def _new_state(client: model_fit.Client) -> dict[str, Any]:
    return {
        "schema": 1,
        "client": client.name,
        "weights": model_fit.default_weights(client),
        "task_types": {},
    }


def _ensure_task(state: dict[str, Any], client: model_fit.Client, task_type: str) -> dict[str, Any]:
    task_types = state.setdefault("task_types", {})
    task = task_types.setdefault(task_type, _new_task(client))
    task.setdefault("recommended", None)
    task.setdefault("known_good", None)
    task.setdefault("recent", [])
    task.setdefault("since_probe", 0)
    task.setdefault("pending", {})
    cells = task.setdefault("cells", {})
    for option in client.options():
        cells.setdefault(option, {"n": N_INITIAL, "count": 0, "costs": []})
    return task


def _check_state(state: dict[str, Any], client: model_fit.Client) -> None:
    if state.get("schema") != 1 or state.get("client") != client.name:
        raise PolicyError(f"state for {client.name} has an unsupported schema or client")
    weights = state.get("weights")
    if not isinstance(weights, dict) or not isinstance(weights.get("family"), dict):
        raise PolicyError(f"state for {client.name} has invalid weights")


def _save(path: Path, state: dict[str, Any]) -> None:
    model_fit.write_atomic(path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    path.chmod(0o644)


@contextmanager
def _locked_state(directory: Path, client: model_fit.Client) -> Iterator[Tuple[dict[str, Any], Path]]:
    path = _state_path(directory, client)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise PolicyError(f"cannot read policy state {path}: {exc}")
            else:
                state = _new_state(client)
            if not isinstance(state, dict):
                raise PolicyError(f"policy state {path} is not an object")
            _check_state(state, client)
            yield state, path
            _save(path, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _instant(now: Optional[Any] = None) -> datetime:
    if now is None:
        stamp = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(timezone.utc)
    else:
        stamp = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(timezone.utc)
    return stamp


def _timestamp(now: Optional[Any] = None) -> str:
    return _instant(now).strftime("%Y%m%dT%H%M%SZ")


def _iso_timestamp(now: Optional[Any] = None) -> str:
    stamp = _instant(now)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dispatch_id(task_type: str, stamp: str, pending: dict[str, Any]) -> str:
    for _ in range(10):
        ident = f"{task_type}-{stamp}-{secrets.token_hex(2)}"
        if ident not in pending:
            return ident
    raise PolicyError("could not create a unique dispatch id")


def _cost(state: dict[str, Any], client: model_fit.Client, task: dict[str, Any], option: str) -> float:
    family, effort = _option(client, option)
    cell = task["cells"][option]
    costs = cell.get("costs", [])
    if len(costs) >= MIN_MEASURED:
        return float(statistics.median(float(cost) for cost in costs))
    weights = state["weights"]
    try:
        return float(weights["family"][family]) * float(weights["effort"][effort])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyError(f"state weights do not cover option {option!r}: {exc}")


def _prune_pending(pending: dict[str, Any]) -> None:
    while len(pending) > PENDING_CAP:
        pending.pop(next(iter(pending)))


def recommend(
    directory: Path, client: str, task_type: str, default: Optional[str] = None,
    now: Optional[Any] = None,
) -> dict[str, Any]:
    directory = Path(directory)
    selected = _client(client)
    _task_type(selected, task_type)
    if default is not None:
        _option(selected, default)
    with _locked_state(directory, selected) as (state, path):
        task = _ensure_task(state, selected, task_type)
        if task["recommended"] is None:
            if default is None:
                _save(path, state)
                raise PolicyError(
                    f"no recommendation yet for {task_type}; pass --default OPTION chosen by the briefs guide's sizing rules"
                )
            task["recommended"] = default
            task["known_good"] = default
        recommended = task["recommended"]
        _option(selected, recommended)
        task["since_probe"] += 1
        for option, cell in task["cells"].items():
            if option != recommended:
                cell["count"] += 1

        current_cost = _cost(state, selected, task, recommended)
        candidates = [
            option for option in selected.options()
            if option != recommended and _cost(state, selected, task, option) < current_cost
        ]
        due = [option for option in candidates if task["cells"][option]["count"] >= task["cells"][option]["n"]]
        probe = False
        option = recommended
        reason = f"recommended: {recommended} (no probe due)"
        if due and task["since_probe"] > MIN_GAP:
            starved = [
                candidate for candidate in due
                if task["cells"][candidate]["count"] >= 2 * task["cells"][candidate]["n"]
            ]
            if starved:
                option = min(
                    starved,
                    key=lambda candidate: (
                        -(task["cells"][candidate]["count"] / task["cells"][candidate]["n"]),
                        _cost(state, selected, task, candidate),
                        candidate,
                    ),
                )
                reason_prefix = "probe (starvation guard)"
            else:
                option = min(
                    due,
                    key=lambda candidate: (_cost(state, selected, task, candidate), candidate),
                )
                reason_prefix = "probe"
            probe = True
            cell = task["cells"][option]
            reason = (
                f"{reason_prefix}: {option} due (count {cell['count']} >= n {cell['n']}), "
                f"cheaper than {recommended}"
            )
            task["since_probe"] = 0
        elif due:
            reason = f"recommended: {recommended} (probe gap is {MIN_GAP}; {MIN_GAP + 1} needed)"

        pending = task["pending"]
        instant = _instant(now)
        stamp = instant.strftime("%Y%m%dT%H%M%SZ")
        ident = _dispatch_id(task_type, stamp, pending)
        pending[ident] = {
            "option": option,
            "probe": probe,
            "at": instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _prune_pending(pending)
        return {
            "dispatch_id": ident,
            "task_type": task_type,
            "option": option,
            "probe": probe,
            "recommended": recommended,
            "reason": reason,
        }


def _find_pending(state: dict[str, Any], dispatch_id: str) -> Tuple[dict[str, Any], dict[str, Any]]:
    for task in state.get("task_types", {}).values():
        pending = task.get("pending", {})
        if dispatch_id in pending:
            return task, pending.pop(dispatch_id)
    raise PolicyError(f"unknown dispatch id {dispatch_id!r}")


def _reset_cells(task: dict[str, Any], keep: str, n: int) -> None:
    for option, cell in task["cells"].items():
        if option != keep:
            cell["n"] = n
            cell["count"] = 0


def _demote(state: dict[str, Any], client: model_fit.Client, task: dict[str, Any]) -> bool:
    current = task["recommended"]
    known_good = task.get("known_good")
    if known_good is not None and known_good != current:
        replacement = known_good
    else:
        current_cost = _cost(state, client, task, current)
        more_expensive = [
            option for option in client.options()
            if option != current and _cost(state, client, task, option) > current_cost
        ]
        replacement = min(
            more_expensive,
            key=lambda option: (_cost(state, client, task, option), option),
            default=current,
        )
    changed = replacement != current
    task["recommended"] = replacement
    task["known_good"] = None
    task["recent"] = []
    _reset_cells(task, replacement, N_AFTER_DEMOTION)
    return changed


def outcome(
    directory: Path, client: str, dispatch_id: str, verdict: str,
    tokens: Optional[int] = None, now: Optional[Any] = None,
) -> dict[str, Any]:
    directory = Path(directory)
    selected = _client(client)
    if verdict not in ("pass", "fail"):
        raise PolicyError("verdict must be 'pass' or 'fail'")
    if tokens is not None and tokens < 0:
        raise PolicyError("tokens must be zero or positive")
    with _locked_state(directory, selected) as (state, _):
        task, entry = _find_pending(state, dispatch_id)
        option = entry["option"]
        _option(selected, option)
        probe = bool(entry["probe"])
        cell = task["cells"][option]
        if tokens is not None:
            family, _ = _option(selected, option)
            try:
                measured = float(state["weights"]["family"][family]) * float(tokens)
                measured /= float(state["weights"].get("tokens_ref", 100000))
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                raise PolicyError(f"state weights cannot measure option {option!r}: {exc}")
            cell["costs"] = (list(cell.get("costs", [])) + [measured])[-COST_WINDOW:]

        event = "recorded"
        record_observation = False
        if probe:
            cell["count"] = 0
            record_observation = True
            if verdict == "fail":
                cell["n"] = min(N_CAP, cell["n"] * FAIL_FACTOR)
                event = "n-updated"
            elif cell["n"] == 1 and option != task["recommended"] and (
                _cost(state, selected, task, option) < _cost(state, selected, task, task["recommended"])
            ):
                # Promote only while the probed option is still a cheaper challenger: the incumbent
                # may have changed while this probe ran.
                old = task["recommended"]
                task["known_good"] = old
                task["recommended"] = option
                task["recent"] = []
                _reset_cells(task, option, N_AFTER_PROMOTION)
                event = "promoted"
            else:
                cell["n"] = max(1, cell["n"] // PASS_DIVISOR)
                event = "n-updated"
        elif task["recommended"] == option:
            task["recent"] = (list(task.get("recent", [])) + [verdict])[-3:]
            if verdict == "fail":
                record_observation = True
            if sum(item == "fail" for item in task["recent"]) >= DEMOTE_FAILS:
                changed = _demote(state, selected, task)
                event = "demoted" if changed else "recorded"

        return {
            "task_type": next(
                name for name, candidate in state["task_types"].items() if candidate is task
            ),
            "option": option,
            "probe": probe,
            "verdict": verdict,
            "event": event,
            "recommended": task["recommended"],
            "n": cell["n"] if probe else None,
            "record_observation": record_observation,
        }


def set_recommendation(directory: Path, client: str, task_type: str, option: str) -> dict[str, Any]:
    directory = Path(directory)
    selected = _client(client)
    _task_type(selected, task_type)
    _option(selected, option)
    with _locked_state(directory, selected) as (state, _):
        task = _ensure_task(state, selected, task_type)
        task["recommended"] = option
        task["known_good"] = option
        task["recent"] = []
        task["since_probe"] = 0
        for candidate, cell in task["cells"].items():
            if candidate != option:
                cell["n"] = N_INITIAL
                cell["count"] = 0
        return {"client": client, "task_type": task_type, "recommended": option}


def _next_candidate(state: dict[str, Any], client: model_fit.Client, task: dict[str, Any]) -> str:
    recommended = task["recommended"]
    if recommended is None:
        return "—"
    current_cost = _cost(state, client, task, recommended)
    candidates = [
        option for option in client.options()
        if option != recommended and _cost(state, client, task, option) < current_cost
    ]
    if not candidates:
        return "—"
    due = [option for option in candidates if task["cells"][option]["count"] >= task["cells"][option]["n"]]
    choices = due or candidates
    if due:
        chosen = min(choices, key=lambda option: (_cost(state, client, task, option), option))
    else:
        chosen = min(
            choices,
            key=lambda option: (
                max(0, task["cells"][option]["n"] - task["cells"][option]["count"]),
                _cost(state, client, task, option), option,
            ),
        )
    cell = task["cells"][chosen]
    return f"{chosen} ({cell['count']}/{cell['n']})"


def policy_table(directory: Path, client: str) -> str:
    directory = Path(directory)
    selected = _client(client)
    with _locked_state(directory, selected) as (state, _):
        rows = [
            "| task type | recommended | known good | recent | next probe candidate | since_probe |",
            "|---|---|---|---|---|---:|",
        ]
        for task_type, task in state.get("task_types", {}).items():
            recent = ", ".join(task.get("recent", [])) or "—"
            rows.append(
                f"| {task_type} | {task.get('recommended') or '—'} | "
                f"{task.get('known_good') or '—'} | {recent} | "
                f"{_next_candidate(state, selected, task)} | {task.get('since_probe', 0)} |"
            )
        return "\n".join(rows)
