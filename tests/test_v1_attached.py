"""Deferred grading is submission, never model-authorized success."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler
from horizon_weaver.core import Action, Budget, Check, FileState, GraphView, StateSnapshot, TaskContract, Termination, ToolResult
from horizon_weaver.evaluation import Evaluator
from horizon_weaver.harness import LocalHarness
from horizon_weaver.policy import ScriptedPolicy


def deferred_task(**budget):
    return TaskContract(task_id="hidden-task", goal="Produce a candidate for hidden grading",
        acceptance_checks=(Check(id="benchmark-grader", kind="external"),), budget=Budget(**budget))


class DeferredGradingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()

    def test_external_evaluator_is_not_called_or_forged_by_harness(self):
        task = deferred_task()
        harness = LocalHarness(self.work)
        harness.start(task)
        with patch.object(harness, "check", return_value=ToolResult(exit_code=0)) as check:
            evaluation = Evaluator(harness).assess(task, 2)
        check.assert_not_called()
        self.assertFalse(evaluation.verdict.passed)
        self.assertFalse(evaluation.verdict.evidence[0].passed)
        self.assertEqual(evaluation.verdict.evidence[0].result.error_type, "external_verification_pending")

    def test_done_submits_durable_candidate_without_success(self):
        run = self.root / "run"
        actions = (Action(kind="write", path="answer.txt", content="candidate"), Action(kind="done"))
        result = NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(deferred_task(), self.work, run)
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(result.steps, 2)
        self.assertFalse(result.verdict.passed)
        self.assertEqual(result.verdict.unsatisfied_requirements, ("benchmark-grader",))
        self.assertEqual(read_run(run).result, result)
        self.assertEqual((self.work / "answer.txt").read_text(), "candidate")

    def test_verify_cannot_submit_and_inline_done_cannot_grant_success(self):
        result = NodeAgent(backend="local", policy=ScriptedPolicy((Action(kind="verify"),))).run(
            deferred_task(max_steps=1), self.work, self.root / "verify")
        self.assertEqual(result.termination, Termination.MAX_STEPS)
        inline = TaskContract(task_id="inline", goal="Write required text",
            acceptance_checks=(Check(id="text", kind="text_equals", path="answer.txt", expected="42"),),
            budget=Budget(max_steps=1))
        result = NodeAgent(backend="local", policy=ScriptedPolicy((Action(kind="done"),))).run(
            inline, self.work, self.root / "inline")
        self.assertEqual(result.termination, Termination.MAX_STEPS)
        self.assertFalse(result.verdict.passed)

    def test_contract_cannot_mix_grading_authorities_or_embed_hidden_tests(self):
        with self.assertRaises(ValidationError):
            Check(id="hidden", kind="external", command="fake tests")
        with self.assertRaises(ValidationError):
            TaskContract(task_id="mixed", goal="x", acceptance_checks=(
                Check(id="hidden", kind="external"), Check(id="local", kind="command", command="true")))

    def test_attached_run_records_opaque_identity_and_releases_adapter(self):
        harness = LocalHarness(self.work)
        run = self.root / "attached"
        result = NodeAgent(policy=ScriptedPolicy((Action(kind="done"),))).run_attached(
            deferred_task(), harness, run, workspace_id="docker:caller-owned:/app")
        graph = read_run(run)
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(graph.manifest.workspace, "docker:caller-owned:/app")
        self.assertEqual(graph.manifest.backend, "attached")
        self.assertIsNone(harness.task)
        self.assertTrue(self.work.is_dir())

    def test_pending_hidden_grading_is_not_reported_as_a_failed_test(self):
        task = deferred_task(max_steps=1)
        harness = LocalHarness(self.work)
        harness.start(task)
        evaluation = Evaluator(harness).assess(task, 2)
        request = ContextCompiler(harness.describe()).build(task,
            GraphView(snapshot=evaluation.snapshot, verdict=evaluation.verdict), evaluation.snapshot, 1, 100000)
        self.assertNotIn("FAILED ACCEPTANCE", request.context)
        result = NodeAgent(backend="local", policy=ScriptedPolicy((Action(kind="verify"),))).run(
            task, self.work, self.root / "pending")
        self.assertIn("pending", result.checkpoint.feedback.lower())
        self.assertNotIn("Acceptance failed", result.checkpoint.feedback)

    def test_state_inventory_exposes_permissions_and_size(self):
        snapshot = StateSnapshot(files=(FileState(path="script.sh", kind="file", sha256="abc",
                                                  size=37, mode=0o640),))
        request = ContextCompiler({"shell": "/bin/sh"}).build(deferred_task(), GraphView(), snapshot, 1, 100000)
        self.assertIn("script.sh file mode=0640 size=37 sha256=abc", request.context)


if __name__ == "__main__":
    unittest.main()
