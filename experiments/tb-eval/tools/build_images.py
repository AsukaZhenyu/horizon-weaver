"""Build the client images for the new stratified tasks.

Uses the harness' own compose manager so the environment and image naming match
`tb run` exactly (tb__<task>__client).  Each result is logged to build-log.json;
a task that cannot build is recorded as an environment exclusion, not as a
method failure.
"""
import json
import sys
import time
import traceback
from pathlib import Path

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'harness'))
import paths
TB = paths.TB_HOME
sys.path.insert(0, str(TB))

from terminal_bench.terminal.docker_compose_manager import DockerComposeManager  # noqa: E402

TASK_ROOT = paths.TB_TASKS
HERE = paths.RUNS
LOGS = HERE / "build-logs"
LOGS.mkdir(parents=True, exist_ok=True)

TASKS = [
    "spinning-up-rl",
    "assign-seats",
    "simple-web-scraper",
    "npm-conflict-resolution",
    "modernize-fortran-build",
    "processing-pipeline",
    "sql-injection-attack",
    "form-filling",
    "multistep-definite-integral",
    "mnist-learning-fix",
    "slurm-simple-node-monitoring",
    "amuse-install",
    "gomoku-planner",
    "modernize-scientific-stack",
    "catch-me-if-you-can",
    "prove-plus-comm",
]


def build_one(task: str) -> dict:
    td = TASK_ROOT / task
    started = time.time()
    entry = {"task": task, "status": "unknown", "attempts": []}
    last_error = None
    for attempt in range(1, 4):
        try:
            mgr = DockerComposeManager(
                client_container_name=f"{task}-build-only",
                client_image_name=f"tb__{task}__client",
                docker_compose_path=td / "docker-compose.yaml",
                docker_image_name_prefix="tb",
                no_rebuild=False,
                cleanup=False,
                sessions_logs_path=LOGS,
                agent_logs_path=LOGS,
            )
            mgr._build_with_image_watchdog(timeout=1800.0)
            img = mgr._client.images.get(f"tb__{task}__client")
            entry.update(
                status="built",
                image_id=img.id,
                tag="tb__{0}__client:latest".format(task),
                size_mb=round(img.attrs.get("Size", 0) / 1e6, 1),
            )
            entry["attempts"].append({"attempt": attempt, "status": "built"})
            break
        except Exception as exc:  # noqa: BLE001
            detail = ""
            out = getattr(exc, "output", None)
            err = getattr(exc, "stderr", None)
            if out:
                detail += str(out)[-4000:]
            if err:
                detail += "\n" + str(err)[-4000:]
            last_error = detail or str(exc)
            entry["attempts"].append(
                {"attempt": attempt, "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:400]}
            )
            (LOGS / f"{task}.attempt{attempt}.log").write_text(
                traceback.format_exc() + "\n\n" + detail, encoding="utf-8"
            )
            # transient mirrors / registry errors: wait and retry
            time.sleep(10 * attempt)
    if entry["status"] != "built":
        entry.update(
            status="failed",
            error=f"{last_error}"[:2000] if last_error else "unknown",
        )
    entry["seconds"] = round(time.time() - started, 1)
    return entry


def main() -> None:
    out = HERE / "build-log.json"
    # Merge with earlier runs: a resumed batch must not erase completed tasks.
    previous = {}
    if out.exists():
        try:
            for record in json.loads(out.read_text(encoding="utf-8")):
                previous[record["task"]] = record
        except (json.JSONDecodeError, KeyError):
            previous = {}
    only = sys.argv[1:] or TASKS
    for task in only:
        print(f"[build] {task} ...", flush=True)
        entry = build_one(task)
        previous[task] = entry
        print(f"[build] {task}: {entry['status']} ({entry.get('seconds')}s) {entry.get('error', '')}", flush=True)
        out.write_text(json.dumps(list(previous.values()), indent=1, ensure_ascii=False), encoding="utf-8")
    ok = [r for r in previous.values() if r["status"] == "built"]
    print(f"\ndone: {len(ok)}/{len(previous)} built")


if __name__ == "__main__":
    main()
