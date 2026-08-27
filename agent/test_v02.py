"""v0.2 单元测试：schema 扩展、退出码哨兵、done 门控。

只依赖 schema.py 与 tools.py（不 import terminal-bench），可离线运行。

运行（在 agent/ 目录）：
    python -m pytest test_v02.py      # 需要 pytest
    python test_v02.py                # 免 pytest，直接跑
"""
from __future__ import annotations

from schema import (
    Action, ActionKind, Node, ToolKind, parse_action, SchemaError,
)
from tools import (
    EXIT_CODE_MARKER, done_gate_rejection, parse_exit_code, with_exit_marker,
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
