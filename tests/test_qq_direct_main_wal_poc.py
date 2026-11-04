"""Fictional encrypted SQLite/WAL coverage for the standalone Stage 3.5H PoC."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "qq_direct_db_snapshot" / "main_wal_poc.mjs"
FICTIONAL_PASSPHRASE = "fictional-local-passphrase"
pytestmark = pytest.mark.slow_integration


@pytest.fixture
def fictional_source(tmp_path: Path):
    plain = tmp_path / "plain.db"
    _reserve_48_bytes(plain)
    writer = sqlite3.connect(plain)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE entries(id INTEGER PRIMARY KEY, body TEXT)")
        writer.execute("INSERT INTO entries VALUES(1, 'fictional baseline')")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("UPDATE entries SET body='fictional selected' WHERE id=1")
        writer.commit()
        main, wal = tmp_path / "encrypted.db", tmp_path / "encrypted.db-wal"
        _run("fixture", plain, Path(f"{plain}-wal"), main, wal)
        yield main, wal, tmp_path / "snapshot.db"
    finally:
        writer.close()


def _interleave(source, action: str, *, mode="snapshot", phase="boundary_selected", driver_transform=None):
    main, wal, output = source
    # In-process barriers are deterministic: the mutation completes exactly at
    # the selected read phase, without sleeps or accessing any real QQ data.
    program = """
import * as fs from 'node:fs';
import {spawnSync} from 'node:child_process';
import {createCipheriv, createDecipheriv, pbkdf2Sync} from 'node:crypto';
import { createSnapshot, createMergedEncryptedDatabase } from './scripts/qq_direct_db_snapshot/main_wal_poc.mjs';
const [mainPath, walPath, outputPath, phase, mode] = process.argv.slice(1);
function checksum(b, little, pair=[0,0]) {
  let [a,c]=pair;
  for(let i=0;i<b.length;i+=8) {
    a=(a+(little?b.readUInt32LE(i):b.readUInt32BE(i))+c)>>>0;
    c=(c+(little?b.readUInt32LE(i+4):b.readUInt32BE(i+4))+a)>>>0;
  }
  return [a,c];
}
function updateIndex(b) {
  const sum=checksum(b.subarray(0,40),true);
  b.writeUInt32LE(sum[0],40); b.writeUInt32LE(sum[1],44);
  b.copy(b,48,0,48);
  // SQLite publishes the wal-index through a mapping, including while byte
  // locks are held. A whole-file WriteFile incorrectly conflicts with those.
  const updated=spawnSync(process.env.ECHO_TEST_PYTHON,['-c',
    'import sys,mmap,base64;f=open(sys.argv[1],"r+b");m=mmap.mmap(f.fileno(),0);b=base64.b64decode(sys.argv[2]);m[:len(b)]=b;m.close();f.close()',
    mainPath+'-shm',b.toString('base64')],{encoding:'utf8'});
  if(updated.status!==0) throw new Error('fictional wal-index publish');
}
function appendFrame(committed, publish=true) {
  const wal=fs.readFileSync(walPath);
  const last=wal.subarray(wal.length-4120);
  const next=Buffer.from(last);
  if(next.readUInt32BE(0)!==1) {
    const salt=fs.readFileSync(mainPath).subarray(1024,1040);
    const key=pbkdf2Sync(process.env.ECHO_POC_PASSPHRASE,salt,4000,32,'sha512');
    const iv=next.subarray(24+4048,24+4064);
    const decrypt=createDecipheriv('aes-256-cbc',key,iv); decrypt.setAutoPadding(false);
    const plain=Buffer.concat([decrypt.update(next.subarray(24,24+4048)),decrypt.final()]);
    const offset=plain.indexOf(Buffer.from('fictional selected'));
    if(offset>=0) Buffer.from('fictional appended').copy(plain,offset);
    const encrypt=createCipheriv('aes-256-cbc',key,iv); encrypt.setAutoPadding(false);
    Buffer.concat([encrypt.update(plain),encrypt.final()]).copy(next,24);
  }
  if(!committed) next.writeUInt32BE(0,4);
  const sum=checksum(Buffer.concat([next.subarray(0,8),next.subarray(24)]),
    wal.readUInt32BE(0)===0x377f0682,[last.readUInt32BE(16),last.readUInt32BE(20)]);
  next.writeUInt32BE(sum[0],16); next.writeUInt32BE(sum[1],20);
  fs.appendFileSync(walPath,next);
  if(committed && publish) {
    const b=fs.readFileSync(mainPath+'-shm');
    b.writeUInt32LE(b.readUInt32LE(16)+1,16);
    b.writeUInt32LE(sum[0],24); b.writeUInt32LE(sum[1],28);
    updateIndex(b);
  }
}
let fired = false;
const onPhase = (current) => {
  if (current !== phase || fired) return;
  fired = true;
  ACTION
};
try {
  const fn = mode === 'merge' ? createMergedEncryptedDatabase : createSnapshot;
  const result = await fn({mainPath, walPath, outputPath, passphrase: process.env.ECHO_POC_PASSPHRASE, onPhase});
  console.log(JSON.stringify({ok:true, fired, ...result}));
} catch (error) { console.log(JSON.stringify({ok:false, fired, code:error.code ?? 'interrupted'})); }
""".replace("ACTION", action)
    if driver_transform is not None:
        program = driver_transform(program)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program, str(main), str(wal),
         str(output), phase, mode], cwd=SCRIPT.parents[2], capture_output=True,
        text=True, timeout=30,
        env=dict(os.environ, ECHO_POC_PASSPHRASE=FICTIONAL_PASSPHRASE, ECHO_TEST_PYTHON=sys.executable),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("action", [
    "const b=fs.readFileSync(mainPath); b[1100]^=1; const s=fs.statSync(mainPath); fs.writeFileSync(mainPath,b); fs.utimesSync(mainPath,s.atime,s.mtime);",
    "fs.truncateSync(walPath,32);",
    "throw new Error('fictional interruption');",
])
def test_read_interleaving_fails_closed_and_cleans(fictional_source, action):
    result = _interleave(fictional_source, action, mode="merge")
    assert result["fired"] is True
    assert result["ok"] is False
    assert not fictional_source[2].exists()


def test_large_wal_is_read_in_bounded_batches_without_changing_snapshot(fictional_source):
    # Hundreds of tiny synchronous reads enlarge the live consistency window.
    # Use real, checksum-valid fictional frames; bound I/O rather than timing.
    def driver(program):
        setup = """
  const wal=fs.readFileSync(walPath), last=wal.subarray(wal.length-4120), extra=[];
  let running=[last.readUInt32BE(16),last.readUInt32BE(20)];
  for(let i=0;i<600;i++) {
    const next=Buffer.from(last);
    running=checksum(Buffer.concat([next.subarray(0,8),next.subarray(24)]),
      wal.readUInt32BE(0)===0x377f0682,running);
    next.writeUInt32BE(running[0],16);next.writeUInt32BE(running[1],20);extra.push(next);
  }
  fs.appendFileSync(walPath,Buffer.concat(extra));
  const index=fs.readFileSync(mainPath+'-shm');
  index.writeUInt32LE(index.readUInt32LE(16)+600,16);
  index.writeUInt32LE(running[0],24);index.writeUInt32LE(running[1],28);updateIndex(index);
  const builtin=await import('node:module'), mutableFs=builtin.createRequire(import.meta.url)('node:fs');
  const originalOpen=mutableFs.openSync, originalRead=mutableFs.readSync, originalClose=mutableFs.closeSync;
  const walFds=new Set();let walReads=0;
  mutableFs.openSync=(p,...args)=>{const fd=originalOpen(p,...args);if(String(p)===walPath)walFds.add(fd);return fd;};
  mutableFs.readSync=(fd,...args)=>{if(walFds.has(fd))walReads++;return originalRead(fd,...args);};
  mutableFs.closeSync=fd=>{walFds.delete(fd);return originalClose(fd);};
  builtin.syncBuiltinESMExports();
"""
        return program.replace('  const result = await fn(', setup + '  const result = await fn(').replace(
            '{ok:true, fired, ...result}', '{ok:true, fired, walReads, ...result}')
    result = _interleave(fictional_source, '', driver_transform=driver)
    assert result['ok'], result
    assert result['validFrames'] > 600
    assert result['walReads'] <= 10  # Includes both full, guarded passes.
    with sqlite3.connect(fictional_source[2]) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('SELECT body FROM entries').fetchone() == ('fictional selected',)


def test_output_collision_preserves_existing_file(fictional_source):
    output = fictional_source[2]
    output.write_bytes(b"fictional owned by another operation")
    result = _interleave(fictional_source, "", mode="merge")
    assert result["ok"] is False
    assert output.read_bytes() == b"fictional owned by another operation"


def test_missing_checkpoint_witness_rejected(fictional_source):
    Path(f"{fictional_source[0]}-shm").unlink(missing_ok=True)
    result = _interleave(fictional_source, "")
    assert result["ok"] is False
    assert not fictional_source[2].exists()


@pytest.mark.parametrize("mode", ["snapshot", "merge"])
@pytest.mark.parametrize("phase", ["main_baseline_read", "output_complete"])
@pytest.mark.parametrize("action", ["appendFrame(true);", "appendFrame(true,false);", "appendFrame(false);", "fs.appendFileSync(walPath,Buffer.from('torn'));"])
def test_append_keeps_frozen_boundary(fictional_source, action, phase, mode):
    index = Path(f"{fictional_source[0]}-shm").read_bytes()
    selected = int.from_bytes(index[16:20], "little")
    result = _interleave(fictional_source, action, phase=phase, mode=mode)
    assert result["fired"] is True
    assert result["ok"] is True, result
    assert result["selectedCommitFrame"] == selected
    assert result["sourceState"]["walPrefixStable"] is True
    if mode == "snapshot":
        with sqlite3.connect(f"{fictional_source[2].as_uri()}?mode=ro", uri=True) as reader:
            assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert reader.execute("SELECT body FROM entries").fetchone() == ("fictional selected",)
        if action == "appendFrame(true);":
            latest_output = fictional_source[2].with_name("latest.db")
            latest = _run("snapshot", fictional_source[0], fictional_source[1], latest_output)
            assert latest["selectedCommitFrame"] > selected
            with sqlite3.connect(f"{latest_output.as_uri()}?mode=ro", uri=True) as reader:
                assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert reader.execute("SELECT body FROM entries").fetchone() == ("fictional appended",)


@pytest.mark.parametrize("phase", ["main_baseline_read", "output_complete"])
def test_checkpoint_progress_detected_at_each_phase(fictional_source, phase):
    action = "const b=fs.readFileSync(mainPath+'-shm'); b.writeUInt32LE(b.readUInt32LE(16),128); fs.writeFileSync(mainPath+'-shm',b);"
    result = _interleave(fictional_source, action, phase=phase)
    assert result["ok"] is False
    assert result["code"] == "checkpoint_state_changed"
    assert not fictional_source[2].exists()


@pytest.mark.parametrize("action", [
    "const b=fs.readFileSync(walPath); b[0]^=1; fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b.writeUInt32BE(3007001,4); fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b.writeUInt32BE(8192,8); fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b[24]^=1; fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b.writeUInt32BE(0,32); fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b.writeUInt32BE(0xffffffff,32); fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b[40]^=1; fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b[48]^=1; fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(walPath); b.writeUInt32BE(0,36); const sum=checksum(Buffer.concat([b.subarray(32,40),b.subarray(56)]),true,[b.readUInt32BE(24),b.readUInt32BE(28)]); b.writeUInt32BE(sum[0],48); b.writeUInt32BE(sum[1],52); fs.writeFileSync(walPath,b);",
    "const b=fs.readFileSync(mainPath+'-shm'); b[48]^=1; fs.writeFileSync(mainPath+'-shm',b);",
    "const b=fs.readFileSync(mainPath+'-shm'); b[40]^=1; b.copy(b,48,0,48); fs.writeFileSync(mainPath+'-shm',b);",
    "const b=fs.readFileSync(mainPath+'-shm'); b.writeUInt32LE(b.readUInt32LE(20)+1,20); updateIndex(b);",
    "const b=fs.readFileSync(mainPath+'-shm'); b.writeUInt32LE(2,128); fs.writeFileSync(mainPath+'-shm',b);",
    "fs.unlinkSync(mainPath+'-shm');",
])
def test_invalid_selected_source_never_falls_back(fictional_source, action):
    result = _interleave(fictional_source, action, phase="main_baseline_read")
    assert result["fired"] is True
    assert result["ok"] is False, result
    assert not fictional_source[2].exists()


def test_interruption_after_output_creation_cleans(fictional_source):
    result = _interleave(fictional_source, "throw new Error('fictional interruption');",
                         phase="output_page_written", mode="merge")
    assert result["ok"] is False
    assert not fictional_source[2].exists()


def _checksum_bytes(data: bytes, little: bool, initial=(0, 0)):
    a, b = initial
    for x, y in struct.iter_unpack("<II" if little else ">II", data):
        a = (a + x + b) & 0xFFFFFFFF
        b = (b + y + a) & 0xFFFFFFFF
    return a, b


def test_big_endian_wal_checksum_supported(fictional_source):
    main, path, output = fictional_source
    wal = bytearray(path.read_bytes())
    struct.pack_into(">I", wal, 0, 0x377F0683)
    running = _checksum_bytes(wal[:24], False)
    struct.pack_into(">II", wal, 24, *running)
    for offset in range(32, len(wal), 4120):
        running = _checksum_bytes(wal[offset:offset+8] + wal[offset+24:offset+4120], False, running)
        struct.pack_into(">II", wal, offset+16, *running)
    path.write_bytes(wal)
    index_path = Path(f"{main}-shm")
    index = bytearray(index_path.read_bytes())
    index[13] = 1
    struct.pack_into("<II", index, 24, *running)
    struct.pack_into("<II", index, 40, *_checksum_bytes(index[:40], True))
    index[48:96] = index[:48]
    index_path.write_bytes(index)
    result = _interleave(fictional_source, "")
    assert result["ok"] is True, result
    with sqlite3.connect(f"{output.as_uri()}?mode=ro", uri=True) as reader:
        assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert reader.execute("SELECT body FROM entries").fetchone() == ("fictional selected",)


@pytest.mark.parametrize("target", ["mainPath", "walPath", "mainPath+'-shm'"])
@pytest.mark.parametrize("mode", ["snapshot", "merge"])
def test_file_replacement_even_with_identical_content_rejected(fictional_source, target, mode):
    action = f"const p={target}; const b=fs.readFileSync(p); fs.renameSync(p,p+'.retired'); fs.writeFileSync(p,b);"
    result = _interleave(fictional_source, action, mode=mode)
    assert result["ok"] is False, result
    assert not fictional_source[2].exists()


def test_partial_selected_frame_rejected(fictional_source):
    result = _interleave(fictional_source,
                         "fs.truncateSync(walPath,fs.statSync(walPath).size-1);",
                         phase="main_baseline_read")
    assert result["ok"] is False
    assert result["code"] == "wal_boundary_truncated"
    assert not fictional_source[2].exists()


def test_wal_replacement_during_main_baseline_is_rejected(fictional_source):
    result = _interleave(fictional_source,
                         "const b=fs.readFileSync(walPath); fs.renameSync(walPath,walPath+'.retired'); fs.writeFileSync(walPath,b);",
                         phase='main_baseline_read', mode="merge")
    assert result['ok'] is False
    assert result['code'] == 'wal_replaced'
    assert not fictional_source[2].exists()


def _reserve_48_bytes(path: Path) -> None:
    """Set SQLite's per-page reserve before creating any fictional tables."""
    import sqlite3 as _loaded_sqlite3  # Ensure sqlite3.dll is loaded on Windows.

    assert _loaded_sqlite3.sqlite_version
    library = ctypes.CDLL("sqlite3.dll")
    library.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    library.sqlite3_open.restype = ctypes.c_int
    library.sqlite3_file_control.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p,
    ]
    library.sqlite3_file_control.restype = ctypes.c_int
    library.sqlite3_exec.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    library.sqlite3_exec.restype = ctypes.c_int
    library.sqlite3_close.argtypes = [ctypes.c_void_p]
    library.sqlite3_close.restype = ctypes.c_int
    database = ctypes.c_void_p()
    assert library.sqlite3_open(os.fsencode(path), ctypes.byref(database)) == 0
    try:
        reserved = ctypes.c_int(48)
        assert library.sqlite3_file_control(
            database, b"main", 38, ctypes.byref(reserved),
        ) == 0
        assert library.sqlite3_exec(database, b"VACUUM", None, None, None) == 0
    finally:
        assert library.sqlite3_close(database) == 0


def _run(mode: str, *paths: Path) -> dict[str, object]:
    environment = dict(os.environ, ECHO_POC_PASSPHRASE=FICTIONAL_PASSPHRASE)
    result = subprocess.run(
        ["node", str(SCRIPT), mode, *(str(path) for path in paths)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert result.returncode == 0, payload
    return payload


def test_main_plus_committed_wal_produces_consistent_plaintext_snapshot(tmp_path: Path) -> None:
    plain_main = tmp_path / "fictional.db"
    plain_wal = Path(f"{plain_main}-wal")
    encrypted_main = tmp_path / "encrypted.db"
    encrypted_wal = tmp_path / "encrypted.db-wal"
    merged_encrypted = tmp_path / "merged-encrypted.db"
    snapshot = tmp_path / "snapshot.db"

    _reserve_48_bytes(plain_main)
    writer = sqlite3.connect(plain_main)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        writer.executemany(
            "INSERT INTO entries(body) VALUES (?)",
            [(f"fictional baseline {number}" * 15,) for number in range(1000)],
        )
        writer.commit()
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

        writer.executemany(
            "INSERT INTO entries(body) VALUES (?)",
            [(f"fictional committed {number}" * 12,) for number in range(30)],
        )
        writer.commit()
        writer.execute("UPDATE entries SET body = body || ' updated' WHERE id <= 20")
        writer.commit()

        fixture = _run(
            "fixture", plain_main, plain_wal, encrypted_main, encrypted_wal,
        )
        assert fixture == {"ok": True, "fixture": True}
        merged = _run("merge", encrypted_main, encrypted_wal, merged_encrypted)
        assert merged["ok"] is True
        assert merged["commitMarkers"] >= 2
        assert merged_encrypted.stat().st_size == 1024 + merged["pages"] * 4096
        result = _run("snapshot", encrypted_main, encrypted_wal, snapshot)
        assert result["ok"] is True
        assert result["commitMarkers"] >= 2
        assert result["selectedCommitFrame"] <= result["validFrames"]
        selected_commit = result["selectedCommitFrame"]

        reader = sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)
        try:
            assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert reader.execute("SELECT count(*) FROM entries").fetchone() == (1030,)
            assert reader.execute(
                "SELECT body FROM entries WHERE id = 1",
            ).fetchone()[0].endswith(" updated")
        finally:
            reader.close()

        # Repeated reads of the same live-style WAL remain tied to the same
        # committed boundary. A torn trailing byte must not become a frame.
        second_snapshot = tmp_path / "snapshot-repeat.db"
        result = _run("snapshot", encrypted_main, encrypted_wal, second_snapshot)
        assert result["selectedCommitFrame"] == selected_commit
        with encrypted_wal.open("ab") as wal_tail:
            wal_tail.write(b"x")
        third_snapshot = tmp_path / "snapshot-torn-tail.db"
        result = _run("snapshot", encrypted_main, encrypted_wal, third_snapshot)
        assert result["ok"] is True
        reader = sqlite3.connect(f"{third_snapshot.as_uri()}?mode=ro", uri=True)
        try:
            assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert reader.execute("SELECT count(*) FROM entries").fetchone() == (1030,)
        finally:
            reader.close()
    finally:
        writer.close()
