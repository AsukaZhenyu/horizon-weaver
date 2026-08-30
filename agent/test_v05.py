"""v0.5 单元测试：mini-Cordis 运行时 + 插件装配 + 门控组合。

只依赖 runtime.py / plugins.py / schema.py / tools.py / engine.py（不 import terminal-bench），
可离线运行。与 test_v03.py 互补：test_v03 覆盖 spec-first（v0.4）语义，这里覆盖
「spec 关」的 v0.3 语义（自由断言 + 证据门控）与插件底座本身。

运行（在 agent/ 目录）：
    python -m pytest test_v05.py
    python test_v05.py
"""
from __future__ import annotations

from engine import CONTEXT_LOG_NAME
from plugins import (
    HandleResult,
    NodeResult,
    RunState,
    build_default_plugins,
)
from runtime import Context, Plugin, Runtime


# ---------------------------------------------------------------------------
# Context（服务总线 + 事件总线 + 副作用账本）
# ---------------------------------------------------------------------------

def test_context_provide_get():
    ctx = Context()
    ctx.provide("llm", object())
    assert ctx.get("llm") is not None
    assert ctx.get("missing") is None
    assert ctx.get("missing", 42) == 42


def test_context_emit_notifies_all():
    ctx = Context()
    seen = []
    ctx.on("tick", lambda: seen.append(1))
    ctx.on("tick", lambda: seen.append(2))
    ctx.emit("tick")
    assert seen == [1, 2]


def test_context_waterfall_short_circuits():
    ctx = Context()
    ctx.on("gate", lambda: None)          # 委托下一环
    ctx.on("gate", lambda: "拒绝")          # 短路
    ctx.on("gate", lambda: "不该被调用")     # 不该到
    assert ctx.waterfall("gate") == "拒绝"


def test_context_waterfall_all_none_returns_none():
    ctx = Context()
    ctx.on("gate", lambda: None)
    assert ctx.waterfall("gate") is None


def test_context_collect_gathers_non_none():
    ctx = Context()
    ctx.on("section", lambda: "A")
    ctx.on("section", lambda: None)   # 被跳过
    ctx.on("section", lambda: "B")
    assert ctx.collect("section") == ["A", "B"]


def test_context_effect_dispose_reversed():
    ctx = Context()
    log = []
    ctx.effect(lambda: log.append("a"))
    ctx.effect(lambda: log.append("b"))
    ctx.dispose()
    assert log == ["b", "a"]  # 逆序回退


def test_context_off_stops_delivery():
    ctx = Context()
    seen = []
    fn = lambda: seen.append(1)
    ctx.on("tick", fn)
    ctx.off("tick", fn)
    ctx.emit("tick")
    assert seen == []


# ---------------------------------------------------------------------------
# Runtime（拓扑装配 + 可逆副作用回退）
# ---------------------------------------------------------------------------

class _P(Plugin):
    def __init__(self, name, provides=(), requires=(), order=None):
        self.name = name
        self.provides = list(provides)
        self.requires = list(requires)
        self._order = order

    def setup(self, ctx):
        if self._order is not None:
            self._order.append(self.name)
        for k in self.provides:
            ctx.provide(k, object())


def test_runtime_topo_order():
    order = []
    plugins = [
        _P("leaf", requires=["mid"], provides=["leaf"], order=order),
        _P("root", provides=["root"], order=order),
        _P("mid", requires=["root"], provides=["mid"], order=order),
    ]
    Runtime(plugins).start()
    assert order.index("root") < order.index("mid") < order.index("leaf")


def test_runtime_duplicate_name():
    try:
        Runtime([_P("a"), _P("a")]).start()
    except RuntimeError:
        return
    raise AssertionError("应抛插件名重复错误")


def test_runtime_duplicate_provider():
    try:
        Runtime([_P("a", provides=["x"]), _P("b", provides=["x"])]).start()
    except RuntimeError:
        return
    raise AssertionError("应抛服务重复提供错误")


def test_runtime_missing_dependency():
    try:
        Runtime([_P("a", requires=["nope"])]).start()
    except RuntimeError:
        return
    raise AssertionError("应抛依赖缺失错误")


def test_runtime_cycle():
    try:
        Runtime([
            _P("a", requires=["b"], provides=["a"]),
            _P("b", requires=["a"], provides=["b"]),
        ]).start()
    except RuntimeError:
        return
    raise AssertionError("应抛依赖成环错误")


def test_runtime_stop_disposes_effects():
    ctx = Runtime([_P("a")]).start()
    log = []
    ctx.effect(lambda: log.append("cleanup"))
    Runtime([_P("a")]).stop()  # no-op on a fresh runtime（幂等）
    assert log == []  # 上面那个 runtime 的 stop 不该碰这个 ctx
    # 直接验证：装配器 stop 会 dispose 它自己 start 出的 ctx
    rt = Runtime([_P("a")])
    c = rt.start()
    c.effect(lambda: log.append("cleanup"))
    rt.stop()
    assert log == ["cleanup"]


# ---------------------------------------------------------------------------
# 默认装配 + prompt 汇总
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 循环（spec 关 → v0.3 语义：自由断言 + 证据门控）
# ---------------------------------------------------------------------------

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
        '{"action":"verify","target":"test -f /app/out.json"}',
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
        '{"action":"verify","target":"test -f /app/out.json"}',
        '{"action":"verify","target":"test -f /app/out.json"}',
        '{"action":"done"}',
    ])
    # 重复 verify 计入卡死但后续 done 仍可在有证据后放行（不超过 stuck 上限即完成）
    assert result.termination == "done"


# ---------------------------------------------------------------------------
# 运行时契约（RunState / HandleResult / NodeResult）
# ---------------------------------------------------------------------------

def test_run_state_append_writes_graph(tmp_path=None):
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
