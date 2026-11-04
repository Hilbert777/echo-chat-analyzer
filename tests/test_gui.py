"""Behavior tests for the PySide6 GUI layer.

These tests never open a real window: Qt runs on the ``offscreen`` platform
and every facade call is served by a stub. The GUI is verified as a pure
consumer of the facade.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 is required for the GUI layer")

from PySide6.QtCore import QDate, QThreadPool, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


def _facade_module():
    return importlib.import_module("qq_chat_analyzer.application.facade")


@pytest.fixture(scope="session")
def qt_app():
    """One QApplication for the whole session; Qt forbids more than one."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def sources():
    module = _facade_module()
    return (
        module.SourceInfo(
            source=module.ChatSource.QQ,
            display_name="QQ",
            available=True,
        ),
        module.SourceInfo(
            source=module.ChatSource.WECHAT,
            display_name="\u5fae\u4fe1",
            available=False,
            description="\u5fae\u4fe1\u6570\u636e\u6e90\u5c1a\u672a\u914d\u7f6e\u3002",
        ),
    )


@pytest.fixture
def instant_qq_connect(monkeypatch):
    """Make the QQ connect min-display delay deterministic.

    ``_connect_display_delay`` keeps the "connecting" state visible for
    ``_QQ_CONNECT_MIN_DISPLAY_MS`` (500ms) and applies the result through
    ``QTimer.singleShot``. Tests assert the *applied* state, not the pause, so
    dropping the delay to 0 turns that callback into a plain zero-timer that
    ``_drain`` delivers immediately - no fixed real wait.
    """
    monkeypatch.setattr(
        importlib.import_module("qq_chat_analyzer.gui.qq_workspace"),
        "_QQ_CONNECT_MIN_DISPLAY_MS",
        0,
    )


class _StaticConnectionService:
    """Return one fixed QQ connection status."""

    def __init__(self, status):
        self._status = status

    def check_status(self):
        return self._status


class StubFacade:
    """Stand in for ChatAnalyzerFacade with recorded calls."""

    def __init__(
        self,
        sources=(),
        sessions=(),
        outcome=None,
        error=None,
        connection_status=None,
        connection_status_after_connect=None,
        connection_error=None,
        verify_error=None,
        connect_qq_error=None,
        setup_status=None,
        setup_error=None,
        data_root=None,
        data_roots=(),
        message_range=None,
        reports=(),
        report_issues=(),
        report_usage=None,
        list_reports_error=None,
    ):
        self._sources = tuple(sources)
        self._sessions = list(sessions)
        self._outcome = outcome
        self._error = error
        self._connection_status = connection_status
        self._connection_status_after_connect = connection_status_after_connect
        self._connection_error = connection_error
        self._verify_error = verify_error
        self._connect_qq_error = connect_qq_error
        self._setup_status = setup_status
        self._setup_error = setup_error
        self._data_root = data_root
        self._data_roots = list(data_roots)
        self._message_range = message_range
        self._reports = list(reports)
        self._report_issues = tuple(report_issues)
        self._report_usage = report_usage
        self._list_reports_error = list_reports_error
        self.list_sessions_calls: list[object] = []
        self.get_connection_status_calls: list[object] = []
        self.get_wechat_setup_status_calls: list[object] = []
        self.setup_wechat_environment_calls: list[object] = []
        self.detect_wechat_data_root_calls: list[object] = []
        self.detect_wechat_data_roots_calls: list[object] = []
        self.acquire_wechat_db_key_calls: list[object] = []
        self.verify_wechat_database_calls: list[object] = []
        self.set_qq_install_path_calls: list[object] = []
        self.start_qq_auth_flow_calls: list[object] = []
        self.get_qq_connection_snapshot_calls: list[object] = []
        self.shutdown_calls: list[object] = []
        self.disconnect_qq_calls: list[object] = []
        self.disconnect_wechat_calls: list[object] = []
        self.get_session_message_range_calls: list[tuple] = []
        self.analyze_session_calls: list[tuple] = []
        self.list_report_packages_calls: list[object] = []

    def list_report_packages(self):
        from qq_chat_analyzer.application.report_package_catalog import ReportPackageListing, ReportStorageUsage
        self.list_report_packages_calls.append(1)
        if self._list_reports_error is not None:
            raise self._list_reports_error
        return ReportPackageListing(
            tuple(self._reports), self._report_issues,
            self._report_usage or ReportStorageUsage(len(self._reports), 0),
        )

    def delete_report_packages(self, names):
        from qq_chat_analyzer.application.report_package_catalog import ReportPackageDeletionResult
        self._reports = [record for record in self._reports if record.package_name not in names]
        return ReportPackageDeletionResult(tuple(names))

    def list_sources(self):
        return self._sources

    def list_sessions(self, source):
        self.list_sessions_calls.append(source)
        if self._error is not None:
            raise self._error
        return self._sessions

    def get_connection_status(self, source):
        self.get_connection_status_calls.append(source)
        if self._connection_error is not None:
            raise self._connection_error
        if self._connection_status is None:
            return self._default_connection_status(source)
        return self._connection_status

    def set_qq_install_path(self, path):
        self.set_qq_install_path_calls.append(path)
        if self._setup_error is not None:
            raise self._setup_error
        return None

    def start_qq_auth_flow(self, progress=None, *, cancel_event=None):
        self.start_qq_auth_flow_calls.append(1)
        if progress is not None:
            progress("正在加载 NapCat...")
        if self._connect_qq_error is not None:
            raise self._connect_qq_error
        if self._connection_status_after_connect is not None:
            self._connection_status = self._connection_status_after_connect
        return self._qq_snapshot()

    def get_qq_connection_snapshot(self):
        self.get_qq_connection_snapshot_calls.append(1)
        if self._connection_error is not None:
            raise self._connection_error
        return self._qq_snapshot()

    def shutdown(self):
        self.shutdown_calls.append(1)

    def disconnect_qq(self):
        self.disconnect_qq_calls.append(1)
        if self._connection_error is not None:
            raise self._connection_error
        module = _facade_module()
        self._connection_status = module.QQConnectionStatus(
            available=False,
            runtime_running=False,
            qq_online=False,
            version="",
            message="QQ 尚未连接。",
            action_hint="",
        )
        return self._qq_snapshot()

    def disconnect_wechat(self):
        self.disconnect_wechat_calls.append(1)
        if self._connection_error is not None:
            raise self._connection_error
        module = _facade_module()
        self._connection_status = module.WeChatConnectionStatus(
            available=False,
            data_found=True,
            db_key_available=False,
            runtime_available=True,
            message="等待微信登录",
            action_hint="",
        )
        return self._connection_status

    def _qq_snapshot(self):
        """Map the stubbed QQ status onto the connection lifecycle model."""
        module = _facade_module()
        status = self._connection_status
        if status is None:
            status = self._default_connection_status(module.ChatSource.QQ)
        manager = importlib.import_module(
            "qq_chat_analyzer.application.qq.qq_connection_manager"
        )
        return manager.QQConnectionManager(
            connection_service=_StaticConnectionService(status),
        ).get_snapshot()

    def get_session_message_range(self, source, session_id):
        self.get_session_message_range_calls.append((source, session_id))
        return self._message_range

    def get_wechat_setup_status(self):
        self.get_wechat_setup_status_calls.append(1)
        if self._setup_error is not None:
            raise self._setup_error
        if self._setup_status is not None:
            return self._setup_status
        module = importlib.import_module(
            "qq_chat_analyzer.application.wechat.wechat_setup_service"
        )
        return module.WeChatSetupStatus(
            state=module.WeChatSetupState.CONFIG_MISSING,
            configured=False,
            message="\u5fae\u4fe1\u73af\u5883\u672a\u51c6\u5907",
            action_hint="\u8bf7\u5148\u5b8c\u6210\u5fae\u4fe1\u73af\u5883\u8bbe\u7f6e",
        )

    def detect_wechat_data_root(self):
        self.detect_wechat_data_root_calls.append(1)
        return self._data_root

    def detect_wechat_data_roots(self):
        self.detect_wechat_data_roots_calls.append(1)
        if self._data_roots:
            return [Path(value) for value in self._data_roots]
        if self._data_root is not None:
            return [Path(self._data_root)]
        return []

    def acquire_wechat_db_key(self, progress=None, *, cancel_event=None):
        self.acquire_wechat_db_key_calls.append(progress)
        if self._connection_status_after_connect is not None:
            self._connection_status = self._connection_status_after_connect
        return "fictional-key-64"

    def verify_wechat_database(self):
        self.verify_wechat_database_calls.append(1)
        if self._verify_error is not None:
            raise self._verify_error
        return None

    def setup_wechat_environment(self, config):
        self.setup_wechat_environment_calls.append(config)
        if self._setup_error is not None:
            raise self._setup_error
        return self._connection_status or self._default_connection_status(
            _facade_module().ChatSource.WECHAT
        )

    def _default_connection_status(self, source):
        module = _facade_module()
        if source == module.ChatSource.WECHAT:
            return module.WeChatConnectionStatus(
                available=True,
                data_found=True,
                db_key_available=True,
                runtime_available=True,
                message="\u5fae\u4fe1\u53ef\u7528",
                action_hint="",
            )
        return module.QQConnectionStatus(
            available=True,
            runtime_running=True,
            qq_online=True,
            version="4.1.0",
            message="\u5df2\u8fde\u63a5",
            action_hint="",
        )

    def analyze_session(self, source, session_id, config=None, progress=None, on_phase=None, cancelled=lambda: False):
        self.analyze_session_calls.append((source, session_id, config))
        if on_phase is not None:
            from qq_chat_analyzer.application.analysis_phase import AnalysisPhase
            on_phase(AnalysisPhase.READING)
            on_phase(AnalysisPhase.ANALYZING_REPORT)
        if progress is not None:
            progress("正在分析聊天内容...")
        if self._error is not None:
            raise self._error
        return self._outcome


def _session(
    source,
    session_id: str,
    display_name: str,
    count=None,
    last_message_time=None,
):
    module = _facade_module()
    return module.SessionInfo(
        source=source,
        session_id=session_id,
        display_name=display_name,
        message_count=count,
        last_message_time=last_message_time,
    )


class _StubOutcome:
    def __init__(
        self,
        *,
        data_acquired_at=None,
        report_path=None,
        report_directory=None,
    ):
        self.data_acquired_at = data_acquired_at
        self.report_path = report_path
        self.report_directory = report_directory


def _main_window(qt_app, facade, executor=None):
    module = importlib.import_module("qq_chat_analyzer.gui.main_window")
    return module.MainWindow(facade, executor=executor or _inline_executor())


def _inline_executor():
    """Run facade calls on the calling thread.

    The GUI defaults to a real thread pool. Tests inject this instead so no
    test depends on thread scheduling or on a Qt event loop turn.
    """
    module = importlib.import_module("qq_chat_analyzer.gui.workers")
    return module.run_inline


def test_cancelled_worker_stops_emitting_and_cleans_up(qt_app) -> None:
    workers = importlib.import_module("qq_chat_analyzer.gui.workers")
    started = threading.Event()
    release = threading.Event()
    successes: list[object] = []
    errors: list[tuple[str, str]] = []
    progress: list[str] = []

    def operation(report):
        report("step-1")
        started.set()
        release.wait(timeout=5)
        report("step-2")
        return "done"

    workers.submit(
        operation,
        on_success=successes.append,
        on_error=lambda code, message: errors.append((code, message)),
        on_progress=progress.append,
    )

    assert started.wait(timeout=2)
    for worker in workers._PENDING:
        worker.cancel()
    release.set()
    _settle_workers()

    assert progress == ["step-1"]
    assert successes == []
    assert errors == []
    assert workers._PENDING == set()


def test_worker_stops_when_signal_source_is_destroyed(qt_app) -> None:
    workers = importlib.import_module("qq_chat_analyzer.gui.workers")
    started = threading.Event()
    progress_received = threading.Event()
    successes: list[object] = []
    errors: list[tuple[str, str]] = []
    progress: list[str] = []

    def operation(report):
        report("step-1")
        started.set()
        progress_received.wait(timeout=5)
        report("step-2")
        return "done"

    worker = workers.submit(
        operation,
        on_success=successes.append,
        on_error=lambda code, message: errors.append((code, message)),
        on_progress=lambda message: (
            progress.append(message),
            progress_received.set(),
        ),
    )

    assert started.wait(timeout=2)
    deadline = time.monotonic() + 2
    while not progress_received.is_set() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert progress_received.is_set()
    import shiboken6

    shiboken6.delete(worker._callback_relay)
    _settle_workers()

    assert progress == ["step-1"]
    assert successes == []
    assert errors == []
    assert workers._PENDING == set()


def test_worker_shutdown_is_non_blocking_and_clears_pool(
    qt_app,
    monkeypatch,
) -> None:
    workers = importlib.import_module("qq_chat_analyzer.gui.workers")
    pool = QThreadPool.globalInstance()
    clears: list[int] = []
    waits: list[int] = []
    monkeypatch.setattr(pool, "clear", lambda: clears.append(1))
    monkeypatch.setattr(
        pool,
        "waitForDone",
        lambda timeout: waits.append(timeout) or True,
    )

    workers.shutdown()

    assert clears == [1]
    assert waits == []


class _DeferredExecutor:
    """Capture a facade call and let the test finish it manually."""

    def __init__(self):
        self.operation = None
        self.on_success = None
        self.on_error = None
        self.on_finished = None
        self.on_progress = None
        self.submission_count = 0
        self.cancelled = False

    def __call__(
        self,
        operation,
        *,
        on_success,
        on_error,
        on_finished=None,
        on_progress=None,
    ):
        self.submission_count += 1
        self.operation = operation
        self.on_success = on_success
        self.on_error = on_error
        self.on_finished = on_finished
        self.on_progress = on_progress
        return self

    def cancel(self):
        self.cancelled = True

    def progress(self, message):
        if self.on_progress is not None:
            self.on_progress(message)

    def succeed(self, result):
        if self.on_success is not None:
            self.on_success(result)
        if self.on_finished is not None:
            self.on_finished()

    def fail(self, code, message):
        if self.on_error is not None:
            self.on_error(code, message)
        if self.on_finished is not None:
            self.on_finished()


class _QueuedExecutor:
    """Keep every submitted call so a test chooses the completion order."""

    def __init__(self):
        self.tasks: list[tuple] = []

    def __call__(
        self,
        operation,
        *,
        on_success,
        on_error,
        on_finished=None,
        on_progress=None,
    ):
        self.tasks.append((operation, on_success, on_error, on_finished))
        return self.tasks[-1]

    def succeed(self, index: int) -> None:
        operation, on_success, _on_error, on_finished = self.tasks[index]
        on_success(operation())
        if on_finished is not None:
            on_finished()

    def fail(self, index: int, code: str, message: str) -> None:
        _operation, _on_success, on_error, on_finished = self.tasks[index]
        on_error(code, message)
        if on_finished is not None:
            on_finished()


@pytest.mark.parametrize("cancel", [False, True])
def test_qq_process_wait_runs_in_background_through_real_facade(qt_app, monkeypatch, cancel):
    from qq_chat_analyzer.application.qq import qq_auth_bridge as auth
    from qq_chat_analyzer.application.qq.qq_process_registry import QQProcessRegistry
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace
    from qq_chat_analyzer.application.connection_models import ConnectionSnapshot, ConnectionState
    main_thread = threading.get_ident()
    detected_threads = []
    conflicts = threading.Event()
    conflicts.set()
    completed = threading.Event()
    launches = []
    def detect(owned):
        detected_threads.append(threading.get_ident())
        return [101, 102] if conflicts.is_set() else []
    def launch():
        launches.append(threading.get_ident())
    monkeypatch.setattr(auth, "find_conflicting_qq_pids", detect)
    manager = SimpleNamespace(
        get_snapshot=lambda: ConnectionSnapshot(ConnectionState.DISCONNECTED, "qq", "disconnected"),
        begin_auth_waiting=lambda: None, end_auth_waiting=completed.set,
    )
    bridge = auth.QQAuthBridge(setup_service=SimpleNamespace(), manager=manager,
                              window_launcher=launch, process_registry=QQProcessRegistry(),
                              process_poll_interval=0.01)
    facade = _facade_module().ChatAnalyzerFacade(qq_auth_bridge=bridge)
    workspace = QQWorkspace(facade)
    workspace.connect_qq()
    deadline = time.monotonic() + 3
    while workspace._qq_guide_label.text() != "请完全退出 QQ" and time.monotonic() < deadline:
        qt_app.processEvents()
        QTest.qWait(5)
    assert workspace._qq_guide_label.text() == "请完全退出 QQ"
    assert detected_threads and main_thread not in detected_threads
    assert launches == []
    if cancel:
        workspace.cancel_connection()
        conflicts.clear()
        assert completed.wait(2)
        QTest.qWait(30)
        assert launches == []
        assert workspace._qq_connect_in_flight is False
    else:
        conflicts.clear()
        deadline = time.monotonic() + 3
        while not launches and time.monotonic() < deadline:
            qt_app.processEvents()
            QTest.qWait(5)
        assert len(launches) == 1
        assert launches[0] != main_thread
        workspace.cancel_connection()
    workspace._stop_qq_status_polling()


@pytest.mark.parametrize("event", ["progress", "delayed_success"])
def test_qq_cancel_rejects_old_callbacks_after_restart(qt_app, monkeypatch, event):
    from qq_chat_analyzer.gui import qq_workspace as module
    executor = _IndependentDeferredExecutor()
    workspace = module.QQWorkspace(StubFacade(), executor=executor)
    delayed = []
    monkeypatch.setattr(module.QTimer, "singleShot", lambda delay, callback: delayed.append(callback))
    workspace.connect_qq()
    first = executor.tasks[0]
    if event == "delayed_success":
        first.succeed(SimpleNamespace(state="waiting_auth", message="old"))
    workspace.cancel_connection()
    workspace.connect_qq()
    second = executor.tasks[-1]
    before = workspace._status_label.text()
    if event == "progress":
        first.progress("请完全退出 QQ")
    for callback in delayed:
        callback()
    assert workspace._status_label.text() == before
    assert workspace._connection_task is second
    assert workspace._qq_connect_in_flight


@pytest.mark.parametrize("poll", [False, True])
def test_qq_cancel_ignores_older_status_worker(qt_app, poll):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace
    executor = _IndependentDeferredExecutor()
    workspace = QQWorkspace(StubFacade(), executor=executor)
    if poll:
        workspace._start_qq_status_polling()
        workspace._poll_qq_status()
    else:
        workspace.refresh_qq_status()
    old_status = executor.tasks[0]
    workspace.connect_qq()
    workspace.cancel_connection()
    before = workspace._status_label.text()
    old_status.succeed(SimpleNamespace(state="waiting_auth", message="old status"))
    assert workspace._status_label.text() == before
    assert not workspace._qq_status_timer.isActive()


class _IndependentDeferredExecutor:
    """Keep each submitted callback set available for stale-event tests."""

    class Task:
        def __init__(
            self,
            operation,
            on_success,
            on_error,
            on_finished,
            on_progress,
        ):
            self.operation = operation
            self.on_success = on_success
            self.on_error = on_error
            self.on_finished = on_finished
            self.on_progress = on_progress
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def progress(self, message):
            if self.on_progress is not None:
                self.on_progress(message)

        def succeed(self, result):
            if self.on_success is not None:
                self.on_success(result)

        def fail(self, code, message):
            if self.on_error is not None:
                self.on_error(code, message)

        def finish(self):
            if self.on_finished is not None:
                self.on_finished()

    def __init__(self):
        self.tasks = []

    def __call__(
        self,
        operation,
        *,
        on_success,
        on_error,
        on_finished=None,
        on_progress=None,
    ):
        task = self.Task(
            operation,
            on_success,
            on_error,
            on_finished,
            on_progress,
        )
        self.tasks.append(task)
        return task


def test_wechat_cancel_queued_attempt_cannot_start_key_and_fresh_attempt_can(qt_app):
    class Facade(StubFacade):
        def cancel_wechat_connection(self, cancel_event):
            cancel_event.set()
        def acquire_wechat_db_key(self, progress=None, *, cancel_event=None):
            assert cancel_event is not None and not cancel_event.is_set()
            return super().acquire_wechat_db_key(progress)
    facade = Facade()
    executor = _IndependentDeferredExecutor()
    workspace = _wechat_guide_module().WeChatWorkspace(facade, executor=executor)
    workspace._start_wechat_connect(_wechat_connection_config())
    old = executor.tasks[0]
    workspace.cancel_connection()
    with pytest.raises(_facade_module().FacadeError) as error:
        old.operation(lambda _: None)
    assert error.value.code == "wechat_key_cancelled"
    assert facade.acquire_wechat_db_key_calls == []
    workspace._start_wechat_connect(_wechat_connection_config())
    executor.tasks[1].operation(lambda _: None)
    assert len(facade.acquire_wechat_db_key_calls) == 1


@pytest.mark.parametrize("request_kind", ["status", "sessions"])
@pytest.mark.parametrize("completion", ["success", "error"])
def test_wechat_old_account_callbacks_cannot_replace_a_reconnecting_workspace(qt_app, request_kind, completion):
    executor = _IndependentDeferredExecutor()
    facade = StubFacade()
    workspace = _wechat_guide_module().WeChatWorkspace(facade, executor=executor)
    old_sessions = [_session(_facade_module().ChatSource.WECHAT, "old-account", "Fictional old account", 10)]
    workspace._handle_sessions_loaded(old_sessions)
    if request_kind == "status":
        workspace.refresh_connection_status(load_sessions_on_ready=True)
    else:
        workspace._load_sessions()
    old = executor.tasks[-1]
    workspace.disconnect_wechat()
    disconnect = executor.tasks[-1]
    disconnect.succeed(_wechat_missing_status())
    disconnect.finish()
    workspace._start_wechat_connect(_wechat_connection_config())
    current = workspace._connection_task
    status = workspace._status_label.text()
    submitted = len(executor.tasks)
    if completion == "success":
        old.succeed(_wechat_connected_status() if request_kind == "status" else old_sessions)
    else:
        old.fail("query_failed", "Fictional old error")
    old.finish()
    assert workspace._status_label.text() == status
    assert workspace._connection_task is current
    assert len(executor.tasks) == submitted
    assert not workspace.session_panel._sessions_ready
    assert not workspace._wechat_connect_button.isHidden()


def test_wechat_disconnect_invalidates_the_connection_session_load_immediately(qt_app):
    executor = _IndependentDeferredExecutor()
    workspace = _wechat_guide_module().WeChatWorkspace(StubFacade(), executor=executor)
    workspace._load_sessions(attempt_generation=workspace._wechat_attempt_generation)
    old = executor.tasks[-1]
    workspace.disconnect_wechat()
    old.succeed([_session(_facade_module().ChatSource.WECHAT, "old", "Fictional old", 10)])
    assert not workspace.session_panel._sessions_ready
    assert not workspace._sessions_loaded
    assert "退出" in workspace._status_label.text()


@pytest.mark.parametrize("transition", ["connect", "cancel"])
def test_wechat_connection_transitions_discard_cached_sessions_and_ignore_filter_signals(qt_app, transition):
    workspace = _wechat_guide_module().WeChatWorkspace(StubFacade(), executor=_IndependentDeferredExecutor())
    workspace._handle_sessions_loaded([_session(_facade_module().ChatSource.WECHAT, "old", "Fictional old", 10)])
    if transition == "connect":
        workspace._start_wechat_connect(_wechat_connection_config())
    else:
        workspace.cancel_connection()
    panel = workspace.session_panel
    panel._session_search.setText("Fictional old")
    panel._session_sort.setCurrentIndex(panel._session_sort.findData("name"))
    assert panel._sessions_data == []
    assert not panel._sessions_ready and not workspace._sessions_loaded
    assert all("Fictional old" not in panel._session_list.item(i).text() for i in range(panel._session_list.count()))
    assert not panel._analyze_button.isEnabled()


def test_wechat_cancelled_queued_disconnect_cannot_clear_a_new_connection(qt_app):
    executor = _IndependentDeferredExecutor()
    facade = StubFacade()
    workspace = _wechat_guide_module().WeChatWorkspace(facade, executor=executor)
    workspace.disconnect_wechat()
    old = executor.tasks[-1]
    workspace.cancel_connection()
    workspace._start_wechat_connect(_wechat_connection_config())
    current = workspace._connection_task
    old.operation()
    assert facade.disconnect_wechat_calls == []
    old.succeed(_wechat_missing_status())
    old.finish()
    assert workspace._connection_task is current
    assert "准备微信连接" in workspace._status_label.text()


def test_wechat_navigation_during_connect_does_not_start_passive_old_account_loading(qt_app):
    executor = _IndependentDeferredExecutor()
    workspace = _wechat_guide_module().WeChatWorkspace(StubFacade(), executor=executor)
    workspace._start_wechat_connect(_wechat_connection_config())
    before = (workspace._status_label.text(), workspace._wechat_guide_label.text())
    workspace.select_source(_facade_module().ChatSource.WECHAT)
    workspace.refresh_connection_status(load_sessions_on_ready=True)
    assert len(executor.tasks) == 1
    assert (workspace._status_label.text(), workspace._wechat_guide_label.text()) == before


def test_begin_shutdown_signals_wechat_attempt_without_waiting_on_gui(qt_app):
    facade = StubFacade()
    executor = _IndependentDeferredExecutor()
    window = _main_window(qt_app, facade, executor=executor)
    window.wechat_workspace._start_wechat_connect(_wechat_connection_config())
    attempt = executor.tasks[-1]
    assert window.begin_shutdown()
    assert attempt.cancelled
    assert window.await_shutdown(2)
    assert not window.shutdown_protocol.result.can_apply_update


def _drain(page):
    """Deliver zero-timer GUI work used by the synchronous test executor."""
    QApplication.processEvents()


def _settle_workers(timeout_ms: int = 5000) -> None:
    """Wait for real thread-pool work and deliver queued GUI callbacks."""
    workers = importlib.import_module("qq_chat_analyzer.gui.workers")
    import shiboken6

    def relay_can_dispatch(relay) -> bool:
        if not shiboken6.isValid(relay):
            return False
        try:
            return any(
                isinstance(source, workers.WorkerSignals)
                and shiboken6.isValid(source)
                for source in relay.children()
            )
        except RuntimeError:
            return False

    deadline = time.monotonic() + timeout_ms / 1000
    pool = QThreadPool.globalInstance()
    while True:
        QApplication.processEvents()
        live_relays = tuple(
            relay
            for relay in workers._RELAYS
            if relay_can_dispatch(relay)
        )
        if (
            not workers._PENDING
            and not live_relays
            and pool.activeThreadCount() == 0
        ):
            return
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return
        pool.waitForDone(min(100, remaining_ms))
        QTest.qWait(min(20, remaining_ms))


# ------------------------------------------------------------ initialization


def test_main_window_close_owns_shutdown_without_waiting(qt_app, sources) -> None:
    """The window disappears at once, but Echo keeps shutdown ownership.

    ``closeEvent`` must not block the GUI thread, and the cleanup must not be
    left to interpreter finalization: the window exposes the very protocol the
    desktop entry point waits on, so the ordered shutdown (Direct DB plaintext
    cleanup, then QQ runtime termination) still completes.
    """
    module = importlib.import_module("qq_chat_analyzer.gui.shutdown")
    release = threading.Event()

    class _SlowShutdownFacade(StubFacade):
        def shutdown(self):
            self.shutdown_calls.append(1)
            release.wait(5.0)

    facade = _SlowShutdownFacade(sources=sources)
    window = _main_window(qt_app, facade)
    window.show()

    started = time.monotonic()
    window.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert window.isVisible() is False

    protocol = window.shutdown_protocol
    assert protocol.started is True
    assert protocol.finished is False
    assert module.THREAD_NAME in {
        thread.name for thread in threading.enumerate()
    }

    # The entry point still owns the outcome: it can wait for the protocol.
    release.set()
    assert window.await_shutdown(2.0) is True
    assert protocol.finished is True
    assert facade.shutdown_calls == [1]


def _wechat_available_sources():
    module = _facade_module()
    return (
        module.SourceInfo(
            source=module.ChatSource.QQ,
            display_name="QQ",
            available=True,
        ),
        module.SourceInfo(
            source=module.ChatSource.WECHAT,
            display_name="\u5fae\u4fe1",
            available=True,
        ),
    )




# ------------------------------------------------------------------ sessions


# ------------------------------------------------------------------ analysis


# -------------------------------------------------------------- Echo report


# -------------------------------------------------------------------- errors


# -------------------------------------------------------------------- layering


# ------------------------------------------------------------- read-only


def _qq_snapshot(state, message="", action_hint="", version=None):
    """Build one QQ ConnectionSnapshot in a given lifecycle state."""
    connection = importlib.import_module(
        "qq_chat_analyzer.application.connection_models"
    )
    return connection.ConnectionSnapshot(
        state=connection.ConnectionState(state),
        source="qq",
        message=message,
        action_hint=action_hint,
        version=version,
    )


def test_qq_cancelled_queued_operations_do_not_use_new_connection(qt_app):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace
    executor = _DeferredExecutor()
    facade = StubFacade()
    calls = []
    facade.list_sessions = lambda source: calls.append("sessions") or []
    workspace = QQWorkspace(facade, executor=executor)
    workspace._load_sessions()
    sessions = executor.operation
    workspace._invalidate_session_request()
    assert sessions() == [] and calls == []
    panel = workspace.session_panel
    identity = object()
    panel._analysis_operation = identity
    panel._set_busy(True)
    panel._submit_analysis(lambda report: calls.append("analysis"), identity)
    analysis = executor.operation
    panel.cancel_analysis()
    analysis(lambda message: None)
    assert calls == []


def test_qq_old_disconnect_callback_cannot_replace_new_status(qt_app):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace
    executor = _IndependentDeferredExecutor()
    workspace = QQWorkspace(StubFacade(), executor=executor)
    workspace.disconnect_qq()
    old = executor.tasks[-1]
    workspace.connect_qq()
    workspace._qq_connect_in_flight = False
    workspace._show_qq_status(_qq_snapshot("connected", message="New connection"), False)
    old.succeed(_qq_snapshot("disconnected", message="Old disconnect"))
    assert "New connection" in workspace._status_label.text()


def test_qq_old_queued_disconnect_cannot_stop_new_connection(qt_app):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace
    executor = _DeferredExecutor()
    facade = StubFacade()
    workspace = QQWorkspace(facade, executor=executor)
    workspace.disconnect_qq()
    old_operation = executor.operation
    workspace.connect_qq()
    old_operation()
    assert facade.disconnect_qq_calls == []


@pytest.mark.parametrize("old_failure_first", [True, False])
def test_qq_session_loading_ignores_old_failure_across_source_reset(qt_app, old_failure_first):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

    executor = _DeferredExecutor()
    workspace = QQWorkspace(StubFacade(), executor=executor)
    errors = []
    workspace.analysis_failed.connect(lambda *args: errors.append(args))
    workspace._load_sessions()
    old_failure = executor.on_error
    workspace.select_source(_facade_module().ChatSource.QQ)
    workspace._load_sessions()
    sessions = [_session(_facade_module().ChatSource.QQ, "fictional", "Fictional group")]
    if old_failure_first:
        old_failure("qq_direct_snapshot_acquire_failed", "Fictional failure")
    executor.succeed(sessions)
    if not old_failure_first:
        old_failure("qq_direct_snapshot_acquire_failed", "Fictional failure")
    assert workspace._sessions_loaded
    assert workspace.session_panel._sessions_ready
    assert errors == []


def test_qq_session_loading_ignores_old_success_during_new_load(qt_app):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

    executor = _DeferredExecutor()
    workspace = QQWorkspace(StubFacade(), executor=executor)
    workspace._load_sessions()
    old_success = executor.on_success
    workspace.select_source(_facade_module().ChatSource.QQ)
    workspace._load_sessions()
    old_success([])
    assert not workspace._sessions_loaded
    assert not workspace.session_panel._sessions_ready


def test_qq_session_loading_timing_logs_contain_only_stage_and_durations(qt_app, caplog):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

    executor = _DeferredExecutor()
    workspace = QQWorkspace(StubFacade(), executor=executor)
    with caplog.at_level("INFO", logger="qq_chat_analyzer.desktop.qq_workspace"):
        workspace._load_sessions()
        executor.succeed([
            _session(_facade_module().ChatSource.QQ, "fictional-private-id", "Fictional private name")
        ])
    messages = [record.getMessage() for record in caplog.records if "[qq sessions]" in record.getMessage()]
    assert "stage=gui_request outcome=started" in messages[0]
    assert any("stage=gui_render outcome=success" in message for message in messages)
    assert "stage=gui_request outcome=success" in messages[-1]
    assert all("elapsed_ms=" in message for message in messages)
    assert "fictional-private-id" not in " ".join(messages)
    assert "Fictional private name" not in " ".join(messages)


@pytest.mark.parametrize("callback", ["success", "failure", "render_failure"])
@pytest.mark.parametrize("stale", [False, True])
def test_qq_session_callback_terminal_diagnostics(qt_app, caplog, monkeypatch, callback, stale):
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace
    executor = _DeferredExecutor()
    workspace = QQWorkspace(StubFacade(), executor=executor)
    with caplog.at_level("INFO", logger="qq_chat_analyzer.desktop.qq_workspace"):
        workspace._load_sessions()
        if stale:
            workspace._invalidate_session_request()
        if callback == "render_failure":
            def broken(*args):
                raise ValueError("PRIVATE_RENDER")
            monkeypatch.setattr(workspace, "_handle_sessions_loaded", broken)
        if callback == "failure":
            executor.on_error("PRIVATE_CODE", "PRIVATE_MESSAGE")
        elif callback == "render_failure" and not stale:
            with pytest.raises(ValueError, match="PRIVATE_RENDER"):
                executor.succeed([])
        else:
            executor.succeed([])
    log = "\n".join(r.getMessage() for r in caplog.records if "[qq sessions]" in r.getMessage())
    expected = ("discarded_failure" if callback == "failure" else "discarded_success") if stale else (
        "success" if callback == "success" else "failed")
    assert f"stage=gui_request outcome={expected}" in log
    assert "PRIVATE" not in log


class _SnapshotFacade(StubFacade):
    """Return one fixed QQ snapshot regardless of the underlying status."""

    def __init__(self, snapshot, **kwargs):
        super().__init__(**kwargs)
        self._snapshot = snapshot

    def set_snapshot(self, snapshot):
        self._snapshot = snapshot

    def _qq_snapshot(self):
        return self._snapshot


class _ConnectWaitingAuthFacade(_SnapshotFacade):
    """Return a fixed status snapshot but a WAITING_AUTH connect result."""

    def __init__(self, status_snapshot, connect_snapshot, **kwargs):
        super().__init__(status_snapshot, **kwargs)
        self._connect_snapshot = connect_snapshot

    def start_qq_auth_flow(self, progress=None, *, cancel_event=None):
        self.start_qq_auth_flow_calls.append(1)
        return self._connect_snapshot


class _GatedQRFacade(_ConnectWaitingAuthFacade):
    """Simulate a fresh auth session that gates the QR file until it changes."""

    def __init__(self, status_snapshot, connect_snapshot, **kwargs):
        super().__init__(status_snapshot, connect_snapshot, **kwargs)
        self.qr_ready = False
        self.qr_path = None

    def get_qq_qrcode_path(self):
        return self.qr_path if self.qr_ready else None


def _write_qrcode_png(path: Path) -> None:
    from PySide6.QtGui import QPixmap

    pixmap = QPixmap(8, 8)
    pixmap.fill(Qt.GlobalColor.black)
    assert pixmap.save(str(path)) is True


@pytest.mark.parametrize("runtime_mode", ["managed", "custom"])
def test_qq_gui_reads_fresh_qr_from_auth_workspace(qt_app, tmp_path, runtime_mode, monkeypatch):
    """Display the bridge's QR, never an installation or previous session QR."""
    from PySide6.QtGui import QPixmap
    from qq_chat_analyzer.application.facade import ChatAnalyzerFacade
    from qq_chat_analyzer.application.qq.qq_auth_bridge import QQAuthBridge
    from qq_chat_analyzer.application.qq import qq_auth_bridge
    monkeypatch.setattr(qq_auth_bridge, "find_conflicting_qq_pids", lambda owned: [])
    from qq_chat_analyzer.application.qq.qq_environment_config import QQEnvironmentConfig
    from qq_chat_analyzer.application.qq.qq_runtime_paths import (
        qq_runtime_paths, resolve_runtime_paths,
    )
    from qq_chat_analyzer.application.qq.qq_setup_service import QQSetupService
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

    program = tmp_path / "fictional-program"
    config = QQEnvironmentConfig(runtime_directory=program, runtime_mode=runtime_mode)
    paths = (qq_runtime_paths(component_id="a" * 64, program_root=program,
                              data_root=tmp_path / "user")
             if runtime_mode == "managed" else resolve_runtime_paths(config))
    qr = (tmp_path / "user/runtime/qq" / ("a" * 64) / "cache/qrcode.png"
          if runtime_mode == "managed" else program / "cache/qrcode.png")
    qr.parent.mkdir(parents=True)
    stale = QPixmap(8, 8)
    stale.fill(Qt.GlobalColor.white)
    assert stale.save(str(qr))
    setup = QQSetupService(config_loader=SimpleNamespace(
        load_or_default=lambda: config, runtime_paths=lambda **_: paths,
    ))
    manager = SimpleNamespace(
        get_snapshot=lambda: _qq_snapshot("disconnected"),
        begin_auth_waiting=lambda: None, end_auth_waiting=lambda: None,
        disconnect=lambda: _qq_snapshot("disconnected"),
    )
    bridge = QQAuthBridge(setup_service=setup, manager=manager,
                          window_launcher=lambda: None, runtime_cleaner=lambda _: None)
    facade = ChatAnalyzerFacade(qq_auth_bridge=bridge)
    workspace = QQWorkspace(facade, executor=_inline_executor())

    workspace._refresh_qq_qrcode()
    assert workspace._qq_qrcode_label.isHidden()
    facade.start_qq_auth_flow()
    workspace._show_qq_status(_qq_snapshot("waiting_auth"), False)
    assert workspace._qq_qrcode_label.isHidden()
    assert workspace._qq_guide_label.text() == "等待二维码出现，请不要打开 QQ。"
    assert "扫码" not in workspace._qq_login_guide_label.text()
    _write_qrcode_png(qr)
    workspace._show_qq_status(_qq_snapshot("waiting_auth"), False)
    assert workspace._qq_qrcode_label.isVisibleTo(workspace)
    assert workspace._qq_guide_label.text() == "请使用手机 QQ 扫码登录"
    assert facade.get_qq_qrcode_path() == qr
    assert bridge.get_qrcode_path() == qr

    # A new GUI instance must not reuse an old runtime's otherwise fresh QR.
    other = ChatAnalyzerFacade(qq_auth_bridge=QQAuthBridge(setup_service=setup))
    assert other.get_qq_qrcode_path() is None
    qr.write_bytes(b"fictional invalid PNG")
    workspace._refresh_qq_qrcode()
    assert workspace._qq_qrcode_label.isHidden()
    qr.unlink()
    workspace._refresh_qq_qrcode()
    assert workspace._qq_qrcode_label.isHidden()
    assert facade.get_qq_qrcode_path() is None

    _write_qrcode_png(qr)
    facade.disconnect_qq()
    assert facade.get_qq_qrcode_path() is None
    # Reconnection resolves the current workspace rather than a GUI-cached path.
    if runtime_mode == "managed":
        paths = qq_runtime_paths(component_id="b" * 64, program_root=program,
                                 data_root=tmp_path / "user")
        next_qr = tmp_path / "user/runtime/qq" / ("b" * 64) / "cache/qrcode.png"
    else:
        config = dataclasses.replace(config, runtime_directory=tmp_path / "custom-second")
        paths = resolve_runtime_paths(config)
        next_qr = tmp_path / "custom-second/cache/qrcode.png"
    facade.start_qq_auth_flow()
    workspace._refresh_qq_qrcode()
    assert workspace._qq_qrcode_label.isHidden()
    next_qr.parent.mkdir(parents=True)
    _write_qrcode_png(next_qr)
    workspace._refresh_qq_qrcode()
    assert workspace._qq_qrcode_label.isVisibleTo(workspace)
    assert facade.get_qq_qrcode_path() == next_qr


# ---------------------------------------------------------------- GUI-2: Home + Navigation

    # home_page is the current (top) page in the stack


    # local_data_page is the current (top) page in the stack


# ----------------------------------------------------------------

# ----------------------------------------------------------------
# GUI-3 stabilization: restore GUI-2 workspace behavior equivalence
# ----------------------------------------------------------------


# ---------------------------------------------------------------------------
# QQ connection journey in the shared Echo visual language
# ---------------------------------------------------------------------------


@pytest.fixture
def custom_date_panel(qt_app, sources):
    """Real widgets with fictional sessions and controllable facade callbacks."""
    from qq_chat_analyzer.gui.session_analysis_panel import SessionAnalysisPanel

    panels = []

    def make(source_name="QQ", executor=None):
        module = _facade_module()
        source = getattr(module.ChatSource, source_name)
        panel = SessionAnalysisPanel()
        panel.configure(StubFacade(sources=sources), source,
                        executor=executor or _inline_executor())
        panel.populate_sessions([
            _session(source, "fiction-A", "虚构会话 A", 10),
            _session(source, "fiction-B", "虚构会话 B", 5),
        ])
        panel.resize(780, 620)
        panel.show()
        panel._session_list.setCurrentRow(0)
        panel._scope_custom.setChecked(True)
        qt_app.processEvents()
        panels.append(panel)
        return panel

    yield make
    for panel in panels:
        panel.cancel_analysis()
        panel.close()
        panel.deleteLater()
    qt_app.processEvents()


def test_session_panel_date_wechat_stale_requests_cannot_pollute_reselected_session(custom_date_panel):
    executor = _IndependentDeferredExecutor()
    panel = custom_date_panel("WECHAT", executor)
    # A -> B -> A needs request identity as well as session identity.
    panel._start_date.setDate(QDate(2023, 5, 6))
    panel._session_list.setCurrentRow(1)
    assert panel._start_date.date() == QDate.currentDate()
    panel._session_list.setCurrentRow(0)
    executor.tasks[2].succeed((1704067200, 1704153600))
    expected = panel.build_config()
    executor.tasks[0].succeed((1609459200, 1609545600))
    executor.tasks[1].succeed((1640995200, 1641081600))
    assert panel.build_config() == expected
    assert panel._start_date.date().toPython() == datetime.fromtimestamp(1704067200).date()
    assert panel._end_date.date().toPython() == datetime.fromtimestamp(1704153600).date()


def test_session_panel_keeps_session_ids_out_of_display_text(qt_app) -> None:
    """Session IDs drive selection but never appear in the displayed text."""
    from qq_chat_analyzer.gui.session_analysis_panel import (
        SESSION_ID_ROLE,
        SessionAnalysisPanel,
    )
    module = _facade_module()
    panel = SessionAnalysisPanel()
    panel.configure(
        StubFacade(sources=_wechat_available_sources()),
        module.ChatSource.WECHAT,
    )
    panel.populate_sessions(
        [
            _session(
                module.ChatSource.WECHAT,
                "secret-id-10001",
                "\u865a\u6784\u7fa4 A",
            )
        ]
    )

    item = panel._session_list.item(0)

    assert item.text() == "\u865a\u6784\u7fa4 A"
    assert "secret-id-10001" not in item.text()
    assert item.data(SESSION_ID_ROLE) == "secret-id-10001"


def test_qq_workspace_full_chain_connect_sessions_analyze(
    qt_app, sources, tmp_path, monkeypatch
) -> None:
    """QQ workspace keeps the GUI-2 connect -> sessions -> analyze chain."""
    from qq_chat_analyzer.gui.main_window import (
        PROCESSING_PAGE_INDEX,
        QQ_WORKSPACE_INDEX,
    )
    module = _facade_module()
    qq_module = importlib.import_module("qq_chat_analyzer.gui.qq_workspace")
    monkeypatch.setattr(qq_module, "_QQ_CONNECT_MIN_DISPLAY_MS", 0)
    disconnected = module.QQConnectionStatus(
        available=False, runtime_running=False, qq_online=False,
        version="", message="QQ 尚未连接。", action_hint="",
    )
    connected = module.QQConnectionStatus(
        available=True, runtime_running=True, qq_online=True,
        version="4.1.0", message="已连接", action_hint="",
    )
    report_path = tmp_path / "echo-qq.html"
    report_path.write_text("<html>fictional report</html>", encoding="utf-8")
    opened: list[Path] = []
    facade = StubFacade(
        sources=sources,
        sessions=[_session(module.ChatSource.QQ, "q1", "QQ群1", 100)],
        outcome=_StubOutcome(report_path=report_path),
        connection_status=disconnected,
        connection_status_after_connect=connected,
    )
    executor = _DeferredExecutor()
    window = _main_window(qt_app, facade, executor=executor)
    window._report_opener = lambda path: opened.append(path) or True
    window.navigate_to_qq()
    _drain(window)

    # status -> disconnected
    assert executor.operation is not None
    executor.operation()
    executor.succeed(facade.get_qq_connection_snapshot())
    assert window.qq_workspace._qq_connect_button.isVisibleTo(window) is True
    assert window.qq_workspace.session_panel._sessions_ready is False

    # connect/auth operation with progress
    window.qq_workspace._qq_connect_button.click()
    executor.on_progress("等待QQ登录")
    assert window.qq_workspace._qq_login_guide_label.isVisibleTo(window) is True
    executor.operation(lambda _message: None)
    assert facade.start_qq_auth_flow_calls
    executor.succeed(facade.get_qq_connection_snapshot())
    _drain(window)

    # connected -> list_sessions(QQ)
    executor.operation()
    executor.succeed(facade._sessions)
    assert facade.list_sessions_calls == [module.ChatSource.QQ]
    assert window.qq_workspace.session_panel._sessions_ready is True
    assert window.qq_workspace.session_panel._session_list.count() == 1

    # select session + custom scope -> analyze_session -> processing -> workspace + Echo report
    window.qq_workspace.session_panel._session_list.setCurrentRow(0)
    window.qq_workspace.session_panel._scope_custom.setChecked(True)
    window.qq_workspace.session_panel._start_date.setDate(QDate(2024, 1, 1))
    window.qq_workspace.session_panel._end_date.setDate(QDate(2024, 12, 31))
    window.qq_workspace.session_panel.start_analysis()
    _drain(window.qq_workspace.session_panel)
    assert window.stack.currentIndex() == PROCESSING_PAGE_INDEX

    executor.operation(lambda _message: None)
    assert facade.analyze_session_calls
    source, session_id, config = facade.analyze_session_calls[0]
    assert source == module.ChatSource.QQ
    assert session_id == "q1"
    assert config.scope_mode is module.AnalysisScopeMode.CUSTOM
    assert config.start_time == "2024-01-01"
    assert config.end_time == "2024-12-31"
    executor.succeed(_StubOutcome(report_path=report_path))
    assert window.stack.currentIndex() == QQ_WORKSPACE_INDEX
    assert opened == [report_path.resolve()]
    assert window._open_echo_button.isVisibleTo(window)


def test_qq_status_snapshot_error_shows_only_the_fixed_error_code(qt_app) -> None:
    """A failed passive status probe keeps the stage it actually failed in."""
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

    workspace = QQWorkspace(StubFacade())

    workspace._handle_connection_status_error(
        "qq_direct_database_unavailable", "fictional status detail",
    )
    assert workspace._status_label.text().endswith(
        "（错误代码：qq_direct_database_unavailable）"
    )

    workspace._handle_connection_status_error("fictional private code", "detail")
    assert "错误代码" not in workspace._status_label.text()


def test_qq_error_snapshot_shows_the_reviewed_code(qt_app) -> None:
    """An error snapshot returned (not raised) still names its fixed stage."""
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

    workspace = QQWorkspace(StubFacade())
    workspace._show_qq_status(
        SimpleNamespace(
            state="error", message="无法启动 QQ 登录授权，请稍后重试。",
            action_hint="请稍后重试。", code="qq_runtime_identity_mismatch",
        ),
        False,
    )
    assert workspace._status_label.text().endswith(
        "（错误代码：qq_runtime_identity_mismatch）"
    )

    workspace._show_qq_status(
        SimpleNamespace(
            state="disconnected", message="QQ 尚未连接。",
            action_hint="", code="qq_direct_snapshot_invalid",
        ),
        False,
    )
    assert "错误代码" not in workspace._status_label.text()


def test_qq_connect_failure_shows_only_the_fixed_error_code(qt_app) -> None:
    """The failing stage stays reportable even when no log file was created."""
    from qq_chat_analyzer.gui.qq_workspace import QQWorkspace

    workspace = QQWorkspace(StubFacade())

    workspace._handle_qq_connect_error(
        "qq_runtime_identity_mismatch", "无法启动 QQ 登录授权，请稍后重试。",
    )

    assert workspace._status_label.text().endswith(
        "（错误代码：qq_runtime_identity_mismatch）"
    )
    assert workspace._status_label.toolTip() == "无法启动 QQ 登录授权，请稍后重试。"

    # Arbitrary text is never promoted to the visible code slot.
    workspace._handle_qq_connect_error(
        "private C:\\Users\\Fictional\\QQ path", "fictional message",
    )
    assert "C:\\Users" not in workspace._status_label.text()
    assert "错误代码" not in workspace._status_label.text()


def test_wechat_workspace_full_chain_connect_sessions_analyze(
    qt_app, sources, tmp_path
) -> None:
    """WeChat workspace keeps the GUI-2 connect -> sessions -> analyze chain."""
    from qq_chat_analyzer.gui.main_window import (
        PROCESSING_PAGE_INDEX,
        WECHAT_WORKSPACE_INDEX,
    )
    module = _facade_module()
    disconnected = module.WeChatConnectionStatus(
        available=False, data_found=True, db_key_available=False,
        runtime_available=True, message="等待微信登录", action_hint="",
    )
    connected = module.WeChatConnectionStatus(
        available=True, data_found=True, db_key_available=True,
        runtime_available=True, message="微信已连接", action_hint="",
    )
    report_path = tmp_path / "echo-wechat.html"
    report_path.write_text("<html>fictional report</html>", encoding="utf-8")
    opened: list[Path] = []
    facade = StubFacade(
        sources=sources,
        sessions=[_session(module.ChatSource.WECHAT, "wx1", "测试会话1", 10)],
        outcome=_StubOutcome(report_path=report_path),
        connection_status=disconnected,
        connection_status_after_connect=connected,
        data_roots=["D:/fictional_wechat"],
    )
    executor = _DeferredExecutor()
    window = _main_window(qt_app, facade, executor=executor)
    window._report_opener = lambda path: opened.append(path) or True
    window.navigate_to_wechat()
    _drain(window)

    # status -> disconnected
    executor.operation()
    executor.succeed(disconnected)
    assert window.wechat_workspace._wechat_connect_button.isVisibleTo(window) is True
    assert window.wechat_workspace.session_panel._sessions_ready is False

    # one-click connect: detect root -> save environment + key -> connected
    window.wechat_workspace._wechat_connect_button.click()
    assert facade.detect_wechat_data_roots_calls
    executor.on_progress(module.WeChatConnectionProgress.READY_FOR_LOGIN)
    assert "现在可以登录微信" in window.wechat_workspace._status_label.text()
    executor.operation(lambda _message: None)
    assert facade.setup_wechat_environment_calls
    assert facade.acquire_wechat_db_key_calls
    executor.succeed(connected)
    _drain(window)

    # connected -> list_sessions(WECHAT)
    executor.operation()
    executor.succeed(facade._sessions)
    assert facade.list_sessions_calls == [module.ChatSource.WECHAT]
    assert window.wechat_workspace.session_panel._sessions_ready is True
    assert window.wechat_workspace.session_panel._session_list.count() == 1

    # select session + analyze -> processing -> workspace + Echo report
    window.wechat_workspace.session_panel._session_list.setCurrentRow(0)
    window.wechat_workspace.session_panel.start_analysis()
    _drain(window.wechat_workspace.session_panel)
    assert window.stack.currentIndex() == PROCESSING_PAGE_INDEX

    executor.operation(lambda _message: None)
    assert facade.analyze_session_calls
    source, session_id, config = facade.analyze_session_calls[0]
    assert source == module.ChatSource.WECHAT
    assert session_id == "wx1"
    assert config.scope_mode is module.AnalysisScopeMode.ALL
    executor.succeed(_StubOutcome(report_path=report_path))
    assert window.stack.currentIndex() == WECHAT_WORKSPACE_INDEX
    assert opened == [report_path.resolve()]
    assert window._open_echo_button.isVisibleTo(window)


# ----------------------------------------------------------------
# GUI-5: WeChat data path auto-detection on workspace entry
# ----------------------------------------------------------------

def _window_with_running_fictional_analysis(qt_app, source):
    """Keep a loaded workspace and hold its analysis until cancellation."""
    module = _facade_module()
    facade = StubFacade(
        sessions=[_session(source, "fiction-A", "Fiction A", 2),
                  _session(source, "fiction-B", "Fiction B", 3)],
        connection_status=SimpleNamespace(
            available=True, runtime_running=True, qq_online=True,
            message="Fiction ready", action_hint="", version=None,
        ),
    )
    executor = _IndependentDeferredExecutor()
    window = _main_window(qt_app, facade, executor=executor)
    workspace = window.qq_workspace if source is module.ChatSource.QQ else window.wechat_workspace
    workspace.select_source(module.SourceInfo(source=source, display_name="Fiction", available=True))
    workspace._handle_sessions_loaded(facade._sessions)
    window._active_source = source.value
    panel = workspace.session_panel
    panel._session_list.setCurrentRow(1)
    panel._scope_custom.setChecked(True)
    panel._start_date.setDate(QDate(2020, 1, 2))
    panel._end_date.setDate(QDate(2021, 3, 4))
    panel.start_analysis()
    _drain(panel)
    return window, facade, executor, panel


def test_wechat_analysis_forwards_cancel_signal(qt_app):
    """WeChat media recovery must observe the same cancel event as the panel."""
    source = _facade_module().ChatSource.WECHAT
    window, facade, executor, panel = _window_with_running_fictional_analysis(qt_app, source)
    window.cancel_analysis()
    captured: dict[str, object] = {}

    def analyze_session(source, session_id, config=None, progress=None, on_phase=None,
                        cancelled=lambda: False):
        captured["cancelled"] = cancelled
        return _StubOutcome()

    facade.analyze_session = analyze_session
    panel.start_analysis()
    _drain(panel)
    event = panel._analysis_cancel
    executor.tasks[-1].operation(None)

    assert captured.get("cancelled") is not None
    event.set()
    assert captured["cancelled"]() is True



@pytest.mark.parametrize("source_name", ["qq", "wechat"])
def test_processing_cancel_returns_while_real_worker_is_still_blocked(qt_app, source_name):
    from qq_chat_analyzer.application.facade import AnalysisPhase
    from qq_chat_analyzer.gui.main_window import QQ_WORKSPACE_INDEX, WECHAT_WORKSPACE_INDEX
    from qq_chat_analyzer.gui.workers import submit
    window, facade, _, panel = _window_with_running_fictional_analysis(qt_app, _facade_module().ChatSource(source_name))
    window.cancel_analysis()
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    def analyze_session(source, session_id, config=None, progress=None, on_phase=None, cancelled=lambda: False):
        on_phase(AnalysisPhase.READING)
        entered.set()
        release.wait(5)
        returned.set()
        on_phase(AnalysisPhase.ANALYZING_REPORT)
        return _StubOutcome()
    facade.analyze_session = analyze_session
    panel._executor = submit
    opened = []
    window._report_opener = lambda path: opened.append(path)
    panel.start_analysis()
    _drain(window)
    try:
        assert entered.wait(2)
        window.cancel_analysis()
        assert not returned.is_set()  # cancellation does not kill blocking work
        assert not panel._analysis_running
        assert not window.processing_page.animation._timer.isActive()
        assert window.stack.currentIndex() == (QQ_WORKSPACE_INDEX if source_name == "qq" else WECHAT_WORKSPACE_INDEX)
        assert panel.selected_session_id() == "fiction-B"
        assert panel.build_config().start_time == "2020-01-02"
        assert panel.build_config().end_time == "2021-03-04"
    finally:
        release.set()
        _settle_workers()
    assert returned.is_set()
    assert opened == []


@pytest.mark.parametrize("source_name", ["qq", "wechat"])
def test_processing_cancel_retry_rejects_old_phase_and_terminal_callbacks(qt_app, source_name, tmp_path, monkeypatch):
    from qq_chat_analyzer.application.analysis_phase import AnalysisPhase
    monkeypatch.setattr(QMessageBox, "warning", lambda *_: pytest.fail("stale error displayed"))
    source = _facade_module().ChatSource(source_name)
    window, _, executor, panel = _window_with_running_fictional_analysis(qt_app, source)
    opened = []
    window._report_opener = lambda path: opened.append(path) or True
    old = executor.tasks[-1]
    window.cancel_analysis()
    assert not window.processing_page.animation.model.active
    assert not window.processing_page.animation._timer.isActive()
    panel.start_analysis()
    _drain(panel)
    current = executor.tasks[-1]
    old.progress(AnalysisPhase.ANALYZING_REPORT)
    old.progress("旧任务进度")
    report = tmp_path / "fictional-report.html"
    report.write_text("<html>Fiction</html>", encoding="utf-8")
    old.succeed(_StubOutcome(report_path=report))
    old.fail("fiction", "旧任务错误")
    old.finish()
    assert window.processing_page.phase is AnalysisPhase.READING
    assert panel._analysis_task is current
    assert panel._analysis_running
    assert opened == []


def test_processing_close_invalidates_task_before_shutdown(qt_app, monkeypatch):
    source = _facade_module().ChatSource.QQ
    window, facade, executor, _ = _window_with_running_fictional_analysis(qt_app, source)
    monkeypatch.setattr("qq_chat_analyzer.gui.main_window._quit_application", lambda: None)
    called = []
    window._report_opener = lambda path: called.append(path)
    task = executor.tasks[-1]
    window.close()
    assert not window.processing_page.animation.model.active
    assert not window.processing_page.animation._timer.isActive()
    assert task.cancelled
    task.succeed(_StubOutcome())
    task.finish()
    assert called == []
    assert window._shutdown_protocol.wait(1)
    assert facade.shutdown_calls == [1]


@pytest.mark.parametrize("source_name", ["qq", "wechat"])
def test_processing_real_worker_delivers_typed_phases_on_gui_thread(qt_app, source_name):
    from qq_chat_analyzer.application.analysis_phase import AnalysisPhase
    from qq_chat_analyzer.gui.workers import submit
    source = _facade_module().ChatSource(source_name)
    window, facade, _, panel = _window_with_running_fictional_analysis(qt_app, source)
    window.cancel_analysis()
    gui_thread = threading.get_ident()
    published_from = []
    received = []
    def analyze_session(source, session_id, config=None, progress=None, on_phase=None, cancelled=lambda: False):
        published_from.append(threading.get_ident())
        on_phase(AnalysisPhase.READING)
        progress("旧字符串仍可传递")
        on_phase(AnalysisPhase.ANALYZING_REPORT)
        return _StubOutcome()
    facade.analyze_session = analyze_session
    panel._executor = submit
    panel.analysis_phase_changed.connect(lambda phase: received.append((phase, threading.get_ident())))
    panel.start_analysis()
    _drain(window)
    _settle_workers()
    assert published_from and published_from[0] != gui_thread
    assert received == [(AnalysisPhase.READING, gui_thread), (AnalysisPhase.ANALYZING_REPORT, gui_thread)]
    assert not panel._analysis_running
    assert not window.processing_page.animation._timer.isActive()


# ----------------------------------------------------------------
# GUI-5b: WeChat key/database verification fallback
# ----------------------------------------------------------------


def _wechat_unreadable_error():
    module = _facade_module()
    return module.FacadeError(
        code="wechat_database_unreadable",
        public_message=(
            "\u5f53\u524d\u5fae\u4fe1\u767b\u5f55\u4fe1\u606f\u65e0\u6cd5"
            "\u8bfb\u53d6\u6240\u9009\u6570\u636e\u5e93\uff0c\u8bf7\u786e\u8ba4"
            "\u6570\u636e\u4f4d\u7f6e\u662f\u5426\u5bf9\u5e94\u5f53\u524d"
            "\u767b\u5f55\u8d26\u53f7\uff0c\u6216\u91cd\u65b0\u83b7\u53d6"
            "\u5fae\u4fe1\u8fde\u63a5\u4fe1\u606f\u3002"
        ),
        source=module.ChatSource.WECHAT,
    )


def _wechat_connected_status():
    module = _facade_module()
    return module.WeChatConnectionStatus(
        available=True, data_found=True, db_key_available=True,
        runtime_available=True, message="\u5fae\u4fe1\u5df2\u8fde\u63a5",
        action_hint="",
    )


def _wechat_missing_status():
    module = _facade_module()
    return module.WeChatConnectionStatus(
        available=False, data_found=False, db_key_available=False,
        runtime_available=False,
        message="\u672a\u627e\u5230\u5fae\u4fe1\u6570\u636e\u4f4d\u7f6e\u3002",
        action_hint="",
    )


def _wechat_mismatch_facade(
    sources,
    *,
    verify_error,
    data_roots=("D:/fictional_wechat",),
):
    module = _facade_module()
    return StubFacade(
        sources=sources,
        connection_status=_wechat_missing_status(),
        connection_status_after_connect=_wechat_connected_status(),
        sessions=[
            _session(module.ChatSource.WECHAT, "wx1", "\u6d4b\u8bd5\u4f1a\u8bdd1", 10)
        ],
        data_roots=list(data_roots),
        verify_error=verify_error,
    )


def _run_wechat_verification(executor):
    """Run the queued verification, which raises for a mismatched database."""
    with pytest.raises(_facade_module().FacadeError):
        executor.operation()


def _drive_wechat_database_mismatch(
    qt_app,
    sources,
    *,
    data_roots=("D:/fictional_wechat",),
):
    """Run the one-click flow until the captured key fails verification."""
    facade = _wechat_mismatch_facade(
        sources,
        verify_error=_wechat_unreadable_error(),
        data_roots=data_roots,
    )
    executor = _DeferredExecutor()
    window = _main_window(qt_app, facade, executor=executor)
    window.navigate_to_wechat()
    _drain(window)

    executor.operation()
    executor.succeed(_wechat_missing_status())
    _drain(window)

    workspace = window.wechat_workspace
    workspace._wechat_connect_button.click()
    if len(data_roots) > 1:
        # Several candidates: the user picks one first, so the connect runs
        # from the setup dialog instead of the one-click auto path.
        workspace._wechat_setup_dialog.set_data_root(data_roots[0])
        workspace._save_wechat_environment_from_dialog()
        _drain(window)

    executor.operation(lambda _message: None)
    executor.succeed(_wechat_connected_status())
    _drain(window)

    _run_wechat_verification(executor)
    executor.fail(
        "wechat_database_unreadable",
        facade._verify_error.public_message,
    )
    _drain(window)
    return window, facade, executor


def test_wechat_workspace_verifies_the_database_before_listing_sessions(
    qt_app, sources
) -> None:
    """A: one auto-detected root keeps the existing connect chain."""
    module = _facade_module()
    connected = _wechat_connected_status()
    facade = _wechat_mismatch_facade(sources, verify_error=None)
    order: list[str] = []
    verify = facade.verify_wechat_database
    list_sessions = facade.list_sessions

    def verify_spy():
        order.append("verify")
        return verify()

    def list_sessions_spy(source):
        order.append("list_sessions")
        return list_sessions(source)

    facade.verify_wechat_database = verify_spy
    facade.list_sessions = list_sessions_spy

    executor = _DeferredExecutor()
    window = _main_window(qt_app, facade, executor=executor)
    window.navigate_to_wechat()
    _drain(window)

    executor.operation()
    executor.succeed(_wechat_missing_status())
    _drain(window)

    assert facade.detect_wechat_data_roots_calls == []
    window.wechat_workspace._wechat_connect_button.click()
    assert facade.detect_wechat_data_roots_calls
    executor.operation(lambda _message: None)
    assert facade.setup_wechat_environment_calls
    assert facade.acquire_wechat_db_key_calls
    executor.succeed(connected)
    _drain(window)

    executor.operation()
    executor.succeed(facade._sessions)
    _drain(window)

    assert order[:2] == ["verify", "list_sessions"]
    assert facade.verify_wechat_database_calls
    assert facade.list_sessions_calls == [module.ChatSource.WECHAT]
    assert window.wechat_workspace.session_panel._sessions_ready is True
    assert window.wechat_workspace.session_panel._session_list.item(0).text() == "测试会话1"


def test_wechat_workspace_mismatch_message_exposes_no_internal_details(
    qt_app, sources
) -> None:
    """F: the recovery text stays actionable without internal details."""
    window, facade, executor = _drive_wechat_database_mismatch(qt_app, sources)
    workspace = window.wechat_workspace
    status_label = workspace._status_label

    visible = status_label.text() + "\n" + status_label.toolTip()
    assert "\u5f53\u524d\u5fae\u4fe1\u767b\u5f55\u4fe1\u606f" in visible
    assert "a" * 64 not in visible
    assert "session.db" not in visible
    assert "SELECT" not in visible
    assert ".db_storage" not in visible
    assert "D:/fictional_wechat" not in visible
    assert "wcdb" not in visible.lower()

# ---------------------------------------------------------------
# GUI-6: Local Data page
# ---------------------------------------------------------------

def _gui_report_summary(package_name, source="wechat", session_name="测试会话",
                        analysis_scope="all", scope_start=None, scope_end=None):
    from qq_chat_analyzer.application.report_package_catalog import ReportPackageSummary
    from qq_chat_analyzer.application.scope_filter import AnalysisScope, AnalysisScopeMode
    return ReportPackageSummary(package_name, datetime(2026, 8, 1, tzinfo=timezone.utc),
                                source, session_name, 42,
                                AnalysisScope(AnalysisScopeMode(analysis_scope), scope_start, scope_end))


def _search_window(qt_app, sources):
    first = _gui_report_summary(
        "Echo_Report_20261004_120000", source="qq", session_name="虚构读书群 Straße",
        analysis_scope="last_six_months",
    )
    second = _gui_report_summary(
        "Echo_Report_20261004_120000_2", source="wechat", session_name="Fictional Alice",
        analysis_scope="custom", scope_start=date(2026, 1, 1), scope_end=date(2026, 6, 30),
    )
    records = [dataclasses.replace(r, conversation_kind=kind)
               for r, kind in ((first, "group"), (second, "private"))]
    from qq_chat_analyzer.application.report_package_catalog import ReportPackageIssue
    facade = StubFacade(sources=sources, reports=records)
    facade._report_issues = (ReportPackageIssue("Echo_Report_20261003_120000", "metadata_unreadable"),)
    window = _main_window(qt_app, facade)
    window.show_local_data_page()
    _drain(window)
    return window, facade, records


def test_session_search_matches_both_names_without_merging_identity(qt_app):
    from qq_chat_analyzer.gui.session_analysis_panel import SessionAnalysisPanel
    from qq_chat_analyzer.application.facade import ChatSource, SessionInfo
    first = SessionInfo(ChatSource.QQ, "private:one", "虚构备注", search_names=("虚构备注", "虚构昵称"))
    second = SessionInfo(ChatSource.QQ, "private:two", "另一个人", search_names=("另一个人", "虚构昵称"))
    panel = SessionAnalysisPanel()
    panel.populate_sessions([first, second])
    for query, expected in (("虚构备注", [first]), ("虚构昵称", [first, second]), ("另一个人", [second])):
        panel._session_search.setText(query)
        assert panel._session_list.count() == len(expected)
        assert [panel._session_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(len(expected))] == [s.session_id for s in expected]


def test_history_search_accepts_report_member_aliases(qt_app, sources):
    window, facade, records = _search_window(qt_app, sources)
    records[1] = dataclasses.replace(records[1], search_names=("Fictional Remark", "Fictional Nick"))
    window.local_data_page._reports = records
    for query in ("Fictional Remark", "Fictional Nick"):
        window.local_data_page._search_input.setText(query)
        assert window.local_data_page._visible_names == (records[1].package_name,)


@pytest.mark.parametrize("action", ["button", "double_click"])
def test_local_data_search_reopen_keeps_identity_and_invalidates_selection(qt_app, sources, tmp_path, action):
    window, facade, records = _search_window(qt_app, sources)
    page = window.local_data_page
    resolved, opened = [], []
    html = tmp_path / "fictional.html"
    facade.get_report_package_html_path = lambda name: resolved.append(name) or html
    window._report_opener = lambda path: opened.append(path) or True
    page._history_list.setCurrentRow(0)
    assert page._open_report_button.isEnabled()
    page._search_input.setText("alice")
    assert not page._open_report_button.isEnabled()
    page._history_list.setCurrentRow(0)
    if action == "button":
        page._open_report_button.click()
    else:
        page._history_list.itemDoubleClicked.emit(page._history_list.item(0))
    assert resolved == [records[1].package_name]
    assert opened == [html]
    page._search_input.setText("absent")
    assert not page._open_report_button.isEnabled()
    page.refresh()
    assert page._search_input.text() == "absent"
    assert not page._open_report_button.isEnabled()


def _archive_window(qt_app, sources, reports=(), **kwargs):
    facade = StubFacade(sources=sources, reports=list(reports), **kwargs)
    window = _main_window(qt_app, facade)
    window.show_local_data_page()
    _drain(window)
    return window, facade


def test_local_data_page_keeps_last_known_archive_when_reload_fails(
    qt_app, sources,
) -> None:
    from qq_chat_analyzer.application.report_package_catalog import ReportStorageUsage

    module = _facade_module()
    window, facade = _archive_window(
        qt_app,
        sources,
        reports=[_gui_report_summary("h1")],
        report_usage=ReportStorageUsage(1, 4_096),
    )
    page = window.local_data_page
    facade._list_reports_error = module.FacadeError(
        "report_list_failed", "无法读取 Echo 本地报告，请稍后重试。",
    )
    page.refresh()

    assert page._status_label.text() == "无法读取 Echo 本地报告，请稍后重试。"
    assert page._history_list.count() == 1
    assert page._history_empty_label.isVisibleTo(window) is False
    assert page._summary_count_label.text() == "1 份"
    assert page._summary_size_label.text() == "4.0 KB"


def test_local_data_page_ignores_stale_refresh_results(qt_app, sources) -> None:
    window, facade = _archive_window(
        qt_app, sources, reports=[_gui_report_summary("first")],
    )
    page = window.local_data_page
    executor = _QueuedExecutor()
    page._executor = executor

    facade._reports = [_gui_report_summary("second")]
    page.refresh()
    facade._reports = [_gui_report_summary("third")]
    page.refresh()
    assert len(executor.tasks) == 2

    # The newer load lands first ...
    executor.succeed(1)
    assert page._history_list.item(0).data(Qt.ItemDataRole.UserRole) == "third"
    # ... and the older result, which arrives last, must not overwrite it.
    facade._reports = [_gui_report_summary("second")]
    executor.succeed(0)
    assert page._history_list.count() == 1
    assert page._history_list.item(0).data(Qt.ItemDataRole.UserRole) == "third"
    assert page._refresh_button.isEnabled() is True


def test_local_data_page_stale_error_and_finish_do_not_end_latest_refresh(
    qt_app, sources,
) -> None:
    window, _facade = _archive_window(
        qt_app, sources, reports=[_gui_report_summary("h1")],
    )
    page = window.local_data_page
    executor = _QueuedExecutor()
    page._executor = executor
    page.refresh()
    assert len(executor.tasks) == 1
    page.refresh()
    assert len(executor.tasks) == 2

    executor.fail(0, "report_list_failed", "过期的错误")
    assert "过期的错误" not in page._status_label.text()
    assert not page._refresh_button.isEnabled()
    executor.succeed(1)
    assert page._refresh_button.isEnabled()
    assert page._status_label.text() == ""
    assert page._history_list.count() == 1


# ---------------------------------------------------------------------------
# MainWindow terminal state + preserved behavior
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WeChat workspace guide coverage
# ---------------------------------------------------------------------------


def _wechat_guide_module():
    return importlib.import_module("qq_chat_analyzer.gui.wechat_workspace")


def _wechat_workspace(qt_app, facade):
    from qq_chat_analyzer.gui.wechat_workspace import WeChatWorkspace

    return WeChatWorkspace(facade, executor=_inline_executor())


def _wechat_connection_config():
    return _facade_module().WeChatEnvironmentConfig()


@pytest.mark.parametrize("state", ["WAITING_FOR_WECHAT_EXIT", "WAITING_FOR_WECHAT_START"])
def test_wechat_guided_setup_stale_exit_start_isolation(qt_app, state):
    executor = _IndependentDeferredExecutor()
    workspace = _wechat_guide_module().WeChatWorkspace(StubFacade(), executor=executor)
    workspace._start_wechat_connect(_wechat_connection_config())
    first = executor.tasks[0]
    workspace.cancel_connection()
    workspace._start_wechat_connect(_wechat_connection_config())
    second = executor.tasks[1]
    progress = _facade_module().WeChatConnectionProgress
    second.progress(progress.READY_FOR_LOGIN)
    before = (workspace._status_label.text(), workspace._wechat_guide_label.text(),
              workspace._wechat_guide_note_label.text(),
              workspace._wechat_guide_image_label.isHidden())
    emitted = []
    workspace.status_changed.connect(emitted.append)

    first.progress(getattr(progress, state))

    assert before == (workspace._status_label.text(), workspace._wechat_guide_label.text(),
                      workspace._wechat_guide_note_label.text(),
                      workspace._wechat_guide_image_label.isHidden())
    assert workspace._wechat_login_progress is progress.READY_FOR_LOGIN
    assert workspace._connection_task is second
    assert emitted == []


def test_wechat_structured_progress_crosses_real_facade_and_worker(
    qt_app, tmp_path, monkeypatch,
):
    import io
    from types import SimpleNamespace
    from qq_chat_analyzer.application.wechat import wechat_key_service
    from qq_chat_analyzer.application.wechat.wechat_setup_service import WeChatSetupService
    from qq_chat_analyzer.application.wechat.wechat_environment_config import (
        WeChatEnvironmentConfigLoader, WeChatEnvironmentConfigWriter,
    )
    from qq_chat_analyzer.gui import workers

    dll = tmp_path / "fictional.dll"
    helper = tmp_path / "fictional.cjs"
    dll.write_bytes(b"fake")
    helper.write_text("", encoding="utf-8")
    process = SimpleNamespace(
        stdout=io.StringIO("ab12" * 16 + "\n"),
        stderr=io.StringIO(
            "exports loaded: InitializeHook, PollKeyData, CleanupHook\n"
            "2026-08-09T00:00:00.000Z hook_success=true\n"
        ),
        returncode=0,
        wait=lambda **_kwargs: 0,
        closed=False,
    )
    def close_after_readers(readers, *, deadline):
        assert not any(reader.is_alive() for reader in readers)
        process.stdout.close()
        process.stderr.close()
        process.closed = True
    process.close_after_readers = close_after_readers
    snapshots = iter([[], [4242]])
    monkeypatch.setattr(
        wechat_key_service, "_find_weixin_pids",
        lambda: next(snapshots, [4242]),
    )
    helper_commands = []

    def launch(command, **_kwargs):
        helper_commands.append(command)
        return process

    key_service = wechat_key_service.WeChatKeyService(
        dll_path=dll, helper_path=helper,
        process_launcher=launch,
        node_finder=lambda _name: "fictional-node",
    )
    target = tmp_path / "wechat.json"
    setup = WeChatSetupService(
        config_loader=WeChatEnvironmentConfigLoader(target),
        config_writer=WeChatEnvironmentConfigWriter(target),
        key_service=key_service,
    )
    facade = _facade_module().ChatAnalyzerFacade(wechat_setup_service=setup)
    workspace = _wechat_workspace(qt_app, facade)
    seen, errors = [], []
    ui_thread = threading.get_ident()

    def on_progress(event):
        workspace._handle_wechat_connect_progress(event)
        seen.append((event, workspace._status_label.text(), threading.get_ident()))

    workers.submit(
        lambda report: facade.acquire_wechat_db_key(progress=report),
        on_success=lambda _result: None,
        on_error=lambda code, message: errors.append((code, message)),
        on_progress=on_progress,
    )
    _settle_workers()

    assert errors == []
    assert key_service._legacy_injected is False
    assert len(helper_commands) == 1
    command = helper_commands[0]
    assert command[:2] == ["fictional-node", str(helper)]
    assert command[command.index("--pid") + 1] == "4242"
    assert [getattr(event, "name", None) for event, _text, _thread in seen] == [
        "PREPARING", "WAITING_FOR_WECHAT_START", "READY_FOR_LOGIN",
        "CREDENTIAL_RECEIVED",
    ]
    assert all(thread == ui_thread for _event, _text, thread in seen)
    assert seen[0][1] == "正在准备微信连接"
    assert "现在可以登录微信" in seen[2][1]
    assert seen[3][1] == "正在连接微信"


@pytest.mark.parametrize("kind,fmt,local,preview,cancel", [
    ("private", "PNG", False, False, False), ("group", "GIF", False, True, False),
    ("private", "PNG", True, False, False), ("group", "WEBP", True, False, False),
    ("group", "GIF", False, False, True),
])
def test_generate_button_opens_only_final_report_with_media(
    qt_app, tmp_path, monkeypatch, kind, fmt, local, preview, cancel,
):
    import hashlib
    import json
    from test_sticker_data import image_bytes
    from test_qq_direct_database_facade import _fetch_facade
    from qq_chat_analyzer.application.facade import AnalysisConfig, ChatSource
    from qq_chat_analyzer.gui.workers import run_inline
    from PySide6.QtWidgets import QMessageBox

    # The local PNG case is APNG; all animated fixtures have red and blue frames.
    raw = image_bytes(fmt, animated=fmt != "PNG" or local)
    digest = b"\xab" * 16 if preview else hashlib.md5(raw).digest()
    facade, provider, runtime = _fetch_facade(
        tmp_path, digest=digest, content=raw, group_sender="10087" if kind == "group" else None)
    roots = tmp_path / "trusted-originals"
    roots.mkdir()
    if local:
        (roots / f"{digest.hex()}.{fmt.lower()}").write_bytes(raw)
    config = AnalysisConfig(output_directory=tmp_path / "generated", sticker_media_roots=(roots,))
    window = _main_window(qt_app, facade)
    opened, outcomes = [], []
    window._report_opener = lambda path: opened.append(path) or True
    panel = window.qq_workspace.session_panel
    panel.configure(facade, ChatSource.QQ, run_inline)
    panel.analysis_succeeded.connect(outcomes.append)
    panel.populate_sessions([s for s in facade.list_sessions(ChatSource.QQ) if s.session_type == kind])
    panel._session_list.setCurrentRow(0)
    monkeypatch.setattr(panel, "build_config", lambda: config)
    runtime.acquired.clear()
    runtime.cleaned.clear()
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: pytest.fail("unexpected consent dialog"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a, **_k: pytest.fail("unexpected report failure"))
    if cancel:
        download = provider.download_pic_media
        def cancel_during_download(**locator):
            window.processing_page.cancel_button.click()
            return download(**locator)
        monkeypatch.setattr(provider, "download_pic_media", cancel_during_download)
    panel._analyze_button.click()
    for _ in range(4):
        qt_app.processEvents()
    outcome = outcomes[0] if outcomes else None
    if cancel:
        assert outcome is None and not opened and not panel._analysis_running
        assert "取消" in window._status_label.text()
        assert not (tmp_path / "generated/echo-report.html").exists()
        assert len(provider.calls) == 2 and runtime.acquired == runtime.cleaned
        window.close()
        return
    assert outcome is not None and opened == [outcome.report_path.resolve()]
    from PySide6.QtWidgets import QPushButton
    assert not any("查看表情素材" in button.text() for button in window.findChildren(QPushButton))
    assert runtime.acquired == runtime.cleaned and len(runtime.acquired) == 1
    assert len(provider.calls) == (0 if local else 2)
    asset, = json.loads((outcome.report_directory / "echo-report.json").read_text(encoding="utf-8"))["stickers"]["assets"].values()
    assert asset["status"] == ("preview" if preview else "available")
    assert asset["data_uri"].startswith("media/")
    assert (outcome.report_directory / asset["data_uri"]).read_bytes() == raw
    assert asset["data_uri"] in outcome.report_path.read_text(encoding="utf-8")
    assert "表情包图片 1/1" in window._status_label.text()
    assert "预算受限" not in window._status_label.text()
    window.close()
