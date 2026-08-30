"""runtime.py：mini-Cordis 插件运行时核心（零新依赖）。

参考 dsh/Cordis 的「万物皆插件」思想，把一个科研自动化 agent 的底层
从「硬编码一个 NodeAgent 类」换成「可插拔组件 + 共享 Context + 拓扑装配」：

- Context：服务总线（provide/get）+ 事件总线（on/off/emit/waterfall/collect）
  + 可逆副作用账本（effect/dispose）。所有组件只对着 ctx 说话，互不直接引用。
- Plugin：声明式组件契约——声明「我提供什么服务、依赖什么服务」，
  setup 里挂服务 / 订阅事件 / 记副作用。
- Runtime：按 provides/requires 拓扑排序装配，依赖先就绪；stop 逆序回退副作用。

v0.5 首版只需三种事件分发（dsh 四种里的前三种；parallel 本阶段用不到，需要时再加）：
- emit      ：按序通知，不等结果（记 markers / 日志）。
- waterfall ：中间件链，返回 None 委托下一环、返回非 None 短路（done 门控 / 动作认领）。
- collect   ：收集所有监听者的非 None 返回值（prompt / context 段落汇总）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable


class Context:
    """所有插件共享的同一实例。

    - 服务：调用方只认 key 不认实现——换 LLM / 换 tools 只是换挂在 key 后的对象，
      循环一行不改（provider 级热切换的底座）。
    - 事件：组件间交互的唯一媒介，组件通过事件订阅彼此，不 import 对方。
    - 副作用：每注册一个监听/资源都记一份 cleanup，dispose 逆序回退——
      「即抛即用」的关键：摘掉插件后不留幽灵回调/泄漏资源。
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable]] = defaultdict(list)
        self._effects: list[Callable[[], None]] = []

    # ---------- 服务 ----------

    def provide(self, key: str, value: Any) -> None:
        """注册具名服务。调用方 `ctx.get(key)` 只认 key，不认实现者。"""
        self._services[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """取服务；缺省返回 default（调用方自己定兜底）。"""
        return self._services.get(key, default)

    # ---------- 事件 ----------

    def on(self, event: str, fn: Callable) -> None:
        """订阅事件。event 是执行流契约的名字（如 "gate-done" / "handle-action"）。"""
        self._listeners[event].append(fn)

    def off(self, event: str, fn: Callable) -> None:
        """取消订阅（供 effect 回退用；已取消则静默）。"""
        try:
            self._listeners[event].remove(fn)
        except ValueError:
            pass

    def emit(self, event: str, *args, **kwargs) -> None:
        """按订阅顺序通知所有监听者，不等也不收集结果（fire-and-forget）。"""
        for fn in list(self._listeners.get(event, [])):
            fn(*args, **kwargs)

    def waterfall(self, event: str, *args, **kwargs):
        """中间件链：按序调用，fn 返回 None 委托下一环，返回非 None 短路并作为整体结果。

        - done 门控：任一插件返回「拒绝理由」即短路（= 拒绝 done）。
        - 动作认领：第一个能处理该动作的插件返回处理结果（= 认领成功）。
        """
        for fn in list(self._listeners.get(event, [])):
            result = fn(*args, **kwargs)
            if result is not None:
                return result
        return None

    def collect(self, event: str, *args, **kwargs) -> list:
        """收集所有监听者的非 None 返回值，按订阅顺序（用于汇总 prompt / context 段落）。"""
        out: list = []
        for fn in list(self._listeners.get(event, [])):
            r = fn(*args, **kwargs)
            if r is not None:
                out.append(r)
        return out

    # ---------- 可逆副作用 ----------

    def effect(self, cleanup: Callable[[], None]) -> Callable[[], None]:
        """注册一个副作用及其卸载回退动作；dispose 时逆序执行。"""
        self._effects.append(cleanup)
        return cleanup

    def dispose(self) -> None:
        """逆序回退所有副作用（模拟卸载），再清空监听器与服务。"""
        for cleanup in reversed(self._effects):
            cleanup()
        self._effects.clear()
        self._listeners.clear()
        self._services.clear()


class Plugin:
    """声明式组件契约。

    子类声明类属性 name / provides / requires，实现 setup(ctx)。
    - provides：本插件暴露的服务 key（如 ["llm"]、["spec"]、["agentLoop"]）。
    - requires：本插件启动前必须已就绪的服务 key（如 ["tools"]、["llm","tools","systemPrompt"]）。
    - setup：启动点——ctx.provide 挂服务 / ctx.on 订阅事件 / ctx.effect 记副作用。
    """

    name: str = "plugin"
    provides: list[str] = []
    requires: list[str] = []

    def setup(self, ctx: Context) -> None:  # pragma: no cover - 抽象方法
        raise NotImplementedError


class Runtime:
    """装配器：按 provides/requires 拓扑排序 setup，依赖先就绪；stop 逆序回退副作用。"""

    def __init__(self, plugins: list[Plugin]) -> None:
        self._plugins = plugins
        self._ctx: Context | None = None

    @staticmethod
    def _topo_order(plugins: list[Plugin]) -> list[Plugin]:
        """按依赖拓扑排序；缺依赖 / 成环 / 重复提供 直接抛错（尽早暴露配置错误）。"""
        by_name: dict[str, Plugin] = {}
        provided_by: dict[str, str] = {}
        for p in plugins:
            if p.name in by_name:
                raise RuntimeError(f"插件名重复：{p.name}")
            by_name[p.name] = p
            for key in p.provides:
                if key in provided_by:
                    raise RuntimeError(
                        f"服务 {key!r} 被多个插件提供（{provided_by[key]} 与 {p.name}）"
                    )
                provided_by[key] = p.name

        order: list[Plugin] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(p: Plugin) -> None:
            if p.name in visited:
                return
            if p.name in visiting:
                raise RuntimeError(f"插件依赖成环：{p.name}")
            visiting.add(p.name)
            for key in p.requires:
                dep_name = provided_by.get(key)
                if dep_name is None:
                    raise RuntimeError(f"插件 {p.name} 依赖 {key!r}，但没有插件提供它")
                visit(by_name[dep_name])
            visiting.remove(p.name)
            visited.add(p.name)
            order.append(p)

        for p in plugins:
            visit(p)
        return order

    def start(self) -> Context:
        """依依赖顺序执行所有插件的 setup，返回装配好的 ctx。"""
        ctx = Context()
        for p in self._topo_order(self._plugins):
            p.setup(ctx)
        self._ctx = ctx
        return ctx

    def stop(self) -> None:
        """逆序回退所有副作用（即抛即用：摘掉整棵插件树，不留垃圾）。"""
        if self._ctx is not None:
            self._ctx.dispose()
            self._ctx = None
