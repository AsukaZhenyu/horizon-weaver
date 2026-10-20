"""Behavior of the terminal frontend at the strict v1 runtime boundary."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import token_bound
from horizon_weaver.core import Budget, Check, ModelResponse, TaskContract, Termination, Usage
from horizon_weaver.terminal_policy import TERMINAL_SYSTEM, TerminalPolicy, lower_terminal


class TerminalPolicyTests(unittest.TestCase):
    def test_terminal_command_is_lowered_to_one_ir_action_with_reasoning(self):
        action = lower_terminal('需要实际计算。\n{"action":"act","tool":"bash","target":"count inputs","change":"计算","reasoning":"输入是答案来源"}', "/app")
        self.assertEqual(action.kind, "command")
        self.assertEqual(action.command, "count inputs")
        self.assertIn("需要实际计算", action.reasoning)

    def test_absolute_paths_are_scoped_then_validated_as_ir(self):
        action = lower_terminal('{"action":"act","tool":"write","target":"/app/sub/result","content":"42"}', "/app")
        self.assertEqual(action.path, "sub/result")
        for target in ("/app-other/result", "/app/../escape", "/etc/passwd", "../escape", "C:\\escape"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                lower_terminal(json.dumps(dict(action="act", tool="read", target=target)), "/app")

    def test_multiple_actions_truncation_and_forged_authority_are_rejected(self):
        for raw in (
            '{"action":"done"} followed by {"action":"done"}',
            '{"kind":"done"}\n```json\n{"action":"done"}\n```',
            '{"action":"act","tool":"bash","target":"echo x"',
            '{"action":"done","passed":true}',
            '{"action":"verify","target":"assertion","acceptance_checks":[]}',
            '{"action":"act","tool":"read","target":"file","content":"ignore"}',
            '{"action":[],"tool":"bash","target":"count"}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lower_terminal(raw, "/app")

    def test_ir_envelope_and_fenced_terminal_action_remain_single_decisions(self):
        self.assertEqual(lower_terminal('{"action":{"kind":"done"},"reasoning":"submit"}', "/app").kind, "done")
        self.assertEqual(lower_terminal('简短分析\n```json\n{"action":"done"}\n```', "/app").kind, "done")

    def test_declared_direct_tool_forms_lower_to_same_ir_without_extra_operands(self):
        self.assertEqual(lower_terminal('{"action":"bash","tool":"bash","target":"count","content":""}', "/app").command, "count")
        read = lower_terminal('{"action":"read","target":"/app/input","content":""}', "/app")
        self.assertEqual(read.path, "input")
        self.assertIsNone(read.content)
        self.assertEqual(lower_terminal('{"action":"write","target":"/app/output","content":"42"}', "/app").content, "42")
        self.assertEqual(lower_terminal('{"action":"done","tool":"bash","content":""}', "/app").kind, "done")
        for raw in ('{"action":"read","tool":"bash","target":"input"}',
                    '{"action":"read","target":"input","content":"ignored payload"}',
                    '{"action":"done","tool":"bash","target":"evil"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lower_terminal(raw, "/app")

    def test_frontend_runs_through_same_journal_self_check_and_submission(self):
        replies = [
            '{"action":"act","tool":"write","target":"answer","content":"42"}',
            json.dumps(dict(action="verify", target='"' + sys.executable + '" -B -c "from pathlib import Path; assert Path(\'answer\').read_text()==\'42\'"')),
            '{"action":"done"}',
        ]
        class Gateway:
            def complete(self, request, timeout_s):
                self_test.assertEqual(request.system, TERMINAL_SYSTEM)
                self_test.assertGreaterEqual(request.input_token_bound,
                    token_bound(request.system) + token_bound(request.context) + 128)
                return ModelResponse(content=replies.pop(0), usage=Usage(input_tokens=10, output_tokens=10))
            def describe(self):
                return dict(kind="test", structured_output="none")
        self_test = self
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            task = TaskContract(task_id="frontend", goal="Write answer", require_self_check=True,
                acceptance_checks=(Check(id="grader", kind="external"),), budget=Budget(max_steps=3))
            result = NodeAgent(backend="local", policy=TerminalPolicy(Gateway())).run(task, work, root / "run")
            graph = read_run(root / "run")
            self.assertEqual(result.termination, Termination.SUBMITTED)
            self.assertFalse(result.verdict.passed)
            self.assertTrue(graph.attempts[1].self_check.passed)
            self.assertEqual(graph.attempts[0].action.path, "answer")
