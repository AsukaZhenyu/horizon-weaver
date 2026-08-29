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


class Attempt(BaseModel):
    """一次尝试的完整记录：action（做了什么）+ result（客观结果）+ inference（引擎自动推断）。

    Node Agent 上下文组织的核心单元——所有尝试都以 (action, result, inference)
    三元组进上下文；其中 inference 由引擎确定性生成（尤其对失败尝试），
    不让弱模型自己反思。供 NodeAgent 记录、高层 agent 未来消费。

    - action_summary  动作摘要，如 "bash pip install pandas" / "write /app/x.py"
    - target          动作对象（命令 / 文件路径）
    - exit_code       该步退出码（0=成功，非0=失败，None=无命令/超时）
    - result          结果摘要（错误优先、截断）
    - raw_output      命令完整输出（剥掉 harness 噪声、不做摘要，截断到 RAW_LIMIT）。
                      与 result（一句话摘要）互补：模型既能快速扫账本，又能看完整细节。
    - inference       引擎自动生成的推断：这一步意味着什么、下一步往哪走
    - verified        是否「已通过」的 verify（done 门控证据）
    """
    step: int
    action_summary: str = ""
    target: str = ""
    exit_code: int | None = None
    result: str = ""
    raw_output: str = ""
    inference: str = ""
    verified: bool = False


class SchemaError(ValueError):
    """模型输出不符合 schema。"""


def _balanced_object(text: str, start: int) -> str | None:
    """从 start（指向 '{'）做括号配对，返回闭合的 {...} 字符串；未闭合返回 None。"""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _extract_json(text: str) -> str:
    """从模型输出中提取动作 JSON 对象（括号配对，容忍包围的自由文本）。

    支持两种输出形态：
    - System 2：模型先自由思考（思考文字里可能含代码块 / JSON 示例），最后输出动作 JSON；
    - write：动作 JSON 在前，文件内容在后面的 ``` 围栏代码块里（可能含大括号）。

    策略：从后往前找每个 `{"action"` 起点做括号配对，返回第一个配对成功的
    （即模型最终要执行的那个动作——动作约定写在最后）；找不到 `{"action"` 时
    退回第一个 `{` 配对。这样思考文字里的杂散大括号 / JSON 示例不会干扰提取。
    """
    text = text.strip()
    starts = [m.start() for m in re.finditer(r'\{\s*"action"', text)]
    for start in reversed(starts):
        obj = _balanced_object(text, start)
        if obj is not None:
            return obj
    first = text.find("{")
    if first == -1:
        raise SchemaError("模型输出中找不到 JSON 对象")
    obj = _balanced_object(text, first)
    if obj is None:
        raise SchemaError("JSON 对象未闭合（缺少配对的 }）")
    return obj


_TOOL_NAMES = {"bash", "write", "read"}


def _normalize_action(data: dict) -> dict:
    """弱模型常见字段错位的归一化。

    1. 把工具名写进 action 字段（如 {"action":"write"}）→ 视为 act + tool=write；
    2. 非 act 动作（done/verify/rollback/retrieve）的 tool 字段无意义，
       弱模型常填 "none"/"N/A"/"null"，直接丢弃，避免 ToolKind 枚举校验失败卡死 done。
    """
    if not isinstance(data, dict):
        return data
    action = data.get("action")
    if isinstance(action, str) and action in _TOOL_NAMES:
        data = dict(data)
        data["action"] = ActionKind.ACT.value
        data["tool"] = action
        return data
    if action is not None and action != ActionKind.ACT.value:
        data = dict(data)
        data.pop("tool", None)
    return data


def parse_action(raw: str) -> Action:
    """解析并校验模型输出的动作块，非法则抛 SchemaError。"""
    extracted = _extract_json(raw)
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError as e:
        hint = ""
        if "control character" in str(e) or "Unterminated string" in str(e):
            hint = "（多行内容不要塞进 content 字段——把文件完整内容放进 JSON 之后的 ``` 围栏代码块）"
        raise SchemaError(f"JSON 解析失败: {e}{hint}") from e
    try:
        # 先归一化弱模型的常见字段错位，再校验
        return Action.model_validate(_normalize_action(data))
    except ValidationError as e:
        # 精炼错误信息，便于模型据以下一轮重写
        errors = e.errors()
        detail = "; ".join(
            f"{err.get('loc', ('?',))}: {err.get('msg', '')}" for err in errors[:3]
        )
        raise SchemaError(f"schema 校验失败: {detail}") from e


_CODE_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


def extract_code_block(raw: str) -> str:
    """从模型输出里提取最后一个 fenced code block 的内容（write 工具用）。

    write 动作的 JSON 之后会紧跟 ```python ... ``` 围栏放文件完整内容；
    取**最后一个**围栏块——System 2 下模型思考文字里可能含 ``` 示例，
    文件内容始终是动作 JSON 之后的那个（最后一个）。
    内容原样返回（不做 JSON 转义），避免多行内容塞进 JSON 字符串导致解析失败。
    """
    blocks = _CODE_FENCE_RE.findall(raw)
    if not blocks:
        return ""
    return blocks[-1].rstrip("\n")


def node_from_json(line: str) -> Node:
    """从 JSONL 的一行解析出 Node（用于 load_graph）。"""
    return Node.model_validate_json(line)
