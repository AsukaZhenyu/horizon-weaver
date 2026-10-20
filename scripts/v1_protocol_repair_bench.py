"""Run the final protocol repair on all five development tasks, once each."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    project = Path(__file__).resolve().parents[1]
    tb = project.parent / "terminal-bench"
    root = project / "runs/v1-policy-repair-20261003"
    source = root / "code/phase-guidance"
    if not (source / "horizon_weaver/policy.py").is_file():
        raise RuntimeError("final source archive missing")
    output = root / "v1-phase-final"
    output.mkdir()
    command = [str(tb / ".venv/Scripts/python.exe"), "-u", "-m", "terminal_bench.cli.tb.main", "run",
        "--dataset-path", str(tb / "original-tasks"), "--output-path", str(output), "--run-id", "phase-final",
        "--agent-import-path", "horizon_weaver.terminal_bench:HorizonWeaverAgent", "--model", "qwen3.5-9b",
        "--agent-kwarg", "api_base=http://127.0.0.1:8080/v1", "--agent-kwarg", "max_steps=40",
        "--agent-kwarg", "wall_time_s=300", "--agent-kwarg", "output_tokens=2048",
        "--n-concurrent", "1", "--n-attempts", "1", "--no-cleanup", "--no-upload-results", "--no-rebuild",
        "--global-agent-timeout-sec", "330", "--global-test-timeout-sec", "180"]
    for task in ["hello-world", "fix-permissions", "log-summary", "regex-log", "cancel-async-tasks"]:
        command += ["--task-id", task]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join([str(source), str(project), str(tb)]))
    (output / "launch.json").write_text(json.dumps(dict(command=command, source_root=str(source),
        scope="five selected development tasks, no best-of selection"), indent=2), encoding="utf-8")
    with (output / "console.txt").open("w", encoding="utf-8") as stream:
        return subprocess.call(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT)


if __name__ == "__main__":
    sys.exit(main())
