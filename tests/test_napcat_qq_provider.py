"""Fictional NapCat core over the real Echo HTTP bridge; no QQ/account state.

The bridge requires a client credential for every RPC method.  These tests use
fictional in-memory values only and a marker file written by the fictional core,
so "the request was refused" can be proven to mean "nothing was executed".
"""
import importlib
import http.client
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts/qq_napcat_plugin/bridge.mjs"

TOKEN = "0123456789abcdef" * 4
WRONG_TOKEN = "fedcba9876543210" * 4
RUNTIME_ID = "ab" * 32

SERVER = """
import {startBridge} from './bridge.mjs';
import fs from 'node:fs';
import {mock} from 'node:test';
import {createInterface} from 'node:readline';
const config = JSON.parse(process.argv[2]);
delete process.env.QCE_LOG_FILE;
if(config.nativeLog) process.env.QCE_LOG_FILE=config.nativeLog;
// File races and old Windows stat differences are confined to fictional files
// in this server's test directory; no QQ or NapCat runtime is loaded.
if(config.mediaRace) {
  if(config.mediaRace==='junction') fs.symlinkSync(config.replacementPath,config.mediaPath,'junction');
  const lstat=fs.promises.lstat.bind(fs.promises);
  const open=fs.promises.open.bind(fs.promises);
  fs.promises.lstat=async(p,options)=>{
    const info=await lstat(p,options);
    if(p===config.mediaPath && config.mediaRace==='windows-dev')
      info.dev=typeof info.dev==='bigint' ? info.dev+1n : info.dev+1;
    if(p===config.mediaPath && config.mediaRace==='rounded-inode')
      info.ino=options?.bigint ? 9007199254740992n : Number(9007199254740992n);
    if(p===config.mediaPath && config.mediaRace==='unknown-inode')
      info.ino=options?.bigint ? 0n : 0;
    return info;
  };
  let replaced=false;
  const replace=async()=>{
    if(replaced) return;
    replaced=true;
    await fs.promises.rename(config.mediaPath,config.mediaPath+'.old');
    if(config.mediaRace==='swapped-link')
      await fs.promises.symlink(config.replacementPath,config.mediaPath);
    else await fs.promises.rename(config.replacementPath,config.mediaPath);
  };
  fs.promises.open=async(p,flags)=>{
    if(p===config.mediaPath && ['before-open','swapped-link'].includes(config.mediaRace)) await replace();
    const handle=await open(p,flags);
    if(p!==config.mediaPath) return handle;
    if(['rounded-inode','unknown-inode'].includes(config.mediaRace)) {
      const stat=handle.stat.bind(handle);
      handle.stat=async options=>{
        const info=await stat(options);
        info.ino=config.mediaRace==='unknown-inode' ? (options?.bigint ? 0n : 0)
          : (options?.bigint ? 9007199254740993n : Number(9007199254740993n));
        return info;
      };
    }
    if(config.mediaRace==='during-read') {
      const read=handle.read.bind(handle);
      handle.read=async(...args)=>{const result=await read(...args);await replace();return result;};
    }
    return handle;
  };
}
if(config.clockFile) Object.defineProperty(performance,'now',{value:()=>Number(fs.readFileSync(config.clockFile,'utf8'))});
const mark = name => { if (config.marker) fs.appendFileSync(config.marker, name + '\\n'); };
const info = fields => new Proxy(fields, {get(target, key) { mark('Core.status'); return target[key]; }});
const core={dbPassphrase:'PRIVATE',selfInfo:info({uin:'12345678',uid:'u_self',nick:'Self',online:true,secret:'PRIVATE'}),apis:{
 DatabaseApi:{hasPassphrase:()=>true,decryptDatabase(){mark('decryptDatabase')}},
 FileApi:{downloadMedia:async function(...args){
   mark('downloadMedia');
   if(config.expectedDownload && JSON.stringify(args.slice(0,4))!==JSON.stringify(config.expectedDownload)) throw new Error('wrong locator');
   // Pinned NapCat FileApi uses argument six for an existing original cache.
   if(args[5] && fs.existsSync(args[5]) && !args[7]) return args[5];
   if(config.nativeDownload) {
     const [,event]=await this.core.eventWrapper.callNormalEventV2(
       'NodeIKernelMsgService/downloadRichMedia',
       'NodeIKernelMsgListener/onRichMediaDownloadComplete',
       [{msgId:args[0],elementId:args[3]}],()=>true,
       event=>event.msgId===args[0] && event.msgElementId===args[3],1,args[6]);
     return event.filePath;
   }
   return config.mediaPath ?? '';
 }},
 FriendApi:{getBuddyV2ExWithCate:async()=>{mark('listFriends');return [{buddyList:[{uin:'23456789',uid:'u_friend',coreInfo:{uin:'23456789',uid:'u_friend',nick:'Nick',remark:'Remark',secret:'PRIVATE'},secret:'PRIVATE'},{uid:'u_only'},{uin:'34567890'}]}]}},
 GroupApi:{getGroups:async()=>{mark('listGroups');return [{groupCode:'45678901',groupName:'Group',memberCount:3},{groupCode:'56789012'}]},getGroupMemberAll:async()=>{mark('getGroupMemberAll');return {result:{infos:new Map([['u_member',{uid:'u_member',uin:'23456789',nick:'Nick',cardName:'Card',secret:'PRIVATE'}]])}}}},
 MsgApi:{getMsgsByMsgId:async(peer,ids)=>{
   mark('getMsgsByMsgId');
   const pic={elementType:2,elementId:'8000000000000000001',picElement:{md5HexStr:'AB'.repeat(16),fileUuid:'fictional-file-uuid',fileName:'PRIVATE.jpg'},...config.picOverrides};
   if(config.thumbPaths) pic.picElement.thumbPath=new Map(config.thumbPaths.map((path,index)=>[index,path]));
   const message={msgId:ids[0],msgSeq:'42',msgTime:'1760000001',chatType:peer.chatType,peerUid:peer.peerUid,senderUid:peer.peerUid,secret:'PRIVATE',elements:[pic,{elementType:1,elementId:'8000000000000000002',textElement:{content:'PRIVATE'}}],...config.messageOverrides};
   if(config.duplicatePic) message.elements.push({...pic});
   if(config.secondMessageOverrides && config.queried) Object.assign(message,config.secondMessageOverrides);
   config.queried=true;
   return {result:0,msgList:config.mode==='probe-duplicate'?[message,{...message}]:[message]};
 }}
}};
if(config.nativeDownload) {
  core.eventWrapper={async callNormalEventV2(method,listener,params,check,match){
    if(this!==core.eventWrapper) throw Error('PRIVATE wrong receiver');
    mark('nativeDownload');
    const result=config.nativeResult ?? {result:0};
    if(!check(result)) throw Error('PRIVATE rejected request');
    const event={msgId:params[0].msgId,msgElementId:params[0].elementId,
      filePath:config.mediaPath ?? '',...config.nativeEvent};
    if(!match(event)) throw Error('PRIVATE wrong callback');
    return [result,event];
  }};
  core.apis.FileApi.core=core;
}
let snapshot={
 acquire:async()=>{mark('acquire');return {ok:true,status:'ready',generation_id:'test-generation'}},
 cleanup:async()=>{mark('cleanup');return {ok:true,status:'cleaned'}},
 recover:async()=>{mark('recover');return {ok:true,status:'recovered'}}};
if(config.mode==='slow-acquire') snapshot.acquire=async()=>{
  mark('acquire'); await new Promise(resolve=>setTimeout(resolve,200));
  mark('ready'); return {ok:true,status:'ready',generation_id:'test-generation'};
};
if(config.mode==='cancellable-acquire') snapshot.acquire=async({signal,deadline})=>{
  if(!Number.isFinite(deadline)) throw new Error('missing deadline');
  mark('acquire');
  await new Promise(resolve=>signal.aborted ? resolve() : signal.addEventListener('abort',resolve,{once:true}));
  mark('cancelled');return {ok:false,status:'failed',code:'snapshot_unstable'};
};
if(config.mode==='blocked-acquire') snapshot.acquire=async()=>{
  mark('acquire'); const until=performance.now()+100;
  while(performance.now()<until) {} // A synchronous capture cannot receive socket-close events.
  mark('ready');return {ok:true,status:'ready',generation_id:'test-generation'};
};
if(config.mode==='direct-db') {
  const {createEchoSnapshotApi}=await import(config.snapshotModule);
  const path=await import('node:path');let mutated=false;
  core.apis.DatabaseApi.getNtDbDir=()=>config.sourceDirectory;
  core.apis.DatabaseApi.decryptDatabase=async(_input,target)=>{
    if(config.decryptDelay) await new Promise(resolve=>setTimeout(resolve,config.decryptDelay));
    fs.copyFileSync(config.plaintext,target);mark('direct-decrypted');return true;
  };
  snapshot=createEchoSnapshotApi(core,{rootDirectory:config.snapshotRoot,onCapturePhase:phase=>{
    if(phase!=='main_baseline_read') return;
    mark('capture');
    if(config.transientMain && !mutated) {
      mutated=true;const main=path.join(config.sourceDirectory,'nt_msg.db');
      const bytes=fs.readFileSync(main);bytes[100]^=1;fs.writeFileSync(main,bytes);
    }
  }});
}
if(config.mode==='offline'){core.selfInfo=info({online:false});core.apis.DatabaseApi={};}
if(config.mode==='empty'){core.apis.FriendApi.getBuddyV2ExWithCate=async()=>{mark('listFriends');return []};core.apis.GroupApi.getGroups=async()=>{mark('listGroups');return []};}
if(config.mode==='failure'){core.apis.FriendApi.getBuddyV2ExWithCate=async()=>{mark('listFriends');throw Error('PRIVATE passphrase token path')};}
// Drive real bridge deadlines and deferred kernel completion independently.
// Neither production timeout values nor bridge source are replaced.
let finishMedia;
if(config.controlledMedia || config.queryFailure==='timeout') mock.timers.enable({apis:['setTimeout']});
if(config.controlledMedia) {
  const download=core.apis.FileApi.downloadMedia;
  core.apis.FileApi.downloadMedia=(...args)=>{
    if(finishMedia) return download(...args);
    mark('downloadMedia');
    return new Promise((resolve,reject)=>{finishMedia=action=>{
      mark('mediaSettled');
      action==='resolve' ? resolve(config.mediaPath) : reject(new Error('PRIVATE late kernel failure'));
    };});
  };
  const lstat=fs.promises.lstat.bind(fs.promises);
  fs.promises.lstat=async(...args)=>{if(args[0]===config.mediaPath) mark('mediaRead');return lstat(...args);};
}
if(config.queryFailure==='unavailable') delete core.apis.MsgApi;
if(config.queryFailure==='timeout') core.apis.MsgApi.getMsgsByMsgId=()=>{
  mark('getMsgsByMsgId');return new Promise(()=>{});
};
if(config.queryFailure==='private') core.apis.MsgApi.getMsgsByMsgId=()=>{
  throw Object.assign(new Error('PRIVATE kernel text'),{code:'PRIVATE_kernel_code'});
};
const bridge=await startBridge(core,snapshot,{port:config.port ?? 0,token:config.token,runtimeId:config.runtimeId ?? null});
console.log(JSON.stringify({port:bridge.port}));
createInterface({input:process.stdin}).on('line',async line=>{
  const action=JSON.parse(line);
  if(typeof action==='number') mock.timers.tick(action);
  else finishMedia(action);
  await new Promise(resolve=>setImmediate(resolve));
  console.log(JSON.stringify({action}));
});
process.stdin.resume();process.stdin.on('end',async()=>{await bridge.stop();});
"""


class _Credential:
    """Stand-in for the real session: exposes the current credential only."""

    def __init__(self, value=TOKEN):
        self._value = value

    def credential(self):
        return self._value


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    assert BRIDGE.is_file(), "Echo bridge missing"
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for real bridge transport contract")
    directory = tmp_path_factory.mktemp("echo-bridge")
    shutil.copy2(BRIDGE, directory / "bridge.mjs")
    (directory / "server.mjs").write_text(SERVER, encoding="utf-8")
    processes = []
    by_base = {}

    def start(mode="ready", *, token=TOKEN, runtime_id=None, marker=None, port=0, **snapshot_options):
        config = {
            "mode": mode,
            "token": token,
            "runtimeId": runtime_id,
            "marker": str(marker) if marker is not None else "",
            "port": port,
            **snapshot_options,
        }
        process = subprocess.Popen(
            [node, str(directory / "server.mjs"), json.dumps(config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        base = "http://127.0.0.1:" + str(json.loads(process.stdout.readline())["port"])
        by_base[base] = process
        return base

    def restart(base, **kwargs):
        process = by_base[base]
        process.stdin.close()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
        return start(port=urllib.parse.urlsplit(base).port, **kwargs)

    start.restart = restart

    def control(base, action):
        process = by_base[base]
        process.stdin.write(json.dumps(action) + "\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline()) == {"action": action}

    start.control = control

    yield start
    for process in processes:
        if not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read()


def provider(base, credential=TOKEN):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    source = None if credential is None else _Credential(credential)
    return module.NapCatQQProvider(base, credential=source)


def test_abandoned_acquire_cleans_late_generation(runtime, tmp_path):
    import time
    marker = tmp_path / "fictional-operations.txt"
    base = runtime("slow-acquire", marker=marker)
    client = provider(base)
    boot = client.status().boot_id
    parsed = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request("POST", "/rpc", json.dumps({
        "method": "EchoSnapshotApi.acquire", "params": [], "boot_id": boot,
    }), headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        deadline = time.monotonic() + 3
        while "acquire" not in marker.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        connection.close()
    deadline = time.monotonic() + 3
    while "cleanup" not in marker.read_text() and time.monotonic() < deadline:
        time.sleep(0.01)
    operations = marker.read_text().splitlines()
    assert operations.count("ready") == 1
    assert operations.count("cleanup") == 1


def test_optional_rpc_budget_includes_initial_status_handshake(monkeypatch):
    module = importlib.import_module('qq_chat_analyzer.providers.napcat_qq_provider')
    clock = [1000.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    timeouts = []
    def transport(url, body, timeout):
        method = json.loads(body)['method']
        timeouts.append(timeout)
        if method == 'Core.status':
            clock[0] += 3
            result = {'rpc_auth': 'bearer-v1', 'boot_id': 'b' * 64,
                      'bridge_ready': True, 'qq_online': True, 'self_info': {},
                      'database_api_ready': True, 'passphrase_ready': True, 'snapshot_api_ready': True}
        else:
            result = []
        return 200, json.dumps({'ok': True, 'result': result})
    client = module.NapCatQQProvider(credential=_Credential(), transport=transport)
    assert client.list_groups(timeout=5) == []
    assert timeouts == [5, 2]


@pytest.mark.parametrize('reason', ['disconnect', 'deadline'])
def test_bridge_forwards_cancellation_to_running_capture(runtime, tmp_path, reason):
    import time
    marker = tmp_path/'operations.txt'
    base = runtime('cancellable-acquire',marker=marker)
    boot = provider(base).status().boot_id
    parsed = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=3)
    connection.request('POST','/rpc',json.dumps({'method':'EchoSnapshotApi.acquire',
        'params':[],'boot_id':boot,'timeout_ms':50 if reason=='deadline' else 65000}),
        headers={'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json'})
    try:
        deadline = time.monotonic()+3
        while 'acquire' not in marker.read_text():
            assert time.monotonic()<deadline
            time.sleep(.01)
        if reason=='deadline':
            assert json.load(connection.getresponse())=={'ok':False,'error':'qq_runtime_operation_failed'}
    finally:
        connection.close()
    while 'cancelled' not in marker.read_text():
        assert time.monotonic()<deadline
        time.sleep(.01)
    operations = marker.read_text().splitlines()
    assert operations.count('cancelled')==1
    assert 'ready' not in operations and 'cleanup' not in operations


def test_expired_acquire_budget_discards_ready_result_even_while_socket_is_open(runtime, tmp_path):
    marker = tmp_path / 'fictional-deadline.txt'
    log = tmp_path / 'snapshot-diagnostics.log'
    base = runtime('blocked-acquire', marker=marker, nativeLog=str(log))
    boot = provider(base).status().boot_id
    status, payload = request(base, raw=json.dumps({
        'method': 'EchoSnapshotApi.acquire', 'params': [], 'boot_id': boot, 'timeout_ms': 50,
    }).encode())
    assert status == 200 and payload == {'ok': False, 'error': 'qq_runtime_operation_failed'}
    assert marker.read_text().splitlines().count('cleanup') == 1
    diagnostic = log.read_text()
    assert 'stage=acquire_response_discarded outcome=deadline_exceeded' in diagnostic
    assert 'stage=abandoned_acquire_cleanup outcome=success' in diagnostic
    assert TOKEN not in diagnostic and str(tmp_path) not in diagnostic


@pytest.mark.parametrize('budget', [0, -1, 120001, 'PRIVATE_BUDGET', True])
def test_invalid_acquire_budget_cannot_start_capture(runtime, tmp_path, budget):
    marker = tmp_path / 'fictional-invalid-budget.txt'
    base = runtime(marker=marker)
    boot = provider(base).status().boot_id
    status, payload = request(base, raw=json.dumps({
        'method': 'EchoSnapshotApi.acquire', 'params': [], 'boot_id': boot, 'timeout_ms': budget,
    }).encode())
    assert status == 400 and payload == {'ok': False, 'error': 'invalid_params'}
    assert 'acquire' not in marker.read_text().splitlines()


def test_bridge_accepts_bounded_large_database_budget(runtime, tmp_path):
    marker = tmp_path / 'fictional-large-budget.txt'
    base = runtime(marker=marker)
    boot = provider(base).status().boot_id
    status, payload = request(base, raw=json.dumps({
        'method': 'EchoSnapshotApi.acquire', 'params': [], 'boot_id': boot, 'timeout_ms': 120000,
    }).encode())
    assert status == 200 and payload['ok'] is True
    assert marker.read_text().splitlines().count('acquire') == 1


@pytest.mark.slow_integration
@pytest.mark.parametrize("scenario", ['ready', 'abandon', 'transient_main'])
def test_real_bridge_snapshot_to_facade_and_abandoned_cleanup(runtime, tmp_path, monkeypatch, scenario):
    import sqlite3
    import time
    from contextlib import closing
    from types import SimpleNamespace
    from test_qq_direct_main_wal_poc import _reserve_48_bytes, _run
    from test_qq_direct_snapshot_workspace import _run as run_workspace
    from test_qq_direct_database_facade import _create_plaintext_snapshot
    from qq_chat_analyzer.application.facade import ChatAnalyzerFacade, ChatSource, FacadeError
    from qq_chat_analyzer.application.qq.qq_direct_database_import_service import (
        QQDirectDatabaseImportService,
    )
    plain = tmp_path / 'fictional.db'
    selected = tmp_path / 'selected.db'
    source = tmp_path / 'source'
    source.mkdir()
    _reserve_48_bytes(plain)
    _create_plaintext_snapshot(plain)
    with closing(sqlite3.connect(plain)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE group_msg_table SET "40050"="40050"+1')
        writer.commit()
        with closing(sqlite3.connect(selected)) as backup:
            writer.backup(backup)
        _run('fixture', plain, Path(f'{plain}-wal'), source / 'nt_msg.db', source / 'nt_msg.db-wal')
    root = tmp_path / 'workspace'
    marker = tmp_path / 'operations.txt'
    base = runtime('direct-db', marker=marker,
                   snapshotModule=(ROOT / 'scripts/qq_direct_db_snapshot/snapshot.mjs').as_uri(),
                   snapshotRoot=str(root), sourceDirectory=str(source), plaintext=str(selected),
                   decryptDelay=200 if scenario == 'abandon' else 0,
                   transientMain=scenario == 'transient_main')
    metadata = provider(base)
    metadata.status()
    client = metadata.snapshot_client(root)
    service = QQDirectDatabaseImportService(runtime_client=client,
                                           provider_factory=SimpleNamespace(create=lambda: metadata))
    service.start()
    if scenario == 'abandon':
        monkeypatch.setattr(client, '_acquire_timeout', 0.05)
    try:
        facade = ChatAnalyzerFacade(qq_service=service)
        if scenario == 'abandon':
            with pytest.raises(FacadeError) as caught:
                facade.list_sessions(ChatSource.QQ)
            assert caught.value.code == 'qq_direct_snapshot_acquire_failed'
            # Recovery is serialized behind cancellation and acknowledges that
            # the abandoned operation has relinquished its workspace lease.
            client.recover()
        else:
            assert {s.session_type for s in facade.list_sessions(ChatSource.QQ)} == {'group', 'private'}
        deadline = time.monotonic() + 5
        while ((scenario != 'abandon' and (not marker.exists() or 'direct-decrypted' not in marker.read_text())) or
               (root / 'staging').exists() or any((root / 'generations').glob('*'))):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert run_workspace(root, 'workspace.recover();')['ok']
        operations = marker.read_text().splitlines() if marker.exists() else []
        if scenario == 'abandon':
            # The deadline now cancels before native decryption starts.
            assert 'direct-decrypted' not in operations
            assert operations.count('capture') <= 1
        else:
            assert operations.count('capture') == (2 if scenario == 'transient_main' else 1)
    finally:
        client.recover()


def request(base, method="Core.status", params=None, *, raw=None, headers=None,
            credential=TOKEN, path="/rpc"):
    payload = {"method": method, "params": params or []}
    if raw is None and method != "Core.status" and credential == TOKEN:
        payload["boot_id"] = request(base)[1]["result"]["boot_id"]
    body = raw if raw is not None else json.dumps(payload).encode()
    authorization = {} if credential is None else {"Authorization": f"Bearer {credential}"}
    req = urllib.request.Request(base + path, data=body, headers={"Content-Type": "application/json", **authorization, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def executed(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.parametrize("method,params", [
    ("EchoMetadata.listFriends", []),
    ("EchoSnapshotApi.cleanup", ["old-generation"]),
])
@pytest.mark.parametrize("boot", [None, "a" * 64])
def test_worker_generation_gate_prevents_all_operations(runtime, tmp_path, method, params, boot):
    calls = tmp_path / "calls.txt"
    base = runtime(runtime_id=RUNTIME_ID, marker=calls)
    body = {"method": method, "params": params}
    if boot is not None:
        body["boot_id"] = boot
    code, value = request(base, raw=json.dumps(body).encode(), path=f"/rpc/{RUNTIME_ID}")
    assert (code, value) == (409, {"ok": False, "error": "worker_generation_mismatch"})
    assert executed(calls) == ""


def test_worker_boot_is_fresh_and_same_generation_operates(runtime):
    base = runtime()
    boot = request(base)[1]["result"]["boot_id"]
    assert len(boot) == 64 and all(c in "0123456789abcdef" for c in boot)
    assert request(base)[1]["result"]["boot_id"] == boot
    assert request(runtime())[1]["result"]["boot_id"] != boot
    body = {"method": "EchoSnapshotApi.recover", "params": [], "boot_id": boot}
    assert request(base, raw=json.dumps(body).encode())[1]["result"]["ok"] is True


def test_auth_then_workspace_then_worker_generation(runtime, tmp_path):
    calls = tmp_path / "calls.txt"
    base = runtime(runtime_id=RUNTIME_ID, marker=calls)
    body = json.dumps({"method": "EchoSnapshotApi.acquire", "boot_id": "a" * 64}).encode()
    assert request(base, raw=body, credential=WRONG_TOKEN) == (
        401, {"ok": False, "error": "invalid_credential"})
    assert request(base, raw=body) == (
        409, {"ok": False, "error": "runtime_identity_mismatch"})
    assert request(base, raw=body, path=f"/rpc/{RUNTIME_ID}") == (
        409, {"ok": False, "error": "worker_generation_mismatch"})
    assert executed(calls) == ""


@pytest.mark.parametrize("method,params", [("EchoSnapshotApi.acquire", [])])
def test_managed_bound_calls_check_workspace_before_generation(runtime, tmp_path, method, params):
    calls = tmp_path / "calls.txt"
    base = runtime(runtime_id=RUNTIME_ID, marker=calls)
    boot = request(base)[1]["result"]["boot_id"]
    before = executed(calls)
    for presented in (boot, "a" * 64):
        body = json.dumps({"method": method, "params": params, "boot_id": presented}).encode()
        assert request(base, raw=body) == (409, {"ok": False, "error": "runtime_identity_mismatch"})
    assert executed(calls) == before


@pytest.mark.slow_integration
def test_same_endpoint_worker_restart_refuses_old_provider_and_snapshot(runtime, tmp_path):
    from qq_chat_analyzer.providers.napcat_qq_provider import NapCatQQWorkerGenerationChanged
    from qq_chat_analyzer.providers.qq_direct_snapshot_runtime import QQSnapshotWorkerGenerationChanged
    base = runtime(runtime_id=RUNTIME_ID)
    old = provider(base)
    boot = old.status().boot_id
    snapshot = old.snapshot_client(tmp_path)
    generation = snapshot.acquire()
    calls = tmp_path / "new-worker-calls.txt"
    assert runtime.restart(base, runtime_id=RUNTIME_ID, marker=calls) == base
    with pytest.raises(QQSnapshotWorkerGenerationChanged):
        snapshot.cleanup(generation)
    with pytest.raises(NapCatQQWorkerGenerationChanged):
        old.status()
    assert executed(calls) == ""  # Even stale status was stopped before selfInfo access.
    fresh = provider(base)
    assert fresh.status().boot_id != boot
    fresh.snapshot_client(tmp_path).recover()
    assert "recover" in executed(calls)


# ------------------------------------------------------------------ happy path


def test_status_readiness_and_no_passphrase(runtime):
    base=runtime()
    status=provider(base).status()
    assert status.bridge_ready and status.qq_online and status.database_api_ready and status.passphrase_ready
    assert status.self_info == {"uin":"12345678","uid":"u_self","nickname":"Self"}
    assert status.snapshot_api_ready and status.ready
    assert "PRIVATE" not in json.dumps(request(base)[1])
    offline=provider(runtime("offline")).status()
    assert offline.bridge_ready and not offline.qq_online and not offline.passphrase_ready and not offline.ready


def test_friends_identity_names_and_missing_field_fallback(runtime):
    friends=provider(runtime()).list_friends()
    assert [(f.uid,f.uin,f.nickname,f.remark,f.display_name) for f in friends] == [
        ("u_friend","23456789","Nick","Remark","Remark"),("u_only","","","","u_only"),("","34567890","","","34567890")]
    assert provider(runtime("empty")).list_friends() == []


def test_members_map_identity_and_existing_snapshot_contract(runtime,tmp_path):
    qq=provider(runtime())
    client=qq.snapshot_client(tmp_path)
    assert qq.get_group_member_all("45678901") == client.get_group_member_all("45678901")
    member=client.get_group_member_all("45678901")["result"]["infos"]["u_member"]
    assert member == {"uid":"u_member","uin":"23456789","nick":"Nick","cardName":"Card"}
    client.recover()
    generation=client.acquire()
    assert generation == "test-generation"
    client.cleanup(generation)
    client.recover()


# ------------------------------------------------------------- unified credential


def test_unauthenticated_core_status_is_refused_and_executes_nothing(runtime, tmp_path):
    calls = tmp_path / "calls.txt"
    base = runtime(marker=calls)
    code, value = request(base, credential=None)
    assert (code, value) == (401, {"ok": False, "error": "invalid_credential"})
    assert executed(calls) == ""


@pytest.mark.parametrize("method,params", [
    ("EchoMetadata.listFriends", []),
    ("EchoSnapshotApi.cleanup", ["fictional-generation"]),
])
def test_unauthenticated_rpc_is_refused_without_executing_the_method(runtime, tmp_path, method, params):
    calls = tmp_path / "calls.txt"
    base = runtime(marker=calls)
    code, value = request(base, method, params, credential=None)
    assert code == 401 and value == {"ok": False, "error": "invalid_credential"}
    assert executed(calls) == ""


@pytest.mark.parametrize("method", ["Core.status"])
def test_wrong_credential_is_refused_without_executing_the_method(runtime, tmp_path, method):
    calls = tmp_path / "calls.txt"
    base = runtime(marker=calls)
    code, value = request(base, method, credential=WRONG_TOKEN)
    assert code == 401 and value == {"ok": False, "error": "invalid_credential"}
    assert executed(calls) == ""


def test_malformed_or_absent_authorization_header_is_refused(runtime, tmp_path, monkeypatch):
    import time
    send = http.client.HTTPConnection.send

    def separated_body(connection, data):
        # urllib sends headers and body separately. An early refusal must not
        # close the socket while a small, rejected request is still arriving.
        if isinstance(data, bytes) and data.startswith(b'{'):
            time.sleep(0.025)
        return send(connection, data)

    monkeypatch.setattr(http.client.HTTPConnection, 'send', separated_body)
    calls = tmp_path / "calls.txt"
    base = runtime(marker=calls)
    for headers in ({}, {"Authorization": "fictional"}, {"Authorization": f"Basic {TOKEN}"},
                    {"Authorization": TOKEN}, {"Authorization": f"Bearer {WRONG_TOKEN}"}):
        code, value = request(base, headers=headers, credential=None)
        assert code == 401 and value == {"ok": False, "error": "invalid_credential"}
    assert executed(calls) == ""


def test_managed_bound_route_requires_the_credential_too(runtime, tmp_path):
    calls = tmp_path / "calls.txt"
    base = runtime(runtime_id=RUNTIME_ID, marker=calls)
    bound = f"/rpc/{RUNTIME_ID}"
    code, value = request(base, "EchoSnapshotApi.recover", credential=None, path=bound)
    assert code == 401 and value == {"ok": False, "error": "invalid_credential"}
    assert executed(calls) == ""
    # The credential and the runtime binding are two independent defenses.
    code, value = request(base, "EchoSnapshotApi.recover", credential=TOKEN, path=bound)
    assert code == 200 and value == {"ok": True, "result": {"ok": True, "status": "recovered"}}
    assert "recover" in executed(calls)


def test_authentication_response_never_echoes_the_credential(runtime):
    base = runtime()
    body = json.dumps(request(base, credential=WRONG_TOKEN)[1])
    assert WRONG_TOKEN not in body
    assert WRONG_TOKEN[:8] not in body
    assert len(body) < 200


def test_authorized_boundary_cases_are_unchanged(runtime):
    base=runtime()
    assert request(base,raw=b"{")[0] == 400
    assert request(base,raw=b"x"*4097)[0] == 413
    assert request(base,headers={"Origin":"https://outside.example"})[0] == 403
    assert request(base,headers={"Host":"outside.example"})[0] == 403
    assert request(base,raw=b'{"method":"Core.status","params":null}')[0] == 400
    assert request(base,headers={"Content-Type":"text/plain"})[0] == 415


@pytest.mark.parametrize("method,params",[("DatabaseApi.decryptDatabase",[]),("__proto__.pollute",[]),("EchoSnapshotApi.cleanup",["../outside"])])
def test_fixed_dispatch_and_parameter_rejection(runtime,method,params):
    code,value=request(runtime(),method,params)
    assert code == 400 and value["ok"] is False
    assert "PRIVATE" not in json.dumps(value)


def test_http_boundary_and_safe_errors(runtime):
    base=runtime()
    value=request(runtime("failure"),"EchoMetadata.listFriends")[1]
    assert value == {"ok":False,"error":"qq_runtime_operation_failed"}
    module=importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    with pytest.raises(module.NapCatQQError) as error:
        provider(runtime("failure")).list_friends()
    assert "PRIVATE" not in str(error.value)


def test_streamed_body_limit_and_http_route(runtime):
    base=runtime()
    parsed=urllib.parse.urlsplit(base)
    connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=5)
    connection.request('POST','/rpc',body=iter([b'x'*2000,b'x'*3000]),headers={'Content-Type':'application/json','Authorization':f'Bearer {TOKEN}'},encode_chunked=True)
    response=connection.getresponse()
    assert response.status == 413
    assert json.loads(response.read()) == {'ok':False,'error':'request_too_large'}
    connection.close()
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(urllib.request.Request(base+'/rpc',headers={'Authorization':f'Bearer {TOKEN}'}),timeout=5)
    assert error.value.code == 404
    error.value.close()


def test_single_fetch_and_invalid_response_errors():
    module=importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    calls=[]
    def transport(url,body,timeout):
        calls.append(json.loads(body))
        if json.loads(body)['method'] == 'Core.status':
            return 200,json.dumps({'ok':True,'result':{'rpc_auth':'bearer-v1',
                'boot_id':'b'*64,
                'bridge_ready':True,'qq_online':True,'self_info':{},
                'database_api_ready':True,'passphrase_ready':True,'snapshot_api_ready':True}})
        return 200,json.dumps({"ok":True,"result":[]})
    client=module.NapCatQQProvider(transport=transport,credential=_Credential())
    assert client.list_friends() == [] and client.list_groups() == []
    assert [c['method'] for c in calls] == ['Core.status','EchoMetadata.listFriends','EchoMetadata.listGroups']
    for body in ['PRIVATE',json.dumps({'ok':False,'error':'PRIVATE'}),json.dumps({'ok':True,'result':{}})]:
        client=module.NapCatQQProvider(transport=lambda *args:(200,body),credential=_Credential())
        with pytest.raises(module.NapCatQQError) as error: client.list_friends()
        assert 'PRIVATE' not in str(error.value)


def test_snapshot_transport_errors_keep_existing_error_family(tmp_path):
    module=importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    from qq_chat_analyzer.providers.qq_direct_snapshot_runtime import QQSnapshotRuntimeUnavailable
    def failing(*args): raise module.NapCatQQError()
    client=module.NapCatQQProvider(transport=failing,credential=_Credential()).snapshot_client(tmp_path)
    with pytest.raises(QQSnapshotRuntimeUnavailable): client.acquire()


@pytest.mark.parametrize("url",["http://127.0.0.1.evil.example:40655","http://user:secret@127.0.0.1:40655","http://192.168.1.1:40655","http://127.0.0.1:40655/path"])
def test_provider_rejects_non_loopback_or_ambiguous_urls(url):
    module=importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    with pytest.raises(module.NapCatQQError):
        module.NapCatQQProvider(url)


def test_bridge_cannot_bind_external_address():
    assert BRIDGE.is_file()
    node=shutil.which("node")
    if not node: pytest.skip("Node unavailable")
    script=f"import {{startBridge}} from {json.dumps(BRIDGE.as_uri())}; try {{ await startBridge({{apis:{{}}}},{{}},{{host:'0.0.0.0',port:0,token:'{TOKEN}'}}); process.exit(1); }} catch {{ process.exit(0); }}"
    result=subprocess.run([node,"--input-type=module","-e",script],capture_output=True,timeout=10)
    assert result.returncode == 0


def test_bridge_refuses_an_absent_or_malformed_credential():
    assert BRIDGE.is_file()
    node=shutil.which("node")
    if not node: pytest.skip("Node unavailable")
    script = (
        f"import {{startBridge}} from {json.dumps(BRIDGE.as_uri())};"
        "const core={apis:{}}; const snapshot={};"
        "for (const token of [undefined, null, '', 'fictional', 'A'.repeat(64), 'f'.repeat(63), 'f'.repeat(65)]) {"
        "  try { await startBridge(core, snapshot, {port:0, token}); process.exit(3); }"
        "  catch (error) { if (error.message !== 'invalid_bridge_credential') throw error; }"
        "} process.exit(0);"
    )
    result=subprocess.run([node,"--input-type=module","-e",script],capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


# ------------------------------------------------------- message mapping probe

_PROBE = {
    "msg_id": "7000000000000000001",
    "peer_uid": "u_fictionalpeer000000000000",
    "expected_time": 1760000001,
    "expected_md5": "ab" * 16,
    "expected_file_uuid": "fictional-file-uuid",
    "expected_segment_id": "8000000000000000001",
    "expected_self": False,
}
_PROBE_WIRE = {
    "msgId": _PROBE["msg_id"],
    "chatType": 1,
    "peerUid": _PROBE["peer_uid"],
    "expectedTime": _PROBE["expected_time"],
    "expectedMd5": _PROBE["expected_md5"],
    "expectedFileUuid": _PROBE["expected_file_uuid"],
    "expectedSegmentId": _PROBE["expected_segment_id"],
    "expectedSelf": _PROBE["expected_self"],
}


def test_probe_maps_one_message_and_element_without_returning_private_values(runtime, tmp_path):
    calls = tmp_path / "probe-calls.txt"
    base = runtime(marker=calls)
    mapping = provider(base).resolve_pic_message(**_PROBE)
    assert mapping.passed
    assert mapping.found and mapping.unique
    assert mapping.msg_id_matches and mapping.time_matches and mapping.peer_matches
    assert mapping.sender_matches is True
    assert mapping.pic_element_count == 1 and mapping.matched_pic_count == 1
    assert mapping.segment_id_matches is True
    assert mapping.file_uuid_matches is True and mapping.md5_matches is True
    assert mapping.match_basis == "segment_id+file_uuid+md5"
    executed = calls.read_text()
    assert "getMsgsByMsgId" in executed
    assert "downloadMedia" not in executed

    status, payload = request(base, "EchoMediaProbe.resolvePicMessage", [dict(_PROBE_WIRE)])
    assert status == 200 and payload["ok"] is True
    body = json.dumps(payload)
    for private in (
        _PROBE["msg_id"], _PROBE["peer_uid"], _PROBE["expected_segment_id"],
        _PROBE["expected_md5"], _PROBE["expected_file_uuid"], "PRIVATE", ".jpg",
    ):
        assert private not in body


def test_probe_reports_segment_mismatch_but_keeps_md5_mapping(runtime):
    mapping = provider(runtime()).resolve_pic_message(
        **{**_PROBE, "expected_segment_id": "8000000000000000002"})
    assert mapping.passed
    assert mapping.segment_id_matches is False
    assert mapping.match_basis == "file_uuid+md5"


def test_probe_rejects_wrong_md5_and_duplicate_message_results(runtime):
    wrong = provider(runtime()).resolve_pic_message(
        **{**_PROBE, "expected_md5": "cd" * 16})
    assert not wrong.passed and wrong.matched_pic_count == 0

    duplicate = provider(runtime("probe-duplicate")).resolve_pic_message(**_PROBE)
    assert duplicate.found and not duplicate.unique and not duplicate.passed


def test_probe_rejects_invalid_parameters_before_any_kernel_call(runtime, tmp_path):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    calls = tmp_path / "invalid-probe-calls.txt"
    client = provider(runtime(marker=calls))
    client.status()
    invalid = (
        {"msg_id": int(_PROBE["msg_id"])},
        {"msg_id": "0"},
        {"peer_uid": "u_short"},
        {"peer_uid": "fictional-peer"},
        {"expected_time": True},
        {"expected_time": 0},
        {"expected_md5": "zz" * 16},
        {"expected_file_uuid": "../outside"},
        {"expected_segment_id": "0"},
        {"expected_self": "yes"},
    )
    for override in invalid:
        with pytest.raises(module.NapCatQQError):
            client.resolve_pic_message(**{**_PROBE, **override})
    assert "getMsgsByMsgId" not in calls.read_text()


# ------------------------------------------------------- bounded media fetch

_MEDIA = {
    "msg_id": _PROBE["msg_id"],
    "peer_uid": _PROBE["peer_uid"],
    "element_id": _PROBE["expected_segment_id"],
    "expected_time": _PROBE["expected_time"],
    "expected_md5": _PROBE["expected_md5"],
    "expected_file_uuid": _PROBE["expected_file_uuid"],
    "expected_self": _PROBE["expected_self"],
}
_MEDIA_WIRE = {
    **{key: value for key, value in _PROBE_WIRE.items() if key != "expectedSegmentId"},
    "elementId": _PROBE["expected_segment_id"],
}


_GROUP = {**_PROBE, "chat_type": 2, "peer_uid": "45678901",
          "expected_sender_uin": "23456789", "expected_sender_uid": "u_fictionalmember000000"}
_GROUP_WIRE = {**_PROBE_WIRE, "chatType": 2, "peerUid": "45678901",
               "expectedSenderUin": "23456789", "expectedSenderUid": "u_fictionalmember000000"}


@pytest.mark.parametrize("ending", ["resolve", "reject"])
@pytest.mark.parametrize("cancel", [False, True])
def test_media_lease_survives_timeout_or_disconnect_until_kernel_settles(runtime, tmp_path, ending, cancel):
    """A deadline/disconnected caller cannot release a still-running download."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from qq_chat_analyzer.providers.napcat_qq_provider import NapCatQQMediaStageError

    path = tmp_path / "fictional-media.bin"
    path.write_bytes(b"fictional-media")
    marker = tmp_path / "calls.txt"
    base = runtime(controlledMedia=True, marker=marker, mediaPath=str(path))
    client = provider(base)
    boot = client.status().boot_id
    with ThreadPoolExecutor(max_workers=1) as pool:
        connection = None
        if cancel:
            parsed = urllib.parse.urlsplit(base)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
            connection.request("POST", "/rpc", json.dumps({
                "method": "EchoMediaProbe.downloadPicMedia", "params": [_MEDIA_WIRE], "boot_id": boot,
            }), headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        else:
            future = pool.submit(client.download_pic_media, **_MEDIA)
        try:
            deadline = time.monotonic() + 3
            while "downloadMedia" not in marker.read_text():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            if connection is not None:
                connection.close()
            with pytest.raises(NapCatQQMediaStageError) as caught:
                client.download_pic_media(**_MEDIA)
            assert caught.value.stage == "media_busy"
            runtime.control(base, 120000)
            if not cancel:
                with pytest.raises(NapCatQQMediaStageError) as caught:
                    future.result(timeout=3)
                assert caught.value.stage == "media_download_timeout"
            with pytest.raises(NapCatQQMediaStageError) as caught:
                client.download_pic_media(**_MEDIA)
            assert caught.value.stage == "media_busy"
        finally:
            if connection is not None:
                connection.close()
            runtime.control(base, ending)
        assert marker.read_text().splitlines().count("downloadMedia") == 1
        assert "mediaRead" not in marker.read_text()  # Ignore late bytes; never read their returned path.
        assert client.download_pic_media(**_MEDIA) == path.read_bytes()
        assert client.download_pic_media(**_MEDIA) == path.read_bytes()
        assert marker.read_text().splitlines().count("downloadMedia") == 3


@pytest.mark.parametrize("failure,code", [
    ("timeout", "probe_timeout"), ("unavailable", "probe_api_unavailable"),
    ("private", "qq_runtime_operation_failed"),
])
def test_query_errors_survive_bridge_and_download_recheck_without_private_details(runtime, tmp_path, failure, code):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from qq_chat_analyzer.providers.napcat_qq_provider import NapCatQQError

    marker = tmp_path / "calls.txt"
    base = runtime(queryFailure=failure, marker=marker)
    client = provider(base)
    client.status()
    with ThreadPoolExecutor(max_workers=1) as pool:
        for ordinal, (method, locator) in enumerate([
            (client.resolve_pic_message, _PROBE), (client.download_pic_media, _MEDIA),
        ], 1):
            future = pool.submit(method, **locator)
            if failure == "timeout":
                deadline = time.monotonic() + 3
                while marker.read_text().splitlines().count("getMsgsByMsgId") < ordinal:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                runtime.control(base, 8000)
            with pytest.raises(NapCatQQError) as caught:
                future.result(timeout=3)
            actual = getattr(caught.value, "stage", None) or caught.value.rpc_error
            want = "media_message_query_failed" if failure == "private" and ordinal == 2 else code
            assert actual == want
            assert "PRIVATE" not in str(caught.value)
    assert "downloadMedia" not in marker.read_text()


@pytest.mark.parametrize("mine", [False, True])
def test_group_pic_maps_sender_independently_of_peer_then_fetches(runtime, tmp_path, mine):
    group = {**_GROUP, "expected_self": mine, "expected_sender_uin": "12345678" if mine else "23456789",
             "expected_sender_uid": None if mine else _GROUP["expected_sender_uid"]}
    raw = b"fictional-group-media"
    path = tmp_path / "group.bin"
    path.write_bytes(raw)
    calls = tmp_path / "calls.txt"
    client = provider(runtime(marker=calls, runtime_id=RUNTIME_ID, mediaPath=str(path),
        messageOverrides={"senderUin": group["expected_sender_uin"],
                          "senderUid": "u_self" if mine else group["expected_sender_uid"]},
        expectedDownload=[_GROUP["msg_id"], 2, "45678901", _GROUP["expected_segment_id"]]))
    mapping = client.resolve_pic_message(**group)
    assert mapping.passed and mapping.sender_matches is True
    assert "downloadMedia" not in calls.read_text()
    fetch = {k: v for k, v in group.items() if k != "expected_segment_id"}
    assert client.download_pic_media(**fetch, element_id=group["expected_segment_id"]) == raw
    assert calls.read_text().splitlines().count("getMsgsByMsgId") == 2


@pytest.mark.parametrize("override", [
    {"messageOverrides": {"msgId": "8000000000000000009"}},
    {"messageOverrides": {"chatType": 1}},
    {"messageOverrides": {"peerUid": "56789012"}},
    {"messageOverrides": {"peerUin": "56789012"}},
    {"messageOverrides": {"peerUin": 45678901}},
    {"messageOverrides": {"senderUin": "34567890"}},
    {"messageOverrides": {"senderUid": "u_wrongmember00000000"}},
    {"messageOverrides": {"msgTime": "1760000002"}},
    {"picOverrides": {"elementId": "8000000000000000002"}},
    {"picOverrides": {"elementType": 1}},
    {"picOverrides": {"picElement": {"md5HexStr": "ab" * 16, "fileUuid": "other-uuid"}}},
    {"picOverrides": {"picElement": {"md5HexStr": "cd" * 16, "fileUuid": "fictional-file-uuid"}}},
    {"duplicatePic": True},
    {"mode": "probe-duplicate"},
])
def test_group_mapping_mismatch_rejects_probe_and_direct_download(runtime, tmp_path, override):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    calls = tmp_path / "calls.txt"
    config = {**override, "messageOverrides": {
        "senderUin": "23456789", "senderUid": _GROUP["expected_sender_uid"], **override.get("messageOverrides", {})}}
    client = provider(runtime(marker=calls, **config))
    mapping = client.resolve_pic_message(**_GROUP)
    assert not mapping.passed
    fetch = {k: v for k, v in _GROUP.items() if k != "expected_segment_id"}
    with pytest.raises(module.NapCatQQMediaStageError) as caught:
        client.download_pic_media(**fetch, element_id=_GROUP["expected_segment_id"])
    assert caught.value.stage == "media_mapping_rejected"
    assert "downloadMedia" not in calls.read_text()


def test_group_mapping_is_rechecked_before_download(runtime, tmp_path):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    calls = tmp_path / "calls.txt"
    client = provider(runtime(marker=calls,
        messageOverrides={"senderUin": "23456789", "senderUid": _GROUP["expected_sender_uid"]},
        secondMessageOverrides={"senderUin": "34567890"}))
    assert client.resolve_pic_message(**_GROUP).passed
    fetch = {k: v for k, v in _GROUP.items() if k != "expected_segment_id"}
    with pytest.raises(module.NapCatQQMediaStageError):
        client.download_pic_media(**fetch, element_id=_GROUP["expected_segment_id"])
    assert "downloadMedia" not in calls.read_text()


@pytest.mark.parametrize("override", [
    {"chatType": 3}, {"chatType": True}, {"peerUid": 45678901}, {"peerUid": "u_fictionalpeer0000000"},
    {"expectedSenderUin": None}, {"expectedSenderUin": "0"},
    {"expectedSenderUid": "u_short"}, {"expectedFileUuid": None}, {"expectedSegmentId": None},
])
def test_group_bridge_rejects_missing_or_invalid_identity_before_kernel(runtime, tmp_path, override):
    calls = tmp_path / "calls.txt"
    base = runtime(marker=calls)
    status, _ = request(base, "EchoMediaProbe.resolvePicMessage", [{**_GROUP_WIRE, **override}])
    assert status == 400
    assert not calls.exists() or "getMsgsByMsgId" not in calls.read_text()


def test_media_fetch_relocates_the_element_and_returns_only_bounded_bytes(runtime, tmp_path, monkeypatch):
    raw = b"fictional-media-bytes" * 3
    media_file = tmp_path / "fictional-media.bin"
    media_file.write_bytes(raw)
    calls = tmp_path / "media-calls.txt"
    base = runtime(marker=calls, mediaPath=str(media_file))
    client = provider(base)
    assert client.download_pic_media(**_MEDIA) == raw
    executed = calls.read_text().splitlines()
    assert "getMsgsByMsgId" in executed and "downloadMedia" in executed

    # The original is already cached. A repeated native download can return no
    # path, so the bridge must pass the confirmed PIC's cache to FileApi.
    base = runtime(mediaPath="", picOverrides={"picElement": {
        "md5HexStr": "AB" * 16, "fileUuid": "fictional-file-uuid",
        "sourcePath": str(media_file), "fileName": "PRIVATE.jpg"}})
    assert provider(base).download_pic_media(**_MEDIA) == raw

    # The raw response carries bytes only: no path, file name or element ID.
    status, payload = request(base, "EchoMediaProbe.downloadPicMedia", [dict(_MEDIA_WIRE)])
    assert status == 200 and payload["ok"] is True
    assert payload["result"]["size"] == len(raw)
    body = json.dumps(payload)
    for private in (str(media_file), "fictional-media.bin", _MEDIA["element_id"], _MEDIA["peer_uid"]):
        assert private not in body


def test_native_media_diagnostics_keep_only_codes_and_cannot_block_downloads(runtime, tmp_path):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    log = tmp_path / "native-codes.log"
    image = tmp_path / "PRIVATE-cached-preview.bin"
    image.write_bytes(b"fictional-native-media")
    base = runtime(nativeDownload=True, nativeLog=str(log), nativeResult="0",
        nativeEvent={"fileErrCode": "-123", "fileSrvErrCode": "1001", "trasferStatus": 2,
                     "step": 3, "errMsg": "PRIVATE event text", "status": "PRIVATE invalid status",
                     "errCode": "77 PRIVATE", "fileDownType": 4294967296},
        picOverrides={"picElement": {"md5HexStr": "AB" * 16,
            "fileUuid": "fictional-file-uuid", "sourcePath": str(tmp_path / "missing-original"),
            "filePath": str(image)}},
        thumbPaths=[str(image), str(tmp_path / "missing-preview")])
    with pytest.raises(module.NapCatQQMediaStageError) as caught:
        provider(base).download_pic_media(**_MEDIA)
    assert caught.value.stage == "media_download_no_path"
    diagnostic = log.read_text(encoding="utf-8")
    assert "stage=native_request return_code=0" in diagnostic
    assert "fileErrCode=-123 fileSrvErrCode=1001 trasferStatus=2 step=3" in diagnostic
    assert "stage=cache_location source_state=2 file_state=3 thumb_count=2 thumb_regular=1 thumb_missing=1 thumb_invalid=0 thumb_other=0" in diagnostic
    assert "path_present=0" in diagnostic
    assert "errCode=" not in diagnostic and "fileDownType=" not in diagnostic
    assert all(secret not in diagnostic for secret in (
        "PRIVATE", str(tmp_path), TOKEN, _MEDIA["msg_id"], _MEDIA["peer_uid"], _MEDIA["expected_md5"]))
    # Cache checks and logging cannot return a different image or block a download.
    base = runtime(nativeDownload=True, nativeLog=str(tmp_path), mediaPath=str(image))
    assert provider(base).download_pic_media(**_MEDIA) == b"fictional-native-media"


def test_large_media_is_chunked_and_does_not_hold_the_next_download(runtime, tmp_path):
    import base64
    raw = b"fictional-chunk-" * (220 * 1024)
    media = tmp_path / "large-media.bin"
    media.write_bytes(raw)
    base = runtime(mediaPath=str(media))
    client = provider(base)
    boundary = b"x" * (32 * 1024 * 1024)
    media.write_bytes(boundary)
    assert client.download_pic_media(**_MEDIA) == boundary
    del boundary
    media.write_bytes(raw)
    assert client.download_pic_media(**_MEDIA) == raw
    status, payload = request(base, "EchoMediaProbe.downloadPicMedia", [dict(_MEDIA_WIRE)])
    assert status == 200 and payload["ok"] is True
    transfer = payload["result"]
    assert set(transfer) == {"transfer_id", "size", "chunk_size"}
    assert transfer["size"] == len(raw) and transfer["chunk_size"] <= 256 * 1024
    offset = 0
    received = bytearray()
    while offset < len(raw):
        status, reply = request(base, "EchoMediaProbe.readPicMediaChunk", [{
            "transfer_id": transfer["transfer_id"], "offset": offset}])
        assert status == 200 and reply["ok"] is True
        chunk = reply["result"]
        assert chunk["offset"] == offset
        decoded = base64.b64decode(chunk["base64"], validate=True)
        assert len(decoded) <= 256 * 1024
        received.extend(decoded)
        offset += len(decoded)
        assert chunk["eof"] is (offset == len(raw))
    assert bytes(received) == raw


@pytest.mark.parametrize("failure", ["cancel", "transport", "changed", "offset"])
def test_partial_media_transfer_is_never_returned_and_releases_its_lease(runtime, tmp_path, monkeypatch, failure):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    raw = b"fictional-media" * 50000
    media = tmp_path / "chunked.bin"
    media.write_bytes(raw)
    client = provider(runtime(mediaPath=str(media)))
    rpc = client._rpc
    reads = 0
    cancelled = False

    def intercepted(method, params=None, **kwargs):
        nonlocal reads, cancelled
        if method == "EchoMediaProbe.readPicMediaChunk":
            reads += 1
            if reads == 2 and failure == "transport":
                raise module.NapCatQQError("transport_timeout")
            if reads == 2 and failure == "offset":
                params = [{**params[0], "offset": 0}]
        result = rpc(method, params, **kwargs)
        if method == "EchoMediaProbe.readPicMediaChunk" and reads == 1:
            if failure == "cancel":
                cancelled = True
            if failure == "changed":
                media.write_bytes(raw[:-1])
        return result

    monkeypatch.setattr(client, "_rpc", intercepted)
    with pytest.raises(module.NapCatQQMediaStageError) as caught:
        client.download_pic_media(**_MEDIA, cancelled=lambda: cancelled)
    assert caught.value.stage == {"cancel":"media_cancelled", "transport":"media_transport_timeout",
        "changed":"media_read_incomplete", "offset":"media_transfer_invalid"}[failure]
    monkeypatch.setattr(client, "_rpc", rpc)
    media.write_bytes(raw)
    assert client.download_pic_media(**_MEDIA) == raw


def test_media_fetch_requires_the_kernel_to_uniquely_confirm_the_element(runtime, tmp_path):
    media_file = tmp_path / "fictional-media.bin"
    media_file.write_bytes(b"fictional-media-bytes")
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    for index, override in enumerate(({"expected_md5": "cd" * 16},
                                      {"element_id": "8000000000000000002"})):
        calls = tmp_path / f"rejected-{index}.txt"
        client = provider(runtime(marker=calls, mediaPath=str(media_file)))
        with pytest.raises(module.NapCatQQError):
            client.download_pic_media(**{**_MEDIA, **override})
        executed = calls.read_text()
        assert "getMsgsByMsgId" in executed
        assert "downloadMedia" not in executed


@pytest.mark.parametrize("race,stage", [
    ("windows-dev", None),
    ("rounded-inode", "media_file_identity_changed"),
    ("unknown-inode", "media_file_identity_changed"),
    ("before-open", "media_file_identity_changed"),
    ("during-read", "media_file_identity_changed"),
    ("swapped-link", "media_file_type_rejected"),
    ("symlink", "media_file_type_rejected"),
    ("junction", "media_file_type_rejected"),
])
def test_media_file_identity_uses_exact_ids_and_rejects_path_swaps(runtime, tmp_path, race, stage):
    import sys
    if race == "windows-dev" and sys.platform != "win32":
        pytest.skip("Only Windows permits cross-API device differences")
    if race == "junction" and sys.platform != "win32":
        pytest.skip("Windows directory reparse point")
    if race == "swapped-link" and sys.platform != "win32":
        stage = "media_open_failed"  # O_NOFOLLOW rejects before the second lstat.
    raw = b"fictional-media-bytes"
    target = tmp_path / "fictional.bin"
    target.write_bytes(raw)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"different-media-bytes")
    if race == "junction":
        target.unlink()
        replacement.unlink()
        replacement.mkdir()
    if race in {"symlink", "swapped-link"}:
        link = tmp_path / "link.bin"
        try:
            link.symlink_to(replacement)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege unavailable")
            raise
        if race == "symlink":
            target = link
        else:
            link.unlink()
    client = provider(runtime(mediaPath=str(target), mediaRace=race,
        picOverrides={"picElement": {"md5HexStr": "AB" * 16,
            "fileUuid": "fictional-file-uuid", "sourcePath": str(target)}},
        replacementPath=str(replacement)))
    if stage is None:
        assert client.download_pic_media(**_MEDIA) == raw
    else:
        module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
        with pytest.raises(module.NapCatQQMediaStageError) as caught:
            client.download_pic_media(**_MEDIA)
        assert caught.value.stage == stage


@pytest.mark.parametrize("kind,stage", [
    ("oversized", "media_file_too_large"),
    ("missing", "media_file_missing"),
    ("directory", "media_file_type_rejected"),
    ("empty_file", "media_file_empty"),
    ("empty_path", "media_download_no_path"),
    ("no_cache_hint", "media_download_no_cache_hint"),
    ("non_string_path", "media_path_type_invalid"),
    ("relative_path", "media_path_not_absolute"),
])
def test_media_fetch_reports_a_fixed_stage_for_each_local_rejection(runtime, tmp_path, kind, stage):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    target = tmp_path / kind
    if kind == "oversized":
        target.write_bytes(b"x" * (32 * 1024 * 1024 + 1))
    elif kind == "directory":
        target.mkdir()
    elif kind == "empty_file":
        target.write_bytes(b"")
    media_path = {"empty_path": "", "no_cache_hint": "",
                  "non_string_path": {"PRIVATE": "kernel-return"},
                  "relative_path": "PRIVATE-relative-path"}.get(kind, str(target))
    options = {"picOverrides": {"picElement": {"md5HexStr": "AB" * 16,
        "fileUuid": "fictional-file-uuid", "sourcePath": str(tmp_path / "missing-original")}}} if kind == "empty_path" else {}
    base = runtime(mediaPath=media_path, **options)
    client = provider(base)
    with pytest.raises(module.NapCatQQMediaStageError) as caught:
        client.download_pic_media(**_MEDIA)
    assert caught.value.stage == stage

    # The same stage, and nothing else, crosses the bridge to any client.
    status, payload = request(base, "EchoMediaProbe.downloadPicMedia", [dict(_MEDIA_WIRE)])
    assert (status, payload) == (200, {"ok": False, "error": stage})
    assert str(tmp_path) not in json.dumps(payload)


def test_report_bridge_budget_is_bounded_and_recovers_without_restart(runtime, tmp_path):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    clock = tmp_path / "fictional-clock.txt"
    clock.write_text("1000")
    media = tmp_path / "fictional-media.bin"
    media.write_bytes(b"fictional-media")
    base = runtime(mediaPath=str(media), clockFile=str(clock))
    client = provider(base)
    for _ in range(64):
        assert client.resolve_pic_message(**_PROBE).passed
        assert client.download_pic_media(**_MEDIA) == b"fictional-media"
    with pytest.raises(module.NapCatQQError) as query:
        client.resolve_pic_message(**_PROBE)
    assert query.value.rpc_error == "media_budget_exhausted"
    with pytest.raises(module.NapCatQQMediaStageError) as fetch:
        client.download_pic_media(**_MEDIA)
    assert fetch.value.stage == "media_budget_exhausted"
    # Status polling and a new client cannot reset the bridge-owned allowance.
    assert request(base)[1]["result"]["media_retry_after_ms"] == 125
    with pytest.raises(module.NapCatQQError) as query:
        provider(base).resolve_pic_message(**_PROBE)
    assert query.value.rpc_error == "media_budget_exhausted"
    import threading
    from concurrent.futures import ThreadPoolExecutor
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(client.wait_media_ready, cancelled=stop.is_set)
        stop.set()
        with pytest.raises(module.NapCatQQMediaStageError) as interrupted:
            waiting.result(timeout=1)
        assert interrupted.value.stage == "media_cancelled"
    clock.write_text("1124")
    assert request(base)[1]["result"]["media_retry_after_ms"] == 1
    with pytest.raises(module.NapCatQQError) as query:
        client.resolve_pic_message(**_PROBE)
    assert query.value.rpc_error == "media_budget_exhausted"
    with pytest.raises(module.NapCatQQMediaStageError) as fetch:
        client.download_pic_media(**_MEDIA)
    assert fetch.value.stage == "media_budget_exhausted"
    clock.write_text("1125")
    client.wait_media_ready()
    assert client.resolve_pic_message(**_PROBE).passed
    assert client.download_pic_media(**_MEDIA) == b"fictional-media"
    # Only one call per budget recovered, rather than an entire fresh burst.
    with pytest.raises(module.NapCatQQError):
        client.resolve_pic_message(**_PROBE)
    with pytest.raises(module.NapCatQQMediaStageError):
        client.download_pic_media(**_MEDIA)
    # Long idle periods replenish at most the fixed burst capacity.
    clock.write_text("61001")
    for _ in range(64):
        assert client.resolve_pic_message(**_PROBE).passed
        assert client.download_pic_media(**_MEDIA) == b"fictional-media"
    with pytest.raises(module.NapCatQQError) as query:
        client.resolve_pic_message(**_PROBE)
    assert query.value.rpc_error == "media_budget_exhausted"
    with pytest.raises(module.NapCatQQMediaStageError) as fetch:
        client.download_pic_media(**_MEDIA)
    assert fetch.value.stage == "media_budget_exhausted"


def test_media_fetch_rejects_invalid_parameters_before_any_kernel_call(runtime, tmp_path):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    calls = tmp_path / "invalid-media-calls.txt"
    media_file = tmp_path / "fictional-media.bin"
    media_file.write_bytes(b"fictional-media-bytes")
    client = provider(runtime(marker=calls, mediaPath=str(media_file)))
    client.status()
    invalid = (
        {"element_id": "0"},
        {"element_id": None},
        {"element_id": "../outside"},
        {"msg_id": _PROBE["msg_id"] + "0" * 20},
        {"peer_uid": "u_short"},
        {"expected_md5": "zz" * 16},
        {"expected_self": "yes"},
    )
    for override in invalid:
        with pytest.raises(module.NapCatQQError):
            client.download_pic_media(**{**_MEDIA, **override})
    assert "getMsgsByMsgId" not in calls.read_text()


def test_media_fetch_wire_shape_cannot_be_widened(runtime, tmp_path):
    media_file = tmp_path / "fictional-media.bin"
    media_file.write_bytes(b"fictional-media-bytes")
    calls = tmp_path / "widened-calls.txt"
    base = runtime(marker=calls, mediaPath=str(media_file))
    for extra in ({"path": "C:/fictional/secret"}, {"elementId": 1}, {"chatType": 2}):
        status, payload = request(base, "EchoMediaProbe.downloadPicMedia", [{**_MEDIA_WIRE, **extra}])
        assert status == 400 and payload == {"ok": False, "error": "invalid_params"}
    missing = {key: value for key, value in _MEDIA_WIRE.items() if key != "elementId"}
    status, payload = request(base, "EchoMediaProbe.downloadPicMedia", [missing])
    assert status == 400 and payload == {"ok": False, "error": "invalid_params"}
    assert not calls.exists() or "downloadMedia" not in calls.read_text()


@pytest.mark.parametrize("failure,stage", [
    ("timeout", "media_transport_timeout"),
    ("oversized", "media_transport_limit"),
    ("invalid", "media_transport_invalid"),
])
def test_media_transport_failures_are_distinct_and_sanitized(runtime, monkeypatch, failure, stage):
    module = importlib.import_module("qq_chat_analyzer.providers.napcat_qq_provider")
    client = provider(runtime())
    client.status()
    def response(*args):
        if failure == "timeout":
            raise TimeoutError("PRIVATE C:/fictional/path")
        size = 32 * 1024 * 1024 + 1 if failure == "oversized" else 3
        return 200, json.dumps({"ok": True, "result": {"transfer_id": "ab" * 32, "size": size, "chunk_size": 256 * 1024}})
    monkeypatch.setattr(client, "_request", response)
    with pytest.raises(module.NapCatQQMediaStageError) as caught:
        client.download_pic_media(**_MEDIA)
    assert caught.value.stage == stage
    assert "PRIVATE" not in str(caught.value)
