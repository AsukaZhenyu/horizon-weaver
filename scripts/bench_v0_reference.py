"""Diagnostic adapter for unmodified v0.x policy/loop with matched run budgets.

This is a development comparison, not a frozen experiment baseline. Only transport
deadlines/output limits and session command deadlines are supplied by this wrapper.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
reference_root = os.environ.get("HORIZON_REFERENCE_CODE_ROOT")
if not reference_root:
    raise RuntimeError("HORIZON_REFERENCE_CODE_ROOT must point to the isolated reference transport archive")
sys.path.insert(0, str(Path(reference_root).resolve()))

from node_agent import NodeAgent, build_default_plugins
from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode

from horizon_weaver.core import DecisionInput, Usage
from horizon_weaver.policy import ModelGateway


class BudgetedClient:
    def __init__(self, model, base_url, temperature, deadline, output_tokens, max_tokens, log):
        self.gateway = ModelGateway(model, base_url, temperature)
        self.deadline, self.output_tokens, self.max_tokens = deadline, output_tokens, max_tokens
        self.log = log
        self.usage = Usage()
        self.calls = 0

    def complete(self, messages, max_attempts=4):
        counted = Usage()
        raw = ""
        for _ in range(max_attempts):
            available = self.deadline - time.monotonic()
            if available <= 0 or self.usage.total >= self.max_tokens:
                raise TimeoutError("matched reference budget exhausted")
            self.calls += 1
            request = DecisionInput(step=self.calls, system=messages[0]["content"], context=messages[1]["content"],
                input_token_bound=sum(len(message["content"].encode("utf-8")) for message in messages) + 128,
                max_output_tokens=self.output_tokens)
            response = self.gateway.complete(request, available)
            self.usage = self.usage.plus(response.usage)
            counted = counted.plus(response.usage)
            raw = response.content
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(request=request.model_dump(mode="json"),
                    response=response.model_dump(mode="json")), ensure_ascii=False) + "\n")
            if self.usage.total > self.max_tokens:
                raise RuntimeError("matched reference model-token budget exceeded")
            if raw.strip():
                break
        return raw, dict(prompt_tokens=counted.input_tokens, completion_tokens=counted.output_tokens)

    def count_tokens(self, usage):
        return usage["prompt_tokens"], usage["completion_tokens"]


class BudgetedSession:
    def __init__(self, session, deadline, timeout):
        self.session, self.deadline, self.timeout = session, deadline, timeout

    def __getattr__(self, name):
        return getattr(self.session, name)

    def send_keys(self, keys, *args, **kwargs):
        available = self.deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError("matched reference wall-time budget exhausted")
        kwargs["max_timeout_sec"] = min(kwargs.get("max_timeout_sec", self.timeout), self.timeout, available)
        return self.session.send_keys(keys, *args, **kwargs)


class V0ReferenceAgent(BaseAgent):
    def __init__(self, model_name="qwen3.5-9b", api_base="http://127.0.0.1:8080/v1",
                 temperature=.2, max_steps=40, wall_time_s=300, output_tokens=2048,
                 tool_timeout_s=30, max_model_tokens=200000, **kwargs):
        super().__init__(**kwargs)
        self.model, self.base_url, self.temperature = model_name, api_base, float(temperature)
        self.max_steps, self.wall_time = int(max_steps), float(wall_time_s)
        self.output_tokens, self.tool_timeout, self.max_tokens = int(output_tokens), float(tool_timeout_s), int(max_model_tokens)

    @staticmethod
    def name():
        return "horizon-v0-reference"

    def perform_task(self, instruction, session, logging_dir=None):
        if logging_dir is None:
            raise ValueError("durable logging directory required")
        log = Path(logging_dir) / "v0-reference"
        log.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        deadline = started + self.wall_time
        client = BudgetedClient(self.model, self.base_url, self.temperature, deadline,
                                self.output_tokens, self.max_tokens, log / "requests.jsonl")
        try:
            agent = NodeAgent(max_steps=self.max_steps, time_limit_s=self.wall_time,
                plugins=build_default_plugins(client=client, model_name=self.model,
                    api_base=self.base_url, temperature=self.temperature))
            result = agent.run(instruction, BudgetedSession(session, deadline, self.tool_timeout), log)
            (log / "result.json").write_text(json.dumps(dict(termination=result.termination,
                steps=len(result.attempts), elapsed_s=time.monotonic() - started,
                usage=client.usage.model_dump(mode="json"), attempts=[a.model_dump(mode="json") for a in result.attempts],
                configuration=dict(output_tokens=self.output_tokens, max_steps=self.max_steps,
                    wall_time_s=self.wall_time, tool_timeout_s=self.tool_timeout, max_model_tokens=self.max_tokens)),
                ensure_ascii=False, indent=2), encoding="utf-8")
            mode = FailureMode.UNKNOWN_AGENT_ERROR if result.termination == "model_error" else FailureMode.NONE
            return AgentResult(total_input_tokens=client.usage.input_tokens,
                total_output_tokens=client.usage.output_tokens, failure_mode=mode,
                timestamped_markers=result.markers)
        finally:
            client.gateway.close()
