"""test_node_agent.py：NodeAgent 门面 + spec-first（v0.4）端到端语义。

覆盖：推理上下文日志（每步写 context.md）、spec-first 前置门控（未锁 spec 拒绝 act）、
spec-first done 全过门控、spec/act 跷跷板独立计数终止、墙钟上限干净返回。
与 test_plugins.py 互补：那里覆盖 spec 关（v0.3）语义，这里覆盖 spec 开（v0.4）语义。
"""
from __future__ import annotations

from _runner import run  # 副作用：把 agent/ 加进 sys.path

from node_agent import CONTEXT_LOG_NAME, NodeAgent


def test_context_log_written_per_step():
    import tempfile
    from pathlib import Path

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

    agent = NodeAgent(enable_spec=True)
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


def test_run_requires_spec_first():
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

    agent = NodeAgent(enable_spec=True)
    agent._call_model_with_retry = _fake_call
    result = agent.run("任务", FakeSession())
    assert result.termination == "stuck"


def test_run_spec_first_done_flow():
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

    agent = NodeAgent(enable_spec=True)
    agent._call_model_with_retry = _fake_call
    session = FakeSession()
    result = agent.run("跑脚本", session)
    assert result.termination == "done"
    # spec 样例命令在 done 门控时被真正执行过
    assert any("python3 -c 'print(1)'" in "".join(k) for k in session.keys)


def test_run_spec_loop_terminates():
    # v0.4 回归：spec 锁定后模型反复输出 spec（穿插 act），spec/act 跷跷板会让 stuck 被 act
    # 重置、永不触发卡死（broken-python 900s 超时）。独立 spec_reemit 计数满 3 次应干净终止。
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

    agent = NodeAgent(enable_spec=True)
    agent._call_model_with_retry = _fake_call
    result = agent.run("任务", FakeSession())
    assert result.termination == "stuck"
    # 终止原因是 spec 循环，而非泛化的连续卡死
    assert any(m[1] == "spec-loop" for m in result.markers)


def test_run_time_limit_returns_max_steps():
    # 墙钟上限：在 terminal-bench 的 900s agent_timeout 前主动返回（jq/countdown-game 卡顿根因）。
    # 用负值 time_limit_s 让首轮即触发，验证干净返回 max_steps 而非超时。
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

    agent = NodeAgent(time_limit_s=-1.0, enable_spec=True)  # 立即超时
    agent._call_model_with_retry = _fake_call
    result = agent.run("任务", FakeSession())
    assert result.termination == "max_steps"
    assert any(m[1] == "time-limit" for m in result.markers)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
