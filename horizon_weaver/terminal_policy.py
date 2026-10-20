"""Typed terminal-tool frontend lowered into the same v1 Action IR.

The familiar act/tool/target protocol can carry brief deliberation before one
final decision. It owns no mutable run state, harness, checks or completion.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import model_validator

from .core import Action, CheckSpec, HistoryQuery, Decision, DecisionInput, Message, PolicyNotes, digest, parse_json
from .policy import parse_action, parse_notes, protocol_error, normalize_workspace_paths, workspace_relative_path
from .ports import GatewayPort


TERMINAL_SYSTEM = """你是处理一个明确终端任务的 Node Agent。
直接输出一个完整 JSON 动作。reasoning 可省略，若使用则不超过 300 字，不重复分析。JSON 是唯一会执行的内容。
格式：{"action":"act|verify|done","tool":"bash|write|read","target":"...","content":"...","change":"...","reasoning":"..."}
act/read：target 为要读取的文件。
act/write：target 为文件，content 为完整文件内容；多行代码也放在 JSON 字符串里。
act/bash：target 为一条 shell 命令，用来执行程序、计算结果、修改权限。
verify：target 为只读的客观断言命令，检查产出是否符合原始要求。
done：申请提交，不带 tool、target 或 content。隐藏评分随后运行，提交不等于成功。
历史账本使用内部 kind/path/command 字段；输出使用上述工具协议，也可输出内部动作格式。

一次一个动作，解题策略由你选择。遵守原始任务的每个条件；推理和“已经完成”的声明不是实际行为。
用工具观察真实错误，测试具体要求；只打印文件或退出 0 不能证明全部要求满足。不要用 echo 掩盖检查的失败。
write 只改内容、不设置权限。verify 不得修改工作区；修改文件后旧检查过期。准备测试数据用 act。
使用环境记录中的 shell 和工作目录。超时已终止可依据错误继续处理；未知结果由运行时停止。
bash / verify 可带 timeout_s 请求等待秒数，实际受任务预算约束。未指定时使用默认等待时间。
可在同一个决策对象附带 notes：{"requirements":[{"ref":"G3","evidence_steps":[3],"uncertainty":"尚未核对的条件"}]}。
G/C 编号定位原始公开目标或约束。notes 最多 12 条，可关联已经执行的 read、bash、verify 步骤；观察不代表验证通过。
条件较多时用便签保留未确认要求；简单任务无需便签，无需额外规划动作。也兼容原文 quote 和旧 check_steps。
较大的下一次响应可在 notes 请求 next_output_tokens、next_timeout_s，受运行时上限限制；只对下一步生效。
原文引用和检查关系是你的主张，不是验收结果。原始合同和独立 Evaluator 决定成功。
若上下文启用了 CHECK INTERFACE，verify/done 可携带 checks；启用 HISTORY INTERFACE 时可查询历史。具体参数见上下文，不需要额外规划或审核动作。
"""


class TerminalInvocation(Message):
    action: Literal["act", "verify", "done", "history", "incomplete"]
    tool: Literal["bash", "write", "read"] | None = None
    target: str = ""
    content: str | None = None
    change: str = ""
    reasoning: str = ""
    timeout_s: float | None = None
    notes: PolicyNotes | None = None
    checks: tuple[CheckSpec, ...] = ()
    check_ids: tuple[str, ...] = ()
    query: HistoryQuery | None = None

    @model_validator(mode="before")
    @classmethod
    def direct_tool_form(cls, data):
        if (isinstance(data, dict) and isinstance(data.get("action"), str)
                and data["action"] in {"bash", "read", "write"}):
            name = data["action"]
            if data.get("tool") not in (None, name):
                raise ValueError("direct tool action conflicts with its tool field")
            return dict(data, action="act", tool=name)
        return data

    @model_validator(mode="after")
    def validate_shape(self):
        if self.action == "act":
            if self.tool is None or not self.target.strip():
                raise ValueError("act requires one tool and a target")
            if self.tool == "write" and self.content is None:
                raise ValueError("write requires complete content")
            if self.tool != "write" and self.content not in (None, ""):
                raise ValueError("only write accepts content")
        elif self.action == "verify":
            if self.tool not in (None, "bash") or not (self.target.strip() or self.checks or self.check_ids) or self.content not in (None, ""):
                raise ValueError("verify requires one assertion command")
        elif self.target or self.content not in (None, ""):
            raise ValueError("done carries no tool operands")
        if self.checks and (self.action not in ('verify', 'done') or self.target):
            raise ValueError('checks require verify/done without target')
        if self.check_ids and (self.action not in ('verify', 'done') or self.target or self.checks):
            raise ValueError('check_ids require verify/done without target/checks')
        if self.query is not None and self.action != 'history':
            raise ValueError('query requires history')
        if self.timeout_s is not None and not (self.action in ("verify", "done") or self.tool == "bash"):
            raise ValueError("timeout_s is only valid for bash/verify")
        return self


def final_object(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if text.endswith("```") and "```json" in text:
        start = text.rfind("```json")
        text = text[:start] + text[start + 7:-3].strip()
    decoder = json.JSONDecoder()
    objects = []
    skip_until = 0
    for index, char in enumerate(text):
        if char != "{" or index < skip_until:
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except ValueError as error:
            # Never extract a nested action from an incomplete outer object.
            if not objects:
                raise ValueError(f"incomplete/invalid decision JSON: {error}") from error
            continue
        skip_until = index + end
        if isinstance(value, dict) and ("action" in value or "kind" in value):
            objects.append((index, index + end))
    if len(objects) != 1 or text[objects[0][1]:].strip():
        raise ValueError("require exactly one complete final decision object")
    start, end = objects[0]
    return text[:start].strip(), text[start:end]


def lower_terminal(raw: str, workspace: str) -> Action:
    prefix, body = final_object(raw)
    data = parse_json(body)
    data = {key: value for key, value in data.items() if key != "notes"}
    # Advisory explanations never prevent an otherwise valid tool invocation.
    for field in ('change', 'reasoning'):
        if field in data and not isinstance(data[field], str):
            data.pop(field)
    data = normalize_workspace_paths(data, workspace)
    if "kind" in data or isinstance(data.get("action"), dict):
        action = parse_action(json.dumps(data))
        return action.model_copy(update={"reasoning": "\n".join(filter(None, (prefix, action.reasoning)))})
    invocation = TerminalInvocation.model_validate(data)
    reasoning = "\n".join(filter(None, (prefix, invocation.change, invocation.reasoning)))
    if invocation.action in ("done", "history", "incomplete"):
        return Action(kind=invocation.action, reasoning=reasoning, checks=invocation.checks,
                      check_ids=invocation.check_ids, query=invocation.query, timeout_s=invocation.timeout_s)
    if invocation.action == "verify" or invocation.tool == "bash":
        return Action(kind="verify" if invocation.action == "verify" else "command",
                      command=invocation.target, reasoning=reasoning, timeout_s=invocation.timeout_s,
                      checks=invocation.checks, check_ids=invocation.check_ids)
    path = workspace_relative_path(invocation.target, workspace)
    return Action(kind=invocation.tool, path=path,
                  content=invocation.content if invocation.tool == "write" else None, reasoning=reasoning)


class TerminalPolicy:
    system_prompt = TERMINAL_SYSTEM

    def __init__(self, gateway: GatewayPort, workspace: str = ""):
        self.gateway, self.workspace = gateway, workspace

    def decide(self, request: DecisionInput, timeout_s: float) -> Decision:
        response = self.gateway.complete(request, timeout_s)
        try:
            action = lower_terminal(response.content, self.workspace)
            data = parse_json(final_object(response.content)[1])
            notes, warning = parse_notes(data.get("notes"))
            return Decision(action=action, notes=notes, error=warning, raw=response.content, usage=response.usage)
        except ValueError as error:
            hint = ("输出截断；省略 reasoning，缩短为一个完整动作，或拆分文件写入。未执行任何动作。" if response.finish_reason == "length"
                    else "动作拒绝；修复以下字段后只输出一个动作。")
            return Decision(raw=response.content, usage=response.usage, error=hint + protocol_error(error))

    def describe(self) -> dict:
        return dict(self.gateway.describe(), frontend="terminal-tools", workspace=self.workspace,
                    system_prompt_hash=digest(self.system_prompt))
