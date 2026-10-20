"""Versioned state representation, independent of tools, models and persistence."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .core import (ActionKind, Attempt, Evidence, FileState, GraphView, Message,
                   SelfCheckReceipt, StateSnapshot, TaskContract, Usage, Verdict,
                   PolicyNotes, StateDelta, ExplorationNote, ExplorationRecord, exploration_record,
                   canonical, current_verdict, digest)


def state_id(fingerprint: str) -> str:
    return "state:" + fingerprint


def artifact_id(file: FileState) -> str:
    return "artifact:" + digest(canonical(file))


def requirement_id(key: str) -> str:
    return "requirement:" + digest(key)


class StateNode(Message):
    id: str
    snapshot: StateSnapshot | None = None

    @model_validator(mode="after")
    def validate_identity(self):
        if not self.id.startswith("state:") or (self.snapshot is not None
                and self.id != state_id(self.snapshot.fingerprint)):
            raise ValueError("state identity must match its snapshot")
        return self


class ArtifactNode(Message):
    id: str
    file: FileState

    @model_validator(mode="after")
    def validate_identity(self):
        if self.id != artifact_id(self.file):
            raise ValueError("artifact identity must match its file version")
        return self


class RequirementNode(Message):
    id: str
    key: str
    definition_hash: str
    path: str = ""
    external: bool = False

    @model_validator(mode="after")
    def validate_identity(self):
        if self.id != requirement_id(self.key):
            raise ValueError("requirement identity must match its key")
        return self


class EvidenceNode(Message):
    id: str
    evidence: Evidence

    @model_validator(mode="after")
    def validate_identity(self):
        if self.id != "evidence:" + digest(canonical(self.evidence)):
            raise ValueError("evidence identity must match its observation")
        return self


class SelfCheckNode(Message):
    id: str
    receipt: SelfCheckReceipt

    @model_validator(mode="after")
    def validate_identity(self):
        if self.id != "self_check:" + digest(canonical(self.receipt)):
            raise ValueError("self-check identity must match its receipt")
        return self


class Transition(Message):
    id: str
    before_id: str
    after_id: str
    attempt: Attempt

    @model_validator(mode="after")
    def validate_identity(self):
        if (self.id != f"transition:{self.attempt.step}"
                or self.before_id != state_id(self.attempt.before.fingerprint)
                or self.after_id != state_id(self.attempt.after.fingerprint)):
            raise ValueError("transition identity and endpoints must match its attempt")
        return self


class StateEdge(Message):
    kind: Literal["contains", "from_state", "to_state", "changes", "observes",
                  "checks", "on_state", "self_check", "requires"]
    source: str
    target: str


class ControlState(Message):
    step: int = Field(default=0, ge=0)
    elapsed_s: float = Field(default=0, ge=0)
    no_progress: int = Field(default=0, ge=0)
    usage: Usage = Field(default_factory=Usage)
    pending_steps: tuple[int, ...] = ()
    pending_evaluation: bool = False


class StateIR(Message):
    schema_version: Literal["1.1", "1.2", "1.3"] = "1.3"
    run_id: str = ""
    contract: TaskContract
    initial_state: str
    current_state: str
    states: tuple[StateNode, ...]
    artifacts: tuple[ArtifactNode, ...] = ()
    requirements: tuple[RequirementNode, ...] = ()
    evidence: tuple[EvidenceNode, ...] = ()
    self_checks: tuple[SelfCheckNode, ...] = ()
    transitions: tuple[Transition, ...] = ()
    edges: tuple[StateEdge, ...] = ()
    verdict: Verdict = Field(default_factory=Verdict)
    control: ControlState = Field(default_factory=ControlState)
    notes: PolicyNotes | None = None
    notes_step: int | None = None
    notes_state_version: str = ""
    notes_history: tuple[ExplorationNote, ...] = ()
    exploration: tuple[ExplorationRecord, ...] = ()

    @model_validator(mode="after")
    def validate_relations(self):
        notes_by_step = {note.step: note for note in self.notes_history}
        if len(notes_by_step) != len(self.notes_history):
            raise ValueError('duplicate exploration notes step')
        attempts_by_step = {item.attempt.step: item.attempt for item in self.transitions}
        if len({item.step for item in self.exploration}) != len(self.exploration):
            raise ValueError('duplicate exploration record')
        for record in self.exploration:
            attempt = attempts_by_step.get(record.step)
            if attempt is None or record != exploration_record(attempt, notes_by_step.get(record.step), record.event_hash):
                raise ValueError('exploration contradicts recorded attempt')
        if self.schema_version == '1.3' and len(self.exploration) != len(self.transitions):
            raise ValueError('missing exploration records')
        families = (("state", self.states), ("artifact", self.artifacts),
                    ("requirement", self.requirements), ("evidence", self.evidence),
                    ("self_check", self.self_checks), ("transition", self.transitions))
        kinds = {}
        for kind, nodes in families:
            for node in nodes:
                if node.id in kinds:
                    raise ValueError("duplicate state IR node identity")
                kinds[node.id] = kind
        snapshots = {node.id: node.snapshot for node in self.states}
        if snapshots.get(self.current_state) is None or snapshots.get(self.initial_state) is None:
            raise ValueError("current and initial state must have observed snapshots")
        by_id = {node.id: node for _, nodes in families for node in nodes}
        files_by_state = {identity: {file.path: file for file in value.files}
                          for identity, value in snapshots.items() if value is not None}
        artifacts_by_file = {node.file: node.id for node in self.artifacts}
        definitions = {check.id: (digest(canonical(check)), check.path, check.kind == "external")
                       for check in self.contract.acceptance_checks}
        definitions.update({"artifact:" + path: (digest(path), path, False)
                            for path in self.contract.expected_artifacts})
        if {node.key: (node.definition_hash, node.path, node.external)
                for node in self.requirements} != definitions:
            raise ValueError("IR requirements must match the task contract")
        for transition in self.transitions:
            if (snapshots.get(transition.before_id) != transition.attempt.before
                    or snapshots.get(transition.after_id) != transition.attempt.after
                    or transition.attempt.delta != StateDelta.between(transition.attempt.before,
                                                                     transition.attempt.after)):
                raise ValueError("transition snapshots and delta must match its recorded facts")
        allowed = {
            "contains": ({"state"}, {"artifact"}),
            "from_state": ({"state"}, {"transition"}),
            "to_state": ({"transition"}, {"state"}),
            "changes": ({"transition"}, {"artifact"}),
            "observes": ({"transition"}, {"artifact"}),
            "checks": ({"requirement"}, {"evidence"}),
            "on_state": ({"evidence", "self_check"}, {"state"}),
            "self_check": ({"transition"}, {"self_check"}),
            "requires": ({"requirement"}, {"artifact"}),
        }
        for edge in self.edges:
            sources, targets = allowed[edge.kind]
            if kinds.get(edge.source) not in sources or kinds.get(edge.target) not in targets:
                raise ValueError(f"invalid {edge.kind} relation endpoints")
            source, target = by_id[edge.source], by_id[edge.target]
            valid = True
            if edge.kind == "contains":
                valid = files_by_state.get(source.id, {}).get(target.file.path) == target.file
            elif edge.kind == "from_state":
                valid = edge.source == target.before_id
            elif edge.kind == "to_state":
                valid = edge.target == source.after_id
            elif edge.kind == "changes":
                attempt = source.attempt
                valid = (target.file.path in attempt.delta.changed_files
                         and target.file == (attempt.after.file(target.file.path)
                                             or attempt.before.file(target.file.path)))
            elif edge.kind == "observes":
                attempt = source.attempt
                valid = (attempt.action.kind == ActionKind.READ and attempt.result.exit_code == 0
                         and not attempt.result.error_type and not attempt.result.outcome_unknown
                         and attempt.before.file(attempt.action.path) == target.file)
            elif edge.kind == "checks":
                valid = (source.key == target.evidence.check_id
                         and source.definition_hash == target.evidence.check_hash)
            elif edge.kind == "on_state":
                record = source.evidence if isinstance(source, EvidenceNode) else source.receipt
                valid = edge.target == state_id(record.snapshot_hash)
            elif edge.kind == "self_check":
                valid = source.attempt.self_check == target.receipt
            elif edge.kind == "requires":
                valid = snapshots[self.current_state].file(source.path) == target.file
            if not valid:
                raise ValueError(f"{edge.kind} relation contradicts recorded facts")
        edge_keys = {(e.kind, e.source, e.target) for e in self.edges}
        if len(edge_keys) != len(self.edges):
            raise ValueError("duplicate IR relation")
        mandatory = {("contains", node.id, artifacts_by_file.get(file)) for node in self.states
                     if node.snapshot is not None for file in node.snapshot.files}
        mandatory.update(("from_state", t.before_id, t.id) for t in self.transitions)
        mandatory.update(("to_state", t.id, t.after_id) for t in self.transitions)
        if not mandatory <= edge_keys:
            raise ValueError("IR is missing state membership or transition relations")
        # A projection cannot introduce authoritative success or revive stale evidence.
        if current_verdict(self.contract, self.verdict, snapshots[self.current_state]) != self.verdict:
            raise ValueError("IR verdict contains stale or incompatible evidence")
        return self

    @property
    def fingerprint(self) -> str:
        return digest(canonical(self))


def build_state_ir(task: TaskContract, view: GraphView, snapshot: StateSnapshot,
                   control: ControlState | None = None) -> StateIR:
    """Pure projection from recorded facts. No inferred command dependencies."""
    states: dict[str, StateNode] = {}
    artifacts: dict[str, ArtifactNode] = {}
    evidence: dict[str, EvidenceNode] = {}
    self_checks: dict[str, SelfCheckNode] = {}
    transitions: list[Transition] = []
    edges: set[tuple[str, str, str]] = set()
    snapshot_ids: dict[StateSnapshot, str] = {}
    artifact_ids: dict[FileState, str] = {}

    def link(kind, source, target):
        edges.add((kind, source, target))

    def add_snapshot(value):
        if value in snapshot_ids:
            return snapshot_ids[value]
        key = state_id(value.fingerprint)
        snapshot_ids[value] = key
        states[key] = StateNode(id=key, snapshot=value)
        for file in value.files:
            identity = artifact_ids.get(file)
            if identity is None:
                identity = artifact_id(file)
                artifact_ids[file] = identity
                artifacts[identity] = ArtifactNode(id=identity, file=file)
            link("contains", key, identity)
        return key

    def add_unobserved(fingerprint):
        key = state_id(fingerprint)
        if key not in states:
            states[key] = StateNode(id=key)
        return key

    for value in view.observations:
        add_snapshot(value)
    if view.snapshot is not None:
        add_snapshot(view.snapshot)
    for attempt in view.attempts:
        before, after = add_snapshot(attempt.before), add_snapshot(attempt.after)
        transition = Transition(id=f"transition:{attempt.step}", before_id=before,
                                after_id=after, attempt=attempt)
        transitions.append(transition)
        link("from_state", before, transition.id)
        link("to_state", transition.id, after)
        for path in attempt.delta.changed_files:
            # Deletions reference the previous artifact version.
            file = attempt.after.file(path) or attempt.before.file(path)
            if file is not None:
                link("changes", transition.id, artifact_id(file))
        if (attempt.action.kind == ActionKind.READ and attempt.result.exit_code == 0
                and not attempt.result.error_type and not attempt.result.outcome_unknown):
            file = attempt.before.file(attempt.action.path)
            if file is not None:
                link("observes", transition.id, artifact_id(file))
        if attempt.self_check is not None:
            receipt = attempt.self_check
            key = "self_check:" + digest(canonical(receipt))
            self_checks[key] = SelfCheckNode(id=key, receipt=receipt)
            link("self_check", transition.id, key)
            link("on_state", key, add_unobserved(receipt.snapshot_hash))
    current = add_snapshot(snapshot)
    first = (view.observations[0] if view.observations else
             view.attempts[0].before if view.attempts else view.snapshot or snapshot)
    initial = add_snapshot(first)
    requirements = [RequirementNode(id=requirement_id(check.id), key=check.id,
                    definition_hash=digest(canonical(check)), path=check.path,
                    external=check.kind == "external") for check in task.acceptance_checks]
    requirements.extend(RequirementNode(id=requirement_id("artifact:" + path),
                        key="artifact:" + path, definition_hash=digest(path), path=path)
                        for path in dict.fromkeys(task.expected_artifacts))
    requirements_by_key = {item.key: item for item in requirements}
    for item in (*view.evidence_history, *view.verdict.evidence):
        key = "evidence:" + digest(canonical(item))
        evidence[key] = EvidenceNode(id=key, evidence=item)
        link("on_state", key, add_unobserved(item.snapshot_hash))
        requirement = requirements_by_key.get(item.check_id)
        if requirement is not None and requirement.definition_hash == item.check_hash:
            link("checks", requirement.id, key)
    for requirement in requirements:
        file = snapshot.file(requirement.path) if requirement.path else None
        if file is not None:
            link("requires", requirement.id, artifact_id(file))
    return StateIR(contract=task, run_id=view.run_id, initial_state=initial, current_state=current,
        states=tuple(states[key] for key in sorted(states)),
        artifacts=tuple(artifacts[key] for key in sorted(artifacts)),
        requirements=tuple(sorted(requirements, key=lambda item: item.id)),
        evidence=tuple(evidence[key] for key in sorted(evidence)),
        self_checks=tuple(self_checks[key] for key in sorted(self_checks)),
        transitions=tuple(transitions),
        edges=tuple(StateEdge(kind=kind, source=source, target=target)
                    for kind, source, target in sorted(edges)),
        verdict=current_verdict(task, view.verdict, snapshot),
        control=control or ControlState(step=max((a.step for a in view.attempts), default=0)),
        notes=view.notes, notes_step=view.notes_step, notes_state_version=view.notes_state_version,
        notes_history=view.notes_history,
        exploration=tuple(exploration_record(attempt,
            next((note for note in view.notes_history if note.step == attempt.step), None),
            view.attempt_event_hashes.get(attempt.step, '')) for attempt in view.attempts))
