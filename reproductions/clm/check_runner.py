#!/usr/bin/env python3
"""Offline supervisor/accounting checks; no Docker commands and no model calls."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from repro import WORK, run_logged, task_artifacts, verify_task_artifacts, usage_accounting, write_json


def main() -> int:
    assert os.name == "posix", "The reproduction runner is validated on Ubuntu."
    with tempfile.TemporaryDirectory(prefix="hw-clm-runner-") as temporary:
        root = Path(temporary)
        artifact = root / "task"
        artifact.mkdir()
        (artifact / "gold.json").write_text('{"private": "fixture only"}')
        pinned = task_artifacts(artifact)
        assert verify_task_artifacts(artifact, pinned) == pinned
        (artifact / "gold.json").write_text('{"private": "changed fixture"}')
        try:
            verify_task_artifacts(artifact, pinned)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Modified task artifact was not refused")
        pid_path = root / "child.pid"
        command = [
            sys.executable, "-c",
            "import pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)",
            str(pid_path),
        ]
        status = run_logged(command, root / "deadline.log", timeout_sec=1)
        assert status == 124 and pid_path.exists()
        child = int(pid_path.read_text())
        stat = Path(f"/proc/{child}/stat")
        if stat.exists():
            assert stat.read_text().split()[2] == "Z", "Child still running after group cleanup"

        trial = root / "trial"
        (trial / "agent").mkdir(parents=True)
        assert usage_accounting(trial, {"n_lm_calls": 1, "prompt_tokens": 0})[
            "total_tokens_including_all_attempts"] is None
        trajectory = {
            "extra": {"export_check": {"consistent": True}},
            "segments": [{"steps": [{"source": "agent", "metrics": {
                "prompt_tokens": 100, "completion_tokens": 10}}]}],
        }
        write_json(trial / "agent/trajectory.ctx.json", trajectory)
        known = usage_accounting(trial, {"n_lm_calls": 1})
        assert known["status"] == "complete" and known["total_tokens_including_all_attempts"] == 110
        partial = usage_accounting(trial, {"n_lm_calls": 2})
        assert partial["calls_with_unknown_usage"] == 1
        assert partial["total_tokens_including_all_attempts"] is None

        env = dict(os.environ, CLM_LLM_MAX_RETRIES="1")
        retry = subprocess.check_output(
            [sys.executable, "-c", "from clm_harness.clm_agent.harness import _MAX_RETRIES; print(_MAX_RETRIES)"],
            env=env, text=True).strip()
        assert retry == "1"
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "checks": ["task artifact tampering refused without printing gold",
                   "whole subprocess deadline fires and terminates its process group",
                   "missing/failed usage remains unknown rather than zero",
                   "known per-call usage is distinguished from partial usage",
                   "actual upstream import reads exactly one configured HTTP attempt"],
        "model_calls": 0, "docker_commands": 0, "passed": True,
    }
    write_json(WORK / "preparation/runner-check.json", report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
