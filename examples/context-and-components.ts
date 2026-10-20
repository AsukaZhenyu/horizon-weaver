import { createHost } from '../runtime/host.js';
import { reference } from '../runtime/protocol.js';
import { compare, evaluate } from '../experiments/evaluate.js';

const mode = process.argv.includes('--trusted-process') ? 'trusted-process' : 'docker';
const host = await createHost({ root: '.horizon/demo', worker: { mode } });
try {
  const seed = host.kernel.define('def main(task, api):\n    return 0\n');
  const builder = host.kernel.define(`def main(task, api):
    source = api.request('values.put', value='def main(task, api):\\n    return sum(task["numbers"])\\n')
    component = api.request('executables.install', definition={
        'abi': 'horizon/python-v0', 'source': source, 'bindings': {}, 'grants': []})
    return component
`, { grants: ['install'] });
  const built = await host.kernel.run(builder, {});
  if (built.status !== 'submitted') throw new Error(built.error);
  const generated = reference(built.output);
  const selection = await compare(host.kernel, [seed, generated], [
    { id: 'development', input: { numbers: [2, 3] }, expected: 5 },
  ]);
  if (!selection.selected) throw new Error('No candidate selected');
  const transfer = await evaluate(host.kernel, selection.selected, [
    { id: 'new-input', input: { numbers: [7, -2, 4] }, expected: 9 },
  ]);
  console.log(JSON.stringify({
    scope: 'Deterministic engineering fixture; the candidate builder is authored, not learned',
    mode, builder: built.id, candidates: selection.candidates.map(({ entry, passed, total }) => ({ entry, passed, total })),
    selected: selection.selected, transfer: { passed: transfer.passed, total: transfer.total },
  }, null, 2));
  if (transfer.passed !== transfer.total) process.exitCode = 1;
} finally { await host.dispose(); }
