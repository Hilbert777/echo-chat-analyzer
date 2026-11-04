import dataclasses
import json
import importlib
from pathlib import Path

import pytest


def _catalog_module():
    return importlib.import_module("qq_chat_analyzer.application.report_package_catalog")


def _catalog(root):
    return _catalog_module().ReportPackageCatalog(root)


def _package(root, name="Echo_Report_20261004_120000", generated_at="2026-10-04T12:00:00+00:00"):
    package = root / name
    package.mkdir(parents=True)
    metadata = {
        "schema_version": "echo-report-meta.v1", "generated_at": generated_at,
        "source": "qq", "conversation_name": "Fictional Conversation",
        "conversation_kind": "group", "message_count": 120, "active_days": 8,
        "participant_count": 5, "message_start_timestamp": None,
        "message_end_timestamp": None,
        "analysis_scope": {"mode": "all", "start_date": None, "end_date": None},
    }
    (package / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (package / "echo-report.json").write_text("not readable report JSON", encoding="utf-8")
    (package / "echo-report.html").write_text("fictional", encoding="utf-8")
    (package / "README.txt").write_text("fictional", encoding="utf-8")
    return package


def test_lists_only_metadata_in_stable_generated_time_order(tmp_path, monkeypatch):
    _package(tmp_path)
    _package(tmp_path, "Echo_Report_20261004_120000_2")
    _package(tmp_path, "Echo_Report_20261003_120000", "2026-10-03T12:00:00+00:00")
    (tmp_path / "unknown").mkdir()
    for name in ("Echo_Report_99999999_999999", "Echo_Report_20261004_120000_1"):
        (tmp_path / name).mkdir()
    original = Path.read_text
    def guarded(path, *args, **kwargs):
        # Importing the catalog resolves the installed distribution metadata via
        # Path.read_text, so only reads inside the scanned root are constrained.
        if path.is_relative_to(tmp_path):
            assert path.name == "metadata.json"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded)
    listing = _catalog(tmp_path).list_reports()
    assert [r.package_name for r in listing.reports] == [
        "Echo_Report_20261004_120000", "Echo_Report_20261004_120000_2",
        "Echo_Report_20261003_120000"]
    assert listing.reports[0].message_count == 120
    assert listing.reports[0].conversation_name == "Fictional Conversation"
    assert not listing.issues


def test_search_names_roundtrip_from_view_metadata_without_reading_report_bodies(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from qq_chat_analyzer.application.report_package_metadata import build_report_metadata
    from qq_chat_analyzer.application.scope_filter import AnalysisScope, AnalysisScopeMode
    from test_language_sources import _view
    view = _view()
    member = dataclasses.replace(view.members[0], remark="虚构备注", contextual_name="虚构群名片", nickname="虚构昵称")
    view = dataclasses.replace(view, members=(member,))
    result = SimpleNamespace(echo_report_view=view, reports=SimpleNamespace(conversations=None))
    metadata = build_report_metadata(result=result, source="qq", scope=AnalysisScope(AnalysisScopeMode.ALL),
                                     generated_at=datetime.now(timezone.utc))
    assert {"虚构备注", "虚构群名片", "虚构昵称"} <= set(metadata["search_names"])
    package = _package(tmp_path)
    (package / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    summary, = _catalog(tmp_path).list_reports().reports
    assert set(summary.search_names) == set(metadata["search_names"])


def test_listing_and_usage_share_observation_including_broken_and_unmeasured_packages(tmp_path, monkeypatch):
    module = _catalog_module()
    good, broken, unmeasured = _packages(tmp_path, 3)
    (broken / "metadata.json").write_text("broken fictional metadata", encoding="utf-8")
    measured_sizes = {good.name: _package_bytes(good), broken.name: _package_bytes(broken)}
    original_candidates, original_bytes = module._candidates, module._package_bytes
    scans, measurements = [], []

    def candidates(root):
        scans.append(root)
        return original_candidates(root)

    def measure(root, package):
        measurements.append(package.name)
        if package == unmeasured:
            raise PermissionError("fictional unavailable size")
        return original_bytes(root, package)

    monkeypatch.setattr(module, "_candidates", candidates)
    monkeypatch.setattr(module, "_package_bytes", measure)
    listing = _catalog(tmp_path).list_reports()
    usage = listing.storage_usage
    assert len(scans) == 1
    assert sorted(measurements) == sorted([good.name, broken.name, unmeasured.name])
    assert [issue.package_name for issue in listing.issues] == [broken.name]
    sizes = {report.package_name: report.size_bytes for report in listing.reports}
    assert sizes == {good.name: measured_sizes[good.name], unmeasured.name: None}
    assert usage.package_count == 3
    assert usage.measured_bytes == sum(measured_sizes.values())
    assert usage.unmeasured_packages == (unmeasured.name,)
    assert not usage.complete


def test_summary_size_does_not_follow_reparse_assets(tmp_path, monkeypatch):
    import stat
    package = _package(tmp_path)
    asset = package / "linked.bin"
    asset.write_bytes(b"fictional")
    original_stat = Path.lstat
    class ReparseStat:
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        st_mode = stat.S_IFREG
    def reparse(path, *args, **kwargs):
        return ReparseStat() if path == asset else original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", reparse)
    listing = _catalog(tmp_path).list_reports()
    assert len(listing.reports) == 1 and not listing.issues
    assert listing.reports[0].size_bytes is None


def test_fixed_01_metadata_survives_list_reopen_and_retention_without_rewrite(tmp_path):
    # Literal historical fixture: never generate this via the current writer.
    old = _package(tmp_path)
    (old / "metadata.json").write_text('''{
      "schema_version": "echo-report-meta.v1",
      "generated_at": "2026-10-04T12:00:00+00:00",
      "source": "qq", "conversation_name": "Fictional 0.1 Conversation",
      "conversation_kind": "group", "message_count": 120,
      "active_days": 8, "participant_count": 5,
      "message_start_timestamp": null, "message_end_timestamp": null,
      "analysis_scope": {"mode": "all", "start_date": null, "end_date": null}
    }''', encoding="utf-8")
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in old.iterdir()}
    # 51 packages exceed the retired 50-package cap; none may be evicted.
    for index in range(50):
        package = _package(
            tmp_path, f"Echo_Report_20261004_120000_{index + 2}",
            "2026-10-03T12:00:00+00:00" if index == 0 else "2026-10-05T12:00:00+00:00",
        )
        path = package / "metadata.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(app_version="0.2.0", report_schema_version="echo-report.v0.7",
                    analysis_revision="echo-analysis.v2", future_optional_field=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    catalog = _catalog(tmp_path)
    listing = catalog.list_reports()
    assert len(listing.reports) == 51 and not listing.issues
    summary = next(report for report in listing.reports if report.package_name == old.name)
    assert summary.conversation_name == "Fictional 0.1 Conversation"
    assert summary.conversation_kind == "group"
    assert catalog.resolve_html_path(old.name) == old / "echo-report.html"
    assert catalog.list_reports().storage_usage.package_count == 51
    assert old.name in {report.package_name for report in catalog.list_reports().reports}
    assert len(list(tmp_path.glob("Echo_Report_*"))) == 51
    assert catalog.resolve_html_path(old.name) == old / "echo-report.html"
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in old.iterdir()} == before


@pytest.mark.parametrize("revision", ["echo-analysis.v1", "echo-analysis.v2", "echo-analysis.v5"])
def test_catalog_reopens_both_analysis_revisions_without_rewriting(tmp_path, revision):
    package = _package(tmp_path)
    metadata_path = package / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        report_schema_version="echo-report.v0.7", analysis_revision=revision,
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in package.iterdir()
    }

    catalog = _catalog(tmp_path)
    listing = catalog.list_reports()

    assert len(listing.reports) == 1
    assert not listing.issues
    assert catalog.resolve_html_path(package.name) == package / "echo-report.html"
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in package.iterdir()
    } == before


def test_named_regular_file_is_ignored_by_all_package_operations(tmp_path):
    package = _package(tmp_path)
    named_file = tmp_path / "Echo_Report_20261004_120000_2"
    named_file.write_bytes(b"fictional sentinel")
    before = named_file.stat()
    catalog = _catalog(tmp_path)
    listing = catalog.list_reports()
    assert [report.package_name for report in listing.reports] == [package.name]
    assert not listing.issues
    usage = catalog.list_reports().storage_usage
    assert usage.package_count == 1 and usage.complete
    assert usage.measured_bytes == sum(
        (package / name).stat().st_size
        for name in ("echo-report.html", "echo-report.json", "metadata.json", "README.txt")
    )
    assert catalog.delete_packages([package.name]).complete
    assert not package.exists()
    assert named_file.read_bytes() == b"fictional sentinel"
    assert named_file.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("location", ["root", "package", "inside"])
def test_delete_packages_rejects_windows_junction_without_following(tmp_path, location):
    from qq_chat_analyzer.application.echo_report_export import EchoReportExportError
    import subprocess
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows junction regression")
    root = tmp_path / "reports"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_bytes(b"fictional external")
    name = "Echo_Report_20261004_120000"
    if location == "root":
        link = root
    else:
        package = _package(root, name)
        if location == "package":
            __import__("shutil").rmtree(package)
            link = package
        else:
            link = package / "linked"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], check=True, capture_output=True)
    try:
        if location == "root":
            with pytest.raises(EchoReportExportError):
                _catalog(root).delete_packages([name])
        else:
            result = _catalog(root).delete_packages([name])
            assert result.deleted == ()
            assert _failure_pairs(result) == [(name, "unsafe_target")]
        assert sentinel.read_bytes() == b"fictional external"
        assert link.exists()
    finally:
        link.rmdir()


@pytest.mark.parametrize("suffix", ["_2"])
def test_resolve_html_path_uses_package_identity_without_reading_reports(tmp_path, monkeypatch, suffix):
    package = _package(tmp_path, "Echo_Report_20261004_120000" + suffix)
    (package / "metadata.json").write_text("broken", encoding="utf-8")
    original = Path.read_text
    def forbidden_read(path, *args, **kwargs):
        if path.is_relative_to(tmp_path):
            pytest.fail("Resolving a report must not read its contents or metadata")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", forbidden_read)
    assert _catalog(tmp_path).resolve_html_path(package.name) == package / "echo-report.html"


def _packages(root, count):
    return [_package(
        root, "Echo_Report_20261004_120000" + (f"_{index + 1}" if index else ""),
        f"2020-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
    ) for index in range(count)]


def _package_bytes(package):
    return sum(child.stat().st_size for child in package.iterdir() if child.is_file())


def test_listing_usage_marks_reparse_package_unmeasured_instead_of_zero(tmp_path, monkeypatch):
    import stat
    packages = _packages(tmp_path, 2)
    linked = packages[0]
    original = Path.lstat
    class ReparseStat:
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        st_mode = stat.S_IFDIR
    def reparse(path, *args, **kwargs):
        return ReparseStat() if path == linked else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", reparse)
    usage = _catalog(tmp_path).list_reports().storage_usage
    assert usage.package_count == 2
    assert usage.unmeasured_packages == (linked.name,)
    assert not usage.complete
    assert usage.measured_bytes == _package_bytes(packages[1])
    assert (linked / "metadata.json").exists()


def test_listing_usage_windows_junction_package_unmeasured_and_external_kept(tmp_path):
    import subprocess
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows junction regression")
    root = tmp_path / "reports"
    packages = _packages(root, 2)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("fictional external", encoding="utf-8")
    link = packages[0]
    __import__("shutil").rmtree(link)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                   check=True, capture_output=True)
    try:
        usage = _catalog(root).list_reports().storage_usage
        assert usage.package_count == 2
        assert usage.unmeasured_packages == (link.name,)
        assert not usage.complete
        assert usage.measured_bytes == _package_bytes(packages[1])
        assert sentinel.read_text(encoding="utf-8") == "fictional external"
    finally:
        link.rmdir()


@pytest.mark.parametrize("name", ["Echo_Report_99999999_999999", "Echo_Report_20261004_120000\\outside"])
def test_resolve_html_path_rejects_invalid_identity(tmp_path, name):
    with pytest.raises((ValueError, OSError, RuntimeError)):
        _catalog(tmp_path).resolve_html_path(name)


def test_resolve_html_path_rejects_absolute_identity(tmp_path):
    package = _package(tmp_path / "outside")
    with pytest.raises((ValueError, OSError, RuntimeError)):
        _catalog(tmp_path / "reports").resolve_html_path(str(package))


@pytest.mark.parametrize("damage", ["missing_package", "html_directory"])
def test_resolve_html_path_rejects_missing_or_non_file_html(tmp_path, damage):
    name = "Echo_Report_20261004_120000"
    if damage != "missing_package":
        package = _package(tmp_path, name)
        html = package / "echo-report.html"
        html.unlink()
        html.mkdir()
    with pytest.raises((ValueError, OSError, RuntimeError)):
        _catalog(tmp_path).resolve_html_path(name)


@pytest.mark.parametrize("location", ["root", "package", "html"])
def test_resolve_html_path_rejects_reparse_points(tmp_path, monkeypatch, location):
    import stat
    package = _package(tmp_path)
    target = {"root": tmp_path, "package": package, "html": package / "echo-report.html"}[location]
    original = Path.lstat
    class ReparseStat:
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        st_mode = stat.S_IFDIR if location != "html" else stat.S_IFREG
    def reparse(path, *args, **kwargs):
        return ReparseStat() if path == target else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(RuntimeError):
        _catalog(tmp_path).resolve_html_path(package.name)


def test_resolve_html_path_windows_junction_never_follows_external(tmp_path):
    import sys
    import subprocess
    if sys.platform != "win32":
        pytest.skip("Windows junction regression")
    root = tmp_path / "reports"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "echo-report.html").write_text("fictional", encoding="utf-8")
    name = "Echo_Report_20261004_120000"
    link = root / name
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                   check=True, capture_output=True)
    try:
        with pytest.raises(RuntimeError):
            _catalog(root).resolve_html_path(name)
        assert (external / "echo-report.html").exists()
    finally:
        link.rmdir()


# ------------------------------------------------- explicit batch deletion


def _failure_pairs(result):
    return [(failure.package_name, failure.reason) for failure in result.failures]


def _forbid_any_deletion(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A rejected selection must never reach the filesystem")
    monkeypatch.setattr("shutil.rmtree", forbidden)


def test_delete_packages_deletes_only_named_targets_and_keeps_the_rest(tmp_path):
    first = _package(tmp_path)
    second = _package(tmp_path, "Echo_Report_20261004_120000_2")
    kept = _package(tmp_path, "Echo_Report_20261003_120000", "2026-10-03T12:00:00+00:00")
    kept_before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in kept.iterdir()}
    manual = tmp_path / "manual"
    manual.mkdir()
    (manual / "keep.txt").write_text("fictional", encoding="utf-8")
    catalog = _catalog(tmp_path)
    result = catalog.delete_packages([second.name, first.name])
    assert result.deleted == (second.name, first.name)
    assert result.failures == () and result.complete
    assert isinstance(result.deleted, tuple) and isinstance(result.failures, tuple)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        result.deleted = ()
    assert not first.exists() and not second.exists()
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in kept.iterdir()} == kept_before
    assert (manual / "keep.txt").read_text(encoding="utf-8") == "fictional"
    assert catalog.list_reports().storage_usage.package_count == 1


@pytest.mark.parametrize("selection", [[], None, "Echo_Report_20261004_120000", [None], ["../Echo_Report_20261004_120000"], ["Echo_Report_20261004_120000\\outside"]])
def test_delete_packages_rejects_empty_or_invalid_selection_before_deleting(tmp_path, monkeypatch, selection):
    package = _package(tmp_path)
    _forbid_any_deletion(monkeypatch)
    with pytest.raises((TypeError, ValueError)):
        _catalog(tmp_path).delete_packages(selection)
    assert package.is_dir()


def test_delete_packages_rejects_whole_selection_when_one_name_is_invalid(tmp_path, monkeypatch):
    selected = _package(tmp_path)
    other = _package(tmp_path, "Echo_Report_20261004_120000_2")
    _forbid_any_deletion(monkeypatch)
    with pytest.raises(ValueError):
        _catalog(tmp_path).delete_packages([selected.name, "manual", other.name])
    assert selected.is_dir() and other.is_dir()


def test_delete_packages_never_rescans_or_recovers_unlisted_entries(tmp_path, monkeypatch):
    module = _catalog_module()
    package = _package(tmp_path)
    staging = tmp_path / (".echo-report-" + "a" * 32)
    staging.mkdir()
    (staging / "keep.txt").write_text("fictional", encoding="utf-8")
    def forbidden(*args, **kwargs):
        pytest.fail("Explicit batch deletion must not rescan or recover unlisted entries")
    monkeypatch.setattr(module, "_candidates", forbidden)
    monkeypatch.setattr(module, "recover_report_staging", forbidden)
    result = module.ReportPackageCatalog(tmp_path).delete_packages([package.name])
    assert result.deleted == (package.name,) and result.failures == ()
    assert (staging / "keep.txt").read_text(encoding="utf-8") == "fictional"


def test_delete_packages_continues_after_failure_and_reports_each_target(tmp_path, monkeypatch):
    import shutil
    module = _catalog_module()
    first = _package(tmp_path)
    blocked = _package(tmp_path, "Echo_Report_20261004_120000_2")
    last = _package(tmp_path, "Echo_Report_20261004_120000_3")
    original = shutil.rmtree
    def fail_blocked(path, *args, **kwargs):
        if Path(path) == blocked:
            raise PermissionError(f"fictional blocked path {tmp_path}")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", fail_blocked)
    result = module.ReportPackageCatalog(tmp_path).delete_packages(
        [first.name, blocked.name, last.name],
    )
    assert result.deleted == (first.name, last.name)
    assert _failure_pairs(result) == [(blocked.name, "undeletable")]
    assert not result.complete
    assert blocked.is_dir() and (blocked / "metadata.json").exists()
    assert result.failures[0].reason in module.REPORT_PACKAGE_DELETION_REASONS
    assert str(tmp_path) not in repr(result)


def test_delete_packages_reports_missing_target_and_never_creates_root(tmp_path):
    root = tmp_path / "reports"
    result = _catalog(root).delete_packages(["Echo_Report_20261004_120000"])
    assert result.deleted == ()
    assert _failure_pairs(result) == [("Echo_Report_20261004_120000", "missing")]
    assert not root.exists()


def test_delete_packages_reports_named_regular_file_as_failure(tmp_path):
    named_file = tmp_path / "Echo_Report_20261004_120000"
    named_file.write_bytes(b"fictional sentinel")
    before = named_file.stat()
    result = _catalog(tmp_path).delete_packages([named_file.name])
    assert result.deleted == ()
    assert _failure_pairs(result) == [(named_file.name, "not_a_package")]
    assert named_file.read_bytes() == b"fictional sentinel"
    assert named_file.stat().st_mtime_ns == before.st_mtime_ns


def test_delete_packages_rejects_reparse_package_without_following(tmp_path, monkeypatch):
    import stat
    module = _catalog_module()
    packages = _packages(tmp_path, 2)
    linked = packages[0]
    original = Path.lstat
    class ReparseStat:
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        st_mode = stat.S_IFDIR
    def reparse(path, *args, **kwargs):
        return ReparseStat() if path == linked else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", reparse)
    result = module.ReportPackageCatalog(tmp_path).delete_packages([linked.name, packages[1].name])
    assert result.deleted == (packages[1].name,)
    assert _failure_pairs(result) == [(linked.name, "unsafe_target")]
    assert (linked / "metadata.json").exists()


def test_delete_packages_windows_junction_is_a_failure_and_external_stays(tmp_path):
    import shutil
    import subprocess
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows junction regression")
    root = tmp_path / "reports"
    packages = _packages(root, 2)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("fictional external", encoding="utf-8")
    link = packages[0]
    shutil.rmtree(link)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                   check=True, capture_output=True)
    try:
        result = _catalog(root).delete_packages([link.name, packages[1].name])
        assert result.deleted == (packages[1].name,)
        assert _failure_pairs(result) == [(link.name, "unsafe_target")]
        assert sentinel.read_text(encoding="utf-8") == "fictional external"
        assert link.exists() and not packages[1].exists()
    finally:
        link.rmdir()


def test_delete_packages_rejects_nested_junction_and_keeps_external_target(tmp_path):
    import subprocess
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows junction regression")
    root = tmp_path / "reports"
    packages = _packages(root, 2)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("fictional external", encoding="utf-8")
    nested = packages[0] / "linked"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(nested), str(external)],
                   check=True, capture_output=True)
    try:
        result = _catalog(root).delete_packages([packages[0].name, packages[1].name])
        assert result.deleted == (packages[1].name,)
        assert _failure_pairs(result) == [(packages[0].name, "unsafe_target")]
        assert sentinel.read_text(encoding="utf-8") == "fictional external"
        assert (packages[0] / "metadata.json").exists() and nested.exists()
    finally:
        nested.rmdir()


def test_delete_packages_never_touches_packages_published_during_execution(tmp_path, monkeypatch):
    import shutil
    first = _package(tmp_path)
    published = []
    original = shutil.rmtree
    def publish_then_delete(path, *args, **kwargs):
        if not published:
            published.append(_package(
                tmp_path, "Echo_Report_20261005_120000", "2026-10-05T12:00:00+00:00",
            ))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", publish_then_delete)
    result = _catalog(tmp_path).delete_packages([first.name])
    assert result.deleted == (first.name,) and result.complete
    assert published[0].is_dir()
    assert (published[0] / "metadata.json").exists()
    assert published[0].name not in result.deleted
