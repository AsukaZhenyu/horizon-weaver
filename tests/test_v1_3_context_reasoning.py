"""Evidence-linked situation awareness without another model or acceptance gate."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler, token_bound
from horizon_weaver.core import (Budget, Check, ModelResponse, TaskContract, Usage, action_schema, canonical)
from horizon_weaver.policy import ModelPolicy, parse_output_with_feedback
from horizon_weaver.state_ir import StateIR


class Replies:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []

    def complete(self, request, timeout_s):
        self.requests.append(request)
        return ModelResponse(content=json.dumps(next(self.values)), usage=Usage(input_tokens=10, output_tokens=10))

    def describe(self):
        return {'kind': 'recorded-fixture'}


class ContextReasoningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def run_replies(self, values, **changes):
        task = TaskContract(task_id='situation', goal='Write 42.\nPreserve the input.',
            expected_artifacts=('answer',), acceptance_checks=(Check(id='external', kind='external'),),
            exploration_history=True, verification_policy='behavioral',
            budget=Budget(max_steps=30, no_progress_limit=20, context_tokens=14336), **changes)
        replies = Replies([*values, {'kind': 'incomplete'}])
        NodeAgent(backend='local', policy=ModelPolicy(replies)).run(task, self.work, self.root / 'run')
        return read_run(self.root / 'run'), replies

    def request(self, graph):
        environment = json.loads(graph.manifest.environment_json)['harness']
        return ContextCompiler(environment).build(graph.manifest.contract, graph.view,
            graph.snapshot, graph.checkpoint.step + 1, 100000)

    def test_revision_persists_across_unrelated_notes_and_replay_without_becoming_evidence(self):
        graph, replies = self.run_replies([
            {'kind': 'write', 'path': 'answer', 'content': '0'},
            {'action': {'kind': 'read', 'path': 'answer'}, 'notes': {'understanding': {
                'approach': 'Compare the answer with the public requirement',
                'current_problem': 'Candidate is wrong', 'interpretation': 'Step 1 wrote 0 rather than 42',
                'next_focus': 'Repair the value', 'evidence_steps': [1], 'open_refs': ['G1']}}},
            {'action': {'kind': 'write', 'path': 'answer', 'content': '42'},
             'notes': {'exploration': {'hypothesis': 'Changing the value may satisfy G1'}}}])
        data = graph.query.reasoning_working_set()
        self.assertEqual(data['understanding']['step'], 2)
        self.assertFalse(data['understanding']['file_state_matches'])
        self.assertEqual(data['understanding']['changes_since'], ['answer'])
        self.assertFalse(data['overview']['predicate_ready'])
        self.assertEqual(data['overview']['public_requirement_coverage'].split(';')[0], 'unknown')
        self.assertFalse(graph.verdict.passed)
        projected = StateIR.model_validate_json(graph.query.ir.model_dump_json())
        self.assertEqual(projected.notes_history[0].notes.understanding.evidence_steps, (1,))
        self.assertIn('Step 1 wrote 0 rather than 42', self.request(graph).context)
        self.assertEqual(len(replies.requests), 4)  # no extra reasoning model call

    def test_latest_revision_supersedes_old_interpretation_and_links_to_observation(self):
        graph, _ = self.run_replies([
            {'kind': 'write', 'path': 'answer', 'content': '0'},
            {'action': {'kind': 'command', 'command': 'echo inspected'}, 'notes': {'understanding': {
                'interpretation': 'Value is wrong', 'evidence_steps': [1], 'open_refs': ['G1']}}},
            {'kind': 'write', 'path': 'answer', 'content': '42'},
            {'action': {'kind': 'read', 'path': 'answer'}, 'notes': {'understanding': {
                'interpretation': 'Value was repaired; correctness still needs checking', 'evidence_steps': [3]}}}])
        data = graph.query.reasoning_working_set(limit=8)
        self.assertEqual(data['understanding']['step'], 4)
        repair = next(row for row in data['exploration_chain'] if row['step'] == 3)
        self.assertEqual(repair['interpretation']['step'], 4)
        self.assertEqual(repair['consequence']['changed_files'], ('answer',))

    def test_invalid_claim_citations_do_not_block_action_or_discard_valid_intent(self):
        graph, _ = self.run_replies([{'action': {'kind': 'write', 'path': 'answer', 'content': '42'},
            'notes': {'understanding': {'interpretation': 'Hidden tests passed', 'evidence_steps': [999],
                                        'open_refs': ['external']},
                      'exploration': {'hypothesis': 'Try the public answer', 'problem_refs': ['G1']}}}])
        self.assertEqual((self.work / 'answer').read_text(), '42')
        self.assertIsNone(graph.notes_history[0].notes.understanding)
        self.assertIsNotNone(graph.notes_history[0].notes.exploration)
        self.assertIsNone(graph.query.reasoning_working_set()['understanding'])

    def test_malformed_interpretation_does_not_discard_executable_action_or_sibling(self):
        output, warning = parse_output_with_feedback(json.dumps({'action': {'kind': 'command', 'command': 'echo ok'},
            'notes': {'understanding': {'evidence_steps': 'not steps'},
                      'exploration': {'hypothesis': 'Diagnostic observation'}}}))
        self.assertEqual(output.action.command, 'echo ok')
        self.assertIsNone(output.notes.understanding)
        self.assertEqual(output.notes.exploration.hypothesis, 'Diagnostic observation')
        self.assertIn('understanding', warning)

    def test_failed_predicate_changes_knowledge_without_changing_files_or_proving_cause(self):
        (self.work / 'answer').write_text('0')
        graph, _ = self.run_replies([{'action': {'kind': 'verify', 'checks': [
            {'id': 'value', 'kind': 'text_equals', 'path': 'answer', 'expected': '42', 'target_refs': ['G1']}]},
            'notes': {'exploration': {'hypothesis': 'Candidate may already contain 42',
                                     'expected_observation': 'Equality passes', 'problem_refs': ['G1']}}}])
        row = graph.query.reasoning_working_set()['exploration_chain'][0]
        self.assertEqual(row['consequence']['changed_files'], ())
        self.assertEqual(row['consequence']['checks'][0]['status'], 'fail')
        self.assertEqual(row['why']['expected_observation'], 'Equality passes')
        self.assertIn('wrong expectation unresolved', row['assessment'])
        self.assertNotIn('algorithm is wrong', canonical(row))

    def test_successful_empty_comparison_is_not_presented_as_requirement_coverage(self):
        (self.work / 'answer').write_text('')
        graph, _ = self.run_replies([{'kind': 'verify', 'checks': [
            {'id': 'empty', 'kind': 'text_equals', 'path': 'answer', 'expected': ''}]}])
        data = graph.query.reasoning_working_set()
        self.assertTrue(data['overview']['predicate_ready'])
        self.assertIn('coverage unknown', data['exploration_chain'][0]['assessment'])
        self.assertEqual(data['exploration_chain'][0]['consequence']['checks'][0]['expectation_authority'],
                         'Policy check definition')

    def test_exact_repeats_are_grouped_without_inventing_a_new_strategy_or_cause(self):
        graph, _ = self.run_replies([
            {'kind': 'command', 'command': 'echo pattern', 'reasoning': 'Inspect the current expression'},
            {'kind': 'command', 'command': 'echo pattern', 'reasoning': 'Try again'}])
        rows = graph.query.reasoning_working_set()['exploration_chain']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['matching_steps'], [1, 2])
        self.assertEqual(rows[0]['consequence']['runtime_progress'], 'none')
        self.assertIn('no new progress', rows[0]['assessment'])

    def test_context_keeps_actual_reply_and_valid_budgeted_reasoning(self):
        graph, _ = self.run_replies([{'kind': 'command', 'command': 'echo ACTUAL-DIAGNOSTIC'}])
        request = self.request(graph)
        self.assertIn('ACTUAL-DIAGNOSTIC', request.context)
        header = 'TASK SITUATION / EXPLORATION REASONING (observations and Policy claims distinguished)\n'
        row = request.context.split(header, 1)[1].split('\n', 1)[0]
        self.assertIn('overview', json.loads(row))
        self.assertLessEqual(token_bound(request.system) + token_bound(request.context), request.input_token_bound)
        self.assertLessEqual(request.input_token_bound, graph.manifest.contract.budget.context_tokens)

    def test_history_disabled_keeps_schema_and_context_without_reasoning_claim_interface(self):
        schema = action_schema(exploration_history=False)
        self.assertNotIn('understanding', canonical(schema))
        graph, _ = self.run_replies([{'kind': 'read', 'path': 'missing'}])
        task = graph.manifest.contract.model_copy(update={'exploration_history': False})
        environment = json.loads(graph.manifest.environment_json)['harness']
        request = ContextCompiler(environment).build(task, graph.view, graph.snapshot, 3, 100000)
        self.assertNotIn('TASK SITUATION / EXPLORATION REASONING', request.context)


if __name__ == '__main__':
    unittest.main(verbosity=2)
