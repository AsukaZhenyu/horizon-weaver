"""Reproducible development comparisons for state retrieval and redundant tools.

This uses fixed traces and a ScriptedPolicy; it is not a model benchmark.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from horizon_weaver import __version__
from horizon_weaver.app import NodeAgent, code_fingerprint, read_run
from horizon_weaver.context import ContextCompiler
from horizon_weaver.core import (Action, Attempt, Budget, Check, FileState, GraphView,
    ProgressAssessment, StateDelta, StateSnapshot, TaskContract, ToolResult, digest)
from horizon_weaver.harness import LocalHarness
from horizon_weaver.policy import ScriptedPolicy


def context_comparison(output: Path) -> dict:
    files = {"implementation.py": "IMPORTANT_WORKING_SET_MARKER\n"}
    files.update({f"noise-{i}.txt": f"unrelated-{i}:" + "n" * 1200 for i in range(24)})
    snapshot = StateSnapshot(files=tuple(FileState(path=path, kind="file", sha256=digest(content),
        size=len(content.encode("utf-8")), mode=0o644) for path, content in sorted(files.items())))
    paths = ["implementation.py", *(f"noise-{i}.txt" for i in range(24))]
    attempts = tuple(Attempt(step=step, action=Action(kind="read", path=path),
        result=ToolResult(output=files[path], exit_code=0), before=snapshot, after=snapshot,
        delta=StateDelta(), progress=ProgressAssessment(level="information", reason_code="fixture"))
        for step, path in enumerate(paths, 1))
    task = TaskContract(task_id="history-retrieval", goal="Repair implementation.py",
        acceptance_checks=(Check(id="implementation", kind="text_equals", path="implementation.py",
                                 expected="correct"),), budget=Budget(context_tokens=14000))
    view = GraphView(snapshot=snapshot, attempts=attempts)
    (output / "context-fixture.json").write_text(json.dumps({"task": task.model_dump(mode="json"),
        "view": view.model_dump(mode="json")}, ensure_ascii=False, indent=2), encoding="utf-8")
    results = {}
    for strategy in ("recent", "state"):
        request = ContextCompiler({"shell": "fixture"}, strategy=strategy).build(task, view, snapshot, 26, 100000)
        (output / f"context-{strategy}.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
        results[strategy] = {
            "input_utf8_byte_bound": request.input_token_bound,
            "selected_attempt_steps": request.selected_attempt_steps,
            "complete_visible_observations": request.visible_observation_steps,
            "required_observation_retained": "IMPORTANT_WORKING_SET_MARKER" in request.context,
        }
    results["input_bound_reduction_percent"] = round(100 * (1 -
        results["state"]["input_utf8_byte_bound"] / results["recent"]["input_utf8_byte_bound"]), 2)
    return results


def tool_comparison(output: Path) -> dict:
    actions = (Action(kind="read", path="note.txt"), Action(kind="read", path="note.txt"),
               Action(kind="write", path="answer.txt", content="42"),
               Action(kind="write", path="answer.txt", content="42"), Action(kind="done"))
    task = TaskContract(task_id="redundant-tools", goal="Write answer 42",
        acceptance_checks=(Check(id="external", kind="external"),),
        budget=Budget(max_steps=5, no_progress_limit=10))
    results = {}
    for enabled in (False, True):
        name = "guard" if enabled else "unguarded"
        workspace, run = output / (name + "-workspace"), output / (name + "-run")
        workspace.mkdir()
        (workspace / "note.txt").write_text("known observation", encoding="utf-8")
        calls = []
        original = LocalHarness.execute
        def execute(harness, action, timeout_s):
            calls.append(action.kind.value)
            return original(harness, action, timeout_s)
        with patch.object(LocalHarness, "execute", execute):
            result = NodeAgent(backend="local", policy=ScriptedPolicy(actions), transition_guard=enabled).run(
                task, workspace, run)
        graph = read_run(run)
        (output / (name + "-state-ir.json")).write_text(graph.ir.model_dump_json(indent=2), encoding="utf-8")
        results[name] = {
            "actual_harness_calls": calls,
            "actual_read_write_calls": sum(kind in ("read", "write") for kind in calls),
            "termination": result.termination.value,
            "steps": result.steps,
            "contract_passed": result.verdict.passed,
            "answer": (workspace / "answer.txt").read_text(encoding="utf-8"),
            "rejected_steps": [a.step for a in graph.attempts if a.tool_executed is False],
            "ir_fingerprint": graph.ir.fingerprint,
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / ("v1.1-state-" + stamp))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "code"
    source.mkdir()
    for file in sorted((ROOT / "horizon_weaver").glob("*.py")):
        (source / file.name).write_bytes(file.read_bytes())
    summary = {
        "version": __version__, "code_hash": code_fingerprint(),
        "validation": "fixed development traces and real local file tools, no model",
        "context": context_comparison(output), "tools": tool_comparison(output),
        "model_validation": "not_run", "docker_validation": "not_run",
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    context, tools = summary["context"], summary["tools"]
    report = f"""# v1.1 状态机制开发对照

使用固定历史轨迹和 ScriptedPolicy，工具对照真实执行本地文件操作。此结果只验证机制行为，不代表真实模型任务成功率或 Terminal-Bench 得分。源代码指纹：`{summary['code_hash']}`。

| 指标 | recent 或关闭 guard | state 或开启 guard |
| --- | --- | --- |
| 输入 UTF8 字节上界 | {context['recent']['input_utf8_byte_bound']} | {context['state']['input_utf8_byte_bound']} |
| 旧关键观察保留 | {context['recent']['required_observation_retained']} | {context['state']['required_observation_retained']} |
| 实际 READ 和 WRITE 调用 | {tools['unguarded']['actual_read_write_calls']} | {tools['guard']['actual_read_write_calls']} |
| 产物内容 | {tools['unguarded']['answer']} | {tools['guard']['answer']} |
| 运行终态 | {tools['unguarded']['termination']} | {tools['guard']['termination']} |

输入上界在此历史噪声场景降低 {context['input_bound_reduction_percent']}%。字节上界来自既有保守计量方法，不是实际模型 token；固定 Policy 的决策步数保持相同。外部验收保持待评分，没有将提交认定为成功。

原始上下文、固定轨迹、完整事件、IR 导出和源码副本均保存在本目录。Docker 和真实模型验证未在这个脚本中执行。
"""
    (output / "report.md").write_text(report, encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
