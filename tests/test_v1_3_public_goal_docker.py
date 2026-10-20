"""Public-goal integration and framed diagnostics in the real container runtime."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.core import Budget, Check, TaskContract, Termination
from horizon_weaver.policy import ModelPolicy
from horizon_weaver.verification import compile_goal_conditions
from tests.test_v1_3_public_goal_contract import Replies, ASYNC_GOAL, ASYNC_OK, ASYNC_BAD


@unittest.skipUnless(os.environ.get('HORIZON_WEAVER_DOCKER_TESTS')=='1','real Docker tests require opt-in')
class PublicGoalDockerTests(unittest.TestCase):
    def run_actions(self,actions,goal=ASYNC_GOAL):
        tmp=tempfile.TemporaryDirectory(prefix='hw-public-goal-')
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name); work=root/'work'; work.mkdir()
        task=TaskContract(task_id='public-goal-docker',goal=goal,
            goal_conditions=compile_goal_conditions(goal),
            acceptance_checks=(Check(id='external',kind='external'),),
            verification_policy='behavioral',exploration_history=True,
            budget=Budget(max_steps=12,wall_time_s=120,context_tokens=14336))
        result=NodeAgent(backend='docker',image=os.environ.get('HORIZON_WEAVER_DOCKER_IMAGE','python:3.12-slim'),
            policy=ModelPolicy(Replies(actions))).run(task,work,root/'run')
        return result,read_run(root/'run')

    def test_real_container_rejects_api_regression_then_accepts_repair(self):
        result,graph=self.run_actions([dict(kind='write',path='jobs.py',content=ASYNC_BAD),dict(kind='done'),
            dict(kind='write',path='jobs.py',content=ASYNC_OK),dict(kind='done')])
        self.assertEqual(graph.attempts[1].result.error_type,'verification_required')
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertFalse(result.verdict.passed)

    def test_real_container_retains_execution_exception_and_shutdown_warning(self):
        code='import asyncio\nasync def leak(): pass\nunused=leak()\nraise RuntimeError("real exception")'
        _,graph=self.run_actions([dict(kind='verify',checks=[dict(id='warn',kind='python_assertions',code=code)]),dict(kind='incomplete')])
        receipt=graph.attempts[0].verification[0]
        detail=json.loads(receipt.result.output)
        self.assertEqual(receipt.result.error_type,'check_inconclusive')
        self.assertIn('real exception',detail['error'])
        self.assertIn('was never awaited',detail['protocol_noise'])

    def test_real_container_observes_shell_csv_and_blocks_missing_header(self):
        goal='Output results to /app/table.csv with the following structure:\nname,total\nA,<value>'
        result,graph=self.run_actions([dict(kind='command',command="printf 'A,3\\n' > table.csv"),
            dict(kind='done'),dict(kind='write',path='table.csv',content='name,total\nA,3\n'),dict(kind='done')],goal)
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertEqual(graph.attempts[1].result.error_type,'verification_required')
        self.assertEqual(graph.attempts[0].goal_observations[0].path,'table.csv')


if __name__=='__main__':
    unittest.main(verbosity=2)
