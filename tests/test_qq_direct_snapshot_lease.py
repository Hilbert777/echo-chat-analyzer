"""RED-GREEN coverage for the Direct DB snapshot lease (Phase 2A).

``QQDirectDatabaseImportService.list_sessions`` must acquire a fresh runtime
generation, validate its manifest and database, materialize session
descriptors into memory, and remove the generation before returning.  These
tests drive that lifecycle through a fake runtime client, so no real NapCat
bridge, passphrase or QQ data is involved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_chat_analyzer.application.qq.qq_direct_database_import_service import (
    QQDirectDatabaseImportService,
    QQDirectSnapshotAcquireFailed,
    QQDirectSnapshotCleanupFailed,
    QQDirectSnapshotInvalid,
)
from qq_chat_analyzer.providers.qq_database_provider import QQDatabaseProvider
from qq_chat_analyzer.providers.qq_direct_snapshot_runtime import (
    QQSnapshotCleanupFailed,
    QQSnapshotRuntimeUnavailable,
)

from qq_direct_db_testing import FICTIONAL_UIN, FakeSnapshotRuntime


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
        connection.execute(
            '''INSERT INTO c2c_msg_table
                ("40001", "40027", "40030", "40033", "40050", "40800")
                VALUES (?, ?, ?, ?, ?, ?)''',
            (201, "private-partition", "fictional-peer", "fictional-sender", 1760000002, _text_blob("fictional private message")),
        )


def _service(root: Path, runtime: FakeSnapshotRuntime) -> QQDirectDatabaseImportService:
    return QQDirectDatabaseImportService(runtime_client=runtime)


def _snapshot(root: Path) -> Path:
    return root / "source.db"


# ------------------------------------------------------------------ success


def test_acquire_success_and_valid_manifest_returns_sessions(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)

    sessions = {session.session_type: session for session in service.list_sessions()}

    assert set(sessions) == {"group", "private"}
    assert sessions["group"].internal_key == "group-partition"
    assert sessions["private"].internal_key == "private-partition"


def test_list_sessions_cleans_up_generation_before_return(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)

    service.list_sessions()

    assert runtime.acquired == ["gen-0001"]
    assert runtime.cleaned == ["gen-0001"]
    assert not runtime.generation_directory("gen-0001").exists()


# ------------------------------------------------------------------ acquire


def test_acquire_failure_never_reads_a_previous_generation(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot, auto_write=False)
    runtime.write_generation("stale-gen")
    runtime.acquire_error = QQSnapshotRuntimeUnavailable()
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotAcquireFailed):
        service.list_sessions()

    # The stale generation was never read and never cleaned up.
    assert runtime.cleaned == []
    assert runtime.generation_directory("stale-gen").exists()


# ------------------------------------------------------------------ manifest


def test_generation_mismatch_fails_closed(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.manifest_overrides = {"generation_id": "someone-else"}
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        service.list_sessions()

    assert runtime.cleaned == ["gen-0001"]


@pytest.mark.parametrize("schema_version", [0, 2, "1", None])
def test_wrong_schema_version_rejects(tmp_path: Path, schema_version) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.manifest_overrides = {"schema_version": schema_version}
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        service.list_sessions()


def test_manifest_non_ready_state_rejects(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.manifest_overrides = {"state": "building"}
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        service.list_sessions()


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {"namespace": "qq_uid", "value": FICTIONAL_UIN},
        {"namespace": "qq_uin", "value": "0"},
        "not-a-dict",
    ],
)
def test_missing_or_invalid_identity_rejects(tmp_path: Path, identity) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.manifest_overrides = {"identity": identity}
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        service.list_sessions()


def test_missing_database_rejects(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.manifest_overrides = {"database": "missing.db"}
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        service.list_sessions()


def test_traversal_database_name_rejects(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.manifest_overrides = {"database": "../outside.db"}
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        service.list_sessions()


def test_unreadable_manifest_rejects(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)
    runtime.acquire()
    (runtime.generation_directory("gen-0001") / "manifest.json").write_text(
        "not json",
        encoding="utf-8",
    )
    runtime.acquired.clear()
    runtime.cleaned.clear()
    runtime.acquire_result = "gen-0001"
    runtime.auto_write = False

    with pytest.raises(QQDirectSnapshotInvalid):
        service.list_sessions()


# ------------------------------------------------------------------ cleanup


def test_provider_exception_still_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)

    def _raise(self, *args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(QQDatabaseProvider, "list_sessions", _raise)

    with pytest.raises(RuntimeError, match="provider exploded"):
        service.list_sessions()

    assert runtime.cleaned == ["gen-0001"]
    assert not runtime.generation_directory("gen-0001").exists()


def test_cleanup_failure_raises_stable_privacy_safe_diagnostic(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.cleanup_error = QQSnapshotCleanupFailed()
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotCleanupFailed):
        service.list_sessions()

    assert runtime.cleaned == ["gen-0001"]


def test_error_does_not_leak_identity_value(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.identity_value = FICTIONAL_UIN
    runtime.manifest_overrides = {"state": "building"}
    service = _service(tmp_path, runtime)

    with pytest.raises(QQDirectSnapshotInvalid) as captured:
        service.list_sessions()

    assert FICTIONAL_UIN not in str(captured.value)
    assert FICTIONAL_UIN not in captured.value.public_message


@pytest.mark.parametrize("failure", [None, "manifest", "quick_check", "group_query", "private_query", "cleanup", "double"])
def test_session_stage_diagnostics(tmp_path, monkeypatch, caplog, failure):
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)
    if failure == "manifest":
        runtime.manifest_overrides = {"state": "building"}
    if failure == "quick_check":
        monkeypatch.setattr(QQDatabaseProvider, "quick_check", lambda path, **kwargs: "failed")
    if failure in ("group_query", "private_query", "double"):
        original = QQDatabaseProvider._query_rows
        def query(self, *args, **kwargs):
            target = "c2c_msg_table" if failure == "private_query" else "group_msg_table"
            if kwargs["table_name"] == target:
                raise sqlite3.DatabaseError("PRIVATE_EXCEPTION SQL account credential")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(QQDatabaseProvider, "_query_rows", query)
    if failure in ("cleanup", "double"):
        runtime.cleanup_error = QQSnapshotCleanupFailed("PRIVATE_CLEANUP")
    expected = (QQDirectSnapshotCleanupFailed if failure in ("cleanup", "double") else
                sqlite3.DatabaseError if failure in ("group_query", "private_query") else
                QQDirectSnapshotInvalid)
    with caplog.at_level("INFO"):
        if failure:
            with pytest.raises(expected):
                service.list_sessions()
        else:
            assert len(service.list_sessions()) == 2
    messages = [r.getMessage() for r in caplog.records if "[qq sessions]" in r.getMessage()]
    log = "\n".join(messages)
    for stage in ("manifest", "quick_check", "group_query", "private_query", "cleanup", "names", "service"):
        if failure is None:
            assert f"stage={stage} outcome=started" in log
            assert f"stage={stage} outcome=success" in log
    if failure:
        stage = "group_query" if failure == "double" else failure
        assert f"stage={stage} outcome=failed" in log
        assert "stage=service outcome=failed" in log
    if failure == "double":
        assert log.index("stage=group_query outcome=failed") < log.index("stage=cleanup outcome=failed")
        assert "failure_code=sqlite_error" in log
        assert "failure_code=operation_failed" in log
    assert runtime.cleaned == ["gen-0001"]
    for private in ("PRIVATE", FICTIONAL_UIN, str(tmp_path), "fictional-group", "fictional-peer"):
        assert private not in log


@pytest.mark.parametrize("body_failure", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_broken_session_logging_preserves_cleanup_and_exception(tmp_path, monkeypatch, body_failure, cleanup_failure):
    from qq_chat_analyzer import diagnostics
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)
    original_info = diagnostics.logging.Logger.info
    def broken(self, message, *args, **kwargs):
        if "[qq sessions]" in message:
            raise RuntimeError("PRIVATE_LOG_FAILURE")
        return original_info(self, message, *args, **kwargs)
    monkeypatch.setattr(diagnostics.logging.Logger, "info", broken)
    if cleanup_failure:
        runtime.cleanup_error = QQSnapshotCleanupFailed("PRIVATE_CLEANUP")
    if body_failure:
        runtime.manifest_overrides = {"state": "building"}
    if body_failure or cleanup_failure:
        expected = QQDirectSnapshotCleanupFailed if cleanup_failure else QQDirectSnapshotInvalid
        with pytest.raises(expected):
            service.list_sessions()
    else:
        assert len(service.list_sessions()) == 2
    assert runtime.cleaned == ["gen-0001"]


@pytest.mark.parametrize("failure", [None, "names_provider", "group_names_rpc", "friend_names_rpc"])
def test_optional_name_rpc_failure_is_observable_without_failing_sessions(tmp_path, caplog, failure):
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)
    def call(stage, result):
        if stage == failure:
            raise RuntimeError("PRIVATE_RPC account token path")
        return result
    provider = SimpleNamespace(
        list_groups=lambda **kwargs: call("group_names_rpc", [SimpleNamespace(
            group_code="fictional-group", group_name="PRIVATE_GROUP_NAME")]),
        list_friends=lambda **kwargs: call("friend_names_rpc", [SimpleNamespace(
            uin="fictional-peer", display_name="PRIVATE_FRIEND_NAME")]),
    )
    service._provider_factory = SimpleNamespace(create=lambda: call("names_provider", provider))
    with caplog.at_level("INFO"):
        assert len(service.list_sessions()) == 2
    log = "\n".join(r.getMessage() for r in caplog.records if "[qq sessions]" in r.getMessage())
    for stage in ("names_provider", "group_names_rpc", "friend_names_rpc"):
        if failure == "names_provider" and stage != failure:
            assert f"stage={stage}" not in log
        else:
            outcome = "failed" if stage == failure else "success"
            assert f"stage={stage} outcome={outcome}" in log
    assert "stage=names outcome=success" in log
    assert runtime.cleaned == ["gen-0001"]
    assert "PRIVATE" not in log


def test_optional_names_share_a_short_budget_and_return_sessions_after_timeout(tmp_path, monkeypatch):
    from qq_chat_analyzer.application.qq import qq_direct_database_import_service as module
    snapshot = _snapshot(tmp_path)
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = _service(tmp_path, runtime)
    service.start()
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    calls = []
    def groups(*, timeout=None):
        calls.append(timeout)
        clock[0] += 30 if timeout is None else timeout
        raise TimeoutError("PRIVATE_NAMES")
    def friends(*, timeout=None):
        pytest.fail("An exhausted optional budget must not start another RPC")
    service._provider_factory = SimpleNamespace(create=lambda: SimpleNamespace(list_groups=groups, list_friends=friends))
    assert len(service.list_sessions()) == 2
    assert calls == [5.0]
    assert clock[0] == 1005.0
    assert runtime.cleaned == ["gen-0001"]
