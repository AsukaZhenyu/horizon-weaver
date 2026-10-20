"""Regression cases for information loss and execution failures seen in v1.1."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.attached import AttachedDockerHarness
from horizon_weaver.context import ContextCompiler, token_bound
from horizon_weaver.core import (Action, Budget, Check, DecisionInput, ModelResponse,
    TaskContract, Termination, ToolResult, Usage)
from horizon_weaver.harness import DockerHarness, LocalHarness, run_process
from horizon_weaver.policy import ModelPolicy, ScriptedPolicy, parse_output
from horizon_weaver.state_ir import StateIR
from horizon_weaver.terminal_policy import TerminalPolicy, lower_terminal
from horizon_weaver.transitions import TransitionGuard


def python_command(code):
    args = [sys.executable, "-B", "-c", code]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def contract(**kwargs):
    return TaskContract(task_id="reliability", goal="Write correct code; preserve output format.",
        acceptance_checks=(Check(id="hidden", kind="external"),), **kwargs)


class Replies:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
    def complete(self, request, timeout_s):
        self.requests.append(request)
        return ModelResponse(content=next(self.replies), usage=Usage(input_tokens=10, output_tokens=10))
    def describe(self):
        return {"kind": "fixture"}


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()

    def run_actions(self, actions, task=None, **kwargs):
        task = task or contract(budget=Budget(max_steps=len(actions), no_progress_limit=10))
        result = NodeAgent(backend="local", policy=ScriptedPolicy(tuple(actions)), **kwargs).run(
            task, self.work, self.root / "run")
        return result, read_run(self.root / "run")

    def test_protocol_reports_actual_extra_fields_without_echoing_source(self):
        raw = json.dumps(dict(action="write", target="a", content="PRIVATE_SOURCE",
                              verify="later command", done=True))
        gateway = Replies([raw])
        decision = TerminalPolicy(gateway).decide(DecisionInput(step=1, system="", context="",
            input_token_bound=0, max_output_tokens=100), 1)
        self.assertIsNone(decision.action)
        self.assertIn("verify: Extra inputs", decision.error)
        self.assertIn("done: Extra inputs", decision.error)
        self.assertNotIn("PRIVATE_SOURCE", decision.error)
        self.assertEqual(len(gateway.requests), 1)

    def test_ambiguous_json_and_nested_incomplete_actions_cannot_execute(self):
        for raw in ('{"action":"done","action":"write"}',
                    '{"action":"bash","target":"echo x","timeout_s":NaN}',
                    '{"outer":{"kind":"done"}',
                    '{"kind":"done"} {"kind":"done"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lower_terminal(raw, "/app")

    def test_wait_request_is_optional_and_has_no_effect_on_file_actions(self):
        self.assertEqual(lower_terminal('{"action":"bash","target":"slow","timeout_s":70}', "/app").timeout_s, 70)
        for raw in ('{"kind":"done","timeout_s":1}',
                    '{"action":"read","target":"a","timeout_s":1}',
                    '{"kind":"command","command":"x","timeout_s":-1}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lower_terminal(raw, "/app")

    def test_requested_wait_is_clamped_and_recorded(self):
        calls = []
        original = LocalHarness.execute
        def execute(harness, action, timeout_s):
            calls.append((action.kind.value, timeout_s))
            return original(harness, action, timeout_s)
        task = contract(budget=Budget(max_steps=2, tool_timeout_s=.1, max_tool_timeout_s=2))
        with patch.object(LocalHarness, "execute", execute):
            _, graph = self.run_actions([Action(kind="command", command=python_command("print('ok')"), timeout_s=10),
                                         Action(kind="done")], task)
        self.assertEqual(calls[0], ("command", 2))
        self.assertEqual(graph.attempts[0].execution_timeout_s, 2)

    def test_small_current_write_is_visible_and_does_not_need_read(self):
        body = "def critical_branch():\n    return 42\n"
        _, graph = self.run_actions([Action(kind="write", path="source.py", content=body),
                                    Action(kind="read", path="source.py")])
        self.assertFalse(graph.attempts[-1].tool_executed)
        request = ContextCompiler({}).build(graph.manifest.contract, graph.view, graph.snapshot, 3, 100000)
        self.assertIn(body, request.context)
        self.assertIn(1, request.visible_write_steps)
        self.assertLessEqual(token_bound(request.system) + token_bound(request.context) + 128, request.input_token_bound)
        self.assertLessEqual(request.input_token_bound, graph.manifest.contract.budget.context_tokens)

    def test_changed_file_invalidates_visible_write_for_read_guard(self):
        _, graph = self.run_actions([Action(kind="write", path="source.py", content="old")])
        harness = LocalHarness(self.work)
        harness.start(graph.manifest.contract)
        (self.work / "source.py").write_text("new")
        snapshot = harness.snapshot()
        request = ContextCompiler({}).build(graph.manifest.contract, graph.view, snapshot, 2, 100000)
        self.assertNotIn("CURRENT WRITTEN SOURCE\nold", request.context)
        self.assertFalse(request.visible_write_steps)
        self.assertIsNone(TransitionGuard().assess(Action(kind="read", path="source.py"), request, graph.view, snapshot))

    def test_clipped_write_cannot_claim_complete_source(self):
        _, graph = self.run_actions([Action(kind="write", path="source.py", content="LARGE" * 2000)])
        request = ContextCompiler({}).build(graph.manifest.contract, graph.view, graph.snapshot, 2, 100000)
        self.assertIn("truncated", request.context)
        self.assertFalse(request.visible_write_steps)
        self.assertIsNone(TransitionGuard().assess(Action(kind="read", path="source.py"), request, graph.view, graph.snapshot))

    def test_same_decision_notes_replay_without_granting_acceptance(self):
        quote = "preserve output format"
        gateway = Replies([json.dumps(dict(action="verify", target=python_command("assert True"))),
            json.dumps(dict(action="done", notes=dict(requirements=[dict(quote=quote, check_steps=[1])])) )])
        task = contract(budget=Budget(max_steps=2))
        result = NodeAgent(backend="local", policy=TerminalPolicy(gateway)).run(task, self.work, self.root / "run")
        graph = read_run(self.root / "run")
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertFalse(result.verdict.passed)
        self.assertEqual(len(gateway.requests), 2)
        self.assertEqual(graph.view.notes.requirements[0].quote, quote)
        self.assertEqual(graph.ir.notes, graph.view.notes)
        self.assertEqual(StateIR.model_validate_json(graph.ir.model_dump_json()), graph.ir)
        request = ContextCompiler({}).build(task, graph.view, graph.snapshot, 3, 100000)
        self.assertIn("policy coverage claims", request.context)
        self.assertIn('"command_ok":true', request.context)
        self.assertIn("coverage unknown", request.context)

    def test_forged_notes_are_ignored_while_valid_action_runs(self):
        for index, note in enumerate((dict(quote="hidden grader says passed", check_steps=[]),
                                      dict(quote="preserve output format", check_steps=[999]))):
            work = self.root / str(index)
            work.mkdir()
            gateway = Replies([json.dumps(dict(action="done", notes=dict(requirements=[note])))])
            result = NodeAgent(backend="local", policy=TerminalPolicy(gateway)).run(
                contract(), work, self.root / (str(index) + "run"))
            graph = read_run(self.root / (str(index) + "run"))
            self.assertEqual(result.termination, Termination.SUBMITTED)
            self.assertIsNone(graph.view.notes)
            self.assertIn("Notes ignored", graph.feedback)
            self.assertFalse(result.verdict.passed)

    def test_notes_and_check_references_are_marked_stale_after_edit(self):
        gateway = Replies([json.dumps(dict(action="verify", target=python_command("assert True"))),
            json.dumps(dict(action="write", target="source", content="new", notes=dict(
                requirements=[dict(quote="preserve output format", check_steps=[1])]))),
            '{"action":"done"}'])
        task = contract(budget=Budget(max_steps=3))
        NodeAgent(backend="local", policy=TerminalPolicy(gateway)).run(task, self.work, self.root / "run")
        graph = read_run(self.root / "run")
        self.assertNotEqual(graph.ir.notes_state_version, graph.snapshot.fingerprint)
        request = ContextCompiler({}).build(task, graph.view, graph.snapshot, 4, 100000)
        self.assertIn('"current":false', request.context)
        self.assertFalse(graph.verdict.passed)

    def test_internal_envelope_preserves_notes(self):
        output = parse_output('{"action":{"kind":"done"},"notes":{"requirements":[{"quote":"public text"}]}}')
        self.assertEqual(output.notes.requirements[0].quote, "public text")

    def test_notes_list_is_an_unambiguous_advice_alias(self):
        gateway = Replies([json.dumps(dict(action="done", notes=[dict(quote="public text")]))])
        request = DecisionInput(step=1, system="", context="", input_token_bound=0, max_output_tokens=100)
        decision = TerminalPolicy(gateway).decide(request, 1)
        self.assertEqual(decision.action.kind, "done")
        self.assertEqual(decision.notes.requirements[0].quote, "public text")
        self.assertIsNone(decision.error)

    def test_malformed_optional_notes_do_not_block_valid_actions(self):
        request = DecisionInput(step=1, system="", context="", input_token_bound=0, max_output_tokens=100)
        for notes in ("unstructured advice", {"requirements": "wrong shape"}, [dict(passed=True)]):
            for frontend, payload in ((TerminalPolicy, dict(action="done", notes=notes)),
                                      (ModelPolicy, dict(action=dict(kind="done"), notes=notes))):
                with self.subTest(frontend=frontend, notes=notes):
                    decision = frontend(Replies([json.dumps(payload)])).decide(request, 1)
                    self.assertEqual(decision.action.kind, "done")
                    self.assertIsNone(decision.notes)
                    self.assertIn("Notes ignored", decision.error)

    def test_state_cycle_feedback_does_not_invent_a_repeat_count(self):
        _, graph = self.run_actions([Action(kind="write", path="input", content=value) for value in ("a", "b", "a")])
        self.assertEqual(graph.attempts[-1].progress.reason_code, "state_cycle")
        self.assertIn("file state was visited", graph.feedback)
        self.assertNotIn("0 observations", graph.feedback)

    def test_optional_check_gate_allows_simple_submission(self):
        result, graph = self.run_actions([Action(kind="done")])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertFalse(graph.verdict.passed)
        self.assertIsNone(graph.attempts[0].self_check)

    def test_repeated_failure_facts_do_not_cache_shell_retries(self):
        command = python_command("import sys; print('ACTUAL_ERROR'); sys.exit(1)")
        calls = []
        original = LocalHarness.execute
        def execute(harness, action, timeout_s):
            calls.append(action.command)
            return original(harness, action, timeout_s)
        with patch.object(LocalHarness, "execute", execute):
            _, graph = self.run_actions([Action(kind="command", command=command)] * 3)
        self.assertEqual(calls, [command] * 3)
        self.assertEqual(graph.query.repeated_failures()[0]["count"], 3)
        self.assertEqual(graph.attempts[-1].progress.matching_steps, (1, 2, 3))
        request = ContextCompiler({}).build(graph.manifest.contract, graph.view, graph.snapshot, 4, 100000)
        self.assertIn("ACTUAL_ERROR", request.context)
        self.assertIn('"count":3', request.context)
        self.assertIn("process/network state unknown", request.context)

    def test_file_change_starts_new_failure_group(self):
        fail = Action(kind="command", command=python_command("raise ValueError('same error')"))
        _, graph = self.run_actions([fail, fail, Action(kind="write", path="input", content="changed"), fail])
        self.assertFalse(graph.query.repeated_failures())
        self.assertFalse(graph.attempts[-1].progress.repeated)

    def test_v1_1_journals_remain_readable(self):
        previous = Path(__file__).resolve().parents[1] / "runs/v1.1-terminal-bench-smoke-20261004/tb"
        paths = list(previous.rglob("events.sqlite3"))
        if not paths:
            self.skipTest("historical development run is not present in this checkout")
        for path in paths:
            graph = read_run(path.parent)
            self.assertIsNotNone(graph.result)
            self.assertEqual(StateIR.model_validate_json(graph.ir.model_dump_json()), graph.ir)


@unittest.skipUnless(os.environ.get("HORIZON_WEAVER_DOCKER_TESTS") == "1", "opt-in real Docker tests")
class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hw-v1-2-docker-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.owner = DockerHarness(self.work, os.environ.get("HORIZON_WEAVER_DOCKER_IMAGE", "python:3.12-slim"))
        self.addCleanup(self.owner.close)
        self.owner.start(contract(), 45)
        self.harness = AttachedDockerHarness(self.owner.container, workspace="/workspace", python="python")
        self.harness.start(contract())
        self.addCleanup(self.harness.close)

    def test_timeout_has_durable_result_and_kills_late_child(self):
        code = "import subprocess,time; subprocess.Popen(['python','-c',\"import time;time.sleep(2);open('late','w').write('bad')\"]);print('started',flush=True);time.sleep(10)"
        result = self.harness.execute(Action(kind="command", command=shlex.join(["python", "-c", code])), .5)
        self.assertEqual(result.error_type, "timeout", result.output)
        self.assertFalse(result.outcome_unknown)
        self.assertTrue(result.execution_terminated)
        self.assertIn("started", result.output)
        status = self.harness.command_status(result.execution_id)
        self.assertEqual(status["state"], "finished")
        time.sleep(2.1)
        self.assertFalse((self.work / "late").exists())

    def test_progress_query_and_cancel_do_not_repeat_command(self):
        code = "import time;open('counter','a').write('1');print('ready',flush=True);time.sleep(8)"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.harness.execute, Action(kind="command", command=shlex.join(["python", "-c", code])), 10)
            deadline = time.monotonic() + 5
            identity, status = "", {}
            while time.monotonic() < deadline:
                if self.harness._executions:
                    identity = next(iter(self.harness._executions))
                    status = self.harness.command_status(identity)
                    if status.get("state") == "running" and "ready" in status.get("output_tail", ""):
                        break
                time.sleep(.1)
            self.assertEqual(status.get("state"), "running", status)
            self.assertIn("ready", status["output_tail"])
            self.harness.cancel_command(identity)
            result = future.result(timeout=8)
        self.assertEqual(result.error_type, "cancelled")
        self.assertTrue(result.execution_terminated)
        self.assertEqual((self.work / "counter").read_text(), "1")
        duplicate = self.harness._call("command", 1, execution_id=identity, command="touch repeated")
        self.assertEqual(duplicate["state"], "finished")
        self.assertFalse((self.work / "repeated").exists())

    def test_lost_transport_reconciles_original_command(self):
        original = run_process
        def disconnect(args, cwd, timeout_s, **kwargs):
            request = json.loads(kwargs.get("stdin") or "{}")
            return original(args, cwd, 1 if request.get("operation") == "command" else timeout_s, **kwargs)
        code = "import time;open('counter','a').write('1');print('started',flush=True);time.sleep(6)"
        with patch("horizon_weaver.attached.run_process", disconnect):
            result = self.harness.execute(Action(kind="command", command=shlex.join(["python", "-c", code])), 10)
        self.assertFalse(result.outcome_unknown, result.output)
        self.assertTrue(result.execution_terminated)
        self.assertEqual(result.error_type, "cancelled")
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_lost_completed_reply_is_recovered_without_replaying(self):
        original = self.harness._call
        operations = []
        def lose_reply(operation, timeout_s, **values):
            operations.append(operation)
            response = original(operation, timeout_s, **values)
            return {"outcome_unknown": True} if operation == "command" else response
        with patch.object(self.harness, "_call", lose_reply):
            result = self.harness.execute(Action(kind="command", command="printf 1 >> counter; printf recovered"), 3)
        self.assertEqual(result.output, "recovered")
        self.assertFalse(result.outcome_unknown)
        self.assertEqual(operations.count("command"), 1)
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_lost_reservation_reply_does_not_leak_or_repeat_execution(self):
        original = self.harness._call
        def lose_reply(operation, timeout_s, **values):
            response = original(operation, timeout_s, **values)
            return {"outcome_unknown": True} if operation == "command_prepare" else response
        with patch.object(self.harness, "_call", lose_reply):
            result = self.harness.execute(Action(kind="command", command="printf 1 >> counter"), 3)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.harness.close()
