"""Exploration provenance and checks that actually distinguish faulty behavior."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from pydantic import ValidationError

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.attached import AttachedDockerHarness
from horizon_weaver.context import ContextCompiler, token_bound
from horizon_weaver.core import (Action, Budget, Check, CheckControl, CheckSpec, Decision,
    ExplorationIntent, HistoryQuery, PolicyNotes, TaskContract, Termination, ToolResult, digest, canonical, action_schema)
from horizon_weaver.harness import DockerHarness, LocalHarness
from horizon_weaver.policy import ModelGateway, ModelPolicy, ScriptedPolicy, parse_output_with_feedback
from horizon_weaver.state_ir import StateIR
from horizon_weaver.state_queries import StateQuery
from horizon_weaver.terminal_policy import lower_terminal
from horizon_weaver.verification import run_checks, verification_state


def task(**changes):
    return TaskContract(task_id='exploration', goal='Write the answer 42 and verify its behavior',
        acceptance_checks=(Check(id='external', kind='external'),), exploration_history=True,
        verification_policy='behavioral', require_self_check=True,
        budget=Budget(max_steps=20, no_progress_limit=12), **changes)


def equality(identity='answer'):
    return CheckSpec(id=identity, kind='text_equals', path='answer', expected='42', target_refs=('G1',))


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def run_actions(self, actions, contract=None, policy=None):
        run = self.root / ('run-' + str(len(list(self.root.glob('run-*')))))
        result = NodeAgent(backend='local', policy=policy or ScriptedPolicy(tuple(actions))).run(
            contract or task(), self.work, run)
        return result, read_run(run)

    def receipts(self, code, controls=()):
        harness = LocalHarness(self.work)
        harness.start(task())
        self.addCleanup(harness.close)
        spec = CheckSpec(id='behavior', kind='python_assertions', code=code, controls=controls)
        return run_checks(harness, (spec,), task(), harness.snapshot(), time.monotonic() + 10, 5)[0]

    def test_done_can_execute_exact_comparison_without_extra_model_turn(self):
        result, graph = self.run_actions([Action(kind='write', path='answer', content='42'),
            Action(kind='done', checks=(equality(),))])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(result.steps, 2)
        self.assertFalse(result.verdict.passed)
        self.assertEqual(graph.attempts[-1].verification[0].status, 'pass')
        self.assertFalse(graph.attempts[-1].verification[0].oracle_validated)

    def test_done_reuses_current_runtime_comparison_without_reexecuting_it(self):
        harness = LocalHarness(self.work)
        actions = (Action(kind='write', path='answer', content='42'),
            Action(kind='verify', checks=(equality(),)), Action(kind='done', checks=(equality(),)))
        with patch.object(harness, 'execute', wraps=harness.execute) as execute:
            result = NodeAgent(policy=ScriptedPolicy(actions)).run_attached(task(), harness,
                self.root / 'reused', workspace_id='local-test')
        graph = read_run(self.root / 'reused')
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(sum(call.args[0].kind.value == 'read' for call in execute.call_args_list), 1)
        self.assertFalse(graph.attempts[-1].tool_executed)
        self.assertEqual(graph.attempts[-1].verification, ())

    def test_import_and_unreachable_assertions_do_not_supply_behavioral_evidence(self):
        for code in ('import math', 'if False:\n    assert False'):
            receipt = self.receipts(code)
            self.assertEqual(receipt.status, 'inconclusive')
            self.assertEqual(receipt.assertions, 0)
        result, graph = self.run_actions([Action(kind='verify', command='"' + sys.executable + '" -c "import math"'),
            Action(kind='done'), Action(kind='incomplete')])
        self.assertEqual(graph.attempts[1].result.error_type, 'verification_required')
        self.assertEqual(result.termination, Termination.INCOMPLETE)

    def test_counterexample_and_test_crash_have_different_status(self):
        for code, status, count in (('assert 1 == 2', 'fail', 1),
                ('assert (', 'inconclusive', 0), ('import missing_hw_test_dependency', 'inconclusive', 0)):
            receipt = self.receipts(code)
            self.assertEqual((receipt.status, receipt.assertions), (status, count))

    def test_explicit_exit_guards_record_actual_passes_and_counterexamples(self):
        (self.work / 'answer').write_text('42')
        code = "import sys\nfrom pathlib import Path\nvalue = Path('answer').read_text()\nif value != '42':\n    print('wrong answer')\n    sys.exit(1)\nprint('PASS')"
        receipt = self.receipts(code, (CheckControl(name='wrong', overrides={'answer': '0'}),))
        self.assertEqual((receipt.status, receipt.assertions), ('pass', 1))
        self.assertTrue(receipt.sensitive)
        self.assertEqual(receipt.controls[0].assertions, 1)
        self.assertIn('failure_guard', receipt.result.output)

    def test_assertion_error_guard_and_swallowed_exit_preserve_failure(self):
        for code in ('value = 0\nif value != 42:\n    raise AssertionError("wrong")',
                     'import sys\nvalue = 0\ntry:\n    if value != 42:\n        sys.exit(1)\nexcept SystemExit:\n    pass'):
            receipt = self.receipts(code)
            self.assertEqual((receipt.status, receipt.assertions), ('fail', 1))

    def test_unreachable_or_zero_exit_guards_do_not_supply_behavioral_evidence(self):
        for code in ('import sys\nif False:\n    sys.exit(1)',
                     'import sys\nvalue = 0\nif value != 42:\n    sys.exit(0)'):
            receipt = self.receipts(code)
            self.assertEqual((receipt.status, receipt.assertions), ('inconclusive', 0))

    def test_dependency_error_after_passing_guard_stays_inconclusive(self):
        receipt = self.receipts('import sys\nvalue = 42\nif value != 42:\n    sys.exit(1)\nimport missing_hw_guard_dependency')
        self.assertEqual((receipt.status, receipt.assertions), ('inconclusive', 1))

    def test_swallowed_failed_assertion_remains_a_failure(self):
        receipt = self.receipts('try:\n    assert False\nexcept AssertionError:\n    pass\nassert True')
        self.assertEqual(receipt.status, 'fail')
        self.assertEqual(receipt.assertions, 2)

    def test_wrong_candidate_control_is_detected_without_modifying_original(self):
        (self.work / 'product.py').write_text('answer = 42', encoding='utf-8')
        receipt = self.receipts('from product import answer\nassert answer == 42',
            (CheckControl(name='wrong answer', overrides={'product.py': 'answer = 0'}),))
        self.assertEqual(receipt.status, 'pass')
        self.assertTrue(receipt.sensitive)
        self.assertEqual(receipt.controls[0].status, 'fail')
        self.assertEqual((self.work / 'product.py').read_text(), 'answer = 42')
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ['product.py'])

    def test_control_crash_does_not_prove_sensitivity(self):
        (self.work / 'product.py').write_text('answer = 42', encoding='utf-8')
        receipt = self.receipts('from product import answer\nassert answer == 42',
            (CheckControl(name='broken syntax', overrides={'product.py': 'answer = ('}),))
        self.assertEqual(receipt.controls[0].status, 'inconclusive')
        self.assertFalse(receipt.sensitive)

    def test_mutating_check_cannot_grant_current_candidate_evidence(self):
        receipt = self.receipts("from pathlib import Path\nPath('answer').write_text('42')\nassert True")
        self.assertEqual(receipt.status, 'inconclusive')
        self.assertFalse(receipt.workspace_unchanged)

    def test_failures_survive_edits_new_ids_and_weaker_predicates(self):
        result, graph = self.run_actions([
            Action(kind='write', path='answer', content='bad'),
            Action(kind='verify', checks=(equality(),)),
            Action(kind='write', path='answer', content='42'),
            Action(kind='verify', checks=(equality('renamed'),)), Action(kind='done'),
            Action(kind='verify', checks=(CheckSpec(id='answer', kind='python_assertions', code='assert True'),)),
            Action(kind='done'), Action(kind='verify', checks=(equality(),)), Action(kind='done')])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(graph.attempts[4].result.error_type, 'verification_required')
        self.assertEqual(graph.attempts[6].result.error_type, 'verification_required')
        self.assertFalse(result.verdict.passed)

    def test_wrong_test_revision_needs_explicit_basis_and_discriminating_control(self):
        wrong = CheckSpec(id='wrong-expectation', kind='text_equals', path='answer', expected='43', target_refs=('G1',))
        revised = CheckSpec(id='public-expectation', kind='text_equals', path='answer', expected='42',
            target_refs=('G1',), replaces=('wrong-expectation',), basis='Public goal asks for 42, not 43',
            controls=(CheckControl(name='wrong answer', overrides={'answer': '0'}),))
        result, graph = self.run_actions([Action(kind='write', path='answer', content='42'),
            Action(kind='verify', checks=(wrong,)), Action(kind='verify', checks=(revised,)), Action(kind='done')])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        state = verification_state(graph.view.attempts, graph.snapshot)
        self.assertEqual(state['replacements'][0]['authority'], 'policy replacement claim')
        self.assertFalse(result.verdict.passed)

    def test_constant_assertion_cannot_replace_a_reached_counterexample(self):
        weak = CheckSpec(id='weak', kind='python_assertions', code='assert True', target_refs=('G1',),
            replaces=('answer',), basis='Claim that the old test was wrong',
            controls=(CheckControl(name='wrong answer', overrides={'answer': 'wrong'}),))
        result, graph = self.run_actions([Action(kind='write', path='answer', content='wrong'),
            Action(kind='verify', checks=(equality(),)), Action(kind='verify', checks=(weak,)),
            Action(kind='done'), Action(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertEqual(graph.attempts[3].result.error_type, 'verification_required')

    def test_test_crash_can_be_revised_without_repeating_a_broken_script(self):
        broken = CheckSpec(id='broken', kind='python_assertions', code='assert (', target_refs=('G1',))
        repaired = CheckSpec(id='repaired', kind='text_equals', path='answer', expected='42', target_refs=('G1',),
            replaces=('broken',), basis='Replace the syntax error with a runtime-owned comparison')
        result, graph = self.run_actions([Action(kind='write', path='answer', content='42'),
            Action(kind='verify', checks=(broken,)), Action(kind='verify', checks=(repaired,)), Action(kind='done')])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(graph.attempts[1].verification[0].status, 'inconclusive')

    def test_long_check_uses_stdin_instead_of_windows_shell_command_limit(self):
        receipt = self.receipts('#' + 'x' * 12000 + '\nassert 2 + 2 == 4')
        self.assertEqual((receipt.status, receipt.assertions), ('pass', 1))

    def test_environment_change_invalidates_current_evidence(self):
        result, graph = self.run_actions([Action(kind='write', path='answer', content='42'),
            Action(kind='verify', checks=(CheckSpec(id='process-scope', kind='python_assertions',
                code="from pathlib import Path\nassert Path('answer').read_text() == '42'"),)), Action(kind='done')])
        self.assertTrue(verification_state(graph.view.attempts, graph.snapshot)['ready'])
        self.assertFalse(verification_state(graph.view.attempts, graph.snapshot, 'different-environment')['ready'])

    def test_shell_environment_epoch_invalidates_previous_checks(self):
        process_check = CheckSpec(id='process-scope', kind='python_assertions',
            code="from pathlib import Path\nassert Path('answer').read_text() == '42'")
        result, graph = self.run_actions([Action(kind='write', path='answer', content='42'),
            Action(kind='verify', checks=(process_check,)), Action(kind='command', command='echo observed'),
            Action(kind='done'), Action(kind='verify', checks=(process_check,)), Action(kind='done')])
        self.assertEqual(graph.attempts[3].result.error_type, 'verification_required')
        self.assertEqual(result.termination, Termination.SUBMITTED)

    def test_file_changes_invalidate_passing_check_without_erasing_history(self):
        result, graph = self.run_actions([Action(kind='write', path='answer', content='42'),
            Action(kind='verify', checks=(equality(),)), Action(kind='write', path='answer', content='wrong'),
            Action(kind='done'), Action(kind='incomplete')])
        self.assertEqual(result.termination, Termination.INCOMPLETE)
        self.assertEqual(graph.attempts[3].result.error_type, 'verification_required')
        self.assertEqual(graph.attempts[1].verification[0].status, 'pass')

    def test_unvalidated_oracle_claim_does_not_override_human_contract(self):
        (self.work / 'answer').write_text('wrong')
        contract = TaskContract(task_id='public', goal='Write 42',
            acceptance_checks=(Check(id='correct', kind='text_equals', path='answer', expected='42'),))
        harness = LocalHarness(self.work)
        harness.start(contract)
        self.addCleanup(harness.close)
        spec = CheckSpec(id='fake', kind='text_equals', path='answer', expected='wrong', oracle='contract')
        receipt = run_checks(harness, (spec,), contract, harness.snapshot(), time.monotonic() + 5, 2)[0]
        self.assertEqual(receipt.status, 'pass')
        self.assertFalse(receipt.oracle_validated)
        trusted = equality()
        receipt = run_checks(harness, (trusted,), contract, harness.snapshot(), time.monotonic() + 5, 2)[0]
        self.assertTrue(receipt.oracle_validated)
        self.assertEqual(receipt.status, 'fail')

    def test_history_query_does_not_execute_tools_and_replay_is_pure(self):
        actions = [Action(kind='write', path='answer', content='bad'),
            Action(kind='verify', checks=(equality(),)),
            Action(kind='history', query=HistoryQuery(failures_only=True)), Action(kind='incomplete')]
        result, graph = self.run_actions(actions)
        history = graph.attempts[2]
        self.assertFalse(history.tool_executed)
        self.assertEqual(json.loads(history.result.output)['records'][0]['step'], 2)
        with patch.object(LocalHarness, 'execute', side_effect=AssertionError('replay executed a tool')):
            self.assertEqual(StateIR.model_validate_json(graph.ir.model_dump_json()), graph.ir)
            StateQuery(graph.ir).exploration_history()
        self.assertTrue(graph.ir.exploration[1].event_hash)

    def test_history_records_preserve_hypothesis_revisions_and_older_failure(self):
        decisions = [Decision(action=Action(kind='verify', checks=(equality(),)), notes=PolicyNotes(
            exploration=ExplorationIntent(problem_refs=('G1',), paths=('answer',), method='compare', hypothesis='answer may be correct')))]
        decisions += [Decision(action=Action(kind='write', path=f'unrelated-{i}', content=str(i))) for i in range(6)]
        decisions += [Decision(action=Action(kind='incomplete'), notes=PolicyNotes(
            exploration=ExplorationIntent(problem_refs=('G1',), method='inspect', evidence_steps=(1,), revises_steps=(1,))))]
        class Policy:
            def __init__(self): self.index = 0
            def decide(self, request, timeout_s):
                value = decisions[self.index]
                self.index += 1
                return value
            def describe(self): return dict(kind='scripted-notes')
        result, graph = self.run_actions([], policy=Policy())
        self.assertEqual(len(graph.view.notes_history), 2)
        old = StateQuery(graph.ir).exploration_history(HistoryQuery(path='answer', ref='G1'))[0]
        self.assertEqual(old['step'], 1)
        self.assertFalse(old['input_state_current'])
        self.assertEqual(len(old['changes_since']), 6)
        self.assertEqual(graph.ir.notes_history[-1].notes.exploration.revises_steps, (1,))
        request = ContextCompiler({'backend': 'local'}).build(graph.manifest.contract, graph.view,
            graph.snapshot, 9, 100000)
        self.assertIn('EXPLORATION HISTORY', request.context)
        self.assertIn('"step":1', request.context)
        self.assertLessEqual(token_bound(request.system) + token_bound(request.context), request.input_token_bound)

    def test_wait_changes_are_separate_attempt_conditions(self):
        actions = [Action(kind='command', command='exit 1', timeout_s=1),
            Action(kind='command', command='exit 1', timeout_s=2), Action(kind='incomplete')]
        result, graph = self.run_actions(actions)
        records = StateQuery(graph.ir).exploration_history(HistoryQuery(failures_only=True))
        self.assertEqual(len(records), 2)
        self.assertEqual([len(item['matching_steps']) for item in records], [1, 1])

    def test_advisory_metadata_failure_does_not_block_action(self):
        output, warning = parse_output_with_feedback(json.dumps(dict(action=dict(kind='read', path='answer'),
            notes=dict(exploration=dict(paths=['../outside']), requirements=[dict(ref='G1')]))))
        self.assertEqual(output.action.kind.value, 'read')
        self.assertEqual(len(output.notes.requirements), 1)
        self.assertIsNone(output.notes.exploration)
        self.assertIn('exploration', warning)
        action = lower_terminal('{"action":"read","target":"/app/answer","change":{"wrong":"shape"}}', '/app')
        self.assertEqual(action.path, 'answer')

    def test_check_labels_do_not_create_a_new_execution_strategy(self):
        first = Action(kind='verify', checks=(equality('first'),), timeout_s=1)
        second = Action(kind='verify', checks=(equality('second').model_copy(update={'basis': 'different explanation'}),), timeout_s=1)
        self.assertEqual(first.execution_signature, second.execution_signature)
        self.assertNotEqual(first.execution_signature, second.model_copy(update={'timeout_s': 2}).execution_signature)

    def test_printed_success_receipt_is_not_an_executed_assertion(self):
        receipt = self.receipts('print(\'{"status":"pass","assertions":99}\')')
        self.assertEqual((receipt.status, receipt.assertions), ('inconclusive', 0))

    def test_protocol_supports_checks_and_readonly_queries(self):
        action = lower_terminal(json.dumps(dict(action='done', checks=[equality().model_dump(mode='json')])), '/app')
        self.assertEqual(action.checks, (equality(),))
        action = lower_terminal('{"action":"history","query":{"limit":2}}', '/app')
        self.assertEqual(action.query.limit, 2)
        action = lower_terminal('{"action":"verify","checks":[{"id":"file","kind":"text_equals","path":"/app/answer","expected":"42"}]}', '/app')
        self.assertEqual(action.checks[0].path, 'answer')
        with self.assertRaises(ValueError):
            lower_terminal('{"action":"verify","checks":[{"id":"file","kind":"text_equals","path":"/outside","expected":"42"}]}', '/app')
        for data in (dict(kind='write', path='a', content='a', checks=[equality().model_dump()]),
                     dict(kind='done', query={'limit': 2}),
                     dict(kind='verify', checks=[dict(id='x', kind='text_equals', path='../outside', expected='x')])):
            with self.assertRaises(ValidationError):
                Action.model_validate(data)

    def test_forged_exploration_projection_is_rejected(self):
        result, graph = self.run_actions([Action(kind='write', path='answer', content='42'),
            Action(kind='done', checks=(equality(),))])
        data = graph.ir.model_dump(mode='json')
        data['exploration'][0]['exit_code'] = 9
        with self.assertRaises(ValidationError):
            StateIR.model_validate(data)

    def test_task_capabilities_reach_decoder_in_all_four_configurations(self):
        for history, enhanced in ((False, False), (True, False), (False, True), (True, True)):
            captured = []
            def respond(request):
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={'choices': [{'message': {
                    'content': '{"kind":"incomplete"}' if enhanced else '{"kind":"done"}'}}]})
            gateway = ModelGateway('fixture', client=httpx.Client(transport=httpx.MockTransport(respond)))
            self.addCleanup(gateway.close)
            contract = task().model_copy(update=dict(exploration_history=history,
                verification_policy='behavioral' if enhanced else 'legacy', require_self_check=False))
            _, graph = self.run_actions([], contract, ModelPolicy(gateway))
            schema = captured[0]['response_format']['json_schema']['schema']
            variants = schema['anyOf'][:-1]
            kinds = {v['properties']['kind']['const'] for v in variants}
            self.assertEqual('history' in kinds, history)
            self.assertEqual(any('checks' in v['properties'] for v in variants), enhanced)
            self.assertFalse(any('checks' in v['properties'] and 'command' in v['properties'] for v in variants))
            self.assertEqual(graph.result.steps, 1)

    def test_disabled_structured_check_cannot_execute_through_scripted_policy(self):
        (self.work / 'answer').write_text('42')
        contract = task().model_copy(update=dict(exploration_history=False, verification_policy='legacy'))
        result, graph = self.run_actions([Action(kind='verify', checks=(equality(),)),
            Action(kind='done')], contract)
        self.assertEqual(graph.attempts[0].result.error_type, 'verification_disabled')
        self.assertFalse(graph.attempts[0].tool_executed)
        self.assertEqual(graph.attempts[0].verification, ())

    def test_decoder_avoids_nested_refs_and_keeps_large_code_limit_in_runtime(self):
        schema = action_schema(exploration_history=True, verification_policy='behavioral')
        self.assertNotIn('"$ref"', json.dumps(schema))
        def assert_pure_unions(value):
            if isinstance(value, dict):
                if 'anyOf' in value:
                    self.assertNotIn('properties', value)
                for item in value.values():
                    assert_pure_unions(item)
            elif isinstance(value, list):
                for item in value:
                    assert_pure_unions(item)
        assert_pure_unions(schema)
        def code_fields(value):
            if isinstance(value, dict):
                if 'code' in value.get('properties', {}):
                    yield value['properties']['code']
                for item in value.values():
                    yield from code_fields(item)
            elif isinstance(value, list):
                for item in value:
                    yield from code_fields(item)
        fields = list(code_fields(schema))
        self.assertTrue(fields)
        self.assertTrue(all('maxLength' not in item for item in fields))
        with self.assertRaises(ValidationError):
            CheckSpec(id='large', kind='python_assertions', code='x' * 32001)


@unittest.skipUnless(os.environ.get('HORIZON_WEAVER_DOCKER_TESTS') == '1', 'opt-in real Docker tests')
class ExplorationDockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.owner = DockerHarness(self.work, os.environ.get('HORIZON_WEAVER_DOCKER_IMAGE', 'python:3.12-slim'))
        self.addCleanup(self.owner.close)
        self.owner.start(task())

    def test_docker_assertions_and_control_copy_preserve_candidate(self):
        (self.work / 'product.py').write_text('answer = 42')
        spec = CheckSpec(id='behavior', kind='python_assertions', code='from product import answer\nassert answer == 42',
            controls=(CheckControl(name='wrong answer', overrides={'product.py': 'answer = 0'}),))
        receipt = run_checks(self.owner, (spec,), task(), self.owner.snapshot(), time.monotonic() + 15, 5)[0]
        self.assertEqual(receipt.status, 'pass')
        self.assertTrue(receipt.sensitive)
        self.assertEqual((self.work / 'product.py').read_text(), 'answer = 42')

    def test_subprocess_failure_guards_work_in_real_container_and_wrong_copy(self):
        (self.work / 'product.py').write_text('print(42)')
        code = "import subprocess, sys\nresult = subprocess.run([sys.executable, 'product.py'], capture_output=True, text=True)\nif result.returncode != 0:\n    sys.exit(1)\nif result.stdout.strip() != '42':\n    sys.exit(1)"
        spec = CheckSpec(id='behavior', kind='python_assertions', code=code,
            controls=(CheckControl(name='wrong', overrides={'product.py': 'print(0)'}),))
        receipt = run_checks(self.owner, (spec,), task(), self.owner.snapshot(), time.monotonic() + 15, 5)[0]
        self.assertEqual((receipt.status, receipt.assertions), ('pass', 2))
        self.assertTrue(receipt.sensitive)
        self.assertEqual(receipt.controls[0].assertions, 2)

    def test_attached_verification_recovers_lost_response_without_reexecution(self):
        harness = AttachedDockerHarness(self.owner.container, workspace='/workspace', python='python')
        harness.start(task())
        self.addCleanup(harness.close)
        original = harness._call
        count = []
        def lose_reply(operation, timeout_s, **values):
            response = original(operation, timeout_s, **values)
            if operation == 'command':
                count.append(operation)
                return dict(outcome_unknown=True)
            return response
        with patch.object(harness, '_call', lose_reply):
            spec = CheckSpec(id='behavior', kind='python_assertions', code='assert 2 + 2 == 4')
            receipt = run_checks(harness, (spec,), task(), harness.snapshot(), time.monotonic() + 15, 5)[0]
        self.assertEqual(receipt.status, 'pass')
        self.assertTrue(receipt.result.transport_recovered)
        self.assertEqual(len(count), 1)

    def test_attached_timeout_is_inconclusive_and_does_not_leave_late_writes(self):
        harness = AttachedDockerHarness(self.owner.container, workspace='/workspace', python='python')
        harness.start(task())
        self.addCleanup(harness.close)
        spec = CheckSpec(id='slow', kind='python_assertions',
            code="import time\nfrom pathlib import Path\ntime.sleep(2)\nPath('late').write_text('bad')\nassert True")
        receipt = run_checks(harness, (spec,), task(), harness.snapshot(), time.monotonic() + 10, .2)[0]
        self.assertEqual(receipt.status, 'inconclusive')
        self.assertFalse(receipt.result.outcome_unknown)
        self.assertTrue(receipt.result.execution_terminated)
        time.sleep(2.1)
        self.assertFalse((self.work / 'late').exists())

    def test_control_cannot_follow_symlink_outside_candidate(self):
        self.owner.execute(Action(kind='command', command='ln -s /tmp outside'), 2)
        harness = AttachedDockerHarness(self.owner.container, workspace='/workspace', python='python')
        harness.start(task())
        self.addCleanup(harness.close)
        # The link is a Linux link on a Windows bind mount. Inspect and remove
        # it in its owning container rather than resolving it on the host.
        self.addCleanup(lambda: self.owner.execute(Action(kind='command', command='rm -- outside'), 2))
        spec = CheckSpec(id='control', kind='python_assertions', code='assert True',
            controls=(CheckControl(name='unsafe copy', overrides={'file': 'wrong'}),))
        receipt = run_checks(harness, (spec,), task(), harness.snapshot(), time.monotonic() + 10, 3)[0]
        self.assertEqual(receipt.controls[0].status, 'inconclusive')
        self.assertFalse(receipt.sensitive)
