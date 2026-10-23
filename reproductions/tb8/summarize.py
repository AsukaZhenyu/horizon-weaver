"""Read one TB8 run only; write a partial-safe analysis JSON under _work/tb8/analysis."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = (ROOT / "_work/tb8/analysis").resolve()


def read_json(path: Path, warnings: list[str]) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        warnings.append(f"{path}: {type(error).__name__}: {error}")
        return None


def events(path: Path, warnings: list[str]) -> list[dict]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        warnings.append(f"{path}: {error}")
        return []
    rows = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            warnings.append(f"{path}: unreadable JSONL line {index + 1}; possibly an in-flight final line")
    return rows


def elapsed(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (ValueError, TypeError):
        return None


def transport_metrics(rows: list[dict], summary: dict, method: str) -> dict:
    starts = [row for row in rows if row.get("event") == "http_start"]
    ended = {row.get("request_number"): row for row in rows
             if row.get("event") in ("http_end", "http_error")}
    known = [row for row in ended.values() if row.get("usage_known") is True
             and isinstance(row.get("usage"), dict)]
    observed_input = sum(row["usage"].get("prompt_tokens") or 0 for row in known)
    observed_output = sum(row["usage"].get("completion_tokens") or 0 for row in known)
    observed_total = sum(row["usage"].get("total_tokens") or 0 for row in known)
    pending = sum(row.get("request_number") not in ended for row in starts)
    unknown_finished = sum(row.get("usage_known") is not True for row in ended.values())
    sends = [row for row in rows if row.get("event") == "session_send"]
    marker_errors = sum(row.get("event") == "marker_logging_error" for row in rows)
    cleanup_errors = []
    foreground = summary.get("foreground_cleanup") or {}
    if foreground.get("error") or (foreground.get("attempted") and foreground.get("shell_return_confirmed") is False):
        cleanup_errors.append(dict(operation="foreground", details=foreground))
    exception = summary.get("exception_cleanup") or {}
    if exception.get("error") or exception.get("fallback_error") or (exception.get("attempted") and
            exception.get("confirmed_stopped") is False and not exception.get("task_container_stopped_fallback")):
        cleanup_errors.append(dict(operation="exception_cleanup", details=exception))
    process = summary.get("process") or {}
    if process.get("cleanup_confirmed") is False:
        cleanup_errors.append(dict(operation="owned_process_cleanup", details=process))
    discrepancies = []
    for field, observed in (("http_attempts", len(starts)), ("known_prompt_tokens", observed_input),
                            ("known_completion_tokens", observed_output), ("known_total_tokens", observed_total)):
        if rows and field in summary and summary[field] != observed:
            discrepancies.append(dict(field=field, summary=summary[field], transport=observed))
    is_mini = method in ("mini", "mini-swe-agent")
    return dict(http_attempts=summary.get("http_attempts", len(starts)),
        http_attempts_observed=len(starts),
        known_input_tokens=summary.get("known_prompt_tokens", observed_input),
        known_output_tokens=summary.get("known_completion_tokens", observed_output),
        known_total_tokens=summary.get("known_total_tokens", observed_total),
        unknown_usage_requests=max(summary.get("unknown_usage_requests", 0), unknown_finished + pending),
        pending_usage_requests=max(summary.get("pending_usage_requests", 0), pending),
        http_error_attempts=sum(row.get("event") == "http_error" or
            (isinstance(row.get("status_code"), int) and row["status_code"] >= 400) for row in ended.values()),
        http_duration_seconds_observed=sum(row.get("duration_seconds", 0) or 0 for row in ended.values()),
        stop=summary.get("loop_end_reason") or summary.get("stop_reason"),
        exploration_seconds=summary.get("elapsed_seconds"),
        exploration_seconds_scope="Adapter budget elapsed; Terminus final summary can include foreground cleanup. Not full harness wall time.",
        observed_elapsed_seconds=max((row.get("elapsed_seconds", 0) or 0 for row in rows), default=None),
        setup_seconds=summary.get("setup_seconds", None if is_mini else 0),
        setup_seconds_source="mini adapter" if is_mini else "not applicable: Terminus has no separate adapter installation phase",
        setup_exit_code=summary.get("setup_exit_code"),
        total_setup_and_agent_seconds=summary.get("total_setup_and_agent_seconds"),
        episodes=summary.get("episodes"),
        native_exact_command_repeats=summary.get("repeated_commands", None if is_mini else
            sum(row.get("previous_exact_count", 0) > 0 for row in sends)),
        native_sent_commands=summary.get("sent_commands", None if is_mini else len(sends)),
        native_repetition_definition=summary.get("repeated_commands_definition") or
            ("Exact native mini command strings; unavailable until its summary" if is_mini else
             "Native session_send str(keys).strip() hash equality; no state or semantic equivalence"),
        repetitions_comparable_to_legacy_typed_actions=False,
        cleanup_error_count=len(cleanup_errors), cleanup_errors=cleanup_errors,
        cleanup_error_definition="At most one failed/error indicator per summary cleanup operation; operations can concern one incident",
        marker_error_count=marker_errors, metric_discrepancies=discrepancies)


def legacy_reference(path: Path, warnings: list[str]) -> dict:
    file = path / "effective-overview.json" if path.is_dir() else path
    report = read_json(file, warnings)
    if not isinstance(report, dict) or not isinstance(report.get("trials"), list):
        return dict(source=str(file), error="No readable effective-overview trials; not merged into run scores")
    rows = []
    for row in report["trials"]:
        graded = row.get("grading_valid") is True
        passed = row.get("resolved") if graded else None
        repeats = row.get("repetitions", {}).get("totals", {})
        rows.append(dict(task=row["task"], external_pass=passed, completed_grading=graded,
            checks_passed=row.get("passed_checks") if graded else None,
            checks_total=row.get("total_checks") if graded else None,
            normal_submission=row.get("runtime_termination") == "submitted",
            normal_submission_and_pass=(passed and row.get("runtime_termination") == "submitted") if graded else None,
            model_calls_recorded=row.get("model_calls"), agent_seconds=row.get("elapsed_s"),
            stop=row.get("runtime_termination"), grading_source=row.get("grading_source"),
            legacy_typed_repetition_totals=repeats))
    return dict(source=str(file.resolve()), reference_only=True, score_splicing=False, tasks=rows,
                note="Legacy typed action/file-state repetitions are not compared to native terminal-command repeats.")


def summarize(run: Path, legacy: Path | None) -> dict:
    warnings: list[str] = []
    launch = read_json(run / "launch.json", warnings) or {}
    protocol = read_json(Path(__file__).with_name("protocol.json"), warnings) or {}
    task_ids = launch.get("task_ids") or [task["id"] for task in protocol.get("tasks", [])]
    method = launch.get("method", "unknown")
    native = run / "native" if (run / "native").is_dir() else run
    records: dict[str, dict[str, tuple[dict, Path]]] = {}
    result_files = [run / "native-results.json", run / "results.json", native / "results.json"]
    result_files += sorted(native.glob("*/*/results.json"))
    for file in dict.fromkeys(result_files):
        obj = read_json(file, warnings)
        if not isinstance(obj, dict):
            continue
        candidates = obj.get("results", []) if isinstance(obj.get("results"), list) else [obj]
        for row in candidates:
            task = row.get("task_id")
            if task not in task_ids:
                continue
            trial = row.get("trial_name") or row.get("id") or task
            records.setdefault(task, {})[str(trial)] = (row, file)
    tasks = []
    for task in task_ids:
        candidates = records.get(task, {})
        ambiguous = len(candidates) > 1
        result, result_file = next(iter(candidates.values())) if len(candidates) == 1 else ({}, None)
        trial_dirs = sorted(path for path in (native / task).glob("*") if path.is_dir())
        if len(trial_dirs) > 1:
            ambiguous = True
        logs = trial_dirs[0] / "agent-logs" if len(trial_dirs) == 1 else None
        summary = (read_json(logs / "budget-summary.json", warnings) or {}) if logs else {}
        trace = events(logs / "transport.jsonl", warnings) if logs else []
        checks = result.get("parser_results")
        graded = isinstance(checks, dict) and bool(checks) and not ambiguous
        external_pass = result.get("is_resolved") if graded else None
        if not isinstance(external_pass, bool):
            external_pass, graded = None, False
        submitted = summary.get("native_submission")
        if not isinstance(submitted, bool):
            submitted = None
        grading_attempt_finished = bool(result.get("test_ended_at"))
        grading_status = ("ambiguous_multiple_trials" if ambiguous else "completed" if graded else
            "invalid_or_unparsed" if grading_attempt_finished or result.get("failure_mode") == "parse_error" else "pending")
        row = dict(task=task, grading_status=grading_status, completed_grading=graded,
            grading_attempt_finished=grading_attempt_finished, external_pass=external_pass,
            checks_passed=sum(value == "passed" for value in checks.values()) if graded else None,
            checks_total=len(checks) if graded else None, parser_results=checks if graded else None,
            normal_submission=submitted, normal_submission_and_pass=(submitted and external_pass)
                if submitted is not None and external_pass is not None else None,
            failure_mode=result.get("failure_mode"), agent_summary_complete=bool(summary),
            harness_agent_seconds=elapsed(result.get("agent_started_at"), result.get("agent_ended_at")),
            full_trial_seconds=elapsed(result.get("trial_started_at"), result.get("trial_ended_at")),
            result_source=str(result_file) if result_file else None, logs=str(logs) if logs else None,
            multiple_trials_detected=ambiguous)
        row.update(transport_metrics(trace, summary, method))
        tasks.append(row)
    completed = sum(row["completed_grading"] for row in tasks)
    passed = sum(row["external_pass"] is True for row in tasks)
    numeric = ("http_attempts", "known_input_tokens", "known_output_tokens", "known_total_tokens",
               "unknown_usage_requests", "pending_usage_requests", "http_error_attempts", "cleanup_error_count", "marker_error_count")
    totals = {key: sum(row[key] or 0 for row in tasks) for key in numeric}
    totals.update(expected_tasks=len(tasks), completed_grading=completed, successes=passed,
        failures_among_graded=sum(row["external_pass"] is False for row in tasks),
        ungraded_tasks=len(tasks) - completed,
        grading_attempts_finished=sum(row["grading_attempt_finished"] for row in tasks),
        invalid_grading_tasks=sum(row["grading_status"] == "invalid_or_unparsed" for row in tasks),
        normal_submissions=sum(row["normal_submission"] is True for row in tasks),
        submission_unknown_tasks=sum(row["normal_submission"] is None for row in tasks),
        normal_submission_and_pass=sum(row["normal_submission_and_pass"] is True for row in tasks),
        native_exact_command_repeats_known=sum(row["native_exact_command_repeats"] or 0 for row in tasks),
        native_command_repeat_unknown_tasks=sum(row["native_exact_command_repeats"] is None for row in tasks),
        accuracy_on_graded=passed / completed if completed else None,
        accuracy_full_suite=passed / len(tasks) if tasks and completed == len(tasks) else None,
        exploration_seconds_completed=sum(row["exploration_seconds"] for row in tasks if row["exploration_seconds"] is not None),
        setup_seconds_completed=sum(row["setup_seconds"] for row in tasks if row["setup_seconds"] is not None),
        stop_counts=dict(Counter(row["stop"] for row in tasks if row["stop"])))
    output = dict(schema_version=1, captured_utc=datetime.now(timezone.utc).isoformat(),
        run=str(run), method=method, run_status=launch.get("status", "not_finalized"),
        partial=completed < len(tasks), totals=totals, tasks=tasks, warnings=warnings,
        run_wall_seconds=elapsed(launch.get("started_utc"), launch.get("finished_utc")),
        score_scope="Only this --run; no sibling runs are read or scores spliced. Ungraded tasks remain null.",
        monetary_cost_usd=None, monetary_cost_status="Local inference not priced; token counts are not dollar costs.")
    if legacy:
        output["legacy_reference"] = legacy_reference(legacy, warnings)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--legacy", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    if not run.is_dir():
        parser.error("--run must be one existing run directory")
    output = (args.output or ANALYSIS / (run.name + ".json")).resolve()
    if not output.is_relative_to(ANALYSIS) or output == ANALYSIS:
        parser.error(f"--output must be a file inside {ANALYSIS}")
    report = summarize(run, args.legacy.resolve() if args.legacy else None)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(dict(output=str(output), partial=report["partial"], **report["totals"]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
