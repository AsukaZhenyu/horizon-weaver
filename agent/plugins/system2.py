"""system2.py：System2Plugin —— 提供 ctx.thinking + 「先思考后输出 JSON」prompt 段。

LlmPlugin 读 ctx.thinking 的 max_tokens 定输出预算——组件经 ctx 传配置，互不引用。
"""
from __future__ import annotations

from engine import MAX_OUTPUT_TOKENS
from runtime import Plugin

_SYSTEM2_SECTION = """## 思考方式（System 2）

可以先用一两句话简要说明「这步做什么、为什么」，然后输出 JSON 动作块。思考有助于规划，但别让思考冲掉 JSON——超过 3 句就很可能让 JSON 出不来而被拒绝；拿不准时直接输出 JSON。"""


class System2Plugin(Plugin):
    name = "system2"
    provides = ["thinking"]
    requires = []

    def __init__(self, max_tokens: int = MAX_OUTPUT_TOKENS) -> None:
        self._max_tokens = max_tokens

    def setup(self, ctx) -> None:
        ctx.provide("thinking", {"max_tokens": self._max_tokens})
        ctx.on("prompt-section", lambda: _SYSTEM2_SECTION)
