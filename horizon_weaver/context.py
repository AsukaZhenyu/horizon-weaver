"""Pure context compilation; it neither reads files nor executes model calls."""
from __future__ import annotations

import json

from .core import (ActionKind, DecisionInput, GraphView, StateSnapshot, TaskContract,
                   canonical, current_self_checks, current_verdict, digest, submission_readiness, requirement_sources)
from .errors import ContextBudgetError
from .state_queries import ContextCandidate, StateQuery
from .verification import verification_state, verification_environment, verification_followup, goal_feedback

SYSTEM = """Solve the original terminal task using one complete JSON action per decision.
Use the request schema: read(path), write(path,content), command(command), verify, done.
Output JSON directly. Keep action reasoning concise (under 300 characters).
Paths are workspace-relative POSIX paths; use the recorded shell and cwd.
write replaces content; command executes programs or changes permissions.
Use tools to inspect errors, compute results and test the public requirements.
A test must fail on mismatches. Printing values/importing code is inspection.
Derive expectations from public requirements or original inputs; the candidate is not its own oracle.
Prepare fixtures with command; verify must leave candidate files unchanged.
Verify the public goal: deliverables, required behavior, constraints and any performance claim.
After a repair, test the current candidate. A failed predicate may indicate a candidate defect, wrong expectation, environment problem or insufficient evidence.
Only the independent evaluator grants acceptance; self-checks and notes are evidence/claims.
Use GOAL / CURRENT STATE / GAPS throughout the task: distinguish public requirements, Policy interpretations and measured observations.
Choose the next action to resolve a remaining goal or uncertainty. A failed self-check can be wrong; inspect its actual operands and public basis before changing a correct candidate.
Runtime submission permission does not establish goal completion. Before done review deliverables, behavior, constraints and performance; unknown coverage is not a pass.
Optional envelope: {"action":{...},"notes":{"requirements":[{"ref":"G3","evidence_steps":[3],"uncertainty":"..."}]}}.
G/C identify public text. Notes cite executed observations; simple tasks need no notes.
When stuck, update a short understanding in the same decision: prior purpose, observed consequence, revised hypothesis, remaining requirement and next discriminating step.
Before repeating an action, identify changed conditions or the new evidence expected; otherwise consider another direction. Repeating a test after repair can be appropriate.
Separate changes to files from information learned. Revise hypotheses when evidence contradicts them.
Command/check actions may request timeout_s. Notes may request next_output_tokens/next_timeout_s within budgets.
"""

GOAL_SYSTEM = """Public goal conditions retain explicit deliverables/interfaces and behavioral criteria across changes.
An interface or missing-file contradiction is a candidate defect; check-definition issues describe repairing the test while preserving the public API.
done carries review:[{ref,status,satisfied_when,evidence_steps,evidence_check_ids,needed_information}] for the listed refs. Cite current executed facts; state remaining uncertainty. Related refs belong to the same criterion. Ordinary actions do not need a review.
"""


def token_bound(text: str) -> int:
    # Conservative UTF-8 byte estimate, not a claim to match the model tokenizer.
    return len(text.encode("utf-8"))


def excerpt(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    marker = f"\n[truncated; full {len(data)} bytes retained in journal]\n"
    if limit < token_bound(marker):
        return '[truncated]'[:max(0, limit)]
    available = max(0, limit - token_bound(marker))
    head = available // 2
    tail = available - head
    return (data[:head].decode("utf-8", errors="ignore") + marker
            + (data[-tail:].decode("utf-8", errors="ignore") if tail else ""))


class ContextCompiler:
    def __init__(self, environment: dict, system: str = SYSTEM, strategy: str = "state"):
        if strategy not in ("state", "recent"):
            raise ValueError("context strategy must be state or recent")
        self.environment = canonical(environment)
        self.system = system
        self.strategy = strategy

    def build(self, task: TaskContract, view: GraphView, snapshot: StateSnapshot,
              step: int, remaining_tokens: int) -> DecisionInput:
        system = self.system + ('\n' + GOAL_SYSTEM if task.goal_conditions else '')
        verdict = current_verdict(task, view.verdict, snapshot)
        refs = requirement_sources(task)
        source_text = "\n".join(f"[G{number}] {line}" if line.strip() else ""
                                for number, line in enumerate(task.goal.splitlines(), 1))
        source_text += "".join(f"\n[C{number}] {value}" for number, value in enumerate(task.constraints, 1))
        contract = task.model_dump(exclude={"goal", "constraints", "goal_conditions"}, exclude_defaults=True, mode="json")
        if task.deferred_verification:
            # External evaluator identities are lifecycle metadata, not saved
            # definitions callable by this Policy. The full contract stays in IR.
            contract.pop('acceptance_checks', None)
        mandatory = ("TASK CONTRACT\n" + canonical(contract)
            + "\nORIGINAL PUBLIC GOAL / CONSTRAINTS (G = goal line, C = constraint)\n" + source_text
            + "\nENVIRONMENT\n" + self.environment + "\nSTATE VERSION\n" + snapshot.fingerprint
            + "\nUNSATISFIED INLINE REQUIREMENTS\n" + canonical([key for key in verdict.unsatisfied_requirements
                if key not in {c.id for c in task.acceptance_checks if c.kind == 'external'}]))
        if view.feedback:
            mandatory += "\nFEEDBACK\n" + excerpt(view.feedback, 600)
        environment_hash = verification_environment(json.loads(self.environment), view.attempts)
        state = verification_state(view.attempts, snapshot, environment_hash)
        query = StateQuery.from_view(task, view, snapshot)
        goal_report = query.goal_assessment(environment_hash) if task.verification_policy == 'behavioral' else None
        review_refs = tuple(goal_report['review_refs']) if goal_report is not None and task.require_goal_review else ()
        goal_section = None
        if goal_report is not None:
            # Always deliver a small goal/current-state/gap index. Detailed
            # evidence competes only after the latest actual reply has space.
            goal_shown = dict(requirements=[dict(ref=r['ref'], status=r['status'],
                needed_information=r['needed_information'][:180], evidence_ids=[e['id'] for e in r['evidence'][-2:]])
                for r in goal_report['requirements'][:3]],
                requirements_omitted=max(0, len(goal_report['requirements'])-3),
                check_issues=[dict(id=r['id'], status=r['status'], current=r['current'],
                    needed_information=r['needed_information'][:180]) for r in goal_report['check_issues'][-1:]],
                check_issues_omitted=max(0, len(goal_report['check_issues'])-1),
                current_state=dict(observed_files=goal_report['current_state']['observed_files'], observations=[]),
                source_review=dict(unmapped_count=len(goal_report['source_review']['unmapped_refs'])),
                coverage='Listed predicates only; full semantic goal coverage unknown.')
            goal_shown['details_omitted'] = False
            goal_header = '\nGOAL / CURRENT STATE / GAPS\n'
            while token_bound(goal_header + canonical(goal_shown)) > 1000:
                goal_shown['details_omitted'] = True
                if goal_shown['check_issues']:
                    goal_shown['check_issues'].pop(0)
                    goal_shown['check_issues_omitted'] += 1
                elif goal_shown['requirements']:
                    goal_shown['requirements'].pop()
                    goal_shown['requirements_omitted'] += 1
                elif goal_shown['current_state']['observations']:
                    goal_shown['current_state']['observations'].pop(0)
                else:
                    break
            goal_section = goal_header + canonical(goal_shown)
            mandatory += goal_section
            if goal_report['objectives']:
                goals = goal_feedback(goal_report, limit=6)['objectives']
                facts = [dict(ref=r['ref'], kind=r['kind'], status=r['status'],
                    fact=r['fact'][:160], gap=r['gap'][:240]) for r in goals]
                condition_header = '\nPUBLIC GOAL CONDITIONS / CONCRETE GAPS\n'
                shown_goals = dict(conditions=facts, omitted=max(0,len(goal_report['objectives'])-len(facts)),
                    review_refs=list(review_refs), missing_review_refs=goal_report['missing_review_refs'],
                    review='done.review: ref/status/satisfied_when/evidence_steps/evidence_check_ids. Ordinary actions need no review. Original source defines each criterion; semantic reviews remain claims.')
                while token_bound(condition_header+canonical(shown_goals)) > 1600 and len(facts)>1:
                    facts.pop()
                    shown_goals['omitted'] += 1
                mandatory += condition_header+canonical(shown_goals)
                compact_index = '\nGOAL / CURRENT STATE / GAPS\n' + canonical(dict(
                    observed_files=goal_report['current_state']['observed_files'],
                    coverage='Public operands observed separately; semantic reviews remain conditional.'))
                mandatory = mandatory.replace(goal_section, compact_index, 1)
                goal_section = None
            diagnoses = goal_report['invocation_issues'][-2:]
            if diagnoses:
                mandatory += '\nCHECK DEFINITION / INVOCATION GAPS\n' + canonical(diagnoses)
        obstacle = query.decision_obstacle(environment_hash) if task.exploration_history else None
        # Do not expand a protocol/time-out recovery into another reflection.
        reflection_required = bool(obstacle and not view.feedback.startswith('Model decision timed out'))
        verification_section = None
        check_interface = None
        history_interface = None
        if task.deferred_verification:
            readiness = submission_readiness(task, view.attempts, snapshot)
            if task.verification_policy == 'behavioral':
                readiness = readiness.model_copy(update={'phase': 'ready_to_submit' if state['ready'] else 'self_test_required'})
                # A bounded index of evidence, never whole receipts/test bodies.
                shown = verification_followup(state, limit=4)
                if task.goal_conditions:
                    # A single concise check index leaves room for goal facts,
                    # latest replies and exploration history instead of repeating
                    # three large views of the same ledger.
                    shown['checks'] = [dict(id=r['id'], phase=r['phase'], step=r['step']) for r in shown['checks']]
                    for key in ('goal_evidence', 'blockers', 'suggested_action'):
                        shown.pop(key, None)
                shown['meaning'] = 'Recorded predicate work, not proof of goal completion. Consult the goal assessment for submission constraints.'
                if not task.require_self_check:
                    shown['predicate_ready'] = shown.pop('ready')
                    if shown['phase'] == 'ready_to_submit':
                        shown['phase'] = 'measured_predicates_passed'
                    shown['next_step'] = ('Use the goal assessment and measured operands to choose the next information or repair. '
                        'Unsupported self-check expectations are advisory; they do not become public requirements by failing.')
                if task.exploration_history:
                    for row in shown['checks']:
                        if row['phase'] not in ('passed', 'superseded'):
                            row['inspect_action'] = dict(kind='history',
                                query=dict(check_id=row['id'], steps=[row['step']], limit=1))
                verification_section = '\nBEHAVIORAL CHECK STATE\n' + canonical(shown)
                mandatory += verification_section
            submission = dict(phase=readiness.phase)
            if goal_report is not None:
                submission.update({k: goal_report['submission'][k] for k in
                    ('allowed', 'hard_blockers', 'completion_established')})
                if task.goal_conditions:
                    submission['hard_blocker_count'] = len(submission.pop('hard_blockers'))
                if not task.require_self_check:
                    submission['phase'] = ('goal_review' if submission['allowed'] else 'public_constraint_unsatisfied')
            mandatory += '\nSUBMISSION READINESS\n' + canonical(submission)
            mandatory += '\nHidden grading runs after done; submission is not acceptance.'
            if goal_report is not None and not task.require_self_check:
                mandatory += ('\nThe runtime checks fixed public constraints; self-defined checks remain evidence with their own uncertainty. '
                    'If the full public goal is fulfilled, return {"kind":"done"}. '
                    'Do not wait for hidden grading or repair unsupported expectations merely to satisfy a submission protocol.')
            elif readiness.phase == 'ready_to_submit':
                mandatory += '\nSubmission is allowed. If the original requirements are fulfilled, return {"kind":"done"}. Repeating checks does not add coverage.'
            elif readiness.phase == 'self_test_required':
                mandatory += '\nUse the verification phase to choose the next measurement. Historical failures are not measurements of an edited candidate.'
        if task.verification_policy == 'behavioral':
            check_interface = ('\nCHECK INTERFACE\nverify/done: checks:[{id,kind,...}] or check_ids:[saved_id] to rerun saved definitions. '
                'Kinds: text_equals/json_equals(path,expected string); python_assertions(code); '
                'json_equals requires concrete valid expected JSON; never descriptive placeholders. '
                'For large/input-derived outputs, use python_assertions to compute expectations from original inputs and compare required properties. '
                'process(argv array, expected_exit_code=0, optional expected_stdout/expected_stderr/stdin). '
                'process executes argv without a shell; test the invocation required by the public goal. '
                'Use public requirements/inputs for expected values. target_refs and basis record their source. '
                'Optional controls:[{name,overrides:{path:wrong_content}}] test sensitivity on copies. '
                'Checks measure predicates; target_refs alone do not establish relevance or correct expectations. '
                'Repair and rerun a valid failure. For a wrong expectation revise the check; optional replaces:[old_id] records the revision lineage, with target_refs and public basis. '
                'Controls are optional; fixed public contract checks cannot be replaced. '
                'Put assertions inside checks[].code; editing tests in the product does not change a saved check. '
                'done can execute checks in the same request; incomplete hands back an unfinished candidate.')
            mandatory += check_interface
        elif task.require_self_check:
            mandatory += '\nSUBMISSION GATE\nA passing current-state verify is required; no current-state check may fail.'
        else:
            mandatory += '\nSELF-CHECK POLICY\nChecks are optional. Exit zero alone does not prove task completion.'
        if task.exploration_history:
            history_interface = ('\nHISTORY INTERFACE\n{"kind":"history","query":{"path":"relative/file","failures_only":true,"limit":5}} '
                'retrieves recorded facts. query.check_id with steps retrieves that measurement\'s full saved definition and result, even after a check_ids rerun. '
                'Optional notes.exploration carries method/hypothesis/expected_observation/evidence_steps. '
                'notes.requirements retains key deliverables/behaviors with ref or exact quote; optional interpretation, satisfied_when, needed_information, evidence_steps and uncertainty describe the remaining goal. '
                'After an obstacle, notes.understanding updates current_problem/interpretation/next_focus, '
                'citing prior evidence_steps and open_refs (G/C). This is a revisable interpretation, not a verdict. '
                'Keep the overall goal in view; a successful command or narrow check need not solve the remaining problem.')
            mandatory += history_interface
            if reflection_required:
                update = ('\nDECISION UPDATE\nReturn {"notes":{"understanding":{'
                    '"current_problem":"remaining goal or obstacle","interpretation":"what the observed result changes; unknown causes stay unknown",'
                    '"next_focus":"next step and why; changed condition/new evidence if retrying","evidence_steps":[prior_executed_step]}},"action":{...}}. '
                    'Use short phrases, about 80 words total. This is the same decision, not another reflection turn. '
                    'Review the full public goal before done; add notes.requirements for remaining obligations when useful.')
                if task.goal_conditions:
                    update='\nDECISION UPDATE\nUse notes.understanding: current_problem, interpretation, next_focus, evidence_steps (prior executed facts). Keep it short; choose an action resolving a goal gap or new uncertainty.'
                mandatory += update
                if state['blockers'] and task.verification_policy == 'behavioral' and task.require_self_check:
                    mandatory += ('\nCHECK CORRECTION FORMAT\nIf your interpretation says an old expectation is wrong, '
                        'the next corrected checks[] item must include replaces:["old_check_id"], target_refs:["public_ref"], '
                        'basis:"public requirement or input derivation", plus the actual check operands. '
                        'A new ID alone leaves the old expectation unresolved. Use real saved IDs and G/C refs, not these placeholders.')
        if task.goal_conditions:
            if check_interface:
                compact = ('\nCHECK INTERFACE\nverify/done: checks:[{id,kind,...}] or check_ids:[saved_id]. '
                    'text_equals/json_equals: path and concrete expected string. python_assertions: code with assertions. '
                    'process: argv, expected_exit_code=0, optional expected_stdout/expected_stderr/stdin. '
                    'Derive expectations from public input/goal; target_refs:[G/C] records the association. '
                    'Repair check logic in checks[].code; optional replaces/basis records corrected expectations. '
                    'Check files must stay unchanged. Saved definitions are available through history(query:check_id).')
                mandatory = mandatory.replace(check_interface, compact, 1)
                check_interface = compact
            if history_interface:
                compact = ('\nHISTORY INTERFACE\nhistory(query:path/ref/steps/check_id,limit) retrieves recorded facts. '
                    'notes.understanding revises current_problem/interpretation/next_focus with evidence_steps. '
                    'notes.requirements reviews public criteria; notes never grant acceptance.')
                mandatory = mandatory.replace(history_interface, compact, 1)
                history_interface = compact
        hint = view.notes if view.notes_step == step - 1 else None
        output_limit = min(task.budget.output_tokens,
            hint.next_output_tokens if hint and hint.next_output_tokens else task.budget.decision_output_tokens)
        model_timeout = min(task.budget.max_model_timeout_s,
            hint.next_timeout_s if hint and hint.next_timeout_s else task.budget.model_timeout_s)
        mandatory += f'\nTHIS DECISION BUDGET\nmax_output_tokens={output_limit}; timeout_s={model_timeout}. Return one compact complete action.'
        input_limit = min(task.budget.context_tokens, remaining_tokens - output_limit)
        history_fallback = '\nEXPLORATION HISTORY: no additional summaries fit; history query remains available.\n'
        # The workflow summary must not displace a short current file read.
        # Keep its phase and callable action; omit complete detail rows first.
        if verification_section and (view.attempts or task.goal_conditions):
            latest = view.attempts[-1] if view.attempts else None
            read_size = token_bound(latest.result.output) if latest else 0
            reserve = (read_size + 500 if latest and latest.action.kind == ActionKind.READ and read_size <= 6000 else 600)
            if task.exploration_history:
                reserve += token_bound(history_fallback)
            if goal_section:
                while token_bound(system) + token_bound(mandatory) + 128 + reserve > input_limit:
                    if goal_shown['requirements']:
                        goal_shown['requirements'].pop()
                        goal_shown['requirements_omitted'] += 1
                    elif goal_shown['check_issues']:
                        goal_shown['check_issues'].pop()
                        goal_shown['check_issues_omitted'] += 1
                    elif goal_shown['current_state']['observations']:
                        goal_shown['current_state']['observations'].pop()
                    else:
                        break
                    goal_shown['details_omitted'] = True
                    compact_goal = '\nGOAL / CURRENT STATE / GAPS\n' + canonical(goal_shown)
                    mandatory = mandatory.replace(goal_section, compact_goal, 1)
                    goal_section = compact_goal
            while shown['checks'] and token_bound(system) + token_bound(mandatory) + 128 + reserve > input_limit:
                shown['checks'].pop()
                shown['checks_omitted'] += 1
                compact = '\nBEHAVIORAL CHECK STATE\n' + canonical(shown)
                mandatory = mandatory.replace(verification_section, compact, 1)
                verification_section = compact
            if check_interface and token_bound(system) + token_bound(mandatory) + 128 + reserve > input_limit:
                compact_interface = ('\nCHECK INTERFACE\nverify/done accepts checks:[{id,kind,...}] or check_ids:[saved_id]. '
                    'text_equals/json_equals: path, concrete expected string; JSON must be valid, never a placeholder. '
                    'python_assertions: code with assertions comparing original-input expectations to the candidate. '
                    'process: argv (no shell), expected_exit_code=0, optional expected_stdout/expected_stderr/stdin. '
                    'Repair a candidate and retest. Correct a wrong check and cite its public basis; optional replaces records lineage. '
                    'Fixed contract checks cannot be replaced; Policy expectations are not task requirements. '
                    'Controls are optional: controls:[{name,overrides:{"relative/file":"wrong content"}}]. '
                    'Change checks[].code to repair check logic; target_refs do not prove coverage. incomplete returns unfinished work.')
                mandatory = mandatory.replace(check_interface, compact_interface, 1)
            if history_interface and token_bound(system) + token_bound(mandatory) + 128 + reserve > input_limit:
                compact_history = ('\nHISTORY INTERFACE\nhistory(query: path/ref/steps/check_id, limit) retrieves saved facts. '
                    'Optional notes.requirements describes goal interpretation, satisfy conditions and needed information; '
                    'notes.understanding records a revisable hypothesis citing executed evidence. No claim establishes acceptance.')
                mandatory = mandatory.replace(history_interface, compact_history, 1)
            if token_bound(system) + token_bound(mandatory) + 128 + reserve > input_limit:
                shown['meaning'] = 'Predicate evidence; full goal coverage unknown.'
                shown['next_step'] = 'Current correctness is unknown unless fresh evidence supports it. Review goal gaps and relevant saved checks.'
                compact = '\nBEHAVIORAL CHECK STATE\n' + canonical(shown)
                mandatory = mandatory.replace(verification_section, compact, 1)
                verification_section = compact
        bound = token_bound(system) + token_bound(mandatory) + 128
        if bound > input_limit:
            raise ContextBudgetError('mandatory contract/context exceeds remaining input budget')
        # Keep a retrieval notice when optional check details exhaust space.
        history_reserve = token_bound(history_fallback) if task.exploration_history else 0
        if bound + history_reserve > input_limit:
            history_reserve = 0
        bound += history_reserve
        sections, ledger, visible, visible_writes = [], [], [], []
        attempts_by_step = {item.step: item for item in view.attempts}

        def add(entry):
            nonlocal bound
            cost = token_bound(entry)
            if bound + cost > input_limit:
                return False
            sections.append(entry)
            bound += cost
            return True

        def remember_read(record):
            item = attempts_by_step.get(record.get('step'))
            if (item is not None and record.get('output_complete')
                    and record.get('output') == item.result.output and query.read_is_current(item)):
                visible.append(item.step)

        def render_candidate(candidate, maximum):
            nonlocal bound
            item, action = candidate.attempt, candidate.attempt.action
            data = dict(kind=action.kind.value)
            if action.path: data['path'] = action.path
            if action.command: data['command'] = excerpt(action.command, 400)
            if action.checks: data['checks'] = [dict(id=c.id, kind=c.kind) for c in action.checks]
            if action.check_ids: data['check_ids'] = list(action.check_ids)
            if action.kind == ActionKind.WRITE:
                data.update(written_bytes=token_bound(action.content or ''), content_sha256=digest(action.content or ''))
            metadata = '\n'.join(f'{key}={canonical(value)}' for key, value in data.items())
            prefix = (f'\nATTEMPT {item.step}\nOBSERVED ACTION (already attempted; not a new proposal)\n'
                + metadata + '\nexecution_signature=' + action.execution_signature
                + f'\nexit={item.result.exit_code} error={item.result.error_type}'
                + f' changed={",".join(item.delta.changed_files)} executed={item.tool_executed}'
                + f' selected_by={candidate.reason}\n')
            if token_bound(prefix) + 80 > maximum:
                return False
            output = item.result.output
            if (self.strategy == 'state' and action.kind == ActionKind.READ and item.result.exit_code == 0
                    and (item.before.file(action.path) != snapshot.file(action.path)
                         or item.after.file(action.path) != snapshot.file(action.path))):
                output = '[Historical file version; body omitted. Read the current version if needed.]'
            # Preserve a short structured counterexample without slicing a long
            # serialized receipt through the middle of its result.
            if item.verification:
                results = []
                for receipt in item.verification:
                    try: detail = json.loads(receipt.result.output)
                    except ValueError: detail = dict(output=receipt.result.output)
                    if isinstance(detail, dict):
                        predicates = detail.get('checks', [])
                        if predicates:
                            failed = [p for p in predicates if not p.get('passed')]
                            detail = dict(detail, checks=(failed[:4] + [p for p in predicates if p.get('passed')][:2]),
                                          predicates_total=len(predicates))
                        if 'output' in detail: detail['output'] = excerpt(str(detail['output']), 600)
                        if 'invocations' in detail: detail['invocations'] = detail['invocations'][-4:]
                    results.append(dict(id=receipt.spec.id, status=receipt.status,
                        behavioral=receipt.behavioral, sensitive=receipt.sensitive,
                        detail=detail))
                    if receipt.status == 'inconclusive':
                        results[-1]['definition'] = dict(kind=receipt.spec.kind,
                            code=excerpt(receipt.spec.code, 400) if receipt.spec.code else None,
                            definition_complete=token_bound(receipt.spec.code) <= 400,
                            inspect_action=dict(kind='history',
                                query=dict(check_id=receipt.spec.id, steps=[item.step], limit=1))
                                if task.exploration_history else None)
                output = canonical(results)
            if newest is candidate and token_bound(output) <= 6000:
                # A complete fresh observation wins over the reserved reasoning
                # space whenever it fits the actual remaining input budget.
                maximum = max(maximum, min(input_limit - bound, token_bound(prefix) + token_bound(output) + 100))
            detailed = newest is candidate or (candidate.priority >= 90 and token_bound(output) <= 6000)
            rendered = excerpt(output, min(6000 if detailed else 1600,
                                           max(0, maximum - token_bound(prefix)
                                               - (0 if action.kind == ActionKind.READ else 100))))
            entry = prefix + rendered
            source_complete = False
            if action.kind == ActionKind.WRITE:
                if candidate.current_write:
                    body = action.content or ''
                    source = excerpt(body, max(0, min(1600 if token_bound(body) > 6000 else 4000,
                                                    maximum - token_bound(entry) - 100)))
                    entry += '\nCURRENT WRITTEN SOURCE\n' + source
                    source_complete = source == body
                else:
                    entry += '\n[Historical or unconfirmed write; source body omitted.]'
            if token_bound(entry) > maximum or bound + token_bound(entry) > input_limit:
                return False
            ledger.append((item.step, entry))
            bound += token_bound(entry)
            if candidate.current_read and rendered == item.result.output:
                visible.append(item.step)
            if action.kind == ActionKind.HISTORY and rendered == item.result.output:
                try:
                    for record in json.loads(item.result.output).get('records', []): remember_read(record)
                except (ValueError, AttributeError): pass
            if source_complete: visible_writes.append(item.step)
            return True

        candidates = (query.context_candidates() if self.strategy == 'state' else
            tuple(ContextCandidate(item, 'recent_history', 100 if item is view.attempts[-1] else 0,
                                   query.read_is_current(item), query.write_is_current(item))
                  for item in reversed(view.attempts)))
        # Reserve the newest actual tool reply before bookkeeping/history.
        newest = next((c for c in candidates if view.attempts and c.attempt.step == view.attempts[-1].step), None)
        if newest:
            available = input_limit - bound
            reserve = (0 if newest.current_read and token_bound(newest.attempt.result.output) <= 6000
                       else min(1600, available // 3))
            render_candidate(newest, available - reserve)
        if goal_report is not None and not task.goal_conditions:
            detail = goal_feedback(goal_report, limit=3)
            for observation in detail['current_state']['observations']:
                observation.pop('output', None)
            detail_header = '\nGOAL EVIDENCE / INFORMATION NEEDED (conditional support; not full acceptance)\n'
            capacity = min(2200, max(0, input_limit - bound - 600))
            while token_bound(detail_header + canonical(detail)) > capacity:
                if detail['check_issues']:
                    detail['check_issues'].pop(0)
                    detail['check_issues_omitted'] += 1
                elif detail['requirements']:
                    detail['requirements'].pop()
                    detail['requirements_omitted'] += 1
                elif detail['current_state']['observations']:
                    detail['current_state']['observations'].pop(0)
                else:
                    break
            if token_bound(detail_header + canonical(detail)) <= capacity:
                add(detail_header + canonical(detail))
        if task.exploration_history:
            reasoning = query.reasoning_working_set(environment_hash=environment_hash)
            header = '\nTASK SITUATION / EXPLORATION REASONING (observations and Policy claims distinguished)\n'
            capacity = min(3200, max(0, input_limit - bound - 600))
            # Keep valid structured rows. Do not byte-slice JSON into an
            # apparent claim whose qualifications or provenance were dropped.
            chain_total = len(reasoning['exploration_chain'])
            reasoning['chain_rows_omitted'] = 0
            protected = {b['step'] for b in state['blockers']}
            while reasoning['exploration_chain'] and token_bound(header + canonical(reasoning)) > capacity:
                chain = reasoning['exploration_chain']
                latest_step = max(row['step'] for row in chain)
                removable = next((i for i, row in enumerate(chain)
                                  if row['step'] not in protected and row['step'] != latest_step), len(chain) - 1)
                chain.pop(removable)
                reasoning['chain_rows_omitted'] = chain_total - len(chain)
            if token_bound(header + canonical(reasoning)) > capacity and reasoning['understanding']:
                claim = reasoning['understanding']
                for name in ('approach', 'current_problem', 'interpretation', 'next_focus'):
                    if name in claim: claim[name] = excerpt(claim[name], 180)
            goal_review = reasoning['overview']['goal_review']
            while goal_review['obligations'] and token_bound(header + canonical(reasoning)) > capacity:
                goal_review['obligations'].pop(0)
                goal_review['obligations_omitted'] += 1
            if token_bound(header + canonical(reasoning)) <= capacity:
                add(header + canonical(reasoning))
            else:
                add('\nTASK SITUATION: detailed reasoning omitted by budget; original requirements and latest reply retain priority.\n')
        saved = {}
        if task.verification_policy == 'behavioral':
            for item in view.attempts:
                for receipt in item.verification:
                    saved[receipt.spec.id] = dict(id=receipt.spec.id, kind=receipt.spec.kind, step=item.step)
            index, included = [], 0
            capacity = min(1200, max(0, input_limit - bound - 100))
            header = '\nSAVED CHECK DEFINITIONS (callable by check_ids; evidence validity shown separately)\n'
            for record in sorted(saved.values(), key=lambda value: -value['step'])[:8]:
                if token_bound(header + canonical(index + [record])) + 80 > capacity:
                    continue
                index.append(record)
                included += 1
            add(header + canonical(dict(checks=index, omitted=len(saved)-included)))
        last_failure = next((a for a in reversed(view.attempts)
            if a.action.kind in (ActionKind.COMMAND, ActionKind.VERIFY) and a.tool_executed is not False
            and a.after.fingerprint == snapshot.fingerprint
            and (a.result.error_type or a.result.exit_code not in (None, 0))), None)
        if last_failure:
            add(f'\nLAST EXECUTED FAILURE ON CURRENT FILE STATE: recorded step {last_failure.step}\n')
        for failure in query.repeated_failures()[:2]:
            add('\nREPEATED FAILURE FACTS (files only; process/network state unknown)\n'
                + canonical(dict(count=failure['count'], steps=failure['steps'],
                                 exit=failure['exit'], error=failure['error'], output=excerpt(failure['output'], 200))))

        # Keep an older unresolved counterexample with its actual predicate.
        seen_failures = set()
        for blocker in state['blockers']:
            key = (blocker['id'], blocker['step'])
            if key in seen_failures: continue
            seen_failures.add(key)
            item = attempts_by_step.get(blocker['step'])
            if item is None: continue
            receipt = next((r for r in item.verification if r.spec.id == blocker['id']), None)
            if receipt is None: continue
            changed = sorted({p for a in view.attempts if a.step > item.step for p in a.delta.changed_files})
            predicate = receipt.spec.model_dump(exclude_defaults=True, mode='json')
            for key in ('code', 'command', 'expected', 'stdin'):
                if key in predicate: predicate[key] = excerpt(predicate[key], 800)
            predicate.pop('controls', None)
            data = dict(id=receipt.spec.id, step=item.step, last_status=receipt.status,
                phase=blocker['phase'], predicate=predicate, changed_since=changed, current=blocker['current'])
            limit = min(2200, max(0, (input_limit - bound) // 2))
            add('\nCHECK HISTORY (last measurement; phase describes what remains to do)\n'
                + excerpt(canonical(data), max(0, limit // 2)) + '\n'
                + excerpt(receipt.result.output, max(0, limit // 2)))
            if len(seen_failures) >= 2: break

        changed = {p for a in view.attempts[-5:] for p in a.delta.changed_files}
        priority = changed | set(task.expected_artifacts)
        files = sorted(snapshot.files, key=lambda item: (item.path not in priority, item.path))
        inventory_limit = min(2400, max(0, (input_limit - bound) // 3))
        inventory, included = '\nFILES (current state)\n', 0
        for file in files:
            row = f'{file.path} {file.kind} mode={file.mode:04o} size={file.size} sha256={file.sha256[:16]}\n'
            if token_bound(inventory + row) + 90 > inventory_limit: break
            inventory += row
            included += 1
        if included < len(files): inventory += f'[{len(files)-included} file entries omitted; inspect with command]\n'
        add(inventory)
        if task.verification_policy != 'behavioral':
            for receipt in current_self_checks(view.attempts, snapshot)[-3:]:
                add('\nSELF-CHECK (requirement coverage unknown)\n' + canonical(dict(
                    classification=receipt.classification, classification_reason=receipt.classification_reason,
                    command_ok=receipt.result.exit_code == 0 and not receipt.result.error_type and not receipt.result.outcome_unknown)))
        for evidence in verdict.evidence:
            if evidence.passed or evidence.result.error_type == 'external_verification_pending': continue
            add(f'\nFAILED ACCEPTANCE {evidence.check_id}\n' + excerpt(evidence.result.output, 1200))
        for index, candidate in enumerate(candidates):
            if any(number == candidate.attempt.step for number, _ in ledger): continue
            pending = sum(not any(number == other.attempt.step for number, _ in ledger) for other in candidates[index+1:])
            reserve = min(pending * 400, max(0, (input_limit - bound) // 3))
            render_candidate(candidate, max(0, input_limit - bound - reserve - (600 if task.exploration_history else 0)))
        if view.notes:
            entries = []
            for note in view.notes.requirements:
                evidence = []
                for number in dict.fromkeys((*note.check_steps, *note.evidence_steps)):
                    item = attempts_by_step.get(number)
                    if item:
                        evidence.append(dict(step=number, kind=item.self_check.classification if item.self_check else item.action.kind.value, coverage='unknown',
                            command_ok=item.result.exit_code == 0 and not item.result.error_type and not item.result.outcome_unknown,
                            current=query.read_is_current(item) if item.action.kind == ActionKind.READ else item.after.fingerprint == snapshot.fingerprint,
                            exit=item.result.exit_code, error=item.result.error_type))
                entries.append(dict(ref=note.ref, quote=note.quote or refs.get(note.ref, ''),
                                    uncertainty=note.uncertainty, evidence=evidence))
            add('\nPUBLIC REQUIREMENT NOTES (policy coverage claims; coverage unknown)\n'
                + excerpt(canonical(dict(step=view.notes_step, current=view.notes_state_version == snapshot.fingerprint,
                                         requirements=entries)), 1600))
        if task.exploration_history:
            # Compact old consequences once; recent replies already have space.
            history = query.exploration_working_set(environment_hash=environment_hash)
            bound -= history_reserve
            history_budget = min(2000, max(0, input_limit - bound - 100))
            used, included = 0, 0
            for item in history:
                if any(number in item['matching_steps'] for number, _ in ledger): continue
                action = {key:value for key,value in item['action'].items() if value not in ('',None,[],{})}
                if action.get('command'): action['command'] = excerpt(action['command'], 400)
                if action.get('checks'): action['checks'] = [dict(id=c['id'],kind=c['kind']) for c in action['checks']]
                facts = dict(step=item['step'], execution_signature=item['execution_signature'], observed_action=action,
                    exit=item['exit_code'], error=item['error_type'], executed=item['tool_executed'],
                    output=excerpt(item['output'], 500), output_complete=item['output_complete'] and token_bound(item['output']) <= 500,
                    matching_steps=item['matching_steps'], input_state_current=item['input_state_current'],
                    changes_since=item['changes_since'])
                entry = canonical(facts) + '\n'
                if used + token_bound(entry) > history_budget: continue
                if not included:
                    if not add('\nEXPLORATION HISTORY (related recorded facts; process/network freshness unknown)\n'): break
                if add(entry):
                    used += token_bound(entry)
                    included += 1
                    remember_read(facts)
            if not included: add(history_fallback)
        if len(ledger) < len(view.attempts):
            add(f'\n[{len(view.attempts)-len(ledger)} attempts omitted; full facts retained in journal]\n')
        # Keep the visible chronology while allocating space by evidence value.
        context = mandatory + ''.join(entry for _, entry in sorted(ledger)) + ''.join(sections)
        return DecisionInput(step=step, system=system, context=context, input_token_bound=bound,
            max_output_tokens=min(output_limit, remaining_tokens-bound), model_timeout_s=model_timeout,
            visible_observation_steps=tuple(sorted(set(visible))), visible_write_steps=tuple(sorted(set(visible_writes))),
            selected_attempt_steps=tuple(sorted(number for number,_ in ledger)), state_version=snapshot.fingerprint,
            exploration_history=task.exploration_history, verification_policy=task.verification_policy,
            known_check_ids=tuple(sorted(saved)), reflection_required=reflection_required,
            goal_review_refs=review_refs)
