"""Isolated Stage 1 runtime contracts; never start QQ or read account state."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap_qq_napcat_runtime.py"
PINS = ROOT / "scripts/qq_napcat_runtime_pins.json"
MANIFEST = ROOT / "scripts/qq_napcat_runtime_manifest.json"
ARCHIVE_SHA = "960cc7b0ef71125dcfa89f5c21754fa1401051082a849bff9dd6925d2b2391d3"
#: Fictional, in-memory only: never a real bridge credential.
FICTIONAL_TOKEN = "0123456789abcdef" * 4


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_builder():
    assert SCRIPT.is_file(), "candidate bootstrap missing"
    spec = importlib.util.spec_from_file_location("candidate_bootstrap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_pins_and_correctness_templates():
    assert PINS.is_file(), "official NapCat pins missing"
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    assert pins["upstream"]["version"] == "4.18.34"
    assert pins["upstream"]["project"] == "NapNeko/NapCatQQ"
    assert pins["upstream"]["archiveUrl"] == "https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.34/NapCat.Shell.zip"
    assert pins["upstream"]["archiveSha256"] == ARCHIVE_SHA
    assert pins["napcatPatch"]["upstreamSha256"] == "6a22c93373c8f73462658d57b00bc88164d7e5858c0fe2f178eb97d66d7cb3ba"
    assert "upstream.version -ne '4.18.34'" in (ROOT / "scripts/build_windows_exe.ps1").read_text(encoding="utf-8-sig")
    assert load_builder().OFFICIAL_URL == pins["upstream"]["archiveUrl"]
    assert "core.dbPassphrase = a" in pins["napcatPatch"]["replacements"][1]["replacement"]
    whitelist = pins["napcatPatch"]["replacements"][3]
    assert '"napcat-plugin-echo"' in whitelist["replacement"]
    assert '"napcat-plugin-echo"' not in whitelist["anchor"]
    assert len(pins["napcatPatch"]["replacements"]) == 5
    for replacement in pins["napcatPatch"]["replacements"]:
        assert "__ECHO_DIRECT_DB_READ_STATE__" not in replacement["anchor"]
        assert "__ECHO_DIRECT_DB_READ_STATE__" not in replacement["replacement"]
    for item in pins["templates"]:
        assert digest((ROOT / "scripts" / item["source"]).read_bytes()) == item["sha256"]
    for name in ("snapshot.mjs", "main_wal.mjs", "workspace.mjs"):
        assert any(item["source"] == f"qq_direct_db_snapshot/{name}" for item in pins["templates"])


@pytest.fixture
def project(tmp_path):
    assert PINS.is_file() and MANIFEST.is_file(), "candidate contract missing"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    shutil.copy2(MANIFEST, scripts / MANIFEST.name)
    for item in pins["templates"]:
        target = scripts / item["source"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "scripts" / item["source"], target)
    # Real patch anchors, fictional file bodies and archive; no external download.
    upstream = "\n".join(edit["anchor"] for edit in pins["napcatPatch"]["replacements"])
    patched = upstream
    for edit in pins["napcatPatch"]["replacements"]:
        patched = patched.replace(edit["anchor"], edit["replacement"])
    pins["napcatPatch"]["upstreamSha256"] = digest(upstream.encode())
    pins["napcatPatch"]["patchedSha256"] = digest(patched.encode())
    members = {name: f"fictional {name}\n".encode() for name in pins["requiredFiles"]}
    members["napcat.mjs"] = upstream.encode()
    members["package.json"] = b'{"type":"module"}\n'
    members["config/napcat.json"] = b'{"fictional_machine_state":true}'
    members["cache/qrcode.png"] = b"fictional private state"
    members["logs/local.log"] = b"fictional private state"
    members["node_modules/extra/untouched.js"] = b"retain full official layout"
    members["static/webui/fictional.js"] = b"retain official WebUI layout"
    pins["requiredFiles"] = {name: digest(members[name]) for name in pins["requiredFiles"]}
    archive = tmp_path / "official-fictional.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in members.items():
            bundle.writestr(name, content)
    pins["upstream"]["archiveSha256"] = digest(archive.read_bytes())
    pins["upstream"]["archiveSizeBytes"] = archive.stat().st_size
    (scripts / PINS.name).write_text(json.dumps(pins), encoding="utf-8")
    return tmp_path, archive, pins, patched


def tree(path):
    return {f.relative_to(path).as_posix(): digest(f.read_bytes()) for f in path.rglob("*") if f.is_file()}


def assert_no_staging(root):
    assert not list((root / "runtime").glob(".qq-napcat-candidate-*"))


def test_complete_runtime_worker_patch_and_repeatability(project):
    root, archive, pins, patched = project
    builder = load_builder()
    untouched = root / "runtime/qq/existing.bin"
    untouched.parent.mkdir(parents=True)
    untouched.write_bytes(b"existing QCE must survive")
    target = builder.build_runtime(root, archive)
    assert target == root / "runtime/qq-napcat-candidate"
    assert (target / "napcat.mjs").read_bytes() == patched.encode()
    assert "__ECHO_DIRECT_DB_READ_STATE__" not in patched
    assert "core.dbPassphrase = a" in patched
    assert '"napcat-plugin-echo"' in patched
    assert json.loads((target / "config/plugins.json").read_text()) == {"napcat-plugin-echo": True}
    assert (target / "node_modules/extra/untouched.js").is_file()
    for item in pins["templates"]:
        assert digest((target / item["target"]).read_bytes()) == item["sha256"]
    for name in pins["requiredFiles"]:
        assert (target / name).is_file()
    for name in ("qce-server.exe", "plugins/napcat-plugin-qce", "static/qce", "cache", "logs", "config/napcat.json"):
        assert not (target / name).exists()
    first = tree(target)
    builder.build_runtime(root, archive)
    assert tree(target) == first
    assert untouched.read_bytes() == b"existing QCE must survive"
    assert_no_staging(root)


@pytest.mark.parametrize("failure", ["archive_hash", "member_hash", "anchor", "patched_hash", "template_hash", "qce", "traversal"])
def test_validation_failure_preserves_existing_and_leaves_no_partial(project, failure):
    root, archive, pins, _ = project
    builder = load_builder()
    target = builder.build_runtime(root, archive)
    before = tree(target)
    if failure == "archive_hash":
        archive.write_bytes(b"corrupt archive")
    elif failure == "member_hash":
        pins["requiredFiles"]["NapCatWinBootMain.exe"] = "0" * 64
    elif failure == "anchor":
        pins["napcatPatch"]["replacements"][0]["anchor"] = "missing anchor"
    elif failure == "patched_hash":
        pins["napcatPatch"]["patchedSha256"] = "0" * 64
    elif failure == "template_hash":
        pins["templates"][0]["sha256"] = "0" * 64
    else:
        with zipfile.ZipFile(archive, "a") as bundle:
            bundle.writestr("qce-server.exe" if failure == "qce" else "../escaped.txt", b"invalid")
        pins["upstream"]["archiveSha256"] = digest(archive.read_bytes())
        pins["upstream"]["archiveSizeBytes"] = archive.stat().st_size
    (root / "scripts" / PINS.name).write_text(json.dumps(pins), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        builder.build_runtime(root, archive)
    assert tree(target) == before
    assert not (root / "escaped.txt").exists()
    assert_no_staging(root)
    # The same failure on a clean root must not publish any candidate.
    shutil.rmtree(target)
    with pytest.raises((ValueError, OSError)):
        builder.build_runtime(root, archive)
    assert not target.exists()
    assert_no_staging(root)


def test_install_failure_rolls_back_existing_runtime(project, monkeypatch):
    root, archive, _, _ = project
    builder = load_builder()
    target = builder.build_runtime(root, archive)
    before = tree(target)
    replace = builder.os.replace

    def fail_publication(source, destination):
        if Path(source).name.startswith(".qq-napcat-candidate-stage-") and Path(destination) == target:
            raise OSError("fictional publication failure")
        return replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", fail_publication)
    with pytest.raises(OSError):
        builder.build_runtime(root, archive)
    assert tree(target) == before
    assert_no_staging(root)


def test_plugin_loads_without_bridge_or_acquisition(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for plugin load contract")
    entry = ROOT / "scripts/qq_napcat_plugin/index.mjs"
    assert entry.is_file(), "Echo plugin skeleton missing"
    shutil.copy2(entry, tmp_path / "index.mjs")
    shutil.copy2(entry.parent / "bridge.mjs", tmp_path / "bridge.mjs")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "snapshot.mjs").write_text("export function registerEchoSnapshotApi(core, options) { core.apis.EchoSnapshotApi = {acquire(){throw Error('unexpected acquisition')},recover:async()=>({ok:true})}; }", encoding="utf-8")
    check = "import {plugin_init,plugin_cleanup} from './index.mjs'; process.env.ECHO_BRIDGE_PORT='0'; process.env.ECHO_SNAPSHOT_ROOT=process.cwd(); process.env.ECHO_BRIDGE_TOKEN='" + FICTIONAL_TOKEN + "'; const core={apis:{}}; let logged=false; await plugin_init({core,logger:{info(){logged=true}}}); if (!core.apis.EchoSnapshotApi) throw Error('not registered'); if (!logged) throw Error('plugin readiness log missing'); await plugin_cleanup();"
    result = subprocess.run([node, "--input-type=module", "-e", check], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("token", [None, "fictional"])
def test_plugin_refuses_an_absent_or_malformed_bridge_credential(tmp_path, token):
    """A credential is mandatory: no token means no bridge, never an open one."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for plugin credential contract")
    entry = ROOT / "scripts/qq_napcat_plugin/index.mjs"
    assert entry.is_file(), "Echo plugin skeleton missing"
    shutil.copy2(entry, tmp_path / "index.mjs")
    shutil.copy2(entry.parent / "bridge.mjs", tmp_path / "bridge.mjs")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "snapshot.mjs").write_text("export function registerEchoSnapshotApi(core, options) { core.apis.EchoSnapshotApi = {acquire(){throw Error('unexpected acquisition')},recover:async()=>({ok:true})}; }", encoding="utf-8")
    lines = [
        "import {plugin_init} from './index.mjs';",
        "process.env.ECHO_BRIDGE_PORT='0';",
        "process.env.ECHO_SNAPSHOT_ROOT=process.cwd();",
        f"if ({json.dumps(token)} === null) {{ delete process.env.ECHO_BRIDGE_TOKEN; }} else {{ process.env.ECHO_BRIDGE_TOKEN={json.dumps(token)}; }}",
        "const core={apis:{}}; let logged=false;",
        "try { await plugin_init({core,logger:{info(){logged=true}}}); process.exit(3); }",
        "catch (error) { if (error.message !== 'echo_bridge_token_missing') throw error; }",
        "if (core.apis.EchoSnapshotApi) throw Error('snapshot api registered without a credential');",
        "if (logged) throw Error('bridge readiness was logged without a credential');",
    ]
    result = subprocess.run([node, "--input-type=module", "-e", "\n".join(lines)], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
