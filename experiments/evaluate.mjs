import { canonical } from '../runtime/values.mjs';

// Evaluation runs outside the component. Only task.input enters its worker.
// Equality is a fixture evaluator, not a general-purpose task verifier.
export async function evaluate(kernel, entry, tasks, limits) {
  const trials = [];
  for (const task of tasks) {
    const execution = await kernel.run(entry, task.input, limits);
    trials.push({ task: task.id, execution, passed: execution.status === 'submitted' && canonical(execution.output) === canonical(task.expected) });
  }
  return { entry, passed: trials.filter(trial => trial.passed).length, total: trials.length, trials };
}

// The caller supplies candidates. This compares actual execution; it is not yet
// a learned proposer or a claim that fixture accuracy transfers to real tasks.
export async function compare(kernel, entries, tasks, limits) {
  const candidates = [];
  for (const entry of entries) candidates.push(await evaluate(kernel, entry, tasks, limits));
  candidates.sort((a, b) => b.passed - a.passed ||
    a.trials.reduce((n, trial) => n + trial.execution.operations, 0) - b.trials.reduce((n, trial) => n + trial.execution.operations, 0));
  return { selected: candidates[0]?.entry ?? null, candidates };
}
