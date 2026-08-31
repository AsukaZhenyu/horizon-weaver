"""test_engine.py：engine.py 的引擎自动推断 + 上下文组织。

覆盖：extract_error_line / summarize_result（含 Bug A、B10 回归）/
attempt_summary / summarize_attempt / build_context（账本、预算截断、反馈、完整输出、
spec 样例）/ make_attempt / _verify_repeat_feedback。只依赖 schema.py / tools.py /
engine.py，离线可运行。
"""
from __future__ import annotations

from _runner import run  # 副作用：把 agent/ 加进 sys.path

from node_agent import (
    MAX_LEDGER_ENTRIES,
    _verify_repeat_feedback,
    attempt_summary,
    build_context,
    extract_error_line,
    summarize_attempt,
    summarize_result,
)
from schema import Action, ActionKind, Attempt, SpecCase, ToolKind
from tools import ToolResult


def _act(**kw) -> Action:
    return Action(action=ActionKind.ACT, **kw)


# ---------- extract_error_line ----------

def test_extract_error_line_prefers_last_error():
    out = (
        "Traceback (most recent call last):\n"
        "  File \"/app/x.py\", line 5, in <module>\n"
        "    import pandas\n"
        "ModuleNotFoundError: No module named 'pandas'"
    )
    assert extract_error_line(out) == "ModuleNotFoundError: No module named 'pandas'"


def test_extract_error_line_falls_back_last_line():
    assert extract_error_line("foo\nbar\nbaz") == "baz"


def test_extract_error_line_empty():
    assert extract_error_line("") == ""
    assert extract_error_line("   \n  ") == ""


# ---------- summarize_result ----------

def test_summarize_result_error_first_on_failure():
    out = "ok\nERROR: something broke\nlast line"
    assert summarize_result(out, exit_code=1) == "ERROR: something broke"


def test_summarize_result_last_line_on_success():
    assert summarize_result("a\nb\nc", exit_code=0) == "c"


def test_summarize_result_empty():
    assert summarize_result("", exit_code=1) == ""


def test_summarize_result_success_strips_prompt_and_marker():
    # Bug A 回归：成功命令的「最后一行」是终端提示符，真实输出在中间，不能返回提示符
    out = (
        "python -m ensurepip --upgrade; echo 'KEYAN_EXIT_CODE='$?; tmux wait -S done\n"
        "Requirement already satisfied: pip in /usr/local/lib/python3.13/site-packages (24.3.1)\n"
        "KEYAN_EXIT_CODE=0\n"
        "root@5a2279e62ed5:/app#"
    )
    assert summarize_result(out, exit_code=0) == \
        "Requirement already satisfied: pip in /usr/local/lib/python3.13/site-packages (24.3.1)"


def test_summarize_result_success_filters_noise():
    # 过滤掉 Looking in links / WARNING 噪声，取真正结果行
    out = (
        "Looking in links: /tmp/tmpxxx\n"
        "Requirement already satisfied: pip in /usr/local/lib/python3.13/site-packages (24.3.1)\n"
        "WARNING: Running pip as the 'root' user can result in broken permissions\n"
        "KEYAN_EXIT_CODE=0\n"
        "root@abc:/app#"
    )
    assert summarize_result(out, exit_code=0) == \
        "Requirement already satisfied: pip in /usr/local/lib/python3.13/site-packages (24.3.1)"


def test_summarize_result_success_all_noise_falls_back():
    # 全部是噪声行时退回原样（保证至少有东西可看），取最后一条
    out = "WARNING: only a warning here\nKEYAN_EXIT_CODE=0\nroot@abc:/app#"
    assert summarize_result(out, exit_code=0) == "WARNING: only a warning here"


def test_summarize_result_strips_harness_header():
    # terminal-bench 的 get_incremental_output 包了 "New Terminal Output:" 表头；
    # 空输出命令（如 find 无结果）剥掉表头后应为空串，而不是把表头当结果。
    out = (
        "New Terminal Output:\n"
        "find /x -name pip -type d; echo 'KEYAN_EXIT_CODE='$?; tmux wait -S done\n"
        "KEYAN_EXIT_CODE=0\n"
        "root@abc:/app#"
    )
    assert summarize_result(out, exit_code=0) == ""


def test_summarize_result_strips_sentinel_appended_to_content():
    # B10：文件末行无换行时，退出码哨兵拼到内容行尾（`echo okKEYAN_EXIT_CODE=0`）。
    # 不能整行丢弃（会吞掉真实内容），只剥哨兵值——否则 read 出来的文件内容缺行，
    # 或模型把哨兵误判成「文件里混入了 KEYAN_EXIT_CODE」，反复 rewrite 去移除它。
    out = (
        'cat "/app/x.sh"; echo \'KEYAN_EXIT_CODE=\'$?; tmux wait -S done\n'
        "echo okKEYAN_EXIT_CODE=0\n"
    )
    assert summarize_result(out, exit_code=0) == "echo ok"


# ---------- attempt_summary ----------

def test_attempt_summary_bash():
    assert attempt_summary(_act(tool=ToolKind.BASH, target="pip install pandas")) == "bash pip install pandas"


def test_attempt_summary_write():
    assert attempt_summary(_act(tool=ToolKind.WRITE, target="/app/x.py")) == "write /app/x.py"


def test_attempt_summary_read():
    assert attempt_summary(_act(tool=ToolKind.READ, target="/app/data.csv")) == "read /app/data.csv"


def test_attempt_summary_verify():
    a = Action(action=ActionKind.VERIFY, target="test -f /app/out.json")
    assert attempt_summary(a) == "verify test -f /app/out.json"


# ---------- summarize_attempt ----------

def test_summarize_attempt_bash_success():
    a = _act(tool=ToolKind.BASH, target="ls")
    assert summarize_attempt(a, ToolResult(output="...", exit_code=0)) == "成功（输出：...）"


def test_summarize_attempt_bash_success_no_output():
    a = _act(tool=ToolKind.BASH, target="true")
    # 只有哨兵和提示符，剥掉后无真实输出
    out = "true; echo 'KEYAN_EXIT_CODE='$?; tmux wait -S done\nKEYAN_EXIT_CODE=0\nroot@abc:/app#"
    assert summarize_attempt(a, ToolResult(output=out, exit_code=0)) == "成功（无输出）"


def test_summarize_attempt_bash_failure():
    a = _act(tool=ToolKind.BASH, target="pip install pandas")
    r = ToolResult(output="ModuleNotFoundError: No module named 'pandas'", exit_code=1)
    assert summarize_attempt(a, r) == "失败(1) ModuleNotFoundError: No module named 'pandas'"


def test_summarize_attempt_command_not_found():
    a = _act(tool=ToolKind.BASH, target="nonsense-cmd")
    assert summarize_attempt(a, ToolResult(output="nonsense-cmd: command not found", exit_code=127)) \
        == "命令未找到(127)：目标命令可能未安装或不在 PATH"


def test_summarize_attempt_no_exit_code():
    a = _act(tool=ToolKind.BASH, target="sleep 10")
    assert summarize_attempt(a, ToolResult(output="", exit_code=None)) \
        == "无退出码：命令可能超时或输出被截断"


def test_summarize_attempt_write():
    a = _act(tool=ToolKind.WRITE, target="/app/x.py")
    assert summarize_attempt(a, ToolResult(output="[已写入 /app/x.py，123 字符]", exit_code=0)) \
        == "[已写入 /app/x.py，123 字符]"


def test_summarize_attempt_write_failure():
    a = _act(tool=ToolKind.WRITE, target="relative.py")
    r = ToolResult(output="[write 需要容器内绝对路径（以 / 开头）]", exit_code=None, ok=False)
    assert summarize_attempt(a, r) == "[write 需要容器内绝对路径（以 / 开头）]"


def test_summarize_attempt_read():
    a = _act(tool=ToolKind.READ, target="/app/data.csv")
    assert summarize_attempt(a, ToolResult(output="a,b\n1,2", exit_code=0)) == "已读（退出码 0）"


def test_summarize_attempt_verify_success():
    a = Action(action=ActionKind.VERIFY, target="test -f /app/out.json")
    assert summarize_attempt(a, ToolResult(output="", exit_code=0)) == "成功（断言通过）"


# ---------- build_context ----------

def test_build_context_empty_shows_terminal():
    ctx = build_context("写一个脚本", [], "root@container:~#", "")
    assert "## 目标" in ctx
    assert "写一个脚本" in ctx
    assert "## 当前终端" in ctx
    assert "root@container:~#" in ctx
    assert "## 已尝试" not in ctx


def test_build_context_with_attempts_shows_ledger():
    attempts = [
        Attempt(step=1, action_summary="write /app/x.py", result="", inference="[已写入 /app/x.py，123 字符]"),
        Attempt(step=2, action_summary="bash pip install pandas", result="not found",
                inference="失败(127) not found"),
    ]
    ctx = build_context("装 pandas", attempts, "", "")
    assert "## 已尝试（动作 → 结果 → 推断）" in ctx
    assert "1. write /app/x.py → [已写入 /app/x.py，123 字符]" in ctx
    assert "2. bash pip install pandas → not found → 失败(127) not found" in ctx
    assert "## 当前终端" not in ctx  # 有 attempts 后不再展示初始屏


def test_build_context_budget_truncation():
    attempts = [
        Attempt(step=i, action_summary=f"bash cmd{i}", result=f"r{i}", inference=f"inf{i}")
        for i in range(1, MAX_LEDGER_ENTRIES + 6)  # 35 条，超预算 5 条
    ]
    ctx = build_context("任务", attempts, "", "")
    omitted = len(attempts) - MAX_LEDGER_ENTRIES
    assert f"…(前 {omitted} 步省略)…" in ctx
    # 最旧 5 条被压缩，最近 30 条全在（用整行比较，避免 "1. bash cmd1" 撞上 "11. bash cmd11"）
    assert "1. bash cmd1 → r1 → inf1" not in ctx
    assert "6. bash cmd6 → r6 → inf6" in ctx
    assert f"{MAX_LEDGER_ENTRIES + 5}. bash cmd{MAX_LEDGER_ENTRIES + 5} → r{MAX_LEDGER_ENTRIES + 5} → inf{MAX_LEDGER_ENTRIES + 5}" in ctx


def test_build_context_feedback():
    ctx = build_context("任务", [], "", "拒绝执行：命令已执行过")
    assert "## 系统反馈" in ctx
    assert "拒绝执行：命令已执行过" in ctx


def test_build_context_includes_raw_output():
    attempts = [
        Attempt(step=1, action_summary="bash which python", result="ModuleNotFoundError: No module named 'pip'",
                inference="失败(1)", raw_output="/usr/local/bin/python\nPython 3.13.1\nModuleNotFoundError: No module named 'pip'"),
    ]
    ctx = build_context("任务", attempts, "", "")
    assert "完整输出:" in ctx
    assert "/usr/local/bin/python" in ctx
    assert "Python 3.13.1" in ctx
    # 摘要行仍在，与完整输出并存
    assert "→ ModuleNotFoundError: No module named 'pip' → 失败(1)" in ctx


def test_build_context_shows_spec_cases():
    cases = [
        SpecCase(
            command="python3 -c \"assert eval(open('/app/output.txt').read().strip())==462\"",
            clause="值等于 462",
            category="target",
        ),
    ]
    ctx = build_context("算出 462", [], "", "", cases)
    assert "## 验收测试样例" in ctx
    assert "assert eval" in ctx
    assert "值等于 462" in ctx


# ---------- make_attempt ----------

def test_make_attempt_populates_raw_output():
    from node_agent import NodeAgent

    agent = NodeAgent()
    a = _act(tool=ToolKind.BASH, target="python -m ensurepip --upgrade")
    out = (
        "New Terminal Output:\n"
        "python -m ensurepip --upgrade; echo 'KEYAN_EXIT_CODE='$?; tmux wait -S done\n"
        "Requirement already satisfied: pip in /usr/local/lib/python3.13/site-packages (24.3.1)\n"
        "KEYAN_EXIT_CODE=0\n"
        "root@abc:/app#"
    )
    attempt = agent._make_attempt(1, a, ToolResult(output=out, exit_code=0))
    assert attempt.result == \
        "Requirement already satisfied: pip in /usr/local/lib/python3.13/site-packages (24.3.1)"
    assert attempt.raw_output == \
        "Requirement already satisfied: pip in /usr/local/lib/python3.13/site-packages (24.3.1)"


def test_make_attempt_raw_output_multi_line():
    from node_agent import NodeAgent

    agent = NodeAgent()
    a = _act(tool=ToolKind.BASH, target="which python && python --version && pip --version")
    out = (
        "New Terminal Output:\n"
        "which python && python --version && pip --version; echo 'KEYAN_EXIT_CODE='$?; tmux wait -S done\n"
        "/usr/local/bin/python\n"
        "Python 3.13.1\n"
        "Traceback (most recent call last):\n"
        "  File \"/usr/local/bin/pip\", line 5, in <module>\n"
        "    from pip._internal.cli.main import main\n"
        "ModuleNotFoundError: No module named 'pip'\n"
        "KEYAN_EXIT_CODE=1\n"
        "root@abc:/app#"
    )
    attempt = agent._make_attempt(1, a, ToolResult(output=out, exit_code=1))
    # result 是最后一条错误行（摘要），raw_output 是剥噪声后的完整输出
    assert attempt.result == "ModuleNotFoundError: No module named 'pip'"
    assert attempt.raw_output.startswith("/usr/local/bin/python")
    assert "Python 3.13.1" in attempt.raw_output
    assert "from pip._internal.cli.main import main" in attempt.raw_output


# ---------- _verify_repeat_feedback（Bug C：重复 verify 的反馈） ----------

def test_verify_repeat_feedback_passed_guides_done():
    fb = _verify_repeat_feedback("python -m pip --version", 0)
    assert '"done"' in fb
    assert "已通过" in fb


def test_verify_repeat_feedback_failed_guides_fix():
    fb = _verify_repeat_feedback("python -m pip --version", 1)
    assert "失败" in fb
    assert "先修复" in fb


if __name__ == "__main__":
    raise SystemExit(run(globals()))
