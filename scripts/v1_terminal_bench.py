"""Launch the v1 adapter with an existing Terminal-Bench 0.2 environment.

Example (from the Horizon-Weaver checkout):
  python scripts/v1_terminal_bench.py --tb-root ../terminal-bench --task hello-world
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tb-root", type=Path, required=True)
    parser.add_argument("--tb-python", type=Path, help="defaults to the existing TB .venv interpreter")
    parser.add_argument("--task", action="append", required=True, help="repeat for multiple task IDs")
    parser.add_argument("--dataset-path", type=Path, help="defaults to TB original-tasks")
    parser.add_argument("--output-path", type=Path, default=Path("runs/terminal-bench"))
    parser.add_argument("--run-id", default=datetime.now().strftime("v1-%Y%m%d-%H%M%S"))
    parser.add_argument("--model", default="qwen3.5-9b")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--wall-time", type=float, default=300)
    parser.add_argument("--tool-timeout", type=float, default=30)
    parser.add_argument("--max-tool-timeout", type=float, default=120)
    parser.add_argument("--require-self-check", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--output-tokens", type=int, default=8192)
    parser.add_argument("--decision-output-tokens", type=int, default=2048)
    parser.add_argument("--model-timeout", type=float, default=60)
    parser.add_argument("--max-model-timeout", type=float, default=120)
    parser.add_argument("--structured-output", choices=["none", "json_schema"])
    parser.add_argument("--policy-style", choices=["ir", "terminal"], default="ir")
    parser.add_argument("--context-strategy", choices=["state", "recent"], default="state")
    parser.add_argument("--transition-guard", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--exploration-history", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enhanced-verify", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--test-timeout", type=float, default=180)
    parser.add_argument("--rebuild", action="store_true", help="build original task images before running")
    args = parser.parse_args()
    if args.structured_output is None:
        args.structured_output = "none" if args.policy_style == "terminal" else "json_schema"
    project = Path(__file__).resolve().parents[1]
    tb_root = args.tb_root.resolve()
    tb_python = args.tb_python or tb_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not tb_python.is_file():
        parser.error("Terminal-Bench interpreter is missing; provide --tb-python for an installed 0.2 environment")
    dataset = (args.dataset_path or tb_root / "original-tasks").resolve()
    if any(not (dataset / task / "task.yaml").is_file() for task in args.task):
        parser.error("all selected tasks must exist in the specified dataset")
    output = args.output_path.resolve()
    run_path = output / args.run_id
    if run_path.exists():
        parser.error("select a fresh run ID; this launcher does not silently resume benchmark trials")
    command = [str(tb_python.resolve()), "-u", "-m", "terminal_bench.cli.tb.main", "run",
        "--dataset-path", str(dataset), "--output-path", str(output), "--run-id", args.run_id,
        "--agent-import-path", "horizon_weaver.terminal_bench:HorizonWeaverAgent",
        "--model", args.model, "--agent-kwarg", f"api_base={args.base_url}",
        "--agent-kwarg", f"max_steps={args.max_steps}", "--agent-kwarg", f"wall_time_s={args.wall_time}",
        "--agent-kwarg", f"output_tokens={args.output_tokens}",
        "--agent-kwarg", f"decision_output_tokens={args.decision_output_tokens}",
        "--agent-kwarg", f"model_timeout_s={args.model_timeout}",
        "--agent-kwarg", f"max_model_timeout_s={args.max_model_timeout}",
        "--agent-kwarg", f"tool_timeout_s={args.tool_timeout}",
        "--agent-kwarg", f"max_tool_timeout_s={args.max_tool_timeout}",
        "--agent-kwarg", f"require_self_check={str(args.require_self_check).lower()}",
        "--agent-kwarg", f"structured_output={args.structured_output}",
        "--agent-kwarg", f"policy_style={args.policy_style}",
        "--agent-kwarg", f"context_strategy={args.context_strategy}",
        "--agent-kwarg", f"transition_guard={str(args.transition_guard).lower()}",
        "--agent-kwarg", f"exploration_history={str(args.exploration_history).lower()}",
        "--agent-kwarg", f"enhanced_verify={str(args.enhanced_verify).lower()}",
        "--n-concurrent", "1", "--n-attempts", "1", "--no-cleanup", "--no-upload-results",
        "--global-agent-timeout-sec", str(args.wall_time + 30),
        "--global-test-timeout-sec", str(args.test_timeout),
        "--rebuild" if args.rebuild else "--no-rebuild"]
    for task in args.task:
        command += ["--task-id", task]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None,
        [str(project), str(tb_root), environment.get("PYTHONPATH", "")]))
    # Keep metadata outside the TB run path: an existing run path triggers TB resume.
    output.mkdir(parents=True, exist_ok=True)
    (output / (args.run_id + ".launch.json")).write_text(json.dumps(
        dict(command=command, tasks=args.task, dataset_path=str(dataset),
             grading="TB hidden tests after runtime submission"), indent=2), encoding="utf-8")
    return subprocess.call(command, cwd=project, env=environment)


if __name__ == "__main__":
    sys.exit(main())
