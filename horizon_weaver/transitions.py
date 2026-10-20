"""Conservative execution-time feedback for redundant file actions."""
from __future__ import annotations

from .core import Action, ActionKind, DecisionInput, GraphView, StateSnapshot, ToolResult, digest


class TransitionGuard:
    def assess(self, action: Action, request: DecisionInput, view: GraphView,
               snapshot: StateSnapshot) -> ToolResult | None:
        if (request.state_version and request.state_version != snapshot.fingerprint
                and action.kind not in (ActionKind.READ, ActionKind.HISTORY, ActionKind.INCOMPLETE)):
            return ToolResult(exit_code=1, error_type="state_changed_during_decision", output=(
                "The workspace changed after this decision's context was constructed. "
                "This action was not executed. Inspect the new state and decide again; "
                "earlier observations and checks may be stale."))
        file = snapshot.file(action.path) if action.path else None
        if file is None or file.kind != "file":
            return None
        if action.kind == ActionKind.READ:
            for attempt in reversed(view.attempts):
                if (attempt.step in request.visible_write_steps and attempt.action.kind == ActionKind.WRITE
                        and attempt.action.path == action.path and attempt.result.exit_code == 0
                        and not attempt.result.error_type and not attempt.result.outcome_unknown
                        and attempt.after.file(action.path) == file
                        and digest(attempt.action.content or "") == file.sha256
                        and len((attempt.action.content or "").encode("utf-8")) == file.size):
                    return ToolResult(exit_code=1, error_type="redundant_action", output=(
                        f"The complete current source of {action.path} is visible in context from recorded step {attempt.step}. "
                        "Use that content or choose a command to investigate actual behavior."))
                if (attempt.step in request.visible_observation_steps
                        and attempt.action.kind == ActionKind.READ and attempt.action.path == action.path
                        and attempt.result.exit_code == 0 and not attempt.result.error_type
                        and not attempt.result.outcome_unknown
                        and attempt.before.file(action.path) == file and attempt.after.file(action.path) == file
                        and digest(attempt.result.output) == file.sha256
                        and len(attempt.result.output.encode("utf-8")) == file.size):
                    return ToolResult(exit_code=1, error_type="redundant_action", output=(
                        f"The complete current version of {action.path} was supplied in context from recorded step {attempt.step}. "
                        "Use that observation. Choose a diagnostic command or behavioral verify, "
                        "or modify the demonstrated failing behavior before reading this version again."))
        elif action.kind == ActionKind.WRITE and digest(action.content or "") == file.sha256:
            for attempt in reversed(view.attempts):
                if (attempt.action.signature == action.signature and attempt.result.exit_code == 0
                        and not attempt.result.error_type and not attempt.result.outcome_unknown
                        and attempt.after.file(action.path) == file):
                    return ToolResult(exit_code=1, error_type="redundant_action", output=(
                        f"ATTEMPT {attempt.step} already wrote the same current content to {action.path}. "
                        "Proposed content is identical to the current file. Actual content changes: 0; no write executed. "
                        "Choose a behavioral verify or a diagnostic command; change content only to address "
                        "a specific failure. If submission gates are satisfied, use done."))
        return None
