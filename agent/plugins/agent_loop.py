"""agent_loop.py：AgentLoopPlugin —— 提供 ctx.agentLoop（主循环）。

主循环骨架固定（observe → decide → 解析 → 分发 → 记录），每环行为由订阅的插件决定：
  prompt  = ctx.get('systemPrompt')()          # 骨架 + Σ prompt_section
  raw     = ctx.get('llm').complete(...)       # 换模型 = 换 ctx.llm
  action  = parse_action(raw)                  # schema 词汇不变
  rej     = ctx.waterfall('gate-action', ...)  # spec-first 前置门控（SpecPlugin）
  rej     = ctx.waterfall('gate-done', ...)    # done 门控（Verify/Spec）
  handled = ctx.waterfall('handle-action', ...) # 动作认领（Spec/Verify/未来工具）
  dispatch(ctx.get('tools'), action)           # 兜底 bash/write/read
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from engine import CONTEXT_LOG_NAME, STUCK_LIMIT, TIME_LIMIT_S, build_context, make_attempt
from runtime import Plugin
from schema import ActionKind, ToolKind, extract_code_block, parse_action, SchemaError
from plugins.types import NodeResult, RunState

_logger = logging.getLogger(__name__)


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
                    # 非法输出也是无进展，累计卡死（v0.5.1 修复 3）
                    state.stuck += 1
                    res = _stuck_result(state, session)
                    if res is not None:
                        return res
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
                    # 修复 1（write 协议）：content 字段非空时优先；否则取 JSON 之后的
                    # 非 json 围栏（extract_code_block 已跳过 ```json 动作围栏，避免把
                    # 动作 JSON 当文件内容写进 cmd.sh——jq 任务实测根因）。
                    content = (action.content or "").strip() or extract_code_block(raw)
                    if not content.strip():
                        state.feedback = (
                            "write 缺少文件内容：请把完整内容放进 content 字段，"
                            "或紧跟 JSON 放一个非 json 的 ``` 围栏代码块（不要用 ```json）。"
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
                            state.write_counts[action.target] = (
                                state.write_counts.get(action.target, 0) + 1
                            )
                            state.append(make_attempt(len(state.attempts) + 1, action, result))
                            _logger.info(
                                f"write {action.target}（{len(content)} 字符，exit={result.exit_code}）"
                            )
                            if result.exit_code == 0:
                                if state.write_counts[action.target] > 1:
                                    # 反复改写同一文件 = 无进展（cprofiling 反复写 fast.py 根因）
                                    state.feedback = (
                                        f"已第 {state.write_counts[action.target]} 次写入 {action.target}，"
                                        "反复改写同一文件不算进展；先用 bash/verify 确认结果再改。"
                                    )
                                    state.stuck += 1
                                else:
                                    state.feedback = ""
                                    state.stuck = 0
                            else:
                                # 写失败不清零（修复 2）
                                state.feedback = f"write 失败：{result.output}"
                                state.stuck += 1
                elif action.tool == ToolKind.READ:
                    target = (action.target or "").strip()
                    if not target:
                        state.feedback = "read 缺少 target：请给出要读取的容器内绝对路径。"
                        state.stuck += 1
                    elif target in state.seen_reads:
                        # 重复 read 不算进展（修复 2）
                        state.feedback = (
                            f"已读过 {target}，重复 read 不算进展；不要反复读同一文件。"
                        )
                        state.stuck += 1
                    else:
                        result = tools["read"](session, target)
                        state.seen_reads.add(target)
                        state.append(make_attempt(len(state.attempts) + 1, action, result))
                        _logger.info(f"read {target}（exit={result.exit_code}）")
                        if result.exit_code == 0:
                            state.feedback = ""
                            state.stuck = 0
                        else:
                            # 读失败（文件不存在）不清零（修复 2）
                            state.feedback = (
                                f"read 失败（退出码 {result.exit_code}）：{target} 可能不存在"
                            )
                            state.stuck += 1
                else:
                    command = (action.target or action.change).strip()
                    if not command:
                        # 空命令也累计卡死（修复 3）
                        _logger.warning("空命令，跳过")
                        state.feedback = "空命令：请给出具体要执行的 shell 命令。"
                        state.stuck += 1
                        res = _stuck_result(state, session)
                        if res is not None:
                            return res
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
