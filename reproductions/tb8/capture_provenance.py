"""Read-only local TB8 preflight; the only write is its JSON audit under _work/tb8."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

WORKTREE = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = (WORKTREE / "_work/tb8").resolve()
TASK_IDS = (
    "hello-world", "fix-permissions", "log-summary", "regex-log",
    "cancel-async-tasks", "jq-data-processing", "sqlite-db-truncate", "cprofiling-python",
)


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def command(args: list[str], timeout: float = 30) -> str:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, env=env, check=False)
    if result.returncode:
        raise RuntimeError(f"exit={result.returncode}: {result.stderr[-2000:]}")
    return result.stdout


def capture(protocol_path: Path) -> dict[str, Any]:
    protocol = json.loads(protocol_path.read_text(encoding="utf-8-sig"))
    checks: list[dict[str, Any]] = []
    blocked: list[str] = []

    def check(name: str, success: bool, **details: Any) -> None:
        checks.append(dict(name=name, success=bool(success), **details))
        if not success:
            blocked.append(name)

    def files(name: str, root: Path, expected: dict[str, str]) -> None:
        entries = []
        for relative, digest in expected.items():
            path = root / relative
            try:
                actual = sha256(path)
                entries.append(dict(path=str(path), expected_sha256=digest, sha256=actual,
                                    success=(actual == digest)))
            except OSError as error:
                entries.append(dict(path=str(path), success=False, error=str(error)))
        check(name, bool(entries) and all(item["success"] for item in entries),
              files=entries, count=len(entries))

    tb = Path(protocol["terminal_bench"]["root"]).resolve()
    task_dir = Path(protocol["terminal_bench"].get("task_dir", tb / "original-tasks"))
    check("schema_version", protocol.get("schema_version") == 1)
    check("fixed_eight_task_ids", tuple(task["id"] for task in protocol["tasks"]) == TASK_IDS)
    check("legacy_reference_summary", protocol["legacy"]["summary"]["resolved"] == 4
          and protocol["legacy"]["summary"]["normal_submission_and_resolved"] == 2
          and protocol["legacy"]["summary"]["passed_checks"] == 25)
    check("native_tb_python_exists", Path(protocol["terminal_bench"]["python"]).is_file())
    try:
        head = command(["git", "-C", str(tb), "rev-parse", "HEAD"]).strip()
        tracked = command(["git", "-C", str(tb), "status", "--porcelain=v1", "--untracked-files=no"])
        dirty_paths = sorted(line[3:].replace("\\", "/") for line in tracked.splitlines())
        expected_dirty = sorted(protocol["terminal_bench"]["expected_dirty_paths"])
        check("tb_git_head", head == protocol["terminal_bench"]["head"], actual=head,
              expected=protocol["terminal_bench"]["head"])
        check("tb_expected_dirty_compatibility_only", dirty_paths == expected_dirty,
              actual=dirty_paths, expected=expected_dirty)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        check("tb_git_inspection", False, error=str(error))
    files("original_task_sha256", tb, protocol["terminal_bench"]["task_files"])
    files("preexisting_compatibility_sha256", tb, protocol["terminal_bench"]["compatibility_files"])
    files("native_terminus_sha256", tb, protocol["terminal_bench"]["native_terminus_files"])
    files("legacy_source_snapshot_sha256", Path(protocol["legacy"]["source_dir"]),
          protocol["legacy"]["source_files"])
    files("legacy_evidence_sha256", Path(), protocol["legacy"]["evidence_files"])

    initial_states = []
    dockerfiles = []
    for task in protocol["tasks"]:
        try:
            ir_path = Path(task["initial_ir_file"])
            ir = json.loads(ir_path.read_text(encoding="utf-8"))
            state = next(item for item in ir["states"] if item["id"] == ir["initial_state"])
            initial_states.append(dict(task=task["id"], state_id=ir["initial_state"],
                workspace_entries=len(state["snapshot"]["files"]),
                success=(sha256(ir_path) == task["initial_ir_sha256"]
                         and ir["initial_state"] == task["initial_state"])))
        except (OSError, ValueError, KeyError, StopIteration) as error:
            initial_states.append(dict(task=task["id"], success=False, error=str(error)))
        try:
            text = (task_dir / task["id"] / "Dockerfile").read_text(encoding="utf-8")
            lines = [line.strip() for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
            copies = [line for line in lines if re.match(r"^(COPY|ADD)\s", line, re.I)]
            suspicious = [line for line in copies if re.search(
                r"\btests?\b|run-tests|\bsolution\b|\s[.*]\s|\[\s*[\"']\.[\"']", line, re.I)]
            dockerfiles.append(dict(task=task["id"], success=not suspicious,
                from_lines=[line for line in lines if line.upper().startswith("FROM ")],
                copy_add_lines=copies, suspicious_copy_add=suspicious, inspection="static Dockerfile only"))
        except OSError as error:
            dockerfiles.append(dict(task=task["id"], success=False, error=str(error)))
    check("legacy_initial_workspace_ir", all(item["success"] for item in initial_states), states=initial_states,
          scope=protocol["legacy"]["initial_state_scope"])
    check("hidden_tests_dockerfile_static_audit", all(item["success"] for item in dockerfiles),
          tasks=dockerfiles, limitation=protocol["docker"]["hidden_tests_check"],
          runtime_hidden_test_paths_inspected=False)

    try:
        # Docker Desktop's name-based GET route can report NoSuchImage even
        # when ID lookup and container creation work. Inspect only the pinned
        # immutable IDs, then independently require every original tag in the
        # returned RepoTags with its exact expected ID. No tag is changed.
        image_ids = list(dict.fromkeys(protocol["docker"]["images"].values()))
        images = json.loads(command(["docker", "image", "inspect", *image_ids]))
        by_tag = {tag: image for image in images for tag in image.get("RepoTags") or []}
        image_rows = []
        for tag, expected in protocol["docker"]["images"].items():
            image = by_tag.get(tag, {})
            image_rows.append(dict(tag=tag, id=image.get("Id"), expected_id=expected,
                success=image.get("Id") == expected, repo_digests=image.get("RepoDigests"),
                created=image.get("Created"), working_dir=image.get("Config", {}).get("WorkingDir")))
        check("cached_docker_image_ids", all(item["success"] for item in image_rows), images=image_rows,
              unique_pinned_ids=image_ids,
              inspection_route="Pinned full IDs, deduplicated; validate all original tags through RepoTags",
              name_lookup_anomaly="Controller and prior preflight observed NoSuchImage for name-based GET while ID-based GET succeeds",
              name_lookup_probe_performed=False, id_route_fallback=True,
              image_or_tag_mutation_performed=False)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        check("cached_docker_image_ids", False, error=str(error))

    model = protocol["model"]
    files("model_and_binary_sha256", Path(), {
        model["file"]: model["sha256"], model["binary"]: model["binary_sha256"]})
    files("controller_launch_manifest_sha256", Path(), {
        model["service_manifest"]: model["service_manifest_sha256"]})
    try:
        launch = json.loads(Path(model["service_manifest"]).read_text(encoding="utf-8-sig"))
        check("controller_launch_arguments_match_r13", launch["arguments"] == model["historical_arguments"],
              manifest=launch, live_process_or_http_probed=False)
    except (OSError, ValueError, KeyError) as error:
        check("controller_launch_arguments_match_r13", False, error=str(error))

    implementation = {}
    for name in ("protocol.json", "capture_provenance.py", "terminus_adapter.py", "run.py"):
        path = Path(__file__).parent / name
        if path.is_file():
            implementation[name] = sha256(path)
    return dict(schema_version=1, protocol_id=protocol.get("protocol_id"),
        captured_at=datetime.now(timezone.utc).isoformat(), success=not blocked, blocked=blocked,
        protocol_path=str(protocol_path.resolve()), implementation_sha256=implementation, checks=checks,
        execution=dict(python=sys.version, executable=sys.executable, platform=sys.platform),
        limitations=["No model request or live service health check was made.",
            "Only cached Docker image metadata and Dockerfiles were inspected; no container was created.",
            "Static absence of test COPY instructions is not proof of hidden-test absence in cached base-image layers.",
            "Historical workspace hashes are not complete container initial-state hashes.",
            "This eight-task development set is not a held-out generalization evaluation."])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=Path(__file__).with_name("protocol.json"))
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT / "preflight.json")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(OUTPUT_ROOT) or output == OUTPUT_ROOT:
        parser.error(f"--output must be a file inside {OUTPUT_ROOT}")
    try:
        report = capture(args.protocol)
    except Exception as error:
        report = dict(schema_version=1, captured_at=datetime.now(timezone.utc).isoformat(),
            success=False, blocked=["preflight_exception"], error=f"{type(error).__name__}: {error}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(success=report["success"], blocked=report["blocked"], output=str(output)), ensure_ascii=False))
    return 0 if report["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
