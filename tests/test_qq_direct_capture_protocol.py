"""BUG-02: fictional SQLite lifecycle, Windows source locks and cancellation."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_qq_direct_main_wal_poc import _interleave, _run, _reserve_48_bytes, fictional_source as poc_source
from test_qq_direct_main_wal_acquisition import fictional_source, _formal

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.slow_integration,
              pytest.mark.skipif(sys.platform != 'win32', reason='Windows SQLite lock protocol')]


def test_native_sqlite_guard_pins_main_and_generation_but_allows_append(tmp_path):
    """Use the shipped DLL and production guard in separate processes."""
    result = subprocess.run([sys.executable, '-c', r'''
import ctypes, hashlib, json, subprocess, sys
from pathlib import Path
root, work = map(Path, sys.argv[1:])
pins = json.loads((root/'scripts/sqlite_runtime_pins.json').read_text())
dll = root/'runtime/sqlite/sqlite3.dll'
assert hashlib.sha256(dll.read_bytes()).hexdigest() == pins['dllSha256']
library = ctypes.CDLL(str(dll))
import sqlite3
assert sqlite3.sqlite_version == pins['version'] == '3.54.1'
plain = work/'fictional.db'
writer = sqlite3.connect(plain, timeout=0)
writer.execute('PRAGMA journal_mode=WAL')
writer.execute('PRAGMA wal_autocheckpoint=0')
writer.execute('CREATE TABLE data(id INTEGER PRIMARY KEY, body TEXT)')
writer.execute("INSERT INTO data VALUES(1,'original')")
writer.commit()
assert writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0] == 0
old = Path(str(plain)+'-wal').read_bytes()
main = plain.read_bytes()
program = r"""
import {acquireSourceGuard} from './scripts/qq_direct_db_snapshot/source_guard.mjs';
import {createInterface} from 'node:readline';
const main=process.argv[1];
const guard=await acquireSourceGuard(main,main+'-wal');
const before=await guard.readWitness();
console.log(JSON.stringify({ready:true,boundary:before.readUInt32LE(16)}));
await new Promise(resolve=>{const lines=createInterface({input:process.stdin});lines.once('line',()=>{lines.close();resolve();});});
const after=await guard.readWitness();
await guard.release();
process.stdin.pause();
console.log(JSON.stringify({released:true,generationStable:before.subarray(32,40).equals(after.subarray(32,40)),
  boundary:after.readUInt32LE(16),backfillStable:before.readUInt32LE(96)===after.readUInt32LE(96)}));
"""
child = subprocess.Popen(['node','--input-type=module','-e',program,str(plain)],
    cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
try:
    before = json.loads(child.stdout.readline())
    assert before['ready']
    writer.execute("UPDATE data SET body='appended' WHERE id=1")
    writer.commit()
    current = Path(str(plain)+'-wal').read_bytes()
    assert old[:32] == current[:32]  # A normal writer would reset the fully backfilled WAL.
    assert len(current) > len(old)
    checkpoints = {}
    for mode in ('PASSIVE','FULL','RESTART','TRUNCATE'):
        checkpoint = writer.execute('PRAGMA wal_checkpoint('+mode+')').fetchone()
        assert checkpoint[0] == 1, (mode,checkpoint)
        checkpoints[mode] = checkpoint
    assert plain.read_bytes() == main
    assert writer.execute('SELECT body FROM data').fetchone() == ('appended',)
    # A second native connection can read and cannot upgrade into a reset.
    other = sqlite3.connect(plain, timeout=0)
    assert other.execute('SELECT body FROM data').fetchone() == ('appended',)
    other.close()
    child.stdin.write('release\n');child.stdin.flush();child.stdin.close()
    after = json.loads(child.stdout.readline())
    assert after['released'] and after['generationStable'] and after['backfillStable']
    assert after['boundary'] > before['boundary']
    assert child.wait(timeout=5) == 0, child.stderr.read()
finally:
    if not child.stdin.closed: child.stdin.close()
    child.wait(timeout=5)
assert writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0] == 0
writer.execute("UPDATE data SET body='new generation' WHERE id=1")
writer.commit()
assert Path(str(plain)+'-wal').read_bytes()[16:24] != current[16:24]
writer.close()
print(json.dumps({'sqlite':sqlite3.sqlite_version,'events':['guard_ready','append_commit',
    'checkpoint_refused','guard_released','checkpoint_succeeded','wal_reused'],
    'old_boundary':before['boundary'],'guarded_boundary':after['boundary'],
    'generation_stable_while_guarded':True,'main_stable':True}))
''', str(ROOT), str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['guarded_boundary'] > evidence['old_boundary']
    assert evidence['events'][-1] == 'wal_reused'


@pytest.mark.parametrize('kind,expected', [('reuse-tail','wal_frame_salt_invalid'),
                                         ('noncommit','wal_boundary_not_committed')])
def test_unlocked_old_boundary_crosses_native_reset(tmp_path, kind, expected):
    """Control: the old unlocked capture must still reject mixed generations."""
    import sqlite3
    plain = tmp_path/'plain.db'
    _reserve_48_bytes(plain)
    writer = sqlite3.connect(plain)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE entries(id INTEGER PRIMARY KEY,body TEXT)')
        writer.executemany('INSERT INTO entries VALUES(?,?)', [(i,'a'*3000) for i in range(8)])
        writer.commit()
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        for letter in ('b','c','d'):
            writer.execute('UPDATE entries SET body=? WHERE id=1',(letter*3000,))
            writer.commit()
        old = Path(f'{plain}-wal').read_bytes()
        boundary = (len(old)-32)//4120
        main, wal = tmp_path/'encrypted.db',tmp_path/'encrypted.db-wal'
        _run('fixture',plain,Path(f'{plain}-wal'),main,wal)
        assert writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone() == (0,boundary,boundary)
        writer.execute('UPDATE entries SET body=?'+(' WHERE id=1' if kind=='reuse-tail' else ''),('z'*3000,))
        writer.commit()
        new = Path(f'{plain}-wal').read_bytes()
        selected = int.from_bytes(Path(f'{plain}-shm').read_bytes()[16:20],'little')
        assert old[16:24] != new[16:24]
        if kind=='reuse-tail':
            assert selected < boundary
        else:
            assert selected > boundary
            assert int.from_bytes(new[32+(boundary-1)*4120+4:][:4],'big') == 0
        prefix = tmp_path/'prefix.wal'
        prefix.write_bytes(new[:32+selected*4120])
        next_main,next_wal = tmp_path/'next.db',tmp_path/'next.db-wal'
        _run('fixture',plain,prefix,next_main,next_wal)
        if kind=='reuse-tail':
            with next_wal.open('ab') as stream:
                stream.write(wal.read_bytes()[32+selected*4120:])
        action = 'for(const suffix of ["","-wal","-shm"])fs.copyFileSync(mainPath.replace("encrypted.db","next.db")+suffix,mainPath+suffix);'
        observed = _interleave((main,wal,tmp_path/'output.db'),action,mode='merge',phase='main_baseline_read')
        assert observed['fired'] and not observed['ok'] and observed['code'] == expected, observed
        assert not (tmp_path/'output.db').exists()
    finally:
        writer.close()


@pytest.mark.parametrize('mutation', [
    'const s=fs.statSync(mainPath);fs.utimesSync(mainPath,s.atime,new Date(s.mtimeMs+2000));',
    'fs.renameSync(mainPath,mainPath+".retired");fs.copyFileSync(mainPath+".retired",mainPath);',
    'const b=fs.readFileSync(mainPath);b[1200]^=1;fs.writeFileSync(mainPath,b);',
])
def test_main_guards_remain_independent_of_source_lock(fictional_source, mutation):
    observed = _interleave(fictional_source, mutation, mode='merge',phase='main_baseline_read')
    assert not observed['ok'] and observed['code']=='main_changed_during_read'
    assert not fictional_source[2].exists()


@pytest.mark.parametrize('phase', ['main_baseline_read','output_page_written'])
def test_capture_cancel_releases_source_before_any_decrypt(fictional_source, phase):
    def driver(program):
        return _formal(program).replace(
            'const result=await api.acquire();',
            'const result=await api.acquire({signal:controller.signal});').replace(
            'let fired = false;', 'const controller=new AbortController();let fired = false;')
    observed = _interleave(fictional_source, 'setImmediate(()=>controller.abort());',
                           phase=phase,driver_transform=driver)
    assert not observed['ok'] and observed['decryptCalls']==0
    assert observed['stagingCleaned'] and observed['recovered']
    # Exclusive rename needs every guard handle gone; no PID/kill assumptions.
    main = fictional_source[0]
    main.rename(main.with_suffix('.released'))


def test_release_without_acknowledgement_discards_completed_merge(fictional_source):
    def driver(program):
        setup = r'''
const builtin=await import('node:module');
const cp=builtin.createRequire(import.meta.url)('node:child_process'),spawn=cp.spawn;
cp.spawn=(exe,args,opts)=>{
  const child=spawn(exe,args,opts);
  if(args.at(-1).endsWith('source_guard.ps1')) {
    const write=child.stdin.write.bind(child.stdin);
    child.stdin.write=(data,...rest)=>write(data==='release\n'?'invalid\n':data,...rest);
  }
  return child;
};
builtin.syncBuiltinESMExports();
'''
        return _formal(program).replace('let fired = false;',setup+'let fired = false;')
    observed = _interleave(fictional_source,'',driver_transform=driver)
    assert not observed['ok'] and observed['decryptCalls']==0
    assert observed['stagingCleaned'] and observed['recovered']


def test_busy_source_releases_partial_lock_set(fictional_source):
    import msvcrt
    main,_,_ = fictional_source
    with Path(f'{main}-shm').open('r+b') as shm:
        shm.seek(121)
        msvcrt.locking(shm.fileno(),msvcrt.LK_NBLCK,1)
        try:
            observed = _interleave(fictional_source,'',driver_transform=_formal)
            assert not observed['ok'] and observed['decryptCalls']==0
            assert observed['stagingCleaned'] and observed['recovered']
        finally:
            shm.seek(121)
            msvcrt.locking(shm.fileno(),msvcrt.LK_UNLCK,1)
    main.rename(main.with_suffix('.released'))


def test_cancelled_queued_acquire_preserves_existing_consumer_lease(fictional_source):
    def driver(program):
        return _formal(program).replace('const result=await api.acquire();',r'''
  const first=api.acquire(), controller=new AbortController();
  const queued=api.acquire({signal:controller.signal});controller.abort();
  const result=await first;
  if((await queued).ok) throw new Error('cancelled request ran');
  const {createWorkspace}=await import('./scripts/qq_direct_db_snapshot/workspace.mjs');
  const foreign=createWorkspace(root);let refused=false;
  try {await foreign.acquireLease();} catch {refused=true;} finally {await foreign.release();}
  if(!refused) throw new Error('consumer lease was released');
''')
    observed = _interleave(fictional_source,'',driver_transform=driver)
    assert observed['ok'] and observed['decryptCalls']==1
    assert observed['stagingCleaned'] and observed['recovered']


def test_guard_receives_unicode_source_paths_without_console_codepage_loss(fictional_source):
    main,wal,output = fictional_source
    directory = main.parent.with_name('fictional 中文 源 # %')
    main.parent.rename(directory)
    observed = _interleave((directory/main.name,directory/wal.name,output),'',driver_transform=_formal)
    assert observed['ok'] and observed['decryptCalls']==1, observed
    assert observed['stagingCleaned'] and observed['recovered']


@pytest.mark.parametrize('stop', ['eof','expiry'])
def test_guard_parent_loss_or_expiry_releases_kernel_locks(fictional_source, stop):
    main,wal,_ = fictional_source
    ps = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    child = subprocess.Popen([str(ps),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
        '-File',str(ROOT/'scripts/qq_direct_db_snapshot/source_guard.ps1')],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        child.stdin.write(json.dumps({'main':str(main),'wal':str(wal),'budget':500 if stop=='expiry' else 65000})+'\n')
        child.stdin.flush()
        assert child.stdout.readline().strip()=='ready'
        if stop=='eof': child.stdin.close()
        assert child.wait(timeout=5)==1
        assert child.stdout.read().strip()=='failed'
        main.rename(main.with_suffix('.released'))
    finally:
        if not child.stdin.closed: child.stdin.close()
        child.wait(timeout=5)
