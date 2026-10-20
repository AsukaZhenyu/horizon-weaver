"""Terminal-Bench 0.2 BaseAgent adapter. Hidden grading remains outside the runtime."""
from __future__ import annotations

from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode

from .app import NodeAgent
from .attached import AttachedDockerHarness
from .core import Budget, Check, TaskContract, Termination


class HorizonWeaverAgent(BaseAgent):
    def __init__(self, model_name: str = "qwen3.5-9b",
                 api_base: str = "http://127.0.0.1:8080/v1", temperature: float = .2,
                 max_steps: int = 40, wall_time_s: float = 300, tool_timeout_s: float = 30,
                 context_tokens: int = 14000, max_model_tokens: int = 200000,
                 output_tokens: int = 8192, workspace: str = "/app", structured_output: str | None = None,
                 policy_style: str = "ir", context_strategy: str = "state",
                 transition_guard: bool | str = True, require_self_check: bool | str = False,
                 max_tool_timeout_s: float = 120, decision_output_tokens: int = 2048,
                 model_timeout_s: float = 60, max_model_timeout_s: float = 120,
                 exploration_history: bool | str = True, enhanced_verify: bool | str = True, **kwargs):
        super().__init__(**kwargs)
        self.model_name, self.api_base, self.temperature = model_name, api_base, float(temperature)
        self.workspace = workspace
        self.structured_output = structured_output
        self.policy_style = policy_style
        if isinstance(transition_guard, str):
            if transition_guard.lower() not in ("true", "false"):
                raise ValueError("transition_guard must be true or false")
            transition_guard = transition_guard.lower() == "true"
        if not isinstance(transition_guard, bool):
            raise ValueError("transition_guard must be a boolean")
        self.context_strategy, self.transition_guard = context_strategy, transition_guard
        if isinstance(require_self_check, str):
            if require_self_check.lower() not in ("true", "false"):
                raise ValueError("require_self_check must be true or false")
            require_self_check = require_self_check.lower() == "true"
        if not isinstance(require_self_check, bool):
            raise ValueError("require_self_check must be a boolean")
        self.require_self_check = require_self_check
        def flag(value, name):
            if isinstance(value, str) and value.lower() in ('true', 'false'):
                return value.lower() == 'true'
            if isinstance(value, bool):
                return value
            raise ValueError(f'{name} must be a boolean')
        self.exploration_history = flag(exploration_history, 'exploration_history')
        self.enhanced_verify = flag(enhanced_verify, 'enhanced_verify')
        self.budget = Budget(max_steps=int(max_steps), wall_time_s=float(wall_time_s),
            tool_timeout_s=float(tool_timeout_s), max_tool_timeout_s=float(max_tool_timeout_s), context_tokens=int(context_tokens),
            max_model_tokens=int(max_model_tokens), output_tokens=int(output_tokens), no_progress_limit=8,
            decision_output_tokens=int(decision_output_tokens), model_timeout_s=float(model_timeout_s),
            max_model_timeout_s=float(max_model_timeout_s))

    @staticmethod
    def name() -> str:
        return "horizon-weaver-v1"

    def perform_task(self, instruction: str, session, logging_dir: Path | None = None) -> AgentResult:
        if logging_dir is None:
            raise ValueError("Terminal-Bench must provide a durable host logging directory")
        container = session.container
        config = container.attrs["Config"]
        harness = AttachedDockerHarness(container.id, workspace=self.workspace,
                                         user=config.get("User", ""), image=config.get("Image", ""))
        from .verification import compile_goal_conditions
        task = TaskContract(task_id=container.name, goal=instruction,
            constraints=("Work inside the benchmark container. Hidden tests are provided only after submission.",),
            acceptance_checks=(Check(id="terminal-bench:hidden-grader", kind="external"),),
            require_self_check=self.require_self_check, budget=self.budget,
            exploration_history=self.exploration_history,
            verification_policy='behavioral' if self.enhanced_verify else 'legacy',
            goal_conditions=compile_goal_conditions(instruction, self.workspace) if self.enhanced_verify else (),
            require_goal_review=self.enhanced_verify)
        result = NodeAgent(model=self.model_name, base_url=self.api_base, temperature=self.temperature,
                           structured_output=self.structured_output, policy_style=self.policy_style,
                           context_strategy=self.context_strategy, transition_guard=self.transition_guard).run_attached(
            task, harness, Path(logging_dir) / "horizon-v1",
            workspace_id=f"docker:{container.id}:{self.workspace}")
        mode = FailureMode.NONE
        if result.termination == Termination.TIMEOUT:
            mode = FailureMode.AGENT_TIMEOUT
        elif result.termination in (Termination.MODEL_ERROR, Termination.HARNESS_ERROR, Termination.RECOVERY_BLOCKED):
            mode = FailureMode.UNKNOWN_AGENT_ERROR
        # NONE reports a usable trajectory, never a passing task. Only the TB
        # harness's parser_results/is_resolved fields constitute a grade.
        return AgentResult(total_input_tokens=result.usage.input_tokens,
                           total_output_tokens=result.usage.output_tokens, failure_mode=mode)
