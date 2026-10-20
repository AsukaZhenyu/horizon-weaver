"""Independent acceptance and deterministic progress measurement."""
from __future__ import annotations

import json
import time

from .core import (ActionKind, Attempt, Check, Evaluation, Evidence, GraphView,
                   ProgressAssessment, StateSnapshot, TaskContract, ToolResult,
                   Verdict, canonical, current_verdict, digest)
from .ports import HarnessPort


class Evaluator:
    def __init__(self, harness: HarnessPort):
        self.harness = harness

    def assess(self, task: TaskContract, timeout_s: float) -> Evaluation:
        deadline = time.monotonic() + timeout_s
        before = self.harness.snapshot()
        checks: list[tuple[str, str, ToolResult]] = []
        for check in task.acceptance_checks:
            remaining = deadline - time.monotonic()
            if check.kind == "external":
                # Hidden benchmark tests run only after control is returned to
                # their owner. A pending receipt can never grant success.
                result = ToolResult(error_type="external_verification_pending",
                                    output="Awaiting the external evaluator after submission; tests are unavailable to the agent.")
            elif remaining <= 0:
                result = ToolResult(error_type="timeout", output="acceptance budget exhausted")
            else:
                result = self.harness.check(check, min(task.budget.tool_timeout_s, remaining))
            checks.append((check.id, digest(canonical(check)), result))
        after = self.harness.snapshot()
        unchanged = before.fingerprint == after.fingerprint
        evidence = [Evidence(check_id=check_id, check_hash=check_hash,
                             snapshot_hash=after.fingerprint,
                             passed=unchanged and result.exit_code == 0 and result.error_type is None,
                             result=result if unchanged else result.model_copy(update={
                                 "error_type": "check_mutated_workspace",
                                 "output": result.output + "\nacceptance checks changed workspace state"}))
                    for check_id, check_hash, result in checks]
        for path in task.expected_artifacts:
            item = after.file(path)
            passed = item is not None and item.kind == "file"
            evidence.append(Evidence(check_id="artifact:" + path, check_hash=digest(path),
                                     snapshot_hash=after.fingerprint, passed=passed,
                                     result=ToolResult(exit_code=0 if passed else 1,
                                                       output="present" if passed else "missing regular file")))
        satisfied = tuple(item.check_id for item in evidence if item.passed)
        unsatisfied = tuple(item.check_id for item in evidence if not item.passed)
        return Evaluation(snapshot=after, verdict=Verdict(
            passed=not unsatisfied, satisfied_requirements=satisfied,
            unsatisfied_requirements=unsatisfied, evidence=tuple(evidence)))


class ProgressMonitor:
    def assess(self, candidate: Attempt, view: GraphView,
               previous: Verdict, current: Verdict) -> ProgressAssessment:
        if candidate.action.kind in (ActionKind.VERIFY, ActionKind.DONE) and candidate.tool_executed is False:
            try:
                reused = json.loads(candidate.result.output).get('source_steps', [])
            except (ValueError, AttributeError):
                reused = []
            if reused:
                return ProgressAssessment(level='none', repeated=True, reason_code='reused_verification',
                                          matching_steps=tuple(reused))
        if candidate.result.error_type == "redundant_action":
            return ProgressAssessment(level="none", repeated=True, reason_code="redundant_action")
        if set(current.satisfied_requirements) - set(previous.satisfied_requirements):
            return ProgressAssessment(level="verified", reason_code="new_acceptance_evidence")
        snapshots = {attempt.before.fingerprint for attempt in view.attempts}
        snapshots.update(attempt.after.fingerprint for attempt in view.attempts)
        if candidate.delta.changed_files:
            if candidate.after.fingerprint in snapshots:
                return ProgressAssessment(level="none", repeated=True, reason_code="state_cycle")
            return ProgressAssessment(level="state_change", reason_code="workspace_changed")
        same = [attempt for attempt in view.attempts
                if attempt.action.execution_signature == candidate.action.execution_signature
                and attempt.execution_timeout_s == candidate.execution_timeout_s
                and attempt.before.fingerprint == candidate.before.fingerprint
                and attempt.after.fingerprint == candidate.after.fingerprint
                and attempt.consequence_signature == candidate.consequence_signature
                and attempt.result.exit_code == candidate.result.exit_code
                and attempt.result.error_type == candidate.result.error_type
                and attempt.result.outcome_unknown == candidate.result.outcome_unknown]
        if same:
            return ProgressAssessment(level="none", repeated=True, reason_code="same_input_and_result",
                                      matching_steps=tuple(a.step for a in same) + (candidate.step,))
        if candidate.self_check is not None:
            return ProgressAssessment(level="information", reason_code="new_self_check_observation")
        if candidate.verification:
            return ProgressAssessment(level='verified' if any(r.status == 'pass' and r.behavioral for r in candidate.verification)
                else 'information', reason_code='new_verification_result')
        if candidate.action.kind == ActionKind.HISTORY and candidate.result.exit_code == 0:
            return ProgressAssessment(level='information', reason_code='retained_history_observation')
        if candidate.action.kind == ActionKind.READ and candidate.result.exit_code == 0:
            return ProgressAssessment(level="information", reason_code="new_file_observation")
        if candidate.result.error_type or candidate.result.exit_code not in (0, None):
            return ProgressAssessment(level="information", reason_code="new_failure")
        if candidate.action.kind == ActionKind.COMMAND and candidate.result.output.strip():
            return ProgressAssessment(level="information", reason_code="new_command_observation")
        return ProgressAssessment(level="none", reason_code="no_new_state_or_evidence")


class FailureReasoner:
    def explain(self, attempt: Attempt | None, verdict: Verdict, error: str | None = None) -> str:
        if error:
            return "Action rejected: " + error[:2000]
        if attempt is None:
            return ""
        if attempt.result.outcome_unknown:
            return "The action outcome is unknown; inspect the environment before retrying."
        if attempt.result.error_type == "redundant_action":
            return attempt.result.output
        if attempt.progress.reason_code == 'reused_verification':
            return ('The recorded file comparison was reused; no new check ran. '
                    'If it failed, repair the candidate or explicitly correct the test. '
                    'If requirements and submission gates are satisfied, use done.')
        if attempt.progress.reason_code == "state_cycle":
            return "This file state was visited earlier; the transition produced no new state or acceptance evidence."
        if attempt.progress.repeated:
            result = attempt.result
            return (f"Same action/result on the same file state: {len(attempt.progress.matching_steps)} "
                    f"observations, steps={attempt.progress.matching_steps}. "
                    f"exit={result.exit_code} error={result.error_type}. "
                    "Process/network state is not captured. Choose a different hypothesis or diagnostic "
                    "if this retry is not producing information.\n"
                    + (attempt.action.command or attempt.action.path)[:800] + "\n" + result.output[-1000:])
        if attempt.result.error_type or attempt.result.exit_code not in (0, None):
            if attempt.result.error_type == 'check_definition_invalid':
                return 'Verification definition rejected; no check executed and no candidate defect established. ' + attempt.result.output[-1200:]
            if attempt.result.error_type == 'verification_inconclusive':
                return 'Check inconclusive; candidate correctness is unknown. Repair the check/prerequisite or explicitly replace it. ' + attempt.result.output[-500:]
            if attempt.verification and any(r.status == 'fail' for r in attempt.verification):
                return ('An executed comparison did not meet its specified expectation. This establishes a predicate mismatch, '
                        'not its cause or a public-goal violation. Compare actual operands, expectation basis and the goal gap '
                        'before choosing candidate repair, check correction or another measurement.')
            return f"Execution failed ({attempt.result.error_type or attempt.result.exit_code}): " + attempt.result.output[-500:]
        if attempt.self_check is not None:
            if attempt.self_check.classification == "observation":
                return ("Inspection executed successfully, but it contains no recognized failure assertion. "
                        + attempt.self_check.classification_reason + " Requirement coverage remains unknown.")
            return ("Self-check command exited zero on the recorded file state. Requirement coverage is unknown; "
                    "compare the observed behavior with each original requirement. Hidden grading remains pending.")
        if attempt.action.kind in (ActionKind.DONE, ActionKind.VERIFY) and not verdict.passed:
            if any(item.result.error_type == "external_verification_pending" for item in verdict.evidence):
                if attempt.verification:
                    return ('The listed comparisons passed on the measured state. Consult verification readiness; '
                            'if it permits done and the full public goal is fulfilled, submit. Hidden grading is pending.')
                return "Hidden grading is pending. Use your own checks, then send done to submit the candidate."
            return "Acceptance failed: " + ", ".join(verdict.unsatisfied_requirements)
        return ""

