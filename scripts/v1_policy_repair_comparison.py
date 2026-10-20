"""Serial, isolated development comparison on original Terminal-Bench tasks.

Requires an existing model server, TB environment and built images. No hidden
tests are loaded into policy context. Results are retained even when tasks fail.
"""
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
    python = tb / ".venv/Scripts/python.exe"
    root = project / "runs/v1-policy-repair-20261003"
    reference = root / "code"
    repaired = root / "code/repaired"
    if not repaired.exists():
        shutil.copytree(project / "horizon_weaver", repaired / "horizon_weaver",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(project / "scripts/bench_v0_reference.py", repaired / "bench_v0_reference.py")
    tasks = ["fix-permissions", "log-summary", "regex-log"]
    configurations = [
        ("v0-isolated-cwd", "reference-cwd", "scripts.bench_v0_reference:V0ReferenceAgent", tasks),
        ("v1-repaired", "repaired", "horizon_weaver.terminal_bench:HorizonWeaverAgent",
         ["hello-world", *tasks, "cancel-async-tasks"]),
    ]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join([str(project), str(tb)]))
    status = []
    for name, run_id, adapter, selected in configurations:
        output = root / name
        output.mkdir(parents=True)
        env = dict(environment)
        is_reference = name.startswith("v0-")
        source_root = reference if is_reference else repaired
        env["PYTHONPATH"] = os.pathsep.join([str(source_root), str(project), str(tb)])
        if is_reference:
            env["HORIZON_REFERENCE_CODE_ROOT"] = str(reference)
        command = [str(python), "-u", "-m", "terminal_bench.cli.tb.main", "run",
            "--dataset-path", str(tb / "original-tasks"), "--output-path", str(output), "--run-id", run_id,
            "--agent-import-path", adapter, "--model", "qwen3.5-9b",
            "--agent-kwarg", "api_base=http://127.0.0.1:8080/v1",
            "--agent-kwarg", "max_steps=40", "--agent-kwarg", "wall_time_s=300",
            "--agent-kwarg", "output_tokens=2048", "--n-concurrent", "1", "--n-attempts", "1",
            "--no-cleanup", "--no-upload-results", "--no-rebuild",
            "--global-agent-timeout-sec", "330", "--global-test-timeout-sec", "180"]
        for task in selected:
            command += ["--task-id", task]
        (output / "launch.json").write_text(json.dumps(dict(command=command,
            source_root=str(source_root),
            scope="development regression, single attempt, not a frozen baseline"), indent=2), encoding="utf-8")
        print("Starting " + name, flush=True)
        with (output / "console.txt").open("w", encoding="utf-8") as stream:
            # Python -m searches cwd before PYTHONPATH, including for gateway
            # children. Pin both to the archived source root.
            code = subprocess.call(command, cwd=source_root, env=env, stdout=stream, stderr=subprocess.STDOUT)
        status.append(dict(run=name, exit_code=code))
        (root / "comparison-status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        print("Completed " + name + " exit=" + str(code), flush=True)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
