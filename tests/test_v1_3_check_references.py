"""Model references are linked to recorded definitions, not invented identifiers."""
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import closing

import httpx

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler
from horizon_weaver.core import (Budget, Check, DecisionInput, GraphView, StateSnapshot,
    TaskContract, Termination, action_schema, canonical, digest)
from horizon_weaver.event_store import EventStore
from horizon_weaver.policy import ModelGateway, ModelPolicy


def branches(value):
    if isinstance(value, dict):
        if 'check_ids' in value.get('properties', {}):
            yield value
        for child in value.values():
            yield from branches(child)
    elif isinstance(value, list):
        for child in value:
            yield from branches(child)


class CheckReferenceTests(unittest.TestCase):
    def test_no_recorded_definitions_removes_reference_variant_but_keeps_new_checks(self):
        schema = action_schema(verification_policy='behavioral', known_check_ids=())
        self.assertEqual(list(branches(schema)), [])
        self.assertIn('python_assertions', canonical(schema))
        self.assertIn('process', canonical(schema))

    def test_recorded_identifiers_are_the_only_reference_choices_in_every_branch(self):
        rows = list(branches(action_schema(known_check_ids=('z', 'a', 'z'))))
        self.assertEqual(len(rows), 4)  # direct/enveloped verify/done
        for row in rows:
            self.assertEqual(row['properties']['check_ids']['items']['enum'], ['a', 'z'])

    def test_generic_and_historical_requests_keep_optional_reference_metadata(self):
        old = dict(step=1, system='', context='', input_token_bound=1, max_output_tokens=1)
        request = DecisionInput.model_validate(old)
        self.assertIsNone(request.known_check_ids)
        for row in branches(action_schema(known_check_ids=request.known_check_ids)):
            self.assertNotIn('enum', row['properties']['check_ids']['items'])

    def test_legacy_never_advertises_structured_check_references(self):
        self.assertEqual(list(branches(action_schema(verification_policy='legacy', known_check_ids=('a',)))), [])

    def test_external_evaluator_name_is_not_a_recorded_definition(self):
        task = TaskContract(task_id='refs', goal='Produce 42.', verification_policy='behavioral',
            acceptance_checks=(Check(id='private-grader', kind='external'),))
        request = ContextCompiler({}).build(task, GraphView(), StateSnapshot(), 1, 100000)
        self.assertEqual(request.known_check_ids, ())
        self.assertNotIn('private-grader', request.context)

    def test_complete_loop_links_failed_then_repaired_definition_and_journals_actual_schema(self):
        bodies = iter([
            dict(kind='verify', checks=[dict(id='product', kind='text_equals', path='answer', expected='42')]),
            dict(kind='write', path='answer', content='42'),
            dict(kind='verify', check_ids=['product']),
            dict(kind='done')])
        payloads = []
        def respond(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(next(bodies))}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
        gateway = ModelGateway('fixture', client=httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(gateway.close)
        task = TaskContract(task_id='refs', goal='The file answer must contain 42.', exploration_history=True,
            verification_policy='behavioral', acceptance_checks=(Check(id='private-grader', kind='external'),),
            budget=Budget(max_steps=8, context_tokens=14336))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root/'work'; work.mkdir()
            (work/'answer').write_text('0')
            result = NodeAgent(backend='local', policy=ModelPolicy(gateway)).run(task, work, root/'run')
            graph = read_run(root/'run')
            self.assertEqual(result.termination, Termination.SUBMITTED)
            self.assertEqual(graph.attempts[0].verification[0].status, 'fail')
            self.assertEqual(graph.attempts[2].verification[0].status, 'pass')
            self.assertEqual(graph.attempts[2].verification[0].spec, graph.attempts[0].verification[0].spec)
            with closing(EventStore(root/'run/events.sqlite3', graph.manifest.run_id, readonly=True)) as store:
                started = [json.loads(e.data_json) for e in store.read() if e.kind=='ModelCallStarted']
            schemas = [p['response_format']['json_schema']['schema'] for p in payloads]
            self.assertEqual(list(branches(schemas[0])), [])
            for event, schema in zip(started, schemas, strict=True):
                self.assertEqual(event['decoder_schema_hash'], digest(canonical(schema)))
            self.assertEqual([s['request']['known_check_ids'] for s in started],
                [[], ['product'], ['product'], ['product']])
            for schema in schemas[1:]:
                self.assertTrue(all(b['properties']['check_ids']['items']['enum']==['product']
                    for b in branches(schema)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
