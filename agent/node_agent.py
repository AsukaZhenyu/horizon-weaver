"""NodeAgent：问题解决层的基础设施（无 terminal-bench 依赖）。

给定一个已圈定的小任务（instruction）与一个终端会话（duck-typed session），
用「结构化 I/O + 工具化 + 退出码哨兵 + 去重/卡死 + 引擎自动推断」把任务做出来。

核心新机制（v0.3）：
- 上下文 = 所有尝试的 (action, result, inference) 三元组；
- 其中 inference（这一步意味着什么、下一步往哪走）由引擎**自动**生成
  （确定性模板：退出码分类 + 错误行抽取），不让弱模型自己反思。

被高层 agent（科研探索，未来）与 tb_test（Terminal-Bench 适配器）共同调用。

session 依赖 duck-typed 协议（与 tools.py 一致）：
    send_keys / get_incremental_output / copy_to_container / get_asciinema_timestamp
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

import httpx

from schema import (
    Action,
    ActionKind,
    Attempt,
    SpecCase,
    ToolKind,
    extract_code_block,
    parse_action,
    SchemaError,
)
from system_prompt import build_node_agent_prompt
from tools import ToolResult, read_file, run_bash, validate_spec, write_file

SYSTEM_PROMPT = build_node_agent_prompt()

# done 门控：前 2 步禁止 done；卡死判定阈值（连续 N 步被去重拒绝）。
MIN_STEPS_BEFORE_DONE = 2
STUCK_LIMIT = 8
# spec 锁定后再输出 spec 的独立终止阈值（不被 act 重置）。
# spec/act 跷跷板：spec 被拒后下一个 act 重置 stuck，让「连续无进展」永远到不了 8，
# 模型空转直到 harness 900s 超时（broken-python 实测）。单独计数，满 3 次干净终止。
SPEC_REEMIT_LIMIT = 3
# 墙钟时间上限（秒）：NodeAgent 必须在 terminal-bench 的 900s agent_timeout 之前主动返回。
# 超时会导致 harness「executor 线程 join」卡死 1~2.5 小时（jq/countdown-game 实测），
# 所以宁可提前干净终止（max_steps），也不撞 900s 硬超时。留 300s 缓冲。
TIME_LIMIT_S = 600.0

# 上下文预算：单条 result 截断长度；单条 raw_output 截断长度；账本最多保留的最近条数（更旧压缩为一行）。
ATTEMPT_RESULT_LIMIT = 200
ATTEMPT_RAW_LIMIT = 800
MAX_LEDGER_ENTRIES = 30

# 推理上下文日志：每一步喂给模型的完整上下文（账本）+ 模型原始输出 + 解析出的动作，
# 逐条追加到 logging_dir/context.md，供事后复盘「模型每一步到底看到了什么」。
CONTEXT_LOG_NAME = "context.md"

# 模型单次输出的 token 预算（reasoning 与回答共享）。System 2 允许模型先自由长考再输出
# 动作 JSON，思考会占预算；太小会让 JSON 被截断（"JSON 对象未闭合"）。4096 曾在长上下文下截断。
MAX_OUTPUT_TOKENS = 8192

_logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"  # ESC
    r"(?:"
    r"\[[0-?]*[ -/]*[@-~]"  # CSI（须先于 Fe，否则 [ 会被当单字符 Fe 吃掉）
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|[@-_]"  # Fe：单字符
    r")"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 错误行特征（供引擎自动推断从输出里抽取错误）。
_ERROR_LINE_RE = re.compile(
    r"error|traceback|no module|not found|no such|permission denied",
    re.IGNORECASE,
)
# 终端 shell 提示符（命令跑完后 tmux 回显 `root@<容器>:/app#`）。用它识别并剥掉尾部提示符，
# 否则成功命令的「最后一行」永远是提示符，真实输出被吞掉（Bug A）。
_SHELL_PROMPT_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:.*[#$]\s*$")
# terminal-bench 的 get_incremental_output 会给每次返回包一层表头
# （"New Terminal Output:\n..." / "Current Terminal Screen:\n..."）。
# 剥提示符后这些表头会冒出来冒充「结果」，需一并剥掉。
_HARNESS_HEADERS = frozenset({"New Terminal Output:", "Current Terminal Screen:"})
# 成功路径里要过滤掉的噪声行（pip/apt 的进度/下载/根用户警告等）。
# 否则摘要会被「Looking in links / WARNING / Downloading」这类非结果行占据，
# 而真正的结果（如 "Requirement already satisfied / Successfully installed"）被藏在中间。
_NOISE_LINE_RE = re.compile(
    r"^(looking in links:|collecting |downloading |using cached |"
    r"installing collected packages|warning:|note:)\s*",
    re.IGNORECASE,
)


def _sanitize_terminal(text: str) -> str:
    """剥离终端输出的 ANSI 转义序列与控制字符。

    本地 9B 推理模型对原始 tmux 面板里的控制码很敏感，偶发直接输出空 content；
    清洗后再喂给模型可显著降低这种失败。
    """
    text = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHARS_RE.sub("", text)


def _clean_bash_output(output: str) -> list[str]:
    """清洗一条 bash 命令的原始终端输出，返回真实命令输出的非空行（顺序保留）。

    剥掉三类 harness 注入的噪声，否则「真实输出」会被它们淹没：
    - 回显的命令行 + 退出码哨兵（含 `KEYAN_EXIT_CODE` / `tmux wait` 的行）；
    - 命令跑完后 tmux 回显的尾部 shell 提示符（`root@...:/app#`）。
    """
    lines = [l.strip() for l in _sanitize_terminal(output or "").splitlines() if l.strip()]
    cleaned = [
        l
        for l in lines
        if "KEYAN_EXIT_CODE" not in l and "tmux wait" not in l and l not in _HARNESS_HEADERS
    ]
    while cleaned and _SHELL_PROMPT_RE.match(cleaned[-1]):
        cleaned.pop()
    return cleaned


def _meaningful_lines(lines: list[str]) -> list[str]:
    """过滤成功输出里的进度/下载/警告噪声行；若全部被滤掉则退回原样（保证至少有东西可看）。"""
    kept = [l for l in lines if not _NOISE_LINE_RE.match(l)]
    return kept or lines


# ---------------------------------------------------------------------------
# 引擎自动推断（确定性，不做推理）
# ---------------------------------------------------------------------------

def extract_error_line(output: str, limit: int = ATTEMPT_RESULT_LIMIT) -> str:
    """从命令输出里抽取最可能的一条错误行（错误优先，否则最后非空行）。

    取**最后**一条匹配错误特征的行——Python traceback 的最后一行才是真正的异常
    （如 `ModuleNotFoundError: No module named 'pandas'`），而非 `Traceback (...)` 头部。
    """
    lines = _clean_bash_output(output)
    if not lines:
        return ""
    for line in reversed(lines):
        if _ERROR_LINE_RE.search(line):
            return line[:limit]
    return lines[-1][:limit]


def summarize_result(
    output: str, exit_code: int | None, limit: int = ATTEMPT_RESULT_LIMIT
) -> str:
    """把命令输出压缩成一条可进上下文的摘要（失败时错误优先，成功时取真实结果行）。

    成功（exit 0）时不再取原始「最后一行」——那通常是终端提示符或 WARNING 噪声；
    而是先剥提示符/哨兵，再过滤进度噪声，取真正表征结果的最后一条行。
    """
    lines = _clean_bash_output(output)
    if not lines:
        return ""
    if exit_code not in (0, None):  # 失败：错误优先（取最后一条错误行）
        for line in reversed(lines):
            if _ERROR_LINE_RE.search(line):
                return line[:limit]
        return lines[-1][:limit]
    return _meaningful_lines(lines)[-1][:limit]


def attempt_summary(action: Action) -> str:
    """动作摘要，如 'bash pip install pandas' / 'write /app/x.py' / 'verify ...'。"""
    target = (action.target or "").strip()
    if action.tool == ToolKind.WRITE:
        return f"write {target}"
    if action.tool == ToolKind.READ:
        return f"read {target}"
    if action.action == ActionKind.VERIFY:
        return f"verify {target}"
    return f"bash {target}"


def summarize_attempt(action: Action, result: ToolResult) -> str:
    """引擎自动推断：按 tool 分派 + 退出码分类 + 错误行抽取。"""
    if action.tool == ToolKind.WRITE:
        return result.output  # 已是 "[已写入 ...]" / "[写失败: ...]"
    if action.tool == ToolKind.READ:
        return f"已读（退出码 {result.exit_code}）"
    # bash / verify
    ec = result.exit_code
    if ec == 0:
        if action.action == ActionKind.VERIFY:
            return "成功（断言通过）"
        # act/bash：exit 0 只是「命令没报错」，不等于问题已解决（Bug B）。
        # 把真实输出首行带上，让模型能判断「跑通」与「修好」的区别。
        lines = _meaningful_lines(_clean_bash_output(result.output))
        if lines:
            return f"成功（输出：{lines[-1][:80]}）"
        return "成功（无输出）"
    if ec == 127:
        return "命令未找到(127)：目标命令可能未安装或不在 PATH"
    if ec is None:
        return "无退出码：命令可能超时或输出被截断"
    return f"失败({ec}) {extract_error_line(result.output)}".strip()


def _verify_repeat_feedback(target: str, prev: int | None) -> str:
    """重复 verify 命令时的反馈：上次已通过→引导 done；上次失败→引导先修再验。

    Bug C 根因：模型修好后反复 emit verify 而不是 done，被去重拒绝直到卡死。
    当上次同一断言已通过（退出码 0），正确的引导是「你已完成，输出 done」，
    而不是旧的「换一条新断言」——那只会让模型继续找新断言空转。
    """
    if prev == 0:
        return (
            f"该 verify 命令 {target[:80]!r} 上次已通过（退出码 0）。"
            '你已具备完成证据，直接输出 {"action":"done"}，不要再重复验证。'
        )
    return (
        f"该 verify 命令 {target[:80]!r} 上次失败（退出码 {prev}）。"
        "先修复问题再重跑断言，不要原样重跑同一条命令。"
    )


# ---------------------------------------------------------------------------
# 上下文组织：所有尝试的 (action, result, inference) 三元组
# ---------------------------------------------------------------------------

def build_context(
    instruction: str,
    attempts: list[Attempt],
    obs: str,
    feedback: str = "",
    spec_cases: list | None = None,
    max_entries: int = MAX_LEDGER_ENTRIES,
) -> str:
    """把「目标 + 已锁定 spec 样例 + 所有尝试（动作→结果→推断）+ 系统反馈」组织成一轮用户消息。

    - spec_cases（v0.4）：已锁定的验收测试样例，done 前全部通过才算完成；渲染在目标之后，
      让模型时刻记得自己承诺的验收线。
    - 所有 attempt 都进（每条一行：action → result → inference）；result 已截断、
      错误优先，模型仍可用 read 工具取完整内容。
    - 总条数超预算时，最旧压缩为 `…(前 N 步省略)…`。
    - 首步（无 attempts）时展示「## 当前终端」初始屏。
    """
    parts = ["## 目标", instruction, ""]
    if spec_cases:
        parts.append("## 验收测试样例（已锁定，done 前全部通过才算完成）")
        for i, case in enumerate(spec_cases, 1):
            clause = f"  —— {case.clause}" if getattr(case, "clause", "") else ""
            parts.append(f"{i}. {case.command}{clause}")
        parts.append("")
    if feedback:
        parts += ["## 系统反馈", feedback, ""]
    if attempts:
        parts.append("## 已尝试（动作 → 结果 → 推断）")
        if len(attempts) > max_entries:
            parts.append(f"…(前 {len(attempts) - max_entries} 步省略)…")
            visible = attempts[-max_entries:]
        else:
            visible = attempts
        for a in visible:
            if a.result:
                parts.append(f"{a.step}. {a.action_summary} → {a.result} → {a.inference}")
            else:
                parts.append(f"{a.step}. {a.action_summary} → {a.inference}")
            if a.raw_output:
                indented = "\n".join(f"    {l}" for l in a.raw_output.splitlines())
                parts.append(f"    完整输出:\n{indented}")
    else:
        screen = _sanitize_terminal(obs or "")
        screen = "\n".join(l for l in screen.splitlines() if l.strip() not in _HARNESS_HEADERS)
        parts += ["## 当前终端", screen[-1500:]]
    parts += ["", "## 下一步：简要思考后输出一个动作 JSON（系统只读取 JSON 块）"]
    return "\n".join(parts)


class NodeResult:
    """NodeAgent.run 的返回：attempts 账本 + 终态 + token 统计。不依赖 terminal-bench。

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


class NodeAgent:
    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        max_steps: int = 100,
        time_limit_s: float = TIME_LIMIT_S,
        **kwargs,
    ):
        self._model_name = model_name
        self._api_base = api_base.rstrip("/")
        self._temperature = temperature
        self._max_steps = max_steps
        self._time_limit_s = time_limit_s

    # ---------- 模型调用（httpx 直连本地 llama-server） ----------

    def _call_model(self, messages: list[dict]) -> tuple[str, dict]:
        payload = {
            "model": self._model_name,
            "messages": messages,
            "temperature": self._temperature,
            # 推理模型（Qwen3.5-9B 思考版）的 reasoning 与回答共享这个预算；
            # 太小会因思考耗尽 token 导致 content 为空；System 2 自由长考更需要空间。
            "max_tokens": MAX_OUTPUT_TOKENS,
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

    def _call_model_with_retry(
        self, messages: list[dict], max_attempts: int = 4
    ) -> tuple[str, dict]:
        """调用模型并在空输出时重试（推理模型偶发思考早停导致 content 为空）。"""
        last_usage: dict = {}
        for attempt in range(1, max_attempts + 1):
            raw, usage = self._call_model(messages)
            last_usage = usage
            if raw.strip():
                return raw, usage
            _logger.warning(f"模型返回空输出（第 {attempt}/{max_attempts} 次），重试…")
        return "", last_usage

    def _count_tokens(self, usage: dict, content: str) -> tuple[int, int]:
        """从 usage 里取 token 数；不同 llama-server 版本字段名可能不同。"""
        inp = usage.get("prompt_tokens", usage.get("tokens_evaluated", 0))
        out = usage.get("completion_tokens", usage.get("tokens_predicted", 0))
        return int(inp or 0), int(out or 0)

    # ---------- 尝试记录 + 卡死判定 ----------

    def _make_attempt(self, step_no: int, action: Action, result: ToolResult) -> Attempt:
        """把一次已执行的动作落成 Attempt（引擎自动生成 inference 与 result 摘要）。

        result 是一句话摘要（错误优先），raw_output 是剥掉 harness 噪声后的完整输出
        （不做摘要、按行保留）。两者互补进上下文：摘要供快速扫账本，完整输出供看细节。
        """
        if action.tool == ToolKind.WRITE:
            res_txt = ""  # write 的结果已由 inference 的 result.output 承载，避免重复
            raw = ""
        elif action.tool == ToolKind.READ:
            res_txt = _sanitize_terminal(result.output or "")[:ATTEMPT_RESULT_LIMIT]
            raw = "\n".join(_clean_bash_output(result.output))[:ATTEMPT_RAW_LIMIT]
        else:
            res_txt = summarize_result(result.output, result.exit_code)
            raw = "\n".join(_clean_bash_output(result.output))[:ATTEMPT_RAW_LIMIT]
        return Attempt(
            step=step_no,
            action_summary=attempt_summary(action),
            target=(action.target or "").strip(),
            exit_code=result.exit_code,
            result=res_txt,
            raw_output=raw,
            inference=summarize_attempt(action, result),
            verified=(result.exit_code == 0 and action.action == ActionKind.VERIFY),
        )

    def _stuck_result(
        self,
        stuck: int,
        markers: list[tuple[float, str]],
        session,
        attempts: list[Attempt],
        total_in: int,
        total_out: int,
    ) -> NodeResult | None:
        """卡死判定：连续 STUCK_LIMIT 步无进展 → 干净终止；否则返回 None 继续。"""
        if stuck >= STUCK_LIMIT:
            markers.append((session.get_asciinema_timestamp(), "stuck"))
            _logger.warning(f"连续 {stuck} 步无进展，判定卡死，终止（已 {len(attempts)} 次尝试）")
            return NodeResult(attempts, "stuck", total_in, total_out, markers)
        return None

    # ---------- 主循环 ----------

    def run(self, instruction: str, session, logging_dir: Path | None = None) -> NodeResult:
        attempts: list[Attempt] = []
        total_in = 0
        total_out = 0
        seen_commands: dict[str, int | None] = {}  # command -> 上次退出码
        seen_writes: dict[tuple[str, str], None] = {}  # (target, 内容 sha1) -> 已写过
        seen_verifies: dict[str, int | None] = {}  # verify 命令 -> 上次退出码
        markers: list[tuple[float, str]] = []
        stuck = 0  # 连续被去重拒绝的步数
        feedback = ""  # 最近一次被拒绝/无进展的系统反馈（注入下一轮）
        spec_cases: list[SpecCase] | None = None  # 已锁定的验收测试样例（spec-first，v0.4）
        spec_reemit = 0  # spec 锁定后再输出 spec 的次数（独立计数，不被 act 重置）

        graph_path = None
        if logging_dir is not None:
            graph_path = logging_dir / "graph" / "nodes.jsonl"
            graph_path.parent.mkdir(parents=True, exist_ok=True)

        # 推理上下文日志：记录每一步喂给模型的完整上下文 + 模型输出 + 解析出的动作。
        # 用 4 反引号围栏（模型 write 时输出里带 ``` 3 反引号，不会提前闭合）。
        context_log = None
        if logging_dir is not None:
            context_log = logging_dir / CONTEXT_LOG_NAME
            context_log.parent.mkdir(parents=True, exist_ok=True)
            with context_log.open("a", encoding="utf-8") as f:
                f.write(
                    "# NodeAgent 推理上下文日志\n\n"
                    f"## 任务\n\n{instruction}\n\n"
                    "## System Prompt\n\n````text\n"
                    + SYSTEM_PROMPT
                    + "\n````\n\n---\n"
                )

        def _log_step(step_no: int, ctx: str, raw: str, action_text: str) -> None:
            """把一步的「上下文 + 模型输出 + 解析结果」追加进 context.md。"""
            if context_log is None:
                return
            with context_log.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n---\n\n### Step {step_no}\n\n"
                    "#### 喂给模型的上下文\n\n````text\n"
                    + ctx
                    + "\n````\n\n#### 模型原始输出\n\n````text\n"
                    + raw
                    + "\n````\n\n#### 解析出的动作\n\n"
                    + action_text
                    + "\n"
                )

        def _append(attempt: Attempt) -> None:
            attempts.append(attempt)
            if graph_path is not None:
                with graph_path.open("a", encoding="utf-8") as f:
                    f.write(attempt.model_dump_json() + "\n")

        # 1. observe（首屏）
        obs = session.get_incremental_output()

        started = time.monotonic()
        for step in range(self._max_steps):
            # 墙钟上限：在 terminal-bench 的 900s agent_timeout 之前主动返回，
            # 避免超时触发 harness 线程 join 卡死（jq/countdown-game 实测 1~2.5h）。
            if time.monotonic() - started > self._time_limit_s:
                markers.append((session.get_asciinema_timestamp(), "time-limit"))
                _logger.warning(
                    f"达到墙钟时间上限 {self._time_limit_s:.0f}s，主动终止（已 {len(attempts)} 次尝试）"
                )
                return NodeResult(attempts, "max_steps", total_in, total_out, markers)

            # 2. decide
            ctx = build_context(instruction, attempts, obs, feedback, spec_cases)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": ctx},
            ]
            try:
                raw, usage = self._call_model_with_retry(messages)
            except Exception as e:  # 模型服务异常
                _logger.error(f"LLM call failed: {e}")
                return NodeResult(attempts, "model_error", total_in, total_out, markers)
            inp, out = self._count_tokens(usage, raw)
            total_in += inp
            total_out += out

            try:
                action = parse_action(raw)
            except SchemaError as e:
                _logger.warning(f"schema 拒绝: {e}")
                _log_step(step, ctx, raw, f"schema 被拒：{e}")
                if "找不到 JSON" in str(e):
                    # System 2 下模型可能只输出思考、忘了动作 JSON（或思考过长被截断）。
                    # 直接命令它输出 JSON，别再长篇思考。
                    feedback = (
                        "你上一步只输出了思考文字、没有输出动作 JSON（或 JSON 被截断）。"
                        "请**直接**输出一个 JSON 动作块（格式见 system prompt），不要长篇思考。"
                    )
                else:
                    feedback = f"上一步输出被拒绝：{e}"
                continue
            _log_step(step, ctx, raw, action.model_dump_json())

            # 2.5 spec-first（v0.4）：动手前必须先构造测试样例，否则任何动作都拒绝
            if spec_cases is None and action.action != ActionKind.SPEC:
                feedback = (
                    "第一步必须先构造验收测试样例：读指令、判断任务类别（target/behavior/transform/diagnose），"
                    '然后输出 {"action":"spec","cases":[{...}]}。样例要带期望输出（内嵌 assert/diff/触发行为），'
                    "不要 echo/test -f/true/ls 这类表面检查。"
                )
                stuck += 1
                res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                if res is not None:
                    return res
                continue

            # 2.6 SPEC：校验并锁定验收样例（锁定后不可改，防「做完再回改样例洗白」）
            if action.action == ActionKind.SPEC:
                if spec_cases is not None:
                    # spec 已锁定，再输出 spec 是「spec/act 跷跷板」：浪费步数，且下一个 act
                    # 会重置 stuck，让卡死保护失效（broken-python 900s 超时的根因）。
                    # 独立计数（不被 act 重置），满 SPEC_REEMIT_LIMIT 次干净终止。
                    spec_reemit += 1
                    if spec_reemit >= SPEC_REEMIT_LIMIT:
                        markers.append((session.get_asciinema_timestamp(), "spec-loop"))
                        _logger.warning(
                            f"spec 锁定后重复输出 spec {spec_reemit} 次，判定陷入 spec 循环，终止"
                        )
                        return NodeResult(attempts, "stuck", total_in, total_out, markers)
                    feedback = (
                        "spec 已锁定，不能再输出 spec。从现在起只允许输出 act（动手做一件事，"
                        "tool=bash/write/read）或 verify（逐字复制你已承诺的某条样例 command）"
                        f"或 done。这是第 {spec_reemit} 次重复 spec，再重复会直接终止。"
                    )
                else:
                    reason = validate_spec(action.cases)
                    if reason:
                        feedback = f"spec 被拒：{reason}。请重新输出合法的 spec。"
                    else:
                        spec_cases = action.cases
                        markers.append((session.get_asciinema_timestamp(), f"spec: {len(spec_cases)} cases"))
                        _logger.info(f"锁定 spec：{len(spec_cases)} 条验收样例")
                        feedback = ""
                        stuck = 0
                        continue
                _logger.warning(f"spec 处理未完成：{feedback}")
                stuck += 1
                res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                if res is not None:
                    return res
                continue

            # 3. done 门控 v2（spec-first）：spec 已锁定 + 全部样例通过才放行
            if action.action == ActionKind.DONE:
                if spec_cases is None:
                    feedback = "拒绝 done：尚未构造 spec 验收样例，先输出 spec。"
                elif step < MIN_STEPS_BEFORE_DONE:
                    feedback = f"拒绝 done：至少完成 {MIN_STEPS_BEFORE_DONE} 步后再报告完成。"
                else:
                    failures = []
                    for i, case in enumerate(spec_cases, 1):
                        r = run_bash(session, case.command)
                        if r.exit_code != 0:
                            failures.append(
                                f"  第 {i} 条（{case.clause or case.command[:60]}）"
                                f"退出码 {r.exit_code}：{extract_error_line(r.output)}"
                            )
                    if failures:
                        feedback = (
                            "拒绝 done：以下 spec 样例未通过——\n" + "\n".join(failures)
                            + "\n请修复后重试 done，不要降低验收标准或改样例。"
                        )
                    else:
                        markers.append((session.get_asciinema_timestamp(), "done"))
                        _logger.info(f"任务完成（{len(spec_cases)} 条 spec 样例全部通过）")
                        return NodeResult(attempts, "done", total_in, total_out, markers)
                _logger.warning(f"拒绝 done：{feedback}")
                stuck += 1
                res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                if res is not None:
                    return res
                continue

            # rollback / retrieve：容器无 git 控制权，转为信号注入（科研层语义，兜底）
            if action.action == ActionKind.ROLLBACK:
                feedback = (
                    "rollback：本环境无 git 控制权，无法整体回滚。"
                    "改用 act+write 把你上一步的修改改回去，不要空转。"
                )
                markers.append((session.get_asciinema_timestamp(), "rollback"))
                stuck += 1
                res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                if res is not None:
                    return res
                continue
            if action.action == ActionKind.RETRIEVE:
                # 带 target 就直接读；否则计入卡死，防止反复 retrieve 空转。
                target = (action.target or "").strip()
                if target:
                    result = read_file(session, target)
                    _append(self._make_attempt(len(attempts) + 1, action, result))
                    _logger.info(f"retrieve→read {target}（exit={result.exit_code}）")
                    feedback = ""
                    stuck = 0
                    continue
                feedback = (
                    "retrieve 缺少 target：若要读文件，用 act + tool=read 并给出绝对路径；"
                    "否则直接输出 act 动手。"
                )
                markers.append((session.get_asciinema_timestamp(), "retrieve"))
                stuck += 1
                res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                if res is not None:
                    return res
                continue

            # 4. verify：只能跑已承诺的 spec 样例命令（防自由造新断言游戏化）
            if action.action == ActionKind.VERIFY:
                target = (action.target or "").strip()
                matched = next((c for c in (spec_cases or []) if c.command.strip() == target), None)
                if matched is None:
                    feedback = (
                        "verify 的目标命令必须是你已承诺的某条 spec 样例的 command（逐字相同）。"
                        "要检查某条样例就复制它的 command；不能自由造新断言。"
                    )
                    stuck += 1
                    res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                    if res is not None:
                        return res
                    continue
                if target in seen_verifies:  # 重复 verify 命令 = 空转，计入卡死
                    prev = seen_verifies[target]
                    feedback = _verify_repeat_feedback(target, prev)
                    stuck += 1
                    res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                    if res is not None:
                        return res
                    continue
                result = run_bash(session, target)
                seen_verifies[target] = result.exit_code
                _append(self._make_attempt(len(attempts) + 1, action, result))
                markers.append(
                    (session.get_asciinema_timestamp(), f"verify({result.exit_code}): {target[:60]}")
                )
                _logger.info(f"verify 退出码 {result.exit_code}")
                feedback = ""
                stuck = 0 if result.exit_code == 0 else stuck + 1  # 失败也算无进展
                res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                if res is not None:
                    return res
                continue

            # 5. act：按 tool 分派（bash / write / read）
            if action.tool == ToolKind.WRITE:
                # 文件内容优先取 JSON 后的 ``` 围栏代码块，而非 JSON 里的 content 字段
                content = extract_code_block(raw) or action.content
                if not content.strip():
                    feedback = (
                        "write 缺少文件内容：请在 JSON 后紧跟 ``` 围栏代码块放完整内容，"
                        "不要把多行内容塞进 content 字段。"
                    )
                    stuck += 1
                    res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                    if res is not None:
                        return res
                    continue
                fp = hashlib.sha1(content.encode()).hexdigest()
                wkey = (action.target, fp)
                if wkey in seen_writes:  # 相同内容写到相同文件 = 空转，计入卡死
                    feedback = (
                        f"拒绝执行：文件 {action.target!r} 已写入过相同内容，不要重复写。"
                        "若确需修改，内容必须与上次不同。"
                    )
                    stuck += 1
                    res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
                    if res is not None:
                        return res
                    continue
                result = write_file(session, action.target, content, logging_dir)
                seen_writes[wkey] = None
                _append(self._make_attempt(len(attempts) + 1, action, result))
                _logger.info(f"write {action.target}（{len(content)} 字符，exit={result.exit_code}）")
                feedback = ""
                stuck = 0
                continue

            if action.tool == ToolKind.READ:
                result = read_file(session, action.target)
                _append(self._make_attempt(len(attempts) + 1, action, result))
                _logger.info(f"read {action.target}（exit={result.exit_code}）")
                feedback = ""
                stuck = 0
                continue

            # bash：命令去重（回环跳过）
            command = (action.target or action.change).strip()
            if not command:
                _logger.warning("空命令，跳过")
                continue
            if command in seen_commands:
                prev = seen_commands[command]
                feedback = (
                    f"拒绝执行：命令 {command[:80]!r} 已执行过（上次退出码 {prev}）。"
                    "不要重试，换一种完全不同的方法。"
                )
                stuck += 1
            else:
                result = run_bash(session, command)
                seen_commands[command] = result.exit_code
                markers.append((session.get_asciinema_timestamp(), f"act: {command[:80]}"))
                _append(self._make_attempt(len(attempts) + 1, action, result))
                _logger.info(f"act: {command[:80]}（exit={result.exit_code}）")
                feedback = ""
                stuck = 0

            # 6. 卡死判定：连续 STUCK_LIMIT 步无进展 → 干净终止
            res = self._stuck_result(stuck, markers, session, attempts, total_in, total_out)
            if res is not None:
                return res

        return NodeResult(attempts, "max_steps", total_in, total_out, markers)
