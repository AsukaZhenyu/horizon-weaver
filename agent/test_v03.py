"""v0.3 单元测试：Node Agent 的引擎自动推断 + 上下文组织 + Attempt。

只依赖 schema.py / tools.py / node_agent.py（不 import terminal-bench），可离线运行。

运行（在 agent/ 目录）：
    python -m pytest test_v03.py      # 需要 pytest
    python test_v03.py                # 免 pytest，直接跑
"""
from __future__ import annotations

from node_agent import (
    MAX_LEDGER_ENTRIES,
    _verify_repeat_feedback,
    attempt_summary,
    build_context,
    extract_error_line,
    summarize_attempt,
    summarize_result,
)
from schema import (
    Action,
    ActionKind,
    Attempt,
    SchemaError,
    SpecCase,
    ToolKind,
    _extract_json,
    extract_code_block,
)
from tools import MAX_SPEC_CASES, ToolResult, is_surface_check, validate_spec


def _act(**kw) -> Action:
    return Action(action=ActionKind.ACT, **kw)


# ---------- Attempt ----------

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


def test_context_log_written_per_step():
    import tempfile
    from pathlib import Path

    from node_agent import CONTEXT_LOG_NAME, NodeAgent

    class FakeSession:
        def send_keys(self, keys, block=True):
            pass

        def get_incremental_output(self):
            return "KEYAN_EXIT_CODE=0\nsome output"

        def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
            pass

        def get_asciinema_timestamp(self):
            return 0.0

    responses = [
        '{"action":"spec","cases":[{"command":"grep -q 462 /app/output.txt","clause":"输出 462","category":"target"}]}',
        '{"action":"act","tool":"bash","target":"echo hi"}',
        '{"action":"done"}',
    ]

    def _fake_call(messages):
        raw = responses.pop(0) if responses else '{"action":"done"}'
        return raw, {"prompt_tokens": 1, "completion_tokens": 1}

    agent = NodeAgent()
    agent._call_model_with_retry = _fake_call

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        result = agent.run("写一个测试任务", FakeSession(), log_dir)

        log_file = log_dir / CONTEXT_LOG_NAME
        assert log_file.exists()
        text = log_file.read_text(encoding="utf-8")
        assert "## 任务" in text
        assert "写一个测试任务" in text
        assert "### Step 0" in text
        assert "### Step 1" in text
        assert "### Step 2" in text
        assert "喂给模型的上下文" in text
        assert "模型原始输出" in text
        assert "解析出的动作" in text
        assert "grep -q 462 /app/output.txt" in text  # spec 样例命令进了日志
        assert result.termination == "done"


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


# ---------- extract_code_block（write 文件内容取最后一个围栏） ----------

def test_extract_code_block_prefers_last():
    raw = '```python\nprint("thinking example")\n```\n{"action":"act"}\n```python\nreal = 1\n```'
    assert extract_code_block(raw) == 'real = 1'


# ---------- _verify_repeat_feedback（Bug C：重复 verify 的反馈） ----------

def test_verify_repeat_feedback_passed_guides_done():
    fb = _verify_repeat_feedback("python -m pip --version", 0)
    assert '"done"' in fb
    assert "已通过" in fb


def test_verify_repeat_feedback_failed_guides_fix():
    fb = _verify_repeat_feedback("python -m pip --version", 1)
    assert "失败" in fb
    assert "先修复" in fb


# ---------- v0.4 spec-first：反表面检查 + spec 校验 + 门控 ----------

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


def test_run_requires_spec_first():
    from node_agent import NodeAgent

    class FakeSession:
        def send_keys(self, keys, block=True):
            pass

        def get_incremental_output(self):
            return "KEYAN_EXIT_CODE=0\n"

        def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
            pass

        def get_asciinema_timestamp(self):
            return 0.0

    # 模型第一步就输出 act（非法），引擎应反复拒绝直至卡死，而不是放行
    responses = ['{"action":"act","tool":"bash","target":"ls"}']

    def _fake_call(messages):
        return responses[0], {"prompt_tokens": 1, "completion_tokens": 1}

    agent = NodeAgent()
    agent._call_model_with_retry = _fake_call
    result = agent.run("任务", FakeSession())
    assert result.termination == "stuck"


def test_run_spec_first_done_flow():
    from node_agent import NodeAgent

    class FakeSession:
        def __init__(self):
            self.keys = []

        def send_keys(self, keys, block=True):
            self.keys.append(keys)

        def get_incremental_output(self):
            return "KEYAN_EXIT_CODE=0\nok"

        def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
            pass

        def get_asciinema_timestamp(self):
            return 0.0

    responses = [
        '{"action":"spec","cases":[{"command":"python3 -c \'print(1)\'","clause":"脚本能跑","category":"behavior"}]}',
        '{"action":"act","tool":"bash","target":"echo hi"}',
        '{"action":"done"}',
    ]

    def _fake_call(messages):
        raw = responses.pop(0) if responses else '{"action":"done"}'
        return raw, {"prompt_tokens": 1, "completion_tokens": 1}

    agent = NodeAgent()
    agent._call_model_with_retry = _fake_call
    session = FakeSession()
    result = agent.run("跑脚本", session)
    assert result.termination == "done"
    # spec 样例命令在 done 门控时被真正执行过
    assert any("python3 -c 'print(1)'" in "".join(k) for k in session.keys)


def test_run_spec_loop_terminates():
    # v0.4 回归：spec 锁定后模型反复输出 spec（穿插 act），spec/act 跷跷板会让 stuck 被 act
    # 重置、永不触发卡死（broken-python 900s 超时）。独立 spec_reemit 计数满 3 次应干净终止。
    from node_agent import NodeAgent

    class FakeSession:
        def send_keys(self, keys, block=True):
            pass

        def get_incremental_output(self):
            return "KEYAN_EXIT_CODE=0\nok"

        def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
            pass

        def get_asciinema_timestamp(self):
            return 0.0

    spec = '{"action":"spec","cases":[{"command":"grep -q 462 /app/output.txt","clause":"输出 462","category":"target"}]}'
    responses = [
        spec,  # 第一步：锁定
        spec,  # 重复 1
        '{"action":"act","tool":"bash","target":"echo a"}',  # act 重置 stuck，但不重置 spec_reemit
        spec,  # 重复 2
        '{"action":"act","tool":"bash","target":"echo b"}',
        spec,  # 重复 3 → 终止
    ]

    def _fake_call(messages):
        raw = responses.pop(0) if responses else spec
        return raw, {"prompt_tokens": 1, "completion_tokens": 1}

    agent = NodeAgent()
    agent._call_model_with_retry = _fake_call
    result = agent.run("任务", FakeSession())
    assert result.termination == "stuck"
    # 终止原因是 spec 循环，而非泛化的连续卡死
    assert any(m[1] == "spec-loop" for m in result.markers)


def test_run_time_limit_returns_max_steps():
    # 墙钟上限：在 terminal-bench 的 900s agent_timeout 前主动返回（jq/countdown-game 卡顿根因）。
    # 用负值 time_limit_s 让首轮即触发，验证干净返回 max_steps 而非超时。
    from node_agent import NodeAgent

    class FakeSession:
        def send_keys(self, keys, block=True):
            pass

        def get_incremental_output(self):
            return "KEYAN_EXIT_CODE=0\n"

        def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
            pass

        def get_asciinema_timestamp(self):
            return 0.0

    spec = '{"action":"spec","cases":[{"command":"grep -q 462 /app/output.txt","clause":"输出 462","category":"target"}]}'

    def _fake_call(messages):
        return spec, {"prompt_tokens": 1, "completion_tokens": 1}

    agent = NodeAgent(time_limit_s=-1.0)  # 立即超时
    agent._call_model_with_retry = _fake_call
    result = agent.run("任务", FakeSession())
    assert result.termination == "max_steps"
    assert any(m[1] == "time-limit" for m in result.markers)


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
