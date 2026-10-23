"""HTTP-only backend adaptation; preserve llamp history and official policy prompts."""
import hashlib
import json
import os
import time
from functools import lru_cache
from pathlib import Path

import httpx
from llamp.llms.base_llm_system import BaseLLMSystem


class LlamaCppTransport(BaseLLMSystem):
    def __init__(self, model, max_model_len=16000, seed=42, **unused):
        super().__init__("LlamaCppTransport", "game_logs", temperature=0.0)
        self.settings = json.loads(os.environ["STATEACT_BACKEND_JSON"])
        self.model = model
        self.seed = seed
        self.stop_sequences = None
        self.native_url = self.settings["base_url"].rstrip("/").removesuffix("/v1")
        self.client = httpx.Client(timeout=180.0, trust_env=False)
        self.log_path = Path(os.environ["STATEACT_TRANSPORT_LOG"])
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.attempt = 0
        self.environment_index = int(os.environ.get("STATEACT_START_INDEX", "0")) - 1

    def set_base_prompt_and_reset(self, base_prompt, *args, **kwargs):
        self.environment_index += 1
        return super().set_base_prompt_and_reset(base_prompt, *args, **kwargs)

    @lru_cache(maxsize=128)
    def count_raw_tokens(self, text):
        response = self.client.post(self.native_url + "/tokenize",
                                    json={"content": text, "add_special": False})
        response.raise_for_status()
        return len(response.json()["tokens"])

    def count_tokens(self, optional_text=None, model_name=None):
        text = optional_text if optional_text else self.generate_text_prompt()
        return self.count_raw_tokens(text)

    def render_and_count(self, prompt):
        response = self.client.post(self.native_url + "/apply-template", json={
            "messages": [{"role": "user", "content": prompt}],
            "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": self.settings["enable_thinking"]},
        })
        response.raise_for_status()
        return self.count_raw_tokens(response.json()["prompt"])

    def call_model(self, temperature=None):
        self.attempt += 1
        started = time.perf_counter()
        prompt = self.generate_text_prompt()
        output_budget = self.settings["max_tokens"]
        request = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.settings["temperature"], "top_p": 1.0,
            "repeat_penalty": 1.0, "seed": self.settings["seed"],
            "max_tokens": output_budget, "stop": self.stop_sequences,
            "chat_template_kwargs": {"enable_thinking": self.settings["enable_thinking"]},
        }
        raw_dir = self.log_path.parent / "backend_requests"
        raw_dir.mkdir(exist_ok=True)
        raw_path = raw_dir / f"{self.attempt:04d}.json"
        raw = {"request": request, "response": None, "error": None}
        record = {
            "attempt": self.attempt, "model": self.model,
            "environment_index": self.environment_index,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_chars": len(prompt), "max_tokens": output_budget,
            "stop": self.stop_sequences, "status": "started",
            "finish_reason": None, "truncated": False, "usage": {},
            "cost_usd": None, "cost_status": "unknown-local-inference",
            "raw_record": str(raw_path.relative_to(self.log_path.parent)),
        }
        raw_path.write_text(json.dumps(raw, indent=2) + "\n")
        try:
            # Retain the complete official prompt. Never silently truncate history.
            tokens = self.render_and_count(prompt)
            record["prompt_tokens_preflight"] = tokens
            if tokens + output_budget > self.settings["context_tokens"]:
                raise RuntimeError(f"Explicit context overflow: {tokens} + "
                                   f"{output_budget} > {self.settings['context_tokens']}")
            response = self.client.post(self.settings["base_url"].rstrip("/") +
                                        "/chat/completions", json=request)
            record["http_status"] = response.status_code
            response.raise_for_status()
            result = response.json()
            raw["response"] = result
            choice = result["choices"][0]
            record["usage"] = result.get("usage", {})
            record["finish_reason"] = choice.get("finish_reason")
            record["truncated"] = choice.get("finish_reason") == "length"
            record["status"] = "truncated" if record["truncated"] else "completed"
            content = choice["message"].get("content")
            if content is None:
                raise RuntimeError("Backend returned no textual content.")
            # Native parser/scorer decides the episode; HTTP completion is not task success.
            return content
        except Exception as error:
            record["status"] = "error"
            record["error"] = type(error).__name__ + ": " + str(error)
            raw["error"] = record["error"]
            if isinstance(error, httpx.HTTPStatusError):
                raw["response_text"] = error.response.text
            raise
        finally:
            record["time_s"] = time.perf_counter() - started
            raw_path.write_text(json.dumps(raw, indent=2) + "\n")
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
