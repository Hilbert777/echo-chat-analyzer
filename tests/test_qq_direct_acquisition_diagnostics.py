"""Anonymous formal acquisition diagnostics, using fictional sources only."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_qq_direct_main_wal_acquisition import fictional_source, _formal
from test_qq_direct_main_wal_poc import fictional_source as poc_source, _interleave

pytestmark = pytest.mark.slow_integration


def diagnose(source, action="", *, setup="", phase="main_baseline_read"):
    def driver(program):
        program = _formal(program)
        program = program.replace("const api=createEchoSnapshotApi", """
  const diagnosticLog=path.join(path.dirname(outputPath),'diagnostics.log');
  process.env.QCE_LOG_FILE=diagnosticLog;
  SETUP
  const api=createEchoSnapshotApi""".replace("SETUP", setup))
        return program.replace("result.recovered=recovered.ok;", """
  result.recovered=recovered.ok;
  result.diagnosticLog=fs.readFileSync(diagnosticLog,'utf8');
""")
    return _interleave(source, action, phase=phase, driver_transform=driver)


def test_wal_generation_reset_during_main_baseline_fails_closed(fictional_source):
    def driver(program):
        program = _formal(program).replace('const api=createEchoSnapshotApi', """
  const diagnosticLog=path.join(path.dirname(outputPath),'diagnostics.log');
  process.env.QCE_LOG_FILE=diagnosticLog;
  const initialBoundary=fs.readFileSync(mainPath+'-shm').readUInt32LE(16);
  const builtin=await import('node:module');
  const mutableFs=builtin.createRequire(import.meta.url)('node:fs');
  const originalOpen=mutableFs.openSync, originalRead=mutableFs.readSync, originalClose=mutableFs.closeSync;
  const activeFds=new Set(), mainFds=new Set();
  let resetDuringBaseline=false, oldFramesPreserved=false, headerChecksumValid=false, mergedOpened=false;
  mutableFs.openSync=(p,...args)=>{const fd=originalOpen(p,...args);
    activeFds.add(fd);
    if(String(p)===mainPath) mainFds.add(fd);
    if(path.basename(String(p))==='merged.db') mergedOpened=true;
    return fd;};
  mutableFs.readSync=(fd,buffer,at,length,position)=>{
    const count=originalRead(fd,buffer,at,length,position);
    // The main content baseline is read after B was fixed, before WAL scanning.
    if(!resetDuringBaseline && mainFds.has(fd) && position===1024 && length>=4096) {
      resetDuringBaseline=true;
      const wal=fs.readFileSync(walPath), oldFrames=Buffer.from(wal.subarray(32));
      // A reset publishes a new header generation while old physical frame
      // slots remain. Keep the header checksum valid so salt is the rejecting guard.
      wal.writeUInt32BE((wal.readUInt32BE(12)+1)>>>0,12);
      wal[16]^=1;
      const sum=checksum(wal.subarray(0,24),wal.readUInt32BE(0)===0x377f0682);
      wal.writeUInt32BE(sum[0],24); wal.writeUInt32BE(sum[1],28);
      fs.writeFileSync(walPath,wal);
      const rewritten=fs.readFileSync(walPath);
      oldFramesPreserved=rewritten.subarray(32).equals(oldFrames);
      const verified=checksum(rewritten.subarray(0,24),rewritten.readUInt32BE(0)===0x377f0682);
      headerChecksumValid=verified[0]===rewritten.readUInt32BE(24) && verified[1]===rewritten.readUInt32BE(28);
    }
    return count;};
  mutableFs.closeSync=(fd)=>{originalClose(fd); activeFds.delete(fd); mainFds.delete(fd);};
  builtin.syncBuiltinESMExports();
  const api=createEchoSnapshotApi""")
        return program.replace('result.recovered=recovered.ok;', """
  result.recovered=recovered.ok;
  result.diagnosticLog=fs.readFileSync(diagnosticLog,'utf8');
  result.initialBoundary=initialBoundary;
  result.resetDuringBaseline=resetDuringBaseline;
  result.oldFramesPreserved=oldFramesPreserved;
  result.headerChecksumValid=headerChecksumValid;
  result.mergedOpened=mergedOpened;
  result.publishedGenerationCount=fs.existsSync(path.join(root,'generations'))
    ? fs.readdirSync(path.join(root,'generations')).length : 0;
  result.workspaceDatabaseCount=fs.readdirSync(root,{recursive:true})
    .filter(name=>/\\.db(?:-wal|-shm|-journal)?$/.test(name)).length;
  result.activeFdCount=activeFds.size;
""")
    result = _interleave(fictional_source, '', driver_transform=driver)
    assert result['initialBoundary'] > 0
    assert f"capture_stage=shm_witness boundary={result['initialBoundary']}" in result['diagnosticLog']
    assert result['resetDuringBaseline'] and result['oldFramesPreserved'] and result['headerChecksumValid']
    assert not result['ok'] and result['code'] == 'snapshot_unstable'
    assert 'failure_stage=wal_prefix guard_code=wal_frame_salt_invalid' in result['diagnosticLog']
    assert result['decryptCalls'] == 0 and not result['mergedOpened']
    assert result['manifest'] is None and result['publishedGenerationCount'] == 0
    assert result['stagingCleaned'] and result['recovered']
    assert result['workspaceDatabaseCount'] == 0 and result['activeFdCount'] == 0
    assert not fictional_source[2].exists()


def test_unknown_error_and_private_fields_never_reach_log(fictional_source):
    result = diagnose(fictional_source, setup="""
  core.apis.DatabaseApi.getNtDbDir=()=>{const e=new Error('PRIVATE_TEXT path UIN key salt payload');
  e.code='PRIVATE_CODE'; e.boundary='PRIVATE_BOUNDARY'; e.sourceHash='PRIVATE_HASH'; throw e;};
""")
    log = result["diagnosticLog"]
    assert "failure_stage=source_location guard_code=unexpected_error" in log
    for private in ("PRIVATE", "10086", str(fictional_source[0]), "fictional-local-passphrase"):
        assert private not in log


def test_unwritable_diagnostics_cannot_change_acquisition(fictional_source):
    def driver(program):
        return _formal(program).replace('const api=createEchoSnapshotApi',
                                        'process.env.QCE_LOG_FILE=path.dirname(mainPath); const api=createEchoSnapshotApi')
    result = _interleave(fictional_source, '', driver_transform=driver)
    assert result['ok'] and result['stagingCleaned'] and result['recovered']


def test_source_diagnostics_preserve_database_api_receiver(fictional_source):
    result = diagnose(fictional_source, setup="""
  core.apis.DatabaseApi.getNtDbDir=function() {
    if(this!==core.apis.DatabaseApi) throw new Error('fictional lost receiver');
    return path.dirname(mainPath);
  };
""")
    assert result['ok']
    assert 'stage=decrypt_source_before' in result['diagnosticLog']
    assert 'stage=decrypt_source_after' in result['diagnosticLog']


@pytest.mark.parametrize('wal_page_one', [False, True])
def test_page_count_mismatch_identifies_selected_page_one_and_cleans(fictional_source, wal_page_one):
    action = """
  const wal=fs.readFileSync(walPath), last=wal.subarray(wal.length-4120);
  const next=Buffer.from(last); next.writeUInt32BE(1,0);
  fs.readFileSync(mainPath).subarray(1024,5120).copy(next,24);
  const sum=checksum(Buffer.concat([next.subarray(0,8),next.subarray(24)]),
    wal.readUInt32BE(0)===0x377f0682,[last.readUInt32BE(16),last.readUInt32BE(20)]);
  next.writeUInt32BE(sum[0],16); next.writeUInt32BE(sum[1],20);fs.appendFileSync(walPath,next);
  const index=fs.readFileSync(mainPath+'-shm');
  index.writeUInt32LE(index.readUInt32LE(16)+1,16);
  index.writeUInt32LE(sum[0],24);index.writeUInt32LE(sum[1],28);updateIndex(index);
""" if wal_page_one else ""
    result = diagnose(fictional_source, setup=action + """
  const decrypt=core.apis.DatabaseApi.decryptDatabase;
  core.apis.DatabaseApi.decryptDatabase=async function(source,target) {
    const result=await decrypt.call(this,source,target);
    const bytes=fs.readFileSync(target);bytes.writeUInt32BE(99,28);fs.writeFileSync(target,bytes);
    return result;
  };
""")
    log = result['diagnosticLog']
    failure = next(line for line in log.splitlines() if 'guard_code=plaintext_header_invalid' in line)
    for field in ('committed_pages=2', 'shm_pages=2', 'main_pages=2', 'header_pages=99',
                  'merged_page_one_matches_selected=true', 'header_count_nonzero=true',
                  'header_counters_equal=true', 'header_pages_match_commit=false', 'page_count_valid=false'):
        assert field in failure
    assert f"page_one_source={'wal' if wal_page_one else 'main'}" in failure
    assert f"page_one_in_committed_wal={str(wal_page_one).lower()}" in failure
    assert f"page_one_frame={2 if wal_page_one else 0}" in failure
    assert 'key_change_since_previous_decrypt=unknown' in log
    for private in ('fictional-local-passphrase', str(fictional_source[0]), '10086',
                    'file_change_counter=', 'version_valid_for=', 'salt=', 'sourceHash='):
        assert private not in log
    assert not result['ok'] and result['retryable'] and result['manifest'] is None
    assert result['stagingCleaned'] and result['recovered']


def test_key_version_diagnostics_distinguish_retry_updates_without_reading_keys(fictional_source):
    def driver(program):
        program = _formal(program).replace('const api=createEchoSnapshotApi', """
  const diagnosticLog=path.join(path.dirname(outputPath),'diagnostics.log');
  process.env.QCE_LOG_FILE=diagnosticLog;
  core.echoDbPassphraseVersion=1;
  core.apis.DatabaseApi.getPassphraseBuffer=()=>{throw Error('PRIVATE_KEY_MUST_NOT_BE_READ');};
  core.apis.DatabaseApi.decryptDatabase=async()=>false;
  const api=createEchoSnapshotApi""")
        program = program.replace('const result=await api.acquire();', """
  const attempts=[await api.acquire(),await api.acquire()];
  core.echoDbPassphraseVersion=2;
  core.apis.DatabaseApi.decryptDatabase=async()=>{core.echoDbPassphraseVersion=3;return false;};
  attempts.push(await api.acquire());
  delete core.echoDbPassphraseVersion;
  core.apis.DatabaseApi.decryptDatabase=async()=>false;
  attempts.push(await api.acquire());
  const result=attempts.at(-1);
""")
        return program.replace('result.recovered=recovered.ok;', """
  result.recovered=recovered.ok;
  result.attempts=attempts.map(a=>a.code);
  result.diagnosticLog=fs.readFileSync(diagnosticLog,'utf8');
""")
    result = _interleave(fictional_source, '', driver_transform=driver)
    lines = [line for line in result['diagnosticLog'].splitlines()
             if 'capture_stage=decrypt' in line]
    assert len(lines) == 4
    for line, expected, during in zip(lines, ('unknown', 'unchanged', 'changed', 'unknown'),
                                      ('unchanged', 'unchanged', 'changed', 'unknown')):
        assert f'key_change_since_previous_decrypt={expected}' in line
        assert f'key_change_during_decrypt={during}' in line
    assert result['attempts'] == ['decrypt_failed'] * 4
    assert result['stagingCleaned'] and result['recovered'] and result['manifest'] is None
    assert 'PRIVATE_KEY_MUST_NOT_BE_READ' not in result['diagnosticLog']


@pytest.mark.parametrize('behavior,stage,guard,retryable', [
    ("throw Object.assign(new Error('PRIVATE_ERROR'),{code:'EACCES'});", 'decrypt', 'access_denied', False),
    ("throw Object.assign(new Error('PRIVATE_ERROR'),{code:'PRIVATE_CODE'});", 'decrypt', 'unexpected_error', False),
    ("throw Object.assign(new Error('PRIVATE_ERROR'),{code:'ENOSPC'});", 'decrypt', 'disk_full', False),
    ("throw Object.assign(new Error('PRIVATE_ERROR'),{code:'ERR_DLOPEN_FAILED'});", 'decrypt', 'native_load_failed', False),
    ("throw Object.assign(new Error('PRIVATE_ERROR'),{code:'ERR_FS_FILE_TOO_LARGE'});", 'decrypt', 'file_too_large', False),
    ("return false;", 'decrypt', 'decrypt_rejected', True),
    ("return true;", 'decrypt', 'decrypt_output_missing', False),
    ("const b=fs.readFileSync(outputPath); b[18]=255; fs.writeFileSync(target,b); return true;",
     'plaintext_validation', 'plaintext_header_invalid', True),
])
def test_decrypt_failure_is_diagnosable_without_private_fields(fictional_source, behavior, stage, guard, retryable):
    # Produce a valid fictional plaintext once, before replacing only the API operation.
    from test_qq_direct_main_wal_poc import _run
    _run('snapshot', fictional_source[0], fictional_source[1], fictional_source[2])
    result = diagnose(fictional_source, setup=f"""
  core.apis.DatabaseApi.decryptDatabase=async(source,target)=>{{ {behavior} }};
""")
    assert result['code'] == 'decrypt_failed'
    assert (result.get('retryable') is True) == retryable
    assert f'failure_stage={stage} guard_code={guard}' in result['diagnosticLog']
    assert 'PRIVATE' not in result['diagnosticLog']
    assert str(fictional_source[0]) not in result['diagnosticLog']
    assert result['manifest'] is None and result['stagingCleaned'] and result['recovered']
    if stage == 'plaintext_validation':
        assert 'file_size_matches=true' in result['diagnosticLog']
        assert 'sqlite_signature_valid=true' in result['diagnosticLog']
        assert 'format_versions_valid=false' in result['diagnosticLog']


@pytest.mark.parametrize('operation,stage', [
    ('writeFileSync', 'manifest'), ('renameSync', 'publish'), ('unlinkSync', 'encrypted_cleanup'),
])
def test_post_decrypt_io_failure_has_a_safe_stage(fictional_source, operation, stage):
    result = diagnose(fictional_source, setup=f"""
  const builtin=await import('node:module');
  const mutableFs=builtin.createRequire(import.meta.url)('node:fs');
  const original=mutableFs.{operation};
  let injected=false;
  mutableFs.{operation}=(p,...args)=>{{
    const target=String(p);
    if(!injected && (target.endsWith('manifest.json') ||
       ('{operation}'==='renameSync' && target.endsWith('staging')) ||
       ('{operation}'==='unlinkSync' && target.endsWith('merged.db')))) {{
      injected=true;
      throw Object.assign(new Error('PRIVATE_ERROR fictional-account fictional-key'),{{code:'EACCES'}});
    }}
    return original(p,...args);
  }};
  builtin.syncBuiltinESMExports();
""")
    assert not result['ok']
    assert f'failure_stage={stage} guard_code=access_denied' in result['diagnosticLog']
    assert 'PRIVATE' not in result['diagnosticLog']
    assert 'fictional-account' not in result['diagnosticLog']
    assert result['manifest'] is None and result['stagingCleaned'] and result['recovered']
