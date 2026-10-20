import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export function canonical(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  throw new Error('Values must be finite JSON data');
}

export class Values {
  constructor(root) {
    this.directory = join(root, 'values');
    mkdirSync(this.directory, { recursive: true });
  }
  put(value, { parents = [], producer = null } = {}) {
    for (const parent of parents) this.get(parent);
    const record = { value, parents, producer };
    const bytes = canonical(record);
    const ref = `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
    try { writeFileSync(this.path(ref), bytes, { flag: 'wx' }); }
    catch (error) { if (error.code !== 'EEXIST') throw error; this.get(ref); }
    return ref;
  }
  path(ref) {
    if (typeof ref !== 'string' || !/^sha256:[a-f0-9]{64}$/.test(ref)) throw new Error('Invalid value reference');
    return join(this.directory, `${ref.slice(7)}.json`);
  }
  get(ref) {
    const bytes = readFileSync(this.path(ref), 'utf8');
    if (`sha256:${createHash('sha256').update(bytes).digest('hex')}` !== ref) throw new Error('Value integrity failure');
    return JSON.parse(bytes);
  }
}

// These functions only describe context operations. They do not rank information.
export function viewOperation(operation, view, args) {
  if (view?.kind !== 'view' || !Array.isArray(view.entries)) throw new Error('Expected a context view');
  const entries = structuredClone(view.entries);
  if (operation === 'load') {
    if (entries.some(item => item.key === args.key)) throw new Error('Context key already exists');
    if (typeof args.key !== 'string' || !args.key) throw new Error('A context key is required');
    if (!['system', 'user', 'assistant'].includes(args.role ?? 'user')) throw new Error('Invalid message role');
    entries.push({ key: args.key, ref: args.ref, role: args.role ?? 'user', remainingCalls: 1 });
  } else if (operation === 'retain') {
    const item = entries.find(item => item.key === args.key);
    if (!item) throw new Error('Context key not found');
    const calls = args.calls === undefined ? null : args.calls;
    if (calls !== null && (!Number.isSafeInteger(calls) || calls < 1)) throw new Error('Invalid retention count');
    item.remainingCalls = calls;
  } else if (operation === 'evict') {
    return { kind: 'view', entries: entries.filter(item => item.key !== args.key) };
  } else throw new Error('Unknown context operation');
  return { kind: 'view', entries };
}

export function consumeView(view) {
  return {
    kind: 'view',
    entries: view.entries.map(item => ({ ...item, remainingCalls: item.remainingCalls === null ? null : item.remainingCalls - 1 }))
      .filter(item => item.remainingCalls === null || item.remainingCalls > 0),
  };
}
