"""Goal feedback, uncertain self-checks and explicit submission constraints."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.core import Budget, Check, ModelResponse, TaskContract, Termination, Usage, canonical
from horizon_weaver.policy import ModelPolicy, parse_output_with_feedback
from horizon_weaver.state_ir import StateIR


class Replies:
    def __init__(self, values):
        self.values, self.requests = iter(values), []

    def complete(self, request, timeout_s):
        self.requests.append(request)
        return ModelResponse(content=json.dumps(next(self.values)), usage=Usage(input_tokens=10, output_tokens=10))

    def describe(self):
        return {'kind': 'continuous-goal-fixture'}


def compare(expected='42', identity='answer', **kwargs):
    return dict(id=identity, kind='text_equals', path='answer', expected=expected, target_refs=['G1'], **kwargs)


class ContinuousVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def run_actions(self, actions, **changes):
        task = TaskContract(task_id='continuous', goal='Write 42 to answer.\nPreserve the original input.',
            exploration_history=True, verification_policy='behavioral',
            acceptance_checks=changes.pop('acceptance_checks', (Check(id='external', kind='external'),)),
            budget=Budget(max_steps=20, no_progress_limit=20, context_tokens=14336), **changes)
        replies = Replies(actions)
        result = NodeAgent(backend='local', policy=ModelPolicy(replies)).run(task, self.work, self.root / 'run')
        return result, read_run(self.root / 'run'), replies

    def test_wrong_self_check_remains_evidence_without_becoming_a_submission_constraint(self):
        result, graph, replies = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[compare('43', oracle='contract')]), dict(kind='done')])
        report = graph.query.goal_assessment()
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertFalse(result.verdict.passed)
        self.assertEqual(graph.attempts[1].verification[0].status, 'fail')
        self.assertEqual(report['requirements'][0]['status'], 'disputed')
        self.assertTrue(report['submission']['allowed'])
        self.assertFalse(report['submission']['completion_established'])
        self.assertFalse(report['check_issues'][0]['blocks_submission'])
        self.assertIn('GOAL / CURRENT STATE / GAPS', replies.requests[2].context)

    def test_changed_expectation_does_not_require_metadata_to_hand_back_a_candidate(self):
        result, graph, _ = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[compare('43')]),
            dict(kind='verify', checks=[compare()]), dict(kind='done')])
        report = graph.query.goal_assessment()
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(report['requirements'][0]['status'], 'supported')
        self.assertEqual(report['historical_definition_changes'][0]['step'], 2)
        self.assertFalse(report['historical_definition_changes'][0]['replacement_claimed'])
        self.assertEqual([r.status for a in graph.attempts for r in a.verification], ['fail', 'pass'])

    def test_failed_done_check_can_submit_but_cannot_grant_acceptance(self):
        result, graph, _ = self.run_actions([dict(kind='write', path='answer', content='42'),
            dict(kind='done', checks=[compare('43')])])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(graph.attempts[-1].verification[0].status, 'fail')
        self.assertEqual(graph.attempts[-1].result.exit_code, 0)
        self.assertFalse(result.verdict.passed)

    def test_no_measurement_is_unknown_not_a_passing_goal(self):
        result, graph, replies = self.run_actions([dict(kind='done')])
        report = graph.query.goal_assessment()
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertFalse(report['submission']['predicate_ready'])
        self.assertFalse(report['submission']['completion_established'])
        self.assertEqual(report['source_review']['unmapped_refs'], ['G1', 'G2'])
        self.assertIn('full goal coverage', report['coverage'])
        self.assertIn('GOAL / CURRENT STATE / GAPS', replies.requests[0].context)

    def test_declared_artifact_is_a_real_constraint_without_a_self_check(self):
        result, graph, _ = self.run_actions([dict(kind='done'),
            dict(kind='write', path='answer', content='42'), dict(kind='done')], expected_artifacts=('answer',))
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(graph.attempts[0].result.error_type, 'verification_required')
        self.assertTrue(graph.query.goal_assessment()['artifacts'][0]['present'])

    def test_fixed_public_contract_cannot_be_overridden_by_policy(self):
        result, graph, _ = self.run_actions([
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[compare('43')]),
            dict(kind='verify', checks=[compare('42', 'weaker', oracle='contract', basis='Policy disagreement')]),
            dict(kind='done'), dict(kind='incomplete')],
            acceptance_checks=(Check(id='public', kind='text_equals', path='answer', expected='43'),))
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        report = graph.query.goal_assessment()
        self.assertFalse(report['submission']['allowed'])
        self.assertEqual(report['fixed_requirements'][0]['status'], 'contradicted')
        self.assertTrue(graph.attempts[1].verification[0].oracle_validated)

    def test_explicit_self_check_policy_is_respected(self):
        result, graph, _ = self.run_actions([dict(kind='done'), dict(kind='incomplete')], require_self_check=True)
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertEqual(graph.query.goal_assessment()['submission']['hard_blockers'][0]['id'], 'explicit-self-check-policy')

    def test_requirement_notes_are_revisable_claims_and_survive_replay(self):
        result, graph, replies = self.run_actions([
            dict(action=dict(kind='write', path='answer', content='42'), notes=dict(requirements=[dict(
                ref='G2', interpretation='Preserve bytes', satisfied_when='Input equals initial input',
                needed_information='Read and compare input', uncertainty='Not observed')])),
            dict(kind='read', path='answer'), dict(kind='incomplete')])
        report = graph.query.goal_assessment()
        self.assertEqual(report['requirements'][0]['status'], 'unknown')
        self.assertEqual(report['requirements'][0]['needed_information'], 'Read and compare input')
        self.assertEqual(len(replies.requests), 3)
        self.assertFalse(result.verdict.passed)
        clone = StateIR.model_validate_json(graph.ir.model_dump_json())
        self.assertEqual(clone.notes_history[0].notes.requirements[0].interpretation, 'Preserve bytes')

    def test_same_source_can_describe_distinct_behaviors(self):
        _, graph, _ = self.run_actions([dict(action=dict(kind='read', path='missing'), notes=dict(requirements=[
            dict(ref='G1', interpretation='File exists'), dict(ref='G1', interpretation='Content equals 42')])),
            dict(kind='incomplete')])
        rows = graph.query.goal_assessment()['requirements']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['status'] == 'unknown' for row in rows))

    def test_file_evidence_survives_unrelated_edit_and_shell_observation(self):
        _, graph, _ = self.run_actions([dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[compare()]), dict(kind='write', path='unrelated', content='x'),
            dict(kind='command', command='echo observed'), dict(kind='incomplete')])
        row = graph.query.goal_assessment()['requirements'][0]
        self.assertEqual(row['status'], 'supported')
        self.assertTrue(row['evidence'][0]['current'])

    def test_candidate_change_makes_old_support_unknown(self):
        _, graph, _ = self.run_actions([dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[compare()]), dict(kind='write', path='answer', content='wrong'),
            dict(kind='incomplete')])
        row = graph.query.goal_assessment()['requirements'][0]
        self.assertEqual(row['status'], 'unknown')
        self.assertFalse(row['evidence'][0]['current'])

    def test_process_evidence_retains_conservative_environment_scope(self):
        _, graph, _ = self.run_actions([dict(kind='verify', checks=[dict(id='cli', kind='process',
            argv=[sys.executable, '-B', '-c', 'print(42)'], expected_stdout='42\n', target_refs=['G1'])]),
            dict(kind='command', command='echo scope changed'), dict(kind='incomplete')])
        self.assertEqual(graph.query.goal_assessment()['requirements'][0]['status'], 'unknown')

    def test_newline_feedback_describes_decoded_strings(self):
        _, graph, _ = self.run_actions([dict(kind='write', path='answer', content='42\n'),
            dict(kind='verify', checks=[compare('42\\n')]), dict(kind='incomplete')])
        shape = graph.query.goal_assessment()['check_issues'][0]['observation']['comparisons'][0]['text_shape']
        self.assertEqual((shape['actual_newlines'], shape['expected_newlines']), (1, 0))
        self.assertEqual((shape['actual_literal_backslash_n'], shape['expected_literal_backslash_n']), (0, 1))

    def test_truncated_text_keeps_full_operand_counts(self):
        (self.work / 'answer').write_text('x' * 5000 + '\n', encoding='utf-8', newline='')
        _, graph, _ = self.run_actions([dict(kind='verify', checks=[compare('x' * 5000)]), dict(kind='incomplete')])
        comparison = graph.query.goal_assessment()['check_issues'][0]['observation']['comparisons'][0]
        self.assertEqual(comparison['text_shape']['actual_length'], 5001)
        self.assertEqual(comparison['text_shape']['actual_newlines'], 1)
        self.assertFalse(comparison['operands_complete'])

    def test_check_crash_is_not_a_goal_counterexample(self):
        _, graph, _ = self.run_actions([dict(kind='verify', checks=[dict(id='broken', kind='python_assertions',
            code='import no_such_hw_dependency', target_refs=['G1'])]), dict(kind='incomplete')])
        report = graph.query.goal_assessment()
        self.assertEqual(report['requirements'][0]['status'], 'unknown')
        self.assertFalse(report['check_issues'][0]['blocks_submission'])
        self.assertIn('prerequisite', report['check_issues'][0]['needed_information'])

    def test_constant_assertion_and_observations_do_not_certify_goal(self):
        _, graph, _ = self.run_actions([dict(kind='command', command='echo 42'),
            dict(kind='verify', checks=[dict(id='constant', kind='python_assertions', code='assert True', target_refs=['G1'])]),
            dict(kind='incomplete')])
        report = graph.query.goal_assessment()
        self.assertEqual(report['requirements'][0]['status'], 'unknown')
        self.assertFalse(report['submission']['completion_established'])

    def test_report_is_present_before_checks_and_after_ordinary_actions(self):
        _, graph, replies = self.run_actions([dict(kind='write', path='answer', content='42'),
            dict(kind='read', path='answer'), dict(kind='incomplete')])
        self.assertTrue(all('GOAL / CURRENT STATE / GAPS' in request.context for request in replies.requests))
        self.assertIn('GOAL UPDATE:', graph.checkpoint.feedback)
        self.assertEqual(len(replies.requests), 3)

    def test_invalid_new_note_does_not_discard_a_valid_action(self):
        output, warning = parse_output_with_feedback(
            json.dumps(dict(action=dict(kind='command', command='echo ok'), notes=dict(requirements=[dict(
                ref='G1', interpretation=123)]))))
        self.assertEqual(output.action.kind.value, 'command')
        self.assertIsNotNone(warning)

    def test_advisory_permission_never_displays_an_implicit_self_check_requirement(self):
        _, _, replies = self.run_actions([dict(kind='write', path='answer', content='42'),
            dict(kind='verify', checks=[compare('43')]), dict(kind='incomplete')])
        for request in replies.requests:
            readiness = json.loads(request.context.split('SUBMISSION READINESS\n', 1)[1].split('\n', 1)[0])
            self.assertTrue(readiness['allowed'])
            self.assertEqual(readiness['phase'], 'goal_review')
            self.assertFalse(readiness['completion_established'])
            self.assertNotIn('self_test_required', request.context)
        self.assertIn('predicate_ready', replies.requests[2].context)

    def test_explicit_self_check_constraint_keeps_a_consistent_required_phase(self):
        _, _, replies = self.run_actions([dict(kind='incomplete')], require_self_check=True)
        readiness = json.loads(replies.requests[0].context.split('SUBMISSION READINESS\n', 1)[1].split('\n', 1)[0])
        self.assertFalse(readiness['allowed'])
        self.assertEqual(readiness['phase'], 'self_test_required')


if __name__ == '__main__':
    unittest.main(verbosity=2)
