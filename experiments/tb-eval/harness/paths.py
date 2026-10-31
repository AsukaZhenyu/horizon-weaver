"""Machine-local paths for the Terminal-Bench evaluation harness.

Everything that legitimately differs between machines -- where Terminal-Bench is
checked out, where run evidence is written, which model file is served, which
Docker volume carries the Spine runtime -- is resolved here from environment
variables.  A fresh ``git clone`` plus ``bootstrap/setup.ps1`` therefore runs
without editing any script.

Defaults are relative to the repository, so the common case needs no
environment at all:

    HW_TB_HOME      Terminal-Bench 0.2.18 checkout   <repo>/.cache/terminal-bench
    HW_CACHE        downloaded third-party artifacts  <repo>/.cache
    HW_RUNS         per-case evidence (gitignored)    <repo>/.cache/runs
    HW_ROUND_DIR    protocol.json / base-images.json  <repo>/experiments/tb-eval/config
    HW_MODEL_GGUF   served model weights              <repo>/.cache/models/qwen3.5-9b-q4_k_m.gguf
    HW_PYTHON       interpreter for the helpers        this interpreter
    HW_SPINE_VOLUME Docker volume with the Pi runtime  week8-spine-linux-20261030
    HW_SPINE_SRC    spine-plugin source checkout      <cache>/upstream/spine-plugin
    HW_SENTRY_SRC   Sentry source checkout            <cache>/upstream/sentry
    HW_DOCKER_CONFIG directory holding config.json    <cache>/docker-config

Set an override only when a machine keeps an artifact somewhere else; every
run records the resolved values in ``run-config.json`` so the evidence states
which paths produced it.
"""
import os
import sys
from pathlib import Path

# .../experiments/tb-eval/harness/paths.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[3]
TB_EVAL = REPO_ROOT / "experiments" / "tb-eval"

CACHE = Path(os.environ.get("HW_CACHE") or REPO_ROOT / ".cache")
TB_HOME = Path(os.environ.get("HW_TB_HOME") or CACHE / "terminal-bench")
TB_TASKS = TB_HOME / "original-tasks"
RUNS = Path(os.environ.get("HW_RUNS") or CACHE / "runs")
ROUND_DIR = Path(os.environ.get("HW_ROUND_DIR") or TB_EVAL / "config")
MODEL_GGUF = Path(
    os.environ.get("HW_MODEL_GGUF") or CACHE / "models" / "qwen3.5-9b-q4_k_m.gguf"
)
PYTHON = os.environ.get("HW_PYTHON") or sys.executable
SPINE_VOLUME = os.environ.get("HW_SPINE_VOLUME") or "week8-spine-linux-20261030"
SPINE_SRC = Path(os.environ.get("HW_SPINE_SRC") or CACHE / "upstream" / "spine-plugin")
SENTRY_SRC = Path(os.environ.get("HW_SENTRY_SRC") or CACHE / "upstream" / "sentry")
DOCKER_CONFIG = Path(os.environ.get("HW_DOCKER_CONFIG") or CACHE / "docker-config")
GRADING_TOOLS = Path(os.environ.get("HW_GRADING_TOOLS") or CACHE / "grading-tools")

ENV_OVERRIDES = {
    key: value
    for key, value in {
        "HW_TB_HOME": os.environ.get("HW_TB_HOME"),
        "HW_CACHE": os.environ.get("HW_CACHE"),
        "HW_RUNS": os.environ.get("HW_RUNS"),
        "HW_ROUND_DIR": os.environ.get("HW_ROUND_DIR"),
        "HW_MODEL_GGUF": os.environ.get("HW_MODEL_GGUF"),
        "HW_PYTHON": os.environ.get("HW_PYTHON"),
        "HW_SPINE_VOLUME": os.environ.get("HW_SPINE_VOLUME"),
        "HW_SPINE_SRC": os.environ.get("HW_SPINE_SRC"),
        "HW_SENTRY_SRC": os.environ.get("HW_SENTRY_SRC"),
    }.items()
    if value
}


def resolved() -> dict:
    """The path set a run was produced with; written into run-config.json."""
    return {
        "repoRoot": str(REPO_ROOT),
        "tbHome": str(TB_HOME),
        "runs": str(RUNS),
        "roundDir": str(ROUND_DIR),
        "modelGguf": str(MODEL_GGUF),
        "python": PYTHON,
        "spineVolume": SPINE_VOLUME,
        "spineSrc": str(SPINE_SRC),
        "sentrySrc": str(SENTRY_SRC),
        "envOverrides": ENV_OVERRIDES,
    }


def require_tasks() -> Path:
    """Fail early, with the fix, instead of half-running against an empty tree."""
    if not (TB_TASKS / "hello-world" / "task.yaml").is_file():
        raise SystemExit(
            f"Terminal-Bench tasks not found at {TB_TASKS}.\n"
            "Run bootstrap/setup.ps1, or set HW_TB_HOME to an existing checkout."
        )
    return TB_TASKS


def require_model() -> Path:
    if not MODEL_GGUF.is_file():
        raise SystemExit(
            f"Model weights not found at {MODEL_GGUF}.\n"
            "Run bootstrap/setup.ps1, or set HW_MODEL_GGUF to the .gguf file."
        )
    return MODEL_GGUF
