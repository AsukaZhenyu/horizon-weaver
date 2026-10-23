#!/usr/bin/env bash
# Isolated setup only; does not launch a model or run inference.
set -euo pipefail
REPRO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$REPRO_DIR/../.." && pwd)"
VENV_DIR="${CLM_VENV_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/horizon-weaver/repro-clm/venv}"
UV="${UV:-$HOME/.local/bin/uv}"
mkdir -p "$REPO_ROOT/_work/upstream"
python3 "$REPRO_DIR/repro.py" fetch
"$UV" venv --allow-existing --python python3.12 "$VENV_DIR"
"$UV" pip install --python "$VENV_DIR/bin/python" \
  -r "$REPRO_DIR/requirements-ubuntu-py312.lock.txt" \
  "litellm[mcp]==1.106.2" \
  -e "$REPO_ROOT/_work/upstream/context-language-models" \
  -e "$REPO_ROOT/_work/upstream/ContextBench[tokenizer]"
"$VENV_DIR/bin/python" "$REPRO_DIR/repro.py" prepare
"$VENV_DIR/bin/python" "$REPRO_DIR/check_mechanism.py"
"$VENV_DIR/bin/python" "$REPRO_DIR/check_runner.py"
"$VENV_DIR/bin/python" "$REPRO_DIR/check_transport.py"
