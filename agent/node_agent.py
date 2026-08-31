"""NodeAgent：问题解决层的基础设施门面（v0.5 起瘦身为「装配器」）。

给定一个已圈定的小任务（instruction）与一个终端会话（duck-typed session），
用「结构化 I/O + 工具化 + 退出码哨兵 + 去重/卡死 + 引擎自动推断」把任务做出来。

v0.5 重构：不再是一个硬编码一切的大类，而是「瘦门面」——把模型适配器、工具、
system prompt、verify、spec、主循环全部拆成 plugins 包（agent/plugins/）里的
可插拔组件，经 runtime.Context 装配。门面只做三件事：

1. 持有 NodeAgent 的历史签名 `run(instruction, session, logging_dir=None)`，
   让 tb_test.py 等调用方零改动或极小改动；
2. 把 `plugins=[...]`（或默认插件集）交给 Runtime 装配，取 `ctx.agentLoop` 执行；
3. 保留 `_call_model_with_retry` / `_make_attempt` 等历史方法名（test_node_agent.py
   直接 monkey-patch / 调用），它们委托给 plugins.LlmClient / engine.make_attempt。

引擎推断（attempt_summary / summarize_attempt / build_context …）在 engine.py；
插件在 plugins 包（agent/plugins/）；这里只 re-export 供 test_engine.py 的历史
import 兼容。

session 依赖 duck-typed 协议（与 tools.py 一致）：
    send_keys / get_incremental_output / copy_to_container / get_asciinema_timestamp
"""
from __future__ import annotations

from pathlib import Path

from engine import (
    CONTEXT_LOG_NAME,
    MAX_LEDGER_ENTRIES,
    MAX_OUTPUT_TOKENS,
    MIN_STEPS_BEFORE_DONE,
    SPEC_REEMIT_LIMIT,
    STUCK_LIMIT,
    TIME_LIMIT_S,
    _verify_repeat_feedback,
    attempt_summary,
    build_context,
    extract_error_line,
    make_attempt,
    summarize_attempt,
    summarize_result,
)
from plugins import LlmClient, NodeResult, build_default_plugins
from runtime import Runtime

# re-export 历史 import 面（test_engine.py：from node_agent import ...）
__all__ = [
    "NodeAgent",
    "NodeResult",
    "build_default_plugins",
    "MAX_LEDGER_ENTRIES",
    "CONTEXT_LOG_NAME",
    "MAX_OUTPUT_TOKENS",
    "MIN_STEPS_BEFORE_DONE",
    "STUCK_LIMIT",
    "SPEC_REEMIT_LIMIT",
    "TIME_LIMIT_S",
    "attempt_summary",
    "summarize_attempt",
    "summarize_result",
    "extract_error_line",
    "build_context",
    "_verify_repeat_feedback",
]


class NodeAgent:
    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        max_steps: int = 100,
        time_limit_s: float = TIME_LIMIT_S,
        plugins=None,
        enable_spec: bool = False,
        **kwargs,
    ):
        self._model_name = model_name
        self._api_base = api_base.rstrip("/")
        self._temperature = temperature
        self._max_steps = max_steps
        self._time_limit_s = time_limit_s
        self._plugins = plugins  # 可选：自定义插件集；None = 用默认（spec 默认关）
        self._enable_spec = enable_spec  # 裸 NodeAgent() 的 spec 开关（默认关，与 tb_test 一致）
        self._llm = None  # 惰性创建：仅当走门面自己的 HTTP 路径时才建

    # ---------- 历史方法（test_node_agent.py monkey-patch / 直接调用） ----------

    def _get_llm(self) -> LlmClient:
        if self._llm is None:
            self._llm = LlmClient(
                self._model_name, self._api_base, self._temperature, MAX_OUTPUT_TOKENS
            )
        return self._llm

    def _call_model_with_retry(self, messages: list[dict], max_attempts: int = 4):
        """委托给 LlmClient.complete（空输出重试）。test_node_agent.py 用假实现替换此方法。"""
        return self._get_llm().complete(messages, max_attempts)

    def _count_tokens(self, usage: dict, content: str = "") -> tuple[int, int]:
        """委托给 LlmClient.count_tokens。content 参数仅为兼容旧签名，不使用。"""
        return self._get_llm().count_tokens(usage)

    def _make_attempt(self, step_no: int, action, result):
        """委托给 engine.make_attempt（引擎自动生成 inference 与 result 摘要）。"""
        return make_attempt(step_no, action, result)

    # ---------- 装配 ----------

    def _make_llm_client(self):
        """把门面自己的 LLM 方法包成一个 client（complete/count_tokens），
        注入 LlmPlugin——这样 test_node_agent.py 对 `agent._call_model_with_retry` 的
        monkey-patch 才能透传到循环里。"""
        agent = self

        class _FacadeClient:
            def complete(self, messages):
                return agent._call_model_with_retry(messages)

            def count_tokens(self, usage):
                return agent._count_tokens(usage)

        return _FacadeClient()

    def _build_plugins(self):
        """裸 NodeAgent() 的默认插件集：spec 默认关（与 build_default_plugins / tb_test 一致）。

        要复现 v0.4 spec-first，用 `NodeAgent(enable_spec=True)` 或
        `NodeAgent(plugins=build_default_plugins(enable_spec=True))`。
        """
        return build_default_plugins(
            enable_spec=self._enable_spec,
            model_name=self._model_name,
            api_base=self._api_base,
            temperature=self._temperature,
            client=self._make_llm_client(),
        )

    def run(self, instruction: str, session, logging_dir: Path | None = None) -> NodeResult:
        plugins = self._plugins or self._build_plugins()
        rt = Runtime(plugins)
        ctx = rt.start()
        try:
            loop = ctx.get("agentLoop")
            if loop is None:
                raise RuntimeError("插件集缺少 AgentLoopPlugin（provides 'agentLoop'）")
            return loop(
                instruction,
                session,
                logging_dir,
                max_steps=self._max_steps,
                time_limit_s=self._time_limit_s,
            )
        finally:
            rt.stop()
