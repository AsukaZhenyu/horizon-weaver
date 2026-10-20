import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { isJsonObject, reference } from './protocol.js';
import type { ContextView, JsonValue, ValueRef, ViewRequest } from './protocol.js';

export function canonical(value: unknown): string {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (isJsonObject(value)) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  throw new Error('Values must be finite JSON data');
}

export function parseJson(text: string): JsonValue {
  const value: unknown = JSON.parse(text);
  canonical(value);
  return value as JsonValue;
}

export type ValueRecord = { value: JsonValue; parents: ValueRef[]; producer: string | null };

export class Values {
  readonly directory: string;

  constructor(root: string) {
    this.directory = join(root, 'values');
    mkdirSync(this.directory, { recursive: true });
  }
  put(value: JsonValue, { parents = [], producer = null }: { parents?: ValueRef[]; producer?: string | null } = {}): ValueRef {
    for (const parent of parents) this.get(parent);
    const record: ValueRecord = { value, parents, producer };
    const bytes = canonical(record);
    const ref: ValueRef = `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
    try { writeFileSync(this.path(ref), bytes, { flag: 'wx' }); }
    catch (error) {
      if (!(error instanceof Error) || !('code' in error) || error.code !== 'EEXIST') throw error;
      this.get(ref);
    }
    return ref;
  }
  path(ref: string): string {
    return join(this.directory, `${reference(ref).slice(7)}.json`);
  }
  get(ref: string): ValueRecord {
    const bytes = readFileSync(this.path(ref), 'utf8');
    if (`sha256:${createHash('sha256').update(bytes).digest('hex')}` !== ref) throw new Error('Value integrity failure');
    const record = parseJson(bytes);
    if (!isJsonObject(record) || record['value'] === undefined || !Array.isArray(record['parents']) ||
        (record['producer'] !== null && typeof record['producer'] !== 'string')) throw new Error('Invalid value record');
    return { value: record['value'], parents: record['parents'].map(reference), producer: record['producer'] };
  }
}

// These functions only describe context operations. They do not rank information.
export function viewOperation(request: ViewRequest, view: ContextView): ContextView {
  const entries = structuredClone(view.entries);
  if (request.operation === 'views.load') {
    const args = request.arguments;
    if (entries.some(item => item.key === args.key)) throw new Error('Context key already exists');
    entries.push({ key: args.key, ref: args.ref, role: args.role, remainingCalls: 1 });
  } else if (request.operation === 'views.retain') {
    const item = entries.find(item => item.key === request.arguments.key);
    if (!item) throw new Error('Context key not found');
    item.remainingCalls = request.arguments.calls;
  } else {
    return { kind: 'view', entries: entries.filter(item => item.key !== request.arguments.key) };
  }
  return { kind: 'view', entries };
}

export function consumeView(view: ContextView): ContextView {
  return {
    kind: 'view',
    entries: view.entries.map(item => ({ ...item, remainingCalls: item.remainingCalls === null ? null : item.remainingCalls - 1 }))
      .filter(item => item.remainingCalls === null || item.remainingCalls > 0),
  };
}
