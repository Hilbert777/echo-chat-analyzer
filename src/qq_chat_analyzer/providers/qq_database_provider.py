"""Materialize raw QQ Direct DB records without interpreting message semantics."""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import warnings
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..qq_db_identity import QQ_DB_SELF_NAMESPACE, canonical_qq_uin
from ..diagnostics import qq_session_stage
from .qq_sqlite_reader import needs_qq_layout, read_rows


QQ_DB_JSON_FORMAT = "qq-db-json"
_GROUP_MESSAGE_TABLE = "group_msg_table"
_C2C_MESSAGE_TABLE = "c2c_msg_table"
_PARTITION_KEY_FIELD = "40027"
_SESSION_OBJECT_FIELD = "40030"
_TIMESTAMP_FIELD = "40050"

_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.qq_database")
_SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass(frozen=True, slots=True)
class QQSession:
    """Privacy-safe descriptor for one QQ conversation discovered from the DB.

    The session is identified by the local partition key (column `40027`),
    not by the external session object identifier (column `40030`).
    This means a single logical session may carry different external
    identifiers across QQ versions or after account migration.

    Fields:
    - `internal_key`   -> column `40027` (local session partition key)
    - `session_object` -> column `40030` (external/session object ID)
    - `display_name`   -> best-effort label; falls back to `session_object`
    - `session_type`   -> `"group"` or `"private"`
    - `message_count`  -> total rows for this partition key
    - `last_message_time` -> max `40050` (Unix seconds)
    """

    internal_key: str
    session_object: str
    display_name: str
    session_type: str = "other"
    message_count: int | None = None
    last_message_time: int | None = None
    peer_uin: str = ""
    search_names: tuple[str, ...] = ()

    @property
    def session_id(self) -> str:
        """Return the selection identifier scoped by the QQ session type."""
        return f"{self.session_type}:{self.internal_key}"

    @property
    def conversation_id(self) -> str:
        """Return the identifier the adapter assigns to this session's messages."""
        return _nonzero_identifier(self.session_object) or self.session_id

class QQDatabaseProvider:
    """Read a supported QQ group-message database into a raw payload v0."""

    def __init__(self, database_path: str | Path, *, cancelled: Callable[[], bool] = lambda: False) -> None:
        self._database_path = Path(database_path)
        self._cancelled = cancelled
        self._success_diagnostic_recorded = False

    @staticmethod
    def quick_check(database_path: str | Path, *, cancelled: Callable[[], bool] = lambda: False) -> str:
        """Run SQLite's real integrity check on a read-only database image."""
        return _quick_check(Path(database_path), cancelled=cancelled)

    def list_sessions(self, *, self_uin: str | None = None) -> list[QQSession]:
        """Return all conversations (groups + private) in the database."""
        with qq_session_stage(_LOGGER, "group_query"):
            groups = self._discover_sessions_from_table(_GROUP_MESSAGE_TABLE, "group")
        with qq_session_stage(_LOGGER, "private_query"):
            c2c = self._discover_sessions_from_table(
                _C2C_MESSAGE_TABLE, "private", self_uin=self_uin
            )
        return groups + c2c

    def _discover_sessions_from_table(
        self,
        table_name: str,
        session_type: str,
        *,
        self_uin: str | None = None,
    ) -> list[QQSession]:
        """Query one message table and return per-session aggregates."""
        peer_column = ""
        parameters: list[Any] = []
        if session_type == "private" and canonical_qq_uin(self_uin):
            peer_column = (
                ', CASE WHEN COUNT(DISTINCT CASE WHEN '
                'TRIM(CAST("40033" AS TEXT)) NOT IN (\'\', \'0\', ?) '
                'THEN "40033" END) = 1 THEN '
                'MAX(CASE WHEN TRIM(CAST("40033" AS TEXT)) '
                'NOT IN (\'\', \'0\', ?) THEN "40033" END) END AS peer_uin '
            )
            parameters = [self_uin, self_uin]
        query = (
            f'SELECT "{_PARTITION_KEY_FIELD}" AS internal_key, '
            f'MAX(CASE WHEN TRIM(CAST("{_SESSION_OBJECT_FIELD}" AS TEXT)) '
            f"NOT IN ('', '0') THEN \"{_SESSION_OBJECT_FIELD}\" END) "
            f'AS session_object, '
            f'COUNT(*) AS message_count, '
            f'MAX("40050") AS last_message_time '
            f'{peer_column} '
            f'FROM "{table_name}" '
            f'GROUP BY "{_PARTITION_KEY_FIELD}" '
            f'ORDER BY last_message_time DESC'
        )
        rows = self._query_rows(
            query,
            parameters,
            operation="list_sessions",
            table_name=table_name,
        )

        sessions: list[QQSession] = []
        for row in rows:
            internal_key_raw = row["internal_key"]
            internal_key = str(internal_key_raw) if internal_key_raw is not None else ""
            # Zero carries no usable partition identity for selection.
            if not internal_key.strip() or internal_key.strip() == "0":
                continue
            session_object = _nonzero_identifier(row["session_object"]) or ""
            display_name = session_object if session_object.strip() else internal_key
            sessions.append(
                QQSession(
                    internal_key=internal_key,
                    session_object=session_object,
                    display_name=display_name,
                    session_type=session_type,
                    message_count=row["message_count"],
                    last_message_time=row["last_message_time"],
                    peer_uin=(
                        _nonzero_identifier(row["peer_uin"])
                        if peer_column else ""
                    ) or "",
                )
            )
        return sessions

    def materialize_group_payload(
        self,
        group_selector: str,
        payload_path: str | Path,
        *,
        start_time: int | float | str | None = None,
        end_time: int | float | str | None = None,
        self_uin: str | None = None,
    ) -> Path:
        """Write source-specific group records as a ``qq-db-json`` payload."""
        records = self._read_group_records(group_selector, start_time, end_time)
        output_path = Path(payload_path)
        payload = {
            "format": QQ_DB_JSON_FORMAT,
            "format_version": 0,
            "source": "qq",
            "source_type": QQ_DB_JSON_FORMAT,
            "self": _self_context(self_uin),
            "query": {
                "requested_session": group_selector,
                "session_type": "group",
                "internal_key": None,
                "session_object": _nonzero_identifier(group_selector),
                "time_range": _time_range(start_time, end_time),
            },
            "records": records,
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return output_path

    def materialize_session_payload(
        self,
        session: QQSession,
        payload_path: str | Path,
        *,
        start_time: int | float | str | None = None,
        end_time: int | float | str | None = None,
        self_uin: str | None = None,
    ) -> Path:
        """Write source-specific records for one session as a qq-db-json payload.

        Uses the session internal_key (column 40027) as the query
        predicate, and routes to the correct table based on session_type.

        ``self_uin`` is the acquisition-time self identity bound to the same
        plaintext snapshot (``core.selfInfo.uin``), or ``None`` when unknown.
        It is passed through verbatim as source metadata; the adapter decides
        how to use it.

        Output is raw / source-specific: no sender/conversation/text
        interpretation is performed here.
        """
        if session.session_type == "group":
            table_name = _GROUP_MESSAGE_TABLE
        elif session.session_type == "private":
            table_name = _C2C_MESSAGE_TABLE
        else:
            raise ValueError(f"Unsupported QQ session type: {session.session_type}")
        records = self._read_session_records(session, table_name, start_time, end_time)
        output_path = Path(payload_path)
        payload = {
            "format": QQ_DB_JSON_FORMAT,
            "format_version": 0,
            "source": "qq",
            "source_type": QQ_DB_JSON_FORMAT,
            "self": _self_context(self_uin),
            "query": {
                "requested_session": session.internal_key,
                "session_type": session.session_type,
                "internal_key": session.internal_key,
                "session_object": _nonzero_identifier(session.session_object),
                "time_range": _time_range(start_time, end_time),
            },
            "records": records,
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return output_path

    def _read_group_records(
        self,
        group_selector: str,
        start_time: int | float | str | None,
        end_time: int | float | str | None,
    ) -> list[dict[str, Any]]:
        conditions = ['"40030" = ?']
        parameters: list[Any] = [group_selector]
        if start_time is not None:
            conditions.append('"40050" >= ?')
            parameters.append(start_time)
        if end_time is not None:
            conditions.append('"40050" <= ?')
            parameters.append(end_time)

        query = (
            'SELECT "40001", "40030", "40033", "40050", "40800"'
            f'{self._group_sequence_selection()} '
            f'FROM {_GROUP_MESSAGE_TABLE} '
            f"WHERE {' AND '.join(conditions)} "
            'ORDER BY "40050" ASC, "40001" ASC'
        )
        rows = self._query_rows(
            query,
            parameters,
            operation="materialize_group_payload",
            table_name=_GROUP_MESSAGE_TABLE,
        )

        records = []
        for row in rows:
            record = _raw_record(row)
            if record is not None:
                records.append(record)
        return records

    def _read_session_records(
        self,
        session: QQSession,
        table_name: str,
        start_time: int | float | str | None,
        end_time: int | float | str | None,
    ) -> list[dict[str, Any]]:
        """Query one table by the session internal_key (40027)."""
        conditions = ['"40027" = ?']
        parameters: list[Any] = [session.internal_key]
        if start_time is not None:
            conditions.append('"40050" >= ?')
            parameters.append(start_time)
        if end_time is not None:
            conditions.append('"40050" <= ?')
            parameters.append(end_time)

        locator_columns = ("40021", "40020") if table_name == _GROUP_MESSAGE_TABLE else ("40021",)
        query = (
            'SELECT "40001", "40027", "40030", "40033", "40050", "40800"'
            f'{self._optional_columns(table_name, locator_columns, operation="materialize_session_payload")}'
            f'{self._group_sequence_selection() if table_name == _GROUP_MESSAGE_TABLE else ""} '
            f'FROM "{table_name}" '
            f"WHERE {' AND '.join(conditions)} "
            'ORDER BY "40050" ASC, "40001" ASC'
        )
        rows = self._query_rows(
            query,
            parameters,
            operation="materialize_session_payload",
            table_name=table_name,
        )

        records = []
        for row in rows:
            record = _session_record(row, table_name)
            if record is not None:
                records.append(record)
        return records

    def _optional_columns(
        self,
        table_name: str,
        columns: tuple[str, ...],
        *,
        operation: str,
    ) -> str:
        """Select optional columns only when this schema has them."""
        present = {
            row["name"]
            for row in self._query_rows(
                f"PRAGMA table_info({table_name})",
                operation=operation,
                table_name=table_name,
            )
        }
        return "".join(f', "{column}"' for column in columns if column in present)

    def _group_sequence_selection(self) -> str:
        """Read 40003 when present; older text-only schemas stay importable."""
        columns = self._query_rows(
            f"PRAGMA table_info({_GROUP_MESSAGE_TABLE})",
            operation="materialize_group_payload",
            table_name=_GROUP_MESSAGE_TABLE,
        )
        return ', "40003"' if any(row["name"] == "40003" for row in columns) else ""

    def _read_only_uri(self) -> str:
        return f"{self._database_path.resolve().as_uri()}?mode=ro"

    def _query_rows(
        self,
        query: str,
        parameters: list[Any] | None = None,
        *,
        operation: str,
        table_name: str,
    ) -> list[sqlite3.Row]:
        """Run one read-only query and diagnose SQLite failures in place.

        Diagnostics contain only fixed operation/table identifiers and file
        structure metadata. No path, session key, message, or SQL parameter is
        logged. The original SQLite exception is always re-raised unchanged.
        """
        connection: sqlite3.Connection | None = None
        stage = "connect"
        try:
            if self._cancelled():
                raise sqlite3.DatabaseError("QQ SQLite read cancelled.")
            if needs_qq_layout(self._database_path):
                stage = "query"
                names, values = read_rows(self._database_path, query, parameters, cancelled=self._cancelled)
                rows = [dict(zip(names, row)) for row in values]
            else:
                connection = sqlite3.connect(self._read_only_uri(), uri=True)
                connection.set_progress_handler(lambda: int(self._cancelled()), 1000)
                connection.row_factory = sqlite3.Row
                stage = "query"
                rows = connection.execute(query, parameters or []).fetchall()
            if not self._success_diagnostic_recorded:
                try:
                    self._log_sqlite_success(
                        operation=operation,
                        table_name=table_name,
                    )
                    self._success_diagnostic_recorded = True
                except Exception:
                    # Diagnostics must never turn a successful read into a failure.
                    pass
            return rows
        except sqlite3.DatabaseError as error:
            try:
                self._log_sqlite_failure(
                    operation=operation,
                    table_name=table_name,
                    stage=stage,
                    error=error,
                )
            except Exception:
                # Diagnostics must never replace the original SQLite failure.
                pass
            raise
        finally:
            if connection is not None:
                connection.close()

    def _log_sqlite_success(self, *, operation: str, table_name: str) -> None:
        metadata = _database_file_metadata(self._database_path)
        _LOGGER.info(
            "QQ Direct DB sqlite read succeeded operation=%s table=%s "
            "database_name=%s file_present=%s file_bytes=%s header_valid=%s "
            "page_size=%s header_page_count=%s size_page_aligned=%s "
            "wal_present=%s wal_bytes=%s shm_present=%s shm_bytes=%s",
            operation,
            table_name,
            self._database_path.name,
            _bool_text(metadata["file_present"]),
            metadata["file_bytes"],
            _bool_text(metadata["header_valid"]),
            metadata["page_size"],
            metadata["header_page_count"],
            _bool_text(metadata["size_page_aligned"]),
            _bool_text(metadata["wal_present"]),
            metadata["wal_bytes"],
            _bool_text(metadata["shm_present"]),
            metadata["shm_bytes"],
        )

    def _log_sqlite_failure(
        self,
        *,
        operation: str,
        table_name: str,
        stage: str,
        error: sqlite3.DatabaseError,
    ) -> None:
        metadata = _database_file_metadata(self._database_path)
        _LOGGER.warning(
            "QQ Direct DB sqlite read failed operation=%s table=%s stage=%s "
            "error=%s database_name=%s file_present=%s file_bytes=%s "
            "header_valid=%s page_size=%s header_page_count=%s "
            "size_page_aligned=%s wal_present=%s wal_bytes=%s "
            "shm_present=%s shm_bytes=%s quick_check=%s",
            operation,
            table_name,
            stage,
            type(error).__name__,
            self._database_path.name,
            _bool_text(metadata["file_present"]),
            metadata["file_bytes"],
            _bool_text(metadata["header_valid"]),
            metadata["page_size"],
            metadata["header_page_count"],
            _bool_text(metadata["size_page_aligned"]),
            _bool_text(metadata["wal_present"]),
            metadata["wal_bytes"],
            _bool_text(metadata["shm_present"]),
            metadata["shm_bytes"],
            "cancelled" if self._cancelled() else _quick_check(self._database_path, cancelled=self._cancelled),
        )


def _database_file_metadata(database_path: Path) -> dict[str, int | bool]:
    try:
        file_bytes = database_path.stat().st_size
        with database_path.open("rb") as stream:
            header = stream.read(100)
    except OSError:
        file_bytes = 0
        header = b""

    header_valid = header.startswith(_SQLITE_HEADER)
    page_size = 0
    header_page_count = 0
    if header_valid and len(header) >= 32:
        page_size = int.from_bytes(header[16:18], "big")
        if page_size == 1:
            page_size = 65536
        header_page_count = int.from_bytes(header[28:32], "big")

    wal_path = database_path.with_name(database_path.name + "-wal")
    shm_path = database_path.with_name(database_path.name + "-shm")
    wal_bytes = _file_size(wal_path)
    shm_bytes = _file_size(shm_path)
    return {
        "file_present": database_path.is_file(),
        "file_bytes": file_bytes,
        "header_valid": header_valid,
        "page_size": page_size,
        "header_page_count": header_page_count,
        "size_page_aligned": bool(page_size and file_bytes % page_size == 0),
        "wal_present": wal_path.is_file(),
        "wal_bytes": wal_bytes,
        "shm_present": shm_path.is_file(),
        "shm_bytes": shm_bytes,
    }


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _quick_check(database_path: Path, *, cancelled: Callable[[], bool] = lambda: False) -> str:
    connection: sqlite3.Connection | None = None
    try:
        if cancelled():
            raise sqlite3.DatabaseError("QQ SQLite read cancelled.")
        if needs_qq_layout(database_path):
            _, rows = read_rows(database_path, "PRAGMA quick_check(1)", cancelled=cancelled)
            row = rows[0] if rows else None
        else:
            uri = f"{database_path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.set_progress_handler(lambda: int(cancelled()), 1000)
            row = connection.execute("PRAGMA quick_check(1)").fetchone()
        if row and row[0] == "ok":
            return "ok"
        # SQLite's text may contain private table names or values. Never log it.
        _LOGGER.warning(
            "QQ snapshot quick_check result=failed category=integrity "
            "integrity_kind=%s sqlite_version=%s classifier_version=1",
            _integrity_failure_kind(row[0] if row else None, sqlite3.sqlite_version),
            _safe_sqlite_version(),
        )
        return "failed"
    except (OSError, sqlite3.DatabaseError) as error:
        code = getattr(error, "sqlite_errorcode", 0)
        primary = code & 0xff
        category = (
            "corrupt" if primary in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
            else "access" if primary in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH)
            else "busy" if primary in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            else "io" if isinstance(error, OSError) or primary == sqlite3.SQLITE_IOERR
            else "sqlite"
        )
        _LOGGER.warning(
            "QQ snapshot quick_check result=error category=%s "
            "sqlite_errorcode=%d sqlite_version=%s",
            category, code, _safe_sqlite_version(),
        )
        return "error"
    finally:
        if connection is not None:
            connection.close()


def _safe_sqlite_version() -> str:
    version = sqlite3.sqlite_version
    return version if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) else "unknown"


def _integrity_failure_kind(message: object, version: str) -> str:
    """Classify reviewed formats, never infer the damaged page or a root cause.

    Reviewed against SQLite version-3.50.4 and version-3.54.1: src/btree.c,
    src/pragma.c and ext/fts3/fts3.c. These six formats are unchanged; unreviewed
    versions and unusual identifiers fail to unknown.
    In particular, the FTS4 format also describes genuine validation failures:
    matching it cannot establish the contentless-FTS4 upstream false positive.
    """
    if version not in ("3.50.4", "3.54.1") or not isinstance(message, str) or len(message) > 4096:
        return "unknown"
    structural = r"\*\*\* in database main \*\*\*\n"
    identifier = r"[A-Za-z0-9_]+"
    patterns = (
        ("btree", structural + r"Fragmentation of [0-9]+ bytes reported as [0-9]+ on page [0-9]+"),
        ("overflow", structural + r"Tree [0-9]+ page [0-9]+ cell [0-9]+: overflow list length is [0-9]+ but should be [0-9]+"),
        ("freelist", structural + r"Freelist: size is [0-9]+ but should be [0-9]+"),
        ("page_reference", structural + r"Page [0-9]+: never used"),
        ("constraint", rf"(?:NULL value in {identifier}\.{identifier}|CHECK constraint failed in {identifier})"),
        ("virtual_table", rf"unable to validate the inverted index for FTS4 table main\.{identifier}: SQL logic error"),
    )
    for kind, pattern in patterns:
        if re.fullmatch(pattern, message):
            return kind
    return "unknown"


def _bool_text(value: int | bool) -> str:
    return "true" if value is True else "false"


def _nonzero_identifier(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    identifier = str(value).strip()
    return identifier if identifier and identifier != "0" else None


def _raw_record(row: sqlite3.Row) -> dict[str, Any] | None:
    # These field numbers only describe the feasibility-supported source
    # shape; they are not stable across QQ versions.
    blob = _message_blob_or_warn(row)
    if blob is None:
        return None
    return {
        "record_id": str(row["40001"]),
        "fields": {
            "40030": row["40030"],
            "40033": row["40033"],
            "40001": row["40001"],
            "40050": row["40050"],
            **({"40003": row["40003"]} if "40003" in row.keys() else {}),
        },
        "message_blob": base64.b64encode(blob).decode("ascii"),
        "blob_encoding": "base64",
        "source_meta": {"table": _GROUP_MESSAGE_TABLE},
    }


def _session_record(row: sqlite3.Row, table_name: str) -> dict[str, Any] | None:
    """Build a raw session record dict (no semantic interpretation).

    Uses column 40001 (source-native message unique ID) as record_id.
    """
    blob = _message_blob_or_warn(row)
    if blob is None:
        return None
    return {
        "record_id": str(row["40001"]),
        "fields": {
            "40001": row["40001"],
            "40027": row["40027"],
            "40030": row["40030"],
            "40033": row["40033"],
            "40050": row["40050"],
            **({"40003": row["40003"]} if "40003" in row.keys() else {}),
            **({"40021": row["40021"]} if "40021" in row.keys() else {}),
            **({"40020": row["40020"]} if "40020" in row.keys() else {}),
        },
        "message_blob": base64.b64encode(blob).decode("ascii"),
        "blob_encoding": "base64",
        "source_meta": {"table": table_name},
    }


def _time_range(
    start_time: int | float | str | None,
    end_time: int | float | str | None,
) -> dict[str, int | float | str | None] | None:
    if start_time is None and end_time is None:
        return None
    return {"start": start_time, "end": end_time}


def _self_context(self_uin: str | None) -> dict[str, str] | None:
    """Return the payload ``self`` context, or ``None`` when unreliable.

    The provider canonicalizes the incoming self value so an invalid/zero
    value never reaches the adapter as a real identity.  The resulting
    structure carries the namespace and canonical value explicitly.
    """
    canonical = canonical_qq_uin(self_uin)
    if canonical is None:
        return None
    return {"namespace": QQ_DB_SELF_NAMESPACE, "value": canonical}

def _message_blob_or_warn(row: sqlite3.Row) -> bytes | None:
    """Return a usable raw blob without exposing malformed data."""
    blob = row["40800"]
    if isinstance(blob, bytes) and blob:
        return blob
    warnings.warn(
        "Skipped QQ DB record with invalid 40800.",
        RuntimeWarning,
        stacklevel=2,
    )
    return None
