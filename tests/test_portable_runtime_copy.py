"""Portable Runtime copy tests using only fictional package directories."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import struct
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_windows_exe.ps1"
KOFFI_SOURCE = PROJECT_ROOT / "runtime" / "wechat" / "node_modules" / "koffi"

# The same contract the build script enforces, so a fictional package is always
# materialised from one source of truth instead of a second hand-kept list.
RUNTIME_CONTRACT = json.loads(
    (PROJECT_ROOT / "scripts" / "windows_runtime_manifest.json").read_text(
        encoding="utf-8"
    )
)

# NapCat ships native addons for every Node.js platform/arch pair it supports
# and selects exactly one variant at runtime via
# `process.platform + "." + process.arch` (native\ffmpeg, native\napi2native,
# native\packet, native\pty, native\dpapi). A Windows x64 distribution can only
# ever load the win32.x64 addons, so the packaged copy must not carry the
# foreign platform/arch variants. The pattern matches a single path segment
# carrying a foreign platform designator (native\pty\linux.x64,
# native\dpapi\win32-arm64) or a foreign architecture designator for an
# x64-only distribution (MoeHoo.linux.arm64.node).
FOREIGN_NATIVE_SEGMENT = re.compile(
    r"(?:^|[._-])(?:linux|darwin|freebsd|openbsd|netbsd|sunos|aix|android|arm64)"
    r"(?:[._-]|$)"
)

WINDOWS_X64_NATIVE_ASSETS = (
    "dpapi/win32-x64/@primno+dpapi.node",
    "napi2native/ffmpeg.dll",
    "napi2native/napi2native.win32.x64.node",
    "packet/MoeHoo.win32.x64.node",
    "pty/win32.x64/conpty.node",
    "pty/win32.x64/conpty_console_list.node",
    "pty/win32.x64/pty.node",
    "pty/win32.x64/winpty-agent.exe",
    "pty/win32.x64/winpty.dll",
)

FOREIGN_NATIVE_ASSETS = (
    "dpapi/win32-arm64/@primno+dpapi.node",
    "ffmpeg/ffmpegAddon.darwin.arm64.node",
    "ffmpeg/ffmpegAddon.linux.arm64.node",
    "ffmpeg/ffmpegAddon.linux.x64.node",
    "napi2native/napi2native.darwin.arm64.node",
    "napi2native/napi2native.linux.arm64.node",
    "napi2native/napi2native.linux.x64.node",
    "packet/MoeHoo.darwin.arm64.node",
    "packet/MoeHoo.linux.arm64.node",
    "packet/MoeHoo.linux.x64.node",
    "pty/linux.arm64/pty.node",
    "pty/linux.x64/pty.node",
)

EXCLUDED_NATIVE_ASSETS = FOREIGN_NATIVE_ASSETS + (
    "ffmpeg/ffmpegAddon.win32.x64.node",
)
EXCLUDED_FONT = "static/fonts/AaCute.woff"

COPYRIGHT_FILES = tuple(RUNTIME_CONTRACT["releaseCopyrightFiles"])

# Full-only: build/packaging smoke that runs build_windows_exe.ps1 and loads
# the bundled Node runtime. Excluded from the Fast Suite (see pyproject.toml).
pytestmark = pytest.mark.slow_integration


def _write(path: Path, content: str = "fictional") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fictional_native_tree(runtime: Path) -> None:
    """Mirror the upstream NapCat native addon layout for every platform."""
    native = runtime / "qq-napcat-candidate" / "native"
    for relative in WINDOWS_X64_NATIVE_ASSETS + EXCLUDED_NATIVE_ASSETS:
        _write(native / relative)


def _write_contract_requirements(runtime: Path) -> None:
    """Materialise every asset the shared runtime contract requires."""
    for requirement in RUNTIME_CONTRACT["requirements"]:
        target = runtime / requirement["path"]
        if requirement["type"] == "file":
            if not target.exists():
                _write(target)
            continue
        target.mkdir(parents=True, exist_ok=True)
        if not any(target.iterdir()):
            _write(target / "sentinel.txt", "fictional directory payload")


def _fictional_runtime(
    project_root: Path,
    *,
    koffi_index: bool = True,
    node_executable: bool = True,
) -> Path:
    runtime = project_root / "runtime"
    _write_contract_requirements(runtime)
    if not node_executable:
        (runtime / "wechat/node.exe").unlink(missing_ok=True)
    if koffi_index:
        _write(
            runtime / "wechat/node_modules/koffi/index.js",
            "module.exports = { fictional: true }\n",
        )
    else:
        (runtime / "wechat/node_modules/koffi/index.js").unlink(missing_ok=True)
    _write(
        runtime
        / "wechat/node_modules/koffi/build/koffi/win32_x64/koffi.node"
    )
    _write(
        runtime / "wechat/node_modules/koffi/nested/sentinel.txt",
        "nested dependency",
    )
    _write(
        runtime / "qq-napcat-candidate/config/plugins.json",
        '{"napcat-plugin-echo":true}\n',
    )
    _write(
        runtime / "qq-napcat-candidate/config/napcat_fictional-account.json",
        '{"account": "fictional"}\n',
    )
    # The build script also ships the WeChat WCDB diagnostic runner next to
    # the frozen app; mirror it in the fictional project so RuntimeOnly builds
    # exercise the same runner packaging path as real builds.
    _write(
        project_root / "scripts" / "run_wechat_wcdb_diagnostic.ps1",
        "# fictional diagnostic runner\n",
    )
    _fictional_native_tree(runtime)
    for relative in COPYRIGHT_FILES:
        _write(project_root / relative, "fictional copyright material: " + relative)
    # Run a temporary build-script copy with fictional, pinned artifacts.
    scripts = project_root / "scripts"
    shutil.copy2(BUILD_SCRIPT, scripts / BUILD_SCRIPT.name)
    # RuntimeOnly now validates the already-frozen SQLite input. Use the same
    # verified test interpreter and validator with fictional PE bytes/pins.
    script = scripts / BUILD_SCRIPT.name
    script.write_text(script.read_text(encoding="utf-8-sig").replace(
        '$VenvPython = Join-Path $ProjectRoot ".venv\\Scripts\\python.exe"',
        "$VenvPython = '" + sys.executable.replace("'", "''") + "'"), encoding="utf-8")
    shutil.copy2(PROJECT_ROOT / "scripts/bootstrap_sqlite_runtime.py", scripts)
    import hashlib

    sqlite = bytearray(256)
    sqlite[:2] = b"MZ"
    struct.pack_into("<I", sqlite, 60, 128)
    sqlite[128:132] = b"PE\0\0"
    struct.pack_into("<H", sqlite, 132, 0x8664)
    sqlite_path = project_root / "dist/Echo/_internal/sqlite3.dll"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.write_bytes(sqlite)
    (scripts / "sqlite_runtime_pins.json").write_text(json.dumps({
        "dllSha256": hashlib.sha256(sqlite).hexdigest(),
    }))

    def digest(relative):
        path = runtime / "qq-napcat-candidate" / relative
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "0" * 64

    def wechat_digest(relative):
        path = runtime / relative
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "0" * 64

    contract = json.loads(json.dumps(RUNTIME_CONTRACT))
    contract["wechatPinnedAssets"] = {
        name: wechat_digest(name) for name in contract["wechatPinnedAssets"]
    }
    (scripts / "windows_runtime_manifest.json").write_text(json.dumps(contract))
    pins = json.loads((PROJECT_ROOT / "scripts/qq_napcat_runtime_pins.json").read_text())
    pins["requiredFiles"] = {name: digest(name) for name in pins["requiredFiles"]}
    pins["napcatPatch"]["patchedSha256"] = digest("napcat.mjs")
    pins["pluginConfigSha256"] = digest("config/plugins.json")
    for item in pins["templates"]:
        item["sha256"] = digest(item["target"])
    (scripts / "qq_napcat_runtime_pins.json").write_text(json.dumps(pins))
    return runtime


def _copy_runtime(
    project_root: Path,
    *,
    msvc_runtime_directory: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project_root / "scripts" / BUILD_SCRIPT.name),
            "-ProjectRootOverride",
            str(project_root),
            "-RuntimeOnly",
        ]
    if msvc_runtime_directory is not None:
        command.extend(
            ["-MsvcRuntimeDirectoryOverride", str(msvc_runtime_directory)]
        )
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_runtime_only_build_copies_complete_wechat_node_module(
    tmp_path: Path,
) -> None:
    _fictional_runtime(tmp_path)

    completed = _copy_runtime(tmp_path)

    assert completed.returncode == 0, completed.stderr
    copied = tmp_path / "dist/Echo/runtime/wechat/node_modules/koffi"
    assert (copied / "index.js").is_file()
    assert (copied / "nested/sentinel.txt").read_text(encoding="utf-8") == (
        "nested dependency"
    )
    shipped_runner = tmp_path / "dist/Echo/scripts/run_wechat_wcdb_diagnostic.ps1"
    assert shipped_runner.is_file()
    assert shipped_runner.read_text(encoding="utf-8").startswith(
        "# fictional diagnostic runner"
    )
    for name in ("source_guard.mjs", "source_guard.ps1"):
        relative = Path("qq-napcat-candidate/plugins/napcat-plugin-echo/snapshot") / name
        assert (tmp_path / "dist/Echo/runtime" / relative).read_bytes() == (tmp_path / "runtime" / relative).read_bytes()


def test_runtime_only_rejects_changed_sqlite_before_copying(tmp_path: Path) -> None:
    _fictional_runtime(tmp_path)
    sqlite = tmp_path / "dist/Echo/_internal/sqlite3.dll"
    sqlite.write_bytes(b"fictional unpinned DLL")
    completed = _copy_runtime(tmp_path)
    assert completed.returncode != 0
    assert "Frozen SQLite DLL failed its release pin" in completed.stderr + completed.stdout
    assert sqlite.read_bytes() == b"fictional unpinned DLL"
    assert not (tmp_path / "dist/Echo/runtime").exists()


def test_build_ships_complete_copyright_materials(tmp_path: Path) -> None:
    _fictional_runtime(tmp_path)
    completed = _copy_runtime(tmp_path)
    assert completed.returncode == 0, completed.stderr
    for relative in COPYRIGHT_FILES:
        target = tmp_path / "dist/Echo" / relative
        assert target.is_file(), relative
        assert target.read_bytes() == (tmp_path / relative).read_bytes()


@pytest.mark.parametrize("relative", ['LICENSE'])
@pytest.mark.parametrize("damage", ["missing"])
def test_build_rejects_incomplete_copyright_materials(tmp_path: Path, relative: str, damage: str) -> None:
    _fictional_runtime(tmp_path)
    source = tmp_path / relative
    if damage == "missing":
        source.unlink()
    else:
        source.write_bytes(b"")
    completed = _copy_runtime(tmp_path)
    assert completed.returncode != 0
    assert "Required release copyright file is missing or empty" in completed.stderr + completed.stdout
    assert relative in (completed.stderr + completed.stdout).replace("\\", "/")
    assert not (tmp_path / "dist/Echo/runtime").exists()


def test_runtime_build_ships_wx_key_msvc_runtime_dependencies(
    tmp_path: Path,
) -> None:
    """wx_key.dll must not rely on VC++ being installed system-wide."""
    _fictional_runtime(tmp_path)

    completed = _copy_runtime(tmp_path)

    assert completed.returncode == 0, completed.stderr
    for runtime_name in ("wechat", "qq-napcat-candidate"):
        portable_runtime = tmp_path / "dist/Echo/runtime" / runtime_name
        for filename in (
            "msvcp140.dll",
            "vcruntime140.dll",
            "vcruntime140_1.dll",
        ):
            dependency = portable_runtime / filename
            assert dependency.is_file(), (
                f"missing app-local native dependency: {runtime_name}/{filename}"
            )
            assert dependency.stat().st_size > 0


def test_runtime_build_rejects_missing_msvc_runtime_dependency(
    tmp_path: Path,
) -> None:
    _fictional_runtime(tmp_path)
    incomplete_runtime = tmp_path / "fictional-msvc-runtime"
    incomplete_runtime.mkdir()

    completed = _copy_runtime(
        tmp_path,
        msvc_runtime_directory=incomplete_runtime,
    )

    assert completed.returncode != 0
    assert "Required native runtime dependency is missing: msvcp140.dll" in (
        completed.stderr + completed.stdout
    )


def test_runtime_build_enforces_minimum_msvc_runtime_version() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8-sig")

    assert '$MsvcRuntimeMinimumVersion = [Version]"14.43"' in script
    assert "Native runtime dependency is too old" in script


def _pe_machine(path: Path) -> int:
    with path.open("rb") as binary:
        assert binary.read(2) == b"MZ"
        binary.seek(0x3C)
        pe_offset = struct.unpack("<I", binary.read(4))[0]
        binary.seek(pe_offset)
        assert binary.read(4) == b"PE\0\0"
        return struct.unpack("<H", binary.read(2))[0]


def test_key_portable_native_binaries_are_x64() -> None:
    portable = PROJECT_ROOT / "dist" / "Echo"
    required = (
        portable / "Echo.exe",
        portable / "runtime/qq-napcat-candidate/NapCatWinBootMain.exe",
        portable / "runtime/qq-napcat-candidate/NapCatWinBootHook.dll",
        portable / "runtime/wechat/node.exe",
        portable / "runtime/wechat/wcdb_cli.exe",
        portable / "runtime/wechat/WCDB.dll",
        portable / "runtime/wechat/wx_key.dll",
        portable
        / "runtime/wechat/node_modules/koffi/build/koffi/win32_x64/koffi.node",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("built portable runtime is unavailable")

    assert {_pe_machine(path) for path in required} == {0x8664}


@pytest.mark.parametrize(
    "filename",
    ["NapCatWinBootMain.exe"],
)
def test_runtime_build_rejects_missing_qq_native_launcher(
    tmp_path: Path,
    filename: str,
) -> None:
    runtime = _fictional_runtime(tmp_path)
    (runtime / "qq-napcat-candidate" / filename).unlink()

    completed = _copy_runtime(tmp_path)

    assert completed.returncode != 0
    assert filename in completed.stderr + completed.stdout


def test_runtime_build_keeps_echo_plugin_enablement_without_account_state(
    tmp_path: Path,
) -> None:
    _fictional_runtime(tmp_path)

    completed = _copy_runtime(tmp_path)

    assert completed.returncode == 0, completed.stderr
    portable_config = tmp_path / "dist/Echo/runtime/qq-napcat-candidate/config"
    assert (portable_config / "plugins.json").read_text(encoding="utf-8") == (
        '{"napcat-plugin-echo":true}\n'
    )
    assert not (portable_config / "napcat_fictional-account.json").exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_copied_portable_koffi_can_be_loaded_by_node(tmp_path: Path) -> None:
    if not (KOFFI_SOURCE / "index.js").is_file():
        pytest.skip("Koffi Runtime package is unavailable")
    runtime = _fictional_runtime(tmp_path)
    shutil.copytree(
        KOFFI_SOURCE,
        runtime / "wechat/node_modules/koffi",
        dirs_exist_ok=True,
    )
    completed = _copy_runtime(tmp_path)
    assert completed.returncode == 0, completed.stderr

    helper_probe = subprocess.run(
        ["node", "-e", "require('koffi'); process.stdout.write('loaded')"],
        cwd=tmp_path / "dist/Echo/runtime/wechat",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert helper_probe.returncode == 0, helper_probe.stderr
    assert helper_probe.stdout == "loaded"


def test_runtime_build_excludes_only_selected_assets(tmp_path: Path) -> None:
    """Portable pruning must leave the complete source and lookalikes intact."""
    runtime = _fictional_runtime(tmp_path)
    source = runtime / "qq-napcat-candidate"
    lookalikes = (
        "native/ffmpeg/ffmpegAddon.win32.x64.node.keep",
        "native/packet/MoeHoo.linux.x64.node.keep",
        "native/other/ffmpeg.dll",
        "static/fonts/AaCute.woff2",
        "static/fonts/Other.woff",
    )
    for relative in (EXCLUDED_FONT,) + lookalikes:
        _write(source / relative)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}

    completed = _copy_runtime(tmp_path)

    assert completed.returncode == 0, completed.stderr
    portable = tmp_path / "dist/Echo/runtime/qq-napcat-candidate"
    excluded = tuple("native/" + name for name in EXCLUDED_NATIVE_ASSETS) + (EXCLUDED_FONT,)
    assert all(not (portable / name).exists() for name in excluded)
    assert all((portable / name).read_bytes() == before[Path(name)] for name in lookalikes)
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert sum(p.is_file() for p in (source / "native").rglob("*")) == 25
    assert sum(p.is_file() for p in (portable / "native").rglob("*")) == 12


def _validate_portable_runtime(project_root: Path) -> subprocess.CompletedProcess[str]:
    """Exercise the real validator without copying or rebuilding the fixture."""
    scripts = project_root / "scripts"
    script = (scripts / BUILD_SCRIPT.name).read_text(encoding="utf-8-sig")
    definitions = script.split(
        'if (-not (Test-Path -LiteralPath $RuntimeSource -PathType Container)) {', 1
    )[0]
    probe = scripts / "validate-portable.ps1"
    probe.write_text(
        definitions + '\nAssert-RuntimeContract -Root $PortableRuntime -Phase "portable"\n',
        encoding="utf-8",
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(probe), "-ProjectRootOverride", str(project_root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


@pytest.mark.parametrize("change", ["missing", "tampered"])
def test_portable_still_validates_retained_native_pin(tmp_path: Path, change: str) -> None:
    _fictional_runtime(tmp_path)
    completed = _copy_runtime(tmp_path)
    assert completed.returncode == 0, completed.stderr
    addon = tmp_path / "dist/Echo/runtime/qq-napcat-candidate/native/napi2native/ffmpeg.dll"
    if change == "missing":
        addon.unlink()
    else:
        addon.write_text("tampered fictional library")
    result = _validate_portable_runtime(tmp_path)
    assert result.returncode != 0
    assert "ffmpeg.dll" in result.stderr + result.stdout


def test_runtime_copy_ships_program_assets_without_mutable_state(tmp_path: Path) -> None:
    runtime = _fictional_runtime(tmp_path)
    private_paths = ["unlisted-program.exe", "qq-napcat-candidate/unlisted-plugin/state.json"]
    for directory in (
        "output", "logs", "cache", "temp", "tmp", "staging", "generations",
        "decrypted", "scratch", "data", "reports", "session", "sessions", "auth",
    ):
        for parent in ("", "qq-napcat-candidate/", "wechat/", "qq-napcat-candidate/plugins/napcat-plugin-echo/"):
            private_paths.append(f"{parent}{directory}/fictional.plaintext.db")
    private_paths.extend([
        "qq-napcat-candidate/plugins/napcat-plugin-echo/snapshot/generations/fictional/snapshot.db",
        "qq-napcat-candidate/plugins/napcat-plugin-echo/snapshot/credentials.json",
        "wechat/node_modules/koffi/scratch/fictional.plaintext.db",
        "qq-napcat-candidate/node_modules/fictional-package/output/report.json",
        "qq-napcat-candidate/node_modules/fictional-package/records.jsonl",
        "qq-napcat-candidate/node_modules/fictional-package/session.json",
        "qq-napcat-candidate/node_modules/fictional-package/token.txt",
        ".codex/local.txt", "docs/research/local.md", "build/rc-environment-backup/local.txt",
    ])
    for relative in private_paths:
        _write(runtime / relative, "fictional private state")
    # These are immutable frontend routes, not local authentication/session state.
    for route in ("assets",):
        _write(runtime / f"qq-napcat-candidate/static/{route}/index.html", "fictional frontend route")
    source_before = {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob("*") if p.is_file()}

    completed = _copy_runtime(tmp_path)

    assert completed.returncode == 0, completed.stderr
    portable = tmp_path / "dist/Echo/runtime"
    assert not (portable / "output").exists()
    assert not any(portable.rglob("*.plaintext.db"))
    assert not any((portable / relative).exists() for relative in private_paths)
    for requirement in RUNTIME_CONTRACT["requirements"]:
        if requirement["type"] == "file":
            target = portable / requirement["path"]
            if requirement["path"] in RUNTIME_CONTRACT["portableExcludedFiles"]:
                assert not target.exists()
            else:
                assert target.is_file()
    for filename in ("snapshot.mjs", "main_wal.mjs", "workspace.mjs"):
        assert (portable / "qq-napcat-candidate/plugins/napcat-plugin-echo/snapshot" / filename).is_file()
    for source in ("qq-napcat-candidate", "wechat"):
        for filename in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            assert (portable / source / filename).is_file()
    for route in ("assets",):
        assert (portable / f"qq-napcat-candidate/static/{route}/index.html").is_file()
    assert {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob("*") if p.is_file()} == source_before


def test_runtime_copy_rejects_junctions_inside_program_assets(tmp_path: Path) -> None:
    import _winapi

    runtime = _fictional_runtime(tmp_path)
    private = tmp_path / "fictional-private"
    _write(private / "private.json", "fictional user state")
    _winapi.CreateJunction(str(private), str(runtime / "qq-napcat-candidate/node_modules/linked-package"))

    completed = _copy_runtime(tmp_path)

    assert completed.returncode != 0
    assert "reparse point" in completed.stderr + completed.stdout
    assert not (tmp_path / "dist/Echo/runtime/qq-napcat-candidate/node_modules/linked-package/private.json").exists()
    assert (private / "private.json").read_text(encoding="utf-8") == "fictional user state"


@pytest.mark.parametrize('artifact', ['napcat.mjs', 'plugins/napcat-plugin-echo/snapshot/snapshot.mjs'])
def test_modified_echo_artifact_cannot_ship(tmp_path, artifact):
    runtime = _fictional_runtime(tmp_path)
    _write(runtime / 'qq-napcat-candidate' / artifact, 'modified fictional artifact')
    result = _copy_runtime(tmp_path)
    assert result.returncode != 0
    assert 'pin mismatch' in result.stderr + result.stdout
    assert not (tmp_path / 'dist/Echo/runtime').exists()


@pytest.mark.parametrize('asset', sorted(('wechat/WCDB.dll', 'wechat/wcdb_cli.exe', 'wechat/wx_key.dll')))
def test_modified_wechat_native_asset_cannot_ship(tmp_path, asset):
    runtime = _fictional_runtime(tmp_path)
    (runtime / asset).write_bytes(b'modified fictional native asset')

    result = _copy_runtime(tmp_path)

    assert result.returncode != 0
    assert 'WeChat artifact pin mismatch' in result.stderr + result.stdout
    assert not (tmp_path / 'dist/Echo/runtime').exists()


def test_runtime_build_leaves_no_release_tree_residue(tmp_path):
    """A clean packaging run must not invent release-tree run residue."""
    _fictional_runtime(tmp_path)

    result = _copy_runtime(tmp_path)

    assert result.returncode == 0, result.stderr
    portable = tmp_path / 'dist/Echo'
    assert not (portable / 'logs').exists()
    assert not (portable / 'scripts/wcdb-diagnostic.txt').exists()
    assert (portable / 'scripts/run_wechat_wcdb_diagnostic.ps1').is_file()


@pytest.mark.parametrize(
    'residue, marker',
    [
        ('logs/echo.log', 'logs'),
        ('scripts/wcdb-diagnostic.txt', 'scripts/wcdb-diagnostic.txt'),
    ],
)
def test_runtime_build_rejects_previous_run_residue(tmp_path, residue, marker):
    """Launching the packaged app, then repackaging, must fail loudly."""
    _fictional_runtime(tmp_path)
    leftover = tmp_path / 'dist/Echo' / residue
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text('fictional local run residue', encoding='utf-8')

    result = _copy_runtime(tmp_path)

    assert result.returncode != 0
    combined = (result.stderr + result.stdout).replace('\\', '/')
    assert marker in combined
    assert 'must never ship' in combined
