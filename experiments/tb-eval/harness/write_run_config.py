"""Write the run provenance record before any method result is inspected.

Captures the frozen protocol, the pinned images, the runner sources, the
resolved machine paths and the model launch so a run can be audited later --
including from another machine after `git fetch` -- without trusting the
session that produced it.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HARNESS))
import paths

ROUND = paths.ROUND_DIR
OUT = Path(os.environ.get("HW_RUN_CONFIG") or paths.RUNS / "run-config.json")
LAUNCH = Path(os.environ.get("HW_SERVER_LAUNCH") or paths.CACHE / "server" / "launch.json")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


protocol = json.loads((ROUND / "protocol.json").read_text(encoding="utf-8"))
images = json.loads((ROUND / "base-images.json").read_text(encoding="utf-8"))
# The server launch record is machine-local evidence; a clone that has not
# started a server yet still gets a complete provenance record.
launch = json.loads(LAUNCH.read_text(encoding="utf-8")) if LAUNCH.is_file() else None

record = {
    "run_id": protocol["round"],
    "created_at": datetime.datetime.now().astimezone().isoformat(),
    "purpose": "L0 local stratified development run: 24 of 241 TB 0.2.18 tasks, Spine and Sentry, one seed",
    "state": "created",
    "tasks": protocol["tasks"],
    "task_count": len(protocol["tasks"]),
    "protocol_sha256": sha(ROUND / "protocol.json"),
    "protocol": protocol,
    "images": images,
    "runners": {
        "spine/run.mjs": sha(HARNESS / "spine" / "run.mjs"),
        "sentry/run_tb8.py": sha(HARNESS / "sentry" / "run_tb8.py"),
        "grade_original.py": sha(HARNESS / "grade_original.py"),
    },
    "paths": paths.resolved(),
    "model": (launch or {}).get("model", launch),
    "server_launch_record": str(LAUNCH) if launch else None,
    "environment_exclusions": protocol.get("environment_exclusions", []),
    "commands": {
        "spine": "node experiments/tb-eval/harness/spine/run.mjs",
        "sentry": "python experiments/tb-eval/harness/sentry/run_tb8.py",
    },
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("wrote", OUT, "tasks", record["task_count"])
