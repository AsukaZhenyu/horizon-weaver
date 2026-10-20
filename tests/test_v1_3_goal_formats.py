"""Goal format facts remain objective even when a shell writes the artifact."""
import json,sys,tempfile,unittest
from pathlib import Path
from horizon_weaver.app import NodeAgent,read_run
from horizon_weaver.core import Budget,Check,TaskContract,Termination
from horizon_weaver.policy import ModelPolicy
from horizon_weaver.verification import compile_goal_conditions
from tests.test_v1_3_public_goal_contract import Replies

CSV_GOAL='Output results to /app/table.csv with the following structure:\nname,total\nA,<number>\nB,<number>'

class GoalFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work=self.root/'work';self.work.mkdir()

    def run_actions(self,goal,actions):
        task=TaskContract(task_id='formats',goal=goal,goal_conditions=compile_goal_conditions(goal),
            acceptance_checks=(Check(id='external',kind='external'),),verification_policy='behavioral',
            exploration_history=True,budget=Budget(max_steps=20,no_progress_limit=20,context_tokens=14000))
        replies=Replies(actions)
        result=NodeAgent(backend='local',policy=ModelPolicy(replies)).run(task,self.work,self.root/'run')
        return result,read_run(self.root/'run'),replies

    def test_required_csv_header_is_an_explicit_goal(self):
        goals=compile_goal_conditions(CSV_GOAL)
        header=next(g for g in goals if g.kind=='csv_header')
        self.assertEqual((header.ref,header.path,header.expected),('G2','table.csv','name,total'))

    def test_csv_with_correct_rows_still_fails_when_header_is_missing(self):
        result,graph,replies=self.run_actions(CSV_GOAL,[dict(kind='write',path='table.csv',content='A,3\nB,4\n'),
            dict(kind='done'),dict(kind='incomplete')])
        self.assertEqual(result.termination,Termination.INCOMPLETE)
        self.assertIn('required CSV header',replies.requests[2].context)
        self.assertEqual(next(r for r in graph.query.goal_assessment()['objectives'] if r['kind']=='csv_header')['status'],'contradicted')

    def test_shell_created_csv_is_observed_and_repaired_without_a_model_read(self):
        command='"'+sys.executable+'" -c "from pathlib import Path; Path(\'table.csv\').write_text(\'A,3\\nB,4\\n\')"'
        result,graph,_=self.run_actions(CSV_GOAL,[dict(kind='command',command=command),dict(kind='done'),
            dict(kind='write',path='table.csv',content='name,total\nA,3\nB,4\n'),dict(kind='done')])
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertEqual(graph.attempts[1].result.error_type,'verification_required')
        self.assertEqual(graph.attempts[0].goal_observations[0].path,'table.csv')
        self.assertEqual(read_run(self.root/'run').attempts[0].goal_observations,graph.attempts[0].goal_observations)

    def test_json_error_strings_do_not_satisfy_an_object_output_format(self):
        goal='Create a JSON file in /app/rows.json. The output should have the format [{"name":"example", "amount":N}, ...]'
        result,graph,_=self.run_actions(goal,[dict(kind='write',path='rows.json',content='["tool not found"]'),
            dict(kind='done'),dict(kind='incomplete')])
        self.assertEqual(result.termination,Termination.INCOMPLETE)
        self.assertEqual(next(r for r in graph.query.goal_assessment()['objectives'] if r['kind']=='json_shape')['status'],'contradicted')

    def test_format_examples_do_not_become_fixed_data_values(self):
        goal='Create a JSON file in /app/rows.json. The output should have the format [{"name":"example", "amount":N}, ...]'
        result,graph,_=self.run_actions(goal,[dict(kind='write',path='rows.json',content='[{"name":"actual", "amount":7}]'),dict(kind='done')])
        self.assertEqual(result.termination,Termination.SUBMITTED)
        self.assertFalse(result.verdict.passed)

    def test_long_goal_repeated_large_read_keeps_the_obstacle_within_budget(self):
        (self.work/'input.json').write_text('x'*110000,encoding='utf-8')
        goal='Create /app/output.json. Save the command to /app/replay.sh.\n'+'Public background information.\n'*150
        _,_,replies=self.run_actions(goal,[dict(kind='read',path='input.json'),dict(kind='read',path='input.json'),dict(kind='incomplete')])
        self.assertTrue(replies.requests[-1].reflection_required)
        self.assertLessEqual(replies.requests[-1].input_token_bound,14000)

if __name__=='__main__':unittest.main(verbosity=2)
