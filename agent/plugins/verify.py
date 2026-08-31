"""verify.py：VerifyPlugin —— 认领 VERIFY（自由断言 + 去重 + done 证据门控，v0.3 语义）。

spec 开启时读 ctx.get('spec')，把自己切换到「verify 必须逐字匹配已锁定样例」模式——
组件经 ctx 读对方，不直接 import SpecPlugin。
"""
from __future__ import annotations

from engine import MIN_STEPS_BEFORE_DONE, _verify_repeat_feedback, make_attempt
from runtime import Plugin
from schema import Action, ActionKind
from tools import done_gate_rejection, is_surface_check
from plugins.types import HandleResult, RunState

_VERIFY_SECTION = """## verify（可选动作）

你也可以输出 {"action":"verify","target":"<一条客观断言命令>"} 来检查产出是否正确（只检查、不修改）。断言要像 `test -f /app/out.json`、`grep -q "expected" /app/out.txt`、`python3 -c "assert ..."` 这样直接判对错，不要写成「运行脚本 && echo」。

## 完成（done）规则

done 之前必须至少有一次 verify 且 KEYAN_EXIT_CODE=0，否则 done 会被拒绝。前 2 步内禁止输出 done。"""


class VerifyPlugin(Plugin):
    name = "verify"
    provides = ["verify"]
    requires = ["tools"]

    def setup(self, ctx) -> None:
        tools = ctx.get("tools")
        verify_state = {"seen": {}}  # verify 命令 -> 上次退出码（每 run 新建，reentrant）
        ctx.provide("verify", verify_state)
        ctx.on("prompt-section", lambda: _VERIFY_SECTION)

        def handle(action: Action, raw: str, state: RunState, session, logging_dir) -> HandleResult | None:
            if action.action != ActionKind.VERIFY:
                return None  # 不认领
            target = (action.target or "").strip()
            seen = verify_state["seen"]
            spec = ctx.get("spec")
            # spec 模式：verify 必须逐字匹配已锁定的某条样例，不能自由造新断言
            if spec is not None:
                matched = next(
                    (c for c in (spec["cases"] or []) if c.command.strip() == target), None
                )
                if matched is None:
                    state.feedback = (
                        "verify 的目标命令必须是你已承诺的某条 spec 样例的 command（逐字相同）。"
                        "要检查某条样例就复制它的 command；不能自由造新断言。"
                    )
                    state.stuck += 1
                    return HandleResult()
            if target in seen:  # 重复 verify = 空转，计入卡死
                state.feedback = _verify_repeat_feedback(target, seen[target])
                state.stuck += 1
                return HandleResult()
            # 修复 4：spec 关时 verify 必须是「可判错的断言」，拒绝观察/存在性检查
            #（cat /app/x、ls、test -f、jq dump）。v0.3 的「自写弱断言—假完成」根因。
            if spec is None and is_surface_check(target):
                state.feedback = (
                    f"verify 拒绝：{target[:80]!r} 是观察/存在性检查，不是可判错的断言。"
                    "请用 grep -q 具体串 / diff 参考 / test -eq / python3 -c 'assert ...' / jq -e 等"
                    "能返回真伪的命令。"
                )
                state.stuck += 1
                return HandleResult()
            result = tools["bash"](session, target)
            seen[target] = result.exit_code
            state.append(make_attempt(len(state.attempts) + 1, action, result))
            state.markers.append(
                (session.get_asciinema_timestamp(), f"verify({result.exit_code}): {target[:60]}")
            )
            state.feedback = ""
            state.stuck = 0 if result.exit_code == 0 else state.stuck + 1  # 失败也算无进展
            return HandleResult()

        ctx.on("handle-action", handle)
        ctx.effect(lambda: ctx.off("handle-action", handle))

        def gate_done(state: RunState, session, step: int) -> str:
            # spec 模式交给 SpecPlugin 的全过门控；自由模式下做证据门控
            if ctx.get("spec") is not None:
                return None
            return done_gate_rejection(state.attempts, step, MIN_STEPS_BEFORE_DONE)

        ctx.on("gate-done", gate_done)
        ctx.effect(lambda: ctx.off("gate-done", gate_done))
