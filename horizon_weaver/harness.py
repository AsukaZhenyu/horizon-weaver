"""The only component allowed to operate on the task workspace.

Docker is the isolated backend. LocalHarness is for explicitly trusted development
workspaces: path checks protect file tools, but do not sandbox arbitrary commands.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .core import (Action, ActionKind, Check, FileState, StateSnapshot,
                   TaskContract, ToolResult, json_equal, parse_json, validate_relative_path)
from .errors import HarnessError


def run_process(args: list[str] | str, cwd: Path | None, timeout_s: float,
                *, shell: bool = False, stdin: str | None = None) -> ToolResult:
    started = time.monotonic()
    process = None
    job = None
    try:
        if os.name == "nt":
            from .windows_job import WindowsJob
            job = WindowsJob()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen(
            args, cwd=cwd, shell=shell, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x4) if os.name == "nt" else 0,
        )
        if job is not None:
            job.attach_and_resume(process)
        try:
            stdout, stderr = process.communicate(
                stdin.encode("utf-8") if stdin is not None else None, timeout=max(.001, timeout_s)
            )
        except subprocess.TimeoutExpired:
            if job is not None:
                job.terminate()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
            return ToolResult(output=(stdout + stderr).decode("utf-8", errors="replace"),
                              error_type="timeout", duration_s=time.monotonic() - started,
                              outcome_unknown=True)
        return ToolResult(output=(stdout + stderr).decode("utf-8", errors="replace"),
                          exit_code=process.returncode, duration_s=time.monotonic() - started)
    except Exception as error:
        if process is not None and process.poll() is None:
            if job is not None:
                job.close()
            process.kill()
            process.communicate(timeout=5)
        return ToolResult(output=str(error), error_type="execution_error",
                          duration_s=time.monotonic() - started,
                          outcome_unknown=process is not None)
    finally:
        # Covers interruption and background children left by a completed shell.
        if job is not None:
            job.close()
        elif process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


class WorkspaceHarness:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.task: TaskContract | None = None
        self.allowed: tuple[Path, ...] = ()

    def start(self, task: TaskContract, timeout_s: float = 30) -> None:
        if not self.workspace.is_dir():
            raise HarnessError(f"workspace does not exist: {self.workspace}")
        self.allowed = tuple(self._resolve(path) for path in task.allowed_paths)
        if any(not path.is_dir() for path in self.allowed):
            raise HarnessError("allowed_paths must identify existing workspace directories")
        self.task = task

    def _resolve(self, path: str) -> Path:
        validate_relative_path(path)
        resolved = (self.workspace / path).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise PermissionError(f"path escapes workspace: {path}")
        return resolved

    def _path(self, path: str) -> Path:
        if self.task is None:
            raise HarnessError("harness is not started")
        resolved = self._resolve(path)
        if not any(resolved.is_relative_to(allowed) for allowed in self.allowed):
            raise PermissionError(f"path is outside allowed_paths: {path}")
        return resolved

    def _read_text(self, path: str) -> str:
        # Universal-newline conversion would make CRLF/LF compare equal and
        # would change the content reported by a file observation.
        with self._path(path).open(encoding="utf-8", newline="") as stream:
            return stream.read()

    def snapshot(self) -> StateSnapshot:
        if self.task is None:
            raise HarnessError("harness is not started")
        files: list[FileState] = []
        paths: set[Path] = set()
        for allowed in self.allowed:
            if allowed != self.workspace:
                paths.add(allowed)
            paths.update(allowed.rglob("*"))
        for path in sorted(paths):
            relative = path.relative_to(self.workspace)
            if ".git" in relative.parts:
                continue
            metadata = path.lstat()
            if path.is_symlink():
                kind, value = "symlink", os.readlink(path).encode("utf-8")
                sha = hashlib.sha256(value).hexdigest()
            elif path.is_file():
                kind = "file"
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                sha = hasher.hexdigest()
            elif path.is_dir():
                kind, sha = "directory", ""
            else:
                raise HarnessError(f"unsupported workspace entry: {relative}")
            files.append(FileState(path=relative.as_posix(), kind=kind, sha256=sha,
                                   size=metadata.st_size if kind != "directory" else 0,
                                   mode=stat.S_IMODE(metadata.st_mode)))
        return StateSnapshot(files=tuple(files))

    def execute(self, action: Action, timeout_s: float) -> ToolResult:
        started = time.monotonic()
        try:
            if action.kind == ActionKind.READ:
                output = self._read_text(action.path)
            elif action.kind == ActionKind.WRITE:
                path = self._path(action.path)
                mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".hw-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write((action.content or "").encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    if mode is not None:
                        os.chmod(temporary, mode)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                output = f"wrote {action.path}"
            elif action.kind == ActionKind.COMMAND:
                return self._command(action.command, timeout_s)
            else:
                # Verify/done request evaluation; they cannot supply new checks.
                output = "acceptance requested"
            return ToolResult(output=output, exit_code=0, duration_s=time.monotonic() - started)
        except (OSError, ValueError) as error:
            return ToolResult(output=str(error), error_type=type(error).__name__,
                              duration_s=time.monotonic() - started)

    def check(self, check: Check, timeout_s: float) -> ToolResult:
        if check.kind == "command":
            return self._command(check.command, timeout_s)
        started = time.monotonic()
        try:
            actual = self._read_text(check.path)
            passed = (actual == check.expected if check.kind == "text_equals"
                      else json_equal(parse_json(actual), parse_json(check.expected)))
            return ToolResult(output="matched" if passed else "value does not match contract",
                              exit_code=0 if passed else 1,
                              duration_s=time.monotonic() - started)
        except (OSError, ValueError) as error:
            return ToolResult(output=str(error), exit_code=1,
                              error_type=type(error).__name__, duration_s=time.monotonic() - started)

    def _command(self, command: str, timeout_s: float) -> ToolResult:
        raise NotImplementedError

    def verify_python(self, source: str, payload: str, timeout_s: float) -> ToolResult:
        code = base64.b64encode(source.encode()).decode()
        data = base64.b64encode(payload.encode()).decode()
        return self._command('python -B -c "import base64;exec(base64.b64decode(\'' + code + '\'))" ' + data, timeout_s)

    def describe(self) -> dict:
        return {"backend": "workspace", "workspace": str(self.workspace)}

    def close(self) -> None:
        self.task = None


class LocalHarness(WorkspaceHarness):
    def verify_python(self, source: str, payload: str, timeout_s: float) -> ToolResult:
        return run_process([sys.executable, '-B', '-c', source], self.workspace, timeout_s, stdin=payload)

    def _command(self, command: str, timeout_s: float) -> ToolResult:
        return run_process(command, self.workspace, timeout_s, shell=True)

    def describe(self) -> dict:
        return {"backend": "local", "isolated": False, "platform": os.name,
                "shell": "cmd.exe" if os.name == "nt" else "/bin/sh",
                "working_directory": str(self.workspace)}


# The child owns a POSIX process group, so timeout kills grandchildren as well.
_CONTAINER_COMMAND = '''
import json, os, signal, subprocess, sys, time
request = json.load(sys.stdin)
started = time.monotonic()
process = subprocess.Popen(['/bin/sh', '-c', request['command']], cwd='/workspace',
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
try:
    out, err = process.communicate(timeout=request['timeout_s'])
    result = dict(output=(out+err).decode('utf-8', errors='replace'),
        exit_code=process.returncode, duration_s=time.monotonic()-started)
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL)
    out, err = process.communicate()
    result = dict(output=(out+err).decode('utf-8', errors='replace'),
        exit_code=124, error_type='timeout', outcome_unknown=False, execution_terminated=True,
        duration_s=time.monotonic()-started)
finally:
    # Successful shells can leave children with detached output pipes. Own their
    # lifetime too, so they cannot mutate an already evaluated workspace later.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
print(json.dumps(result))
'''


class DockerHarness(WorkspaceHarness):
    def __init__(self, workspace: Path, image: str = "python:3.12-slim"):
        super().__init__(workspace)
        self.image = image
        self.container: str | None = None
        self.image_id: str | None = None

    def start(self, task: TaskContract, timeout_s: float = 30) -> None:
        deadline = time.monotonic() + timeout_s
        super().start(task, timeout_s)
        inspected = run_process(["docker", "image", "inspect", self.image,
                                 "--format", "{{.Id}}"], None, min(10, max(.001, deadline - time.monotonic())))
        if inspected.exit_code != 0:
            raise HarnessError(f"Docker image unavailable (no automatic pull): {inspected.output}")
        self.image_id = inspected.output.strip()
        if "," in str(self.workspace):
            raise HarnessError("Docker mount source cannot contain a comma")
        self.container = "horizon-weaver-" + uuid.uuid4().hex
        args = ["docker", "run", "--name", self.container, "--detach", "--rm", "--pull", "never",
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "128",
                "--memory", "512m", "--cpus", "1", "--tmpfs", "/tmp:rw,size=64m",
                "--workdir", "/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        if self.workspace in self.allowed:
            args += ["--mount", f"type=bind,source={self.workspace},target=/workspace"]
        else:
            args += ["--tmpfs", "/workspace:rw,size=64m"]
            for allowed in self.allowed:
                relative = allowed.relative_to(self.workspace).as_posix()
                args += ["--mount", f"type=bind,source={allowed},target=/workspace/{relative}"]
        args += [self.image_id, "python", "-c", "import time; time.sleep(86400)"]
        if time.monotonic() >= deadline:
            raise TimeoutError("Docker startup budget exhausted")
        result = run_process(args, None, min(20, deadline - time.monotonic()))
        if result.exit_code != 0:
            raise HarnessError(f"cannot start Docker environment: {result.output}")

    def _command(self, command: str, timeout_s: float) -> ToolResult:
        if self.container is None:
            raise HarnessError("Docker environment is not started")
        result = run_process(["docker", "exec", "-i", self.container, "python", "-c",
                              _CONTAINER_COMMAND], None, timeout_s + 3,
                             stdin=json.dumps({"command": command, "timeout_s": timeout_s}))
        if result.exit_code != 0:
            error_type = result.error_type or "docker_exec_error"
            return result.model_copy(update={"error_type": error_type, "outcome_unknown": True,
                "output": result.output or f"Docker exec failed ({error_type}); container={self.container}"})
        try:
            return ToolResult.model_validate_json(result.output)
        except ValueError:
            return result.model_copy(update={"error_type": "docker_protocol_error", "outcome_unknown": True})

    def describe(self) -> dict:
        return {"backend": "docker", "image": self.image, "image_id": self.image_id,
                "isolated": True, "network": "none", "shell": "/bin/sh", "working_directory": "/workspace"}

    def close(self) -> None:
        if self.container is not None:
            container = self.container
            result = run_process(["docker", "rm", "--force", container], None, 10)
            if result.exit_code != 0 and "No such container" not in result.output:
                failure = result.error_type or f"exit_code={result.exit_code}"
                raise HarnessError(f"cannot dispose Docker environment {container} ({failure}): {result.output}")
            # Keep ownership until the daemon confirms removal, so a later close
            # can reconcile a request whose response timed out.
            self.container = None
        super().close()
