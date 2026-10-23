"""Bounded wrapper around the pinned upstream backend; no policy changes."""
from __future__ import annotations
import json
import time
from pathlib import Path

def make_llm(upstream: Path, config: dict, role: str, output: Path):
    import sys
    sys.path.insert(0, str(upstream))
    from agentcore.llm.LLMBackend import VLLMLLM
    from agentcore.llm.LLMConfig import LLMConfig

    class BoundedLLM(VLLMLLM):
        def __init__(self):
            self.phase_started = time.monotonic()
            self.attempted_calls = 0
            self.trace_path = output / f"{role}_calls.jsonl"
            self.summary_path = output / f"{role}_usage.json"
            super().__init__(LLMConfig({
                "llm_name": config["model"], "base_url": config["base_url"],
                "api_key": "EMPTY",
                "temperature": config["temperature"], "top_p": config["top_p"],
                "seed": config["seed"], "enable_thinking": config["enable_thinking"],
                "max_output_tokens": config[f"{role}_max_output_tokens"],
                "send_chat_template_kwargs": config["send_chat_template_kwargs"],
                "request_attempts": config["request_attempts"],
                "request_timeout": config["request_timeout_s"],
                "retry_delay": 0,
            }))
            self.persist()

        def persist(self):
            summary = self.get_trace_summary()
            summary.update(role=role, attempted_calls=self.attempted_calls,
                           elapsed_seconds=time.monotonic() - self.phase_started)
            self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        def run(self, prompt, system_prompt=None):
            if self.attempted_calls >= config[f"{role}_max_calls"]:
                raise RuntimeError(f"{role} call budget exhausted")
            if time.monotonic() - self.phase_started >= config[f"{role}_timeout_s"]:
                raise RuntimeError(f"{role} time budget exhausted")
            self.attempted_calls += 1
            record = {"role": role, "call": self.attempted_calls, "started": time.time(),
                      "prompt": prompt, "system_prompt": system_prompt or "You are a helpful assistant."}
            try:
                result = super().run(prompt, system_prompt)
                record.update(status="ok", **self._call_trace[-1])
                return result
            except BaseException as exc:
                record.update(status="error", error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                record["elapsed_seconds"] = time.time() - record["started"]
                with self.trace_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.persist()

    return BoundedLLM()
