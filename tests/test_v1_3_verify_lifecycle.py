"""Current verification work, repair/retest handoff and invalid definitions."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.core import (Action, Budget, Check, CheckSpec, HistoryQuery, ModelResponse,
                                TaskContract, Termination, Usage)
from horizon_weaver.policy import ModelPolicy, ScriptedPolicy
from horizon_weaver.verification import verification_environment, verification_state


def check(identity='value', expected='42', **extra):
    return dict(id=identity, kind='text_equals', path='answer', expected=expected, **extra)


def work_state(request):
    header = 'BEHAVIORAL CHECK STATE\n'
    return json.loads(request.context.split(header, 1)[1].split('\n', 1)[0])


class Replies:
    def __init__(self, values):
        self.values, self.requests = iter(values), []

    def complete(self, request, timeout_s):
        self.requests.append(request)
        value = next(self.values)
        if callable(value):
            value = value(request)
        return ModelResponse(content=json.dumps(value), usage=Usage(input_tokens=10, output_tokens=10))

    def describe(self):
        return {'kind': 'verification-handoff-fixture'}


class VerificationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def run_actions(self, values, goal='Write the answer 42 and verify the public goal.'):
        task = TaskContract(task_id='lifecycle', goal=goal,
            acceptance_checks=(Check(id='external', kind='external'),),
            exploration_history=True, verification_policy='behavioral', require_self_check=True,
            budget=Budget(max_steps=20, no_progress_limit=20, context_tokens=14336))
        replies = Replies(values)
        result = NodeAgent(backend='local', policy=ModelPolicy(replies)).run(task, self.work, self.root / 'run')
        return result, read_run(self.root / 'run'), replies

    def test_repaired_candidate_gets_callable_retest_then_submission_without_new_check_definition(self):
        result, graph, replies = self.run_actions([
            dict(kind='write', path='answer', content='bad'),
            dict(kind='verify', checks=[check()]),
            dict(kind='write', path='answer', content='42'),
            lambda request: work_state(request)['suggested_action'],
            lambda request: work_state(request)['suggested_action']])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertFalse(result.verdict.passed)  # External acceptance is still pending.
        self.assertEqual(len(replies.requests), 5)
        self.assertEqual(work_state(replies.requests[2])['phase'], 'current_failure')
        pending = work_state(replies.requests[3])
        self.assertEqual(pending['phase'], 'needs_recheck')
        self.assertEqual(pending['suggested_action'], dict(kind='verify', check_ids=['value']))
        self.assertEqual(pending['checks'][0]['last_status'], 'fail')
        self.assertFalse(pending['checks'][0]['current'])
        self.assertEqual(pending['checks'][0]['changed_files'], ['answer'])
        self.assertIn('Current correctness is unknown', pending['next_step'])
        self.assertEqual(work_state(replies.requests[4])['phase'], 'ready_to_submit')
        self.assertEqual(graph.attempts[3].verification[0].spec, graph.attempts[1].verification[0].spec)

    def test_edit_does_not_promote_a_previous_pass_or_clear_a_previous_failure(self):
        result, graph, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[check()]),
            dict(kind='write', path='answer', content='bad'),
            dict(kind='done'),
            lambda request: work_state(request)['suggested_action'],
            dict(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertEqual(graph.attempts[3].result.error_type, 'verification_required')
        self.assertEqual(work_state(replies.requests[3])['checks'][0]['last_status'], 'pass')
        self.assertEqual(work_state(replies.requests[5])['phase'], 'current_failure')

    def test_inconclusive_check_requests_check_repair_instead_of_blind_repetition(self):
        result, graph, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[dict(id='broken', kind='python_assertions', code='import missing_hw_check_dependency')]),
            dict(kind='verify', checks=[check(replaces=['broken'], target_refs=['G1'],
                basis='Replace the invalid test with the comparison required by G1')]),
            dict(kind='done')])
        pending = work_state(replies.requests[2])
        self.assertEqual(pending['phase'], 'check_inconclusive')
        self.assertIsNone(pending['suggested_action'])
        self.assertIn('not a task counterexample', pending['next_step'])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        phases = {row['id']: row['phase'] for row in verification_state(graph.attempts, graph.snapshot)['checks']}
        self.assertEqual(phases, dict(broken='superseded', value='passed'))

    def test_changed_expectation_without_revision_is_not_disguised_as_readiness(self):
        _, graph, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[check(expected='43')]),
            dict(kind='verify', checks=[check(expected='42')]),
            dict(kind='done'), dict(kind='incomplete')])
        self.assertEqual(work_state(replies.requests[3])['phase'], 'unresolved_expectation')
        self.assertIsNone(work_state(replies.requests[3])['suggested_action'])
        self.assertEqual(graph.attempts[3].result.error_type, 'verification_required')

    def test_same_id_explicit_revision_keeps_the_new_definition_active(self):
        result, graph, _ = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[check(expected='43', target_refs=['G1'])]),
            dict(kind='verify', checks=[check(replaces=['value'], target_refs=['G1'],
                basis='G1 asks for 42; the previous expectation of 43 was wrong')]),
            dict(kind='done')])
        state = verification_state(graph.attempts, graph.snapshot)
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(state['checks'][0]['phase'], 'passed')
        self.assertEqual(state['suggested_action'], dict(kind='done'))

    def test_execution_scope_change_requests_remeasurement_without_claiming_a_new_file_defect(self):
        _, graph, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[dict(id='process-scope', kind='python_assertions',
                code="from pathlib import Path\nassert Path('answer').read_text() == '42'")]),
            dict(kind='command', command='echo observed'), dict(kind='incomplete')])
        pending = work_state(replies.requests[3])
        self.assertEqual(pending['phase'], 'needs_recheck')
        self.assertEqual(pending['checks'][0]['changed_files'], [])
        self.assertIn('execution_scope_changed', pending['checks'][0]['invalidated_by'])
        environment = verification_environment(json.loads(graph.manifest.environment_json), graph.attempts)
        self.assertFalse(verification_state(graph.attempts, graph.snapshot, environment)['ready'])

    def test_revised_expectation_stays_pending_on_the_replacement_after_a_candidate_edit(self):
        result, _, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[check('wrong', expected='43', target_refs=['G1'])]),
            dict(kind='verify', checks=[check('revised', replaces=['wrong'], target_refs=['G1'],
                basis='G1 requires 42, not the previous 43 expectation')]),
            dict(kind='write', path='answer', content='bad'),
            lambda request: work_state(request)['suggested_action'],
            dict(kind='write', path='answer', content='42'),
            lambda request: work_state(request)['suggested_action'],
            lambda request: work_state(request)['suggested_action']])
        pending = work_state(replies.requests[4])
        self.assertFalse(pending['ready'])
        self.assertEqual(pending['suggested_action'], dict(kind='verify', check_ids=['revised']))
        old = next(row for row in pending['checks'] if row['id'] == 'wrong')
        self.assertEqual(old['phase'], 'revision_pending')
        self.assertEqual(old['revision_check_id'], 'revised')
        self.assertEqual(work_state(replies.requests[5])['phase'], 'current_failure')
        self.assertEqual(result.termination, Termination.SUBMITTED)

    def test_placeholder_json_is_a_definition_error_and_recovers_with_input_derived_comparison(self):
        (self.work / 'input.json').write_text('[1,2,3]', encoding='utf-8')
        (self.work / 'output.json').write_text('[2,4,6]', encoding='utf-8')
        code = ("import json\nfrom pathlib import Path\n"
            "original=json.loads(Path('input.json').read_text())\n"
            "actual=json.loads(Path('output.json').read_text())\n"
            "assert actual == [2*x for x in original]")
        result, graph, replies = self.run_actions([
            dict(kind='verify', checks=[dict(id='bad-definition', kind='json_equals',
                path='output.json', expected='[...array of correct values...]')]),
            dict(kind='verify', checks=[dict(id='derived', kind='python_assertions', code=code, target_refs=['G1'])]),
            dict(kind='done')], goal='Double every input.json value and write output.json.')
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(len(graph.attempts), 2)  # Invalid definition executed no action.
        feedback = replies.requests[1].context
        self.assertIn('Invalid verification definition; no check or tool was executed', feedback)
        self.assertIn('json_equals.expected must be concrete valid JSON', feedback)
        self.assertIn('python_assertions', feedback)
        self.assertNotIn('Return one complete JSON action, e.g.', feedback)
        self.assertEqual((self.work / 'input.json').read_text(), '[1,2,3]')

    def test_invalid_candidate_json_is_a_measured_failure_not_a_definition_error(self):
        (self.work / 'answer').write_text('[broken JSON', encoding='utf-8')
        _, graph, replies = self.run_actions([
            dict(kind='verify', checks=[dict(id='json', kind='json_equals', path='answer', expected='[42]')]),
            dict(kind='incomplete')])
        self.assertEqual(graph.attempts[0].verification[0].status, 'fail')
        self.assertEqual(work_state(replies.requests[1])['phase'], 'current_failure')

    def test_invalid_python_in_a_batch_executes_no_check_and_registers_no_blocker(self):
        (self.work / 'answer').write_text('42', encoding='utf-8')
        malformed = 'total=0; for n in [1,2]: total+=n; assert total==3'
        result, graph, replies = self.run_actions([
            dict(kind='verify', checks=[check(), dict(id='broken', kind='python_assertions', code=malformed)]),
            dict(kind='verify', checks=[check()]), dict(kind='done')])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(len(graph.attempts), 2)
        self.assertEqual(replies.requests[1].known_check_ids, ())
        self.assertEqual(work_state(replies.requests[1])['phase'], 'needs_check')
        self.assertIn('Invalid verification definition; no check or tool was executed', replies.requests[1].context)
        self.assertIn('checks.1.code (broken)', replies.requests[1].context)
        self.assertIn('newlines and indentation', replies.requests[1].context)
        self.assertEqual((self.work / 'answer').read_text(), '42')

    def test_python_definition_preflight_never_executes_candidate_or_check_side_effects(self):
        result, graph, _ = self.run_actions([
            dict(kind='verify', checks=[dict(id='broken', kind='python_assertions',
                code="from pathlib import Path; Path('touched').write_text('bad'); assert (")]),
            dict(kind='incomplete')])
        self.assertFalse((self.work / 'touched').exists())
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertEqual(len(graph.attempts), 1)
        self.assertEqual(graph.attempts[0].action.kind.value, 'incomplete')

    def test_valid_python_candidate_failure_remains_a_measured_counterexample(self):
        (self.work / 'answer').write_text('bad', encoding='utf-8')
        _, graph, replies = self.run_actions([
            dict(kind='verify', checks=[dict(id='real', kind='python_assertions',
                code="from pathlib import Path\nassert Path('answer').read_text() == '42'")]),
            dict(kind='incomplete')])
        self.assertEqual(graph.attempts[0].verification[0].status, 'fail')
        self.assertEqual(work_state(replies.requests[1])['phase'], 'current_failure')

    def test_historical_invalid_python_is_readable_and_an_edit_does_not_recommend_rerunning_it(self):
        # Immutable old CheckSpecs intentionally remain decodable. Admission
        # of new ModelPolicy source is a different boundary from replay.
        spec = CheckSpec(id='broken', kind='python_assertions', code='assert (')
        task = TaskContract(task_id='old-definition', goal='Write answer 42.',
            acceptance_checks=(Check(id='external', kind='external'),),
            exploration_history=True, verification_policy='behavioral')
        actions = tuple(Action.model_validate(value) for value in [
            dict(kind='write', path='answer', content='bad'),
            dict(kind='verify', checks=[spec.model_dump(mode='json')]),
            dict(kind='write', path='answer', content='42'), dict(kind='incomplete')])
        NodeAgent(backend='local', policy=ScriptedPolicy(actions)).run(task, self.work, self.root / 'old')
        graph = read_run(self.root / 'old')
        state = verification_state(graph.attempts, graph.snapshot)
        self.assertEqual(state['phase'], 'check_inconclusive')
        self.assertIsNone(state['suggested_action'])
        self.assertEqual(state['checks'][0]['changed_files'], ['answer'])
        self.assertIn('Invalid Python syntax', state['checks'][0]['definition_error'])
        self.assertEqual(graph.attempts[1].verification[0].spec, spec)

    def test_history_returns_the_measured_definition_even_for_a_saved_id_rerun(self):
        code = "from pathlib import Path\nassert Path('answer').read_text() == '42'"
        (self.work / 'answer').write_text('bad', encoding='utf-8')
        _, graph, replies = self.run_actions([
            dict(kind='verify', checks=[dict(id='real', kind='python_assertions', code=code)]),
            dict(kind='verify', check_ids=['real']),
            dict(kind='history', query=dict(check_id='real', steps=[2], limit=1)),
            dict(kind='incomplete')])
        records = json.loads(graph.attempts[2].result.output)['records']
        self.assertEqual(len(records), 1)
        receipt = records[0]['verification'][0]
        self.assertEqual(receipt['spec']['code'], code)
        self.assertEqual(receipt['spec_hash'], graph.attempts[1].verification[0].spec_hash)
        self.assertEqual(receipt['status'], 'fail')
        # The Loop expands saved IDs into concrete checks before execution.
        self.assertEqual(records[0]['step'], 2)
        self.assertEqual(records[0]['action']['checks'][0]['code'], code)
        inspect = work_state(replies.requests[2])['checks'][0]['inspect_action']
        self.assertEqual(inspect, dict(kind='history', query=dict(check_id='real', steps=[2], limit=1)))
        self.assertEqual(graph.query.exploration_history(HistoryQuery(check_id='absent')), ())

    def test_check_history_keeps_the_specific_predicate_even_when_its_attempt_is_in_the_ledger(self):
        _, _, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[check(expected='43', target_refs=['G1'])]),
            dict(kind='incomplete')])
        context = replies.requests[2].context
        self.assertIn('ATTEMPT 2', context)
        self.assertIn('CHECK HISTORY (last measurement; phase describes what remains to do)', context)
        self.assertIn('"expected":"43"', context)


@unittest.skipUnless(os.environ.get('HORIZON_WEAVER_DOCKER_TESTS') == '1', 'opt-in real Docker tests')
class VerificationLifecycleDockerTests(unittest.TestCase):
    def test_failed_container_comparison_retests_after_repair_and_submits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'work'
            work.mkdir()
            task = TaskContract(task_id='docker-lifecycle', goal='Write 42 to answer.',
                acceptance_checks=(Check(id='external', kind='external'),),
                exploration_history=True, verification_policy='behavioral')
            actions = tuple(Action.model_validate(value) for value in [
                dict(kind='write', path='answer', content='0'), dict(kind='verify', checks=[check()]),
                dict(kind='write', path='answer', content='42'),
                dict(kind='verify', check_ids=['value']), dict(kind='done')])
            result = NodeAgent(backend='docker', policy=ScriptedPolicy(actions),
                image=os.environ.get('HORIZON_WEAVER_DOCKER_IMAGE', 'python:3.12-slim')).run(task, work, root / 'run')
            graph = read_run(root / 'run')
            pending = verification_state(graph.attempts[:3], graph.attempts[2].after)
            self.assertEqual(pending['phase'], 'needs_recheck')
            self.assertEqual(pending['suggested_action'], dict(kind='verify', check_ids=['value']))
            self.assertEqual(result.termination, Termination.SUBMITTED)
            self.assertEqual(graph.attempts[3].verification[0].status, 'pass')


if __name__ == '__main__':
    unittest.main(verbosity=2)
