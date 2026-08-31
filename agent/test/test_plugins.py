"""test_plugins.py：plugins 包的默认装配 + 门控组合 + 运行时契约。

覆盖：build_default_plugins（spec 默认关/开）、prompt 汇总（spec 关只有 verify 段、
spec 开两者都有）、「spec 关」v0.3 语义（自由断言 + 证据门控）闭环，以及
HandleResult / NodeResult / RunState 契约。与 test_node_agent.py 互补：那里覆盖
spec-first（v0.4）语义，这里覆盖 spec 关语义与插件底座本身。
"""
from __future__ import annotations

from _runner import run  # 副作用：把 agent/ 加进 sys.path

from plugins import (
    HandleResult,
    NodeResult,
    RunState,
    build_default_plugins,
)
from runtime import Runtime


# ---------- 默认装配 + prompt 汇总 ----------

class _FakeClient:
    """注入给 LlmPlugin 的假 client（complete/count_tokens）。"""
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, messages, max_attempts=4):
        raw = self._responses.pop(0) if self._responses else '{"action":"done"}'
        return raw, {"prompt_tokens": 1, "completion_tokens": 1}

    def count_tokens(self, usage):
        return 1, 1


def _assemble_prompt(enable_spec=False):
    rt = Runtime(build_default_plugins(enable_spec=enable_spec, client=_FakeClient([])))
    ctx = rt.start()
    try:
        return ctx.get("systemPrompt")()
    finally:
        rt.stop()


def test_default_plugins_spec_off():
    names = {p.name for p in build_default_plugins()}
    assert "spec" not in names          # v0.5 默认关
    assert {"llm", "tools", "systemPrompt", "system2", "verify", "agentLoop"} <= names


def test_default_plugins_spec_on():
    names = {p.name for p in build_default_plugins(enable_spec=True)}
    assert "spec" in names


def test_prompt_spec_off_has_verify_no_spec():
    prompt = _assemble_prompt(enable_spec=False)
    assert "## verify" in prompt
    assert "## spec" not in prompt


def test_prompt_spec_on_has_both():
    prompt = _assemble_prompt(enable_spec=True)
    assert "## verify" in prompt
    assert "## spec" in prompt
    assert "第一步" in prompt  # spec-first 引导


# ---------- 循环（spec 关 → v0.3 语义：自由断言 + 证据门控） ----------

class _RecSession:
    def __init__(self):
        self.keys = []

    def send_keys(self, keys, block=True):
        self.keys.append("".join(keys))

    def get_incremental_output(self):
        return "KEYAN_EXIT_CODE=0\nok"

    def copy_to_container(self, paths=None, container_dir=None, container_filename=None):
        pass

    def get_asciinema_timestamp(self):
        return 0.0


def _run_spec_off(responses):
    from node_agent import NodeAgent
    agent = NodeAgent(plugins=build_default_plugins(client=_FakeClient(responses)), max_steps=100)
    return agent.run("任务", _RecSession())


def test_spec_off_verify_then_done():
    result = _run_spec_off([
        '{"action":"verify","target":"grep -q expected /app/out.txt"}',
        '{"action":"done"}',
    ])
    assert result.termination == "done"


def test_spec_off_done_without_verify_rejected():
    # done 前无 verify 证据 → 被证据门控反复拒绝，最终卡死，绝不假完成
    result = _run_spec_off(['{"action":"done"}'])
    assert result.termination == "stuck"


def test_spec_off_verify_is_free_assertion():
    # spec 关 → verify 是自由断言，不要求匹配任何 spec 样例，命令被真正执行
    session = _RecSession()
    from node_agent import NodeAgent
    agent = NodeAgent(plugins=build_default_plugins(client=_FakeClient([
        '{"action":"verify","target":"grep -q expected /app/out.txt"}',
        '{"action":"done"}',
    ])))
    result = agent.run("任务", session)
    assert result.termination == "done"
    assert any("grep -q expected" in k for k in session.keys)


def test_spec_off_verify_repeat_guides_done():
    # 重复 verify 已通过的断言 → 引导 done，而非换新断言空转
    result = _run_spec_off([
        '{"action":"verify","target":"grep -q expected /app/out.txt"}',
        '{"action":"verify","target":"grep -q expected /app/out.txt"}',
        '{"action":"done"}',
    ])
    # 重复 verify 计入卡死但后续 done 仍可在有证据后放行（不超过 stuck 上限即完成）
    assert result.termination == "done"


def test_spec_off_verify_rejects_surface():
    # v0.5.1 修复 4：spec 关时 verify 只接受可判错的断言，观察/存在性检查（test -f）
    # 在 spec 关也必须被拒绝，不能凭退出码 0 假完成（v0.3「自写弱断言」根因）。
    session = _RecSession()
    from node_agent import NodeAgent
    agent = NodeAgent(plugins=build_default_plugins(client=_FakeClient([
        '{"action":"verify","target":"test -f /app/out.json"}',
    ])))
    result = agent.run("任务", session)
    # 表面检查被拒绝 → 永远凑不出 verify 证据 → 卡死，且命令根本没被执行
    assert result.termination == "stuck"
    assert not any("test -f" in k for k in session.keys)


# ---------- 运行时契约（RunState / HandleResult / NodeResult） ----------

def test_run_state_append_writes_graph():
    import tempfile
    from pathlib import Path
    from schema import Action, ActionKind, ToolKind
    from engine import make_attempt

    with tempfile.TemporaryDirectory() as d:
        state = RunState()
        state.graph_path = Path(d) / "graph" / "nodes.jsonl"
        state.graph_path.parent.mkdir(parents=True, exist_ok=True)
        a = Action(action=ActionKind.ACT, tool=ToolKind.BASH, target="echo hi")
        from tools import ToolResult
        state.append(make_attempt(1, a, ToolResult(output="hi", exit_code=0)))
        assert len(state.attempts) == 1
        assert state.graph_path.exists()
        assert "echo hi" in state.graph_path.read_text(encoding="utf-8")


def test_handle_result_defaults_continue():
    h = HandleResult()
    assert h.terminate is None


def test_node_result_fields():
    r = NodeResult([], "done", 3, 5, [(0.0, "done")])
    assert r.termination == "done"
    assert r.total_input_tokens == 3
    assert r.total_output_tokens == 5
    assert r.markers == [(0.0, "done")]


if __name__ == "__main__":
    raise SystemExit(run(globals()))
