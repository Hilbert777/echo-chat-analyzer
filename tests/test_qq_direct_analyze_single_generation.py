"""RED-GREEN coverage for the single-generation analyze path (Phase 2B).

One ``analyze_session`` for the Direct DB QQ source must acquire exactly one
runtime snapshot generation, validate it once, locate the target session and
materialize its payload from that same generation, then release the generation
before tokenizer/analyzer/report run.  These tests drive the service and the
facade through the fake runtime client, so no real NapCat bridge, passphrase or
QQ data is involved.  Every database, identity and message here is fictional.
"""

from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from qq_chat_analyzer.application.analysis_service import AnalysisApplicationService
from qq_chat_analyzer.application.facade import (
    AnalysisConfig,
    ChatAnalyzerFacade,
    ChatSource,
)
from qq_chat_analyzer.application.import_request import ImportRequest
from qq_chat_analyzer.application.import_service import ImportService
from qq_chat_analyzer.application.qq.qq_direct_database_import_service import (
    QQDirectDatabaseImportService,
    QQDirectSessionNotFound,
    QQDirectSnapshotAcquireFailed,
    QQDirectSnapshotCleanupFailed,
    QQDirectSnapshotInvalid,
)
from qq_chat_analyzer.providers.qq_database_provider import QQDatabaseProvider
from qq_chat_analyzer.qq_db_adapter import parse_qq_db_rich_messages
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
            (101, "group-partition", "fictional-group", "fictional-group-sender", 1760000001, _text_blob("fictional group message")),
        )
        connection.execute(
            '''INSERT INTO c2c_msg_table
                ("40001", "40027", "40030", "40033", "40050", "40800")
                VALUES (?, ?, ?, ?, ?, ?)''',
            (201, "private-partition", "fictional-peer", "fictional-private-sender", 1760000002, _text_blob("fictional private message")),
        )


GROUP_SESSION_ID = "group:group-partition"
PRIVATE_SESSION_ID = "private:private-partition"


def _service(tmp_path: Path, **kwargs) -> QQDirectDatabaseImportService:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    return QQDirectDatabaseImportService(runtime_client=runtime, **kwargs), runtime, snapshot


# ------------------------------------------------------------------ service


def test_group_member_card_and_friend_remark_join_only_by_uin(tmp_path):
    from qq_chat_analyzer.providers.napcat_qq_provider import NapCatFriend
    service, runtime, snapshot = _service(tmp_path)
    with sqlite3.connect(snapshot) as connection:
        connection.execute('UPDATE group_msg_table SET "40033" = ?', ("123456789",))
    runtime.get_group_member_all = lambda _: {"result": {"infos": {
        "opaque-map-key": {"uin": "123456789", "cardName": "虚构群名片", "nick": "虚构昵称"}}}}
    friends = [NapCatFriend("fictional-uid", "123456789", "虚构昵称", "虚构备注"),
               NapCatFriend("other-uid", "234567890", "虚构昵称", "另一个人的备注")]
    provider = SimpleNamespace(list_groups=lambda **_: [], list_friends=lambda **_: friends)
    service._provider_factory = SimpleNamespace(create=lambda: provider)
    with service.acquired_session(GROUP_SESSION_ID) as acquisition:
        payload = json.loads(acquisition.payload_path.read_text(encoding="utf-8"))
        messages, warnings = parse_qq_db_rich_messages(payload)
        assert not warnings and len(messages) == 1
        sender = messages[0].sender
        assert sender.identity_id == "123456789"
        assert (sender.remark, sender.contextual_name, sender.nickname) == ("虚构备注", "虚构群名片", "虚构昵称")
        assert runtime.cleaned == ["gen-0001"]


def test_payload_materialized_before_generation_release(tmp_path: Path) -> None:
    """The plaintext generation is gone before the analysis consumer runs."""
    service, runtime, _ = _service(tmp_path)

    with service.acquired_session(GROUP_SESSION_ID) as acquisition:
        # The generation lease was released before yield, so tokenizer/analyzer
        # never hold the plaintext DB.
        assert runtime.cleaned == ["gen-0001"]
        assert not runtime.generation_directory("gen-0001").exists()
        # The transient payload is still fully readable.
        assert acquisition.payload_path.is_file()
        outcome = ImportService().execute(ImportRequest(input_path=acquisition.payload_path))
        assert outcome.messages[0].text == "fictional group message"

    assert runtime.acquired == ["gen-0001"]


def test_materialize_exception_still_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runtime, _ = _service(tmp_path)

    def _raise(*_args, **_kwargs) -> None:
        raise RuntimeError("materialization exploded")

    monkeypatch.setattr(QQDatabaseProvider, "materialize_session_payload", _raise)

    with pytest.raises(RuntimeError, match="materialization exploded"):
        with service.acquired_session(GROUP_SESSION_ID):
            pass

    assert runtime.cleaned == ["gen-0001"]
    assert not runtime.generation_directory("gen-0001").exists()


def test_payload_enrichment_reads_once_after_generation_cleanup(tmp_path, monkeypatch):
    service, runtime, _ = _service(tmp_path)
    original_read = Path.read_text
    payload_reads = []

    def read(path, *args, **kwargs):
        if path.name == "qq-db-session.json":
            assert runtime.cleaned == ["gen-0001"]
            payload_reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with service.acquired_session(GROUP_SESSION_ID) as acquisition:
        assert len(payload_reads) == 1
        payload = json.loads(original_read(acquisition.payload_path, encoding="utf-8"))
        assert len(payload["records"]) == 1
    assert not payload_reads[0].exists()


def test_invalid_manifest_still_cleans_up(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.manifest_overrides = {"state": "building"}
    service = QQDirectDatabaseImportService(runtime_client=runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        with service.acquired_session(GROUP_SESSION_ID):
            pass

    assert runtime.cleaned == ["gen-0001"]


def test_unreadable_payload_releases_scratch_and_generation(tmp_path, monkeypatch):
    service, runtime, _ = _service(tmp_path)
    materialize = QQDatabaseProvider.materialize_session_payload
    paths = []

    def corrupt(provider, *args, **kwargs):
        path = materialize(provider, *args, **kwargs)
        path.write_text("{invalid fictional payload", encoding="utf-8")
        paths.append(path)
        return path

    monkeypatch.setattr(QQDatabaseProvider, "materialize_session_payload", corrupt)
    with pytest.raises(json.JSONDecodeError):
        with service.acquired_session(GROUP_SESSION_ID):
            pytest.fail("Invalid JSON must not reach a consumer")
    assert runtime.cleaned == ["gen-0001"]
    assert not runtime.generation_directory("gen-0001").exists()
    assert not paths[0].parent.exists()


def test_cleanup_failure_returns_stable_privacy_safe_error(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    runtime.cleanup_error = QQSnapshotCleanupFailed()
    service = QQDirectDatabaseImportService(runtime_client=runtime)

    with pytest.raises(QQDirectSnapshotCleanupFailed) as captured:
        with service.acquired_session(GROUP_SESSION_ID):
            pass

    assert FICTIONAL_UIN not in str(captured.value)
    assert FICTIONAL_UIN not in captured.value.public_message


def test_second_acquire_failure_does_not_reuse_previous(tmp_path: Path) -> None:
    service, runtime, _ = _service(tmp_path)

    with service.acquired_session(GROUP_SESSION_ID):
        pass

    runtime.acquire_error = QQSnapshotRuntimeUnavailable()

    with pytest.raises(QQDirectSnapshotAcquireFailed):
        with service.acquired_session(GROUP_SESSION_ID):
            pass

    # The failed second acquire never appended a generation, and the first
    # generation was already cleaned.
    assert runtime.acquired == ["gen-0001"]
    assert runtime.cleaned == ["gen-0001"]


# ------------------------------------------------------------------ facade


def _facade(service: QQDirectDatabaseImportService) -> ChatAnalyzerFacade:
    return ChatAnalyzerFacade(
        qq_service=service,
        analysis_service=AnalysisApplicationService(),
        stopwords_directory=Path(__file__).resolve().parents[1],
    )


def test_analyze_acquires_exactly_one_generation(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = QQDirectDatabaseImportService(runtime_client=runtime)
    facade = _facade(service)

    outcome = facade.analyze_session(
        ChatSource.QQ,
        GROUP_SESSION_ID,
        AnalysisConfig(output_directory=tmp_path / "report"),
    )

    assert outcome.session is not None
    assert outcome.session.session_id == GROUP_SESSION_ID
    assert runtime.acquired == ["gen-0001"]
    assert runtime.cleaned == ["gen-0001"]


def test_malformed_staging_database_is_rejected_and_cleaned(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    snapshot.write_bytes(b"fictional malformed sqlite image")
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = QQDirectDatabaseImportService(runtime_client=runtime)

    with pytest.raises(QQDirectSnapshotInvalid):
        with service.acquired_session(GROUP_SESSION_ID):
            pass

    assert runtime.cleaned == ["gen-0001"]


def test_group_sender_outside_current_member_list_stays_unknown(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    service, runtime, _ = _service(tmp_path)
    runtime.get_group_member_all = lambda _group_code: {
        "result": {"infos": {
            "other-member": {
                "uin": "other-member", "cardName": "Other Current Member", "nick": "Other Nick"
            }
        }}
    }

    class MetadataProvider:
        def list_groups(self, *, page=1, limit=200, timeout=None):
            return []

        def list_friends(self, *, page=1, limit=200, timeout=None):
            return [SimpleNamespace(
                peer_uin="fictional-group-sender", display_name="Fictional Friend Remark"
            )]

    service._provider_factory = SimpleNamespace(create=lambda: MetadataProvider())
    with service.acquired_session(GROUP_SESSION_ID) as acquisition:
        payload = json.loads(acquisition.payload_path.read_text(encoding="utf-8"))

    assert "sender" not in payload["records"][0]
    messages, _ = parse_qq_db_rich_messages(payload)
    assert messages[0].sender.display_name == "\u672a\u77e5\u6210\u5458"
    assert (
        "[qq-direct-identity-coverage] status=ok distinct_sender_count=1 "
        "rpc_member_count=1 matched_sender_count=0 unmatched_sender_count=1 "
        "matched_with_card_count=0 matched_without_card_with_nick_count=0 "
        "matched_without_card_or_nick_count=0 rpc_member_with_card_count=1 "
        "rpc_member_with_nick_count=1"
    ) in caplog.text
    assert "fictional-group-sender" not in caplog.text
    assert "Fictional Friend Remark" not in caplog.text


def test_group_report_member_contract_keeps_self_and_departed_messages(tmp_path: Path) -> None:
    service, runtime, snapshot = _service(tmp_path)
    senders = [FICTIONAL_UIN, "fictional-group-sender", "nick-only", "nameless", "departed"]
    texts = ["今天准备去公园散步顺便欣赏花朵", "周末一起研究新的数学题目吧",
             "刚刚读完一本关于旅行的有趣小说", "晚上想尝试做一道番茄鸡蛋汤",
             "以前大家一起讨论音乐的时光很快乐"]
    with sqlite3.connect(snapshot) as connection:
        connection.execute('DELETE FROM group_msg_table')
        connection.executemany(
            'INSERT INTO group_msg_table VALUES (?, ?, ?, ?, ?, ?)',
            [(101 + index, "group-partition", "fictional-group", sender,
              1760000001 + index, _text_blob(texts[index]))
             for index, sender in enumerate(senders)],
        )
    runtime.get_group_member_all = lambda _group_code: {"result": {"infos": {
        "self-key": {"uin": FICTIONAL_UIN, "cardName": "Self Group Card", "nick": "Self Nick"},
        "card-key": {"uin": senders[1], "cardName": "Fictional Group Card", "nick": "Fictional Nick"},
        "nick-key": {"uin": "nick-only", "cardName": "", "nick": "Fictional Nick Only"},
        "unknown-key": {"uin": "nameless", "cardName": "", "nick": "",
                        "remark": "RPC Friend Remark"},
    }}}

    class MetadataProvider:
        def list_groups(self, *, page=1, limit=200, timeout=None):
            return []

        def list_friends(self, *, page=1, limit=200, timeout=None):
            return [SimpleNamespace(peer_uin=sender, display_name="Fictional Friend Remark")
                    for sender in senders]

    service._provider_factory = SimpleNamespace(create=lambda: MetadataProvider())
    outcome = _facade(service).analyze_session(
        ChatSource.QQ, GROUP_SESSION_ID,
        AnalysisConfig(output_directory=tmp_path / "report"),
    )
    profiles = outcome.result.reports.user_profiles
    assert profiles.total_message_count == 5
    expected_names = {
        FICTIONAL_UIN: "我",
        "fictional-group-sender": "Fictional Group Card",
        "nick-only": "Fictional Nick Only",
        "nameless": "未知成员",
        "departed": "未知成员",
    }
    assert {profile.speaker_key: profile.resolved_display_name for profile in profiles.profiles} == expected_names
    assert all(profile.message_count == 1 for profile in profiles.profiles)
    report = json.loads((tmp_path / "report/echo-report.json").read_text(encoding="utf-8"))
    assert {member["speaker_key"]: member["display_name"] for member in report["members"]} == expected_names
    assert report["overview"]["total_message_count"] == 5
    assert (tmp_path / "report/echo-report.html").is_file()
    assert outcome.result.status.value == "completed"
    assert runtime.acquired == runtime.cleaned == ["gen-0001"]


def test_second_analyze_acquire_failure_does_not_fallback(tmp_path: Path) -> None:
    snapshot = tmp_path / "source.db"
    _create_snapshot(snapshot)
    runtime = FakeSnapshotRuntime(tmp_path, snapshot_path=snapshot)
    service = QQDirectDatabaseImportService(runtime_client=runtime)
    facade = _facade(service)

    facade.analyze_session(
        ChatSource.QQ, GROUP_SESSION_ID,
        AnalysisConfig(output_directory=tmp_path / "report-1"),
    )

    runtime.acquire_error = QQSnapshotRuntimeUnavailable()

    with pytest.raises(Exception) as captured:
        facade.analyze_session(
            ChatSource.QQ, GROUP_SESSION_ID,
            AnalysisConfig(output_directory=tmp_path / "report-2"),
        )

    assert getattr(captured.value, "code", None) == "qq_direct_snapshot_acquire_failed"
    assert runtime.acquired == ["gen-0001"]


# ------------------------------------------------------------------ wiring


def test_production_service_builds_runtime_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI composition's default service really gets a runtime client."""
    from qq_chat_analyzer.application.qq.qq_environment_config import (
        QQEnvironmentConfig,
        QQEnvironmentConfigLoader,
    )
    from qq_chat_analyzer.gui.app import _optional_qq_service
    from qq_chat_analyzer.providers.qq_direct_snapshot_runtime import (
        QQDirectSnapshotRuntimeClient,
    )

    service = _optional_qq_service(provider_factory=SimpleNamespace(create=lambda: SimpleNamespace()))
    assert isinstance(service, QQDirectDatabaseImportService)

    config = QQEnvironmentConfig(
        runtime_directory=tmp_path / "runtime" / "qq",
        runtime_mode="custom",
        napcat_bridge_url="http://127.0.0.1:40654",
    )
    monkeypatch.setattr(
        QQEnvironmentConfigLoader,
        "load_or_default",
        lambda self: config,
    )

    client = service._require_runtime_client()
    assert isinstance(client, QQDirectSnapshotRuntimeClient)
