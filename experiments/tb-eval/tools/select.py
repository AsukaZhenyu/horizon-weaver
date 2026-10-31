"""Deterministic stratified selection of the local Terminal-Bench dev set.

Design (pre-registered before any method result is seen):
  - unit: task
  - group: category collapsed into 11 buckets
  - keep the 8 historical tasks as stratum H (continuity across rounds)
  - fill 16 new slots, floor 2 per bucket where possible
  - exclusions: GPU hint; expert estimate > 120 min (incommensurate with the
    30-minute local budget); environment/build failure (added later, recorded)
  - order randomized with a fixed seed so duration does not confound
"""
import hashlib
import json
import random
import re
from pathlib import Path

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'harness'))
import paths
HERE = paths.TB_EVAL / "config"
META = json.loads((HERE / "task-meta.json").read_text(encoding="utf-8"))
BY = {r["task"]: r for r in META}

HISTORICAL = [
    "hello-world",
    "fix-permissions",
    "log-summary",
    "regex-log",
    "jq-data-processing",
    "sqlite-db-truncate",
    "cprofiling-python",
    "cancel-async-tasks",
]

GROUP = {
    "software-engineering": "software-engineering",
    "system-administration": "system-administration",
    "security": "security",
    "protocol-analysis": "security",
    "data-science": "data",
    "data-processing": "data",
    "data-querying": "data",
    "scientific-computing": "scientific-computing",
    "debugging": "debugging",
    "file-operations": "file-operations",
    "file_operations": "file-operations",
    "file-system": "file-operations",
    "mathematics": "mathematics-algorithms",
    "math": "mathematics-algorithms",
    "algorithms": "mathematics-algorithms",
    "optimization": "mathematics-algorithms",
    "model-training": "model-training-ml",
    "machine-learning": "model-training-ml",
    "games": "games",
    "game": "games",
}

SLOTS = {
    "software-engineering": 2,
    "system-administration": 2,
    "security": 2,
    "data": 1,
    "scientific-computing": 2,
    "debugging": 1,
    "mathematics-algorithms": 2,
    "model-training-ml": 2,
    "games": 1,
    "other": 1,
}


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def bucket(r):
    return GROUP.get(r["category"], "other")


TASK_ROOT = paths.require_tasks()


def compose_services(task):
    dc = TASK_ROOT / task / "docker-compose.yaml"
    if not dc.exists():
        return []
    text = dc.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^services:\s*$", text, re.M)
    if not m:
        return []
    out = []
    for line in text[m.end():].splitlines():
        if re.match(r"^\S", line) and line.strip():
            break
        if re.match(r"^  [A-Za-z0-9_.-]+:\s*$", line):
            out.append(line.strip()[:-1])
    return out


def exclude_reason(r):
    if r["has_gpu_hint"]:
        return "needs GPU (Dockerfile/compose references nvidia/cuda/gpu)"
    if r["category"] in {"audio-processing", "video-processing", "computer-vision"}:
        return "media/vision task needs large model weights not available offline"
    svcs = compose_services(r["task"])
    if len(svcs) > 1:
        return (
            "multi-service compose ("
            + ",".join(svcs)
            + "); the Spine/Sentry hosts run the client container alone"
        )
    e = num(r["expert_time_estimate_min"])
    if e is not None and e > 120:
        return f"expert estimate {e:.0f} min > 120 min local budget"
    return None


def sort_key(r):
    # stable, unrelated to difficulty or wording
    return hashlib.sha256(("w8strat:" + r["task"]).encode()).hexdigest()


pool = [r for r in META if r["task"] not in HISTORICAL]
for r in pool:
    r["group"] = bucket(r)
    r["excluded"] = exclude_reason(r)

chosen = list(HISTORICAL)
selection = []
for task in HISTORICAL:
    r = BY[task]
    selection.append({"task": task, "group": bucket(r), "stratum": "H", "reason": "historical dev set"})

for group, slots in SLOTS.items():
    cands = [r for r in pool if r["group"] == group and not r["excluded"]]
    cands.sort(key=sort_key)
    # spread difficulty: alternate by difficulty label while walking the stable order
    by_diff = {"easy": [], "medium": [], "hard": []}
    for r in cands:
        by_diff.setdefault(r["difficulty"] or "unknown", []).append(r)
    order = []
    for diff in ("easy", "medium", "hard", "unknown"):
        order.extend(by_diff.get(diff, []))
    picked = order[:slots]
    for r in picked:
        chosen.append(r["task"])
        selection.append(
            {
                "task": r["task"],
                "group": group,
                "stratum": "N",
                "difficulty": r["difficulty"],
                "expert_min": r["expert_time_estimate_min"],
                "reason": "stratified new task",
            }
        )
    if len(picked) < slots:
        selection.append(
            {
                "task": None,
                "group": group,
                "stratum": "gap",
                "reason": f"only {len(picked)} feasible of {slots} slots",
            }
        )

assert len(chosen) == len(set(chosen)), "duplicate task"
rng = random.Random(20261031)
run_order = chosen[:]
rng.shuffle(run_order)

excluded = [{"task": r["task"], "group": r["group"], "reason": r["excluded"]} for r in pool if r["excluded"]]
out = {
    "design": {
        "unit": "task",
        "groups": SLOTS,
        "strata": "H = 8 historical; N = 16 new across 10 buckets",
        "seed": 20261031,
        "exclusion_rules": [
            "GPU hint in Dockerfile/compose",
            "media/vision task requiring large offline weights",
            "multi-service compose (host runs the client container alone)",
            "expert_time_estimate_min > 120",
            "environment/build failure (recorded after build)",
        ],
        "budget": "300 s agent per task, 1 seed, 180 s original-test grading",
    },
    "selected": selection,
    "run_order": run_order,
    "excluded_before_running": excluded,
}
(HERE / "selection.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")

print(f"selected: {len(chosen)}")
print()
print(f"{'#':>3} {'task':32s} {'group':24s} {'stratum':8s} {'diff':8s} {'expert':>7s}")
for i, t in enumerate(run_order, 1):
    s = next(x for x in selection if x["task"] == t)
    r = BY[t]
    print(f"{i:3d} {t:32s} {s['group']:24s} {s['stratum']:8s} {r['difficulty']:8s} {r['expert_time_estimate_min'] or '-':>7s}")
print()
print("excluded before running:", len(excluded))
for e in excluded:
    print(f"  - {e['task']:32s} {e['reason']}")
