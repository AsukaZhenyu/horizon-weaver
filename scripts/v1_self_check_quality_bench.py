"""Run three matched tasks and the source-context regression on one revision."""
from __future__ import annotations

import json
import argparse
import os
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append")
    parser.add_argument("--revision", default="self-check-quality")
    parser.add_argument("--tests-log", default="tests-final-context-quality.txt")
    parser.add_argument("--policy-style", choices=["ir", "terminal"], default="ir")
    args = parser.parse_args()
    if not args.revision.replace("-", "").isalnum():
        parser.error("revision must be an alphanumeric label with optional hyphens")
    project = Path(__file__).resolve().parents[1]
    tb = project.parent / "terminal-bench"
    root = project / "runs/v1-policy-repair-20261003"
    tests = root / args.tests_log
    if not tests.exists() or not tests.read_text(encoding="utf-8-sig").rstrip().endswith("OK"):
        raise RuntimeError("Final context/runtime regression tests must pass before launching this revision")
    source = root / "code" / args.revision
    shutil.copytree(project / "horizon_weaver", source / "horizon_weaver",
                    ignore=shutil.ignore_patterns("__pycache__"))
    output = root / ("v1-" + args.revision)
    output.mkdir()
    command = [str(tb / ".venv/Scripts/python.exe"), "-u", "-m", "terminal_bench.cli.tb.main", "run",
        "--dataset-path", str(tb / "original-tasks"), "--output-path", str(output), "--run-id", args.revision,
        "--agent-import-path", "horizon_weaver.terminal_bench:HorizonWeaverAgent", "--model", "qwen3.5-9b",
        "--agent-kwarg", "api_base=http://127.0.0.1:8080/v1", "--agent-kwarg", "max_steps=40",
        "--agent-kwarg", "wall_time_s=300", "--agent-kwarg", "output_tokens=2048",
        "--agent-kwarg", "structured_output=" + ("none" if args.policy_style == "terminal" else "json_schema"),
        "--agent-kwarg", "policy_style=" + args.policy_style, "--n-concurrent", "1", "--n-attempts", "1",
        "--no-cleanup", "--no-upload-results", "--no-rebuild",
        "--global-agent-timeout-sec", "330", "--global-test-timeout-sec", "180"]
    tasks = args.task or ("fix-permissions", "log-summary", "regex-log", "cancel-async-tasks")
    for task in tasks:
        command += ["--task-id", task]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join([str(source), str(project), str(tb)]))
    (output / "launch.json").write_text(json.dumps(dict(command=command, source_root=str(source),
        tasks=tasks, scope="selected development regression tasks; one attempt; no holdout claim"), indent=2), encoding="utf-8")
    with (output / "console.txt").open("w", encoding="utf-8") as stream:
        return subprocess.call(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT)


if __name__ == "__main__":
    raise SystemExit(main())
