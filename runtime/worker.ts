import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { errorMessage, isJsonObject } from './protocol.js';
import type { JsonValue } from './protocol.js';
import { parseJson } from './values.js';

// Compiled modules live in dist/runtime; Python assets stay at the project root.
const sdk = fileURLToPath(new URL('../../sdk/python/worker.py', import.meta.url));
export type WorkerConfiguration = { mode: 'docker' | 'trusted-process'; python: string; image: string };
export type WorkerOptions = WorkerConfiguration & { timeoutMs: number; signal: AbortSignal };
export type Dispatcher = (operation: string, args: JsonValue) => Promise<JsonValue>;
type WorkerOutcome = { type: 'return'; value: JsonValue } | { type: 'failure'; error: string };

export function execute(source: string, input: JsonValue, dispatch: Dispatcher,
  { mode, python, image, timeoutMs, signal }: WorkerOptions): Promise<JsonValue> {
  if (!['docker', 'trusted-process'].includes(mode)) throw new Error('Unknown worker mode');
  const container = `hw-worker-${randomUUID()}`;
  const command = mode === 'docker' ? 'docker' : python;
  const args = mode === 'docker'
    ? ['run', '--rm', '--name', container, '-i', '--network', 'none', '--read-only', '--cap-drop=ALL',
      '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '128m', '--cpus', '1',
      '--mount', `type=bind,source=${sdk},target=/worker.py,readonly`, image, 'python', '-I', '/worker.py']
    : ['-I', sdk];
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
      env: Object.fromEntries(Object.entries({ PATH: process.env['PATH'], SystemRoot: process.env['SystemRoot'], TEMP: process.env['TEMP'] })
        .filter((entry): entry is [string, string] => entry[1] !== undefined)),
    });
    let outcome: WorkerOutcome | undefined;
    let failure: Error | undefined;
    let stderr = '', size = 0, closed = false;
    let queue = Promise.resolve();
    const terminate = (): void => {
      if (mode === 'docker') {
        const cleanup = spawn('docker', ['rm', '-f', container], { stdio: 'ignore', windowsHide: true });
        cleanup.on('error', () => {});
      }
      child.kill();
    };
    const timer = setTimeout(() => { failure = new Error('Component deadline exceeded'); closed = true; terminate(); }, timeoutMs);
    const onAbort = (): void => { failure = new Error('Run cancelled'); closed = true; terminate(); };
    if (signal.aborted) onAbort();
    else signal.addEventListener('abort', onAbort, { once: true });
    child.stdin.on('error', () => {});
    child.stderr.on('data', (bytes: Buffer) => { stderr = (stderr + bytes.toString()).slice(-8192); });
    child.on('error', error => { clearTimeout(timer); signal.removeEventListener('abort', onAbort); closed = true; reject(error); });
    const lines = createInterface({ input: child.stdout });
    lines.on('line', line => {
      size += Buffer.byteLength(line);
      if (size > 4 * 1024 * 1024) { failure = new Error('Component protocol output exceeded limit'); terminate(); return; }
      queue = queue.then(async () => {
        if (closed) return;
        const packet = parseJson(line);
        if (!isJsonObject(packet)) throw new Error('Invalid component packet');
        if (packet['type'] === 'request') {
          const id = packet['id'], operation = packet['operation'], arguments_ = packet['arguments'];
          if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 1 || typeof operation !== 'string' || arguments_ === undefined) {
            throw new Error('Invalid component request');
          }
          let response: JsonValue;
          try { response = { id, result: await dispatch(operation, arguments_) }; }
          catch (error) { response = { id, error: errorMessage(error) }; }
          if (!closed) child.stdin.write(JSON.stringify(response) + '\n');
        } else if (packet['type'] === 'return' || packet['type'] === 'failure') {
          if (outcome) throw new Error('Multiple component results');
          if (packet['type'] === 'return' && packet['value'] !== undefined) outcome = { type: 'return', value: packet['value'] };
          else if (packet['type'] === 'failure' && typeof packet['error'] === 'string') outcome = { type: 'failure', error: packet['error'] };
          else throw new Error('Invalid component result');
        } else throw new Error('Invalid component packet');
      }).catch(error => { failure = new Error(errorMessage(error)); terminate(); });
    });
    child.on('close', async code => {
      await queue;
      closed = true;
      clearTimeout(timer);
      signal.removeEventListener('abort', onAbort);
      if (failure) reject(failure);
      else if (code !== 0 || outcome?.type !== 'return') reject(new Error(outcome?.type === 'failure' ? outcome.error : `Worker exited ${code}: ${stderr}`));
      else resolve(outcome.value);
    });
    child.stdin.write(JSON.stringify({ source, input }) + '\n');
  });
}
