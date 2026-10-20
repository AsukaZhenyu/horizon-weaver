"""One hard-task comparison at restored 8192 output capacity; kept separate."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    project = Path(__file__).resolve().parents[1]
    tb = project.parent / "terminal-bench"
    root = project / "runs/v1-policy-repair-20261003"
    current = root / "code/final-defaults"
    shutil.copytree(project / "horizon_weaver", current / "horizon_weaver",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for adapter, name, source in [
        ("scripts.bench_v0_reference:V0ReferenceAgent", "v0-output8192", root / "code"),
        ("horizon_weaver.terminal_bench:HorizonWeaverAgent", "v1-output8192", current),
    ]:
        output = root / name
        output.mkdir()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
            HORIZON_REFERENCE_CODE_ROOT=str(root / "code"),
            PYTHONPATH=os.pathsep.join([str(source), str(project), str(tb)]))
        command = [str(tb / ".venv/Scripts/python.exe"), "-u", "-m", "terminal_bench.cli.tb.main", "run",
            "--dataset-path", str(tb / "original-tasks"), "--output-path", str(output), "--run-id", name,
            "--agent-import-path", adapter, "--model", "qwen3.5-9b", "--task-id", "regex-log",
            "--agent-kwarg", "api_base=http://127.0.0.1:8080/v1", "--agent-kwarg", "max_steps=40",
            "--agent-kwarg", "wall_time_s=300", "--agent-kwarg", "output_tokens=8192",
            "--n-concurrent", "1", "--n-attempts", "1", "--no-cleanup", "--no-upload-results", "--no-rebuild",
            "--global-agent-timeout-sec", "330", "--global-test-timeout-sec", "180"]
        (output / "launch.json").write_text(json.dumps(dict(command=command, source_root=str(source),
            scope="single regex task, output-capacity follow-up, not part of the five-task result"), indent=2), encoding="utf-8")
        print("Starting " + name, flush=True)
        with (output / "console.txt").open("w", encoding="utf-8") as stream:
            code = subprocess.call(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT)
        print("Completed " + name + " exit=" + str(code), flush=True)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
