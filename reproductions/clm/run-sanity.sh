#!/usr/bin/env bash
# Runs one real-model trial. Obtain the shared GPU inference window first.
set -euo pipefail
REPRO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${CLM_VENV_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/horizon-weaver/repro-clm/venv}"
: "${CLM_API_BASE:?Set the already-running OpenAI-compatible endpoint; this script never starts a model.}"
exec "$VENV_DIR/bin/python" "$REPRO_DIR/repro.py" run --api-base "$CLM_API_BASE" "$@"
