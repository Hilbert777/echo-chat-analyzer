"""Behavioral tests for the WeChat database source (WCDB provider chain).

All fixtures are fictional. No real WeChat data, key, or account appears here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
import zstandard as zstd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from qq_chat_analyzer import wechat_db_adapter
from qq_chat_analyzer.application import ImportRequest, ImportService
from qq_chat_analyzer.application.import_service import WECHAT_DB_FORMAT
from qq_chat_analyzer.legacy_projection import project_legacy_message
from qq_chat_analyzer.rich_message import (
    EXPRESSION_KIND_PLATFORM_FACE,
    ExpressionContent,
    TextContent,
)
from qq_chat_analyzer.providers.wechat_database_provider import (
    DatabaseNotFound,
    DatabaseUnreadable,
    KeyUnavailable,
    QueryFailed,
    WeChatDatabaseProvider,
    message_table_name,
    _sanitize_wcdb_message,
)
from qq_chat_analyzer.providers import wechat_database_provider


TEXT_LOCAL_TYPE = 1
IMAGE_LOCAL_TYPE = 3
STICKER_LOCAL_TYPE = 47
FICTIONAL_KEY = "a" * 64
FICTIONAL_SESSION = "wxid_fictional_room@chatroom"


def _db_row(
    message_content: str = "\u4f60\u597d\uff0c\u4eca\u5929\u5929\u6c14\u4e0d\u9519",
    local_type: object = TEXT_LOCAL_TYPE,
    create_time: object = 1753412807,
    user_name: object = "wxid_fictional_sender",
    server_id: object = 900001,
    local_id: object = 11,
) -> dict[str, object]:
    return {
        "local_id": local_id,
        "server_id": server_id,
        "local_type": local_type,
        "create_time": create_time,
        "message_content": message_content,
        "user_name": user_name,
    }


def _write_db_export(
    path: Path,
    rows: list[object],
    username: str = FICTIONAL_SESSION,
) -> None:
    document = {
        "source": "wechat-db",
        "conversation": {"username": username},
        "messages": rows,
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


class _FakeCompleted:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _helper_result(rows: list[object], columns: list[str] | None = None) -> str:
    return json.dumps(
        {
            "ok": True,
            "columns": columns or ["local_id"],
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
        }
    )


def _schema_result(columns: list[str]) -> str:
    pragma_rows = [
        {"cid": index, "name": name, "type": "TEXT", "notnull": 0, "dflt_value": None, "pk": 0}
        for index, name in enumerate(columns)
    ]
    return _helper_result(
        pragma_rows,
        columns=["cid", "name", "type", "notnull", "dflt_value", "pk"],
    )


def _make_data_root(
    tmp_path: Path,
    table: str | None = None,
    account_name: str = "wxid_owner",
) -> Path:
    """Build a fictional WeChat data directory layout."""
    storage = tmp_path / "xwechat_files" / account_name / "db_storage"
    message_dir = storage / "message"
    message_dir.mkdir(parents=True, exist_ok=True)
    (message_dir / "session.db").write_bytes(b"fake")
    (message_dir / "message_0.db").write_bytes(b"fake")
    return tmp_path / "xwechat_files"


def _provider(
    tmp_path: Path,
    runner,
    account_name: str = "wxid_owner",
) -> WeChatDatabaseProvider:
    helper = tmp_path / "wcdb_cli.exe"
    helper.write_bytes(b"fake")
    library = tmp_path / "WCDB.dll"
    library.write_bytes(b"fake")
    return WeChatDatabaseProvider(
        data_root=_make_data_root(tmp_path, account_name=account_name),
        db_key=FICTIONAL_KEY,
        wcdb_cli_path=helper,
        wcdb_dll_path=library,
        runner=runner,
    )


# ------------------------------------------------------------------ adapter


def test_private_pat_queries_and_time_bounds_use_the_exact_supported_type(tmp_path):
    """Execute acquisition SQL, including its date bounds, on fictional rows."""
    def runner(command, timeout, environment):
        database = Path(command[command.index("--db") + 1])
        sql = command[command.index("--sql") + 1]
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(sql)]
        return _FakeCompleted(stdout=_helper_result(rows))

    provider = _provider(tmp_path, runner)
    database = tmp_path / "xwechat_files" / "wxid_owner" / "db_storage" / "message" / "message_0.db"
    database.write_bytes(b"")  # Replace only this helper's fictional placeholder.
    private, group = "wxid_fictional_peer", "fictional-room@chatroom"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
        connection.execute("INSERT INTO Name2Id VALUES ('wxid_owner')")
        for session in (private, group):
            table = message_table_name(session)
            connection.execute(f'CREATE TABLE "{table}" (local_id INTEGER, server_id INTEGER, '
                               'local_type INTEGER, create_time INTEGER, message_content TEXT, '
                               'WCDB_CT_message_content INTEGER, real_sender_id INTEGER)')
            connection.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, 0, 1)', [
                (1, 101, 266287972401, 10, "fictional-pat"),
                (2, 102, 1, 20, "fictional-text"),
                (3, 103, 47, 21, "fictional-sticker"),
                (4, 104, 266287972401, 30, "fictional-pat"),
                (5, 105, 49, 5, "unsupported-app"),
                (6, 106, (63 << 32) | 49, 40, "unsupported-app-subtype"),
            ])
    assert [row["server_id"] for row in provider.read_session_rows(private)] == [101, 102, 103, 104]
    assert [row["server_id"] for row in provider.read_session_rows(private, 15, 25)] == [102, 103]
    assert provider.read_session_time_bounds(private) == [{"min_time": 10, "max_time": 30}]
    assert [row["server_id"] for row in provider.read_session_rows(group)] == [102, 103]
    assert provider.read_session_time_bounds(group) == [{"min_time": 20, "max_time": 21}]


def _counting_session_provider(tmp_path, shards, usernames, faults=None, native_cli=None):
    """Execute provider SQL on fictional SQLite DBs at the helper boundary."""
    queries = []
    faults = faults or {}

    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        database = Path(command[command.index("--db") + 1])
        queries.append((database.name, sql))
        assert "message_content" not in sql
        if native_cli is not None:
            command = list(command)
            command[0] = str(native_cli)
            command[command.index("--wcdb") + 1] = str(native_cli.with_name("WCDB.dll"))
            command.append("--no-cipher")
            return subprocess.run(
                command, timeout=timeout, env=environment, capture_output=True,
                text=True, encoding="utf-8",
            )
        fault = faults.get(database.name)
        if (fault == "inventory" and "sqlite_master" in sql) or (
            fault == "count" and "COUNT(*)" in sql
        ):
            return _FakeCompleted(stdout=json.dumps({"ok": False}))
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(sql)]
        if "COUNT(*)" in sql:
            if fault == "omit":
                rows = rows[1:]
            elif fault == "invalid":
                rows[0]["message_count"] = -1
            elif fault == "duplicate":
                rows.append(rows[0])
        return _FakeCompleted(stdout=_helper_result(rows))

    provider = _provider(tmp_path, runner)
    directory = tmp_path / "xwechat_files" / "wxid_owner" / "db_storage" / "message"
    for database in directory.glob("*.db"):
        database.unlink()  # Only this helper's fictional placeholders.
    with closing(sqlite3.connect(directory / "session.db")) as connection, connection:
        connection.execute("CREATE TABLE SessionTable (username TEXT, last_timestamp INTEGER)")
        connection.executemany(
            "INSERT INTO SessionTable VALUES (?, ?)",
            [(username, 100 + index) for index, username in enumerate(usernames)],
        )
    for index, tables in enumerate(shards):
        with closing(sqlite3.connect(directory / f"message_{index}.db")) as connection, connection:
            for username, count in tables.items():
                table = message_table_name(username)
                connection.execute(f'CREATE TABLE "{table}" (local_type INTEGER)')
                connection.executemany(
                    f'INSERT INTO "{table}" VALUES (?)',
                    [(1 if row % 2 else 3,) for row in range(count)],
                )
    return provider, queries


def test_session_counts_sum_shards_include_all_types_and_reach_facade(tmp_path):
    from qq_chat_analyzer.application.facade import ChatAnalyzerFacade, ChatSource
    from qq_chat_analyzer.application.wechat.wechat_export_import_service import WeChatExportImportService

    provider, queries = _counting_session_provider(
        tmp_path,
        [{"wxid_alpha": 3, "wxid_empty": 0}, {"wxid_alpha": 4, "wxid_beta": 2}, {}],
        ["wxid_alpha", "wxid_empty", "wxid_beta", "wxid_missing"],
    )
    facade = ChatAnalyzerFacade(wechat_service=WeChatExportImportService(provider=provider))
    sessions = facade.list_sessions(ChatSource.WECHAT)
    by_id = {session.session_id: session for session in sessions}
    assert by_id["wxid_alpha"].message_count == 7
    assert by_id["wxid_beta"].message_count == 2
    assert by_id["wxid_empty"].message_count == 0
    assert by_id["wxid_empty"].message_available is True
    assert by_id["wxid_missing"].message_count is None
    assert by_id["wxid_missing"].message_available is False
    assert by_id["wxid_alpha"].last_message_time == 100
    assert len([sql for _, sql in queries if "sqlite_master" in sql]) == 3
    assert len([sql for _, sql in queries if "COUNT(*)" in sql]) == 2


@pytest.mark.parametrize("rows, expected", [
    ([[(1, 1_700_000_100), (3, 1)], [(47, 1_700_000_000_000), (1, 1_700_000_200)]],
     (1_700_000_000, 1_700_000_200)),
    ([[(1, 0), (1, -1), (47, None)], []], None),
    ([[], []], None),
    ([[(1, "1700000000"), (1, "invalid"), (1, float("inf"))],
      [(0x100000001, "2023-11-15T00:00:00+08:00"), (47, 0.5)]],
     (0, 1_700_000_000)),
    ([[(0x100000001, 1_700_000_000_000), (3, 1)],
      [(0x10000002F, "2023-11-16T00:00:00+08:00"), (1, -1)]],
     (1_700_000_000, 1_700_064_000)),
])
def test_session_time_range_aggregates_all_shards_without_message_bodies(tmp_path, rows, expected):
    from qq_chat_analyzer.application.facade import ChatAnalyzerFacade, ChatSource
    from qq_chat_analyzer.application.wechat.wechat_export_import_service import WeChatExportImportService

    provider, queries = _counting_session_provider(
        tmp_path, [{FICTIONAL_SESSION: 0} for _ in rows], [FICTIONAL_SESSION],
    )
    directory = tmp_path / "xwechat_files" / "wxid_owner" / "db_storage" / "message"
    table = message_table_name(FICTIONAL_SESSION)
    for index, shard_rows in enumerate(rows):
        with closing(sqlite3.connect(directory / f"message_{index}.db")) as connection, connection:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN create_time')
            connection.executemany(f'INSERT INTO "{table}" VALUES (?, ?)', shard_rows)
    facade = ChatAnalyzerFacade(wechat_service=WeChatExportImportService(provider=provider))

    assert facade.get_session_message_range(ChatSource.WECHAT, FICTIONAL_SESSION) == expected
    assert facade.get_session_message_range(ChatSource.WECHAT, "") is None
    assert facade.get_session_message_range(ChatSource.WECHAT, "wxid_missing") is None
    range_queries = [sql for _, sql in queries if "MIN(" in sql]
    assert len(range_queries) == len(rows)
    assert all("Name2Id" not in sql for _, sql in queries)


@pytest.mark.parametrize("fault", ["inventory", "count", "duplicate"])
def test_session_counts_never_publish_partial_or_invalid_totals(tmp_path, fault):
    provider, _ = _counting_session_provider(
        tmp_path, [{"wxid_alpha": 3}, {"wxid_alpha": 4}], ["wxid_alpha"],
        faults={"message_1.db": fault},
    )
    session = provider.list_sessions()[0]
    assert session.message_count is None
    assert session.message_available is True


@pytest.mark.slow_integration
def test_session_counts_native_wcdb_batches_and_shards(tmp_path):
    configured = os.environ.get("ECHO_NATIVE_WCDB_CLI_PATH")
    if not configured:
        pytest.skip("ECHO_NATIVE_WCDB_CLI_PATH is required for native counting")
    native_cli = Path(configured)
    assert native_cli.is_file() and native_cli.with_name("WCDB.dll").is_file()
    usernames = [f"wxid_fictional_{index}" for index in range(205)]
    provider, queries = _counting_session_provider(
        tmp_path, [dict.fromkeys(usernames, 2), dict.fromkeys(usernames, 3)],
        usernames, native_cli=native_cli,
    )
    sessions = provider.list_sessions(limit=300)
    assert len(sessions) == 205
    assert all(session.message_count == 5 for session in sessions)
    assert len([sql for _, sql in queries if "COUNT(*)" in sql]) <= 6


def test_text_row_becomes_rich_message_then_projects_to_legacy(
    tmp_path: Path,
) -> None:
    export = tmp_path / "session.json"
    row = _db_row()
    row["sender_name"] = "Fictional Alice"
    _write_db_export(export, [row])

    rich_messages = wechat_db_adapter.parse_rich_messages(
        wechat_db_adapter.messages_from_payload(json.loads(export.read_text(encoding="utf-8")))
    )

    assert len(rich_messages) == 1
    rich_message = rich_messages[0]
    assert rich_message.source == "wechat"
    assert rich_message.message_id == "900001"
    assert rich_message.conversation_id == FICTIONAL_SESSION
    assert rich_message.sender.identity_id == "wxid_fictional_sender"
    assert rich_message.sender.display_name == "Fictional Alice"
    assert rich_message.timestamp == 1753412807
    assert rich_message.message_type == "text"
    assert rich_message.contents == (
        TextContent(text="\u4f60\u597d\uff0c\u4eca\u5929\u5929\u6c14\u4e0d\u9519"),
    )
    assert rich_message.relations == ()
    assert rich_message.recall_state is None

    legacy_message = project_legacy_message(rich_message)
    assert legacy_message.sender == "Fictional Alice"
    assert legacy_message.text == "\u4f60\u597d\uff0c\u4eca\u5929\u5929\u6c14\u4e0d\u9519"
    assert legacy_message.timestamp == 1753412807


@pytest.mark.parametrize("compressed", [False, True])
def test_group_text_sender_header_is_metadata_not_authored_original(tmp_path, compressed):
    sender = "wxid_fictional_sender"
    body = "周末去公园散步[微笑]\n原话里的换行和  空格都保留。"
    content = f"{sender}:\n{body}"
    row = _db_row(message_content=content)
    row["sender_name"] = "Fictional Alice"
    if compressed:
        row["message_content"] = zstd.ZstdCompressor().compress(content.encode("utf-8")).hex()
        row["WCDB_CT_message_content"] = 4
    export = tmp_path / "sender-header.json"
    _write_db_export(export, [row])
    outcome = ImportService().execute(ImportRequest(input_path=export, platform="wechat"))
    assert outcome.messages[0].text == body
    assert outcome.messages[0].sender == "Fictional Alice"
    assert outcome.messages[0].sender_id == sender
    rich = outcome.rich_message_pairs[0][1]
    assert next(item.text for item in rich.contents if isinstance(item, TextContent)) == body
    assert all(sender not in item.text_before for item in rich.contents if isinstance(item, ExpressionContent))


@pytest.mark.parametrize("kind,is_self,body", [
    ("private", False, "wxid_fictional_sender:\n用户写下的账号示例"),
    ("unknown", False, "wxid_fictional_sender:\n不能确认属于群聊"),
    ("group", True, "wxid_fictional_sender:\n本人写下的账号示例"),
    ("group", False, "wxid_fictional_other:\n另一个人的账号原话"),
])
def test_text_header_normalization_preserves_unproven_or_authored_prefixes(kind, is_self, body):
    row = _db_row(message_content=body)
    row["conversation_type"] = kind
    row["self_username"] = "wxid_fictional_sender" if is_self else "wxid_fictional_owner"
    rich = wechat_db_adapter.parse_rich_message(row)
    assert project_legacy_message(rich).text == body


def test_sticker_row_becomes_rich_expression_and_legacy_message(
    tmp_path: Path,
) -> None:
    export = tmp_path / "session.json"
    row = _db_row(
        local_type=STICKER_LOCAL_TYPE,
        server_id=900047,
        message_content=(
            '<msg><emoji md5="fictionalmd5abcdef" '
            'cdnurl="fictional-cdn-url"/></msg>'
        ),
    )
    row["sender_name"] = "Fictional Alice"
    _write_db_export(export, [row])

    rich_messages = wechat_db_adapter.parse_rich_messages(
        wechat_db_adapter.messages_from_payload(json.loads(export.read_text(encoding="utf-8")))
    )

    assert len(rich_messages) == 1
    rich_message = rich_messages[0]
    assert rich_message.message_type == "sticker"
    assert rich_message.contents == (
        ExpressionContent(
            expression_kind="sticker",
            expression_key="fictionalmd5abcdef",
            sticker=wechat_db_adapter.StickerIdentity(
                classification_basis="wechat_type_47", identity_basis="unresolved",
            ),
            display_text="[贴图]",
            source="wechat",
            position=0,
        ),
    )
    legacy = project_legacy_message(rich_message)
    assert legacy.text == ""
    assert legacy.message_type == "sticker"


FICTIONAL_STICKER_DIGEST = "0123456789abcdef0123456789abcdef"
FICTIONAL_STICKER_XML = (
    f'<msg><emoji md5="{FICTIONAL_STICKER_DIGEST.upper()}" '
    'cdnurl="https://fictional.invalid/never-fetch"/></msg>'
)


@pytest.mark.parametrize(("primary", "android", "expected"), [
    (None, FICTIONAL_STICKER_DIGEST.upper(), FICTIONAL_STICKER_DIGEST),
    (None, "not-a-digest", None),
    ("malformed", FICTIONAL_STICKER_DIGEST, None),
    ("a" * 32, FICTIONAL_STICKER_DIGEST, "a" * 32),
])
def test_wechat_android_digest_identifies_material_only_when_primary_is_absent(primary, android, expected):
    attributes = f'androidmd5="{android}"'
    if primary is not None:
        attributes += f' md5="{primary}"'
    message = wechat_db_adapter.parse_rich_message(_db_row(
        local_type=STICKER_LOCAL_TYPE, server_id=900047,
        message_content=f'<msg><emoji {attributes}/></msg>',
    ))
    expression = message.contents[0]
    assert expression.sticker.content_md5 == expected
    assert expression.sticker.identity_key == (f"wechat:md5:{expected}" if expected else None)
    assert expression.expression_key == (primary or "900047")


@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "zstd"])
def test_group_sticker_strips_only_confirmed_sender_prefix(
    tmp_path: Path, compressed: bool
) -> None:
    """WeChat 4.x group rows prefix another member's payload with its username."""
    export = tmp_path / "session.json"
    content = f"wxid_fictional_sender:\n{FICTIONAL_STICKER_XML}"
    row = _db_row(
        local_type=STICKER_LOCAL_TYPE,
        server_id=900047,
        message_content=(
            zstd.ZstdCompressor().compress(content.encode()).hex() if compressed else content
        ),
    )
    if compressed:
        row["WCDB_CT_message_content"] = 4
    _write_db_export(export, [row])

    rich_messages = wechat_db_adapter.parse_rich_messages(
        wechat_db_adapter.messages_from_payload(json.loads(export.read_text(encoding="utf-8")))
    )

    assert len(rich_messages) == 1
    sticker = rich_messages[0].contents[0]
    assert sticker.sticker.identity_basis == "wechat_xml_md5"
    assert sticker.sticker.content_md5 == FICTIONAL_STICKER_DIGEST
    assert sticker.sticker.identity_key == f"wechat:md5:{FICTIONAL_STICKER_DIGEST}"
    # The legacy expression key keeps the source attribute's case; only the
    # material identity is normalized.
    assert sticker.expression_key.lower() == FICTIONAL_STICKER_DIGEST


def test_group_sticker_prefix_of_another_sender_stays_unresolved(
    tmp_path: Path,
) -> None:
    """A header that is not this row's confirmed sender is never stripped."""
    export = tmp_path / "session.json"
    _write_db_export(
        export,
        [
            _db_row(
                local_type=STICKER_LOCAL_TYPE,
                server_id=900048,
                message_content=f"wxid_fictional_other:\n{FICTIONAL_STICKER_XML}",
            )
        ],
    )

    rich_messages = wechat_db_adapter.parse_rich_messages(
        wechat_db_adapter.messages_from_payload(json.loads(export.read_text(encoding="utf-8")))
    )

    sticker = rich_messages[0].contents[0]
    assert sticker.sticker.identity_basis == "unresolved"
    assert sticker.sticker.identity_key is None
    assert sticker.sticker.content_md5 is None
    assert sticker.expression_key == "900048"


def test_group_sticker_prefix_does_not_relax_single_emoji_rule(
    tmp_path: Path,
) -> None:
    """Stripping the header must not make an ambiguous XML resolve."""
    export = tmp_path / "session.json"
    ambiguous = (
        f'<msg><emoji md5="{FICTIONAL_STICKER_DIGEST}"/>'
        f'<emoji md5="{FICTIONAL_STICKER_DIGEST}"/></msg>'
    )
    _write_db_export(
        export,
        [
            _db_row(
                local_type=STICKER_LOCAL_TYPE,
                server_id=900049,
                message_content=f"wxid_fictional_sender:\n{ambiguous}",
            )
        ],
    )

    rich_messages = wechat_db_adapter.parse_rich_messages(
        wechat_db_adapter.messages_from_payload(json.loads(export.read_text(encoding="utf-8")))
    )

    sticker = rich_messages[0].contents[0]
    assert sticker.sticker.identity_basis == "unresolved"
    assert sticker.sticker.identity_key is None


def test_malformed_rows_do_not_drop_valid_neighbours(tmp_path: Path) -> None:
    export = tmp_path / "session.json"
    _write_db_export(export, [_db_row(create_time=None), _db_row(), "junk"])

    messages = wechat_db_adapter.parse_messages(
        wechat_db_adapter.messages_from_payload(json.loads(export.read_text(encoding="utf-8")))
    )

    assert len(messages) == 1


def test_row_username_takes_precedence_over_conversation(tmp_path: Path) -> None:
    export = tmp_path / "session.json"
    row = _db_row()
    row["username"] = "wxid_explicit_conversation"
    _write_db_export(export, [row])

    messages = wechat_db_adapter.parse_messages(
        wechat_db_adapter.messages_from_payload(json.loads(export.read_text(encoding="utf-8")))
    )

    assert messages[0].conversation_id == "wxid_explicit_conversation"


# ----------------------------------------------------------------- provider


@pytest.mark.parametrize("payload, limit, expected", [
    pytest.param({"ok": True}, 0, None, id="missing-rows"),
    pytest.param({"ok": True, "rows": {}}, 0, None, id="wrong-rows-type"),
    pytest.param({"ok": True, "rows": [], "truncated": True}, 0, None, id="unlimited-truncated"),
    pytest.param({"ok": True, "rows": [], "truncated": True}, -1, None, id="negative-limit-truncated"),
    pytest.param({"ok": True, "rows": []}, 0, [], id="empty-result"),
    pytest.param({"ok": True, "rows": [{"name": "fictional-table"}]}, 0,
                 [{"name": "fictional-table"}], id="nonempty-result"),
    pytest.param({"ok": True, "rows": [{"name": "fictional-table"}], "truncated": True}, 1,
                 [{"name": "fictional-table"}], id="limited-truncated"),
])
def test_query_validates_success_payload(tmp_path, payload, limit, expected):
    commands = []

    def runner(command, timeout, environment):
        commands.append(command)
        return _FakeCompleted(stdout=json.dumps(payload))

    provider = _provider(tmp_path, runner)
    database = tmp_path / "xwechat_files/wxid_owner/db_storage/message/message_0.db"
    if expected is None:
        with pytest.raises(QueryFailed):
            provider._query(database, "SELECT name FROM sqlite_master", limit=limit)
    else:
        assert provider._query(database, "SELECT name FROM sqlite_master", limit=limit) == expected
    if limit > 0:
        assert commands[0][-2:] == ["--limit", str(limit)]
    else:
        assert "--limit" not in commands[0]


def test_list_sessions_returns_privacy_safe_descriptors(tmp_path: Path) -> None:
    rows = [
        {"username": FICTIONAL_SESSION, "last_timestamp": 1753412900},
        {"username": "wxid_friend", "last_timestamp": 1753412800},
        {"username": "   ", "last_timestamp": 1},
        "junk",
    ]

    def runner(command, timeout, environment):
        sql = " ".join(command)
        if "PRAGMA table_info" in sql:
            return _FakeCompleted(
                stdout=_schema_result(["username", "summary", "last_timestamp"])
            )
        if "SessionTable" in sql:
            return _FakeCompleted(stdout=_helper_result(rows))
        if "sqlite_master" in sql:
            return _FakeCompleted(
                stdout=_helper_result([], columns=["name"])
            )
        raise AssertionError(f"unexpected query: {sql}")

    sessions = _provider(tmp_path, runner).list_sessions()

    assert [session.session_id for session in sessions] == [
        FICTIONAL_SESSION,
        "wxid_friend",
    ]
    assert [session.session_type for session in sessions] == [
        "group",
        "private",
    ]
    assert [getattr(session, "last_timestamp", None) for session in sessions] == [
        1753412900, 1753412800,
    ]


def test_export_session_json_writes_self_username_from_account_dir(
    tmp_path: Path,
) -> None:
    table = message_table_name(FICTIONAL_SESSION)

    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        if "FROM contact" in sql:
            return _FakeCompleted(stdout=_helper_result([]))
        if "sqlite_master" in sql:
            return _FakeCompleted(stdout=_helper_result([{"name": table}]))
        return _FakeCompleted(stdout=_helper_result([_db_row()]))

    provider = _provider(tmp_path, runner)
    output = tmp_path / "out" / "session.json"

    provider.export_session_json(FICTIONAL_SESSION, output)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["conversation"]["self_username"] == "wxid_owner"


def test_contact_aliases_survive_export_and_import(tmp_path):
    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        if "FROM contact" in sql:
            return _FakeCompleted(stdout=_helper_result([{
                "username": "wxid_fictional_sender", "remark": "虚构备注", "nick_name": "虚构昵称"}]))
        if "sqlite_master" in sql:
            return _FakeCompleted(stdout=_helper_result([{"name": message_table_name(FICTIONAL_SESSION)}]))
        return _FakeCompleted(stdout=_helper_result([_db_row(user_name="wxid_fictional_sender")]))
    provider = _provider(tmp_path, runner)
    contact = tmp_path / "xwechat_files/wxid_owner/db_storage/contact/contact.db"
    contact.parent.mkdir(parents=True, exist_ok=True)
    contact.write_bytes(b"fictional-contact-placeholder")
    identity = provider._contact_identities()["wxid_fictional_sender"]
    assert (identity.remark, identity.nickname) == ("虚构备注", "虚构昵称")
    output = provider.export_session_json(FICTIONAL_SESSION, tmp_path / "fictional.json")
    outcome = ImportService().execute(ImportRequest(input_path=output))
    assert (outcome.messages[0].sender_remark, outcome.messages[0].sender_nickname) == ("虚构备注", "虚构昵称")


def test_resolve_self_username_canonicalizes_account_directory_suffix(
    tmp_path: Path,
) -> None:
    base_identity = "wxid_fictional_owner"
    account_directory = f"{base_identity}_a1b2c3d4"
    table = message_table_name(FICTIONAL_SESSION)

    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        if "FROM contact" in sql:
            return _FakeCompleted(
                stdout=_helper_result(
                    [{"username": base_identity}],
                    columns=["username"],
                )
            )
        if "sqlite_master" in sql:
            return _FakeCompleted(stdout=_helper_result([{"name": table}]))
        return _FakeCompleted(
            stdout=_helper_result([_db_row(user_name=base_identity)])
        )

    provider = _provider(
        tmp_path,
        runner,
        account_name=account_directory,
    )
    contact_dir = (
        tmp_path
        / "xwechat_files"
        / account_directory
        / "db_storage"
        / "contact"
    )
    contact_dir.mkdir(parents=True, exist_ok=True)
    (contact_dir / "contact.db").write_bytes(b"fake")

    assert provider._resolve_self_username() == base_identity

    output = tmp_path / "out" / "session.json"
    provider.export_session_json(FICTIONAL_SESSION, output)
    outcome = ImportService().execute(ImportRequest(input_path=output))

    assert outcome.messages[0].sender_id == base_identity
    assert outcome.messages[0].is_self is True


def test_resolve_self_username_keeps_unconfirmed_hex_ending_username(
    tmp_path: Path,
) -> None:
    legal_identity = "wxid_fictional_feed"

    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        if "FROM contact" in sql:
            return _FakeCompleted(
                stdout=_helper_result(
                    [{"username": legal_identity}],
                    columns=["username"],
                )
            )
        return _FakeCompleted(stdout=_helper_result([]))

    provider = _provider(
        tmp_path,
        runner,
        account_name=legal_identity,
    )
    contact_dir = (
        tmp_path
        / "xwechat_files"
        / legal_identity
        / "db_storage"
        / "contact"
    )
    contact_dir.mkdir(parents=True, exist_ok=True)
    (contact_dir / "contact.db").write_bytes(b"fake")

    assert provider._resolve_self_username() == legal_identity


def test_exported_self_username_marks_db_messages(tmp_path: Path) -> None:
    table = message_table_name(FICTIONAL_SESSION)

    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        if "FROM contact" in sql:
            return _FakeCompleted(stdout=_helper_result([]))
        if "sqlite_master" in sql:
            return _FakeCompleted(stdout=_helper_result([{"name": table}]))
        return _FakeCompleted(
            stdout=_helper_result(
                [
                    _db_row(user_name="wxid_owner", message_content="mine"),
                    _db_row(
                        user_name="wxid_fictional_sender",
                        message_content="theirs",
                    ),
                ]
            )
        )

    provider = _provider(tmp_path, runner)
    output = tmp_path / "out" / "session.json"
    provider.export_session_json(FICTIONAL_SESSION, output)

    outcome = ImportService().execute(ImportRequest(input_path=output))

    assert [message.is_self for message in outcome.messages] == [True, False]


def test_time_window_is_pushed_into_the_query(tmp_path: Path) -> None:
    table = message_table_name(FICTIONAL_SESSION)
    seen_sql: list[str] = []

    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        seen_sql.append(sql)
        if "sqlite_master" in sql:
            return _FakeCompleted(stdout=_helper_result([{"name": table}]))
        return _FakeCompleted(stdout=_helper_result([]))

    provider = _provider(tmp_path, runner)
    provider.read_session_rows(
        FICTIONAL_SESSION,
        start_time=1753400000,
        end_time=1753500000,
    )

    message_sql = seen_sql[-1]
    assert "create_time >= 1753400000" in message_sql
    assert "create_time <= 1753500000" in message_sql


def test_key_is_never_placed_on_the_command_line(tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def runner(command, timeout, environment):
        captured.append(list(command))
        sql = " ".join(command)
        if "PRAGMA table_info" in sql:
            return _FakeCompleted(
                stdout=_schema_result(["username", "summary", "last_timestamp"])
            )
        return _FakeCompleted(stdout=_helper_result([]))

    _provider(tmp_path, runner).list_sessions()

    assert FICTIONAL_KEY not in " ".join(captured[0])


# ----------------------------------------------------------- import service


# ------------------------------------------------- schema / diagnostic contracts


def test_sanitize_wcdb_message_redacts_path_key_and_sql() -> None:
    db_path = Path("C:/Users/Fake/AppData/secret/session.db")
    key = "a" * 64
    sql = "SELECT secret FROM SessionTable"
    raw = (
        f"cannot open {db_path} with sql {sql} and key {key} "
        "and some user data here"
    )
    cleaned = _sanitize_wcdb_message(raw, db_path, key, sql)
    assert str(db_path) not in cleaned
    assert key not in cleaned
    assert sql not in cleaned
    assert "user data here" in cleaned


# ------------------------------------------------- key/database verification


def _verify_runner(
    captured: list[list[str]] | None = None,
    *,
    rows: list[object] | None = None,
):
    def runner(command, timeout, environment):
        if captured is not None:
            captured.append(list(command))
        return _FakeCompleted(
            stdout=_helper_result(
                rows if rows is not None else [{"count(*)": 1}]
            )
        )

    return runner


def _fictional_session_db(tmp_path: Path) -> Path:
    return (
        _make_data_root(tmp_path)
        / "wxid_owner"
        / "db_storage"
        / "message"
        / "session.db"
    )


def test_verify_readable_uses_a_minimal_read_only_query(tmp_path: Path) -> None:
    captured: list[list[str]] = []

    _provider(tmp_path, _verify_runner(captured)).verify_readable()

    assert captured, "verification must reuse the existing wcdb query path"
    command = captured[0]
    sql = command[command.index("--sql") + 1]
    assert "sqlite_master" in sql
    assert "SessionTable" not in sql
    assert "Msg_" not in sql
    assert "message" not in sql.lower()
    assert command[command.index("--db") + 1].endswith("session.db")


def test_verify_readable_keeps_the_key_off_the_command_line(
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []
    seen: list[dict[str, str]] = []

    def runner(command, timeout, environment):
        captured.append(list(command))
        seen.append(dict(environment))
        return _FakeCompleted(stdout=_helper_result([{"count(*)": 1}]))

    _provider(tmp_path, runner).verify_readable()

    assert seen[0]["WX_DB_KEY"] == FICTIONAL_KEY
    assert all(FICTIONAL_KEY not in part for part in captured[0])


def test_verify_readable_raises_unreadable_when_the_key_cannot_open_the_db(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = FICTIONAL_KEY
    session_db = _fictional_session_db(tmp_path)

    def failing(command, timeout, environment):
        assert environment["WX_DB_KEY"] == secret_key
        return _FakeCompleted(
            stdout=json.dumps(
                {
                    "ok": False,
                    "stage": "prepare",
                    "error": "prepare failed",
                    "wcdb_error_code": 26,
                    "wcdb_error_ext_code": 1032,
                    "wcdb_error_message": (
                        f"file is not a database at {session_db} "
                        f"using key {secret_key}"
                    ),
                }
            ),
            returncode=1,
        )

    with caplog.at_level(
        "INFO",
        logger="qq_chat_analyzer.providers.wechat_database_provider",
    ):
        with pytest.raises(DatabaseUnreadable) as failure:
            _provider(tmp_path, failing).verify_readable()

    assert failure.value.code == "wechat_database_unreadable"
    assert failure.value.code != QueryFailed.code

    logs = caplog.text
    assert "wechat.database.verify success=false" in logs
    assert "wcdb_error_code=26" in logs
    assert str(session_db) not in logs
    assert secret_key not in logs


def test_verify_readable_fails_closed_when_no_row_is_returned(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(
        "INFO",
        logger="qq_chat_analyzer.providers.wechat_database_provider",
    ):
        with pytest.raises(DatabaseUnreadable):
            _provider(tmp_path, _verify_runner(rows=[])).verify_readable()

    assert "wechat.database.verify success=false" in caplog.text
    assert "error_type=EmptyResult" in caplog.text


# ============================================================
# Multi-shard / rollover test helpers
# ============================================================


@pytest.mark.parametrize("method", ["read_session_rows", "read_session_time_bounds"])
@pytest.mark.parametrize("corrupt", [False, True], ids=["absent-table", "broken-shard"])
def test_shard_lookup_distinguishes_absent_table_from_database_failure(tmp_path, method, corrupt):
    def runner(command, timeout, environment):
        database = command[command.index("--db") + 1]
        sql = command[command.index("--sql") + 1]
        try:
            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                rows = [dict(row) for row in connection.execute(sql)]
        except sqlite3.DatabaseError:
            return _FakeCompleted(stdout=json.dumps({"ok": False}), returncode=1)
        return _FakeCompleted(stdout=_helper_result(rows))

    provider = _provider(tmp_path, runner)
    directory = tmp_path / "xwechat_files/wxid_owner/db_storage/message"
    healthy = directory / "message_0.db"
    healthy.unlink()  # Replace only _provider's fictional placeholder.
    table = message_table_name(FICTIONAL_SESSION)
    with closing(sqlite3.connect(healthy)) as connection, connection:
        connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
        connection.execute(f'CREATE TABLE "{table}" (local_id INTEGER, server_id INTEGER, '
                           'local_type INTEGER, create_time INTEGER, message_content TEXT, '
                           'WCDB_CT_message_content INTEGER, real_sender_id INTEGER)')
        connection.execute(f'INSERT INTO "{table}" VALUES (1, 2, 1, 100, ?, 0, 1)',
                           ("fictional message",))
    other = directory / "message_1.db"
    if corrupt:
        other.write_bytes(b"fictional corrupt database")
        with pytest.raises(QueryFailed):
            getattr(provider, method)(FICTIONAL_SESSION)
    else:
        with closing(sqlite3.connect(other)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (value INTEGER)")
        rows = getattr(provider, method)(FICTIONAL_SESSION)
        if method == "read_session_rows":
            assert [row["message_content"] for row in rows] == ["fictional message"]
        else:
            assert rows == [{"min_time": 100, "max_time": 100}]

_ROLLOVER_TABLE = "Msg_0_fictional_rollover_session"
FICTIONAL_SESSION_FOR_ROLLOVER = "fictional_rollover_session"


@pytest.mark.parametrize("payload, stage", [
    pytest.param({"ok": True}, "lookup", id="missing-lookup-rows"),
    pytest.param({"ok": True, "rows": {}}, "messages", id="wrong-message-rows"),
    pytest.param({"ok": True, "rows": [_db_row()], "truncated": True}, "messages", id="truncated-messages"),
])
@pytest.mark.parametrize("operation", ["export", "range"])
def test_invalid_shard_payload_propagates_through_service(tmp_path, payload, stage, operation):
    from qq_chat_analyzer.application import WeChatExportImportRequest, WeChatExportImportService

    def runner(command, timeout, environment):
        sql = command[command.index("--sql") + 1]
        database = command[command.index("--db") + 1]
        lookup = "sqlite_master" in sql
        if "other_account" in database and lookup == (stage == "lookup"):
            return _FakeCompleted(stdout=json.dumps(payload))
        if lookup:
            rows = [{"name": _ROLLOVER_TABLE}]
        elif "WITH times" in sql:
            rows = [{"min_time": 100, "max_time": 100}]
        elif "FROM Msg_" in sql:
            rows = [_db_row(create_time=100)]
        else:
            rows = []
        return _FakeCompleted(stdout=_helper_result(rows))

    service = WeChatExportImportService(provider=_multi_shard_provider(tmp_path, runner))
    output = tmp_path / "fictional-export.json"
    with pytest.raises(QueryFailed):
        if operation == "export":
            service.execute(WeChatExportImportRequest(FICTIONAL_SESSION_FOR_ROLLOVER, output))
        else:
            service.get_session_message_range(FICTIONAL_SESSION_FOR_ROLLOVER)
    assert not output.exists()


def _multi_shard_data_root(tmp_path):
    root = tmp_path / "wechat_multi_shard"
    # _iter_db_directories uses rglob("db_storage") for exact match
    # Create multiple db_storage dirs at different depths
    db_dir_0 = root / "MicroMsg" / "db_storage"
    db_dir_0.mkdir(parents=True)
    (db_dir_0 / "message_0.db").write_bytes(b"fake db")
    # For a second shard, put db_storage in a sibling account dir
    db_dir_1 = root / "other_account" / "MicroMsg" / "db_storage"
    db_dir_1.mkdir(parents=True)
    (db_dir_1 / "message_0.db").write_bytes(b"fake db")
    return root

def _multi_shard_provider(tmp_path, runner):
    helper = tmp_path / "wcdb_cli.exe"
    helper.write_bytes(b"fake")
    library = tmp_path / "WCDB.dll"
    library.write_bytes(b"fake")
    return WeChatDatabaseProvider(
        data_root=_multi_shard_data_root(tmp_path),
        db_key=FICTIONAL_KEY,
        wcdb_cli_path=helper,
        wcdb_dll_path=library,
        runner=runner,
    )


def _make_runner_for_rollover(old_rows, new_rows):
    call_log = []

    def runner(command, timeout, environment):
        sql_index = command.index("--sql") if "--sql" in command else -1
        sql = command[sql_index + 1] if sql_index >= 0 else ""
        db_index = command.index("--db") if "--db" in command else -1
        db_path = command[db_index + 1] if db_index >= 0 else ""

        # Handle table existence checks from _table_exists (sqlite_master query)
        if "sqlite_master" in sql and "Msg_" in sql:
            call_log.append(("table_exists", db_path))
            # Return the rollover table so _find_all_message_dbs considers this shard valid
            return _FakeCompleted(stdout=_helper_result([{"name": _ROLLOVER_TABLE}]))

        # Handle message queries: the SQL contains "FROM Msg_" pattern
        if "FROM Msg_" in sql or "FROM " in sql and "create_time" in sql:
            call_log.append(("message_query", db_path))
            if "other_account" not in db_path:
                return _FakeCompleted(stdout=_helper_result(old_rows))
            else:
                return _FakeCompleted(stdout=_helper_result(new_rows))

        return _FakeCompleted(stdout=_helper_result([]))

    return runner, call_log

def test_read_session_rows_queries_all_shards_with_matching_table(tmp_path):
    old_rows = [
        _db_row(create_time=100, local_id=1),
        _db_row(create_time=101, local_id=2),
        _db_row(create_time=102, local_id=3),
    ]
    new_rows = [
        _db_row(create_time=200, local_id=4),
        _db_row(create_time=201, local_id=5),
        _db_row(create_time=202, local_id=6),
    ]

    runner, call_log = _make_runner_for_rollover(old_rows, new_rows)
    provider = _multi_shard_provider(tmp_path, runner)
    rows = provider.read_session_rows(FICTIONAL_SESSION_FOR_ROLLOVER)

    message_queries = [c for c in call_log if c[0] == "message_query"]
    assert len(message_queries) >= 2, (
        "Expected at least 2 message queries, got %d. Call log: %s" % (len(message_queries), call_log)
    )
    assert len(rows) == 6, "Expected 6 rows from both shards, got %d" % len(rows)
    times = [r["create_time"] for r in rows]
    assert times == sorted(times), "Rows should be globally sorted by create_time ASC, got %s" % times
    assert min(times) == 100
    assert max(times) == 202


def test_read_session_rows_respects_global_limit_across_shards(tmp_path):
    old_rows = [
        _db_row(create_time=100, local_id=1),
        _db_row(create_time=101, local_id=2),
        _db_row(create_time=102, local_id=3),
    ]
    new_rows = [
        _db_row(create_time=200, local_id=4),
        _db_row(create_time=201, local_id=5),
        _db_row(create_time=202, local_id=6),
    ]

    runner, _ = _make_runner_for_rollover(old_rows, new_rows)
    provider = _multi_shard_provider(tmp_path, runner)
    rows = provider.read_session_rows(FICTIONAL_SESSION_FOR_ROLLOVER, limit=4)

    assert len(rows) == 4, "Expected 4 rows with global limit=4, got %d" % len(rows)
    times = [r["create_time"] for r in rows]
    assert times == sorted(times)
    assert times == [100, 101, 102, 200]


def test_read_session_rows_respects_time_scope_across_shards(tmp_path):
    old_rows = [
        _db_row(create_time=50, local_id=1),
        _db_row(create_time=100, local_id=2),
    ]
    new_rows = [
        _db_row(create_time=200, local_id=3),
        _db_row(create_time=500, local_id=4),
    ]

    captured_sqls = []

    def runner(command, timeout, environment):
        sql_index = command.index("--sql") if "--sql" in command else -1
        sql = command[sql_index + 1] if sql_index >= 0 else ""
        db_index = command.index("--db") if "--db" in command else -1
        db_path = command[db_index + 1] if db_index >= 0 else ""

        if "sqlite_master" in sql and "Msg_" in sql:
            return _FakeCompleted(stdout=_helper_result([{"name": _ROLLOVER_TABLE}]))

        if "FROM Msg_" in sql:
            captured_sqls.append(sql)
            shard_rows = old_rows if "other_account" not in db_path else new_rows
            return _FakeCompleted(stdout=_helper_result(shard_rows))

        return _FakeCompleted(stdout=_helper_result([]))

    provider = _multi_shard_provider(tmp_path, runner)
    rows = provider.read_session_rows(
        FICTIONAL_SESSION_FOR_ROLLOVER,
        start_time=90,
        end_time=210,
    )

    assert len(rows) == 4, "Mock returns all rows, got %d" % len(rows)
    assert len(captured_sqls) == 2, "Expected 2 SQL queries, got %d" % len(captured_sqls)
    for sql in captured_sqls:
        assert "create_time >= 90" in sql, "SQL missing start_time: %s" % sql
        assert "create_time <= 210" in sql, "SQL missing end_time: %s" % sql


def test_read_session_rows_default_limit_is_unlimited(tmp_path):
    all_rows = [
        _db_row(create_time=100 + index, local_id=index + 1)
        for index in range(100_001)
    ]
    runner, _ = _make_runner_for_rollover(all_rows, [])
    provider = _multi_shard_provider(tmp_path, runner)

    rows = provider.read_session_rows(FICTIONAL_SESSION_FOR_ROLLOVER)

    assert len(rows) == 100_001
