"""Goal-based check correction and evidence updates in the ordinary decision."""
import json
import tempfile
import unittest
from pathlib import Path

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.core import (Budget, Check, ModelResponse, TaskContract, Termination,
                                 Usage, action_schema, canonical)
from horizon_weaver.policy import ModelPolicy
from horizon_weaver.verification import verification_state
from scripts.v1_terminal_bench_compare import repetition_metrics


class Replies:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []

    def complete(self, request, timeout_s):
        self.requests.append(request)
        return ModelResponse(content=json.dumps(next(self.values)), usage=Usage(input_tokens=10, output_tokens=10))

    def describe(self):
        return {'kind': 'recorded-fixture'}


class GoalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def run_actions(self, values, checks=None):
        task = TaskContract(task_id='goal', goal='Write 42.\nDeliver answer and preserve input.',
            expected_artifacts=('answer',), acceptance_checks=checks or (Check(id='external', kind='external'),),
            exploration_history=True, verification_policy='behavioral', require_self_check=True,
            budget=Budget(max_steps=20, no_progress_limit=20, context_tokens=14336))
        replies = Replies(values)
        result = NodeAgent(backend='local', policy=ModelPolicy(replies)).run(task, self.work, self.root / 'run')
        return result, read_run(self.root / 'run'), replies

    def test_wrong_expectation_can_be_corrected_from_goal_without_negative_control(self):
        corrected = dict(id='correct', kind='text_equals', path='answer', expected='42',
            replaces=['wrong'], target_refs=['G1'], basis='G1 asks for 42; the previous 43 expectation was unsupported')
        result, graph, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[dict(id='wrong', kind='text_equals', path='answer', expected='43', target_refs=['G1'])]),
            dict(action=dict(kind='verify', checks=[corrected]), notes=dict(understanding=dict(
                current_problem='Wrong check', interpretation='The measured mismatch is against 43, not G1',
                next_focus='Compare to the public 42', evidence_steps=[2], open_refs=['G1']))),
            dict(kind='done')])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertTrue(replies.requests[2].reflection_required)
        self.assertFalse(replies.requests[3].reflection_required)
        self.assertFalse(result.verdict.passed)
        state = verification_state(graph.attempts, graph.snapshot)
        self.assertEqual(state['replacements'][0]['authority'], 'policy replacement claim')
        self.assertFalse(state['replacements'][0]['sensitive'])
        self.assertIn('G1', state['goal_evidence'])

    def test_contract_oracle_is_not_replaceable_by_a_policy_expectation(self):
        result, graph, _ = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[dict(id='contract', kind='text_equals', path='answer', expected='43', target_refs=['G1'])]),
            dict(kind='verify', checks=[dict(id='weaker', kind='text_equals', path='answer', expected='42',
                replaces=['contract'], target_refs=['G1'], basis='A Policy claim disagrees')]),
            dict(kind='done'), dict(kind='incomplete')], checks=(Check(id='public', kind='text_equals', path='answer', expected='43'),))
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertTrue(graph.attempts[1].verification[0].oracle_validated)
        self.assertFalse(verification_state(graph.attempts, graph.snapshot)['ready'])

    def test_constant_pass_cannot_clear_a_failure_even_without_requested_controls(self):
        result, graph, _ = self.run_actions([
            dict(kind='write', path='answer', content='wrong'),
            dict(kind='verify', checks=[dict(id='value', kind='text_equals', path='answer', expected='42', target_refs=['G1'])]),
            dict(kind='verify', checks=[dict(id='constant', kind='python_assertions', code='assert True',
                replaces=['value'], target_refs=['G1'], basis='Unsubstantiated revision')]),
            dict(kind='done'), dict(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertFalse(verification_state(graph.attempts, graph.snapshot)['ready'])

    def test_changed_id_or_unrelated_goal_does_not_silently_clear_a_failure(self):
        result, graph, _ = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[dict(id='old', kind='text_equals', path='answer', expected='43', target_refs=['G1'])]),
            dict(kind='verify', checks=[dict(id='new', kind='text_equals', path='answer', expected='42',
                replaces=['old'], target_refs=['G2'], basis='Unrelated evidence')]),
            dict(kind='done'), dict(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertFalse(verification_state(graph.attempts, graph.snapshot)['ready'])

    def test_goal_notes_survive_unrelated_updates_and_do_not_certify_coverage(self):
        _, graph, replies = self.run_actions([
            dict(action=dict(kind='write', path='answer', content='42'), notes=dict(requirements=[dict(
                ref='G2', uncertainty='Input preservation still needs observation')])),
            dict(action=dict(kind='command', command='echo observed'), notes=dict(exploration=dict(hypothesis='Inspect'))),
            dict(kind='incomplete')])
        review = graph.query.reasoning_working_set()['overview']['goal_review']
        self.assertEqual(review['obligations'][0]['ref'], 'G2')
        self.assertEqual(review['obligations'][0]['current_checks'], [])
        self.assertFalse(graph.verdict.passed)
        self.assertEqual(len(replies.requests), 3)

    def test_only_obstacles_request_reflection_and_simple_actions_remain_direct(self):
        _, graph, replies = self.run_actions([
            dict(kind='command', command='echo value'), dict(kind='command', command='echo value'),
            dict(kind='incomplete')])
        self.assertFalse(replies.requests[0].reflection_required)
        self.assertFalse(replies.requests[1].reflection_required)
        self.assertTrue(replies.requests[2].reflection_required)
        self.assertIn('changed conditions', replies.requests[2].system)
        schema = action_schema(reflection_required=True, known_check_ids=())
        envelope = schema['anyOf'][0]
        self.assertEqual(list(envelope['properties']), ['notes', 'action'])
        self.assertEqual(envelope['required'], ['notes', 'action'])
        self.assertNotIn('understanding', canonical(action_schema(exploration_history=False, reflection_required=True)))
        metrics = repetition_metrics(graph.attempts)['totals']
        self.assertEqual(metrics['repeated_proposal'], 1)
        self.assertEqual(metrics['same_outcome_repeat'], 1)
        self.assertEqual(metrics['repeated_failure'], 0)

    def test_revision_decoder_requires_basis_and_related_public_sources(self):
        schema = action_schema(reflection_required=True, known_check_ids=('old',))
        actions = schema['anyOf'][0]['properties']['action']['anyOf']
        items = next(a['properties']['checks']['items']['anyOf'] for a in actions
                     if 'checks' in a['properties'])
        revisions = [item for item in items if item['properties']['replaces'].get('minItems') == 1]
        self.assertEqual(len(revisions), 5)
        for item in revisions:
            self.assertTrue({'basis', 'target_refs', 'replaces'}.issubset(item['required']))
            self.assertEqual(item['properties']['target_refs']['minItems'], 1)
            self.assertEqual(item['properties']['basis']['minLength'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
