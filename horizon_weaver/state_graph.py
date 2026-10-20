"""Deterministic event projection and state queries; replay has no side effects."""
from __future__ import annotations

import json

from .core import (Action, Attempt, Checkpoint, Decision, Evidence, ExplorationNote, GraphView, RunEvent,
                   RunManifest, RunResult, StateSnapshot, Usage, Verdict, canonical)
from .event_store import StoreError
from .state_ir import ControlState, StateIR, build_state_ir
from .state_queries import StateQuery

class StateGraph:
    """Replay only deserializes facts. It never invokes a tool or model."""

    def __init__(self, events: tuple[RunEvent, ...]):
        self.manifest: RunManifest | None = None
        self.snapshot: StateSnapshot | None = None
        self.verdict = Verdict()
        self.attempts: list[Attempt] = []
        self.checkpoint: Checkpoint | None = None
        self.usage = Usage()
        self.result: RunResult | None = None
        self.pending: dict[int, str] = {}
        self.pending_evaluation = False
        self.elapsed_s = 0.0
        model_calls: dict[str, Usage] = {}
        self.feedback = ""
        self.observations: list[StateSnapshot] = []
        self.evidence_history: list[Evidence] = []
        self.notes = None
        self.notes_step = None
        self.notes_state_version = ""
        self.notes_history: list[ExplorationNote] = []
        self.attempt_event_hashes: dict[int, str] = {}
        for event in events:
            try:
                data = json.loads(event.data_json)
                if event.kind == "RunStarted":
                    if self.manifest is not None:
                        raise StoreError("duplicate RunStarted")
                    self.manifest = RunManifest.model_validate(data)
                elif self.manifest is None:
                    raise StoreError("journal must start with RunStarted")
                elif event.kind == "ObservationCaptured":
                    self.snapshot = StateSnapshot.model_validate(data)
                    self.observations.append(self.snapshot)
                elif event.kind == "ModelCallStarted":
                    model_calls[data["call_id"]] = Usage(input_tokens=data["input_token_bound"],
                        output_tokens=data["output_token_bound"], estimated=True)
                elif event.kind == "ModelCallCompleted":
                    if model_calls.pop(data["call_id"], None) is None:
                        raise StoreError("model response has no matching request")
                    decision = Decision.model_validate(data["decision"])
                    self.usage = self.usage.plus(decision.usage)
                    if decision.action is not None and decision.notes is not None:
                        self.notes, self.notes_step = decision.notes, data["step"]
                        self.notes_state_version = data.get("state_version", "")
                        self.notes_history.append(ExplorationNote(step=data['step'],
                            state_version=self.notes_state_version, notes=decision.notes, event_hash=event.event_hash))
                elif event.kind == "ActionProposed":
                    step = data["step"]
                    if step in self.pending:
                        raise StoreError("duplicate pending action")
                    self.pending[step] = canonical(Action.model_validate(data["action"]))
                elif event.kind == "ActionExecuted":
                    attempt = Attempt.model_validate(data)
                    proposed = self.pending.pop(attempt.step, None)
                    if proposed != canonical(attempt.action):
                        raise StoreError("execution has no matching proposed action")
                    self.attempts.append(attempt)
                    self.attempt_event_hashes[attempt.step] = event.event_hash
                    self.snapshot = attempt.after
                elif event.kind == "StepFinished":
                    self.checkpoint = Checkpoint.model_validate(data)
                    self.snapshot = self.checkpoint.snapshot
                    self.verdict = self.checkpoint.verdict
                    self.feedback = self.checkpoint.feedback
                    self.elapsed_s = max(self.elapsed_s, self.checkpoint.elapsed_s)
                    self.pending_evaluation = False
                elif event.kind == "EvaluationStarted":
                    self.pending_evaluation = True
                elif event.kind == "CheckEvaluated":
                    # Also validated independently of the checkpoint containing the verdict.
                    self.evidence_history.append(Evidence.model_validate(data))
                elif event.kind == "RunTerminated":
                    self.result = RunResult.model_validate(data)
                elif event.kind == "RunInterrupted":
                    interrupted = RunResult.model_validate(data)
                    self.elapsed_s = max(self.elapsed_s, interrupted.elapsed_s)
                elif event.kind in ("EnvironmentStarted", "RunResumed"):
                    pass
                else:
                    raise StoreError(f"unknown event kind: {event.kind}")
            except StoreError:
                raise
            except Exception as error:
                raise StoreError(f"invalid {event.kind} event at {event.sequence}: {error}") from error
        for reservation in model_calls.values():
            self.usage = self.usage.plus(reservation)

    @property
    def view(self) -> GraphView:
        return GraphView(run_id=self.manifest.run_id if self.manifest is not None else "",
                         snapshot=self.snapshot, verdict=self.verdict,
                         attempts=tuple(self.attempts), feedback=self.feedback,
                         observations=tuple(self.observations), evidence_history=tuple(self.evidence_history),
                         notes=self.notes, notes_step=self.notes_step, notes_state_version=self.notes_state_version,
                         notes_history=tuple(self.notes_history), attempt_event_hashes=self.attempt_event_hashes)

    @property
    def ir(self) -> StateIR | None:
        if self.manifest is None or self.snapshot is None:
            return None
        checkpoint = self.checkpoint
        return build_state_ir(self.manifest.contract, self.view, self.snapshot,
            ControlState(step=checkpoint.step if checkpoint else 0, elapsed_s=self.elapsed_s,
                         no_progress=checkpoint.no_progress if checkpoint else 0,
                         usage=self.usage, pending_steps=tuple(sorted(self.pending)),
                         pending_evaluation=self.pending_evaluation))

    @property
    def query(self) -> StateQuery:
        ir = self.ir
        if ir is None:
            raise ValueError("run has no observed state to query")
        return StateQuery(ir)

