"""keyan-automation：结构化输入输出的 schema 定义（pydantic 版）。

定义两种结构：
1. 图结点 (Node)：Agent 探索过的一个状态，写入 graph/nodes.jsonl（每行一个）。
2. 动作块 (Action)：模型每次推理输出的结构化动作。

设计原则（与 README 一致）：
- 客观字段（index / changes / git）由 harness 测量与填写，不由模型填写；
- 主观字段（inference / reasoning）由模型填写，只用于规划建议，不用于判定结点好坏；
- 模型输出必须经 parse_action 校验，非法则拒绝（8B 模型极易吐出非法 JSON）。

依赖：pydantic（`pip install pydantic`）。
"""
from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, Field, ValidationError


class ActionKind(str, Enum):
    """模型可选的动作。"""
    ACT = "act"            # 采取一个变化 / 跑一个实验（配合 tool 字段）
    VERIFY = "verify"      # 运行自写的客观检查命令，产出「通过/失败」证据
    ROLLBACK = "rollback"  # 回滚到 best 结点
    RETRIEVE = "retrieve"  # 按需加载某段上下文（精确上下文管理的抓手）
    DONE = "done"          # 声称到达 target


class ToolKind(str, Enum):
    """action=act 时使用的工具（v0.2：对齐 Claude Code 的 validate→execute→result）。"""
    BASH = "bash"          # 在终端执行一条单行命令（带退出码哨兵）
    WRITE = "write"        # 把多行内容写进文件（解决 heredoc 被破坏的问题）
    READ = "read"          # 读一个文件（观察，可重复）


class NodeStatus(str, Enum):
    """结点的验证状态，驱动回滚 / 回环。"""
    EXPLORING = "exploring"
    BEST = "best"          # 当前最优
    REFUTED = "refuted"    # 已证伪，回环时标记，不再进入
    TARGET = "target"      # 到达目标


class Index(BaseModel):
    """客观指标：由 harness 测量，不由模型填写。"""
    metric: str            # 例如 "tokens/s"、"F1"、"accuracy"、"container_pass"
    value: float


class Node(BaseModel):
    """图结点：Agent 探索过的一个状态。

    - index     客观指标（测量值，可信，用于判定结点好坏）
    - changes   客观变化（相对 parent，来自 git diff，与结点转移直接相关）
    - inference 主观推断（模型对"变化 -> 指标变化"因果映射的判断，只用于规划）
    """
    id: str
    parent: str | None = None
    git: str | None = None
    index: Index | None = None
    changes: list[str] = Field(default_factory=list)
    inference: str = ""
    status: NodeStatus = NodeStatus.EXPLORING
    summary: str = ""      # 一句话人话，供 human-readable 视图使用
    exit_code: int | None = None   # 该步命令的退出码（0=成功，非0=失败，None=无命令）
    verified: bool = False         # 该步是否是「已通过」的 verify（done 门控证据）


class Action(BaseModel):
    """模型输出的结构化动作块。"""
    action: ActionKind
    tool: ToolKind = ToolKind.BASH   # 仅 action=act 时生效
    target: str = ""       # 动作对象：bash=命令；write/read=文件路径；verify=检查命令
    content: str = ""      # 仅 action=act + tool=write 时使用：文件完整内容
    change: str = ""       # 具体做什么
    reasoning: str = ""    # 为什么这步能缩小与 target 的差距（写入结点的 inference）


class SchemaError(ValueError):
    """模型输出不符合 schema。"""


def _extract_json(text: str) -> str:
    """从模型输出中提取第一个 JSON 对象。

    8B 模型常在 JSON 外包裹 ```json ... ``` 或多余说明文字。
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    raise SchemaError("模型输出中找不到 JSON 对象")


def parse_action(raw: str) -> Action:
    """解析并校验模型输出的动作块，非法则抛 SchemaError。"""
    extracted = _extract_json(raw)
    try:
        # model_validate_json 同时处理 JSON 语法错误与字段校验错误
        return Action.model_validate_json(extracted)
    except ValidationError as e:
        # 精炼错误信息，便于模型据以下一轮重写
        errors = e.errors()
        detail = "; ".join(
            f"{err.get('loc', ('?',))}: {err.get('msg', '')}" for err in errors[:3]
        )
        raise SchemaError(f"schema 校验失败: {detail}") from e
    except json.JSONDecodeError as e:
        raise SchemaError(f"JSON 解析失败: {e}") from e


def node_from_json(line: str) -> Node:
    """从 JSONL 的一行解析出 Node（用于 load_graph）。"""
    return Node.model_validate_json(line)
