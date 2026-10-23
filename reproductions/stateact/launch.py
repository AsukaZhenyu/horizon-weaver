"""Measure complete child-process wall time for future runs; no policy changes."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE.parents[1] / ".horizon/reproductions/stateact"
args = sys.argv[1:]
if not args or args[0] != "run" or "--trial" not in args:
    raise SystemExit("Usage: launch.py run --method react|stateact --trial UNIQUE [--start N --num-envs N]")
trial = args[args.index("--trial") + 1]
if not trial or Path(trial).name != trial:
    raise SystemExit("Trial must be a single directory name")
directory = CACHE / "command-timing"
directory.mkdir(parents=True, exist_ok=True)
target = directory / (trial + ".json")
if target.exists():
    raise SystemExit("Refusing to overwrite existing timing record")
start_utc = datetime.now(timezone.utc).isoformat()
started = time.perf_counter()
exit_code = None
try:
    exit_code = subprocess.run([sys.executable, str(HERE / "run.py"), *args]).returncode
finally:
    target.write_text(json.dumps({
        "trial": trial, "started_utc": start_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "process_wall_clock_s": time.perf_counter() - started,
        "exit_code": exit_code,
        "scope": "Entire child Python process including imports, env setup and cleanup; excludes parent interpreter and WSL startup"
    }, indent=2) + "\n")
raise SystemExit(exit_code if exit_code is not None else 1)
