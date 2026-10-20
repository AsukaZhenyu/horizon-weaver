// Shared wire contracts. Decoders validate data arriving from Python or storage;
// TypeScript types alone cannot establish trust across those boundaries.
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };
export type ValueRef = `sha256:${string}`;
export type Capability = 'model' | 'install';
export type MessageRole = 'system' | 'user' | 'assistant';
export type ModelMessage = { role: MessageRole; content: string };
export type ModelReply = { content: string; usage?: JsonValue; finishReason?: JsonValue };
export type ModelAdapter = (messages: ModelMessage[], options: { signal: AbortSignal }) => Promise<ModelReply>;

export type ExecutableDefinition = {
  abi: 'horizon/python-v0';
  source: ValueRef;
  bindings: Record<string, ValueRef>;
  grants: Capability[];
};
export type Executable = ExecutableDefinition & { kind: 'executable' };
export type ContextEntry = { key: string; ref: ValueRef; role: MessageRole; remainingCalls: number | null };
export type ContextView = { kind: 'view'; entries: ContextEntry[] };

export type OperationArguments = {
  'values.put': { value: JsonValue; parents: ValueRef[] };
  'values.get': { ref: ValueRef };
  'values.search': { query: string; offset: number };
  'views.create': Record<string, never>;
  'views.load': { view: ValueRef; key: string; ref: ValueRef; role: MessageRole };
  'views.retain': { view: ValueRef; key: string; calls: number | null };
  'views.evict': { view: ValueRef; key: string };
  'model.invoke': { view: ValueRef };
  'executables.install': { definition: ExecutableDefinition };
  'call': { target: string; input: JsonValue };
};
export type Operation = keyof OperationArguments;
export type HostRequest = { [K in Operation]: { operation: K; arguments: OperationArguments[K] } }[Operation];
export type ViewRequest = Extract<HostRequest, { operation: 'views.load' | 'views.retain' | 'views.evict' }>;

export type RunLimits = { maxOperations: number; timeoutMs: number; contextBytes: number; maxDepth: number; grants: Capability[] };
export type RunOutcome = { status: 'submitted'; output: JsonValue } | { status: 'failed'; error: string };
export type RunResult = RunOutcome & { id: string; entry: ValueRef; operations: number; modelCalls: number; elapsedMs: number };
export type EventData = {
  'call.begin': { parent: string | null; definition: ValueRef; input: ValueRef };
  'call.return': { output: ValueRef };
  'call.failure': { error: string };
  'operation.begin': { operation: string; arguments: JsonValue };
  'operation.return': { operation: string; result: JsonValue };
  'operation.failure': { operation: string; error: string };
  'run.end': RunResult;
};
export type JournalEvent = { [K in keyof EventData]: { sequence: number; kind: K; call: string | null; data: EventData[K] } }[keyof EventData];

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype;
}

export function reference(value: unknown): ValueRef {
  if (typeof value !== 'string' || !/^sha256:[a-f0-9]{64}$/.test(value)) throw new Error('Invalid value reference');
  return value as ValueRef;
}

function text(value: unknown): string {
  if (typeof value !== 'string') throw new Error('Expected text argument');
  return value;
}

function key(value: unknown): string {
  const result = text(value);
  if (!result) throw new Error('A context key is required');
  return result;
}

function count(value: unknown, minimum: number): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) throw new Error('Invalid count');
  return value;
}

export function grants(value: unknown): Capability[] {
  if (!Array.isArray(value) || !value.every((item: unknown) => item === 'model' || item === 'install')) throw new Error('Unknown capability');
  return value as Capability[];
}

function role(value: unknown): MessageRole {
  if (value !== 'system' && value !== 'user' && value !== 'assistant') throw new Error('Invalid message role');
  return value;
}

export function executableDefinition(value: unknown): ExecutableDefinition {
  if (!isJsonObject(value) || value['abi'] !== 'horizon/python-v0') throw new Error('Unsupported executable ABI');
  const bindings = value['bindings'];
  if (!isJsonObject(bindings)) throw new Error('Invalid bindings');
  return {
    abi: 'horizon/python-v0', source: reference(value['source']),
    bindings: Object.fromEntries(Object.entries(bindings).map(([name, ref]) => [name, reference(ref)])),
    grants: grants(value['grants']),
  };
}

export function contextView(value: JsonValue): ContextView {
  if (!isJsonObject(value) || value['kind'] !== 'view' || !Array.isArray(value['entries'])) throw new Error('Expected a context view');
  const entries = value['entries'].map(entry => {
    if (!isJsonObject(entry)) throw new Error('Invalid context entry');
    return { key: key(entry['key']), ref: reference(entry['ref']), role: role(entry['role']),
      remainingCalls: entry['remainingCalls'] === null ? null : count(entry['remainingCalls'], 1) };
  });
  if (new Set(entries.map(entry => entry.key)).size !== entries.length) throw new Error('Context key already exists');
  return { kind: 'view', entries };
}

export function decodeRequest(operation: string, args: JsonValue): HostRequest {
  if (!isJsonObject(args)) throw new Error('RPC arguments must be an object');
  switch (operation) {
    case 'values.put': {
      const parents = args['parents'] ?? [];
      if (!Array.isArray(parents)) throw new Error('Invalid parents');
      const value = args['value'];
      if (value === undefined) throw new Error('Missing value');
      return { operation, arguments: { value, parents: parents.map(reference) } };
    }
    case 'values.get': return { operation, arguments: { ref: reference(args['ref']) } };
    case 'values.search': return { operation, arguments: { query: text(args['query']), offset: count(args['offset'] ?? 0, 0) } };
    case 'views.create': return { operation, arguments: {} };
    case 'views.load': return { operation, arguments: { view: reference(args['view']), key: key(args['key']), ref: reference(args['ref']), role: role(args['role'] ?? 'user') } };
    case 'views.retain': return { operation, arguments: { view: reference(args['view']), key: key(args['key']), calls: args['calls'] == null ? null : count(args['calls'], 1) } };
    case 'views.evict': return { operation, arguments: { view: reference(args['view']), key: key(args['key']) } };
    case 'model.invoke': return { operation, arguments: { view: reference(args['view']) } };
    case 'executables.install': return { operation, arguments: { definition: executableDefinition(args['definition']) } };
    case 'call': {
      const input = args['input'];
      if (input === undefined) throw new Error('Missing component input');
      return { operation, arguments: { target: text(args['target']), input } };
    }
    default: throw new Error(`Unknown operation: ${operation}`);
  }
}
