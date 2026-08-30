#!/bin/sh
set -eu

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -v
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/creme-pycache" python3 -m compileall -q creme .agents/skills/lean-prover/tools
git diff --check
empty_tree=$(git hash-object -t tree /dev/null)
git diff --check "$empty_tree" HEAD
