// Standalone PoC CLI; capture is shared with formal acquisition.
import { createMergedEncryptedDatabase, createSnapshot, encryptFixture } from './main_wal.mjs';
export { createMergedEncryptedDatabase, createSnapshot, encryptFixture } from './main_wal.mjs';
function fail(code) { const error = new Error(code); error.code = code; throw error; }
if (process.argv[1]?.endsWith('main_wal_poc.mjs')) {
  const [mode, ...paths] = process.argv.slice(2);
  const passphrase = process.env.ECHO_POC_PASSPHRASE;
  try {
    if (mode === 'merge' && paths.length === 3) {
      const result = await createMergedEncryptedDatabase({ mainPath: paths[0], walPath: paths[1], outputPath: paths[2] });
      console.log(JSON.stringify({ ok: true, ...result }));
    } else if (mode === 'snapshot' && paths.length === 3) {
      const result = await createSnapshot({ mainPath: paths[0], walPath: paths[1], outputPath: paths[2], passphrase });
      console.log(JSON.stringify({ ok: true, ...result }));
    } else if (mode === 'fixture' && paths.length === 4) {
      await encryptFixture({ plainMainPath: paths[0], plainWalPath: paths[1], encryptedMainPath: paths[2], encryptedWalPath: paths[3], passphrase });
      console.log(JSON.stringify({ ok: true, fixture: true }));
    } else {
      fail('arguments_invalid');
    }
  } catch (error) {
    console.log(JSON.stringify({ ok: false, code: error?.code ?? 'poc_failed' }));
    process.exitCode = 1;
  }
}
