"""Render a reviewed, fixed-recipe host capability; no command-bearing CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .host_build_broker import broker_inputs, render_contained_build_broker

WORKFLOW_BROKER_NAME = "codex-creme-contained-workflow"
RECIPES_RELATIVE = Path(".creme/workflow-recipes.json")
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,63}")
ENVIRONMENT = {"HOME", "PATH", "PYTHONNOUSERSITE", "VIRTUAL_ENV", "JAUNE_T8N_TARGET"}


def validate_recipes(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "repositories", "operations"} or value["version"] != 1:
        raise ValueError("workflow recipes require version 1, repositories, operations")
    repositories = value["repositories"]
    if not isinstance(repositories, dict) or set(repositories) != {"blanc", "jaune"}:
        raise ValueError("recipes require both repository roots")
    for root in repositories.values():
        if not isinstance(root, str) or not root.startswith("/") or any(p in {".", ".."} for p in root.split("/")):
            raise ValueError("recipe repository roots must be absolute without traversal")
    operations = value["operations"]
    if not isinstance(operations, dict) or not 1 <= len(operations) <= 256:
        raise ValueError("recipes require 1..256 operations")
    for name, operation in operations.items():
        if not IDENTIFIER.fullmatch(name) or name == "status":
            raise ValueError("invalid recipe operation identifier")
        if not isinstance(operation, dict) or set(operation) - {"profile", "memory_gib", "modes", "guard"} or not {"profile", "memory_gib", "modes"} <= set(operation):
            raise ValueError("recipe operation requires profile, memory_gib, modes")
        if operation["profile"] not in repositories or type(operation["memory_gib"]) is not int or not 1 <= operation["memory_gib"] <= 8:
            raise ValueError("invalid recipe profile or memory estimate")
        if operation.get("guard", "none") not in {"none", "blanc-build-certificate"}:
            raise ValueError("unknown recipe guard")
        if operation.get("guard") == "blanc-build-certificate" and operation["profile"] != "blanc":
            raise ValueError("Blanc certificate guard requires Blanc profile")
        modes = operation["modes"]
        if not isinstance(modes, dict) or not 1 <= len(modes) <= 16:
            raise ValueError("recipe requires bounded modes")
        for mode, execution in modes.items():
            if not IDENTIFIER.fullmatch(mode) or not isinstance(execution, dict) or set(execution) != {"argv", "env"}:
                raise ValueError("invalid recipe mode")
            argv, env = execution["argv"], execution["env"]
            if not isinstance(argv, list) or not 2 <= len(argv) <= 64 or any(not isinstance(a, str) or not a or '\x00' in a or '\n' in a for a in argv):
                raise ValueError("recipe argv must be a bounded array of nonempty strings")
            if argv[0] != "/usr/bin/bash" and argv[0] != "/usr/bin/python3" and not (argv[0].startswith("/") and argv[0].endswith("/.venv/bin/python")):
                raise ValueError("recipe executable must be a reviewed Python or bash interpreter")
            # Fixed scripts, never shell expressions or interpreter evaluation.
            index = 1
            if argv[0] != "/usr/bin/bash":
                while index < len(argv) and argv[index] in {"-B", "-s"}:
                    index += 1
            if index >= len(argv) or not argv[index].startswith("{repo}/scripts/"):
                raise ValueError("recipe must execute a repository script")
            if not isinstance(env, dict) or set(env) - ENVIRONMENT or any(not isinstance(v, str) or '\x00' in v or '\n' in v for v in env.values()):
                raise ValueError("recipe environment contains unsupported keys or values")
            for text in [*argv, *env.values()]:
                remainder = text.replace("{repo}", "").replace("{creme}", "")
                if "{" in remainder or "}" in remainder or any(p in {".", ".."} for p in text.split("/")):
                    raise ValueError("unknown recipe placeholder or path traversal")
    return value


def render_workflow_broker(root: Path) -> str | None:
    recipes_path = root / RECIPES_RELATIVE
    if not recipes_path.exists():
        return None
    if recipes_path.is_symlink() or not recipes_path.is_file():
        raise ValueError("workflow recipes must be a regular file")
    raw = recipes_path.read_bytes()
    inputs = broker_inputs(root)
    if inputs is None:
        raise ValueError("workflow recipes require a reviewed host preflight")
    return render_workflow_source(root, inputs, raw)


def render_workflow_source(root: Path, inputs: tuple[str, str, str], raw: bytes) -> str:
    """Render a review preview from proposed recipes without installing them."""
    recipes = validate_recipes(json.loads(raw))
    # Reuse the generated self-contained control-plane, worktree, cgroup and
    # secure lock checks. No import from the writable checkout before pin checks.
    base = render_contained_build_broker(root, *inputs).split("\ndef main(arguments:", 1)[0]
    constants = (
        f"\nRECIPES_PATH = CREME_ROOT / {str(RECIPES_RELATIVE)!r}\n"
        f"RECIPES_SHA256 = {hashlib.sha256(raw).hexdigest()!r}\n"
        f"RECIPES = {recipes!r}\n"
    )
    runtime = Path(__file__).with_name("host_workflow_runtime.py").read_text()
    return base + constants + runtime
