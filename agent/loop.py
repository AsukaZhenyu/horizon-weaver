"""keyan-automation：六步自主探索循环（最小实现骨架）。

    observe -> decide -> act -> sense -> update -> (loop-closure check)

设计原则（最小实现）：
- 图 = graph/nodes.jsonl（每行一个结点，append-only）；
- 状态 = git（每个结点 = 一个 commit，rollback = git checkout）；
- 模型只交换结构化消息（Action），harness 拥有原始文件与客观指标；
- 只依赖标准库，模型通过 OpenAI 兼容接口调用（llama.cpp / vLLM / Ollama 都提供）。

运行前设置环境变量：
    OPENAI_BASE_URL   例如 http://127.0.0.1:8080/v1
    MODEL             例如 qwen2.5-7b-instruct
    TASK_WORKDIR      被探索的项目目录（git 仓库）

两个待接入的 seam（v0.1 先留接口）：
    run_step()    —— 执行 ACT 动作（v0：在 TASK_WORKDIR 里跑一条 shell 命令）
    sense()       —— 跑任务评测，返回客观 Index（Terminal-Bench 的容器状态 / MLAgentBench 的指标）
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from schema import (
    Action, ActionKind, Index, Node, NodeStatus,
    parse_action, SchemaError, node_from_json,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH = REPO_ROOT / "graph" / "nodes.jsonl"
WORKDIR = Path(os.environ.get("TASK_WORKDIR", str(REPO_ROOT)))

MODEL = os.environ.get("MODEL", "qwen2.5-7b-instruct")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1")

MAX_STEPS = int(os.environ.get("MAX_STEPS", "200"))  # 单次运行的动作预算


SYSTEM_PROMPT = """你是 keyan-automation 的科研探索 Agent。你的任务是探索一个项目，逼近一个目标指标。

你只能输出一个 JSON 动作块，格式：
{"action": "act|rollback|retrieve|done", "target": "...", "change": "...", "reasoning": "..."}

- act：改一处代码 / 跑一个实验。target=文件路径或命令，change=具体改动。
- rollback：当前状态不如已知最优，回滚到 best 结点。
- retrieve：需要加载某段上下文才能决策。target=文件路径或符号名。
- done：判断已达到目标指标。

规则：
1. 执行动作前，检查当前状态是否已被探索过；探索过的状态不要重走。
2. 每一步的 reasoning 必须说明"这步变化 -> 指标变化"的因果推断。
3. 目标：思路清晰的探索，把错误探索转化为经验，不反复横跳。
"""


# ---------- 0. 基础设施 ----------

def load_graph() -> list[Node]:
    if not GRAPH.exists():
        return []
    return [node_from_json(l) for l in GRAPH.read_text(encoding="utf-8").splitlines() if l.strip()]


def append_node(node: Node) -> None:
    GRAPH.parent.mkdir(parents=True, exist_ok=True)
    with GRAPH.open("a", encoding="utf-8") as f:
        f.write(node.to_json() + "\n")


def call_model(messages: list[dict]) -> str:
    """OpenAI 兼容接口调用本地模型，返回原始文本。"""
    payload = json.dumps({"model": MODEL, "messages": messages, "temperature": 0.2}).encode()
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def git_commit(message: str) -> str:
    """提交当前 TASK_WORKDIR 状态，返回 commit hash。失败返回空串。"""
    r = subprocess.run(
        ["git", "-C", str(WORKDIR), "add", "-A"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ""
    r = subprocess.run(
        ["git", "-C", str(WORKDIR), "commit", "-m", message, "--allow-empty", "--no-gpg-sign"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ""
    out = subprocess.run(
        ["git", "-C", str(WORKDIR), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def git_checkout(commit: str) -> bool:
    if not commit:
        return False
    r = subprocess.run(
        ["git", "-C", str(WORKDIR), "checkout", commit],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def best_node(nodes: list[Node]) -> Node | None:
    return next((n for n in reversed(nodes) if n.status == NodeStatus.BEST), None)


def state_key(node: Node) -> str:
    """回环检测用的状态指纹：客观指标 + 客观变化。"""
    idx = "" if node.index is None else f"{node.index.metric}={node.index.value}"
    return hashlib.sha1((idx + "|" + "|".join(sorted(node.changes))).encode()).hexdigest()[:12]


# ---------- 1. observe ----------

def build_context(goal: dict, nodes: list[Node]) -> str:
    best = best_node(nodes)
    cur = nodes[-1] if nodes else None
    lines = [SYSTEM_PROMPT, "", "## 目标", json.dumps(goal, ensure_ascii=False), ""]
    lines.append("## 当前结点")
    lines.append(cur.to_json() if cur else "(起点，尚无结点)")
    lines.append("\n## 最优结点")
    lines.append(best.to_json() if best else "(无)")
    refuted = [n for n in nodes if n.status == NodeStatus.REFUTED][-5:]
    if refuted:
        lines.append("\n## 最近已证伪结点（不要重走）")
        lines.extend(n.to_json() for n in refuted)
    lines.append("\n输出你的下一步动作（仅一个 JSON 块）：")
    return "\n".join(lines)


# ---------- 2/3. decide + act ----------

def run_step(action: Action) -> None:
    """执行 ACT。v0 语义：把 target 当作一条 shell 命令在 TASK_WORKDIR 里跑。

    TODO: Terminal-Bench 应接入容器环境；代码编辑类动作接入具体文件修改。
    """
    if not action.target:
        print("[run_step] 空 target，跳过")
        return
    print(f"[run_step] 执行: {action.target}")
    subprocess.run(action.target, shell=True, cwd=str(WORKDIR))


# ---------- 4. sense ----------

def sense(goal: dict) -> Index:
    """跑任务评测，返回客观指标。v0 先返回占位值。

    TODO: 接 Terminal-Bench 的容器状态 / MLAgentBench 的模型指标 / RepoBench 的准确率。
    """
    # 占位：后续用真实评测替换。metric 名与 goal 对齐。
    return Index(metric=goal.get("metric", "placeholder"), value=0.0)


# ---------- 主循环 ----------

def main(goal: dict) -> None:
    nodes = load_graph()
    seen = {state_key(n) for n in nodes}
    cur_id = len(nodes)

    for _ in range(MAX_STEPS):
        # 1. observe
        ctx = build_context(goal, nodes)
        # 2. decide
        raw = call_model([{"role": "user", "content": ctx}])
        try:
            action = parse_action(raw)
        except SchemaError as e:
            print(f"[schema 拒绝] {e}\n模型输出: {raw[:200]}")
            continue  # 非法输出不计入结点，重新让模型输出

        # 3. act
        if action.action == ActionKind.DONE:
            print("[done] 模型声称到达 target")
            break
        if action.action == ActionKind.ROLLBACK:
            b = best_node(nodes)
            print(f"[rollback] 回滚到 {b.id if b else '无'} ({b.git if b else ''})")
            if b:
                git_checkout(b.git)
            continue
        if action.action == ActionKind.RETRIEVE:
            # v0：retrieve 暂不实现，只打印；后续把目标上下文注入下一轮
            print(f"[retrieve] 请求加载: {action.target}（v0 暂不处理）")
            continue
        run_step(action)

        # 4. sense
        index = sense(goal)

        # 5. update
        cur_id += 1
        git = git_commit(f"keyan step {cur_id}: {action.reasoning[:80]}")
        node = Node(
            id=f"n{cur_id:04d}",
            parent=nodes[-1].id if nodes else None,
            git=git,
            index=index,
            changes=[action.target],  # v0：以 target 作为变化记录；后续从 git diff 提取
            inference=action.reasoning,
            summary=action.reasoning,
        )

        # 6. loop-closure + 状态判定
        key = state_key(node)
        if key in seen:
            node.status = NodeStatus.REFUTED
            print(f"[回环] 状态已探索过，标记 refuted: {key}")
        elif best_node(nodes) is None or index.value > best_node(nodes).index.value:
            node.status = NodeStatus.BEST
        else:
            node.status = NodeStatus.EXPLORING
        seen.add(key)
        append_node(node)
        nodes.append(node)

        if index.value >= goal.get("target", float("inf")):
            node.status = NodeStatus.TARGET
            print(f"[target] 达成: {index.value} >= {goal['target']}")
            break

    print(f"[结束] 共 {len(nodes)} 个结点，图在 {GRAPH}")


if __name__ == "__main__":
    # v0 入口：从命令行读一个目标描述，例如
    #   python agent/loop.py '{"metric":"container_pass","target":1.0}'
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("goal_json", nargs="?", default='{"metric":"container_pass","target":1.0}')
    args = p.parse_args()
    main(json.loads(args.goal_json))
