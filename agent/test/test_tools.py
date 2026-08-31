"""test_tools.py：tools.py 纯函数工具层。

覆盖：退出码哨兵（with_exit_marker / parse_exit_code，含 B8 剥自回显哨兵）、
done 证据门控（done_gate_rejection）、write_file（B4 容器绝对路径 / B9 LF 保持）、
反表面检查（is_surface_check）与 spec 样例校验（validate_spec）。只依赖 schema.py
与 tools.py，离线可运行。
"""
from __future__ import annotations

from _runner import run  # 副作用：把 agent/ 加进 sys.path

from schema import Node, SpecCase
from tools import (
    EXIT_CODE_MARKER,
    MAX_SPEC_CASES,
    done_gate_rejection,
    is_surface_check,
    parse_exit_code,
    validate_spec,
    with_exit_marker,
    write_file,
)


# ---------- 退出码哨兵 ----------

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


# ---------- done 证据门控 ----------

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


# ---------- write_file ----------

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


def test_write_file_preserves_lf_newlines():
    # B9：write_text 在 Windows 文本模式默认把 \n 翻成 \r\n，写进 Linux 容器的文件
    # 会带 \r，跑脚本时报 `$'\r': command not found`（fix-permissions 冒烟实测）。
    # write_file 必须用 write_bytes 保持 LF。
    from pathlib import Path

    class FakeSession:
        def __init__(self):
            self.written_bytes = None

        def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
            self.written_bytes = Path(paths[0]).read_bytes()

    s = FakeSession()
    content = "#!/bin/bash\n\necho hi\n"
    r = write_file(s, "/app/x.sh", content)
    assert r.exit_code == 0
    assert s.written_bytes == content.encode("utf-8")  # 原始 LF
    assert b"\r" not in s.written_bytes


# ---------- is_surface_check（v0.4 反表面检查） ----------

def test_is_surface_check_true_and_echo():
    assert is_surface_check("true") is True
    assert is_surface_check(":") is True
    assert is_surface_check("echo done") is True
    assert is_surface_check("echo 'ok'") is True


def test_is_surface_check_pure_observation():
    assert is_surface_check("ls") is True
    assert is_surface_check("cat /app/x.py") is True
    assert is_surface_check("head /app/data.csv") is True


def test_is_surface_check_existence_only():
    assert is_surface_check("test -f /app/output.txt") is True
    assert is_surface_check("test -s /app/output.txt") is True
    assert is_surface_check("[ -f /app/x ]") is True


def test_is_surface_check_not_surface():
    assert is_surface_check(
        "python3 -c \"assert eval(open('/app/output.txt').read().strip())==462\""
    ) is False
    assert is_surface_check("test -f /app/out.json && grep -q expected /app/out.json") is False
    assert is_surface_check("diff <(jq '.a' /app/in.json) /app/expected.json") is False
    assert is_surface_check(
        "python3 -m pip install somepkg && python3 -c 'import somepkg'"
    ) is False
    # cat 带 pipe = 语义检查（grep 具体串），不是纯观察
    assert is_surface_check("cat /app/x | grep -q expected") is False


def test_is_surface_check_jq_dump():
    # v0.5.1：jq 只打印不比对（无 -e / 无管道）= 观察，不是断言 → 表面检查
    assert is_surface_check("jq '.a' /app/in.json") is True
    # 带 -e / 带管道 = 可判错的断言，不是表面检查
    assert is_surface_check("jq -e '.a == 1' /app/in.json") is False
    assert is_surface_check("jq '.a' /app/in.json | grep -q 1") is False


# ---------- validate_spec ----------

def _case(cmd, clause="", category=""):
    return SpecCase(command=cmd, clause=clause, category=category)


def test_validate_spec_empty():
    assert validate_spec([]) is not None
    assert validate_spec(None) is not None


def test_validate_spec_rejects_surface():
    assert validate_spec([_case("echo ok")]) is not None
    assert validate_spec([_case("test -f /app/x")]) is not None


def test_validate_spec_rejects_duplicate():
    assert validate_spec([_case("grep -q x /app/y"), _case("grep -q x /app/y")]) is not None


def test_validate_spec_rejects_too_many():
    cases = [_case(f"grep -q {i} /app/x") for i in range(MAX_SPEC_CASES + 1)]
    assert validate_spec(cases) is not None


def test_validate_spec_accepts_valid():
    cases = [
        _case(
            "python3 -c \"assert eval(open('/app/output.txt').read().strip())==462\"",
            "值等于 462",
            "target",
        ),
        _case("python3 -m pip install somepkg && python3 -c 'import somepkg'", "pip 可装包", "behavior"),
    ]
    assert validate_spec(cases) is None


if __name__ == "__main__":
    raise SystemExit(run(globals()))
