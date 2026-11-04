"""Shared session list, search, sort, date-range, and analysis controls panel."""

from __future__ import annotations

import logging
from threading import Event
from datetime import datetime
from typing import Any

from PySide6.QtCore import (
    QDate, QEasingCurve, QEvent, QPersistentModelIndex, QRectF, QSize,
    Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import QColor, QRadialGradient
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QBoxLayout,
    QComboBox,
    QDateEdit,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ..application.facade import AnalysisConfig, AnalysisPhase, AnalysisScopeMode, ChatSource
from .theme import (
    COLOR_ACCENT_SOFT, COLOR_PAPER, COLOR_PAPER_ALT, HOME_COLOR_ACCENT,
    SESSION_CONNECTION_STYLE, SESSION_WORKSPACE_MAX_WIDTH, SESSION_WORKSPACE_QSS,
)
from .workers import submit


SESSION_ID_ROLE = Qt.ItemDataRole.UserRole
SOURCE_ROLE = Qt.ItemDataRole.UserRole + 1
MESSAGE_COUNT_ROLE = Qt.ItemDataRole.UserRole + 2

_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.session_analysis_panel")

_SESSION_EMPTY_TITLE = "暂无会话"
_SESSION_EMPTY_DETAIL = "连接数据源后，这里会显示聊天记录"
_SESSION_CONNECTING_TITLE = "正在连接数据源..."
_SESSION_READING_TITLE = "正在读取聊天数据..."
_SESSION_NO_DATA_TITLE = "没有找到可分析的聊天记录"
_NO_MESSAGES_AVAILABLE = "该会话没有可分析消息"
_NO_MATCHING_SESSIONS = "没有匹配的会话"
_SESSION_SEARCH_PLACEHOLDER = "搜索群名、好友名或显示名称"
_SESSION_SORT_LABEL = "排序"
_SESSION_SORT_RECENT = "最近消息"
_SESSION_SORT_MESSAGE_COUNT = "消息数量"
_SESSION_SORT_NAME = "名称"
_SESSION_SORT_ORDER = (
    (_SESSION_SORT_RECENT, "recent"),
    (_SESSION_SORT_MESSAGE_COUNT, "message_count"),
    (_SESSION_SORT_NAME, "name"),
)
_ANALYZING = "正在准备分析..."
_ANALYSIS_CANCELLED = "分析已取消。"
_MAXIMIZED_WORKSPACE_MAX_WIDTH = 1520
_MAXIMIZED_CONFIGURATION_WIDTH = 404

# Piano echo tuning: one linear clock, with easing applied within each phase.
_ECHO_DURATION_MS = 400
_ECHO_PRESS_PEAK_MS = 45
_ECHO_PRESS_RELEASE_MS = 160
_ECHO_WAVE_START_MS = 70
_ECHO_WAVE_ATTACK_MS = 35
_ECHO_WAVE_ARRIVAL_MS = 300
_ECHO_FADE_START_MS = 280
_ECHO_PRESS_COLOR = "#d2b29f"
_ECHO_PRESS_OPACITY = 0.30
_ECHO_WAVE_OPACITY = 0.95
_ECHO_WAVE_HALF_WIDTH = 0.24  # Fraction of row width; 48% overall.
_ECHO_WAVE_START_X = -0.20
_ECHO_WAVE_END_X = 1.02
_ECHO_ARC_RADII = (2.0, 3.0)  # Row widths / heights: only a shallow arc is visible.
_ECHO_WAVE_STOPS = (
    (-1.0, COLOR_PAPER_ALT, 0.0),
    (-0.5, COLOR_PAPER_ALT, 0.65),
    (0.0, COLOR_PAPER, 1.0),
    (0.5, COLOR_PAPER_ALT, 0.65),
    (1.0, COLOR_PAPER_ALT, 0.0),
)


class _SessionSelectionDelegate(QStyledItemDelegate):
    """Native foreground over a light press and a single curved paper wave."""

    def __init__(self, view: QListWidget) -> None:
        super().__init__(view)
        self._view = view
        self._current = QPersistentModelIndex()
        self._progress = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setObjectName("sessionSelectionEcho")
        self._animation.setDuration(_ECHO_DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.Linear)
        self._press_easing = QEasingCurve(QEasingCurve.Type.OutSine)
        self._wave_easing = QEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.valueChanged.connect(self._advance)
        self._animation.finished.connect(self._finish)

    def _invalidate(self, index: QPersistentModelIndex) -> None:
        if index.isValid():
            rect = self._view.visualRect(index).intersected(self._view.viewport().rect())
            if not rect.isEmpty():
                self._view.viewport().update(rect)

    def _advance(self, value: float) -> None:
        self._progress = value
        self._invalidate(self._current)

    def _finish(self) -> None:
        previous = self._current
        self._current = QPersistentModelIndex()
        self._invalidate(previous)

    def reset(self) -> None:
        previous = self._current
        self._animation.stop()
        self._current = QPersistentModelIndex()
        self._progress = 0.0
        self._invalidate(previous)

    def animate_selection(self) -> None:
        self.reset()
        index = self._view.currentIndex()
        if index.isValid() and self._view.selectionModel().isSelected(index):
            self._current = QPersistentModelIndex(index)
            self._animation.start()
            self._invalidate(self._current)

    def _paint_echo(self, painter: Any, rect: Any) -> None:
        elapsed = self._progress * _ECHO_DURATION_MS
        if elapsed <= _ECHO_PRESS_PEAK_MS:
            press = self._press_easing.valueForProgress(elapsed / _ECHO_PRESS_PEAK_MS)
        else:
            release = min(1.0, (elapsed - _ECHO_PRESS_PEAK_MS)
                          / (_ECHO_PRESS_RELEASE_MS - _ECHO_PRESS_PEAK_MS))
            press = 1.0 - self._press_easing.valueForProgress(release)
        tint = QColor(_ECHO_PRESS_COLOR)
        tint.setAlphaF(_ECHO_PRESS_OPACITY * press)
        painter.fillRect(rect, tint)

        if elapsed <= _ECHO_WAVE_START_MS or elapsed >= _ECHO_DURATION_MS:
            return
        travel = min(1.0, (elapsed - _ECHO_WAVE_START_MS)
                     / (_ECHO_WAVE_ARRIVAL_MS - _ECHO_WAVE_START_MS))
        front = (_ECHO_WAVE_START_X + (_ECHO_WAVE_END_X - _ECHO_WAVE_START_X)
                 * self._wave_easing.valueForProgress(travel))
        attack = min(1.0, (elapsed - _ECHO_WAVE_START_MS) / _ECHO_WAVE_ATTACK_MS)
        fade = max(0.0, (elapsed - _ECHO_FADE_START_MS)
                   / (_ECHO_DURATION_MS - _ECHO_FADE_START_MS))
        strength = (_ECHO_WAVE_OPACITY * self._press_easing.valueForProgress(attack)
                    * (1.0 - self._wave_easing.valueForProgress(fade)))

        # A large ellipse centred outside the row gives a shallow curved crest,
        # not a circular ripple. Both sides of the broad band feather to zero.
        radius_x = rect.width() * _ECHO_ARC_RADII[0]
        radius_y = rect.height() * _ECHO_ARC_RADII[1]
        center_x = rect.left() + rect.width() * front - radius_x
        half_band = _ECHO_WAVE_HALF_WIDTH / _ECHO_ARC_RADII[0]
        outer = 1.0 + half_band
        gradient = QRadialGradient(0, 0, outer)
        for offset, color_token, opacity in _ECHO_WAVE_STOPS:
            color = QColor(color_token)
            color.setAlphaF(strength * opacity)
            gradient.setColorAt((1.0 + offset * half_band) / outer, color)
        painter.save()
        painter.translate(center_x, rect.center().y())
        painter.scale(radius_x, radius_y)
        painter.fillRect(QRectF(
            (rect.left() - center_x) / radius_x,
            (rect.top() - rect.center().y()) / radius_y,
            rect.width() / radius_x, rect.height() / radius_y,
        ), gradient)
        painter.restore()

    def paint(self, painter: Any, option: Any, index: Any) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.save()
            painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
            painter.fillRect(option.rect, QColor(COLOR_ACCENT_SOFT))
            if index == self._current:
                self._paint_echo(painter, option.rect)
            painter.restore()
        super().paint(painter, option, index)
        if selected:
            painter.fillRect(
                QRectF(option.rect.left(), option.rect.top(), 2, option.rect.height()),
                QColor(HOME_COLOR_ACCENT),
            )


class SessionConnectionBar(QFrame):
    """Shared lightweight presentation of workspace-owned connection controls."""

    def __init__(self, status: QLabel, disconnect: QPushButton) -> None:
        super().__init__()
        self.setObjectName("sessionConnectionBar")
        self.setStyleSheet(SESSION_CONNECTION_STYLE)
        self._status = status
        disconnect.setObjectName("sessionDisconnect")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        row.addWidget(status, stretch=1)
        row.addWidget(disconnect)

    def set_compact(self, compact: bool) -> None:
        self._status.setProperty("sessionReady", compact)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)


class _SessionNameLabel(QLabel):
    """Keep the selected display name on one line with its full name on hover."""

    def __init__(self) -> None:
        super().__init__()
        self._full_name = "尚未选择会话"
        self.setObjectName("selectedSessionName")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.set_name(None)

    def set_name(self, name: str | None) -> None:
        self._full_name = name or "尚未选择会话"
        self.setToolTip(name or "")
        self._elide()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(
            self._full_name, Qt.TextElideMode.ElideRight, max(1, self.width()),
        ))


class SessionAnalysisPanel(QWidget):
    """Reusable session list, search, sort, date-range, and analysis controls.

    The panel owns no QQ/WeChat connection orchestration. Workspaces supply
    sessions through :meth:`populate_sessions` and forward the analysis
    signals this widget emits.
    """

    analysis_started = Signal()
    analysis_succeeded = Signal(object)
    analysis_failed = Signal(str, str)
    analysis_phase_changed = Signal(object)
    status_changed = Signal(str)
    workspace_width_changed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._facade: Any = None
        self._executor: Any = submit
        self._analysis_running = False
        self._analysis_task: Any = None
        self._analysis_operation: object | None = None
        self._analysis_cancel: Event | None = None
        self._range_request: object | None = None
        self._sessions_ready = False
        self._selected_source: ChatSource | None = None
        self._sessions_data: list[Any] = []
        self._message_range: tuple[int, int] | None = None
        self._date_user_edited = [False, False]
        self._setting_date_defaults = False
        self._build_ui()
        self._show_unconnected_session_placeholder()

    # ---------------------------------------------------------------- UI build

    def _build_ui(self) -> None:
        self.setStyleSheet(SESSION_WORKSPACE_QSS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 24)
        layout.setSpacing(0)
        centered = QHBoxLayout()
        centered.setContentsMargins(0, 0, 0, 0)
        centered.addStretch(0)
        self._workspace_content = QWidget()
        self._workspace_content.setMaximumWidth(SESSION_WORKSPACE_MAX_WIDTH)
        centered.addWidget(self._workspace_content, stretch=1)
        centered.addStretch(0)
        layout.addLayout(centered, stretch=1)
        content_layout = QVBoxLayout(self._workspace_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(24)

        self._workspace_heading = QWidget()
        heading_layout = QVBoxLayout(self._workspace_heading)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(8)
        title = QLabel("回看聊天")
        title.setObjectName("sessionWorkspaceTitle")
        description = QLabel("选一个会话，把这段聊天整理成回顾报告。")
        description.setObjectName("sessionWorkspaceDescription")
        description.setWordWrap(True)
        heading_layout.addWidget(title)
        heading_layout.addWidget(description)
        content_layout.addWidget(self._workspace_heading)
        self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._columns.setSpacing(28)
        content_layout.addLayout(self._columns, stretch=1)

        self._session_box = QGroupBox("聊天会话（0）")
        self._session_box.setObjectName("sessionListSection")
        self._session_box.setVisible(False)
        session_layout = QVBoxLayout(self._session_box)
        session_layout.setContentsMargins(0, 8, 0, 0)
        session_layout.setSpacing(12)
        self._session_search = QLineEdit()
        self._session_search.setPlaceholderText(_SESSION_SEARCH_PLACEHOLDER)
        self._session_search.setClearButtonEnabled(True)
        self._session_search.textChanged.connect(self._reapply_session_view)
        sort_row = QHBoxLayout()
        sort_row.setSpacing(12)
        sort_row.addWidget(self._session_search, stretch=1)
        sort_row.addWidget(QLabel(_SESSION_SORT_LABEL))
        self._session_sort = QComboBox()
        for label, value in _SESSION_SORT_ORDER:
            self._session_sort.addItem(label, value)
        self._session_sort.setMinimumWidth(124)
        sort_row.addWidget(self._session_sort)
        session_layout.addLayout(sort_row)

        self._session_list = QListWidget()
        self._session_list.setObjectName("sessionList")
        self._session_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._session_list.setUniformItemSizes(True)
        self._selection_delegate = _SessionSelectionDelegate(self._session_list)
        self._session_list.setItemDelegate(self._selection_delegate)
        self._session_list.itemSelectionChanged.connect(self._selection_delegate.animate_selection)
        self._session_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._session_list.itemSelectionChanged.connect(
            self._on_session_selection_changed
        )
        self._session_sort.currentIndexChanged.connect(
            self._reapply_session_view
        )
        self._session_views = QStackedWidget()
        self._session_views.addWidget(self._session_list)
        self._session_empty_state = QWidget()
        empty_layout = QVBoxLayout(self._session_empty_state)
        empty_layout.setContentsMargins(16, 24, 16, 24)
        empty_layout.setSpacing(8)
        self._session_empty_title = QLabel()
        self._session_empty_title.setObjectName("sessionEmptyTitle")
        self._session_empty_title.setTextFormat(Qt.TextFormat.PlainText)
        self._session_empty_title.setWordWrap(True)
        self._session_empty_detail = QLabel()
        self._session_empty_detail.setObjectName("sessionEmptyDetail")
        self._session_empty_detail.setTextFormat(Qt.TextFormat.PlainText)
        self._session_empty_detail.setWordWrap(True)
        empty_layout.addWidget(self._session_empty_title)
        empty_layout.addWidget(self._session_empty_detail)
        empty_layout.addStretch(1)
        self._session_views.addWidget(self._session_empty_state)
        session_layout.addWidget(self._session_views)
        self._columns.addWidget(self._session_box, stretch=63)

        self._configuration_panel = QFrame()
        self._configuration_panel.setObjectName("sessionConfiguration")
        self._columns.addWidget(self._configuration_panel, stretch=37)
        configuration_layout = QVBoxLayout(self._configuration_panel)
        configuration_layout.setContentsMargins(24, 0, 0, 0)
        configuration_layout.setSpacing(20)
        self._configuration_scroll = QScrollArea()
        self._configuration_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._configuration_scroll.setWidgetResizable(True)
        self._configuration_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        configuration_layout.addWidget(self._configuration_scroll, stretch=1)
        configuration_content = QWidget()
        self._configuration_scroll.setWidget(configuration_content)
        settings_layout = QVBoxLayout(configuration_content)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(20)
        selected_layout = QVBoxLayout()
        selected_layout.setSpacing(8)
        selected_caption = QLabel("当前会话")
        selected_caption.setObjectName("selectedSessionCaption")
        self._selected_session_caption = selected_caption
        selected_layout.addWidget(selected_caption)
        self._selected_session_label = _SessionNameLabel()
        selected_layout.addWidget(self._selected_session_label)
        self._selected_session_meta = QLabel()
        self._selected_session_meta.setObjectName("selectedSessionMeta")
        self._selected_session_meta.setTextFormat(Qt.TextFormat.PlainText)
        self._selected_session_meta.setWordWrap(True)
        selected_layout.addWidget(self._selected_session_meta)
        self._session_selection_hint = QLabel("选择一段聊天，看看时间留下了什么。")
        self._session_selection_hint.setObjectName("sessionSelectionHint")
        self._session_selection_hint.setWordWrap(True)
        selected_layout.addWidget(self._session_selection_hint)
        settings_layout.addLayout(selected_layout)

        self._analysis_range_box = QGroupBox("分析范围")
        self._analysis_range_box.setObjectName("analysisRangeSection")
        self._analysis_range_box.setVisible(False)
        range_layout = QVBoxLayout(self._analysis_range_box)
        range_layout.setContentsMargins(0, 8, 0, 0)
        range_layout.setSpacing(10)
        range_options = QGridLayout()
        range_options.setSpacing(8)
        self._scope_group = QButtonGroup(self)
        self._scope_all = QPushButton("全部记录")
        self._scope_last_year = QPushButton("最近一年")
        self._scope_last_six_months = QPushButton("最近半年")
        self._scope_custom = QPushButton("自定义")
        for index, button in enumerate((
            self._scope_all,
            self._scope_last_year,
            self._scope_last_six_months,
            self._scope_custom,
        )):
            button.setObjectName("analysisScopeOption")
            button.setCheckable(True)
            button.setMinimumHeight(42)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._scope_group.addButton(button)
            range_options.addWidget(button, index // 2, index % 2)
        range_options.setColumnStretch(0, 1)
        range_options.setColumnStretch(1, 1)
        self._scope_all.setChecked(True)
        range_layout.addLayout(range_options)

        self._custom_range_widget = QWidget(self._analysis_range_box)
        custom_range_layout = QGridLayout(self._custom_range_widget)
        custom_range_layout.setContentsMargins(0, 0, 0, 0)
        custom_range_layout.setSpacing(12)
        custom_range_layout.setVerticalSpacing(6)
        self._start_date = QDateEdit()
        self._start_date.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
        self._start_date.setDate(QDate.currentDate())
        self._start_date.setCalendarPopup(False)
        self._start_date.setKeyboardTracking(False)
        self._start_date.setDisplayFormat("yyyy-MM-dd")
        self._end_date = QDateEdit()
        self._end_date.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
        self._end_date.setDate(QDate.currentDate())
        self._end_date.setCalendarPopup(False)
        self._end_date.setKeyboardTracking(False)
        self._end_date.setDisplayFormat("yyyy-MM-dd")
        custom_range_layout.addWidget(QLabel("开始日期"), 0, 0)
        custom_range_layout.addWidget(self._start_date, 1, 0)
        custom_range_layout.addWidget(QLabel("结束日期"), 2, 0)
        custom_range_layout.addWidget(self._end_date, 3, 0)
        self._date_adjust_tools = QWidget()
        self._date_adjust_tools.setObjectName("dateAdjustmentTools")
        tools_layout = QVBoxLayout(self._date_adjust_tools)
        tools_layout.setContentsMargins(0, 4, 0, 0)
        tools_layout.setSpacing(6)
        tools_layout.addWidget(QLabel("日期微调"))
        selector_row = QHBoxLayout()
        selector_row.setSpacing(6)
        self._date_adjust_target = QComboBox()
        self._date_adjust_target.setObjectName("dateAdjustTarget")
        self._date_adjust_target.setAccessibleName("微调目标")
        self._date_adjust_target.addItems(["开始日期", "结束日期"])
        selector_row.addWidget(self._date_adjust_target, stretch=2)
        self._date_adjust_unit = QComboBox()
        self._date_adjust_unit.setObjectName("dateAdjustUnit")
        self._date_adjust_unit.setAccessibleName("微调单位")
        for label, unit in (("年", "years"), ("月", "months"), ("日", "days")):
            self._date_adjust_unit.addItem(label, unit)
        self._date_adjust_unit.setCurrentIndex(2)
        selector_row.addWidget(self._date_adjust_unit, stretch=1)
        tools_layout.addLayout(selector_row)
        step_row = QHBoxLayout()
        step_row.setSpacing(6)
        for step in (-5, -1, 1, 5):
            button = QPushButton(f"{step:+d}")
            button.setObjectName("dateAdjustStep")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, step=step: self._adjust_date(step))
            step_row.addWidget(button, stretch=1)
        tools_layout.addLayout(step_row)
        custom_range_layout.addWidget(self._date_adjust_tools, 4, 0)
        self._date_range_error = QLabel("开始日期不能晚于结束日期，请调整日期。")
        self._date_range_error.setObjectName("dateRangeError")
        self._date_range_error.setWordWrap(True)
        self._date_range_error.hide()
        custom_range_layout.addWidget(self._date_range_error, 5, 0)
        self._date_adjust_target.currentIndexChanged.connect(self._update_date_adjust_target)
        self._update_date_adjust_target()
        for index, edit in enumerate((self._start_date, self._end_date)):
            edit.dateChanged.connect(lambda _date, index=index: self._on_date_changed(index))
            # textEdited fires before dateChanged with keyboard tracking off.
            # Protect even an in-progress edit from an asynchronous default.
            edit.lineEdit().textEdited.connect(
                lambda _text, index=index: self._mark_date_edited(index)
            )
        self._custom_range_widget.setVisible(False)
        self._scope_custom.toggled.connect(self._on_custom_scope_toggled)
        range_layout.addWidget(self._custom_range_widget)
        settings_layout.addWidget(self._analysis_range_box)
        settings_layout.addStretch(1)

        self._analyze_button = QPushButton("生成回顾报告")
        self._analyze_button.setObjectName("sessionAnalyze")
        self._analyze_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._analyze_button.setEnabled(False)
        self._analyze_button.setVisible(False)
        self._analyze_button.clicked.connect(self.start_analysis)
        self._analyze_button.setMinimumSize(160, 44)
        configuration_layout.addWidget(self._analyze_button)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._update_workspace_layout()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        # The panel is embedded in a workspace: window-state events are sent
        # to its top-level window, including changes that cause no resize.
        self.window().installEventFilter(self)
        self._update_workspace_layout()

    def eventFilter(self, watched: Any, event: Any) -> bool:
        if watched is self.window() and event.type() == QEvent.Type.WindowStateChange:
            self._update_workspace_layout()
        return super().eventFilter(watched, event)

    def workspace_width_limit(self) -> int:
        """Shared content limit for the panel and the ready workspace header."""
        return (
            _MAXIMIZED_WORKSPACE_MAX_WIDTH
            if self.window().isMaximized()
            else SESSION_WORKSPACE_MAX_WIDTH
        )

    def _update_workspace_layout(self) -> None:
        width_limit = self.workspace_width_limit()
        width_changed = self._workspace_content.maximumWidth() != width_limit
        self._workspace_content.setMaximumWidth(width_limit)
        stacked = self.width() - 48 < 560
        self._configuration_panel.setMaximumWidth(
            _MAXIMIZED_CONFIGURATION_WIDTH
            if self.window().isMaximized() and not stacked
            else 16777215  # Qt's unrestricted widget width.
        )
        self._columns.setDirection(
            QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        )
        self._columns.setSpacing(16 if stacked else 28)
        self._columns.setStretch(0, 40 if stacked else 63)
        self._columns.setStretch(1, 60 if stacked else 37)
        self._configuration_panel.layout().setContentsMargins(0 if stacked else 24, 0, 0, 0)
        # Reserve the date editors' actual minimum width, including a possible
        # vertical scrollbar and the configuration divider, rather than clipping
        # them at fractional column widths.
        self._configuration_panel.setMinimumWidth(
            0 if stacked else max(
                self._configuration_scroll.widget().minimumSizeHint().width(),
                self._start_date.minimumSizeHint().width(),
                self._end_date.minimumSizeHint().width(),
            )
            + 24 + 1 + self._configuration_scroll.verticalScrollBar().sizeHint().width()
        )
        self._configuration_panel.setProperty("stacked", stacked)
        self._configuration_panel.style().unpolish(self._configuration_panel)
        self._configuration_panel.style().polish(self._configuration_panel)
        if width_changed:
            self.workspace_width_changed.emit()

    # ---------------------------------------------------------------- public API

    def configure(
        self,
        facade: Any,
        source: ChatSource,
        executor: Any = None,
    ) -> None:
        """Set the facade, source, and optional executor for this panel."""
        self._facade = facade
        self._selected_source = ChatSource(source)
        self._reset_time_range()
        if executor is not None:
            self._executor = executor

    def populate_sessions(self, sessions: Any) -> None:
        """Load sessions into the list and make analysis controls ready."""
        self._sessions_data = list(sessions or ())
        count_index = self._session_sort.findData("message_count")
        has_counts = any(session.message_count is not None for session in self._sessions_data)
        hint = "" if has_counts else "当前会话未提供消息数量，消息数量排序不可用。"
        count_item = self._session_sort.model().item(count_index)
        count_item.setEnabled(has_counts)
        count_item.setToolTip(hint)
        self._session_sort.setToolTip(hint)
        if not has_counts and self._session_sort.currentData() == "message_count":
            self._session_sort.setCurrentIndex(self._session_sort.findData("recent"))
        self._set_session_controls_ready(True)
        self._reapply_session_view()

    def clear(self) -> None:
        """Clear all session data and restore the unconnected placeholder."""
        self._sessions_data = []
        self._sessions_ready = False
        self._reset_time_range()
        self._show_unconnected_session_placeholder()

    def selected_session_id(self) -> str | None:
        """Return the selected session ID or None."""
        item = self._session_list.currentItem()
        if item is None or not item.isSelected():
            return None
        return item.data(SESSION_ID_ROLE)

    def build_config(self) -> AnalysisConfig:
        """Translate the widgets into a facade config."""
        if self._scope_last_year.isChecked():
            scope_mode = AnalysisScopeMode.LAST_YEAR
        elif self._scope_last_six_months.isChecked():
            scope_mode = AnalysisScopeMode.LAST_SIX_MONTHS
        elif self._scope_custom.isChecked():
            scope_mode = AnalysisScopeMode.CUSTOM
        else:
            scope_mode = AnalysisScopeMode.ALL
        if scope_mode is AnalysisScopeMode.CUSTOM:
            self._start_date.interpretText()
            self._end_date.interpretText()
        return AnalysisConfig(
            scope_mode=scope_mode,
            start_time=(
                self._start_date.date().toString("yyyy-MM-dd")
                if scope_mode is AnalysisScopeMode.CUSTOM
                else None
            ),
            end_time=(
                self._end_date.date().toString("yyyy-MM-dd")
                if scope_mode is AnalysisScopeMode.CUSTOM
                else None
            ),
        )

    def start_analysis(self) -> None:
        """Start analysis for the selected session through the facade."""
        if self._analysis_running:
            return
        source = self._selected_source
        if source is None:
            return
        session_id = self.selected_session_id()
        if not session_id:
            return
        item = self._session_list.currentItem()
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        config = self.build_config()
        if not self._update_date_range_validation():
            return
        cancel_event = Event()
        self._analysis_cancel = cancel_event
        # Cancellation reaches every source: WeChat media recovery is network
        # bound and must stop fetching once the user cancels.
        operation = lambda report: self._facade.analyze_session(
            source,
            session_id,
            config,
            progress=report,
            on_phase=report,
            cancelled=cancel_event.is_set,
        )
        identity = object()
        self._analysis_operation = identity
        self._set_busy(True)
        self.analysis_started.emit()
        self.status_changed.emit(_ANALYZING)
        QTimer.singleShot(0, lambda: self._submit_analysis(operation, identity))

    def cancel_analysis(self) -> None:
        """Cancel the active analysis and restore selection controls."""
        if not self._analysis_running:
            return
        if self._analysis_cancel is not None:
            self._analysis_cancel.set()
        # Invalidate before cancellation: already queued Qt signals and the
        # deferred submission can still arrive after this operation ends.
        self._analysis_operation = None
        cancel = getattr(self._analysis_task, "cancel", None)
        if callable(cancel):
            cancel()
        self._analysis_task = None
        self._analysis_cancel = None
        self._set_busy(False)
        self.status_changed.emit(_ANALYSIS_CANCELLED)

    def update_analyze_enabled(self) -> None:
        """Refresh the analyze button state (used by workspaces)."""
        self._update_analyze_enabled()

    def show_connecting_placeholder(self) -> None:
        """Show the connecting state inside the session list region."""
        self._show_session_placeholder(_SESSION_CONNECTING_TITLE)

    def show_reading_placeholder(self) -> None:
        """Show the reading state inside the session list region."""
        self._show_session_placeholder(_SESSION_READING_TITLE)

    def show_disconnected_placeholder(self) -> None:
        """Show the disconnected/empty state inside the session list region."""
        self._show_unconnected_session_placeholder()

    def show_unconnected_placeholder(self) -> None:
        """Show the disconnected/empty state inside the session list region."""
        self._show_unconnected_session_placeholder()

    # ---------------------------------------------------------------- internal

    def _submit_analysis(self, operation: Any, identity: object) -> None:
        if self._analysis_operation is not identity:
            return
        task = self._executor(
            lambda report: operation(report) if self._analysis_operation is identity else None,
            on_success=lambda outcome: self._handle_success(identity, outcome),
            on_error=lambda code, message: self._handle_error(identity, code, message),
            on_finished=lambda: self._finish_analysis(identity),
            on_progress=lambda message: self._handle_analysis_progress(identity, message),
        )
        # An inline executor may finish before returning its task handle.
        if self._analysis_operation is identity:
            self._analysis_task = task

    def _handle_analysis_progress(self, identity: object, message: object) -> None:
        if self._analysis_operation is not identity:
            return
        if isinstance(message, AnalysisPhase):
            self.analysis_phase_changed.emit(message)
        elif isinstance(message, str) and message:
            self.status_changed.emit(message)

    def _finish_analysis(self, identity: object) -> None:
        if self._analysis_operation is not identity:
            return
        self._analysis_operation = None
        self._analysis_task = None
        self._analysis_cancel = None
        self._set_busy(False)

    def _handle_success(self, identity: object, outcome: Any) -> None:
        if self._analysis_operation is not identity:
            return
        self._finish_analysis(identity)
        self.analysis_succeeded.emit(outcome)

    def _handle_error(self, identity: object, code: str, message: str) -> None:
        if self._analysis_operation is not identity:
            return
        self._finish_analysis(identity)
        self.analysis_failed.emit(code, message)

    def _set_busy(self, busy: bool) -> None:
        self._analysis_running = busy
        self.setEnabled(not busy)
        self._analyze_button.setEnabled(not busy)
        if not busy:
            self._update_analyze_enabled()

    def _update_analyze_enabled(self) -> None:
        self._update_analysis_controls_visibility()
        valid_range = self._update_date_range_validation()
        item = self._session_list.currentItem()
        has_selection = bool(item is not None and item.isSelected()
                             and item.flags() & Qt.ItemFlag.ItemIsEnabled)
        if not has_selection:
            item = None
        self._selected_session_caption.setVisible(has_selection)
        self._session_selection_hint.setVisible(not has_selection)
        if self._selected_session_label.property("hasSelection") != has_selection:
            self._selected_session_label.setProperty("hasSelection", has_selection)
            self._selected_session_label.style().unpolish(self._selected_session_label)
            self._selected_session_label.style().polish(self._selected_session_label)
        self._selected_session_label.set_name(
            item.text() if item is not None and item.flags() & Qt.ItemFlag.ItemIsEnabled else None
        )
        details = []
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            source_name = {ChatSource.QQ: "QQ", ChatSource.WECHAT: "微信"}.get(item.data(SOURCE_ROLE))
            if source_name:
                details.append(source_name)
            count = item.data(MESSAGE_COUNT_ROLE)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                details.append(f"{count:,} 条消息")
        self._selected_session_meta.setText(" · ".join(details))
        self._selected_session_meta.setVisible(bool(details))
        source = self._selected_source
        if source is None:
            self._analyze_button.setEnabled(False)
            return
        self._analyze_button.setEnabled(
            item is not None
            and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
            and valid_range
            and not self._analysis_running
        )

    def _update_analysis_controls_visibility(self) -> None:
        visible = bool(self._sessions_ready)
        self._workspace_heading.setVisible(visible)
        self._configuration_panel.setVisible(visible)
        self._analysis_range_box.setVisible(visible)
        self._analyze_button.setVisible(visible)

    def _on_session_selection_changed(self) -> None:
        self._update_analyze_enabled()
        self._reset_time_range()
        if self._selected_source is ChatSource.WECHAT:
            session_id = self.selected_session_id()
            if session_id:
                self._request_session_time_range(session_id)

    def _reset_time_range(self) -> None:
        self._range_request = None
        self._message_range = None
        self._setting_date_defaults = True
        try:
            self._start_date.setDate(QDate.currentDate())
            self._end_date.setDate(QDate.currentDate())
        finally:
            self._setting_date_defaults = False
        self._date_user_edited = [False, False]
        self._update_analyze_enabled()

    def _request_session_time_range(self, session_id: str) -> None:
        facade_method = getattr(
            self._facade,
            "get_session_message_range",
            None,
        )
        if facade_method is None:
            return
        source = self._selected_source
        if source not in (ChatSource.QQ, ChatSource.WECHAT):
            return
        request = object()
        self._range_request = request

        def apply_range(message_range: Any) -> None:
            if (
                self._range_request is not request
                or self._selected_source is not source
                or self.selected_session_id() != session_id
            ):
                return
            self._range_request = None
            self._set_message_range(message_range)

        self._executor(
            lambda: facade_method(source, session_id),
            on_success=apply_range,
            on_error=lambda *_: None,
        )

    def _set_message_range(self, message_range: Any) -> None:
        if not isinstance(message_range, (tuple, list)) or len(message_range) != 2:
            return
        self._message_range = (
            int(message_range[0]),
            int(message_range[1]),
        )
        self._apply_time_range_defaults()

    def _apply_time_range_defaults(self) -> None:
        if self._scope_custom.isChecked():
            self._apply_date_default(self._start_date, 0)
            self._apply_date_default(self._end_date, 1)

    def _apply_date_default(
        self,
        edit: QDateEdit,
        index: int,
    ) -> None:
        if self._date_user_edited[index]:
            return
        timestamp = (
            self._message_range[index]
            if self._message_range is not None
            else None
        )
        if timestamp is not None:
            date_text = datetime.fromtimestamp(timestamp).strftime(
                "%Y-%m-%d"
            )
            value = QDate.fromString(date_text, "yyyy-MM-dd")
        else:
            value = QDate.currentDate()
        self._setting_date_defaults = True
        try:
            edit.setDate(value)
        finally:
            self._setting_date_defaults = False

    def _on_custom_scope_toggled(self, checked: bool) -> None:
        self._custom_range_widget.setVisible(checked)
        if checked:
            self._apply_date_default(self._start_date, 0)
            self._apply_date_default(self._end_date, 1)
        self._update_analyze_enabled()

    def _mark_date_edited(self, index: int) -> None:
        if not self._setting_date_defaults:
            self._date_user_edited[index] = True

    def _on_date_changed(self, index: int) -> None:
        self._mark_date_edited(index)
        self._update_analyze_enabled()

    def _update_date_adjust_target(self) -> None:
        for index, edit in enumerate((self._start_date, self._end_date)):
            edit.setProperty("adjustmentTarget", index == self._date_adjust_target.currentIndex())
            edit.style().unpolish(edit)
            edit.style().polish(edit)

    def _adjust_date(self, step: int) -> None:
        if not self._scope_custom.isChecked():
            return
        index = self._date_adjust_target.currentIndex()
        edit = (self._start_date, self._end_date)[index]
        edit.interpretText()
        self._mark_date_edited(index)
        value = edit.date()
        unit = self._date_adjust_unit.currentData()
        if unit == "years":
            adjusted = value.addYears(step)
        elif unit == "months":
            adjusted = value.addMonths(step)
        else:
            adjusted = value.addDays(step)
        minimum, maximum = edit.minimumDate(), edit.maximumDate()
        if not adjusted.isValid():
            adjusted = minimum if step < 0 else maximum
        edit.setDate(max(minimum, min(maximum, adjusted)))
        self._update_analyze_enabled()

    def _update_date_range_validation(self) -> bool:
        invalid = (
            self._scope_custom.isChecked()
            and self._start_date.date() > self._end_date.date()
        )
        self._date_range_error.setVisible(invalid)
        return not invalid

    def _reapply_session_view(self) -> None:
        """Filter and sort the cached sessions for the current controls."""
        if not self._sessions_ready:
            return
        query = self._session_search.text().strip().casefold()
        sort_mode = self._session_sort.currentData() or _SESSION_SORT_ORDER[0][1]
        indexed = list(enumerate(self._sessions_data))
        filtered = [
            (index, session)
            for index, session in indexed
            if any(query in name.casefold() for name in (session.display_name, *session.search_names))
        ]
        if sort_mode == _SESSION_SORT_ORDER[2][1]:
            filtered.sort(
                key=lambda pair: (pair[1].display_name.casefold(), pair[0])
            )
        elif sort_mode == _SESSION_SORT_ORDER[1][1]:
            filtered.sort(
                key=lambda pair: (
                    pair[1].message_count is None,
                    -(pair[1].message_count or 0),
                    pair[0],
                )
            )
        else:
            filtered.sort(key=self._recent_session_key)

        self._selection_delegate.reset()
        self._session_list.clear()

        for _, session in filtered:
            self._add_session_item(session)

        count = self._session_list.count()
        self._set_session_count(count)
        if count == 0 and self._sessions_data:
            self._show_session_placeholder(
                _NO_MATCHING_SESSIONS,
                session_controls_ready=True,
                session_container_visible=True,
            )
        elif count == 0:
            self._show_session_placeholder(
                _SESSION_NO_DATA_TITLE,
                session_controls_ready=True,
            )
        else:
            self._session_box.setVisible(True)
            self._session_views.setCurrentWidget(self._session_list)
        self._update_analyze_enabled()

    def _add_session_item(self, session: Any) -> None:
        item = QListWidgetItem(session.display_name)
        item.setSizeHint(QSize(0, 54))
        item.setData(SESSION_ID_ROLE, session.session_id)
        item.setData(SOURCE_ROLE, session.source)
        item.setData(MESSAGE_COUNT_ROLE, session.message_count)
        tooltip = [session.display_name]
        if not bool(getattr(session, "message_available", True)):
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            tooltip.append(
                getattr(session, "unavailable_reason", None)
                or _NO_MESSAGES_AVAILABLE
            )
        elif session.message_count is not None:
            tooltip.append(f"消息数：{session.message_count}")
        item.setToolTip("\n".join(tooltip))
        self._session_list.addItem(item)

    @staticmethod
    def _recent_session_key(pair: tuple[int, Any]) -> tuple[int, int, int]:
        index, session = pair
        timestamp = getattr(session, "last_message_time", None)
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            return (0, -int(timestamp), index)
        return (1, 0, index)

    def _show_session_placeholder(
        self,
        title: str,
        detail: str = "",
        *,
        session_controls_ready: bool = False,
        session_container_visible: bool = False,
    ) -> None:
        """Render a non-interactive state inside the session list region."""
        self._session_box.setVisible(session_container_visible or session_controls_ready)
        self._set_session_controls_ready(session_controls_ready)
        self._selection_delegate.reset()
        self._session_list.clear()
        self._set_session_count(0)
        if session_controls_ready:
            self._session_empty_title.setText(title)
            self._session_empty_detail.setText(
                detail or ("试试其他关键词，或清空搜索。"
                           if title == _NO_MATCHING_SESSIONS else "当前来源暂无可分析内容。")
            )
            self._session_views.setCurrentWidget(self._session_empty_state)
            self._update_analyze_enabled()
            return
        # Workspace-owned login/loading surfaces retain their existing lifecycle.
        self._session_views.setCurrentWidget(self._session_list)
        text = f"{title}\n{detail}" if detail else title
        item = QListWidgetItem(text)
        item.setFlags(
            item.flags()
            & ~Qt.ItemFlag.ItemIsEnabled
            & ~Qt.ItemFlag.ItemIsSelectable
        )
        self._session_list.addItem(item)
        self._update_analyze_enabled()

    def _show_unconnected_session_placeholder(self) -> None:
        self._show_session_placeholder(
            _SESSION_EMPTY_TITLE,
            _SESSION_EMPTY_DETAIL,
        )

    def _set_session_count(self, count: int) -> None:
        self._session_box.setTitle(f"聊天会话（{count}）")

    def _set_session_controls_ready(self, ready: bool) -> None:
        self._sessions_ready = bool(ready)
        self._session_search.setEnabled(ready)
        self._session_sort.setEnabled(ready)
