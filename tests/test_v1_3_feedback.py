"""Budgeted evidence delivery, executable checks and immutable check reuse."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx

from pydantic import ValidationError
from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler, excerpt, token_bound
from horizon_weaver.core import (Action, Attempt, Budget, Check, CheckControl, CheckSpec, FileState,
    GraphView, ProgressAssessment, StateDelta, StateSnapshot, TaskContract, Termination, ToolResult,
    VerificationReceipt, action_schema, canonical, digest)
from horizon_weaver.harness import DockerHarness, LocalHarness
from horizon_weaver.policy import ModelGateway, ModelPolicy, ScriptedPolicy
from horizon_weaver.errors import GatewayError
from horizon_weaver.terminal_policy import lower_terminal
from horizon_weaver.verification import run_checks


def contract(goal='Run product.py and compare its output with 42.', **kw):
    return TaskContract(task_id='feedback', goal=goal, exploration_history=True,
        verification_policy='behavioral', require_self_check=True,
        acceptance_checks=(Check(id='external', kind='external'),),
        budget=Budget(max_steps=30, no_progress_limit=20, context_tokens=14336), **kw)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def execute(self, spec):
        harness = LocalHarness(self.work)
        harness.start(contract())
        self.addCleanup(harness.close)
        return run_checks(harness, (spec,), contract(), harness.snapshot(), time.monotonic()+10, 5)[0]

    def actions(self, actions):
        harness = LocalHarness(self.work)
        with patch.object(harness, 'execute', wraps=harness.execute) as calls:
            result = NodeAgent(policy=ScriptedPolicy(tuple(actions))).run_attached(contract(), harness,
                self.root / 'run', workspace_id='feedback-local')
        return result, read_run(self.root / 'run'), calls

    def test_comparison_reports_actual_values_and_evaluates_operands_once(self):
        code = "calls=[]\ndef value():\n    calls.append(1)\n    return len(calls)\nassert value() == 1\nassert len(calls) == 1\nassert value() == 7"
        receipt = self.execute(CheckSpec(id='values', kind='python_assertions', code=code))
        detail = json.loads(receipt.result.output)
        self.assertEqual(receipt.status, 'fail')
        self.assertEqual((detail['checks'][-1]['left'], detail['checks'][-1]['right']), (2, 7))
        self.assertEqual(detail['checks'][1]['left'], 1)

    def test_comparison_keeps_identity_membership_and_lazy_assert_messages(self):
        code = "x=[]\nassert x is x\nassert 2 in [1,2]\nassert 3 not in [1,2]\nassert 1 == 1, 1/0"
        self.assertEqual(self.execute(CheckSpec(id='semantics', kind='python_assertions', code=code)).status, 'pass')

    def test_nonfinite_operand_is_diagnostic_data_instead_of_protocol_failure(self):
        receipt = self.execute(CheckSpec(id='nan',kind='python_assertions',code="assert float('nan') == 42"))
        self.assertEqual(receipt.status,'fail')
        self.assertEqual(json.loads(receipt.result.output)['checks'][0]['left'],'nan')

    def test_python_check_records_subprocess_invocation_and_guard_operands(self):
        code = "import subprocess,sys\nr=subprocess.run([sys.executable,'-c','print(0)'],capture_output=True,text=True)\nif r.stdout.strip() != '42':\n    raise AssertionError('wrong result')"
        receipt = self.execute(CheckSpec(id='guard', kind='python_assertions', code=code))
        detail = json.loads(receipt.result.output)
        self.assertEqual(receipt.status, 'fail')
        self.assertEqual((detail['checks'][0]['left'], detail['checks'][0]['right']), ('0', '42'))
        self.assertIn('print(0)', detail['invocations'][0]['argv'])

    def test_process_check_rejects_wrong_output_on_copy(self):
        (self.work / 'product.py').write_text('print(42)', encoding='utf-8')
        spec = CheckSpec(id='cli', kind='process', argv=(sys.executable, '-B', 'product.py'),
            expected_stdout='42\n', controls=(CheckControl(name='wrong output', overrides={'product.py':'print(0)'}),))
        receipt = self.execute(spec)
        self.assertEqual(receipt.status, 'pass')
        self.assertTrue(receipt.sensitive)
        self.assertEqual((self.work / 'product.py').read_text(), 'print(42)')
        detail = json.loads(receipt.result.output)
        self.assertEqual(detail['argv'][-1], 'product.py')
        self.assertEqual(detail['checks'][1]['actual'], '42\n')

    def test_process_stdin_and_stderr_are_real_comparisons(self):
        code = "import sys; print(sys.stdin.read().upper()); print('warning',file=sys.stderr)"
        receipt = self.execute(CheckSpec(id='io', kind='process', argv=(sys.executable,'-c',code),
            stdin='hello', expected_stdout='HELLO\n', expected_stderr='different\n'))
        detail = json.loads(receipt.result.output)
        self.assertEqual(receipt.status, 'fail')
        self.assertEqual(detail['checks'][-1]['actual'], 'warning\n')

    def test_failed_file_comparison_reuses_fact_without_new_execution_or_progress(self):
        (self.work / 'answer').write_text('0')
        spec = CheckSpec(id='answer', kind='text_equals', path='answer', expected='42')
        result, graph, calls = self.actions([Action(kind='verify', checks=(spec,)),
            Action(kind='verify', check_ids=('answer',)), Action(kind='done'), Action(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertEqual(sum(c.args[0].kind.value == 'read' for c in calls.call_args_list), 1)
        self.assertFalse(graph.attempts[1].tool_executed)
        self.assertEqual(graph.attempts[1].verification, ())
        self.assertEqual(graph.attempts[1].progress.reason_code, 'reused_verification')
        self.assertEqual(graph.attempts[2].result.error_type, 'verification_required')
        self.assertEqual(json.loads(graph.attempts[1].result.output)['source_steps'], [1])

    def test_saved_definition_reruns_after_edit_and_done_reuses_current_comparison(self):
        (self.work / 'answer').write_text('0')
        spec = CheckSpec(id='answer', kind='text_equals', path='answer', expected='42')
        result, graph, calls = self.actions([Action(kind='verify', checks=(spec,)),
            Action(kind='write', path='answer', content='42'), Action(kind='verify', check_ids=('answer',)),
            Action(kind='done', check_ids=('answer',))])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(graph.attempts[2].verification[0].spec, spec)
        self.assertEqual(sum(c.args[0].kind.value == 'read' for c in calls.call_args_list), 2)
        self.assertFalse(graph.attempts[-1].tool_executed)

    def test_saved_process_check_executes_again_instead_of_caching_unknown_process_state(self):
        (self.work / 'product.py').write_text('print(42)')
        spec = CheckSpec(id='cli', kind='process', argv=(sys.executable,'-B','product.py'), expected_stdout='42\n')
        result, graph, _ = self.actions([Action(kind='verify', checks=(spec,)), Action(kind='done', check_ids=('cli',))])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertTrue(graph.attempts[1].tool_executed)
        self.assertEqual(graph.attempts[1].verification[0].spec, spec)

    def test_unknown_check_id_does_not_execute_any_check(self):
        _, graph, _ = self.actions([Action(kind='verify', check_ids=('missing',)), Action(kind='incomplete')])
        self.assertEqual(graph.attempts[0].result.error_type, 'check_definition_invalid')
        self.assertFalse(graph.attempts[0].tool_executed)

    def test_reference_shapes_and_terminal_frontend_share_validation(self):
        self.assertEqual(lower_terminal('{"action":"verify","check_ids":["cli"]}','/app').check_ids, ('cli',))
        for data in [dict(kind='verify', check_ids=['x'], command='true'),
                     dict(kind='command', check_ids=['x'], command='true'),
                     dict(kind='done', check_ids=['x','x'])]:
            with self.assertRaises(ValidationError): Action.model_validate(data)
        legacy = json.dumps(action_schema(exploration_history=False, verification_policy='legacy'))
        self.assertNotIn('check_ids', legacy)

    def test_decoder_selects_check_type_before_operands(self):
        schema=action_schema(verification_policy='behavioral')
        def branches(value):
            if isinstance(value,dict):
                if 'id' in value.get('properties',{}) and 'kind' in value['properties']:
                    yield value['properties']
                for item in value.values(): yield from branches(item)
            elif isinstance(value,list):
                for item in value: yield from branches(item)
        rows=list(branches(schema))
        self.assertTrue(rows)
        self.assertTrue(all(next(iter(row))=='kind' for row in rows))
        self.assertEqual(next(row['argv']['minItems'] for row in rows if 'argv' in row),1)

    def test_latest_reply_survives_long_goal_and_many_verification_receipts(self):
        body = 'start\n' + 'a'*900 + '\nCURRENT_DECISION_MARKER\n' + 'b'*900
        snapshot = StateSnapshot(files=(FileState(path='product.py',kind='file',sha256=digest(body),size=len(body),mode=420),))
        attempts = []
        for number in range(1,10):
            spec = CheckSpec(id='check-'+str(number),kind='python_assertions',code='assert 0 == 42\n#'+'x'*2500)
            receipt = VerificationReceipt(spec=spec,spec_hash=digest(canonical(spec)),snapshot_hash=snapshot.fingerprint,
                environment_hash='e',workspace_unchanged=True,status='fail',assertions=1,
                result=ToolResult(exit_code=1,output=canonical(dict(checks=[dict(expression='0 == 42',passed=False,left=0,right=42)]))))
            attempts.append(Attempt(step=number,action=Action(kind='verify',checks=(spec,)),before=snapshot,after=snapshot,
                delta=StateDelta(),result=receipt.result,verification=(receipt,),tool_executed=True,
                progress=ProgressAssessment(level='information',reason_code='fixture')))
        attempts.append(Attempt(step=10,action=Action(kind='read',path='product.py'),before=snapshot,after=snapshot,
            delta=StateDelta(),result=ToolResult(exit_code=0,output=body),tool_executed=True,
            progress=ProgressAssessment(level='information',reason_code='fixture')))
        task = contract(goal='public requirement\n'*270)
        request = ContextCompiler({}).build(task, GraphView(attempts=tuple(attempts)), snapshot,11,100000)
        self.assertIn('CURRENT_DECISION_MARKER', request.context)
        self.assertIn(10,request.selected_attempt_steps)
        self.assertIn(10,request.visible_observation_steps)
        self.assertLessEqual(token_bound(request.system)+token_bound(request.context)+128,request.input_token_bound)
        self.assertLessEqual(request.input_token_bound,14336)

    def test_observed_actions_cannot_be_mistaken_for_a_decision_envelope(self):
        (self.work/'answer').write_text('42')
        _,graph,_=self.actions([Action(kind='read',path='answer'),Action(kind='incomplete')])
        request=ContextCompiler({}).build(contract(),graph.view,graph.snapshot,3,100000)
        self.assertIn('OBSERVED ACTION (already attempted; not a new proposal)',request.context)
        self.assertNotIn('"action":{',request.context)
        self.assertIn('42',request.context)

    def test_fresh_symlink_read_keeps_actual_reply_without_claiming_reusable_content(self):
        snapshot=StateSnapshot(files=(FileState(path='alias',kind='symlink',sha256=digest('target'),size=6,mode=511),))
        attempt=Attempt(step=1,action=Action(kind='read',path='alias'),before=snapshot,after=snapshot,
            delta=StateDelta(),result=ToolResult(exit_code=0,output='ACTUAL_LINK_READ'),tool_executed=True,
            progress=ProgressAssessment(level='information',reason_code='fixture'))
        request=ContextCompiler({}).build(contract(),GraphView(attempts=(attempt,)),snapshot,2,100000)
        self.assertIn('ACTUAL_LINK_READ',request.context)
        self.assertNotIn(1,request.visible_observation_steps)

    def test_transient_model_service_failure_retries_without_executing_a_tool(self):
        count=[]
        def respond(request):
            count.append(1)
            if len(count)==1: return httpx.Response(500,json={'error':'generation parsing failed'})
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"kind":"incomplete"}'}}],
                'usage':{'prompt_tokens':1,'completion_tokens':1}})
        gateway=ModelGateway('fixture',client=httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(gateway.close)
        result=NodeAgent(backend='local',policy=ModelPolicy(gateway)).run(contract(),self.work,self.root/'http')
        graph=read_run(self.root/'http')
        self.assertEqual(result.termination,Termination.INCOMPLETE)
        self.assertEqual(len(graph.attempts),1)
        self.assertEqual(graph.attempts[0].action.kind.value,'incomplete')
        self.assertEqual(len(count),2)

    def test_bad_request_remains_fatal_instead_of_retrying_configuration_errors(self):
        from horizon_weaver.core import DecisionInput
        gateway=ModelGateway('fixture',client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(400))))
        self.addCleanup(gateway.close)
        with self.assertRaises(GatewayError) as error:
            gateway.complete(DecisionInput(step=1,system='',context='',input_token_bound=1,max_output_tokens=1),1)
        self.assertFalse(error.exception.retryable)

    def test_tiny_excerpt_never_exceeds_its_byte_budget(self):
        for limit in range(100): self.assertLessEqual(token_bound(excerpt('你好'*100,limit)),limit)

    def test_old_receipt_hash_is_readable_but_cannot_hide_new_operands(self):
        spec=CheckSpec(id='legacy',kind='python_assertions',code='assert True')
        fields={'argv','stdin','expected_exit_code','expected_stdout','expected_stderr'}
        data=dict(spec=spec,spec_hash=digest(canonical(spec.model_dump(exclude=fields,mode='json'))),
            snapshot_hash='s',environment_hash='e',workspace_unchanged=True,status='pass',assertions=1,result=ToolResult(exit_code=0))
        self.assertEqual(VerificationReceipt(**data).spec.code,'assert True')
        with self.assertRaises(ValidationError):
            VerificationReceipt(**dict(data,spec=CheckSpec(id='legacy',kind='process',argv=('bad',))))


@unittest.skipUnless(os.environ.get('HORIZON_WEAVER_DOCKER_TESTS')=='1','opt-in real Docker tests')
class ProcessDockerTests(unittest.TestCase):
    def test_direct_invocation_detects_missing_permission_and_recovers_after_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)
            harness=DockerHarness(work,os.environ.get('HORIZON_WEAVER_DOCKER_IMAGE','python:3.12-slim'))
            self.addCleanup(harness.close)
            harness.start(contract())
            harness.execute(Action(kind='command',command="printf '#!/bin/sh\\nprintf 42' > product.sh; chmod 0644 product.sh"),5)
            direct=CheckSpec(id='cli',kind='process',argv=('./product.sh',),expected_stdout='42')
            wrapped=CheckSpec(id='wrapper',kind='process',argv=('sh','product.sh'),expected_stdout='42')
            def check(spec): return run_checks(harness,(spec,),contract(),harness.snapshot(),time.monotonic()+15,5)[0]
            self.assertEqual(check(wrapped).status,'pass')
            bad=check(direct)
            self.assertEqual(bad.status,'fail')
            self.assertIn('PermissionError',bad.result.output)
            harness.execute(Action(kind='command',command='chmod 0755 product.sh'),5)
            self.assertEqual(check(direct).status,'pass')


if __name__=='__main__': unittest.main(verbosity=2)
