"""Fetch pinned source/data and create a math-only isolated environment."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = ROOT / "_work" / "mage"
SOURCES = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))

def run(*args, cwd=None):
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--download-wheels", action="store_true")
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    upstream = WORK / "upstream"
    if not upstream.exists():
        run("git", "clone", SOURCES["upstream"]["url"], upstream)
    actual = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if actual != SOURCES["upstream"]["revision"]:
        raise RuntimeError(f"Unexpected upstream revision: {actual}; do not reset a modified checkout.")
    patch = HERE / "patches" / "bounded-runtime.patch"
    if subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=upstream, capture_output=True).returncode:
        run("git", "apply", "--check", patch, cwd=upstream)
        run("git", "apply", patch, cwd=upstream)
    data_dir = WORK / "data"
    data_dir.mkdir(exist_ok=True)
    data_path = data_dir / "math.jsonl"
    dataset = SOURCES["dataset"]
    if not data_path.exists():
        url = f"https://huggingface.co/datasets/{dataset['repo']}/resolve/{dataset['revision']}/{dataset['file']}"
        urllib.request.urlretrieve(url, data_path)
    checksum = hashlib.sha256(data_path.read_bytes()).hexdigest()
    if checksum != dataset["sha256"]:
        raise RuntimeError("Dataset checksum mismatch")
    tasks = [json.loads(x) for x in data_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if len(tasks) != dataset["tasks"]:
        raise RuntimeError("Dataset count mismatch")
    python = WORK / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if args.install:
        if not python.exists():
            venv.create(WORK / ".venv", with_pip=True)
        run(python, "-m", "pip", "install", "-r", HERE / "requirements-math.lock.txt")
    wheel_dir = WORK / "linux-wheels"
    if args.download_wheels:
        run(python if python.exists() else sys.executable, "-m", "pip", "download",
            "-r", HERE / "requirements-math.lock.txt", "--dest", wheel_dir,
            "--only-binary=:all:", "--python-version", "3.12", "--implementation", "cp",
            "--abi", "cp312", "--platform", "manylinux_2_28_x86_64",
            "--platform", "manylinux_2_27_x86_64", "--platform", "manylinux_2_17_x86_64",
            "--platform", "manylinux2014_x86_64")
    wheel_lock = json.loads((HERE / "linux-wheels.lock.json").read_text(encoding="utf-8"))
    if {p.name for p in wheel_dir.glob("*.whl")} != {x["filename"] for x in wheel_lock}:
        raise RuntimeError("Linux wheel set mismatch; run prepare.py --download-wheels or inspect cache")
    for entry in wheel_lock:
        if hashlib.sha256((wheel_dir / entry["filename"]).read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Linux wheel checksum mismatch: {entry['filename']}")
    # Build context deliberately excludes .git, all datasets, and all private scorer files.
    stage = WORK / "image"
    stage.mkdir(exist_ok=True)
    # Only package requirements and Dockerfile enter the code image: no agent, task, gold, or logs.
    for name in ("Dockerfile", "requirements-math.lock.txt"):
        shutil.copy2(HERE / name, stage / name)
    shutil.copytree(wheel_dir, stage / "wheels", dirs_exist_ok=True)
    print(json.dumps({"upstream": actual, "dataset_sha256": checksum, "tasks": len(tasks),
                      "first_task_subtasks": len(tasks[0]["questions"]), "image_context": str(stage)}, indent=2))

if __name__ == "__main__":
    main()
