"""v0.2 单元测试：schema 扩展、退出码哨兵、done 门控。

只依赖 schema.py 与 tools.py（不 import terminal-bench），可离线运行。

运行（在 agent/ 目录）：
    python -m pytest test_v02.py      # 需要 pytest
    python test_v02.py                # 免 pytest，直接跑
"""
from __future__ import annotations

from schema import (
    Action, ActionKind, Node, ToolKind, extract_code_block, parse_action, SchemaError,
)
from tools import (
    EXIT_CODE_MARKER, done_gate_rejection, parse_exit_code, with_exit_marker, write_file,
)


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


def test_extract_code_block():
    raw = (
        '{"action":"act","tool":"write","target":"/app/x.py"}\n'
        '```python\nprint(1)\nprint(2)\n```'
    )
    assert extract_code_block(raw) == "print(1)\nprint(2)"


def test_extract_code_block_missing():
    assert extract_code_block('{"action":"act","tool":"bash","target":"ls"}') == ""


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


def test_node_new_fields_default():
    n = Node(id="n1")
    assert n.exit_code is None
    assert n.verified is False


def test_with_exit_marker_appends_sentinel():
    cmd = with_exit_marker("ls -la")
    assert cmd == "ls -la; echo 'KEYAN_EXIT_CODE='$?"
    assert EXIT_CODE_MARKER in cmd


def test_parse_exit_code_takes_last():
    text = "New Terminal Output:\nfoo\nKEYAN_EXIT_CODE=1\n...\nKEYAN_EXIT_CODE=0"
    assert parse_exit_code(text) == 0


def test_with_exit_marker_strips_self_echo_pipe():
    # B8：模型手动加 `; echo 'KEYAN_EXIT_CODE='$?`，须被剥掉，只剩 harness 哨兵
    cmd = with_exit_marker("python -m pip install --upgrade pip; echo 'KEYAN_EXIT_CODE='$?")
    assert cmd == "python -m pip install --upgrade pip; echo 'KEYAN_EXIT_CODE='$?"


def test_with_exit_marker_strips_self_echo_literal_zero():
    # B8：模型写成 `&& echo 'KEYAN_EXIT_CODE=0'`，同样剥掉
    cmd = with_exit_marker("bash /app/process.sh && echo 'KEYAN_EXIT_CODE=0'")
    assert cmd == "bash /app/process.sh; echo 'KEYAN_EXIT_CODE='$?"


def test_with_exit_marker_leaves_plain_command():
    # 正常命令不含哨兵，行为与原来一致
    cmd = with_exit_marker("ls -la")
    assert cmd == "ls -la; echo 'KEYAN_EXIT_CODE='$?"


def test_parse_exit_code_missing():
    assert parse_exit_code("no marker here") is None


def test_done_gate_too_early():
    assert done_gate_rejection([], step=0, min_steps_before_done=2) != ""
    assert done_gate_rejection([], step=1, min_steps_before_done=2) != ""


def test_done_gate_requires_verify_evidence():
    n = Node(id="n1", exit_code=0, verified=False)  # 普通 act，非 verify
    assert done_gate_rejection([n], step=3) != ""


def test_done_gate_passes_with_verify():
    n = Node(id="n1", exit_code=0, verified=True)
    assert done_gate_rejection([n], step=3) == ""


def test_done_gate_rejects_failed_verify():
    n = Node(id="n1", exit_code=1, verified=False)
    assert done_gate_rejection([n], step=3) != ""


def test_write_file_accepts_posix_absolute_path():
    # B4：容器路径 /app/x.py 在 Windows 上必须被接受（不能用 Path.is_absolute 判）
    class FakeSession:
        def __init__(self):
            self.copied = None

        def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
            self.copied = (container_dir, container_filename)

    s = FakeSession()
    r = write_file(s, "/app/cmd.sh", "print(1)\n")
    assert r.exit_code == 0
    assert r.ok
    assert s.copied == ("/app", "cmd.sh")


def test_write_file_rejects_relative_path():
    class FakeSession:
        def copy_to_container(self, **kwargs):
            raise AssertionError("相对路径不该触发 copy_to_container")

    r = write_file(FakeSession(), "app/cmd.sh", "x")
    assert r.exit_code is None
    assert not r.ok


def main() -> int:
    import traceback

    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
