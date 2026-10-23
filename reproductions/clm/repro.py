#!/usr/bin/env python3
"""Reproduce the official single-agent CLM; this file adds no agent policy."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time
import tomllib
import traceback

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = ROOT / "_work" / "clm"
LOCK = json.loads((HERE / "upstream.lock.json").read_text())
CONFIG = HERE / "configs" / "local-sanity.json"


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def stop_process_tree(proc: subprocess.Popen) -> None:
    """Terminate only the new process session created by run_logged."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # A child can outlive its parent after SIGTERM; the group is still ours.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def run_logged(args: list[str], log: Path, cwd: Path | None = None,
               timeout_sec: float | None = None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"argv": args, "cwd": str(cwd or ROOT),
                                 "timeout_sec": timeout_sec}) + "\n")
        stream.flush()
        # Direct file output cannot block the timeout watchdog waiting for a newline.
        proc = subprocess.Popen(args, cwd=cwd or ROOT, stdout=stream,
                                stderr=subprocess.STDOUT,
                                start_new_session=(os.name == "posix"))
        print(f"Started pid={proc.pid}; raw log: {log}", flush=True)
        try:
            return proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            stop_process_tree(proc)
            stream.write(f"\n[supervisor timeout after {timeout_sec:.1f} seconds]\n")
            return 124
        except BaseException:
            stop_process_tree(proc)
            raise
        finally:
            if proc.poll() is None:
                stop_process_tree(proc)


def task_artifacts(task: Path) -> dict[str, str]:
    """Hash every packaged task file, including gold, without exposing its contents."""
    return {p.relative_to(task).as_posix(): sha(p)
            for p in sorted(task.rglob("*")) if p.is_file()}


def verify_task_artifacts(task: Path, expected: dict[str, str]) -> dict[str, str]:
    actual = task_artifacts(task)
    if actual != expected:
        changed = sorted(k for k in set(actual) | set(expected)
                         if actual.get(k) != expected.get(k))
        raise RuntimeError("Packaged task artifacts changed since prepare: " + ", ".join(changed))
    return actual


def cleanup_trial_containers(project: str) -> dict:
    """Match only Harbor's exact, unique compose project label for this run."""
    report = {"compose_project": project, "removed_containers": []}
    try:
        result = subprocess.run(["docker", "ps", "-aq", "--filter",
            f"label=com.docker.compose.project={project}"],
            capture_output=True, text=True, timeout=15, check=True)
        identifiers = result.stdout.split()
        if not all(re.fullmatch(r"[0-9a-f]{12,64}", x) for x in identifiers):
            raise RuntimeError("Unexpected container identifier in scoped cleanup")
        if identifiers:
            removed = subprocess.run(["docker", "rm", "--force", *identifiers],
                capture_output=True, text=True, timeout=20, check=True)
            report["removed_containers"] = removed.stdout.split()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def pinned_sources() -> list[dict]:
    rows = []
    for spec in LOCK["repositories"]:
        directory = ROOT / spec["directory"]
        got = output(["git", "-C", str(directory), "rev-parse", "HEAD"])
        if got != spec["commit"]:
            raise RuntimeError(f"{spec['name']} HEAD {got} != pinned {spec['commit']}")
        # A Windows checkout can have stale Git stat-cache entries under WSL.
        # Verify actual bytes against HEAD blobs rather than trusting that cache.
        tree = subprocess.check_output(["git", "-C", str(directory), "ls-tree",
                                        "-r", "-z", "HEAD"])
        verified = 0
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            header, raw_path = entry.split(b"\t", 1)
            mode, kind, expected = header.split()
            if kind != b"blob":
                raise RuntimeError("Unexpected upstream gitlink: inspect before reproduction")
            path = directory / os.fsdecode(raw_path)
            data = path.read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if actual != expected.decode():
                raise RuntimeError(f"Upstream file differs from pinned HEAD: {path}")
            verified += 1
        rows.append({**spec, "verified_head_blobs": verified,
                     "license_sha256": sha(directory / spec["license_file"])})
    return rows


def fetch() -> None:
    for spec in LOCK["repositories"]:
        directory = ROOT / spec["directory"]
        if not directory.exists():
            directory.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--no-tags",
                            spec["url"], str(directory)], check=True)
            subprocess.run(["git", "-C", str(directory), "checkout", "--detach",
                            spec["commit"]], check=True)
        got = output(["git", "-C", str(directory), "rev-parse", "HEAD"])
        if got != spec["commit"]:
            raise RuntimeError(f"Existing upstream is at {got}; refusing automatic reset")
    print(json.dumps(pinned_sources(), indent=2))


def packages() -> dict[str, str]:
    return dict(sorted((d.metadata["Name"], d.version) for d in metadata.distributions()
                       if d.metadata["Name"]))


def prepare() -> None:
    sources = pinned_sources()
    cfg = json.loads(CONFIG.read_text())
    instance = WORK / "instances" / LOCK["task"]["name"]
    task = WORK / "tasks" / LOCK["task"]["name"]
    # Never bless mutated existing gold/grader files by replacing their baseline hashes.
    previous_manifest = WORK / "preparation/manifest.json"
    if task.exists():
        if not previous_manifest.exists():
            raise RuntimeError("Unrecorded existing task; preserve it and prepare a new task directory")
        previous = json.loads(previous_manifest.read_text())
        verify_task_artifacts(task, previous["task_artifact_sha256"])
    commands = [
        [sys.executable, "-m", "contextbench.cli", "generate", "--family", "kv_store",
         "--level", "R1000", "--seed", "42", "--out-dir", str(instance), "--receipt"],
        [sys.executable, "-m", "contextbench.cli", "package", "--instance-dir", str(instance),
         "--out-dir", str(WORK / "tasks"), "--name", LOCK["task"]["name"],
         "--agent-timeout-sec", str(cfg["agent_timeout_sec"])]
    ]
    for index, command in enumerate(commands):
        if index == 1 and task.exists():
            continue
        if run_logged(command, WORK / "preparation" / f"command-{index}.log"):
            raise RuntimeError(f"Preparation command {index} failed")
    from contextbench.harbor.turn_source import canonical_sha256
    records = [json.loads(line) for line in (task / "environment/stream/chunks.jsonl")
               .read_text().splitlines() if line.strip()]
    actual = canonical_sha256(records)
    if actual != LOCK["task"]["stream_sha256"] or len(records) != LOCK["task"]["stream_count"]:
        raise RuntimeError("Generated task differs from the official task stream")
    task_config = tomllib.loads((task / "task.toml").read_text())
    if task_config["agent"]["timeout_sec"] != cfg["agent_timeout_sec"]:
        raise RuntimeError("Existing packaged task has another timeout; preserve it and use a new task directory")
    oracle = WORK / "oracle-self-check"
    if not oracle.exists():
        command = [sys.executable, "-m", "contextbench.cli", "run", "--instance-dir",
                   str(instance), "--out-dir", str(oracle)]
        if run_logged(command, WORK / "preparation" / "oracle-self-check.log"):
            raise RuntimeError("Official task/oracle self-check failed")
    manifest = {
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": LOCK["scope"], "sources": sources, "python": platform.python_version(),
        "packages": packages(),
        "dependency_lock_sha256": sha(HERE / "requirements-ubuntu-py312.lock.txt"),
        "transport_extra": "litellm[mcp]==1.106.2",
        "runtime_environment": {"CLM_LLM_MAX_RETRIES": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
        "task": LOCK["task"], "task_toml_sha256": sha(task / "task.toml"),
        "task_artifact_sha256": task_artifacts(task),
        "task_contract_budget": task_config["metadata"]["contextbench_budget_tokens"],
        "local_config": cfg,
        "model_calls": 0,
        "oracle_note": "The gold OracleAgent is only a generator/protocol/grader self-check, never an agent baseline.",
        "model_result": "not_run"
    }
    write_json(WORK / "preparation" / "manifest.json", manifest)
    print(json.dumps({"task": str(task), "stream_sha256": actual,
                      "model_calls": 0, "prepared": True}, indent=2))


def usage_accounting(trial: Path, usage: dict | None) -> dict:
    """Raw zero defaults never stand in for missing per-response usage."""
    trajectory = trial / "agent/trajectory.ctx.json"
    metrics = []
    export_check = {}
    if trajectory.exists():
        document = json.loads(trajectory.read_text())
        export_check = document.get("extra", {}).get("export_check", {})
        metrics = [step.get("metrics") or {}
                   for segment in document.get("segments", [])
                   for step in segment.get("steps", [])
                   if step.get("source") == "agent"]
    complete = [m for m in metrics
                if isinstance(m.get("prompt_tokens"), int) and m["prompt_tokens"] > 0
                and isinstance(m.get("completion_tokens"), int) and m["completion_tokens"] >= 0]
    calls = usage.get("n_lm_calls") if usage else None
    fully_known = (isinstance(calls, int) and calls == len(metrics) == len(complete)
                   and export_check.get("consistent") is True)
    return {
        "status": "complete" if fully_known else "unknown_or_incomplete",
        "logical_calls_started": calls,
        "configured_http_attempt_limit_per_logical_call": 1,
        "calls_with_complete_usage": len(complete),
        "calls_with_unknown_usage": max(calls - len(complete), 0) if isinstance(calls, int) else None,
        "recorded_tokens_known_calls": sum(m["prompt_tokens"] + m["completion_tokens"] for m in complete),
        "total_tokens_including_all_attempts": (
            sum(m["prompt_tokens"] + m["completion_tokens"] for m in complete) if fully_known else None),
        "official_export_check": export_check,
        "note": "Unknown request/response usage is not counted as zero; upstream aggregate zeros remain raw evidence only."
    }


def summarize(run_dir: Path) -> dict:
    rows = []
    for trial in sorted((run_dir / "trials").glob("*")):
        if not trial.is_dir():
            continue
        row = {"trial": trial.name}
        for name, rel in [("reward", "verifier/reward.json"),
                          ("grader", "verifier/contextbench_result.json"),
                          ("stream", "agent/interactive_stream.json"),
                          ("usage", "agent/usage.json"),
                          ("adapter", "agent/clm_stream.json")]:
            path = trial / rel
            if path.exists():
                row[name] = json.loads(path.read_text())
        timing_path = trial / "agent/timing.json"
        if timing_path.exists():
            timings = json.loads(timing_path.read_text())
            row["timing_seconds"] = {
                "llm_calls_sum": sum(t.get("llm_s", 0) for t in timings),
                "bash_sum": sum(t.get("bash_s", 0) for t in timings),
                "source": "official agent/timing.json",
                "coverage": "Recorded completed stages only; interrupted/failed calls can be absent."
            }
        snapshot_dir = trial / "agent/context_snapshots"
        row["context_snapshots"] = len(list(snapshot_dir.glob("turn-*.json")))
        row["usage_accounting"] = usage_accounting(trial, row.get("usage"))
        row["official_grading_present"] = "reward" in row and "grader" in row
        error = trial / "exception.txt"
        if error.exists():
            row["exception"] = error.read_text()[-4000:]
        row["trial_status"] = ("exception_not_a_model_score" if "exception" in row
                               else ("externally_scored" if row["official_grading_present"] else "unscored"))
        rows.append(row)
    result = {"run_dir": str(run_dir), "trials": rows, "cost_usd": None, "cost_status": "unknown for unpriced local alias; upstream raw zero is not a measured cost",
              "warning": "Reward is the official post-run external grader, not CLM self-evaluation. No score is inferred from a zero process exit code."}
    write_json(run_dir / "summary.json", result)
    return result


def real_run(api_base: str) -> int:
    cfg = json.loads(CONFIG.read_text())
    task = WORK / "tasks" / cfg["task"]
    run_dir = WORK / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True)
    trial_name = "hw-clm-" + run_dir.name.lower()
    # Verified Harbor 0.16.1 uses <trial_name>__env as the compose project.
    project = trial_name + "__env"
    started = time.monotonic()
    code, status, launched = 1, "preflight_failed", False
    manifest = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
                "config": cfg, "model_endpoint": api_base,
                "api_key_recorded": False, "trial_name": trial_name,
                "compose_project": project,
                "supervisor_timeout_sec": cfg["supervisor_timeout_sec"],
                "request_environment": {"CLM_LLM_MAX_RETRIES": "1",
                                        "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
                "request_attempts_per_logical_call": 1}
    write_json(run_dir / "launch-manifest.json", manifest)
    try:
        manifest["sources"] = pinned_sources()
        manifest["dependency_lock_sha256"] = sha(HERE / "requirements-ubuntu-py312.lock.txt")
        manifest["model_service_identity"] = json.loads((HERE / "configs/model-service-identity.json").read_text())
        transport = json.loads((WORK / "preparation/transport-check.json").read_text())
        if not transport["passed"] or transport["dependency_lock_sha256"] != manifest["dependency_lock_sha256"]:
            raise RuntimeError("Transport preflight missing or from another dependency lock")
        manifest["transport_preflight"] = transport
        preparation_path = WORK / "preparation/manifest.json"
        preparation = json.loads(preparation_path.read_text())
        manifest["packages"] = packages()
        if manifest["packages"] != preparation["packages"]:
            raise RuntimeError("Runtime packages differ from preparation; refresh the environment manifest")
        manifest["task_artifact_sha256"] = verify_task_artifacts(
            task, preparation["task_artifact_sha256"])
        manifest["preparation_manifest"] = str(preparation_path)
        subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                       check=True, timeout=30)
        # Model HTTP calls run on this Ubuntu host; only bash/tool commands run inside Docker.
        import urllib.request
        with urllib.request.urlopen(api_base.rstrip("/") + "/models", timeout=15) as response:
            models = json.load(response)
        model_id = cfg["model"].split("/", 1)[1]
        if model_id not in [m.get("id") for m in models.get("data", [])]:
            raise RuntimeError(f"Endpoint does not advertise the pinned local model {model_id}")
        image = subprocess.check_output(["docker", "image", "inspect", "python:3.12-slim"],
                                        text=True, timeout=30)
        manifest["docker_images"] = json.loads(image)
        args = [str(Path(sys.executable).with_name("harbor")), "trials", "start",
                "-p", str(task), "-e", "docker", "-a", cfg["agent"],
                "-m", cfg["model"], "--trial-name", trial_name,
                "--trials-dir", str(run_dir / "trials"),
                "--agent-kwarg", f"api_base={api_base}"]
        for key, value in cfg["agent_kwargs"].items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            args += ["--agent-kwarg", f"{key}={text}"]
        os.environ["OPENAI_API_KEY"] = "EMPTY"  # Never forward a real API key.
        # Official CLM reads this at import time; litellm already has num_retries=0.
        os.environ["CLM_LLM_MAX_RETRIES"] = "1"
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        manifest.update(argv=args, status="running")
        write_json(run_dir / "launch-manifest.json", manifest)
        remaining = cfg["supervisor_timeout_sec"] - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Run deadline exhausted during preflight")
        launched = True
        code = run_logged(args, run_dir / "console.log", timeout_sec=remaining)
        status = "timeout" if code == 124 else ("finished" if code == 0 else "process_failed")
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "error"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        (run_dir / "supervisor-error.txt").write_text(traceback.format_exc())
        if isinstance(exc, KeyboardInterrupt):
            code = 130
    finally:
        manifest["cleanup"] = (cleanup_trial_containers(project) if launched
                               else {"status": "no_trial_process_started"})
        manifest.update(status=status, elapsed_seconds=time.monotonic() - started,
                        finished_at_utc=datetime.now(timezone.utc).isoformat(),
                        return_code=code)
        write_json(run_dir / "launch-manifest.json", manifest)
        write_json(run_dir / "process-exit.json", {
            k: manifest[k] for k in ("return_code", "status", "finished_at_utc", "elapsed_seconds")})
        try:
            print(json.dumps(summarize(run_dir), indent=2))
        except Exception:
            (run_dir / "summary-error.txt").write_text(traceback.format_exc())
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch")
    sub.add_parser("prepare")
    sub.add_parser("lock-dependencies")
    launch = sub.add_parser("run")
    launch.add_argument("--api-base", required=True)
    summary = sub.add_parser("summarize")
    summary.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        fetch()
    elif args.command == "prepare":
        prepare()
    elif args.command == "lock-dependencies":
        lines = [f"{name}=={version}" for name, version in packages().items()
                 if name.lower() not in {"clm", "contextbench"}]
        (HERE / "requirements-ubuntu-py312.lock.txt").write_text("\n".join(lines) + "\n")
    elif args.command == "run":
        return real_run(args.api_base)
    else:
        print(json.dumps(summarize(args.run_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
