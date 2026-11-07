// Shared Stage 3.5I hardened capture, used by formal acquisition and the PoC.
// Inputs and outputs stay local; diagnostics contain counts and fixed codes only.
import {
  closeSync, existsSync, fstatSync, lstatSync, openSync, readSync, statSync, unlinkSync,
  writeSync, writeFileSync, readFileSync,
} from 'node:fs';
import { createCipheriv, createDecipheriv, createHash, createHmac, pbkdf2Sync, randomBytes } from 'node:crypto';
import { setImmediate as yieldRead } from 'node:timers/promises';

// Capture the module bytes at evaluation, never a database fingerprint.
export const captureCodeHash = createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex');

const PAGE_SIZE = 4096;
const PREFIX_SIZE = 1024;
const RESERVED = 48;
const HEADER = Buffer.from('SQLite format 3\0');
const CUSTOM_HEADER = Buffer.from('SQLite header 3\0');
const MAGIC_LE = 0x377f0682;
const MAGIC_BE = 0x377f0683;

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

function readExactly(fd, length, position) {
  const result = Buffer.alloc(length);
  let read = 0;
  while (read < length) {
    const count = readSync(fd, result, read, length - read, position + read);
    if (count === 0) fail('short_read');
    read += count;
  }
  return result;
}

function writeExactly(fd, buffer) {
  let written = 0;
  while (written < buffer.length) {
    written += writeSync(fd, buffer, written, buffer.length - written);
  }
}

export function fileState(path) {
  try {
    const state = statSync(path, { bigint: true });
    if (state.isFile() !== true ||
        ![state.dev, state.ino, state.size, state.mtimeNs, state.ctimeNs]
          .every(value => typeof value === 'bigint') ||
        state.dev < 0n || state.ino < 0n || state.size < 0n) return null;
    return `${state.dev}:${state.ino}:${state.size}:${state.mtimeNs}:${state.ctimeNs}`;
  } catch {
    return null;
  }
}

function checksum(bytes, littleEndian, initial = [0, 0]) {
  if (bytes.length % 8 !== 0) fail('checksum_input_invalid');
  let [s0, s1] = initial;
  for (let at = 0; at < bytes.length; at += 8) {
    const a = littleEndian ? bytes.readUInt32LE(at) : bytes.readUInt32BE(at);
    const b = littleEndian ? bytes.readUInt32LE(at + 4) : bytes.readUInt32BE(at + 4);
    s0 = (s0 + a + s1) >>> 0;
    s1 = (s1 + b + s0) >>> 0;
  }
  return [s0, s1];
}

function storedChecksum(bytes, offset) {
  return [bytes.readUInt32BE(offset), bytes.readUInt32BE(offset + 4)];
}

function sameChecksum(left, right) {
  return left[0] === right[0] && left[1] === right[1];
}

function putChecksum(bytes, offset, pair) {
  bytes.writeUInt32BE(pair[0], offset);
  bytes.writeUInt32BE(pair[1], offset + 4);
}

function walHeader(header) {
  if (header.length !== 32) fail('wal_header_missing');
  const magic = header.readUInt32BE(0);
  if (magic !== MAGIC_LE && magic !== MAGIC_BE) fail('wal_format_unsupported');
  if (header.readUInt32BE(4) !== 3007000) fail('wal_version_unsupported');
  const pageSize = header.readUInt32BE(8);
  if (pageSize !== PAGE_SIZE) fail('page_size_unsupported');
  const littleEndian = magic === MAGIC_LE;
  const initial = checksum(header.subarray(0, 24), littleEndian);
  if (!sameChecksum(initial, storedChecksum(header, 24))) fail('wal_header_checksum_invalid');
  return { littleEndian, initial, salt: header.subarray(16, 24) };
}

async function readCommittedWal(path, boundary, onPhase = () => {}, retainPages = true, check = () => {}) {
  const fd = openSync(path, 'r');
  try {
    const bytes = fstatSync(fd).size;
    if (bytes < 32) fail('wal_header_missing');
    const header = readExactly(fd, 32, 0);
    const parsed = walHeader(header);
    const frameSize = 24 + PAGE_SIZE;
    const available = boundary ?? Math.floor((bytes - 32) / frameSize);
    if (bytes < 32 + available * frameSize) fail('wal_boundary_truncated');
    const digest = createHash('sha256').update(header);
    const committedPages = new Map();
    const transactionPages = new Map();
    let running = parsed.initial;
    let validFrames = 0;
    let commitMarkers = 0;
    let lastCommitFrame = 0;
    let committedPageCount = 0;
    let lastCommitChecksum = null;
    let transactionPageOneFrame = 0;
    let pageOneFrame = 0;
    // Read only through B, in bounded batches. Per-frame salt/checksum/commit
    // validation is unchanged; avoid one synchronous filesystem call per page.
    let batch = Buffer.alloc(0), batchStart = 0;
    for (let index = 0; index < available; index++) {
      if (index >= batchStart + batch.length / frameSize) {
        await yieldRead(); check();
        batchStart = index;
        batch = readExactly(fd, Math.min(256, available - index) * frameSize, 32 + index * frameSize);
      }
      const frame = batch.subarray((index - batchStart) * frameSize, (index - batchStart + 1) * frameSize);
      const pageNumber = frame.readUInt32BE(0);
      onPhase('wal_frame_read');
      if (pageNumber === 0 || pageNumber > 0xfffffffe) fail('wal_page_number_invalid');
      if (!frame.subarray(8, 16).equals(parsed.salt)) fail('wal_frame_salt_invalid');
      const candidate = checksum(
        Buffer.concat([frame.subarray(0, 8), frame.subarray(24)]),
        parsed.littleEndian,
        running,
      );
      if (!sameChecksum(candidate, storedChecksum(frame, 16))) fail('wal_frame_checksum_invalid');
      digest.update(frame);
      running = candidate;
      validFrames++;
      if (pageNumber === 1) transactionPageOneFrame = validFrames;
      // Own just the retained page, not the whole batch behind a subarray.
      if (retainPages) transactionPages.set(pageNumber, Buffer.from(frame.subarray(24)));
      const sizeAfterCommit = frame.readUInt32BE(4);
      if (sizeAfterCommit !== 0) {
        if (sizeAfterCommit > 10_000_000) fail('committed_page_count_invalid');
        if (retainPages) {
          for (const [number, encrypted] of transactionPages) {
            committedPages.set(number, encrypted);
          }
          for (const number of committedPages.keys()) {
            if (number > sizeAfterCommit) committedPages.delete(number);
          }
          transactionPages.clear();
        }
        commitMarkers++;
        if (transactionPageOneFrame) pageOneFrame = transactionPageOneFrame;
        transactionPageOneFrame = 0;
        lastCommitFrame = validFrames;
        committedPageCount = sizeAfterCommit;
        lastCommitChecksum = Buffer.from(frame.subarray(16, 24));
      }
    }
    if (boundary !== undefined && lastCommitFrame !== boundary) fail('wal_boundary_not_committed');
    if (lastCommitFrame === 0) fail('wal_has_no_complete_transaction');
    return {
      header, salt: parsed.salt, pages: committedPages, validFrames,
      commitMarkers, lastCommitFrame, committedPageCount, pageOneFrame,
      lastCommitChecksum, fingerprint: digest.digest('hex'), identity: identity(fd),
    };
  } finally {
    closeSync(fd);
  }
}

function keyFor(passphrase, salt) {
  return pbkdf2Sync(passphrase, salt, 4000, 32, 'sha512');
}

function decryptPage(encrypted, pageNumber, key, salt) {
  if (encrypted.length !== PAGE_SIZE) fail('encrypted_page_size_invalid');
  const first = pageNumber === 1;
  if (first && !encrypted.subarray(0, 16).equals(salt)) fail('page_one_salt_mismatch');
  const start = first ? 16 : 0;
  const ciphertext = encrypted.subarray(start, PAGE_SIZE - RESERVED);
  const iv = encrypted.subarray(PAGE_SIZE - RESERVED, PAGE_SIZE - RESERVED + 16);
  const decipher = createDecipheriv('aes-256-cbc', key, iv);
  decipher.setAutoPadding(false);
  const body = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  const page = Buffer.alloc(PAGE_SIZE);
  if (first) {
    HEADER.copy(page, 0);
    body.copy(page, 16);
  } else {
    body.copy(page, 0);
  }
  return page;
}

// Windows little-endian WAL-index witness. Both header copies and their
// checksum must agree; missing/torn/unsupported SHM is never an optional guard.
async function checkpointWitness(mainPath, observe = () => {}, readWitness = null) {
  const path = `${mainPath}-shm`;
  if (!existsSync(path)) fail('checkpoint_witness_missing');
  const fd = openSync(path, 'r');
  try {
    const bytes = readWitness ? await readWitness() : readExactly(fd, 136, 0);
    const header = bytes.subarray(0, 48);
    const witness = { boundary: header.readUInt32LE(16), mxFrame: header.readUInt32LE(16),
      nBackfill: bytes.readUInt32LE(96), attempted: bytes.readUInt32LE(128),
      headerCopiesMatch: header.equals(bytes.subarray(48, 96)) };
    observe(witness);
    if (!header.equals(bytes.subarray(48, 96))) fail('checkpoint_witness_torn');
    const expected = checksum(header.subarray(0, 40), true);
    if (header.readUInt32LE(0) !== 3007000 || header[12] !== 1 ||
        header[13] > 1 || header.readUInt16LE(14) !== PAGE_SIZE ||
        expected[0] !== header.readUInt32LE(40) || expected[1] !== header.readUInt32LE(44)) {
      fail('checkpoint_witness_invalid');
    }
    const boundary = header.readUInt32LE(16);
    const backfill = bytes.readUInt32LE(96);
    const attempted = bytes.readUInt32LE(128);
    if (!boundary || backfill > attempted || attempted > boundary) fail('checkpoint_state_invalid');
    const repeated = readWitness ? await readWitness() : readExactly(fd, 136, 0);
    let shmIdentityStable, descriptorIdentity, pathnameIdentity;
    // Preserve the original predicate and its short-circuit order. If identity
    // was skipped, sample it only for diagnostics, without changing rejection.
    const torn = !bytes.subarray(0, 100).equals(repeated.subarray(0, 100)) ||
      attempted !== repeated.readUInt32LE(128) ||
      !(shmIdentityStable = (descriptorIdentity = identity(fd)) === (pathnameIdentity = shmPathIdentity(path)));
    if (shmIdentityStable === undefined) {
      try { shmIdentityStable = (descriptorIdentity = identity(fd)) === (pathnameIdentity = shmPathIdentity(path)); }
      catch { shmIdentityStable = false; }
    }
    // Split only the strings already used by the original guard; no extra stat.
    const identityDiagnostic = {};
    if (typeof descriptorIdentity === 'string' && typeof pathnameIdentity === 'string') {
      const [descriptorDev, descriptorIno] = descriptorIdentity.split(':');
      const [pathnameDev, pathnameIno] = pathnameIdentity.split(':');
      identityDiagnostic.dev_equal = descriptorDev === pathnameDev;
      identityDiagnostic.ino_equal = descriptorIno === pathnameIno;
    }
    observe({ ...witness, ...identityDiagnostic,
      wal_index_headers_stable: bytes.subarray(0, 96).equals(repeated.subarray(0, 96)),
      nbackfill_stable: bytes.subarray(96, 100).equals(repeated.subarray(96, 100)),
      attempted_stable: attempted === repeated.readUInt32LE(128),
      shm_identity_stable: shmIdentityStable });
    if (torn) {
      fail('checkpoint_witness_torn');
    }
    return { boundary, backfill, attempted, salt: Buffer.from(header.subarray(32, 40)),
      pageCount: header.readUInt32LE(20), bigEndian: header[13],
      frameChecksum: [header.readUInt32LE(24), header.readUInt32LE(28)],
      identity: identity(fd), pathIdentity: shmPathIdentity(path) };
  } finally { closeSync(fd); }
}

function identity(fd) {
  const state = fstatSync(fd, { bigint: true });
  return `${state.dev}:${state.ino}`;
}
function shmPathIdentity(path) {
  // Both SHM identities must use fstat: Windows/libuv path stat can report a
  // different volume serial for the same file. Keep dev AND ino protection.
  const fd = openSync(path, 'r');
  try { return identity(fd); }
  finally { closeSync(fd); }
}
function identityPath(path) {
  // WAL path probes must use the same Windows/libuv identity source as reads.
  const fd = openSync(path, 'r');
  try { return identity(fd); }
  finally { closeSync(fd); }
}
function mainPathIdentity(path) {
  // Compare main descriptors using the same Windows/libuv identity source.
  const fd = openSync(path, 'r');
  try { return identity(fd); }
  finally { closeSync(fd); }
}
async function mainFingerprint(fd, check, pageDigests = null) {
  const digest = createHash('sha256');
  const size = fstatSync(fd).size;
  // Align witnesses with the page batches used by the merge. A before/after
  // whole-file digest alone cannot detect changed pages restored mid-capture.
  const prefix = readExactly(fd, Math.min(PREFIX_SIZE, size), 0);
  digest.update(prefix);
  if (pageDigests) pageDigests.set(0, createHash('sha256').update(prefix).digest('hex'));
  for (let offset = PREFIX_SIZE; offset < size; offset += 1024 * 1024) {
    await yieldRead(); check();
    const bytes = readExactly(fd, Math.min(1024 * 1024, size - offset), offset);
    digest.update(bytes);
    if (pageDigests) pageDigests.set(offset, createHash('sha256').update(bytes).digest('hex'));
  }
  return digest.digest('hex');
}
function matchesWitness(wal, witness) {
  if (!wal.salt.equals(witness.salt) ||
      Number(wal.header.readUInt32BE(0) === MAGIC_BE) !== witness.bigEndian ||
      wal.committedPageCount !== witness.pageCount ||
      !sameChecksum(storedChecksum(wal.lastCommitChecksum, 0), witness.frameChecksum)) {
    fail('wal_index_boundary_mismatch');
  }
}

// Only for an owned, completed main+committed-WAL image after decryption.
// Never call on a live source: changing these bytes there could hide its WAL.
export function finalizeStandaloneSnapshot(path, pageCount, onDiagnostic = () => {}) {
  const observe = witness => { try { onDiagnostic(witness); } catch {} };
  const state = lstatSync(path);
  observe({regular_file: state.isFile(), single_link: state.nlink === 1,
    file_bytes: state.size, expected_pages: pageCount, file_size_matches: state.size === pageCount * PAGE_SIZE,
    sidecar_present: ['-wal', '-shm', '-journal'].some(suffix => existsSync(path + suffix))});
  if (!state.isFile() || state.nlink !== 1 ||
      ['-wal', '-shm', '-journal'].some(suffix => existsSync(path + suffix))) {
    fail('plaintext_snapshot_invalid');
  }
  const fd = openSync(path, 'r+');
  try {
    const header = readExactly(fd, 100, 0);
    const fileBytes = fstatSync(fd).size;
    // SQLite 3.54.1 lockBtree() uses a nonzero, counter-valid header size
    // unless it exceeds the Pager size. Preallocated trailing capacity is legal.
    // Zero or stale header sizes fall back to the already verified image size.
    const pageCountValid = header.readUInt32BE(28) === 0 || header.readUInt32BE(24) !== header.readUInt32BE(92) ||
      header.readUInt32BE(28) <= pageCount;
    observe({file_bytes: fileBytes, file_size_matches: fileBytes === pageCount * PAGE_SIZE,
      sqlite_signature_valid: header.subarray(0, 16).equals(HEADER),
      page_size_valid: header.readUInt16BE(16) === PAGE_SIZE,
      format_versions_valid: [1, 2].includes(header[18]) && header[19] === header[18],
      header_pages: header.readUInt32BE(28),
      header_count_nonzero: header.readUInt32BE(28) !== 0,
      header_counters_equal: header.readUInt32BE(24) === header.readUInt32BE(92),
      header_pages_match_commit: header.readUInt32BE(28) === pageCount,
      page_count_valid: pageCountValid});
    if (!Number.isSafeInteger(pageCount) || pageCount < 1 ||
        fileBytes !== pageCount * PAGE_SIZE) {
      fail('plaintext_snapshot_invalid');
    }
    if (!header.subarray(0, 16).equals(HEADER) || header.readUInt16BE(16) !== PAGE_SIZE ||
        ![1, 2].includes(header[18]) || header[19] !== header[18] ||
        !pageCountValid) {
      fail('plaintext_header_invalid');
    }
    // SQLite WAL mode (2/2) can require writable auxiliary files even for
    // mode=ro. All committed pages are already in this image; publish 1/1.
    // Preserve reserve bytes, page count, counters and every b-tree byte.
    const versions = Buffer.from([1, 1]);
    let written = 0;
    while (written < versions.length) {
      written += writeSync(fd, versions, written, versions.length - written, 18 + written);
    }
  } finally { closeSync(fd); }
}

// These two PoC entry points share exactly the same capture/verification window.
export function createMergedEncryptedDatabase(options) {
  return capture(options, false);
}
export function createSnapshot(options) {
  if (!options.passphrase) fail('passphrase_unavailable');
  return capture(options, true);
}
async function capture({ mainPath, walPath, outputPath, passphrase, onPhase = () => {}, onDiagnostic = () => {},
  readWitness = null, signal, deadline = Infinity }, plaintext) {
  const check = () => {
    if (signal?.aborted || performance.now() >= deadline) fail('capture_cancelled');
  };
  check();
  // Observability is best effort and cannot influence capture or its guards.
  const observe = (failureStage, witness = {}) => {
    try { onDiagnostic({ failureStage, ...witness }); } catch {}
  };
  observe('shm_witness');
  const before = await checkpointWitness(mainPath, witness => observe('shm_witness', witness), readWitness);
  observe('main_identity');
  const mainFd = openSync(mainPath, 'r');
  let outputFd = null;
  let ownedOutput = false;
  let published = false;
  try {
    const initialMainState = fileState(mainPath);
    if (initialMainState === null) fail('main_changed_during_read');
    if (identity(mainFd) !== mainPathIdentity(mainPath)) fail('main_replaced');
    observe('wal_identity');
    const walIdentity = identityPath(walPath);
    observe('main_stability');
    // Only the formal source-lock path may accept metadata-only changes.
    // Witness every main batch consumed by the merge before permitting that.
    const mainPageDigests = readWitness ? new Map() : null;
    const initialDigest = await mainFingerprint(mainFd, check, mainPageDigests);
    onPhase('main_baseline_read');
    observe('wal_prefix');
    const wal = await readCommittedWal(walPath, before.boundary, onPhase, true, check);
    if (wal.identity !== walIdentity) fail('wal_replaced');
    matchesWitness(wal, before);
    // Freeze the observed committed index. Later commits cannot move this end mark.
    onPhase('boundary_selected');
    observe('main_identity');
    const prefix = readExactly(mainFd, PREFIX_SIZE, 0);
    if (mainPageDigests && mainPageDigests.get(0) !== createHash('sha256').update(prefix).digest('hex')) {
      fail('main_changed_during_read');
    }
    if (!prefix.subarray(0, 16).equals(CUSTOM_HEADER)) fail('main_format_unsupported');
    const mainBytes = fstatSync(mainFd).size;
    if ((mainBytes - PREFIX_SIZE) % PAGE_SIZE !== 0) fail('main_size_invalid');
    const mainPages = (mainBytes - PREFIX_SIZE) / PAGE_SIZE;
    if (mainPages < 1) fail('main_empty');
    const salt = readExactly(mainFd, 16, PREFIX_SIZE);
    const key = plaintext ? keyFor(passphrase, salt) : null;
    const pageCount = wal.committedPageCount;
    if (pageCount < 1 || pageCount > 10_000_000) fail('committed_page_count_invalid');
    observe('commit_boundary', { selected_commit_frame: wal.lastCommitFrame, committed_pages: pageCount,
      shm_pages: before.pageCount, main_pages: mainPages,
      page_one_source: wal.pages.has(1) ? 'wal' : 'main',
      page_one_in_committed_wal: wal.pages.has(1), page_one_frame: wal.pageOneFrame });
    let selectedPageOne;
    outputFd = openSync(outputPath, 'wx', 0o600);
    ownedOutput = true;
    if (!plaintext) writeExactly(outputFd, prefix);
    let mainBatch = Buffer.alloc(0), mainBatchStart = 0;
    const outputBatch = Buffer.alloc(256 * PAGE_SIZE);
    let outputPages = 0;
    for (let number = 1; number <= pageCount; number++) {
      if ((number - 1) % 256 === 0) { await yieldRead(); check(); }
      if (!wal.pages.has(number) && number <= mainPages &&
          (number < mainBatchStart || number >= mainBatchStart + mainBatch.length / PAGE_SIZE)) {
        mainBatchStart = 1 + Math.floor((number - 1) / 256) * 256;
        const batchOffset = PREFIX_SIZE + (mainBatchStart - 1) * PAGE_SIZE;
        mainBatch = readExactly(mainFd, Math.min(256, mainPages - mainBatchStart + 1) * PAGE_SIZE, batchOffset);
        if (mainPageDigests && mainPageDigests.get(batchOffset) !==
            createHash('sha256').update(mainBatch).digest('hex')) fail('main_changed_during_read');
      }
      const encrypted = wal.pages.get(number) ??
        (number <= mainPages
          ? mainBatch.subarray((number - mainBatchStart) * PAGE_SIZE, (number - mainBatchStart + 1) * PAGE_SIZE)
          : null);
      if (!encrypted) fail('new_page_missing_from_wal');
      if (number === 1 && !encrypted.subarray(0, 16).equals(salt)) fail('page_one_salt_mismatch');
      if (number === 1) selectedPageOne = encrypted;
      (plaintext ? decryptPage(encrypted, number, key, salt) : encrypted).copy(outputBatch, outputPages * PAGE_SIZE);
      outputPages++;
      if (outputPages === 256 || number === pageCount) {
        writeExactly(outputFd, outputBatch.subarray(0, outputPages * PAGE_SIZE));
        for (let index = 0; index < outputPages; index++) onPhase('output_page_written');
        outputPages = 0;
      }
    }
    closeSync(outputFd);
    outputFd = null;
    onPhase('output_complete');
    if (!plaintext) {
      // Best-effort comparison of owned bytes only. Do not log either page or
      // a data fingerprint, or change the existing capture acceptance guards.
      try {
        const fd = openSync(outputPath, 'r');
        try { observe('commit_boundary', {
          merged_page_one_matches_selected: readExactly(fd, PAGE_SIZE, PREFIX_SIZE).equals(selectedPageOne),
        }); } finally { closeSync(fd); }
      } catch { /* Missing diagnostic evidence is not a successful comparison. */ }
    }
    observe('main_stability');
    const finalMainState = fileState(mainPath);
    const metadataStable = initialMainState === finalMainState;
    observe('main_stability', { main_metadata_stable: metadataStable });
    // Windows can publish file-time changes after the bytes have settled. Keep
    // identity/size, full before/after content, verified merge batches, source
    // locks and the complete WAL/SHM checks; never waive a content change.
    const sameObjectSize = finalMainState !== null &&
      initialMainState.split(':').slice(0, 3).join(':') === finalMainState.split(':').slice(0, 3).join(':');
    if ((!metadataStable && (!mainPageDigests || !sameObjectSize)) || identity(mainFd) !== mainPathIdentity(mainPath) ||
        initialDigest !== await mainFingerprint(mainFd, check)) fail('main_changed_during_read');
    // Revalidate every byte and every rolling checksum through the frozen end
    // mark, not just the eight stored bytes on the last frame.
    observe('wal_prefix');
    // Verification still re-reads every byte and checksum, but needs no second
    // copy of all committed pages while the first copy is still retained.
    const latest = await readCommittedWal(walPath, before.boundary, undefined, false, check);
    if (walIdentity !== identityPath(walPath) || latest.identity !== walIdentity || !latest.header.equals(wal.header) ||
        latest.fingerprint !== wal.fingerprint) fail('wal_boundary_changed');
    observe('checkpoint_witness');
    const after = await checkpointWitness(mainPath, witness => observe('checkpoint_witness', witness), readWitness);
    if (before.identity !== after.identity || before.pathIdentity !== after.pathIdentity ||
        !before.salt.equals(after.salt) || after.boundary < before.boundary ||
        after.attempted !== before.attempted || after.backfill !== before.backfill) {
      fail('checkpoint_state_changed');
    }
    // If no append occurred, the indexed commit itself must remain identical.
    if (after.boundary === before.boundary) matchesWitness(latest, after);
    check();
    if (plaintext) finalizeStandaloneSnapshot(outputPath, pageCount);
    published = true;
    return {
      pages: pageCount, validFrames: wal.validFrames, commitMarkers: wal.commitMarkers,
      selectedCommitFrame: wal.lastCommitFrame, mainChangedDuringRead: false,
      checkpointAttemptedFrame: after.attempted,
      sourceState: {
        before: { committedFrame: before.boundary, backfill: before.backfill, attempted: before.attempted },
        after: { committedFrame: after.boundary, backfill: after.backfill, attempted: after.attempted },
        mainIdentityStable: true, mainContentStable: true, walGenerationStable: true,
        walPrefixStable: true, checkpointWitnessStable: true,
      },
    };
  } finally {
    // Only unlink files successfully created by this invocation. In particular,
    // an exclusive-open collision must not delete another invocation's file.
    try { if (outputFd !== null) closeSync(outputFd); }
    finally {
      try { closeSync(mainFd); }
      finally { if (!published && ownedOutput) unlinkSync(outputPath); }
    }
  }
}

function encryptPage(plain, number, key, hmacKey, salt) {
  if (plain.length !== PAGE_SIZE) fail('fixture_page_size_invalid');
  const first = number === 1;
  const iv = randomBytes(16);
  const cipher = createCipheriv('aes-256-cbc', key, iv);
  cipher.setAutoPadding(false);
  const body = plain.subarray(first ? 16 : 0, PAGE_SIZE - RESERVED);
  const ciphertext = Buffer.concat([cipher.update(body), cipher.final()]);
  const tag = createHmac('sha512', hmacKey)
    .update(ciphertext).update(Buffer.from([number & 255])).digest().subarray(0, 32);
  return Buffer.concat([first ? salt : Buffer.alloc(0), ciphertext, iv, tag]);
}

// Build a completely fictional encrypted fixture, preserving SQLite WAL framing.
export async function encryptFixture({ plainMainPath, plainWalPath, encryptedMainPath, encryptedWalPath, passphrase }) {
  const salt = randomBytes(16);
  const key = keyFor(passphrase, salt);
  const hmacKey = randomBytes(32);
  const mainFd = openSync(plainMainPath, 'r');
  const outMainFd = openSync(encryptedMainPath, 'wx', 0o600);
  try {
    const bytes = fstatSync(mainFd).size;
    if (bytes % PAGE_SIZE !== 0 || bytes === 0) fail('fixture_main_size_invalid');
    const prefix = Buffer.alloc(PREFIX_SIZE);
    CUSTOM_HEADER.copy(prefix);
    writeExactly(outMainFd, prefix);
    for (let number = 1; number <= bytes / PAGE_SIZE; number++) {
      const plain = readExactly(mainFd, PAGE_SIZE, (number - 1) * PAGE_SIZE);
      writeExactly(outMainFd, encryptPage(plain, number, key, hmacKey, salt));
    }
  } finally {
    closeSync(mainFd);
    closeSync(outMainFd);
  }
  const walFd = openSync(plainWalPath, 'r');
  const outWalFd = openSync(encryptedWalPath, 'wx', 0o600);
  try {
    const header = readExactly(walFd, 32, 0);
    const parsed = walHeader(header);
    writeExactly(outWalFd, header);
    let running = parsed.initial;
    const frames = Math.floor((fstatSync(walFd).size - 32) / (24 + PAGE_SIZE));
    for (let index = 0; index < frames; index++) {
      const frame = readExactly(walFd, 24 + PAGE_SIZE, 32 + index * (24 + PAGE_SIZE));
      const number = frame.readUInt32BE(0);
      const encrypted = encryptPage(frame.subarray(24), number, key, hmacKey, salt);
      running = checksum(Buffer.concat([frame.subarray(0, 8), encrypted]), parsed.littleEndian, running);
      putChecksum(frame, 16, running);
      writeExactly(outWalFd, frame.subarray(0, 24));
      writeExactly(outWalFd, encrypted);
    }
  } finally {
    closeSync(walFd);
    closeSync(outWalFd);
  }
  const wal = await readCommittedWal(encryptedWalPath);
  const index = Buffer.alloc(136);
  index.writeUInt32LE(3007000, 0);
  index[12] = 1;
  index[13] = Number(wal.header.readUInt32BE(0) === MAGIC_BE);
  index.writeUInt16LE(PAGE_SIZE, 14);
  index.writeUInt32LE(wal.lastCommitFrame, 16);
  index.writeUInt32LE(wal.committedPageCount, 20);
  const frameSum = storedChecksum(wal.lastCommitChecksum, 0);
  index.writeUInt32LE(frameSum[0], 24);
  index.writeUInt32LE(frameSum[1], 28);
  wal.salt.copy(index, 32);
  const headerSum = checksum(index.subarray(0, 40), true);
  index.writeUInt32LE(headerSum[0], 40);
  index.writeUInt32LE(headerSum[1], 44);
  index.copy(index, 48, 0, 48);
  writeFileSync(`${encryptedMainPath}-shm`, index, { flag: 'wx', mode: 0o600 });
}
