import { Context, Service } from '@deepseek-ai/cordis';
import { randomUUID } from 'node:crypto';
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { Values, canonical, consumeView, viewOperation } from './values.js';
import { execute } from './worker.js';
import type { WorkerConfiguration } from './worker.js';
import { contextView, decodeRequest, errorMessage, executableDefinition, grants, isJsonObject, reference } from './protocol.js';
import type { Capability, EventData, Executable, ExecutableDefinition, JsonValue, ModelAdapter,
  ModelMessage, RunLimits, RunOutcome, RunResult, ValueRef } from './protocol.js';

declare module '@deepseek-ai/cordis' {
  interface Context { weaver: Weaver | undefined }
}

const capabilities: Capability[] = ['model', 'install'];
export type HostOptions = { root: string; worker?: Partial<WorkerConfiguration>; model?: ModelAdapter };
type RunState = {
  id: string; directory: string; sequence: number; operations: number; modelCalls: number;
  closed: boolean; controller: AbortController; allowed: Set<ValueRef>; executables: Set<ValueRef>;
  limits: RunLimits; deadline: number;
};

async function bounded<T>(operation: Promise<T>, milliseconds: number, signal: AbortSignal): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('Operation deadline exceeded')), Math.max(1, milliseconds)); }),
      new Promise<never>((_, reject) => {
        onAbort = () => reject(new Error('Run cancelled'));
        if (signal.aborted) onAbort();
        else signal.addEventListener('abort', onAbort, { once: true });
      }),
    ]);
  } finally {
    clearTimeout(timer);
    if (onAbort) signal.removeEventListener('abort', onAbort);
  }
}

export class Weaver extends Service {
  readonly root: string;
  readonly values: Values;
  readonly worker: WorkerConfiguration;
  readonly model: ModelAdapter | undefined;
  private disposed = false;
  private readonly controllers = new Set<AbortController>();

  constructor(ctx: Context, options: HostOptions) {
    super(ctx, 'weaver');
    this.root = options.root;
    this.values = new Values(this.root);
    this.worker = { mode: 'docker', python: 'python', image: 'python:3.12-alpine', ...options.worker };
    this.model = options.model;
    ctx.effect(() => () => {
      this.disposed = true;
      for (const controller of this.controllers) controller.abort();
    });
  }
  define(source: string, { bindings = {}, grants = [] }: { bindings?: Record<string, ValueRef>; grants?: Capability[] } = {}): ValueRef {
    if (this.disposed) throw new Error('Host has been disposed');
    const ref = this.values.put(source, { producer: 'bootstrap' });
    return this.install({ abi: 'horizon/python-v0', source: ref, bindings, grants }, 'bootstrap');
  }
  install(input: ExecutableDefinition, producer: string): ValueRef {
    if (this.disposed) throw new Error('Host has been disposed');
    const definition = executableDefinition(input);
    if (typeof this.values.get(definition.source).value !== 'string') throw new Error('Component source must be text');
    for (const ref of Object.values(definition.bindings)) this.definition(ref);
    return this.values.put({ kind: 'executable', ...definition }, { parents: [definition.source, ...Object.values(definition.bindings)], producer });
  }
  definition(ref: ValueRef): Executable {
    const value = this.values.get(ref).value;
    if (!isJsonObject(value) || value['kind'] !== 'executable') throw new Error('Expected executable definition');
    return { kind: 'executable', ...executableDefinition(value) };
  }
  async run(entry: ValueRef, input: JsonValue, limits: Partial<RunLimits> = {}): Promise<RunResult> {
    if (this.disposed) throw new Error('Host has been disposed');
    const id = randomUUID();
    const directory = join(this.root, 'runs', id);
    mkdirSync(directory, { recursive: true });
    const started = Date.now();
    const run: RunState = {
      id, directory, sequence: 0, operations: 0, modelCalls: 0, closed: false, controller: new AbortController(),
      allowed: new Set(), executables: new Set(),
      limits: { maxOperations: 256, timeoutMs: 30000, contextBytes: 65536, maxDepth: 12, grants: capabilities, ...limits },
      deadline: 0,
    };
    run.deadline = started + run.limits.timeoutMs;
    this.controllers.add(run.controller);
    const admit = (ref: ValueRef): void => {
      if (run.executables.has(ref)) return;
      const definition = this.definition(ref);
      run.executables.add(ref);
      run.allowed.add(ref);
      run.allowed.add(definition.source);
      for (const child of Object.values(definition.bindings)) admit(child);
    };
    let outcome: RunOutcome;
    try {
      if (!Number.isSafeInteger(run.limits.maxOperations) || run.limits.maxOperations < 1 ||
          !Number.isSafeInteger(run.limits.timeoutMs) || run.limits.timeoutMs < 1 ||
          !Number.isSafeInteger(run.limits.contextBytes) || run.limits.contextBytes < 1 ||
          !Number.isSafeInteger(run.limits.maxDepth) || run.limits.maxDepth < 1) throw new Error('Invalid run limits');
      grants(run.limits.grants);
      canonical(input);
      admit(entry);
      const output = await this.invoke(run, entry, input, run.limits.grants, null, 0);
      outcome = { status: 'submitted', output };
    } catch (error) { outcome = { status: 'failed', error: errorMessage(error) }; }
    run.closed = true;
    this.controllers.delete(run.controller);
    const result: RunResult = { id, entry, ...outcome, operations: run.operations, modelCalls: run.modelCalls, elapsedMs: Date.now() - started };
    this.record(run, 'run.end', null, result);
    writeFileSync(join(directory, 'result.json'), JSON.stringify(result, null, 2));
    return result;
  }
  private record<K extends keyof EventData>(run: RunState, kind: K, call: string | null, data: EventData[K]): void {
    appendFileSync(join(run.directory, 'events.jsonl'), JSON.stringify({ sequence: ++run.sequence, kind, call, data }) + '\n');
  }
  private check(run: RunState): void {
    if (run.controller.signal.aborted) throw new Error('Run cancelled');
    if (run.closed || Date.now() >= run.deadline) throw new Error('Run deadline exceeded');
    if (++run.operations > run.limits.maxOperations) throw new Error('Run operation budget exceeded');
  }
  private read(run: RunState, ref: ValueRef): JsonValue {
    if (!run.allowed.has(ref)) throw new Error('Reference is not available to this run');
    return this.values.get(ref).value;
  }
  private put(run: RunState, value: JsonValue, producer: string, parents: ValueRef[] = []): ValueRef {
    for (const ref of parents) this.read(run, ref);
    const ref = this.values.put(value, { parents, producer });
    run.allowed.add(ref);
    return ref;
  }
  private async invoke(run: RunState, ref: ValueRef, input: JsonValue, available: Capability[], parent: string | null, depth: number): Promise<JsonValue> {
    this.check(run);
    if (depth > run.limits.maxDepth) throw new Error('Component depth exceeded');
    if (!run.executables.has(ref)) throw new Error('Executable is not available to this run');
    const definition = this.definition(ref);
    if (definition.grants.some(grant => !available.includes(grant))) throw new Error('Component capability escalation');
    const call = randomUUID();
    const inputRef = this.put(run, input, call);
    this.record(run, 'call.begin', call, { parent, definition: ref, input: inputRef });
    try {
      const source = this.read(run, definition.source);
      if (typeof source !== 'string') throw new Error('Component source must be text');
      const output = await execute(source, input,
        (operation, args) => this.dispatch(run, definition, call, depth, operation, args),
        { ...this.worker, timeoutMs: Math.max(1, run.deadline - Date.now()), signal: run.controller.signal });
      if (run.closed || Date.now() >= run.deadline) throw new Error('Run deadline exceeded');
      const outputRef = this.put(run, output, call, [inputRef]);
      this.record(run, 'call.return', call, { output: outputRef });
      return output;
    } catch (error) {
      this.record(run, 'call.failure', call, { error: errorMessage(error) });
      throw error;
    }
  }
  private async dispatch(run: RunState, definition: Executable, call: string, depth: number, operation: string, args: JsonValue): Promise<JsonValue> {
    this.check(run);
    this.record(run, 'operation.begin', call, { operation, arguments: args });
    const requireGrant = (grant: Capability): void => {
      if (!definition.grants.includes(grant)) throw new Error(`Capability required: ${grant}`);
    };
    let result: JsonValue;
    try {
      const request = decodeRequest(operation, args);
      switch (request.operation) {
        case 'values.put': result = this.put(run, request.arguments.value, call, request.arguments.parents); break;
        case 'values.get': result = this.read(run, request.arguments.ref); break;
        case 'values.search': {
          const { query, offset } = request.arguments;
          const refs = [...run.allowed].sort().filter(ref => canonical(this.read(run, ref)).includes(query));
          result = { refs: refs.slice(offset, offset + 20), nextOffset: offset + 20 < refs.length ? offset + 20 : null };
          break;
        }
        case 'views.create': result = this.put(run, { kind: 'view', entries: [] }, call); break;
        case 'views.load': case 'views.retain': case 'views.evict': {
          const view = contextView(this.read(run, request.arguments.view));
          if (request.operation === 'views.load') this.read(run, request.arguments.ref);
          result = this.put(run, viewOperation(request, view), call,
            request.operation === 'views.load' ? [request.arguments.view, request.arguments.ref] : [request.arguments.view]);
          break;
        }
        case 'model.invoke': {
          requireGrant('model');
          const model = this.model;
          if (!model) throw new Error('No model adapter configured');
          const viewRef = request.arguments.view;
          const view = contextView(this.read(run, viewRef));
          const messages: ModelMessage[] = view.entries.map(item => {
            const value = this.read(run, item.ref);
            return { role: item.role, content: typeof value === 'string' ? value : canonical(value) };
          });
          const bytes = Buffer.byteLength(JSON.stringify(messages));
          if (bytes > run.limits.contextBytes) throw new Error(`Context capacity exceeded: ${bytes} bytes`);
          const modelRequest = this.put(run, { messages, bytes, measurement: 'utf8-bytes' }, call, [viewRef]);
          const continuationView = this.put(run, consumeView(view), call, [viewRef]);
          run.modelCalls++;
          const signal = AbortSignal.any([run.controller.signal, AbortSignal.timeout(Math.max(1, run.deadline - Date.now()))]);
          try {
            const reply = await bounded(Promise.resolve().then(() => model(messages, { signal })), run.deadline - Date.now(), signal);
            result = { request: modelRequest, reply: this.put(run, reply, call, [modelRequest]), continuationView };
          } catch (error) { result = { request: modelRequest, error: errorMessage(error), continuationView }; }
          break;
        }
        case 'executables.install': {
          requireGrant('install');
          const next = request.arguments.definition;
          this.read(run, next.source);
          for (const ref of Object.values(next.bindings)) this.read(run, ref);
          if (next.grants.some(grant => !definition.grants.includes(grant))) throw new Error('Component capability escalation');
          const installed = this.install(next, call);
          run.allowed.add(installed);
          run.executables.add(installed);
          result = installed;
          break;
        }
        case 'call': {
          const { target, input } = request.arguments;
          const ref = Object.hasOwn(definition.bindings, target) ? definition.bindings[target] : reference(target);
          if (!ref) throw new Error('Missing component binding');
          result = await this.invoke(run, ref, input, definition.grants, call, depth + 1);
          break;
        }
      }
      this.record(run, 'operation.return', call, { operation, result });
      return result;
    } catch (error) {
      this.record(run, 'operation.failure', call, { operation, error: errorMessage(error) });
      throw error;
    }
  }
}

export async function createHost(options: HostOptions): Promise<{ context: Context; kernel: Weaver; dispose: () => Promise<void> }> {
  const context = new Context();
  const fiber = await context.plugin(Weaver, options);
  const kernel = context.weaver;
  if (!kernel) throw new Error('Cordis did not register the weaver service');
  return { context, kernel, dispose: async () => { await fiber.dispose(); } };
}
