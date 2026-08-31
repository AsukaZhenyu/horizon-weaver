"""plugins 包：Node Agent 的「即抛即用」插件集（v0.5，参考 dsh/Cordis）。

把 v0.4 硬编码在 node_agent.py 一个类里的能力，拆成可插拔组件，全部通过
共享 Context（runtime.Context）交互——组件只认 ctx 上的服务 key 与事件名，
互不 import 对方，实现低耦合高内聚。

模块与插件的对应（每个插件一个文件，文件名 = 插件名）：
- llm.py            LlmPlugin（ctx.llm；含 LlmClient 本地 llama-server 客户端）
- tools.py          ToolsPlugin（ctx.tools：bash/write/read）
- system2.py        System2Plugin（ctx.thinking + 「先思考后输出 JSON」prompt 段）
- system_prompt.py  SystemPromptPlugin（ctx.systemPrompt：骨架 + Σ prompt_section）
- verify.py         VerifyPlugin（VERIFY：自由断言 + 去重 + done 证据门控，v0.3 语义）
- spec.py           SpecPlugin（SPEC：预提交样例 + done 全过门控，v0.4 语义，**默认不装**）
- agent_loop.py     AgentLoopPlugin（ctx.agentLoop：主循环，决策委托给事件）
- types.py          HandleResult / NodeResult / RunState（运行时契约，跨插件共享）

组件间交互范式（用户要的「组件与 ctx / 组件间交互」）：
- System2Plugin provide ctx.thinking，LlmPlugin 读 ctx.thinking 定 token 预算——
  组件经 ctx 传配置，互不引用。
- SpecPlugin provide ctx.spec；VerifyPlugin 认领 VERIFY 时读 ctx.get('spec') 判断
  自己在「自由断言」还是「必须匹配 spec 样例」模式下——组件经 ctx 读对方，不直接 import。

「摘掉 spec」= 不装 SpecPlugin；「加 websearch」= 装一个认领 handle-action 的工具插件。
A/B 对比不改循环源码，可逆副作用保证卸载干净。

本文件 re-export 公共面，并提供 build_default_plugins() 默认装配。
"""
from __future__ import annotations

from .agent_loop import AgentLoopPlugin
from .llm import LlmClient, LlmPlugin
from .spec import SpecPlugin
from .system2 import System2Plugin
from .system_prompt import SystemPromptPlugin
from .tools import ToolsPlugin
from .types import HandleResult, NodeResult, RunState
from .verify import VerifyPlugin

__all__ = [
    "AgentLoopPlugin",
    "HandleResult",
    "LlmClient",
    "LlmPlugin",
    "NodeResult",
    "RunState",
    "SpecPlugin",
    "System2Plugin",
    "SystemPromptPlugin",
    "ToolsPlugin",
    "VerifyPlugin",
    "build_default_plugins",
]


def build_default_plugins(
    enable_spec: bool = False,
    model_name: str = "qwen3.5-9b",
    api_base: str = "http://127.0.0.1:8080/v1",
    temperature: float = 0.2,
    client=None,
) -> list:
    """默认插件集：llm + tools + systemPrompt + system2 + verify + agentLoop。

    spec 默认关（v0.4 结论：spec-first 对 9B 模型净负）。开启 = enable_spec=True，
    或自建 `NodeAgent(plugins=[..., SpecPlugin()])`。

    顺序即 prompt_section 的汇总顺序：思考 → (spec) → verify。SpecPlugin 插在 verify
    之前，让「先 spec 后 verify」的语义在 prompt 里也保持这个先后。
    """
    plugins = [
        ToolsPlugin(),
        System2Plugin(),
        VerifyPlugin(),
        LlmPlugin(model_name=model_name, api_base=api_base, temperature=temperature, client=client),
        SystemPromptPlugin(),
        AgentLoopPlugin(),
    ]
    if enable_spec:
        plugins.insert(2, SpecPlugin())  # 在 Verify 之前，prompt 顺序「思考→spec→verify」
    return plugins
