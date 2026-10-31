"""Generate round4-strat24/protocol.json from the frozen selection."""
import json
from pathlib import Path

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'harness'))
import paths
HERE = paths.TB_EVAL / "config"
ROUND = paths.ROUND_DIR
sel = json.loads((HERE / "selection.json").read_text(encoding="utf-8"))

LEGACY_CHECKS = {
    "hello-world": 2,
    "fix-permissions": 1,
    "log-summary": 3,
    "regex-log": 1,
    "jq-data-processing": 14,
    "sqlite-db-truncate": 1,
    "cprofiling-python": 2,
    "cancel-async-tasks": 6,
}

protocol = {
    "round": "20261031-strat24-round4",
    "scope": (
        "One 24-task stratified Terminal-Bench 0.2.18 development set: "
        "8 historical tasks (H) plus 16 new tasks (N) across 10 category buckets. "
        "Single fresh attempt per method; no cross-task memory."
    ),
    "methods": [
        "official Pi plus official Spine plugin",
        "official Sentry core plus controlled terminal host",
    ],
    "strata": sel["design"]["groups"],
    "selection_seed": sel["design"]["seed"],
    "exclusion_rules": sel["design"]["exclusion_rules"],
    "tasks": sel["run_order"],
    "legacy_tasks": [t for t in sel["run_order"] if t in LEGACY_CHECKS],
    "expected_checks": {t: LEGACY_CHECKS[t] for t in LEGACY_CHECKS},
    "grading": {
        "legacy": (
            "offline fixed-wheel protocol from round3: paused snapshot, docker commit, "
            "unchanged original tests, cached uv + pinned wheels, no network"
        ),
        "new": (
            "generic protocol: paused snapshot, docker commit, unchanged original tests, "
            "then the task's own run-tests.sh with network; this is a documented "
            "deviation from the legacy offline path and must be reported per task"
        ),
    },
    "model": "Qwen3.5-9B Q4_K_M",
    "model_sha256": "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8",
    "context_tokens": 16384,
    "parallel_model_requests": 1,
    "reasoning": "off",
    "actor_temperature": 0.2,
    "actor_max_output_tokens": 2048,
    "sentry_auxiliary_temperature": 0,
    "sentry_auxiliary_max_output_tokens": 4096,
    "agent_budget_seconds": 300,
    "grading_budget_seconds": 180,
    "cross_task_experience": "reset for every task; execution evaluation only",
    "feedback": (
        "task instruction, initial environment and ordinary tool results only; "
        "hidden tests injected after stopping"
    ),
    "host_differences": (
        "Pi and controlled terminal host are different; results compare complete "
        "integrations rather than isolated component effects"
    ),
    "purpose": "stratified development set; not a benchmark leaderboard or generalization result",
}

ROUND.mkdir(parents=True, exist_ok=True)
(ROUND / "protocol.json").write_text(
    json.dumps(protocol, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
print("wrote", ROUND / "protocol.json", "tasks:", len(protocol["tasks"]))
print("\n".join(f"{i:2d} {t}" for i, t in enumerate(protocol["tasks"], 1)))
