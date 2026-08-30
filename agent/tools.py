"""工具层：对齐 Claude Code 的 Tool 接口（validate→execute→result）。

v0.2 的四个工具对应四种动作，核心是把「裸 send_keys 发命令」升级成带退出码的执行路径：

- bash   ：执行一条单行命令，命令末尾注入退出码哨兵 `; echo 'KEYAN_EXIT_CODE='$?`。
           哨兵在 harness 追加 `; tmux wait -S done` **之前**，所以 $? 捕获的是原命令的退出码。
- write  ：把多行内容写进容器文件。走 session.copy_to_container（put_archive），
           绕开 heredoc——这是 P1「多行命令被 `; tmux wait` 破坏」的正解。
- read   ：读文件（观察，可重复，不参与 act 的去重）。
- verify ：运行自写的客观检查命令，退出码 0 记 verified=True（done 门控的证据）。

工具层不依赖 terminal-bench 的运行时导入（仅类型标注），因此单元测试可独立导入。
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terminal_bench.terminal.tmux_session import TmuxSession

EXIT_CODE_MARKER = "KEYAN_EXIT_CODE"
_EXIT_ECHO = "; echo '" + EXIT_CODE_MARKER + "='$?"

# B8：模型会偷学 harness 的哨兵，在自己命令里手动写
#   `; echo 'KEYAN_EXIT_CODE='$?` 或 `&& echo 'KEYAN_EXIT_CODE=0'`。
# 这会在真实命令退出码之后再产生第二个「伪哨兵」；parse_exit_code 取最后一位，
# 于是被这个恒为 0 的伪哨兵掩盖成 0（pip install 失败被报成成功）。
# 执行前先剥掉模型自带的手动哨兵，保证只剩 harness 追加的那一个，
# $? 捕获的才是原命令的真实退出码。
# 引号可能落在 KEYAN_EXIT_CODE 前、`=` 后（如 'KEYAN_EXIT_CODE='$?）、或值后；
# 值可能是 $? 或数字。故用 [\$?\d]* 统一收尾，引号各位置可选。
_SELF_ECHO_RE = re.compile(
    r"(?:;|&&|\|\|)\s*echo\s+['\"]?KEYAN_EXIT_CODE\s*=\s*['\"]?[\$?\d]*['\"]?"
)


@dataclass
class ToolResult:
    """工具执行结果：给模型看的输出 + 客观退出码 + 是否通过验证。"""
    output: str
    exit_code: int | None = None
    verified: bool = False
    ok: bool = True


def with_exit_marker(command: str) -> str:
    """给单行命令末尾注入退出码哨兵（必须在 harness 追加 `; tmux wait -S done` 之前）。

    先剥掉模型可能偷学的 `echo 'KEYAN_EXIT_CODE=...'`（B8），
    避免伪哨兵掩盖真实退出码。
    """
    command = _SELF_ECHO_RE.sub("", command)
    return command.rstrip() + _EXIT_ECHO


def parse_exit_code(text: str) -> int | None:
    """从终端输出解析最近一次 KEYAN_EXIT_CODE=N。取最后一次出现，避免历史干扰。"""
    matches = re.findall(re.escape(EXIT_CODE_MARKER) + r"=(-?\d+)", text)
    return int(matches[-1]) if matches else None


def done_gate_rejection(nodes: list, step: int, min_steps_before_done: int = 2) -> str:
    """done 门控：返回拒绝理由，空串表示放行。

    会话日志是唯一真相：done 前必须 (1) 超过最少步数下限 (2) 有至少一次
    通过（verified 且 exit_code==0）的 verify 证据。nodes 用 duck-typing，
    只依赖 verified / exit_code 两个属性，避免工具层反向依赖 schema。
    """
    if step < min_steps_before_done:
        return f"至少完成 {min_steps_before_done} 步后再报告完成"
    has_evidence = any(
        getattr(n, "verified", False) and getattr(n, "exit_code", None) == 0
        for n in nodes
    )
    if not has_evidence:
        return "尚无通过的 verify 证据（KEYAN_EXIT_CODE=0）"
    return ""


# spec 样例条数硬约束（v0.4）：最少 1 条（空 spec 拒绝），最多 6 条（防灌水）。
# 「至少 2 条（目标断言 + 一条黑天鹅）」是 prompt 层的软约束，不在此硬卡——
# 弱模型对 countdown-game 这种单目标任务可能只写得出一条有效断言，硬卡会把它卡死。
MIN_SPEC_CASES = 1
MAX_SPEC_CASES = 6


def is_surface_check(command: str) -> bool:
    """判定一条命令是否「表面检查」——只验存在/类型/非空/恒真，不验值/行为。

    假完成的根因是模型用表面检查（`test -f` / 非空 / `echo` / `true`）替代语义检查
    （值对不对 / 症状消没消）。done 门控据此拒绝这类样例。
    """
    cmd = " ".join(command.split())
    if not cmd:
        return True
    # 恒真 / 空转 / 只回显
    if cmd in {"true", ":", "false"} or cmd.startswith("echo "):
        return True
    # 纯观察：只读命令、无判定（ls/cat/head/…）。若带 |/&/</> 则可能是
    # `cat file | grep expected` 这类语义检查，不判表面。
    if re.fullmatch(r"(ls|pwd|whoami|id|env|printenv|cat|head|tail|wc|file)(\s.*)?", cmd) \
            and not re.search(r"[|&<>]", cmd):
        return True
    # 纯存在/类型/非空/权限检查（test -f / [ -f ），后面没跟任何值/行为断言
    if re.match(r"^(test|\[)\s+-[efdsrwxL]\b", cmd) and not re.search(r"(&&|\|\||[|;<>])", cmd):
        return True
    return False


def validate_spec(cases) -> str | None:
    """校验一组 spec 样例，返回拒绝理由（None = 通过）。duck-typed：每条只依赖 .command。

    硬约束：非空、条数 [MIN, MAX]、每条命令非空、非表面检查、命令去重。
    「不重不漏 / 分类构造」是 prompt 层的软约束，由模型尽力，不在引擎硬卡（弱模型易跑偏）。
    """
    if not cases:
        return "spec 至少需要 1 条测试样例"
    if len(cases) > MAX_SPEC_CASES:
        return f"spec 样例太多（最多 {MAX_SPEC_CASES} 条，取最小充分集，不要灌水）"
    seen: set[str] = set()
    for i, case in enumerate(cases, 1):
        cmd = " ".join((getattr(case, "command", "") or "").split())
        if not cmd:
            return f"第 {i} 条命令为空"
        if is_surface_check(cmd):
            return f"第 {i} 条是表面检查（只验存在/非空/恒真，不验值或行为）：{cmd[:80]!r}"
        if cmd in seen:
            return f"第 {i} 条与前面的样例重复"
        seen.add(cmd)
    return None


def run_bash(session: "TmuxSession", command: str) -> ToolResult:
    """执行单行命令，返回输出 + 退出码。"""
    if not command.strip():
        return ToolResult(output="[空命令，未执行]", exit_code=None, ok=False)
    try:
        session.send_keys([with_exit_marker(command), "Enter"], block=True)
    except Exception as e:  # 超时 / tmux 异常
        return ToolResult(output=f"[bash 执行失败: {e}]", exit_code=None, ok=False)
    out = session.get_incremental_output()
    return ToolResult(output=out, exit_code=parse_exit_code(out))


def write_file(
    session: "TmuxSession",
    path: str,
    content: str,
    logging_dir: Path | None = None,
) -> ToolResult:
    """把 content 写入容器内 path（须为容器 POSIX 绝对路径）。经 copy_to_container 实现。

    容器路径是 POSIX 风格（/app/x.py），必须用 PurePosixPath 解析：
    Windows 上的 Path 会把 /app 判成非绝对（缺盘符），导致写文件全部被拒。
    """
    if not path.startswith("/"):
        return ToolResult(
            output=f"[write 需要容器内绝对路径（以 / 开头），收到: {path!r}]",
            exit_code=None,
            ok=False,
        )
    target = PurePosixPath(path)
    tmp_dir = (
        (logging_dir / "tmp")
        if logging_dir is not None
        else Path(tempfile.gettempdir()) / "keyan"
    )
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / (hashlib.sha1(content.encode()).hexdigest()[:12] + ".tmp")
    tmp_file.write_text(content, encoding="utf-8")
    try:
        session.copy_to_container(
            paths=[tmp_file],
            container_dir=str(target.parent) or "/",
            container_filename=target.name,
        )
    except Exception as e:
        return ToolResult(output=f"[write 失败: {e}]", exit_code=None, ok=False)
    return ToolResult(output=f"[已写入 {path}，{len(content)} 字符]", exit_code=0)


def read_file(session: "TmuxSession", path: str) -> ToolResult:
    """读文件（观察，可重复）。退出码 0 表示文件存在。"""
    return run_bash(session, f'cat "{path}"')
