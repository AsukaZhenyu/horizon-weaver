"""Explicit single-task loop and supervisor. No component shares mutable RunState."""
from __future__ import annotations

import time
import uuid

from .core import (Action, ActionKind, Attempt, Checkpoint, Decision, GraphView,
                   ProgressAssessment, RunManifest, RunResult, StateDelta,
                   SelfCheckReceipt, StepOutcome, TaskContract, Termination, ToolResult,
                   Usage, Verdict, ExplorationNote, RequirementNote, PolicyNotes, GoalObservation, canonical, current_verdict, digest, self_check_ready, requirement_sources, action_schema)
from .errors import ContextBudgetError, HarnessError, GatewayError
from .ports import (ContextCompilerPort, EvaluatorPort, EventStorePort,
                    FailureReasonerPort, HarnessPort, PolicyPort, ProgressMonitorPort)
from .ports import TransitionGuardPort
from .state import StateGraph
from .verification import (observation_only, lower_legacy_verify, run_checks, verification_state,
                           verification_environment, verification_followup, recorded_comparisons, resolve_check_ids,
                           goal_assessment, goal_feedback, current_file_text)
from .state_queries import StateQuery


class DeadlineExceeded(TimeoutError):
    pass


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise DeadlineExceeded("run wall-time budget exhausted")
    return value


class Loop:
    def __init__(self, harness: HarnessPort, policy: PolicyPort,
                 compiler: ContextCompilerPort, evaluator: EvaluatorPort,
                 monitor: ProgressMonitorPort, reasoner: FailureReasonerPort, store: EventStorePort,
                 guard: TransitionGuardPort | None = None):
        self.harness, self.policy, self.compiler = harness, policy, compiler
        self.evaluator, self.monitor, self.reasoner, self.store = evaluator, monitor, reasoner, store
        self.guard = guard

    def step(self, task: TaskContract, view: GraphView, step: int,
             deadline: float, remaining_tokens: int) -> StepOutcome:
        remaining(deadline)
        before = self.harness.snapshot()
        self.store.append("ObservationCaptured", before)
        previous = current_verdict(task, view.verdict, before)
        request = self.compiler.build(task, view, before, step, remaining_tokens)
        call_id = uuid.uuid4().hex
        available = remaining(deadline)
        call_timeout = min(request.model_timeout_s or task.budget.model_timeout_s,
                           task.budget.max_model_timeout_s, available - min(5.0, available / 5))
        self.store.append("ModelCallStarted", {"call_id": call_id, "step": step,
            "input_token_bound": request.input_token_bound, "output_token_bound": request.max_output_tokens,
            "timeout_s": call_timeout,
            "decoder_schema_hash": digest(canonical(action_schema(
                exploration_history=request.exploration_history,
                verification_policy=request.verification_policy,
                known_check_ids=request.known_check_ids,
                reflection_required=request.reflection_required, goal_review_refs=request.goal_review_refs)))
                if self.policy.describe().get('structured_output') == 'json_schema' else None,
            "request": request.model_dump(mode="json")})
        try:
            decision = self.policy.decide(request, call_timeout)
        except GatewayError as error:
            if not error.retryable:
                self.store.append("ModelCallCompleted", {"call_id": call_id, "step": step,
                    "decision": Decision(error=str(error), usage=error.usage).model_dump(mode="json")})
                raise
            prefix = ("Model service temporarily failed; no action was executed. " if str(error).startswith('HTTPStatusError:')
                      else "Model decision timed out; no action was executed. ")
            decision = Decision(error=(prefix +
                "Return a short complete action, omitting reasoning. " + str(error)), usage=error.usage)
        except Exception as error:
            charged = Usage(input_tokens=request.input_token_bound,
                            output_tokens=request.max_output_tokens, estimated=True)
            failure = GatewayError(f"{type(error).__name__}: {error}", charged)
            self.store.append("ModelCallCompleted", {"call_id": call_id, "step": step,
                "decision": Decision(error=str(failure), usage=charged).model_dump(mode="json")})
            raise failure from error
        decision = self.validated_notes(decision, task, view)
        self.store.append("ModelCallCompleted", {"call_id": call_id, "step": step,
            "decision": decision.model_dump(mode="json"), "state_version": request.state_version})
        remaining(deadline)
        if decision.usage.total > remaining_tokens:
            raise ContextBudgetError("provider-reported usage exceeded the remaining model budget")
        if decision.action is None:
            return StepOutcome(decision=decision, snapshot=before, verdict=previous,
                               feedback=self.reasoner.explain(None, previous, decision.error))
        action = decision.action
        reference_error = None
        if action.check_ids and task.verification_policy == 'behavioral':
            try:
                action = action.model_copy(update=dict(checks=resolve_check_ids(view.attempts, action.check_ids), check_ids=()))
            except ValueError as error:
                reference_error = str(error)
        if task.verification_policy == 'behavioral' and action.kind == ActionKind.VERIFY and action.command:
            action = lower_legacy_verify(action)
        # A model call can outlive the state it observed. Recheck before
        # accepting a cached observation or attributing a delta to this attempt.
        execution_before = self.harness.snapshot()
        if execution_before.fingerprint != before.fingerprint:
            before = execution_before
            self.store.append("ObservationCaptured", before)
            previous = current_verdict(task, previous, before)
        self.store.append("ActionProposed", {"step": step, "action": action.model_dump(mode="json"),
                                             "snapshot_hash": before.fingerprint})
        receipt = None
        check_executed = False
        observation_reason = None
        tool_executed = False
        execution_timeout = None
        verification = ()
        environment_hash = verification_environment(self.harness.describe(), view.attempts)
        goal_notes = (*view.notes_history, ExplorationNote(step=step, state_version=before.fingerprint,
            notes=decision.notes)) if decision.notes is not None else view.notes_history
        def assess_goal(snapshot, check_state=None):
            assessments = view.attempts
            if verification:
                assessments = (*assessments, Attempt(step=step, action=action, result=result,
                    before=before, after=snapshot, delta=StateDelta.between(before, snapshot),
                    progress=ProgressAssessment(level='none', reason_code='pending-progress'),
                    tool_executed=True, verification=verification))
            return goal_assessment(task, assessments, snapshot, environment_hash,
                notes_history=goal_notes, verdict=previous, state=check_state)
        def tool_wait():
            nonlocal execution_timeout
            available = remaining(deadline)
            reserve = min(5.0, available / 5) if self.harness.describe().get("backend") == "attached_docker" else 0
            execution_timeout = min(action.timeout_s or task.budget.tool_timeout_s,
                                    task.budget.max_tool_timeout_s, available - reserve)
            return execution_timeout
        guarded = self.guard.assess(action, request, view, before) if self.guard is not None else None
        reusable = recorded_comparisons(view.attempts, action.checks, before, environment_hash)
        if reference_error:
            result = ToolResult(exit_code=1, error_type='check_definition_invalid', output=reference_error)
        elif action.check_ids and task.verification_policy != 'behavioral':
            result = ToolResult(exit_code=1, error_type='verification_disabled', output='Saved checks are disabled for this run.')
        elif guarded is not None:
            result = guarded
        elif action.kind == ActionKind.HISTORY:
            if not task.exploration_history:
                result = ToolResult(exit_code=1, error_type='history_disabled', output='Exploration retrieval is disabled for this run.')
            else:
                records = StateQuery.from_view(task, view, before).exploration_history(action.query)
                result = ToolResult(exit_code=0, output=canonical(dict(records=records,
                    source='recorded attempts; no historical tool was re-executed')))
        elif action.kind == ActionKind.INCOMPLETE:
            result = ToolResult(exit_code=0, output=canonical(goal_feedback(assess_goal(before))))
        elif action.checks and task.verification_policy != 'behavioral':
            result = ToolResult(exit_code=1, error_type='verification_disabled',
                output='Structured verification is disabled for this task; use the legacy verify command.')
        elif reusable:
            state = verification_state(view.attempts, before, environment_hash)
            passed = all(receipt.status == 'pass' for _, receipt in reusable)
            submitted = action.kind == ActionKind.DONE
            assessment = assess_goal(before, state)
            ok = assessment['submission']['allowed'] if submitted else passed
            result = ToolResult(exit_code=0 if ok else 1,
                error_type=None if ok else ('verification_required' if submitted else 'verification_failed'),
                output=canonical(dict(reused_check_ids=[receipt.spec.id for _, receipt in reusable],
                    source_steps=[number for number, _ in reusable], new_tool_execution=False,
                    results=[dict(id=receipt.spec.id, status=receipt.status, output=receipt.result.output)
                             for _, receipt in reusable],
                    predicate_ready=state['ready'], submission_allowed=assessment['submission']['allowed'],
                    acceptance='external evaluation pending')))
        elif action.checks:
            tool_executed = True
            try:
                known_ids = {item.spec.id for attempt in view.attempts for item in attempt.verification}
                if any(identity not in known_ids for spec in action.checks for identity in spec.replaces):
                    raise ValueError('check replacement must cite a previously executed check ID')
                verification = run_checks(self.harness, action.checks, task, before, deadline, tool_wait(), environment_hash)
                unknown = any(item.result.outcome_unknown for item in verification)
                passed = len(verification) == len(action.checks) and all(item.status == 'pass' for item in verification)
                result = ToolResult(exit_code=None if unknown else (0 if passed else 1),
                    error_type=('verification_failed' if any(item.status == 'fail' for item in verification)
                                else 'verification_inconclusive') if unknown or not passed else None,
                    outcome_unknown=unknown, duration_s=sum(item.result.duration_s + sum(c.result.duration_s for c in item.controls)
                                                           for item in verification),
                    output=canonical([dict(id=item.spec.id, status=item.status, assertions=item.assertions,
                        behavioral=item.behavioral, sensitive=item.sensitive, oracle_validated=item.oracle_validated,
                        controls=[control.model_dump(mode='json') for control in item.controls],
                        result=item.result.model_dump(mode='json')) for item in verification]))
            except ValueError as error:
                tool_executed = False
                result = ToolResult(exit_code=1, error_type='check_definition_invalid', output=str(error))
            if (action.kind == ActionKind.DONE and task.verification_policy == 'behavioral' and not result.outcome_unknown
                    and result.error_type != 'check_definition_invalid'):
                state = verification_state(view.attempts, self.harness.snapshot(),
                    environment_hash, verification, step)
                assessment = assess_goal(self.harness.snapshot(), state)
                if not assessment['submission']['allowed']:
                    result = result.model_copy(update=dict(exit_code=1, error_type='verification_required',
                        output=result.output + '\n' + canonical(goal_feedback(assessment))))
                else:
                    # Handing back a candidate succeeded; individual check
                    # failures remain unchanged in the immutable receipts.
                    result = result.model_copy(update=dict(exit_code=0, error_type=None,
                        output=result.output + '\nCandidate submitted; self-check outcomes above remain evidence, not acceptance.'))
        elif action.kind == ActionKind.DONE and task.verification_policy == 'behavioral':
            assessment = assess_goal(before)
            result = ToolResult(exit_code=0 if assessment['submission']['allowed'] else 1,
                error_type=None if assessment['submission']['allowed'] else 'verification_required',
                output=canonical(goal_feedback(assessment)))
        elif (action.kind == ActionKind.DONE and task.require_self_check
                and not self_check_ready(view.attempts, before)):
            result = ToolResult(exit_code=1, error_type="self_check_required", output=(
                "Submission requires an executed verify command that tests the task's behavior on the current state. "
                "Run relevant assertions; fix and rerun any failed current-state check before done. "
                "File edits invalidate earlier passing checks."))
        elif action.kind == ActionKind.VERIFY and action.command:
            observation_reason = observation_only(action.command)
            if observation_reason and task.require_self_check:
                result = ToolResult(exit_code=1, error_type="self_check_observation_only", output=(
                    observation_reason + " Use command for inspection. Use verify for assertions that fail when behavior is wrong. "
                    "Derive expected values from the original inputs and requirements, not from the candidate output."))
            else:
                check_executed = True
                tool_executed = True
                result = self.harness.execute(Action(kind=ActionKind.COMMAND, command=action.command),
                                             tool_wait())
        elif action.kind == ActionKind.VERIFY and task.deferred_verification:
            result = ToolResult(exit_code=1, error_type="self_check_command_required",
                output='Hidden grading is pending. Supply an executable self-check: {"kind":"verify","command":"..."}.')
        else:
            tool_executed = True
            result = self.harness.execute(action, tool_wait())
        after = self.harness.snapshot()
        if check_executed:
            unchanged = before.fingerprint == after.fingerprint
            if not unchanged:
                result = result.model_copy(update={"error_type": "self_check_mutated_workspace",
                    "output": result.output + "\nSelf-check changed workspace files; use command for preparation, then verify without modifying files."})
            receipt = SelfCheckReceipt(command=action.command, command_hash=digest(action.command),
                snapshot_hash=after.fingerprint, workspace_unchanged=unchanged, result=result,
                classification="observation" if observation_reason else "check_candidate",
                classification_reason=observation_reason or "Executable check; assertion coverage and oracle independence are unknown.")
        verdict = current_verdict(task, previous, after)
        if not result.outcome_unknown and action.kind not in (ActionKind.READ, ActionKind.HISTORY, ActionKind.INCOMPLETE):
            self.store.append("EvaluationStarted", {"step": step})
            evaluation = self.evaluator.assess(task, remaining(deadline))
            after, verdict = evaluation.snapshot, evaluation.verdict
        candidate = Attempt(step=step, action=action, result=result, before=before, after=after,
                            delta=StateDelta.between(before, after),
                            progress=ProgressAssessment(level="none", reason_code="unassessed"), self_check=receipt,
                            tool_executed=tool_executed, execution_timeout_s=execution_timeout,
                            verification=verification)
        if task.verification_policy=='behavioral' and not result.outcome_unknown:
            observed=[]
            paths=dict.fromkeys(c.path for c in task.goal_conditions if c.kind in
                ('python_interface','text_literal','csv_header','json_shape'))
            for path in paths:
                file=after.file(path)
                if file is None or file.kind!='file' or file.size>131072:
                    continue
                text,_=current_file_text(path,(*view.attempts,candidate),after)
                if text is not None: continue
                reading=self.harness.execute(Action(kind='read',path=path),min(5,remaining(deadline)))
                if reading.exit_code==0 and not reading.error_type and not reading.outcome_unknown and digest(reading.output)==file.sha256:
                    observed.append(GoalObservation(path=path,sha256=file.sha256,result=reading))
            candidate=candidate.model_copy(update={'goal_observations':tuple(observed)})
        progress = self.monitor.assess(candidate, view, previous, verdict)
        attempt = candidate.model_copy(update={"progress": progress})
        feedback = "\n".join(filter(None, (self.reasoner.explain(attempt, verdict), decision.error)))
        if task.verification_policy == 'behavioral':
            report = goal_assessment(task, (*view.attempts, attempt), after,
                verification_environment(self.harness.describe(), (*view.attempts, attempt)),
                notes_history=goal_notes, verdict=verdict)
            # The compact outcome is regenerated from recorded facts on the
            # next decision; no additional tool or model call is introduced.
            feedback += '\nGOAL UPDATE: ' + canonical(dict(
                statuses=[dict(ref=r['ref'], status=r['status'], gap=r['gap']) for r in report['requirements'][:3]],
                objectives=[dict(ref=r['ref'], status=r['status'], fact=r['fact'], gap=r['gap'])
                    for r in goal_feedback(report, limit=3)['objectives']],
                hard_blockers=report['submission']['hard_blockers'], coverage=report['coverage']))
        return StepOutcome(decision=decision, snapshot=after, verdict=verdict, attempt=attempt,
                           feedback=feedback)

    @staticmethod
    def validated_notes(decision: Decision, task: TaskContract, view: GraphView) -> Decision:
        if decision.action is not None and decision.action.review:
            notes = decision.notes or PolicyNotes()
            reviews = tuple(RequirementNote(**r.model_dump()) for r in decision.action.review)
            reviewed_refs = {r.ref for r in reviews}
            retained = tuple(n for n in notes.requirements if n.ref not in reviewed_refs)[:24-len(reviews)]
            decision = decision.model_copy(update={'notes': notes.model_copy(update={
                'requirements': (*retained, *reviews)})})
        if decision.notes is None:
            return decision
        checks = {attempt.step for attempt in view.attempts if attempt.self_check is not None or attempt.verification}
        observations = {attempt.step for attempt in view.attempts if attempt.tool_executed is not False}
        sources = (task.goal, *task.constraints)
        known_check_ids = {r.spec.id for attempt in view.attempts for r in attempt.verification}
        if decision.action is not None:
            known_check_ids.update(spec.id for spec in decision.action.checks)
        refs = requirement_sources(task)
        valid, warnings = [], []
        for index, note in enumerate(decision.notes.requirements):
            source_ok = ((note.ref in refs and (not note.quote or note.quote in refs[note.ref]))
                         if note.ref else any(note.quote in source for source in sources))
            if (not source_ok or not set(note.check_steps) <= checks
                    or not set(note.evidence_steps) <= observations or not set(note.evidence_check_ids) <= known_check_ids):
                warnings.append(f"requirement[{index}]: use a public G/C reference or exact quote and already executed evidence steps")
            else:
                valid.append(note)
        intent = decision.notes.exploration
        if intent is not None and (any(ref not in refs for ref in intent.problem_refs)
                or any(step not in observations for step in intent.evidence_steps)
                or any(step not in {a.step for a in view.attempts} for step in intent.revises_steps)):
            warnings.append('exploration: cite public requirements and existing executed evidence')
            intent = None
        understanding = decision.notes.understanding
        if understanding is not None and (not set(understanding.evidence_steps) <= observations
                or not set(understanding.open_refs) <= set(refs)):
            warnings.append('understanding: cite public G/C requirements and already executed evidence')
            understanding = None
        if not warnings:
            return decision
        notes = decision.notes.model_copy(update={"requirements": tuple(valid), 'exploration': intent,
                                                'understanding': understanding})
        if not valid and intent is None and understanding is None and notes.next_timeout_s is None and notes.next_output_tokens is None:
            notes = None
        return decision.model_copy(update={"notes": notes, "error": "\n".join(filter(None, (
            decision.error, "Notes ignored (invalid entries only); action remains valid. " + "; ".join(warnings))))})


class Supervisor:
    def __init__(self, loop: Loop, harness: HarnessPort, evaluator: EvaluatorPort,
                 store: EventStorePort):
        self.loop, self.harness, self.evaluator, self.store = loop, harness, evaluator, store

    def run(self, manifest: RunManifest, *, resume: bool = False) -> RunResult:
        graph = StateGraph(self.store.read())
        if graph.manifest is not None and not resume:
            raise ValueError("run already exists; explicitly select resume")
        if graph.manifest is None and resume:
            raise ValueError("cannot resume a run with no manifest")
        if graph.manifest is not None and canonical(graph.manifest) != canonical(manifest):
            raise ValueError("resume requires the same contract, workspace, policy, backend and code version")
        if graph.result is not None:
            raise ValueError("run is already terminated; use inspect to read its historical result")

        task = manifest.contract
        previous_elapsed = graph.elapsed_s
        steps = graph.checkpoint.step if graph.checkpoint else 0
        stall = graph.checkpoint.no_progress if graph.checkpoint else 0
        started = time.monotonic()
        deadline = started + max(0, task.budget.wall_time_s - previous_elapsed)
        snapshot, verdict = graph.snapshot, graph.verdict

        def elapsed() -> float:
            return previous_elapsed + time.monotonic() - started

        def finish(status: Termination, reason: str) -> RunResult:
            nonlocal graph
            try:
                self.harness.close()
            except Exception as error:
                status, reason = Termination.HARNESS_ERROR, f"environment cleanup failed: {error}"
            graph = StateGraph(self.store.read())
            result = RunResult(run_id=manifest.run_id, termination=status, reason=reason,
                               steps=steps, elapsed_s=elapsed(), usage=graph.usage,
                               snapshot=snapshot, verdict=verdict,
                               checkpoint=graph.checkpoint, pending_steps=tuple(graph.pending))
            self.store.append("RunInterrupted" if status == Termination.INTERRUPTED else "RunTerminated", result)
            return result

        if graph.manifest is None:
            self.store.append("RunStarted", manifest)
        if graph.pending or graph.pending_evaluation:
            return finish(Termination.RECOVERY_BLOCKED,
                          "uncommitted action outcome is unknown; reconcile the workspace before creating a new run")
        try:
            remaining(deadline)
            self.harness.start(task, remaining(deadline))
            actual_environment = self.harness.describe()
            if resume:
                old_environments = [event for event in self.store.read() if event.kind == "EnvironmentStarted"]
                if old_environments and old_environments[-1].data_json != canonical(actual_environment):
                    return finish(Termination.RECOVERY_BLOCKED, "execution environment differs from the recorded run")
                current = self.harness.snapshot()
                verdict = current_verdict(task, verdict, current)
                if snapshot is not None and snapshot.fingerprint != current.fingerprint:
                    snapshot = current
                    verdict = current_verdict(task, verdict, current)
                    return finish(Termination.RECOVERY_BLOCKED,
                                  "workspace differs from the last committed snapshot; previous evidence is invalid")
                self.store.append("RunResumed", {"step": steps, "elapsed_s": previous_elapsed})
            else:
                self.store.append("EnvironmentStarted", actual_environment)
            # Evaluate existing artifacts before requesting a model decision.
            if graph.checkpoint is None or resume:
                self.store.append("EvaluationStarted", {"step": steps})
                evaluation = self.evaluator.assess(task, remaining(deadline))
                snapshot, verdict = evaluation.snapshot, evaluation.verdict
                self.store.append_many(
                    [("ObservationCaptured", snapshot)]
                    + [("CheckEvaluated", item) for item in verdict.evidence]
                    + [("StepFinished", Checkpoint(step=steps, elapsed_s=elapsed(), no_progress=stall,
                                                   feedback=graph.feedback, snapshot=snapshot, verdict=verdict))])
                graph = StateGraph(self.store.read())
                if any(item.result.outcome_unknown for item in verdict.evidence):
                    return finish(Termination.HARNESS_ERROR,
                                  "initial acceptance stopped with an uncertain external outcome")
            while True:
                remaining(deadline)
                if verdict.passed:
                    return finish(Termination.SUCCESS, "all human contract requirements passed on the recorded state")
                if steps >= task.budget.max_steps:
                    return finish(Termination.MAX_STEPS, "maximum decision steps reached")
                if stall >= task.budget.no_progress_limit:
                    return finish(Termination.NO_PROGRESS, "consecutive steps produced no new information, state or evidence")
                if graph.usage.total >= task.budget.max_model_tokens:
                    return finish(Termination.BUDGET_EXHAUSTED, "model token budget exhausted")
                steps += 1
                outcome = self.loop.step(task, graph.view, steps, deadline,
                                         task.budget.max_model_tokens - graph.usage.total)
                snapshot, verdict = outcome.snapshot, outcome.verdict
                stall = (0 if outcome.attempt is not None and outcome.attempt.progress.level != "none"
                         else stall + 1)
                records: list[tuple[str, object]] = []
                if outcome.attempt is not None:
                    records.append(("ActionExecuted", outcome.attempt))
                records.extend(("CheckEvaluated", item) for item in verdict.evidence)
                records.append(("StepFinished", Checkpoint(
                    step=steps, elapsed_s=elapsed(), no_progress=stall, feedback=outcome.feedback,
                    snapshot=snapshot, verdict=verdict)))
                # Commit result, evidence and checkpoint together; graph changes only after commit.
                self.store.append_many(records)
                graph = StateGraph(self.store.read())
                if outcome.attempt is not None and outcome.attempt.result.outcome_unknown:
                    status = (Termination.TIMEOUT if outcome.attempt.result.error_type == "timeout"
                              else Termination.HARNESS_ERROR)
                    return finish(status, "tool stopped with an uncertain external outcome")
                if any(item.result.outcome_unknown for item in verdict.evidence):
                    return finish(Termination.HARNESS_ERROR, "acceptance execution stopped with an uncertain outcome")
                if outcome.attempt is not None and outcome.attempt.action.kind == ActionKind.INCOMPLETE:
                    return finish(Termination.INCOMPLETE, 'Candidate handed back with unresolved verification; no success claimed.')
                if (task.deferred_verification and outcome.attempt is not None
                        and outcome.attempt.action.kind == ActionKind.DONE
                        and outcome.attempt.result.exit_code == 0
                        and outcome.attempt.result.error_type is None):
                    return finish(Termination.SUBMITTED,
                                  "candidate submitted; success requires the external evaluator's separate receipt")
        except KeyboardInterrupt:
            return finish(Termination.INTERRUPTED, "interrupted; only committed steps can be resumed automatically")
        except (DeadlineExceeded, TimeoutError):
            return finish(Termination.TIMEOUT, "run wall-time budget exhausted")
        except ContextBudgetError as error:
            return finish(Termination.BUDGET_EXHAUSTED, str(error))
        except GatewayError as error:
            status = Termination.TIMEOUT if time.monotonic() >= deadline else Termination.MODEL_ERROR
            return finish(status, str(error))
        except (HarnessError, OSError) as error:
            status = Termination.TIMEOUT if time.monotonic() >= deadline else Termination.HARNESS_ERROR
            return finish(status, str(error))

