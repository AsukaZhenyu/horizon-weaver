"""llm.py：LlmPlugin —— 提供 ctx.llm（本地 llama-server 客户端）。

含 LlmClient（httpx 直连 /chat/completions，空输出重试）与 LlmPlugin（require
thinking 可选：最小闭环无 System2 也能跑）。换模型 = 换挂在 ctx.llm 后的对象，
主循环一行不改。
"""
from __future__ import annotations

import logging

import httpx

from engine import MAX_OUTPUT_TOKENS
from runtime import Plugin

_logger = logging.getLogger(__name__)


class LlmClient:
    """本地 llama-server 的 HTTP 客户端。complete 带空输出重试。"""

    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        max_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        self._model_name = model_name
        self._api_base = api_base.rstrip("/")
        self._temperature = temperature
        self._max_tokens = max_tokens

    def _call(self, messages: list[dict]) -> tuple[str, dict]:
        payload = {
            "model": self._model_name,
            "messages": messages,
            "temperature": self._temperature,
            # 推理模型（Qwen3.5-9B 思考版）的 reasoning 与回答共享这个预算；
            # 太小会因思考耗尽 token 导致 content 为空；System 2 自由长考更需要空间。
            "max_tokens": self._max_tokens,
        }
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            resp = client.post(
                self._api_base + "/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        # 思考模型偶尔把最终 JSON 留在 reasoning_content，或 content 为空/无花括号；
        # 此时退回含 JSON 的那一段，保证 parse_action 有东西可解析。
        if ("{" not in content) and ("{" in reasoning):
            content = reasoning
        usage = data.get("usage", {})
        return content, usage

    def complete(self, messages: list[dict], max_attempts: int = 4) -> tuple[str, dict]:
        """调用模型并在空输出时重试（推理模型偶发思考早停导致 content 为空）。"""
        last_usage: dict = {}
        for attempt in range(1, max_attempts + 1):
            raw, usage = self._call(messages)
            last_usage = usage
            if raw.strip():
                return raw, usage
            _logger.warning(f"模型返回空输出（第 {attempt}/{max_attempts} 次），重试…")
        return "", last_usage

    def count_tokens(self, usage: dict) -> tuple[int, int]:
        """从 usage 里取 token 数；不同 llama-server 版本字段名可能不同。"""
        inp = usage.get("prompt_tokens", usage.get("tokens_evaluated", 0))
        out = usage.get("completion_tokens", usage.get("tokens_predicted", 0))
        return int(inp or 0), int(out or 0)


class LlmPlugin(Plugin):
    name = "llm"
    provides = ["llm"]
    requires = []  # thinking 可选：最小闭环（无 System2）也能跑

    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        client=None,
    ) -> None:
        self._model_name = model_name
        self._api_base = api_base
        self._temperature = temperature
        self._client = client  # 门面注入的 client（实现 monkey-patch 兼容）

    def setup(self, ctx) -> None:
        if self._client is not None:
            client = self._client
        else:
            thinking = ctx.get("thinking") or {}
            max_tokens = thinking.get("max_tokens", MAX_OUTPUT_TOKENS)
            client = LlmClient(self._model_name, self._api_base, self._temperature, max_tokens)
        ctx.provide("llm", client)
