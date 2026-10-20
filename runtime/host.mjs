import { Context, Service } from '@deepseek-ai/cordis';
import { randomUUID } from 'node:crypto';
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { Values, canonical, consumeView, viewOperation } from './values.mjs';
import { execute } from './worker.mjs';

const capabilities = ['model', 'install'];

async function bounded(operation, milliseconds, signal) {
  let timer;
  let onAbort;
  try {
    return await Promise.race([
      operation,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Operation deadline exceeded')), Math.max(1, milliseconds)); }),
      new Promise((_, reject) => {
        onAbort = () => reject(new Error('Run cancelled'));
        if (signal.aborted) onAbort();
        else signal.addEventListener('abort', onAbort, { once: true });
      }),
    ]);
  } finally { clearTimeout(timer); signal.removeEventListener('abort', onAbort); }
}

export class Weaver extends Service {
  constructor(ctx, options) {
    super(ctx, 'weaver');
    this.root = options.root;
    this.values = new Values(this.root);
    this.worker = { mode: 'docker', python: 'python', image: 'python:3.12-alpine', ...options.worker };
    this.model = options.model;
    this.disposed = false;
    this.controllers = new Set();
    ctx.effect(() => () => {
      this.disposed = true;
      for (const controller of this.controllers) controller.abort();
    });
  }
  define(source, { bindings = {}, grants = [] } = {}) {
    if (this.disposed) throw new Error('Host has been disposed');
    const ref = this.values.put(source, { producer: 'bootstrap' });
    return this.install({ abi: 'horizon/python-v0', source: ref, bindings, grants }, 'bootstrap');
  }
  install(definition, producer) {
    if (this.disposed) throw new Error('Host has been disposed');
    if (definition?.abi !== 'horizon/python-v0') throw new Error('Unsupported executable ABI');
    if (typeof this.values.get(definition.source).value !== 'string') throw new Error('Component source must be text');
    if (!Array.isArray(definition.grants) || definition.grants.some(item => !capabilities.includes(item))) throw new Error('Unknown capability');
    if (!definition.bindings || Array.isArray(definition.bindings) || typeof definition.bindings !== 'object') throw new Error('Invalid bindings');
    for (const ref of Object.values(definition.bindings)) this.definition(ref);
    return this.values.put({ kind: 'executable', ...definition }, { parents: [definition.source, ...Object.values(definition.bindings)], producer });
  }
  definition(ref) {
    const definition = this.values.get(ref).value;
    if (definition?.kind !== 'executable' || definition.abi !== 'horizon/python-v0') throw new Error('Expected executable definition');
    return definition;
  }
  async run(entry, input, limits = {}) {
    if (this.disposed) throw new Error('Host has been disposed');
    const id = randomUUID();
    const directory = join(this.root, 'runs', id);
    mkdirSync(directory, { recursive: true });
    const started = Date.now();
    const run = {
      id, directory, sequence: 0, operations: 0, modelCalls: 0, closed: false, controller: new AbortController(),
      allowed: new Set(), executables: new Set(),
      limits: { maxOperations: 256, timeoutMs: 30000, contextBytes: 65536, maxDepth: 12, grants: capabilities, ...limits },
    };
    run.deadline = started + run.limits.timeoutMs;
    this.controllers.add(run.controller);
    const admit = ref => {
      if (run.executables.has(ref)) return;
      const definition = this.definition(ref);
      run.executables.add(ref);
      run.allowed.add(ref);
      run.allowed.add(definition.source);
      for (const child of Object.values(definition.bindings)) admit(child);
    };
    let result;
    try {
      if (!Number.isSafeInteger(run.limits.maxOperations) || run.limits.maxOperations < 1 ||
          !Number.isSafeInteger(run.limits.timeoutMs) || run.limits.timeoutMs < 1 ||
          !Number.isSafeInteger(run.limits.contextBytes) || run.limits.contextBytes < 1 ||
          !Number.isSafeInteger(run.limits.maxDepth) || run.limits.maxDepth < 1 ||
          !Array.isArray(run.limits.grants) || run.limits.grants.some(item => !capabilities.includes(item))) throw new Error('Invalid run limits');
      canonical(input);
      admit(entry);
      const output = await this.invoke(run, entry, input, run.limits.grants, null, 0);
      result = { status: 'submitted', output };
    } catch (error) { result = { status: 'failed', error: error.message }; }
    run.closed = true;
    this.controllers.delete(run.controller);
    result = { id, entry, ...result, operations: run.operations, modelCalls: run.modelCalls, elapsedMs: Date.now() - started };
    this.record(run, 'run.end', null, result);
    writeFileSync(join(directory, 'result.json'), JSON.stringify(result, null, 2));
    return result;
  }
  record(run, kind, call, data) {
    appendFileSync(join(run.directory, 'events.jsonl'), JSON.stringify({ sequence: ++run.sequence, kind, call, data }) + '\n');
  }
  check(run) {
    if (run.controller.signal.aborted) throw new Error('Run cancelled');
    if (run.closed || Date.now() >= run.deadline) throw new Error('Run deadline exceeded');
    if (++run.operations > run.limits.maxOperations) throw new Error('Run operation budget exceeded');
  }
  read(run, ref) {
    if (!run.allowed.has(ref)) throw new Error('Reference is not available to this run');
    return this.values.get(ref).value;
  }
  put(run, value, producer, parents = []) {
    for (const ref of parents) this.read(run, ref);
    const ref = this.values.put(value, { parents, producer });
    run.allowed.add(ref);
    return ref;
  }
  async invoke(run, ref, input, available, parent, depth) {
    this.check(run);
    if (depth > run.limits.maxDepth) throw new Error('Component depth exceeded');
    if (!run.executables.has(ref)) throw new Error('Executable is not available to this run');
    const definition = this.definition(ref);
    if (definition.grants.some(grant => !available.includes(grant))) throw new Error('Component capability escalation');
    const call = randomUUID();
    const inputRef = this.put(run, input, call);
    this.record(run, 'call.begin', call, { parent, definition: ref, input: inputRef });
    try {
      const output = await execute(this.read(run, definition.source), input,
        (operation, args) => this.dispatch(run, definition, call, depth, operation, args),
        { ...this.worker, timeoutMs: Math.max(1, run.deadline - Date.now()), signal: run.controller.signal });
      if (run.closed || Date.now() >= run.deadline) throw new Error('Run deadline exceeded');
      const outputRef = this.put(run, output, call, [inputRef]);
      this.record(run, 'call.return', call, { output: outputRef });
      return output;
    } catch (error) {
      this.record(run, 'call.failure', call, { error: error.message });
      throw error;
    }
  }
  async dispatch(run, definition, call, depth, operation, args) {
    this.check(run);
    if (!args || Array.isArray(args) || typeof args !== 'object') throw new Error('RPC arguments must be an object');
    this.record(run, 'operation.begin', call, { operation, arguments: args });
    const requireGrant = grant => { if (!definition.grants.includes(grant)) throw new Error(`Capability required: ${grant}`); };
    let result;
    try {
      if (operation === 'values.put') result = this.put(run, args.value, call, args.parents ?? []);
      else if (operation === 'values.get') result = this.read(run, args.ref);
      else if (operation === 'values.search') {
        if (typeof args.query !== 'string' || !Number.isSafeInteger(args.offset ?? 0) || (args.offset ?? 0) < 0) throw new Error('Invalid search');
        const refs = [...run.allowed].sort().filter(ref => canonical(this.read(run, ref)).includes(args.query));
        const offset = args.offset ?? 0;
        result = { refs: refs.slice(offset, offset + 20), nextOffset: offset + 20 < refs.length ? offset + 20 : null };
      } else if (operation === 'views.create') result = this.put(run, { kind: 'view', entries: [] }, call);
      else if (['views.load', 'views.retain', 'views.evict'].includes(operation)) {
        const view = this.read(run, args.view);
        if (operation === 'views.load') this.read(run, args.ref);
        result = this.put(run, viewOperation(operation.slice(6), view, args), call,
          operation === 'views.load' ? [args.view, args.ref] : [args.view]);
      } else if (operation === 'model.invoke') {
        requireGrant('model');
        if (!this.model) throw new Error('No model adapter configured');
        const view = this.read(run, args.view);
        if (view?.kind !== 'view' || !Array.isArray(view.entries)) throw new Error('Expected a context view');
        const messages = view.entries.map(item => ({ role: item.role, content: typeof this.read(run, item.ref) === 'string' ? this.read(run, item.ref) : canonical(this.read(run, item.ref)) }));
        const bytes = Buffer.byteLength(JSON.stringify(messages));
        if (bytes > run.limits.contextBytes) throw new Error(`Context capacity exceeded: ${bytes} bytes`);
        const request = this.put(run, { messages, bytes, measurement: 'utf8-bytes' }, call, [args.view]);
        const continuationView = this.put(run, consumeView(view), call, [args.view]);
        run.modelCalls++;
        const signal = AbortSignal.any([run.controller.signal, AbortSignal.timeout(Math.max(1, run.deadline - Date.now()))]);
        try {
          const reply = await bounded(Promise.resolve().then(() => this.model(messages, { signal })), run.deadline - Date.now(), signal);
          result = { request, reply: this.put(run, reply, call, [request]), continuationView };
        } catch (error) { result = { request, error: error.message, continuationView }; }
      } else if (operation === 'executables.install') {
        requireGrant('install');
        this.read(run, args.definition?.source);
        for (const ref of Object.values(args.definition.bindings ?? {})) this.read(run, ref);
        if (args.definition.grants?.some(grant => !definition.grants.includes(grant))) throw new Error('Component capability escalation');
        result = this.install(args.definition, call);
        run.allowed.add(result);
        run.executables.add(result);
      } else if (operation === 'call') {
        const target = Object.hasOwn(definition.bindings, args.target) ? definition.bindings[args.target] : args.target;
        result = await this.invoke(run, target, args.input, definition.grants, call, depth + 1);
      } else throw new Error(`Unknown operation: ${operation}`);
      this.record(run, 'operation.return', call, { operation, result });
      return result;
    } catch (error) {
      this.record(run, 'operation.failure', call, { operation, error: error.message });
      throw error;
    }
  }
}

export async function createHost(options) {
  const context = new Context();
  const fiber = await context.plugin(Weaver, options);
  return { context, kernel: context.weaver, dispose: () => fiber.dispose() };
}
