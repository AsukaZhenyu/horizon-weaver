"""Compare native grades, costs and factual repetitions using one metric definition."""
from __future__ import annotations
import argparse
import importlib.util
import inspect
import json
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.v1_terminal_bench_report import collect
from horizon_weaver.app import read_run
from horizon_weaver.core import action_schema, canonical, digest
from horizon_weaver.event_store import EventStore


def repetition_metrics(attempts):
    proposals, outcomes = set(), set()
    rows = []
    for attempt in attempts:
        signature = (attempt.action.execution_signature, attempt.before.fingerprint, attempt.execution_timeout_s)
        outcome = (*signature, attempt.after.fingerprint, attempt.result.exit_code,
                   attempt.result.error_type, attempt.result.outcome_unknown, digest(attempt.result.output))
        repeat = signature in proposals
        same_outcome = outcome in outcomes
        failed = bool(attempt.result.error_type or attempt.result.exit_code not in (None, 0))
        rows.append(dict(step=attempt.step, kind=attempt.action.kind.value, repeated_proposal=repeat,
            same_outcome_repeat=same_outcome, repeated_failure=same_outcome and failed,
            runtime_repeated=attempt.progress.repeated, executed=attempt.tool_executed,
            zero_change_write=attempt.action.kind.value == 'write' and not attempt.delta.changed_files,
            signature=attempt.action.execution_signature, input_state=attempt.before.fingerprint))
        proposals.add(signature)
        outcomes.add(outcome)
    keys = ('repeated_proposal', 'same_outcome_repeat', 'repeated_failure', 'runtime_repeated', 'zero_change_write')
    return dict(totals={key: sum(row[key] for row in rows) for key in keys}, rows=rows,
        scope='Same execution signature, observed file state and wait budget; process/network equality and semantic waste are not established.')


def cohort(root):
    report = collect(root)
    archived_core = root.parent / 'code/horizon_weaver/core.py'
    schema_builder = action_schema
    if archived_core.is_file():
        spec = importlib.util.spec_from_file_location('_metrics_archived_core', archived_core)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        schema_builder = module.action_schema
    for row in report['trials']:
        graph = read_run(Path(row['runtime_result']).parent)
        repetitions = repetition_metrics(graph.attempts)
        row['repetitions'] = repetitions
        with closing(EventStore(Path(row['runtime_result']).parent / 'events.sqlite3', graph.manifest.run_id, readonly=True)) as store:
            events = store.read()
        calls = [json.loads(e.data_json) for e in events if e.kind == 'ModelCallCompleted']
        started = [json.loads(e.data_json) for e in events if e.kind == 'ModelCallStarted']
        # Source requests bind capabilities, saved IDs and the decision update.
        for call in started:
            request = call['request']
            options = dict(exploration_history=request.get('exploration_history'),
                verification_policy=request.get('verification_policy'),
                known_check_ids=tuple(request['known_check_ids']) if request.get('known_check_ids') is not None else None)
            if 'reflection_required' in inspect.signature(schema_builder).parameters:
                options['reflection_required'] = request.get('reflection_required', False)
            if 'goal_review_refs' in inspect.signature(schema_builder).parameters:
                options['goal_review_refs'] = tuple(request.get('goal_review_refs', ()))
            actual = schema_builder(**options)
            assert call['decoder_schema_hash'] == digest(canonical(actual)), 'decoder provenance mismatch'
        row.update(model_calls=len(calls), reflection_requested=sum(c['request'].get('reflection_required', False) for c in started),
            understanding_updates=sum(bool((c['decision'].get('notes') or {}).get('understanding')) for c in calls),
            cited_understanding_updates=sum(bool(((c['decision'].get('notes') or {}).get('understanding') or {}).get('evidence_steps')) for c in calls),
            requirement_notes=sum(len((c['decision'].get('notes') or {}).get('requirements', [])) for c in calls),
            blocked_submissions=sum(a.action.kind.value == 'done' and a.result.error_type == 'verification_required' for a in graph.attempts),
            protocol_errors=sum(any(s in (c['decision'].get('error') or '') for s in ('Return one complete JSON action', 'Output was truncated.')) for c in calls),
            model_timeouts=sum('Model decision timed out' in (c['decision'].get('error') or '') for c in calls))
    rows = report['trials']
    report['totals'] = dict(tasks=len(rows), resolved=sum(r['resolved'] for r in rows),
        grading_valid=sum(r['grading_valid'] for r in rows), passed_checks=sum(r['passed_checks'] for r in rows),
        total_checks=sum(r['total_checks'] for r in rows), elapsed_s=sum(r.get('elapsed_s', 0) for r in rows),
        normal_submission_and_resolved=sum(r['resolved'] and r.get('runtime_termination') == 'submitted' for r in rows),
        **{key: sum(r.get(key, 0) for r in rows) for key in ('model_calls', 'reflection_requested', 'understanding_updates',
            'cited_understanding_updates', 'requirement_notes', 'blocked_submissions', 'protocol_errors', 'model_timeouts')},
        repetitions={key: sum(r['repetitions']['totals'][key] for r in rows) for key in (
            'repeated_proposal', 'same_outcome_repeat', 'repeated_failure', 'runtime_repeated', 'zero_change_write')})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = dict(before=cohort(args.before), after=cohort(args.after),
        limitation='One attempt per task at two different code versions; descriptive development comparison, not a causal estimate.')
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: result[key]['totals'] for key in ('before', 'after')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
