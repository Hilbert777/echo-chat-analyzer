"""Build the Echo NapCat release source using only the Python stdlib.

Run with the project .venv Python. Never starts QQ, installs dependencies or
touches runtime/qq. Validation finishes before replacing the candidate tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import urllib.parse
import urllib.request
from uuid import uuid4
import zipfile

TARGET = "qq-napcat-candidate"
OFFICIAL_URL = "https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.34/NapCat.Shell.zip"
DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(data: bytes, expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or sha(data) != expected:
        raise ValueError("SHA256 mismatch")


def apply_napcat_patch(text: str, patch: dict) -> str:
    """Version-specific key capture, Echo allowlist and bounded decrypt I/O.

    Hashes establish provenance, not correctness. Independently restrict the
    executable changes and their location; fixture tests exercise the changes.
    No upstream entry is executed by this builder.
    """
    edits = patch["replacements"]
    # G1 key capture (3 edits), G3 whitelist, and one file-decrypt entry. The G2 read-window
    # telemetry patch was removed; the snapshot runtime now witnesses its own
    # staging input before and after decryption.
    if len(edits) != 5 or not edits[4]["anchor"].startswith("function zE(t, e, n) {"):
        raise ValueError("unexpected NapCat patch scope")
    whitelist = edits[3]
    anchor_names = re.findall(r'"([a-z0-9-]+)"', whitelist["anchor"])
    replacement_names = re.findall(r'"([a-z0-9-]+)"', whitelist["replacement"])
    if replacement_names != ["napcat-plugin-echo", *anchor_names]:
        raise ValueError("plugin authorization exceeds Echo-only delta")
    for edit in edits:
        if not edit["anchor"] or text.count(edit["anchor"]) != 1:
            raise ValueError("NapCat patch anchor must occur exactly once")
        text = text.replace(edit["anchor"], edit["replacement"], 1)
    if ("__ECHO_DIRECT_DB_READ_STATE__" in text
            or "core.dbPassphrase" not in text
            or '"napcat-plugin-echo"' not in text):
        raise ValueError("NapCat patch postcondition failed")
    return text


def relative(name: str) -> Path:
    # ZIP and contract paths share a portable, Windows-safe relative grammar.
    if "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("unsafe member path")
    parts = name.rstrip("/").split("/")
    if not parts or any(p in ("", ".", "..") or p.endswith((".", " ")) for p in parts):
        raise ValueError("unsafe member path")
    for part in parts:
        if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise ValueError("reserved member path")
    return Path(*PurePosixPath(name).parts)


def remove_owned(path: Path, parent: Path) -> None:
    # Every recursive deletion is limited to this candidate's private siblings.
    if path.is_symlink() or path.resolve().parent != parent.resolve() or not path.name.startswith(f".{TARGET}-"):
        raise ValueError("unsafe cleanup target")
    if path.exists():
        shutil.rmtree(path)


def check_download_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("non-official download URL")


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_runtime(project_root: Path, archive_path: Path | None = None) -> Path:
    root = Path(project_root).resolve()
    scripts = root / "scripts"
    pins = json.loads((scripts / "qq_napcat_runtime_pins.json").read_text(encoding="utf-8"))
    contract = json.loads((scripts / "qq_napcat_runtime_manifest.json").read_text(encoding="utf-8"))
    upstream = pins["upstream"]
    if upstream["version"] != "4.18.34" or upstream["project"] != "NapNeko/NapCatQQ" or upstream["archiveUrl"] != OFFICIAL_URL:
        raise ValueError("unsupported official source")
    runtime = root / "runtime"
    if runtime.is_symlink() or runtime.resolve().parent != root:
        raise ValueError("unsafe runtime root")
    runtime.mkdir(exist_ok=True)
    target = runtime / TARGET
    if target.is_symlink() or target.resolve().parent != runtime.resolve():
        raise ValueError("unsafe candidate target")
    stage = runtime / f".{TARGET}-stage-{uuid4().hex}"
    backup = runtime / f".{TARGET}-backup-{uuid4().hex}"
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="echo-napcat-download-") as download:
            if archive_path is None:
                archive_path = Path(download) / "NapCat.Shell.zip"
                opener = urllib.request.build_opener(OfficialRedirect())
                with opener.open(OFFICIAL_URL, timeout=60) as response, archive_path.open("wb") as output:
                    check_download_url(response.url)
                    shutil.copyfileobj(response, output)
            archive_path = Path(archive_path)
            if archive_path.stat().st_size != upstream["archiveSizeBytes"]:
                raise ValueError("archive size mismatch")
            verify(archive_path.read_bytes(), upstream["archiveSha256"])
            stage.mkdir()
            with zipfile.ZipFile(archive_path) as bundle:
                seen = set()
                for member in bundle.infolist():
                    name = member.filename.rstrip("/")
                    path = relative(name)
                    if name.casefold() in seen or stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError("duplicate or linked archive member")
                    seen.add(name.casefold())
                    if any(name.casefold() == p.casefold() or name.casefold().startswith(p.casefold() + "/") for p in contract["forbiddenPaths"]):
                        raise ValueError("QCE member in official candidate")
                    destination = stage / path
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.open(member) as source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
            for name, expected in pins["requiredFiles"].items():
                verify((stage / relative(name)).read_bytes(), expected)
            patch = pins["napcatPatch"]
            napcat = stage / "napcat.mjs"
            verify(napcat.read_bytes(), patch["upstreamSha256"])
            text = napcat.read_text(encoding="utf-8")
            text = apply_napcat_patch(text, patch)
            # Worker loads this exact filename; do not install a second patched entry.
            napcat.write_bytes(text.encode("utf-8"))
            verify(napcat.read_bytes(), patch["patchedSha256"])
            for name in contract["privatePaths"]:
                path = stage / relative(name)
                if path.is_dir():
                    if path.resolve().is_relative_to(stage.resolve()) and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        raise ValueError("unsafe private state target")
                elif path.exists():
                    path.unlink()
            for item in pins["templates"]:
                source = scripts / relative(item["source"])
                data = source.read_bytes()
                verify(data, item["sha256"])
                destination = stage / relative(item["target"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            enabled = b'{"napcat-plugin-echo":true}\n'
            verify(enabled, pins["pluginConfigSha256"])
            (stage / "config").mkdir(exist_ok=True)
            (stage / "config/plugins.json").write_bytes(enabled)
            for name in contract["requiredFiles"]:
                if not (stage / relative(name)).is_file():
                    raise ValueError("candidate contract file missing")
            for name in contract["requiredDirectories"]:
                if not (stage / relative(name)).is_dir():
                    raise ValueError("candidate contract directory missing")
            for name in contract["forbiddenPaths"] + contract["privatePaths"]:
                if (stage / relative(name)).exists():
                    raise ValueError("candidate contains forbidden or private state")
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(stage, target)
                published = True
            except OSError:
                if backup.exists():
                    os.replace(backup, target)
                raise
        return target
    finally:
        remove_owned(stage, runtime)
        # On rollback failure, keep the backup rather than destroy the old runtime.
        if published or target.exists():
            remove_owned(backup, runtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path, help="reuse a local official archive; all pins still checked")
    args = parser.parse_args()
    try:
        target = build_runtime(args.project_root, args.archive)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"Candidate bootstrap failed: {type(error).__name__}")
        return 1
    print(f"Candidate runtime verified: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
