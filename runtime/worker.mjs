import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const sdk = fileURLToPath(new URL('../sdk/python/worker.py', import.meta.url));

export function execute(source, input, dispatch, { mode, python, image, timeoutMs, signal }) {
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
      env: { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot, TEMP: process.env.TEMP },
    });
    let outcome, failure, stderr = '', size = 0, closed = false;
    let queue = Promise.resolve();
    const terminate = () => {
      if (mode === 'docker') {
        const cleanup = spawn('docker', ['rm', '-f', container], { stdio: 'ignore', windowsHide: true });
        cleanup.on('error', () => {});
      }
      child.kill();
    };
    const timer = setTimeout(() => { failure = new Error('Component deadline exceeded'); closed = true; terminate(); }, timeoutMs);
    const onAbort = () => { failure = new Error('Run cancelled'); closed = true; terminate(); };
    if (signal?.aborted) onAbort();
    else signal?.addEventListener('abort', onAbort, { once: true });
    child.stdin.on('error', () => {});
    child.stderr.on('data', bytes => { stderr = (stderr + bytes.toString()).slice(-8192); });
    child.on('error', error => { clearTimeout(timer); signal?.removeEventListener('abort', onAbort); closed = true; reject(error); });
    const lines = createInterface({ input: child.stdout });
    lines.on('line', line => {
      size += Buffer.byteLength(line);
      if (size > 4 * 1024 * 1024) { failure = new Error('Component protocol output exceeded limit'); terminate(); return; }
      queue = queue.then(async () => {
        if (closed) return;
        const packet = JSON.parse(line);
        if (packet.type === 'request') {
          let response;
          try { response = { id: packet.id, result: await dispatch(packet.operation, packet.arguments) }; }
          catch (error) { response = { id: packet.id, error: error.message }; }
          if (!closed) child.stdin.write(JSON.stringify(response) + '\n');
        } else if (packet.type === 'return' || packet.type === 'failure') {
          if (outcome) throw new Error('Multiple component results');
          outcome = packet;
        } else throw new Error('Invalid component packet');
      }).catch(error => { failure = error; terminate(); });
    });
    child.on('close', async code => {
      await queue;
      closed = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      if (failure) reject(failure);
      else if (code !== 0 || outcome?.type !== 'return') reject(new Error(outcome?.error ?? `Worker exited ${code}: ${stderr}`));
      else resolve(outcome.value);
    });
    child.stdin.write(JSON.stringify({ source, input }) + '\n');
  });
}
