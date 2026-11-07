import {createServer} from 'node:http';
import {randomBytes, timingSafeEqual} from 'node:crypto';
import {appendFileSync, constants as fsConstants, promises as fs} from 'node:fs';
import {isAbsolute} from 'node:path';

const MAX_REQUEST_BYTES = 4096;
const CREDENTIAL = /^[0-9a-f]{64}$/;
const BEARER = 'Bearer ';
const text = value => typeof value === 'string' ? value : '';
const identifier = value => typeof value === 'string' ? value : Number.isSafeInteger(value) ? String(value) : '';
const uin = value => /^[1-9][0-9]{4,19}$/.test(identifier(value)) ? identifier(value) : '';
const object = value => value !== null && typeof value === 'object';
const generation = value => typeof value === 'string' && value.length > 0 && value.length <= 128 &&
  value === value.trim() && value !== '.' && value !== '..' && !/[\\/\x00]/.test(value);
// Internal literal stages/outcomes only; stdout/stderr are intentionally not copied into user logs.
const logSnapshot = (stage, outcome) => {
  try {
    const file = process.env.QCE_LOG_FILE;
    if (file) appendFileSync(file, `[${new Date().toISOString()}] [echo-snapshot] stage=${stage} outcome=${outcome}\n`);
  } catch {}
};
const MEDIA_CALL_BURST = 64;
const MEDIA_CALL_INTERVAL_MS = 125;
// Independent bridge-owned token buckets for probes and downloads. Smooth
// refill avoids minute-long pauses in serial multi-member report queues;
// bursts stay bounded, with no reset RPC or caller-supplied allowance.
const mediaCallBudget = () => {
  // Keep credits in milliseconds to avoid fractional-token rounding at refill.
  const capacity = MEDIA_CALL_BURST * MEDIA_CALL_INTERVAL_MS;
  let creditMs = capacity;
  let updatedAt = performance.now();
  const refill = () => {
    const now = performance.now();
    creditMs = Math.min(capacity, creditMs + now - updatedAt);
    updatedAt = now;
  };
  const allow = () => {
    refill();
    if (creditMs < MEDIA_CALL_INTERVAL_MS) return false;
    creditMs -= MEDIA_CALL_INTERVAL_MS;
    return true;
  };
  allow.retryAfter = () => {
    refill();
    return Math.max(0, Math.ceil(MEDIA_CALL_INTERVAL_MS - creditMs));
  };
  return allow;
};
const PROBE_TIMEOUT_MS = 8000;
const DECIMAL_ID = /^[1-9][0-9]{0,19}$/;
const PEER_UID = /^u_[A-Za-z0-9_-]{10,64}$/;
const MD5 = /^[0-9a-f]{32}$/i;
const FILE_UUID = /^[A-Za-z0-9_.:+=\/-]{1,128}$/;
const PROBE_KEYS = new Set([
  'msgId', 'chatType', 'peerUid', 'expectedTime', 'expectedMd5',
  'expectedFileUuid', 'expectedSegmentId', 'expectedSelf',
]);
const GROUP_PROBE_KEYS = new Set([...PROBE_KEYS, 'expectedSenderUin', 'expectedSenderUid']);
const bounded = (promise, ms, timeoutError = () => new Error('probe_timeout')) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(timeoutError()), ms);
  Promise.resolve(promise).then(
    value => { clearTimeout(timer); resolve(value); },
    error => { clearTimeout(timer); reject(error); },
  );
});
// One fixed read-only request shape. It carries no path, URL, account or
// arbitrary method name, and cannot be widened by callers.
const probeRequest = value => {
  if (!object(value) || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  const allowed = value.chatType === 2 ? GROUP_PROBE_KEYS : PROBE_KEYS;
  if (keys.length !== allowed.size || !keys.every(key => allowed.has(key))) return false;
  if (typeof value.msgId !== 'string' || !DECIMAL_ID.test(value.msgId)) return false;
  if (value.chatType !== 1 && value.chatType !== 2) return false;
  if (typeof value.peerUid !== 'string') return false;
  if (value.chatType === 1 ? !PEER_UID.test(value.peerUid) : !uin(value.peerUid)) return false;
  if (value.chatType === 2 && (
      typeof value.expectedSenderUin !== 'string' || !uin(value.expectedSenderUin) ||
      (value.expectedSenderUid !== null && (typeof value.expectedSenderUid !== 'string' ||
        !PEER_UID.test(value.expectedSenderUid))) ||
      value.expectedFileUuid === null || value.expectedSegmentId === null)) return false;
  if (!Number.isSafeInteger(value.expectedTime) ||
      value.expectedTime < 1 || value.expectedTime > 99999999999) return false;
  if (typeof value.expectedMd5 !== 'string' || !MD5.test(value.expectedMd5)) return false;
  if (value.expectedFileUuid !== null && (
      typeof value.expectedFileUuid !== 'string' ||
      !FILE_UUID.test(value.expectedFileUuid) ||
      value.expectedFileUuid.includes('..') ||
      value.expectedFileUuid.startsWith('/'))) return false;
  if (value.expectedSegmentId !== null &&
      (typeof value.expectedSegmentId !== 'string' || !DECIMAL_ID.test(value.expectedSegmentId))) return false;
  return value.expectedSelf === null || typeof value.expectedSelf === 'boolean';
};

// Serial, rate-bounded fetches. One authenticated, generation-bound transfer
// owns one pinned file handle; only a small chunk is base64 encoded at a time.
const MAX_MEDIA_BYTES = 32 * 1024 * 1024;
const MEDIA_CHUNK_BYTES = 256 * 1024;
const MEDIA_TRANSFER_IDLE_MS = 30000;
const MEDIA_TIMEOUT_MS = 120000;
// Fixed, sanitised stage codes. Only these may cross the bridge; anything else
// is reported as the generic operation failure so no path or kernel message leaks.
const MEDIA_STAGES = new Set([
  'probe_timeout', 'probe_api_unavailable',
  'media_mapping_rejected', 'media_message_query_failed', 'media_api_unavailable',
  'media_busy', 'media_budget_exhausted', 'media_download_timeout',
  'media_download_failed', 'media_download_no_path', 'media_download_no_cache_hint', 'media_path_type_invalid',
  'media_path_not_absolute', 'media_path_invalid', 'media_file_missing',
  'media_open_failed', 'media_file_type_rejected', 'media_file_identity_changed',
  'media_file_empty', 'media_file_too_large', 'media_read_incomplete',
  'media_file_changed', 'media_file_rejected', 'media_transfer_invalid',
]);
const mediaError = code => Object.assign(new Error(code), {code});
// Use the existing child-only diagnostic channel. Native messages, paths,
// identities and arbitrary fields never reach it; logging is best effort.
const logNativeMedia = (stage, value) => {
  try {
    const logFile = process.env.QCE_LOG_FILE;
    if (!logFile) return;
    const numericCode = raw => {
      const code = typeof raw === 'string' && /^-?(?:0|[1-9]\d{0,9})$/.test(raw) ? Number(raw) : raw;
      return Number.isSafeInteger(code) && code >= -2147483648 && code <= 4294967295 ? code : null;
    };
    let fields = '';
    const returned = numericCode(value);
    if (returned !== null) fields += ` return_code=${returned}`;
    for (const key of ['result', 'errCode', 'retCode', 'resultCode', 'errorCode', 'status',
      'fileErrCode', 'fileSrvErrCode', 'trasferStatus', 'step', 'fileDownType', 'thumbSize',
      'source_state', 'file_state', 'thumb_count', 'thumb_regular', 'thumb_missing', 'thumb_invalid', 'thumb_other']) {
      const code = numericCode(value?.[key]);
      if (code !== null) fields += ` ${key}=${code}`;
    }
    const present = typeof value?.filePath === 'string' && value.filePath.length > 0;
    appendFileSync(logFile, `[${new Date().toISOString()}] [echo-media] stage=${stage}${fields} path_present=${Number(present)}\n`);
  } catch { /* Diagnostics cannot change acquisition behavior. */ }
};
// Inspect only paths supplied by the identity-confirmed PIC, never a directory.
// States: 0 absent, 1 malformed, 2 missing, 3 regular file, 4 other type, 5 inaccessible.
const logMediaCache = async pic => {
  if (!process.env.QCE_LOG_FILE) return;
  try {
    const state = async path => {
      if (path == null || path === '') return 0;
      try { validateMediaPath(path); } catch { return 1; }
      try { return (await fs.lstat(path)).isFile() ? 3 : 4; }
      catch (error) { return error?.code === 'ENOENT' ? 2 : 5; }
    };
    const fields = {source_state: await state(pic.sourcePath), file_state: await state(pic.filePath),
      thumb_count: 0, thumb_regular: 0, thumb_missing: 0, thumb_invalid: 0, thumb_other: 0};
    if (pic.thumbPath instanceof Map) {
      for (const path of pic.thumbPath.values()) {
        if (fields.thumb_count >= 8) break;
        fields.thumb_count++;
        const kind = await state(path);
        fields[kind === 3 ? 'thumb_regular' : kind === 2 ? 'thumb_missing' :
          kind === 0 || kind === 1 ? 'thumb_invalid' : 'thumb_other']++;
      }
    }
    logNativeMedia('cache_location', fields);
  } catch { /* Cache diagnostics cannot change acquisition behavior. */ }
};
const observedMediaApi = fileApi => {
  const wrapper = fileApi.core?.eventWrapper;
  if (!process.env.QCE_LOG_FILE || typeof wrapper?.callNormalEventV2 !== 'function') return fileApi;
  // The pinned FileApi drops the native result and returns only filePath.
  // Observe that call on a per-download receiver; never patch the shared API
  // or event wrapper used by QQ and other plugins.
  const observer = Object.create(wrapper);
  observer.callNormalEventV2 = async (...args) => {
    const media = args[0] === 'NodeIKernelMsgService/downloadRichMedia' &&
      args[1] === 'NodeIKernelMsgListener/onRichMediaDownloadComplete';
    if (media) {
      const check = args[3];
      args[3] = value => { logNativeMedia('native_request', value); return check(value); };
    }
    const result = await wrapper.callNormalEventV2(...args);
    if (media) logNativeMedia('native_complete', result?.[1]);
    return result;
  };
  const core = Object.create(fileApi.core);
  core.eventWrapper = observer;
  const api = Object.create(fileApi);
  api.core = core;
  return api;
};
const MEDIA_KEYS = new Set([...PROBE_KEYS].filter(key => key !== 'expectedSegmentId').concat('elementId'));
const GROUP_MEDIA_KEYS = new Set([...MEDIA_KEYS, 'expectedSenderUin', 'expectedSenderUid']);
// Same fixed request shape as the probe, with the verified element ID in the
// place of the DB segment ID; both identify the same PIC element.
const mediaRequest = value => {
  if (!object(value) || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  const allowed = value.chatType === 2 ? GROUP_MEDIA_KEYS : MEDIA_KEYS;
  if (keys.length !== allowed.size || !keys.every(key => allowed.has(key))) return false;
  if (typeof value.elementId !== 'string' || !DECIMAL_ID.test(value.elementId)) return false;
  return probeRequest({
    msgId: value.msgId, chatType: value.chatType, peerUid: value.peerUid,
    expectedTime: value.expectedTime, expectedMd5: value.expectedMd5,
    expectedFileUuid: value.expectedFileUuid, expectedSegmentId: value.elementId,
    expectedSelf: value.expectedSelf,
    ...(value.chatType === 2 ? {expectedSenderUin: value.expectedSenderUin,
      expectedSenderUid: value.expectedSenderUid} : {}),
  });
};
// NapCat returns a local cache path. Treat it as opaque: absolute, regular,
// stable and bounded. The path never leaves this process.
//
// The opened handle is the single source of truth for the bytes returned. The
// path lookup is only used to reject links/special files and to prove the path
// still names the file that was opened; its size is never compared with the
// handle size, because QQ can still be finishing the write it just reported.
// Stability is proven around the read on that same handle instead.
const validateMediaPath = filePath => {
  if (filePath == null || filePath === '') throw mediaError('media_download_no_path');
  if (typeof filePath !== 'string') throw mediaError('media_path_type_invalid');
  if (filePath.length > 4096 || filePath.includes('\x00')) throw mediaError('media_path_invalid');
  if (!isAbsolute(filePath)) throw mediaError('media_path_not_absolute');
};
const openBoundedMedia = async filePath => {
  validateMediaPath(filePath);
  const openFailed = error => mediaError(error?.code === 'ENOENT' ? 'media_file_missing' : 'media_open_failed');
  let before;
  try {
    before = await fs.lstat(filePath, {bigint: true});
  } catch (error) {
    throw openFailed(error);
  }
  if (!before.isFile()) throw mediaError('media_file_type_rejected');
  let handle;
  try {
    handle = await fs.open(filePath, fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0));
  } catch (error) {
    throw openFailed(error);
  }
  let retained = false;
  try {
    let opened;
    try {
      opened = await handle.stat({bigint: true});
    } catch (error) {
      throw openFailed(error);
    }
    if (!opened.isFile()) throw mediaError('media_file_type_rejected');
    // Windows Node versions can report different dev values for lstat/fstat.
    // Keep exact, nonzero file IDs across APIs, and compare dev only within
    // the same API on Windows. Other platforms also require cross-API dev.
    if (before.ino <= 0n || opened.ino !== before.ino ||
        (process.platform !== 'win32' && opened.dev !== before.dev)) {
      throw mediaError('media_file_identity_changed');
    }
    const checkPath = async () => {
      const named = await fs.lstat(filePath, {bigint: true});
      if (!named.isFile()) throw mediaError('media_file_type_rejected');
      if (named.ino !== before.ino || named.dev !== before.dev) {
        throw mediaError('media_file_identity_changed');
      }
    };
    await checkPath();
    if (opened.size <= 0n) throw mediaError('media_file_empty');
    if (opened.size > BigInt(MAX_MEDIA_BYTES)) throw mediaError('media_file_too_large');
    let position = 0;
    const size = Number(opened.size);
    retained = true;
    return {size, close: () => handle.close(), async read(offset) {
      if (offset !== position || position >= size) throw mediaError('media_transfer_invalid');
      const raw = Buffer.alloc(Math.min(MEDIA_CHUNK_BYTES, size - position));
      let filled = 0;
      while (filled < raw.length) {
        const {bytesRead} = await handle.read(raw, filled, raw.length - filled, position + filled);
        if (bytesRead <= 0) throw mediaError('media_read_incomplete');
        filled += bytesRead;
      }
      position += filled;
      const eof = position === size;
      if (eof) {
        const tail = Buffer.alloc(1);
        if ((await handle.read(tail, 0, 1, position)).bytesRead !== 0) throw mediaError('media_file_changed');
        await checkPath();
        const after = await handle.stat({bigint: true});
        if (!after.isFile() || after.ino !== opened.ino || after.dev !== opened.dev ||
            after.size !== opened.size || after.mtimeNs !== opened.mtimeNs ||
            after.ctimeNs !== opened.ctimeNs) throw mediaError('media_file_changed');
      }
      return {offset, base64: raw.toString('base64'), eof};
    }};
  } catch (error) {
    throw MEDIA_STAGES.has(error?.code) ? error : mediaError('media_file_rejected');
  } finally {
    if (!retained) await handle.close();
  }
};

export async function startBridge(core, snapshot, {port = 40655, host = '127.0.0.1', runtimeId = null, token = null} = {}) {
  if (host !== '127.0.0.1' || !Number.isInteger(port) || port < 0 || port > 65535) throw new Error('invalid_bridge_address');
  if (!core?.apis) throw new Error('echo_core_unavailable');
  if (runtimeId !== null && (typeof runtimeId !== 'string' || !/^[0-9a-f]{64}$/.test(runtimeId))) {
    throw new Error('invalid_runtime_identity');
  }
  // Every RPC method requires the client credential Echo minted for this launch.
  // A bridge without a usable credential is never started: there is no mode in
  // which this server answers an unauthenticated call.
  if (typeof token !== 'string' || !CREDENTIAL.test(token)) throw new Error('invalid_bridge_credential');
  // Mint inside the actual bridge lifecycle, never in the parent environment:
  // a restarted Worker (or a reloaded bridge) cannot inherit the old generation.
  const bootId = randomBytes(32).toString('hex');
  const expected = Buffer.from(token, 'utf8');
  const authorized = req => {
    const header = req.headers.authorization;
    if (typeof header !== 'string' || !header.startsWith(BEARER)) return false;
    const presented = Buffer.from(header.slice(BEARER.length), 'utf8');
    return presented.length === expected.length && timingSafeEqual(presented, expected);
  };
  const status = () => ({
    rpc_auth: 'bearer-v1',
    boot_id: bootId,
    bridge_ready: true,
    media_busy: mediaBusy || transfer !== null,
    media_retry_after_ms: Math.max(allowProbe.retryAfter(), allowMedia.retryAfter()),
    qq_online: core.selfInfo?.online === true,
    self_info: {uin: uin(core.selfInfo?.uin), uid: text(core.selfInfo?.uid), nickname: text(core.selfInfo?.nick)},
    database_api_ready: typeof core.apis.DatabaseApi?.decryptDatabase === 'function',
    passphrase_ready: core.apis.DatabaseApi?.hasPassphrase?.() === true,
    snapshot_api_ready: ['acquire', 'cleanup', 'recover'].every(name => typeof snapshot?.[name] === 'function'),
    ...(runtimeId === null ? {} : {runtime_id: runtimeId})
  });
  const friends = async () => {
    const categories = await core.apis.FriendApi.getBuddyV2ExWithCate();
    if (!Array.isArray(categories)) throw new Error('invalid_metadata');
    return categories.flatMap(category => Array.isArray(category?.buddyList) ? category.buddyList : [])
      .filter(object).map(row => ({uin: uin(row.coreInfo?.uin) || uin(row.uin), uid: text(row.coreInfo?.uid) || text(row.uid),
        nickname: text(row.coreInfo?.nick) || text(row.nick), remark: text(row.coreInfo?.remark) || text(row.remark)}))
      .filter(row => row.uin || row.uid);
  };
  const groups = async () => {
    const rows = await core.apis.GroupApi.getGroups();
    if (!Array.isArray(rows)) throw new Error('invalid_metadata');
    return rows.filter(object).map(row => ({group_code: uin(row.groupCode), group_name: text(row.groupName),
      member_count: Number.isSafeInteger(row.memberCount) && row.memberCount >= 0 ? row.memberCount : null}))
      .filter(row => row.group_code);
  };
  const members = async group => {
    const result = await core.apis.GroupApi.getGroupMemberAll(group);
    const source = result?.result?.infos;
    if (!object(source)) throw new Error('invalid_metadata');
    const entries = source instanceof Map ? [...source.entries()] : Object.entries(source);
    // Preserve Map keys (UID) separately from UIN; only name-enrichment fields leave NapCat.
    const infos = Object.fromEntries(entries.filter(([, row]) => object(row)).map(([key, row]) => [text(key), {
      uid: text(row.uid) || text(key), uin: uin(row.uin), nick: text(row.nick), cardName: text(row.cardName)
    }]));
    return {result: {infos}};
  };
  // One shared read-only locator for the diagnostic probe and the media fetch.
  // It fetches exactly the requested message and returns only the fields each
  // caller may use; the caller never supplies an element independently.
  const inspectPicMessage = async value => {
    const msgApi = core.apis?.MsgApi;
    if (!msgApi || typeof msgApi.getMsgsByMsgId !== 'function') throw mediaError('probe_api_unavailable');
    const peer = {guildId: '', chatType: value.chatType, peerUid: value.peerUid};
    const response = await bounded(msgApi.getMsgsByMsgId(peer, [value.msgId]),
      PROBE_TIMEOUT_MS, () => mediaError('probe_timeout'));
    const messages = Array.isArray(response?.msgList) ? response.msgList : [];
    const empty = {
      found: messages.length > 0, unique: false, msg_id_matches: false,
      time_matches: false, peer_matches: false, sender_matches: null,
      pic_element_count: 0, matched_pic_count: 0, segment_id_matches: null,
      file_uuid_matches: null, md5_matches: false, match_basis: 'none', element: null,
    };
    if (messages.length !== 1) return empty;
    const message = messages[0];
    const msgIdMatches = text(message?.msgId) === value.msgId;
    const timeMatches = String(message?.msgTime ?? '') === String(value.expectedTime);
    const peerMatches = text(message?.peerUid) === value.peerUid &&
      (value.chatType !== 2 || (message.chatType === 2 &&
        (message.peerUin === undefined || message.peerUin === '' || text(message.peerUin) === value.peerUid)));
    const senderUid = text(message?.senderUid);
    let senderMatches = null;
    if (value.chatType === 2) {
      senderMatches = text(message.senderUin) === value.expectedSenderUin &&
        (value.expectedSenderUid === null || senderUid === value.expectedSenderUid);
      if (typeof value.expectedSelf === 'boolean') {
        const selfUin = uin(core.selfInfo?.uin);
        senderMatches = senderMatches && selfUin !== '' &&
          ((value.expectedSenderUin === selfUin) === value.expectedSelf);
        const selfUid = text(core.selfInfo?.uid);
        if (selfUid !== '') senderMatches = senderMatches && ((senderUid === selfUid) === value.expectedSelf);
      }
    } else if (typeof value.expectedSelf === 'boolean') {
      senderMatches = value.expectedSelf
        ? (text(core.selfInfo?.uid) !== '' && senderUid === text(core.selfInfo?.uid))
        : senderUid === value.peerUid;
    }
    const elements = Array.isArray(message?.elements) ? message.elements : [];
    const pics = elements.filter(element => object(element) && object(element.picElement) &&
      (value.chatType !== 2 || element.elementType === 2));
    const expectedMd5 = value.expectedMd5.toLowerCase();
    const candidates = pics.filter(element => {
      const pic = element.picElement;
      if (text(pic.md5HexStr).toLowerCase() !== expectedMd5) return false;
      return value.expectedFileUuid === null || text(pic.fileUuid) === value.expectedFileUuid;
    });
    const matched = candidates.length === 1 ? candidates[0] : null;
    const segmentId = value.expectedSegmentId ?? value.elementId ?? null;
    const segmentIdMatches = matched !== null && segmentId !== null
      ? text(matched.elementId) === segmentId : null;
    const fileUuidMatches = matched !== null && value.expectedFileUuid !== null
      ? text(matched.picElement?.fileUuid) === value.expectedFileUuid : null;
    const md5Matches = matched !== null &&
      text(matched.picElement?.md5HexStr).toLowerCase() === expectedMd5;
    const bases = [];
    if (segmentIdMatches === true) bases.push('segment_id');
    if (fileUuidMatches === true) bases.push('file_uuid');
    if (md5Matches) bases.push('md5');
    return {
      found: true, unique: true, msg_id_matches: msgIdMatches,
      time_matches: timeMatches, peer_matches: peerMatches,
      sender_matches: senderMatches, pic_element_count: pics.length,
      matched_pic_count: candidates.length, segment_id_matches: segmentIdMatches,
      file_uuid_matches: fileUuidMatches, md5_matches: md5Matches,
      match_basis: bases.length ? bases.join('+') : 'none', element: matched,
    };
  };
  const allowProbe = mediaCallBudget();
  const probePicMessage = async value => {
    if (!allowProbe()) throw mediaError('media_budget_exhausted');
    const {element, ...facts} = await inspectPicMessage(value);
    // Recompute the verdict instead of trusting the caller or a label.
    const passed = facts.unique && facts.msg_id_matches && facts.time_matches &&
      facts.peer_matches && facts.sender_matches !== false &&
      facts.matched_pic_count === 1 && facts.md5_matches &&
      (value.chatType !== 2 || (facts.sender_matches === true &&
        facts.segment_id_matches === true && facts.file_uuid_matches === true));
    return {...facts, passed};
  };
  const allowMedia = mediaCallBudget();
  let mediaBusy = false;
  let transfer = null;
  const closeTransfer = async current => {
    if (!current || transfer !== current) return;
    transfer = null;
    clearTimeout(current.timer);
    await current.file.close();
  };
  const armTransfer = current => {
    current.timer = setTimeout(() => { void closeTransfer(current).catch(() => {}); }, MEDIA_TRANSFER_IDLE_MS);
    current.timer.unref?.();
  };
  const transferRequest = value => object(value) && !Array.isArray(value) &&
    Object.keys(value).length === 1 && CREDENTIAL.test(value.transfer_id);
  const chunkRequest = value => object(value) && !Array.isArray(value) &&
    Object.keys(value).length === 2 && CREDENTIAL.test(value.transfer_id) &&
    Number.isSafeInteger(value.offset) && value.offset >= 0 && value.offset < MAX_MEDIA_BYTES;
  const readChunk = async value => {
    const current = transfer;
    if (!current || value.transfer_id !== current.id) throw mediaError('media_transfer_invalid');
    if (current.reading) throw mediaError('media_busy');
    current.reading = true;
    clearTimeout(current.timer);
    try {
      const chunk = await current.file.read(value.offset);
      if (chunk.eof) await closeTransfer(current);
      else armTransfer(current);
      return chunk;
    } catch (error) {
      await closeTransfer(current);
      throw MEDIA_STAGES.has(error?.code) ? error : mediaError('media_file_rejected');
    } finally { current.reading = false; }
  };
  const releaseTransfer = async value => {
    if (transfer && transfer.id === value.transfer_id) await closeTransfer(transfer);
    return {closed: true};
  };
  // The only media read path. It re-locates the same message and the one PIC
  // element matching the recorded element ID, digest and file UUID, then asks
  // QQ for that element only. Bytes leave bounded; the local path never does.
  const downloadPicMedia = async value => {
    if (mediaBusy || transfer !== null) throw mediaError('media_busy');
    if (!allowMedia()) throw mediaError('media_budget_exhausted');
    mediaBusy = true;
    let download = null;
    try {
      let facts;
      try {
        facts = await inspectPicMessage(value);
      } catch (error) {
        throw MEDIA_STAGES.has(error?.code) ? error : mediaError('media_message_query_failed');
      }
      const element = facts.element;
      if (!facts.found || !facts.unique || !facts.msg_id_matches || !facts.time_matches ||
          !facts.peer_matches || facts.sender_matches === false ||
          facts.matched_pic_count !== 1 || !facts.md5_matches ||
          (value.chatType === 2 && (facts.sender_matches !== true ||
            facts.segment_id_matches !== true || facts.file_uuid_matches !== true)) ||
          element === null || text(element.elementId) !== value.elementId) {
        throw mediaError('media_mapping_rejected');
      }
      const fileApi = core.apis?.FileApi;
      if (!fileApi || typeof fileApi.downloadMedia !== 'function') throw mediaError('media_api_unavailable');
      let localPath;
      try {
        // The confirmed PIC supplies the original-cache hint. NapCat reuses
        // that file when present; an unnecessary native download can return
        // no path. Never accept a path from the RPC caller or force deletion.
        const cachedPath = element.picElement.sourcePath ?? '';
        if (cachedPath !== '') validateMediaPath(cachedPath);
        await logMediaCache(element.picElement);
        download = Promise.resolve(observedMediaApi(fileApi).downloadMedia(value.msgId, value.chatType, value.peerUid, value.elementId,
          '', cachedPath, MEDIA_TIMEOUT_MS, false));
        localPath = await bounded(download, MEDIA_TIMEOUT_MS, () => mediaError('media_download_timeout'));
        if ((localPath == null || localPath === '') && cachedPath === '') {
          throw mediaError('media_download_no_cache_hint');
        }
      } catch (error) {
        throw MEDIA_STAGES.has(error?.code) ? error : mediaError('media_download_failed');
      }
      const file = await openBoundedMedia(localPath);
      transfer = {id: randomBytes(32).toString('hex'), file, reading: false};
      armTransfer(transfer);
      return {transfer_id: transfer.id, size: file.size, chunk_size: MEDIA_CHUNK_BYTES};
    } finally {
      // A deadline or disconnected caller cannot cancel QQ's native operation.
      // Keep its lease until it settles; late paths/bytes never enter the report.
      if (download === null) mediaBusy = false;
      else void download.then(() => { mediaBusy = false; }, () => { mediaBusy = false; });
    }
  };
  const methods = new Map([
    ['Core.status', {count: 0, call: status}],
    ['EchoMetadata.listFriends', {count: 0, call: friends}],
    ['EchoMetadata.listGroups', {count: 0, call: groups}],
    ['GroupApi.getGroupMemberAll', {count: 1, valid: value => !!uin(value) && typeof value === 'string', call: members}],
    ['EchoMediaProbe.resolvePicMessage', {count: 1, valid: probeRequest, call: probePicMessage}],
    ['EchoMediaProbe.downloadPicMedia', {count: 1, valid: mediaRequest, call: downloadPicMedia}],
    ['EchoMediaProbe.readPicMediaChunk', {count: 1, valid: chunkRequest, call: readChunk}],
    ['EchoMediaProbe.releasePicMedia', {count: 1, valid: transferRequest, call: releaseTransfer}],
    ['EchoSnapshotApi.acquire', {count: 0, call: context => snapshot.acquire(context)}],
    ['EchoSnapshotApi.cleanup', {count: 1, valid: generation, call: id => snapshot.cleanup(id)}],
    ['EchoSnapshotApi.recover', {count: 0, call: () => snapshot.recover()}]
  ]);
  const server = createServer((req, res) => {
    const send = (code, value) => {
      if (!res.writableEnded && !res.destroyed) res.writeHead(code, {'content-type': 'application/json', 'cache-control': 'no-store'}).end(JSON.stringify(value));
    };
    const failure = (code, error) => {
      const refused = () => send(code, {ok: false, error});
      const bytes = Number(req.headers['content-length'] ?? '0');
      // Reject before routing, but drain a bounded ordinary request before
      // closing its connection. Otherwise a separately arriving body can turn
      // the 401 into a Windows connection reset instead of an auth rejection.
      req.on('error', () => {});
      if (!req.complete && !req.headers['transfer-encoding'] &&
          Number.isSafeInteger(bytes) && bytes >= 0 && bytes <= MAX_REQUEST_BYTES) {
        req.once('end', refused);
      } else refused();
      req.resume();
    };
    const boundPort = server.address().port;
    if (req.headers.origin || ![`127.0.0.1:${boundPort}`, `localhost:${boundPort}`].includes(req.headers.host)) return failure(403, 'invalid_origin');
    // Authenticate before any routing detail is decided, so an unauthenticated
    // caller learns nothing about which routes or methods exist, and no method
    // is ever reached without a credential.
    if (!authorized(req)) return failure(401, 'invalid_credential');
    const boundRoute = runtimeId !== null && req.url === `/rpc/${runtimeId}`;
    if (req.method !== 'POST' || (req.url !== '/rpc' && !boundRoute)) return failure(404, 'invalid_route');
    if (req.headers['content-type']?.split(';')[0].trim() !== 'application/json') return failure(415, 'invalid_content_type');
    if (Number(req.headers['content-length']) > MAX_REQUEST_BYTES) return failure(413, 'request_too_large');
    let size = 0;
    const chunks = [];
    req.on('data', chunk => {
      size += chunk.length;
      if (size > MAX_REQUEST_BYTES) { chunks.length = 0; send(413, {ok: false, error: 'request_too_large'}); }
      else chunks.push(chunk);
    });
    req.on('error', () => send(400, {ok: false, error: 'invalid_request'}));
    req.on('end', async () => {
      if (res.writableEnded) return;
      let body;
      try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { return send(400, {ok: false, error: 'invalid_request'}); }
      if (!object(body) || Array.isArray(body) || typeof body.method !== 'string') return send(400, {ok: false, error: 'invalid_request'});
      // Every bound managed call checks workspace identity first. Only initial
      // authenticated status discovery can use the unbound route.
      const discovery = body.method === 'Core.status' && !Object.hasOwn(body, 'boot_id');
      if (runtimeId !== null && !discovery && !boundRoute) {
        return send(409, {ok: false, error: 'runtime_identity_mismatch'});
      }
      // Only the initial authenticated status discovery may omit boot_id.
      // Bound status polling and every operation are guarded before touching core.
      if ((body.method !== 'Core.status' || Object.hasOwn(body, 'boot_id')) &&
          (typeof body.boot_id !== 'string' || !CREDENTIAL.test(body.boot_id) || body.boot_id !== bootId)) {
        return send(409, {ok: false, error: 'worker_generation_mismatch'});
      }
      const method = methods.get(body.method);
      if (!method) return send(400, {ok: false, error: 'invalid_method'});
      const params = Object.hasOwn(body, 'params') ? body.params : [];
      if (!Array.isArray(params) || params.length !== method.count || (method.valid && !method.valid(params[0]))) return send(400, {ok: false, error: 'invalid_params'});
      const acquireBudget = body.timeout_ms ?? 120000;
      const acquiring = body.method === 'EchoSnapshotApi.acquire';
      if (acquiring && (!Number.isInteger(acquireBudget) || acquireBudget < 1 || acquireBudget > 120000)) {
        return send(400, {ok: false, error: 'invalid_params'});
      }
      const acquireDeadline = acquiring ? performance.now() + acquireBudget : 0;
      const acquireController = acquiring ? new AbortController() : null;
      const acquireTimer = acquiring ? setTimeout(() => acquireController.abort(), acquireBudget) : null;
      let result, cleanupStarted = false;
      const cleanupAbandoned = async () => {
        if (cleanupStarted || result?.ok !== true || result.status !== 'ready' || !generation(result.generation_id)) return;
        cleanupStarted = true;
        logSnapshot('abandoned_acquire_cleanup', 'started');
        try {
          const cleaned = await snapshot.cleanup(result.generation_id);
          if (cleaned?.ok !== true && cleaned?.code !== 'generation_not_found') throw new Error();
          logSnapshot('abandoned_acquire_cleanup', 'success');
        } catch {
          // Keep snapshot ownership/recovery fail-closed; never log payloads.
          logSnapshot('abandoned_acquire_cleanup', 'failed');
        }
      };
      const onAcquireClose = () => {
        if (!res.writableFinished) { acquireController.abort(); void cleanupAbandoned(); }
      };
      if (acquiring) res.once('close', onAcquireClose);
      try {
        result = acquiring
          ? await method.call({ signal: acquireController.signal, deadline: acquireDeadline })
          : await method.call(...params);
        if (acquiring && (res.destroyed || performance.now() >= acquireDeadline)) {
          logSnapshot('acquire_response_discarded', res.destroyed ? 'client_disconnected' : 'deadline_exceeded');
          await cleanupAbandoned();
          send(200, {ok: false, error: 'qq_runtime_operation_failed'});
          return;
        }
        send(200, {ok: true, result});
      }
      catch (error) {
        if (acquiring) logSnapshot('acquire_operation', 'failed');
        // Media stages are a fixed whitelist; every other failure stays generic.
        send(200, {ok: false, error: MEDIA_STAGES.has(error?.code) ? error.code : 'qq_runtime_operation_failed'});
      } finally {
        // Keep the close listener until the response settles: a disconnect
        // during the final socket flush still owns an abandoned generation.
        if (acquiring) clearTimeout(acquireTimer);
      }
    });
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, host, resolve); });
  return {port: server.address().port, async stop() {
    await closeTransfer(transfer);
    await new Promise((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      server.closeAllConnections();
    });
  }};
}
