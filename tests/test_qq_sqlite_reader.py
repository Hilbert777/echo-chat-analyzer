from __future__ import annotations

import ctypes
import os
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

import pytest

from qq_chat_analyzer.providers.qq_sqlite_reader import read_rows, needs_qq_layout, _pending_byte_control

pytestmark = [pytest.mark.slow_integration, pytest.mark.skipif(os.name != "nt", reason="Windows SQLite layout")]


@pytest.mark.qq
def test_qq_layout_reader_runs_query_in_child_and_returns_rows(tmp_path: Path) -> None:
    path = tmp_path / "fictional-qq.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fictional_table(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO fictional_table VALUES(1, 'fictional')")

    names, rows = read_rows(path, "SELECT id, value FROM fictional_table")

    assert names == ["id", "value"]
    assert rows == [(1, "fictional")]


@pytest.mark.qq
def test_qq_layout_reader_does_not_expose_sqlite_error_text(tmp_path: Path) -> None:
    path = tmp_path / "fictional-qq.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fictional_table(id INTEGER PRIMARY KEY)")

    with pytest.raises(sqlite3.DatabaseError) as caught:
        read_rows(path, "SELECT private_column FROM fictional_table")

    assert "private_column" not in str(caught.value)

    with pytest.raises(sqlite3.DatabaseError):
        read_rows(path, "DELETE FROM fictional_table")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM fictional_table").fetchone() == (0,)


def _make_qq_lock_page_fixture(path: Path) -> None:
    """Allocate only freelist trunks and two real pages in a sparse 1 GiB file."""
    from test_qq_direct_main_wal_poc import _reserve_48_bytes
    import msvcrt

    _reserve_48_bytes(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE fictional_table(id INTEGER PRIMARY KEY,body TEXT)")
        connection.execute("INSERT INTO fictional_table VALUES(1,'fictional')")
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_schema SET rootpage=262145 WHERE name='fictional_table'")
        connection.commit()
    with path.open("r+b") as stream:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                          ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        returned = ctypes.c_ulong()
        assert kernel.DeviceIoControl(msvcrt.get_osfhandle(stream.fileno()), 0x900c4,
                                      None, 0, None, 0, ctypes.byref(returned), None)
        stream.seek(4096)
        root = stream.read(4096)
        pages = 262147
        stream.truncate(pages * 4096)
        stream.seek((262145 - 1) * 4096)
        stream.write(root)
        free = [page for page in range(2, pages + 1) if page not in (262144, 262145)]
        trunks = free[::1001]
        for index, offset in enumerate(range(0, len(free), 1001)):
            group = free[offset:offset + 1001]
            next_page = trunks[index + 1] if index + 1 < len(trunks) else 0
            values = [next_page, len(group) - 1, *group[1:]]
            stream.seek((group[0] - 1) * 4096)
            stream.write(struct.pack(">" + "I" * len(values), *values))
        stream.seek(28)
        stream.write(struct.pack(">III", pages, free[0], len(free)))


def test_large_qq_lock_page_passes_real_check_and_query_without_changing_parent(tmp_path: Path) -> None:
    from qq_chat_analyzer.providers.qq_database_provider import QQDatabaseProvider

    path = tmp_path / "fictional-large-qq.db"
    _make_qq_lock_page_fixture(path)
    before = path.stat()
    assert needs_qq_layout(path)
    with closing(sqlite3.connect(path)) as connection:
        error = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
        assert "2nd reference to page 262145" in error

    assert QQDatabaseProvider.quick_check(path) == "ok"
    assert read_rows(path, "PRAGMA integrity_check(1)")[1] == [("ok",)]
    rows = QQDatabaseProvider(path)._query_rows(
        "SELECT id,body FROM fictional_table", operation="list_sessions", table_name="group_msg_table",
    )
    assert rows == [{"id": 1, "body": "fictional"}]
    assert _pending_byte_control()(11, 0) == 0x40000000
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns

    # Real damage still fails even with the correct lock-page layout.
    with path.open("r+b") as stream:
        stream.seek((262145 - 1) * 4096 + 7)
        stream.write(bytes([61]))
    assert QQDatabaseProvider.quick_check(path) != "ok"


def test_stock_sqlite_large_file_keeps_standard_layout(tmp_path: Path) -> None:
    path = tmp_path / "fictional-stock.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE fictional_table(id INTEGER PRIMARY KEY)")
    with path.open("r+b") as stream:
        stream.truncate(0x40000000 + 4096)
    assert not needs_qq_layout(path)


def test_cancel_running_query_reaps_owned_child_and_releases_file(tmp_path):
    import multiprocessing
    import time

    path = tmp_path / "fictional-cancel.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE fictional_table(id INTEGER PRIMARY KEY)")
    previous = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(sqlite3.DatabaseError, match="cancelled"):
        read_rows(path, "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) "
                       "SELECT sum(x) FROM n", cancelled=lambda: time.monotonic()-started > 0.5)
    assert time.monotonic()-started < 5
    assert {child.pid for child in multiprocessing.active_children()} == previous
    path.unlink()  # Windows denies this while the SQLite read handle survives.
    assert not path.exists()
