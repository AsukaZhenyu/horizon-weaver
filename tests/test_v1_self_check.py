"""Behavioral regressions for versioned self-tests and bounded causal context."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler, token_bound
from horizon_weaver.core import (Action, Attempt, Budget, Check, GraphView, ProgressAssessment,
    StateDelta, StateSnapshot, TaskContract, Termination, ToolResult, current_self_checks, submission_readiness)
from horizon_weaver.policy import ScriptedPolicy, parse_action


def py(code):
    return '"' + sys.executable + '" -B -c "' + code + '"'


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()

    def run_actions(self, actions):
        task = TaskContract(task_id="hidden", goal="Produce a correct answer",
            acceptance_checks=(Check(id="grader", kind="external"),), require_self_check=True,
            budget=Budget(max_steps=len(actions), no_progress_limit=20))
        run = self.root / "run"
        result = NodeAgent(backend="local", policy=ScriptedPolicy(tuple(actions))).run(task, self.work, run)
        return result, read_run(run)

    def test_done_requires_executed_check_and_cannot_forge_receipt(self):
        result, graph = self.run_actions([Action(kind="done")])
        self.assertEqual(result.termination, Termination.MAX_STEPS)
        self.assertEqual(graph.attempts[0].result.error_type, "self_check_required")
        with self.assertRaises(ValidationError):
            Action(kind="done", self_check={"passed": True})
        with self.assertRaises(ValidationError):
            ToolResult(error_type="timeout", execution_terminated=True, outcome_unknown=True)

    def test_typed_envelope_preserves_one_action_and_rejects_ambiguous_authority(self):
        action = parse_action('{"reasoning":"test behavior", "action":{"kind":"verify","command":"assertion"}}')
        self.assertEqual(action, Action(kind="verify", command="assertion", reasoning="test behavior"))
        for raw in (
            '{"kind":"done","action":{"kind":"done"}}',
            '{"action":{"kind":"done"},"passed":true}',
            '{"action":[{"kind":"done"},{"kind":"command","command":"evil"}]}',
            '{"action":{"kind":"write","path":"../escape","content":"x"}}',
            '{"action":{"kind":"done","acceptance_checks":[]}}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_action(raw)

    def test_real_self_check_is_replayed_but_cannot_grant_acceptance(self):
        result, graph = self.run_actions([
            Action(kind="write", path="answer", content="42"),
            Action(kind="verify", command=py("from pathlib import Path; assert Path('answer').read_text() == '42'")),
            Action(kind="done")])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertFalse(result.verdict.passed)
        receipt = graph.attempts[1].self_check
        self.assertTrue(receipt.passed)
        self.assertEqual(receipt.snapshot_hash, result.snapshot.fingerprint)
        self.assertEqual(receipt.source, "policy")
        self.assertEqual(current_self_checks(graph.view.attempts, graph.snapshot), (receipt,))
        self.assertEqual(submission_readiness(graph.manifest.contract, (), graph.attempts[0].before).phase, "work")
        self.assertEqual(submission_readiness(graph.manifest.contract, (graph.attempts[0],),
                                             graph.attempts[0].after).phase, "self_test_required")
        self.assertEqual(submission_readiness(graph.manifest.contract, tuple(graph.attempts),
                                             graph.snapshot).phase, "ready_to_submit")
        request = ContextCompiler({"shell": "test"}).build(graph.manifest.contract, graph.view,
                                                           graph.snapshot, 4, 100000)
        self.assertIn('"phase":"ready_to_submit"', request.context)
        self.assertIn('return {"kind":"done"}', request.context)
        self.assertIn('UNSATISFIED INLINE REQUIREMENTS\n[]', request.context)

    def test_write_invalidates_check_and_cannot_be_followed_by_done(self):
        result, graph = self.run_actions([
            Action(kind="verify", command=py("assert True")),
            Action(kind="write", path="answer", content="changed"), Action(kind="done")])
        self.assertEqual(result.termination, Termination.MAX_STEPS)
        self.assertEqual(graph.attempts[-1].result.error_type, "self_check_required")
        self.assertEqual(submission_readiness(graph.manifest.contract, tuple(graph.attempts),
                                             graph.snapshot).phase, "self_test_required")

    def test_unrelated_pass_does_not_hide_current_failed_check(self):
        result, graph = self.run_actions([
            Action(kind="verify", command=py("assert False")),
            Action(kind="verify", command=py("assert True")), Action(kind="done")])
        self.assertEqual(result.termination, Termination.MAX_STEPS)
        self.assertEqual(graph.attempts[-1].result.error_type, "self_check_required")

    def test_mutating_check_is_not_evidence_even_with_exit_zero(self):
        result, graph = self.run_actions([
            Action(kind="verify", command=py("from pathlib import Path; Path('answer').write_text('42')")),
            Action(kind="done")])
        receipt = graph.attempts[0].self_check
        self.assertFalse(receipt.passed)
        self.assertEqual(receipt.result.error_type, "self_check_mutated_workspace")
        self.assertEqual(result.termination, Termination.MAX_STEPS)

    def test_fix_and_recheck_new_state_allows_submission(self):
        command = py("from pathlib import Path; assert Path('answer').read_text() == '42'")
        result, graph = self.run_actions([
            Action(kind="write", path="answer", content="wrong"),
            Action(kind="verify", command=command),
            Action(kind="write", path="answer", content="42"),
            Action(kind="verify", command=command), Action(kind="done")])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertFalse(graph.attempts[1].self_check.passed)
        self.assertTrue(graph.attempts[3].self_check.passed)

    def test_printing_wrong_candidate_does_not_allow_submission(self):
        result, graph = self.run_actions([
            Action(kind="write", path="answer", content="wrong"),
            Action(kind="verify", command=py("from pathlib import Path; print(Path('answer').read_text())")),
            Action(kind="done")])
        self.assertEqual(graph.attempts[1].result.error_type, "self_check_observation_only")
        self.assertIsNone(graph.attempts[1].self_check)
        self.assertEqual(graph.attempts[2].result.error_type, "self_check_required")
        self.assertEqual(result.termination, Termination.MAX_STEPS)

    def test_observation_rejection_can_be_replaced_by_executed_assertions(self):
        result, graph = self.run_actions([
            Action(kind="write", path="answer", content="42"),
            Action(kind="verify", command=py("print('42')")),
            Action(kind="verify", command=py("from pathlib import Path; actual=Path('answer').read_text(); print(actual); assert actual == '42'")),
            Action(kind="done")])
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertIsNone(graph.attempts[1].self_check)
        self.assertTrue(graph.attempts[2].self_check.passed)

    def test_observation_preflight_leaves_real_predicates_for_execution(self):
        from horizon_weaver.verification import observation_only
        for command in ("true", "echo passed", "cat answer", py("print('wrong')")):
            self.assertIsNotNone(observation_only(command))
        for command in ("cat answer | grep -q 42", "pytest -q", "python3 -B test_candidate.py",
                        py("import sys; print('wrong'); sys.exit(1)")):
            self.assertIsNone(observation_only(command))

    def test_counting_pipeline_has_no_submission_receipt(self):
        command = "grep -c 'INFO' logs/*.log | awk -F: '{sum+=$2} END {print sum}' && echo 'Expected 7'"
        result, graph = self.run_actions([Action(kind="verify", command=command), Action(kind="done")])
        self.assertEqual(graph.attempts[0].result.error_type, "self_check_observation_only")
        self.assertIsNone(graph.attempts[0].self_check)
        self.assertEqual(result.termination, Termination.MAX_STEPS)
        from horizon_weaver.verification import observation_only
        for predicate in ("cat answer | grep -q 42", "echo 42 | grep -qx 42",
                          "grep -c INFO logs/*.log | awk -F: '{sum+=$2} END {exit sum != 8}'"):
            self.assertIsNone(observation_only(predicate))

    def test_identical_self_check_is_not_new_progress(self):
        action = Action(kind="verify", command=py("assert True"))
        _, graph = self.run_actions([action, action, Action(kind="done")])
        self.assertEqual(graph.attempts[0].progress.level, "information")
        self.assertEqual(graph.attempts[1].progress.level, "none")

    def test_context_keeps_state_and_causal_order_without_repeating_write(self):
        _, graph = self.run_actions([
            Action(kind="write", path="answer", content="UNIQUE_BODY_" * 10000),
            Action(kind="read", path="answer"), Action(kind="done")])
        task = graph.manifest.contract
        request = ContextCompiler({"shell": "test"}).build(task, graph.view, graph.snapshot, 4, 100000)
        self.assertIn("answer file mode=", request.context)
        self.assertIn("content_sha256", request.context)
        self.assertNotIn("UNIQUE_BODY_" * 100, request.context)
        self.assertIn("truncated", request.context)
        self.assertLess(request.context.index("ATTEMPT 1"), request.context.index("ATTEMPT 2"))
        self.assertLess(request.context.index("ATTEMPT 2"), request.context.index("ATTEMPT 3"))
        self.assertLessEqual(token_bound(request.system) + token_bound(request.context) + 128,
                             request.input_token_bound)
        self.assertLessEqual(request.input_token_bound, task.budget.context_tokens)

    def test_latest_source_observation_keeps_middle_when_budget_allows(self):
        content = "start\n" + "x" * 1200 + "\nCRITICAL_FAILURE_BRANCH\n" + "y" * 1200 + "\nend"
        _, graph = self.run_actions([
            Action(kind="write", path="implementation.py", content=content),
            Action(kind="read", path="implementation.py")])
        request = ContextCompiler({"shell": "test"}).build(graph.manifest.contract, graph.view,
                                                           graph.snapshot, 3, 100000)
        self.assertIn(content, request.context)
        self.assertLessEqual(request.input_token_bound, graph.manifest.contract.budget.context_tokens)


if __name__ == "__main__":
    unittest.main()
