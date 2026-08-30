"""plugins.py：Node Agent 的「即抛即用」插件集（v0.5，参考 dsh/Cordis）。

把 v0.4 硬编码在 node_agent.py 一个类里的能力，拆成可插拔组件，全部通过
共享 Context（runtime.Context）交互——组件只认 ctx 上的服务 key 与事件名，
互不 import 对方，实现低耦合高内聚：

- LlmPlugin         提供 ctx.llm（本地 llama-server 客户端；可选读 ctx.thinking 预算）
- ToolsPlugin       提供 ctx.tools（bash/write/read 三个终端工具）
- System2Plugin     提供 ctx.thinking + 「先思考后输出 JSON」prompt 段
- SystemPromptPlugin 提供 ctx.systemPrompt（骨架 + Σ 各插件 prompt_section）
- VerifyPlugin      认领 VERIFY（自由断言 + 去重 + done 证据门控，v0.3 语义）
- SpecPlugin        认领 SPEC（预提交样例 + done 全过门控，v0.4 语义，**默认不装**）
- AgentLoopPlugin   提供 ctx.agentLoop（主循环，决策委托给事件）

组件间交互范式（用户要的「组件与 ctx / 组件间交互」）：
- System2Plugin provide ctx.thinking，LlmPlugin 读 ctx.thinking 定 token 预算——
  组件经 ctx 传配置，互不引用。
- SpecPlugin provide ctx.spec（已锁定样例状态）；VerifyPlugin 认领 VERIFY 时读
  ctx.get('spec') 判断自己在「自由断言」还是「必须匹配 spec 样例」模式下——
  组件经 ctx 读对方，不直接 import。

执行流（AgentLoopPlugin 内）骨架固定，每环行为由订阅的插件决定：
  prompt = ctx.get('systemPrompt')()      # 骨架 + Σ prompt_section
  raw    = ctx.get('llm').complete(...)   # 换模型 = 换 ctx.llm
  action = parse_action(raw)              # schema 词汇不变
  rej    = ctx.waterfall('gate-action', ...)   # spec-first 前置门控（SpecPlugin）
  rej    = ctx.waterfall('gate-done', ...)     # done 门控（Verify/Spec）
  handled = ctx.waterfall('handle-action', ...)  # 动作认领（Spec/Verify/未来工具）
  dispatch(ctx.get('tools'), action)      # 兜底 bash/write/read

「摘掉 spec」= 不装 SpecPlugin；「加 websearch」= 装一个认领 handle-action 的工具插件。
A/B 对比不改循环源码，可逆副作用保证卸载干净。
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from engine import (
    CONTEXT_LOG_NAME,
    MAX_OUTPUT_TOKENS,
    MIN_STEPS_BEFORE_DONE,
    SPEC_REEMIT_LIMIT,
    STUCK_LIMIT,
    TIME_LIMIT_S,
    _verify_repeat_feedback,
    build_context,
    extract_error_line,
    make_attempt,
)
from runtime import Plugin
from schema import (
    Action,
    ActionKind,
    Attempt,
    ToolKind,
    extract_code_block,
    parse_action,
    SchemaError,
)
from system_prompt import NODE_AGENT_SKELETON
from tools import (
    ToolResult,
    done_gate_rejection,
    read_file,
    run_bash,
    validate_spec,
    write_file,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 运行时契约（plugins 内部使用；node_agent 门面不 import 它们）
# ---------------------------------------------------------------------------

@dataclass
class HandleResult:
    """插件认领动作后的返回：terminate 非 None = 立即以该终态结束本次 run。

    handle-action 是 waterfall：返回 None = 不认领（委托下一环），
    返回 HandleResult = 认领成功（terminate=None 继续，否则终止）。
    """
    terminate: str | None = None


class NodeResult:
    """AgentLoop.run 的返回：attempts 账本 + 终态 + token 统计。不依赖 terminal-bench。

    termination 用纯字符串（stuck|done|max_steps|model_error），
    由调用方（如 tb_test）自行映射到 terminal-bench 的 FailureMode。
    """

    def __init__(
        self,
        attempts: list[Attempt],
        termination: str,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        markers: list[tuple[float, str]] | None = None,
    ):
        self.attempts = attempts
        self.termination = termination
        self.total_input_tokens = total_input_tokens
        self.total_output_tokens = total_output_tokens
        self.markers = markers or []


class RunState:
    """单次 run 的共享可变状态：循环与插件共同读写，每次 run 新建（reentrant-safe）。

    - 循环拥有：attempts / stuck / feedback / markers / seen_commands / seen_writes /
      total_in / total_out。
    - 插件经 handle-action / gate-done 直接 mutate 这些字段（duck-typing，不 import 类型）。
    - append() 统一落账（追加 attempts + 写 graph/nodes.jsonl），插件用它而不是
      直接 attempts.append，保证图日志不丢。
    """

    def __init__(self) -> None:
        self.attempts: list[Attempt] = []
        self.stuck: int = 0
        self.feedback: str = ""
        self.markers: list[tuple[float, str]] = []
        self.total_in: int = 0
        self.total_out: int = 0
        self.seen_commands: dict[str, int | None] = {}  # command -> 上次退出码
        self.seen_writes: dict[tuple[str, str], None] = {}  # (target, 内容 sha1)
        self.graph_path: Path | None = None

    def append(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        if self.graph_path is not None:
            with self.graph_path.open("a", encoding="utf-8") as f:
                f.write(attempt.model_dump_json() + "\n")


# ---------------------------------------------------------------------------
# prompt 段（各能力插件贡献；SystemPromptPlugin 汇总到骨架之后）
# ---------------------------------------------------------------------------

_SYSTEM2_SECTION = """## 思考方式（System 2）

可以先用一两句话简要说明「这步做什么、为什么」，然后输出 JSON 动作块。思考有助于规划，但别让思考冲掉 JSON——超过 3 句就很可能让 JSON 出不来而被拒绝；拿不准时直接输出 JSON。"""

_VERIFY_SECTION = """## verify（可选动作）

你也可以输出 {"action":"verify","target":"<一条客观断言命令>"} 来检查产出是否正确（只检查、不修改）。断言要像 `test -f /app/out.json`、`grep -q "expected" /app/out.txt`、`python3 -c "assert ..."` 这样直接判对错，不要写成「运行脚本 && echo」。

## 完成（done）规则

done 之前必须至少有一次 verify 且 KEYAN_EXIT_CODE=0，否则 done 会被拒绝。前 2 步内禁止输出 done。"""

_SPEC_SECTION = """## spec（动手前先输出一次）

第一步先输出 {"action":"spec","cases":[{"command":"...","clause":"...","category":"..."}]}，把「怎样算完成」写成一组验收样例锁定下来，然后才开始动手。先判断任务类别再构造：
- target（目标值显式）：断言目标值（如 `python3 -c "assert eval(open('/app/output.txt').read().strip())==462"`）。
- behavior（行为/症状）：触发行为、断言症状消失。
- transform（数据变换）：小输入跑参考、diff 产物。
- diagnose（分析诊断）：断言报告含关键发现。

样例硬要求：每条内嵌断言（`assert` / `==` / `grep -q 具体串` / `diff 参考` / 触发行为后断言 exit 0），**不要** `echo` / `true` / `test -f` / `ls` 这类表面检查。取最小充分集（2~4 条，最多 6 条）。

锁定后禁止再输出 spec；done 前引擎会跑全部样例、全部通过才算完成；verify 的目标必须是某条样例的 command。"""


# ---------------------------------------------------------------------------
# Llm：本地 llama-server 客户端（httpx 直连）
# ---------------------------------------------------------------------------

class LlmClient:
    """本地 llama-server 的 HTTP 客户端。complete 带空输出重试。"""

    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        max_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        self._model_name = model_name
        self._api_base = api_base.rstrip("/")
        self._temperature = temperature
        self._max_tokens = max_tokens

    def _call(self, messages: list[dict]) -> tuple[str, dict]:
        payload = {
            "model": self._model_name,
            "messages": messages,
            "temperature": self._temperature,
            # 推理模型（Qwen3.5-9B 思考版）的 reasoning 与回答共享这个预算；
            # 太小会因思考耗尽 token 导致 content 为空；System 2 自由长考更需要空间。
            "max_tokens": self._max_tokens,
        }
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            resp = client.post(
                self._api_base + "/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        # 思考模型偶尔把最终 JSON 留在 reasoning_content，或 content 为空/无花括号；
        # 此时退回含 JSON 的那一段，保证 parse_action 有东西可解析。
        if ("{" not in content) and ("{" in reasoning):
            content = reasoning
        usage = data.get("usage", {})
        return content, usage

    def complete(self, messages: list[dict], max_attempts: int = 4) -> tuple[str, dict]:
        """调用模型并在空输出时重试（推理模型偶发思考早停导致 content 为空）。"""
        last_usage: dict = {}
        for attempt in range(1, max_attempts + 1):
            raw, usage = self._call(messages)
            last_usage = usage
            if raw.strip():
                return raw, usage
            _logger.warning(f"模型返回空输出（第 {attempt}/{max_attempts} 次），重试…")
        return "", last_usage

    def count_tokens(self, usage: dict) -> tuple[int, int]:
        """从 usage 里取 token 数；不同 llama-server 版本字段名可能不同。"""
        inp = usage.get("prompt_tokens", usage.get("tokens_evaluated", 0))
        out = usage.get("completion_tokens", usage.get("tokens_predicted", 0))
        return int(inp or 0), int(out or 0)


class LlmPlugin(Plugin):
    name = "llm"
    provides = ["llm"]
    requires = []  # thinking 可选：最小闭环（无 System2）也能跑

    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        client=None,
    ) -> None:
        self._model_name = model_name
        self._api_base = api_base
        self._temperature = temperature
        self._client = client  # 门面注入的 client（实现 monkey-patch 兼容）

    def setup(self, ctx) -> None:
        if self._client is not None:
            client = self._client
        else:
            thinking = ctx.get("thinking") or {}
            max_tokens = thinking.get("max_tokens", MAX_OUTPUT_TOKENS)
            client = LlmClient(self._model_name, self._api_base, self._temperature, max_tokens)
        ctx.provide("llm", client)


# ---------------------------------------------------------------------------
# Tools / System2 / SystemPrompt
# ---------------------------------------------------------------------------

class ToolsPlugin(Plugin):
    name = "tools"
    provides = ["tools"]
    requires = []

    def setup(self, ctx) -> None:
        # 未来工具插件（websearch/localfilesearch）往这个 dict 加 key，循环零改动。
        ctx.provide("tools", {"bash": run_bash, "write": write_file, "read": read_file})


class System2Plugin(Plugin):
    name = "system2"
    provides = ["thinking"]
    requires = []

    def __init__(self, max_tokens: int = MAX_OUTPUT_TOKENS) -> None:
        self._max_tokens = max_tokens

    def setup(self, ctx) -> None:
        ctx.provide("thinking", {"max_tokens": self._max_tokens})
        ctx.on("prompt-section", lambda: _SYSTEM2_SECTION)


class SystemPromptPlugin(Plugin):
    name = "systemPrompt"
    provides = ["systemPrompt"]
    requires = []

    def __init__(self, skeleton: str = NODE_AGENT_SKELETON) -> None:
        self._skeleton = skeleton

    def setup(self, ctx) -> None:
        def assemble() -> str:
            sections = ctx.collect("prompt-section")
            return "\n\n".join([self._skeleton] + sections)

        ctx.provide("systemPrompt", assemble)


# ---------------------------------------------------------------------------
# Verify（v0.3 语义：自由断言 + 去重 + done 证据门控；spec 开启时切换为「匹配样例」）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Spec（v0.4 语义：预提交样例 + done 全过门控；默认不装）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# AgentLoop：主循环（observe → decide → 解析 → 分发 → 记录）
# ---------------------------------------------------------------------------

class AgentLoopPlugin(Plugin):
    name = "agentLoop"
    provides = ["agentLoop"]
    requires = ["llm", "tools", "systemPrompt"]

    def setup(self, ctx) -> None:
        llm = ctx.get("llm")
        tools = ctx.get("tools")
        system_prompt = ctx.get("systemPrompt")

        def _stuck_result(state: RunState, session) -> NodeResult | None:
            """卡死判定：连续 STUCK_LIMIT 步无进展 → 干净终止；否则返回 None 继续。"""
            if state.stuck >= STUCK_LIMIT:
                state.markers.append((session.get_asciinema_timestamp(), "stuck"))
                _logger.warning(
                    f"连续 {state.stuck} 步无进展，判定卡死，终止（已 {len(state.attempts)} 次尝试）"
                )
                return NodeResult(
                    state.attempts, "stuck", state.total_in, state.total_out, state.markers
                )
            return None

        def agent_loop(
            instruction: str,
            session,
            logging_dir: Path | None = None,
            max_steps: int = 100,
            time_limit_s: float = TIME_LIMIT_S,
        ) -> NodeResult:
            system_text = system_prompt()
            state = RunState()

            if logging_dir is not None:
                state.graph_path = logging_dir / "graph" / "nodes.jsonl"
                state.graph_path.parent.mkdir(parents=True, exist_ok=True)

            # 推理上下文日志：记录每一步喂给模型的完整上下文 + 模型输出 + 解析出的动作。
            context_log = None
            if logging_dir is not None:
                context_log = logging_dir / CONTEXT_LOG_NAME
                context_log.parent.mkdir(parents=True, exist_ok=True)
                with context_log.open("a", encoding="utf-8") as f:
                    f.write(
                        "# NodeAgent 推理上下文日志\n\n"
                        f"## 任务\n\n{instruction}\n\n"
                        "## System Prompt\n\n````text\n"
                        + system_text
                        + "\n````\n\n---\n"
                    )

            def _log_step(step_no: int, ctx_text: str, raw: str, action_text: str) -> None:
                if context_log is None:
                    return
                with context_log.open("a", encoding="utf-8") as f:
                    f.write(
                        f"\n---\n\n### Step {step_no}\n\n"
                        "#### 喂给模型的上下文\n\n````text\n"
                        + ctx_text
                        + "\n````\n\n#### 模型原始输出\n\n````text\n"
                        + raw
                        + "\n````\n\n#### 解析出的动作\n\n"
                        + action_text
                        + "\n"
                    )

            # 1. observe（首屏）
            obs = session.get_incremental_output()

            started = time.monotonic()
            for step in range(max_steps):
                # 墙钟上限：在 terminal-bench 的 900s agent_timeout 之前主动返回
                if time.monotonic() - started > time_limit_s:
                    state.markers.append((session.get_asciinema_timestamp(), "time-limit"))
                    _logger.warning(
                        f"达到墙钟时间上限 {time_limit_s:.0f}s，主动终止（已 {len(state.attempts)} 次尝试）"
                    )
                    return NodeResult(
                        state.attempts, "max_steps", state.total_in, state.total_out, state.markers
                    )

                # 2. decide
                spec = ctx.get("spec")
                spec_cases = spec["cases"] if spec else None
                ctx_text = build_context(
                    instruction, state.attempts, obs, state.feedback, spec_cases
                )
                messages = [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": ctx_text},
                ]
                try:
                    raw, usage = llm.complete(messages)
                except Exception as e:  # 模型服务异常
                    _logger.error(f"LLM call failed: {e}")
                    return NodeResult(
                        state.attempts, "model_error", state.total_in, state.total_out, state.markers
                    )
                inp, out = llm.count_tokens(usage)
                state.total_in += inp
                state.total_out += out

                try:
                    action = parse_action(raw)
                except SchemaError as e:
                    _logger.warning(f"schema 拒绝: {e}")
                    _log_step(step, ctx_text, raw, f"schema 被拒：{e}")
                    if "找不到 JSON" in str(e):
                        state.feedback = (
                            "你上一步只输出了思考文字、没有输出动作 JSON（或 JSON 被截断）。"
                            "请**直接**输出一个 JSON 动作块（格式见 system prompt），不要长篇思考。"
                        )
                    else:
                        state.feedback = f"上一步输出被拒绝：{e}"
                    continue
                _log_step(step, ctx_text, raw, action.model_dump_json())

                # 2.5 动作前置门控（spec-first，由 SpecPlugin 订阅）
                rej = ctx.waterfall("gate-action", action, state, session)
                if rej:
                    state.feedback = rej
                    state.stuck += 1
                    res = _stuck_result(state, session)
                    if res is not None:
                        return res
                    continue

                # 3. done 门控（由 Verify/Spec 插件订阅）
                if action.action == ActionKind.DONE:
                    rej = ctx.waterfall("gate-done", state, session, step)
                    if rej:
                        state.feedback = rej
                        state.stuck += 1
                        res = _stuck_result(state, session)
                        if res is not None:
                            return res
                        continue
                    state.markers.append((session.get_asciinema_timestamp(), "done"))
                    _logger.info(f"任务完成（已 {len(state.attempts)} 次尝试）")
                    return NodeResult(
                        state.attempts, "done", state.total_in, state.total_out, state.markers
                    )

                # rollback / retrieve：容器无 git 控制权，转为信号注入（科研层语义，兜底）
                if action.action == ActionKind.ROLLBACK:
                    state.feedback = (
                        "rollback：本环境无 git 控制权，无法整体回滚。"
                        "改用 act+write 把你上一步的修改改回去，不要空转。"
                    )
                    state.markers.append((session.get_asciinema_timestamp(), "rollback"))
                    state.stuck += 1
                    res = _stuck_result(state, session)
                    if res is not None:
                        return res
                    continue
                if action.action == ActionKind.RETRIEVE:
                    target = (action.target or "").strip()
                    if target:
                        result = tools["read"](session, target)
                        state.append(make_attempt(len(state.attempts) + 1, action, result))
                        state.feedback = ""
                        state.stuck = 0
                        continue
                    state.feedback = (
                        "retrieve 缺少 target：若要读文件，用 act + tool=read 并给出绝对路径；"
                        "否则直接输出 act 动手。"
                    )
                    state.markers.append((session.get_asciinema_timestamp(), "retrieve"))
                    state.stuck += 1
                    res = _stuck_result(state, session)
                    if res is not None:
                        return res
                    continue

                # 4/5. 动作认领（spec/verify/未来工具插件）→ 兜底 act 分发
                handled = ctx.waterfall("handle-action", action, raw, state, session, logging_dir)
                if handled is not None:
                    if handled.terminate:
                        return NodeResult(
                            state.attempts, handled.terminate, state.total_in, state.total_out,
                            state.markers,
                        )
                    res = _stuck_result(state, session)
                    if res is not None:
                        return res
                    continue

                # 兜底 act：bash / write / read
                if action.tool == ToolKind.WRITE:
                    content = extract_code_block(raw) or action.content
                    if not content.strip():
                        state.feedback = (
                            "write 缺少文件内容：请在 JSON 后紧跟 ``` 围栏代码块放完整内容，"
                            "不要把多行内容塞进 content 字段。"
                        )
                        state.stuck += 1
                    else:
                        fp = hashlib.sha1(content.encode()).hexdigest()
                        wkey = (action.target, fp)
                        if wkey in state.seen_writes:
                            state.feedback = (
                                f"拒绝执行：文件 {action.target!r} 已写入过相同内容，不要重复写。"
                                "若确需修改，内容必须与上次不同。"
                            )
                            state.stuck += 1
                        else:
                            result = tools["write"](session, action.target, content, logging_dir)
                            state.seen_writes[wkey] = None
                            state.append(make_attempt(len(state.attempts) + 1, action, result))
                            _logger.info(
                                f"write {action.target}（{len(content)} 字符，exit={result.exit_code}）"
                            )
                            state.feedback = ""
                            state.stuck = 0
                elif action.tool == ToolKind.READ:
                    result = tools["read"](session, action.target)
                    state.append(make_attempt(len(state.attempts) + 1, action, result))
                    _logger.info(f"read {action.target}（exit={result.exit_code}）")
                    state.feedback = ""
                    state.stuck = 0
                else:
                    command = (action.target or action.change).strip()
                    if not command:
                        _logger.warning("空命令，跳过")
                        continue
                    if command in state.seen_commands:
                        prev = state.seen_commands[command]
                        state.feedback = (
                            f"拒绝执行：命令 {command[:80]!r} 已执行过（上次退出码 {prev}）。"
                            "不要重试，换一种完全不同的方法。"
                        )
                        state.stuck += 1
                    else:
                        result = tools["bash"](session, command)
                        state.seen_commands[command] = result.exit_code
                        state.markers.append(
                            (session.get_asciinema_timestamp(), f"act: {command[:80]}")
                        )
                        state.append(make_attempt(len(state.attempts) + 1, action, result))
                        _logger.info(f"act: {command[:80]}（exit={result.exit_code}）")
                        state.feedback = ""
                        state.stuck = 0

                # 6. 卡死判定：连续 STUCK_LIMIT 步无进展 → 干净终止
                res = _stuck_result(state, session)
                if res is not None:
                    return res

            return NodeResult(state.attempts, "max_steps", state.total_in, state.total_out, state.markers)

        ctx.provide("agentLoop", agent_loop)


# ---------------------------------------------------------------------------
# 默认装配
# ---------------------------------------------------------------------------

def build_default_plugins(
    enable_spec: bool = False,
    model_name: str = "qwen3.5-9b",
    api_base: str = "http://127.0.0.1:8080/v1",
    temperature: float = 0.2,
    client=None,
) -> list[Plugin]:
    """默认插件集：llm + tools + systemPrompt + system2 + verify + agentLoop。

    spec 默认关（v0.4 结论：spec-first 对 9B 模型净负）。开启 = enable_spec=True，
    或自建 `NodeAgent(plugins=[..., SpecPlugin()])`。

    顺序即 prompt_section 的汇总顺序：思考 → (spec) → verify。SpecPlugin 插在 verify
    之前，让「先 spec 后 verify」的语义在 prompt 里也保持这个先后。
    """
    plugins: list[Plugin] = [
        ToolsPlugin(),
        System2Plugin(),
        VerifyPlugin(),
        LlmPlugin(model_name=model_name, api_base=api_base, temperature=temperature, client=client),
        SystemPromptPlugin(),
        AgentLoopPlugin(),
    ]
    if enable_spec:
        plugins.insert(2, SpecPlugin())  # 在 Verify 之前，prompt 顺序「思考→spec→verify」
    return plugins
