"""test_runtime.py：runtime.py 的 mini-Cordis 底座。

覆盖：Context（provide/get、emit/on/off、waterfall 短路、collect、effect 逆序 dispose）
与 Runtime（依赖拓扑排序、插件名/服务重复、依赖缺失、依赖成环、stop 回退副作用）。
只依赖 runtime.py，离线可运行。
"""
from __future__ import annotations

from _runner import run  # 副作用：把 agent/ 加进 sys.path

from runtime import Context, Plugin, Runtime


# ---------- Context（服务总线 + 事件总线 + 副作用账本） ----------

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


# ---------- Runtime（拓扑装配 + 可逆副作用回退） ----------

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


if __name__ == "__main__":
    raise SystemExit(run(globals()))
