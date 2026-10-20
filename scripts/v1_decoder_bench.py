"""Verify local schema decoding, then grade five original tasks once each."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from horizon_weaver.app import code_fingerprint
from horizon_weaver.core import DecisionInput
from horizon_weaver.policy import ModelGateway, parse_action


def main():
    project = Path(__file__).resolve().parents[1]
    tb = project.parent / "terminal-bench"
    root = project / "runs/v1-policy-repair-20261003"
    source = root / "code/decoder-v2"
    shutil.copytree(project / "horizon_weaver", source / "horizon_weaver",
                    ignore=shutil.ignore_patterns("__pycache__"))
    gateway = ModelGateway("qwen3.5-9b", structured_output="json_schema")
    started = time.monotonic()
    try:
        response = gateway.complete(DecisionInput(step=1,
            system="Return exactly the plain word finished. No JSON, braces, code fences or actions.",
            context="Use plain text only. Do not return a JSON action.",
            input_token_bound=512, max_output_tokens=128), 30)
        probe = dict(response=response.model_dump(mode="json"), profile=gateway.describe(),
            adversarial_format_request=True,
            elapsed_s=time.monotonic()-started, code_hash=code_fingerprint())
        (root / "decoder-probe.json").write_text(json.dumps(probe, indent=2), encoding="utf-8")
        action = parse_action(response.content)
        probe["action"] = action.model_dump(mode="json")
        (root / "decoder-probe.json").write_text(json.dumps(probe, indent=2), encoding="utf-8")
    finally:
        gateway.close()
    print("Local schema decoding verified", flush=True)
    output = root / "v1-decoder"
    output.mkdir()
    command = [str(tb / ".venv/Scripts/python.exe"), "-u", "-m", "terminal_bench.cli.tb.main", "run",
        "--dataset-path", str(tb / "original-tasks"), "--output-path", str(output), "--run-id", "decoder",
        "--agent-import-path", "horizon_weaver.terminal_bench:HorizonWeaverAgent", "--model", "qwen3.5-9b",
        "--agent-kwarg", "api_base=http://127.0.0.1:8080/v1", "--agent-kwarg", "max_steps=40",
        "--agent-kwarg", "wall_time_s=300", "--agent-kwarg", "output_tokens=2048",
        "--agent-kwarg", "structured_output=json_schema", "--n-concurrent", "1", "--n-attempts", "1",
        "--no-cleanup", "--no-upload-results", "--no-rebuild",
        "--global-agent-timeout-sec", "330", "--global-test-timeout-sec", "180"]
    for task in ["hello-world", "fix-permissions", "log-summary", "regex-log", "cancel-async-tasks"]:
        command += ["--task-id", task]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join([str(source), str(project), str(tb)]))
    (output / "launch.json").write_text(json.dumps(dict(command=command, source_root=str(source),
        scope="five original development tasks; schema decoder revision; one attempt"), indent=2), encoding="utf-8")
    with (output / "console.txt").open("w", encoding="utf-8") as stream:
        return subprocess.call(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT)


if __name__ == "__main__":
    sys.exit(main())
