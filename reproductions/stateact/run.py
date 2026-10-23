"""Run unmodified upstream StateAct/ReAct with an explicitly labelled HTTP backend."""
import argparse
import hashlib
import json
import os
import platform
import runpy
import shutil
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CACHE = ROOT / ".horizon" / "reproductions" / "stateact"
UPSTREAM = CACHE / "upstream"
RUNNER = UPSTREAM / "alfworld_runs"
CONFIG = json.loads((HERE / "config.json").read_text())


def install_transport(log_path):
    os.environ["STATEACT_BACKEND_JSON"] = json.dumps(CONFIG["backend"])
    os.environ["STATEACT_TRANSPORT_LOG"] = str(log_path)
    from transport import LlamaCppTransport
    shim = types.ModuleType("llamp.llms.local")
    shim.VLLMChat = LlamaCppTransport
    sys.modules["llamp.llms.local"] = shim
    return LlamaCppTransport


def check_source():
    actual = subprocess.check_output(["git", "-C", str(UPSTREAM),
                                      "rev-parse", "HEAD"], text=True).strip()
    if actual != CONFIG["upstream"]["commit"]:
        raise RuntimeError(f"Wrong upstream SHA: {actual}")
    subprocess.run(["git", "-c", "core.autocrlf=true", "-C", str(UPSTREAM), "diff", "--quiet"], check=True)
    return actual


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "run", "score"])
    parser.add_argument("--method", choices=["react", "stateact"], default="stateact")
    parser.add_argument("--trial", default="paired-smoke-stateact-001")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--num-envs", type=int, default=1)
    args = parser.parse_args()
    source_sha = check_source()
    sys.path.insert(0, str(RUNNER))
    os.environ["ALFWORLD_DATA"] = str(CACHE / "data")
    transport_class = install_transport(CACHE / "preflight-transport.jsonl")
    if args.command == "preflight":
        os.chdir(RUNNER)
        module = runpy.run_path(str(RUNNER / "alfworld_run.py"))
        transport = transport_class(model=CONFIG["backend"]["model"])
        rows = []
        for method in ["react", "stateact"]:
            for kind in ["clean", "cool", "examine", "heat", "put", "puttwo"]:
                agent_type = "ours-text" if method == "stateact" else "react"
                name, example, base, n = module["get_prompt_example"](
                    agent_type=agent_type, env_type=kind, prompt_ids=[1, 0],
                    version=1, keys_to_use=CONFIG["benchmark"]["keys_to_use"])
                messages = module["generate_prompt_from_example"](
                    example, number_of_examples=n, base_prompt=base)
                prompt = "".join(row["content"] for row in messages)
                tokens = transport.render_and_count(prompt)
                rows.append({"method": method, "kind": kind, "chars": len(prompt),
                             "template_tokens": tokens,
                             "output_budget": CONFIG["backend"]["max_tokens"],
                             "fits": tokens + CONFIG["backend"]["max_tokens"] <=
                                     CONFIG["backend"]["context_tokens"]})
        report = {"upstream_sha": source_sha, "config": CONFIG, "rows": rows}
        (CACHE / "preflight.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(rows, indent=2))
        if not all(row["fits"] for row in rows):
            raise RuntimeError("Full official few-shot exceeds context; not running.")
        return

    work = CACHE / "runs" / args.trial
    if args.command == "score":
        os.chdir(work)
        sys.argv = ["get_score.py", "--trial_name", args.trial]
        runpy.run_path(str(RUNNER / "get_score.py"), run_name="__main__")
        return
    if work.exists():
        raise RuntimeError(f"Refusing to append duplicate trial rows: {work}")
    work.mkdir(parents=True)
    shutil.copyfile(RUNNER / "base_config.yaml", work / "base_config.yaml")
    (work / "prompts").symlink_to(RUNNER / "prompts", target_is_directory=True)
    (work / "game_logs").mkdir()
    os.environ["STATEACT_TRANSPORT_LOG"] = str(work / "transport.jsonl")
    os.environ["STATEACT_START_INDEX"] = str(args.start)
    manifest = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "upstream_sha": source_sha, "method": args.method,
        "task_start_index": args.start, "num_envs": args.num_envs,
        "python": platform.python_version(), "config": CONFIG,
        "adapter_sha256": hashlib.sha256((HERE / "transport.py").read_bytes()).hexdigest(),
        "native_runner_and_prompts_unmodified": True,
        "expert_or_answer_input": False,
        "deviations": [
            "Qwen3.5 9B Q4 llama.cpp HTTP replaces Qwen2.5 14B BF16 vLLM.",
            "Thinking disabled; completion cap 1024 instead of 2000.",
            "Full prompt retained with explicit token-budget errors; no legacy character slicing.",
        ],
    }
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    from audit import install_environment_audit
    install_environment_audit(work / "environment.jsonl", CACHE / "data")
    os.chdir(work)
    sys.argv = [
        "alfworld_run.py", "--agent", "ours-text" if args.method == "stateact" else "react",
        "--llm_type", "VLLMChat", "--model", CONFIG["backend"]["model"],
        "--trial_name", args.trial, "--start_index", str(args.start),
        "--num_envs", str(args.num_envs), "--prompt_ids", "1", "0",
        "--eval_split", CONFIG["benchmark"]["split"],
        "--apply_correction", "--force_run", "--force_model",
        "--max_model_len", "16000", "--seed", "42",
    ]
    if args.method == "stateact":
        sys.argv += ["--keys_to_use", json.dumps(CONFIG["benchmark"]["keys_to_use"])]
    runpy.run_path(str(RUNNER / "alfworld_run.py"), run_name="__main__")


if __name__ == "__main__":
    main()
