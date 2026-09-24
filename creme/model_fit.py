"""Per-client model/effort fit tables: format, derivation, validation, recording.

Creme owns the method only (docs/guides/model-fit.md). The tables themselves,
with every observation, live in the goal store at
``$GOAL_STORE/model-fit/<client>.md``. Nothing in this module states how well
any model does; it only checks that a table says what its observations say.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

TABLE_DIR = "model-fit"
POINTER = "$GOAL_STORE/model-fit/<client>.md"
GUIDE_THRESHOLD = 3

# The fixed task-type vocabulary shared by every client (row identifiers).
TASK_TYPES: dict[str, str] = {
    "fact-finding": "read-only fact-finding: inventories, searches, state reconnaissance",
    "interface-design": "interface or architecture design",
    "statement-freezing": "statement freezing and Lean-free design (statements, proof transcripts from donors)",
    "lean-elaboration": "Lean elaboration from a frozen design",
    "open-proof": "open-shape proof discovery (no proof to mirror)",
    "proof-repair": "proof repair and diagnosis",
    "mutation-controls": "mutation controls (apply, build, restore, report the biting site)",
    "gate-integration": "gate runs, landings, and integration",
    "code-change": "code change with tests (non-Lean)",
    "doc-authoring": "document authoring",
    "review-audit": "hostile review or audit",
    "mechanical-edit": "mechanical edits and hoists",
}

STANDARD_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Client:
    name: str
    prefix: str
    title: str
    families: dict[str, tuple[str, ...]]
    routes: dict[str, str]
    # families only reachable through particular routes
    family_routes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def options(self) -> list[str]:
        return [f"{family}/{effort}" for family, efforts in self.families.items() for effort in efforts]


CLIENTS: dict[str, Client] = {
    "claude-code": Client(
        name="claude-code",
        prefix="cc",
        title="Claude Code",
        families={family: STANDARD_EFFORTS for family in ("fable", "opus", "sonnet")},
        routes={
            "claude-agent-tool": "Claude subagent spawned through the Agent tool by a Claude Code master",
            "claude-session": "a separate Claude Code session the master briefed",
        },
    ),
    "codex": Client(
        name="codex",
        prefix="cx",
        title="Codex",
        families={
            **{family: STANDARD_EFFORTS for family in ("astra", "sol", "luna")},
            "luna-reserve": STANDARD_EFFORTS,
        },
        routes={
            "codex-subagent": "Codex worker spawned by a Codex master",
            "codex-session": "a separate Codex session the master briefed",
            "luna-reserve-broker": "Luna reserve (gpt-reserve) through the Creme broker (start/send), any master",
            "luna-reserve-run": "Luna reserve (gpt-reserve) through one-shot `creme luna-reserve run`, any master",
        },
        family_routes={"luna-reserve": ("luna-reserve-broker", "luna-reserve-run")},
    ),
    "muse": Client(
        name="muse",
        prefix="mu",
        title="Muse",
        families={"muse-spark": ("none", "minimal", "low", "medium", "high", "xhigh", "max")},
        routes={
            "muse-worker": "worker inheriting its Muse master's session route",
            "muse-session": "a separate Muse session launched at the named route",
        },
    ),
    "antigravity": Client(
        name="antigravity",
        prefix="ag",
        title="Antigravity",
        families={"gemini-3.8-flash": ("low", "medium", "high")},
        routes={
            "antigravity-run": "read-only one-shot `creme antigravity run`, any master",
        },
    ),
}

FAILURE_MODES_SHOWN = 3
VERDICTS = ("pass", "partial", "fail", "unknown")
TOKEN_KEYS = ("uncached_input", "cache_read", "cache_write", "output", "reasoning")
FIELDS = (
    "task_type",
    "option",
    "route",
    "goal",
    "date",
    "run",
    "source",
    "verdict",
    "verdict_source",
    "failure_modes",
    "tokens",
    "wall_time",
    "turns",
    "retries",
    "rework",
    "recorded_by",
    "notes",
)
OPTIONAL_FIELDS = {"notes"}

CLIENT_MARKER = re.compile(r"^<!-- model-fit client=([a-z0-9-]+) -->$")
SUMMARY_BEGIN = "<!-- model-fit:summary:begin -->"
SUMMARY_END = "<!-- model-fit:summary:end -->"
OBSERVATIONS_HEADING = "## Observations"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINK = re.compile(r"^(?:/|~/|\$GOAL_STORE/|https?://|[A-Za-z0-9_.-]+/)\S*")
DURATION = re.compile(r"^(\d+)s$")
COUNT = re.compile(r"^\d+$")


class ModelFitError(Exception):
    pass


@dataclass
class Observation:
    ident: str
    fields: dict[str, str]
    line: int

    @property
    def verdict(self) -> str:
        return self.fields.get("verdict", "")

    @property
    def option(self) -> str:
        return self.fields.get("option", "")


@dataclass
class Table:
    path: Path
    client: Optional[Client]
    head: str
    summary: Optional[str]
    observations: list[Observation]
    errors: list[str]


# ---------------------------------------------------------------- parsing


def parse(path: Path, text: Optional[str] = None) -> Table:
    if text is None:
        text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    lines = text.splitlines()
    client: Optional[Client] = None
    for line in lines:
        match = CLIENT_MARKER.match(line.strip())
        if match:
            client = CLIENTS.get(match.group(1))
            if client is None:
                errors.append(f"unknown client {match.group(1)!r} (known: {', '.join(CLIENTS)})")
            break
    else:
        errors.append("missing client marker '<!-- model-fit client=NAME -->'")
    if client is not None and path.name != f"{client.name}.md":
        errors.append(f"file name {path.name!r} does not match client {client.name!r}")

    summary: Optional[str] = None
    head = text
    if SUMMARY_BEGIN in text and SUMMARY_END in text:
        begin = text.index(SUMMARY_BEGIN)
        end = text.index(SUMMARY_END)
        if end < begin:
            errors.append("summary end marker precedes its begin marker")
        else:
            summary = text[begin + len(SUMMARY_BEGIN):end].strip("\n")
            head = text[:begin]
    else:
        errors.append("missing generated summary block markers")

    observations: list[Observation] = []
    obs_start = None
    for index, line in enumerate(lines):
        if line.strip() == OBSERVATIONS_HEADING:
            obs_start = index
            break
    if obs_start is None:
        errors.append(f"missing '{OBSERVATIONS_HEADING}' section")
    else:
        current: Optional[Observation] = None
        for index in range(obs_start + 1, len(lines)):
            line = lines[index]
            stripped = line.strip()
            if stripped.startswith("## ") and not stripped.startswith("### "):
                errors.append(f"line {index + 1}: unexpected section after observations: {stripped}")
                break
            if stripped.startswith("### "):
                current = Observation(stripped[4:].strip(), {}, index + 1)
                observations.append(current)
                continue
            if not stripped or stripped.startswith("<!--"):
                continue
            if current is None:
                errors.append(f"line {index + 1}: text outside an observation")
                continue
            match = re.match(r"^- ([a-z_]+): (.*)$", stripped)
            if not match:
                errors.append(f"{current.ident}: line {index + 1}: not a '- key: value' field")
                continue
            key, value = match.group(1), match.group(2).strip()
            if key in current.fields:
                errors.append(f"{current.ident}: duplicate field {key}")
            current.fields[key] = value
    return Table(path, client, head, summary, observations, errors)


# ---------------------------------------------------------------- validation


def _check_link(value: str) -> bool:
    return bool(value) and value.lower() not in {"n/a", "none", "-"} and bool(LINK.match(value))


def parse_tokens(value: str) -> dict[str, Optional[int]]:
    parts = value.split()
    result: dict[str, Optional[int]] = {}
    for part in parts:
        if "=" not in part:
            raise ModelFitError(f"token entry {part!r} is not key=value")
        key, raw = part.split("=", 1)
        if key not in TOKEN_KEYS:
            raise ModelFitError(f"unknown token category {key!r} (allowed: {', '.join(TOKEN_KEYS)})")
        if key in result:
            raise ModelFitError(f"duplicate token category {key!r}")
        if raw == "n/a":
            result[key] = None
        elif COUNT.match(raw):
            result[key] = int(raw)
        else:
            raise ModelFitError(f"token count {key}={raw!r} is not a non-negative integer or n/a")
    missing = [key for key in TOKEN_KEYS if key not in result]
    if missing:
        raise ModelFitError(f"token categories missing: {', '.join(missing)}")
    return result


def parse_duration(value: str) -> Optional[int]:
    if value == "n/a":
        return None
    match = DURATION.match(value)
    if not match:
        raise ModelFitError(f"wall_time {value!r} is not '<seconds>s' or n/a")
    return int(match.group(1))


def parse_count(name: str, value: str) -> Optional[int]:
    if value == "n/a":
        return None
    if not COUNT.match(value):
        raise ModelFitError(f"{name} {value!r} is not a non-negative integer or n/a")
    return int(value)


def other_client_of(family: str, client: Client) -> Optional[str]:
    for other in CLIENTS.values():
        if other.name != client.name and family in other.families:
            return other.name
    return None


def validate_observation(observation: Observation, client: Client) -> list[str]:
    errors: list[str] = []
    ident = observation.ident
    fields = observation.fields
    if not re.match(rf"^{client.prefix}-\d{{4}}$", ident):
        errors.append(f"{ident}: observation id must be {client.prefix}-NNNN")
    for key in fields:
        if key not in FIELDS:
            errors.append(f"{ident}: unknown field {key!r}")
    for key in FIELDS:
        if key not in OPTIONAL_FIELDS and not fields.get(key):
            errors.append(f"{ident}: missing field {key!r}")
    task = fields.get("task_type")
    if task and task not in TASK_TYPES:
        errors.append(f"{ident}: unknown task type {task!r}")
    option = fields.get("option", "")
    family = ""
    if option:
        if "/" not in option:
            errors.append(f"{ident}: option {option!r} is not family/effort")
        else:
            family, effort = option.split("/", 1)
            if family not in client.families:
                owner = other_client_of(family, client)
                if owner:
                    errors.append(f"{ident}: option {option!r} names a {owner} model; "
                                  f"a {client.name} table holds only {client.name} options")
                else:
                    errors.append(f"{ident}: unknown {client.name} model {family!r}")
            elif effort not in client.families[family]:
                errors.append(f"{ident}: effort {effort!r} is not an option of {client.name} {family}")
    route = fields.get("route", "")
    if route:
        tag = route.split()[0]
        if tag not in client.routes:
            errors.append(f"{ident}: route tag {tag!r} is not a {client.name} route "
                          f"(known: {', '.join(client.routes)})")
        elif family in client.family_routes and tag not in client.family_routes[family]:
            errors.append(f"{ident}: {family} is reachable only through {', '.join(client.family_routes[family])}")
        elif family and family not in client.family_routes and any(
            tag in routes for routes in client.family_routes.values()
        ):
            errors.append(f"{ident}: route {tag!r} does not serve model {family!r}")
    date = fields.get("date", "")
    if date and not DATE.match(date):
        errors.append(f"{ident}: date {date!r} is not YYYY-MM-DD")
    source = fields.get("source", "")
    if "source" in fields and not _check_link(source):
        errors.append(f"{ident}: source {source!r} is not an evidence link (path or URL)")
    verdict = fields.get("verdict", "")
    if verdict and verdict not in VERDICTS:
        errors.append(f"{ident}: verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    verdict_source = fields.get("verdict_source", "")
    if verdict in {"pass", "partial", "fail"} and not _check_link(verdict_source):
        errors.append(f"{ident}: a {verdict} verdict needs a verdict_source link to the master's "
                      f"verification record, not {verdict_source!r}")
    if verdict == "unknown" and verdict_source and not (
        verdict_source.startswith("none") or _check_link(verdict_source)
    ):
        errors.append(f"{ident}: verdict_source for an unknown verdict is 'none — reason' or a link")
    for key, parser in (
        ("tokens", parse_tokens),
        ("wall_time", parse_duration),
        ("turns", lambda value: parse_count("turns", value)),
        ("retries", lambda value: parse_count("retries", value)),
    ):
        if fields.get(key):
            try:
                parser(fields[key])
            except ModelFitError as exc:
                errors.append(f"{ident}: malformed cost field {key}: {exc}")
    if fields.get("recorded_by", "").lower().startswith("worker"):
        errors.append(f"{ident}: an observation is recorded by the master, not the worker")
    return errors


# ---------------------------------------------------------------- derivation


def _cell_text(observations: list[Observation]) -> str:
    if not observations:
        return "no data"
    counts = {verdict: 0 for verdict in VERDICTS}
    for observation in observations:
        counts[observation.verdict] = counts.get(observation.verdict, 0) + 1
    verified = counts["pass"] + counts["partial"] + counts["fail"]
    text = f"{verified}v: {counts['pass']}P {counts['partial']}A {counts['fail']}F"
    if counts["unknown"]:
        text += f" +{counts['unknown']}U"
    if verified >= GUIDE_THRESHOLD:
        text += " guides"
    return text


def derive_summary(client: Client, observations: list[Observation]) -> str:
    by_cell: dict[tuple[str, str], list[Observation]] = {}
    for observation in observations:
        key = (observation.fields.get("task_type", ""), observation.option)
        by_cell.setdefault(key, []).append(observation)
    out: list[str] = [
        "Generated by `python3 -m creme model-fit summarize`; do not edit by hand.",
        f"Cell: `Nv: pP aA fF +uU` = N master-verified runs (pass, partial, fail) plus u runs whose verdict",
        f"is unknown; `guides` marks a cell with at least {GUIDE_THRESHOLD} verified runs, the only cells the",
        "selection rule lets decide a dispatch. Every run is a single uncontrolled observation whose outcome",
        "also depends on its brief and its verification. Tokens are observables, not dollars.",
        "",
    ]
    for family, efforts in client.families.items():
        out.append(f"### {family}")
        out.append("")
        out.append("| task type | " + " | ".join(efforts) + " |")
        out.append("|---|" + "---|" * len(efforts))
        for task in TASK_TYPES:
            cells = [_cell_text(by_cell.get((task, f"{family}/{effort}"), [])) for effort in efforts]
            out.append(f"| {task} | " + " | ".join(cells) + " |")
        out.append("")
    out.append("### Populated cells")
    out.append("")
    out.append("One line per cell with data: verdicts; up to three most recent failure modes; median output")
    out.append("tokens and wall time. The observations below the summary hold everything else.")
    out.append("")
    populated = [key for key in ((t, o) for t in TASK_TYPES for o in client.options()) if key in by_cell]
    if not populated:
        out.append("No observations yet.")
    for task, option in populated:
        cell = by_cell[(task, option)]
        line = f"- {task} × {option}: {_cell_text(cell)}"
        modes = [f"{o.ident}: {o.fields.get('failure_modes')}" for o in cell
                 if o.fields.get("failure_modes", "none").lower() not in {"none", "n/a"}]
        if modes:
            shown = "; ".join(modes[-FAILURE_MODES_SHOWN:])
            more = len(modes) - FAILURE_MODES_SHOWN
            line += f"; failures: {shown}" + (f" (+{more} earlier)" if more > 0 else "")
        output_tokens: list[int] = []
        walls: list[int] = []
        for o in cell:
            try:
                tokens = parse_tokens(o.fields.get("tokens", ""))
                if tokens["output"] is not None:
                    output_tokens.append(tokens["output"])
            except ModelFitError:
                pass
            try:
                wall = parse_duration(o.fields.get("wall_time", "n/a"))
                if wall is not None:
                    walls.append(wall)
            except ModelFitError:
                pass
        if output_tokens:
            line += f"; output {int(statistics.median(output_tokens)):,}"
        if walls:
            line += f"; wall {int(statistics.median(walls)):,}s"
        out.append(line)
    out.append("")
    return "\n".join(out).rstrip("\n")


# ---------------------------------------------------------------- whole files


def validate_table(table: Table) -> list[str]:
    errors = list(table.errors)
    client = table.client
    if client is None:
        return errors
    seen: set[str] = set()
    for observation in table.observations:
        if observation.ident in seen:
            errors.append(f"{observation.ident}: duplicate observation id")
        seen.add(observation.ident)
        errors.extend(validate_observation(observation, client))
    if table.summary is not None and not errors:
        expected = derive_summary(client, table.observations)
        if table.summary.strip("\n") != expected:
            errors.extend(_summary_mismatch(table.summary, expected))
    return [f"{table.path.name}: {error}" for error in errors]


def _summary_mismatch(actual: str, expected: str) -> list[str]:
    errors: list[str] = []
    actual_lines = actual.strip("\n").splitlines()
    expected_lines = expected.splitlines()
    family = ""
    expected_rows: dict[tuple[str, str], list[str]] = {}
    headers: dict[str, list[str]] = {}
    for line in expected_lines:
        if line.startswith("### "):
            family = line[4:]
        elif line.startswith("| task type |"):
            headers[family] = [c.strip() for c in line.strip("|").split("|")][1:]
        elif line.startswith("| ") and family in headers:
            cells = [c.strip() for c in line.strip("|").split("|")]
            expected_rows[(family, cells[0])] = cells[1:]
    family = ""
    for line in actual_lines:
        if line.startswith("### "):
            family = line[4:]
        elif line.startswith("| ") and not line.startswith("| task type |") and family in headers:
            cells = [c.strip() for c in line.strip("|").split("|")]
            want = expected_rows.get((family, cells[0]))
            if want is None:
                continue
            for effort, have, need in zip(headers[family], cells[1:], want):
                if have != need and need == "no data":
                    errors.append(f"cell {cells[0]} × {family}/{effort} claims {have!r} "
                                  f"but no observation stands behind it")
                elif have != need:
                    errors.append(f"cell {cells[0]} × {family}/{effort} reads {have!r}; "
                                  f"its observations derive {need!r}")
    if not errors:
        errors.append("generated summary does not match the observations; "
                      "run `python3 -m creme model-fit summarize FILE`")
    return errors


def render(client: Client, head: str, observations: list[Observation]) -> str:
    body = [head.rstrip("\n"), "", SUMMARY_BEGIN, derive_summary(client, observations), SUMMARY_END, "",
            OBSERVATIONS_HEADING, ""]
    for observation in observations:
        body.append(f"### {observation.ident}")
        body.append("")
        for key in FIELDS:
            if key in observation.fields:
                body.append(f"- {key}: {observation.fields[key]}")
        body.append("")
    return "\n".join(body).rstrip("\n") + "\n"


def skeleton(client: Client) -> str:
    families = ", ".join(f"`{family}` ({'/'.join(efforts)})" for family, efforts in client.families.items())
    routes = "\n".join(f"- `{tag}`: {text}" for tag, text in client.routes.items())
    head = f"""# Model fit — {client.title}

<!-- model-fit client={client.name} -->

This file compares {client.title} model/effort options with each other only. The method, the
task-type vocabulary, the observation format, and the selection rule are in Creme's
`docs/guides/model-fit.md`; the generated summary below is derived from the observations at
the end of this file by `python3 -m creme model-fit summarize`, and
`python3 -m creme model-fit validate` refuses a summary that does not match them.

Options (columns): {families}. Routes (the `route` tag of an observation):

{routes}

A cell guides a dispatch only with at least {GUIDE_THRESHOLD} master-verified runs; otherwise the
briefs guide's sizing rules decide, and the dispatch log says which applied.

## Summary
"""
    return render(client, head, [])


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def validate_dir(directory: Path) -> tuple[list[str], dict[str, dict[str, int]]]:
    errors: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    if not directory.is_dir():
        return [f"{directory}: not a directory"], counts
    for name in CLIENTS:
        if not (directory / f"{name}.md").is_file():
            errors.append(f"{name}.md: missing table file for client {name}")
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        if path.stem not in CLIENTS:
            errors.append(f"{path.name}: not a known client table (known: {', '.join(CLIENTS)})")
            continue
        table = parse(path)
        errors.extend(validate_table(table))
        tally = {"observations": len(table.observations), "verified": 0, "unknown": 0}
        for observation in table.observations:
            if observation.verdict == "unknown":
                tally["unknown"] += 1
            elif observation.verdict in VERDICTS:
                tally["verified"] += 1
        counts[path.stem] = tally
    return errors, counts


def summarize_file(path: Path) -> list[str]:
    table = parse(path)
    errors = [e for e in validate_table(table) if "summary" not in e and "cell " not in e]
    if errors:
        return errors
    assert table.client is not None
    write_atomic(path, render(table.client, table.head, table.observations))
    return []


def next_ident(client: Client, observations: list[Observation], archive: Optional[Path] = None) -> str:
    """The next free id, counting ids archived beside the table so none is ever reused."""
    idents = [observation.ident for observation in observations]
    if archive is not None and archive.is_dir():
        for path in sorted(archive.glob("*.md")):
            idents += re.findall(rf"^### ({client.prefix}-\d+)$", path.read_text(encoding="utf-8"), re.M)
    highest = 0
    for ident in idents:
        match = re.match(rf"^{client.prefix}-(\d+)$", ident)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{client.prefix}-{highest + 1:04d}"


def add_observation(path: Path, fields: dict[str, str], dry_run: bool = False) -> tuple[list[str], str]:
    table = parse(path)
    existing = [e for e in validate_table(table)]
    if existing:
        return existing, ""
    assert table.client is not None
    ident = next_ident(table.client, table.observations, path.parent / "archive")
    observation = Observation(ident, {k: v for k, v in fields.items() if v not in (None, "")}, 0)
    errors = [f"{path.name}: {e}" for e in validate_observation(observation, table.client)]
    if errors:
        return errors, ident
    if not dry_run:
        write_atomic(path, render(table.client, table.head, table.observations + [observation]))
    return [], ident


# ---------------------------------------------------------------- run extractors


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def claude_transcript_usage(path: Path, since: Optional[str] = None, until: Optional[str] = None) -> dict[str, Any]:
    """Tokens, wall time, turns, and served model from a Claude subagent transcript.

    ``since``/``until`` (ISO timestamps) restrict it to one segment, e.g. the part
    before a later continuation message that the master has not yet verified.
    """
    lower = _iso(since) if since else None
    upper = _iso(until) if until else None
    seen: dict[str, dict[str, Any]] = {}
    models: dict[str, int] = {}
    first = last = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stamp = row.get("timestamp")
        if stamp and ((lower and _iso(stamp) < lower) or (upper and _iso(stamp) > upper)):
            continue
        if stamp:
            first = first or stamp
            last = stamp
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        ident = message.get("id")
        usage = message.get("usage")
        if not ident or not isinstance(usage, dict):
            continue
        seen[ident] = usage  # streamed chunks repeat one message's usage; keep one per id
        model = message.get("model")
        if model and model != "<synthetic>":
            models[model] = models.get(model, 0) + 1
    totals = {"uncached_input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    for usage in seen.values():
        totals["uncached_input"] += int(usage.get("input_tokens") or 0)
        totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        totals["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        totals["output"] += int(usage.get("output_tokens") or 0)
    wall = int((_iso(last) - _iso(first)).total_seconds()) if first and last else None
    return {
        "tokens": " ".join(f"{k}={v}" for k, v in totals.items()) + " reasoning=n/a",
        "wall_time": f"{wall}s" if wall is not None else "n/a",
        "turns": str(len(seen)),
        "model": max(models, key=models.get) if models else None,
        "date": first[:10] if first else None,
        "started": first,
        "ended": last,
    }


def _luna_cumulative(session_dir: Path) -> Optional[dict[str, int]]:
    audits = sorted(session_dir.glob("turns/*/audit.json"), key=lambda p: int(p.parent.name))
    for audit in reversed(audits):
        usage = json.loads(audit.read_text(encoding="utf-8")).get("token_usage")
        if isinstance(usage, dict):
            return usage
    return None


def luna_run_usage(run_dir: Path) -> dict[str, Any]:
    """Tokens, wall time, and effort of one one-shot `luna-reserve run` record."""
    verdict = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
    usage = json.loads((run_dir / "audit.json").read_text(encoding="utf-8")).get("token_usage")
    stamps: list[float] = []
    transcript = run_dir / "transcript.jsonl"
    if transcript.is_file():
        for raw in transcript.read_text(encoding="utf-8").splitlines():
            try:
                stamp = json.loads(raw).get("t")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(stamp, (int, float)):
                stamps.append(float(stamp))
    if isinstance(usage, dict):
        cached = int(usage.get("cached_input_tokens") or 0)
        tokens = (f"uncached_input={int(usage.get('input_tokens') or 0) - cached} cache_read={cached} "
                  f"cache_write={int(usage.get('cache_write_input_tokens') or 0)} "
                  f"output={int(usage.get('output_tokens') or 0)} "
                  f"reasoning={int(usage.get('reasoning_output_tokens') or 0)}")
    else:
        tokens = " ".join(f"{k}=n/a" for k in TOKEN_KEYS)
    name = run_dir.name
    date = f"{name[0:4]}-{name[4:6]}-{name[6:8]}" if re.match(r"^\d{8}T", name) else None
    return {
        "tokens": tokens,
        "wall_time": f"{int(max(stamps) - min(stamps))}s" if stamps else "n/a",
        "turns": "1",
        "effort": verdict.get("effort"),
        "date": date,
        "resumed_from": None,
    }


def luna_session_usage(session_dir: Path) -> dict[str, Any]:
    """Tokens (thread-cumulative minus any resumed base), wall time, turns of one Luna reserve session.

    A one-shot run directory (no session.json) is read by `luna_run_usage`.
    """
    if not (session_dir / "session.json").is_file() and (session_dir / "verdict.json").is_file():
        return luna_run_usage(session_dir)
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    usage = _luna_cumulative(session_dir)
    base: dict[str, int] = {}
    resumed = session.get("resumed_from")
    if resumed:
        prior = session_dir.parent / resumed
        if (prior / "session.json").is_file():
            base = _luna_cumulative(prior) or {}
    turns = session.get("turns") or []
    wall = 0
    for turn in turns:
        if turn.get("started") and turn.get("completed"):
            wall += int((_iso(turn["completed"]) - _iso(turn["started"])).total_seconds())
    if usage is None:
        tokens = " ".join(f"{k}=n/a" for k in TOKEN_KEYS)
    else:
        def delta(key: str) -> int:
            return int(usage.get(key) or 0) - int(base.get(key) or 0)
        cached = delta("cached_input_tokens")
        tokens = (f"uncached_input={delta('input_tokens') - cached} cache_read={cached} "
                  f"cache_write={delta('cache_write_input_tokens')} output={delta('output_tokens')} "
                  f"reasoning={delta('reasoning_output_tokens')}")
    return {
        "tokens": tokens,
        "wall_time": f"{wall}s" if turns else "n/a",
        "turns": str(len(turns)),
        "effort": session.get("effort"),
        "date": (session.get("created") or "")[:10] or None,
        "resumed_from": resumed,
    }


def codex_rollout_usage(path: Path) -> dict[str, Any]:
    """Tokens, wall time, and model/effort from a Codex rollout JSONL (last total_token_usage)."""
    usage = None
    first = last = None
    model = effort = None
    turns = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stamp = row.get("timestamp")
        if stamp:
            first = first or stamp
            last = stamp
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("type") == "turn_context":
            model = payload.get("model") or model
            settings = (payload.get("collaboration_mode") or {}).get("settings") or {}
            effort = (settings.get("reasoning_effort") or payload.get("effort")
                      or payload.get("reasoning_effort") or effort)
            turns += 1
        if payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                usage = info["total_token_usage"]
    if usage is None:
        tokens = " ".join(f"{k}=n/a" for k in TOKEN_KEYS)
    else:
        cached = int(usage.get("cached_input_tokens") or 0)
        tokens = (f"uncached_input={int(usage.get('input_tokens') or 0) - cached} cache_read={cached} "
                  f"cache_write=n/a output={int(usage.get('output_tokens') or 0)} "
                  f"reasoning={int(usage.get('reasoning_output_tokens') or 0)}")
    wall = int((_iso(last) - _iso(first)).total_seconds()) if first and last else None
    return {
        "tokens": tokens,
        "wall_time": f"{wall}s" if wall is not None else "n/a",
        "turns": str(turns) if turns else "n/a",
        "model": model,
        "effort": effort,
        "date": first[:10] if first else None,
    }


# ---------------------------------------------------------------- goal store


def default_dir(creme_root: Path) -> Path:
    from . import semaphore
    from .profile import DEFAULT_RELATIVE_PROFILE, load as load_profile

    shared = semaphore.canonical_creme_root(creme_root)
    checked = load_profile(shared / DEFAULT_RELATIVE_PROFILE)
    if checked.status != "VALID" or checked.profile is None:
        raise ModelFitError(f"host profile is not VALID ({checked.status}); pass DIR explicitly")
    workspace = checked.profile["workspace"]
    store = workspace.get("goal_store")
    if not store:
        raise ModelFitError("no goal store is configured; pass DIR explicitly")
    return Path(workspace["root"]).expanduser() / store / TABLE_DIR
