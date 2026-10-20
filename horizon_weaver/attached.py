"""HarnessPort adapter for an existing Docker container owned by a benchmark."""
from __future__ import annotations

import json
import base64
import time
import uuid
from pathlib import Path, PurePosixPath

from .core import Action, ActionKind, Check, StateSnapshot, TaskContract, ToolResult, json_equal, parse_json
from .errors import HarnessError
from .harness import run_process


class AttachedDockerHarness:
    def __init__(self, container_id: str, *, workspace: str = "/app", user: str = "",
                 image: str = "", python: str = "python3", shell: str = "/bin/sh"):
        if not container_id or container_id.startswith("-"):
            raise ValueError("a concrete container identity is required")
        if not PurePosixPath(workspace).is_absolute() or ".." in PurePosixPath(workspace).parts:
            raise ValueError("container workspace must be an absolute POSIX path")
        self.container_id, self.workspace, self.user = container_id, workspace, user
        self.image, self.python, self.shell = image, python, shell
        self.task: TaskContract | None = None
        self.session_root = "/tmp/horizon-weaver-" + uuid.uuid4().hex
        self._executions: set[str] = set()
        self._worker = Path(__file__).with_name("attached_worker.py").read_text(encoding="utf-8")

    def _call(self, operation: str, timeout_s: float, **values) -> dict:
        if self.task is None:
            raise HarnessError("attached harness is not started")
        request = dict(operation=operation, workspace=self.workspace,
                       allowed_paths=self.task.allowed_paths, shell=self.shell,
                       timeout_s=timeout_s, session_root=self.session_root, **values)
        args = ["docker", "exec", "-i"]
        if self.user:
            args += ["--user", self.user]
        result = run_process(args + [self.container_id, self.python, "-c", self._worker],
                             None, timeout_s + (5 if operation == "command" else 2), stdin=json.dumps(request))
        if result.exit_code != 0:
            return result.model_copy(update={"error_type": result.error_type or "docker_exec_error",
                "outcome_unknown": True, "output": result.output or "attached Docker transport failed"}).model_dump()
        try:
            value = parse_json(result.output)
            if not isinstance(value, dict):
                raise ValueError("worker response must be an object")
            return value
        except ValueError:
            return dict(error_type="docker_protocol_error", outcome_unknown=True, output=result.output)

    def start(self, task: TaskContract, timeout_s: float = 30) -> None:
        self.task = task
        result = ToolResult.model_validate(self._call("start", timeout_s))
        if result.exit_code != 0 or result.error_type:
            raise HarnessError(f"cannot attach benchmark workspace: {result.output}")

    def snapshot(self) -> StateSnapshot:
        result = self._call("snapshot", 10)
        if "files" not in result:
            raise HarnessError(f"cannot snapshot benchmark workspace: {result}")
        return StateSnapshot.model_validate(result)

    def execute(self, action: Action, timeout_s: float) -> ToolResult:
        if action.kind in (ActionKind.VERIFY, ActionKind.DONE):
            return ToolResult(exit_code=0, output="acceptance requested")
        if action.kind == ActionKind.COMMAND:
            identity = uuid.uuid4().hex
            self._executions.add(identity)
            prepared = self._call("command_prepare", 2, execution_id=identity, command=action.command)
            if prepared.get("state") != "prepared":
                prepared = self.command_status(identity)
            if prepared.get("state") != "prepared":
                # Reservation never executes a command; failure here is known.
                self._executions.discard(identity)
                return ToolResult(exit_code=1, error_type="command_prepare_failed",
                                  output=prepared.get("output", "command was not launched"), execution_id=identity)
            response = self._call("command", timeout_s, execution_id=identity, command=action.command)
            recovered = response.get("state") != "finished"
            if response.get("state") != "finished":
                # A lost transport is reconciled by identity, never by replaying the command.
                response = self.command_status(identity)
                if response.get("state") != "finished":
                    response = self.cancel_command(identity)
                for _ in range(2):
                    if response.get("state") == "finished":
                        break
                    time.sleep(.1)
                    response = self.command_status(identity)
            if response.get("state") == "finished":
                self._executions.discard(identity)
                return ToolResult.model_validate(dict(response["result"], execution_id=identity,
                                                      transport_recovered=recovered))
            return ToolResult(error_type="docker_exec_error", outcome_unknown=True, execution_id=identity,
                output="Original command status could not be confirmed; it was not replayed. "
                       + str(response.get("output", "")))
        values = ({"command": action.command} if action.kind == ActionKind.COMMAND
                  else {"path": action.path})
        if action.kind == ActionKind.WRITE:
            values["content"] = action.content or ""
        return ToolResult.model_validate(self._call(action.kind.value, timeout_s, **values))

    def command_status(self, execution_id: str) -> dict:
        """Read a reserved command's status/output; does not execute its command."""
        return self._call("command_status", 2, execution_id=execution_id)

    def verify_python(self, source: str, payload: str, timeout_s: float) -> ToolResult:
        code = base64.b64encode(source.encode()).decode()
        data = base64.b64encode(payload.encode()).decode()
        command = '"' + self.python + '" -B -c "import base64;exec(base64.b64decode(\'' + code + '\'))" ' + data
        return self.execute(Action(kind='command', command=command), timeout_s)

    def cancel_command(self, execution_id: str) -> dict:
        return self._call("command_cancel", 2, execution_id=execution_id)

    def check(self, check: Check, timeout_s: float) -> ToolResult:
        if check.kind == "external":
            raise HarnessError("external checks are owned by the benchmark evaluator")
        if check.kind == "command":
            return self.execute(Action(kind="command", command=check.command), timeout_s)
        result = self.execute(Action(kind="read", path=check.path), timeout_s)
        if result.exit_code != 0 or result.error_type:
            return result
        try:
            passed = (result.output == check.expected if check.kind == "text_equals"
                      else json_equal(parse_json(result.output), parse_json(check.expected)))
            return result.model_copy(update={"exit_code": 0 if passed else 1,
                "output": "matched" if passed else "value does not match contract"})
        except ValueError as error:
            return ToolResult(exit_code=1, error_type="ValueError", output=str(error))

    def describe(self) -> dict:
        return dict(backend="attached_docker", container_id=self.container_id, image=self.image,
                    workspace=self.workspace, user=self.user, shell=self.shell, python=self.python,
                    container_owner="caller", snapshot_scope="workspace files only",
                    command_scope="benchmark container", persistent_shell=False,
                    background_processes="terminated after each command")

    def close(self) -> None:
        # The benchmark must retain the container to run its hidden tests.
        if self.task is None:
            return
        for identity in tuple(self._executions):
            self.cancel_command(identity)
            status = self.command_status(identity)
            if status.get("state") != "finished":
                raise HarnessError(f"cannot confirm termination of owned command {identity}")
            self._executions.discard(identity)
        response = self._call("close", 2)
        if response.get("exit_code") != 0:
            raise HarnessError("cannot release execution records: " + str(response.get("output", "")))
        self.task = None
