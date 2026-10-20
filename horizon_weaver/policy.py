"""Structured policies and an OpenAI-compatible HTTP gateway, independent of tools."""
from __future__ import annotations

import ast
import json
import os
from copy import deepcopy
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .core import (Action, Decision, DecisionInput, ModelResponse, PolicyNotes, PolicyOutput, Usage,
                   action_schema, canonical, digest, parse_json)
from .errors import CheckDefinitionError, GatewayError
from .ports import GatewayPort


class ModelGateway:
    def __init__(self, model: str, base_url: str = "http://127.0.0.1:8080/v1",
                 temperature: float = .2, client: httpx.Client | None = None,
                 structured_output: str = "json_schema"):
        if structured_output not in ("none", "json_schema"):
            raise ValueError("structured_output must be none or json_schema")
        self.structured_output = structured_output
        if urlsplit(base_url).username or urlsplit(base_url).password:
            raise ValueError("credentials in base_url are forbidden; use HORIZON_WEAVER_API_KEY")
        if urlsplit(base_url).query or urlsplit(base_url).fragment:
            raise ValueError("base_url cannot contain a query or fragment")
        self.model, self.base_url, self.temperature = model, base_url.rstrip("/"), temperature
        self.client = client

    def complete(self, request: DecisionInput, timeout_s: float) -> ModelResponse:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("HORIZON_WEAVER_API_KEY")
        if key:
            headers["Authorization"] = "Bearer " + key
        conservative = Usage(input_tokens=request.input_token_bound,
                             output_tokens=request.max_output_tokens, estimated=True)
        if self.client is None:
            # A separate cancellable process bounds the whole request, including
            # slow streaming responses. Socket read timeouts alone do not do that.
            package_root = str(Path(__file__).resolve().parent.parent)
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(
                part for part in (package_root, os.environ.get("PYTHONPATH", "")) if part))
            payload = json.dumps({"model": self.model, "base_url": self.base_url,
                "temperature": self.temperature, "request": request.model_dump(mode="json"),
                "timeout_s": timeout_s, "structured_output": self.structured_output})
            process = subprocess.Popen([sys.executable, "-m", "horizon_weaver.gateway_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=package_root)
            try:
                stdout, stderr = process.communicate(payload.encode("utf-8"), timeout=max(.001, timeout_s))
                if process.returncode != 0:
                    raise GatewayError(f"gateway worker failed ({process.returncode}): "
                                       + stderr.decode("utf-8", errors="replace")[-500:], conservative)
                body = json.loads(stdout)
                if "error" in body:
                    raise GatewayError(body["error"], Usage.model_validate(body["usage"]),
                                       retryable=body.get("retryable", False))
                return ModelResponse.model_validate(body)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.communicate(timeout=5)
                raise GatewayError("model request exceeded its total deadline", conservative, retryable=True) from error
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
        try:
            payload = {"model": self.model, "temperature": self.temperature,
                       "max_tokens": request.max_output_tokens,
                       "messages": [{"role": "system", "content": request.system},
                                    {"role": "user", "content": request.context}]}
            if self.structured_output == "json_schema":
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "horizon_action", "schema": action_schema(
                        exploration_history=request.exploration_history,
                        verification_policy=request.verification_policy,
                        known_check_ids=request.known_check_ids,
                        reflection_required=request.reflection_required,
                        goal_review_refs=request.goal_review_refs)}}
            response = self.client.post(self.base_url + "/chat/completions", headers=headers,
                json=payload,
                timeout=httpx.Timeout(max(.001, timeout_s), connect=min(5, max(.001, timeout_s))))
            response.raise_for_status()
            body = response.json()
            message = body["choices"][0]["message"]
            content = message.get("content") or ""
            if not content.strip():
                content = message.get("reasoning_content") or ""
            usage = body.get("usage") or {}
            if "prompt_tokens" in usage and "completion_tokens" in usage:
                counted = Usage(input_tokens=usage["prompt_tokens"], output_tokens=usage["completion_tokens"])
            else:
                counted = conservative
            return ModelResponse(content=content, usage=counted, finish_reason=body["choices"][0].get("finish_reason"))
        except Exception as error:
            # A request can have reached the server even when no response arrives.
            transient_status = (isinstance(error, httpx.HTTPStatusError)
                and (error.response.status_code in (408, 429) or error.response.status_code >= 500))
            raise GatewayError(f"{type(error).__name__}: {error}", conservative,
                               retryable=isinstance(error, httpx.TimeoutException) or transient_status) from error

    def describe(self) -> dict:
        return {"kind": "model", "model": self.model, "base_url": self.base_url,
                "temperature": self.temperature, "structured_output": self.structured_output,
                "action_schema_hash": digest(canonical(action_schema())) if self.structured_output == "json_schema" else None,
                "schema_scope": "template; each request binds task capabilities and saved check references"}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def parse_notes(data) -> tuple[PolicyNotes | None, str | None]:
    if data is None:
        return None, None
    try:
        header = {"requirements": data} if isinstance(data, list) else data
        if not isinstance(header, dict) or not isinstance(header.get("requirements", []), (list, tuple)):
            return PolicyNotes.model_validate(header), None
        # Validate advisory entries independently. No malformed note can block
        # the executable action or discard its valid siblings.
        from .core import ExplorationIntent, RequirementNote, TaskUnderstanding
        valid, warnings = [], []
        for index, item in enumerate(header.get("requirements", [])):
            if index >= 24:
                warnings.append("requirements: entries beyond 24 ignored")
                break
            try:
                valid.append(RequirementNote.model_validate(item))
            except ValueError as error:
                warnings.append(f"requirements[{index}]: " + protocol_error(error))
        if warnings and not valid and header.get("requirements") and len(header) == 1:
            return None, "Notes ignored; action remains valid. " + "; ".join(warnings)[:1400]
        header = dict(header)
        if header.get('exploration') is not None:
            try:
                header['exploration'] = ExplorationIntent.model_validate(header['exploration'])
            except ValueError as error:
                header.pop('exploration')
                warnings.append('exploration: ' + protocol_error(error))
        if header.get('understanding') is not None:
            try:
                header['understanding'] = TaskUnderstanding.model_validate(header['understanding'])
            except ValueError as error:
                header.pop('understanding')
                warnings.append('understanding: ' + protocol_error(error))
        notes = PolicyNotes.model_validate(dict(header, requirements=valid))
        return notes, ("Invalid notes entries ignored; action remains valid. " + "; ".join(warnings)[:1400]
                       if warnings else None)
    except ValueError as error:
        return None, "Notes ignored; action remains valid. " + protocol_error(error)


def workspace_relative_path(path, workspace: str):
    """Translate file operands using the harness namespace, never program text."""
    if not isinstance(path, str) or not path:
        return path
    value = PurePosixPath(path)
    if path.startswith('/'):
        if (not workspace.startswith('/') or '..' in value.parts
                or '..' in PurePosixPath(workspace).parts):
            raise ValueError('absolute path is outside the declared workspace')
        try:
            value = value.relative_to(PurePosixPath(workspace))
        except ValueError as error:
            raise ValueError('absolute path is outside the declared workspace') from error
    return value.as_posix()


def normalize_workspace_paths(data, workspace: str):
    if not isinstance(data, dict):
        return data
    data = deepcopy(data)
    operands = data.get('action') if isinstance(data.get('action'), dict) else data
    if 'path' in operands:
        operands['path'] = workspace_relative_path(operands['path'], workspace)
    if isinstance(operands.get('query'), dict) and 'path' in operands['query']:
        operands['query']['path'] = workspace_relative_path(operands['query']['path'], workspace)
    for spec in operands.get('checks', []) if isinstance(operands.get('checks'), list) else []:
        if not isinstance(spec, dict):
            continue
        if 'path' in spec:
            spec['path'] = workspace_relative_path(spec['path'], workspace)
        for control in spec.get('controls', []) if isinstance(spec.get('controls'), list) else []:
            if not isinstance(control, dict) or not isinstance(control.get('overrides'), dict):
                continue
            translated = {}
            for path, value in control['overrides'].items():
                path = workspace_relative_path(path, workspace)
                if path in translated:
                    raise ValueError('duplicate control paths after workspace normalization')
                translated[path] = value
            control['overrides'] = translated
    return data


def parse_output_with_feedback(raw: str, workspace: str | None = None) -> tuple[PolicyOutput, str | None]:
    text = raw.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    # One strict object: either the action itself or a typed decision envelope.
    # No tool aliases, multiple-action selection, or extraction from prose.
    data = parse_json(text)
    if workspace is not None:
        data = normalize_workspace_paths(data, workspace)
    notes, warning = parse_notes(data.get("notes")) if isinstance(data, dict) else (None, None)
    if isinstance(data, dict):
        data = {key: value for key, value in data.items() if key != "notes"}
    if isinstance(data, dict) and "action" in data:
        envelope = PolicyOutput.model_validate(data)
        reasoning = "\n".join(part for part in (envelope.reasoning, envelope.action.reasoning) if part)
        output = envelope.model_copy(update={"action": envelope.action.model_copy(update={"reasoning": reasoning}),
                                             "notes": notes})
    else:
        output = PolicyOutput(action=Action.model_validate(data), notes=notes)
    # Validate new Policy definitions, not immutable messages during replay.
    # Parsing executes no code and never determines candidate correctness.
    for index, spec in enumerate(output.action.checks):
        if spec.kind != 'python_assertions':
            continue
        try:
            compile(ast.parse(spec.code), '<verification definition>', 'exec')
        except (SyntaxError, ValueError) as error:
            line = getattr(error, 'lineno', None)
            detail = getattr(error, 'msg', str(error))
            raise CheckDefinitionError(
                f'checks.{index}.code ({spec.id}): invalid Python syntax'
                + (f' at line {line}' if line else '') + f': {detail}. '
                'code contains Python source, not a shell command. Compound for/if/with blocks '
                'need newlines and indentation; semicolons cannot join compound statements.') from error
    return output, warning


def parse_output(raw: str) -> PolicyOutput:
    return parse_output_with_feedback(raw)[0]


def parse_action(raw: str) -> Action:
    return parse_output(raw).action


def protocol_error(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join((".".join(map(str, item["loc"])) or "object") + ": " + item["msg"]
                         for item in error.errors(include_input=False, include_url=False))[:1800]
    return str(error)[:1800]


class ModelPolicy:
    def __init__(self, gateway: GatewayPort, workspace: str = ''):
        self.gateway, self.workspace = gateway, workspace

    def decide(self, request: DecisionInput, timeout_s: float) -> Decision:
        response = self.gateway.complete(request, timeout_s)
        try:
            output, warning = parse_output_with_feedback(response.content, self.workspace)
            return Decision(action=output.action, notes=output.notes, error=warning,
                            raw=response.content, usage=response.usage)
        except ValueError as error:
            invalid_check = (isinstance(error, CheckDefinitionError) or
                (isinstance(error, ValidationError)
                 and any('checks' in item['loc'] or 'check_ids' in item['loc']
                         for item in error.errors(include_input=False, include_url=False))))
            hint = ("Output was truncated. Use a shorter single JSON action or split work across commands. "
                    if response.finish_reason == "length" else
                    'Invalid verification definition; no check or tool was executed. '
                    'Correct the check operands, not the candidate based on this error. '
                    if invalid_check else
                    'Return one complete JSON action, e.g. {"kind":"command","command":"..."}. ')
            # Do not echo malformed objects into context: small models copy
            # the faulty envelope from Pydantic's input_value diagnostics.
            detail = protocol_error(error)
            return Decision(raw=response.content, usage=response.usage, error=hint + detail)

    def describe(self) -> dict:
        return dict(self.gateway.describe(), frontend='state-ir', workspace=self.workspace)


class ScriptedPolicy:
    """A stateless deterministic adapter for architecture tests and reproducible demos."""
    def __init__(self, actions: tuple[Action, ...]):
        self.actions = actions

    def decide(self, request: DecisionInput, timeout_s: float) -> Decision:
        if request.step > len(self.actions):
            action = Action(kind="done")
        else:
            action = self.actions[request.step - 1]
        return Decision(action=action, raw=action.model_dump_json())

    def describe(self) -> dict:
        return {"kind": "scripted", "actions": [action.model_dump(mode="json") for action in self.actions]}
