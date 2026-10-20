"""Decision deadlines, public references and evidence regressions."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler, token_bound
from horizon_weaver.core import (Action, Budget, Check, Decision, DecisionInput, ModelResponse,
    TaskContract, Termination, Usage, requirement_sources)
from horizon_weaver.errors import GatewayError
from horizon_weaver.event_store import EventStore
from horizon_weaver.policy import ModelGateway, ModelPolicy, ScriptedPolicy, parse_notes
from horizon_weaver.state_ir import StateIR
from horizon_weaver.terminal_policy import TerminalPolicy
from test_v1_2_reliability import python_command


class Replies:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []
        self.timeouts = []

    def complete(self, request, timeout_s):
        self.requests.append(request)
        self.timeouts.append(timeout_s)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return ModelResponse(content=value, usage=Usage(input_tokens=10, output_tokens=10))

    def describe(self):
        return {"kind": "fixture"}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()

    def task(self, **kwargs):
        return TaskContract(task_id="decisions", goal="Keep exact format.\n\nHandle boundary inputs.",
            acceptance_checks=(Check(id="hidden", kind="external"),), **kwargs)

    def run_replies(self, replies, task=None):
        gateway = Replies(replies)
        task = task or self.task(budget=Budget(max_steps=len(replies), no_progress_limit=20))
        result = NodeAgent(backend="local", policy=TerminalPolicy(gateway)).run(task, self.work, self.root / "run")
        return result, read_run(self.root / "run"), gateway

    def events(self, graph):
        with closing(EventStore(self.root / "run" / "events.sqlite3", graph.manifest.run_id, readonly=True)) as store:
            return store.read()

    def test_model_timeout_commits_no_action_and_next_decision_runs(self):
        result, graph, gateway = self.run_replies([
            GatewayError("deadline", Usage(input_tokens=13, output_tokens=21, estimated=True), retryable=True),
            '{"action":"write","target":"answer","content":"42"}', '{"action":"done"}'])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual((self.work / "answer").read_text(), "42")
        self.assertEqual([a.step for a in graph.attempts], [2, 3])
        self.assertEqual(result.usage.total, 74)
        self.assertIn("no action was executed", gateway.requests[1].context)
        self.assertEqual(len([e for e in self.events(graph) if e.kind == "ModelCallCompleted"]), 3)

    def test_nonretryable_gateway_failure_stops(self):
        result, graph, gateway = self.run_replies([GatewayError("unauthorized", Usage(input_tokens=1))])
        self.assertEqual(result.termination, Termination.MODEL_ERROR)
        self.assertFalse(graph.attempts)
        self.assertEqual(len(gateway.requests), 1)

    def test_repeated_model_timeouts_stop_at_no_progress_budget(self):
        task = self.task(budget=Budget(max_steps=10, no_progress_limit=2))
        result, graph, _ = self.run_replies([
            GatewayError("deadline", Usage(output_tokens=100), retryable=True) for _ in range(2)], task)
        self.assertEqual(result.termination, Termination.NO_PROGRESS)
        self.assertFalse(graph.attempts)
        self.assertEqual(result.usage.output_tokens, 200)

    def test_decision_budget_is_small_and_recorded(self):
        task = self.task(budget=Budget(model_timeout_s=2, max_model_timeout_s=1, output_tokens=3000))
        _, graph, gateway = self.run_replies(['{"action":"done"}'], task)
        self.assertEqual(gateway.requests[0].max_output_tokens, 2048)
        self.assertEqual(gateway.timeouts, [1])
        event = next(e for e in self.events(graph) if e.kind == "ModelCallStarted")
        self.assertEqual(json.loads(event.data_json)["timeout_s"], 1)

    def test_explicit_larger_budget_is_clamped_and_used_once(self):
        _, _, gateway = self.run_replies([
            json.dumps(dict(action="write", target="answer", content="x", notes=dict(
                next_output_tokens=999999, next_timeout_s=999))),
            '{"action":"read","target":"answer"}', '{"action":"done"}'])
        self.assertEqual([r.max_output_tokens for r in gateway.requests], [2048, 8192, 2048])
        self.assertEqual(gateway.timeouts, [60, 120, 60])

    def test_stable_refs_match_unmodified_public_lines(self):
        task = self.task(constraints=("Preserve source input.",))
        self.assertEqual(requirement_sources(task), {"G1": "Keep exact format.",
            "G3": "Handle boundary inputs.", "C1": "Preserve source input."})

    def test_partial_structural_and_semantic_bad_notes_preserve_siblings(self):
        result, graph, _ = self.run_replies([json.dumps(dict(action="done", notes=dict(requirements=[
            dict(ref="G3", uncertainty="boundary not checked"), dict(passed=True), dict(ref="G999")])) )])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual([n.ref for n in graph.view.notes.requirements], ["G3"])
        self.assertIn("ignored", graph.feedback)
        self.assertFalse(result.verdict.passed)
        self.assertEqual(StateIR.model_validate_json(graph.ir.model_dump_json()), graph.ir)

    def test_read_can_be_evidence_without_becoming_a_passing_test(self):
        (self.work / "answer").write_text("wrong")
        task = self.task(budget=Budget(max_steps=2))
        _, graph, _ = self.run_replies(['{"action":"read","target":"answer"}',
            json.dumps(dict(action="done", notes=dict(requirements=[dict(ref="G1", evidence_steps=[1])])) )], task)
        request = ContextCompiler({}).build(task, graph.view, graph.snapshot, 3, 100000)
        self.assertIn('"kind":"read"', request.context)
        self.assertIn('"coverage":"unknown"', request.context)
        self.assertFalse(graph.verdict.passed)
        self.assertLessEqual(token_bound(request.system) + token_bound(request.context) + 128, request.input_token_bound)

    def test_unexecuted_redundant_read_cannot_be_cited_as_new_evidence(self):
        _, graph, _ = self.run_replies(['{"action":"write","target":"answer","content":"42"}',
            '{"action":"read","target":"answer"}',
            json.dumps(dict(action="done", notes=dict(requirements=[dict(ref="G1", evidence_steps=[2])])) )])
        self.assertFalse(graph.attempts[1].tool_executed)
        self.assertIsNone(graph.view.notes)

    def test_print_only_verify_executes_as_observation_with_optional_gate(self):
        _, graph, _ = self.run_replies([
            json.dumps(dict(action="verify", target=python_command("print('wrong')"))),
            json.dumps(dict(action="done", notes=dict(requirements=[dict(ref="G1", evidence_steps=[1])])) )])
        receipt = graph.attempts[0].self_check
        self.assertEqual(receipt.classification, "observation")
        self.assertEqual(receipt.result.exit_code, 0)
        self.assertFalse(receipt.passed)
        request = ContextCompiler({}).build(graph.manifest.contract, graph.view, graph.snapshot, 3, 100000)
        self.assertIn('"kind":"observation"', request.context)
        self.assertIn('"command_ok":true', request.context)
        self.assertFalse(graph.verdict.passed)

    def test_admitted_assertion_is_still_not_contract_acceptance(self):
        result, graph, _ = self.run_replies([
            json.dumps(dict(action="verify", target=python_command("assert True"))), '{"action":"done"}'])
        self.assertEqual(graph.attempts[0].self_check.classification, "check_candidate")
        self.assertTrue(graph.attempts[0].self_check.passed)
        self.assertFalse(result.verdict.passed)

    def test_fake_change_keeps_actual_failure_visible(self):
        actions = (Action(kind="write", path="source.py", content="raise RuntimeError('actual failure')\n"),
            Action(kind="verify", command=python_command("raise RuntimeError('actual failure')")),
            Action(kind="write", path="source.py", content="raise RuntimeError('actual failure')\n", reasoning="fixed"))
        task = self.task(budget=Budget(max_steps=3))
        NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(task, self.work, self.root / "run")
        graph = read_run(self.root / "run")
        self.assertFalse(graph.attempts[-1].tool_executed)
        self.assertIn("Actual content changes: 0", graph.feedback)
        request = ContextCompiler({}).build(task, graph.view, graph.snapshot, 4, 100000)
        self.assertIn("LAST EXECUTED FAILURE ON CURRENT FILE STATE", request.context)
        self.assertIn("RuntimeError: actual failure", request.context)

    def test_http_timeout_has_typed_retryability_and_conservative_usage(self):
        def timeout(request):
            raise httpx.ReadTimeout("slow response")
        gateway = ModelGateway("fixture", client=httpx.Client(transport=httpx.MockTransport(timeout)))
        self.addCleanup(gateway.close)
        with self.assertRaises(GatewayError) as caught:
            gateway.complete(DecisionInput(step=1, system="", context="", input_token_bound=40, max_output_tokens=20), 1)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.usage.total, 60)


if __name__ == "__main__":
    unittest.main()
