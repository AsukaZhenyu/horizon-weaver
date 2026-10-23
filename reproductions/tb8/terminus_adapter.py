"""Budget and transport probes around unchanged Terminal-Bench 0.2.18 agents.

Policy, parser, Chat history, summary prompts, and native retry decorators remain
upstream code. Runtime hooks dispatch only in the active agent thread.
"""
from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import threading
import uuid
import time
from pathlib import Path
from typing import Any

import httpx
import litellm
from terminal_bench.agents.base_agent import AgentResult
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.agents.terminus_1 import Terminus
from terminal_bench.agents.terminus_2.terminus_2 import Terminus2
from terminal_bench.terminal.tmux_session import TmuxSession

_ACTIVE = contextvars.ContextVar("tb8_active_budget", default=None)
_ORIGINAL_COMPLETION = litellm.completion
_ORIGINAL_SEND = httpx.Client.send
_ORIGINAL_SLEEP = time.sleep
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


class BudgetStop(BaseException):
    """Bypass upstream Exception-based retries after the task budget expires."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Budget:
    def __init__(self, directory: Path, wall_seconds=300.0, max_total_tokens=200000,
                 max_output_tokens=8192):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.deadline = self.started + float(wall_seconds)
        self.max_total_tokens = int(max_total_tokens)
        self.max_output_tokens = int(max_output_tokens)
        self.known_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown_usage_requests = 0
        self.http_attempts = 0
        self.completion_calls = 0
        self.episodes = 0
        self.episode = None
        self.command_counts = {}
        self.sent_commands = 0
        self.repeated_commands = 0
        self.phase = "decision"
        self.events_path = self.directory / "transport.jsonl"
        self._lock = threading.Lock()

    def remaining(self):
        return self.deadline - time.monotonic()

    def check(self):
        if self.remaining() <= 0:
            raise BudgetStop("wall_deadline")
        if self.known_tokens >= self.max_total_tokens:
            raise BudgetStop("observed_token_budget")

    def event(self, event: str, **fields):
        row = dict(event=event, elapsed_seconds=time.monotonic() - self.started,
                   episode=self.episode, phase=self.phase, **fields)
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def add_usage(self, usage):
        if not isinstance(usage, dict) or not isinstance(usage.get("total_tokens"), int):
            self.unknown_usage_requests += 1
            return False
        self.known_tokens += usage["total_tokens"]
        self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or 0)
        return True

    def summary(self):
        return dict(known_total_tokens=self.known_tokens,
                    known_prompt_tokens=self.input_tokens,
                    known_completion_tokens=self.output_tokens,
                    unknown_usage_requests=self.unknown_usage_requests,
                    http_attempts=self.http_attempts,
                    completion_calls=self.completion_calls, episodes=self.episodes,
                    sent_commands=self.sent_commands, repeated_commands=self.repeated_commands,
                    elapsed_seconds=time.monotonic() - self.started,
                    max_total_tokens=self.max_total_tokens,
                    token_budget_is_observed_stop_threshold=True,
                    prompt_estimate_is_not_server_tokenizer=True)


def register_model(model_name: str, context_tokens=16384, max_output_tokens=8192):
    """Prevent Terminus2's unknown-model 1M context fallback."""
    metadata = dict(max_tokens=int(context_tokens), max_input_tokens=int(context_tokens),
                    litellm_provider="openai",
                    mode="chat", supports_function_calling=True,
                    supports_response_schema=True, input_cost_per_token=0,
                    output_cost_per_token=0)
    bare = model_name.removeprefix("openai/")
    litellm.register_model(model_cost={bare: metadata, "openai/" + bare: metadata})
    for name in (bare, "openai/" + bare):
        litellm.model_cost[name] = metadata.copy()
    from litellm.utils import get_max_tokens
    if get_max_tokens("openai/" + bare) != int(context_tokens):
        raise RuntimeError("LiteLLM alias did not register the physical context window")
    return "openai/" + bare


def _bounded_sleep(seconds):
    budget = _ACTIVE.get()
    if budget is None:
        return _ORIGINAL_SLEEP(seconds)
    budget.check()
    duration = max(0.0, min(float(seconds), budget.remaining()))
    _ORIGINAL_SLEEP(duration)
    budget.check()


def _completion(*args, **kwargs):
    budget = _ACTIVE.get()
    if budget is None:
        return _ORIGINAL_COMPLETION(*args, **kwargs)
    budget.check()
    budget.completion_calls += 1
    estimate = None
    try:
        estimate = litellm.token_counter(model=kwargs.get("model"),
                                         messages=kwargs.get("messages", []))
    except Exception:
        pass
    available = budget.max_total_tokens - budget.known_tokens - (estimate or 0)
    if available <= 0:
        raise BudgetStop("estimated_prompt_exhausts_token_budget")
    requested_cap = kwargs.get("max_tokens") or budget.max_output_tokens
    kwargs["max_tokens"] = min(int(requested_cap), budget.max_output_tokens, available)
    kwargs["timeout"] = max(0.001, min(float(kwargs.get("timeout") or 90),
                                      budget.remaining()))
    budget.event("completion_start", completion_call=budget.completion_calls,
                 estimated_prompt_tokens=estimate, output_cap=kwargs["max_tokens"],
                 timeout_seconds=kwargs["timeout"], model=kwargs.get("model"))
    try:
        return _ORIGINAL_COMPLETION(*args, **kwargs)
    finally:
        budget.event("completion_end", completion_call=budget.completion_calls)


def _http_send(client, request, *args, **kwargs):
    budget = _ACTIVE.get()
    if budget is None or not request.url.path.rstrip("/").endswith("/chat/completions"):
        return _ORIGINAL_SEND(client, request, *args, **kwargs)
    budget.check()
    budget.http_attempts += 1
    request_number = budget.http_attempts
    remaining = max(0.001, budget.remaining())
    timeouts = dict(request.extensions.get("timeout") or {})
    for key in ("connect", "read", "write", "pool"):
        timeouts[key] = min(float(timeouts.get(key) or remaining), remaining)
    request.extensions["timeout"] = timeouts
    body = request.content
    try:
        data = json.loads(body)
    except Exception:
        data = {}
    budget.event("http_start", request_number=request_number,
                 request_sha256=_sha(body), request_bytes=len(body),
                 model=data.get("model"), temperature=data.get("temperature"),
                 max_tokens=data.get("max_tokens"), response_format=data.get("response_format"),
                 request_parameters={k: v for k, v in data.items() if k != "messages"},
                 message_count=len(data.get("messages", [])), timeout=timeouts,
                 sdk_retry_count=request.headers.get("x-stainless-retry-count"))
    started = time.monotonic()
    try:
        response = _ORIGINAL_SEND(client, request, *args, **kwargs)
        raw = response.read()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        usage = parsed.get("usage")
        known = budget.add_usage(usage)
        choices = parsed.get("choices") or []
        budget.event("http_end", request_number=request_number,
                     duration_seconds=time.monotonic() - started,
                     status_code=response.status_code, usage=usage,
                     usage_known=known, finish_reasons=[c.get("finish_reason") for c in choices],
                     response_sha256=_sha(raw), response_bytes=len(raw))
        budget.check()
        return response
    except BudgetStop:
        raise
    except BaseException as exc:
        budget.unknown_usage_requests += 1
        budget.event("http_error", request_number=request_number,
                     duration_seconds=time.monotonic() - started,
                     error_type=type(exc).__name__, error=str(exc), usage=None,
                     usage_known=False)
        raise


def install_probes():
    global _INSTALLED
    with _INSTALL_LOCK:
        if not _INSTALLED:
            litellm.telemetry = False
            litellm.suppress_debug_info = True
            litellm.completion = _completion
            httpx.Client.send = _http_send
            # Thread-local dispatch also bounds SDK, tenacity and tmux waits.
            time.sleep = _bounded_sleep
            _INSTALLED = True


class SessionScope:
    """Gate every Docker exec, including native blocking sends bypassing _exec_run."""

    def __init__(self, session, budget):
        self.session, self.budget = session, budget
        self.original_send = session.send_keys
        self.container = session.container
        self.original_exec = self.container.exec_run
        self.send_was_local = "send_keys" in getattr(session, "__dict__", {})
        self.exec_was_local = "exec_run" in getattr(self.container, "__dict__", {})

    def __enter__(self):
        @functools.wraps(self.original_send)
        def send_keys(keys, block=False, min_timeout_sec=0.0, max_timeout_sec=180.0):
            self.budget.check()
            remaining = max(0.001, self.budget.remaining())
            key_hash = _sha(str(keys).strip().encode())
            self.budget.sent_commands += 1
            prior = self.budget.command_counts.get(key_hash, 0)
            self.budget.command_counts[key_hash] = prior + 1
            self.budget.repeated_commands += int(prior > 0)
            self.budget.event("session_send", keys_sha256=_sha(str(keys).encode()),
                              normalized_exact_sha256=key_hash, previous_exact_count=prior,
                              block=block, requested_min_timeout=min_timeout_sec,
                              requested_max_timeout=max_timeout_sec,
                              effective_min_timeout=min(float(min_timeout_sec), remaining),
                              effective_max_timeout=min(float(max_timeout_sec), remaining))
            result = self.original_send(keys, block=block,
                min_timeout_sec=min(float(min_timeout_sec), remaining),
                max_timeout_sec=min(float(max_timeout_sec), remaining))
            self.budget.check()
            return result

        @functools.wraps(self.original_exec)
        def exec_run(*args, **kwargs):
            self.budget.check()
            api = getattr(getattr(self.container, "client", None), "api", None)
            prior_timeout = getattr(api, "timeout", None)
            if api is not None:
                api.timeout = min(float(prior_timeout or 60),
                                  max(0.001, self.budget.remaining()))
            try:
                result = self.original_exec(*args, **kwargs)
            finally:
                if api is not None:
                    api.timeout = prior_timeout
            self.budget.check()
            return result

        self.session.send_keys = send_keys
        self.container.exec_run = exec_run
        return self

    def __exit__(self, *exc):
        if self.send_was_local:
            self.session.send_keys = self.original_send
        else:
            del self.session.send_keys
        if self.exec_was_local:
            self.container.exec_run = self.original_exec
        else:
            del self.container.exec_run


def stop_foreground(session):
    """Cleanup only abnormal/unsubmitted runs; preserve normal submitted services."""
    result = {"attempted": True, "shell_return_confirmed": False,
              "session_stopped_fallback": False}
    api = getattr(getattr(session.container, "client", None), "api", None)
    old_timeout = getattr(api, "timeout", None)
    if api is not None:
        api.timeout = 3.0
    try:
        marker = "__TB8_CLEANUP_" + uuid.uuid4().hex + "__"
        session.send_keys("C-c", min_timeout_sec=0.1, max_timeout_sec=2)
        session.send_keys("printf '" + marker + "\\n'\n", min_timeout_sec=0.1,
                          max_timeout_sec=2)
        output = session.capture_pane()
        result["shell_return_confirmed"] = marker in [line.strip() for line in output.splitlines()]
        if not result["shell_return_confirmed"]:
            # Native stop() only exits the recorder; target this session explicitly.
            session.container.exec_run(["tmux", "kill-session", "-t", session._session_name])
            result["session_stopped_fallback"] = not session.is_session_alive()
            session.container.exec_run(session._tmux_start_session)
            session._previous_buffer = None
            result["grader_shell_restored"] = session.is_session_alive()
        result["success"] = (result["shell_return_confirmed"] or
            (result["session_stopped_fallback"] and result.get("grader_shell_restored", False)))
    except Exception as exc:
        result.update(success=False, error_type=type(exc).__name__, error=str(exc))
    finally:
        if api is not None:
            api.timeout = old_timeout
    return result


class _BudgetedAgent:
    def __init__(self, model_name, api_base=None, wall_seconds=300,
                 max_total_tokens=200000, max_output_tokens=8192,
                 context_tokens=16384, **kwargs):
        self._tb8_budget_args = dict(wall_seconds=wall_seconds,
            max_total_tokens=max_total_tokens, max_output_tokens=max_output_tokens)
        install_probes()
        routed = register_model(model_name, context_tokens, max_output_tokens)
        super().__init__(model_name=routed, api_base=api_base, **kwargs)
        self._tb8_confirmed = False
        self._tb8_batch_completed = False

    def _record_asciinema_marker(self, marker_text, session):
        try:
            return super()._record_asciinema_marker(marker_text, session)
        except ValueError as exc:
            origin = exc.__traceback__
            while origin.tb_next is not None:
                origin = origin.tb_next
            budget = _ACTIVE.get()
            # Only the original timestamp parser's float("") debug failure.
            if (str(exc) != "could not convert string to float: ''" or
                    origin.tb_frame.f_code is not TmuxSession.get_asciinema_timestamp.__code__ or
                    budget is None):
                raise
            budget.event("marker_logging_error", error_type=type(exc).__name__,
                         error=str(exc), skipped_debug_marker=True, marker=marker_text)

    def _setup_episode_logging(self, logging_dir, episode):
        budget = _ACTIVE.get()
        if budget:
            budget.check()
            budget.episode, budget.episodes = episode, episode + 1
            budget.event("episode_start")
        return super()._setup_episode_logging(logging_dir, episode)

    def _summarize(self, *args, **kwargs):
        budget = _ACTIVE.get()
        old_phase = budget.phase
        budget.phase = "native-summary"
        budget.event("summary_start")
        try:
            return super()._summarize(*args, **kwargs)
        finally:
            budget.event("summary_end")
            budget.phase = old_phase

    def _handle_llm_interaction(self, *args, **kwargs):
        self._tb8_batch_completed = False
        result = super()._handle_llm_interaction(*args, **kwargs)
        if isinstance(result, tuple):
            _, complete, feedback = result
            self._tb8_confirmed = bool(complete and self._pending_completion
                                       and "ERROR:" not in feedback)
        else:
            self._tb8_confirmed = bool(result.is_task_complete)
        budget = _ACTIVE.get()
        budget.event("native_decision", is_task_complete=(bool(result[1]) if isinstance(result, tuple)
                     else bool(result.is_task_complete)),
                     feedback=(result[2] if isinstance(result, tuple) else ""),
                     pending_before_execution=getattr(self, "_pending_completion", None),
                     completion_candidate=self._tb8_confirmed)
        return result

    def _execute_commands(self, commands, session):
        budget = _ACTIVE.get()
        budget.check()
        budget.event("command_batch_start", batch_commands=len(commands))
        result = super()._execute_commands(commands, session)
        self._tb8_batch_completed = True
        budget.event("command_batch_end", batch_commands=len(commands), native_timeout=result[0])
        return result

    def perform_task(self, instruction, session, logging_dir=None):
        if logging_dir is None:
            raise ValueError("TB8 requires per-task logging_dir")
        budget = Budget(Path(logging_dir), **self._tb8_budget_args)
        token = _ACTIVE.set(budget)
        stop_reason = "native_return"
        failure = None
        try:
            with SessionScope(session, budget):
                result = super().perform_task(instruction, session, logging_dir)
            budget.check()
            return result
        except BudgetStop as exc:
            stop_reason = exc.reason
            failure = (FailureMode.AGENT_TIMEOUT if exc.reason == "wall_deadline"
                       else FailureMode.UNKNOWN_AGENT_ERROR)
            return AgentResult(total_input_tokens=budget.input_tokens,
                total_output_tokens=budget.output_tokens, failure_mode=failure,
                timestamped_markers=self._timestamped_markers)
        except BaseException as exc:
            stop_reason = "native_exception"
            failure = type(exc).__name__
            raise
        finally:
            _ACTIVE.reset(token)
            submitted = (stop_reason == "native_return" and self._tb8_confirmed
                         and self._tb8_batch_completed)
            cleanup = ({"attempted": False, "reason": "native_submission"} if submitted
                       else stop_foreground(session))
            loop_end_reason = stop_reason
            if stop_reason == "native_return":
                loop_end_reason = ("native_submission" if submitted else
                    "episode_limit" if budget.episodes >= self._max_episodes else "session_ended")
            summary = budget.summary()
            summary.update(stop_reason=stop_reason, failure=str(failure) if failure else None,
                           native_submission=submitted, loop_end_reason=loop_end_reason,
                           foreground_cleanup=cleanup,
                           native_policy_unmodified=True, native_retry_decorators_retained=True,
                           adapter_changes=["OpenAI-compatible routing and context metadata",
                               "output/token/wall budget and per-HTTP instrumentation",
                               "deadline gates on HTTP/Docker exec/session waits"])
            (Path(logging_dir) / "budget-summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8")


class BudgetedTerminus2(_BudgetedAgent, Terminus2):
    @staticmethod
    def name():
        return "tb8-official-terminus-2-budgeted"


class BudgetedTerminus1(_BudgetedAgent, Terminus):
    @staticmethod
    def name():
        return "tb8-official-terminus-1-budgeted"


