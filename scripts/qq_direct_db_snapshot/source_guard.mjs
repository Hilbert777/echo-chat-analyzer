// SQLite's Windows byte locks, held by a dedicated, bounded helper process.
// Closing stdin releases our helper only; no QQ process is ever terminated.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

function failure() { const error = new Error('source_guard_failed'); error.code = 'source_guard_failed'; return error; }
export async function acquireSourceGuard(main, wal, { signal, deadline } = {}) {
  if (process.platform !== 'win32' || signal?.aborted) throw failure();
  const budget = Math.min(65000, Math.floor((deadline ?? performance.now() + 65000) - performance.now()));
  if (budget <= 0) throw failure();
  const executable = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const child = spawn(executable, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', fileURLToPath(new URL('./source_guard.ps1', import.meta.url))], {
    windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'],
  });
  child.stdin.on('error', () => {});
  let ended = false, exitCode, pending = null, buffered = [];
  const lines = createInterface({ input: child.stdout });
  const deliver = (error, value) => {
    if (!pending) { if (error) ended = true; else buffered.push(value); return; }
    const { resolve, reject, timer } = pending; pending = null; clearTimeout(timer);
    error ? reject(error) : resolve(value);
  };
  lines.on('line', line => {
    if (line === 'failed' || line.length > 200 || buffered.length > 2) deliver(failure());
    else deliver(null, line);
  });
  const exited = new Promise(resolve => {
    child.once('error', () => { ended = true; deliver(failure()); resolve(); });
    // 'exit' may precede the last stdout bytes. Drain the release acknowledgement
    // before deciding whether the helper closed successfully.
    child.once('close', code => { exitCode = code; ended = true; deliver(failure()); resolve(); });
  });
  const abort = () => { child.stdin.end(); deliver(failure()); };
  signal?.addEventListener('abort', abort, { once: true });
  const next = async (command, timeout = Math.max(1, deadline === undefined ? budget : deadline - performance.now())) => {
    if (ended || signal?.aborted) throw failure();
    if (command) child.stdin.write(command + '\n');
    if (buffered.length) return buffered.shift();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { deliver(failure()); child.stdin.end(); }, timeout);
      pending = { resolve, reject, timer };
    });
  };
  // Bounded graceful exit, including failed startup. The helper also has an
  // independent lifetime limit if its parent disappears or stops responding.
  async function close(release) {
    let acknowledged = false;
    try { if (release) acknowledged = await next('release', 5000) === 'released'; }
    finally {
      child.stdin.end(); signal?.removeEventListener('abort', abort);
      let timer;
      try { await Promise.race([exited, new Promise((_, reject) => { timer = setTimeout(() => reject(failure()), 5000); })]); }
      finally { clearTimeout(timer); lines.close(); }
    }
    if (release && (!acknowledged || exitCode !== 0)) throw failure();
  }
  try {
    child.stdin.write(JSON.stringify({ main, wal, budget }) + '\n');
    if (await next() !== 'ready') throw failure();
    return {
      async readWitness() {
        const line = await next('witness');
        if (!/^[A-Za-z0-9+/]{182}==$/.test(line)) throw failure();
        const bytes = Buffer.from(line, 'base64');
        if (bytes.length !== 136) throw failure();
        return bytes;
      },
      release: () => close(true),
    };
  } catch (error) { await close(false); throw error; }
}
