"""spec.py：SpecPlugin —— 认领 SPEC（预提交样例 + done 全过门控，v0.4 语义，默认不装）。

spec-first：未锁定 spec 前除 spec 外一切动作被 gate-action 拒绝；done 时引擎跑全部
已锁定样例、全部通过才放行。
"""
from __future__ import annotations

from engine import MIN_STEPS_BEFORE_DONE, SPEC_REEMIT_LIMIT, extract_error_line
from runtime import Plugin
from schema import Action, ActionKind
from tools import validate_spec
from plugins.types import HandleResult, RunState

_SPEC_SECTION = """## spec（动手前先输出一次）

第一步先输出 {"action":"spec","cases":[{"command":"...","clause":"...","category":"..."}]}，把「怎样算完成」写成一组验收样例锁定下来，然后才开始动手。先判断任务类别再构造：
- target（目标值显式）：断言目标值（如 `python3 -c "assert eval(open('/app/output.txt').read().strip())==462"`）。
- behavior（行为/症状）：触发行为、断言症状消失。
- transform（数据变换）：小输入跑参考、diff 产物。
- diagnose（分析诊断）：断言报告含关键发现。

样例硬要求：每条内嵌断言（`assert` / `==` / `grep -q 具体串` / `diff 参考` / 触发行为后断言 exit 0），**不要** `echo` / `true` / `test -f` / `ls` 这类表面检查。取最小充分集（2~4 条，最多 6 条）。

锁定后禁止再输出 spec；done 前引擎会跑全部样例、全部通过才算完成；verify 的目标必须是某条样例的 command。"""


class SpecPlugin(Plugin):
    name = "spec"
    provides = ["spec"]
    requires = ["tools"]

    def setup(self, ctx) -> None:
        tools = ctx.get("tools")
        spec_state = {"cases": None, "reemit": 0}  # 每 run 新建，reentrant
        ctx.provide("spec", spec_state)
        ctx.on("prompt-section", lambda: _SPEC_SECTION)

        def gate_action(action: Action, state: RunState, session) -> str:
            # spec-first：未锁定 spec 前，除 spec 外一切动作都拒绝
            if spec_state["cases"] is None and action.action != ActionKind.SPEC:
                return (
                    "第一步必须先构造验收测试样例：读指令、判断任务类别（target/behavior/transform/diagnose），"
                    '然后输出 {"action":"spec","cases":[{...}]}。样例要带期望输出（内嵌 assert/diff/触发行为），'
                    "不要 echo/test -f/true/ls 这类表面检查。"
                )
            return None

        ctx.on("gate-action", gate_action)
        ctx.effect(lambda: ctx.off("gate-action", gate_action))

        def handle(action: Action, raw: str, state: RunState, session, logging_dir) -> HandleResult | None:
            if action.action != ActionKind.SPEC:
                return None
            if spec_state["cases"] is not None:
                # spec 已锁定再输出 spec =「spec/act 跷跷板」；独立计数（不被 act 重置），满 3 次终止
                spec_state["reemit"] += 1
                if spec_state["reemit"] >= SPEC_REEMIT_LIMIT:
                    state.markers.append((session.get_asciinema_timestamp(), "spec-loop"))
                    return HandleResult(terminate="stuck")
                state.feedback = (
                    "spec 已锁定，不能再输出 spec。从现在起只允许输出 act（动手做一件事，"
                    "tool=bash/write/read）或 verify（逐字复制你已承诺的某条样例 command）"
                    f"或 done。这是第 {spec_state['reemit']} 次重复 spec，再重复会直接终止。"
                )
                state.stuck += 1
                return HandleResult()
            reason = validate_spec(action.cases)
            if reason:
                state.feedback = f"spec 被拒：{reason}。请重新输出合法的 spec。"
                state.stuck += 1
                return HandleResult()
            spec_state["cases"] = action.cases
            state.markers.append(
                (session.get_asciinema_timestamp(), f"spec: {len(action.cases)} cases")
            )
            state.feedback = ""
            state.stuck = 0
            return HandleResult()

        ctx.on("handle-action", handle)
        ctx.effect(lambda: ctx.off("handle-action", handle))

        def gate_done(state: RunState, session, step: int) -> str:
            if spec_state["cases"] is None:
                return "拒绝 done：尚未构造 spec 验收样例，先输出 spec。"
            if step < MIN_STEPS_BEFORE_DONE:
                return f"拒绝 done：至少完成 {MIN_STEPS_BEFORE_DONE} 步后再报告完成。"
            failures = []
            for i, case in enumerate(spec_state["cases"], 1):
                r = tools["bash"](session, case.command)
                if r.exit_code != 0:
                    failures.append(
                        f"  第 {i} 条（{case.clause or case.command[:60]}）"
                        f"退出码 {r.exit_code}：{extract_error_line(r.output)}"
                    )
            if failures:
                return (
                    "拒绝 done：以下 spec 样例未通过——\n" + "\n".join(failures)
                    + "\n请修复后重试 done，不要降低验收标准或改样例。"
                )
            return None  # 放行

        ctx.on("gate-done", gate_done)
        ctx.effect(lambda: ctx.off("gate-done", gate_done))
