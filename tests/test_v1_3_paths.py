"""Policy namespace translation must preserve strict IR and execution scope."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.core import Action, Budget, Check, DecisionInput, ModelResponse, TaskContract, Usage
from horizon_weaver.harness import LocalHarness
from horizon_weaver.policy import ModelPolicy, parse_action
from horizon_weaver.terminal_policy import lower_terminal


class Gateway:
    def __init__(self, data):
        self.raw = json.dumps(data)

    def complete(self, request, timeout_s):
        return ModelResponse(content=self.raw, usage=Usage(input_tokens=1, output_tokens=1))

    def describe(self):
        return {'kind': 'fixture'}

    def close(self):
        pass


class PolicyPathTests(unittest.TestCase):
    def decide(self, data, workspace='/app'):
        gateway = Gateway(data)
        decision = ModelPolicy(gateway, workspace=workspace).decide(
            DecisionInput(step=1, system='', context='', input_token_bound=1, max_output_tokens=200), 1)
        self.assertEqual(decision.raw, gateway.raw)
        return decision

    def test_ir_reads_writes_and_checks_match_relative_definitions(self):
        cases = [dict(kind='read', path='/app/product.txt'),
                 dict(kind='write', path='/app/product.txt', content='42'),
                 dict(kind='verify', checks=[dict(id='answer', kind='text_equals', path='/app/product.txt', expected='42')])]
        for data in cases:
            with self.subTest(kind=data['kind']):
                expected = json.loads(json.dumps(data).replace('/app/product.txt', 'product.txt'))
                decision = self.decide(data)
                self.assertIsNone(decision.error)
                self.assertEqual(decision.action, Action.model_validate(expected))

    def test_envelope_controls_and_history_queries_use_the_same_namespace(self):
        action = dict(kind='verify', checks=[dict(id='answer', kind='text_equals', path='/app/product.txt', expected='42',
            controls=[dict(name='wrong', overrides={'/app/product.txt': '0'})])])
        decision = self.decide(dict(action=action))
        self.assertIsNone(decision.error)
        self.assertEqual(decision.action.checks[0].controls[0].overrides, {'product.txt': '0'})
        history = self.decide(dict(kind='history', query=dict(path='/app/product.txt')))
        self.assertEqual(history.action.query.path, 'product.txt')

    def test_sibling_prefix_traversal_and_missing_namespace_are_rejected(self):
        for path, workspace in [('/application/product.txt', '/app'), ('/etc/passwd', '/app'),
                                ('/app/../etc/passwd', '/app'), ('/app/product.txt', ''),
                                ('/app/product.txt', r'C:\workspace')]:
            with self.subTest(path=path, workspace=workspace):
                decision = self.decide(dict(kind='read', path=path), workspace)
                self.assertIsNone(decision.action)
                self.assertIsNotNone(decision.error)

    def test_normalization_cannot_silently_overwrite_control_paths(self):
        for alias in ('/app/product.txt', './product.txt'):
            with self.subTest(alias=alias):
                data = dict(kind='verify', checks=[dict(id='answer', kind='text_equals', path='product.txt', expected='42',
                    controls=[dict(name='ambiguous', overrides={alias: '0', 'product.txt': '1'})])])
                self.assertIsNone(self.decide(data).action)
                with self.assertRaises(ValueError):
                    lower_terminal(json.dumps(data), '/app')

    def test_relative_aliases_have_one_artifact_identity(self):
        for path in ('./product.txt', '/app//./product.txt'):
            self.assertEqual(self.decide(dict(kind='read', path=path)).action.path, 'product.txt')

    def test_composition_uses_harness_namespace_and_guards_repeated_alias_reads(self):
        class NamespacedHarness(LocalHarness):
            def describe(self):
                return dict(super().describe(), working_directory='/app')
        class QueueGateway(Gateway):
            def __init__(self):
                self.rows=iter([dict(kind='read', path='/app/product.txt'),
                                dict(kind='read', path='./product.txt'), dict(kind='incomplete')])
            def complete(self, request, timeout_s):
                self.raw=json.dumps(next(self.rows))
                return super().complete(request, timeout_s)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            work=root/'work';work.mkdir()
            (work/'product.txt').write_text('42')
            harness=NamespacedHarness(work)
            task=TaskContract(task_id='path-scope', goal='Read product.txt.', exploration_history=True,
                verification_policy='behavioral', acceptance_checks=(Check(id='external', kind='external'),),
                budget=Budget(max_steps=8, context_tokens=14336))
            with patch('horizon_weaver.app.ModelGateway', return_value=QueueGateway()), \
                    patch.object(harness, 'execute', wraps=harness.execute) as executed:
                NodeAgent(model='fixture', policy_style='ir').run_attached(task, harness, root/'run', workspace_id='mapped-local')
            graph=read_run(root/'run')
            self.assertEqual(graph.attempts[0].action.path, 'product.txt')
            self.assertEqual(graph.attempts[1].result.error_type, 'redundant_action')
            self.assertEqual(executed.call_count, 1)

    def test_executable_arguments_and_embedded_program_paths_are_preserved(self):
        data = dict(kind='verify', checks=[dict(id='cli', kind='process', argv=['/usr/bin/python3', '/app/product.py'])])
        self.assertEqual(self.decide(data).action.checks[0].argv, ('/usr/bin/python3', '/app/product.py'))
        data = dict(kind='verify', checks=[dict(id='py', kind='python_assertions', code="assert open('/app/product.txt').read() == '42'")])
        self.assertEqual(self.decide(data).action.checks[0].code, data['checks'][0]['code'])
        data = dict(kind='command', command='cat /app/product.txt')
        self.assertEqual(self.decide(data).action.command, data['command'])

    def test_extra_fields_cannot_declare_authority_or_change_workspace(self):
        data = dict(action=dict(kind='read', path='/app/product.txt'), workspace='/etc')
        self.assertIsNone(self.decide(data).action)
        data = dict(kind='done', acceptance_success=True)
        self.assertIsNone(self.decide(data).action)

    def test_domain_and_bare_ir_parser_still_require_relative_paths(self):
        with self.assertRaises(ValueError):
            Action(kind='read', path='/app/product.txt')
        with self.assertRaises(ValueError):
            parse_action('{"kind":"read","path":"/app/product.txt"}')

    def test_terminal_and_ir_lower_the_same_scoped_check(self):
        data = dict(kind='done', checks=[dict(id='answer', kind='text_equals', path='/app/product.txt', expected='42')])
        self.assertEqual(self.decide(data).action, lower_terminal(json.dumps(data), '/app'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
