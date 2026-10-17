"""Canonical ordered goal-stack manifest core.

Pure functions over a small dict-based schema. The master layer owns
authorization, locking, and atomic file I/O; this module never touches the
filesystem except to validate ``goal``/``context`` file references against a
given Plans root, and never elaborates Lean.

Schema (dict form)::

    {"schema_version": 1, "revision": <nonneg int>,
     "entries": [<entry>, ...]}

Entry keys: ``id``, ``title``, ``status`` (required) plus optional ``goal``,
``done``, ``context``, ``next``, ``reason``, ``trigger``, ``depends_on``,
and contextual ``worktree``/``branch``/``checkpoint`` (free text used by
runtime reconciliation; they never affect order or selection).

Rules enforced by :func:`validate`:

* ``status`` is one of ``ready`` / ``active`` / ``blocked`` / ``parked``.
* Every entry has ``title``/``id``/``status`` and at least ``goal`` or
  ``done``; ``blocked`` needs a ``reason``; ``parked`` needs a ``trigger``
  or a ``reason``.
* ``depends_on`` lists distinct, existing live IDs: no self-edges, no
  cycles. ``active`` entries must have no dependencies.
* ``goal``/``context`` are Plans-relative paths resolving inside ``root``
  to an existing regular file (no absolute paths, URLs, traversal, or
  symlink escape). ``.worktrees/`` and ``master/`` subpaths are allowed.
* Unknown keys, wrong types (including bools where ints are expected),
  duplicate IDs, and unreasonable sizes are rejected.

Schema 2 (the shared, multi-host form) keeps the same keys and adds host
qualification: a path-valued field is either an unqualified Plans-relative
file in the shared goal store, or ``HOST:PATH`` naming a file on host
``HOST``. ``goal``/``context`` qualified with the reading host, and every
unqualified reference, are existence-checked exactly as in schema 1;
qualified with another host they are shape-checked only (unverifiable
here). ``worktree`` must be qualified with an absolute path and is never
existence-checked. An optional ``host`` names the owning host. Schema-1
documents never interpret ``HOST:`` prefixes, and schema-1 readers refuse
schema 2.

Entry list order is the only priority. :func:`select_next` returns the
first ``active`` entry (active work is never preempted by pushes), else the
first ``ready`` entry with no unsatisfied dependencies, else ``None``.
``blocked``/``parked`` entries are skipped.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

SCHEMA_VERSION = 1
SHARED_SCHEMA_VERSION = 2
SCHEMA_VERSIONS = (SCHEMA_VERSION, SHARED_SCHEMA_VERSION)

STATUSES = ("ready", "active", "blocked", "parked")

TOP_KEYS = frozenset({"schema_version", "revision", "entries"})

ENTRY_KEYS = frozenset({
    "id", "title", "status", "goal", "done", "context", "next",
    "reason", "trigger", "depends_on", "worktree", "branch", "checkpoint",
    "host",
})

UPDATABLE_KEYS = ENTRY_KEYS - {"id"}

# File references validated against the Plans root.
REF_FIELDS = ("goal", "context")

# Ordered emission for dumps().
FIELD_ORDER = (
    "id", "title", "status", "goal", "done", "context", "next",
    "reason", "trigger", "host", "worktree", "branch", "checkpoint",
    "depends_on",
)

ID_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*")
# A host name: short, lowercase, and never confusable with a URL scheme use.
HOST_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
QUALIFIED_RE = re.compile(r"([a-z][a-z0-9-]{0,31}):(.+)", re.S)
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")

MAX_ID_LEN = 128
MAX_TITLE_LEN = 500
MAX_TEXT_LEN = 8000
MAX_REF_LEN = 1024
MAX_DEPS = 64
MAX_ENTRIES = 2000
MAX_EVIDENCE_LEN = 16000


class GoalStackError(ValueError):
    """Raised for any malformed stack, bad reference, or refused operation."""


def _fail(message):
    raise GoalStackError(message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_text(name, value, *, max_len, what="field"):
    if not isinstance(value, str) or isinstance(value, bool):
        _fail("%s %r must be a string" % (what, name))
    if len(value) > max_len:
        _fail("%s %r exceeds %d characters" % (what, name, max_len))
    if "\x7f" in value:
        _fail("%s %r contains a forbidden character" % (what, name))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("%s %r is not valid Unicode text" % (what, name))
    return value


def _check_top(stack):
    if not isinstance(stack, dict):
        _fail("stack must be a dict")
    unknown = set(stack) - set(TOP_KEYS)
    if unknown:
        _fail("unknown top-level keys: %s" % sorted(unknown))
    missing = set(TOP_KEYS) - set(stack)
    if missing:
        _fail("missing top-level keys: %s" % sorted(missing))
    if (not _is_int(stack["schema_version"])
            or stack["schema_version"] not in SCHEMA_VERSIONS):
        _fail("schema_version must be the integer 1 or 2")
    if not _is_int(stack["revision"]) or stack["revision"] < 0:
        _fail("revision must be a nonnegative integer")
    entries = stack["entries"]
    if not isinstance(entries, list):
        _fail("entries must be a list")
    if len(entries) > MAX_ENTRIES:
        _fail("too many entries (max %d)" % MAX_ENTRIES)
    return entries


def _check_entry_shape(entry, index):
    where = "entries[%d]" % index
    if not isinstance(entry, dict):
        _fail("%s must be a dict" % where)
    unknown = set(entry) - set(ENTRY_KEYS)
    if unknown:
        _fail("%s has unknown keys: %s" % (where, sorted(unknown)))
    for key in ("id", "title", "status"):
        if key not in entry:
            _fail("%s is missing required key %r" % (where, key))
    vid = entry["id"]
    _check_text("id", vid, max_len=MAX_ID_LEN, what="%s field" % where)
    if not ID_RE.fullmatch(vid):
        _fail("%s has invalid id %r (want [a-z0-9][a-z0-9_.-]*)"
              % (where, vid))
    title = entry["title"]
    _check_text("title", title, max_len=MAX_TITLE_LEN,
                what="%s field" % where)
    if not title.strip():
        _fail("%s has an empty title" % where)
    if entry["status"] not in STATUSES:
        _fail("%s has invalid status %r" % (where, entry["status"]))
    for key in ("goal", "done", "context", "next", "reason", "trigger",
                "worktree", "branch", "checkpoint", "host"):
        if key in entry:
            _check_text(key, entry[key],
                        max_len=MAX_REF_LEN if key in REF_FIELDS
                        else MAX_TEXT_LEN,
                        what="%s field" % where)
    for key in REF_FIELDS:
        if key in entry and not entry[key].strip():
            _fail("%s field %r must not be empty" % (where, key))
    if "host" in entry and not HOST_RE.fullmatch(entry["host"]):
        _fail("%s has invalid host %r (want [a-z][a-z0-9-]{0,31})"
              % (where, entry["host"]))
    goal = entry.get("goal", "")
    done = entry.get("done", "")
    if not (isinstance(goal, str) and goal.strip()
            or isinstance(done, str) and done.strip()):
        _fail("%s needs at least 'goal' or 'done'" % where)
    if entry["status"] == "blocked":
        reason = entry.get("reason", "")
        if not (isinstance(reason, str) and reason.strip()):
            _fail("%s: blocked entries need a 'reason'" % where)
    if entry["status"] == "parked":
        trigger = entry.get("trigger", "")
        reason = entry.get("reason", "")
        if not ((isinstance(trigger, str) and trigger.strip())
                or (isinstance(reason, str) and reason.strip())):
            _fail("%s: parked entries need a 'trigger' or a 'reason'"
                  % where)
    if "depends_on" in entry:
        deps = entry["depends_on"]
        if not isinstance(deps, list):
            _fail("%s field 'depends_on' must be a list" % where)
        if len(deps) > MAX_DEPS:
            _fail("%s has too many dependencies (max %d)"
                  % (where, MAX_DEPS))
        for dep in deps:
            if not isinstance(dep, str) or not ID_RE.fullmatch(dep):
                _fail("%s has invalid dependency %r" % (where, dep))
        if len(set(deps)) != len(deps):
            _fail("%s has duplicate dependencies" % where)
        if vid in deps:
            _fail("%s depends on itself" % where)


def _check_entries_shape(entries, schema=SCHEMA_VERSION):
    for index, entry in enumerate(entries):
        _check_entry_shape(entry, index)
        if schema == SCHEMA_VERSION and "host" in entry:
            _fail("entries[%d] key 'host' needs schema_version 2" % index)
    seen = {}
    for entry in entries:
        if entry["id"] in seen:
            _fail("duplicate entry id %r" % entry["id"])
        seen[entry["id"]] = entry
    return seen


def _check_graph(by_id):
    for eid, entry in by_id.items():
        for dep in entry.get("depends_on") or []:
            if dep not in by_id:
                _fail("entry %r depends on unknown live id %r"
                      % (eid, dep))
    for eid, entry in by_id.items():
        if entry["status"] == "active" and (entry.get("depends_on") or []):
            _fail("active entry %r must not have unresolved dependencies"
                  % eid)
    color = {eid: 0 for eid in by_id}  # 0=white 1=gray 2=black
    for start in by_id:
        if color[start] != 0:
            continue
        color[start] = 1
        trail = [(start, iter(by_id[start].get("depends_on") or []))]
        while trail:
            node, it = trail[-1]
            descended = False
            for dep in it:
                if color[dep] == 1:
                    _fail("dependency cycle involving %r" % dep)
                if color[dep] == 0:
                    color[dep] = 1
                    trail.append(
                        (dep, iter(by_id[dep].get("depends_on") or [])))
                    descended = True
                    break
            if not descended:
                color[node] = 2
                trail.pop()


def _coerce_root(root):
    if isinstance(root, str):
        root = Path(root)
    if not isinstance(root, Path):
        _fail("root must be a path")
    if not root.is_dir():
        _fail("reference root %s is not a directory" % root)
    return root.resolve()


def split_ref(value):
    """Return ``(host, path)`` for ``HOST:PATH``, else ``(None, value)``.

    Only meaningful for schema-2 stacks; schema 1 never interprets prefixes.
    """
    if isinstance(value, str) and not URL_RE.match(value):
        match = QUALIFIED_RE.fullmatch(value)
        if match is not None:
            return match.group(1), match.group(2)
    return None, value


def _check_ref_shape(field, value, eid):
    if URL_RE.search(value):
        _fail("entry %r field %r must not be an external URL" % (eid, field))
    if "\x00" in value:
        _fail("entry %r field %r contains NUL" % (eid, field))


def _check_ref(field, value, root, resolved_root, eid):
    _check_ref_shape(field, value, eid)
    path = Path(value)
    if path.is_absolute():
        _fail("entry %r field %r must be Plans-relative" % (eid, field))
    target = (root / path).resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError:
        _fail("entry %r field %r escapes the Plans root" % (eid, field))
    if not target.is_file():
        _fail("entry %r field %r does not name an existing file: %r"
              % (eid, field, value))


def _check_foreign_ref(field, path, eid):
    """Shape-check a goal-store-relative file on another host."""
    _check_ref_shape(field, path, eid)
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts or not path.strip():
        _fail("entry %r field %r must be a goal-store-relative path on its "
              "host" % (eid, field))


def _check_worktree(value, eid, host):
    qualifier, path = split_ref(value)
    if qualifier is None:
        _fail("entry %r field 'worktree' must be host-qualified "
              "(HOST:/absolute/path)" % (eid,))
    _check_ref_shape("worktree", path, eid)
    if not path.startswith("/") or ".." in Path(path).parts:
        _fail("entry %r field 'worktree' must name an absolute path on host "
              "%r" % (eid, qualifier))


def _check_refs(by_id, root, schema=SCHEMA_VERSION, host=None):
    resolved = _coerce_root(root)
    root_path = Path(root)
    for eid, entry in by_id.items():
        for field in REF_FIELDS:
            if field not in entry:
                continue
            value = entry[field]
            if schema == SCHEMA_VERSION:
                _check_ref(field, value, root_path, resolved, eid)
                continue
            qualifier, path = split_ref(value)
            if qualifier is None or qualifier == host:
                # Shared and own-host files fail closed exactly as in schema 1.
                _check_ref(field, path, root_path, resolved, eid)
            else:
                _check_foreign_ref(field, path, eid)
        if schema != SCHEMA_VERSION and "worktree" in entry:
            _check_worktree(entry["worktree"], eid, host)


def _check_host(host):
    if host is not None and (not isinstance(host, str)
                             or not HOST_RE.fullmatch(host)):
        _fail("host name %r is invalid (want [a-z][a-z0-9-]{0,31})" % (host,))
    return host


def _check_schema(stack, schema):
    if schema is not None and stack["schema_version"] != schema:
        _fail("schema_version must be the integer %d here" % schema)


def validate(stack, root, *, host=None, schema=None):
    """Check a stack dict fully (structure, graph, file refs).

    ``host`` is the reading host's name (schema 2 only): its qualified refs
    are existence-checked; other hosts' are shape-checked. ``schema`` pins
    the accepted schema version when given.
    """
    _check_host(host)
    entries = _check_top(stack)
    _check_schema(stack, schema)
    by_id = _check_entries_shape(entries, stack["schema_version"])
    _check_graph(by_id)
    _check_refs(by_id, root, stack["schema_version"], host)
    return None


def check_structure(stack, *, schema=None):
    """Structure and graph only; never touches the filesystem."""
    entries = _check_top(stack)
    _check_schema(stack, schema)
    by_id = _check_entries_shape(entries, stack["schema_version"])
    _check_graph(by_id)
    return None


def ref_status(entry, host, schema=SHARED_SCHEMA_VERSION):
    """Describe each path-valued field: ``{field: (qualifier, path, verified)}``.

    ``verified`` is False only for refs on another host (or any qualified
    ref when ``host`` is unknown): those are displayed, never checked here.
    """
    result = {}
    for field in REF_FIELDS + ("worktree",):
        if field not in entry:
            continue
        if schema == SCHEMA_VERSION:
            result[field] = (None, entry[field], field != "worktree")
            continue
        qualifier, path = split_ref(entry[field])
        verified = (qualifier is None or qualifier == host) and field != "worktree"
        result[field] = (qualifier, path, verified)
    return result


def entry_owner(entry):
    """The owning host: explicit ``host``, else the worktree's qualifier."""
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("host"), str):
        return entry["host"]
    qualifier, _ = split_ref(entry.get("worktree"))
    return qualifier


def _check_position(name, value, low, high):
    if not _is_int(value):
        _fail("%s must be an integer" % name)
    if not low <= value <= high:
        _fail("%s %r out of range [%d, %d]" % (name, value, low, high))
    return value


def _toml_str(value):
    return json.dumps(value, ensure_ascii=False)


def dumps(stack):
    """Serialize a stack dict to canonical TOML text."""
    entries = _check_top(stack)
    schema = stack["schema_version"]
    by_id = _check_entries_shape(entries, schema)
    _check_graph(by_id)
    lines = ["schema_version = %d" % schema,
               "revision = %d" % stack["revision"], ""]
    for entry in entries:
        lines.append("[[entries]]")
        for key in FIELD_ORDER:
            if key not in entry:
                continue
            value = entry[key]
            if key == "depends_on":
                items = ", ".join(_toml_str(v) for v in value)
                lines.append("depends_on = [%s]" % items)
            else:
                lines.append("%s = %s" % (key, _toml_str(value)))
        lines.append("")
    if entries:
        return "\n".join(lines).rstrip("\n") + "\n"
    return "schema_version = %d\nrevision = %d\nentries = []\n" % (
        schema, stack["revision"])


def parse(text):
    """Read the portable generated TOML subset without a Python 3.11 dependency.

    One assignment per line, JSON-compatible scalar/array values, and
    ``[[entries]]`` tables. Other TOML syntax is deliberately refused on all
    Python versions. Creme's system-Python launchers also run on Python 3.9.
    """
    if not isinstance(text, str) or not text.strip():
        _fail("missing goal stack (no stack file content)")
    data = {}
    table = data
    explicit_entries = False
    decoder = json.JSONDecoder()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"\[\[entries\]\]\s*(?:#.*)?", line):
            if explicit_entries:
                _fail("entries array cannot be redeclared as tables")
            table = {}
            data.setdefault("entries", []).append(table)
            continue
        assignment = re.fullmatch(r"([a-z_]+)\s*=\s*(.+)", line)
        if assignment is None:
            _fail("unsupported goal-stack TOML syntax on line %d" % number)
        key, source = assignment.groups()
        if key in table:
            _fail("duplicate goal-stack key %r on line %d" % (key, number))
        try:
            value, end = decoder.raw_decode(source)
        except ValueError as exc:
            _fail("invalid goal-stack TOML value on line %d: %s" % (number, exc))
        tail = source[end:].strip()
        if tail and not tail.startswith("#"):
            _fail("unsupported goal-stack TOML value on line %d" % number)
        if not (type(value) in (str, int, bool) or isinstance(value, list)):
            _fail("unsupported goal-stack value type on line %d" % number)
        if table is data and key == "entries":
            if value != []:
                _fail("nonempty entries must use [[entries]] tables")
            explicit_entries = True
        table[key] = value
    return data


def loads(text, root, *, host=None, schema=None):
    """Parse and validate a stack, including its file references."""
    data = parse(text)
    validate(data, root, host=host, schema=schema)
    return data


def select_next(stack):
    """Return the next entry (copy) or None.

    Active entries come first, in list order, and are never preempted by
    pushes. Otherwise the first ``ready`` entry with no unsatisfied
    dependencies wins; ``blocked``/``parked`` are skipped.
    """
    entries = stack.get("entries") if isinstance(stack, dict) else None
    if not isinstance(entries, list):
        _fail("stack must have an entries list")
    fallback = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "active":
            return copy.deepcopy(entry)
        if (fallback is None and entry.get("status") == "ready"
                and not (entry.get("depends_on") or [])):
            fallback = entry
    return copy.deepcopy(fallback) if fallback is not None else None


def summary(stack):
    """Return counts, active IDs in order, and the next ID."""
    entries = stack.get("entries") if isinstance(stack, dict) else None
    if not isinstance(entries, list):
        _fail("stack must have an entries list")
    counts = {name: 0 for name in STATUSES}
    active_ids = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status in counts:
            counts[status] += 1
        if status == "active" and "id" in entry:
            active_ids.append(entry["id"])
    nxt = select_next(stack)
    result = {
        "schema_version": stack.get("schema_version"),
        "revision": stack.get("revision"),
        "total": len(entries),
        "active_ids": active_ids,
        "next_id": nxt["id"] if nxt is not None else None,
    }
    result.update(counts)
    return result


def _payload(payload, action, allowed):
    if not isinstance(payload, dict):
        _fail("%s payload must be a dict" % action)
    unknown = set(payload) - allowed
    if unknown:
        _fail("%s payload has unknown keys: %s" % (action, sorted(unknown)))
    return payload


def apply(stack, action, payload, root, *, host=None):
    """Apply one operation; return ``(new_stack, archive_record_or_None)``.

    The input is never mutated: work happens on a deep copy, and on any
    error the original object is untouched. ``revision`` increments exactly
    once per actual mutation. No-op ``move``/``reorder`` calls return an
    unchanged copy with no increment. Only ``complete``/``retire`` return
    an archive record.
    """
    if not isinstance(stack, dict):
        _fail("stack must be a dict")
    if action == "bootstrap":
        _payload(payload, action, set())
        work = copy.deepcopy(stack)
        if not isinstance(work.get("entries"), list):
            _fail("bootstrap needs an entries list")
        if work["entries"]:
            _fail("bootstrap refuses a nonempty stack")
        fresh = {"schema_version": SCHEMA_VERSION, "revision": 0,
                 "entries": []}
        validate(fresh, root, host=host)
        return fresh, None
    if action not in ("push", "move", "reorder", "update",
                      "complete", "retire"):
        _fail("unknown action %r" % (action,))
    if not isinstance(payload, dict):
        _fail("%s payload must be a dict" % action)
    work = copy.deepcopy(stack)
    validate(work, root, host=host)
    old_rev = work["revision"]
    entries = work["entries"]
    by_id = {e["id"]: e for e in entries}
    record = None

    if action == "push":
        _payload(payload, action, {"entry", "position"})
        if "entry" not in payload:
            _fail("push payload needs 'entry'")
        new_entry = copy.deepcopy(payload["entry"])
        pos = payload.get("position", 1)
        _check_position("position", pos, 1, len(entries) + 1)
        entries.insert(pos - 1, new_entry)
        work["revision"] = old_rev + 1
        validate(work, root, host=host)
    elif action == "move":
        _payload(payload, action, {"id", "position"})
        eid = payload.get("id")
        if eid not in by_id:
            _fail("unknown entry id %r" % (eid,))
        pos = payload.get("position")
        _check_position("position", pos, 1, len(entries))
        current = next(i for i, e in enumerate(entries) if e["id"] == eid)
        if current == pos - 1:
            return work, None
        entries.insert(pos - 1, entries.pop(current))
        work["revision"] = old_rev + 1
        validate(work, root, host=host)
    elif action == "reorder":
        _payload(payload, action, {"ids"})
        ids = payload.get("ids")
        if (not isinstance(ids, list)
                or any(not isinstance(i, str) for i in ids)):
            _fail("reorder payload needs an 'ids' string list")
        if (len(ids) != len(entries) or set(ids) != set(by_id)
                or len(set(ids)) != len(ids)):
            _fail("reorder 'ids' must be an exact permutation of live IDs")
        if [e["id"] for e in entries] == list(ids):
            return work, None
        entries[:] = [by_id[i] for i in ids]
        work["revision"] = old_rev + 1
        validate(work, root, host=host)
    elif action == "update":
        _payload(payload, action, {"id", "changes"})
        eid = payload.get("id")
        if eid not in by_id:
            _fail("unknown entry id %r" % (eid,))
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            _fail("update payload needs nonempty 'changes'")
        unknown = set(changes) - UPDATABLE_KEYS
        if unknown:
            _fail("update refuses keys: %s" % sorted(unknown))
        target = by_id[eid]
        for key, value in changes.items():
            target[key] = copy.deepcopy(value)
        work["revision"] = old_rev + 1
        validate(work, root, host=host)
    elif action == "complete":
        _payload(payload, action, {"id", "evidence"})
        eid = payload.get("id")
        if eid not in by_id:
            _fail("unknown entry id %r" % (eid,))
        evidence = payload.get("evidence")
        _check_text("evidence", evidence, max_len=MAX_EVIDENCE_LEN,
                    what="complete")
        if not evidence.strip():
            _fail("complete needs nonempty evidence")
        removed = by_id[eid]
        entries[:] = [e for e in entries if e["id"] != eid]
        for other in entries:
            deps = other.get("depends_on")
            if isinstance(deps, list) and eid in deps:
                kept = [d for d in deps if d != eid]
                if kept:
                    other["depends_on"] = kept
                else:
                    del other["depends_on"]
        work["revision"] = old_rev + 1
        validate(work, root, host=host)
        record = {
            "id": eid,
            "status": "complete",
            "entry": copy.deepcopy(removed),
            "evidence": evidence,
            "old_revision": old_rev,
            "new_revision": old_rev + 1,
        }
    elif action == "retire":
        _payload(payload, action, {"id", "reason"})
        eid = payload.get("id")
        if eid not in by_id:
            _fail("unknown entry id %r" % (eid,))
        reason = payload.get("reason")
        _check_text("reason", reason, max_len=MAX_TEXT_LEN, what="retire")
        if not reason.strip():
            _fail("retire needs a nonempty reason")
        dependents = [e["id"] for e in entries
                      if eid in (e.get("depends_on") or [])]
        if dependents:
            _fail("retire refuses %r with dependents: %s"
                  % (eid, sorted(dependents)))
        removed = by_id[eid]
        entries[:] = [e for e in entries if e["id"] != eid]
        work["revision"] = old_rev + 1
        validate(work, root, host=host)
        record = {
            "id": eid,
            "status": "retired",
            "entry": copy.deepcopy(removed),
            "reason": reason,
            "old_revision": old_rev,
            "new_revision": old_rev + 1,
        }
    return work, record


def touched_ids(action, payload):
    """The entry IDs whose prepared-against state a mutation depends on."""
    if not isinstance(payload, dict):
        _fail("%s payload must be a dict" % action)
    if action == "push":
        entry = payload.get("entry")
        return [entry["id"]] if isinstance(entry, dict) and isinstance(
            entry.get("id"), str) else []
    if action in ("update", "complete", "retire", "move"):
        return [payload["id"]] if isinstance(payload.get("id"), str) else []
    if action == "reorder":
        ids = payload.get("ids")
        return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
    _fail("action %r cannot be replayed" % (action,))


def replay_conflict(base, head, action, payload):
    """Return why a mutation prepared against ``base`` cannot replay on ``head``.

    ``None`` means the mutation's semantic preconditions are unchanged: every
    entry it touches is field-for-field identical in both revisions (or, for
    ``push``, absent from both), and order-sensitive mutations (``move``,
    ``reorder``, ``push`` with an explicit position) see the same live order.
    The caller still applies and validates the mutation against ``head``.
    """
    base_by = {e["id"]: e for e in base.get("entries", [])}
    head_by = {e["id"]: e for e in head.get("entries", [])}
    for eid in touched_ids(action, payload):
        if action == "push":
            if eid in head_by and eid not in base_by:
                return "entry %r was pushed concurrently" % eid
            continue
        if base_by.get(eid) != head_by.get(eid):
            return "entry %r changed since the prepared revision" % eid
    order_sensitive = action in ("move", "reorder") or (
        action == "push" and "position" in payload)
    if order_sensitive and [e["id"] for e in base.get("entries", [])] != [
            e["id"] for e in head.get("entries", [])]:
        return "the live order changed since the prepared revision"
    return None
