"""Offline checks for budgets, score isolation and the unchanged MAGE state operations."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
UPSTREAM = ROOT / "_work/mage/upstream"
sys.path[:0] = [str(HERE), str(UPSTREAM), str(UPSTREAM / "benchmark/math")]
from agentcore.llm.LLMConfig import LLMConfig
from agentcore.llm.LLMBackend import BaseLLM, VLLMLLM
from math_actions import CodeExecutorAction
from math_env import MathEnvironment
from math_mage_agent import MathMAGEAgent, PaperSharedState
from budget_backend import make_llm

spec = importlib.util.spec_from_file_location("repro_driver", HERE / "run.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

class Checks(unittest.TestCase):
    def test_cleanup_error_cannot_overwrite_the_original_trial_status(self):
        for status in ("completed", "solver_failed"):
            manifest = {"status": status, "solver_exit": 0 if status == "completed" else 1}
            with patch.object(driver, "cleanup_code", side_effect=subprocess.TimeoutExpired("docker", 15)):
                driver.cleanup_preserving_manifest("this-run-label-only", manifest)
            self.assertEqual(manifest["status"], status)
            self.assertIn("TimeoutExpired", manifest["cleanup_error"])

    def test_public_input_has_no_reference_answers(self):
        task = {"id": 0, "questions": ["q"], "backgrounds": ["b"], "answers": ["PRIVATE"], "other": "PRIVATE"}
        self.assertEqual(driver.public_task(task), {"id": 0, "questions": ["q"], "backgrounds": ["b"]})
        stage = ROOT / "_work/mage/image"
        self.assertEqual({p.name for p in stage.iterdir()}, {"Dockerfile", "requirements-math.lock.txt", "wheels"})

    def test_request_budget_and_explicit_thinking(self):
        llm = BaseLLM(LLMConfig({"max_output_tokens": 768, "enable_thinking": False, "request_attempts": 1}))
        kwargs = llm._build_sampling_kwargs()
        self.assertEqual(kwargs["max_completion_tokens"], 768)
        self.assertFalse(kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        llm.send_chat_template_kwargs = False
        self.assertNotIn("extra_body", llm._build_sampling_kwargs())

    def test_call_cap_prevents_another_request_and_records_cost(self):
        config = json.loads((HERE / "configs/math1-sanity.json").read_text())
        config.update(model="offline-fixture", base_url="http://127.0.0.1:1/v1", solver_max_calls=1)
        def fake_run(llm, prompt, system_prompt=None):
            llm._call_trace.append({"prompt_tokens": 10, "completion_tokens": 2, "prompt": prompt, "response": "fixture"})
            return "fixture"
        with tempfile.TemporaryDirectory() as directory, patch.object(VLLMLLM, "run", fake_run):
            llm = make_llm(UPSTREAM, config, "solver", Path(directory))
            try:
                self.assertEqual(llm.run("evidence"), "fixture")
                with self.assertRaisesRegex(RuntimeError, "call budget"):
                    llm.run("should never be sent")
                summary = json.loads((Path(directory) / "solver_usage.json").read_text())
                self.assertEqual(summary["total_tokens"], 12)
                self.assertEqual(summary["attempted_calls"], 1)
                self.assertEqual(llm.request_attempts, 1)
            finally:
                llm.client.close()

    def test_strict_judge_does_not_fallback_or_accept_ambiguous_output(self):
        class Judge:
            llm_name, base_url = "fixture", "offline"
            temperature, top_p, seed, enable_thinking = 0, 1, 2026, False
            def run(self, *args, **kwargs):
                return "No verdict available"
        env = MathEnvironment(Judge())
        env.strict_judge = True
        with self.assertRaisesRegex(RuntimeError, "Strict judge failed"):
            env.judge_answer("q", "b", "same", "same")
        with patch.object(Judge, "run", side_effect=ConnectionError("offline")):
            with self.assertRaisesRegex(RuntimeError, "Strict judge failed"):
                env.judge_answer("q", "b", "same", "same")

    def test_generated_code_is_stdin_only_in_separate_no_network_container(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="2\n", stderr="")
        with patch.dict(os.environ, {"MAGE_CODE_IMAGE": "fixture:math", "MAGE_RUN_ID": "offline-check"}):
            with patch("math_actions.subprocess.run", side_effect=fake_run):
                output = CodeExecutorAction()(code="print(1+1)")
        command, kwargs = calls[0]
        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertNotIn("--mount", command)
        self.assertNotIn("-v", command)
        self.assertEqual(kwargs["input"], "print(1+1)")
        self.assertIn("code = sys.stdin.read()", command[-1])
        self.assertNotIn("sys.path[:] =", command[-1])
        self.assertIn("Code Output:\n2", output)
        self.assertEqual(calls[-1][0][:3], ["docker", "rm", "--force"])

    def test_original_grow_compress_revise_loop_without_model_or_generated_code(self):
        from agentcore.actions.MAGEActions import STORE_SUMMARY_KEY, BACKTRACE_STEP_KEY
        actions = iter([
            'SymbolicReasoning[{"reasoning":"Try a first derivation."}]',
            "Store[" + json.dumps({STORE_SUMMARY_KEY: "First derivation checkpoint."}) + "]",
            'Backtrace[' + json.dumps({BACKTRACE_STEP_KEY: 0, 'reason': 'Test the revision boundary.'}) + ']',
            'SymbolicReasoning[{"reasoning":"Recompute 1+1=2."}]',
            'Finish[{"response":"2"}]',
            "YES",
        ])
        class Scripted(BaseLLM):
            def run(self, *args, **kwargs):
                return next(actions)
        with tempfile.TemporaryDirectory() as directory:
            state = PaperSharedState(num_subtasks=1, subtask_answers=[], final_answer="", max_judge_retries=0)
            llm = Scripted(LLMConfig({}))
            agent = MathMAGEAgent(
                question="What is 1+1?", background="Work over the integers.",
                subtask_idx=0, shared_state=state, math_env=MathEnvironment(), llm=llm,
                max_steps=10, max_verify_retries=1, max_check_retries=0, max_backtrace=5,
                log_file=str(Path(directory) / "fixture.log"),
            )
            agent.logger.FLAG_PRINT = False
            agent(agent.build_task())
            self.assertEqual(state.subtask_tree.active_records()[0].submitted_answer, "2")
            self.assertGreater(len(state.exploration_tree.summaries), 1)
            self.assertEqual(state.judge_count, 0)
            self.assertEqual(state.subtask_answers, [])
            self.assertEqual(agent._agent_backtrace_count, 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
