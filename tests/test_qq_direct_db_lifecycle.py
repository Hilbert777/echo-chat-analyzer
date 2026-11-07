"""RED-GREEN coverage for the Direct DB snapshot lifecycle (Phase 2C).

The Direct DB service owns application-side lifecycle state so the plaintext
snapshot can be recovered at startup and cleaned up before the QQ runtime is
stopped.  These tests drive the service and facade through the fake runtime
client; every database, identity and message here is fictional.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from qq_chat_analyzer.application.facade import ChatAnalyzerFacade, ChatSource
from qq_chat_analyzer.application.connection_models import ConnectionState
from qq_chat_analyzer.application.qq.qq_connection_service import QQConnectionService
from qq_chat_analyzer.application.qq.qq_direct_database_import_service import (
    DEFAULT_SHUTDOWN_RECOVER_SECONDS,
    QQDirectDatabaseImportService,
    QQDirectDatabaseRecoveryFailed,
    QQDirectDatabaseShuttingDown,
    QQDirectDatabaseState,
    QQDirectDatabaseUnavailable,
    QQDirectDatabaseNotReady,
)
from qq_chat_analyzer.providers.qq_direct_snapshot_runtime import (
    QQSnapshotRuntimeFailure,
    QQSnapshotRuntimeNotReady,
    QQSnapshotRuntimeUnavailable,
)

from qq_direct_db_testing import FICTIONAL_UIN, FakeSnapshotRuntime


GROUP_SESSION_ID = "group:group-partition"


from qq_db_test_data import (
    encode_varint as _varint,
    length_delimited_field as _field,
)


def _text_blob(text: str) -> bytes:
    segment = _varint((45002 << 3) | 0) + _varint(1) + _field(45101, text.encode("utf-8"))
    return _field(40800, segment)


def _create_snapshot(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        for table_name in ("group_msg_table", "c2c_msg_table"):
            connection.execute(
                f'''CREATE TABLE {table_name} (
                    "40001" INTEGER PRIMARY KEY,
                    "40027" TEXT NOT NULL,
                    "40030" TEXT NOT NULL,
                    "40033" TEXT NOT NULL,
                    "40050" INTEGER NOT NULL,
                    "40800" BLOB NOT NULL
                )'''
            )
        connection.execute(
            '''INSERT INTO group_msg_table
                ("40001", "40027", "40030", "40033", "40050", "40800")
                VALUES (?, ?, ?, ?, ?, ?)''',
            (101, "group-partition", "fictional-group", "fictional-sender", 1760000001, _text_blob("fictional group message")),
        )


def _service(
    runtime: FakeSnapshotRuntime,
    *,
    shutdown_drain_seconds: float = 5.0,
) -> QQDirectDatabaseImportService:
    return QQDirectDatabaseImportService(
        runtime_client=runtime,
        shutdown_drain_seconds=shutdown_drain_seconds,
    )


# ------------------------------------------------------------------ startup


def test_startup_recover_failure_fails_closed(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.recover_error = QQSnapshotRuntimeFailure()
    service = _service(runtime)

    with pytest.raises(QQDirectDatabaseRecoveryFailed):
        service.start()

    assert service.state is QQDirectDatabaseState.CLOSED
    # Fail-closed: no generation is ever read after a failed recover.
    with pytest.raises(QQDirectDatabaseUnavailable):
        service.list_sessions()


def test_recover_failure_never_reads_old_generation(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.write_generation("stale-gen")
    runtime.recover_error = QQSnapshotRuntimeFailure()
    service = _service(runtime)

    with pytest.raises(QQDirectDatabaseRecoveryFailed):
        service.list_sessions()

    assert runtime.acquired == []
    assert runtime.cleaned == []


# ------------------------------------------------------------------ shutdown


def test_shutdown_rejects_new_list_sessions(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime)
    service.start()

    service.shutdown()

    with pytest.raises(QQDirectDatabaseShuttingDown):
        service.list_sessions()


def test_shutdown_rejects_new_acquired_session(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime)
    service.start()

    service.shutdown()

    with pytest.raises(QQDirectDatabaseShuttingDown):
        with service.acquired_session(GROUP_SESSION_ID):
            pass


def test_shutdown_recover_failure_is_stable_privacy_safe(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime)
    service.start()

    runtime.recover_error = QQSnapshotRuntimeFailure()

    with pytest.raises(QQDirectDatabaseRecoveryFailed) as captured:
        service.shutdown()

    assert FICTIONAL_UIN not in str(captured.value)
    assert FICTIONAL_UIN not in captured.value.public_message
    assert service.state is QQDirectDatabaseState.CLOSED


def test_shutdown_drains_inflight_acquisition_without_second_acquire(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime, shutdown_drain_seconds=5.0)
    service.start()

    entered = threading.Event()
    release = threading.Event()
    original_acquire = runtime.acquire

    def _blocking_acquire() -> str:
        entered.set()
        release.wait(timeout=5.0)
        return original_acquire()

    runtime.acquire = _blocking_acquire

    outcome: dict[str, bool] = {}

    def _run() -> None:
        with service.acquired_session(GROUP_SESSION_ID) as acquisition:
            outcome["materialized"] = acquisition.payload_path.is_file()

    worker = threading.Thread(target=_run)
    worker.start()
    assert entered.wait(timeout=2.0)

    shutdown_thread = threading.Thread(target=service.shutdown)
    shutdown_thread.start()

    # Shutdown is now draining; release the in-flight acquire to let it finish.
    time.sleep(0.05)
    release.set()

    worker.join(timeout=5.0)
    shutdown_thread.join(timeout=5.0)

    assert not worker.is_alive()
    assert not shutdown_thread.is_alive()
    assert outcome.get("materialized") is True
    # Exactly one generation: shutdown never started a second acquire.
    assert runtime.acquired == ["gen-0001"]
    assert runtime.cleaned == ["gen-0001"]
    assert service.state is QQDirectDatabaseState.CLOSED


def test_session_refresh_and_analysis_do_not_replace_a_generation_being_read(tmp_path, monkeypatch):
    """A second acquire would delete the first task's live snapshot on Windows."""
    from qq_chat_analyzer.providers.qq_database_provider import QQDatabaseProvider

    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    overlap = threading.Event()

    class SingleGenerationRuntime(FakeSnapshotRuntime):
        def acquire(self):
            if set(self.acquired) - set(self.cleaned):
                overlap.set()
                raise QQSnapshotRuntimeFailure()
            return super().acquire()

    runtime = SingleGenerationRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime)
    service.start()
    checking = threading.Event()
    release = threading.Event()
    analyzing = threading.Event()
    original = QQDatabaseProvider.quick_check

    def blocked_check(path, **kwargs):
        if not checking.is_set():
            checking.set()
            assert release.wait(3)
        return original(path, **kwargs)

    monkeypatch.setattr(QQDatabaseProvider, "quick_check", blocked_check)
    outcomes = []

    def refresh():
        try:
            service.list_sessions()
            outcomes.append("refresh_ok")
        except Exception as error:
            outcomes.append(type(error).__name__)

    def analyze():
        analyzing.set()
        try:
            with service.acquired_session(GROUP_SESSION_ID) as acquired:
                assert acquired.payload_path.is_file()
            outcomes.append("analysis_ok")
        except Exception as error:
            outcomes.append(type(error).__name__)

    first = threading.Thread(target=refresh)
    second = threading.Thread(target=analyze)
    first.start()
    assert checking.wait(3)
    second.start()
    assert analyzing.wait(3)
    try:
        assert not overlap.wait(0.2)
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert sorted(outcomes) == ["analysis_ok", "refresh_ok"]
    assert runtime.acquired == runtime.cleaned == ["gen-0001", "gen-0002"]


def test_shutdown_cancels_slow_reader_and_rejects_queued_acquisition(tmp_path, monkeypatch):
    from qq_chat_analyzer.providers.qq_database_provider import QQDatabaseProvider
    from qq_chat_analyzer.application.shutdown_models import ShutdownStatus

    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime, shutdown_drain_seconds=0.01)
    service.start()
    entered = threading.Event()
    outcomes = []

    def slow_check(path, *, cancelled):
        entered.set()
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert cancelled()
        return "error"

    monkeypatch.setattr(QQDatabaseProvider, "quick_check", slow_check)

    def refresh():
        try:
            service.list_sessions()
        except Exception as error:
            outcomes.append(type(error).__name__)

    first = threading.Thread(target=refresh)
    second = threading.Thread(target=refresh)
    first.start()
    assert entered.wait(3)
    second.start()
    result = service.shutdown()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert result is ShutdownStatus.TIMED_OUT
    assert sorted(outcomes) == ["QQDirectDatabaseShuttingDown", "QQDirectSnapshotInvalid"]
    assert runtime.acquired == runtime.cleaned == ["gen-0001"]
    assert not runtime.generation_directory("gen-0001").exists()
    assert runtime.recover_calls == 2


def test_second_drain_timeout_keeps_live_snapshot_and_runtime(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from qq_chat_analyzer.providers.qq_database_provider import QQDatabaseProvider
    from qq_chat_analyzer.application.shutdown_models import ShutdownStatus

    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime)
    service.start()
    entered, release = threading.Event(), threading.Event()
    original_check = QQDatabaseProvider.quick_check
    outcomes = []

    def held_check(path, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_check(path, **kwargs)

    def read():
        try:
            service.list_sessions()
        except Exception as error:
            outcomes.append(type(error).__name__)

    monkeypatch.setattr(QQDatabaseProvider, "quick_check", held_check)
    owner = threading.Thread(target=read)
    owner.start()
    registry = Mock()
    credential = Mock()
    facade = ChatAnalyzerFacade(qq_service=service, qq_process_registry=registry,
                               qq_runtime_session=credential)
    try:
        assert entered.wait(3)
        # The reader is demonstrably live throughout both deterministic drains.
        drain = Mock(return_value=False)
        monkeypatch.setattr(service, "_drain_acquisitions", drain)
        result = facade.shutdown()
        statuses = {step.name: step.status for step in result.steps}
        assert drain.call_count == 2 and service._read_cancelled.is_set()
        assert runtime.recover_calls == 1  # Only startup recovery ran.
        assert runtime.generation_directory("gen-0001").is_dir()
        assert runtime.cleaned == [] and owner.is_alive()
        assert service.state is QQDirectDatabaseState.SHUTTING_DOWN
        assert not service.runtime_stop_safe
        assert statuses["direct_db_cleanup"] is ShutdownStatus.TIMED_OUT
        assert statuses["qq_runtime_termination"] is ShutdownStatus.TIMED_OUT
        assert not result.can_apply_update
        registry.terminate_all.assert_not_called()
        credential.retire.assert_not_called()
        assert service.shutdown() is ShutdownStatus.TIMED_OUT
        assert runtime.recover_calls == 1
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    assert outcomes == ["QQDirectSnapshotInvalid"]
    assert runtime.cleaned == ["gen-0001"]
    assert not runtime.generation_directory("gen-0001").exists()


def test_connected_napcat_waits_for_delayed_snapshot_api_before_listing(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)

    class DelayedRuntime(FakeSnapshotRuntime):
        def recover(self, *, deadline=None) -> None:
            if self.recover_calls < 2:
                self.recover_calls += 1
                raise QQSnapshotRuntimeNotReady()
            super().recover(deadline=deadline)

    class ConnectedNapCat:
        def status(self):
            from qq_chat_analyzer.providers.napcat_qq_provider import NapCatStatus
            return NapCatStatus(True, True, {"uin": "12345678"}, True, True, True)

    runtime = DelayedRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime)
    facade = ChatAnalyzerFacade(
        qq_service=service,
        qq_connection_service=QQConnectionService(provider=ConnectedNapCat()),
    )

    assert facade.get_qq_connection_snapshot().state is ConnectionState.CONNECTED
    assert [session.session_id for session in facade.list_sessions(ChatSource.QQ)] == [GROUP_SESSION_ID]
    assert runtime.recover_calls == 3
    assert service.state is QQDirectDatabaseState.ACTIVE


def test_real_recover_cleanup_failure_remains_terminal_and_never_reads_old_generation(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.write_generation("stale-gen")
    runtime.recover_error = QQSnapshotRuntimeFailure()
    service = _service(runtime)

    with pytest.raises(QQDirectDatabaseRecoveryFailed):
        service.list_sessions()
    runtime.recover_error = None
    with pytest.raises(QQDirectDatabaseUnavailable):
        service.list_sessions()
    assert runtime.acquired == []
    assert service.state is QQDirectDatabaseState.CLOSED


def test_shutdown_passes_one_monotonic_recover_deadline(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(runtime)
    service.start()

    before = time.monotonic()
    service.shutdown()
    after = time.monotonic()

    assert runtime.recover_deadlines[0] is not None
    assert len(runtime.recover_deadlines) == 2
    assert (
        before + DEFAULT_SHUTDOWN_RECOVER_SECONDS
        <= runtime.recover_deadlines[1]
        <= after + DEFAULT_SHUTDOWN_RECOVER_SECONDS
    )


# ------------------------------------------------------------------ orphans


# ------------------------------------------------------------------ success


# ------------------------------------------------------------------ facade


def test_facade_shutdown_survives_direct_db_cleanup_failure() -> None:
    events: list[str] = []

    class _FailingQQService:
        def shutdown(self) -> None:
            events.append("direct_db_cleanup")
            raise QQDirectDatabaseRecoveryFailed()

    class _OrderedRegistry:
        def terminate_all(self) -> int:
            events.append("qq_terminate")
            return 0

    facade = ChatAnalyzerFacade(
        qq_service=_FailingQQService(),
        qq_process_registry=_OrderedRegistry(),
    )

    # A Direct DB cleanup failure must not skip QQ runtime termination.
    facade.shutdown()

    assert events == ["direct_db_cleanup", "qq_terminate"]
