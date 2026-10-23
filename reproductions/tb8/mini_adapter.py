"""Run official mini-swe-agent 2.4.6 inside the original TB task container.

Only installation, endpoint routing, budgets and measurement live here. The
upstream mini.yaml, InteractiveAgent, bash tool and LocalEnvironment are used
unchanged. Hidden tests remain the responsibility of Terminal-Bench 0.2.18.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import socket
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from terminal_bench.agents.base_agent import AgentResult
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.agents.installed_agents.mini_swe_agent.mini_swe_agent import MiniSweAgent

VERSION = "2.4.6"
LITELLM_VERSION = "1.75.8"
CONTAINER_DIR = "/installed-agent/tb8-mini"
CONTAINER_LOGS = "/agent-logs"


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def put_bytes(container, path, content):
    """Docker archive transport preserves bytes and never interpolates task text."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    directory, filename = path.rsplit("/", 1)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as out:
        info = tarfile.TarInfo(filename)
        info.size, info.mode = len(content), 0o600
        out.addfile(info, io.BytesIO(content))
    container.put_archive(directory, archive.getvalue())


def read_bytes(container, path):
    stream, _ = container.get_archive(path)
    data = io.BytesIO(b"".join(stream))
    with tarfile.open(fileobj=data, mode="r:*") as archive:
        files = [m for m in archive.getmembers() if m.isfile()]
        if len(files) != 1:
            raise ValueError(f"Expected one regular artifact at {path}")
        return archive.extractfile(files[0]).read()


class ProxyBudget:
    def __init__(self, directory, wall_seconds=300, max_total_tokens=200000,
                 max_output_tokens=8192, temperature=0.2):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.deadline = self.started + float(wall_seconds)
        self.max_total_tokens, self.max_output_tokens = int(max_total_tokens), int(max_output_tokens)
        self.temperature = float(temperature)
        self.attempts = self.prompt_tokens = self.completion_tokens = self.total_tokens = 0
        self.unknown = 0
        self.stop_reason = None
        self.lock = threading.Lock()
        self.connections = set()
        self.pending = set()
        self.handler_threads = set()

    def remaining(self):
        return self.deadline - time.monotonic()

    def gate(self):
        if self.remaining() <= 0:
            self.stop_reason = "wall_deadline"
        elif self.total_tokens >= self.max_total_tokens:
            self.stop_reason = "observed_token_budget"
        return self.stop_reason

    def event(self, event, **fields):
        row = dict(event=event, elapsed_seconds=time.monotonic() - self.started, **fields)
        with self.lock:
            with (self.directory / "transport.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def snapshot(self):
        return dict(http_attempts=self.attempts, known_prompt_tokens=self.prompt_tokens,
                    known_completion_tokens=self.completion_tokens, known_total_tokens=self.total_tokens,
                    unknown_usage_requests=self.unknown + len(self.pending), pending_usage_requests=len(self.pending), elapsed_seconds=time.monotonic() - self.started,
                    max_total_tokens=self.max_total_tokens, token_budget_is_observed_stop_threshold=True,
                    monetary_cost_usd=None, monetary_cost_status="not priced for local model",
                    stop_reason=self.stop_reason)


class ModelProxy:
    """Loopback-only, dummy-key relay; logs every physical completion request.

    The request and response artifacts contain task data, not HTTP credentials.
    It never reads an API key from the host environment.
    """
    def __init__(self, budget, upstream="http://127.0.0.1:8080/v1", port=8081):
        self.budget = budget
        self.upstream = urlsplit(upstream)
        if (self.upstream.scheme != "http" or self.upstream.hostname not in ("127.0.0.1", "localhost")
                or self.upstream.username or self.upstream.password or self.upstream.query):
            raise ValueError("mini proxy only forwards to the explicitly configured local HTTP model")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, payload):
                if not isinstance(payload, bytes):
                    payload = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                self.close_connection = True

            def do_GET(self):
                self.respond(200 if self.path == "/health" else 404, {"proxy": "ready", "model_contacted": False})

            def do_POST(self):
                budget = owner.budget
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self.respond(404, {"error": {"message": "Only chat completions are supported"}})
                    return
                if budget.gate():
                    self.respond(429, {"error": {"message": budget.stop_reason, "type": "budget_exhausted"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length < 32 * 1024 * 1024:
                        raise ValueError("invalid request size")
                    data = json.loads(self.rfile.read(length))
                    if data.get("stream"):
                        raise ValueError("Native mini completion must use non-streaming response")
                    data["max_tokens"] = min(int(data.get("max_tokens") or budget.max_output_tokens), budget.max_output_tokens)
                    data["temperature"] = budget.temperature
                    body = json.dumps(data, ensure_ascii=False).encode()
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self.respond(400, {"error": {"message": str(exc)}})
                    return
                with budget.lock:
                    budget.attempts += 1
                    number = budget.attempts
                    budget.pending.add(number)
                    budget.handler_threads.add(threading.current_thread())
                prefix = budget.directory / f"http-{number:04d}"
                prefix.with_suffix(".request.json").write_bytes(body)
                budget.event("http_start", request_number=number, request_sha256=hashlib.sha256(body).hexdigest(),
                             model=data.get("model"), max_tokens=data["max_tokens"], temperature=data["temperature"],
                             message_count=len(data.get("messages", [])), tools=data.get("tools"),
                             client_sdk_retry_count=self.headers.get("x-stainless-retry-count"))
                connection = http.client.HTTPConnection(owner.upstream.hostname, owner.upstream.port or 80,
                                                       timeout=max(0.01, budget.remaining()))
                with budget.lock:
                    budget.connections.add(connection)
                started = time.monotonic()
                try:
                    connection.request("POST", owner.upstream.path.rstrip("/") + "/chat/completions", body,
                                       {"Content-Type": "application/json", "Authorization": "Bearer EMPTY"})
                    response = connection.getresponse()
                    raw = response.read()
                    prefix.with_suffix(".response.json").write_bytes(raw)
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        parsed = {}
                    usage = parsed.get("usage")
                    known = (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens")))
                    if known:
                        budget.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                        budget.completion_tokens += int(usage.get("completion_tokens") or 0)
                        budget.total_tokens += usage["total_tokens"]
                    else:
                        budget.unknown += 1
                    budget.event("http_end", request_number=number, status_code=response.status,
                                 duration_seconds=time.monotonic() - started, usage=usage, usage_known=known,
                                 finish_reasons=[c.get("finish_reason") for c in parsed.get("choices", [])],
                                 response_sha256=hashlib.sha256(raw).hexdigest())
                    budget.gate()
                    self.respond(response.status, raw)
                except Exception as exc:
                    budget.unknown += 1
                    budget.event("http_error", request_number=number, error_type=type(exc).__name__, error=str(exc),
                                 duration_seconds=time.monotonic() - started, usage=None, usage_known=False)
                    budget.gate()
                    self.respond(504, {"error": {"message": str(exc), "type": "local_transport_error"}})
                finally:
                    connection.close()
                    with budget.lock:
                        budget.connections.discard(connection)
                        budget.pending.discard(number)
                        budget.handler_threads.discard(threading.current_thread())

        self.server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        with self.budget.lock:
            connections = list(self.budget.connections)
        for connection in connections:
            try:
                if connection.sock:
                    connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        with self.budget.lock:
            handlers = list(self.budget.handler_threads)
        for handler in handlers:
            handler.join(timeout=2)
        self.server.server_close()
        self.thread.join(timeout=2)


# This supervisor is environment plumbing only. All agent policy code is in the
# pinned installed package. Remember descendant PID start times so that deadline
# cleanup cannot kill an unrelated process after PID reuse.
SUPERVISOR = r'''
import ctypes, hashlib, importlib.metadata, json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path("/installed-agent/tb8-mini")
spec = json.loads((root / "run.json").read_text())
logs = pathlib.Path("/agent-logs")
status_path = logs / "mini-process.json"
stop_path = root / "stop"
owned = {}
# Orphaned/double-forked tool children are reparented here instead of PID 1.
# A process-group-only kill would miss LocalEnvironment's start_new_session tools.
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
def processes():
    result = {}
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit(): continue
        try:
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            result[int(entry.name)] = (int(stat[1]), stat[19], stat[0])
        except (OSError, ValueError, IndexError): pass
    return result
def remember(pid):
    table = processes()
    parents = {pid, os.getpid()} | set(owned)
    changed = True
    while changed:
        changed = False
        for child, (parent, stamp, state) in table.items():
            if parent in parents and child not in parents:
                parents.add(child); owned[child] = stamp; changed = True
    if pid in table: owned[pid] = table[pid][1]
def stop_owned():
    table = processes()
    killed = []
    for pid, stamp in reversed(list(owned.items())):
        if pid in table and table[pid][1] == stamp:
            try: os.kill(pid, signal.SIGKILL); killed.append(pid)
            except ProcessLookupError: pass
    return killed
def cleanup_owned():
    killed = []
    until = time.monotonic() + 3
    empty_count = 0
    while time.monotonic() < until:
        remember(process.pid)
        killed.extend(stop_owned())
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0: break
            except ChildProcessError: break
        remember(process.pid)
        table = processes()
        live = [pid for pid, stamp in owned.items()
                if pid in table and table[pid][1] == stamp and table[pid][2] != "Z"]
        empty_count = empty_count + 1 if not live else 0
        if empty_count >= 2: return True, sorted(set(killed))
        time.sleep(0.05)
    return False, sorted(set(killed))
started = time.monotonic()
env = os.environ.copy()
env.update(spec["env"])
with (logs / "mini-console.log").open("wb") as console:
    process = subprocess.Popen(spec["argv"], stdin=subprocess.DEVNULL, stdout=console,
                               stderr=subprocess.STDOUT, cwd=spec["cwd"], env=env, start_new_session=True)
    state = {"pid": process.pid, "started_monotonic": started, "state": "running", "cwd": spec["cwd"]}
    status_path.write_text(json.dumps(state))
    reason = None
    while process.poll() is None:
        remember(process.pid)
        if stop_path.exists(): reason = stop_path.read_text().strip() or "host_stop"
        elif time.monotonic() - started >= spec["wall_seconds"]: reason = "wall_deadline"
        if reason:
            killed = stop_owned()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: pass
            state["killed_owned_pids"] = killed
            break
        time.sleep(0.1)
    try:
        trajectory = json.loads((logs / "mini.traj.json").read_text())
        submitted = trajectory.get("info", {}).get("exit_status") == "Submitted" and process.poll() == 0 and not reason
    except (OSError, ValueError): submitted = False
    cleanup_confirmed = True
    if not submitted:
        cleanup_confirmed, state["killed_owned_pids"] = cleanup_owned()
    state["cleanup_confirmed"] = cleanup_confirmed
    state["native_submission_before_cleanup"] = submitted
    state.update(state="finished" if cleanup_confirmed else "cleanup_failed", exit_code=process.poll(), stop_reason=reason,
                 elapsed_seconds=time.monotonic() - started)
    status_path.write_text(json.dumps(state, indent=2))
'''

INSTALL_SCRIPT = r'''set -eu
ROOT=/installed-agent/tb8-mini
mkdir -p "$ROOT"
if ! python3 -m venv "$ROOT/venv"; then
    apt-get update
    apt-get install -y python3-venv
    python3 -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/python" -m pip install --disable-pip-version-check --report /agent-logs/mini-pip-report.json 'mini-swe-agent==2.4.6' 'litellm==1.75.8'
"$ROOT/venv/bin/python" -m pip freeze > /agent-logs/mini-environment.txt
"$ROOT/venv/bin/python" - <<'PY'
import hashlib, importlib.metadata, json, pathlib, minisweagent
root=pathlib.Path(minisweagent.__file__).parent
assert importlib.metadata.version("mini-swe-agent")=="2.4.6"
assert importlib.metadata.version("litellm")=="1.75.8"
files={}
metadata=importlib.metadata.distribution("mini-swe-agent")
for name in ["config/mini.yaml", "run/mini.py", "agents/default.py", "agents/interactive.py", "environments/local.py", "models/litellm_model.py"]:
    files[name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
pathlib.Path("/agent-logs/mini-package-provenance.json").write_text(json.dumps({"mini_version":"2.4.6","litellm_version":"1.75.8","policy_files_sha256":files,"license":metadata.metadata.get("License"),"classifiers":metadata.metadata.get_all("Classifier")},indent=2))
pathlib.Path("/agent-logs/mini-official-config.yaml").write_bytes((root/"config/mini.yaml").read_bytes())
PY
"$ROOT/venv/bin/mini" --help > /agent-logs/mini-cli-help.txt
'''


class MiniTB8Agent(MiniSweAgent):
    @staticmethod
    def name():
        return "tb8-official-mini-swe-agent-2.4.6-budgeted"

    def __init__(self, model_name, api_base="http://127.0.0.1:8080/v1", temperature=0.2,
                 max_episodes=40, wall_seconds=300, context_tokens=16384,
                 max_output_tokens=8192, max_total_tokens=200000, proxy_port=8081,
                 setup_timeout_seconds=300, **kwargs):
        model_name = "openai/" + model_name.removeprefix("openai/")
        kwargs["version"] = VERSION
        super().__init__(model_name=model_name, **kwargs)
        self.api_base, self.temperature = api_base, float(temperature)
        self.steps, self.wall_seconds = int(max_episodes), float(wall_seconds)
        self.context_tokens, self.output_tokens = int(context_tokens), int(max_output_tokens)
        self.total_tokens, self.proxy_port = int(max_total_tokens), int(proxy_port)
        self.setup_timeout = float(setup_timeout_seconds)

    @property
    def _env(self):
        # Do not read or forward any real provider credentials.
        return {"OPENAI_API_KEY": "EMPTY", "MSWEA_API_KEY": "EMPTY", "MSWEA_CONFIGURED": "true",
                "MSWEA_COST_TRACKING": "ignore_errors",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHONDONTWRITEBYTECODE": "1"}

    def runtime_config(self):
        return dict(agent=dict(step_limit=self.steps, wall_time_limit_seconds=int(self.wall_seconds), cost_limit=0),
                    model=dict(cost_tracking="ignore_errors", model_kwargs=dict(
                        api_base=f"http://host.docker.internal:{self.proxy_port}/v1", api_key="EMPTY",
                        temperature=self.temperature, max_tokens=self.output_tokens,
                        timeout=self.wall_seconds)))

    def perform_task(self, instruction, session, logging_dir=None):
        if logging_dir is None:
            raise ValueError("Mini TB8 requires native logging_dir")
        logging_dir = Path(logging_dir)
        logging_dir.mkdir(parents=True, exist_ok=True)
        container = session.container
        api = container.client.api
        original_timeout = api.timeout
        setup_started = time.monotonic()
        budget = None
        launched = False
        state = {}
        summary = {"native_submission": False, "mini_version": VERSION, "native_policy_unmodified": True,
                   "monetary_cost_usd": None, "monetary_cost_status": "not priced for local model"}
        failure = FailureMode.UNKNOWN_AGENT_ERROR
        try:
            api.timeout = self.setup_timeout + 10
            result = container.exec_run(["sh", "-c", "mkdir -p " + CONTAINER_DIR + " " + CONTAINER_LOGS])
            if result.exit_code:
                raise RuntimeError("Cannot create mini installation directory")
            put_bytes(container, CONTAINER_DIR + "/install.sh", INSTALL_SCRIPT)
            setup = container.exec_run(["timeout", "--signal=KILL", str(int(self.setup_timeout)), "sh", CONTAINER_DIR + "/install.sh"],
                                       environment=self._env, demux=True)
            stdout, stderr = setup.output
            (logging_dir / "mini-install.stdout").write_bytes(stdout or b"")
            (logging_dir / "mini-install.stderr").write_bytes(stderr or b"")
            summary.update(setup_seconds=time.monotonic() - setup_started, setup_exit_code=setup.exit_code)
            if setup.exit_code:
                failure = FailureMode.AGENT_INSTALLATION_FAILED
                summary["stop_reason"] = "installation_failed"
                return AgentResult(failure_mode=failure)
            api.timeout = 5
            # Native local execution starts in the original task container workdir.
            cwd = container.attrs.get("Config", {}).get("WorkingDir") or "/"
            put_bytes(container, CONTAINER_DIR + "/runtime.yaml", json.dumps(self.runtime_config()))
            put_bytes(container, CONTAINER_DIR + "/supervise.py", SUPERVISOR)
            argv = [CONTAINER_DIR + "/venv/bin/mini", "-m", self._model_name,
                    "-t", self._render_instruction(instruction), "-y", "--exit-immediately",
                    "-c", "mini.yaml", "-c", CONTAINER_DIR + "/runtime.yaml",
                    "-o", CONTAINER_LOGS + "/mini.traj.json"]
            spec = dict(argv=argv, cwd=cwd, env=self._env, wall_seconds=self.wall_seconds)
            put_bytes(container, CONTAINER_DIR + "/run.json", json.dumps(spec))
            dump(logging_dir / "mini-launch.json", dict(spec=spec, physical_context_tokens=self.context_tokens,
                 upstream=self.api_base, proxy_bind="127.0.0.1", policy="official mini.yaml and official CLI",
                 setup_is_separately_reported=True, setup_counts_toward_harness_timeout=True))
            budget = ProxyBudget(logging_dir, self.wall_seconds, self.total_tokens,
                                 self.output_tokens, self.temperature)
            with ModelProxy(budget, self.api_base, self.proxy_port):
                launched = True
                container.exec_run(["python3", CONTAINER_DIR + "/supervise.py"], detach=True,
                                   environment=self._env, user=getattr(session, "_user", ""))
                while True:
                    try:
                        state = json.loads(read_bytes(container, CONTAINER_LOGS + "/mini-process.json"))
                    except Exception:
                        state = {}
                    if state.get("state") == "cleanup_failed":
                        raise RuntimeError("mini supervisor could not confirm descendant cleanup")
                    if state.get("state") == "finished":
                        break
                    if budget.gate():
                        put_bytes(container, CONTAINER_DIR + "/stop", budget.stop_reason)
                        # Supervisor polls every 0.1s and kills only recorded CLI descendants.
                        for _ in range(30):
                            time.sleep(0.1)
                            try:
                                state = json.loads(read_bytes(container, CONTAINER_LOGS + "/mini-process.json"))
                            except Exception:
                                continue
                            if state.get("state") == "finished":
                                break
                        if state.get("state") != "finished":
                            raise RuntimeError("mini process cleanup unconfirmed; refuse to proceed silently")
                        break
                    time.sleep(min(0.25, max(0.01, budget.remaining())))
            # The supervisor can reach its deadline and finish between two host
            # polls. Preserve its objective reason even if proxy.gate() did not win.
            if not budget.stop_reason and state.get("stop_reason"):
                budget.stop_reason = state["stop_reason"]
            summary.update(budget.snapshot(), process=state)
            try:
                trajectory = json.loads(read_bytes(container, CONTAINER_LOGS + "/mini.traj.json"))
                dump(logging_dir / "mini.traj.json", trajectory)
            except Exception as exc:
                trajectory = {}
                summary["trajectory_error"] = str(exc)
            info = trajectory.get("info", {})
            status = info.get("exit_status")
            commands = [a.get("command", "") for m in trajectory.get("messages", [])
                        for a in m.get("extra", {}).get("actions", [])]
            complete_trace = bool(trajectory) and state.get("exit_code") is not None and state["exit_code"] >= 0
            summary.update(native_exit_status=status,
                           episodes=info.get("model_stats", {}).get("api_calls") if complete_trace else None,
                           native_steps_in_saved_trajectory=info.get("model_stats", {}).get("api_calls"),
                           native_submission=(status == "Submitted" and state.get("exit_code") == 0
                                              and not state.get("stop_reason") and not budget.stop_reason),
                           sent_commands=len(commands) if complete_trace else None,
                           repeated_commands=(len(commands) - len(set(commands))) if complete_trace else None,
                           commands_in_saved_trajectory=len(commands), native_action_trace_complete=complete_trace,
                           incomplete_saved_trace_is_only_a_lower_bound=not complete_trace,
                           repeated_commands_definition="exact command strings; not semantic/state repetition")
            summary["loop_end_reason"] = ("native_submission" if summary["native_submission"] else
                budget.stop_reason or state.get("stop_reason") or status or "native_process_exit")
            failure = (FailureMode.NONE if summary["native_submission"] else
                       FailureMode.AGENT_TIMEOUT if budget.stop_reason == "wall_deadline" or status == "TimeExceeded"
                       else FailureMode.UNKNOWN_AGENT_ERROR)
            return AgentResult(total_input_tokens=budget.prompt_tokens, total_output_tokens=budget.completion_tokens,
                               failure_mode=failure)
        except BaseException as exc:
            summary.update(error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            if launched and state.get("state") != "finished":
                cleanup = {"attempted": True, "confirmed_stopped": False}
                try:
                    api.timeout = 3
                    put_bytes(container, CONTAINER_DIR + "/stop", "host_exception")
                    for _ in range(20):
                        time.sleep(0.1)
                        state = json.loads(read_bytes(container, CONTAINER_LOGS + "/mini-process.json"))
                        if state.get("state") == "finished":
                            cleanup["confirmed_stopped"] = True
                            break
                except Exception as exc:
                    cleanup["error"] = str(exc)
                if not cleanup["confirmed_stopped"]:
                    # Scoped task-container stop prevents mutations during hidden grading.
                    # This is an infrastructure failure, never an apparent model score.
                    try:
                        container.stop(timeout=1)
                        cleanup["task_container_stopped_fallback"] = True
                    except Exception as exc:
                        cleanup["fallback_error"] = str(exc)
                summary["exception_cleanup"] = cleanup
            api.timeout = original_timeout
            if budget:
                summary.update(budget.snapshot())
            summary.update(failure_mode=failure.value, total_setup_and_agent_seconds=time.monotonic() - setup_started)
            dump(logging_dir / "budget-summary.json", summary)
