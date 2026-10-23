#!/usr/bin/env python3
"""Real CLM/LiteLLM transport against authored loopback fixtures; not model results."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading

# This must precede the actual official harness import.
os.environ["CLM_LLM_MAX_RETRIES"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["OPENAI_API_KEY"] = "EMPTY"

from clm_harness.clm_agent.harness import _MAX_RETRIES, _extract_tool_call
from clm_contextbench.clm_stream import ClmStream
from repro import CONFIG, HERE, WORK, sha, write_json


def main() -> int:
    captured = []
    mode = {"status": 200}
    fixture_command = "echo authored_transport_fixture"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append({"path": self.path, "body": request, "fixture_status": mode["status"]})
            if mode["status"] == 200:
                body = {
                    "id": "authored-fixture-no-model", "object": "chat.completion",
                    "created": 0, "model": "repro-qwen35-9b-q4",
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant", "content": "",
                                   "tool_calls": [{"id": "fixture-call", "type": "function",
                                     "function": {"name": "bash", "arguments": json.dumps({
                                         "command": fixture_command})}}]}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                }
            else:
                body = {"error": {"message": "authored no-retry error fixture",
                                  "type": "server_error", "code": "fixture_unavailable"}}
            payload = json.dumps(body).encode()
            self.send_response(mode["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = json.loads(CONFIG.read_text())
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        with tempfile.TemporaryDirectory(prefix="hw-clm-http-") as temporary:
            agent = ClmStream(logs_dir=Path(temporary), model_name=cfg["model"],
                              api_base=endpoint, **cfg["agent_kwargs"])
            messages = [{"role": "system", "content": "Authored transport fixture only."},
                        {"role": "user", "content": "Return the fixture tool call."}]
            response = asyncio.run(agent._query_with_retry(cfg["model"], messages))
            action = _extract_tool_call(response.choices[0].message.model_dump())
            assert action["command"] == fixture_command
            totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            agent._accumulate_usage(response, totals)
            assert totals == {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
            assert len(captured) == 1 and captured[0]["path"] == "/v1/chat/completions"
            request = captured[0]["body"]
            assert request["model"] == cfg["model"].split("/", 1)[1]
            assert request["tools"][0]["function"]["name"] == "bash"
            assert request["max_tokens"] == 2048
            assert request["chat_template_kwargs"]["enable_thinking"] is False
            assert _MAX_RETRIES == 1
            mode["status"] = 503
            try:
                asyncio.run(agent._query_with_retry(cfg["model"], messages))
            except Exception as exc:
                assert type(exc).__name__ in {"InternalServerError", "ServiceUnavailableError", "APIError"}, type(exc).__name__
            else:
                raise AssertionError("The authored error response was not propagated")
            assert len(captured) == 2, "A failed logical call made hidden retries"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "passed": True, "response_source": "Authored loopback HTTP fixture; no model, no benchmark score.",
        "official_path": "ClmStream._query_with_retry -> ClmAgent._query_with_retry -> litellm.completion -> OpenAI HTTP",
        "successful_fixture_http_requests": 1, "failing_fixture_http_requests": 1,
        "failed_request_retried": False, "tool_call_parser_passed": True,
        "usage_accumulator_passed": True, "model_calls": 0, "gpu_calls": 0,
        'dependency_lock_sha256': sha(HERE / "requirements-ubuntu-py312.lock.txt"),
        "request_environment": {"CLM_LLM_MAX_RETRIES": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
        "stub_closed": True,
    }
    write_json(WORK / "preparation/transport-check.json", report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
