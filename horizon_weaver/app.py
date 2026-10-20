"""Composition root and public API for the v1 Node Agent."""
from __future__ import annotations

import importlib.metadata
import json
import platform
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable, cast

from .composition import Component, Runtime
from .context import ContextCompiler, SYSTEM
from .control import Loop, Supervisor
from .core import RunEvent, RunManifest, RunResult, TaskContract, Termination, canonical, digest
from .evaluation import Evaluator, FailureReasoner, ProgressMonitor
from .errors import HarnessError
from .harness import DockerHarness, LocalHarness
from .policy import ModelGateway, ModelPolicy
from .ports import HarnessPort, PolicyPort
from .reporting import JsonReporter
from .event_store import EventStore
from .state_graph import StateGraph
from .terminal_policy import TerminalPolicy
from .transitions import TransitionGuard


def code_fingerprint() -> str:
    root = Path(__file__).parent
    return digest("\n".join(path.name + ":" + digest(path.read_text(encoding="utf-8"))
                            for path in sorted(root.glob("*.py"))))


def read_run(run_dir: Path) -> StateGraph:
    path = (run_dir / "events.sqlite3").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        first = connection.execute("SELECT event FROM events ORDER BY sequence LIMIT 1").fetchone()
    if first is None:
        raise ValueError("run has no events")
    run_id = RunEvent.model_validate_json(first[0]).run_id
    store = EventStore(path, run_id, readonly=True)
    try:
        return StateGraph(store.read())
    finally:
        store.close()


class NodeAgent:
    def __init__(self, *, backend: str = "docker", image: str = "python:3.12-slim",
                 model: str = "qwen3.5-9b", base_url: str = "http://127.0.0.1:8080/v1",
                 temperature: float = .2, policy: PolicyPort | None = None,
                 structured_output: str | None = None, policy_style: str = "ir",
                 context_strategy: str = "state", transition_guard: bool = True):
        if backend not in ("local", "docker"):
            raise ValueError("backend must be local or docker")
        self.backend, self.image = backend, image
        self.model, self.base_url, self.temperature = model, base_url, temperature
        self.policy = policy
        if policy_style not in ("ir", "terminal"):
            raise ValueError("policy_style must be ir or terminal")
        self.policy_style = policy_style
        if structured_output is None:
            structured_output = "none" if policy_style == "terminal" else "json_schema"
        if structured_output not in ("none", "json_schema"):
            raise ValueError("structured_output must be none or json_schema")
        if policy_style == "terminal" and structured_output != "none":
            raise ValueError("terminal frontend requires structured_output=none")
        self.structured_output = structured_output
        if context_strategy not in ("state", "recent"):
            raise ValueError("context strategy must be state or recent")
        if not isinstance(transition_guard, bool):
            raise ValueError("transition_guard must be a boolean")
        self.context_strategy, self.transition_guard = context_strategy, transition_guard

    def run(self, task: TaskContract, workspace: Path, run_dir: Path,
            *, resume: bool = False) -> RunResult:
        task = TaskContract.model_validate(task.model_dump(mode="json"))
        workspace, run_dir = workspace.resolve(), run_dir.resolve()
        if run_dir.is_relative_to(workspace):
            raise ValueError("run_dir must be outside the task workspace to keep snapshots independent of logs")
        if workspace.is_relative_to(run_dir):
            raise ValueError("task workspace cannot be inside run_dir")
        return self._run(task, str(workspace), run_dir, self.backend,
                         lambda: LocalHarness(workspace) if self.backend == "local"
                         else DockerHarness(workspace, self.image), resume=resume)

    def run_attached(self, task: TaskContract, harness: HarnessPort, run_dir: Path,
                     *, workspace_id: str, resume: bool = False) -> RunResult:
        """Run on a caller-owned environment through the same typed runtime.

        The adapter's close must release its own resources without destroying
        the caller's environment. workspace_id is an opaque durable identity,
        never a host path to a container filesystem.
        """
        if not workspace_id.strip():
            raise ValueError("attached workspace identity must be nonempty")
        task = TaskContract.model_validate(task.model_dump(mode="json"))
        return self._run(task, workspace_id, run_dir.resolve(), "attached",
                         lambda: harness, resume=resume)

    def _run(self, task: TaskContract, workspace_id: str, run_dir: Path,
             backend: str, harness_factory: Callable[[], HarnessPort], *, resume: bool) -> RunResult:
        path = run_dir / "events.sqlite3"
        if path.exists():
            old = read_run(run_dir)
            if not resume:
                raise ValueError("run directory already contains a run; select resume or use a new directory")
            assert old.manifest is not None
            run_id = old.manifest.run_id
        else:
            if resume:
                raise ValueError("cannot resume: event journal is missing")
            run_id = uuid.uuid4().hex

        def store_factory(services, resources):
            store = EventStore(path, run_id)
            resources.callback(store.close)
            return {"store": store}

        def make_harness(services, resources):
            harness = harness_factory()
            resources.callback(harness.close)
            return {"harness": harness}

        def policy_factory(services, resources):
            if self.policy is not None:
                return {"policy": self.policy}
            gateway = ModelGateway(self.model, self.base_url, self.temperature,
                                   structured_output=self.structured_output)
            resources.callback(gateway.close)
            environment = services['harness'].describe()
            workspace = environment.get('working_directory', environment.get('workspace', ''))
            if self.policy_style == "terminal":
                return {"policy": TerminalPolicy(gateway, workspace)}
            return {"policy": ModelPolicy(gateway, workspace)}

        components = (
            Component("event_store", ("store",), (), store_factory),
            Component("harness", ("harness",), (), make_harness),
            Component("policy", ("policy",), ("harness",), policy_factory),
            Component("evaluator", ("evaluator",), ("harness",),
                      lambda services, resources: {"evaluator": Evaluator(services["harness"])}),
            Component("context_compiler", ("compiler",), ("harness", "policy"),
                      lambda services, resources: {"compiler": ContextCompiler(services["harness"].describe(),
                          system=getattr(services["policy"], "system_prompt", SYSTEM),
                          strategy=self.context_strategy)}),
            Component("transition_guard", ("guard",), (),
                      lambda services, resources: {"guard": TransitionGuard() if self.transition_guard else None}),
            Component("progress_monitor", ("monitor",), (),
                      lambda services, resources: {"monitor": ProgressMonitor()}),
            Component("failure_reasoner", ("reasoner",), (),
                      lambda services, resources: {"reasoner": FailureReasoner()}),
            Component("loop", ("loop",), ("harness", "policy", "compiler", "evaluator", "monitor", "reasoner", "store", "guard"),
                      lambda services, resources: {"loop": Loop(
                          services["harness"], services["policy"], services["compiler"],
                          services["evaluator"], services["monitor"], services["reasoner"], services["store"],
                          services["guard"])}),
            Component("supervisor", ("supervisor",), ("loop", "harness", "evaluator", "store"),
                      lambda services, resources: {"supervisor": Supervisor(
                          services["loop"], services["harness"], services["evaluator"], services["store"])}),
        )
        result: RunResult | None = None
        try:
            with Runtime(components) as services:
                manifest = RunManifest(run_id=run_id, contract=task, workspace=workspace_id,
                    backend=backend,
                    environment_json=canonical({"python": platform.python_version(), "platform": platform.platform(),
                        "pydantic": importlib.metadata.version("pydantic"),
                        "httpx": importlib.metadata.version("httpx"),
                        "harness": services["harness"].describe()}),
                    policy_json=canonical(services["policy"].describe()), code_hash=code_fingerprint(),
                    runtime_json=canonical({"context_strategy": self.context_strategy,
                                            "transition_guard": self.transition_guard}))
                result = cast(Supervisor, services["supervisor"]).run(manifest, resume=resume)
        except HarnessError:
            # The supervisor already journaled a cleanup failure. A second
            # resource-owner cleanup attempt must not suppress that report.
            if result is None or result.termination != Termination.HARNESS_ERROR:
                raise
        assert result is not None
        JsonReporter().write(result, run_dir)
        return result

