import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHost } from '../runtime/host.js';
import { openAICompatible } from '../runtime/model.js';
import { parseJson } from '../runtime/values.js';

test('Python RPC rejects malformed arguments and can continue after rejection', async t => {
  const root = mkdtempSync(join(tmpdir(), 'horizon-rpc-'));
  const host = await createHost({ root, worker: { mode: 'trusted-process' } });
  t.after(async () => { await host.dispose(); rmSync(root, { recursive: true, force: true }); });
  const entry = host.kernel.define(`def main(task, api):
    view = api.request('views.create')
    ref = api.request('values.put', value='observation')
    rejected = 0
    for operation, arguments in [
        ('views.load', {'view': view, 'key': 'x', 'ref': 123}),
        ('views.load', {'view': view, 'key': 'x', 'ref': ref, 'role': 'invalid'}),
        ('values.search', {'query': 'x', 'offset': -1}),
        ('executables.install', {'definition': {'abi': 'horizon/python-v0', 'source': ref, 'bindings': {}, 'grants': ['shell']}}),
    ]:
        try:
            api.request(operation, **arguments)
        except RuntimeError:
            rejected += 1
    view = api.request('views.load', view=view, key='x', ref=ref)
    return {'rejected': rejected, 'entries': len(api.request('values.get', ref=view)['entries'])}
`, { grants: ['install'] });
  const run = await host.kernel.run(entry, {});
  if (run.status !== 'submitted') assert.fail(run.error);
  assert.deepEqual(run.output, { rejected: 4, entries: 1 });
});

test('worker rejects an invalid result packet instead of accepting unchecked JSON', async t => {
  const root = mkdtempSync(join(tmpdir(), 'horizon-packet-'));
  const host = await createHost({ root, worker: { mode: 'trusted-process' } });
  t.after(async () => { await host.dispose(); rmSync(root, { recursive: true, force: true }); });
  const entry = host.kernel.define(`import sys
def main(task, api):
    sys.__stdout__.write('{"type":"return"}\\n')
    sys.__stdout__.flush()
    return 5
`);
  const run = await host.kernel.run(entry, {});
  assert.equal(run.status, 'failed');
  if (run.status !== 'failed') assert.fail('Invalid worker result was accepted');
  assert.match(run.error, /Invalid component result/);
});

test('model transport validates HTTP JSON and preserves usage without a real model', async t => {
  const replies = [
    { choices: [{ message: { content: 'answer' }, finish_reason: 'stop' }], usage: { total_tokens: 7 } },
    { choices: [{ message: { content: 7 } }] },
    { choices: [] },
  ];
  t.mock.method(globalThis, 'fetch', async (_url: string | URL | Request, init?: RequestInit) => {
    assert.ok(init && typeof init.body === 'string');
    assert.deepEqual(JSON.parse(init.body), { model: 'fixture', messages: [{ role: 'user', content: 'input' }], max_tokens: 2048, temperature: 0 });
    return Response.json(replies.shift());
  });
  const adapter = openAICompatible({ baseURL: 'http://fixture.invalid/v1', model: 'fixture' });
  const invoke = () => adapter([{ role: 'user', content: 'input' }], { signal: new AbortController().signal });
  assert.deepEqual(await invoke(), { content: 'answer', usage: { total_tokens: 7 }, finishReason: 'stop' });
  await assert.rejects(invoke, /no text content/);
  await assert.rejects(invoke, /no text content/);
  assert.throws(() => parseJson('{"value":1e400}'), /finite JSON/);
});
