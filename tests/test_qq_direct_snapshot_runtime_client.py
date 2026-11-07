"""RED-GREEN coverage for the Python snapshot runtime client (Phase 2A).

These tests drive ``QQDirectSnapshotRuntimeClient`` against an injected HTTP
transport, so no real NapCat bridge or QQ data is touched.  Every identity,
generation id and response body is fictional.

The client is deliberately narrow: it may only invoke ``EchoSnapshotApi``
``acquire`` / ``cleanup`` / ``recover`` over a localhost ``/rpc`` bridge, and it
never accepts an absolute database path from the runtime.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
import qq_chat_analyzer.providers.qq_direct_snapshot_runtime as snapshot_runtime

from qq_chat_analyzer.providers.qq_direct_snapshot_runtime import (
    QQDirectSnapshotRuntimeClient,
    QQSnapshotCleanupFailed,
    QQSnapshotInvalidGenerationId,
    QQSnapshotRuntimeFailure,
    QQSnapshotRuntimeInvalidResponse,
    QQSnapshotRuntimeNotReady,
    QQSnapshotRuntimeUnavailable,
    _urllib_transport,
    validate_generation_id,
)


def _envelope(result: object, *, ok: bool = True) -> str:
    return json.dumps({"id": 1, "ok": ok, "result": result})


def _rpc_error(message: str) -> str:
    return json.dumps({"id": 1, "ok": False, "error": message})


def _acquire_result(generation_id: str, *, ok: bool = True, status: str = "ready") -> dict:
    if not ok:
        return {"ok": False, "code": "decrypt_failed", "status": "failed"}
    return {"ok": True, "generation_id": generation_id, "status": status}


class _FakeTransport:
    """Captures one RPC call and returns a canned response."""

    def __init__(self, status: int = 200, body: str = "", error: Exception | None = None) -> None:
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[tuple[str, bytes, int]] = []

    def __call__(self, url: str, body: bytes, timeout: int) -> tuple[int, str]:
        self.calls.append((url, body, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


class _FakeClock:
    """Deterministic monotonic clock so acquire budget math stays exact."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client(transport, *, base_url: str = "http://127.0.0.1:40654", root: Path | None = None) -> QQDirectSnapshotRuntimeClient:
    return QQDirectSnapshotRuntimeClient(
        base_url=base_url,
        snapshot_root=root or Path("."),
        transport=transport,
        acquire_timeout=65,  # Exercise an explicit, tight shared budget below.
        boot_id="b" * 64,  # Business-operation fixtures start with a verified binding.
    )


# ------------------------------------------------------------------ acquire

def test_default_budget_covers_capture_and_streamed_large_database_decrypt(tmp_path, monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(snapshot_runtime.time, "monotonic", clock)
    budgets = []

    def transport(url, body, timeout):
        budgets.append((timeout, json.loads(body)["timeout_ms"]))
        clock.advance(70)  # Measured capture + decryption exceeds the old 65 s.
        return 200, _envelope(_acquire_result("gen-large"))

    client = QQDirectSnapshotRuntimeClient(
        base_url="http://127.0.0.1:40654", snapshot_root=tmp_path,
        transport=transport, boot_id="b" * 64,
    )
    assert client.acquire() == "gen-large"
    assert budgets == [(120, 120000)]

@pytest.mark.parametrize('error,category', [
    (TimeoutError('PRIVATE_TRANSPORT'), 'timeout'),
    (ConnectionRefusedError('PRIVATE_TRANSPORT'), 'connection_refused'),
    (OSError('PRIVATE_TRANSPORT'), 'transport_unavailable'),
])
def test_transport_failure_logs_a_fixed_category(tmp_path, caplog, error, category):
    caplog.set_level('INFO', logger='qq_chat_analyzer.desktop.qq_direct_snapshot')
    with pytest.raises(QQSnapshotRuntimeUnavailable):
        _client(_FakeTransport(error=error), root=tmp_path).acquire()
    assert f'operation=acquire failure_code={category}' in caplog.text
    assert 'PRIVATE_TRANSPORT' not in caplog.text and str(tmp_path) not in caplog.text

def test_acquire_timeout_covers_sequential_passphrase_and_identity_waits(tmp_path: Path) -> None:
    transport = _FakeTransport(body=_envelope(_acquire_result("gen-late")))
    original_transport = transport.__call__

    def delayed_runtime(url: str, body: bytes, timeout: float) -> tuple[int, str]:
        # The helper may wait 30 s for a passphrase and then 30 s for identity.
        if timeout <= 60:
            raise TimeoutError()
        return original_transport(url, body, timeout)

    client = _client(delayed_runtime, root=tmp_path)

    assert client.acquire() == "gen-late"
    assert transport.calls[0][2] > 60


def test_acquire_retries_inside_one_shared_budget_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every retry spends the remaining budget instead of a fresh 65 s timeout."""
    clock = _FakeClock()
    monkeypatch.setattr(snapshot_runtime.time, "monotonic", clock)
    monkeypatch.setattr(snapshot_runtime.time, "sleep", clock.advance)
    delay = snapshot_runtime._SNAPSHOT_UNSTABLE_RETRY_DELAY_SECONDS
    timeouts: list[float] = []
    server_budgets: list[int] = []

    def transport(url: str, body: bytes, timeout: float) -> tuple[int, str]:
        timeouts.append(timeout)
        server_budgets.append(json.loads(body)["timeout_ms"])
        clock.advance(10.0)  # one capture re-reads the whole source
        if len(timeouts) <= 3:
            return 200, _envelope({"ok": False, "code": "snapshot_unstable", "status": "failed"})
        return 200, _envelope(_acquire_result("gen-budgeted"))

    assert _client(transport, root=tmp_path).acquire() == "gen-budgeted"

    assert timeouts == pytest.approx([
        65.0,
        65.0 - 10.0 - delay,
        65.0 - 20.0 - 2 * delay,
        65.0 - 30.0 - 3 * delay,
    ])
    assert server_budgets == [int(timeout * 1000) for timeout in timeouts]


def test_acquire_stops_at_the_shared_deadline_instead_of_retrying_for_minutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six attempts may no longer stack six 65 s RPC timeouts."""
    clock = _FakeClock()
    monkeypatch.setattr(snapshot_runtime.time, "monotonic", clock)
    monkeypatch.setattr(snapshot_runtime.time, "sleep", clock.advance)
    timeouts: list[float] = []

    def transport(url: str, body: bytes, timeout: float) -> tuple[int, str]:
        timeouts.append(timeout)
        clock.advance(min(20.0, timeout))  # a slow capture burns its own allowance
        return 200, _envelope({"ok": False, "code": "snapshot_unstable", "status": "failed"})

    with pytest.raises(QQSnapshotRuntimeFailure) as captured:
        _client(transport, root=tmp_path).acquire()

    assert captured.value.code == "qq_snapshot_runtime_failure"
    assert len(timeouts) == 4
    assert timeouts[-1] == pytest.approx(4.25)
    assert clock.now == pytest.approx(1065.0)


def test_acquire_does_not_retry_a_non_transient_failure_within_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(snapshot_runtime.time, "monotonic", clock)
    monkeypatch.setattr(snapshot_runtime.time, "sleep", clock.advance)
    timeouts: list[float] = []

    def transport(url: str, body: bytes, timeout: float) -> tuple[int, str]:
        timeouts.append(timeout)
        return 200, _envelope({"ok": False, "code": "decrypt_failed", "status": "failed"})

    with pytest.raises(QQSnapshotRuntimeFailure):
        _client(transport, root=tmp_path).acquire()

    assert len(timeouts) == 1
    assert clock.now == pytest.approx(1000.0)


@pytest.mark.parametrize('recovers', [True, False])
def test_acquire_retries_only_explicitly_recoverable_decrypt_failures(tmp_path, monkeypatch, recovers, caplog):
    caplog.set_level('INFO', logger='qq_chat_analyzer.desktop.qq_direct_snapshot')
    clock = _FakeClock()
    monkeypatch.setattr(snapshot_runtime.time, 'monotonic', clock)
    monkeypatch.setattr(snapshot_runtime.time, 'sleep', clock.advance)
    timeouts = []

    def transport(url, body, timeout):
        timeouts.append(timeout)
        if recovers and len(timeouts) == 2:
            return 200, _envelope(_acquire_result('gen-key-ready'))
        return 200, _envelope({'ok': False, 'status': 'failed', 'code': 'decrypt_failed', 'retryable': True})

    client = _client(transport, root=tmp_path)
    if recovers:
        assert client.acquire() == 'gen-key-ready'
        assert len(timeouts) == 2
    else:
        with pytest.raises(QQSnapshotRuntimeFailure):
            client.acquire()
        assert len(timeouts) == 3
    assert timeouts[0] == 65
    assert all(a > b for a, b in zip(timeouts, timeouts[1:]))
    assert 'decision=retry reason=transient_failure' in caplog.text
    if not recovers:
        assert 'decision=stop reason=decrypt_attempt_limit' in caplog.text


def test_decrypt_retries_still_share_the_acquire_deadline(tmp_path, monkeypatch, caplog):
    caplog.set_level('INFO', logger='qq_chat_analyzer.desktop.qq_direct_snapshot')
    clock = _FakeClock()
    monkeypatch.setattr(snapshot_runtime.time, 'monotonic', clock)
    monkeypatch.setattr(snapshot_runtime.time, 'sleep', clock.advance)
    timeouts = []

    def transport(url, body, timeout):
        timeouts.append(timeout)
        clock.advance(timeout)
        return 200, _envelope({'ok': False, 'status': 'failed', 'code': 'decrypt_failed', 'retryable': True})

    with pytest.raises(QQSnapshotRuntimeFailure):
        _client(transport, root=tmp_path).acquire()
    assert len(timeouts) == 1 and clock.now == 1065
    assert 'decision=stop reason=budget_exhausted' in caplog.text


def test_acquire_unknown_failure_code_does_not_leak_payload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _FakeTransport(body=_envelope({
        "ok": False,
        "code": "private-code-987654 /fictional/path",
        "status": "failed",
        "message": "fictional private message",
    }))
    client = _client(transport, root=tmp_path)

    with caplog.at_level("INFO", logger="qq_chat_analyzer.desktop.qq_direct_snapshot"):
        with pytest.raises(QQSnapshotRuntimeFailure) as captured:
            client.acquire()

    assert captured.value.code == "qq_snapshot_runtime_failure"
    assert "failure_code=" not in caplog.text
    assert "private-code-987654" not in caplog.text
    assert "/fictional/path" not in caplog.text
    assert "fictional private message" not in caplog.text


def test_acquire_malformed_response_maps_to_invalid_response(tmp_path: Path) -> None:
    transport = _FakeTransport(body="not json at all")
    client = _client(transport, root=tmp_path)

    with pytest.raises(QQSnapshotRuntimeInvalidResponse):
        client.acquire()


# ------------------------------------------------------- generation id safety


def test_acquire_rejects_traversal_generation_id_from_runtime(tmp_path: Path) -> None:
    transport = _FakeTransport(body=_envelope(_acquire_result("../evil")))
    client = _client(transport, root=tmp_path)

    with pytest.raises(QQSnapshotInvalidGenerationId):
        client.acquire()


# ------------------------------------------------------------------ cleanup


def test_cleanup_rejects_traversal_before_rpc(tmp_path: Path) -> None:
    transport = _FakeTransport(body=_envelope({"ok": True, "status": "cleaned"}))
    client = _client(transport, root=tmp_path)

    with pytest.raises(QQSnapshotInvalidGenerationId):
        client.cleanup("../evil")
    assert transport.calls == []


# ------------------------------------------------------------------ recover


def test_recover_waits_until_echo_snapshot_api_is_registered(tmp_path: Path) -> None:
    responses = iter(
        (
            _rpc_error("NapCatCore method not found: EchoSnapshotApi.recover"),
            _envelope({"ok": True, "status": "recovered"}),
        )
    )

    def transport(url: str, body: bytes, timeout: int) -> tuple[int, str]:
        return 200, next(responses)

    client = _client(transport, root=tmp_path)

    assert client.recover() is None


def test_recover_api_readiness_wait_is_bounded(monkeypatch, tmp_path: Path) -> None:
    transport = _FakeTransport(
        body=_rpc_error("NapCatCore method not found: EchoSnapshotApi.recover")
    )
    monkeypatch.setattr(snapshot_runtime, "RECOVER_READINESS_ATTEMPTS", 3)
    monkeypatch.setattr(snapshot_runtime.time, "sleep", lambda _seconds: None)
    client = _client(transport, root=tmp_path)

    with pytest.raises(QQSnapshotRuntimeNotReady):
        client.recover()

    assert len(transport.calls) == 3


def test_shutdown_recover_uses_remaining_deadline_for_one_rpc(monkeypatch, tmp_path: Path) -> None:
    now = [100.0]
    monkeypatch.setattr(snapshot_runtime.time, "monotonic", lambda: now[0])
    transport = _FakeTransport(
        body=_rpc_error("NapCatCore method not found: EchoSnapshotApi.recover")
    )
    client = QQDirectSnapshotRuntimeClient(
        "http://127.0.0.1:40654", snapshot_root=tmp_path,
        timeout=30, transport=transport,
        boot_id="b" * 64,
    )

    with pytest.raises(QQSnapshotRuntimeNotReady):
        client.recover(deadline=105.1)

    assert len(transport.calls) == 1
    assert transport.calls[0][2] == pytest.approx(5.1)


def test_shutdown_recover_does_not_rpc_after_deadline(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(snapshot_runtime.time, "monotonic", lambda: 106.0)
    transport = _FakeTransport(body=_envelope({"ok": True}))
    client = _client(transport, root=tmp_path)

    with pytest.raises(QQSnapshotRuntimeUnavailable):
        client.recover(deadline=105.1)

    assert transport.calls == []


def test_shutdown_recover_rejects_response_after_total_deadline(monkeypatch, tmp_path: Path) -> None:
    now = [100.0]
    monkeypatch.setattr(snapshot_runtime.time, "monotonic", lambda: now[0])

    def delayed_transport(url: str, body: bytes, timeout: float) -> tuple[int, str]:
        now[0] = 106.0
        return 200, _envelope({"ok": True})

    client = QQDirectSnapshotRuntimeClient(
        "http://127.0.0.1:40654", snapshot_root=tmp_path,
        timeout=30, transport=delayed_transport,
        boot_id="b" * 64,
    )

    with pytest.raises(QQSnapshotRuntimeUnavailable):
        client.recover(deadline=105.1)


# ------------------------------------------------------------ privacy guards


def test_client_errors_never_embed_runtime_or_identity_text(tmp_path: Path) -> None:
    secret = "10086"
    transport = _FakeTransport(body=_rpc_error(f"boom {secret} /abs/path passphrase"))
    client = _client(transport, root=tmp_path)

    with pytest.raises(QQSnapshotRuntimeFailure) as captured:
        client.acquire()

    assert secret not in str(captured.value)
    assert secret not in captured.value.public_message
    assert "/abs/path" not in str(captured.value)
    assert "passphrase" not in str(captured.value)


def test_urllib_transport_rejects_non_localhost() -> None:
    with pytest.raises(QQSnapshotRuntimeUnavailable):
        _urllib_transport("http://evil.example/rpc", b"{}", 1)
