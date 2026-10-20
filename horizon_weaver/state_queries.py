"""Read-only relation queries and deterministic context working-set selection."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .core import ActionKind, Attempt, GraphView, HistoryQuery, StateSnapshot, TaskContract, canonical, digest
from .state_ir import ArtifactNode, EvidenceNode, StateIR, Transition, build_state_ir
from .verification import verification_state, goal_assessment


@dataclass(frozen=True)
class ContextCandidate:
    attempt: Attempt
    reason: str
    priority: int
    current_read: bool = False
    current_write: bool = False


class StateQuery:
    def __init__(self, ir: StateIR):
        self.ir = ir
        self.states = {node.id: node for node in ir.states}
        self.snapshot = self.states[ir.current_state].snapshot
        self._outgoing: dict[str, list[Transition]] = {}
        self._incoming: dict[str, list[Transition]] = {}
        for transition in ir.transitions:
            self._outgoing.setdefault(transition.before_id, []).append(transition)
            self._incoming.setdefault(transition.after_id, []).append(transition)

    @classmethod
    def from_view(cls, task: TaskContract, view: GraphView, snapshot: StateSnapshot):
        return cls(build_state_ir(task, view, snapshot))

    def parents(self, identity: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t.before_id for t in self._incoming.get(identity, ())))

    def children(self, identity: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t.after_id for t in self._outgoing.get(identity, ())))

    def path_to(self, identity: str) -> tuple[Transition, ...] | None:
        """Shortest recorded path. It is not permission to replay its tools."""
        if identity not in self.states:
            return None
        queue = deque([(self.ir.initial_state, ())])
        visited = {self.ir.initial_state}
        while queue:
            current, path = queue.popleft()
            if current == identity:
                return path
            for transition in self._outgoing.get(current, ()):
                if transition.after_id not in visited:
                    visited.add(transition.after_id)
                    queue.append((transition.after_id, (*path, transition)))
        return None

    def file_versions(self, path: str) -> tuple[ArtifactNode, ...]:
        return tuple(node for node in self.ir.artifacts if node.file.path == path)

    def touching(self, path: str) -> tuple[Transition, ...]:
        return tuple(t for t in self.ir.transitions
                     if path in t.attempt.delta.changed_files or t.attempt.action.path == path)

    def current_evidence(self) -> tuple[EvidenceNode, ...]:
        valid = set(self.ir.verdict.evidence)
        return tuple(node for node in self.ir.evidence if node.evidence in valid)

    def stale_evidence(self) -> tuple[EvidenceNode, ...]:
        fingerprint = self.snapshot.fingerprint
        definitions = {node.key: node.definition_hash for node in self.ir.requirements}
        return tuple(node for node in self.ir.evidence
                     if node.evidence.snapshot_hash != fingerprint
                     or definitions.get(node.evidence.check_id) != node.evidence.check_hash)

    def read_is_current(self, attempt: Attempt) -> bool:
        if (attempt.action.kind != ActionKind.READ or attempt.result.exit_code != 0
                or attempt.result.error_type or attempt.result.outcome_unknown):
            return False
        file = self.snapshot.file(attempt.action.path)
        return (file is not None and file.kind == "file"
                and attempt.before.file(file.path) == file and attempt.after.file(file.path) == file
                and digest(attempt.result.output) == file.sha256
                and len(attempt.result.output.encode("utf-8")) == file.size)

    def latest_observation(self, path: str) -> Attempt | None:
        return next((t.attempt for t in reversed(self.ir.transitions)
                     if t.attempt.action.path == path and self.read_is_current(t.attempt)), None)

    def write_is_current(self, attempt: Attempt) -> bool:
        file = self.snapshot.file(attempt.action.path)
        return (attempt.action.kind == ActionKind.WRITE and attempt.result.exit_code == 0
                and not attempt.result.error_type and not attempt.result.outcome_unknown
                and file is not None and file.kind == "file" and attempt.after.file(file.path) == file
                and digest(attempt.action.content or "") == file.sha256
                and len((attempt.action.content or "").encode("utf-8")) == file.size)

    def repeated_failures(self) -> tuple[dict, ...]:
        groups = {}
        for transition in self.ir.transitions:
            attempt = transition.attempt
            if (attempt.before.fingerprint != self.snapshot.fingerprint
                    or attempt.after.fingerprint != self.snapshot.fingerprint
                    or attempt.result.outcome_unknown
                    or not (attempt.result.error_type or attempt.result.exit_code not in (None, 0))):
                continue
            key = (attempt.action.execution_signature, attempt.execution_timeout_s, attempt.result.exit_code,
                   attempt.result.error_type, digest(attempt.result.output))
            groups.setdefault(key, []).append(attempt)
        return tuple(dict(action=items[-1].action.model_dump(exclude={"content", "reasoning"}, mode="json"),
                          count=len(items), steps=[a.step for a in items],
                          file_state=self.snapshot.fingerprint, exit=items[-1].result.exit_code,
                          error=items[-1].result.error_type, output=items[-1].result.output)
                     for items in sorted(groups.values(), key=lambda v: -v[-1].step) if len(items) > 1)

    def exploration_history(self, query: HistoryQuery | None = None) -> tuple[dict, ...]:
        query = query or HistoryQuery()
        attempts = {item.attempt.step: item.attempt for item in self.ir.transitions}
        selected = []
        for record in reversed(self.ir.exploration):
            if attempts[record.step].action.kind == ActionKind.HISTORY and not query.steps:
                continue
            if query.steps and record.step not in query.steps:
                continue
            if query.path and query.path not in record.paths:
                continue
            if query.ref and query.ref not in record.problem_refs:
                continue
            if query.method and query.method != record.method:
                continue
            if query.failures_only and not (record.error_type or record.exit_code not in (None, 0)):
                continue
            attempt = attempts[record.step]
            receipts = [r for r in attempt.verification if not query.check_id or r.spec.id == query.check_id]
            if query.check_id and not receipts:
                continue
            group = [other for other in self.ir.exploration if other.execution_signature == record.execution_signature
                     and other.before_hash == record.before_hash and other.after_hash == record.after_hash
                     and other.consequence_hash == record.consequence_hash and other.exit_code == record.exit_code
                     and other.error_type == record.error_type and other.outcome_unknown == record.outcome_unknown
                     and other.execution_timeout_s == record.execution_timeout_s]
            if any(record.step in item['matching_steps'] for item in selected):
                continue
            data = record.model_dump(mode='json')
            data.update(action=attempt.action.model_dump(exclude={'content', 'reasoning'}, mode='json'),
                output=attempt.result.output[-1600:], output_complete=len(attempt.result.output) <= 1600,
                matching_steps=[item.step for item in group],
                scope='observed workspace files only; process/network freshness unknown',
                input_state_current=record.before_hash == self.snapshot.fingerprint,
                changes_since=sorted({path for item in self.ir.exploration if item.step > record.step
                                      for path in item.changed_paths}))
            if query.check_id:
                # A rerun action contains only check_ids. Return the actual
                # measured definition from its immutable receipt, not the
                # latest predicate with a coincidentally equal name.
                data['verification'] = [dict(spec=r.spec.model_dump(exclude_defaults=True, mode='json'),
                    spec_hash=r.spec_hash, status=r.status, output=r.result.output,
                    snapshot_hash=r.snapshot_hash, environment_hash=r.environment_hash,
                    workspace_unchanged=r.workspace_unchanged) for r in receipts]
            selected.append(data)
            if len(selected) >= query.limit:
                break
        return tuple(selected)

    def exploration_working_set(self, limit: int = 5, environment_hash: str | None = None) -> tuple[dict, ...]:
        # Older unresolved failures survive the recent-window cutoff. Exact
        # grouping is factual; method labels never authorize tool suppression.
        state = verification_state(tuple(t.attempt for t in self.ir.transitions), self.snapshot, environment_hash)
        active_ids = {item['id'] for item in state['blockers']}
        failures = tuple(item for item in self.exploration_history(HistoryQuery(failures_only=True, limit=12))
                         if not item['check_ids'] or set(item['check_ids']) & active_ids)
        recent = self.exploration_history(HistoryQuery(limit=limit))
        steps = set()
        result = []
        for item in (*failures, *recent):
            if item['step'] not in steps:
                result.append(item)
                steps.add(item['step'])
            if len(result) == limit:
                break
        return tuple(result)

    def decision_obstacle(self, environment_hash: str | None = None) -> dict | None:
        attempts = tuple(t.attempt for t in self.ir.transitions)
        if not attempts:
            return None
        latest = attempts[-1]
        state = verification_state(attempts, self.snapshot, environment_hash)
        failed = bool(latest.result.error_type or latest.result.exit_code not in (None, 0))
        assessment = self.goal_assessment(environment_hash)
        blocked_check = latest.action.kind == ActionKind.VERIFY and (
            any(r['step'] == latest.step and r['current'] for r in assessment['check_issues'])
            or any(r['status'] == 'contradicted' for r in assessment['submission']['hard_blockers']))
        if not (failed or latest.progress.repeated or blocked_check):
            return None
        sources = [a.step for a in attempts if a.tool_executed is not False][-3:]
        return dict(step=latest.step, failed=failed, repeated=latest.progress.repeated,
                    unchanged_files=not latest.delta.changed_files, evidence_steps=sources,
                    question='What did the observation establish, what remains uncertain, and what changes in the next try?')

    def goal_working_set(self, state: dict, limit: int = 6) -> dict:
        """Retain cited obligations; their selection and coverage are Policy claims."""
        from .core import requirement_sources
        sources = requirement_sources(self.ir.contract)
        notes = {}
        for entry in self.ir.notes_history:
            for note in entry.notes.requirements:
                key = note.ref or note.quote
                notes[key] = (entry, note)
        rows = []
        for entry, note in list(notes.values())[-limit:]:
            rows.append(dict(ref=note.ref, quote=note.quote or sources.get(note.ref, ''),
                uncertainty=note.uncertainty, step=entry.step,
                cited_steps=list(dict.fromkeys((*note.evidence_steps, *note.check_steps))),
                interpretation_state_current=entry.state_version == self.snapshot.fingerprint,
                current_checks=state['goal_evidence'].get(note.ref, [])))
        covered_refs = set(state['goal_evidence'])
        unmapped = [ref for ref in sources if ref not in covered_refs]
        return dict(obligations=rows, obligations_omitted=max(0, len(notes) - len(rows)),
            source_lines_without_current_check=unmapped[:8], unmapped_source_lines=len(unmapped),
            scope='Public text locations are not a semantic checklist. Policy must review the full goal, deliverables and constraints; cited checks do not certify full coverage.')

    def goal_assessment(self, environment_hash: str | None = None) -> dict:
        return goal_assessment(self.ir.contract, tuple(t.attempt for t in self.ir.transitions),
            self.snapshot, environment_hash, notes_history=self.ir.notes_history, verdict=self.ir.verdict)

    def reasoning_working_set(self, environment_hash: str | None = None, limit: int = 4) -> dict:
        """Join purposes, measured consequences and revisable interpretations.

        This is a pure evidence projection. A nonzero exit proves that execution
        failed, not why it failed or which public requirement was violated.
        """
        attempts = tuple(t.attempt for t in self.ir.transitions)
        by_step = {a.step: a for a in attempts}
        notes = {n.step: n for n in self.ir.notes_history}
        state = verification_state(attempts, self.snapshot, environment_hash)
        understanding_notes = [n for n in self.ir.notes_history if n.notes.understanding is not None]
        latest = understanding_notes[-1] if understanding_notes else None
        claim = None
        if latest is not None:
            understanding = latest.notes.understanding
            cited = [by_step[s] for s in understanding.evidence_steps if s in by_step]
            claim = dict(source='Policy interpretation; not acceptance evidence', step=latest.step,
                evidence_basis='cited observations; interpretation unconfirmed' if cited else 'uncited Policy hypothesis',
                based_on_state=latest.state_version,
                file_state_matches=latest.state_version == self.snapshot.fingerprint,
                process_network_freshness='unknown',
                changes_since=sorted({p for a in attempts if a.step >= latest.step for p in a.delta.changed_files}),
                cited_evidence=[dict(step=a.step, file_state_matches=a.after == self.snapshot,
                    outcome_unknown=a.result.outcome_unknown) for a in cited],
                **understanding.model_dump(exclude_defaults=True, mode='json'))
        artifacts = []
        for path in self.ir.contract.expected_artifacts[:8]:
            file = self.snapshot.file(path)
            artifacts.append(dict(path=path, present=file is not None,
                size=file.size if file else None, correctness='unknown'))
        overview = dict(candidate_artifacts=artifacts,
            measured_checks=state['current'][-4:], unresolved_checks=state['blockers'][:4],
            unresolved_checks_total=len(state['blockers']), predicate_ready=state['ready'],
            predicate_phase='measured_predicates_passed' if state['ready'] else state['phase'],
            next_verification_action=state['suggested_action'],
            public_requirement_coverage='unknown; passing self-checks only establish their predicates',
            external_acceptance='pending' if self.ir.contract.deferred_verification else self.ir.verdict.passed)
        overview['goal_review'] = self.goal_working_set(state)
        assessment = self.goal_assessment(environment_hash)
        overview['submission_allowed'] = assessment['submission']['allowed']
        overview['goal_statuses'] = [dict(ref=r['ref'], status=r['status']) for r in assessment['requirements'][:6]]
        overview['public_goal_conditions'] = [dict(ref=r['ref'], kind=r['kind'], status=r['status'],
            fact=r['fact'], gap=r['gap']) for r in assessment['objectives'] if r['status'] != 'supported'][:4]

        # Keep the cause of an old live blocker, then spend the remaining space
        # on distinct recent consequences. Repeated matching facts are one row.
        priorities = [attempts[-1].step] if attempts else []
        priorities.extend(b['step'] for b in state['blockers'])
        recent = self.exploration_history(HistoryQuery(limit=12))
        priorities.extend(r['step'] for r in recent)
        selected = []
        seen = set()
        for number in priorities:
            attempt = by_step.get(number)
            if attempt is None or attempt.action.kind in (ActionKind.HISTORY, ActionKind.INCOMPLETE):
                continue
            matching = next((r['matching_steps'] for r in recent if number in r['matching_steps']), [number])
            if any(s in seen for s in matching):
                continue
            seen.update(matching)
            selected.append((attempt, matching))
            if len(selected) == limit:
                break
        chain = []
        for attempt, matching in sorted(selected, key=lambda item: item[0].step):
            note = notes.get(attempt.step)
            intent = note.notes.exploration if note else None
            purpose = dict(source='Policy hypothesis; correctness unconfirmed',
                           reason=attempt.action.reasoning[:300])
            if intent:
                purpose.update(intent.model_dump(exclude_defaults=True, mode='json'))
            result = attempt.result
            predicates = [dict(id=r.spec.id, kind=r.spec.kind, status=r.status,
                assertions=r.assertions, source_refs=r.spec.target_refs,
                expectation_authority='public contract' if r.oracle_validated else 'Policy check definition')
                for r in attempt.verification]
            if result.outcome_unknown:
                assessment = 'execution outcome unknown; no failure cause established'
            elif any(r.status == 'fail' for r in attempt.verification):
                assessment = 'measured predicate failed; candidate defect versus wrong expectation unresolved'
            elif any(r.status == 'inconclusive' for r in attempt.verification):
                assessment = 'check inconclusive; no correctness conclusion'
            elif result.error_type == 'verification_required':
                assessment = 'submission blocked by self-check protocol; not an external task verdict'
            elif attempt.progress.repeated or attempt.progress.level == 'none':
                assessment = 'no new progress credited; repeated output does not establish correctness'
            elif result.error_type or result.exit_code not in (None, 0):
                assessment = 'execution failed; underlying cause unknown unless established by evidence'
            elif predicates:
                assessment = 'listed predicates passed; remaining requirement coverage unknown'
            else:
                assessment = 'execution completed; task correctness not established'
            reflections = [n for n in understanding_notes
                           if attempt.step in n.notes.understanding.evidence_steps and n.step > attempt.step]
            interpretation = None
            if reflections:
                n = reflections[-1]
                interpretation = dict(source='Policy interpretation', step=n.step,
                    text=n.notes.understanding.interpretation, evidence_steps=n.notes.understanding.evidence_steps)
            output = result.output[-180:]
            chain.append(dict(step=attempt.step, why=purpose,
                observed_action=dict(kind=attempt.action.kind.value, path=attempt.action.path,
                            command=attempt.action.command[:240]),
                consequence=dict(executed=attempt.tool_executed, exit=result.exit_code,
                    error=result.error_type, outcome_unknown=result.outcome_unknown,
                    changed_files=attempt.delta.changed_files,
                    checks=predicates, output_tail=output, output_complete=output == result.output,
                    runtime_progress=attempt.progress.level, progress_reason=attempt.progress.reason_code),
                assessment=assessment, interpretation=interpretation, matching_steps=matching))
        return dict(overview=overview, understanding=claim, exploration_chain=chain,
                    decision_obstacle=self.decision_obstacle(environment_hash),
                    reasoning_boundary='Facts describe observations. Hypotheses and interpretations may be revised; failure causes and coverage can remain unknown.')

    def context_candidates(self, recent_window: int = 4) -> tuple[ContextCandidate, ...]:
        attempts = tuple(t.attempt for t in self.ir.transitions)
        if not attempts:
            return ()
        unsatisfied = set(self.ir.verdict.unsatisfied_requirements)
        focus = {node.path for node in self.ir.requirements if node.path and node.key in unsatisfied}
        focus.update(self.ir.contract.expected_artifacts)
        focus.update(path for attempt in attempts[-recent_window:] for path in attempt.delta.changed_files)
        candidates = []
        seen = set()
        for index in range(len(attempts) - 1, -1, -1):
            attempt = attempts[index]
            current_read = self.read_is_current(attempt)
            current_write = self.write_is_current(attempt)
            # Identical observations on identical file versions are one fact.
            file = attempt.before.file(attempt.action.path) if attempt.action.path else None
            key = (attempt.action.signature, digest(attempt.result.output),
                   attempt.result.exit_code, attempt.result.error_type,
                   file if attempt.action.kind == ActionKind.READ else attempt.before.fingerprint)
            if key in seen:
                continue
            seen.add(key)
            if index == len(attempts) - 1:
                priority, reason = 100, "latest_tool_reply"
            elif (current_read or current_write) and attempt.action.path in focus:
                priority, reason = 90, "current_required_artifact"
            elif (attempt.result.error_type or attempt.result.exit_code not in (None, 0)) \
                    and index >= len(attempts) - recent_window:
                priority, reason = 80, "recent_failure"
            elif index >= len(attempts) - recent_window:
                priority, reason = 50, "recent_attempt"
            else:
                continue
            candidates.append(ContextCandidate(attempt, reason, priority, current_read, current_write))
        return tuple(sorted(candidates, key=lambda c: (-c.priority, -c.attempt.step)))
