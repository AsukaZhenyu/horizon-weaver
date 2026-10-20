import { canonical } from '../runtime/values.js';
import type { Weaver } from '../runtime/host.js';
import type { JsonValue, RunLimits, RunResult, ValueRef } from '../runtime/protocol.js';

export type EvaluationTask = { id: string; input: JsonValue; expected: JsonValue };
export type EvaluationTrial = { task: string; execution: RunResult; passed: boolean };
export type Evaluation = { entry: ValueRef; passed: number; total: number; trials: EvaluationTrial[] };
export type Comparison = { selected: ValueRef | null; candidates: Evaluation[] };

// Evaluation runs outside the component. Only task.input enters its worker.
// Equality is a fixture evaluator, not a general-purpose task verifier.
export async function evaluate(kernel: Weaver, entry: ValueRef, tasks: EvaluationTask[], limits?: Partial<RunLimits>): Promise<Evaluation> {
  const trials: EvaluationTrial[] = [];
  for (const task of tasks) {
    const execution = await kernel.run(entry, task.input, limits);
    trials.push({ task: task.id, execution, passed: execution.status === 'submitted' && canonical(execution.output) === canonical(task.expected) });
  }
  return { entry, passed: trials.filter(trial => trial.passed).length, total: trials.length, trials };
}

// The caller supplies candidates. This compares actual execution; it is not yet
// a learned proposer or a claim that fixture accuracy transfers to real tasks.
export async function compare(kernel: Weaver, entries: ValueRef[], tasks: EvaluationTask[], limits?: Partial<RunLimits>): Promise<Comparison> {
  const candidates: Evaluation[] = [];
  for (const entry of entries) candidates.push(await evaluate(kernel, entry, tasks, limits));
  candidates.sort((a, b) => b.passed - a.passed ||
    a.trials.reduce((n, trial) => n + trial.execution.operations, 0) - b.trials.reduce((n, trial) => n + trial.execution.operations, 0));
  return { selected: candidates[0]?.entry ?? null, candidates };
}
