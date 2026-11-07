"""Bounded upstream decryption, using only fictional keys and page contents."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "scripts/qq_napcat_runtime_pins.json"
CAPSULE = ROOT / "tests/fixtures/napcat_41834_database_crypto.mjs"


def run_node(tmp_path, program, *, timeout=20):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for upstream decryption contract")
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    edit = next(edit for edit in pins["napcatPatch"]["replacements"]
                if edit["anchor"].startswith("function zE(t, e, n) {"))
    driver = tmp_path / "decrypt-test.mjs"
    driver.write_text("""
import fs from 'node:fs';
import * as xe from 'node:path';
import {createCipheriv,createDecipheriv as GL,pbkdf2Sync as aI} from 'node:crypto';
import assert from 'node:assert/strict';
let $e=fs;
""" + CAPSULE.read_text(encoding="utf-8") + "\n" + edit["replacement"] + "\n" + """
const passphrase=Buffer.from('fictional-only'),salt=Buffer.alloc(16,17);
const {encKey}=bY(passphrase,salt);
function encryptedPage(first) {
  const plain=Buffer.alloc(first?4032:4048,91),iv=Buffer.alloc(16,43);
  const cipher=createCipheriv('aes-256-cbc',encKey,iv);cipher.setAutoPadding(false);
  return Buffer.concat([...(first?[salt]:[]),cipher.update(plain),cipher.final(),iv,Buffer.alloc(32)]);
}
const firstPage=encryptedPage(true),otherPage=encryptedPage(false);
""" + program, encoding="utf-8")
    result = subprocess.run([node, "--max-old-space-size=64", str(driver)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("pages", [1, 257])
def test_streamed_output_matches_upstream_bytes_and_reserved_tail(tmp_path, pages):
    result = run_node(tmp_path, f"const pages={pages};\n" + """
const prefix=Buffer.alloc(T8);yY.copy(prefix);
const encrypted=Buffer.concat([prefix,firstPage,...Array(pages-1).fill(otherPage)]);
fs.writeFileSync('source.db',encrypted);
const expected=WE(encrypted,passphrase);
let opened=0,closed=0;
$e={...fs,openSync(...args){opened++;return fs.openSync(...args);},
  closeSync(fd){closed++;fs.closeSync(fd);},
  // OS reads and writes need not consume a complete request.
  readSync(fd,b,o,n,p){return fs.readSync(fd,b,o,Math.min(n,379),p);},
  writeSync(fd,b,o,n,p){return fs.writeSync(fd,b,o,Math.min(n,509),p);}};
assert.equal(zE('source.db',passphrase),'source_decrypted.db');
const actual=fs.readFileSync('source_decrypted.db');assert.deepEqual(actual,expected);
for(let p=0;p<pages;p++)assert.deepEqual(actual.subarray((p+1)*Qs-48,(p+1)*Qs),Buffer.alloc(48));
assert.equal(opened,2);assert.equal(closed,opened);
console.log(JSON.stringify({bytes:actual.length,matches:true,closed}));
""")
    assert result == {"bytes": pages * 4096, "matches": True, "closed": 2}


@pytest.mark.parametrize("operation", ["read", "write"])
def test_decrypt_io_failure_releases_both_handles(tmp_path, operation):
    result = run_node(tmp_path, f"const operation={json.dumps(operation)};\n" + """
const prefix=Buffer.alloc(T8);yY.copy(prefix);fs.writeFileSync('source.db',Buffer.concat([prefix,firstPage]));
let opened=0,closed=0;
$e={...fs,openSync(...args){opened++;return fs.openSync(...args);},
  closeSync(fd){closed++;fs.closeSync(fd);},
  readSync(fd,b,o,n,p){if(operation==='read' && n===Qs)throw Object.assign(Error('fictional'),{code:'EIO'});return fs.readSync(fd,b,o,n,p);},
  writeSync(){throw Object.assign(Error('fictional'),{code:'ENOSPC'});}};
assert.throws(()=>zE('source.db',passphrase,'target.db'),{code:operation==='read'?'EIO':'ENOSPC'});
assert.equal(opened,2);assert.equal(closed,opened);
console.log(JSON.stringify({closed}));
""")
    assert result == {"closed": 2}


@pytest.mark.slow_integration
def test_six_gib_decrypt_keeps_buffers_bounded_and_positions_above_four_gib(tmp_path):
    # A virtual file drives every real AES page through the production patch.
    # Only the filesystem is virtual, avoiding 12 GiB of disposable disk writes.
    result = run_node(tmp_path, """
const pages=1618236,bytes=T8+pages*Qs,batch=Buffer.alloc(256*Qs);
for(let p=0;p<256;p++)otherPage.copy(batch,p*Qs);
let written=0,closed=0,readBeyondFourGib=false,writeBeyondFourGib=false,maxRequest=0;
$e={openSync(path){return path==='source.db'?1:2;},fstatSync(){return {size:bytes};},
  readFileSync(){throw Object.assign(Error('whole file read'),{code:'ERR_FS_FILE_TOO_LARGE'});},
  readSync(fd,target,at,length,position){
    assert.equal(fd,1);assert.ok(length<=batch.length);maxRequest=Math.max(maxRequest,length);
    if(position===0){yY.copy(target,at);return length;}
    if(length===CN){salt.copy(target,at);return length;}
    assert.equal((position-T8)%Qs,0);batch.copy(target,at,0,length);
    if(position===T8)firstPage.copy(target,at);
    readBeyondFourGib ||= position>2**32;return length;
  },
  writeSync(fd,buffer,at,length,position){
    assert.equal(fd,2);assert.equal(position,written);assert.ok(length<=batch.length);
    if(position===0)assert.deepEqual(buffer.subarray(at,at+16),vY);
    assert.deepEqual(buffer.subarray(at+32,at+64),Buffer.alloc(32,91));
    assert.deepEqual(buffer.subarray(at+length-48,at+length),Buffer.alloc(48));
    writeBeyondFourGib ||= position>2**32;written+=length;return length;
  },closeSync(){closed++;}};
const allocate=Buffer.alloc;
Buffer.alloc=function(size,...args){assert.ok(size<=1024*1024,'unbounded allocation');return allocate(size,...args);};
const start=performance.now();assert.equal(zE('source.db',passphrase,'target.db'),'target.db');
assert.equal(written,pages*Qs);assert.equal(closed,2);
assert.ok(readBeyondFourGib && writeBeyondFourGib);
console.log(JSON.stringify({bytes:written,maxRequest,readBeyondFourGib,writeBeyondFourGib,
  elapsedMs:Math.round(performance.now()-start),peakRssKiB:process.resourceUsage().maxRSS}));
""", timeout=120)
    assert result["bytes"] == 1618236 * 4096
    assert result["maxRequest"] <= 1024 * 1024
    assert result["readBeyondFourGib"] and result["writeBeyondFourGib"]
    print(json.dumps(result, sort_keys=True))
