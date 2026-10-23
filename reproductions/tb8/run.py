"""Run fixed TB8 through the existing official 0.2.18 harness, or zero-GPU checks."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_TB = Path(r"C:\Users\89664\Desktop\master degree\terminal-bench")
OUTPUT = ROOT / "_work" / "tb8" / "runs"


def load_adapter(tb_root, method="terminus-2"):
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ["OPENAI_API_KEY"] = "EMPTY"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(tb_root))
    module = "mini_adapter" if method in ("mini", "mini-swe-agent") else "terminus_adapter"
    return importlib.import_module("reproductions.tb8." + module)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def snapshot_executed_inputs(out, protocol, protocol_bytes, method, grading_transport):
    """Save one read of each actual launch input; hash the exact saved bytes."""
    adapter_name = "mini_adapter.py" if method in ("mini", "mini-swe-agent") else "terminus_adapter.py"
    sources = {"protocol.json": (protocol, protocol_bytes),
               "run.py": (Path(__file__).resolve(), Path(__file__).read_bytes()),
               adapter_name: (HERE / adapter_name, (HERE / adapter_name).read_bytes())}
    if grading_transport:
        helper = HERE / "grading_transport.py"
        sources[helper.name] = (helper, helper.read_bytes())
    destination = out / "executed-inputs"
    destination.mkdir(exist_ok=False)
    inputs = {}
    for name, (source, content) in sources.items():
        target = destination / name
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o444)
        inputs[name] = dict(source_path=str(source.resolve()), snapshot_path=str(target),
                           sha256=hashlib.sha256(content).hexdigest(), bytes=len(content),
                           read_only=True)
    dump(destination / "manifest.json", inputs)
    (destination / "manifest.json").chmod(0o444)
    return inputs, adapter_name


class FakeContainer:
    def __init__(self):
        self.client = SimpleNamespace(api=SimpleNamespace(timeout=60))
        self.exec_calls = 0

    def exec_run(self, *args, **kwargs):
        self.exec_calls += 1
        return SimpleNamespace(exit_code=0, output=b"fake output")


class FakeSession:
    def __init__(self):
        self.container = FakeContainer()
        self.marker = None

    def capture_pane(self, **kwargs):
        self.container.exec_run(["capture"])
        return self.marker or "fake terminal"

    def get_incremental_output(self):
        return self.capture_pane()

    def is_session_alive(self):
        self.container.exec_run(["alive"])
        return True

    def get_asciinema_timestamp(self):
        return 0.0

    def send_keys(self, keys, block=False, min_timeout_sec=0, max_timeout_sec=180):
        self.container.exec_run(["send", keys])
        if isinstance(keys, str) and keys.startswith("printf '__TB8_CLEANUP_"):
            self.marker = keys.split("'")[1].split("\\n")[0]
        time.sleep(min_timeout_sec)


def offline_check(adapter, out):
    """Real loopback HTTP, unchanged policy/parser, fake session: no Docker/GPU."""
    import faulthandler
    faulthandler.dump_traceback_later(15)
    requests = []
    behavior = {"delay": 0.0, "fail_next": False, "complete": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(data)
            if behavior["delay"]:
                time.sleep(behavior["delay"])
            if behavior["fail_next"]:
                behavior["fail_next"] = False
                status, payload = 500, {"error": {"message": "synthetic transient failure",
                                                  "type": "server_error"}}
            else:
                content = json.dumps(dict(analysis="fake analysis", plan="fake plan",
                    commands=[], task_complete=behavior["complete"]))
                status, payload = 200, dict(id="offline-fake", object="chat.completion",
                    created=0, model=data["model"], choices=[dict(index=0,
                    message=dict(role="assistant", content=content), finish_reason="stop")],
                    usage=dict(prompt_tokens=3, completion_tokens=2, total_tokens=5))
            encoded = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    checks = []
    try:
        agent = adapter.BudgetedTerminus2(model_name="tb8-offline-alias", api_base=endpoint,
            temperature=0.2, max_episodes=40, wall_seconds=20)
        assert agent._get_model_context_limit() == 16384
        assert agent._llm._supports_temperature
        checks.append("unknown alias registered at 16384; native temperature supported")
        result = agent.perform_task("offline fake task", FakeSession(), out / "native-loop")
        summary = json.loads((out / "native-loop/budget-summary.json").read_text())
        assert result.failure_mode.value == "none"
        assert summary["http_attempts"] == 2 and summary["known_total_tokens"] == 10
        assert summary["episodes"] == 2 and summary["native_submission"]
        assert all(r["model"] == "tb8-offline-alias" and r["temperature"] == 0.2
                   and r["max_tokens"] == 8192 for r in requests)
        assert all("chat_template_kwargs" not in r for r in requests)
        checks.append("unchanged T2 parser and double-confirmation through two actual fake HTTPs")
        checks.append("wire model/temp/output cap and actual usage/finish_reason recorded")
        one = adapter.BudgetedTerminus2(model_name="tb8-offline-alias", api_base=endpoint,
            temperature=0.2, max_episodes=1, wall_seconds=20)
        one.perform_task("single-episode fake task", FakeSession(), out / "one-complete")
        one_summary = json.loads((out / "one-complete/budget-summary.json").read_text())
        assert not one_summary["native_submission"]
        checks.append("first completion at final episode is not counted as submission")

        budget = adapter.Budget(out / "native-summary", wall_seconds=20)
        token = adapter._ACTIVE.set(budget)
        try:
            from terminal_bench.llms.chat import Chat
            chat = Chat(agent._llm)
            chat._messages = [{"role": "user", "content": "prior fake context"}]
            agent._summarize(chat, "fake task", FakeSession())
            assert budget.http_attempts == 3 and budget.known_tokens == 15
        finally:
            adapter._ACTIVE.reset(token)
        checks.append("native summary's three HTTPs included independently of episodes")

        behavior["fail_next"] = True
        retry_agent = adapter.BudgetedTerminus2(model_name="tb8-offline-alias", api_base=endpoint,
            temperature=0.2, max_episodes=1, wall_seconds=20)
        retry_agent.perform_task("fake retry task", FakeSession(), out / "native-retry")
        retry_summary = json.loads((out / "native-retry/budget-summary.json").read_text())
        assert retry_summary["http_attempts"] >= 2
        assert retry_summary["unknown_usage_requests"] >= 1
        checks.append("native transient retry retained; failed HTTP usage is unknown")

        behavior["delay"] = 0.4
        slow = adapter.BudgetedTerminus2(model_name="tb8-offline-alias", api_base=endpoint,
            temperature=0.2, max_episodes=40, wall_seconds=0.15)
        started = time.monotonic()
        slow_result = slow.perform_task("fake slow task", FakeSession(), out / "http-deadline")
        elapsed = time.monotonic() - started
        assert elapsed < 2 and slow_result.failure_mode.value == "agent_timeout"
        behavior["delay"] = 0.0
        checks.append("HTTP and native retry waits stop at internal monotonic deadline")

        budget = adapter.Budget(out / "session-deadline", wall_seconds=0.05)
        session = FakeSession()
        token = adapter._ACTIVE.set(budget)
        try:
            with adapter.SessionScope(session, budget):
                try:
                    session.send_keys("fake command", min_timeout_sec=60)
                    raise AssertionError("sleep was not bounded")
                except adapter.BudgetStop:
                    pass
                before = session.container.exec_calls
                try:
                    session.container.exec_run(["late mutation"])
                    raise AssertionError("late action was not fenced")
                except adapter.BudgetStop:
                    pass
                assert session.container.exec_calls == before
        finally:
            adapter._ACTIVE.reset(token)
        session.container.exec_run(["fake independent grader"])
        assert session.container.exec_calls == before + 1
        checks.append("session wait bounded; late writes blocked; grader session restored")

        budget = adapter.Budget(out / "token-deadline", wall_seconds=10, max_total_tokens=1)
        budget.known_tokens = 1
        token = adapter._ACTIVE.set(budget)
        before = len(requests)
        try:
            try:
                agent._llm.call(prompt="must not send")
                raise AssertionError("token gate did not stop")
            except adapter.BudgetStop:
                pass
            assert len(requests) == before
        finally:
            adapter._ACTIVE.reset(token)
        checks.append("observed token threshold blocks subsequent HTTP before native retries")
        report = dict(success=True, checks=checks, model_calls=0, fake_http_requests=len(requests),
                      endpoint=endpoint, output=str(out), actual_model_endpoint_contacted=False)
        dump(out / "offline-check.json", report)
        return report
    finally:
        server.shutdown()
        server.server_close()
        faulthandler.cancel_dump_traceback_later()



def pin_container_environment(cfg, out):
    """No implicit Compose build/pull; verify real image before policy startup."""
    from terminal_bench.terminal.docker_compose_manager import DockerComposeManager
    original_command = DockerComposeManager.get_docker_compose_command
    original_start = DockerComposeManager.start

    def command(manager, args):
        if args and args[0] == "build":
            raise RuntimeError("Fixed protocol forbids implicit image builds")
        if args == ["up", "-d"]:
            args = ["up", "-d", "--no-build", "--pull", "never"]
        return original_command(manager, args)

    def start(manager):
        container = original_start(manager)
        container.reload()
        tag = manager._client_image_name
        if ":" not in tag.rsplit("/", 1)[-1]:
            tag += ":latest"
        expected = cfg["docker"]["images"][tag]
        actual = container.attrs["Image"]
        record = dict(container_id=container.id, image_tag=manager._client_image_name,
                      expected_image=expected, actual_image=actual, matches=actual == expected,
                      compose_flags=["--no-build", "--pull", "never"])
        dump(out / "container-identities" / (container.id + ".json"), record)
        if actual != expected:
            raise RuntimeError("Actual task container image differs from fixed protocol")
        return container

    DockerComposeManager.get_docker_compose_command = command
    DockerComposeManager.start = start


def run(protocol, method, run_id, tb_root, factory_only=False, grading_transport=False, task_ids=None):
    protocol_bytes = protocol.read_bytes()
    cfg = json.loads(protocol_bytes.decode("utf-8-sig"))
    fixed_ids = [task["id"] for task in cfg["tasks"]]
    selected_ids = list(task_ids) if task_ids else fixed_ids
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids).issubset(fixed_ids):
        raise ValueError("Task selection must be unique IDs from the fixed eight")
    if grading_transport and method not in ("mini", "mini-swe-agent") and not task_ids:
        raise ValueError("Grading transport is only for future mini or explicit infrastructure replacement")
    out = OUTPUT / run_id
    if out.exists():
        raise FileExistsError(f"Refusing to append/retry existing run: {out}")
    out.mkdir(parents=True)
    inputs, adapter_name = snapshot_executed_inputs(
        out, protocol, protocol_bytes, method, grading_transport)
    capture = HERE / "capture_provenance.py"
    subprocess.run([sys.executable, "-B", str(capture), "--protocol",
                    inputs["protocol.json"]["snapshot_path"],
                    "--output", str(out / "provenance.json")], check=True)
    pin_container_environment(cfg, out)
    model, budget = cfg["model"], cfg["budget"]
    effective_timeout = (630 if method in ("mini", "mini-swe-agent")
                         else budget["harness_agent_timeout_seconds"])
    from terminal_bench.harness.harness import Harness
    if grading_transport:
        from reproductions.tb8.grading_transport import GradingTransportHarness
        Harness = GradingTransportHarness
    if method in ("mini", "mini-swe-agent"):
        agent_path = "reproductions.tb8.mini_adapter:MiniTB8Agent"
    else:
        agent_class = "BudgetedTerminus2" if method == "terminus-2" else "BudgetedTerminus1"
        agent_path = f"reproductions.tb8.terminus_adapter:{agent_class}"
    manifest = dict(protocol_sha256=inputs["protocol.json"]["sha256"],
        driver_sha256=inputs["run.py"]["sha256"],
        adapter_sha256=inputs[adapter_name]["sha256"],
        executed_inputs=inputs,
        method=method, started_utc=datetime.now(timezone.utc).isoformat(),
        model=model, budget=budget, task_ids=selected_ids,
        grading_transport_enabled=grading_transport,
        container_environment="no-build; pull never; verify actual image ID before policy",
        grading_transport_sha256=(inputs["grading_transport.py"]["sha256"]
                                  if grading_transport else None),
        attempt_role=("explicit infrastructure-replacement; extra Agent attempt disclosed"
                      if task_ids else "fixed eight-task comparison"),
        selection_policy="one attempt per selected task; no automatic retry or best-of",
        policy="official installed Terminal-Bench0.2.18; no prompt/parser edits",
        hidden_tests="native harness only after perform_task returns",
        effective_harness_agent_timeout_seconds=effective_timeout,
        timeout_override=("mini setup300 + explore300 + margin30" if method in
                          ("mini", "mini-swe-agent") else None))
    dump(out / "launch.json", manifest)
    harness = Harness(output_path=out, run_id="native",
        agent_import_path=agent_path,
        dataset_path=Path(cfg["terminal_bench"].get("task_dir", tb_root / "original-tasks")),
        model_name=model.get("model_name", model["alias"]),
        task_ids=manifest["task_ids"], n_concurrent_trials=1, n_attempts=1,
        global_agent_timeout_sec=effective_timeout,
        global_test_timeout_sec=budget["test_timeout_seconds"], cleanup=False, no_rebuild=True,
        upload_results=False, livestream=False,
        agent_kwargs=dict(model_name=model.get("model_name", model["alias"]),
            api_base=model["base_url"], temperature=budget["temperature"],
            max_episodes=budget["max_episodes"], wall_seconds=budget["agent_wall_seconds"],
            context_tokens=model["context_tokens"], max_output_tokens=budget["max_output_tokens"],
            max_total_tokens=budget["max_total_tokens"]))
    if factory_only:
        agent = harness._create_agent_for_task(manifest["task_ids"][0])
        assert agent._max_episodes == budget["max_episodes"]
        assert agent._get_model_context_limit() == model["context_tokens"]
        manifest.update(status="factory_passed", model_calls=0, docker_containers_started=0,
                        instantiated_agent=type(agent).__name__)
        dump(out / "launch.json", manifest)
        return dict(output=str(out), status="factory_passed", model_calls=0,
                    agent=type(agent).__name__)
    try:
        results = harness.run()
        dump(out / "native-results.json", results.model_dump(mode="json"))
        manifest["status"] = "completed"
    except BaseException as exc:
        manifest.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        dump(out / "launch.json", manifest)
    return dict(output=str(out), status=manifest["status"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "run"])
    parser.add_argument("--protocol", type=Path, default=HERE / "protocol.json")
    parser.add_argument("--tb-root", type=Path, default=DEFAULT_TB)
    parser.add_argument("--agent", choices=["terminus-2", "terminus-1", "mini", "mini-swe-agent"], default="terminus-2")
    parser.add_argument("--run-id")
    parser.add_argument("--factory-only", action="store_true")
    parser.add_argument("--grading-transport", action="store_true")
    parser.add_argument("--task-id", action="append", dest="task_ids")
    args = parser.parse_args()
    if args.command == "run":
        cfg = json.loads(args.protocol.read_text(encoding="utf-8-sig"))
        args.tb_root = Path(cfg["terminal_bench"]["root"])
    adapter = load_adapter(args.tb_root, args.agent)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if Path(run_id).name != run_id or any(c in run_id for c in "/\\:"):
        raise ValueError("run-id must be one directory name")
    if args.command == "check":
        if args.agent in ("mini", "mini-swe-agent"):
            raise ValueError("Use mini_preflight.py for the container CLI checks")
        out = OUTPUT / ("offline-" + run_id)
        if out.exists():
            raise FileExistsError(out)
        result = offline_check(adapter, out)
    else:
        result = run(args.protocol, args.agent, run_id, args.tb_root, args.factory_only,
                     args.grading_transport, args.task_ids)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

