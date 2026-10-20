"""Immutable domain messages. This module has no execution or storage dependencies."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1
EventKind = Literal["RunStarted", "ObservationCaptured", "ModelCallStarted", "ModelCallCompleted",
                    "ActionProposed", "ActionExecuted", "EvaluationStarted", "StepFinished",
                    "CheckEvaluated", "RunTerminated", "RunInterrupted", "EnvironmentStarted", "RunResumed"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def canonical(value: BaseModel | dict | list) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_json(text: str):
    def reject_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)


def json_equal(actual, expected) -> bool:
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(json_equal(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(json_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Budget(Message):
    max_steps: int = Field(default=100, ge=1)
    wall_time_s: float = Field(default=600, gt=0)
    tool_timeout_s: float = Field(default=30, gt=0)
    max_tool_timeout_s: float = Field(default=120, gt=0)
    max_model_tokens: int = Field(default=100_000, ge=1)
    context_tokens: int = Field(default=8192, ge=256)
    output_tokens: int = Field(default=8192, ge=1)
    decision_output_tokens: int = Field(default=2048, ge=1)
    model_timeout_s: float = Field(default=60, gt=0)
    max_model_timeout_s: float = Field(default=120, gt=0)
    no_progress_limit: int = Field(default=8, ge=1)


class Check(Message):
    id: str = Field(min_length=1)
    kind: Literal["text_equals", "json_equals", "command", "external"]
    path: str = ""
    expected: str = ""
    command: str = ""

    @model_validator(mode="after")
    def validate_check(self):
        if self.id.startswith("artifact:"):
            raise ValueError("check IDs cannot use the reserved artifact: prefix")
        if self.kind == "command" and not self.command.strip():
            raise ValueError("command checks require a command")
        if self.kind == "external" and (self.path or self.command or self.expected):
            raise ValueError("external checks carry only an evaluator identity, not executable tests")
        if self.kind not in ("command", "external") and not self.path:
            raise ValueError("file checks require a path")
        if self.kind == "json_equals":
            parse_json(self.expected)
        return self


class GoalCondition(Message):
    """A narrow public condition, with its exact source and observable operands."""
    id: str
    ref: str
    quote: str
    related_refs: tuple[str, ...] = ()
    kind: Literal['artifact', 'python_interface', 'text_literal', 'behavior', 'csv_header', 'json_shape']
    path: str = ''
    symbol: str = ''
    required_async: bool = False
    parameters: tuple[str, ...] = ()
    callable_tasks: bool = False
    expected: str = ''
    required_keys: tuple[str, ...] = ()


class GoalObservation(Message):
    path: str
    sha256: str
    result: 'ToolResult'


class TaskContract(Message):
    task_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    constraints: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = (".",)
    expected_artifacts: tuple[str, ...] = ()
    acceptance_checks: tuple[Check, ...] = Field(min_length=1)
    require_self_check: bool = False
    exploration_history: bool = False
    verification_policy: Literal["legacy", "behavioral"] = "legacy"
    goal_conditions: tuple[GoalCondition, ...] = ()
    require_goal_review: bool = False
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def validate_contract(self):
        ids = [check.id for check in self.acceptance_checks]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance check IDs must be unique")
        if any(check.kind == "external" for check in self.acceptance_checks) and not self.deferred_verification:
            raise ValueError("deferred external verification cannot be mixed with inline checks")
        if not self.allowed_paths:
            raise ValueError("allowed_paths cannot be empty")
        for path in (*self.allowed_paths, *self.expected_artifacts,
                     *(item.path for item in self.goal_conditions if item.path),
                     *(check.path for check in self.acceptance_checks if check.path)):
            validate_relative_path(path)
        allowed = tuple(PurePosixPath(path) for path in self.allowed_paths)
        for path in (*self.expected_artifacts, *(check.path for check in self.acceptance_checks if check.path)):
            if not any(PurePosixPath(path).is_relative_to(scope) for scope in allowed):
                raise ValueError(f"contract file is outside allowed_paths: {path}")
        refs = {f'G{i}': line for i, line in enumerate(self.goal.splitlines(), 1) if line.strip()}
        if len({item.id for item in self.goal_conditions}) != len(self.goal_conditions):
            raise ValueError('goal condition IDs must be distinct')
        for item in self.goal_conditions:
            if item.ref not in refs or not item.quote or item.quote not in refs[item.ref]:
                raise ValueError('goal condition must cite its exact public source')
            if any(ref not in refs for ref in item.related_refs):
                raise ValueError('goal condition related source must exist')
            if item.path and not any(PurePosixPath(item.path).is_relative_to(scope) for scope in allowed):
                raise ValueError('goal condition path outside allowed_paths')
        return self

    @property
    def deferred_verification(self) -> bool:
        return all(check.kind == "external" for check in self.acceptance_checks)


def validate_relative_path(path: str) -> None:
    # Contract paths are portable, workspace-relative POSIX paths.
    if not path or "\\" in path or ":" in path or path.startswith("/"):
        raise ValueError(f"expected a workspace-relative POSIX path: {path!r}")
    if ".." in path.split("/"):
        raise ValueError(f"parent traversal is forbidden: {path!r}")


class ActionKind(str, Enum):
    READ = "read"
    WRITE = "write"
    COMMAND = "command"
    VERIFY = "verify"
    DONE = "done"
    HISTORY = "history"
    INCOMPLETE = "incomplete"


class CheckControl(Message):
    name: str = Field(min_length=1, max_length=100)
    overrides: dict[str, str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_paths(self):
        if sum(len(value.encode('utf-8')) for value in self.overrides.values()) > 32768:
            raise ValueError('control overrides exceed 32 KiB')
        for path in self.overrides:
            validate_relative_path(path)
        return self


class CheckSpec(Message):
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["text_equals", "json_equals", "python_assertions", "process", "command"]
    path: str = ""
    expected: str = ""
    code: str = Field(default="", max_length=32000)
    command: str = ""
    argv: tuple[str, ...] = Field(default=(), max_length=64)
    stdin: str = Field(default="", max_length=32000)
    expected_exit_code: int = 0
    expected_stdout: str | None = None
    expected_stderr: str | None = None
    target_refs: tuple[str, ...] = Field(default=(), max_length=12)
    oracle: Literal["policy", "public_input", "contract"] = "policy"
    basis: str = Field(default="", max_length=400)
    controls: tuple[CheckControl, ...] = Field(default=(), max_length=2)
    replaces: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_spec(self):
        if self.replaces and not self.basis.strip():
            raise ValueError('check replacement requires an explicit basis')
        if self.kind in ("text_equals", "json_equals"):
            validate_relative_path(self.path)
            if self.code or self.command:
                raise ValueError("file comparisons carry path/expected, not code/command")
            if self.kind == "json_equals":
                try:
                    parse_json(self.expected)
                except ValueError as error:
                    raise ValueError(
                        'json_equals.expected must be concrete valid JSON, not a description or placeholder. '
                        'For large or input-derived results, use python_assertions to compare the candidate '
                        'with expectations computed from the original inputs/public goal.') from error
        elif self.path or self.expected:
            raise ValueError("path/expected are only valid for file comparisons")
        if self.kind == "python_assertions" and (not self.code.strip() or self.command):
            raise ValueError("python_assertions requires code, not command")
        if self.kind == "command" and (not self.command.strip() or self.code or self.controls):
            raise ValueError("command requires command; controls need an instrumented check")
        if self.kind == 'process':
            if not self.argv or not self.argv[0] or self.code or self.command:
                raise ValueError('process requires argv, without code/command')
            if any('\x00' in arg for arg in self.argv):
                raise ValueError('process argv cannot contain NUL')
        elif (self.argv or self.stdin or self.expected_exit_code != 0
              or self.expected_stdout is not None or self.expected_stderr is not None):
            raise ValueError('process operands require kind=process')
        return self


class HistoryQuery(Message):
    path: str = ""
    ref: str = ""
    check_id: str = Field(default="", max_length=100)
    method: str = ""
    steps: tuple[int, ...] = Field(default=(), max_length=12)
    failures_only: bool = False
    limit: int = Field(default=5, ge=1, le=12)

    @model_validator(mode="after")
    def validate_path(self):
        if self.path:
            validate_relative_path(self.path)
        return self


class GoalReview(Message):
    ref: str = Field(min_length=1, max_length=40)
    status: Literal['unknown', 'supported', 'contradicted'] = 'unknown'
    satisfied_when: str = Field(min_length=1, max_length=300)
    evidence_steps: tuple[int, ...] = Field(default=(), max_length=8)
    evidence_check_ids: tuple[str, ...] = Field(default=(), max_length=8)
    needed_information: str = Field(default='', max_length=300)


class Action(Message):
    kind: ActionKind
    path: str = ""
    content: str | None = None
    command: str = ""
    reasoning: str = ""

    timeout_s: float | None = Field(default=None, gt=0)
    checks: tuple[CheckSpec, ...] = Field(default=(), max_length=8)
    check_ids: tuple[str, ...] = Field(default=(), max_length=8)
    query: HistoryQuery | None = None
    review: tuple[GoalReview, ...] = Field(default=(), max_length=24)

    @model_validator(mode="after")
    def validate_action(self):
        if self.review and self.kind != ActionKind.DONE:
            raise ValueError('goal review is only carried by done')
        if self.kind in (ActionKind.READ, ActionKind.WRITE):
            validate_relative_path(self.path)
        if self.kind == ActionKind.WRITE and self.content is None:
            raise ValueError("write requires content (an empty string is allowed)")
        if self.kind == ActionKind.COMMAND and not self.command.strip():
            raise ValueError("command requires a nonempty command")
        if self.kind not in (ActionKind.READ, ActionKind.WRITE) and self.path:
            raise ValueError("path is only valid for read/write")
        if self.kind != ActionKind.WRITE and self.content is not None:
            raise ValueError("content is only valid for write")
        if self.kind not in (ActionKind.COMMAND, ActionKind.VERIFY) and self.command:
            raise ValueError("command is only valid for command/verify actions")
        if self.timeout_s is not None and self.kind not in (ActionKind.COMMAND, ActionKind.VERIFY, ActionKind.DONE):
            raise ValueError("timeout_s is only valid for command/verify")
        if self.kind == ActionKind.DONE and self.timeout_s is not None and not (self.checks or self.check_ids):
            raise ValueError('done timeout_s requires executable checks')
        if self.checks and (self.kind not in (ActionKind.VERIFY, ActionKind.DONE) or self.command):
            raise ValueError("checks are only valid for verify/done, without command")
        if len({check.id for check in self.checks}) != len(self.checks):
            raise ValueError("duplicate check id in one action")
        if self.check_ids and (self.kind not in (ActionKind.VERIFY, ActionKind.DONE)
                               or self.checks or self.command):
            raise ValueError('check_ids require verify/done without checks/command')
        if len(set(self.check_ids)) != len(self.check_ids) or any(not identity for identity in self.check_ids):
            raise ValueError('check_ids must be distinct nonempty IDs')
        if self.query is not None and self.kind != ActionKind.HISTORY:
            raise ValueError("query is only valid for history")
        return self

    @property
    def signature(self) -> str:
        return digest(canonical(self.model_dump(exclude={"reasoning", "timeout_s", "review"}, mode="json")))

    @property
    def execution_signature(self) -> str:
        data = self.model_dump(exclude={"reasoning", "review"}, mode="json")
        data['checks'] = [spec.model_dump(exclude={'id', 'target_refs', 'oracle', 'basis', 'replaces'}, mode='json')
                          for spec in self.checks]
        for spec in data['checks']:
            spec['controls'] = [dict(overrides=control['overrides']) for control in spec.get('controls', [])]
        return digest(canonical(data))


class FileState(Message):
    path: str
    kind: Literal["file", "directory", "symlink"]
    sha256: str
    size: int
    mode: int


class RequirementNote(Message):
    ref: str = Field(default="", max_length=40)
    quote: str = Field(default="", max_length=400)
    check_steps: tuple[int, ...] = Field(default=(), max_length=8)
    evidence_steps: tuple[int, ...] = Field(default=(), max_length=8)
    uncertainty: str = Field(default="", max_length=300)
    interpretation: str = Field(default="", max_length=300)
    satisfied_when: str = Field(default="", max_length=300)
    needed_information: str = Field(default="", max_length=300)
    status: Literal['unknown', 'supported', 'contradicted'] = 'unknown'
    evidence_check_ids: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def has_source(self):
        if not self.ref and not self.quote:
            raise ValueError("cite a public requirement ref or an exact quote")
        return self


def requirement_sources(task: TaskContract) -> dict[str, str]:
    """Stable textual locations, without interpreting or rewriting requirements."""
    sources = {f"G{number}": line for number, line in enumerate(task.goal.splitlines(), 1) if line.strip()}
    sources.update({f"C{number}": value for number, value in enumerate(task.constraints, 1)})
    return sources


class ExplorationIntent(Message):
    problem_refs: tuple[str, ...] = Field(default=(), max_length=12)
    paths: tuple[str, ...] = Field(default=(), max_length=12)
    method: str = Field(default="", max_length=100)
    hypothesis: str = Field(default="", max_length=400)
    expected_observation: str = Field(default="", max_length=400)
    evidence_steps: tuple[int, ...] = Field(default=(), max_length=12)
    revises_steps: tuple[int, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def validate_paths(self):
        for path in self.paths:
            validate_relative_path(path)
        return self


class TaskUnderstanding(Message):
    """Revisable Policy interpretation, never a verdict or an inferred fact."""
    approach: str = Field(default="", max_length=300)
    current_problem: str = Field(default="", max_length=300)
    interpretation: str = Field(default="", max_length=500)
    next_focus: str = Field(default="", max_length=300)
    evidence_steps: tuple[int, ...] = Field(default=(), max_length=12)
    open_refs: tuple[str, ...] = Field(default=(), max_length=12)


class PolicyNotes(Message):
    """Policy claims, never human requirements or acceptance evidence."""
    requirements: tuple[RequirementNote, ...] = Field(default=(), max_length=24)
    next_output_tokens: int | None = Field(default=None, ge=1)
    next_timeout_s: float | None = Field(default=None, gt=0)
    exploration: ExplorationIntent | None = None
    understanding: TaskUnderstanding | None = None

    @model_validator(mode="before")
    @classmethod
    def list_form(cls, data):
        # Both forms describe advice, never an additional executable action.
        return {"requirements": data} if isinstance(data, list) else data


class PolicyOutput(Message):
    """Unambiguous model decision envelope; its single action uses the same IR."""
    action: Action
    reasoning: str = ""
    notes: PolicyNotes | None = None


def action_schema(*, exploration_history: bool | None = None,
                  verification_policy: str | None = None,
                  known_check_ids: tuple[str, ...] | None = None,
                  reflection_required: bool = False,
                  goal_review_refs: tuple[str, ...] = ()) -> dict:
    """Decoder syntax for the Action IR; runtime validators still enforce semantics."""
    history_enabled = exploration_history is not False
    checks_enabled = verification_policy != 'legacy'
    variants = []
    for kind in ActionKind:
        if (kind == ActionKind.HISTORY and not history_enabled
                or kind == ActionKind.INCOMPLETE and not checks_enabled):
            continue
        properties = {"kind": {"const": kind.value}, "reasoning": {"type": "string", "maxLength": 300}}
        required = ["kind"]
        if kind in (ActionKind.READ, ActionKind.WRITE):
            properties["path"] = {"type": "string", "minLength": 1,
                                  "pattern": r"^[^/\\:][^\\:]*$"}
            required.append("path")
        if kind == ActionKind.WRITE:
            properties["content"] = {"type": "string"}
            required.append("content")
        if kind in (ActionKind.COMMAND, ActionKind.VERIFY):
            properties["command"] = {"type": "string", "minLength": 1}
            properties["timeout_s"] = {"type": "number", "exclusiveMinimum": 0}
            required.append("command")
        if kind in (ActionKind.VERIFY, ActionKind.DONE) and checks_enabled:
            # Separate command and structured-check variants. A decoder must
            # never produce two incompatible execution operands in one action.
            if kind == ActionKind.VERIFY:
                variants.append({"type": "object", "properties": dict(properties),
                    "required": list(required), "additionalProperties": False})
                properties.pop('command')
                required.remove('command')
                required.append('checks')
            properties["checks"] = {"type": "array", "minItems": 1, "maxItems": 8,
                                    "items": {"$ref": "#/$defs/CheckSpec"}}
            properties["timeout_s"] = {"type": "number", "exclusiveMinimum": 0}
            # None preserves the generic schema API. Runtime requests bind
            # references to actually recorded definitions, including none yet.
            if known_check_ids is None or known_check_ids:
                identity = {'type': 'string', 'minLength': 1}
                if known_check_ids is not None:
                    identity['enum'] = sorted(set(known_check_ids))
                variants.append({"type": "object", "properties": {
                    'kind': {'const': kind.value}, 'reasoning': properties['reasoning'],
                    'timeout_s': properties['timeout_s'],
                    'check_ids': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                                  'items': identity}},
                    'required': ['kind', 'check_ids'], 'additionalProperties': False})
            if kind == ActionKind.DONE:
                variants.append({"type": "object", "properties": {
                    'kind': {'const': 'done'}, 'reasoning': properties['reasoning']},
                    "required": ['kind'], "additionalProperties": False})
                required.append('checks')
        if kind == ActionKind.HISTORY:
            properties["query"] = HistoryQuery.model_json_schema()
        variants.append({"type": "object", "properties": properties,
                         "required": required, "additionalProperties": False})
    action = {"anyOf": variants}
    note_fields = {
        "ref": {"type": "string", "maxLength": 40},
        "quote": {"type": "string", "minLength": 1, "maxLength": 400},
        "check_steps": {"type": "array", "maxItems": 8, "items": {"type": "integer"}},
        "evidence_steps": {"type": "array", "maxItems": 8, "items": {"type": "integer"}},
        "uncertainty": {"type": "string", "maxLength": 300},
        "interpretation": {"type": "string", "maxLength": 300},
        "satisfied_when": {"type": "string", "maxLength": 300},
        "needed_information": {"type": "string", "maxLength": 300},
        "status": {"type": "string", "enum": ['unknown', 'supported', 'contradicted']},
        "evidence_check_ids": {"type": "array", "maxItems": 8, "items": {"type": "string"}}}
    # Put the complete object constraints in each branch: grammar converters
    # commonly discard sibling properties when they encounter anyOf.
    if not checks_enabled:
        note_fields.pop('evidence_check_ids')
    note = {'anyOf': [dict(type='object', properties=note_fields,
                          required=[source], additionalProperties=False)
                      for source in ('ref', 'quote')]}
    envelope = {"type": "object", "properties": {
        "action": action, "reasoning": {"type": "string", "maxLength": 300},
        "notes": {"anyOf": [{"type": "object", "properties": {
            "requirements": {"type": "array", "maxItems": 24, "items": note},
            "next_output_tokens": {"type": "integer", "minimum": 1},
            "next_timeout_s": {"type": "number", "exclusiveMinimum": 0}},
            "additionalProperties": False}, {"type": "array", "maxItems": 24, "items": note}]}},
        "required": ["action"], "additionalProperties": False}
    if history_enabled:
        envelope["properties"]["notes"]["anyOf"][0]["properties"]["exploration"] = ExplorationIntent.model_json_schema()
        envelope["properties"]["notes"]["anyOf"][0]["properties"]["understanding"] = TaskUnderstanding.model_json_schema()
    definitions = {}
    if checks_enabled:
        original = CheckSpec.model_json_schema()
        definitions.update(original.get('$defs', {}))
        fields = original['properties']
        specs = []
        for check_kind, operands in [('text_equals', ('path', 'expected')),
                ('json_equals', ('path', 'expected')), ('python_assertions', ('code',)),
                ('process', ('argv',)), ('command', ('command',))]:
            names = ('id', 'target_refs', 'oracle', 'basis', 'replaces', *operands)
            # Choose the check type before emitting its operands. Local grammar
            # decoders follow property order; a late discriminator encourages
            # generating a command before a process/code branch is selected.
            spec_fields = {'kind': {'const': check_kind}, **{key: fields[key] for key in names}}
            if check_kind == 'python_assertions':
                # Some local decoders expand bounded strings into repetitions
                # and silently disable a grammar above their implementation cap.
                # The 32k limit stays in CheckSpec's runtime validator.
                spec_fields['code'] = {key: value for key, value in spec_fields['code'].items()
                                       if key != 'maxLength'}
            spec_fields['kind'] = {'const': check_kind}
            if check_kind != 'command':
                spec_fields['controls'] = fields['controls']
            if check_kind == 'process':
                spec_fields['argv'] = dict(fields['argv'], minItems=1)
                for key in ('stdin', 'expected_exit_code', 'expected_stdout', 'expected_stderr'):
                    spec_fields[key] = fields[key]
                spec_fields['stdin'] = {'type': 'string'}
            base_fields = dict(spec_fields)
            base_fields['replaces'] = dict(fields['replaces'], maxItems=0)
            specs.append(dict(type='object', properties=base_fields,
                required=['id', 'kind', *operands], additionalProperties=False))
            # Revision metadata is coupled in grammar as well as at runtime.
            # Emit it before possibly long code/content so it cannot be omitted.
            revision_fields = dict(kind=spec_fields['kind'], id=spec_fields['id'],
                replaces=dict(fields['replaces'], minItems=1),
                target_refs=dict(fields['target_refs'], minItems=1),
                basis=dict(fields['basis'], minLength=1))
            revision_fields.update({key: value for key, value in spec_fields.items()
                                    if key not in revision_fields})
            specs.append(dict(type='object', properties=revision_fields,
                required=['id', 'kind', 'replaces', 'target_refs', 'basis', *operands], additionalProperties=False))
        definitions['CheckSpec'] = {'anyOf': specs}
    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            return inline(definitions[value['$ref'].removeprefix('#/$defs/')])
        return {key: inline(item) for key, item in value.items()}
    # Inline finite IR definitions: nested refs are not uniformly enforced by
    # OpenAI-compatible grammar implementations, even when HTTP returns 200.
    # Review belongs to done, not every ordinary action/obstacle envelope.
    if goal_review_refs:
        fields = GoalReview.model_json_schema()['properties']
        fields['ref'] = {'type': 'string', 'enum': list(goal_review_refs)}
        for name in ('satisfied_when', 'needed_information'):
            fields[name] = {'type': 'string'}
        review_item = dict(type='object', properties=fields,
            required=['ref','status','satisfied_when','evidence_steps'],additionalProperties=False)
        for variant in variants:
            if variant['properties']['kind']['const']=='done':
                variant['properties']['review'] = dict(type='array',minItems=1,maxItems=24,items=review_item)
                variant['required'].append('review')
    if reflection_required and history_enabled:
        # The same decision carries a short evidence update before its action.
        # Runtime parsing still accepts valid actions from unstructured providers.
        understanding = TaskUnderstanding.model_json_schema()
        understanding['required'] = ['current_problem', 'interpretation', 'next_focus', 'evidence_steps']
        note_properties = envelope['properties']['notes']['anyOf'][0]['properties']
        notes = {'type': 'object', 'properties': dict(note_properties, understanding=understanding),
            'required': ['understanding'], 'additionalProperties': False}
        envelope = {'type': 'object', 'properties': {'notes': notes, 'action': action},
                    'required': ['notes', 'action'], 'additionalProperties': False}
        return inline({'anyOf': [envelope]})
    return inline({"anyOf": [*variants, envelope]})


class StateSnapshot(Message):
    files: tuple[FileState, ...] = ()

    @property
    def fingerprint(self) -> str:
        return digest(canonical(self))

    def file(self, path: str) -> FileState | None:
        return next((item for item in self.files if item.path == path), None)


class StateDelta(Message):
    changed_files: tuple[str, ...] = ()

    @classmethod
    def between(cls, before: StateSnapshot, after: StateSnapshot) -> StateDelta:
        old = {item.path: item for item in before.files}
        new = {item.path: item for item in after.files}
        return cls(changed_files=tuple(sorted(
            path for path in old.keys() | new.keys() if old.get(path) != new.get(path)
        )))


class ToolResult(Message):
    output: str = ""
    exit_code: int | None = None
    error_type: str | None = None
    duration_s: float = Field(default=0, ge=0)
    outcome_unknown: bool = False
    execution_terminated: bool = False
    execution_id: str = ""
    transport_recovered: bool = False

    @model_validator(mode="after")
    def validate_termination(self):
        if self.execution_terminated and (self.outcome_unknown or self.exit_code is None):
            raise ValueError("confirmed execution termination requires a known failure exit status")
        return self


class Evidence(Message):
    check_id: str
    check_hash: str
    snapshot_hash: str
    passed: bool
    result: ToolResult

class SelfCheckReceipt(Message):
    """Runtime observation of a policy-proposed check, never contract acceptance."""
    source: Literal["policy"] = "policy"
    command: str = Field(min_length=1)
    command_hash: str
    snapshot_hash: str
    workspace_unchanged: bool
    result: ToolResult
    classification: Literal["observation", "check_candidate", "legacy_unknown"] = "legacy_unknown"
    classification_reason: str = ""

    @model_validator(mode="after")
    def validate_receipt(self):
        if self.command_hash != digest(self.command):
            raise ValueError("self-check command hash does not match")
        return self

    @property
    def passed(self) -> bool:
        return (self.classification != "observation" and self.workspace_unchanged and not self.result.outcome_unknown
                and self.result.exit_code == 0 and self.result.error_type is None)


class ControlReceipt(Message):
    name: str
    status: Literal["pass", "fail", "inconclusive"]
    result: ToolResult
    assertions: int = Field(default=0, ge=0)


class VerificationReceipt(Message):
    spec: CheckSpec
    spec_hash: str
    snapshot_hash: str
    environment_hash: str
    workspace_unchanged: bool
    result: ToolResult
    status: Literal["pass", "fail", "inconclusive"]
    assertions: int = Field(default=0, ge=0)
    controls: tuple[ControlReceipt, ...] = ()
    oracle_validated: bool = False

    @model_validator(mode="after")
    def validate_record(self):
        if self.spec_hash != digest(canonical(self.spec)):
            # dev1--dev6 receipts predate process operands. Read old immutable
            # journals without rewriting their definition hashes.
            process_fields = {'argv', 'stdin', 'expected_exit_code', 'expected_stdout', 'expected_stderr'}
            old_spec = self.spec.model_dump(exclude=process_fields, mode='json')
            if self.spec.kind == 'process' or self.spec.argv or self.spec.stdin or self.spec.expected_exit_code != 0 or self.spec.expected_stdout is not None or self.spec.expected_stderr is not None or self.spec_hash != digest(canonical(old_spec)):
                raise ValueError("verification definition hash mismatch")
        if self.result.outcome_unknown and self.status != "inconclusive":
            raise ValueError("unknown execution cannot yield a check result")
        if self.status == "pass" and (not self.workspace_unchanged or self.result.error_type
                                      or self.result.exit_code != 0):
            raise ValueError("passing verification requires completed unchanged execution")
        if self.assertions and self.spec.kind == "command":
            raise ValueError("shell exit status cannot certify executed assertions")
        if len(self.controls) > len(self.spec.controls):
            raise ValueError("control receipts have no matching definitions")
        for item, definition in zip(self.controls, self.spec.controls):
            if item.name != definition.name:
                raise ValueError("control identity mismatch")
        return self

    @property
    def behavioral(self) -> bool:
        return self.spec.kind != "command" and self.assertions > 0

    @property
    def sensitive(self) -> bool:
        return bool(self.controls) and len(self.controls) == len(self.spec.controls) and all(
            item.status == "fail" and item.assertions > 0 for item in self.controls)


class Verdict(Message):
    passed: bool = False
    satisfied_requirements: tuple[str, ...] = ()
    unsatisfied_requirements: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()


class Evaluation(Message):
    snapshot: StateSnapshot
    verdict: Verdict


def current_verdict(task: TaskContract, verdict: Verdict, snapshot: StateSnapshot) -> Verdict:
    """Invalidate stale evidence; this operation cannot create a passing verdict."""
    hashes = {check.id: digest(canonical(check)) for check in task.acceptance_checks}
    hashes.update({"artifact:" + path: digest(path) for path in task.expected_artifacts})
    valid = tuple(item for item in verdict.evidence
                  if item.snapshot_hash == snapshot.fingerprint
                  and hashes.get(item.check_id) == item.check_hash)
    satisfied = tuple(item.check_id for item in valid if item.passed)
    unsatisfied = tuple(key for key in hashes if key not in satisfied)
    return verdict.model_copy(update={"passed": verdict.passed and not unsatisfied,
                                      "satisfied_requirements": satisfied,
                                      "unsatisfied_requirements": unsatisfied,
                                      "evidence": valid})


class Usage(Message):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, other: Usage) -> Usage:
        return Usage(input_tokens=self.input_tokens + other.input_tokens,
                     output_tokens=self.output_tokens + other.output_tokens,
                     estimated=self.estimated or other.estimated)


class DecisionInput(Message):
    step: int
    system: str
    context: str
    input_token_bound: int
    max_output_tokens: int
    visible_observation_steps: tuple[int, ...] = ()
    visible_write_steps: tuple[int, ...] = ()
    selected_attempt_steps: tuple[int, ...] = ()
    state_version: str = ""
    model_timeout_s: float | None = Field(default=None, gt=0)
    exploration_history: bool | None = None
    verification_policy: Literal['legacy', 'behavioral'] | None = None
    known_check_ids: tuple[str, ...] | None = None
    reflection_required: bool = False
    goal_review_refs: tuple[str, ...] = ()


class ModelResponse(Message):
    content: str
    usage: Usage
    finish_reason: str | None = None


class Decision(Message):
    action: Action | None = None
    raw: str = ""
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    notes: PolicyNotes | None = None


class ProgressAssessment(Message):
    level: Literal["none", "information", "state_change", "verified"]
    repeated: bool = False
    reason_code: str
    matching_steps: tuple[int, ...] = ()


class Attempt(Message):
    step: int
    action: Action
    result: ToolResult
    before: StateSnapshot
    after: StateSnapshot
    delta: StateDelta
    progress: ProgressAssessment
    self_check: SelfCheckReceipt | None = None
    tool_executed: bool | None = None
    execution_timeout_s: float | None = None
    verification: tuple[VerificationReceipt, ...] = ()
    goal_observations: tuple[GoalObservation, ...] = ()

    @model_validator(mode="after")
    def validate_self_check(self):
        receipt = self.self_check
        if receipt is not None and (self.action.kind != ActionKind.VERIFY
                or receipt.command != self.action.command
                or receipt.result != self.result
                or (receipt.workspace_unchanged and receipt.snapshot_hash != self.before.fingerprint)):
            raise ValueError("self-check receipt does not match its executed attempt")
        for item in self.verification:
            if item.spec not in self.action.checks or (item.workspace_unchanged
                    and item.snapshot_hash != self.before.fingerprint):
                raise ValueError("verification does not match executed action/state")
        return self

    @property
    def consequence_signature(self) -> str:
        if self.verification:
            data = [dict(status=r.status, assertions=r.assertions, result=r.result.model_dump(
                exclude={'duration_s', 'execution_id', 'transport_recovered'}, mode='json'),
                controls=[dict(status=c.status, assertions=c.assertions, result=c.result.model_dump(
                    exclude={'duration_s', 'execution_id', 'transport_recovered'}, mode='json')) for c in r.controls])
                for r in self.verification]
        else:
            data = self.result.model_dump(exclude={'duration_s', 'execution_id', 'transport_recovered'}, mode='json')
        return digest(canonical(data))


class ExplorationNote(Message):
    step: int
    state_version: str
    notes: PolicyNotes
    event_hash: str = ""


class ExplorationRecord(Message):
    step: int
    event_hash: str = ""
    action_signature: str
    execution_signature: str
    before_hash: str
    after_hash: str
    paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    output_hash: str
    consequence_hash: str
    exit_code: int | None
    error_type: str | None
    outcome_unknown: bool
    tool_executed: bool | None
    method: str = ""
    problem_refs: tuple[str, ...] = ()
    intent: ExplorationIntent | None = None
    check_ids: tuple[str, ...] = ()
    duration_s: float
    execution_timeout_s: float | None = None


def exploration_record(attempt: Attempt, note: ExplorationNote | None = None,
                       event_hash: str = "") -> ExplorationRecord:
    intent = note.notes.exploration if note is not None else None
    return ExplorationRecord(step=attempt.step, event_hash=event_hash,
        action_signature=attempt.action.signature, execution_signature=attempt.action.execution_signature,
        before_hash=attempt.before.fingerprint, after_hash=attempt.after.fingerprint,
        paths=tuple(dict.fromkeys((*(intent.paths if intent else ()),
                                   *((attempt.action.path,) if attempt.action.path else ()),
                                   *(item.spec.path for item in attempt.verification if item.spec.path),
                                   *attempt.delta.changed_files))), changed_paths=attempt.delta.changed_files,
        output_hash=digest(attempt.result.output), consequence_hash=attempt.consequence_signature, exit_code=attempt.result.exit_code,
        error_type=attempt.result.error_type, outcome_unknown=attempt.result.outcome_unknown,
        tool_executed=attempt.tool_executed, method=intent.method if intent else "",
        problem_refs=tuple(dict.fromkeys((*(intent.problem_refs if intent else ()),
            *(ref for item in attempt.verification for ref in item.spec.target_refs)))), intent=intent,
        check_ids=tuple(item.spec.id for item in attempt.verification),
        duration_s=attempt.result.duration_s, execution_timeout_s=attempt.execution_timeout_s)


def current_self_checks(attempts: tuple[Attempt, ...], snapshot: StateSnapshot) -> tuple[SelfCheckReceipt, ...]:
    """Latest result per command on this exact state, including failed results."""
    latest = {}
    for attempt in attempts:
        receipt = attempt.self_check
        if receipt is not None and receipt.snapshot_hash == snapshot.fingerprint:
            latest[receipt.command_hash] = receipt
    return tuple(latest.values())


def self_check_ready(attempts: tuple[Attempt, ...], snapshot: StateSnapshot) -> bool:
    checks = current_self_checks(attempts, snapshot)
    return bool(checks) and all(check.passed for check in checks)


class SubmissionReadiness(Message):
    """Derived gate eligibility, not a claim that the task has been solved."""
    phase: Literal["work", "self_test_required", "ready_to_submit"]
    snapshot_hash: str


def submission_readiness(task: TaskContract, attempts: tuple[Attempt, ...],
                         snapshot: StateSnapshot) -> SubmissionReadiness:
    if not task.require_self_check or self_check_ready(attempts, snapshot):
        phase = "ready_to_submit"
    elif any(attempt.delta.changed_files or attempt.self_check for attempt in attempts):
        phase = "self_test_required"
    else:
        phase = "work"
    return SubmissionReadiness(phase=phase, snapshot_hash=snapshot.fingerprint)


class GraphView(Message):
    run_id: str = ""
    snapshot: StateSnapshot | None = None
    verdict: Verdict = Field(default_factory=Verdict)
    attempts: tuple[Attempt, ...] = ()
    feedback: str = ""
    observations: tuple[StateSnapshot, ...] = ()
    evidence_history: tuple[Evidence, ...] = ()
    notes: PolicyNotes | None = None
    notes_step: int | None = None
    notes_state_version: str = ""
    notes_history: tuple[ExplorationNote, ...] = ()
    attempt_event_hashes: dict[int, str] = Field(default_factory=dict)


class Checkpoint(Message):
    step: int
    elapsed_s: float
    no_progress: int
    feedback: str
    snapshot: StateSnapshot
    verdict: Verdict


class Termination(str, Enum):
    SUCCESS = "success"
    SUBMITTED = "submitted"
    INCOMPLETE = "incomplete"
    NO_PROGRESS = "no_progress"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_ERROR = "model_error"
    HARNESS_ERROR = "harness_error"
    INTERRUPTED = "interrupted"
    RECOVERY_BLOCKED = "recovery_blocked"


class RunResult(Message):
    run_id: str
    termination: Termination
    reason: str
    steps: int
    elapsed_s: float
    usage: Usage
    snapshot: StateSnapshot | None = None
    verdict: Verdict = Field(default_factory=Verdict)
    checkpoint: Checkpoint | None = None
    pending_steps: tuple[int, ...] = ()


class RunManifest(Message):
    run_id: str
    schema_version: int = SCHEMA_VERSION
    contract: TaskContract
    workspace: str
    backend: str
    environment_json: str
    policy_json: str
    code_hash: str
    runtime_json: str = "{}"


class RunEvent(Message):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    sequence: int
    kind: EventKind
    timestamp: str
    data_json: str
    previous_hash: str
    event_hash: str


class StepOutcome(Message):
    decision: Decision
    snapshot: StateSnapshot
    verdict: Verdict
    attempt: Attempt | None = None
    feedback: str = ""

