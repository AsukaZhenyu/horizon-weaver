#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cache="$repo_root/.horizon/reproductions/stateact"
uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
mkdir -p "$cache"
if [[ ! -d "$cache/upstream/.git" ]]; then
  git clone https://github.com/ai-nikolai/StateAct.git "$cache/upstream"
fi
git -C "$cache/upstream" checkout --detach 8fb47be6f5f3978a38726608ae347987341f1d82
if [[ ! -x "$cache/venv/bin/python" ]]; then
  "$uv_bin" venv --python 3.10 "$cache/venv"
fi
"$uv_bin" pip install --link-mode copy --python "$cache/venv/bin/python" -r "$repo_root/reproductions/stateact/requirements-environment.txt"
"$uv_bin" pip install --link-mode copy --python "$cache/venv/bin/python" --no-deps llamp==0.0.19
export ALFWORLD_DATA="$cache/data"
"$cache/venv/bin/python" "$repo_root/reproductions/stateact/prepare_data.py"
"$uv_bin" pip freeze --python "$cache/venv/bin/python" > "$cache/environment-freeze.txt"
