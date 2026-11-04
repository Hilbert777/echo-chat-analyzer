"""Read WeChat 4.x databases through the bundled read-only ``wcdb_cli`` helper.

This provider only *acquires* data. It locates the WeChat data directory, runs
the already-built ``wcdb_cli`` executable against ``session.db`` and the right
``message_N.db`` shard, and writes the rows to a JSON document. Turning those
rows into :class:`~qq_chat_analyzer.message.ChatMessage` objects is the job of
``wechat_db_adapter``; orchestration is the job of the application layer.

Schema facts confirmed against a real WeChat 4.x install:

* ``session.db``   -> ``SessionTable.username`` identifies a conversation
* ``md5(username)`` -> the ``Msg_<md5>`` table inside a ``message_N.db`` shard
* ``Msg_<md5>.local_type = 1`` marks a plain text message
* ``Msg_<md5>.real_sender_id`` -> ``Name2Id.rowid`` -> ``user_name``

The database key is only ever held in memory and passed to the helper through
the ``WX_DB_KEY`` environment variable, never written to disk or logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from .wechat_wcdb_diagnostic import maybe_launch_wcdb_diagnostic
from ..identity_names import identity_search_names
from ..rich_message import SenderIdentity


DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_SESSION_LIMIT = 200
DEFAULT_MESSAGE_LIMIT = 100000
# Keep compound SELECTs below SQLite's term limit and Windows' command line
# limit. Each branch contains only a generated Msg_<md5> name and COUNT(*).
_MESSAGE_COUNT_BATCH_SIZE = 100
DB_KEY_ENVIRONMENT_VARIABLE = "WX_DB_KEY"

SESSION_DB_NAME = "session.db"
MESSAGE_DB_GLOB = "message_*.db"
CONTACT_DB_NAME = "contact.db"
_SESSION_TABLE = "SessionTable"
# Minimal read that still requires decryption: one aggregate over the schema
# table. It reads no conversation table, no message content, and assumes no
# WeChat-specific layout.
_VERIFY_SQL = "SELECT count(*) FROM sqlite_master"
WCDB_DLL_NAME = "WCDB.dll"
MESSAGE_AVAILABILITY_REASON = (
    "\u8be5\u4f1a\u8bdd\u6ca1\u6709\u53ef\u5206\u6790\u6d88\u606f"
)

_DB_STORAGE_DIR_NAMES = ("db_storage",)
_ACCOUNT_DIRECTORY_SUFFIX_PATTERN = re.compile(
    r"^(?P<username>.+)_[0-9a-fA-F]{4,}$"
)
_LOGGER = logging.getLogger(
    "qq_chat_analyzer.providers.wechat_database_provider"
)


class WeChatDatabaseError(Exception):
    """Base error for WeChat database access failures."""

    code = "wechat_database_error"
    public_message = "\u5fae\u4fe1\u6570\u636e\u5e93\u8bfb\u53d6\u5931\u8d25\u3002"

    def __init__(self, public_message: str | None = None) -> None:
        self.public_message = public_message or type(self).public_message
        super().__init__(self.public_message)


class WcdbHelperNotFound(WeChatDatabaseError):
    """Raised when the ``wcdb_cli`` helper executable is unavailable."""

    code = "wcdb_helper_not_found"
    public_message = (
        "\u672a\u627e\u5230 wcdb_cli \u8f85\u52a9\u7a0b\u5e8f\u3002"
        "\u8bf7\u5148\u6784\u5efa src/qq_chat_analyzer/native/wcdb_cli\u3002"
    )


class WcdbLibraryNotFound(WeChatDatabaseError):
    """Raised when ``WCDB.dll`` cannot be located."""

    code = "wcdb_library_not_found"
    public_message = (
        "\u672a\u627e\u5230 WCDB.dll\u3002"
        "\u8bf7\u786e\u8ba4\u5fae\u4fe1\u5df2\u5b89\u88c5\uff0c"
        "\u6216\u624b\u52a8\u6307\u5b9a WCDB.dll \u8def\u5f84\u3002"
    )


class DatabaseNotFound(WeChatDatabaseError):
    """Raised when the WeChat data directory or its databases are missing."""

    code = "database_not_found"
    public_message = (
        "\u672a\u627e\u5230\u5fae\u4fe1\u6570\u636e\u76ee\u5f55\u3002"
        "\u8bf7\u786e\u8ba4\u5fae\u4fe1\u5df2\u5728\u672c\u673a\u767b\u5f55\u8fc7\uff0c"
        "\u6216\u624b\u52a8\u6307\u5b9a\u6570\u636e\u76ee\u5f55\u3002"
    )


class KeyUnavailable(WeChatDatabaseError):
    """Raised when no database key was supplied."""

    code = "key_unavailable"
    public_message = (
        "\u7f3a\u5c11\u5fae\u4fe1\u6570\u636e\u5e93\u5bc6\u94a5\u3002"
        "\u8bf7\u5148\u83b7\u53d6 DbKey \u540e\u91cd\u8bd5\u3002"
    )


class QueryFailed(WeChatDatabaseError):
    """Raised when the helper could not run a query."""

    code = "query_failed"
    public_message = (
        "\u8bfb\u53d6\u5fae\u4fe1\u6570\u636e\u5e93\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5\u3002"
    )

    def __init__(
        self,
        public_message: str | None = None,
        *,
        wcdb_error_code: Any = None,
    ) -> None:
        super().__init__(public_message)
        # Safe diagnostic only: the numeric WCDB error code. The helper's raw
        # message, path, SQL text, and key value are never carried here.
        self.wcdb_error_code = wcdb_error_code


class DatabaseUnreadable(WeChatDatabaseError):
    """Raised when the current key cannot read the selected database.

    WeChat stores its databases encrypted, so a structurally valid
    ``session.db`` still fails to open when the captured key belongs to
    another login: the WCDB prepare fails with ``NOTADB`` (code 26). A
    matching folder layout is therefore never proof that this key can read
    this file, and a caller needs to tell this case apart from a generic
    query failure so it can ask for a different data location.
    """

    code = "wechat_database_unreadable"
    public_message = (
        "\u5f53\u524d\u5fae\u4fe1\u767b\u5f55\u4fe1\u606f\u65e0\u6cd5\u8bfb\u53d6"
        "\u6240\u9009\u6570\u636e\u5e93\uff0c\u8bf7\u786e\u8ba4\u6570\u636e\u4f4d\u7f6e"
        "\u662f\u5426\u5bf9\u5e94\u5f53\u524d\u767b\u5f55\u8d26\u53f7\uff0c"
        "\u6216\u91cd\u65b0\u83b7\u53d6\u5fae\u4fe1\u8fde\u63a5\u4fe1\u606f\u3002"
    )


class SessionNotFound(WeChatDatabaseError):
    """Raised when a session has no message table in any shard."""

    code = "session_not_found"
    public_message = (
        "\u672a\u627e\u5230\u8be5\u804a\u5929\uff0c"
        "\u6216\u6240\u9009\u65f6\u95f4\u8303\u56f4\u5185\u6ca1\u6709\u6d88\u606f\u3002"
    )


@dataclass(frozen=True, slots=True)
class WeChatSession:
    """Privacy-safe descriptor for one WeChat conversation."""

    session_id: str
    display_name: str
    session_type: str = "other"
    message_count: int | None = None
    message_available: bool = True
    unavailable_reason: str | None = None
    last_timestamp: int | float | str | None = None
    search_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseProbeResult:
    """Internal result of probing one known WeChat session database."""

    session_db: Path
    data_root: Path
    readable: bool
    wcdb_error_code: Any = None


@dataclass(frozen=True, slots=True)
class _SessionDatabaseCandidate:
    session_db: Path
    data_root: Path


def message_table_name(username: str) -> str:
    """Return the ``Msg_<md5>`` table name WeChat uses for ``username``."""
    digest = hashlib.md5(username.encode("utf-8")).hexdigest()
    return f"Msg_{digest}"


def default_data_root() -> Path | None:
    """Best-effort guess of the local WeChat 4.x data directory."""
    candidates = [
        Path.home() / "Documents" / "xwechat_files",
        Path.home() / "Documents" / "WeChat Files",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


class WeChatDatabaseProvider:
    """Acquire raw WeChat rows from local databases via ``wcdb_cli``."""

    def __init__(
        self,
        data_root: str | Path | None = None,
        db_key: str | None = None,
        wcdb_cli_path: str | Path | None = None,
        wcdb_dll_path: str | Path | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        runner: Callable[..., Any] | None = None,
        diagnostic_spawner: Callable[..., Any] | None = None,
        sticker_media_roots: tuple[Path, ...] = (),
    ) -> None:
        self._data_root = Path(data_root) if data_root is not None else None
        self._db_key = db_key
        self._wcdb_cli_path = (
            Path(wcdb_cli_path) if wcdb_cli_path is not None else None
        )
        self._wcdb_dll_path = (
            Path(wcdb_dll_path) if wcdb_dll_path is not None else None
        )
        self._timeout = timeout
        self._runner = runner or _run_subprocess
        self._diagnostic_spawner = diagnostic_spawner
        self._sticker_media_roots = tuple(sticker_media_roots)

    def sticker_media_roots(self) -> tuple[Path, ...]:
        """Return explicitly configured media authority, without discovery or I/O."""
        return self._sticker_media_roots

    # --------------------------------------------------------- verification

    def verify_readable(self) -> None:
        """Prove the current key can open the selected ``session.db``.

        This is the cheapest read that still requires decryption, and it is
        what separates "the folder layout looks right" from "this key can
        read this database". Session loading must not start until this
        succeeds, otherwise a non-matching directory is reported as a generic
        read failure with no way for the user to pick another location.

        Raises :class:`DatabaseNotFound` when no ``session.db`` exists,
        :class:`KeyUnavailable` when no key is configured, and
        :class:`DatabaseUnreadable` when the key cannot read the file.
        """
        session_db = self._session_db_path()
        try:
            rows = self._query(
                session_db,
                _VERIFY_SQL,
                limit=1,
                query_stage="verify",
                log_wcdb_error_message=False,
            )
        except QueryFailed as error:
            _log_database_verification(
                success=False,
                error_type=type(error).__name__,
                wcdb_error_code=getattr(error, "wcdb_error_code", None),
            )
            raise DatabaseUnreadable() from error

        if not rows:
            # The helper reported success but returned no row, so the read is
            # not usable as proof: fail closed instead of trusting the layout.
            _log_database_verification(
                success=False,
                error_type="EmptyResult",
                wcdb_error_code=None,
            )
            raise DatabaseUnreadable()

        _log_database_verification(
            success=True,
            error_type=None,
            wcdb_error_code=None,
        )

    def probe_session_databases(
        self,
        additional_roots: Iterable[str | Path] = (),
    ) -> list[DatabaseProbeResult]:
        """Probe every known ``session.db`` with the provider's current key.

        This is diagnostic only. It never changes ``data_root``, retries a
        different cipher profile, or raises probe failures to the caller.
        Candidate numbers are local to the log; the underlying paths stay in
        memory solely for de-duplication and are never logged.
        """
        candidates = self._session_db_candidates(additional_roots)
        _LOGGER.info(
            "wechat.database.probe candidate_count=%d",
            len(candidates),
        )
        results: list[DatabaseProbeResult] = []
        for index, candidate in enumerate(candidates, start=1):
            success = False
            wcdb_error_code: Any = None
            try:
                rows = self._query(
                    candidate.session_db,
                    _VERIFY_SQL,
                    limit=1,
                    query_stage="probe",
                    log_wcdb_error_message=False,
                )
                success = bool(rows)
            except QueryFailed as error:
                wcdb_error_code = getattr(error, "wcdb_error_code", None)
            except Exception:
                # A diagnostic failure must never replace the original
                # verification error or disturb the caller's recovery flow.
                pass
            results.append(
                DatabaseProbeResult(
                    session_db=candidate.session_db,
                    data_root=candidate.data_root,
                    readable=success,
                    wcdb_error_code=wcdb_error_code,
                )
            )
            _log_database_probe_candidate(
                index,
                success=success,
                wcdb_error_code=wcdb_error_code,
            )
        return results

    def with_data_root(self, data_root: str | Path) -> WeChatDatabaseProvider:
        """Return an equivalent provider scoped to one verified account root."""
        return WeChatDatabaseProvider(
            data_root=data_root,
            db_key=self._db_key,
            wcdb_cli_path=self._wcdb_cli_path,
            wcdb_dll_path=self._wcdb_dll_path,
            timeout=self._timeout,
            runner=self._runner,
            diagnostic_spawner=self._diagnostic_spawner,
            # Account probing must not carry media authority into another account.
            sticker_media_roots=(
                self._sticker_media_roots if Path(data_root) == self._data_root else ()
            ),
        )

    # ---------------------------------------------------------------- listing

    def list_sessions(self, limit: int = DEFAULT_SESSION_LIMIT) -> list[WeChatSession]:
        """List conversations found in ``session.db``."""
        session_db = self._session_db_path()
        self._maybe_launch_wcdb_diagnostic(session_db)
        columns = self._query_table_columns(session_db)
        sql = _build_session_list_sql(columns)
        rows = self._query(
            session_db,
            sql,
            limit=limit,
            query_stage="session_list",
        )
        inventory = self._message_table_inventory()
        available_tables = {
            name for names in inventory.values() if names is not None for name in names
        }
        requested_tables = {
            message_table_name(row["username"])
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get("username"), str)
            and _is_conversation_username(row["username"])
        }
        message_counts = self._session_message_counts(inventory, requested_tables)
        contact_names = self._contact_identities()

        sessions: list[WeChatSession] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            username = row.get("username")
            if not isinstance(username, str) or not username.strip():
                continue
            if not _is_conversation_username(username):
                continue
            message_available = message_table_name(username) in available_tables
            identity = contact_names.get(username)
            sessions.append(
                WeChatSession(
                    session_id=username,
                    display_name=identity.display_name if identity else username,
                    search_names=identity_search_names(identity.remark, identity.nickname) if identity else (),
                    session_type=_session_type(username),
                    message_count=message_counts.get(message_table_name(username)),
                    last_timestamp=row.get("last_timestamp"),
                    message_available=message_available,
                    unavailable_reason=(
                        None
                        if message_available
                        else MESSAGE_AVAILABILITY_REASON
                    ),
                )
            )
        return sessions

    def _query_table_columns(self, db_path: Path) -> set[str]:
        """Return the set of column names for the session table."""
        rows = self._query(
            db_path,
            f"PRAGMA table_info({_SESSION_TABLE})",
            limit=DEFAULT_MESSAGE_LIMIT,
            query_stage="schema_info",
        )
        columns: set[str] = set()
        for row in rows:
            if isinstance(row, Mapping) and isinstance(row.get("name"), str):
                columns.add(row["name"])
        return columns

    def _maybe_launch_wcdb_diagnostic(self, session_db: Path) -> None:
        """Start the standalone WCDB diagnostic runner when the env gate is on.

        Fire-and-forget and fully isolated: any failure (missing key, missing
        script, subprocess error) is swallowed so the WeChat connection flow
        and its return values are never disturbed. The DbKey is only passed
        through the child environment.
        """
        try:
            key = self._resolve_key()
        except Exception:
            return
        try:
            maybe_launch_wcdb_diagnostic(
                session_db, key, runner=self._diagnostic_spawner
            )
        except Exception:
            _LOGGER.debug("wcdb diagnostic launch skipped", exc_info=True)

    # -------------------------------------------------------------- exporting

    def export_session_json(
        self,
        session_id: str,
        output_path: str | Path,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 0,
    ) -> Path:
        """Write one conversation's raw rows to ``output_path`` as JSON."""
        rows = self.read_session_rows(
            session_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        contact_names = self._contact_identities()
        resolved_rows: list[Any] = []
        for row in rows:
            if isinstance(row, Mapping):
                merged = dict(row)
                sender_id = row.get("user_name")
                if isinstance(sender_id, str) and sender_id.strip():
                    identity = contact_names.get(sender_id.strip())
                    merged["sender_name"] = identity.display_name if identity else sender_id.strip()
                    if identity:
                        merged["sender_remark"] = identity.remark
                        merged["sender_nickname"] = identity.nickname
                resolved_rows.append(merged)
            else:
                resolved_rows.append(row)
        document = {
            "source": "wechat-db",
            "conversation": {
                "username": session_id,
                "session_type": _session_type(session_id),
                "display_name": (
                    contact_names[session_id].display_name if session_id in contact_names else session_id
                ),
            },
            "messages": resolved_rows,
        }
        try:
            self_username = self._resolve_self_username()
        except Exception:
            self_username = None
        if self_username:
            document["conversation"]["self_username"] = self_username
        destination.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )
        return destination

    def read_session_rows(
        self,
        session_id: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 0,
    ) -> list[Any]:
        """Return raw message rows for one conversation, sender names resolved."""
        cleaned_session = (session_id or "").strip()
        if not cleaned_session:
            raise SessionNotFound()

        table = message_table_name(cleaned_session)
        message_dbs = self._find_all_message_dbs(table)
        conditions = [_supported_message_types_sql(cleaned_session, prefix="m.")]
        if isinstance(start_time, int) and not isinstance(start_time, bool):
            conditions.append(f"m.create_time >= {start_time}")
        if isinstance(end_time, int) and not isinstance(end_time, bool):
            conditions.append(f"m.create_time <= {end_time}")
        where_clause = " AND ".join(conditions)

        sql = (
            "SELECT m.local_id, m.server_id, m.local_type, m.create_time, "
            "m.message_content, m.WCDB_CT_message_content, n.user_name "
            f"FROM {table} AS m "
            "LEFT JOIN Name2Id AS n ON n.rowid = m.real_sender_id "
            f"WHERE {where_clause} ORDER BY m.create_time ASC"
        )
        all_rows: list[Any] = []
        for message_db in message_dbs:
            shard_rows = self._query(
                message_db,
                sql,
                limit=0,
                query_stage="message_rows",
            )
            all_rows.extend(shard_rows)

        all_rows.sort(key=lambda r: r.get("create_time", 0) if isinstance(r, Mapping) else 0)

        if limit > 0 and limit < len(all_rows):
            all_rows = all_rows[:limit]

        if _session_type(cleaned_session) == "private":
            _LOGGER.info("wechat.pats.read rows=%d candidates=%d shards=%d", len(all_rows),
                         sum(isinstance(row, Mapping) and row.get("local_type") == 266287972401
                             for row in all_rows), len(message_dbs))
        return all_rows

    def read_session_time_bounds(self, session_id: str) -> list[Any]:
        """Aggregate time columns across every shard, without reading bodies.

        Seconds and milliseconds need separate extrema: normalizing a mixed
        pair of raw MIN/MAX values could lose the actual earliest timestamp.
        Non-numeric values are returned distinctly for the application's
        tolerant timestamp parser, rather than SQLite's lossy numeric casts.
        """
        cleaned_session = (session_id or "").strip()
        if not cleaned_session:
            raise SessionNotFound()
        table = message_table_name(cleaned_session)
        sql = (
            "WITH times AS (SELECT create_time FROM "
            f"{table} WHERE {_supported_message_types_sql(cleaned_session)}) "
            "SELECT MIN(create_time) AS min_time, MAX(create_time) AS max_time "
            "FROM times WHERE typeof(create_time) IN ('integer', 'real') "
            "AND create_time > 0 AND create_time < 1e999 "
            "GROUP BY create_time >= 100000000000 "
            "UNION ALL SELECT create_time, create_time FROM times "
            "WHERE typeof(create_time) = 'text' GROUP BY create_time"
        )
        bounds: list[Any] = []
        for message_db in self._find_all_message_dbs(table):
            bounds.extend(self._query(message_db, sql, limit=0, query_stage="message_range"))
        return bounds

    # --------------------------------------------------------------- internals

    def _query(
        self,
        db_path: Path,
        sql: str,
        limit: int,
        query_stage: str = "query",
        *,
        log_wcdb_error_message: bool = True,
    ) -> list[Any]:
        command = [
            str(self._resolve_helper()),
            "--wcdb",
            str(self._resolve_library()),
            "--db",
            str(db_path),
            "--sql",
            sql,
        ]
        if limit > 0:
            command.extend(["--limit", str(limit)])

        environment = dict(os.environ)
        environment[DB_KEY_ENVIRONMENT_VARIABLE] = self._resolve_key()
        database_type = _database_type(db_path)
        _LOGGER.info(
            "wechat.wcdb.start database_type=%s query_stage=%s",
            database_type,
            query_stage,
        )

        try:
            completed = self._runner(command, self._timeout, environment)
        except FileNotFoundError as error:
            _log_query_exception(database_type, query_stage, error)
            raise WcdbHelperNotFound() from error
        except subprocess.TimeoutExpired as error:
            _log_query_exception(database_type, query_stage, error)
            raise QueryFailed(
                "\u8bfb\u53d6\u5fae\u4fe1\u6570\u636e\u8d85\u65f6\uff0c\u8bf7\u91cd\u8bd5\u3002"
            ) from error
        except Exception as error:
            _log_query_exception(database_type, query_stage, error)
            raise QueryFailed() from error

        stdout = getattr(completed, "stdout", "") or ""
        returncode = getattr(completed, "returncode", None)
        payload = _parse_result(stdout)
        if payload is None:
            _log_query_failure(
                database_type,
                query_stage,
                wcdb_stage="parse_result",
                returncode=returncode,
                error_type="QueryFailed",
            )
            raise QueryFailed()
        if payload.get("ok") is not True:
            wcdb_error_code = payload.get("wcdb_error_code")
            wcdb_error_message = payload.get("wcdb_error_message")
            if (
                log_wcdb_error_message
                and isinstance(wcdb_error_message, str)
            ):
                wcdb_error_message = _sanitize_wcdb_message(
                    wcdb_error_message, db_path, self._resolve_key(), sql
                )
            elif not log_wcdb_error_message:
                wcdb_error_message = None
            _log_query_failure(
                database_type,
                query_stage,
                wcdb_stage=_safe_stage(payload.get("stage")),
                returncode=returncode,
                error_type="QueryFailed",
                wcdb_error_code=wcdb_error_code,
                wcdb_error_message=wcdb_error_message,
            )
            raise QueryFailed(wcdb_error_code=wcdb_error_code)

        rows = payload.get("rows")
        # An invalid result is not proof of an empty shard. Unbounded reads
        # also require completeness; explicit limits may legitimately truncate.
        if not isinstance(rows, list) or (limit <= 0 and payload.get("truncated") is True):
            _log_query_failure(
                database_type,
                query_stage,
                wcdb_stage="validate_result",
                returncode=returncode,
                error_type="QueryFailed",
            )
            raise QueryFailed()
        _LOGGER.info(
            "wechat.wcdb.success database_type=%s query_stage=%s",
            database_type,
            query_stage,
        )
        return rows

    def _session_db_candidates(
        self,
        additional_roots: Iterable[str | Path],
    ) -> list[_SessionDatabaseCandidate]:
        roots: list[Path] = []
        if self._data_root is not None:
            roots.append(self._data_root)
        try:
            for value in additional_roots:
                if value is not None:
                    roots.append(Path(value))
        except Exception:
            pass

        candidates: list[_SessionDatabaseCandidate] = []
        seen: set[str] = set()
        for root in roots:
            try:
                directories = _iter_db_directories(root)
            except Exception:
                continue
            for directory in directories:
                session_db = directory / SESSION_DB_NAME
                try:
                    if not session_db.is_file():
                        continue
                except OSError:
                    continue
                identity = _path_identity(session_db)
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(
                    _SessionDatabaseCandidate(
                        session_db=session_db,
                        data_root=_owning_data_root(root, directory),
                    )
                )
        return candidates

    def _resolve_key(self) -> str:
        key = self._db_key or os.environ.get(DB_KEY_ENVIRONMENT_VARIABLE)
        if not key or not key.strip():
            raise KeyUnavailable()
        return key.strip()

    def _resolve_helper(self) -> Path:
        if self._wcdb_cli_path is not None:
            if not self._wcdb_cli_path.exists():
                raise WcdbHelperNotFound()
            return self._wcdb_cli_path

        for candidate in _helper_candidates():
            if candidate.exists():
                self._wcdb_cli_path = candidate
                return candidate
        raise WcdbHelperNotFound()

    def _resolve_library(self) -> Path:
        if self._wcdb_dll_path is not None:
            if not self._wcdb_dll_path.exists():
                raise WcdbLibraryNotFound()
            return self._wcdb_dll_path
        raise WcdbLibraryNotFound()

    def _resolve_data_root(self) -> Path:
        root = self._data_root or default_data_root()
        if root is None or not root.is_dir():
            raise DatabaseNotFound()
        return root

    def _resolve_self_username(self) -> str | None:
        """Return the account identity used by WeChat database rows."""
        root = self._resolve_data_root()
        candidates = _account_directory_names(root)
        if len(candidates) != 1:
            return None

        account_directory = candidates[0]
        match = _ACCOUNT_DIRECTORY_SUFFIX_PATTERN.fullmatch(
            account_directory
        )
        if match is None:
            return account_directory

        canonical_username = match.group("username")
        for directory in _iter_db_directories(root):
            contact_db = directory / CONTACT_DB_NAME
            if not contact_db.is_file():
                continue
            try:
                rows = self._query(
                    contact_db,
                    "SELECT username FROM contact",
                    limit=DEFAULT_MESSAGE_LIMIT,
                    query_stage="self_username",
                )
            except WeChatDatabaseError:
                continue
            if any(
                isinstance(row, Mapping)
                and row.get("username") == canonical_username
                for row in rows
            ):
                return canonical_username
        return account_directory

    def _session_db_path(self) -> Path:
        try:
            root = self._resolve_data_root()
        except DatabaseNotFound:
            _LOGGER.warning("wechat.database.discovery success=false")
            raise
        for candidate in _iter_db_directories(root):
            session_db = candidate / SESSION_DB_NAME
            if session_db.is_file():
                _LOGGER.info("wechat.database.discovery success=true")
                return session_db
        _LOGGER.warning("wechat.database.discovery success=false")
        raise DatabaseNotFound()

    def _find_all_message_dbs(self, table: str) -> list[Path]:
        """Return every message shard that contains the named table."""
        root = self._resolve_data_root()
        shards = [
            shard
            for directory in _iter_db_directories(root)
            for shard in sorted(directory.glob(MESSAGE_DB_GLOB))
        ]
        if not shards:
            raise DatabaseNotFound()

        matched: list[Path] = []
        for shard in shards:
            if self._table_exists(shard, table):
                matched.append(shard)
        if not matched:
            raise SessionNotFound()
        return matched

    def _table_exists(self, db_path: Path, table: str) -> bool:
        escaped = table.replace("'", "''")
        sql = (
            "SELECT name FROM sqlite_master "
            f"WHERE type = 'table' AND name = '{escaped}'"
        )
        # Only a successful empty lookup proves absence. A broken shard must
        # abort discovery rather than silently produce a partial report.
        rows = self._query(
            db_path,
            sql,
            limit=1,
            query_stage="message_table_lookup",
        )
        return bool(rows)

    def _message_table_inventory(self) -> dict[Path, set[str] | None]:
        """Discover tables once per shard; None means discovery failed."""
        root = self._resolve_data_root()
        shards = [
            shard
            for directory in _iter_db_directories(root)
            for shard in sorted(directory.glob(MESSAGE_DB_GLOB))
        ]
        inventory: dict[Path, set[str] | None] = {}
        for shard in shards:
            try:
                rows = self._query(
                    shard,
                    (
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name LIKE 'Msg_%'"
                    ),
                    limit=0,
                    query_stage="message_table_inventory",
                    log_wcdb_error_message=False,
                )
            except WeChatDatabaseError:
                inventory[shard] = None
                continue
            names: set[str] = set()
            for row in rows:
                if (
                    isinstance(row, Mapping)
                    and isinstance(row.get("name"), str)
                ):
                    names.add(row["name"])
            inventory[shard] = names
        return inventory

    def _session_message_counts(
        self, inventory: Mapping[Path, set[str] | None], requested: set[str]
    ) -> dict[str, int]:
        """Return complete stored-row counts, never partial shard totals.

        Missing tables stay unknown rather than zero. An existing empty table
        has a known count of zero. If discovery fails on any shard, no total
        can be proven complete. Failed batches invalidate only their tables.
        """
        if any(names is None for names in inventory.values()):
            return {}
        totals: dict[str, int] = {}
        unknown: set[str] = set()
        for shard, names in inventory.items():
            tables = sorted(requested.intersection(names or ()))
            for offset in range(0, len(tables), _MESSAGE_COUNT_BATCH_SIZE):
                batch = tables[offset:offset + _MESSAGE_COUNT_BATCH_SIZE]
                # Every name here comes from message_table_name(username),
                # not raw schema text or a user-supplied SQL identifier.
                sql = " UNION ALL ".join(
                    f"SELECT '{table}' AS table_name, COUNT(*) AS message_count "
                    f'FROM "{table}"'
                    for table in batch
                )
                try:
                    rows = self._query(
                        shard, sql, limit=0, query_stage="session_message_counts",
                        log_wcdb_error_message=False,
                    )
                except WeChatDatabaseError:
                    unknown.update(batch)
                    continue
                counts: dict[str, int] = {}
                seen: set[str] = set()
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    table = row.get("table_name")
                    if not isinstance(table, str) or table not in batch:
                        continue
                    value = row.get("message_count")
                    if table in seen or type(value) is not int or value < 0:
                        unknown.add(table)
                    else:
                        counts[table] = value
                    seen.add(table)
                unknown.update(set(batch) - counts.keys())
                for table, count in counts.items():
                    totals[table] = totals.get(table, 0) + count
        return {table: count for table, count in totals.items() if table not in unknown}

    def _contact_identities(self) -> dict[str, SenderIdentity]:
        """Keep both supplied contact names, keyed by the source username."""
        root = self._resolve_data_root()
        names: dict[str, SenderIdentity] = {}
        for directory in _iter_db_directories(root):
            contact_db = directory / CONTACT_DB_NAME
            if not contact_db.is_file():
                continue
            try:
                rows = self._query(
                    contact_db,
                    (
                        "SELECT username, remark, nick_name "
                        "FROM contact"
                    ),
                    limit=DEFAULT_MESSAGE_LIMIT,
                    query_stage="contact_names",
                )
            except WeChatDatabaseError:
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                username = row.get("username")
                if not isinstance(username, str) or not username.strip():
                    continue
                display_name = _first_display_name(
                    row.get("remark"),
                    row.get("nick_name"),
                )
                if display_name:
                    names[username] = SenderIdentity(
                        username, display_name,
                        remark=_first_display_name(row.get("remark")),
                        nickname=_first_display_name(row.get("nick_name")),
                    )
        return names


def _helper_candidates() -> list[Path]:
    package_root = Path(__file__).resolve().parents[1]
    project_root = package_root.parents[1]
    return [
        package_root / "native" / "wcdb_cli" / "wcdb_cli.exe",
        project_root / "build" / "wcdb_cli" / "Release" / "wcdb_cli.exe",
        project_root / "build" / "wcdb_cli" / "Debug" / "wcdb_cli.exe",
    ]


def _iter_db_directories(root: Path) -> list[Path]:
    directories: list[Path] = []
    if (root / SESSION_DB_NAME).is_file() or list(root.glob(MESSAGE_DB_GLOB)):
        directories.append(root)

    for name in _DB_STORAGE_DIR_NAMES:
        for candidate in sorted(root.rglob(name)):
            if candidate.is_dir():
                directories.append(candidate)
                directories.extend(
                    child for child in sorted(candidate.iterdir()) if child.is_dir()
                )
    return directories


def _owning_data_root(root: Path, db_directory: Path) -> Path:
    """Map a provider-scanned db directory back to its known account root."""
    if db_directory == root:
        return root
    if db_directory.name in _DB_STORAGE_DIR_NAMES:
        return db_directory.parent
    if db_directory.parent.name in _DB_STORAGE_DIR_NAMES:
        return db_directory.parent.parent
    return root


def _supported_message_types_sql(username: str, prefix: str = "") -> str:
    supported = f"({prefix}local_type & 0xFFFFFFFF) IN (1, 47)"
    if _session_type(username) == "private":
        # Exact verified appmsg subtype 62; other app/system types stay excluded.
        return f"({supported} OR {prefix}local_type = 266287972401)"
    return supported


def _session_type(username: str) -> str:
    if username.endswith("@chatroom"):
        return "group"
    if username.startswith("gh_"):
        return "official"
    return "private"


def _account_directory_names(root: Path) -> list[str]:
    """Return account-style directory names directly under ``root``."""
    candidates: list[str] = []
    if _is_account_directory_name(root.name):
        candidates.append(root.name)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return candidates
    for child in children:
        if child.is_dir() and _is_account_directory_name(child.name):
            candidates.append(child.name)
    return candidates


def _is_account_directory_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(("wxid_", "wx_"))


def _is_conversation_username(username: str) -> bool:
    """Return whether a session is a real private or group conversation."""
    return username.endswith("@chatroom") or username.startswith(
        ("wxid_", "wx_")
    )


def _first_display_name(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _database_type(path: Path) -> str:
    if path.name == SESSION_DB_NAME:
        return "session"
    if path.name == CONTACT_DB_NAME:
        return "contact"
    if path.match(MESSAGE_DB_GLOB):
        return "message"
    return "unknown"


def _safe_stage(value: Any) -> str:
    text = str(value or "unknown").strip()
    return text[:80] or "unknown"


def _build_session_list_sql(columns: set[str]) -> str:
    """Return a SessionTable query that adapts to available columns.

    ``username`` is required; ``summary`` and ``last_timestamp`` are optional.
    If ``last_timestamp`` is absent, no ORDER BY clause is emitted.
    """
    if "username" not in columns:
        raise QueryFailed("SessionTable 缺少必需的 username 列")
    selected = ["username"]
    if "last_timestamp" in columns:
        selected.append("last_timestamp")
    if "summary" in columns:
        selected.append("summary")
    order = " ORDER BY last_timestamp DESC" if "last_timestamp" in columns else ""
    return f"SELECT {', '.join(selected)} FROM {_SESSION_TABLE}{order}"


def _sanitize_wcdb_message(
    message: str, db_path: Path, db_key: str, sql: str | None = None
) -> str:
    """Redact sensitive strings from a WCDB error message before logging."""
    text = str(message or "")
    text = text.replace(str(db_path), "[db_path]")
    if db_key:
        text = text.replace(db_key, "[key]")
    if sql:
        text = text.replace(sql, "[sql]")
    return text


def _log_database_verification(
    *,
    success: bool,
    error_type: str | None,
    wcdb_error_code: Any,
) -> None:
    """Record the verification outcome as a safe state only.

    No database path, account directory, key, SQL text, or chat content is
    ever logged; a failed verification adds only the error type and the
    numeric WCDB code.
    """
    if success:
        _LOGGER.info("wechat.database.verify success=true")
        return
    _LOGGER.warning(
        "wechat.database.verify success=false error_type=%s "
        "wcdb_error_code=%s",
        error_type or "unknown",
        wcdb_error_code if wcdb_error_code is not None else "",
    )


def _log_database_probe_candidate(
    candidate: int,
    *,
    success: bool,
    wcdb_error_code: Any,
) -> None:
    if success:
        _LOGGER.info(
            "wechat.database.probe candidate=%d success=true",
            candidate,
        )
        return
    _LOGGER.warning(
        "wechat.database.probe candidate=%d success=false "
        "wcdb_error_code=%s",
        candidate,
        wcdb_error_code if wcdb_error_code is not None else "",
    )


def _log_query_failure(
    database_type: str,
    query_stage: str,
    *,
    wcdb_stage: str,
    returncode: Any,
    error_type: str,
    wcdb_error_code: Any = None,
    wcdb_error_message: str | None = None,
) -> None:
    _LOGGER.error(
        "wechat.wcdb.failed database_type=%s query_stage=%s "
        "wcdb_stage=%s returncode=%s error_type=%s "
        "wcdb_error_code=%s wcdb_error_message=%s",
        database_type,
        query_stage,
        wcdb_stage,
        returncode,
        error_type,
        wcdb_error_code if wcdb_error_code is not None else "",
        wcdb_error_message or "",
    )


def _log_query_exception(
    database_type: str,
    query_stage: str,
    error: Exception,
) -> None:
    _LOGGER.error(
        "wechat.wcdb.failed database_type=%s query_stage=%s "
        "wcdb_stage=invoke original_error_type=%s",
        database_type,
        query_stage,
        type(error).__name__,
    )


def _parse_result(stdout: str) -> Mapping[str, Any] | None:
    for line in reversed(stdout.strip().splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _path_identity(path: Path) -> str:
    """Return an in-memory-only path identity used for candidate de-duplication."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _run_subprocess(
    command: Sequence[str],
    timeout: int,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess:
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(  # noqa: S603 - resolved executable, list form, no shell
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        check=False,
        env=dict(environment),
        **process_options,
    )
