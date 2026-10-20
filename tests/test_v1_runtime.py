from __future__ import annotations

import ast
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.composition import Component, Runtime
from horizon_weaver.context import ContextCompiler, ContextBudgetError, token_bound
from horizon_weaver.core import (Action, Budget, Check, Decision, DecisionInput, GraphView,
                                 TaskContract, Termination, ToolResult, Usage)
from horizon_weaver.evaluation import Evaluator, current_verdict
from horizon_weaver.harness import DockerHarness, LocalHarness, run_process
from horizon_weaver.errors import HarnessError
from horizon_weaver.policy import GatewayError, ModelGateway, ModelPolicy, ScriptedPolicy, parse_action
from horizon_weaver.state import EventStore, StateGraph, StoreError


def python_command(code: str) -> str:
    args = [sys.executable, "-c", code]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def contract(**budget) -> TaskContract:
    return TaskContract(task_id="answer", goal="Write the exact answer",
        expected_artifacts=("answer.txt",),
        acceptance_checks=(Check(id="answer", kind="text_equals", path="answer.txt", expected="42\n"),),
        budget=Budget(**budget))


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.run_dir = self.root / "run"

    def tearDown(self):
        # Windows will fail this if a database or process handle is leaked.
        self.temporary.cleanup()

    def run_actions(self, actions, task=None):
        return NodeAgent(backend="local", policy=ScriptedPolicy(tuple(actions))).run(
            task or contract(), self.work, self.run_dir)


class DockerTransportTests(unittest.TestCase):
    def test_cleanup_timeout_keeps_identity_for_reconciliation(self):
        harness = DockerHarness(Path('.'))
        harness.container = 'horizon-weaver-owned-test'
        failed = ToolResult(error_type='timeout', outcome_unknown=True)
        with patch('horizon_weaver.harness.run_process', side_effect=[failed, ToolResult(exit_code=0)]) as call:
            with self.assertRaisesRegex(HarnessError, 'horizon-weaver-owned-test.*timeout'):
                harness.close()
            self.assertEqual(harness.container, 'horizon-weaver-owned-test')
            harness.close()
            self.assertIsNone(harness.container)
            harness.close()
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.args[0],
                             ['docker', 'rm', '--force', 'horizon-weaver-owned-test'])

    def test_exec_transport_timeout_remains_diagnostic(self):
        harness = DockerHarness(Path('.'))
        harness.container = 'horizon-weaver-owned-test'
        failed = ToolResult(error_type='timeout', outcome_unknown=True)
        with patch('horizon_weaver.harness.run_process', return_value=failed):
            result = harness.execute(Action(kind='command', command='printf ready'), 1)
        self.assertEqual(result.error_type, 'timeout')
        self.assertTrue(result.outcome_unknown)
        self.assertIn('horizon-weaver-owned-test', result.output)


class DomainTests(unittest.TestCase):
    def test_contract_and_nested_checks_are_immutable(self):
        task = contract()
        with self.assertRaises(ValidationError):
            task.goal = "lower requirements"
        with self.assertRaises(ValidationError):
            task.acceptance_checks[0].expected = "anything"

    def test_contract_requires_unique_external_checks_and_relative_paths(self):
        with self.assertRaises(ValidationError):
            TaskContract(task_id="x", goal="x", acceptance_checks=())
        for path in ("../outside", "/absolute", "C:/outside", "dir\\file"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                Action(kind="read", path=path)
        with self.assertRaises(ValidationError):
            contract().model_validate({**contract().model_dump(),
                                      "acceptance_checks": [contract().acceptance_checks[0]] * 2})

    def test_policy_cannot_attach_checks_to_done(self):
        with self.assertRaises(ValidationError):
            parse_action('{"kind":"done","acceptance_checks":[]}')
        with self.assertRaises(ValueError):
            parse_action('reasoning {"kind":"done"}')
        self.assertEqual(parse_action('```json\n{"kind":"done"}\n```').kind.value, "done")
        with self.assertRaises(ValueError):
            parse_action('{"kind":"read","kind":"done","path":"x"}')


class CompositionTests(unittest.TestCase):
    def test_dependency_validation_precedes_allocation(self):
        acquired = []
        def factory(services, resources):
            acquired.append(True)
            return {"a": object()}
        for components in (
            (Component("a", ("a",), ("missing",), factory),),
            (Component("a", ("a",), ("b",), factory), Component("b", ("b",), ("a",), factory)),
            (Component("a", ("a",), (), factory), Component("b", ("a",), (), factory)),
        ):
            with self.assertRaises(ValueError):
                Runtime(components).start()
        self.assertEqual(acquired, [])

    def test_partial_setup_rollback_and_reverse_cleanup(self):
        cleanup = []
        def first(services, resources):
            resources.callback(cleanup.append, "first")
            return {"first": 1}
        def second(services, resources):
            self.assertEqual(services["first"], 1)
            with self.assertRaises(TypeError):
                services["first"] = 2
            resources.callback(cleanup.append, "second")
            raise RuntimeError("partial allocation failed")
        runtime = Runtime((Component("second", ("second",), ("first",), second),
                           Component("first", ("first",), (), first)))
        with self.assertRaises(RuntimeError):
            runtime.start()
        self.assertEqual(cleanup, ["second", "first"])
        runtime.stop()
        self.assertEqual(cleanup, ["second", "first"])


class AcceptanceTests(WorkspaceCase):
    def test_text_assertions_distinguish_crlf_from_lf(self):
        payload = 'alpha\r\nbeta\r\n'
        (self.work/'answer.txt').write_bytes(payload.encode('utf-8'))
        harness = LocalHarness(self.work)
        harness.start(contract())
        exact = Check(id='crlf', kind='text_equals', path='answer.txt', expected=payload)
        different = Check(id='lf', kind='text_equals', path='answer.txt', expected=payload.replace('\r\n','\n'))
        self.assertEqual(harness.check(exact,1).exit_code,0)
        self.assertEqual(harness.check(different,1).exit_code,1)

    def test_file_observation_preserves_newline_content(self):
        payload = 'alpha\r\nbeta\rbeta\n'
        (self.work/'answer.txt').write_bytes(payload.encode('utf-8'))
        harness = LocalHarness(self.work)
        harness.start(contract())
        result = harness.execute(Action(kind='read', path='answer.txt'),1)
        self.assertEqual(result.output,payload)

    def test_failed_acceptance_output_reaches_context_with_its_state_version(self):
        task = TaskContract(task_id='stale-report', goal='Regenerate the report', acceptance_checks=(
            Check(id='functional', kind='command',
                  command=python_command("raise AssertionError('stale result.json')")),))
        harness = LocalHarness(self.work)
        harness.start(task)
        evaluation = Evaluator(harness).assess(task, 5)
        view = GraphView(snapshot=evaluation.snapshot, verdict=evaluation.verdict)
        compiler = ContextCompiler(harness.describe())
        request = compiler.build(task, view, evaluation.snapshot, 1, 60_000)
        self.assertIn('FAILED ACCEPTANCE functional', request.context)
        self.assertIn('AssertionError: stale result.json', request.context)
        self.assertLessEqual(request.input_token_bound, task.budget.context_tokens)
        harness.execute(Action(kind='write', path='result.json', content='{}'), 1)
        changed = compiler.build(task, view, harness.snapshot(), 2, 60_000)
        self.assertNotIn('FAILED ACCEPTANCE functional', changed.context,
                         'old evidence must not be presented as a current failure')

    def test_repeated_cleanup_failure_still_returns_and_reports_result(self):
        class BrokenClose(LocalHarness):
            def close(self):
                raise HarnessError('simulated cleanup timeout for an owned environment')
        with patch('horizon_weaver.app.LocalHarness', BrokenClose):
            result = self.run_actions((Action(kind='write', path='answer.txt', content='42\n'),))
        self.assertEqual(result.termination, Termination.HARNESS_ERROR)
        self.assertIn('cleanup timeout', result.reason)
        self.assertEqual(json.loads((self.run_dir/'result.json').read_text())['termination'], 'harness_error')
        self.assertEqual(read_run(self.run_dir).result, result)

    def test_json_assertion_distinguishes_booleans_and_rejects_ambiguous_json(self):
        task = TaskContract(task_id="json", goal="numeric answer", acceptance_checks=(
            Check(id="json", kind="json_equals", path="result.json", expected='{"answer":1}'),))
        harness = LocalHarness(self.work)
        harness.start(task)
        for content in ('{"answer":true}', '{"answer":NaN}', '{"answer":0,"answer":1}'):
            (self.work / "result.json").write_text(content)
            self.assertFalse(Evaluator(harness).assess(task, 5).verdict.passed)
        (self.work / "result.json").write_text('{"answer":1.0}')
        self.assertTrue(Evaluator(harness).assess(task, 5).verdict.passed)
        harness.close()

    def test_write_preserves_payload_and_passes_external_contract(self):
        result = self.run_actions((Action(kind="write", path="answer.txt", content="42\n"),))
        self.assertEqual(result.termination, Termination.SUCCESS)
        self.assertEqual((self.work / "answer.txt").read_bytes(), b"42\n")
        self.assertTrue(result.verdict.passed)
        replay = read_run(self.run_dir)
        self.assertEqual(replay.result, result)
        self.assertEqual(len(replay.attempts), 1)
        self.assertFalse(replay.pending)
        self.assertEqual(json.loads((self.run_dir / "result.json").read_text())["termination"], "success")

    def test_done_and_command_exit_zero_cannot_grant_success(self):
        result = self.run_actions((Action(kind="command", command=python_command("print('ok')")),
                                   Action(kind="done")), contract(no_progress_limit=2, max_steps=10))
        self.assertEqual(result.termination, Termination.NO_PROGRESS)
        self.assertFalse(result.verdict.passed)
        self.assertIn("answer", result.verdict.unsatisfied_requirements)

    def test_failed_check_can_be_repeated_after_change(self):
        harness = LocalHarness(self.work)
        task = contract()
        harness.start(task)
        evaluator = Evaluator(harness)
        self.assertFalse(evaluator.assess(task, 5).verdict.passed)
        harness.execute(Action(kind="write", path="answer.txt", content="42\n"), 5)
        self.assertTrue(evaluator.assess(task, 5).verdict.passed)
        harness.close()

    def test_evidence_is_invalidated_by_later_write(self):
        harness = LocalHarness(self.work)
        task = contract()
        harness.start(task)
        harness.execute(Action(kind="write", path="answer.txt", content="42\n"), 5)
        verdict = Evaluator(harness).assess(task, 5).verdict
        harness.execute(Action(kind="write", path="answer.txt", content="wrong\n"), 5)
        invalidated = current_verdict(task, verdict, harness.snapshot())
        self.assertFalse(invalidated.passed)
        self.assertEqual(invalidated.evidence, ())
        harness.close()

    def test_command_check_that_mutates_workspace_fails(self):
        task = TaskContract(task_id="check", goal="check without mutation", acceptance_checks=(
            Check(id="check", kind="command", command=python_command("open('changed.txt','w').write('x')")),))
        harness = LocalHarness(self.work)
        harness.start(task)
        evaluation = Evaluator(harness).assess(task, 5)
        self.assertFalse(evaluation.verdict.passed)
        self.assertEqual(evaluation.verdict.evidence[0].result.error_type, "check_mutated_workspace")
        harness.close()

    def test_scoped_paths_and_symlinks_cannot_escape(self):
        (self.work / "allowed").mkdir()
        (self.work / "private.txt").write_text("private")
        task = contract().model_copy(update={"allowed_paths": ("allowed",)})
        harness = LocalHarness(self.work)
        harness.start(task)
        result = harness.execute(Action(kind="read", path="private.txt"), 5)
        self.assertEqual(result.error_type, "PermissionError")
        self.assertNotIn("private", result.output.replace("private.txt", ""))
        self.assertIsNone(harness.snapshot().file("private.txt"))
        outside = self.root / "outside.txt"
        outside.write_text("secret")
        try:
            (self.work / "allowed" / "link").symlink_to(outside)
        except OSError:
            harness.close()
            return  # Windows without symlink privilege still tests the scope boundary.
        result = harness.execute(Action(kind="read", path="allowed/link"), 5)
        self.assertEqual(result.error_type, "PermissionError")
        self.assertNotIn("secret", result.output)
        harness.close()


class ControlTests(WorkspaceCase):
    def test_uncertain_initial_acceptance_stops_before_model_or_write(self):
        class UnknownAcceptance(LocalHarness):
            def check(self, check, timeout_s):
                return ToolResult(error_type='timeout', outcome_unknown=True)
        policy = ScriptedPolicy((Action(kind='write', path='answer.txt', content='42\n'),))
        with patch('horizon_weaver.app.LocalHarness', UnknownAcceptance), \
                patch.object(policy, 'decide', wraps=policy.decide) as decide:
            result = NodeAgent(backend='local', policy=policy).run(contract(), self.work, self.run_dir)
            decide.assert_not_called()
        self.assertEqual(result.termination, Termination.HARNESS_ERROR)
        self.assertEqual(result.steps, 0)
        self.assertFalse((self.work/'answer.txt').exists())
        self.assertEqual(read_run(self.run_dir).result, result)

    def test_repeated_read_and_fully_visible_current_write_are_redundant(self):
        (self.work / "note.txt").write_text("first")
        result = self.run_actions((Action(kind="read", path="note.txt"),
            Action(kind="read", path="note.txt"),
            Action(kind="write", path="note.txt", content="second"),
            Action(kind="read", path="note.txt")), contract(max_steps=4))
        attempts = read_run(self.run_dir).attempts
        self.assertEqual(attempts[1].progress.level, "none")
        self.assertEqual(attempts[3].progress.level, "none")
        self.assertEqual(attempts[3].result.error_type, "redundant_action")
        self.assertFalse(attempts[3].tool_executed)
        self.assertEqual(result.termination, Termination.MAX_STEPS)

    def test_repeated_state_cycle_is_no_progress(self):
        result = self.run_actions(tuple(Action(kind="write", path="note.txt", content=value)
            for value in ("a", "b", "a", "b", "a")), contract(no_progress_limit=2))
        self.assertEqual(result.termination, Termination.NO_PROGRESS)
        self.assertEqual(read_run(self.run_dir).attempts[-1].progress.reason_code, "state_cycle")

    def test_context_budget_stops_before_effectful_action(self):
        result = self.run_actions((Action(kind="write", path="answer.txt", content="42\n"),),
                                  contract(max_model_tokens=10))
        self.assertEqual(result.termination, Termination.BUDGET_EXHAUSTED)
        self.assertFalse((self.work / "answer.txt").exists())

    def test_provider_usage_over_budget_stops_before_tool_execution(self):
        class OverspendingPolicy:
            def describe(self): return {"kind": "overspending"}
            def decide(self, request, timeout_s):
                return Decision(action=Action(kind="write", path="answer.txt", content="42\n"),
                                usage=Usage(input_tokens=100_001))
        result = NodeAgent(backend="local", policy=OverspendingPolicy()).run(contract(), self.work, self.run_dir)
        self.assertEqual(result.termination, Termination.BUDGET_EXHAUSTED)
        self.assertFalse((self.work / "answer.txt").exists())

    def test_harness_start_failure_is_persisted(self):
        (self.work / "allowed").write_text("not a directory")
        task = TaskContract(task_id="start", goal="write answer", allowed_paths=("allowed",),
                            acceptance_checks=(Check(id="answer", kind="text_equals", path="allowed/answer.txt", expected="42"),))
        result = self.run_actions((), task)
        self.assertEqual(result.termination, Termination.HARNESS_ERROR)
        self.assertEqual(read_run(self.run_dir).result, result)

    def test_tool_timeout_kills_child_and_records_uncertainty(self):
        command = python_command("import time; time.sleep(2); open('late.txt','w').write('late')")
        result = self.run_actions((Action(kind="command", command=command),),
                                  contract(tool_timeout_s=.1, wall_time_s=10))
        self.assertEqual(result.termination, Termination.TIMEOUT)
        time.sleep(.2)
        self.assertFalse((self.work / "late.txt").exists())
        self.assertTrue(read_run(self.run_dir).attempts[0].result.outcome_unknown)

    def test_model_errors_get_structured_results(self):
        class BrokenPolicy:
            def describe(self): return {"kind": "broken"}
            def decide(self, request, timeout_s): raise RuntimeError("model unavailable")
        result = NodeAgent(backend="local", policy=BrokenPolicy()).run(contract(), self.work, self.run_dir)
        self.assertEqual(result.termination, Termination.MODEL_ERROR)
        self.assertTrue(result.usage.estimated)
        self.assertGreater(result.usage.total, 0)
        self.assertEqual(read_run(self.run_dir).result, result)

    def test_invalid_model_output_is_bounded_and_charged(self):
        class InvalidPolicy:
            def describe(self): return {"kind": "invalid"}
            def decide(self, request, timeout_s):
                return Decision(error="invalid JSON", raw="blah", usage=Usage(input_tokens=7, output_tokens=3))
        result = NodeAgent(backend="local", policy=InvalidPolicy()).run(
            contract(no_progress_limit=2), self.work, self.run_dir)
        self.assertEqual(result.termination, Termination.NO_PROGRESS)
        self.assertEqual(result.usage.total, 20)


class RecoveryTests(WorkspaceCase):
    def interrupted_run(self, *, delay=0, **budget):
        (self.work / "note.txt").write_text("note")
        actions = (Action(kind="read", path="note.txt"),
                   Action(kind="write", path="answer.txt", content="42\n"))
        class InterruptPolicy(ScriptedPolicy):
            def decide(self, request, timeout_s):
                if request.step == 2:
                    time.sleep(delay)
                    raise KeyboardInterrupt
                return super().decide(request, timeout_s)
        result = NodeAgent(backend="local", policy=InterruptPolicy(actions)).run(
            contract(**budget), self.work, self.run_dir)
        self.assertEqual(result.termination, Termination.INTERRUPTED)
        return actions

    def test_committed_state_resumes_without_reexecuting_completed_steps(self):
        actions = self.interrupted_run()
        result = NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(
            contract(), self.work, self.run_dir, resume=True)
        self.assertEqual(result.termination, Termination.SUCCESS)
        graph = read_run(self.run_dir)
        self.assertEqual([item.step for item in graph.attempts], [1, 2])
        self.assertFalse(graph.pending)
        self.assertGreater(graph.usage.total, 0)  # Interrupted request reservation was retained.

    def test_resume_does_not_reset_elapsed_budget(self):
        actions = self.interrupted_run(delay=.15, wall_time_s=.1)
        result = NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(
            contract(wall_time_s=.1), self.work, self.run_dir, resume=True)
        self.assertEqual(result.termination, Termination.TIMEOUT)
        self.assertFalse((self.work / "answer.txt").exists())

    def test_external_workspace_change_blocks_resume(self):
        actions = self.interrupted_run()
        (self.work / "note.txt").write_text("external change")
        result = NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(
            contract(), self.work, self.run_dir, resume=True)
        self.assertEqual(result.termination, Termination.RECOVERY_BLOCKED)

    def test_pending_effectful_action_is_never_blindly_replayed(self):
        actions = self.interrupted_run()
        graph = read_run(self.run_dir)
        store = EventStore(self.run_dir / "events.sqlite3", graph.manifest.run_id)
        try:
            store.append("ActionProposed", {"step": 2, "action": actions[1].model_dump(mode="json")})
        finally:
            store.close()
        result = NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(
            contract(), self.work, self.run_dir, resume=True)
        self.assertEqual(result.termination, Termination.RECOVERY_BLOCKED)
        self.assertFalse((self.work / "answer.txt").exists())

    def test_changed_contract_and_corrupt_event_are_rejected(self):
        actions = self.interrupted_run()
        changed = contract().model_copy(update={"goal": "different"})
        with self.assertRaises(ValueError):
            NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(changed, self.work, self.run_dir, resume=True)
        with closing(sqlite3.connect(self.run_dir / "events.sqlite3")) as connection:
            row = connection.execute("SELECT event FROM events WHERE sequence=1").fetchone()
            event = json.loads(row[0])
            event["timestamp"] = "tampered"
            connection.execute("UPDATE events SET event=? WHERE sequence=1", (json.dumps(event),))
            connection.commit()
        with self.assertRaises(StoreError):
            read_run(self.run_dir)

    def test_replay_never_calls_a_tool_or_policy(self):
        self.run_actions((Action(kind="write", path="answer.txt", content="42\n"),))
        before = (self.work / "answer.txt").read_bytes()
        with patch.object(LocalHarness, "execute", side_effect=AssertionError("execution during replay")), \
             patch.object(ScriptedPolicy, "decide", side_effect=AssertionError("model during replay")):
            first, second = read_run(self.run_dir), read_run(self.run_dir)
            self.assertEqual(first.view, second.view)
            self.assertEqual(first.result, second.result)
        self.assertEqual((self.work / "answer.txt").read_bytes(), before)

    def test_incomplete_acceptance_is_not_reexecuted_on_resume(self):
        actions = self.interrupted_run()
        graph = read_run(self.run_dir)
        store = EventStore(self.run_dir / "events.sqlite3", graph.manifest.run_id)
        try:
            store.append("EvaluationStarted", {"step": 2})
        finally:
            store.close()
        result = NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(
            contract(), self.work, self.run_dir, resume=True)
        self.assertEqual(result.termination, Termination.RECOVERY_BLOCKED)

    def test_atomic_event_batch_rolls_back_on_serialization_failure(self):
        self.interrupted_run()
        graph = read_run(self.run_dir)
        store = EventStore(self.run_dir / "events.sqlite3", graph.manifest.run_id)
        try:
            before = store.read()
            with self.assertRaises(TypeError):
                store.append_many([("RunResumed", {}), ("RunResumed", object())])
            self.assertEqual(store.read(), before)
        finally:
            store.close()

    def test_terminal_run_cannot_be_mistaken_for_fresh_acceptance(self):
        actions = (Action(kind="write", path="answer.txt", content="42\n"),)
        self.run_actions(actions)
        (self.work / "answer.txt").write_text("changed")
        with self.assertRaises(ValueError):
            NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(
                contract(), self.work, self.run_dir, resume=True)

    def test_resume_rechecks_acceptance_even_when_workspace_hash_matches(self):
        task = TaskContract(task_id="external", goal="assert environment condition",
            acceptance_checks=(Check(id="condition", kind="command", command=python_command("pass")),),
            budget=Budget(no_progress_limit=2))
        self.run_actions((), task)
        # Simulate a crash after the passing checkpoint, before final termination.
        with closing(sqlite3.connect(self.run_dir / "events.sqlite3")) as connection:
            connection.execute("DELETE FROM events WHERE sequence=(SELECT MAX(sequence) FROM events)")
            connection.commit()
        with patch.object(LocalHarness, "check", return_value=ToolResult(exit_code=1, output="condition changed")):
            result = NodeAgent(backend="local", policy=ScriptedPolicy(())).run(
                task, self.work, self.run_dir, resume=True)
        self.assertEqual(result.termination, Termination.NO_PROGRESS)
        self.assertFalse(result.verdict.passed)


class GatewayTests(unittest.TestCase):
    def request(self):
        return DecisionInput(step=1, system="system", context="context", input_token_bound=100, max_output_tokens=20)

    def test_request_protocol_usage_and_reasoning_fallback(self):
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "", "reasoning_content": '{"kind":"done"}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        gateway = ModelGateway("test", client=httpx.Client(transport=httpx.MockTransport(respond)))
        try:
            decision = ModelPolicy(gateway).decide(self.request(), 2)
            self.assertEqual(decision.action.kind.value, "done")
            self.assertEqual(decision.usage.total, 10)
            self.assertEqual(captured[0]["max_tokens"], 20)
            self.assertEqual([item["role"] for item in captured[0]["messages"]], ["system", "user"])
        finally:
            gateway.close()

    def test_missing_usage_and_network_failure_are_conservatively_accounted(self):
        def fail(request): raise httpx.ReadError("disconnected")
        gateway = ModelGateway("test", client=httpx.Client(transport=httpx.MockTransport(fail)))
        try:
            with self.assertRaises(GatewayError) as caught:
                gateway.complete(self.request(), 1)
            self.assertEqual(caught.exception.usage.total, 120)
            self.assertTrue(caught.exception.usage.estimated)
        finally:
            gateway.close()

    def test_truncated_model_output_gives_feedback_and_no_tool_action(self):
        def respond(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"write","content":"'},
                "finish_reason": "length"}], "usage": {"prompt_tokens": 7, "completion_tokens": 20}})
        gateway = ModelGateway("test", client=httpx.Client(transport=httpx.MockTransport(respond)))
        try:
            decision = ModelPolicy(gateway).decide(self.request(), 2)
            self.assertIsNone(decision.action)
            self.assertIn("truncated", decision.error)
            self.assertIn("shorter", decision.error)
            self.assertEqual(decision.usage.total, 27)
        finally:
            gateway.close()

    def test_decoder_schema_does_not_replace_runtime_validation(self):
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {
                "content": '{"kind":"write","path":"../escape","content":"x"}'}}]})
        gateway = ModelGateway("test", structured_output="json_schema",
                               client=httpx.Client(transport=httpx.MockTransport(respond)))
        try:
            decision = ModelPolicy(gateway).decide(self.request(), 2)
            self.assertIsNone(decision.action)
            self.assertIn("parent traversal", decision.error)
            schema = captured[0]["response_format"]["json_schema"]["schema"]
            self.assertEqual({variant["properties"]["kind"]["const"] for variant in schema["anyOf"]
                              if "kind" in variant["properties"]},
                             {"read", "write", "command", "verify", "done", "history", "incomplete"})
            self.assertTrue(all(variant["additionalProperties"] is False for variant in schema["anyOf"]))
            self.assertIn("notes", schema["anyOf"][-1]["properties"])
        finally:
            gateway.close()

    def test_real_http_worker_and_total_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"choices": [{"message": {"content": '{"kind":"done"}'}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        gateway = ModelGateway("test", f"http://127.0.0.1:{server.server_port}/v1")
        try:
            response = gateway.complete(self.request(), 10)
            self.assertEqual(response.usage.total, 6)
            # The caller's working directory may contain a different package
            # with the same name. The worker must execute its own runtime.
            with tempfile.TemporaryDirectory() as shadow:
                fake = Path(shadow) / "horizon_weaver"
                fake.mkdir()
                (fake / "__init__.py").write_text("raise RuntimeError('wrong gateway package')")
                previous_cwd = Path.cwd()
                try:
                    os.chdir(shadow)
                    response = gateway.complete(self.request(), 10)
                    self.assertEqual(response.usage.total, 6)
                finally:
                    os.chdir(previous_cwd)
            started = time.monotonic()
            with self.assertRaises(GatewayError):
                gateway.complete(self.request(), .001)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            gateway.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ArchitectureTests(unittest.TestCase):
    def test_new_runtime_has_no_legacy_or_reverse_imports(self):
        root = Path(__file__).resolve().parents[1] / "horizon_weaver"
        forbidden = {"core": {"harness", "control", "state", "policy", "app"},
                     "state": {"harness", "control", "policy", "app"},
                     "harness": {"control", "policy", "state", "app"},
                     "policy": {"harness", "control", "state", "app"},
                     "control": {"harness", "policy", "context", "evaluation", "app"},
                     "context": {"harness", "policy", "control", "state", "app"}}
        for file in root.glob("*.py"):
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(module == "agent" or module.startswith("agent."))
                    self.assertNotIn(module, forbidden.get(file.stem, set()))


if __name__ == "__main__":
    unittest.main()
