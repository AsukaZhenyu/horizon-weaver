"""Separate solver and post-hoc judge entry points for math1, no gold feedback."""
from __future__ import annotations
import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from budget_backend import make_llm

def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("solver", "judge"))
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    task = json.loads(args.task.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(args.upstream), str(args.upstream / "benchmark" / "math")]
    from math_env import MathEnvironment
    llm = make_llm(args.upstream, config, args.phase, args.output)
    try:
        if args.phase == "solver":
            # This JSON and the entire solver filesystem must contain no answer file.
            if set(task) != {"id", "questions", "backgrounds"}:
                raise ValueError("solver input must contain only id/questions/backgrounds")
            from math_mage_agent import MathMAGEAgent, PaperSharedState
            from math_actions import CodeExecutorAction
            CodeExecutorAction.TIMEOUT_SECONDS = config["code_timeout_s"]
            os.environ["MAGE_CODE_IMAGE"] = config["code_image"]
            os.environ["MAGE_RUN_ID"] = config["run_id"]
            os.environ["DOCKER_CONTEXT"] = config["docker_context"]
            if not getattr(CodeExecutorAction, "SUPPORTS_ISOLATED_CODE", False):
                raise RuntimeError("Pinned isolated-code engineering patch is required")
            env = MathEnvironment(judge_llm=None)
            state = PaperSharedState(
                num_subtasks=len(task["questions"]), final_answer="",
                subtask_answers=[], max_judge_retries=0,
                task_id=int(task["id"]), log_dir=str(args.output),
            )
            status = "completed"
            try:
                for index, question in enumerate(task["questions"]):
                    agent = MathMAGEAgent(
                        question=question, background=task["backgrounds"][index],
                        subtask_idx=index, shared_state=state, math_env=env, llm=llm,
                        max_steps=config["max_steps"], PROMPT_DEBUG_FLAG=True,
                        log_file=str(args.output / f"task_{task['id']}_subtask_{index}.log"),
                        max_verify_retries=config["max_verify_retries"],
                        max_check_retries=config["max_check_retries"],
                        max_backtrace=config["max_backtrace"],
                    )
                    agent.logger.FLAG_PRINT = False
                    agent(agent.build_task())
                    records = {r.subtask_idx: r for r in state.subtask_tree.active_records()}
                    write(args.output / "predictions.json", {
                        "task_id": task["id"], "status": "partial",
                        "predictions": [records[i].submitted_answer if i in records else "" for i in range(len(task["questions"]))],
                    })
            except BaseException:
                status = "error"
                (args.output / "solver_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
                raise
            finally:
                records = {r.subtask_idx: r for r in state.subtask_tree.active_records()}
                write(args.output / "predictions.json", {
                    "task_id": task["id"], "status": status,
                    "predictions": [records[i].submitted_answer if i in records else "" for i in range(len(task["questions"]))],
                    "judge_retries": state.judge_count,
                    "tree_nodes": len(state.exploration_tree.nodes),
                    "summary_nodes": len(state.exploration_tree.summaries),
                })
        else:
            if args.predictions is None:
                raise ValueError("--predictions required")
            prediction = json.loads(args.predictions.read_text(encoding="utf-8"))
            env = MathEnvironment(judge_llm=llm)
            env.strict_judge = True
            backgrounds, scores, explanations = [], [], []
            for index, question in enumerate(task["questions"]):
                background = env._strip_latex_preamble(task["backgrounds"][index])
                if background and background not in backgrounds:
                    backgrounds.append(background)
                correct, explanation = env.judge_answer(
                    question=question, background="\n\n".join(backgrounds),
                    predicted=prediction["predictions"][index], reference=task["answers"][index],
                )
                scores.append(int(correct))
                explanations.append(explanation)
            write(args.output / "scores.json", {
                "task_id": task["id"], "sub_scores": scores,
                "task_success": bool(scores and scores[-1]),
                "all_subtasks_pass": bool(scores and all(scores)),
                "solver_status": prediction["status"],
                "judge_explanations": explanations,
                "gold_feedback_to_solver": False,
            })
    finally:
        llm.persist()
        llm.client.close()

if __name__ == "__main__":
    main()
