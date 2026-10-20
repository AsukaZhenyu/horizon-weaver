import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHost } from '../runtime/host.mjs';
import { canonical, Values } from '../runtime/values.mjs';
import { compare, evaluate } from '../experiments/evaluate.mjs';

async function fixture(t, options = {}) {
  const root = mkdtempSync(join(tmpdir(), 'horizon-test-'));
  const host = await createHost({ root, worker: { mode: 'trusted-process' }, ...options });
  t.after(async () => { await host.dispose(); rmSync(root, { recursive: true, force: true }); });
  return { ...host, root };
}

test('immutable references preserve provenance and detect tampering', t => {
  const root = mkdtempSync(join(tmpdir(), 'horizon-values-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const values = new Values(root);
  const a = values.put({ b: 2, a: 1 }, { producer: 'first' });
  assert.equal(a, values.put({ a: 1, b: 2 }, { producer: 'first' }));
  assert.notEqual(a, values.put({ a: 1, b: 2 }, { producer: 'second' }));
  writeFileSync(values.path(a), '{}');
  assert.throws(() => values.get(a), /integrity/);
  assert.throws(() => values.path('../secret'), /Invalid/);
  assert.throws(() => canonical({ unknown: undefined }), /finite JSON/);
});

test('actual Cordis lifecycle removes the registered service', async t => {
  const host = await fixture(t);
  assert.ok(host.context.weaver);
  await host.dispose();
  assert.equal(host.context.weaver, undefined);
  assert.throws(() => host.kernel.define('def main(task, api): return 0'), /disposed/);
});

test('unloading the Cordis service cancels an active component', async t => {
  const host = await fixture(t);
  const entry = host.kernel.define('def main(task, api):\n    while True: pass\n');
  const pending = host.kernel.run(entry, {});
  await new Promise(resolve => setTimeout(resolve, 100));
  await host.dispose();
  const run = await pending;
  assert.equal(run.status, 'failed');
  assert.match(run.error, /cancelled/);
  assert.ok(run.elapsedMs < 3000);
});

test('context load, retain, consumption, eviction and search are explicit', async t => {
  const requests = [];
  const { kernel } = await fixture(t, { model: async messages => { requests.push(messages); return { content: 'ok' }; } });
  const entry = kernel.define(`def main(task, api):
    old = api.request('values.put', value='old history')
    current = api.request('values.put', value='current observation')
    v0 = api.request('views.create')
    v1 = api.request('views.load', view=v0, key='old', ref=old)
    v2 = api.request('views.retain', view=v1, key='old')
    v3 = api.request('views.load', view=v2, key='current', ref=current)
    first = api.request('model.invoke', view=v3)
    v4 = first['continuationView']
    v5 = api.request('views.evict', view=v4, key='old')
    v6 = api.request('views.load', view=v5, key='old', ref=old)
    api.request('model.invoke', view=v6)
    return {'original': api.request('values.get', ref=v0),
            'after': api.request('values.get', ref=v4),
            'search': api.request('values.search', query='old history')}
`, { grants: ['model'] });
  const run = await kernel.run(entry, {});
  assert.equal(run.status, 'submitted', run.error);
  assert.deepEqual(requests.map(messages => messages.map(item => item.content)), [['old history', 'current observation'], ['old history']]);
  assert.deepEqual(run.output.original.entries, []);
  assert.equal(run.output.after.entries.length, 1);
  assert.ok(run.output.search.refs.length > 0);
  assert.equal(run.modelCalls, 2);
});

test('generated component is callable and reusable after restarting the host', async t => {
  const { kernel, root } = await fixture(t);
  const builder = kernel.define(`def main(task, api):
    source = api.request('values.put', value='def main(x, api):\\n    return x * 2\\n')
    ref = api.request('executables.install', definition={'abi': 'horizon/python-v0', 'source': source, 'bindings': {}, 'grants': []})
    return {'ref': ref, 'answer': api.request('call', target=ref, input=6)}
`, { grants: ['install'] });
  const run = await kernel.run(builder, {});
  assert.equal(run.status, 'submitted', run.error);
  assert.equal(run.output.answer, 12);
  const restarted = await createHost({ root, worker: { mode: 'trusted-process' } });
  try { assert.equal((await restarted.kernel.run(run.output.ref, 9)).output, 18); }
  finally { await restarted.dispose(); }
  const events = readFileSync(join(root, 'runs', run.id, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  const calls = events.filter(event => event.kind === 'call.begin');
  assert.equal(calls.length, 2);
  assert.equal(calls[1].data.parent, calls[0].call);
  assert.deepEqual(events.map(event => event.sequence), events.map((_, index) => index + 1));
});

test('reference knowledge does not authorize reading private evaluator values', async t => {
  const { kernel } = await fixture(t);
  const secret = kernel.values.put('hidden answer');
  const entry = kernel.define(`def main(task, api):\n    return api.request('values.get', ref=task['ref'])\n`);
  const run = await kernel.run(entry, { ref: secret });
  assert.equal(run.status, 'failed');
  assert.match(run.error, /not available/);
});

test('nested component capabilities cannot exceed the caller', async t => {
  const { kernel } = await fixture(t);
  const child = kernel.define('def main(task, api):\n    return True\n', { grants: ['model'] });
  const parent = kernel.define(`def main(task, api):\n    return api.request('call', target='child', input=task)\n`, { bindings: { child } });
  const run = await kernel.run(parent, {});
  assert.equal(run.status, 'failed');
  assert.match(run.error, /capability escalation/);
});

test('nested calls consume a shared operation budget', async t => {
  const { kernel } = await fixture(t);
  const child = kernel.define(`def main(task, api):\n    return api.request('values.put', value=1)\n`);
  const parent = kernel.define(`def main(task, api):\n    return api.request('call', target='child', input=task)\n`, { bindings: { child } });
  const run = await kernel.run(parent, {}, { maxOperations: 3 });
  assert.equal(run.status, 'failed');
  assert.match(run.error, /budget/);
});

test('oversized context is rejected before the model is invoked', async t => {
  let invoked = false;
  const { kernel } = await fixture(t, { model: async () => { invoked = true; return 'unexpected'; } });
  const entry = kernel.define(`def main(task, api):
    ref = api.request('values.put', value='x' * 100)
    view = api.request('views.create')
    view = api.request('views.load', view=view, key='input', ref=ref)
    return api.request('model.invoke', view=view)
`, { grants: ['model'] });
  const run = await kernel.run(entry, {}, { contextBytes: 20 });
  assert.equal(run.status, 'failed');
  assert.match(run.error, /capacity/);
  assert.equal(invoked, false);
  assert.equal(run.modelCalls, 0);
});

test('worker deadline stops a component that never returns', async t => {
  const { kernel } = await fixture(t);
  const entry = kernel.define('def main(task, api):\n    while True: pass\n');
  const run = await kernel.run(entry, {}, { timeoutMs: 200 });
  assert.equal(run.status, 'failed');
  assert.match(run.error, /deadline/);
  assert.ok(run.elapsedMs < 3000);
});

test('evaluation withholds expected values and compares actual candidates', async t => {
  const { kernel } = await fixture(t);
  const wrong = kernel.define('def main(task, api):\n    return 0\n');
  const correct = kernel.define('def main(task, api):\n    assert "expected" not in task\n    return sum(task["numbers"])\n');
  const selection = await compare(kernel, [wrong, correct], [{ id: 'development', input: { numbers: [2, 3] }, expected: 5 }]);
  assert.equal(selection.selected, correct);
  const transfer = await evaluate(kernel, selection.selected, [{ id: 'new-task', input: { numbers: [-4, 7] }, expected: 3 }]);
  assert.equal(transfer.passed, 1);
});

test('editable seed generates and invokes source through the model boundary', async t => {
  const { kernel } = await fixture(t, { model: async messages => {
    assert.equal(messages[0].role, 'system');
    assert.deepEqual(JSON.parse(messages[1].content), { numbers: [3, 8] });
    return { content: JSON.stringify({ source: 'def main(task, api):\n    return sum(task["numbers"])\n' }), usage: { total_tokens: 25 } };
  } });
  const entry = kernel.define(readFileSync(new URL('../seeds/generate.py', import.meta.url), 'utf8'), { grants: ['model', 'install'] });
  const run = await kernel.run(entry, { numbers: [3, 8] });
  assert.equal(run.status, 'submitted', run.error);
  assert.equal(run.output, 11);
  assert.equal(run.modelCalls, 1);
});

if (process.env.HW_TEST_DOCKER === '1') {
  test('Docker worker executes an installed component with no model credentials', async t => {
    const { kernel } = await fixture(t, { worker: { mode: 'docker' } });
    const entry = kernel.define('import os\ndef main(task, api):\n    assert "HW_API_KEY" not in os.environ\n    return task + 1\n');
    const run = await kernel.run(entry, 4, { timeoutMs: 30000 });
    assert.equal(run.status, 'submitted', run.error);
    assert.equal(run.output, 5);
  });
}
