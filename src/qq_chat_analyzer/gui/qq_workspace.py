"""QQ workspace: connection status, login guidance, and session analysis."""

from __future__ import annotations

import logging
import threading
import time
from math import ceil, cos, pi, sin
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..application.facade import ChatSource
from ..diagnostics import log_qq_session_event, qq_session_stage
from .progress_track import ConnectionProgressTrack
from .session_analysis_panel import SessionAnalysisPanel, SessionConnectionBar
from .theme import (
    HOME_COLOR_ACCENT,
    HOME_COLOR_MUTED,
    HOME_COLOR_PAPER,
    QQ_GUIDE_STYLE,
    QQ_SESSION_LOADING_STYLE,
    QQ_SETUP_QSS,
    QQ_STATUS_STYLE,
    QQ_STATUS_STYLE_ERROR,
)
from .workers import submit


_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.qq_workspace")

_QQ_CONNECT_LABEL = "连接QQ"
_QQ_CONNECTING = "正在准备QQ连接环境，请稍候..."
_QQ_CONNECT_FAILED = "QQ 连接失败"
_QQ_PATH_PROMPT_TITLE = "选择 QQ.exe"
_QQ_PATH_PROMPT_FILTER = "QQ 程序 (QQ.exe);;所有文件 (*)"
_QQ_PATH_INVALID_TITLE = "QQ 路径无效"
_QQ_PATH_INVALID_HINT = "请选择有效的 QQ.exe 文件。"
_QQ_PATH_SAVING = "正在保存 QQ 路径..."
_QQ_PATH_SAVED = "QQ 路径已保存，正在重新连接..."
_QQ_PATH_SAVE_FAILED = "QQ 路径保存失败"
_QQ_DISCONNECT_LABEL = "退出连接"
_QQ_DISCONNECTING = "正在退出QQ连接..."
_QQ_DISCONNECT_FAILED = "QQ退出连接失败"
_CANCEL_CONNECTION_LABEL = "取消连接"
_RESTART_CONNECTION_LABEL = "重新开始"
_CONNECTION_CANCELLED = "连接已取消，可以重新开始。"
_QQ_CONNECT_MIN_DISPLAY_MS = 500
_QQ_STATUS_POLL_INTERVAL_MS = 2000
_QQ_WAITING_AUTH_TIMEOUT_MS = 120_000
_QQ_AUTH_TIMEOUT_TITLE = "QQ登录等待超时"
_QQ_AUTH_TIMEOUT_HINT = "登录等待时间过长，请重新连接。"
# Before a QR is available, cancellation is disabled for at most this bound.
# Waiting without a QR must not trap the user or be treated as a failed login.
_QQ_QR_RELEASE_MS = 8000
_QQ_QR_LOADING_ACTION = "等待二维码出现，请不要打开 QQ。"
_QQ_QR_READY_ACTION = "请使用手机 QQ 扫码登录"
_QQ_QR_SLOW_ACTION = "二维码尚未出现，你可以继续等待，或取消后重新连接。"
_QQ_QRCODE_SIZE = 240
# The connection journey the trail walks: 准备 → 启动 QQ → 登录 → 连接.
_QQ_STAGE_PREPARING = 0
_QQ_STAGE_STARTING_QQ = 1
_QQ_STAGE_SCANNING = 2
_QQ_STAGE_CONNECTING = 3
_QQ_CONNECT_STAGES = ("准备", "启动 QQ", "登录", "连接")
# One short current action plus one short note per stage. Login does not
# require a QR; the scan instruction is shown only with a displayable fresh QR.
_QQ_STAGE_COPY = {
    _QQ_STAGE_PREPARING: (
        "正在准备连接环境",
        "Echo 正在检查本机的 QQ 环境。",
    ),
    _QQ_STAGE_STARTING_QQ: (
        "正在启动 QQ",
        "首次连接时，系统可能弹出权限确认窗口。"
        "这是 Echo 内置的 QQ 数据读取组件，请允许它运行。",
    ),
    _QQ_STAGE_SCANNING: (
        "等待二维码出现",
        "请不要打开 QQ，请等待二维码出现在此页面。",
    ),
    _QQ_STAGE_CONNECTING: (
        "正在准备聊天记录",
        "首次连接可能需要一点时间，Echo 正在整理可读取的会话内容。",
    ),
}
_QQ_IDLE_ACTION = _QQ_CONNECT_LABEL
_QQ_IDLE_NOTE = "点击后 Echo 会自动启动 QQ，等待你完成登录。"
_QQ_STATE_DISCONNECTED = "disconnected"
_QQ_STATE_INITIALIZING = "initializing"
_QQ_STATE_STARTING = "starting"
_QQ_STATE_WAITING_AUTH = "waiting_auth"
_QQ_STATE_CONNECTED = "connected"
_QQ_STATE_ERROR = "error"
_CONNECTED_PREFIX = "\U0001F7E2 "
_DISCONNECTED_PREFIX = "\U0001F534 "
_QQ_PENDING_PREFIX = "\U0001F7E1 "
_QQ_PROGRESS_STATES = (
    _QQ_STATE_INITIALIZING,
    _QQ_STATE_STARTING,
)
# Snapshot lifecycle → trail stage. States outside this map carry no trail.
_QQ_STATE_STAGE = {
    _QQ_STATE_INITIALIZING: _QQ_STAGE_PREPARING,
    _QQ_STATE_STARTING: _QQ_STAGE_STARTING_QQ,
    _QQ_STATE_WAITING_AUTH: _QQ_STAGE_SCANNING,
}
_QQ_STATE_MESSAGES = {
    _QQ_STATE_DISCONNECTED: "QQ 尚未连接。",
    _QQ_STATE_INITIALIZING: "正在初始化 QQ 连接，请稍候...",
    _QQ_STATE_STARTING: "正在启动 QQ，请稍候...",
    _QQ_STATE_WAITING_AUTH: "等待 QQ 登录...",
    _QQ_STATE_CONNECTED: "QQ 已连接。",
    _QQ_STATE_ERROR: "QQ 连接异常。",
}
_CONNECTION_STATUS_UNKNOWN = "无法确认连接状态。"
_QQ_STATUS_CHECKING = "正在检测 QQ 连接状态..."
_LOADING_SESSIONS = "正在加载会话列表..."
# The waiting copy the session read keeps while the first list is read.
_QQ_SESSION_LOADING_ACTION = "正在准备聊天记录"
_QQ_SESSION_LOADING_NOTE = "首次连接可能需要一点时间，Echo 正在整理可读取的会话内容。"


def _current_action_height(metrics: Any, text: str) -> int:
    """Return the height one current-action line really needs, from its font.

    ``font-size`` is a request, not a promise: the resolved font's ink can reach
    past the ascent and descent it declares - the Latin "Q" tail is the usual
    offender - and a box sized from the declared line height then cuts it. The
    reserve therefore comes from the metrics themselves: the line the layout
    needs plus whatever ink reaches outside it, never a hand-tuned offset.
    """
    ink = metrics.tightBoundingRect(text or "QQ")
    above = max(0.0, -ink.top() - metrics.ascent())
    below = max(0.0, ink.bottom() - metrics.descent())
    return int(
        ceil(
            metrics.ascent()
            + metrics.descent()
            + metrics.leading()
            + above
            + below
        )
    )


class _CurrentActionLabel(QLabel):
    """The 26px stage action, reserved from its font instead of its font-size.

    Qt sizes a word-wrapped label from the line box the font declares, so a
    glyph whose ink sits deeper than the declared descent - the Latin "Q" - is
    painted outside the box and cut by it. The label re-reserves its height from
    its own metrics whenever the font or the text it draws changes, which covers
    the style sheet's font taking effect and a moving current action.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("qqCurrentAction")
        self.setWordWrap(True)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.setStyleSheet(QQ_GUIDE_STYLE)

    def setText(self, text: str) -> None:
        super().setText(text)
        self._reserve_font_height()

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._reserve_font_height()

    def _reserve_font_height(self) -> None:
        self.setMinimumHeight(
            _current_action_height(QFontMetricsF(self.font()), self.text())
        )


class _QqProgressTrack(ConnectionProgressTrack):
    """QQ's own four-stage connection journey: 准备 → 启动 QQ → 扫码 → 连接."""

    STAGES = _QQ_CONNECT_STAGES

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if self.stage != _QQ_STAGE_CONNECTING:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        counter_width = painter.fontMetrics().horizontalAdvance("4 / 4") + 12
        slot = max(1.0, self.width() - 12 - counter_width) / 4
        center = QPointF(6 + slot * 3.5, 15)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(HOME_COLOR_PAPER))
        painter.drawRect(QRectF(center.x() - 10, 0, 20, 26))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(HOME_COLOR_ACCENT), 1.8))
        painter.drawArc(QRectF(center.x() - 5, 10, 10, 10), 40 * 16, 280 * 16)


class _PageTurningBook(QWidget):
    """A quiet line-drawn book; hidden pages never keep an animation timer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(60, 60)
        self.setAccessibleName("正在整理聊天记录")
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._phase = 0.0
        self._timer.start()

    def hideEvent(self, event: Any) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _advance(self) -> None:
        self._phase = (self._phase + 0.025) % 1.0
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(HOME_COLOR_MUTED), 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        book = QPainterPath()
        book.moveTo(30, 20)
        book.quadTo(19, 14, 7, 18)
        book.lineTo(7, 43)
        book.quadTo(19, 39, 30, 45)
        book.quadTo(41, 39, 53, 43)
        book.lineTo(53, 18)
        book.quadTo(41, 14, 30, 20)
        book.lineTo(30, 45)
        painter.drawPath(book)
        turn = cos(self._phase * pi)
        lift = sin(self._phase * pi) * 6
        page = QPainterPath()
        page.moveTo(30, 20)
        page.quadTo(30 + 12 * turn, 14 - lift, 30 + 23 * turn, 18 - lift)
        page.lineTo(30 + 23 * turn, 43 - lift)
        page.quadTo(30 + 12 * turn, 39 - lift, 30, 45)
        page.closeSubpath()
        painter.setBrush(QColor(HOME_COLOR_PAPER))
        painter.setPen(QPen(QColor(HOME_COLOR_ACCENT), 1.5))
        painter.drawPath(page)


class QQWorkspace(QWidget):
    """QQ workspace: connection status, QR code, and session analysis.

    This workspace owns the QQ connection and analysis lifecycle:
    status through ``get_qq_connection_snapshot``, auth through
    ``start_qq_auth_flow``, polling while waiting for login, then session
    loading and analysis through the shared panel.
    """

    analysis_started = Signal()
    analysis_succeeded = Signal(object)
    analysis_failed = Signal(str, str)
    analysis_phase_changed = Signal(object)
    status_changed = Signal(str)

    def __init__(
        self,
        facade: Any,
        parent: QWidget | None = None,
        executor: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("qqWorkspace")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(QQ_SETUP_QSS)
        self._facade = facade
        self._executor = executor or submit
        self._qq_connect_in_flight = False
        self._connection_task: Any = None
        self._qq_install_prompt_active = False
        # A cancelled QQ.exe prompt must not reappear on passive refreshes;
        # an explicit reconnect clears it again.
        self._qq_install_prompt_shown = False
        self._last_qq_status_message = ""
        self._qq_waiting_auth_since: float | None = None
        self._qq_attempt_generation = 0
        self._qq_reconnect_required = False
        self._qq_cancel_event: threading.Event | None = None
        self._sessions_loaded = False
        self._session_request: object | None = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self._connection_surface = QFrame()
        self._connection_surface.setObjectName("qqConnectionSurface")
        self._connection_layout = QVBoxLayout(self._connection_surface)
        self._connection_layout.setSpacing(24)
        main_layout.addWidget(self._connection_surface)

        # The trail replaces the top status line while the connection journey
        # runs; the status label keeps owning every other state.
        self._progress_track = _QqProgressTrack()
        self._progress_track.setVisible(False)
        self._connection_layout.addWidget(self._progress_track)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setVisible(False)
        self._status_label.setStyleSheet(QQ_STATUS_STYLE)

        self._qq_connect_button = QPushButton(_QQ_CONNECT_LABEL)
        self._qq_connect_button.setVisible(False)
        self._qq_connect_button.clicked.connect(self._on_qq_connect_clicked)
        self._qq_connect_button.setMinimumHeight(34)

        self._qq_disconnect_button = QPushButton(_QQ_DISCONNECT_LABEL)
        self._qq_disconnect_button.setVisible(False)
        self._qq_disconnect_button.clicked.connect(self.disconnect_qq)
        self._qq_disconnect_button.setMinimumHeight(34)
        self._connection_bar = SessionConnectionBar(
            self._status_label, self._qq_disconnect_button,
        )
        self._connection_layout.addWidget(self._connection_bar)

        self._qq_guide_label = _CurrentActionLabel()
        self._qq_guide_label.setVisible(False)

        self._qq_login_guide_label = QLabel("")
        self._qq_login_guide_label.setWordWrap(True)
        self._qq_login_guide_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._qq_login_guide_label.setVisible(False)
        self._qq_login_guide_label.setStyleSheet(QQ_GUIDE_STYLE)

        self._qq_qrcode_label = QLabel("")
        self._qq_qrcode_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self._qq_qrcode_label.setFixedSize(_QQ_QRCODE_SIZE, _QQ_QRCODE_SIZE)
        self._qq_qrcode_label.setVisible(False)

        # While scanning, the QR is the subject: short copy on the left, the
        # code itself on the right, exactly like the guided setup's picture.
        guide_text_column = QVBoxLayout()
        guide_text_column.setSpacing(20)
        guide_text_column.addWidget(self._qq_guide_label)
        guide_text_column.addWidget(self._qq_login_guide_label)
        guide_text_column.addStretch(1)

        self._qq_guide_row = QHBoxLayout()
        self._qq_guide_row.setSpacing(32)
        self._qq_guide_row.addLayout(guide_text_column, stretch=1)
        self._qq_guide_row.addWidget(
            self._qq_qrcode_label,
            stretch=0,
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        self._connection_layout.addLayout(self._qq_guide_row)

        actions = QHBoxLayout()
        actions.setSpacing(16)
        actions.addWidget(self._qq_connect_button)
        actions.addStretch(1)
        self._connection_layout.addLayout(actions)
        for button in (self._qq_connect_button, self._qq_disconnect_button):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.session_panel = SessionAnalysisPanel()
        self.session_panel.setObjectName("qqSessionPanel")
        self.session_panel.configure(facade, ChatSource.QQ, executor=self._executor)
        # Stage four keeps the trail above and centers a quiet loading group.
        self._session_loading = QWidget(self.session_panel)
        self._session_loading.setObjectName("qqSessionLoading")
        self._session_loading.setStyleSheet(QQ_SESSION_LOADING_STYLE)
        loading_layout = QVBoxLayout(self._session_loading)
        loading_layout.setContentsMargins(24, 0, 24, 0)
        loading_layout.setSpacing(14)
        loading_layout.addStretch(1)
        self._session_loading_book = _PageTurningBook()
        loading_layout.addWidget(self._session_loading_book, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._session_loading_action = QLabel(_QQ_SESSION_LOADING_ACTION)
        self._session_loading_action.setObjectName("qqSessionLoadingAction")
        self._session_loading_note = QLabel(_QQ_SESSION_LOADING_NOTE)
        for label in (self._session_loading_action, self._session_loading_note):
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMaximumWidth(520)
            label.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            loading_layout.addWidget(label, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._session_loading_exit = QPushButton(_QQ_DISCONNECT_LABEL)
        self._session_loading_exit.setObjectName("qqSessionLoadingExit")
        self._session_loading_exit.setCursor(Qt.CursorShape.PointingHandCursor)
        self._session_loading_exit.clicked.connect(self.disconnect_qq)
        loading_layout.addWidget(self._session_loading_exit, alignment=Qt.AlignmentFlag.AlignHCenter)
        loading_layout.addStretch(1)
        self.session_panel.layout().insertWidget(0, self._session_loading)
        self._session_loading.hide()
        main_layout.addWidget(self.session_panel, stretch=1)

        self.session_panel.analysis_started.connect(self._on_analysis_started)
        self.session_panel.analysis_succeeded.connect(self.analysis_succeeded.emit)
        self.session_panel.analysis_failed.connect(self._handle_analysis_error)
        self.session_panel.analysis_phase_changed.connect(self.analysis_phase_changed.emit)
        self.session_panel.status_changed.connect(self._on_panel_status)
        self.session_panel.workspace_width_changed.connect(self._update_setup_spacing)

        self._qq_status_timer = QTimer(self)
        self._qq_status_timer.setInterval(_QQ_STATUS_POLL_INTERVAL_MS)
        self._qq_status_timer.timeout.connect(self._poll_qq_status)

        # One single-shot bound for "QR still loading". It only releases the
        # cancel button; the 2s status poll keeps running alongside it.
        self._qq_qr_wait_timer = QTimer(self)
        self._qq_qr_wait_timer.setSingleShot(True)
        self._qq_qr_wait_timer.setInterval(_QQ_QR_RELEASE_MS)
        self._qq_qr_wait_timer.timeout.connect(self._on_qq_qr_wait_elapsed)

        self.session_panel.show_unconnected_placeholder()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_setup_spacing()

    def _update_setup_spacing(self) -> None:
        # Ready workspaces align the compact status row with the session panel.
        ready = self._sessions_loaded
        horizontal = max(24, (self.width() - self.session_panel.workspace_width_limit()) // 2) if ready else max(24, min(96, round(self.width() * 0.08)))
        vertical = 12 if ready else max(20, min(48, round(self.height() * 0.06)))
        self._connection_bar.set_compact(ready)
        self._connection_layout.setSpacing(0 if ready else 24)
        self._connection_layout.setContentsMargins(
            horizontal, vertical, horizontal, 0 if ready else 20,
        )
        # Wrapped loading copy needs its real font height at the available width.
        for label in (self._session_loading_action, self._session_loading_note):
            label.setFixedWidth(max(1, min(520, self.width() - 48)))
            metrics = QFontMetricsF(label.font())
            text_height = metrics.boundingRect(
                QRectF(0, 0, label.width(), 1000),
                int(Qt.TextFlag.TextWordWrap), label.text(),
            ).height()
            label.setFixedHeight(ceil(max(text_height, _current_action_height(metrics, label.text()))) + 4)

    # ---------------------------------------------------------------- public API

    def select_source(self, source: Any) -> None:
        """Configure the panel for QQ and reset transient state."""
        self.session_panel.configure(self._facade, ChatSource.QQ, executor=self._executor)
        self._invalidate_session_request()
        self._sessions_loaded = False
        self._stop_qq_status_polling()
        self._hide_qq_qrcode()
        self._set_stage(None)
        self._hide_qq_guide()
        self._qq_disconnect_button.setVisible(False)
        self.session_panel.clear()

    def refresh_connection_status(
        self,
        *,
        load_sessions_on_ready: bool = False,
    ) -> None:
        """Refresh the QQ connection status through the connection snapshot."""
        self.refresh_qq_status(load_sessions_on_ready=load_sessions_on_ready)

    def refresh_qq_status(self, *, load_sessions_on_ready: bool = False) -> None:
        """Ask the connection manager, through the facade, for QQ state."""
        generation = self._qq_attempt_generation
        self._set_stage(None)
        self._hide_qq_guide()
        self._status_label.setVisible(True)
        self._status_label.setStyleSheet(QQ_STATUS_STYLE)
        self._status_label.setText(_QQ_STATUS_CHECKING)
        self._status_label.setToolTip("")
        self.session_panel.show_connecting_placeholder()
        self.session_panel.update_analyze_enabled()
        self._executor(
            lambda: self._facade.get_qq_connection_snapshot(),
            on_success=lambda snapshot: self._show_qq_status(
                snapshot,
                load_sessions_on_ready,
            ) if generation == self._qq_attempt_generation else None,
            on_error=lambda code, message: self._handle_source_status_error(
                code,
                message,
            ) if generation == self._qq_attempt_generation else None,
        )

    # ---------------------------------------------------------------- status

    def _handle_source_status_error(self, code: str, message: str) -> None:
        self._handle_connection_status_error(code, message)

    def _handle_connection_status_error(self, code: str, message: str) -> None:
        if code == "qq_reconnect_required":
            self._require_qq_reconnect(message)
            return
        if self._qq_connect_in_flight:
            return
        if self._qq_reconnect_required:
            return
        self._set_stage(None)
        self._hide_qq_guide()
        self._status_label.setText(
            _CONNECTION_STATUS_UNKNOWN + _failure_code_suffix(code)
        )
        self._status_label.setToolTip(message)
        self._status_label.setStyleSheet(QQ_STATUS_STYLE)
        self._status_label.setVisible(True)
        self.session_panel.show_disconnected_placeholder()
        self._set_restart_action()
        self.session_panel.update_analyze_enabled()

    def _show_qq_status(
        self,
        snapshot: Any,
        load_sessions_on_ready: bool,
    ) -> None:
        """Render one QQ connection snapshot and the connect button."""
        if self._qq_connect_in_flight:
            return
        if getattr(snapshot, "code", None) == "qq_reconnect_required":
            self._require_qq_reconnect(_snapshot_message(snapshot))
            return
        if self._qq_reconnect_required:
            return
        state = _snapshot_state(snapshot)
        message = _snapshot_message(snapshot)
        action_hint = _snapshot_hint(snapshot)
        waiting_auth = state == _QQ_STATE_WAITING_AUTH
        connected = state == _QQ_STATE_CONNECTED
        if waiting_auth:
            if self._qq_waiting_auth_since is None:
                self._qq_waiting_auth_since = time.monotonic()
        else:
            self._qq_waiting_auth_since = None
        self._last_qq_status_message = message
        self._status_label.setStyleSheet(
            QQ_STATUS_STYLE_ERROR
            if state == _QQ_STATE_ERROR
            else QQ_STATUS_STYLE
        )

        code_suffix = (
            _failure_code_suffix(getattr(snapshot, "code", None))
            if state == _QQ_STATE_ERROR
            else ""
        )
        self._status_label.setText(
            f"{_snapshot_prefix(snapshot)}{message}{code_suffix}"
        )
        self._status_label.setToolTip(action_hint)
        self._status_label.setVisible(True)
        # While the journey advances by itself the button must not read like a
        # second invitation to connect: it is the way out of the journey.
        if _snapshot_in_progress(snapshot):
            self._qq_connect_button.setText(_CANCEL_CONNECTION_LABEL)
        elif state == _QQ_STATE_ERROR:
            self._qq_connect_button.setText(_RESTART_CONNECTION_LABEL)
        else:
            self._qq_connect_button.setText(_QQ_CONNECT_LABEL)
        self._qq_connect_button.setVisible(not connected)
        self._qq_connect_button.setEnabled(not _snapshot_in_progress(snapshot))
        self._qq_connect_button.setToolTip("")
        self._qq_disconnect_button.setVisible(connected)
        self._qq_disconnect_button.setEnabled(connected)
        self._qq_disconnect_button.setToolTip("")
        self.session_panel.update_analyze_enabled()

        if (
            state == _QQ_STATE_ERROR
            and getattr(snapshot, "code", None) == "qq_install_path_missing"
            and not self._qq_install_prompt_shown
        ):
            self._offer_qq_install_path_selection()

        if load_sessions_on_ready:
            self.status_changed.emit(message)

        if waiting_auth:
            self.session_panel.show_connecting_placeholder()
            self._show_qq_stage(_QQ_STAGE_SCANNING)
            self._start_qq_status_polling()
            self._refresh_qq_qrcode()
            self._sync_qq_qr_wait_state()
        elif state in _QQ_PROGRESS_STATES:
            self.session_panel.show_connecting_placeholder()
            self._show_qq_stage(_QQ_STATE_STAGE[state])
            self._stop_qq_status_polling()
            self._hide_qq_qrcode()
        else:
            if connected:
                if load_sessions_on_ready and not self._sessions_loaded:
                    # The trail's last stage covers the session read that
                    # follows the login; loading hands the page over on arrival.
                    self._show_qq_stage(_QQ_STAGE_CONNECTING)
                    self._load_sessions()
                else:
                    self._leave_qq_journey()
            else:
                self._leave_qq_journey()
                self.session_panel.show_disconnected_placeholder()
                if state == _QQ_STATE_DISCONNECTED:
                    self._show_qq_guide(_QQ_IDLE_ACTION, _QQ_IDLE_NOTE)
            self._stop_qq_status_polling()
            self._hide_qq_qrcode()

    def _poll_qq_status(self) -> None:
        """Refresh the QQ snapshot while the user is waiting to log in."""
        if self._qq_auth_waiting_expired():
            self._handle_qq_auth_timeout()
            return
        # A fresh local QR must not wait for the async snapshot/health worker:
        # the worker can stay blocked for seconds, but the QR file is already
        # fresh and can be shown immediately during this poll.
        if self._qq_waiting_auth_since is not None:
            # A QR that appears between polls must release the button at once,
            # without waiting for the snapshot worker to come back.
            self._refresh_qq_qrcode()
            self._sync_qq_qr_wait_state()
        generation = self._qq_attempt_generation
        self._executor(
            lambda: self._facade.get_qq_connection_snapshot(),
            on_success=lambda snapshot: self._show_qq_status(
                snapshot,
                load_sessions_on_ready=True,
            ) if self._qq_status_timer.isActive() and generation == self._qq_attempt_generation else None,
            on_error=lambda code, message: self._handle_connection_status_error(
                code, message,
            ) if self._qq_status_timer.isActive() and generation == self._qq_attempt_generation else None,
        )

    def _start_qq_status_polling(self) -> None:
        if not self._qq_status_timer.isActive():
            self._qq_status_timer.start()

    def _stop_qq_status_polling(self) -> None:
        self._qq_status_timer.stop()

    def _sync_qq_qr_wait_state(self) -> None:
        """Own the scan-stage copy and the cancel button while waiting for QR.

        The button is held disabled only until the QR shows or the bounded wait
        elapses. Once released it stays released: a later poll that still
        reports the same waiting state must not disable it again.
        """
        note = _QQ_STAGE_COPY[_QQ_STAGE_SCANNING][1]
        if self._qq_qrcode_label.isVisibleTo(self):
            self._stop_qq_qr_wait_timer()
            self._show_qq_guide(_QQ_QR_READY_ACTION, "请使用手机 QQ 扫描上方二维码，Echo 会自动继续。")
            self._qq_connect_button.setEnabled(True)
            return
        if self._qq_qr_wait_elapsed():
            self._stop_qq_qr_wait_timer()
            self._show_qq_guide(_QQ_QR_SLOW_ACTION, note)
            self._qq_connect_button.setEnabled(True)
            return
        self._show_qq_guide(_QQ_QR_LOADING_ACTION, note)
        self._qq_connect_button.setEnabled(False)
        self._start_qq_qr_wait_timer()

    def _qq_qr_wait_elapsed(self) -> bool:
        """Return whether the QR has been loading long enough to release."""
        since = self._qq_waiting_auth_since
        if since is None:
            return False
        return (time.monotonic() - since) * 1000 >= _QQ_QR_RELEASE_MS

    def _start_qq_qr_wait_timer(self) -> None:
        """Arm the single release bound once, counted from the wait's start."""
        if self._qq_qr_wait_timer.isActive():
            return
        remaining = _QQ_QR_RELEASE_MS
        since = self._qq_waiting_auth_since
        if since is not None:
            remaining = max(
                0, _QQ_QR_RELEASE_MS - int((time.monotonic() - since) * 1000)
            )
        self._qq_qr_wait_timer.start(remaining)

    def _stop_qq_qr_wait_timer(self) -> None:
        self._qq_qr_wait_timer.stop()

    def _on_qq_qr_wait_elapsed(self) -> None:
        """Release the cancel button after a slow QR load, and keep polling."""
        if self._qq_waiting_auth_since is None:
            return
        self._sync_qq_qr_wait_state()

    def _qq_auth_waiting_expired(self) -> bool:
        since = self._qq_waiting_auth_since
        if since is None:
            return False
        return (time.monotonic() - since) * 1000 >= _QQ_WAITING_AUTH_TIMEOUT_MS

    def _handle_qq_auth_timeout(self) -> None:
        """Stop polling and show a reconnectable error after a long wait."""
        self._stop_qq_status_polling()
        self._hide_qq_qrcode()
        self._set_stage(None)
        self._hide_qq_guide()
        self._status_label.setStyleSheet(QQ_STATUS_STYLE_ERROR)
        self._status_label.setText(_DISCONNECTED_PREFIX + _QQ_AUTH_TIMEOUT_TITLE)
        self._status_label.setToolTip(_QQ_AUTH_TIMEOUT_HINT)
        self._status_label.setVisible(True)
        self.session_panel.show_disconnected_placeholder()
        self._set_restart_action()
        self.status_changed.emit(_QQ_AUTH_TIMEOUT_TITLE)
        self._facade.disconnect_qq()

    def _refresh_qq_qrcode(self) -> None:
        """Show the runtime QR only when the facade says it is fresh."""
        try:
            path = self._facade.get_qq_qrcode_path()
        except Exception:
            _LOGGER.debug("[qq gui] qr path unavailable")
            path = None
        if path is None:
            self._hide_qq_qrcode()
            return
        try:
            pixmap = QPixmap(str(path))
        except Exception:
            pixmap = QPixmap()
        if pixmap.isNull():
            self._hide_qq_qrcode()
            return
        scaled = pixmap.scaled(
            self._qq_qrcode_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._qq_qrcode_label.setPixmap(scaled)
        self._qq_qrcode_label.setVisible(True)

    def _hide_qq_qrcode(self) -> None:
        """Clear and hide the QR image once it is no longer needed."""
        self._qq_qrcode_label.clear()
        self._qq_qrcode_label.setVisible(False)

    def _set_stage(self, stage: int | None) -> None:
        """Move the connection trail, or hand the top line back to the status.

        ``None`` leaves the guided journey: the trail hides and the status
        label (owned by the caller) speaks again. Leaving the scan stage also
        drops any pending QR release, so a stale callback can never touch a
        later connection state.
        """
        self._update_setup_spacing()
        if stage != _QQ_STAGE_SCANNING:
            self._stop_qq_qr_wait_timer()
        if stage is None:
            self._progress_track.setVisible(False)
            return
        self._progress_track.set_stage(stage)
        self._progress_track.setVisible(True)
        self._status_label.setVisible(False)

    def _show_qq_stage(self, stage: int) -> None:
        """Move the trail and its short action line onto one stage."""
        self._set_stage(stage)
        self._show_qq_guide(*_QQ_STAGE_COPY[stage])
        self._session_loading.setVisible(stage == _QQ_STAGE_CONNECTING)
        if stage == _QQ_STAGE_CONNECTING:
            self._qq_guide_label.hide()
            self._qq_login_guide_label.hide()
            self._qq_connect_button.hide()
            self._qq_disconnect_button.hide()

    def _show_qq_guide(self, action: str, note: str) -> None:
        """Speak one short action line plus one short note under the trail."""
        self._qq_guide_label.setText(action)
        self._qq_guide_label.setVisible(True)
        self._qq_login_guide_label.setText(note)
        self._qq_login_guide_label.setVisible(True)

    def _hide_qq_guide(self) -> None:
        """Hide the journey copy once the page speaks for itself again."""
        self._qq_guide_label.clear()
        self._qq_guide_label.setVisible(False)
        self._qq_login_guide_label.clear()
        self._qq_login_guide_label.setVisible(False)

    def _leave_qq_journey(self) -> None:
        """Leave the journey: no stage copy is left and the status line speaks."""
        self._set_stage(None)
        self._hide_qq_guide()
        self._session_loading.hide()
        if self._sessions_loaded:
            self._qq_disconnect_button.show()
        self._status_label.setVisible(True)

    # ---------------------------------------------------------------- connect

    def _on_qq_connect_clicked(self) -> None:
        """The waiting action exits even after the authorization worker ends."""
        if self._qq_connect_button.text() == _CANCEL_CONNECTION_LABEL:
            self.disconnect_qq()
        else:
            self.connect_qq()

    def connect_qq(self) -> None:
        """Start the QQ authorization flow in one click."""
        if self._qq_connect_in_flight:
            self.cancel_connection()
            return
        # An explicit connect is the user asking again: a previously cancelled
        # QQ.exe prompt may be offered once more.
        self._qq_install_prompt_shown = False
        self._qq_reconnect_required = False
        self.session_panel.cancel_analysis()
        _LOGGER.info("[qq gui] connect_qq requested")
        self._qq_attempt_generation += 1
        generation = self._qq_attempt_generation
        cancelled = threading.Event()
        self._qq_cancel_event = cancelled
        self._stop_qq_status_polling()
        self._invalidate_session_request()
        started_at = time.monotonic()
        self._qq_waiting_auth_since = None
        self._qq_connect_in_flight = True
        self._qq_connect_button.setText(_CANCEL_CONNECTION_LABEL)
        self._qq_connect_button.setEnabled(False)
        self._status_label.setVisible(True)
        self._status_label.setText(_QQ_CONNECTING)
        self._status_label.setToolTip("")
        self._show_qq_stage(_QQ_STAGE_PREPARING)
        self._hide_qq_qrcode()
        self.session_panel.show_connecting_placeholder()
        self.status_changed.emit(_QQ_CONNECTING)
        _LOGGER.info("[qq gui] connect_qq worker submitted")
        self._connection_task = self._executor(
            lambda report: self._facade.start_qq_auth_flow(progress=report, cancel_event=cancelled),
            on_success=lambda status: self._finish_qq_connect(
                status,
                started_at,
                generation,
            ),
            on_error=lambda code, message: self._finish_qq_connect_error(
                code,
                message,
                started_at,
                generation,
            ),
            on_progress=lambda message: self._handle_qq_connect_progress(message)
            if generation == self._qq_attempt_generation else None,
        )

    def _handle_qq_connect_progress(self, message: str) -> None:
        """Translate backend progress into one of the user-facing stages."""
        if not message:
            return
        stage = _QQ_STAGE_PREPARING if message == "请完全退出 QQ" else _qq_progress_stage(message)
        action = _QQ_STAGE_COPY[stage][0]
        self._status_label.setText(_QQ_PENDING_PREFIX + action)
        self._status_label.setToolTip("")
        self._status_label.setVisible(True)
        self._show_qq_stage(stage)
        if message == "请完全退出 QQ":
            self._show_qq_guide(message, "退出所有 QQ 窗口及托盘进程后，将自动继续连接。")
            self._qq_connect_button.setEnabled(True)
        self.status_changed.emit(action)

    def _after_qq_connect(self, snapshot: Any) -> None:
        self._show_qq_status(snapshot, load_sessions_on_ready=True)

    def _finish_qq_connect(self, status: Any, started_at: float, generation: int) -> None:
        def _apply() -> None:
            if generation != self._qq_attempt_generation:
                return
            self._qq_connect_in_flight = False
            self._connection_task = None
            _LOGGER.info(
                "[qq gui] connect_qq succeeded state=%s",
                _snapshot_state(status),
            )
            self._after_qq_connect(status)

        QTimer.singleShot(self._connect_display_delay(started_at), _apply)

    def _finish_qq_connect_error(
        self,
        code: str,
        message: str,
        started_at: float,
        generation: int,
    ) -> None:
        def _apply() -> None:
            if generation != self._qq_attempt_generation:
                return
            self._qq_connect_in_flight = False
            self._connection_task = None
            _LOGGER.info("[qq gui] connect_qq failed code=%s", code)
            self._handle_qq_connect_error(code, message)
            self._qq_connect_button.setEnabled(True)

        QTimer.singleShot(self._connect_display_delay(started_at), _apply)

    @staticmethod
    def _connect_display_delay(started_at: float) -> int:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        return max(0, _QQ_CONNECT_MIN_DISPLAY_MS - elapsed_ms)

    def _handle_qq_connect_error(self, code: str, message: str) -> None:
        self._show_qq_error(_qq_error_title(code), message, code=code)

    def _offer_qq_install_path_selection(self) -> None:
        """Ask the user for QQ.exe only when every automatic path failed.

        Asked once per workspace: once the prompt has been shown, a cancelled
        selection stays dismissed until the user explicitly connects again.
        """
        if self._qq_install_prompt_active or self._qq_install_prompt_shown:
            return
        self._qq_install_prompt_active = True
        self._qq_install_prompt_shown = True
        try:
            path, _ = QFileDialog.getOpenFileName(
                self,
                _QQ_PATH_PROMPT_TITLE,
                "",
                _QQ_PATH_PROMPT_FILTER,
            )
        finally:
            self._qq_install_prompt_active = False
        if not path:
            return
        candidate = Path(path)
        if (
            not candidate.is_file()
            or candidate.name.lower() != "qq.exe"
        ):
            self._show_qq_error(_QQ_PATH_INVALID_TITLE, _QQ_PATH_INVALID_HINT)
            return
        self._save_qq_install_path(candidate)

    def _save_qq_install_path(self, path: Path) -> None:
        """Persist the chosen QQ.exe through the facade, then retry."""
        _LOGGER.info("[qq gui] saving user-selected QQ path")
        self._leave_qq_journey()
        self._status_label.setStyleSheet(QQ_STATUS_STYLE)
        self._status_label.setText(_QQ_PATH_SAVING)
        self._status_label.setToolTip("")
        self._status_label.setVisible(True)
        self._executor(
            lambda: self._facade.set_qq_install_path(path),
            on_success=lambda _status: self._after_qq_install_path_saved(),
            on_error=self._handle_qq_install_path_save_error,
        )

    def _after_qq_install_path_saved(self) -> None:
        """Reconnect immediately after the user-selected path is saved."""
        self._leave_qq_journey()
        self._status_label.setStyleSheet(QQ_STATUS_STYLE)
        self._status_label.setText(_QQ_PATH_SAVED)
        self._status_label.setToolTip("")
        self._status_label.setVisible(True)
        self.connect_qq()

    def _handle_qq_install_path_save_error(
        self,
        code: str,
        message: str,
    ) -> None:
        _LOGGER.warning(
            "[qq gui] save QQ path failed code=%s",
            code,
        )
        self._show_qq_error(_QQ_PATH_SAVE_FAILED, message, code=code)

    def _show_qq_error(self, title: str, message: str, *, code: Any = None) -> None:
        """Render a failure with its fixed internal code when one is provided.

        The code is a literal call-site token (never exception text, server data
        or a path), so the user can report the failing stage even when the
        diagnostic log could not be created. Unreviewed values are not shown.
        """
        self._leave_qq_journey()
        self._status_label.setStyleSheet(QQ_STATUS_STYLE_ERROR)
        self._status_label.setText(_DISCONNECTED_PREFIX + title + _failure_code_suffix(code))
        self._status_label.setToolTip(message)
        self._status_label.setVisible(True)
        self.session_panel.show_disconnected_placeholder()
        self._set_restart_action()
        self.status_changed.emit(title)

    def _set_restart_action(self) -> None:
        self._qq_connect_button.setText(_RESTART_CONNECTION_LABEL)
        self._qq_connect_button.setEnabled(True)
        self._qq_connect_button.setVisible(True)
        self._qq_disconnect_button.setVisible(False)

    def disconnect_qq(self) -> None:
        """Stop the current QQ session and return to a reconnectable page."""
        if self._qq_connect_in_flight:
            self.cancel_connection()
            return
        _LOGGER.info("[qq gui] disconnect_qq requested")
        self._qq_attempt_generation += 1
        generation = self._qq_attempt_generation
        self.session_panel.cancel_analysis()
        self._invalidate_session_request()
        self._stop_qq_status_polling()
        self._hide_qq_qrcode()
        self._leave_qq_journey()
        self._qq_disconnect_button.setEnabled(False)
        self._status_label.setVisible(True)
        self._status_label.setText(_QQ_DISCONNECTING)
        self._status_label.setToolTip("")
        self.session_panel.clear()
        self.status_changed.emit(_QQ_DISCONNECTING)
        self._executor(
            lambda: self._facade.disconnect_qq()
            if generation == self._qq_attempt_generation else None,
            on_success=lambda snapshot: self._show_qq_status(snapshot, False)
            if generation == self._qq_attempt_generation else None,
            on_error=lambda code, message: self._handle_qq_disconnect_error(
                code,
                message,
            ) if generation == self._qq_attempt_generation else None,
        )

    def _handle_qq_disconnect_error(self, code: str, message: str) -> None:
        self._qq_disconnect_button.setEnabled(True)
        self._show_qq_error(_QQ_DISCONNECT_FAILED, message, code=code)

    def cancel_connection(self) -> None:
        """Cancel the active source task and return to a reconnectable page."""
        task = self._connection_task
        if task is None and not self._qq_connect_in_flight:
            return
        self._qq_attempt_generation += 1
        cancelled = self._qq_cancel_event
        if cancelled is not None:
            cancelled.set()
        cancel = getattr(task, "cancel", None)
        if callable(cancel):
            cancel()
        cleanup = getattr(self._facade, "cancel_qq_auth_flow", None)
        if callable(cleanup) and cancelled is not None:
            self._executor(lambda: cleanup(cancelled),
                           on_success=lambda _result: None,
                           on_error=lambda _code, _message: None)
        self._connection_task = None
        self._qq_connect_in_flight = False
        self._qq_waiting_auth_since = None
        self._invalidate_session_request()
        self._stop_qq_status_polling()
        self._hide_qq_qrcode()
        self._leave_qq_journey()
        self._qq_connect_button.setText(_QQ_CONNECT_LABEL)
        self._qq_connect_button.setEnabled(True)
        self._qq_connect_button.setVisible(True)
        self._qq_disconnect_button.setVisible(False)
        self.session_panel.show_disconnected_placeholder()
        self._status_label.setText(_CONNECTION_CANCELLED)
        self._status_label.setVisible(True)
        self.status_changed.emit(_CONNECTION_CANCELLED)

    def cancel_analysis(self) -> None:
        """Cancel the active analysis."""
        self.session_panel.cancel_analysis()

    # ---------------------------------------------------------------- sessions

    def _load_sessions(self) -> None:
        if self._qq_reconnect_required:
            return
        # Several queued connection snapshots can resolve to connected. They
        # belong to one load, not independent acquisitions of the live DB.
        if self._session_request is not None:
            return
        request = object()
        self._session_request = request
        started_at = time.monotonic()
        self._sessions_loaded = False
        self.session_panel.show_reading_placeholder()
        self._session_loading.show()
        self.status_changed.emit(_LOADING_SESSIONS)
        log_qq_session_event(_LOGGER, "gui_request", "started")

        def terminal(outcome: str) -> None:
            log_qq_session_event(
                _LOGGER, "gui_request", outcome,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                failure_code="operation_failed" if outcome == "failed" else "none",
            )

        def succeeded(sessions: Any) -> None:
            if self._session_request is not request:
                terminal("discarded_success")
                return
            self._session_request = None
            try:
                with qq_session_stage(_LOGGER, "gui_render"):
                    self._handle_sessions_loaded(sessions)
            except Exception:
                terminal("failed")
                raise
            terminal("success")

        def failed(code: str, message: str) -> None:
            if self._session_request is not request:
                terminal("discarded_failure")
                return
            terminal("failed")
            self._session_request = None
            self._handle_session_error(code, message)

        self._executor(
            lambda: self._facade.list_sessions(ChatSource.QQ) if self._session_request is request else [],
            on_success=succeeded,
            on_error=failed,
        )

    def _invalidate_session_request(self) -> None:
        # Do not interrupt acquisition/cleanup. Only invalidate its GUI result.
        self._session_request = None
        self._sessions_loaded = False
        self._session_loading.hide()

    def _handle_sessions_loaded(self, sessions: Any) -> None:
        self._sessions_loaded = True
        self.session_panel.populate_sessions(sessions)
        self._session_loading.hide()
        # The journey is over: the session panel owns the page from here.
        self._leave_qq_journey()
        self._hide_qq_qrcode()
        self._update_setup_spacing()
        self.status_changed.emit(
            self._last_qq_status_message
            or _QQ_STATE_MESSAGES[_QQ_STATE_CONNECTED]
        )

    def _handle_session_error(self, code: str, message: str) -> None:
        """Render a session-read failure as a connection error, not analysis.

        Reading the session list belongs to connecting. It must never be
        reported through ``analysis_failed``, which belongs to a user-started
        analysis and drives the main window's "分析失败" dialog. The error stays
        visible on the page and the restart action stays available.
        """
        if code == "qq_reconnect_required":
            self._require_qq_reconnect(message)
            return
        self._sessions_loaded = False
        self._stop_qq_status_polling()
        self._hide_qq_qrcode()
        self._show_qq_error(_qq_error_title(code), message, code=code)

    # ---------------------------------------------------------------- signals

    def _require_qq_reconnect(self, message: str) -> None:
        if not self._qq_reconnect_required:
            self._qq_attempt_generation += 1
        self._qq_reconnect_required = True
        self.session_panel.cancel_analysis()
        self._invalidate_session_request()
        self._stop_qq_status_polling()
        self._hide_qq_qrcode()
        self._show_qq_error(
            "QQ 运行环境已变化，请重新连接。", message,
            code="qq_reconnect_required",
        )

    def _handle_analysis_error(self, code: str, message: str) -> None:
        if code == "qq_reconnect_required":
            self._require_qq_reconnect(message)
        else:
            self.analysis_failed.emit(code, message)

    def _on_analysis_started(self) -> None:
        self.analysis_started.emit()

    def _on_panel_status(self, message: str) -> None:
        self.status_changed.emit(message)


def _snapshot_state(snapshot: Any) -> str:
    """Read the lifecycle state a connection snapshot resolved."""
    state = getattr(snapshot, "state", None)
    value = getattr(state, "value", state)
    return (
        value
        if value in _QQ_STATE_MESSAGES
        else _QQ_STATE_DISCONNECTED
    )


def _snapshot_prefix(snapshot: Any) -> str:
    """Pick the status dot that matches one lifecycle state."""
    state = _snapshot_state(snapshot)
    if state == _QQ_STATE_CONNECTED:
        return _CONNECTED_PREFIX
    if state in _QQ_PROGRESS_STATES or state == _QQ_STATE_WAITING_AUTH:
        return _QQ_PENDING_PREFIX
    return _DISCONNECTED_PREFIX


def _snapshot_message(snapshot: Any) -> str:
    message = getattr(snapshot, "message", "") or ""
    if message:
        return message
    return _QQ_STATE_MESSAGES.get(
        _snapshot_state(snapshot),
        _CONNECTION_STATUS_UNKNOWN,
    )


def _snapshot_hint(snapshot: Any) -> str:
    return getattr(snapshot, "action_hint", "") or ""


def _snapshot_in_progress(snapshot: Any) -> bool:
    return (
        _snapshot_state(snapshot) in _QQ_PROGRESS_STATES
        or _snapshot_state(snapshot) == _QQ_STATE_WAITING_AUTH
    )


def _qq_progress_stage(message: str) -> int:
    """Map one backend progress message onto the connection trail."""
    lowered = message.lower()
    if any(term in lowered for term in ("扫码", "登录", "auth", "qrcode")):
        return _QQ_STAGE_SCANNING
    return _QQ_STAGE_STARTING_QQ


#: Literal codes produced by the QQ application, provider and runtime layers
#: (reviewed 2026-10-19; the drift test keeps this list honest). Anything else,
#: including class-derived fallbacks such as ``os_error``, is never displayed.
_REVIEWED_QQ_FAILURE_CODES = frozenset({
    "budget_exhausted",
    "napcat_qq_auth_rejected",
    "napcat_qq_unavailable",
    "napcat_qq_worker_generation_changed",
    "qq_auth_cancelled",
    "qq_auth_window_unavailable",
    "qq_config_corrupted",
    "qq_config_not_found",
    "qq_config_write_failed",
    "qq_direct_database_not_ready",
    "qq_direct_database_recovery_failed",
    "qq_direct_database_shutting_down",
    "qq_direct_database_unavailable",
    "qq_direct_session_not_found",
    "qq_direct_snapshot_acquire_failed",
    "qq_direct_snapshot_cleanup_failed",
    "qq_direct_snapshot_invalid",
    "qq_environment_config_error",
    "qq_install_path_invalid",
    "qq_install_path_missing",
    "qq_invalid_environment",
    "qq_process_detection_failed",
    "qq_process_exit_timeout",
    "qq_provider_unavailable",
    "qq_reconnect_required",
    "qq_resources_changed",
    "qq_resources_unverified",
    "qq_runtime_cleanup_failed",
    "qq_runtime_error",
    "qq_runtime_identity_mismatch",
    "qq_runtime_ownership_unknown",
    "qq_snapshot_cleanup_failed",
    "qq_snapshot_invalid_generation_id",
    "qq_snapshot_runtime_error",
    "qq_snapshot_runtime_failure",
    "qq_snapshot_runtime_invalid_response",
    "qq_snapshot_runtime_not_ready",
    "qq_snapshot_runtime_unauthorized",
    "qq_snapshot_runtime_unavailable",
    "qq_snapshot_worker_generation_changed",
    "qq_version_metadata_conflict",
    "qq_version_too_old",
    "runtime_directory_missing",
    "source_unavailable",
})


def _failure_code_suffix(code: Any) -> str:
    """Return the fixed-code suffix for a reviewed code, otherwise nothing."""
    if isinstance(code, str) and code in _REVIEWED_QQ_FAILURE_CODES:
        return f"（错误代码：{code}）"
    return ""


def _qq_error_title(code: str) -> str:
    if code in {"qq_not_installed", "qq_runtime_missing", "runtime_unavailable"}:
        return "QQ连接环境启动失败"
    if code in {"qq_login_timeout", "qq_auth_failed", "authentication_failed"}:
        return "QQ登录失败"
    if code == "service_unavailable":
        return "QQ连接服务启动失败"
    return _QQ_CONNECT_FAILED
