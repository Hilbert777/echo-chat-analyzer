"""QQ login guidance and bounded cancellation without assuming a QR exists.

All data here is fictional; no real QQ client, QR code, or chat data is used.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

from test_gui import (  # noqa: F401  (fixtures are re-exported here for pytest)
    StubFacade,
    _GatedQRFacade,
    _IndependentDeferredExecutor,
    _drain,
    _inline_executor,
    _qq_snapshot,
    _write_qrcode_png,
    instant_qq_connect,
    qt_app,
    sources,
)

_LOADING_COPY = "等待二维码出现，请不要打开 QQ。"
_READY_COPY = "请使用手机 QQ 扫码登录"
_SLOW_COPY = "二维码尚未出现，你可以继续等待，或取消后重新连接。"


def _enter_waiting_auth(facade) -> QQWorkspace:
    workspace = QQWorkspace(facade, executor=_inline_executor())
    workspace._show_qq_status(_qq_snapshot("waiting_auth"), False)
    return workspace


def _expire_qr_wait(workspace: QQWorkspace) -> None:
    """Move the start stamp back so the bounded wait has already elapsed."""
    workspace._qq_waiting_auth_since = time.monotonic() - 9.0


def test_login_can_connect_and_load_sessions_without_any_qr(qt_app, sources) -> None:
    facade = _GatedQRFacade(_qq_snapshot("waiting_auth"), _qq_snapshot("waiting_auth"), sources=sources)
    workspace = _enter_waiting_auth(facade)
    assert facade.get_qq_qrcode_path() is None
    assert workspace._qq_guide_label.text() == _LOADING_COPY
    assert "请不要打开 QQ" in workspace._qq_login_guide_label.text()

    facade.set_snapshot(_qq_snapshot("connected"))
    workspace._poll_qq_status()

    assert workspace.session_panel._sessions_ready is True
    assert not workspace._qq_status_timer.isActive()
    assert not workspace._qq_qr_wait_timer.isActive()
    assert not workspace._qq_qrcode_label.isVisibleTo(workspace)
    assert not workspace._qq_guide_label.isVisibleTo(workspace)
    assert facade.disconnect_qq_calls == []


def test_cancelled_poll_cannot_change_the_retried_login_guide(qt_app, sources, instant_qq_connect) -> None:
    facade = _GatedQRFacade(_qq_snapshot("waiting_auth"), _qq_snapshot("waiting_auth"), sources=sources)
    executor = _IndependentDeferredExecutor()
    workspace = QQWorkspace(facade, executor=executor)
    workspace.connect_qq()
    first_connect = executor.tasks[-1]
    first_connect.succeed(_qq_snapshot("waiting_auth"))
    _drain(workspace)
    workspace._poll_qq_status()
    old_poll = executor.tasks[-1]
    _expire_qr_wait(workspace)
    workspace._on_qq_qr_wait_elapsed()
    assert workspace._qq_connect_button.isEnabled()

    workspace._qq_connect_button.click()
    # Complete the facade disconnect, then start a new authorization attempt.
    executor.tasks[-1].succeed(_qq_snapshot("disconnected"))
    workspace.connect_qq()
    executor.tasks[-1].succeed(_qq_snapshot("waiting_auth"))
    _drain(workspace)
    since = workspace._qq_waiting_auth_since
    assert workspace._qq_guide_label.text() == _LOADING_COPY

    old_poll.succeed(_qq_snapshot("connected"))
    old_poll.fail("fictional_old_error", "旧连接错误")
    first_connect.progress("等待 QQ 登录...")
    first_connect.succeed(_qq_snapshot("connected"))
    workspace._on_qq_qr_wait_elapsed()
    _drain(workspace)

    assert workspace._qq_guide_label.text() == _LOADING_COPY
    assert workspace._qq_waiting_auth_since == since
    assert workspace._qq_status_timer.isActive()
    assert not workspace._qq_connect_button.isEnabled()
    assert not workspace._qq_qrcode_label.isVisibleTo(workspace)
    assert not workspace.session_panel._sessions_ready


def test_cancel_after_slow_qr_really_disconnects_through_the_facade(
    qt_app, sources
) -> None:
    """The released button must act, not only look enabled."""
    facade = StubFacade(sources=sources)
    workspace = _enter_waiting_auth(facade)
    _expire_qr_wait(workspace)
    workspace._on_qq_qr_wait_elapsed()

    workspace._qq_connect_button.click()

    assert facade.disconnect_qq_calls == [1]
    assert facade.start_qq_auth_flow_calls == []
    assert workspace._qq_status_timer.isActive() is False
    assert workspace._qq_qr_wait_timer.isActive() is False
    assert workspace._qq_qrcode_label.isVisibleTo(workspace) is False


def test_late_qr_wait_callback_does_not_touch_a_new_connection_state(
    qt_app, sources
) -> None:
    facade = StubFacade(sources=sources)
    workspace = _enter_waiting_auth(facade)
    assert workspace._qq_connect_button.isEnabled() is False
    _expire_qr_wait(workspace)

    workspace._show_qq_status(_qq_snapshot("connected"), False)
    connected_enabled = workspace._qq_connect_button.isEnabled()

    workspace._on_qq_qr_wait_elapsed()  # a stale release callback

    assert workspace._qq_connect_button.isEnabled() == connected_enabled
    assert workspace._qq_guide_label.isVisibleTo(workspace) is False
    assert workspace._qq_qrcode_label.isVisibleTo(workspace) is False


def test_scan_timeout_still_wins_and_stops_the_release_timer(
    qt_app, sources
) -> None:
    """The existing 120s scan timeout keeps its behaviour and its precedence."""
    facade = StubFacade(sources=sources)
    workspace = _enter_waiting_auth(facade)
    workspace._qq_waiting_auth_since = time.monotonic() - 121.0

    workspace._poll_qq_status()

    assert facade.disconnect_qq_calls == [1]
    assert workspace._qq_connect_button.text() == "重新开始"
    assert workspace._qq_connect_button.isEnabled() is True
    assert workspace._qq_status_timer.isActive() is False
    assert workspace._qq_qr_wait_timer.isActive() is False
    assert workspace._qq_qrcode_label.isVisibleTo(workspace) is False
