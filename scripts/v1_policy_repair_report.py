"""Report the isolated development comparison without selecting best attempts."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v1_terminal_bench_report import collect


def fingerprint(path):
    text = "\n".join(file.name + ":" + hashlib.sha256(file.read_text(encoding="utf-8").encode()).hexdigest()
                     for file in sorted(path.glob("*.py")))
    return hashlib.sha256(text.encode()).hexdigest()


def v0_report(root):
    path = next(root.rglob("results.json"))
    grades = json.loads(path.read_text(encoding="utf-8"))["results"]
    trials = []
    for grade in grades:
        parser = grade.get("parser_results") or {}
        runtime_path = path.parent / grade["task_id"] / grade["trial_name"] / "agent-logs/v0-reference/result.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
        trials.append(dict(task=grade["task_id"],
            resolved=bool(grade["is_resolved"] and parser and all(value == "passed" for value in parser.values())),
            passed_checks=sum(value == "passed" for value in parser.values()), total_checks=len(parser),
            grading_valid=bool(parser), benchmark_failure=grade.get("failure_mode"),
            runtime_termination=runtime.get("termination"), steps=runtime.get("steps"),
            elapsed_s=runtime.get("elapsed_s"), usage=runtime.get("usage"),
            usage_complete=runtime.get("termination") != "model_error",
            benchmark_results=str(path), runtime_result=str(runtime_path)))
    return dict(resolved=sum(item["resolved"] for item in trials), completed_trials=len(trials), trials=trials)


def main():
    project = Path(__file__).resolve().parents[1]
    root = project / "runs/v1-policy-repair-20261003"
    reference = v0_report(root / "v0-isolated-cwd")
    phase_only = collect(root / "v1-phase-final")
    repaired = collect(root / "v1-decoder")
    quality = collect(root / "v1-self-check-quality")
    pipeline = collect(root / "v1-pipeline-quality")
    terminal = collect(root / "v1-terminal-policy")
    initial = json.loads((project / "runs/v1-terminal-bench-20261003/initial.json").read_text(encoding="utf-8"))
    old = {row["task"]: row for row in reference["trials"]}
    new = {row["task"]: row for row in repaired["trials"]}
    paired = [dict(task=task, v0_passed=old[task]["resolved"], v1_passed=new[task]["resolved"])
              for task in sorted(old.keys() & new.keys())]
    quality_by_task = {row["task"]: row for row in quality["trials"]}
    quality_hash = fingerprint(root / "code/self-check-quality/horizon_weaver")
    quality_matches = all(row.get("code_hash") == quality_hash for row in quality["trials"])
    pipeline_hash = fingerprint(root / "code/pipeline-quality/horizon_weaver")
    pipeline_matches = all(row.get("code_hash") == pipeline_hash for row in pipeline["trials"])
    terminal_hash = fingerprint(root / "code/terminal-policy/horizon_weaver")
    terminal_matches = all(row.get("code_hash") == terminal_hash for row in terminal["trials"])
    terminal_by_task = {row["task"]: row for row in terminal["trials"]}
    terminal_pairs = [dict(task=task, v0_passed=old[task]["resolved"],
                          v1_passed=terminal_by_task[task]["resolved"])
                      for task in sorted(old.keys() & terminal_by_task.keys())]
    quality_pairs = [dict(task=task, v0_passed=old[task]["resolved"],
                         v1_passed=quality_by_task[task]["resolved"])
                     for task in sorted(old.keys() & quality_by_task.keys())]
    source_hash = fingerprint(root / "code/decoder-v2/horizon_weaver")
    source_matches = all(row.get("code_hash") == source_hash for row in repaired["trials"])
    if (len(paired) != 3 or repaired["completed_trials"] != 5 or not source_matches
            or len(quality_pairs) != 3 or quality["completed_trials"] != 4 or not quality_matches
            or pipeline["completed_trials"] != 1 or not pipeline_matches
            or len(terminal_pairs) != 3 or terminal["completed_trials"] != 3 or not terminal_matches):
        raise RuntimeError("comparison is incomplete or source fingerprint does not match")
    report = dict(scope="Selected original Terminal-Bench 0.2 development regression; not a full score or frozen baseline",
        model="Qwen3.5-9B Q4_K_M, thinking off, temperature 0.2, same server",
        paired_budget=dict(output_tokens=2048, max_steps=40, wall_time_s=300, tool_timeout_s=30,
                           max_model_tokens=200000, attempts=1),
        reference_limitations="v0 policy, loop and tools unchanged; wrapper bounds transport/session deadlines and token usage. v0 native output limit is 8192, so this is a matched-2048 comparison, not native-default parity.",
        original_v1=initial, phase_only_v1=phase_only, reference_v0=reference, repaired_v1=repaired, paired=paired,
        paired_v0_resolved=sum(row["v0_passed"] for row in paired),
        paired_v1_resolved=sum(row["v1_passed"] for row in paired),
        self_check_quality_v1=quality, self_check_quality_pairs=quality_pairs,
        self_check_quality_v1_resolved=sum(row["v1_passed"] for row in quality_pairs),
        self_check_quality_archive_fingerprint=quality_hash,
        quality_archive_matches_all_manifests=quality_matches,
        final_pipeline_followup=pipeline, final_pipeline_archive_fingerprint=pipeline_hash,
        pipeline_archive_matches_all_manifests=pipeline_matches,
        terminal_frontend_v1=terminal, terminal_frontend_pairs=terminal_pairs,
        terminal_frontend_archive_fingerprint=terminal_hash,
        terminal_archive_matches_all_manifests=terminal_matches,
        repaired_archive_fingerprint=source_hash, archive_matches_all_manifests=source_matches,
        current_default_output_tokens=8192,
        current_default_policy_style="terminal", current_default_structured_output="none",
        excluded_batches=[dict(path=str(root / "v0"), reason="gateway children loaded concurrently edited source"),
                          dict(path=str(root / "v0-isolated"), reason="Python -m cwd overrode reference PYTHONPATH; stopped and retained")],
        intermediate_revision=dict(path=str(root / "v1-repaired"),
            reason="First permission task stopped after 8 rejected wrapped actions and no tool execution; original grading failed 0/1. The remaining batch was stopped to fix the protocol, so no five-task score exists.",
            source_archive=str(root / "code/repaired")),
        tests=dict(repaired_before_default_restore="62/62, including 6 real Docker tests",
                   decoder_revision="65 tests: full Docker suite before final path-pattern refinement and 59 runtime tests afterward; see tests-final-decoder.txt/tests-decoder-paths.txt",
                   context_repair="69/69 including 6 real Docker tests; see tests-final-context-quality.txt; preceding context-budget test failure retained in tests-final-quality.txt",
                   confirmed_termination="70/70 including 6 real Docker tests; see tests-final-termination.txt",
                   terminal_frontend="75/75 including 6 real Docker tests; see tests-final-terminal-policy.txt",
                   current_defaults="76/76 including 6 real Docker tests; see tests-final-defaults-terminal.txt. After a final malformed-array guard, 70 runtime tests passed again (6 Docker tests skipped) in tests-final-codec-runtime.txt"),
        limitations=["One attempt per task, development tasks already seen, no statistical or holdout claim",
                     "v0 wrapper reports returned usage; failed HTTP calls can consume unreported tokens, so cost comparisons on model_error trials are incomplete",
                     "Self-check gate records execution and freshness; inadequate policy tests can still pass",
                     "Attached harness still has no persistent interactive shell or services",
                     "Restoring 8192 output capacity is separate from the explicit-2048 paired run",
                     "Final decoder batch explicitly enables json_schema; unconstrained phase-only batch remains a separate negative result"])
    report["self_check_quality_repair"] = dict(
        diagnosis="log-summary wrote INFO=7, then verified by printing CSV rows with no predicate; original grader rejected incorrect count",
        mechanism="Conservative preflight rejects recognizable observations and gives no execution receipt; expected derived values must be recomputed from original inputs. The latest tool observation gets priority so a small source file's central failure branch is not cut away despite available context budget.",
        limitation="Preflight does not prove arbitrary shell predicates or delegated test scripts are adequate; independent grading remains required",
        note="The four-task follow-up contains the three v0 matched tasks plus the async context regression. Its log check still merely printed counts in a grep/awk/echo pipeline; the final single-task pipeline repair is separate. Neither revision is substituted into any earlier score.")
    report["terminal_frontend_repair"] = dict(
        mechanism="A typed act/tool/target frontend permits brief deliberation and lowers one validated decision into the same v1 Action IR. It imports no v0 loop/plugins. All scoped file access, self-check freshness, fixed acceptance and recovery still run.",
        residual="log-summary has correct original grading but ended no_progress after a false-negative self-check on wc -l; missing final newline made its line-count test wrong. regex-log still failed. Do not treat grading parity as complete workflow reliability.",
        default_followup="Current defaults select terminal/none and additionally accept three declared direct-tool forms with matching tool tags and inert empty fields. Replay recovered 10/10 previously rejected outputs as valid IR decisions without executing any tool. This is codec evidence, not a new benchmark score.",
        limitation="Changing frontend also changes prompt language and constrained-vs-unconstrained decoding. This is a bundled development comparison, not an attribution to a single mechanism.")
    report["current_source"] = dict(fingerprint=fingerprint(root / "code/current-defaults/horizon_weaver"),
        live_matches_archive=fingerprint(project / "horizon_weaver") == fingerprint(root / "code/current-defaults/horizon_weaver"),
        source_archive=str(root / "code/current-defaults"),
        benchmark_revision_note="Terminal benchmark source precedes final default/codec changes; it is never relabeled as the live-source score")
    if not report["current_source"]["live_matches_archive"]:
        raise RuntimeError("current source archive differs from live package")
    report["context_diagnostic"] = json.loads((root / "context-diagnostic.json").read_text(encoding="utf-8"))
    report["provenance"] = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    report["cleanup"] = json.loads((root / "cleanup.json").read_text(encoding="utf-8-sig"))
    report["codec_replay"] = json.loads((root / "terminal-codec-replay.json").read_text(encoding="utf-8"))
    report["decoder_profile"] = json.loads((root / "decoder-probe.json").read_text(encoding="utf-8"))
    report["initial_decoder_probe"] = dict(source_archive=str(root / "code/decoder"),
        result="Valid JSON shape, runtime rejected absolute path /home/user/notes.txt. No tool executed; raw response was not saved by the initial probe.")
    if (root / "v0-output8192").exists() and (root / "v1-output8192").exists():
        native_v0 = v0_report(root / "v0-output8192")
        native_v1 = collect(root / "v1-output8192")
        native_hash = fingerprint(root / "code/final-defaults/horizon_weaver")
        if native_v1["completed_trials"] != 1 or not all(
                row.get("code_hash") == native_hash for row in native_v1["trials"]):
            raise RuntimeError("output-capacity follow-up incomplete or source mismatch")
        report["output8192_followup"] = dict(task="regex-log", wall_time_s=300, output_tokens=8192,
            reference_v0=native_v0, repaired_v1=native_v1,
            code_hash=native_hash, phase_guidance=False,
            limitation="Separate single-task follow-up before phase-guidance repair; do not mix into the five-task result. The original v0 default wall budget is longer, so this is not a full native-default comparison.")
    report["stopped_before_phase_guidance"] = dict(path=str(root / "v1-final"),
        reason="Queued launcher loaded the earlier source archive just before phase-guidance was saved. Stopped before a complete five-task score; full partial trajectory retained.")
    (root / "overview.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# v1 development regression repair — 2026-10-03", "",
        "These are previously selected original Terminal-Bench 0.2 tasks, one attempt per revision. They are development checks, not a full benchmark, holdout result or frozen baseline.", "",
        "| Matched task | v0 reference | v1 decoder | v1 self-check repair | v1 terminal frontend |",
        "| --- | --- | --- | --- | --- |"]
    for pair in quality_pairs:
        task = pair["task"]
        cells = ["Pass" if value else "Fail" for value in (
            pair["v0_passed"], new[task]["resolved"], pair["v1_passed"], terminal_by_task[task]["resolved"])]
        lines.append("| " + " | ".join([task, *cells]) + " |")
    lines += ["", f"Matched totals: v0 {report['paired_v0_resolved']}/3; v1 decoder {report['paired_v1_resolved']}/3; v1 self-check repair {report['self_check_quality_v1_resolved']}/3; v1 terminal frontend {terminal['resolved']}/3.",
        "", f"Separate complete five-task batches: initial v1 {initial['resolved']}/5; phase guidance {phase_only['resolved']}/5; decoder {repaired['resolved']}/5. No cross-revision best-of score is reported.",
        "", "Matched budget: same Qwen3.5-9B Q4_K_M server, thinking off, temperature 0.2, 2048 output tokens, 40 steps, 300 s, 30 s tools, one attempt. v0's native output cap is 8192 and its native wall budget is longer; this is a bounded matched comparison, not native-default parity.",
        "", f"Separate four-task follow-up: {quality['resolved']}/4. Its three matched tasks are compared above; cancel-async-tasks is an additional context regression without a v0 comparison.",
        "", f"Final counting-pipeline guard follow-up: log-summary {pipeline['resolved']}/1. This is a separate one-task revision; it is not added to any previous three/four/five-task score.",
        "", "Current defaults: terminal frontend, unconstrained gateway output and 8192 output tokens. The frontend lowers one strict decision into Action IR; IR/JSON Schema remains an explicit alternative. Runtime tests: 76/76, including six real Docker tests; 70 runtime tests passed after the final malformed-array guard. Engineering tests do not establish model task-solving ability.",
        "", "The terminal frontend restored permissions (5 steps, submitted) and correct log counts (original grader 3/3). Log-summary nevertheless ended no_progress after its own false-negative line-count check; regex-log failed. Grading parity is not full workflow reliability, and v1.0 remains unaccepted.",
        "", "Final default/codec changes were validated by tests and offline replay of 10/10 formerly rejected decisions; no tool ran in replay. They do not create a new three-task score. Every graded revision remains tied to its actual source fingerprint.",
        "", "Limitations: policy tests may be inadequate even when they exit 0; persistent interactive shells/services remain unsupported; prior failure records and partial/excluded runs are retained. Actual per-task grades, source fingerprints and usage caveats are in overview.json.", ""]
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(dict(paired=paired, v0=report["paired_v0_resolved"], v1=report["paired_v1_resolved"],
                         repaired_five=repaired["resolved"], quality_pairs=quality_pairs,
                         quality_total=report["self_check_quality_v1_resolved"], terminal_total=terminal["resolved"],
                         source_matches=source_matches), indent=2))


if __name__ == "__main__":
    main()
