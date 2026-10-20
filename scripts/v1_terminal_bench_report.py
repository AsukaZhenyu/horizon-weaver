"""Join Terminal-Bench grades with v1 journals without changing either authority."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from horizon_weaver.app import read_run
from horizon_weaver.state import EventStore


def collect(root: Path) -> dict:
    trials = []
    for result_path in sorted(root.rglob("results.json")):
        results = json.loads(result_path.read_text(encoding="utf-8"))
        for grade in results.get("results", []):
            task_dir = result_path.parent / grade["task_id"] / grade["trial_name"]
            run_dir = task_dir / "agent-logs" / "horizon-v1"
            graph = read_run(run_dir) if (run_dir / "events.sqlite3").is_file() else None
            parser = grade.get("parser_results") or {}
            resolved = bool(grade.get("is_resolved") and parser and all(value == "passed" for value in parser.values()))
            row = dict(task=grade["task_id"], trial=grade["trial_name"], resolved=resolved,
                grading_valid=bool(parser), passed_checks=sum(value == "passed" for value in parser.values()),
                total_checks=len(parser), failed_checks=[name for name, value in parser.items() if value != "passed"],
                benchmark_failure=grade.get("failure_mode"), benchmark_results=str(result_path.resolve()),
                runtime_result=str((run_dir / "result.json").resolve()))
            if graph is not None:
                store = EventStore(run_dir / "events.sqlite3", graph.manifest.run_id, readonly=True)
                try:
                    events = store.read()
                finally:
                    store.close()
                row.update(runtime_termination=graph.result.termination.value if graph.result else None,
                    runtime_claimed_success=bool(graph.result and graph.result.verdict.passed),
                    steps=graph.result.steps if graph.result else graph.checkpoint.step if graph.checkpoint else 0,
                    usage=graph.usage.model_dump(), elapsed_s=graph.result.elapsed_s if graph.result else graph.elapsed_s,
                    invalid_actions=sum(event.kind == "ModelCallCompleted" and
                    json.loads(event.data_json)["decision"].get("action") is None for event in events),
                    code_hash=graph.manifest.code_hash)
            trials.append(row)
    return dict(scope="Selected local Terminal-Bench 0.2 tasks, not a full benchmark score",
                completed_trials=len(trials), resolved=sum(row["resolved"] for row in trials),
                unresolved=sum(not row["resolved"] for row in trials),
                runtime_false_successes=sum(row.get("runtime_claimed_success", False) and not row["resolved"] for row in trials),
                trials=trials)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = collect(args.root)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
