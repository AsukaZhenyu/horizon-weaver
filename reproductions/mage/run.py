"""One host orchestrator attempt, isolated code containers, then a separate gold judge."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = ROOT / "_work" / "mage"

def validate_local_endpoint(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    local = host in {"localhost", "host.docker.internal"}
    try:
        local = local or ipaddress.ip_address(host).is_private
    except ValueError:
        pass
    if parsed.scheme != "http" or not local or parsed.username or parsed.password or not parsed.path.rstrip("/").endswith("/v1"):
        raise ValueError("Only an explicitly supplied local/private HTTP /v1 endpoint is allowed.")

def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def public_task(task):
    return {key: task[key] for key in ("id", "questions", "backgrounds")}

def cleanup_code(run_id):
    found = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=hw.mage.run={run_id}"],
                           capture_output=True, text=True, timeout=15)
    if found.returncode:
        raise RuntimeError(f"Docker cleanup query failed ({found.returncode}): {found.stderr[-1000:]}")
    identifiers = found.stdout.split()
    if identifiers and not all(re.fullmatch(r"[0-9a-f]{12,64}", x) for x in identifiers):
        raise RuntimeError("Unexpected Docker cleanup identifiers; refusing removal")
    if identifiers:
        removed = subprocess.run(["docker", "rm", "--force", *identifiers],
                                 capture_output=True, text=True, timeout=20)
        if removed.returncode:
            raise RuntimeError(f"Docker cleanup failed ({removed.returncode}): {removed.stderr[-1000:]}")

def cleanup_preserving_manifest(run_id, manifest):
    """Report cleanup failure without overwriting the experiment result or blocking its save."""
    try:
        cleanup_code(run_id)
    except Exception as exc:
        manifest["cleanup_error"] = f"{type(exc).__name__}: {exc}"

def bounded_process(command, log_path, timeout):
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1"))
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.kill()
            proc.wait(timeout=15)
            return 124

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-sha256", help="Controller-supplied local model artifact hash")
    parser.add_argument("--server-context", type=int, default=16384)
    parser.add_argument("--config", type=Path, default=HERE / "configs" / "math1-sanity.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    validate_local_endpoint(args.base_url)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if (config["num"], config["workers"], config["max_retry_num"], config["exception_task_attempts"]) != (1, 1, 0, 1):
        raise ValueError("Only math1, single worker, one attempt, no gold-assisted retries are supported.")
    if config["request_attempts"] != 1:
        raise ValueError("Sanity does not retry failed model requests.")
    sources = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
    upstream = WORK / "upstream"
    if subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip() != sources["upstream"]["revision"]:
        raise ValueError("Unexpected upstream revision")
    patch = HERE / "patches" / "bounded-runtime.patch"
    subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=upstream, check=True)
    actual_diff = subprocess.check_output(["git", "diff", "--no-ext-diff"], cwd=upstream)
    if actual_diff != patch.read_bytes():
        raise ValueError("Unrecorded upstream changes; inspect and explicitly regenerate the engineering patch")
    data = WORK / "data" / "math.jsonl"
    if hashlib.sha256(data.read_bytes()).hexdigest() != sources["dataset"]["sha256"]:
        raise ValueError("Dataset checksum mismatch")
    task = [json.loads(line) for line in data.read_text(encoding="utf-8").splitlines() if line.strip()][config["task_index"]]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = WORK / "runs" / stamp
    public, solver, private = [output / name for name in ("public", "solver", "private")]
    for directory in (public, solver, private):
        directory.mkdir(parents=True)
    write(public / "task.json", public_task(task))
    write(private / "task.json", task)
    effective = dict(config, model=args.model, base_url=args.base_url, run_id=stamp)
    write(public / "config.json", effective)
    write(private / "config.json", effective)
    python = WORK / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    command = [
        str(python), str(HERE / "worker.py"), "solver", "--upstream", str(upstream),
        "--config", str(public / "config.json"), "--task", str(public / "task.json"),
        "--output", str(solver),
    ]
    manifest = dict(sources=sources, config=effective, solver_command=command,
                    model_artifact_sha256=args.model_sha256, server_context=args.server_context,
                    gold_in_solver_input_or_shared_state=False, oracle_feedback=False,
                    generated_code="separate no-network Docker container; no host mounts/socket",
                    exception_task_attempts=1, status="prepared" if args.dry_run else "running")
    write(output / "manifest.json", manifest)
    if args.dry_run:
        print(output)
        print(json.dumps(command))
        return
    try:
        image_info = subprocess.check_output(
            ["docker", "--context", config["docker_context"], "image", "inspect",
             config["code_image"], "--format", "{{.Id}}"], text=True, timeout=20).strip()
    except Exception as exc:
        manifest.update(status="preflight_failed", error=f"{type(exc).__name__}: {exc}")
        write(output / "manifest.json", manifest)
        print(output)
        raise SystemExit(1)
    manifest["code_image_id"] = image_info
    # Every generated-code call uses this immutable local image ID.
    effective["code_image"] = image_info
    write(public / "config.json", effective)
    write(private / "config.json", effective)
    write(output / "manifest.json", manifest)
    os.environ["DOCKER_CONTEXT"] = config["docker_context"]
    started = time.monotonic()
    try:
        solver_exit = bounded_process(command, output / "solver_stdout.log",
                                      min(config["solver_timeout_s"], config["total_timeout_s"]))
        manifest["solver_exit"] = solver_exit
        manifest["status"] = "solver_failed"
        if solver_exit == 0:
            remaining = config["total_timeout_s"] - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired("total run", config["total_timeout_s"])
            judge_command = [
                str(python), str(HERE / "worker.py"), "judge", "--upstream", str(upstream),
                "--config", str(private / "config.json"), "--task", str(private / "task.json"),
                "--predictions", str(solver / "predictions.json"), "--output", str(private),
            ]
            judge_exit = bounded_process(judge_command, output / "judge_stdout.log",
                                         min(remaining, config["judge_timeout_s"]))
            manifest["judge_exit"] = judge_exit
            manifest["status"] = "completed" if judge_exit == 0 else "judge_failed"
    except subprocess.TimeoutExpired:
        manifest["status"] = "timeout"
    finally:
        cleanup_preserving_manifest(stamp, manifest)
        costs = {}
        for role, directory in (("solver", solver), ("judge", private)):
            path = directory / f"{role}_usage.json"
            if path.exists():
                usage = json.loads(path.read_text(encoding="utf-8"))
                costs[role] = {key: usage[key] for key in ("num_calls", "attempted_calls", "total_prompt_tokens", "total_completion_tokens", "total_tokens", "elapsed_seconds")}
        costs["total_tokens"] = sum(costs[r]["total_tokens"] for r in ("solver", "judge") if r in costs)
        costs["calls_with_unknown_usage"] = sum(costs[r]["attempted_calls"] - costs[r]["num_calls"] for r in ("solver", "judge") if r in costs)
        write(output / "costs.json", costs)
        if (private / "scores.json").exists():
            write(output / "scores.json", json.loads((private / "scores.json").read_text(encoding="utf-8")))
        manifest["elapsed_seconds"] = time.monotonic() - started
        write(output / "manifest.json", manifest)
        print(output)
        print(json.dumps({k: v for k, v in manifest.items() if k in ("status", "solver_exit", "judge_exit", "elapsed_seconds")}))
    if manifest["status"] != "completed":
        raise SystemExit(1)

if __name__ == "__main__":
    main()
