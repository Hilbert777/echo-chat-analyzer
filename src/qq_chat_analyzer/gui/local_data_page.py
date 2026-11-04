"""Local data management page: Echo report packages."""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPointF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from .theme import LOCAL_DATA_QSS, ARCHIVE_CONFIRMATION_QSS, COLOR_PAPER, STATUS_STYLE_BASE
from .workers import submit


_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.local_data")


_SOURCE_DISPLAY = {
    "qq": "QQ",
    "wechat": "微信",
}
_KIND_DISPLAY = {"group": "群聊", "private": "私聊", "unknown": "未知类型"}
_SCOPE_DISPLAY = {
    "all": "全部消息",
    "last_six_months": "最近六个月",
    "last_year": "最近一年",
}
_UNKNOWN_SESSION_NAME = "未知会话"
_LOADING_STATUS = "正在读取本地数据..."


class LocalDataPage(QWidget):
    """Show and clear Echo report packages.

    This page owns no storage logic: it only reads view models through the
    facade and renders state, errors, and empty states.
    """

    def __init__(
        self,
        facade: Any,
        parent: QWidget | None = None,
        executor: Any = None,
        report_opener: Callable[[Any], bool] | None = None,
        confirm_delete_reports: Callable[[tuple[str, ...], int], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._facade = facade
        self._executor = executor or submit
        self._report_opener = report_opener
        self._confirm_delete_reports = confirm_delete_reports
        self._reports = ()
        self._refresh_generation = 0
        self._has_loaded = False
        self._load_failed = False
        self._checked_names: set[str] = set()
        self._visible_names: tuple[str, ...] = ()
        self._refresh_pending = False
        self._deleting = False
        self._delete_mode = False
        self._retain_retry_records: tuple[Any, ...] = ()
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(4000)
        self._feedback_timer.timeout.connect(lambda: self._set_status(""))
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("localDataPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(LOCAL_DATA_QSS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)

        title = QLabel("Echo 历史")
        title.setObjectName("localDataTitle")
        layout.addWidget(title)
        self._summary_count_label = QLabel("—")
        self._summary_count_label.setObjectName("localDataCount")
        self._summary_size_label = QLabel("—")
        self._summary_size_label.setObjectName("localDataSize")
        self._summary_note_label = QLabel("")
        self._summary_note_label.setObjectName("localDataSummaryNote")
        self._summary_note_label.setWordWrap(True)
        self._summary_note_label.hide()

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(STATUS_STYLE_BASE)
        self._status_label.hide()
        layout.addWidget(self._status_label)

        history_box = QWidget()
        history_box.setObjectName("localDataHistory")
        history_layout = QVBoxLayout(history_box)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(12)
        search_row = QHBoxLayout()
        self._refresh_button = QPushButton("刷新")
        self._refresh_button.setMinimumHeight(34)
        self._refresh_button.clicked.connect(self.refresh)
        self._search_input = QLineEdit()
        self._search_input.setMinimumHeight(36)
        self._search_input.setPlaceholderText("搜索会话名、来源、类型、日期或分析范围")
        self._search_input.setAccessibleName("搜索历史报告")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._apply_search)
        search_row.addWidget(self._search_input, stretch=1)
        search_row.addWidget(self._summary_count_label)
        search_row.addWidget(self._summary_size_label)
        search_row.addWidget(self._refresh_button)
        history_layout.addLayout(search_row)
        history_layout.addWidget(self._summary_note_label)
        self._history_empty_state = QWidget()
        self._history_empty_state.setObjectName("localDataEmptyState")
        empty_layout = QVBoxLayout(self._history_empty_state)
        empty_layout.setContentsMargins(12, 40, 12, 40)
        empty_layout.addStretch(1)
        self._history_empty_label = QLabel("暂无报告")
        self._history_empty_label.setObjectName("localDataEmptyTitle")
        self._history_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._history_empty_label)
        self._history_empty_detail_label = QLabel("分析完成后，报告会保存在本机，方便随时回看。")
        self._history_empty_detail_label.setObjectName("localDataEmptyDetail")
        self._history_empty_detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._history_empty_detail_label.setWordWrap(True)
        empty_layout.addWidget(self._history_empty_detail_label)
        empty_layout.addStretch(1)
        history_layout.addWidget(self._history_empty_state, stretch=1)
        self._issues_label = QLabel("")
        self._issues_label.setWordWrap(True)
        self._issues_label.hide()
        history_layout.addWidget(self._issues_label)
        self._history_list = QListWidget()
        self._history_list.setObjectName("archiveList")
        self._history_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._history_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._history_list.verticalScrollBar().setSingleStep(12)
        self._history_list.setSpacing(4)
        self._history_list.viewport().installEventFilter(self)
        self._history_list.itemSelectionChanged.connect(self._update_controls)
        self._history_list.itemDoubleClicked.connect(lambda _item: self._open_selected_report())
        history_layout.addWidget(self._history_list, stretch=1)

        selection_row = QHBoxLayout()
        self._checked_count_label = QLabel("")
        self._checked_count_label.setObjectName("archiveCheckedCount")
        selection_row.addWidget(self._checked_count_label)
        selection_row.addStretch(1)
        self._select_results_button = QPushButton("全选当前结果")
        self._select_results_button.clicked.connect(self._select_visible_reports)
        self._clear_checks_button = QPushButton("清空勾选")
        self._clear_checks_button.clicked.connect(self._clear_checks)
        selection_row.addWidget(self._select_results_button)
        selection_row.addWidget(self._clear_checks_button)
        self._open_report_button = QPushButton("打开报告")
        self._open_report_button.setMinimumHeight(34)
        self._open_report_button.clicked.connect(self._open_selected_report)
        self._enter_delete_button = QPushButton("删除报告")
        self._enter_delete_button.clicked.connect(lambda: self._set_delete_mode(True))
        self._cancel_delete_button = QPushButton("取消")
        self._cancel_delete_button.clicked.connect(lambda: self._set_delete_mode(False))
        self._retain_reports_button = QPushButton("保留所选，删除其余")
        self._retain_reports_button.clicked.connect(self._retain_selected_reports)
        self._delete_checked_button = QPushButton("删除所选")
        self._delete_checked_button.setMinimumHeight(34)
        self._delete_checked_button.clicked.connect(self._delete_checked_reports)
        selection_row.addWidget(self._open_report_button)
        selection_row.addWidget(self._enter_delete_button)
        selection_row.addWidget(self._retain_reports_button)
        selection_row.addWidget(self._cancel_delete_button)
        selection_row.addWidget(self._delete_checked_button)
        history_layout.addLayout(selection_row)
        self._update_controls()
        layout.addWidget(history_box, stretch=1)

    # ---------------------------------------------------------------- public API

    def refresh(self, *, error_message: str = "", transient_message: bool = False) -> None:
        """Load the list and storage summary together off the GUI thread."""
        if self._deleting:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._history_list.clearSelection()
        self._history_list.setCurrentRow(-1)
        self._refresh_pending = True
        self._set_status(_LOADING_STATUS)
        self._update_controls()

        def on_success(result: Any) -> None:
            if generation == self._refresh_generation:
                listing, usage, usage_error = result
                self._refresh_pending = False
                self._render_data(listing, usage, usage_error, error_message)
                if error_message and transient_message:
                    self._feedback_timer.start()

        def on_error(code: str, message: str) -> None:
            if generation == self._refresh_generation:
                self._refresh_pending = False
                self._load_failed = not self._has_loaded
                self._apply_search()
                self._show_error(code, "\n".join(filter(None, (error_message, message))))

        self._executor(
            self._load_archive,
            on_success=on_success,
            on_error=on_error,
        )

    # ---------------------------------------------------------------- internals

    def _load_archive(self) -> tuple[Any, Any, str]:
        listing = self._facade.list_report_packages()
        return listing, listing.storage_usage, ""

    def _render_data(
        self, listing: Any, usage: Any, usage_error: str, error_message: str = "",
    ) -> None:
        _LOGGER.info("Local data rendered reports=%d issues=%d deletion_error=%s",
                     len(listing.reports), len(listing.issues), bool(error_message))
        self._reports = tuple(listing.reports)
        self._checked_names.intersection_update(record.package_name for record in self._reports)
        self._has_loaded = True
        self._load_failed = False
        count = usage.package_count if usage is not None else len(self._reports)
        self._summary_count_label.setText(f"{count} 份")
        size = _format_bytes(usage.measured_bytes) if usage is not None else "—"
        self._summary_size_label.setText(size)
        note = usage_error
        if usage is not None and not usage.complete:
            note = (
                f"当前大小为已测量下界；{len(usage.unmeasured_packages)} 份报告无法测量。"
            )
        self._summary_note_label.setText(note)
        self._summary_note_label.setVisible(bool(note))
        self._apply_search()
        self._issues_label.setText(f"发现 {len(listing.issues)} 个无法读取的 Echo 报告")
        self._issues_label.setVisible(bool(listing.issues))
        self._set_status(error_message)

    def _apply_search(self) -> None:
        """Filter only loaded summaries using their user-facing text."""
        query = self._search_input.text().strip().casefold()
        records = []
        for record in self._reports:
            values = _history_values(record)
            kind = _KIND_DISPLAY.get(getattr(record, "conversation_kind", "unknown"), "未知类型")
            fields = (values[0], values[1], values[2], values[4], kind, *record.search_names)
            if not query or any(query in field.casefold() for field in fields):
                records.append(record)
        self._render_history(records)
        if self._load_failed:
            title, detail = "暂时无法读取本地报告", "请稍后点击刷新重试。"
        elif self._reports:
            title, detail = "没有匹配的报告", "换一个关键词试试，或清空搜索框。"
        else:
            title, detail = "暂无报告", "分析完成后，报告会保存在本机，方便随时回看。"
        self._history_empty_label.setText(title)
        self._history_empty_detail_label.setText(detail)

    def _render_history(self, history: Any) -> None:
        records = tuple(history)
        self._visible_names = tuple(record.package_name for record in records)
        self._history_list.clear()
        for record in records:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, record.package_name)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, "\n".join((*_history_values(record), _report_size_text(record))))
            self._history_list.addItem(item)
            entry = _report_entry(
                record, record.package_name in self._checked_names,
                lambda checked, name=record.package_name: self._check_report(name, checked),
                self._delete_mode,
            )
            self._history_list.setItemWidget(item, entry)
            entry.ensurePolished()
            item.setSizeHint(QSize(entry.sizeHint().width(), entry.height()))
        self._history_list.setCurrentRow(-1)
        empty = not records
        self._history_empty_label.setVisible(empty)
        self._history_empty_detail_label.setVisible(empty)
        self._history_empty_state.setVisible(empty)
        self._history_list.setVisible(not empty)
        self._update_controls()

    def _selected_package_name(self) -> str | None:
        selected = self._history_list.selectedItems()
        return selected[0].data(Qt.ItemDataRole.UserRole) if selected else None

    def _check_report(self, name: str, checked: bool) -> None:
        if self._deleting or self._refresh_pending or not self._delete_mode:
            return
        if checked:
            self._checked_names.add(name)
        else:
            self._checked_names.discard(name)
        self._sync_checks()

    def _set_delete_mode(self, enabled: bool) -> None:
        if self._deleting or self._refresh_pending or enabled == self._delete_mode:
            return
        self._delete_mode = enabled
        self._refresh_generation += 1
        if not enabled:
            self._checked_names.clear()
            self._retain_retry_records = ()
        self._history_list.setProperty("deleteMode", enabled)
        self._history_list.style().unpolish(self._history_list)
        self._history_list.style().polish(self._history_list)
        for row in range(self._history_list.count()):
            entry = self._history_list.itemWidget(self._history_list.item(row))
            entry.findChild(QCheckBox).setVisible(enabled)
        self._sync_checks()

    def eventFilter(self, watched: Any, event: Any) -> bool:
        if watched is self._history_list.viewport() and self._delete_mode:
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                                QEvent.Type.MouseButtonDblClick) and event.button() == Qt.MouseButton.LeftButton:
                if event.type() == QEvent.Type.MouseButtonPress and not self._deleting and not self._refresh_pending:
                    item = self._history_list.itemAt(event.position().toPoint())
                    if item is not None:
                        name = item.data(Qt.ItemDataRole.UserRole)
                        self._check_report(name, name not in self._checked_names)
                # Qt sends the second press as a double-click event: consume it
                # without another toggle or the browse-mode open action.
                return True
        return super().eventFilter(watched, event)

    def _select_visible_reports(self) -> None:
        if self._deleting or self._refresh_pending or not self._delete_mode:
            return
        self._checked_names.update(self._visible_names)
        self._sync_checks()

    def _clear_checks(self) -> None:
        if self._deleting or self._refresh_pending or not self._delete_mode:
            return
        self._checked_names.clear()
        self._sync_checks()

    def _sync_checks(self) -> None:
        for row in range(self._history_list.count()):
            item = self._history_list.item(row)
            entry = self._history_list.itemWidget(item)
            checkbox = entry.findChild(QCheckBox)
            checkbox.blockSignals(True)
            checkbox.setChecked(item.data(Qt.ItemDataRole.UserRole) in self._checked_names)
            checkbox.blockSignals(False)
            entry.setProperty("checked", checkbox.isChecked())
            entry.style().unpolish(entry)
            entry.style().polish(entry)
            entry.update()
        self._update_controls()

    def _update_controls(self) -> None:
        busy = self._deleting or self._refresh_pending
        self._refresh_button.setEnabled(not busy)
        self._search_input.setEnabled(not self._deleting)
        self._history_list.setEnabled(not busy)
        self._open_report_button.setEnabled(not busy and bool(self._selected_package_name()))
        self._open_report_button.setVisible(not self._delete_mode)
        self._enter_delete_button.setVisible(not self._delete_mode)
        self._enter_delete_button.setEnabled(not busy and bool(self._reports))
        self._cancel_delete_button.setVisible(self._delete_mode)
        self._cancel_delete_button.setEnabled(not busy)
        self._retain_reports_button.setVisible(self._delete_mode)
        self._retain_reports_button.setText("重试删除失败项" if self._retain_retry_records else "保留所选，删除其余")
        checked = self._checked_names.intersection(record.package_name for record in self._reports)
        has_targets = (
            any(record.package_name not in checked for record in self._retain_retry_records)
            if self._retain_retry_records else len(checked) < len(self._reports)
        )
        self._retain_reports_button.setEnabled(
            not busy and bool(checked) and has_targets
        )
        for control in (self._delete_checked_button, self._select_results_button,
                        self._clear_checks_button, self._checked_count_label):
            control.setVisible(self._delete_mode)
        self._delete_checked_button.setEnabled(not busy and bool(self._checked_names))
        self._select_results_button.setEnabled(not busy and bool(self._visible_names))
        self._clear_checks_button.setEnabled(not busy and bool(self._checked_names))
        hidden_count = len(self._checked_names.difference(self._visible_names))
        self._checked_count_label.setText(
            f"已选 {len(self._checked_names)} 份"
            + (f"（筛选外 {hidden_count} 份）" if hidden_count else "")
        )

    def _open_selected_report(self) -> None:
        package_name = self._selected_package_name()
        if package_name is None or self._deleting or self._refresh_pending or self._delete_mode:
            return
        generation = self._refresh_generation
        def on_success(path: Any) -> None:
            if generation == self._refresh_generation and not self._deleting and not self._delete_mode:
                self._open_resolved_report(path)
        def on_error(code: str, message: str) -> None:
            if generation == self._refresh_generation and not self._deleting and not self._delete_mode:
                self._show_error(code, message)
        self._executor(
            lambda: self._facade.get_report_package_html_path(package_name),
            on_success=on_success, on_error=on_error,
        )

    def _open_resolved_report(self, path: Any) -> None:
        try:
            opened = self._report_opener(path) if self._report_opener is not None else False
        except Exception:
            opened = False
        self._set_status(
            "" if opened else "无法打开 Echo 报告，请检查系统默认浏览器后重试。"
        )

    def _delete_checked_reports(self) -> None:
        if self._deleting or self._refresh_pending or not self._delete_mode:
            return
        records = tuple(record for record in self._reports if record.package_name in self._checked_names)
        self._delete_report_records(records)

    def _retain_selected_reports(self) -> None:
        if self._deleting or self._refresh_pending or not self._delete_mode:
            return
        checked = self._checked_names.intersection(record.package_name for record in self._reports)
        if not checked:
            return
        snapshot = self._retain_retry_records or self._reports
        records = tuple(record for record in snapshot if record.package_name not in checked)
        self._delete_report_records(records, retain_selected=True)

    def _delete_report_records(self, records: tuple[Any, ...], *, retain_selected: bool = False) -> None:
        frozen_names = tuple(record.package_name for record in records)
        if not frozen_names:
            return
        kept_count = sum(record.package_name not in frozen_names for record in self._reports)
        self._deleting = True
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._update_controls()
        try:
            if self._confirm_delete_reports is not None:
                confirmed = bool(self._confirm_delete_reports(frozen_names, kept_count))
            else:
                dialog = _bulk_delete_confirmation_dialog(records, kept_count, self, retain_selected=retain_selected)
                confirmed = dialog.exec() == QDialog.DialogCode.Accepted
        except Exception:
            self._deleting = False
            self._update_controls()
            raise
        if not confirmed:
            self._deleting = False
            self._update_controls()
            return
        self._set_status("正在删除…")
        if not retain_selected:
            self._retain_retry_records = ()

        def on_success(result: Any) -> None:
            if generation != self._refresh_generation or not self._deleting:
                return
            self._checked_names.difference_update(result.deleted)
            if retain_selected:
                failed_names = {failure.package_name for failure in result.failures}
                self._retain_retry_records = tuple(record for record in records if record.package_name in failed_names)
            if result.deleted:
                self._reports = tuple(record for record in self._reports if record.package_name not in result.deleted)
                self._summary_count_label.setText("—")
                self._summary_size_label.setText("—")
                self._summary_note_label.setText("报告已删除，存储统计待刷新。")
                self._summary_note_label.show()
                self._apply_search()
            message = f"已删除 {len(result.deleted)} 份报告。"
            if result.failures:
                retry_note = "可通过“重试删除失败项”重试。" if retain_selected else "已保留勾选，可重试。"
                message += f"{len(result.failures)} 份未能删除，{retry_note}"
            self._deleting = False
            if not result.failures:
                self._set_delete_mode(False)
            self.refresh(error_message=message, transient_message=not result.failures)

        def on_error(code: str, message: str) -> None:
            if generation != self._refresh_generation or not self._deleting:
                return
            if retain_selected:
                self._retain_retry_records = records
            self._deleting = False
            self.refresh(error_message=message)

        self._executor(
            lambda: self._facade.delete_report_packages(frozen_names),
            on_success=on_success, on_error=on_error,
        )

    def _show_error(self, code: str, message: str) -> None:
        self._set_status(message)

    def _set_status(self, message: str) -> None:
        self._feedback_timer.stop()
        self._status_label.setText(message)
        self._status_label.setVisible(bool(message))


def _plain_label(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    label.setToolTip(text)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


class _ArchiveCheckBox(QCheckBox):
    """Keep the native checkbox interaction and add a contrasting checkmark."""

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(COLOR_PAPER), 2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPolyline(QPolygonF([
            QPointF(rect.left() + rect.width() * 0.22, rect.top() + rect.height() * 0.51),
            QPointF(rect.left() + rect.width() * 0.43, rect.top() + rect.height() * 0.72),
            QPointF(rect.left() + rect.width() * 0.78, rect.top() + rect.height() * 0.28),
        ]))
        painter.end()


def _report_entry(record: Any, checked: bool, on_check: Callable[[bool], None], delete_mode: bool) -> QWidget:
    entry = QFrame()
    entry.setObjectName("archiveEntry")
    entry.setProperty("checked", checked)
    entry.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    entry.setFixedHeight(68)
    layout = QHBoxLayout(entry)
    layout.setContentsMargins(12, 8, 12, 8)
    layout.setSpacing(16)
    checkbox = _ArchiveCheckBox()
    policy = checkbox.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    checkbox.setSizePolicy(policy)
    checkbox.setVisible(delete_mode)
    checkbox.setAccessibleName(f"勾选报告：{record.conversation_name or _UNKNOWN_SESSION_NAME}")
    checkbox.setChecked(checked)
    checkbox.toggled.connect(on_check)
    layout.addWidget(checkbox)
    content = QVBoxLayout()
    content.setSpacing(4)
    generated, source, name, messages, scope = _history_values(record)
    content.addWidget(_plain_label(name, "archiveEntryTitle"))
    metadata = QHBoxLayout()
    metadata.setSpacing(16)
    metadata.addWidget(_plain_label(f"{source} · {generated} · {messages} 条消息 · {_report_size_text(record)}", "archiveEntryMeta"), stretch=1)
    metadata.addWidget(_plain_label(f"分析范围：{scope}", "archiveEntryScope"), stretch=1)
    content.addLayout(metadata)
    layout.addLayout(content, stretch=1)
    return entry


def _bulk_delete_confirmation_dialog(
    records: tuple[Any, ...], kept_count: int, parent: QWidget | None = None,
    *, retain_selected: bool = False,
) -> QDialog:
    dialog = QDialog(parent)
    dialog.setObjectName("archiveDeletionDialog")
    dialog.setWindowTitle("保留所选，删除其余" if retain_selected else "确认删除勾选报告")
    dialog.setStyleSheet(ARCHIVE_CONFIRMATION_QSS)
    dialog.resize(620, 520)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(16)
    heading = _plain_label(f"将删除 {len(records)} 份报告", "archiveDeletionTitle")
    layout.addWidget(heading)
    retained = _plain_label(f"将保留 {kept_count} 份正常报告", "archiveDeletionRetained")
    layout.addWidget(retained)
    note = _plain_label(
        "只删除下方清单中的 Echo 报告文件，不影响 QQ / 微信原始聊天数据。删除后无法恢复。"
        + ("删除范围不受搜索筛选限制。" if retain_selected else ""),
        "archiveDeletionNote",
    )
    note.setWordWrap(True)
    layout.addWidget(note)
    listing = QListWidget()
    listing.setObjectName("archiveDeletionList")
    listing.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    listing.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    listing.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    listing.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    listing.verticalScrollBar().setSingleStep(12)
    listing.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    listing.horizontalScrollBar().setSingleStep(12)
    listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    for record in records:
        generated, source, name, messages, scope = _history_values(record)
        item = QListWidgetItem(f"{name}\n{source} · {generated} · {messages} 条消息 · {_report_size_text(record)} · {scope}\n{record.package_name}")
        item.setData(Qt.ItemDataRole.UserRole, record.package_name)
        listing.addItem(item)
    layout.addWidget(listing, stretch=1)
    actions = QHBoxLayout()
    actions.addStretch(1)
    cancel = QPushButton("取消")
    cancel.setDefault(True)
    cancel.clicked.connect(dialog.reject)
    delete = QPushButton("确认删除")
    delete.setObjectName("archiveConfirmDelete")
    delete.setAutoDefault(False)
    delete.clicked.connect(dialog.accept)
    actions.addWidget(cancel)
    actions.addWidget(delete)
    layout.addLayout(actions)
    cancel.setFocus()
    return dialog


def _report_size_text(record: Any) -> str:
    size = getattr(record, "size_bytes", None)
    return "大小未知" if size is None else _format_bytes(size)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable size unit")


def _history_values(record: Any) -> tuple[str, ...]:
    return (
        _format_datetime(record.generated_at),
        _source_display(getattr(record, "source", "")),
        record.conversation_name or _UNKNOWN_SESSION_NAME,
        str(getattr(record, "message_count", 0)),
        _scope_display(
            record.analysis_scope.mode,
            start=record.analysis_scope.start_date,
            end=record.analysis_scope.end_date,
        ),
    )


def _source_display(source: Any) -> str:
    value = getattr(source, "value", source)
    return _SOURCE_DISPLAY.get(str(value), str(value))


def _scope_display(value: Any, start: Any = None, end: Any = None) -> str:
    text = getattr(value, "value", value)
    key = str(text)
    if key in _SCOPE_DISPLAY:
        return _SCOPE_DISPLAY[key]
    if key == "custom":
        start_text = _format_date(start)
        end_text = _format_date(end)
        if start_text and end_text:
            return f"{start_text} 至 {end_text}"
        return "-"
    return key or "-"


def _format_date(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    return text[:10] if text else ""


def _format_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    if value is None:
        return "-"
    return str(value)
