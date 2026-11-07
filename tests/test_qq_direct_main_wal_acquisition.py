"""Stage 3.5J: real acquisition seam, fictional encrypted main/WAL only."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_qq_direct_main_wal_poc import fictional_source as poc_source, _interleave

pytestmark = pytest.mark.slow_integration


@pytest.fixture
def fictional_source(poc_source):
    main, wal, output = poc_source
    directory = main.parent / 'source'
    directory.mkdir()
    for path in (main, wal, Path(f'{main}-shm')):
        path.rename(directory / path.name)
    return directory / main.name, directory / wal.name, output


def _formal(program):
    program = program.replace(
        "const result = await fn({mainPath, walPath, outputPath, passphrase: process.env.ECHO_POC_PASSPHRASE, onPhase});",
        """
  const {createEchoSnapshotApi} = await import('./scripts/qq_direct_db_snapshot/snapshot.mjs');
  const path = await import('node:path');
  const root = path.join(path.dirname(outputPath), 'formal-root');
  let decryptCalls=0, mainOnly=false, mergedConsumed=false;
  const core={selfInfo:{uin:'10086'},apis:{DatabaseApi:{
    hasPassphrase:()=>true, getNtDbDir:()=>path.dirname(mainPath),
    async decryptDatabase(source,target) {
      decryptCalls++; mainOnly ||= source==='nt_msg.db' || source===mainPath;
      mergedConsumed=fs.existsSync(source) && source.endsWith('merged.db');
      if(mode==='decrypt-throw') throw new Error('fictional decrypt failure');
      if(mode==='decrypt-cleanup-failure') {
        fs.writeFileSync(target,'partial'); fs.writeFileSync(path.join(path.dirname(target),'unknown.db'),'fictional'); return false;
      }
      if(mode==='decrypt-failure') {fs.writeFileSync(target,'partial'); return false;}
      const encrypted=fs.readFileSync(source), salt=encrypted.subarray(1024,1040);
      const key=pbkdf2Sync(process.env.ECHO_POC_PASSPHRASE,salt,4000,32,'sha512');
      const pages=[];
      for(let at=1024;at<encrypted.length;at+=4096) {
        const page=encrypted.subarray(at,at+4096), first=at===1024;
        const d=createDecipheriv('aes-256-cbc',key,page.subarray(4048,4064)); d.setAutoPadding(false);
        const body=Buffer.concat([d.update(page.subarray(first?16:0,4048)),d.final()]);
        const plain=Buffer.alloc(4096); body.copy(plain,first?16:0);
        if(first) Buffer.from('SQLite format 3\\0').copy(plain);
        pages.push(plain);
      }
      fs.writeFileSync(target,Buffer.concat(pages));
      if(mode==='merged-append') fs.appendFileSync(source,'fictional change');
      if(mode==='merged-replace') {fs.unlinkSync(source); fs.writeFileSync(source,encrypted);}
      if(mode==='merged-delete') fs.unlinkSync(source);
      return true;
    }
  }}};
  const api=createEchoSnapshotApi(core,{rootDirectory:root, sourceDatabaseName:path.basename(mainPath), onCapturePhase:onPhase});
  const result=await api.acquire();
  let manifest=null, encryptedPublished=false;
  if(result.ok) {
    const dir=path.join(root,'generations',result.generation_id);
    manifest=JSON.parse(fs.readFileSync(path.join(dir,'manifest.json')));
    encryptedPublished=fs.existsSync(path.join(dir,'merged.db'));
    fs.copyFileSync(path.join(dir,'snapshot.db'),outputPath);
    const cleaned=await api.cleanup(result.generation_id);
    if(!cleaned.ok) throw new Error('cleanup');
  }
  const recovered=await api.recover();
  result.decryptCalls=decryptCalls; result.mainOnly=mainOnly; result.mergedConsumed=mergedConsumed;
  result.manifest=manifest; result.encryptedPublished=encryptedPublished;
  result.stagingCleaned=!fs.existsSync(path.join(root,'staging'));
  result.recovered=recovered.ok;
""");
    return program


def _acquire(source, action="", **kwargs):
    return _interleave(source, action, driver_transform=_formal, **kwargs)


def test_formal_generation_uses_merged_input_and_keeps_manifest_contract(fictional_source):
    import sqlite3
    result = _acquire(fictional_source)
    assert result["ok"] is True, result
    assert result["mergedConsumed"] and not result["mainOnly"]
    assert not result["encryptedPublished"]
    assert result["stagingCleaned"] and result["recovered"]
    manifest = result["manifest"]
    assert manifest == {"schema_version": 1, "generation_id": result["generation_id"],
                        "state": "ready", "database": "snapshot.db",
                        "identity": {"namespace": "qq_uin", "value": "10086"}}
    with sqlite3.connect(fictional_source[2]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT body FROM entries").fetchone() == ("fictional selected",)


def test_formal_append_after_boundary_succeeds(fictional_source):
    result = _acquire(fictional_source, "appendFrame(true);")
    assert result["ok"] is True, result
    import sqlite3
    with sqlite3.connect(fictional_source[2]) as db:
        assert db.execute("SELECT body FROM entries").fetchone() == ("fictional selected",)


def test_streamed_upstream_decrypt_publishes_a_valid_sqlite_generation(fictional_source):
    """Use the actual bounded NapCat decoder at the formal acquisition seam."""
    root = Path(__file__).resolve().parents[1]
    pins = json.loads((root / 'scripts/qq_napcat_runtime_pins.json').read_text(encoding='utf-8'))
    replacement = pins['napcatPatch']['replacements'][4]['replacement']
    capsule = (root / 'tests/fixtures/napcat_41834_database_crypto.mjs').read_text(encoding='utf-8')

    def driver(program):
        program = _formal(program)
        start = program.index('      const encrypted=fs.readFileSync(source)')
        end = program.index('      return true;', start) + len('      return true;')
        decoder = 'const GL=createDecipheriv,aI=pbkdf2Sync,$e=fs,xe=path;\n' + capsule + '\n' + replacement
        decoder += '\nreturn Boolean(zE(source,Buffer.from(process.env.ECHO_POC_PASSPHRASE),target));'
        return program[:start] + decoder + program[end:]

    result = _interleave(fictional_source, '', driver_transform=driver)
    assert result['ok'] and result['decryptCalls'] == 1, result
    assert result['mergedConsumed'] and not result['mainOnly']
    assert not result['encryptedPublished'] and result['stagingCleaned'] and result['recovered']
    import sqlite3
    with sqlite3.connect(fictional_source[2]) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('SELECT body FROM entries').fetchone() == ('fictional selected',)


def test_guarded_timestamp_change_with_identical_content_reaches_decryption(fictional_source):
    """A file-time update alone is not evidence that captured pages changed."""
    action = "const s=fs.statSync(mainPath);fs.utimesSync(mainPath,s.atime,new Date(s.mtimeMs+2000));"
    result = _acquire(fictional_source, action, phase='main_baseline_read')
    assert result['ok'] and result['decryptCalls'] == 1, result
    assert result['stagingCleaned'] and result['recovered']
    import sqlite3
    with sqlite3.connect(fictional_source[2]) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('SELECT body FROM entries').fetchone() == ('fictional selected',)


def test_guarded_content_change_restored_after_merge_is_rejected_before_decrypt(fictional_source):
    """Equal before/after hashes alone must not admit an image read in between."""
    def driver(program):
        return _formal(program).replace('let fired = false;', 'let originalMain;let fired = false;').replace(
            '  if (current !== phase || fired) return;',
            "  if(current==='output_complete' && originalMain)fs.writeFileSync(mainPath,originalMain);\n"
            '  if (current !== phase || fired) return;')
    action = "originalMain=fs.readFileSync(mainPath);const changed=Buffer.from(originalMain);changed[1200]^=1;fs.writeFileSync(mainPath,changed);"
    result = _interleave(fictional_source, action, phase='main_baseline_read', driver_transform=driver)
    assert not result['ok'] and result['decryptCalls'] == 0, result
    assert result['stagingCleaned'] and result['recovered']


def test_published_single_file_needs_no_wal_or_shm(fictional_source):
    """A merged WAL image must remain readable when sidecars cannot be created."""
    import sqlite3
    from qq_chat_analyzer.providers.qq_database_provider import QQDatabaseProvider

    main, wal, output = fictional_source
    original = (main.read_bytes(), wal.read_bytes())
    def driver(program):
        return _formal(program).replace(
            'fs.writeFileSync(target,Buffer.concat(pages));',
            "fs.writeFileSync(target,Buffer.concat(pages));fs.copyFileSync(target,outputPath+'.raw');")

    result = _interleave(fictional_source, 'appendFrame(false);', driver_transform=driver)
    assert result['ok'], result
    # Deterministically deny SQLite's auxiliary file namespace, without changing
    # the user's ACLs or requiring administrator privileges on Windows.
    Path(f'{output}-wal').mkdir()
    Path(f'{output}-shm').mkdir()
    assert QQDatabaseProvider.quick_check(output) == 'ok'
    with sqlite3.connect(f'{output.as_uri()}?mode=ro', uri=True) as reader:
        assert reader.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert reader.execute('SELECT body FROM entries').fetchone() == ('fictional selected',)
    header = output.read_bytes()[:100]
    assert header[18:20] == b'\x01\x01'
    assert header[20] == 48
    assert int.from_bytes(header[28:32], 'big') == output.stat().st_size // 4096
    raw = Path(f'{output}.raw').read_bytes()
    final = output.read_bytes()
    assert raw[18:20] == b'\x02\x02'
    assert raw[:18] == final[:18] and raw[20:] == final[20:]
    assert main.read_bytes() == original[0]
    assert wal.read_bytes().startswith(original[1])


@pytest.mark.parametrize('header_pages,counters_equal,accepted', [
    (2, True, True),
    (8, True, True),
    (9, True, False),
    (0, True, True),
    (9, False, True),
])
def test_standalone_header_size_uses_sqlite_bounds(tmp_path, header_pages, counters_equal, accepted):
    """Reject a valid size beyond the image, not legal trailing capacity."""
    output = tmp_path / 'snapshot.db'
    original = bytearray(8 * 4096)
    original[:16] = b'SQLite format 3\0'
    original[16:20] = b'\x10\x00\x02\x02'
    original[24:28] = (1).to_bytes(4, 'big')
    original[28:32] = header_pages.to_bytes(4, 'big')
    original[92:96] = (1 if counters_equal else 2).to_bytes(4, 'big')
    output.write_bytes(original)
    result = subprocess.run([
        'node', '--input-type=module', '-e', """
import {finalizeStandaloneSnapshot} from './scripts/qq_direct_db_snapshot/main_wal.mjs';
const diagnostic={};
try {
  finalizeStandaloneSnapshot(process.argv[1],8,value=>Object.assign(diagnostic,value));
  console.log(JSON.stringify({ok:true,diagnostic}));
} catch(error) {console.log(JSON.stringify({ok:false,code:error.code,diagnostic}));}
""", str(output)], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(result.stdout)
    assert result['ok'] is accepted, result
    assert result['diagnostic']['header_pages_match_commit'] is (header_pages == 8)
    assert result['diagnostic']['page_count_valid'] is accepted
    if accepted:
        original[18:20] = b'\x01\x01'
    else:
        assert result['code'] == 'plaintext_header_invalid'
    assert output.read_bytes() == original


@pytest.mark.skipif(sys.platform != 'win32', reason='requires the pinned Windows SQLite DLL')
def test_preallocated_snapshot_publication_with_pinned_sqlite(tmp_path):
    """The formal capture must publish a readable 2-page DB in an 8-page file."""
    # Select the pinned DLL before importing sqlite3 in an isolated interpreter;
    # never replace the pytest process's already-loaded SQLite library.
    result = subprocess.run([sys.executable, '-c', r"""
import ctypes, hashlib, json, os, sys
from contextlib import closing
from pathlib import Path
project, work = map(Path, sys.argv[1:])
pins = json.loads((project / 'scripts/sqlite_runtime_pins.json').read_text())
dll = project / 'runtime/sqlite/sqlite3.dll'
assert hashlib.sha256(dll.read_bytes()).hexdigest() == pins['dllSha256']
library = ctypes.CDLL(str(dll))
import sqlite3
assert sqlite3.sqlite_version == pins['version'] == '3.54.1'
with closing(sqlite3.connect(':memory:')) as db:
    assert db.execute('SELECT sqlite_source_id()').fetchone() == (pins['sourceId'],)
sys.path.insert(0, str(project / 'tests'))
from test_qq_direct_main_wal_poc import _reserve_48_bytes, _run, _interleave
from test_qq_direct_main_wal_acquisition import _formal
plain = work / 'plain.db'
_reserve_48_bytes(plain)
library.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
library.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
library.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
library.sqlite3_close.argtypes = [ctypes.c_void_p]
handle = ctypes.c_void_p()
assert library.sqlite3_open(os.fsencode(plain), ctypes.byref(handle)) == 0
try:
    chunk = ctypes.c_int(32768)  # Official VFS rounding, not fabricated header bytes.
    assert library.sqlite3_file_control(handle, b'main', 6, ctypes.byref(chunk)) == 0
    assert library.sqlite3_exec(handle, b'''
        CREATE TABLE entries(id INTEGER PRIMARY KEY, body TEXT);
        INSERT INTO entries VALUES(1,'fictional baseline');
        PRAGMA journal_mode=WAL;
        PRAGMA wal_checkpoint(TRUNCATE);
    ''', None, None, None) == 0
finally:
    assert library.sqlite3_close(handle) == 0
assert plain.stat().st_size == 8 * 4096
assert int.from_bytes(plain.read_bytes()[28:32], 'big') == 2
source = work / 'source'
source.mkdir()
main, wal, output = source / 'encrypted.db', source / 'encrypted.db-wal', work / 'snapshot.db'
with closing(sqlite3.connect(plain)) as writer:
    writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute("UPDATE entries SET body='fictional selected' WHERE id=1")
    writer.commit()
    assert writer.execute('PRAGMA page_count').fetchone() == (2,)
    frames = Path(f'{plain}-wal').read_bytes()
    assert len(frames) == 32 + 4120
    assert int.from_bytes(frames[32:36], 'big') == 2  # No Page 1 in the WAL.
    assert int.from_bytes(frames[36:40], 'big') == 8
    _run('fixture', plain, Path(f'{plain}-wal'), main, wal)
    original = [p.read_bytes() for p in (main, wal, Path(f'{main}-shm'))]
    def driver(program):
        return _formal(program).replace(
            'fs.writeFileSync(target,Buffer.concat(pages));',
            "fs.writeFileSync(target,Buffer.concat(pages));fs.copyFileSync(target,outputPath+'.raw');")
    acquired = _interleave((main, wal, output), '', driver_transform=driver)
    assert acquired['ok'], acquired
    assert acquired['mergedConsumed'] and not acquired['mainOnly']
    assert acquired['stagingCleaned'] and acquired['recovered'] and not acquired['encryptedPublished']
    assert [p.read_bytes() for p in (main, wal, Path(f'{main}-shm'))] == original
raw, final = Path(f'{output}.raw').read_bytes(), output.read_bytes()
assert len(final) == 8 * 4096
assert int.from_bytes(final[28:32], 'big') == 2
assert final[24:28] == final[92:96]
assert raw[18:20] == b'\x02\x02' and final[18:20] == b'\x01\x01'
assert raw[:18] == final[:18] and raw[20:] == final[20:]
# Deny the sidecar namespace: the published file must stand alone.
Path(f'{output}-wal').mkdir()
Path(f'{output}-shm').mkdir()
with closing(sqlite3.connect(f'{output.as_uri()}?mode=ro', uri=True)) as reader:
    assert reader.execute('PRAGMA page_count').fetchone() == (2,)
    assert reader.execute('PRAGMA quick_check').fetchall() == [('ok',)]
    assert reader.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() == [('entries',)]
    assert reader.execute('SELECT id,body FROM entries').fetchall() == [(1,'fictional selected')]
print('pinned SQLite 3.54.1: physical=8 header=2 page_count=2 quick_check=ok query=ok')
""", str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('mutation', [
    "fs.truncateSync(target,4096);",
    "fs.appendFileSync(target,Buffer.alloc(4096));",
    "const b=fs.readFileSync(target);b[0]^=1;fs.writeFileSync(target,b);",
    "const b=fs.readFileSync(target);b.writeUInt16BE(1024,16);fs.writeFileSync(target,b);",
    "const b=fs.readFileSync(target);b[19]=3;fs.writeFileSync(target,b);",
    "const b=fs.readFileSync(target);b[19]=1;fs.writeFileSync(target,b);",
    "const b=fs.readFileSync(target);b.writeUInt32BE(99,28);fs.writeFileSync(target,b);",
    "fs.writeFileSync(target+'-wal','fictional unexpected WAL');",
    "fs.linkSync(target,outputPath+'.alias');",
])
def test_invalid_decrypted_image_is_not_published(fictional_source, mutation):
    def driver(program):
        return _formal(program).replace(
            'fs.writeFileSync(target,Buffer.concat(pages));',
            'fs.writeFileSync(target,Buffer.concat(pages));' + mutation)

    result = _interleave(fictional_source, '', driver_transform=driver)
    assert not result['ok'] and result['code'] == 'decrypt_failed', result
    assert result['manifest'] is None
    assert result['stagingCleaned'] and result['recovered']
    assert not fictional_source[2].exists()


@pytest.mark.parametrize('corrupt', [False, True])
def test_encrypted_wal_capture_reaches_sessions_and_report(tmp_path, corrupt):
    """Drive synthetic encrypted pages through publication and the real facade."""
    import sqlite3
    from contextlib import closing
    from test_qq_direct_main_wal_poc import _reserve_48_bytes, _run
    from test_qq_direct_database_facade import _create_plaintext_snapshot
    from qq_direct_db_testing import FakeSnapshotRuntime
    from qq_chat_analyzer.application.analysis_service import AnalysisApplicationService
    from qq_chat_analyzer.application.facade import ChatAnalyzerFacade, ChatSource, AnalysisConfig, FacadeError
    from qq_chat_analyzer.application.qq.qq_direct_database_import_service import QQDirectDatabaseImportService

    plain, output = tmp_path / 'plain.db', tmp_path / 'snapshot.db'
    source = tmp_path / 'source'
    source.mkdir()
    main, wal = source / 'encrypted.db', source / 'encrypted.db-wal'
    _reserve_48_bytes(plain)
    _create_plaintext_snapshot(plain)
    with closing(sqlite3.connect(plain)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        # Include page growth and overflow pages in the committed view.
        writer.execute('CREATE TABLE padding(body TEXT)')
        writer.execute('INSERT INTO padding VALUES (?)', ('fictional ' * 9000,))
        writer.execute('UPDATE group_msg_table SET "40050"="40050"+1')
        writer.commit()
        _run('fixture', plain, Path(f'{plain}-wal'), main, wal)
        assert _acquire((main, wal, output))['ok']
    if corrupt:
        data = bytearray(output.read_bytes())
        data[4096] = 0xff  # Invalid b-tree page type; a valid header cannot hide it.
        output.write_bytes(data)
    runtime = FakeSnapshotRuntime(tmp_path / 'consumer', snapshot_path=output)
    facade = ChatAnalyzerFacade(
        qq_service=QQDirectDatabaseImportService(runtime_client=runtime),
        analysis_service=AnalysisApplicationService(),
        stopwords_directory=Path(__file__).resolve().parents[1],
    )
    if corrupt:
        with pytest.raises(FacadeError) as caught:
            facade.list_sessions(ChatSource.QQ)
        assert caught.value.code == 'qq_direct_snapshot_invalid'
    else:
        sessions = {s.session_type: s for s in facade.list_sessions(ChatSource.QQ)}
        assert set(sessions) == {'group', 'private'}
        for kind, session in sessions.items():
            outcome = facade.analyze_session(ChatSource.QQ, session.session_id,
                                            AnalysisConfig(output_directory=tmp_path / f'{kind}-report'))
            assert outcome.result.processed_message_count == 1
            assert outcome.report_path.is_file()
    assert runtime.cleaned == runtime.acquired
    assert not any((runtime.root / 'generations').glob('*'))


@pytest.mark.parametrize('mode', ['merged-append', 'merged-replace'])
def test_formal_changed_decrypt_input_never_publishes(fictional_source, mode):
    result = _acquire(fictional_source, mode=mode)
    assert result['ok'] is False and result['code'] == 'snapshot_unstable'
    assert result['decryptCalls'] == 1 and result['mergedConsumed']
    assert result['manifest'] is None and not result['encryptedPublished']
    assert result['stagingCleaned'] and result['recovered']
    assert not fictional_source[2].exists()


@pytest.mark.parametrize("action", [
    "const b=fs.readFileSync(mainPath); b[1100]^=1; fs.writeFileSync(mainPath,b);",
    "const b=fs.readFileSync(mainPath+'-shm'); b.writeUInt32LE(1,128); fs.writeFileSync(mainPath+'-shm',b);",
    "fs.truncateSync(walPath,32);",
    "const b=fs.readFileSync(walPath); fs.renameSync(walPath,walPath+'.retired'); fs.writeFileSync(walPath,b);",
])
def test_formal_invalid_capture_fails_closed_without_decrypt(fictional_source, action):
    result = _acquire(fictional_source, action, phase="main_baseline_read")
    assert result["ok"] is False, result
    assert result["code"] == "snapshot_unstable"
    assert result["decryptCalls"] == 0 and not result["mainOnly"]
    assert result["stagingCleaned"] and result["recovered"]
    assert not fictional_source[2].exists()


@pytest.mark.parametrize('mode', ['decrypt-failure', 'decrypt-throw'])
def test_formal_decrypt_failure_cleans_both_artifacts(fictional_source, mode):
    result = _acquire(fictional_source, mode=mode)
    assert result["ok"] is False and result["code"] == "decrypt_failed"
    assert result["decryptCalls"] == 1 and result["mergedConsumed"]
    assert result["stagingCleaned"] and result["recovered"]


def test_formal_cleanup_failure_is_reported_and_recovery_stays_closed(fictional_source):
    result = _acquire(fictional_source, mode='decrypt-cleanup-failure')
    assert not result['ok'] and result['code'] == 'cleanup_failed'
    assert not result['recovered'] and not result['stagingCleaned']
    assert (fictional_source[2].parent / 'formal-root/staging/owner.json').is_file()
    assert not fictional_source[2].exists()
