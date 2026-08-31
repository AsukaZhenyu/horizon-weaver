"""system_prompt.py：SystemPromptPlugin —— 提供 ctx.systemPrompt（骨架 + Σ prompt_section）。

骨架（act/done + bash/write/read + 硬规则）来自顶层 system_prompt.NODE_AGENT_SKELETON，
spec/verify/system2 的可选语义由各插件经 ctx.on("prompt-section") 贡献——
摘掉哪个插件，哪段就从 prompt 消失。
"""
from __future__ import annotations

from runtime import Plugin
from system_prompt import NODE_AGENT_SKELETON


class SystemPromptPlugin(Plugin):
    name = "systemPrompt"
    provides = ["systemPrompt"]
    requires = []

    def __init__(self, skeleton: str = NODE_AGENT_SKELETON) -> None:
        self._skeleton = skeleton

    def setup(self, ctx) -> None:
        def assemble() -> str:
            sections = ctx.collect("prompt-section")
            return "\n\n".join([self._skeleton] + sections)

        ctx.provide("systemPrompt", assemble)
