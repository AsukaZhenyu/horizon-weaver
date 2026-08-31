"""test_schema.py：schema.py 词汇与 JSON 提取。

覆盖：Action 解析（act/verify/write/done + tool 归一化 + 非法 tool 拒绝）、
extract_code_block / _extract_json（System 2 自由思考里取动作 JSON）、
Attempt / Node 模型默认值与序列化。只依赖 schema.py，离线可运行。
"""
from __future__ import annotations

from _runner import run  # 副作用：把 agent/ 加进 sys.path

from schema import (
    Action,
    ActionKind,
    Attempt,
    Node,
    SchemaError,
    ToolKind,
    _extract_json,
    extract_code_block,
    parse_action,
)


# ---------- Action 解析 ----------

def test_action_defaults_to_bash():
    a = parse_action('{"action": "act", "target": "ls", "reasoning": "观察"}')
    assert a.action == ActionKind.ACT
    assert a.tool == ToolKind.BASH
    assert a.content == ""


def test_action_verify_kind():
    a = parse_action('{"action": "verify", "target": "test -f /app/hello.txt"}')
    assert a.action == ActionKind.VERIFY
    assert a.target == "test -f /app/hello.txt"


def test_action_write_tool_with_content():
    raw = '{"action": "act", "tool": "write", "target": "/app/x.py", "content": "print(1)\\nprint(2)"}'
    a = parse_action(raw)
    assert a.tool == ToolKind.WRITE
    assert a.content == "print(1)\nprint(2)"


def test_action_rejects_bad_tool():
    try:
        parse_action('{"action": "act", "tool": "hack"}')
        raise AssertionError("应该拒绝非法 tool")
    except SchemaError:
        pass


def test_action_normalizes_tool_name_in_action_field():
    a = parse_action('{"action": "write", "tool": "write", "target": "/app/x.py", "content": "print(1)"}')
    assert a.action == ActionKind.ACT
    assert a.tool == ToolKind.WRITE
    assert a.content == "print(1)"


def test_action_done_ignores_invalid_tool():
    # B2：done 常带 tool="none"/"N/A"，应被丢弃而非枚举校验失败卡死
    a = parse_action(
        '{"action": "done", "tool": "none", "target": "N/A", "change": "完成", "reasoning": "脚本成功"}'
    )
    assert a.action == ActionKind.DONE
    assert a.tool == ToolKind.BASH  # 回落默认，done 不关心 tool


# ---------- extract_code_block（write 文件内容取最后一个围栏） ----------

def test_extract_code_block():
    raw = (
        '{"action":"act","tool":"write","target":"/app/x.py"}\n'
        '```python\nprint(1)\nprint(2)\n```'
    )
    assert extract_code_block(raw) == "print(1)\nprint(2)"


def test_extract_code_block_missing():
    assert extract_code_block('{"action":"act","tool":"bash","target":"ls"}') == ""


def test_extract_code_block_prefers_last():
    raw = '```python\nprint("thinking example")\n```\n{"action":"act"}\n```python\nreal = 1\n```'
    assert extract_code_block(raw) == 'real = 1'


def test_extract_code_block_skips_json_fence():
    # v0.5.1 回归（jq 任务根因）：动作 JSON 被 ```json 包起来时，旧实现把它当文件
    # 内容写进 /app/cmd.sh。新实现跳过 json 标签围栏，取后面的真实文件内容。
    raw = (
        '```json\n'
        '{"action":"act","tool":"write","target":"/app/cmd.sh","change":"写 jq 脚本"}\n'
        '```\n'
        '```bash\n'
        "jq '.username' /app/active_users_report.json > /app/report.json\n"
        '```'
    )
    assert extract_code_block(raw) == (
        "jq '.username' /app/active_users_report.json > /app/report.json"
    )


def test_extract_code_block_only_json_fence_returns_empty():
    # 只有 ```json 动作围栏、没有文件内容围栏 → 返回空，交给 write 的「缺内容」反馈
    raw = '```json\n{"action":"act","tool":"write","target":"/app/x.py"}\n```'
    assert extract_code_block(raw) == ""


# ---------- parse_action 里的 JSON 提取（B1：围栏代码块干扰括号配对） ----------

def test_extract_json_ignores_trailing_fence():
    # B1：JSON 后紧跟含花括号的围栏代码块，括号配对应只取第一个 JSON 对象
    raw = (
        '{"action":"act","tool":"write","target":"/app/x.py"}\n'
        '```python\nd = {"a": 1}\nreturn d\n```'
    )
    a = parse_action(raw)
    assert a.action == ActionKind.ACT
    assert a.tool == ToolKind.WRITE
    assert a.target == "/app/x.py"


def test_extract_json_handles_escaped_quote_in_string():
    raw = '{"action":"act","tool":"bash","target":"echo \\"hi\\" \\"there\\""}'
    a = parse_action(raw)
    assert a.target == 'echo "hi" "there"'


# ---------- _extract_json（System 2：自由思考 + 动作 JSON） ----------

def test_extract_json_system2_prefers_last_action():
    # 思考文字里带了一个 {"action":"act"} 的示例，真正的动作 JSON 在最后
    raw = (
        '我先想想：pip 报错说明包目录没了，示例 {"action":"act"}\n'
        '所以删掉孤儿元数据。\n'
        '{"action":"done","reasoning":"已修复"}'
    )
    assert _extract_json(raw) == '{"action":"done","reasoning":"已修复"}'


def test_extract_json_write_style_action_first():
    raw = '{"action":"act","tool":"write","target":"/app/x.py"}\n```python\nprint({"k": 1})\n```'
    assert _extract_json(raw).startswith('{"action":"act"')


def test_extract_json_braces_in_thinking_skipped():
    # 思考里的普通大括号（无 "action" 键）不该干扰提取
    raw = '思考：dict 示例 {"k": {"nested": 1}} 不重要\n{"action":"verify","target":"x"}'
    assert _extract_json(raw) == '{"action":"verify","target":"x"}'


def test_extract_json_no_action_falls_back_first():
    assert _extract_json('{"foo": "bar"}') == '{"foo": "bar"}'


def test_extract_json_unclosed_raises():
    try:
        _extract_json('{"action":"done"')
    except SchemaError:
        return
    raise AssertionError("应当抛出 SchemaError")


# ---------- Node / Attempt 模型 ----------

def test_node_new_fields_default():
    n = Node(id="n1")
    assert n.exit_code is None
    assert n.verified is False


def test_attempt_defaults():
    a = Attempt(step=1)
    assert a.action_summary == ""
    assert a.target == ""
    assert a.exit_code is None
    assert a.result == ""
    assert a.inference == ""
    assert a.verified is False


def test_attempt_serialization_roundtrip():
    a = Attempt(step=3, action_summary="bash pip install pandas", target="pip install pandas",
                exit_code=127, result="not found", inference="命令未找到(127)", verified=False)
    got = Attempt.model_validate_json(a.model_dump_json())
    assert got == a
    assert got.exit_code == 127


if __name__ == "__main__":
    raise SystemExit(run(globals()))
