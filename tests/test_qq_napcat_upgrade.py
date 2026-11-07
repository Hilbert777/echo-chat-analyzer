"""4.18.34 adaptation: fictional executable capsule, optional official ZIP audit.

Never launch the upstream entry, QQ, or use an account/credential.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from test_qq_napcat_runtime_bootstrap import ROOT, PINS, MANIFEST, load_builder


@pytest.mark.parametrize("early", [True, False])
def test_41834_key_capture_survives_both_arrival_orders(early):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for G1 executable capsule")
    # Same three upstream lexical sites; every external dependency is fictional.
    capsule = '''
async function boot(early) {
  let listener;
  const r = {onCmd(name, fn) { listener = fn; return true; }};
  const e = {log() {}};
  let a;
  if (r.onCmd("OidbSvcTrpcTcp.0xcde_2", (D) => {
    D.inner?.value && (a = D.inner.value, e.log("ready"));
  })) {}
  listener({inner: {value: Buffer.alloc(0)}});
  if (a !== undefined) throw Error("empty key marked ready");
  if (early) listener({inner: {value: "fictional-key"}});
  const V = {core: {}, async InitNapCat() {}};
  a && (V.core.dbPassphrase = a), await V.InitNapCat();
  if (!early) listener({inner: {value: "fictional-key"}});
  if (V.core.dbPassphrase !== "fictional-key") throw Error("G1 not captured");
  const firstVersion = V.core.echoDbPassphraseVersion;
  if (firstVersion !== 1) throw Error("key version not initialized");
  listener({inner: {value: Buffer.from("fictional-key")}});
  if (V.core.echoDbPassphraseVersion !== firstVersion) throw Error("same key changed version");
  listener({inner: {value: "fictional-new-key"}});
  if (V.core.echoDbPassphraseVersion !== firstVersion + 1) throw Error("new key version not observed");
  if (V.core.dbPassphrase !== "fictional-new-key") throw Error("new key not captured");
  listener({inner: {value: Buffer.alloc(0)}});
  if (V.core.dbPassphrase !== "fictional-new-key") throw Error("empty key replaced valid key");
  if (V.core.echoDbPassphraseVersion !== firstVersion + 1) throw Error("empty key changed version");
}
const allowed = new Set([
  "napcat-plugin-builtin",
  "napcat-plugin-cleaner",
  "napcat-plugin-ssqq",
  "napcat-plugin-qce"
]);
if (!allowed.has("napcat-plugin-echo") || allowed.has("untrusted-plugin") || allowed.size !== 5)
  throw Error("whitelist scope changed");
'''
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    # Include the upstream file-decrypt site without executing it in this key test.
    capsule += "\n" + pins["napcatPatch"]["replacements"][4]["anchor"]
    patched = load_builder().apply_napcat_patch(capsule, pins["napcatPatch"])
    result = subprocess.run([node, "--input-type=module", "-e",
                             patched + f"\nawait boot({str(early).lower()});"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.slow_integration
def test_official_41834_archive_and_distribution_contract(tmp_path):
    archive = os.environ.get("ECHO_NAPCAT_AUDIT_ARCHIVE")
    if not archive:
        pytest.skip("Set ECHO_NAPCAT_AUDIT_ARCHIVE to the official 4.18.34 ZIP")
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    contract = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bundle_path = Path(archive)
    assert hashlib.sha256(bundle_path.read_bytes()).hexdigest() == (
        "960cc7b0ef71125dcfa89f5c21754fa1401051082a849bff9dd6925d2b2391d3")
    assert bundle_path.stat().st_size == 29500688
    with zipfile.ZipFile(bundle_path) as bundle:
        text = bundle.read("napcat.mjs").decode("utf-8")
        crypto_capsule = (ROOT / "tests/fixtures/napcat_41834_database_crypto.mjs").read_text(encoding="utf-8")
        assert crypto_capsule[crypto_capsule.index("const T8 ="):].rstrip() in text
        # Both PacketBackend and the passphrase listener's PacketHandler need
        # exact-build offsets; merely passing the minimum QQ build is insufficient.
        offsets = re.findall(r'"9\.9\.36-53644-x64": \{ send: "([0-9A-F]+)", recv: "([0-9A-F]+)" \}', text)
        assert offsets == [("C3EE2F", "24A6355"), ("38255F0", "3828E30")]
        assert '"9.9.36-53644": { appid: 537391628, qua: "V1_WIN_NQ_9.9.36_53644_GW_B" }' in text
        for name, expected in pins["requiredFiles"].items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == expected, name
        notices = (ROOT / "third_party/napcat/NPM-LICENSES.txt").read_text(encoding="utf-8")
        notice_blocks = notices.split("========================================================================\n")[1:]
        packages = [n for n in bundle.namelist()
                    if n.startswith("node_modules/") and n.endswith("/package.json")]
        assert len(packages) == 69
        assert len(notice_blocks) == 69
        for name in packages:
            data = json.loads(bundle.read(name))
            directory = name.removesuffix("/package.json")
            block = (f"Package: {data['name']}\nVersion: {data['version']}\n"
                     f"Path: runtime/qq-napcat-candidate/{directory}\n")
            notice_block = next((b for b in notice_blocks if block in b), "")
            assert notice_block, directory
            assert f"Declared license: {data['license']}\n" in notice_block
            licenses = [n for n in bundle.namelist() if n.startswith(directory + "/")
                        and "/" not in n[len(directory) + 1:]
                        and Path(n).name.lower().startswith(("license", "copying"))]
            assert licenses, directory
            for license_name in licenses:
                assert bundle.read(license_name).decode("utf-8").strip() in notice_block, license_name
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for source in [PINS.name, MANIFEST.name, *(t["source"] for t in pins["templates"])]:
        destination = scripts / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "scripts" / source, destination)
    target = load_builder().build_runtime(tmp_path, bundle_path)
    assert all((target / n).is_file() for n in contract["requiredFiles"])
    assert all(not (target / n).exists() for n in contract["privatePaths"] + contract["forbiddenPaths"])
    assert hashlib.sha256((target / "napcat.mjs").read_bytes()).hexdigest() == pins["napcatPatch"]["patchedSha256"]
    for template in pins["templates"]:
        assert (target / template["target"]).read_bytes() == (ROOT / "scripts" / template["source"]).read_bytes()
    node = shutil.which("node")
    if node:
        result = subprocess.run([node, "--check", str(target / "napcat.mjs")],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
