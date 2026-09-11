"""An ADOPTED bridge dying mid-session was structurally undetectable.

Confirmed live (2026-09-11/12): when this adapter reuses an already-running
bridge from a previous gateway life ("Using existing bridge" in connect()),
``self._bridge_process`` is left ``None`` — not spawned by this instance.
``_check_managed_bridge_exit()`` opens with ``if self._bridge_process is
None: return None``, so it can never detect that bridge dying: the poll loop
just kept logging "Poll error" and sleeping every 5s forever, never reaching
the retryable fatal-error path that queues a reconnect. WhatsApp stayed down
for minutes with zero gateway-log activity until a full gateway restart
forced a fresh connect().

``_poll_messages`` now counts consecutive connection-shaped failures
(ClientConnectorError / ConnectionRefusedError / TimeoutError) and, once they
cross ``_UNMANAGED_BRIDGE_DEAD_THRESHOLD``, declares the bridge unreachable
and runs the same fatal-error + notify sequence ``_check_managed_bridge_exit``
uses for a managed child's death — regardless of whether ``_bridge_process``
exists at all.
"""

import asyncio

import aiohttp
import pytest

from gateway.config import Platform


def _connector_error():
    # aiohttp's own __str__ dereferences connection_key.ssl/host/port, so a
    # bare None there (valid for the constructor, not for printing) blows up
    # the moment the adapter logs the caught exception — construct a real one.
    from aiohttp.client_reqrep import ConnectionKey

    key = ConnectionKey(
        host="127.0.0.1", port=19876, is_ssl=False,
        ssl=None, proxy=None, proxy_auth=None, proxy_headers_hash=None,
    )
    return aiohttp.ClientConnectorError(key, OSError("Connection refused"))


class _AsyncCM:
    """Minimal async context manager: returns a value or raises on __aenter__."""

    def __init__(self, value=None, exc=None):
        self.value = value
        self.exc = exc

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self.value

    async def __aexit__(self, *a):
        return False


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _ScriptedSession:
    """Returns queued responses in order; flips ``adapter._running`` off once
    the script is exhausted so the loop terminates deterministically instead
    of relying on an exception escaping the adapter's own broad
    ``except Exception`` (which would just be absorbed as "another poll
    error" and retried forever with a REAL sleep — the loop never ends)."""

    def __init__(self, adapter, script):
        self._adapter = adapter
        self._script = list(script)

    def get(self, *a, **k):
        if not self._script:
            self._adapter._running = False
            return _AsyncCM(exc=asyncio.CancelledError())
        return self._script.pop(0)


def _make_adapter(script):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._bridge_port = 19876
    adapter._bridge_process = None  # the adopted-bridge case under test
    adapter._running = True
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = None
    adapter._fatal_error_handler = None
    adapter._shutting_down = False
    adapter._http_session = _ScriptedSession(adapter, script)
    return adapter


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    monkeypatch.setattr(WhatsAppAdapter, "_close_bridge_log", lambda self: None)
    # The retry backoff (5s) and poll interval (1s) are real production
    # delays — irrelevant to what this test verifies (the failure-counting
    # logic) and would make every test here take 10s+ for no reason.
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _instant())


async def _instant():
    return None


def test_adopted_bridge_declared_dead_after_threshold_failures():
    """Exactly the confirmed-live scenario: _bridge_process is None."""
    threshold = 3
    adapter = _make_adapter([_AsyncCM(exc=_connector_error())] * threshold)
    assert adapter._UNMANAGED_BRIDGE_DEAD_THRESHOLD == threshold

    asyncio.run(adapter._poll_messages())

    assert adapter._fatal_error_code == "whatsapp_bridge_unreachable"
    assert adapter._fatal_error_retryable is True
    assert adapter._running is False


def test_fewer_than_threshold_failures_do_not_trigger_fatal_error():
    threshold = 3
    adapter = _make_adapter([_AsyncCM(exc=_connector_error())] * (threshold - 1))

    asyncio.run(adapter._poll_messages())  # script exhausts -> loop ends cleanly

    assert adapter._fatal_error_code is None


def test_a_success_between_failures_resets_the_streak():
    threshold = 3
    ok = _AsyncCM(value=_FakeResponse(200, []))
    script = (
        [_AsyncCM(exc=_connector_error())] * (threshold - 1)
        + [ok]
        + [_AsyncCM(exc=_connector_error())] * (threshold - 1)
    )
    adapter = _make_adapter(script)

    asyncio.run(adapter._poll_messages())

    assert adapter._fatal_error_code is None


def test_non_connection_errors_do_not_count_toward_the_streak():
    """A JSON-decode hiccup or handler bug must not look like a dead bridge."""
    threshold = 3
    adapter = _make_adapter([_AsyncCM(exc=ValueError("bad json"))] * (threshold * 2))

    asyncio.run(adapter._poll_messages())

    assert adapter._fatal_error_code is None


def test_managed_bridge_death_path_is_unaffected():
    """A real managed-child death must still be caught by the original,
    faster path — this fix only adds coverage, never removes it."""
    adapter = _make_adapter([_AsyncCM(exc=_connector_error())])

    class _FakeProc:
        def poll(self):
            return 1

    adapter._bridge_process = _FakeProc()

    asyncio.run(adapter._poll_messages())

    assert adapter._fatal_error_code == "whatsapp_bridge_exited"


def test_threshold_streak_survives_a_non_connection_error_in_between():
    """A stray unrelated error mid-streak resets the count (documented,
    conservative behaviour) rather than compounding with connection errors —
    verifies the reset is exact, not a fluke of the earlier tests."""
    threshold = 3
    script = (
        [_AsyncCM(exc=_connector_error())] * (threshold - 1)
        + [_AsyncCM(exc=ValueError("unrelated"))]
        + [_AsyncCM(exc=_connector_error())] * (threshold - 1)
    )
    adapter = _make_adapter(script)

    asyncio.run(adapter._poll_messages())

    assert adapter._fatal_error_code is None
