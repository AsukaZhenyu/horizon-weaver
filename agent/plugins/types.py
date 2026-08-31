"""types.py：插件系统跨组件共享的运行时契约类型。

HandleResult / NodeResult / RunState 不依赖 terminal-bench，也不依赖任何插件实现，
供 agent_loop / verify / spec 等插件共同读写（duck-typing，互不 import 对方）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from schema import Attempt


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
        self.seen_reads: set[str] = set()  # 已读过的文件 target（重复 read 不算进展）
        self.write_counts: dict[str, int] = {}  # target -> 写入次数（反复改写同一文件触发停滞）
        self.graph_path: Path | None = None

    def append(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        if self.graph_path is not None:
            with self.graph_path.open("a", encoding="utf-8") as f:
                f.write(attempt.model_dump_json() + "\n")
