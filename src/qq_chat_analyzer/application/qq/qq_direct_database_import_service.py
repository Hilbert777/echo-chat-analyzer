"""Acquire one QQ session from NapCat's local plaintext DB snapshot.

NapCat's in-process `DatabaseApi.decryptDatabase` helper owns decryption and
the passphrase.  This service deliberately starts after that boundary: it
acquires one local snapshot generation through the snapshot runtime, asks the
existing provider to materialize one source-specific payload, releases the
generation, and yields that payload to the existing import path.
"""

from __future__ import annotations

from contextlib import contextmanager
from enum import Enum
import json
import logging
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator, NoReturn

from ...identity_names import first_identity_name, identity_search_names
from ...rich_message import SenderIdentity
from ...diagnostics import log_qq_session_event, qq_session_stage

from ...providers.qq_database_provider import QQDatabaseProvider, QQSession
from ...providers.qq_direct_snapshot_runtime import (
    QQDirectSnapshotRuntimeClient,
    QQSnapshotRuntimeError,
    QQSnapshotRuntimeNotReady,
    QQSnapshotRuntimeUnavailable,
    QQSnapshotRuntimeUnauthorized,
    QQSnapshotRuntimeInvalidResponse,
    QQSnapshotRuntimeFailure,
    QQSnapshotWorkerGenerationChanged,
)
from ...providers.napcat_qq_provider import NapCatQQWorkerGenerationChanged, NapCatQQError, NapCatQQAuthRejected
from ...qq_db_identity import QQ_DB_SELF_NAMESPACE, canonical_qq_uin
from ..errors import ApplicationServiceError
from ..shutdown_models import ShutdownStatus
from .qq_environment_config import QQEnvironmentConfigLoader
from .qq_runtime_session import default_qq_runtime_session
from .qq_provider_factory import QQReconnectRequired


_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.qq_direct_database")


def _startup_failure_code(error: Exception) -> str:
    """Fixed diagnostic categories, independent of exception text/code overrides."""
    for types, category in (
        ((NapCatQQWorkerGenerationChanged, QQSnapshotWorkerGenerationChanged), "worker_generation_changed"),
        ((NapCatQQAuthRejected, QQSnapshotRuntimeUnauthorized), "auth_rejected"),
        ((QQSnapshotRuntimeNotReady,), "rpc_method_not_ready"),
        ((QQSnapshotRuntimeUnavailable,), "runtime_unavailable"),
        # Provider's generic error already conflates transport and validation;
        # diagnostics must not turn that lost information into a specific cause.
        ((NapCatQQError,), "provider_error"),
        ((QQSnapshotRuntimeInvalidResponse,), "invalid_response"),
        ((QQSnapshotRuntimeFailure,), "operation_failed"),
        ((QQSnapshotRuntimeError,), "snapshot_runtime_error"),
    ):
        if isinstance(error, types):
            return category
    return "unexpected_error"


_GENERATION_MANIFEST_NAME = "manifest.json"
_MANIFEST_SCHEMA_VERSION = 1

#: Bounded window `shutdown` waits for in-flight acquisitions to drain before
#: it recovers orphan plaintext and reports completion.  This is deliberately
#: finite: a stuck runtime call is never killed and never waited on forever.
DEFAULT_SHUTDOWN_DRAIN_SECONDS = 5.0

#: Maximum wall-clock time for the single shutdown recover RPC. Startup keeps
#: its separate readiness retry policy.
DEFAULT_SHUTDOWN_RECOVER_SECONDS = 5.1
DEFAULT_STARTUP_READINESS_SECONDS = 10.0
DEFAULT_METADATA_TIMEOUT_SECONDS = 5.0
_STARTUP_RETRY_INTERVAL_SECONDS = 0.1


class QQDirectDatabaseState(str, Enum):
    """Application-side lifecycle state owned by the Direct DB service."""

    CLOSED = "closed"
    ACTIVE = "active"
    SHUTTING_DOWN = "shutting_down"


class QQDirectDatabaseUnavailable(ApplicationServiceError):
    """Raised when NapCat has not produced a readable plaintext snapshot."""

    code = "qq_direct_database_unavailable"
    public_message = "QQ local chat data is unavailable. Please finish QQ login and try again."


class QQDirectDatabaseNotReady(ApplicationServiceError):
    """The local bridge or snapshot API has not finished starting."""

    code = "qq_direct_database_not_ready"
    public_message = "QQ local chat data is still starting. Please try again."


class QQDirectSessionNotFound(ApplicationServiceError):
    """Raised when a selected direct DB session is no longer present."""

    code = "qq_direct_session_not_found"
    public_message = "The selected QQ session is unavailable. Refresh the session list and try again."


class QQDirectSnapshotAcquireFailed(ApplicationServiceError):
    """Raised when the snapshot runtime cannot produce a fresh generation."""

    code = "qq_direct_snapshot_acquire_failed"
    public_message = "QQ local chat data is temporarily unavailable. Please confirm QQ is logged in and try again."


class QQDirectSnapshotInvalid(ApplicationServiceError):
    """Raised when a generation fails manifest or database validation."""

    code = "qq_direct_snapshot_invalid"
    public_message = "QQ local chat data is temporarily unavailable. Please try again."


class QQDirectSnapshotCleanupFailed(ApplicationServiceError):
    """Raised when a generation cannot be removed after reading."""

    code = "qq_direct_snapshot_cleanup_failed"
    public_message = "QQ local chat data cleanup failed. Please try again."


class QQDirectDatabaseRecoveryFailed(ApplicationServiceError):
    """Raised when the runtime cannot recover orphan snapshot plaintext."""

    code = "qq_direct_database_recovery_failed"
    public_message = "QQ local chat data recovery failed. Please restart Echo and try again."


class QQDirectDatabaseShuttingDown(ApplicationServiceError):
    """Raised when a new acquisition arrives after shutdown has begun."""

    code = "qq_direct_database_shutting_down"
    public_message = "QQ local chat data is shutting down. Please try again."


@dataclass(frozen=True, slots=True)
class QQDirectDatabaseAcquisition:
    """The local source-specific payload consumed by `ImportService`."""

    payload_path: Path
    self_uin: str | None = None
    session: QQSession | None = None


class QQDirectDatabaseImportService:
    """Small Direct DB acquisition orchestrator for the QQ product path.

    It intentionally delegates DB reads to `QQDatabaseProvider` and leaves
    parsing, importing, analysis, and reporting to their existing services.
    Each acquisition owns exactly one runtime generation: it is validated,
    read, materialized into a `qq-db-json` payload, and released in
    `finally` before the payload is handed to the consumer, so the plaintext
    DB never outlives payload materialization.

    It also owns the application-side lifecycle: before the first generation is
    read, `recover` removes any plaintext left behind by a previous run (a
    crash, a shutdown, or a kill), and `shutdown` drains in-flight
    acquisitions before recovering one last time, so the plaintext snapshot is
    always cleaned up before the QQ runtime is allowed to stop.
    """

    def __init__(
        self,
        *,
        runtime_client: Any | None = None,
        provider_factory: Any | None = None,
        config_loader: QQEnvironmentConfigLoader | None = None,
        shutdown_drain_seconds: float = DEFAULT_SHUTDOWN_DRAIN_SECONDS,
        bridge_credential: object | None = None,
    ) -> None:
        self._runtime_client = runtime_client
        self._provider_factory = provider_factory
        self._config_loader = config_loader or QQEnvironmentConfigLoader()
        self._shutdown_drain_seconds = shutdown_drain_seconds
        self._bridge_credential = bridge_credential or default_qq_runtime_session()
        self._state = QQDirectDatabaseState.CLOSED
        self._startup_attempted = False
        self._shutdown_started = False
        self._shutdown_status = ShutdownStatus.UNKNOWN
        self._active_acquisitions = 0
        self._condition = threading.Condition()
        self._reconnect_required = False
        self._connection_epoch = object()
        self._starting = False
        self._read_cancelled = threading.Event()

    def _require_reconnect(self) -> None:
        with self._condition:
            self._reconnect_required = True
            if self._provider_factory is not None:
                mark = getattr(self._provider_factory, "require_reconnect", None)
                if callable(mark):
                    mark()

    def prepare_reconnect(self) -> None:
        """Reset only after old acquisitions have finished, on explicit connect."""
        with self._condition:
            if self._starting or self._active_acquisitions or self._state is QQDirectDatabaseState.SHUTTING_DOWN:
                raise QQDirectDatabaseShuttingDown()
            self._connection_epoch = object()
            self._runtime_client = None
            self._reconnect_required = False
            self._startup_attempted = False
            self._shutdown_started = False
            self._shutdown_status = ShutdownStatus.UNKNOWN
            self._state = QQDirectDatabaseState.CLOSED
            self._read_cancelled.clear()

    @contextmanager
    def reconnect_guard(self) -> Iterator[None]:
        """Serialize the two in-memory connection resets with task admission.

        The caller must not run authentication or other I/O inside this scope.
        """
        with self._condition:
            yield

    @property
    def state(self) -> QQDirectDatabaseState:
        return self._state

    @property
    def runtime_stop_safe(self) -> bool:
        """Prove shutdown has finished using the runtime, under the task gate."""
        with self._condition:
            return (self._shutdown_started and self._state is QQDirectDatabaseState.CLOSED
                    and not self._starting and not self._active_acquisitions)

    @property
    def shutdown_budget_seconds(self) -> float:
        """Return the bounded worst case ``shutdown`` may spend before it returns.

        The desktop entry point sizes the Direct DB step window from this value,
        so a normal bounded cleanup finishes instead of being abandoned and then
        killed by the process exit in the middle of recovering orphan plaintext.
        """
        return self._shutdown_drain_seconds + DEFAULT_SHUTDOWN_RECOVER_SECONDS

    def start(self, *, connection_epoch: object | None = None) -> None:
        """Recover orphan plaintext and transition to ACTIVE.

        A transient bridge startup failure leaves the service retryable. A real
        recovery failure remains terminal and never permits an acquisition.
        """
        deadline = time.monotonic() + DEFAULT_STARTUP_READINESS_SECONDS
        with self._condition:
            if connection_epoch is None:
                connection_epoch = self._connection_epoch
            while self._starting:
                if connection_epoch is not self._connection_epoch:
                    raise QQReconnectRequired()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QQDirectDatabaseNotReady()
                self._condition.wait(timeout=remaining)
            if connection_epoch is not self._connection_epoch:
                raise QQReconnectRequired()
            if self._reconnect_required or getattr(self._provider_factory, "reconnect_required", False) is True:
                raise QQReconnectRequired()
            if self._state is QQDirectDatabaseState.ACTIVE:
                return
            if self._shutdown_started:
                raise QQDirectDatabaseShuttingDown()
            if self._startup_attempted:
                raise QQDirectDatabaseUnavailable()
            self._startup_attempted = True
            self._starting = True
        try:
            while True:
                with self._condition:
                    if self._shutdown_started:
                        raise QQDirectDatabaseShuttingDown()
                try:
                    client = self._require_runtime_client(deadline=deadline)
                    self._recover_on_startup(deadline=deadline, client=client)
                    break
                except QQDirectDatabaseNotReady:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(_STARTUP_RETRY_INTERVAL_SECONDS, remaining))
            with self._condition:
                if self._shutdown_started:
                    raise QQDirectDatabaseShuttingDown()
                self._state = QQDirectDatabaseState.ACTIVE
        except QQDirectDatabaseNotReady:
            with self._condition:
                self._startup_attempted = False
            raise
        finally:
            with self._condition:
                self._starting = False
                self._condition.notify_all()
    def shutdown(self) -> ShutdownStatus:
        """Transition to SHUTTING_DOWN, drain in-flight acquisitions, then recover.

        Entry, drain, recover and return are each logged with their own boundary
        and elapsed time, because a real shutdown that left an owned launcher
        tree running also left no way to tell which phase had stalled.

        Return only this service's cleanup evidence, not source/runtime exit
        permission. A drain timeout remains TIMED_OUT after late completion;
        recover errors still raise and retain FAILED for repeated calls.
        A repeat during cleanup returns UNKNOWN rather than waiting unbounded.
        """
        started_at = time.monotonic()
        shutdown_deadline = started_at + self.shutdown_budget_seconds
        with self._condition:
            if self._shutdown_started:
                _LOGGER.info("QQ Direct DB shutdown ignored reason=already_started")
                return self._shutdown_status
            self._shutdown_started = True
            if (self._state is QQDirectDatabaseState.CLOSED and not self._starting
                    and not self._startup_attempted and not self._active_acquisitions
                    and self._runtime_client is None):
                _LOGGER.info("QQ Direct DB shutdown skipped reason=closed")
                self._shutdown_status = ShutdownStatus.SUCCESS
                return self._shutdown_status
            self._state = QQDirectDatabaseState.SHUTTING_DOWN
            active = self._active_acquisitions
            self._condition.notify_all()
        _LOGGER.info(
            "QQ Direct DB shutdown entered state=shutting_down active=%s",
            active,
        )
        drained = self._drain_acquisitions()
        quiescent = drained
        if not drained:
            # Stop owned database readers before recover tries to unlink their
            # snapshot. The original drain timeout remains part of the result.
            self._read_cancelled.set()
            quiescent = self._drain_acquisitions(
                deadline=min(shutdown_deadline, time.monotonic() + 2.0))
        with self._condition:
            active = self._active_acquisitions
            # Never construct a new binding during shutdown, even if startup
            # timed out while still resolving the old client.
            client = self._runtime_client
        _LOGGER.info(
            "QQ Direct DB drain %s active=%s elapsed=%.2fs",
            "completed" if drained else "timed out",
            active,
            time.monotonic() - started_at,
        )
        if not quiescent:
            # Keep admission sealed and the runtime/credential available to the
            # live owner. Its finally block still owns generation cleanup.
            with self._condition:
                self._shutdown_status = ShutdownStatus.TIMED_OUT
            _LOGGER.warning("QQ Direct DB recovery deferred reason=tasks_not_drained")
            return self._shutdown_status
        _LOGGER.info("QQ Direct DB recover started")
        try:
            if client is None:
                raise QQDirectDatabaseRecoveryFailed()
            self._recover_on_shutdown(
                client=client,
                deadline=min(
                    shutdown_deadline,
                    time.monotonic() + DEFAULT_SHUTDOWN_RECOVER_SECONDS,
                ),
            )
        except Exception as error:
            with self._condition:
                self._shutdown_status = ShutdownStatus.FAILED
            _LOGGER.warning(
                "QQ Direct DB recover failed error=%s",
                type(error).__name__,
            )
            raise
        else:
            _LOGGER.info("QQ Direct DB recover completed")
            with self._condition:
                # CLOSED alone is not proof: drain can time out while recover
                # succeeds, and late completion cannot repair that timeout.
                self._shutdown_status = (
                    ShutdownStatus.SUCCESS
                    if drained and not self._starting and not self._active_acquisitions
                    else ShutdownStatus.TIMED_OUT
                )
        finally:
            with self._condition:
                self._state = QQDirectDatabaseState.CLOSED
            _LOGGER.info(
                "QQ Direct DB shutdown returned state=closed elapsed=%.2fs",
                time.monotonic() - started_at,
            )
        return self._shutdown_status
    def list_sessions(self) -> list[QQSession]:
        """Discover sessions from one fresh generation, then release it.

        The generation is cleaned up in `finally` so the plaintext DB never
        outlives this call.
        """
        with qq_session_stage(_LOGGER, "service"):
            client = self._begin_acquisition()
            try:
                generation_id: str | None = None
                try:
                    generation_id = client.acquire()
                    database_path, self_uin = _validated_generation(
                        client.generation_directory(generation_id),
                        generation_id,
                        cancelled=self._read_cancelled.is_set,
                    )
                    provider = QQDatabaseProvider(database_path, cancelled=self._read_cancelled.is_set)
                    sessions = provider.list_sessions(self_uin=self_uin)
                except QQSnapshotWorkerGenerationChanged as error:
                    self._require_reconnect()
                    raise QQReconnectRequired() from error
                except QQSnapshotRuntimeError as error:
                    raise QQDirectSnapshotAcquireFailed() from error
                finally:
                    if generation_id is not None:
                        try:
                            with qq_session_stage(_LOGGER, "cleanup"):
                                client.cleanup(generation_id)
                        except QQSnapshotWorkerGenerationChanged as error:
                            self._require_reconnect()
                            raise QQReconnectRequired() from error
                        except QQSnapshotRuntimeError as error:
                            raise QQDirectSnapshotCleanupFailed() from error
                with qq_session_stage(_LOGGER, "names"):
                    return self._named_sessions(sessions)
            finally:
                self._end_acquisition()

    @contextmanager
    def acquired_session(
        self,
        session_id: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> Iterator[QQDirectDatabaseAcquisition]:
        """Materialize one selected session from one fresh generation.

        One runtime generation is acquired exactly once.  Its manifest and
        database are validated with the same helper used by
        :meth:list_sessions; the target session is located by typed identity
        and materialized from that same generation DB.  The generation is
        removed in `finally` before the payload is yielded, so the plaintext
        DB is released as soon as the `qq-db-json` payload is fully written
        and no tokenizer/analyzer/report ever holds the plaintext DB.
        """
        client = self._begin_acquisition()
        try:
            started_at = time.perf_counter()
            generation_id: str | None = None
            scratch_context: TemporaryDirectory | None = None
            try:
                try:
                    generation_id = client.acquire()
                    database_path, self_uin = _validated_generation(
                        client.generation_directory(generation_id),
                        generation_id,
                        cancelled=self._read_cancelled.is_set,
                    )
                    snapshot_at = time.perf_counter()
                    provider = QQDatabaseProvider(database_path, cancelled=self._read_cancelled.is_set)
                    session = next(
                        (
                            candidate
                            for candidate in provider.list_sessions(self_uin=self_uin)
                            if candidate.session_id == session_id
                        ),
                        None,
                    )
                    lookup_at = time.perf_counter()
                    if session is None:
                        raise QQDirectSessionNotFound()
                    scratch_context = TemporaryDirectory(prefix="chat-analyzer-qq-db-")
                    payload_path = provider.materialize_session_payload(
                        session,
                        Path(scratch_context.name) / "qq-db-session.json",
                        start_time=start_time,
                        end_time=end_time,
                        self_uin=self_uin,
                    )
                    materialized_at = time.perf_counter()
                    acquisition = QQDirectDatabaseAcquisition(
                        payload_path=payload_path,
                        self_uin=self_uin,
                        session=session,
                    )
                except QQSnapshotWorkerGenerationChanged as error:
                    self._require_reconnect()
                    raise QQReconnectRequired() from error
                except QQSnapshotRuntimeError as error:
                    raise QQDirectSnapshotAcquireFailed() from error
                finally:
                    if generation_id is not None:
                        try:
                            with qq_session_stage(_LOGGER, "cleanup"):
                                client.cleanup(generation_id)
                        except QQSnapshotWorkerGenerationChanged as error:
                            self._require_reconnect()
                            raise QQReconnectRequired() from error
                        except QQSnapshotRuntimeError as error:
                            raise QQDirectSnapshotCleanupFailed() from error
                cleaned_at = time.perf_counter()
                group_names, friend_names = self._metadata_names()
                metadata_at = time.perf_counter()
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
                if session.session_type == "group":
                    try:
                        sender_names = _group_sender_names_with_diagnostics(client, session, payload)
                    except QQSnapshotWorkerGenerationChanged as error:
                        self._require_reconnect()
                        raise QQReconnectRequired() from error
                    sender_names = {
                        **friend_names,
                        **{
                            uin: replace(identity, remark=friend_names[uin].remark,
                                         nickname=identity.nickname or friend_names[uin].nickname)
                            if uin in friend_names else identity
                            for uin, identity in sender_names.items()
                        },
                    }
                else:
                    sender_names = friend_names
                    try:
                        context = _private_nudge_identity_context(client, session, self_uin)
                    except QQSnapshotWorkerGenerationChanged as error:
                        self._require_reconnect()
                        raise QQReconnectRequired() from error
                    except QQSnapshotRuntimeError as error:
                        # Authentication/transport failure cannot authorize identity
                        # inference from an unbound source or stale account.
                        raise QQDirectSnapshotAcquireFailed() from error
                    if context is not None:
                        payload["private_nudge_context"] = context
                members_at = time.perf_counter()
                named_session = self._named_sessions(
                    [session], group_names=group_names, friend_names=friend_names
                )[0]
                _attach_sender_names(payload_path, payload, sender_names, self_uin)
                # Do not retain the full decoded payload while the consumer analyzes it.
                del payload
                ready_at = time.perf_counter()
                _LOGGER.info(
                    "[analysis-timing] stage=direct_db_acquisition elapsed_ms=%d "
                    "snapshot_ms=%d lookup_ms=%d materialize_ms=%d "
                    "cleanup_ms=%d metadata_ms=%d member_ms=%d enrich_ms=%d",
                    _elapsed_ms(started_at, ready_at),
                    _elapsed_ms(started_at, snapshot_at),
                    _elapsed_ms(snapshot_at, lookup_at),
                    _elapsed_ms(lookup_at, materialized_at),
                    _elapsed_ms(materialized_at, cleaned_at),
                    _elapsed_ms(cleaned_at, metadata_at),
                    _elapsed_ms(metadata_at, members_at),
                    _elapsed_ms(members_at, ready_at),
                )
                yield replace(acquisition, session=named_session)
            finally:
                if scratch_context is not None:
                    scratch_context.cleanup()
        finally:
            self._end_acquisition()

    def _named_sessions(
        self,
        sessions: list[QQSession],
        *,
        group_names: dict[str, str] | None = None,
        friend_names: dict[str, SenderIdentity] | None = None,
    ) -> list[QQSession]:
        """Join DB session keys to the existing QQ metadata provider."""
        if group_names is None or friend_names is None:
            group_names, friend_names = self._metadata_names()
        named = []
        for session in sessions:
            if session.session_type == "group":
                name = group_names.get(session.session_object)
                search_names = identity_search_names(name)
                fallback = "未知群聊"
            else:
                identity = (
                    friend_names.get(session.peer_uin)
                    or friend_names.get(session.session_object)
                )
                name = identity.display_name if identity else None
                search_names = identity_search_names(name, identity.remark, identity.nickname) if identity else ()
                fallback = "未知联系人"
            named.append(replace(session, display_name=first_identity_name(name) or fallback, search_names=search_names))
        return named

    def _metadata_names(self) -> tuple[dict[str, str], dict[str, SenderIdentity]]:
        group_names: dict[str, str] = {}
        friend_names: dict[str, SenderIdentity] = {}
        # Names are optional. Two default 30 s RPC waits must not hold an
        # already-read session list for another minute on a slow runtime.
        deadline = time.monotonic() + DEFAULT_METADATA_TIMEOUT_SECONDS
        if self._provider_factory is not None:
            try:
                with qq_session_stage(_LOGGER, "names_provider"):
                    provider = self._provider_factory.create()
            except QQReconnectRequired:
                raise
            except Exception:
                return group_names, friend_names
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log_qq_session_event(_LOGGER, "group_names_rpc", "skipped", failure_code="budget_exhausted")
                return group_names, friend_names
            try:
                with qq_session_stage(_LOGGER, "group_names_rpc"):
                    groups = provider.list_groups(timeout=remaining)
                group_names = {
                    group.group_code: group.group_name
                    for group in groups
                    if first_identity_name(group.group_name)
                }
            except NapCatQQWorkerGenerationChanged as error:
                self._require_reconnect()
                raise QQReconnectRequired() from error
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log_qq_session_event(_LOGGER, "friend_names_rpc", "skipped", failure_code="budget_exhausted")
                return group_names, friend_names
            try:
                with qq_session_stage(_LOGGER, "friend_names_rpc"):
                    friends = provider.list_friends(timeout=remaining)
                friend_names = {
                    friend.uin: SenderIdentity(
                        friend.uin, friend.display_name,
                        remark=first_identity_name(getattr(friend, "remark", None)),
                        nickname=first_identity_name(getattr(friend, "nickname", None)),
                    )
                    for friend in friends
                    if friend.uin
                    and first_identity_name(friend.display_name)
                    and friend.display_name != friend.uin
                }
            except NapCatQQWorkerGenerationChanged as error:
                self._require_reconnect()
                raise QQReconnectRequired() from error
            except Exception:
                # Names are optional metadata; the local message acquisition
                # remains usable if the metadata endpoint is unavailable.
                pass
        return group_names, friend_names

    # -------------------------------------------------------------- lifecycle

    def _ensure_usable(self, *, connection_epoch: object) -> None:
        """Ensure startup recover has run, or refuse a non-active service.

        The first call auto-starts the service (recover -> ACTIVE).  After a
        failed startup or a completed shutdown the service is terminal and
        raises without ever reading a generation.
        """
        with self._condition:
            if connection_epoch is not self._connection_epoch:
                raise QQReconnectRequired()
            if self._reconnect_required or getattr(self._provider_factory, "reconnect_required", False) is True:
                raise QQReconnectRequired()
            if self._state is QQDirectDatabaseState.ACTIVE:
                return
            if self._shutdown_started:
                raise QQDirectDatabaseShuttingDown()
            if self._startup_attempted and not self._starting:
                raise QQDirectDatabaseUnavailable()
        # start owns its short state transitions, not the HTTP recovery lock.
        self.start(connection_epoch=connection_epoch)
    def _recover_on_startup(self, *, deadline: float, client: Any) -> None:
        """Recover orphan plaintext during startup."""
        _LOGGER.info("QQ Direct DB stage=startup_recover outcome=started")
        try:
            client.recover(deadline=deadline)
        except QQSnapshotWorkerGenerationChanged as error:
            _LOGGER.info("QQ Direct DB stage=startup_recover outcome=failed failure_code=%s", _startup_failure_code(error))
            self._require_reconnect()
            raise QQReconnectRequired() from error
        except (QQSnapshotRuntimeNotReady, QQSnapshotRuntimeUnavailable) as error:
            _LOGGER.info("QQ Direct DB stage=startup_recover outcome=failed failure_code=%s", _startup_failure_code(error))
            raise QQDirectDatabaseNotReady() from error
        except QQSnapshotRuntimeError as error:
            _LOGGER.info("QQ Direct DB stage=startup_recover outcome=failed failure_code=%s", _startup_failure_code(error))
            raise QQDirectDatabaseRecoveryFailed() from error
        _LOGGER.info("QQ Direct DB stage=startup_recover outcome=success")

    def _begin_acquisition(self) -> Any:
        """Atomically admit a task with the client of its original connection."""
        with self._condition:
            epoch = self._connection_epoch
        self._ensure_usable(connection_epoch=epoch)
        with self._condition:
            # The runtime owns a single published generation. Its next acquire
            # recovers the previous one, so serialize the complete consumer
            # lifetime, including SQLite reads and cleanup, on this side too.
            while self._active_acquisitions:
                if epoch is not self._connection_epoch:
                    raise QQReconnectRequired()
                if self._shutdown_started or self._reconnect_required:
                    raise QQDirectDatabaseShuttingDown()
                self._condition.wait()
            if epoch is not self._connection_epoch:
                raise QQReconnectRequired()
            if self._reconnect_required or getattr(self._provider_factory, "reconnect_required", False) is True:
                raise QQReconnectRequired()
            if self._state is not QQDirectDatabaseState.ACTIVE:
                raise QQDirectDatabaseShuttingDown()
            client = self._runtime_client
            if client is None:
                raise QQDirectDatabaseUnavailable()
            self._active_acquisitions += 1
            return client

    def _end_acquisition(self) -> None:
        """Release one in-flight acquisition and wake any shutdown drain."""
        with self._condition:
            self._active_acquisitions -= 1
            if self._active_acquisitions == 0:
                self._condition.notify_all()

    def _drain_acquisitions(self, *, deadline: float | None = None) -> bool:
        """Wait, up to a bounded window, for in-flight acquisitions to finish.

        Returns ``True`` when every in-flight acquisition finished inside the
        window, and ``False`` when the window ran out; the caller reports which.
        """
        if deadline is None:
            deadline = time.monotonic() + self._shutdown_drain_seconds
        with self._condition:
            _LOGGER.info(
                "QQ Direct DB drain started active=%s window=%.2fs",
                self._active_acquisitions,
                self._shutdown_drain_seconds,
            )
            while self._active_acquisitions > 0 or self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
        return True

    def _recover_on_shutdown(self, *, deadline: float, client: Any) -> None:
        """Recover orphan plaintext one last time before the runtime stops."""
        try:
            client.recover(deadline=deadline)
        except QQSnapshotRuntimeError as error:
            raise QQDirectDatabaseRecoveryFailed() from error

    def _require_runtime_client(self, *, deadline: float | None = None) -> Any:
        """Return the injected runtime client, or build one from config."""
        if self._runtime_client is not None:
            return self._runtime_client
        config = self._environment_config()
        try:
            paths = self._config_loader.runtime_paths()
        except Exception:
            _LOGGER.info("QQ Direct DB stage=snapshot_client outcome=failed failure_code=runtime_paths_unavailable")
            raise QQDirectDatabaseUnavailable() from None
        base_url = config.napcat_bridge_url or "http://127.0.0.1:40655"
        if self._provider_factory is not None:
            provider = self._provider_factory.create()
            builder = getattr(provider, "snapshot_client", None)
            if callable(builder):
                _LOGGER.info("QQ Direct DB stage=provider_status outcome=started")
                try:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise QQDirectDatabaseNotReady()
                    status = provider.status(timeout=None if deadline is None else deadline - time.monotonic())
                except NapCatQQWorkerGenerationChanged as error:
                    _LOGGER.info("QQ Direct DB stage=provider_status outcome=failed failure_code=%s", _startup_failure_code(error))
                    self._require_reconnect()
                    raise QQReconnectRequired() from error
                except NapCatQQAuthRejected as error:
                    _LOGGER.info("QQ Direct DB stage=provider_status outcome=failed failure_code=%s", _startup_failure_code(error))
                    raise QQDirectDatabaseRecoveryFailed() from error
                except NapCatQQError as error:
                    _LOGGER.info("QQ Direct DB stage=provider_status outcome=failed failure_code=%s", _startup_failure_code(error))
                    raise QQDirectDatabaseNotReady() from error
                expected = paths.runtime_id if config.runtime_mode == "managed" else None
                if status.runtime_id != expected:
                    _LOGGER.info("QQ Direct DB stage=provider_status outcome=failed failure_code=runtime_identity_mismatch")
                    raise QQDirectDatabaseUnavailable()
                _LOGGER.info("QQ Direct DB stage=provider_status outcome=success")
                _LOGGER.info(
                    "QQ Direct DB readiness bridge=%s online=%s database_api=%s passphrase=%s snapshot_api=%s",
                    status.bridge_ready is True, status.qq_online is True,
                    status.database_api_ready is True, status.passphrase_ready is True,
                    status.snapshot_api_ready is True,
                )
                _LOGGER.info("QQ Direct DB stage=snapshot_client outcome=started")
                try:
                    self._runtime_client = builder(paths.snapshot_root)
                except Exception as error:
                    _LOGGER.info("QQ Direct DB stage=snapshot_client outcome=failed failure_code=%s", _startup_failure_code(error))
                    raise
                _LOGGER.info("QQ Direct DB stage=snapshot_client outcome=success")
                return self._runtime_client
        self._runtime_client = QQDirectSnapshotRuntimeClient(
            base_url=base_url,
            snapshot_root=paths.snapshot_root,
            runtime_id=paths.runtime_id if config.runtime_mode == "managed" else None,
            credential=self._bridge_credential,
        )
        return self._runtime_client

    def _environment_config(self) -> Any:
        try:
            return self._config_loader.load_or_default()
        except Exception:
            raise QQDirectDatabaseUnavailable() from None


def _private_nudge_identity_context(client: Any, session: QQSession, self_uin: str) -> dict[str, str] | None:
    """Bind the selected private partition to a unique authenticated UID pair."""
    fetch = getattr(client, "get_private_identity_metadata", None)
    if session.session_type != "private" or not callable(fetch):
        log_qq_session_event(_LOGGER, "private_nudge_identity", "skipped", failure_code="metadata_unavailable")
        return None
    with qq_session_stage(_LOGGER, "private_nudge_metadata"):
        metadata = fetch(timeout=DEFAULT_METADATA_TIMEOUT_SECONDS)
    if not isinstance(metadata, Mapping):
        return None
    owner, friends = metadata.get("self_info"), metadata.get("friends")
    if not isinstance(owner, Mapping) or not isinstance(friends, list):
        return None
    if owner.get("uin") != self_uin:
        log_qq_session_event(_LOGGER, "private_nudge_identity", "skipped", failure_code="identity_mismatch")
        return None
    # The external session object may be a UID rather than a UIN. Only exact
    # authenticated metadata matches are useful; display names are never keys.
    peers = [row for row in friends if isinstance(row, Mapping) and (
        (session.peer_uin and row.get("uin") == session.peer_uin)
        or (session.session_object and session.session_object in (row.get("uin"), row.get("uid")))
    )]
    if len(peers) != 1:
        log_qq_session_event(_LOGGER, "private_nudge_identity", "skipped", failure_code="peer_ambiguous")
        return None
    peer = peers[0]
    for row in (owner, peer):
        if (not isinstance(row.get("uin"), str) or re.fullmatch(r"[1-9][0-9]{4,19}", row["uin"]) is None
                or not isinstance(row.get("uid"), str) or re.fullmatch(r"u_[A-Za-z0-9_-]{10,64}", row["uid"]) is None):
            return None
    if owner["uid"] == peer["uid"] or owner["uin"] == peer["uin"]:
        return None
    for row in friends:
        if not isinstance(row, Mapping):
            continue
        for party in (owner, peer):
            if ((row.get("uid") == party["uid"] and row.get("uin") != party["uin"])
                    or (row.get("uin") == party["uin"] and row.get("uid") != party["uid"])):
                log_qq_session_event(_LOGGER, "private_nudge_identity", "skipped", failure_code="identity_conflict")
                return None
    log_qq_session_event(_LOGGER, "private_nudge_identity", "ok")
    return {
        "namespace": QQ_DB_SELF_NAMESPACE, "session_key": session.internal_key,
        "conversation_id": session.conversation_id,
        "owner_uin": self_uin, "owner_uid": owner["uid"],
        "peer_uin": peer["uin"], "peer_uid": peer["uid"],
        "peer_name": first_identity_name(peer.get("remark"), peer.get("nickname")) or "对方",
    }


def _group_sender_names_with_diagnostics(
    client: Any,
    session: QQSession,
    payload: Any,
) -> dict[str, SenderIdentity]:
    """Fetch optional member names and log anonymous identity coverage."""
    get_members = getattr(client, "get_group_member_all", None)
    sender_names: dict[str, SenderIdentity] = {}
    member_data = _group_member_data(None)
    rpc_status = "api_unavailable"
    if callable(get_members):
        try:
            member_result = get_members(session.session_object)
            _log_group_member_shape(member_result)
            member_data = _group_member_data(member_result)
            sender_names = member_data["names"]
            rpc_status = member_data["status"]
        except QQSnapshotWorkerGenerationChanged:
            raise
        except Exception:
            # Member metadata is optional; do not fail local DB analysis.
            rpc_status = "rpc_failed"
    participant_uins = _payload_sender_uins(payload)
    coverage = _identity_coverage_counts(participant_uins, member_data)
    _LOGGER.info(
        "[qq-direct-identity-coverage] status=%s "
        "distinct_sender_count=%d rpc_member_count=%d "
        "matched_sender_count=%d unmatched_sender_count=%d "
        "matched_with_card_count=%d "
        "matched_without_card_with_nick_count=%d "
        "matched_without_card_or_nick_count=%d "
        "rpc_member_with_card_count=%d rpc_member_with_nick_count=%d",
        rpc_status,
        coverage["distinct_sender_count"],
        coverage["rpc_member_count"],
        coverage["matched_sender_count"],
        coverage["unmatched_sender_count"],
        coverage["matched_with_card_count"],
        coverage["matched_without_card_with_nick_count"],
        coverage["matched_without_card_or_nick_count"],
        coverage["rpc_member_with_card_count"],
        coverage["rpc_member_with_nick_count"],
    )
    return sender_names


def _validated_generation(
    generation_directory: Path,
    generation_id: str,
    *,
    cancelled=lambda: False,
) -> tuple[Path, str]:
    """Validate one generation's manifest and return its database and self UIN.

    Fail closed on any mismatch: the manifest must declare the exact
    `schema_version`, the exact `generation_id` returned by the runtime, a
    `ready` state, a strictly safe same-directory database name, and a
    canonicalizable `qq_uin` identity.  The database file must then exist.
    This is the single manifest-validation helper shared by `list_sessions`
    and `acquired_session`; no second validation path exists.
    """
    with qq_session_stage(_LOGGER, "manifest"):
        def reject(reason: str) -> NoReturn:
            # Caller-owned literals only; never serialize the manifest or identity.
            _LOGGER.info("QQ snapshot stage=manifest outcome=invalid guard_code=%s", reason)
            raise QQDirectSnapshotInvalid() from None

        manifest_path = generation_directory / _GENERATION_MANIFEST_NAME
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError:
            reject("manifest_unreadable")
        except (UnicodeDecodeError, json.JSONDecodeError):
            reject("manifest_encoding_invalid")
        if not isinstance(payload, dict):
            reject("manifest_shape_invalid")
        if payload.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
            reject("schema_version_mismatch")
        if payload.get("generation_id") != generation_id:
            reject("generation_mismatch")
        if payload.get("state") != "ready":
            reject("generation_not_ready")

        database_name = _safe_relative_name(payload.get("database"))
        if database_name is None:
            reject("database_name_invalid")

        identity = payload.get("identity")
        if not isinstance(identity, dict):
            reject("identity_shape_invalid")
        if identity.get("namespace") != QQ_DB_SELF_NAMESPACE:
            reject("identity_namespace_invalid")
        self_uin = canonical_qq_uin(identity.get("value"))
        if self_uin is None:
            reject("identity_value_invalid")

        database_path = generation_directory / database_name
        if not database_path.is_file():
            reject("database_missing")
    # Runtime-side file stability telemetry prevents known torn reads. SQLite's
    # own quick_check is the independent final integrity gate before the app
    # exposes the generation to session discovery or materialization.
    with qq_session_stage(_LOGGER, "quick_check"):
        if QQDatabaseProvider.quick_check(database_path, cancelled=cancelled) != "ok":
            _LOGGER.info("QQ snapshot stage=quick_check outcome=invalid guard_code=sqlite_integrity_failed")
            raise QQDirectSnapshotInvalid()
    return database_path, self_uin


def _safe_relative_name(value: Any) -> str | None:
    """Return a single safe same-directory name, or None when unsafe."""
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    return name


def _attach_sender_names(
    payload_path: Path,
    payload: Any,
    friend_names: dict[str, SenderIdentity],
    self_uin: str | None,
) -> None:
    """Add known QQ names to the transient payload after DB cleanup."""
    for record in payload.get("records", ()):
        fields = record.get("fields", {})
        sender_uin = canonical_qq_uin(fields.get("40033"))
        if sender_uin is None or sender_uin == self_uin:
            continue
        identity = friend_names.get(sender_uin)
        if identity:
            record["sender"] = {
                "displayName": identity.display_name, "remark": identity.remark,
                "nickname": identity.nickname, "groupCard": identity.contextual_name,
            }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _elapsed_ms(started_at: float, ended_at: float) -> int:
    return max(0, round((ended_at - started_at) * 1000))


def _log_group_member_shape(result: Any) -> None:
    """Log only counts for fixed name fields in the raw member RPC response."""
    if not isinstance(result, Mapping):
        return
    inner = result.get("result")
    if not isinstance(inner, Mapping):
        return
    members = inner.get("infos")
    if not isinstance(members, Mapping):
        return

    fields = ("cardName", "card", "nick", "nickname", "remark", "displayName")
    counts = {field: [0, 0, 0, 0, 0] for field in fields}
    for member in members.values():
        if not isinstance(member, Mapping):
            continue
        for field in fields:
            if field not in member:
                continue
            field_counts = counts[field]
            field_counts[0] += 1  # present
            value = member[field]
            if isinstance(value, str):
                field_counts[2] += 1  # string
                if value.strip():
                    field_counts[1] += 1  # nonempty string
            elif value is None:
                field_counts[3] += 1  # null
            else:
                field_counts[4] += 1  # other type

    parts = [f"member_count={len(members)}"]
    for field in fields:
        present, nonempty, string, null, other = counts[field]
        parts.extend((
            f"{field}_present_count={present}",
            f"{field}_nonempty_string_count={nonempty}",
            f"{field}_string_count={string}",
            f"{field}_null_count={null}",
            f"{field}_other_type_count={other}",
        ))
    _LOGGER.debug("[qq-direct-member-shape] %s", " ".join(parts))


def _group_member_data(result: Any) -> dict[str, Any]:
    """Read the verified NapCat ``result.infos`` member map and safe counts."""
    empty = {
        "status": "invalid_response", "names": {}, "uins": set(),
        "card_uins": set(), "nick_uins": set(),
    }
    if not isinstance(result, Mapping):
        return empty
    infos = result.get("result")
    if not isinstance(infos, Mapping):
        return empty
    members = infos.get("infos")
    if not isinstance(members, Mapping):
        return empty
    empty["status"] = "ok"
    names: dict[str, str] = {}
    uins: set[str] = set()
    card_uins: set[str] = set()
    nick_uins: set[str] = set()
    for member in members.values():
        if not isinstance(member, Mapping):
            continue
        uin = canonical_qq_uin(member.get("uin"))
        card = first_identity_name(member.get("cardName"))
        nick = first_identity_name(member.get("nick"))
        if uin:
            uins.add(uin)
            if card:
                card_uins.add(uin)
            if nick:
                nick_uins.add(uin)
        name = first_identity_name(card, nick)
        if uin and name:
            names[uin] = SenderIdentity(uin, name, nickname=nick, contextual_name=card)
    return {
        "status": "ok",
        "names": names,
        "uins": uins,
        "card_uins": card_uins,
        "nick_uins": nick_uins,
    }


def _identity_coverage_counts(
    participant_uins: set[str], member_data: Mapping[str, Any]
) -> dict[str, int]:
    """Summarize distinct sender/member UIN coverage without retaining values."""
    member_uins = member_data["uins"]
    card_uins = member_data["card_uins"]
    nick_uins = member_data["nick_uins"]
    matched = participant_uins & member_uins
    matched_with_card = matched & card_uins
    matched_with_nick_only = matched & (nick_uins - card_uins)
    matched_without_names = matched - card_uins - nick_uins
    return {
        "distinct_sender_count": len(participant_uins),
        "rpc_member_count": len(member_uins),
        "matched_sender_count": len(matched),
        "unmatched_sender_count": len(participant_uins - member_uins),
        "matched_with_card_count": len(matched_with_card),
        "matched_without_card_with_nick_count": len(matched_with_nick_only),
        "matched_without_card_or_nick_count": len(matched_without_names),
        "rpc_member_with_card_count": len(card_uins),
        "rpc_member_with_nick_count": len(nick_uins),
    }


def _payload_sender_uins(payload: Any) -> set[str]:
    records = payload.get("records", ()) if isinstance(payload, Mapping) else ()
    return {
        sender
        for record in records
        if isinstance(record, Mapping)
        and isinstance(record.get("fields"), Mapping)
        if (sender := canonical_qq_uin(record["fields"].get("40033"))) is not None
    }


__all__ = [
    "DEFAULT_SHUTDOWN_DRAIN_SECONDS",
    "QQDirectDatabaseAcquisition",
    "QQDirectDatabaseImportService",
    "QQDirectDatabaseRecoveryFailed",
    "QQDirectDatabaseShuttingDown",
    "QQDirectDatabaseState",
    "QQDirectDatabaseUnavailable",
    "QQDirectDatabaseNotReady",
    "QQDirectSessionNotFound",
    "QQDirectSnapshotAcquireFailed",
    "QQDirectSnapshotCleanupFailed",
    "QQDirectSnapshotInvalid",
]
