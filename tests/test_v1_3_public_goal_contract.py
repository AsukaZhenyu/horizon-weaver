"""Public goals survive repairs; test defects do not silently redefine an API."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler
from horizon_weaver.core import (Action, Budget, Check, CheckSpec, ModelResponse, TaskContract,
    Termination, ToolResult, Usage, action_schema)
from horizon_weaver.policy import ModelPolicy
from horizon_weaver.state_ir import StateIR
from horizon_weaver.verification import (check_goal_diagnostics, compile_goal_conditions,
    execute_python_check)


class Replies:
    def __init__(self, values):
        self.values, self.requests = iter(values), []

    def complete(self, request, timeout_s):
        self.requests.append(request)
        return ModelResponse(content=json.dumps(next(self.values)), usage=Usage(input_tokens=10, output_tokens=10))

    def describe(self):
        return {'kind': 'public-goal-fixture'}


ASYNC_GOAL = ('Create a Python function called `async execute_jobs(jobs: list[Callable[[], Awaitable[None]]], limit: int) -> None`. '
              'Put the function in a file called /app/jobs.py.\n'
              'The jobs should run concurrently and cleanup must run after cancellation.')
ASYNC_OK = 'import asyncio\nasync def execute_jobs(jobs, limit):\n    await asyncio.gather(*(job() for job in jobs))\n'
ASYNC_BAD = 'import asyncio\ndef execute_jobs(jobs, limit):\n    asyncio.run(asyncio.gather(*(job() for job in jobs)))\n'


class PublicGoalContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def run_actions(self, goal, actions, review=False):
        task = TaskContract(task_id='public-goal', goal=goal,
            goal_conditions=compile_goal_conditions(goal), require_goal_review=review,
            acceptance_checks=(Check(id='external', kind='external'),),
            exploration_history=True, verification_policy='behavioral',
            budget=Budget(max_steps=30, no_progress_limit=30, context_tokens=14336))
        replies = Replies(actions)
        result = NodeAgent(backend='local', policy=ModelPolicy(replies)).run(task, self.work, self.root/'run')
        return result, read_run(self.root/'run'), replies

    def test_input_paths_examples_and_negations_are_not_deliverables(self):
        goal = ('Read /app/source.csv. Create a file named /app/result.json.\n'
                'Do not create /app/unused.txt.\n'
                'For example, save data to /app/example.json.\n'
                '```\nsave /app/code-example.txt\n```')
        self.assertEqual([g.path for g in compile_goal_conditions(goal) if g.kind=='artifact'], ['result.json'])

    def test_other_workspace_and_parent_traversal_are_not_public_operands(self):
        goals = compile_goal_conditions('Create /sandbox/result.txt. Save to /sandbox/../escape.txt.', '/sandbox')
        self.assertEqual([g.path for g in goals if g.kind=='artifact'], ['result.txt'])

    def test_input_after_output_in_same_sentence_is_not_a_deliverable(self):
        goals=compile_goal_conditions('Create /app/output.json from /app/input.json. Do not create /app/spare.txt.')
        self.assertEqual([g.path for g in goals if g.kind=='artifact'], ['output.json'])

    def test_output_directory_is_not_invented_as_a_regular_file(self):
        self.assertFalse(any(g.kind=='artifact' for g in compile_goal_conditions('Save images in /app/images. Keep the folder empty.')))

    def test_nested_criteria_keep_their_public_sources(self):
        goals = compile_goal_conditions('1. Extract fields:\n   - Extract id.\n   - Extract email.\n2. Sort users.')
        self.assertEqual(goals[0].related_refs, ('G2','G3'))
        self.assertEqual(goals[1].ref, 'G4')

    def test_public_conditions_survive_event_and_ir_replay(self):
        _, graph, _ = self.run_actions(ASYNC_GOAL, [dict(kind='write', path='jobs.py', content=ASYNC_OK), dict(kind='incomplete')])
        clone = StateIR.model_validate_json(graph.ir.model_dump_json())
        self.assertEqual(clone.contract.goal_conditions, graph.manifest.contract.goal_conditions)
        self.assertTrue(any(g.required_async for g in clone.contract.goal_conditions))

    def test_sync_repair_is_a_contradiction_even_after_unrelated_command(self):
        result, graph, replies = self.run_actions(ASYNC_GOAL, [
            dict(kind='write', path='jobs.py', content=ASYNC_OK),
            dict(kind='write', path='jobs.py', content=ASYNC_BAD),
            dict(kind='command', command='echo inspected'), dict(kind='done'), dict(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        row = next(r for r in graph.query.goal_assessment()['objectives'] if r['kind']=='python_interface')
        self.assertEqual(row['status'], 'contradicted')
        self.assertIn('async def execute_jobs', replies.requests[3].context)
        self.assertEqual(graph.attempts[3].result.error_type, 'verification_required')

    def test_repair_restores_interface_fact_without_claiming_full_behavior(self):
        _, graph, _ = self.run_actions(ASYNC_GOAL, [dict(kind='write',path='jobs.py',content=ASYNC_BAD),
            dict(kind='write',path='jobs.py',content=ASYNC_OK),dict(kind='incomplete')])
        rows = graph.query.goal_assessment()['objectives']
        self.assertEqual(next(r for r in rows if r['kind']=='python_interface')['status'],'supported')
        self.assertTrue(all(r['status']=='unknown' for r in rows if r['kind']=='behavior'))

    def test_imported_interface_is_not_invented_as_a_direct_definition_constraint(self):
        result,graph,_=self.run_actions(ASYNC_GOAL,[dict(kind='write',path='jobs.py',content='from helpers import execute_jobs\n'),
            dict(action=dict(kind='done'),notes=dict(requirements=[
                dict(ref='G1',status='unknown',satisfied_when='Export the requested async function',evidence_steps=[1],needed_information='Inspect the imported function'),
                dict(ref='G2',status='unknown',satisfied_when='Concurrency and cleanup work',evidence_steps=[1],needed_information='Measure behavior')]))],review=True)
        self.assertEqual(result.termination,Termination.SUBMITTED)
        row=next(r for r in graph.query.goal_assessment()['objectives'] if r['kind']=='python_interface')
        self.assertEqual(row['status'],'unknown')
        self.assertFalse(any(b['id']==row['id'] for b in graph.query.goal_assessment()['submission']['hard_blockers']))

    def test_missing_named_deliverable_blocks_done_despite_related_passing_check(self):
        goal = 'Create /app/result.json. Save the exact command to a file named /app/replay.sh.'
        result, graph, replies = self.run_actions(goal, [dict(kind='write', path='result.json', content='[]'),
            dict(kind='verify', checks=[dict(id='output', kind='json_equals', path='result.json', expected='[]')]),
            dict(kind='done'), dict(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertIn('replay.sh: required regular file missing', replies.requests[2].context)
        self.assertTrue(graph.attempts[1].verification[0].status=='pass')

    def test_changed_attention_does_not_remove_an_unfinished_deliverable(self):
        goal = 'Create /app/result.json. Save the command to a file named /app/replay.sh.'
        _, graph, _ = self.run_actions(goal,[dict(action=dict(kind='write',path='result.json',content='[]'),
            notes=dict(requirements=[dict(ref='G1',needed_information='Later write replay.sh')])),
            dict(action=dict(kind='read',path='result.json'),notes=dict(requirements=[dict(ref='G1',uncertainty='Check data')])),
            dict(kind='incomplete')])
        self.assertEqual(next(r for r in graph.query.goal_assessment()['objectives'] if r['path']=='replay.sh')['status'],'contradicted')

    def test_wrong_async_invocation_is_diagnosed_before_candidate_execution(self):
        bad = 'import asyncio\nfrom jobs import execute_jobs\nasync def job(): pass\nasync def main():\n    jobs=[job() for i in range(2)]\n    execute_jobs(jobs,2)\nasyncio.run(main())\nassert True'
        _, graph, replies = self.run_actions(ASYNC_GOAL,[dict(kind='write',path='jobs.py',content=ASYNC_OK),
            dict(kind='verify',checks=[dict(id='wrong',kind='python_assertions',code=bad,target_refs=['G1'])]),
            dict(kind='incomplete')])
        a = graph.attempts[1]
        self.assertEqual(a.result.error_type,'check_definition_invalid')
        self.assertIn('without awaiting',a.result.output)
        self.assertIn('coroutine objects',a.result.output)
        self.assertIn('preserve the public async interface',replies.requests[2].context)
        self.assertEqual(next(r for r in graph.query.goal_assessment()['objectives'] if r['kind']=='python_interface')['status'],'supported')

    def test_correct_await_and_callable_fixture_are_not_rejected(self):
        code = 'import asyncio\nfrom jobs import execute_jobs as run\nasync def job(): pass\nasync def main():\n    await run([job],2)\nasyncio.run(main())\nassert True'
        self.assertEqual(check_goal_diagnostics(CheckSpec(id='right',kind='python_assertions',code=code),compile_goal_conditions(ASYNC_GOAL)),[])

    def test_current_command_written_code_is_assessed_from_recorded_automatic_observation(self):
        (self.work/'jobs.py').write_text(ASYNC_OK,encoding='utf-8')
        _, graph, _ = self.run_actions(ASYNC_GOAL,[dict(kind='command',command='echo prepared'),dict(kind='incomplete')],review=True)
        row=next(r for r in graph.query.goal_assessment()['objectives'] if r['kind']=='python_interface')
        self.assertEqual(row['status'],'supported')
        self.assertEqual(graph.attempts[0].goal_observations[0].path,'jobs.py')
        self.assertEqual(row['evidence_steps'],[1])

    def test_missing_or_stale_semantic_review_cannot_silently_submit(self):
        goal='Create a file named /app/data.txt. The result must describe the input.'
        result,graph,_=self.run_actions(goal,[dict(kind='write',path='data.txt',content='ok'),
            dict(kind='done'),dict(kind='incomplete')],review=True)
        self.assertEqual(result.termination,Termination.INCOMPLETE)
        self.assertEqual(graph.query.goal_assessment()['missing_review_refs'],['G1'])

    def test_same_decision_review_uses_current_recorded_evidence(self):
        goal='Create a file named /app/data.txt. The result must describe the input.'
        result,graph,_=self.run_actions(goal,[dict(kind='write',path='data.txt',content='ok'),
            dict(action=dict(kind='done'),notes=dict(requirements=[dict(ref='G1',status='supported',
                satisfied_when='The current output describes the supplied input.',evidence_steps=[1])]))],review=True)
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertEqual(graph.query.goal_assessment()['missing_review_refs'],[])
        self.assertFalse(result.verdict.passed)

    def test_done_can_review_its_newly_executed_check_without_prior_measurement(self):
        goal='Create a file named /app/data.txt. The result must describe the input.'
        result,graph,_=self.run_actions(goal,[dict(kind='write',path='data.txt',content='ok'),
            dict(action=dict(kind='done',checks=[dict(id='fresh',kind='text_equals',path='data.txt',expected='ok',target_refs=['G1'])]),
                notes=dict(requirements=[dict(ref='G1',status='supported',satisfied_when='Matches the public input',evidence_check_ids=['fresh'])]))],review=True)
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertEqual(graph.attempts[-1].verification[0].status,'pass')

    def test_repair_invalidates_old_review_and_requires_current_evidence(self):
        goal='Create a file named /app/data.txt. The result must describe the input.'
        result,graph,_=self.run_actions(goal,[dict(kind='write',path='data.txt',content='ok'),
            dict(action=dict(kind='read',path='data.txt'),notes=dict(requirements=[dict(ref='G1',status='supported',satisfied_when='Describe input',evidence_steps=[1])])),
            dict(kind='write',path='data.txt',content='changed'),dict(kind='done'),dict(kind='incomplete')],review=True)
        self.assertEqual(result.termination,Termination.INCOMPLETE)
        self.assertEqual(graph.query.goal_assessment()['missing_review_refs'],['G1'])

    def test_literal_newline_policy_is_not_invented(self):
        result,graph,_=self.run_actions('Create a file called /app/greeting.txt. Write "Hi!" to it.',
            [dict(kind='write',path='greeting.txt',content='Hi!\n'),dict(kind='done')],review=True)
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertEqual(next(r for r in graph.query.goal_assessment()['objectives'] if r['kind']=='text_literal')['status'],'supported')

    def test_protocol_warning_does_not_hide_the_original_exception(self):
        code='import asyncio\nasync def leak(): pass\nunused=leak()\nraise RuntimeError("original failure")'
        _,graph,_=self.run_actions('Inspect errors.',[dict(kind='verify',checks=[dict(id='crash',kind='python_assertions',code=code)]),dict(kind='incomplete')])
        receipt=graph.attempts[0].verification[0]
        detail=json.loads(receipt.result.output)
        self.assertEqual(receipt.result.error_type,'check_inconclusive')
        self.assertIn('original failure',detail['error'])
        self.assertIn('traceback',detail)
        self.assertIn('was never awaited',detail.get('protocol_noise',''))

    def test_ambiguous_protocol_frames_are_rejected(self):
        class Harness:
            task=TaskContract(task_id='p',goal='Inspect',acceptance_checks=(Check(id='x',kind='external'),))
            def verify_python(self,*args):
                return ToolResult(exit_code=0,output='HW_CHECK_RESULT:e30=\nHW_CHECK_RESULT:e30=')
        result,status,_=execute_python_check(Harness(),CheckSpec(id='x',kind='python_assertions',code='assert True'),2)
        self.assertEqual(result.error_type,'check_protocol_error')
        self.assertEqual(status,'inconclusive')

    def test_bounded_context_keeps_concrete_gaps_without_duplicate_contract(self):
        goal='Create a file named /app/report.json. Save the command to /app/replay.sh.\n'+'Public background information.\n'*90
        _,graph,replies=self.run_actions(goal,[dict(kind='write',path='report.json',content='[]'),dict(kind='incomplete')])
        context=replies.requests[1].context
        self.assertIn('replay.sh: required regular file missing',context)
        contract=json.loads(context.split('TASK CONTRACT\n')[1].split('\n')[0])
        self.assertNotIn('goal_conditions',contract)

    def test_decoder_requires_review_for_done_but_keeps_normal_actions_simple(self):
        schema=action_schema(exploration_history=True,verification_policy='behavioral',goal_review_refs=('G1','G2'))
        direct=[v for v in schema['anyOf'] if 'kind' in v.get('properties',{})]
        done=[v for v in direct if v['properties']['kind']['const']=='done']
        self.assertTrue(done)
        self.assertTrue(all('review' in v['required'] for v in done))
        self.assertTrue(any(v['properties']['kind']['const']=='write' for v in direct))

    def test_direct_done_review_survives_validation_and_replay(self):
        goal='Create /app/data.txt. The result must describe the input.'
        result,graph,_=self.run_actions(goal,[dict(kind='write',path='data.txt',content='ok'),
            dict(kind='done',review=[dict(ref='G1',status='supported',satisfied_when='Describe input',evidence_steps=[1])])],review=True)
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertEqual(graph.notes_history[-1].notes.requirements[0].status,'supported')
        self.assertEqual(graph.attempts[-1].action.review[0].ref,'G1')

    def test_goal_review_does_not_change_execution_identity(self):
        plain=Action(kind='done')
        reviewed=Action(kind='done',review=[dict(ref='G1',status='unknown',satisfied_when='Measure',evidence_steps=[1])])
        self.assertEqual(plain.execution_signature,reviewed.execution_signature)


if __name__=='__main__':
    unittest.main(verbosity=2)
