"""TbTestAgent：Terminal-Bench 适配器，把 TmuxSession 喂给 NodeAgent。

v0.3：keyan_agent.py 改名并瘦身——不再承担科研层（图导航 / 结点三分），
只做一件小事：把 Terminal-Bench 的 BaseAgent 接口翻译成 NodeAgent 的基础设施调用，
并把 NodeResult.termination（纯字符串）映射成 terminal-bench 的 FailureMode。

运行方式（在 terminal-bench 目录，且本文件所在目录在 sys.path 上）：
    uv run tb run --task-id hello-world \
        --agent-import-path "tb_test:TbTestAgent" \
        --model qwen3.5-9b \
        --agent-kwarg api_base=http://127.0.0.1:8080/v1
"""
from __future__ import annotations

from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode

from node_agent import NodeAgent

# termination -> FailureMode：stuck / max_steps 都不是「引擎崩溃」，只是没解出来；
# 唯一致命的引擎错误是模型服务调用失败（model_error）。
_TERMINATION_TO_FAILURE = {
    "stuck": FailureMode.UNKNOWN,
    "model_error": FailureMode.FATAL_LLM_PARSE_ERROR,
    "done": FailureMode.NONE,
    "max_steps": FailureMode.NONE,
}


class TbTestAgent(BaseAgent):
    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        max_steps: int = 100,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._node_agent = NodeAgent(
            model_name=model_name,
            api_base=api_base,
            temperature=temperature,
            max_steps=max_steps,
        )

    @staticmethod
    def name() -> str:
        return "keyan"  # 沿用 v0.2 的 run 目录名，便于与历史对比

    def perform_task(
        self,
        instruction: str,
        session,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        result = self._node_agent.run(instruction, session, logging_dir)
        return AgentResult(
            total_input_tokens=result.total_input_tokens,
            total_output_tokens=result.total_output_tokens,
            failure_mode=_TERMINATION_TO_FAILURE.get(result.termination, FailureMode.NONE),
            timestamped_markers=result.markers,
        )
