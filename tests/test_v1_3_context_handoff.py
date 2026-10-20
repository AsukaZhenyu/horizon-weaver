"""History/context/guard regressions reproduced in real-model development."""
import json
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
from horizon_weaver.app import NodeAgent,read_run
from horizon_weaver.context import ContextCompiler
from horizon_weaver.core import Action,ActionKind,Budget,Check,HistoryQuery,TaskContract
from horizon_weaver.policy import ScriptedPolicy
from horizon_weaver.state_queries import StateQuery
from horizon_weaver.transitions import TransitionGuard

class ContextHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work=self.root/'work';self.work.mkdir()

    def graph(self,actions,text='42',history=True):
        (self.work/'answer').write_text(text,encoding='utf-8')
        task=TaskContract(task_id='handoff',goal='Inspect the current answer',
            acceptance_checks=(Check(id='external',kind='external'),),
            exploration_history=history,verification_policy='behavioral',
            budget=Budget(max_steps=20,no_progress_limit=12,context_tokens=14336))
        NodeAgent(backend='local',policy=ScriptedPolicy(tuple(actions)+
            (Action(kind='incomplete'),))).run(task,self.work,self.root/'run')
        return read_run(self.root/'run')

    def request(self,g,compiler=ContextCompiler):
        env=json.loads(g.manifest.environment_json)['harness']
        return compiler(env).build(g.manifest.contract,g.view,g.snapshot,g.checkpoint.step+1,100000)

    def guarded(self,g,r):
        return TransitionGuard().assess(Action(kind='read',path='answer'),r,g.view,g.snapshot)

    def test_complete_read_delivered_only_by_history_is_reused(self):
        g=self.graph([Action(kind='read',path='answer')])
        with patch.object(StateQuery,'context_candidates',return_value=()):r=self.request(g)
        self.assertNotIn('ATTEMPT 1\n',r.context)
        self.assertIn(1,r.visible_observation_steps)
        self.assertEqual(self.guarded(g,r).error_type,'redundant_action')
        self.assertIn('recorded step 1',self.guarded(g,r).output)

    def test_byte_clipped_history_does_not_block_a_full_read(self):
        g=self.graph([Action(kind='read',path='answer')],text='你好'*150)
        with patch.object(StateQuery,'context_candidates',return_value=()):r=self.request(g)
        self.assertNotIn(1,r.visible_observation_steps)
        self.assertIsNone(self.guarded(g,r))

    def test_old_file_version_in_history_does_not_block_current_read(self):
        g=self.graph([Action(kind='read',path='answer'),Action(kind='write',path='answer',content='changed')])
        with patch.object(StateQuery,'context_candidates',return_value=()):r=self.request(g)
        self.assertNotIn(1,r.visible_observation_steps)
        self.assertIsNone(self.guarded(g,r))

    def test_unrelated_file_change_does_not_expire_the_visible_read(self):
        g=self.graph([Action(kind='read',path='answer'),Action(kind='write',path='other',content='changed')])
        with patch.object(StateQuery,'context_candidates',return_value=()):r=self.request(g)
        self.assertIn(1,r.visible_observation_steps)
        self.assertEqual(self.guarded(g,r).error_type,'redundant_action')

    def history_request(self,text):
        g=self.graph([Action(kind='read',path='answer'),Action(kind='history',query=HistoryQuery(steps=(1,)))],text=text)
        candidates=tuple(c for c in g.query.context_candidates() if c.attempt.action.kind==ActionKind.HISTORY)
        with patch.object(StateQuery,'context_candidates',return_value=candidates), \
             patch.object(StateQuery,'exploration_working_set',return_value=()):r=self.request(g)
        return g,r

    def test_complete_pure_history_query_delivers_original_read(self):
        g,r=self.history_request('42')
        self.assertIn(1,r.visible_observation_steps)
        self.assertFalse(g.attempts[1].tool_executed)
        self.assertEqual(self.guarded(g,r).error_type,'redundant_action')

    def test_partial_pure_history_query_allows_original_read(self):
        g,r=self.history_request('x'*2000)
        self.assertNotIn(1,r.visible_observation_steps)
        self.assertIsNone(self.guarded(g,r))

    def test_large_actions_keep_current_inventory_and_factual_identity(self):
        command='python -c "print(\'ok\'); #'+('x'*4500)+'"'
        g=self.graph([Action(kind='command',command=command)])
        r=self.request(g)
        self.assertIn('answer file',r.context)
        self.assertNotIn('x'*4500,r.context)
        self.assertIn(g.attempts[0].action.execution_signature,r.context)
        self.assertIn('OBSERVED ACTION',r.context)
        self.assertEqual(g.attempts[0].action.command,command)

    def test_history_disabled_keeps_current_read_protection(self):
        g=self.graph([Action(kind='read',path='answer')],history=False)
        r=self.request(g)
        self.assertNotIn('EXPLORATION HISTORY',r.context)
        self.assertIn(1,r.visible_observation_steps)
        self.assertEqual(self.guarded(g,r).error_type,'redundant_action')

if __name__=='__main__':unittest.main(verbosity=2)
