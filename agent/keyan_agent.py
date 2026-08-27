"""KeyanAgent：keyan-automation 六步循环在 Terminal-Bench 上的适配器。

把本项目的核心机制接到 Terminal-Bench 的 BaseAgent 接口上：
- 结构化输入输出：模型只输出一个 Action JSON 块，harness 校验并执行；
- 图结点：每步 action 落一个 Node 到 logging_dir/graph/nodes.jsonl；
- 六步循环：observe -> decide -> act -> sense -> update -> loop-closure；
- 本地推理：httpx 直连 llama-server（OpenAI 兼容 /v1/chat/completions）。

v0.2 相对 v0.1 的升级（三条铁律）：
- 工具化（bash/write/read/verify）+ 退出码哨兵 —— 正对 P0-2「过早 done」与 P1「多行命令破坏」；
- 回环跳过 + 卡死终止 —— 正对 P0-1「回环检测空转」；
- done 门控（前 2 步禁止 done + 必须有 verify 通过证据）—— 会话日志是唯一真相。

运行方式（在 terminal-bench 目录，且本文件所在目录在 sys.path 上）：
    uv run tb run --task-id hello-world \
        --agent-import-path "keyan_agent:KeyanAgent" \
        --model qwen3.5-9b \
        --agent-kwarg api_base=http://127.0.0.1:8080/v1
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession
from terminal_bench.utils.logger import logger

from schema import Action, ActionKind, ToolKind, Node, NodeStatus, parse_action, SchemaError
from system_prompt import build_terminal_prompt
from tools import done_gate_rejection, read_file, run_bash, write_file

SYSTEM_PROMPT = build_terminal_prompt()

# done 门控：前 2 步禁止 done；卡死判定阈值（连续 N 步被去重拒绝）。
MIN_STEPS_BEFORE_DONE = 2
STUCK_LIMIT = 8


_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"  # ESC
    r"(?:"
    r"\[[0-?]*[ -/]*[@-~]"  # CSI（须先于 Fe，否则 [ 会被当单字符 Fe 吃掉）
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|[@-_]"  # Fe：单字符
    r")"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_terminal(text: str) -> str:
    """剥离终端输出的 ANSI 转义序列与控制字符。

    本地 9B 推理模型对原始 tmux 面板里的控制码很敏感，偶发直接输出空 content；
    清洗后再喂给模型可显著降低这种失败。
    """
    text = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHARS_RE.sub("", text)


class KeyanAgent(BaseAgent):
    def __init__(
        self,
        model_name: str = "qwen3.5-9b",
        api_base: str = "http://127.0.0.1:8080/v1",
        temperature: float = 0.2,
        max_steps: int = 100,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._model_name = model_name
        self._api_base = api_base.rstrip("/")
        self._temperature = temperature
        self._max_steps = max_steps
        self._logger = logger.getChild(__name__)

    @staticmethod
    def name() -> str:
        return "keyan"

    # ---------- 模型调用（httpx 直连本地 llama-server） ----------

    def _call_model(self, messages: list[dict]) -> tuple[str, dict]:
        payload = {
            "model": self._model_name,
            "messages": messages,
            "temperature": self._temperature,
            # 推理模型（Qwen3.5-9B 思考版）的 reasoning 与回答共享这个预算；
            # 太小会因思考耗尽 token 导致 content 为空。
            "max_tokens": 4096,
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
            self._logger.warning(
                f"模型返回空输出（第 {attempt}/{max_attempts} 次），重试…"
            )
        return "", last_usage

    def _count_tokens(self, usage: dict, content: str) -> tuple[int, int]:
        """从 usage 里取 token 数；不同 llama-server 版本字段名可能不同。"""
        inp = usage.get("prompt_tokens", usage.get("tokens_evaluated", 0))
        out = usage.get("completion_tokens", usage.get("tokens_predicted", 0))
        return int(inp or 0), int(out or 0)

    # ---------- 上下文构造 ----------

    def _build_messages(
        self,
        instruction: str,
        obs: str,
        nodes: list[Node],
        seen_commands: dict[str, int | None],
    ) -> list[dict]:
        # 只保留最近几个结点与终端尾部，控制上下文体积
        recent = nodes[-6:]
        obs = _sanitize_terminal(obs)
        lines = [
            "## 任务",
            instruction,
            "",
            "## 终端现状（最近输出）",
            obs[-4000:],
        ]
        if recent:
            lines.append("\n## 最近探索（避免重走）")
            for n in recent:
                ver = " verified" if n.verified else ""
                ec = f" exit={n.exit_code}" if n.exit_code is not None else ""
                lines.append(
                    f"- [{n.status.value}{ver}{ec}] {n.changes or ['(无变化)']}: {n.inference[:80]}"
                )
        if seen_commands:
            lines.append("\n## 已执行过的命令（不要重试）")
            for cmd in list(seen_commands)[-10:]:
                lines.append(f"- {cmd[:100]}")
        lines.append("\n输出你的下一步动作（仅一个 JSON 块）：")
        user = "\n".join(lines)
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    # ---------- 主循环 ----------

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        total_in = 0
        total_out = 0
        nodes: list[Node] = []
        seen_commands: dict[str, int | None] = {}  # command -> 上次退出码
        markers: list[tuple[float, str]] = []
        obs = ""
        stuck = 0  # 连续被去重拒绝的步数

        graph_path = None
        if logging_dir is not None:
            graph_path = logging_dir / "graph" / "nodes.jsonl"
            graph_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. observe（首屏）
        obs = session.get_incremental_output()

        def _append_node(
            *, changes: list[str], inference: str, summary: str,
            status: NodeStatus = NodeStatus.EXPLORING,
            exit_code: int | None = None, verified: bool = False,
        ) -> Node:
            node = Node(
                id=f"n{len(nodes) + 1:04d}",
                parent=nodes[-1].id if nodes else None,
                index=None,  # Terminal-Bench 无在线指标
                changes=changes,
                inference=inference,
                status=status,
                summary=summary,
                exit_code=exit_code,
                verified=verified,
            )
            nodes.append(node)
            if graph_path is not None:
                with graph_path.open("a", encoding="utf-8") as f:
                    f.write(node.model_dump_json() + "\n")
            return node

        for step in range(self._max_steps):
            # 2. decide
            messages = self._build_messages(instruction, obs, nodes, seen_commands)
            try:
                raw, usage = self._call_model_with_retry(messages)
            except Exception as e:  # 模型服务异常
                self._logger.error(f"LLM call failed: {e}")
                return AgentResult(
                    total_input_tokens=total_in,
                    total_output_tokens=total_out,
                    failure_mode=FailureMode.FATAL_LLM_PARSE_ERROR,
                    timestamped_markers=markers,
                )
            inp, out = self._count_tokens(usage, raw)
            total_in += inp
            total_out += out

            try:
                action = parse_action(raw)
            except SchemaError as e:
                self._logger.warning(f"schema 拒绝: {e}")
                self._logger.warning(f"原始输出: {raw[:400]!r}")
                # 把拒绝原因注入下一轮，让模型重写，不计入结点
                obs = f"[上一步输出被拒绝：{e}]\n" + obs
                continue

            # 3. done 门控（会话日志是唯一真相：done 必须有可验证证据）
            if action.action == ActionKind.DONE:
                reason = done_gate_rejection(nodes, step, MIN_STEPS_BEFORE_DONE)
                if reason:
                    reject = f"拒绝 done：{reason}。"
                    self._logger.warning(reject)
                    obs = f"[{reject}]\n" + obs
                    stuck += 1
                    continue
                markers.append((session.get_asciinema_timestamp(), "done"))
                self._logger.info("模型声称任务完成（已通过 verify 门控）")
                break

            # rollback / retrieve：容器无 git 控制权，转为信号注入
            if action.action == ActionKind.ROLLBACK:
                obs = (
                    "[rollback：本环境无 git 控制权，无法整体回滚。"
                    "改用 act+write 把你上一步的修改改回去，不要空转。]\n" + obs
                )
                markers.append((session.get_asciinema_timestamp(), "rollback"))
                continue
            if action.action == ActionKind.RETRIEVE:
                obs = (
                    "[retrieve：如需某段上下文，用 act + tool=read 读取对应文件。]\n" + obs
                )
                markers.append((session.get_asciinema_timestamp(), "retrieve"))
                continue

            # 4. verify：自写客观检查命令，产出 done 门控证据
            if action.action == ActionKind.VERIFY:
                result = run_bash(session, action.target)
                exit_code = result.exit_code
                verified = exit_code == 0
                obs = result.output
                _append_node(
                    changes=[f"verify: {action.target}"],
                    inference=action.reasoning,
                    summary=action.change or f"verify: {action.target[:60]}",
                    exit_code=exit_code,
                    verified=verified,
                )
                markers.append(
                    (session.get_asciinema_timestamp(), f"verify({exit_code}): {action.target[:60]}")
                )
                self._logger.info(f"verify 退出码 {exit_code}，verified={verified}")
                stuck = 0
                continue

            # 5. act：按 tool 分派（bash / write / read）
            if action.tool == ToolKind.WRITE:
                result = write_file(session, action.target, action.content, logging_dir)
                obs = result.output
                _append_node(
                    changes=[f"write {action.target}"],
                    inference=action.reasoning,
                    summary=action.change or f"write {action.target}",
                    exit_code=result.exit_code,
                )
                stuck = 0
                continue

            if action.tool == ToolKind.READ:
                result = read_file(session, action.target)
                obs = result.output
                _append_node(
                    changes=[f"read {action.target}"],
                    inference=action.reasoning,
                    summary=action.change or f"read {action.target}",
                    exit_code=result.exit_code,
                )
                stuck = 0
                continue

            # BASH：命令去重（回环跳过，正对 P0-1）
            command = (action.target or action.change).strip()
            if not command:
                self._logger.warning("空命令，跳过")
                continue
            if command in seen_commands:
                prev = seen_commands[command]
                obs = (
                    f"[拒绝执行：命令 {command[:80]!r} 已执行过（上次退出码 {prev}）。"
                    "不要重试，换一种完全不同的方法。]\n" + obs
                )
                _append_node(
                    changes=[command],
                    inference=action.reasoning,
                    summary=action.change or action.reasoning,
                    status=NodeStatus.REFUTED,
                    exit_code=prev,
                )
                stuck += 1
            else:
                result = run_bash(session, command)
                exit_code = result.exit_code
                obs = result.output
                seen_commands[command] = exit_code
                markers.append(
                    (session.get_asciinema_timestamp(), f"act: {command[:80]}")
                )
                _append_node(
                    changes=[command],
                    inference=action.reasoning,
                    summary=action.change or action.reasoning,
                    exit_code=exit_code,
                )
                stuck = 0

            # 6. 卡死判定：连续 STUCK_LIMIT 步被去重拒绝 → 干净终止
            if stuck >= STUCK_LIMIT:
                markers.append((session.get_asciinema_timestamp(), "stuck"))
                self._logger.warning(
                    f"连续 {stuck} 步无新命令，判定卡死，终止（已 {len(nodes)} 结点）"
                )
                return AgentResult(
                    total_input_tokens=total_in,
                    total_output_tokens=total_out,
                    failure_mode=FailureMode.UNKNOWN,
                    timestamped_markers=markers,
                )

        return AgentResult(
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            failure_mode=FailureMode.NONE,
            timestamped_markers=markers,
        )
