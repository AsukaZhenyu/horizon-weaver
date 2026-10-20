"""Container-side worker for caller-owned Docker environments (standard library only)."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_identity(pid):
    try:
        # Field 22 follows the parenthesized comm field, which may contain spaces.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def job_operation(request, root):
    session = Path(request["session_root"])
    if (session.parent != Path("/tmp") or not session.name.startswith("horizon-weaver-")
            or session.is_symlink() or session.stat().st_uid != os.getuid()
            or stat.S_IMODE(session.stat().st_mode) != 0o700):
        raise ValueError("invalid private execution directory")
    identity = request["execution_id"]
    if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("invalid execution identity")
    path = session / (identity + ".json")
    operation = request["operation"]
    with (session / (identity + ".lock")).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if operation == "command_prepare":
            if path.exists():
                raise ValueError("execution identity already exists; never replay")
            record = dict(state="prepared", cancel_requested=False,
                          command_hash=hashlib.sha256(request["command"].encode()).hexdigest())
            atomic_json(path, record)
            return record
        record = json.loads(path.read_text())
        if operation == "command_status":
            if record["state"] == "running":
                output = session / (identity + ".out")
                if output.exists():
                    with output.open("rb") as stream:
                        stream.seek(max(0, output.stat().st_size - 4096))
                        record["output_tail"] = stream.read().decode("utf-8", errors="replace")
            return record
        if operation == "command_cancel":
            if record["state"] == "finished":
                return record
            record["cancel_requested"] = True
            atomic_json(path, record)
            if (record["state"] == "running" and record["pid_identity"] is not None
                    and process_identity(record["pid"]) == record["pid_identity"]):
                try:
                    os.killpg(record["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif record["state"] == "prepared":
                record.update(state="finished", result=dict(exit_code=124, error_type="cancelled",
                    output="Cancelled before process launch", execution_terminated=True))
                atomic_json(path, record)
            return record
        if operation != "command":
            raise ValueError("unsupported execution operation")
        if record["state"] != "prepared":
            # Duplicate command RPC returns a receipt, never starts another process.
            return record
        if record["command_hash"] != hashlib.sha256(request["command"].encode()).hexdigest():
            raise ValueError("command differs from its reserved execution")
        started = time.monotonic()
        output_path = session / (identity + ".out")
        with output_path.open("wb") as output:
            process = subprocess.Popen([request["shell"], "-c", request["command"]], cwd=root,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        record.update(state="running", pid=process.pid, pid_identity=process_identity(process.pid))
        atomic_json(path, record)
    # Output is a file rather than a pipe, so descendants cannot block communicate.
    error_type = None
    try:
        process.wait(timeout=request["timeout_s"])
    except subprocess.TimeoutExpired:
        error_type = "timeout"
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    with (session / (identity + ".lock")).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text())
        error_type = error_type or ("cancelled" if record["cancel_requested"] else None)
        result = dict(output=output_path.read_bytes().decode("utf-8", errors="replace"),
            exit_code=124 if error_type else process.returncode, error_type=error_type,
            execution_terminated=bool(error_type), duration_s=time.monotonic() - started,
            execution_id=identity)
        record.update(state="finished", result=result)
        atomic_json(path, record)
        return record


def dispatch(request: dict) -> dict:
    root = Path(request["workspace"]).resolve()
    scopes = tuple((root / path).resolve() for path in request["allowed_paths"])
    if not root.is_dir() or any(not scope.is_relative_to(root) or not scope.is_dir() for scope in scopes):
        raise ValueError("workspace scopes must be existing directories within the container workspace")
    operation = request["operation"]
    if operation == "snapshot":
        entries: set[Path] = set()
        for scope in scopes:
            if scope != root:
                entries.add(scope)
            for directory, directories, files in os.walk(scope, followlinks=False):
                directories[:] = [name for name in directories if name != ".git"]
                entries.update(Path(directory) / name for name in directories + files)
                if len(entries) > 10000:
                    raise ValueError("workspace snapshot exceeds 10000 entries")
        result = []
        for path in sorted(entries):
            metadata = path.lstat()
            if path.is_symlink():
                kind = "symlink"
                sha = hashlib.sha256(os.readlink(path).encode()).hexdigest()
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
                raise ValueError(f"unsupported workspace entry: {path}")
            result.append(dict(path=path.relative_to(root).as_posix(), kind=kind, sha256=sha,
                               size=metadata.st_size if kind != "directory" else 0,
                               mode=stat.S_IMODE(metadata.st_mode)))
        return {"files": result}
    if operation == "start":
        session = Path(request["session_root"])
        if session.parent != Path("/tmp") or not session.name.startswith("horizon-weaver-"):
            raise ValueError("invalid execution directory")
        session.mkdir(mode=0o700)
        return {"output": "attached", "exit_code": 0}
    if operation == "close":
        session = Path(request["session_root"])
        if session.parent != Path("/tmp") or not session.name.startswith("horizon-weaver-") or session.is_symlink():
            raise ValueError("invalid execution directory")
        if session.exists():
            for path in session.glob("*.json"):
                if json.loads(path.read_text())["state"] != "finished":
                    raise ValueError("cannot remove records for unfinished execution")
            for path in session.iterdir():
                path.unlink()
            session.rmdir()
        return {"exit_code": 0, "output": "execution records released"}
    if operation.startswith("command"):
        return job_operation(request, root)
    path_text = request["path"]
    if not path_text or "\\" in path_text or ":" in path_text or path_text.startswith("/") or ".." in path_text.split("/"):
        raise ValueError("file tools require a workspace-relative POSIX path")
    path = (root / path_text).resolve()
    if not any(path.is_relative_to(scope) for scope in scopes):
        raise PermissionError("file path escapes the configured workspace scopes")
    if operation == "read":
        with path.open(encoding="utf-8", newline="") as stream:
            return {"output": stream.read(), "exit_code": 0}
    if operation != "write":
        raise ValueError("unsupported worker operation")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hw-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(request["content"].encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"output": f"wrote {path_text}", "exit_code": 0}


if __name__ == "__main__":
    try:
        print(json.dumps(dispatch(json.load(sys.stdin))))
    except Exception as error:
        print(json.dumps(dict(output=str(error), error_type=type(error).__name__, exit_code=1)))
