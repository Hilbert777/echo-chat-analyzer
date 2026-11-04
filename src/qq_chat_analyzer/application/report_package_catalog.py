"""List package-owned metadata, measure report storage, and delete owned assets."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
import json
import logging
from pathlib import Path
import re
import shutil
import stat

from ..resources import user_data_dir
from .echo_report_export import (
    ECHO_REPORT_HTML_NAME,
    EchoReportExportError,
    _require_no_reparse_points,
)
from .scope_filter import AnalysisScope, AnalysisScopeMode
from .report_staging import recover_report_staging

_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.report_package")
_PACKAGE_NAME = re.compile(r"Echo_Report_[0-9]{8}_[0-9]{6}(?:_(?:[2-9]|[1-9][0-9]+))?\Z")

# Closed vocabulary for a failed batch deletion target. Results never carry the
# internal exception, real paths, or report contents, so callers can surface a
# failure without leaking local storage details.
_DELETION_MISSING = "missing"
_DELETION_NOT_A_PACKAGE = "not_a_package"
_DELETION_UNSAFE_TARGET = "unsafe_target"
_DELETION_UNDELETABLE = "undeletable"
_DELETION_UNKNOWN = "unknown"

REPORT_PACKAGE_DELETION_REASONS: tuple[str, ...] = (
    _DELETION_MISSING,
    _DELETION_NOT_A_PACKAGE,
    _DELETION_UNSAFE_TARGET,
    _DELETION_UNDELETABLE,
    _DELETION_UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class ReportPackageSummary:
    package_name: str
    generated_at: datetime
    source: str
    conversation_name: str
    message_count: int
    analysis_scope: AnalysisScope
    conversation_kind: str = "unknown"
    size_bytes: int | None = None
    search_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportPackageIssue:
    package_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReportPackageListing:
    reports: tuple[ReportPackageSummary, ...] = ()
    issues: tuple[ReportPackageIssue, ...] = ()
    storage_usage: ReportStorageUsage | None = None


@dataclass(frozen=True, slots=True)
class ReportStorageUsage:
    """What the owned report packages currently occupy on this machine.

    ``measured_bytes`` sums only packages that could be measured safely, so
    it is a lower bound unless ``complete`` is true. ``unmeasured_packages``
    names every owned package whose size stays unknown instead of zero.
    """

    package_count: int
    measured_bytes: int
    unmeasured_packages: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True when every owned package contributed to ``measured_bytes``."""
        return not self.unmeasured_packages


@dataclass(frozen=True, slots=True)
class ReportPackageDeletionFailure:
    """One explicit target that stayed on disk, with a safe category only."""

    package_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReportPackageDeletionResult:
    """Immutable outcome of one explicit batch deletion.

    ``deleted`` keeps the order of the requested names; ``failures`` names
    every target that was not removed. Both tuples are immutable, and each
    failure carries only the package name and a reason from
    :data:`REPORT_PACKAGE_DELETION_REASONS`.
    """

    deleted: tuple[str, ...] = ()
    failures: tuple[ReportPackageDeletionFailure, ...] = ()

    @property
    def complete(self) -> bool:
        """True when every requested package was deleted."""
        return not self.failures


class ReportPackageCatalog:
    def __init__(self, reports_root: Path | None = None) -> None:
        self._reports_root = reports_root

    def _root(self) -> Path:
        root = Path(self._reports_root or (user_data_dir() / "reports")).absolute()
        _require_no_reparse_points(root)
        return root.resolve()

    @property
    def reports_root(self) -> Path:
        """Expose the validated canonical root for same-root publishing."""
        return self._root()

    def resolve_html_path(self, package_name: str) -> Path:
        """Locate an owned package's regular HTML file without reading it."""
        if not isinstance(package_name, str) or not _is_package_name(package_name):
            raise ValueError("Invalid report package name.")
        root = self._root()
        package = root / package_name
        identity = _check_package(root, package)
        html = package / ECHO_REPORT_HTML_NAME
        _require_no_reparse_points(html)
        if not stat.S_ISREG(html.lstat().st_mode):
            raise ValueError("Report HTML is not a regular file.")
        resolved = html.resolve(strict=True)
        if resolved.parent != package or _check_package(root, package) != identity:
            raise ValueError("Report package changed during resolution.")
        return resolved

    def list_reports(self) -> ReportPackageListing:
        root = self._root()
        recover_report_staging(root)
        reports, issues = [], []
        candidates = _candidates(root)
        measured_bytes, unmeasured = 0, []
        _LOGGER.info("Report catalog scan root=%s candidates=%d", root, len(candidates))
        for package in candidates:
            # Ownership, not readable metadata, determines storage accounting.
            try:
                size_bytes = _package_bytes(root, package)
                measured_bytes += size_bytes
            except Exception:
                size_bytes = None
                unmeasured.append(package.name)
                _LOGGER.warning("Report summary size unknown package=%s", package, exc_info=True)
            try:
                _check_package(root, package)
                for name in ("echo-report.html", "echo-report.json", "metadata.json", "README.txt"):
                    artifact = package / name
                    _require_no_reparse_points(artifact)
                    if not stat.S_ISREG(artifact.lstat().st_mode):
                        raise ValueError("Incomplete report package.")
                metadata = package / "metadata.json"
                _require_no_reparse_points(metadata)
                summary = _read_summary(package.name, metadata)
            except Exception:
                _LOGGER.warning("Unreadable report package: %s", package, exc_info=True)
                issues.append(ReportPackageIssue(package.name, "metadata_unreadable"))
                continue
            reports.append(replace(summary, size_bytes=size_bytes))
        # Newest generated time first; equal times use ascending package name.
        reports.sort(key=lambda report: report.package_name)
        reports.sort(key=lambda report: report.generated_at, reverse=True)
        _LOGGER.info("Report catalog scan finished root=%s reports=%d issues=%d",
                     root, len(reports), len(issues))
        usage = ReportStorageUsage(len(candidates), measured_bytes, tuple(unmeasured))
        return ReportPackageListing(tuple(reports), tuple(issues), usage)

    def delete_packages(self, package_names: Iterable[str]) -> ReportPackageDeletionResult:
        """Delete exactly the named owned packages, without rescanning the root.

        The selection is complete and explicit: names are validated and
        deduplicated before the first deletion, so an empty, malformed, or
        partly invalid selection removes nothing. Each target reuses the
        single-package ownership boundary, and one failing target does not stop
        the remaining ones. Packages published while this call runs are outside
        the selection and stay untouched. Failures carry only a safe category
        from :data:`REPORT_PACKAGE_DELETION_REASONS`.
        """
        names = _validated_selection(package_names)
        root = self._root()
        deleted: list[str] = []
        failures: list[ReportPackageDeletionFailure] = []
        _LOGGER.info("Report package batch delete started root=%s targets=%d", root, len(names))
        for name in names:
            package = root / name
            try:
                _delete_package(root, package)
            except Exception as error:
                reason = _deletion_failure_reason(error)
                failures.append(ReportPackageDeletionFailure(name, reason))
                _LOGGER.warning("Report package delete failed path=%s reason=%s",
                                package, reason, exc_info=True)
            else:
                deleted.append(name)
                _LOGGER.info("Report package delete finished path=%s", package)
        _LOGGER.info("Report package batch delete finished root=%s deleted=%d failed=%d",
                     root, len(deleted), len(failures))
        return ReportPackageDeletionResult(tuple(deleted), tuple(failures))


def _validated_selection(package_names: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate and validate an explicit selection before any deletion."""
    if isinstance(package_names, (str, bytes)) or not isinstance(package_names, Iterable):
        raise TypeError("Report package names must be an iterable of names.")
    names: list[str] = []
    seen: set[str] = set()
    for name in package_names:
        if not isinstance(name, str) or not _is_package_name(name):
            raise ValueError("Invalid report package name.")
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        raise ValueError("Report package selection is empty.")
    return tuple(names)


def _deletion_failure_reason(error: BaseException) -> str:
    """Map an internal deletion failure to a safe, closed category."""
    if isinstance(error, FileNotFoundError):
        return _DELETION_MISSING
    if isinstance(error, EchoReportExportError):
        return _DELETION_UNSAFE_TARGET
    if isinstance(error, OSError):
        return _DELETION_UNDELETABLE
    if isinstance(error, RuntimeError):
        return _DELETION_UNSAFE_TARGET
    if isinstance(error, ValueError):
        return _DELETION_NOT_A_PACKAGE
    return _DELETION_UNKNOWN


def _candidates(root: Path) -> list[Path]:
    """Enumerate directories without following links; unsafe entries stay visible."""
    if not root.exists():
        return []
    candidates = []
    for package in sorted(root.iterdir(), key=lambda child: child.name):
        if not _is_package_name(package.name):
            continue
        try:
            info = package.lstat()
        except OSError:
            # An inaccessible directory cannot safely be discounted.
            candidates.append(package)
            continue
        if stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            candidates.append(package)
    return candidates


def _package_bytes(root: Path, package: Path) -> int:
    """Sum regular-file bytes of one owned package without reading contents."""
    identity = _check_package(root, package)
    total = 0
    pending = [package]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            _require_no_reparse_points(child)
            info = child.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
            else:
                raise ValueError("Unsupported report package member.")
    if _check_package(root, package) != identity:
        raise RuntimeError("Report package changed during measurement.")
    return total


def _is_package_name(name: str) -> bool:
    if not _PACKAGE_NAME.fullmatch(name):
        return False
    try:
        datetime.strptime(name[len("Echo_Report_"):len("Echo_Report_") + 15], "%Y%m%d_%H%M%S")
    except ValueError:
        return False
    return True


def _check_package(root: Path, package: Path) -> tuple[int, int]:
    _require_no_reparse_points(package)
    if package.parent != root or not _is_package_name(package.name):
        raise ValueError("Report package is outside the catalog boundary.")
    info = package.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Report package is not a directory.")
    return info.st_dev, info.st_ino


def _check_tree(directory: Path) -> None:
    for child in directory.iterdir():
        _require_no_reparse_points(child)
        if stat.S_ISDIR(child.lstat().st_mode):
            _check_tree(child)


def _delete_package(root: Path, package: Path) -> None:
    identity = _check_package(root, package)
    _check_tree(package)
    if _check_package(root, package) != identity:
        raise RuntimeError("Report package changed during deletion.")
    shutil.rmtree(package)


def _read_summary(package_name: str, path: Path) -> ReportPackageSummary:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["schema_version"] != "echo-report-meta.v1":
        raise ValueError("Unsupported report metadata.")
    generated_at = datetime.fromisoformat(data["generated_at"])
    if generated_at.utcoffset() is None:
        raise ValueError("Report time requires a timezone.")
    if data["source"] not in {"qq", "wechat"}:
        raise ValueError("Unsupported report source.")
    if not isinstance(data["conversation_name"], str):
        raise ValueError("Invalid conversation name.")
    if type(data["message_count"]) is not int or data["message_count"] < 0:
        raise ValueError("Invalid report message count.")
    raw_scope = data["analysis_scope"]
    mode = AnalysisScopeMode(raw_scope["mode"])
    start = date.fromisoformat(raw_scope["start_date"]) if raw_scope["start_date"] is not None else None
    end = date.fromisoformat(raw_scope["end_date"]) if raw_scope["end_date"] is not None else None
    if mode is AnalysisScopeMode.ALL:
        if start is not None or end is not None:
            raise ValueError("All scope cannot have date filters.")
    elif start is None or end is None or start > end:
        raise ValueError("Invalid report scope.")
    kind = data.get("conversation_kind", "unknown")
    search_names = data.get("search_names", [])
    if not isinstance(search_names, list) or any(not isinstance(name, str) for name in search_names):
        raise ValueError("Invalid report search names.")
    return ReportPackageSummary(package_name, generated_at, data["source"],
                                data["conversation_name"], data["message_count"],
                                AnalysisScope(mode, start, end),
                                kind if isinstance(kind, str) else "unknown",
                                search_names=tuple(search_names))
