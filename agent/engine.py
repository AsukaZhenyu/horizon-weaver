"""engine.py：Node Agent 的「引擎推断 + 上下文组织」纯函数层（无 terminal-bench 依赖）。

从 v0.4 的 node_agent.py 里抽出来的一层：把「一次动作 + 工具结果」落成
Attempt（引擎**自动**生成 inference 与 result 摘要，不让弱模型自己反思），
并把所有尝试组织成一轮用户上下文。plugins.py（各插件）与 node_agent.py（门面）
都 import 这里，互不 import 对方，避免循环依赖。

这层是「纯函数 + 常量」，不持有任何运行态；运行态在 plugins.RunState 里。
"""
from __future__ import annotations

import re

from schema import Action, ActionKind, Attempt, ToolKind

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
# 退出码哨兵值（KEYAN_EXIT_CODE=N）。可能独立成行，也可能因文件末行无换行而拼在
# 内容行尾（如 `echo "ok"KEYAN_EXIT_CODE=0`）——后者不能整行丢弃，只剥掉哨兵值。
_SENTINEL_VALUE_RE = re.compile(r"KEYAN_EXIT_CODE=-?\d+")


def _sanitize_terminal(text: str) -> str:
    """剥离终端输出的 ANSI 转义序列与控制字符。

    本地 9B 推理模型对原始 tmux 面板里的控制码很敏感，偶发直接输出空 content；
    清洗后再喂给模型可显著降低这种失败。
    """
    text = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHARS_RE.sub("", text)


def _clean_bash_output(output: str) -> list[str]:
    """清洗一条 bash 命令的原始终端输出，返回真实命令输出的非空行（顺序保留）。

    剥掉 harness 注入的噪声，否则「真实输出」会被它们淹没：
    - 回显的命令行（含 `tmux wait`）；
    - 退出码哨兵：独立成行 → 丢弃；拼在内容行尾（文件末行无换行）→ 只剥哨兵值、保留内容；
    - 命令跑完后 tmux 回显的尾部 shell 提示符（`root@...:/app#`）。
    """
    lines = [l.strip() for l in _sanitize_terminal(output or "").splitlines() if l.strip()]
    cleaned: list[str] = []
    for l in lines:
        if "tmux wait" in l or l in _HARNESS_HEADERS:
            continue
        if "KEYAN_EXIT_CODE" in l:
            l = _SENTINEL_VALUE_RE.sub("", l).strip()
            if not l:
                continue
        cleaned.append(l)
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


def summarize_attempt(action: Action, result) -> str:
    """引擎自动推断：按 tool 分派 + 退出码分类 + 错误行抽取。result duck-typed（ToolResult）。"""
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


# ---------------------------------------------------------------------------
# Attempt 落账（引擎自动生成 inference 与 result 摘要）
# ---------------------------------------------------------------------------

def make_attempt(step_no: int, action: Action, result) -> Attempt:
    """把一次已执行的动作落成 Attempt（引擎自动生成 inference 与 result 摘要）。

    result 是一句话摘要（错误优先），raw_output 是剥掉 harness 噪声后的完整输出
    （不做摘要、按行保留）。两者互补进上下文：摘要供快速扫账本，完整输出供看细节。
    """
    if action.tool == ToolKind.WRITE:
        res_txt = ""  # write 的结果已由 inference 的 result.output 承载，避免重复
        raw = ""
    elif action.tool == ToolKind.READ:
        # read = `cat "path"`，res_txt 只应展示文件内容（清洗后的），不能把终端噪声
        #（回显命令行 / 退出码哨兵）当文件内容喂给模型——否则模型会把 KEYAN_EXIT_CODE=0
        # 误判成「文件里混入了哨兵」，反复 rewrite 文件去「移除」它（B10，fix-permissions 实测）。
        lines = _clean_bash_output(result.output)
        res_txt = "\n".join(lines)[:ATTEMPT_RESULT_LIMIT]
        raw = "\n".join(lines)[:ATTEMPT_RAW_LIMIT]
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
