"""Canonical forms of Git remote URLs, for comparing remotes safely.

``canonical`` maps the spellings of one network repository to one string:
scheme (https, http, ssh, git, scp-like ``user@host:path``), user and
credentials are dropped; the host is lower-cased with brackets, ports,
trailing dots and ``www.`` stripped (``ssh.github.com`` is GitHub's
SSH-over-443 alias for ``github.com``); the path is percent-decoded, cut at
``?`` or ``#``, has dot segments collapsed, is case-folded (hosting services
treat owner and repository names case-insensitively) and then loses
duplicate and trailing slashes and any trailing ``.git``. Local paths and
``file://`` URLs resolve through ``realpath`` (relative paths against
``base``) and are case-folded too, so a case-insensitive file system cannot
make one directory look like two.

The canonical form is used only to recognise a public repository, where
over-matching makes the check stricter, never to decide that two URLs are
the same repository for the purpose of allowing a push. SSH host aliases
(``~/.ssh/config``) and literal IP addresses are not resolved.

Expansion of ``url.<base>.insteadOf`` and ``pushInsteadOf`` is Git's job:
read URLs with ``git remote get-url [--push] --all`` or ``git ls-remote
--get-url`` in the repository whose configuration applies.
"""
from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$")
_SCP = re.compile(r"^(?:[^@/]+@)?([^/:]+):(.*)$")
HOST_ALIASES = {"ssh.github.com": "github.com"}


def _host(host: str) -> str:
    host = unquote(host.strip()).lower()
    host = host.rsplit("@", 1)[-1]
    if host.startswith("["):  # [host]:port or [ipv6]:port
        host = host[1: host.find("]")] if "]" in host else host[1:]
    else:
        host = host.split(":", 1)[0]
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return HOST_ALIASES.get(host, host)


def _path(path: str) -> str:
    path = re.split(r"[?#]", unquote(path.strip()), maxsplit=1)[0]
    path = posixpath.normpath("/" + path).casefold()
    path = re.sub(r"/+", "/", path).strip("/")
    while path.endswith(".git") or path.endswith("/"):
        path = path[:-4] if path.endswith(".git") else path[:-1]
    return path


def is_local(url: str) -> bool:
    value = url.strip()
    match = _SCHEME.match(value)
    if match:
        return match.group(1).lower() == "file"
    # scp-like syntax needs a colon before any slash (``host:path``).
    head = value.split("/", 1)[0]
    return ":" not in head or value.startswith("/") or value.startswith(".")


def canonical(url: str, base: Optional[Path] = None) -> str:
    """One string per repository location; see the module docstring."""
    value = url.strip()
    match = _SCHEME.match(value)
    if match and match.group(1).lower() != "file":
        rest = match.group(2)
        if rest.startswith("["):
            close = rest.find("]")
            host, path = rest[: close + 1], rest[close + 1:]
            port, _, path = path.partition("/")
            host += port
        else:
            host, _, path = rest.partition("/")
        return f"{_host(host)}/{_path(path)}".casefold()
    if match:  # file://
        value = unquote(match.group(2))
        if value.startswith("localhost/"):
            value = value[len("localhost"):]
    elif not is_local(value):
        scp = _SCP.match(value)
        if scp:
            return f"{_host(scp.group(1))}/{_path(scp.group(2))}".casefold()
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    resolved = os.path.realpath(candidate)
    if resolved.endswith("/.git"):
        resolved = resolved[: -len("/.git")]
    return resolved.rstrip("/").casefold() or "/"


def local_path(url: str, base: Optional[Path] = None) -> str:
    """The absolute real path of a local URL (``file://``, ``~``, relative to ``base``)."""
    value = url.strip()
    match = _SCHEME.match(value)
    if match:
        value = unquote(match.group(2))
        if value.startswith("localhost/"):
            value = value[len("localhost"):]
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    return os.path.realpath(candidate)


def display(url: str) -> str:
    """Doctor's short display form: host/path without scheme or ``.git``."""
    return canonical(url) if not is_local(url) else url.strip()
