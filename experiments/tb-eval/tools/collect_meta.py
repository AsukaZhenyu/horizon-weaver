"""Collect task metadata for the stratified Terminal-Bench subset.

Read-only: writes a single JSON summary to config/task-meta.json.
"""
import json
import os
import re
from pathlib import Path

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'harness'))
import paths
ROOT = paths.require_tasks()
OUT = paths.TB_EVAL / "config"


def grab(txt, key):
    # [ \t]* keeps the match on one line: \s* would swallow the newline and
    # capture the next key when the value is empty.
    m = re.search(rf"^{key}:[ \t]*(.+)$", txt, re.M)
    return m.group(1).strip() if m else ""


def read(p):
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


rows = []
for d in sorted(ROOT.iterdir()):
    if not d.is_dir():
        continue
    ty = d / "task.yaml"
    if not ty.exists():
        continue
    y = read(ty)
    df = read(d / "Dockerfile")
    dc = read(d / "docker-compose.yaml") or read(d / "docker-compose.yml")
    tests = list((d / "tests").glob("*")) if (d / "tests").is_dir() else []

    lower_blob = (df + "\n" + dc).lower()
    rows.append(
        {
            "task": d.name,
            "category": grab(y, "category"),
            "difficulty": grab(y, "difficulty"),
            "tags": grab(y, "tags"),
            "max_agent_timeout_sec": grab(y, "max_agent_timeout_sec"),
            "max_test_timeout_sec": grab(y, "max_test_timeout_sec"),
            "expert_time_estimate_min": grab(y, "expert_time_estimate_min"),
            "junior_time_estimate_min": grab(y, "junior_time_estimate_min"),
            "base_image": (re.search(r"^FROM\s+(\S+)", df, re.M) or [None, ""])[1],
            "dockerfile_bytes": len(df),
            "compose_bytes": len(dc),
            "has_gpu_hint": bool(re.search(r"nvidia|cuda|\bgpu\b", lower_blob)),
            "has_download_hint": bool(
                re.search(r"curl |wget |git clone|pip download|huggingface|kaggle", lower_blob)
            ),
            "test_files": [t.name for t in tests],
            "solution_exists": (d / "solution.sh").exists()
            or (d / "solution.yaml").exists(),
        }
    )

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "task-meta.json").write_text(
    json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8"
)
print("tasks:", len(rows))
print("out:", OUT / "task-meta.json")
