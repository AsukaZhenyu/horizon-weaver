"""Zero-model checks for the official-mini TB8 adapter.

Default: exercise real HTTP relay against a local fake OpenAI server, no Docker.
--container-image: additionally install/run the exact upstream CLI inside a
throwaway original-task-image container against that same fake server. This is
infrastructure validation, never a benchmark/model result.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_TB = Path(r"C:\Users\89664\Desktop\master degree\terminal-bench")


class Stub:
    def __init__(self):
        self.requests = []
        self.behavior = "ok"
        self.commands = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(dict(body=data, authorization=self.headers.get("Authorization")))
                if owner.behavior == "slow": time.sleep(0.3)
                if owner.behavior == "error":
                    status, payload = 500, {"error": {"message": "synthetic failure", "type": "server_error"}}
                else:
                    number = len(owner.requests)
                    command = (owner.commands[min(number - 1, len(owner.commands) - 1)] if owner.commands else
                        "printf 'mini-stub-ok\\n'" if number == 1 else "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
                    status, payload = 200, dict(id=f"fake-{number}", object="chat.completion", created=0,
                        model=data.get("model"), choices=[dict(index=0, finish_reason="tool_calls", message=dict(
                            role="assistant", content="Synthetic infrastructure response.", tool_calls=[dict(
                                id=f"call-{number}", type="function", function=dict(name="bash", arguments=json.dumps({"command": command})))]))],
                        usage=dict(prompt_tokens=7, completion_tokens=3, total_tokens=10))
                raw = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self): self.thread.start(); return self
    def __exit__(self, *args): self.server.shutdown(); self.server.server_close()


def post(port, payload):
    request = Request(f"http://127.0.0.1:{port}/v1/chat/completions", json.dumps(payload).encode(),
                      {"Content-Type": "application/json", "Authorization": "Bearer SYNTHETIC-DO-NOT-FORWARD"})
    try:
        with urlopen(request, timeout=2) as response: return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def offline(adapter, output):
    checks = []
    request = dict(model="repro-qwen35-9b-q4", messages=[dict(role="user", content="fake")],
                   tools=[dict(type="function", function=dict(name="bash", parameters={"type": "object"}))],
                   max_tokens=9999, temperature=1.0)
    with Stub() as stub:
        budget = adapter.ProxyBudget(output / "relay", wall_seconds=10, max_total_tokens=20)
        with adapter.ModelProxy(budget, stub.endpoint, port=0) as proxy:
            port = proxy.server.server_port
            for _ in range(2):
                status, result = post(port, request)
                assert status == 200 and result["usage"]["total_tokens"] == 10
            status, _ = post(port, request)
            assert status == 429 and len(stub.requests) == 2
        assert budget.total_tokens == 20 and budget.attempts == 2 and budget.unknown == 0
        assert all(x["authorization"] == "Bearer EMPTY" for x in stub.requests)
        assert all(x["body"]["max_tokens"] == 8192 and x["body"]["temperature"] == 0.2 for x in stub.requests)
        checks.extend(["real loopback relay preserves tool payload/usage", "dummy-only upstream authorization",
                       "8192 output/.2 temperature on wire", "observed token gate refuses next physical HTTP"])
        stub.behavior = "error"
        failed = adapter.ProxyBudget(output / "failed", wall_seconds=10)
        with adapter.ModelProxy(failed, stub.endpoint, port=0) as proxy:
            status, _ = post(proxy.server.server_port, request)
            assert status == 500
        assert failed.attempts == 1 and failed.unknown == 1 and failed.total_tokens == 0
        checks.append("failed upstream request saved as unknown usage, not free model cost")
        stub.behavior = "slow"
        slow = adapter.ProxyBudget(output / "slow", wall_seconds=.05)
        with adapter.ModelProxy(slow, stub.endpoint, port=0) as proxy:
            started = time.monotonic()
            status, _ = post(proxy.server.server_port, request)
            assert status == 504 and time.monotonic() - started < 1
        assert slow.stop_reason == "wall_deadline" and slow.unknown == 1
        checks.append("in-flight upstream read respects monotonic deadline")
    agent = adapter.MiniTB8Agent(model_name="repro-qwen35-9b-q4")
    config = agent.runtime_config()
    assert config["agent"]["step_limit"] == 40 and config["agent"]["wall_time_limit_seconds"] == 300
    assert "num_retries" not in config["model"]["model_kwargs"]
    assert "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT" not in agent._env
    assert all("template" not in k for section in config.values() for k in section)
    compile(adapter.SUPERVISOR, "mini-supervise.py", "exec")
    checks.extend(["only runtime settings override official mini.yaml; no prompt templates",
                   "40 native steps / 300s native wall / upstream retry defaults retained", "container supervisor compiles"])
    return dict(success=True, checks=checks, actual_model_requests=0, docker_used=False,
                fake_http_requests=budget.attempts + failed.attempts + slow.attempts)


def container_check(adapter, image, output):
    import docker
    client = docker.from_env()
    client.api.timeout = 15
    container = None
    with Stub() as stub:
        try:
            container = client.containers.run(image, entrypoint=["sh", "-c"], command=["sleep 1200"],
                detach=True, user="root", labels={"horizon-weaver.tb8.preflight": "mini"},
                volumes={str(output.resolve()): {"bind": "/agent-logs", "mode": "rw"}})
            agent = adapter.MiniTB8Agent(model_name="repro-qwen35-9b-q4", api_base=stub.endpoint,
                                        wall_seconds=30, setup_timeout_seconds=300)
            result = agent.perform_task("Infrastructure smoke test with a fake model; no benchmark score.",
                                        SimpleNamespace(container=container, _user=""), output)
            summary = json.loads((output / "budget-summary.json").read_text())
            assert result.failure_mode.value == "none", summary
            assert summary["native_submission"] and summary["http_attempts"] == 2, summary
            assert summary["known_total_tokens"] == 20 and summary["native_exit_status"] == "Submitted"
            assert len(stub.requests) == 2
            # Reuse the already-installed CLI, but isolate all per-run files. This
            # second fake-only run checks that a detached orphan cannot mutate
            # the task after the exploration deadline and before grading.
            first = output / "submitted-smoke"
            first.mkdir()
            for artifact in list(output.iterdir()):
                if artifact.is_file(): artifact.rename(first / artifact.name)
            for name in ("stop", "run.json"):
                container.exec_run(["rm", "-f", adapter.CONTAINER_DIR + "/" + name])
            container.exec_run(["rm", "-f", "/agent-logs/mini.traj.json", "/agent-logs/mini-process.json"])
            stub.requests.clear()
            stub.commands = ["python3 -c \"import pathlib, subprocess; pathlib.Path('/tmp/mini-preflight-orphan-started').write_text('1'); subprocess.Popen(['sh','-c','sleep 15; echo leaked > /tmp/mini-preflight-leak'], start_new_session=True)\"", "sleep 60"]
            deadline_agent = adapter.MiniTB8Agent(model_name="repro-qwen35-9b-q4", api_base=stub.endpoint,
                wall_seconds=10, setup_timeout_seconds=300)
            deadline_result = deadline_agent.perform_task("Fake-only deadline cleanup check.",
                SimpleNamespace(container=container, _user=""), output)
            deadline_summary = json.loads((output / "budget-summary.json").read_text())
            assert deadline_result.failure_mode.value == "agent_timeout", deadline_summary
            assert deadline_summary["process"]["cleanup_confirmed"], deadline_summary
            started = container.exec_run(["test", "-f", "/tmp/mini-preflight-orphan-started"])
            assert started.exit_code == 0, "deadline fixture never actually started a detached child"
            time.sleep(5.2)
            leak = container.exec_run(["sh", "-c", "test ! -e /tmp/mini-preflight-leak"])
            assert leak.exit_code == 0, "orphan action survived the deadline"
            return dict(success=True, actual_model_requests=0, docker_used=True,
                        official_cli_used=True, mini_version=adapter.VERSION,
                        fake_http_requests=2 + len(stub.requests), native_submission=True,
                        orphan_cleanup_confirmed=True, deadline_seconds=10,
                        container_id=container.id, image_id=container.image.id)
        finally:
            if container is not None:
                container.remove(force=True)
            client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tb-root", type=Path, default=DEFAULT_TB)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container-image", help="Optional fixed original image ID for real CLI, fake model smoke")
    args = parser.parse_args()
    if args.output.exists(): raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    sys.dont_write_bytecode = True
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    sys.path[:0] = [str(ROOT), str(args.tb_root)]
    adapter = importlib.import_module("reproductions.tb8.mini_adapter")
    report = offline(adapter, args.output)
    try:
        if args.container_image:
            report["container"] = container_check(adapter, args.container_image, args.output)
    except BaseException as exc:
        report.update(success=False, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        adapter.dump(args.output / "preflight.json", report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
