"""State relationships, historical retrieval and conservative transition handling."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.context import ContextCompiler, token_bound
from horizon_weaver.core import (Action, Attempt, Budget, Check, DecisionInput, Evidence,
    FileState, GraphView, ProgressAssessment, StateDelta, StateSnapshot, TaskContract,
    Termination, ToolResult, Verdict, canonical, digest)
from horizon_weaver.event_store import EventStore
from horizon_weaver.harness import LocalHarness
from horizon_weaver.policy import ScriptedPolicy
from horizon_weaver.state_ir import StateEdge, StateIR, artifact_id, build_state_ir, state_id
from horizon_weaver.state_queries import StateQuery
from horizon_weaver.transitions import TransitionGuard


def task(**budget):
    return TaskContract(task_id="state-query", goal="Repair implementation.py",
        acceptance_checks=(Check(id="implementation", kind="text_equals", path="implementation.py",
                                 expected="correct"),), budget=Budget(**budget))


def snapshot(files):
    return StateSnapshot(files=tuple(FileState(path=path, kind="file", sha256=digest(content),
        size=len(content.encode("utf-8")), mode=0o644) for path, content in sorted(files.items())))


def attempt(step, action, before, after=None, output="", exit_code=0):
    after = after or before
    return Attempt(step=step, action=action, before=before, after=after,
        result=ToolResult(output=output, exit_code=exit_code), delta=StateDelta.between(before, after),
        progress=ProgressAssessment(level="information", reason_code="fixture"))


class RepresentationTests(unittest.TestCase):
    def test_versions_relations_and_path_survive_round_trip(self):
        before = snapshot({"implementation.py": "bad"})
        after = snapshot({"implementation.py": "correct"})
        write = attempt(1, Action(kind="write", path="implementation.py", content="correct"), before, after)
        ir = build_state_ir(task(), GraphView(attempts=(write,)), after)
        restored = StateIR.model_validate_json(ir.model_dump_json())
        self.assertEqual(restored, ir)
        self.assertEqual(restored.fingerprint, ir.fingerprint)
        query = StateQuery(restored)
        self.assertEqual(len(query.file_versions("implementation.py")), 2)
        self.assertEqual(query.parents(ir.current_state), (ir.initial_state,))
        self.assertEqual(query.children(ir.initial_state), (ir.current_state,))
        self.assertEqual([t.attempt.step for t in query.path_to(ir.current_state)], [1])
        self.assertEqual(query.touching("implementation.py")[0].attempt.delta.changed_files,
                         ("implementation.py",))
        self.assertIsNone(query.path_to("unknown"))
        with self.assertRaises(ValidationError):
            ir.current_state = ir.initial_state

    def test_graph_cycle_keeps_transition_history_and_finite_path_query(self):
        first, second = snapshot({"implementation.py": "a"}), snapshot({"implementation.py": "b"})
        changes = (attempt(1, Action(kind="write", path="implementation.py", content="b"), first, second),
                   attempt(2, Action(kind="write", path="implementation.py", content="a"), second, first))
        ir = build_state_ir(task(), GraphView(attempts=changes), first)
        self.assertEqual(len(ir.states), 2)
        self.assertEqual(len(ir.transitions), 2)
        self.assertEqual(StateQuery(ir).path_to(ir.current_state), ())
        self.assertEqual(len(StateQuery(ir).path_to(state_id(second.fingerprint))), 1)

    def test_ir_rejects_dangling_and_factually_wrong_relations(self):
        before, after = snapshot({"implementation.py": "a"}), snapshot({"implementation.py": "b"})
        change = attempt(1, Action(kind="write", path="implementation.py", content="b"), before, after)
        ir = build_state_ir(task(), GraphView(attempts=(change,)), after)
        for edge in (StateEdge(kind="contains", source=ir.current_state, target="missing"),
                     StateEdge(kind="from_state", source=ir.current_state, target="transition:1"),
                     StateEdge(kind="contains", source=ir.current_state, target=artifact_id(before.files[0]))):
            with self.subTest(edge=edge), self.assertRaises(ValidationError):
                StateIR.model_validate({**ir.model_dump(), "edges": [*ir.edges, edge]})
        with self.assertRaises(ValidationError):
            StateIR.model_validate({**ir.model_dump(), "edges": []})

    def test_historical_evidence_is_retained_without_granting_current_success(self):
        contract = task()
        before, after = snapshot({"implementation.py": "correct"}), snapshot({"implementation.py": "wrong"})
        evidence = Evidence(check_id="implementation", check_hash=digest(canonical(contract.acceptance_checks[0])),
            snapshot_hash=before.fingerprint, passed=True, result=ToolResult(exit_code=0))
        view = GraphView(snapshot=before, verdict=Verdict(passed=True, evidence=(evidence,)),
                         evidence_history=(evidence,))
        ir = build_state_ir(contract, view, after)
        self.assertFalse(ir.verdict.passed)
        self.assertEqual(StateQuery(ir).current_evidence(), ())
        self.assertEqual(StateQuery(ir).stale_evidence()[0].evidence, evidence)
        self.assertIn("implementation", ir.verdict.unsatisfied_requirements)

    def test_failed_read_is_not_an_observation_edge(self):
        state = snapshot({"implementation.py": "bad"})
        read = attempt(1, Action(kind="read", path="implementation.py"), state, output="denied", exit_code=1)
        ir = build_state_ir(task(), GraphView(attempts=(read,)), state)
        self.assertFalse(any(edge.kind == "observes" for edge in ir.edges))
        self.assertIsNone(StateQuery(ir).latest_observation("implementation.py"))


class ContextQueryTests(unittest.TestCase):
    def noise_fixture(self):
        files = {"implementation.py": "IMPORTANT_WORKING_SET_MARKER\n"}
        files.update({f"noise-{i}.txt": f"unrelated-{i}:" + "n" * 1200 for i in range(24)})
        state = snapshot(files)
        observations = [attempt(1, Action(kind="read", path="implementation.py"), state,
                                output=files["implementation.py"])]
        observations.extend(attempt(i + 2, Action(kind="read", path=f"noise-{i}.txt"), state,
                                    output=files[f"noise-{i}.txt"]) for i in range(24))
        return state, GraphView(snapshot=state, attempts=tuple(observations))

    def test_required_observation_survives_long_irrelevant_history(self):
        state, view = self.noise_fixture()
        contract = task(context_tokens=14000)
        compiler = ContextCompiler({"shell": "test"}, strategy="state")
        request = compiler.build(contract, view, state, 26, 100000)
        self.assertIn("IMPORTANT_WORKING_SET_MARKER", request.context)
        self.assertIn(1, request.visible_observation_steps)
        self.assertIn(1, request.selected_attempt_steps)
        self.assertEqual(request.selected_attempt_steps[-1], 25)
        self.assertLess(len(request.selected_attempt_steps), 10)
        self.assertLessEqual(token_bound(request.system) + token_bound(request.context) + 128,
                             request.input_token_bound)
        self.assertLessEqual(request.input_token_bound, contract.budget.context_tokens)
        recent = ContextCompiler({"shell": "test"}, strategy="recent").build(contract, view, state, 26, 100000)
        self.assertLess(request.input_token_bound, recent.input_token_bound)

    def test_same_file_version_is_coalesced_across_unrelated_changes(self):
        first = snapshot({"implementation.py": "bad"})
        second = snapshot({"implementation.py": "bad", "other.txt": "changed"})
        reads = (attempt(1, Action(kind="read", path="implementation.py"), first, output="bad"),
                 attempt(2, Action(kind="read", path="implementation.py"), second, output="bad"))
        request = ContextCompiler({}).build(task(), GraphView(attempts=reads), second, 3, 100000)
        self.assertEqual(request.selected_attempt_steps, (2,))
        self.assertEqual(request.visible_observation_steps, (2,))

    def test_old_source_body_is_not_displayed_as_current(self):
        before = snapshot({"implementation.py": "OBSOLETE_SOURCE_MARKER"})
        after = snapshot({"implementation.py": "new source"})
        reads = (attempt(1, Action(kind="read", path="implementation.py"), before, output="OBSOLETE_SOURCE_MARKER"),)
        request = ContextCompiler({}).build(task(), GraphView(attempts=reads), after, 2, 100000)
        self.assertNotIn("OBSOLETE_SOURCE_MARKER", request.context)
        self.assertIn("Historical file version", request.context)
        self.assertEqual(request.visible_observation_steps, ())

    def test_truncated_read_does_not_block_a_new_read(self):
        body = "large source\n" * 10000
        state = snapshot({"implementation.py": body})
        read = attempt(1, Action(kind="read", path="implementation.py"), state, output=body)
        view = GraphView(attempts=(read,))
        request = ContextCompiler({}).build(task(), view, state, 2, 100000)
        self.assertIn("truncated", request.context)
        self.assertEqual(request.visible_observation_steps, ())
        self.assertIsNone(TransitionGuard().assess(read.action, request, view, state))

    def test_complete_current_read_is_guarded_but_changed_version_is_allowed(self):
        state = snapshot({"implementation.py": "bad"})
        read = attempt(1, Action(kind="read", path="implementation.py"), state, output="bad")
        view = GraphView(attempts=(read,))
        request = ContextCompiler({}).build(task(), view, state, 2, 100000)
        guarded = TransitionGuard().assess(read.action, request, view, state)
        self.assertEqual(guarded.error_type, "redundant_action")
        changed = snapshot({"implementation.py": "changed"})
        self.assertIsNone(TransitionGuard().assess(read.action, request, view, changed))
        hidden = request.model_copy(update={"visible_observation_steps": ()})
        self.assertIsNone(TransitionGuard().assess(read.action, hidden, view, state))

    def test_commands_and_self_checks_are_always_executed(self):
        state = snapshot({"implementation.py": "bad"})
        request = DecisionInput(step=2, system="", context="", input_token_bound=100, max_output_tokens=10)
        for kind in ("command", "verify"):
            action = Action(kind=kind, command="assertion")
            view = GraphView(attempts=(attempt(1, action, state, output="passed"),))
            self.assertIsNone(TransitionGuard().assess(action, request, view, state))

    def test_identical_write_is_allowed_after_change_or_a_failed_write(self):
        state = snapshot({"implementation.py": "bad"})
        action = Action(kind="write", path="implementation.py", content="bad")
        request = DecisionInput(step=2, system="", context="", input_token_bound=100, max_output_tokens=10)
        succeeded = attempt(1, action, state)
        view = GraphView(attempts=(succeeded,))
        self.assertEqual(TransitionGuard().assess(action, request, view, state).error_type, "redundant_action")
        changed = snapshot({"implementation.py": "changed"})
        self.assertIsNone(TransitionGuard().assess(action, request, view, changed))
        failed = attempt(1, action, state, exit_code=1)
        self.assertIsNone(TransitionGuard().assess(action, request, GraphView(attempts=(failed,)), state))


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_fixture(self, enabled):
        work = self.root / ("guard" if enabled else "reference")
        work.mkdir()
        (work / "note.txt").write_text("known observation", encoding="utf-8")
        actions = (Action(kind="read", path="note.txt"), Action(kind="read", path="note.txt"),
                   Action(kind="write", path="answer.txt", content="42"),
                   Action(kind="write", path="answer.txt", content="42"), Action(kind="done"))
        contract = TaskContract(task_id="guard", goal="Write answer 42", acceptance_checks=(
            Check(id="external", kind="external"),), budget=Budget(max_steps=5, no_progress_limit=10))
        executed = []
        original = LocalHarness.execute
        def execute(harness, action, timeout_s):
            executed.append(action.kind.value)
            return original(harness, action, timeout_s)
        with patch.object(LocalHarness, "execute", execute):
            result = NodeAgent(backend="local", policy=ScriptedPolicy(actions), transition_guard=enabled).run(
                contract, work, self.root / (work.name + "-run"))
        return result, read_run(self.root / (work.name + "-run")), executed

    def test_runtime_avoids_redundant_tools_and_records_actual_rejection(self):
        reference, _, reference_calls = self.run_fixture(False)
        result, graph, calls = self.run_fixture(True)
        self.assertEqual(reference.termination, Termination.SUBMITTED)
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(reference_calls, ["read", "read", "write", "write", "done"])
        self.assertEqual(calls, ["read", "write", "done"])
        self.assertEqual([a.result.error_type for a in graph.attempts],
                         [None, "redundant_action", None, "redundant_action", None])
        self.assertEqual(graph.attempts[1].progress.level, "none")
        self.assertEqual([a.tool_executed for a in graph.attempts], [True, False, True, False, True])
        self.assertFalse(result.verdict.passed)
        self.assertEqual(graph.ir.control.step, 5)
        self.assertEqual(graph.ir.run_id, graph.manifest.run_id)
        self.assertEqual(json.loads(graph.manifest.runtime_json),
                         {"context_strategy": "state", "transition_guard": True})

    def test_replay_and_queries_do_not_execute_models_or_tools(self):
        _, graph, _ = self.run_fixture(True)
        run = self.root / "guard-run"
        with patch.object(LocalHarness, "execute", side_effect=AssertionError("replay side effect")), \
             patch.object(ScriptedPolicy, "decide", side_effect=AssertionError("replay model call")):
            first, second = read_run(run), read_run(run)
            self.assertEqual(first.ir, second.ir)
            self.assertEqual(first.ir.fingerprint, graph.ir.fingerprint)
            self.assertEqual(len(first.query.file_versions("note.txt")), 1)
        # The legacy import is the same storage implementation, not a second writer.
        from horizon_weaver.state import EventStore as LegacyStore
        self.assertIs(LegacyStore, EventStore)

    def test_self_check_is_separate_from_contract_evidence(self):
        work = self.root / "work"
        work.mkdir()
        command_args = [sys.executable, "-B", "-c", "assert True"]
        command = subprocess.list2cmdline(command_args) if os.name == "nt" else shlex.join(command_args)
        actions = (Action(kind="verify", command=command),
                   Action(kind="done"))
        contract = TaskContract(task_id="self-check", goal="External grade", acceptance_checks=(
            Check(id="grader", kind="external"),), require_self_check=True, budget=Budget(max_steps=2))
        result = NodeAgent(backend="local", policy=ScriptedPolicy(actions)).run(contract, work, self.root / "run")
        ir = read_run(self.root / "run").ir
        self.assertEqual(result.termination, Termination.SUBMITTED)
        self.assertEqual(len(ir.self_checks), 1)
        self.assertFalse(ir.verdict.passed)
        self.assertTrue(all(node.evidence.check_id == "grader" for node in ir.evidence))

    def test_changed_runtime_configuration_blocks_resume(self):
        work = self.root / "work"
        work.mkdir()
        (work / "implementation.py").write_text("bad", encoding="utf-8")
        actions = (Action(kind="read", path="implementation.py"), Action(kind="done"))
        class InterruptedPolicy(ScriptedPolicy):
            def decide(self, request, timeout_s):
                if request.step == 2:
                    raise KeyboardInterrupt
                return super().decide(request, timeout_s)
        run = self.root / "run"
        result = NodeAgent(backend="local", policy=InterruptedPolicy(actions), transition_guard=False).run(
            task(), work, run)
        self.assertEqual(result.termination, Termination.INTERRUPTED)
        with self.assertRaisesRegex(ValueError, "resume requires the same"):
            NodeAgent(backend="local", policy=ScriptedPolicy(actions), transition_guard=True).run(
                task(), work, run, resume=True)

    def test_file_changed_during_model_decision_can_be_read_again(self):
        work = self.root / "work"
        work.mkdir()
        (work / "implementation.py").write_text("bad", encoding="utf-8")
        actions = (Action(kind="read", path="implementation.py"), Action(kind="read", path="implementation.py"))
        class ChangingPolicy(ScriptedPolicy):
            def decide(self, request, timeout_s):
                if request.step == 2:
                    (work / "implementation.py").write_text("changed during decision", encoding="utf-8")
                return super().decide(request, timeout_s)
        run = self.root / "run"
        NodeAgent(backend="local", policy=ChangingPolicy(actions)).run(task(max_steps=2), work, run)
        graph = read_run(run)
        self.assertEqual(graph.attempts[1].result.output, "changed during decision")
        self.assertTrue(graph.attempts[1].tool_executed)
        self.assertEqual(graph.attempts[1].before.file("implementation.py").sha256,
                         digest("changed during decision"))

    def test_stale_write_is_rejected_before_overwriting_external_change(self):
        work = self.root / "work"
        work.mkdir()
        (work / "implementation.py").write_text("bad", encoding="utf-8")
        actions = (Action(kind="write", path="implementation.py", content="correct"),)
        class ChangingPolicy(ScriptedPolicy):
            def decide(self, request, timeout_s):
                (work / "implementation.py").write_text("external update", encoding="utf-8")
                return super().decide(request, timeout_s)
        run = self.root / "run"
        NodeAgent(backend="local", policy=ChangingPolicy(actions)).run(task(max_steps=1), work, run)
        graph = read_run(run)
        self.assertEqual(graph.attempts[0].result.error_type, "state_changed_during_decision")
        self.assertFalse(graph.attempts[0].tool_executed)
        self.assertEqual((work / "implementation.py").read_text(encoding="utf-8"), "external update")


if __name__ == "__main__":
    unittest.main()
