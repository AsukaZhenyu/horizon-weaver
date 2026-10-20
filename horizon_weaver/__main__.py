"""python -m horizon_weaver run|inspect"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import NodeAgent, read_run
from .core import Action, TaskContract, Termination, parse_json
from .policy import ScriptedPolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Horizon-Weaver v1 atomic task runtime")
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--backend", choices=["docker", "local"], default="docker",
                     help="local is for trusted development workspaces, without command isolation")
    run.add_argument("--image", default="python:3.12-slim")
    run.add_argument("--model", default="qwen3.5-9b")
    run.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    run.add_argument("--temperature", type=float, default=.2)
    run.add_argument("--structured-output", choices=["none", "json_schema"])
    run.add_argument("--policy-style", choices=["ir", "terminal"], default="ir")
    run.add_argument("--context-strategy", choices=["state", "recent"], default="state")
    run.add_argument("--transition-guard", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--actions", type=Path, help="deterministic action fixture instead of a model")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--run-dir", type=Path, required=True)
    inspect.add_argument("--state-ir", action="store_true", help="export the versioned state representation")
    args = parser.parse_args(argv)
    try:
        if args.operation == "inspect":
            graph = read_run(args.run_dir)
            if args.state_ir:
                print(graph.ir.model_dump_json(indent=2) if graph.ir is not None else "null")
                return 0
            print(json.dumps({"manifest": graph.manifest.model_dump(mode="json"),
                "result": graph.result.model_dump(mode="json") if graph.result else None,
                "pending_steps": list(graph.pending), "usage": graph.usage.model_dump(mode="json"),
                "view": graph.view.model_dump(mode="json")}, ensure_ascii=False, indent=2))
            return 0
        task = TaskContract.model_validate(parse_json(args.contract.read_text(encoding="utf-8")))
        policy = None
        if args.actions:
            actions = parse_json(args.actions.read_text(encoding="utf-8"))
            policy = ScriptedPolicy(tuple(Action.model_validate(action) for action in actions))
        result = NodeAgent(backend=args.backend, image=args.image, model=args.model,
                           base_url=args.base_url, temperature=args.temperature, policy=policy,
                           structured_output=args.structured_output, policy_style=args.policy_style,
                           context_strategy=args.context_strategy, transition_guard=args.transition_guard).run(
                               task, args.workspace, args.run_dir, resume=args.resume)
        print(result.model_dump_json(indent=2))
        return 0 if result.termination == Termination.SUCCESS else 1
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

