"""Minimal local-only HTTP client for NapCat's `EchoSnapshotApi` RPC.

The Direct DB snapshot helper exposes `runtimeCore.apis.EchoSnapshotApi` over
NapCat's in-process `/rpc` bridge (`napcat_bridge_url`).  This module is a
thin client that may only invoke the three frozen methods `acquire` /
`cleanup` / `recover`:

* `acquire`    -> a fresh opaque `generation_id` for a ready snapshot
* `cleanup`    -> remove one generation by its id
* `recover`    -> remove orphan staging / legacy plaintext artifacts

It never accepts an absolute database path from the runtime.  Callers compute a
generation directory from the returned opaque id under a known snapshot root,
and the id is strictly validated so path traversal can never escape that root.
Additional fixed metadata requests resolve group senders and bind private
action UIDs to the same launch/account as the acquired snapshot.

Privacy contract: error objects may only carry stable codes and privacy-safe
messages.  A runtime error message, the QQ UIN, nicknames, message text, the
passphrase and absolute paths never reach an exception's public message.
"""

from __future__ import annotations

import json
import re
import logging
import time
import urllib.error
import urllib.request
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .bridge_credential import authorization_headers, resolve_bridge_credential


DEFAULT_RPC_TIMEOUT_SECONDS = 30
# Capture plus streamed decryption of a 6 GiB database exceeds 65 seconds on
# the affected machine. Keep one bounded budget shared by all attempts.
DEFAULT_ACQUIRE_RPC_TIMEOUT_SECONDS = 120
# ``snapshot_unstable`` is the single error code the snapshot helper returns
# whenever it refuses to publish a capture it cannot prove stable. It is a
# failure class, not one condition: it covers the whole capture path plus the
# input-stability witnesses (source location, workspace/lease, WAL
# identity/header/prefix/checksum/commit boundary, main identity, checkpoint
# witness, and the input witness taken before and after decrypt). Only the
# helper's privacy-safe ``failure_stage`` / ``guard_code`` diagnostics name the
# concrete cause.
# The cause confirmed on the affected machine was the guard
# ``main_changed_during_read`` (failure_stage ``main_stability``): the main
# database metadata or content changed inside the read window. A WAL checkpoint or QQ's post-login
# sync can produce that, but so can any other concurrent writer, so the guard is
# one member of the ``snapshot_unstable`` class rather than its definition.
# That class of instability is transient, yet one attempt is not cheap: it
# re-acquires the OS lease and re-reads the whole source, so a large database
# makes a single attempt take several seconds. A three-attempt limit therefore
# expired while the source was still settling and surfaced as a permanent
# connection failure.
# Six attempts is the maximum, but they are not six fresh timeouts: the 120s
# is the budget of the whole ``acquire()`` loop (one monotonic deadline), not the
# timeout of a single RPC, so a burst of retries can never accumulate into
# six separate waits. No consistency guard is relaxed: every attempt keeps the
# same all-or-nothing capture contract.
_SNAPSHOT_UNSTABLE_MAX_ATTEMPTS = 6
_SNAPSHOT_UNSTABLE_RETRY_DELAY_SECONDS = 0.25
# Explicitly recoverable decryption failures may reflect a late key update.
# Never infer recoverability from a generic failure or spend a new timeout.
_DECRYPT_MAX_ATTEMPTS = 3
_DECRYPT_RETRY_DELAY_SECONDS = 1.0
RECOVER_READINESS_ATTEMPTS = 51
RECOVER_READINESS_POLL_SECONDS = 0.1
RPC_METHOD_NAMESPACE = "EchoSnapshotApi"
GENERATIONS_DIRECTORY_NAME = "generations"

_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.qq_direct_snapshot")

_ACQUIRE = "acquire"
_CLEANUP = "cleanup"
_RECOVER = "recover"
_GROUP_MEMBER_ALL = "getGroupMemberAll"
_SAFE_ACQUIRE_FAILURE_CODES = frozenset({
    "cleanup_failed",
    "passphrase_unavailable",
    "identity_missing",
    "decrypt_failed",
    "snapshot_unstable",
    "identity_changed",
    "manifest_failed",
    "publish_failed",
})

class QQSnapshotRuntimeError(Exception):
    """Base, privacy-safe error for snapshot runtime client failures."""

    code = "qq_snapshot_runtime_error"
    public_message = "QQ local chat snapshot is unavailable."

    def __init__(self, public_message: str | None = None, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        self.public_message = public_message or type(self).public_message
        super().__init__(self.public_message)


class QQSnapshotRuntimeUnavailable(QQSnapshotRuntimeError):
    """The local runtime could not be reached at all."""

    code = "qq_snapshot_runtime_unavailable"
    public_message = (
        "QQ local chat data is unavailable. Please finish QQ login and try again."
    )


class QQSnapshotRuntimeUnauthorized(QQSnapshotRuntimeError):
    """The bridge refused this client's credential.

    Raised instead of a generic invalid-response failure when the bridge answers
    ``401``: the request never reached a method, so nothing was acquired or
    cleaned.  The public message stays the privacy-safe base message.
    """

    code = "qq_snapshot_runtime_unauthorized"
    public_message = (
        "QQ local chat data is unavailable. Please finish QQ login and try again."
    )


class QQSnapshotRuntimeInvalidResponse(QQSnapshotRuntimeError):
    """The runtime answered but the response was not a valid RPC envelope."""

    code = "qq_snapshot_runtime_invalid_response"
    public_message = "QQ local chat data is temporarily unavailable. Please try again."


class QQSnapshotWorkerGenerationChanged(QQSnapshotRuntimeError):
    code = "qq_snapshot_worker_generation_changed"
    public_message = "QQ runtime restarted. Please reconnect explicitly."


class QQSnapshotRuntimeNotReady(QQSnapshotRuntimeError):
    """The bridge is ready but `EchoSnapshotApi` is not registered yet."""

    code = "qq_snapshot_runtime_not_ready"
    public_message = "QQ local chat data is still starting. Please try again."


class QQSnapshotRuntimeFailure(QQSnapshotRuntimeError):
    """The runtime reported a failure for the requested snapshot operation."""

    code = "qq_snapshot_runtime_failure"
    public_message = "QQ local chat data is temporarily unavailable. Please try again."


class QQSnapshotInvalidGenerationId(QQSnapshotRuntimeError):
    """A generation id was not a safe opaque path segment."""

    code = "qq_snapshot_invalid_generation_id"
    public_message = "QQ local chat data is temporarily unavailable. Please try again."


class QQSnapshotCleanupFailed(QQSnapshotRuntimeError):
    """The runtime could not remove a generation that may still hold plaintext."""

    code = "qq_snapshot_cleanup_failed"
    public_message = "QQ local chat data cleanup failed. Please try again."


def validate_generation_id(generation_id: Any) -> str:
    """Return a normalized opaque generation id, or raise on any unsafe value.

    Only a single, non-empty path segment is allowed.  Separators, traversal
    segments, NUL bytes and non-string values are rejected before the value can
    ever be joined under the snapshot root.
    """
    if isinstance(generation_id, bool) or not isinstance(generation_id, str):
        raise QQSnapshotInvalidGenerationId()
    value = generation_id.strip()
    if not value or value in (".", ".."):
        raise QQSnapshotInvalidGenerationId()
    if "/" in value or "\\" in value or "\x00" in value:
        raise QQSnapshotInvalidGenerationId()
    return value


class QQDirectSnapshotRuntimeClient:
    """Thin `EchoSnapshotApi` client bound to a known snapshot root."""

    def __init__(
        self,
        base_url: str,
        *,
        snapshot_root: str | Path,
        timeout: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        acquire_timeout: float = DEFAULT_ACQUIRE_RPC_TIMEOUT_SECONDS,
        transport: Callable[[str, bytes, float], tuple[int, str]] | None = None,
        runtime_id: str | None = None,
        credential: object | None = None,
        boot_id: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._snapshot_root = Path(snapshot_root)
        if runtime_id is not None and (not isinstance(runtime_id, str)
                                      or re.fullmatch(r"[0-9a-f]{64}", runtime_id) is None):
            raise QQSnapshotRuntimeInvalidResponse()
        self._runtime_id = runtime_id
        if boot_id is not None and (not isinstance(boot_id, str) or re.fullmatch(r"[0-9a-f]{64}", boot_id) is None):
            raise QQSnapshotRuntimeInvalidResponse()
        # A caller may pass the generation already verified by its Provider.
        self._boot_id = boot_id
        self._generation_changed = False
        self._timeout = timeout
        self._acquire_timeout = acquire_timeout
        self._credential_source = credential
        # A snapshot lifecycle belongs to one launch, including delayed cleanup
        # and bounded retries. Never upgrade an old task to a new launch's token.
        self._launch_credential = resolve_bridge_credential(credential)
        self._transport = transport or self._request_with_credential

    # ------------------------------------------------------------------ lifecycle

    def acquire(self) -> str:
        """Acquire a fresh generation and return its validated opaque id.

        ``acquire_timeout`` is the wall-clock budget of this whole call, not the
        timeout of one RPC: every attempt shares a single monotonic deadline, and
        no attempt may wait longer than the budget that is still left.
        """
        started_at = time.monotonic()
        deadline = started_at + self._acquire_timeout
        _LOGGER.info("QQ snapshot acquire RPC started timeout=%.1fs", self._acquire_timeout)
        decrypt_failures = 0
        try:
            for attempt in range(1, _SNAPSHOT_UNSTABLE_MAX_ATTEMPTS + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _LOGGER.info(
                        "QQ snapshot acquire RPC attempt=%d outcome=budget_exhausted elapsed=%.2fs",
                        attempt, time.monotonic() - started_at,
                    )
                    raise QQSnapshotRuntimeFailure()
                # One attempt can never outlive the shared deadline.
                result = self._rpc(_ACQUIRE, [], timeout=remaining)
                if not isinstance(result, Mapping):
                    raise QQSnapshotRuntimeInvalidResponse()
                if result.get("ok") is not True or result.get("status") != "ready":
                    code = result.get("code")
                    if isinstance(code, str) and code in _SAFE_ACQUIRE_FAILURE_CODES:
                        _LOGGER.info(
                            "QQ snapshot acquire RPC attempt=%d max_attempts=%d failure_code=%s",
                            attempt, _SNAPSHOT_UNSTABLE_MAX_ATTEMPTS, code,
                        )
                    retry_delay = None
                    if result.get("ok") is False and result.get("status") == "failed":
                        if code == "snapshot_unstable":
                            retry_delay = _SNAPSHOT_UNSTABLE_RETRY_DELAY_SECONDS
                        elif code == "decrypt_failed" and result.get("retryable") is True:
                            decrypt_failures += 1
                            if decrypt_failures < _DECRYPT_MAX_ATTEMPTS:
                                retry_delay = _DECRYPT_RETRY_DELAY_SECONDS
                    # Both recovery classes share this acquisition's deadline,
                    # attempt cap, credential and Worker generation. The runtime
                    # grants decrypt retries only after owned staging is gone.
                    remaining = max(0.0, deadline - time.monotonic())
                    can_retry = (retry_delay is not None
                                 and attempt < _SNAPSHOT_UNSTABLE_MAX_ATTEMPTS
                                 and remaining > retry_delay)
                    reason = (
                        "transient_failure" if can_retry else
                        "decrypt_attempt_limit" if decrypt_failures >= _DECRYPT_MAX_ATTEMPTS else
                        "attempt_limit" if attempt >= _SNAPSHOT_UNSTABLE_MAX_ATTEMPTS else
                        "budget_exhausted" if remaining <= (retry_delay or 0) else
                        "terminal_failure"
                    )
                    _LOGGER.info(
                        "QQ snapshot acquire decision=%s reason=%s attempt=%d "
                        "decrypt_failures=%d remaining_ms=%d retry_delay_ms=%d",
                        "retry" if can_retry else "stop", reason, attempt, decrypt_failures,
                        int(remaining * 1000), int((retry_delay or 0) * 1000) if can_retry else 0,
                    )
                    if can_retry:
                        time.sleep(retry_delay)
                        continue
                    raise QQSnapshotRuntimeFailure()
                generation_id = validate_generation_id(result.get("generation_id"))
                break
        except Exception as error:
            _LOGGER.info(
                "QQ snapshot acquire RPC finished outcome=%s elapsed=%.2fs",
                type(error).__name__,
                time.monotonic() - started_at,
            )
            raise
        _LOGGER.info(
            "QQ snapshot acquire RPC finished outcome=ready elapsed=%.2fs generation_id=%s",
            time.monotonic() - started_at,
            generation_id,
        )
        return generation_id
    def cleanup(self, generation_id: str) -> None:
        """Remove one generation.  A missing generation is already cleaned."""
        normalized = validate_generation_id(generation_id)
        result = self._rpc(_CLEANUP, [normalized])
        if isinstance(result, Mapping) and result.get("ok") is True:
            return
        code = ""
        if isinstance(result, Mapping):
            code = _clean_str(result.get("code"))
        if code == "generation_not_found":
            return
        raise QQSnapshotCleanupFailed()

    def recover(self, *, deadline: float | None = None) -> None:
        """Ask the runtime to remove orphan staging and legacy artifacts."""
        started = time.perf_counter()
        _LOGGER.info("QQ snapshot stage=recover_rpc outcome=started")
        category = "operation_failed"
        try:
            if deadline is not None:
                result = self._rpc(_RECOVER, [], deadline=deadline)
                if not isinstance(result, Mapping) or result.get("ok") is not True:
                    if isinstance(result, Mapping) and result.get("code") == "recover_failed":
                        category = "recover_failed"
                    raise QQSnapshotRuntimeFailure()
            else:
                for attempt in range(RECOVER_READINESS_ATTEMPTS):
                    try:
                        result = self._rpc(_RECOVER, [])
                    except QQSnapshotRuntimeNotReady:
                        if attempt == RECOVER_READINESS_ATTEMPTS - 1:
                            raise
                        time.sleep(RECOVER_READINESS_POLL_SECONDS)
                        continue
                    if isinstance(result, Mapping) and result.get("ok") is True:
                        break
                    if isinstance(result, Mapping) and result.get("code") == "recover_failed":
                        category = "recover_failed"
                    raise QQSnapshotRuntimeFailure()
        except Exception as error:
            # Never trust an overridden exception code or raw server payload.
            for error_type, fixed_code in (
                (QQSnapshotWorkerGenerationChanged, "worker_generation_changed"),
                (QQSnapshotRuntimeUnauthorized, "auth_rejected"),
                (QQSnapshotRuntimeNotReady, "rpc_method_not_ready"),
                (QQSnapshotRuntimeUnavailable, "runtime_unavailable"),
                (QQSnapshotRuntimeInvalidResponse, "invalid_response"),
            ):
                if isinstance(error, error_type):
                    category = fixed_code
                    break
            if not isinstance(error, QQSnapshotRuntimeError):
                category = "unexpected_error"
            _LOGGER.info("QQ snapshot stage=recover_rpc outcome=failed failure_code=%s elapsed_ms=%d",
                         category, int((time.perf_counter() - started) * 1000))
            raise
        _LOGGER.info("QQ snapshot stage=recover_rpc outcome=success elapsed_ms=%d",
                     int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ paths

    def generation_directory(self, generation_id: str) -> Path:
        """Compute the generation directory under the known snapshot root."""
        normalized = validate_generation_id(generation_id)
        return self._snapshot_root / GENERATIONS_DIRECTORY_NAME / normalized

    def get_group_member_all(self, group_code: str) -> Mapping[str, Any]:
        """Fetch one group's member metadata through NapCat's local RPC bridge."""
        result = self._rpc(
            _GROUP_MEMBER_ALL,
            [group_code],
            namespace="GroupApi",
        )
        if not isinstance(result, Mapping):
            raise QQSnapshotRuntimeInvalidResponse()
        return result

    def get_private_identity_metadata(self, *, timeout: float) -> Mapping[str, Any]:
        """Read identity metadata with this snapshot client's launch binding."""
        deadline = time.monotonic() + timeout
        status = self._rpc("status", [], namespace="Core", deadline=deadline)
        if not isinstance(status, Mapping) or not isinstance(status.get("self_info"), Mapping):
            raise QQSnapshotRuntimeInvalidResponse()
        if status.get("boot_id") != self._boot_id or status.get("runtime_id") != self._runtime_id:
            self._generation_changed = True
            raise QQSnapshotWorkerGenerationChanged()
        if status.get("rpc_auth") != "bearer-v1":
            raise QQSnapshotRuntimeUnauthorized()
        friends = self._rpc("listFriends", [], namespace="EchoMetadata", deadline=deadline)
        if not isinstance(friends, list):
            raise QQSnapshotRuntimeInvalidResponse()
        return {"self_info": status["self_info"], "friends": friends}

    # ------------------------------------------------------------------ internals

    def _rpc(
        self, method: str, params: list[Any], *,
        deadline: float | None = None, timeout: float | None = None,
        namespace: str = RPC_METHOD_NAMESPACE,
    ) -> Any:
        if self._generation_changed:
            _LOGGER.info("QQ snapshot stage=rpc_binding failure_code=worker_generation_changed")
            raise QQSnapshotWorkerGenerationChanged()
        if self._credential_source is not None and (
            self._launch_credential is None or
            resolve_bridge_credential(self._credential_source) != self._launch_credential
        ):
            _LOGGER.info("QQ snapshot stage=rpc_binding failure_code=credential_changed")
            raise QQSnapshotRuntimeUnavailable()
        url = f"{self._base_url}/rpc"
        if self._runtime_id is not None:
            url += "/" + self._runtime_id
        timeout = self._timeout if timeout is None else timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QQSnapshotRuntimeUnavailable()
            timeout = min(remaining, self._timeout)
        try:
            if self._boot_id is None:
                handshake_deadline = time.monotonic() + timeout
                status, text = self._transport(url, b'{"method":"Core.status","params":[]}', timeout)
                result = _unwrap_rpc(status, text)
                if not isinstance(result, Mapping) or result.get("rpc_auth") != "bearer-v1":
                    raise QQSnapshotRuntimeUnauthorized()
                boot = result.get("boot_id")
                if not isinstance(boot, str) or re.fullmatch(r"[0-9a-f]{64}", boot) is None:
                    raise QQSnapshotRuntimeInvalidResponse()
                if result.get("runtime_id") != self._runtime_id:
                    raise QQSnapshotRuntimeInvalidResponse()
                if self._boot_id is not None and self._boot_id != boot:
                    raise QQSnapshotWorkerGenerationChanged()
                self._boot_id = boot
                timeout = handshake_deadline - time.monotonic()
                if timeout <= 0:
                    raise QQSnapshotRuntimeUnavailable()
            payload = {"method": f"{namespace}.{method}", "params": params, "boot_id": self._boot_id}
            if namespace == RPC_METHOD_NAMESPACE and method == _ACQUIRE:
                # Share this attempt's remaining budget with the owner. A
                # synchronous capture can outlive the socket without processing
                # its close event; the owner must discard that late generation.
                payload["timeout_ms"] = max(1, min(int(timeout * 1000), DEFAULT_ACQUIRE_RPC_TIMEOUT_SECONDS * 1000))
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            status, text = self._transport(url, body, timeout)
            result = _unwrap_rpc(status, text)
        except QQSnapshotWorkerGenerationChanged:
            self._generation_changed = True
            raise
        except QQSnapshotRuntimeError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            category = ("timeout" if isinstance(cause, TimeoutError) else
                        "connection_refused" if isinstance(cause, ConnectionRefusedError) else
                        "transport_unavailable")
            operation = method if method in {_ACQUIRE, _CLEANUP, _RECOVER, _GROUP_MEMBER_ALL} else "other"
            _LOGGER.info("QQ snapshot stage=rpc_transport operation=%s failure_code=%s", operation, category)
            raise QQSnapshotRuntimeUnavailable() from error
        if deadline is not None and time.monotonic() >= deadline:
            raise QQSnapshotRuntimeUnavailable()
        return result

    def _request_with_credential(self, url: str, body: bytes, timeout: float) -> tuple[int, str]:
        """Send one request with this launch's credential, or send nothing.

        This is the only transport production uses, so "no live credential" is a
        local refusal rather than an unauthenticated attempt.
        """
        credential = self._launch_credential
        if credential is None or resolve_bridge_credential(self._credential_source) != credential:
            raise QQSnapshotRuntimeUnavailable()
        return _urllib_transport(url, body, timeout, credential=credential)


def _unwrap_rpc(status: int, text: str) -> Any:
    """Validate the bridge `{id, ok, result|error}` envelope."""
    if status == 401:
        _LOGGER.info("QQ snapshot stage=rpc_response outcome=failed http_status=401 failure_code=auth_rejected")
        # The credential was missing, stale or foreign: no method ran.
        raise QQSnapshotRuntimeUnauthorized()
    payload: Any = None
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    if not isinstance(payload, Mapping):
        _LOGGER.info("QQ snapshot stage=rpc_response outcome=failed http_status=%s failure_code=invalid_response", status)
        raise QQSnapshotRuntimeInvalidResponse()
    if status == 409 and payload.get("error") == "worker_generation_mismatch":
        _LOGGER.info("QQ snapshot stage=rpc_response outcome=failed http_status=409 failure_code=worker_generation_changed")
        raise QQSnapshotWorkerGenerationChanged()
    if status < 200 or status >= 300:
        category = (
            "runtime_identity_mismatch" if status == 409 and payload.get("error") == "runtime_identity_mismatch" else
            "rpc_method_missing" if payload.get("error") == "invalid_method" else
            "invalid_response"
        )
        _LOGGER.info("QQ snapshot stage=rpc_response outcome=failed http_status=%s failure_code=%s", status, category)
        raise QQSnapshotRuntimeInvalidResponse()
    if payload.get("ok") is not True:
        if _is_recover_api_not_ready(payload.get("error")):
            _LOGGER.info("QQ snapshot stage=rpc_response outcome=failed http_status=%s failure_code=rpc_method_not_ready", status)
            raise QQSnapshotRuntimeNotReady()
        # The bridge error string may embed runtime details; it is never
        # surfaced, only mapped to a stable, privacy-safe failure.
        _LOGGER.info("QQ snapshot stage=rpc_response outcome=failed http_status=%s failure_code=rpc_operation_failed", status)
        raise QQSnapshotRuntimeFailure()
    _LOGGER.info("QQ snapshot stage=rpc_response outcome=success http_status=%s", status)
    return payload.get("result")


def _is_recover_api_not_ready(error: Any) -> bool:
    """Match only the bridge's exact privacy-safe missing-method response."""
    return error == (
        f"NapCatCore method not found: {RPC_METHOD_NAMESPACE}.{_RECOVER}"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise QQSnapshotRuntimeUnavailable()


def _urllib_transport(
    url: str,
    body: bytes,
    timeout: float,
    *,
    credential: str | None = None,
) -> tuple[int, str]:
    """Perform one POST against the local `/rpc` bridge only."""
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                 and not parsed.username and not parsed.password and parsed.port is not None
                 and not parsed.query and not parsed.fragment
                 and re.fullmatch(r"/rpc(?:/[0-9a-f]{64})?", parsed.path) is not None)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise QQSnapshotRuntimeUnavailable()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if credential is not None:
        headers.update(authorization_headers(credential))
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        response_text = ""
        try:
            response_text = error.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - best effort on broken streams
            response_text = ""
        return int(error.code), response_text


def _clean_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


__all__ = [
    "DEFAULT_RPC_TIMEOUT_SECONDS",
    "DEFAULT_ACQUIRE_RPC_TIMEOUT_SECONDS",
    "GENERATIONS_DIRECTORY_NAME",
    "QQDirectSnapshotRuntimeClient",
    "QQSnapshotCleanupFailed",
    "QQSnapshotInvalidGenerationId",
    "QQSnapshotRuntimeError",
    "QQSnapshotRuntimeFailure",
    "QQSnapshotRuntimeNotReady",
    "QQSnapshotRuntimeInvalidResponse",
    "QQSnapshotRuntimeUnauthorized",
    "QQSnapshotRuntimeUnavailable",
    "QQSnapshotWorkerGenerationChanged",
    "validate_generation_id",
]
