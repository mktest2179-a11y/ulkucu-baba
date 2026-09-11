"""Tests for WhatsAppAdapter.list_channels() and its effect on the directory.

WhatsApp had no ``list_channels()`` hook, so ``build_channel_directory`` could
only fall back to scanning session history.  On an install that had not yet
exchanged a message the directory published ``{"whatsapp": []}`` and
``send_message`` had no target to route to — the platform looked connected but
was unusable.  These tests pin the new bridge-backed enumeration and, just as
importantly, that a bridge failure still degrades to session history rather
than publishing an authoritative empty list.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _make_adapter():
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter._bridge_port = 19876
    adapter._bridge_script = "/tmp/test-bridge.js"
    adapter._session_path = Path("/tmp/test-wa-session")
    adapter._bridge_log_fh = None
    adapter._bridge_log = None
    adapter._bridge_process = None
    adapter._running = True
    adapter._http_session = MagicMock()
    return adapter


def _respond(adapter, *, status=200, payload=None):
    resp = MagicMock()
    resp.status = status

    async def _json():
        return payload

    resp.json = _json
    adapter._http_session.get = MagicMock(return_value=_AsyncCM(resp))
    return resp


@pytest.fixture(autouse=True)
def _no_managed_bridge_exit(monkeypatch):
    async def _ok(self):
        return None

    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    monkeypatch.setattr(WhatsAppAdapter, "_check_managed_bridge_exit", _ok)


@pytest.fixture(autouse=True)
def _no_session_history(monkeypatch):
    """Isolate from the real state.db so tests don't depend on local data."""
    monkeypatch.setattr(
        "gateway.channel_directory._build_from_sessions",
        lambda platform_name: [],
    )


def test_list_channels_returns_groups_from_bridge():
    adapter = _make_adapter()
    _respond(adapter, payload={"chats": [
        {"id": "120363000000000000@g.us", "name": "Aile", "type": "group"},
        {"id": "905551112233@s.whatsapp.net", "name": "Kendim", "type": "dm"},
    ]})

    channels = asyncio.run(adapter.list_channels())

    assert [c["id"] for c in channels] == [
        "120363000000000000@g.us",
        "905551112233@s.whatsapp.net",
    ]
    assert channels[0]["name"] == "Aile"
    assert channels[0]["type"] == "group"
    assert channels[1]["type"] == "dm"


def test_list_channels_names_unnamed_chat_from_jid():
    adapter = _make_adapter()
    _respond(adapter, payload={"chats": [{"id": "905551112233@s.whatsapp.net"}]})

    channels = asyncio.run(adapter.list_channels())

    assert channels == [{
        "id": "905551112233@s.whatsapp.net",
        "name": "905551112233",
        "type": "dm",
    }]


def test_list_channels_merges_session_history(monkeypatch):
    """DMs aren't enumerable through Baileys, so past chats must survive."""
    monkeypatch.setattr(
        "gateway.channel_directory._build_from_sessions",
        lambda platform_name: [
            {"id": "905559998877@s.whatsapp.net", "name": "Ayse", "type": "dm"},
            {"id": "120363000000000000@g.us", "name": "stale", "type": "group"},
        ],
    )
    adapter = _make_adapter()
    _respond(adapter, payload={"chats": [
        {"id": "120363000000000000@g.us", "name": "Aile", "type": "group"},
    ]})

    channels = asyncio.run(adapter.list_channels())

    # Bridge entry wins for the group; the DM-only session entry is added.
    assert len(channels) == 2
    by_id = {c["id"]: c for c in channels}
    assert by_id["120363000000000000@g.us"]["name"] == "Aile"
    assert by_id["905559998877@s.whatsapp.net"]["name"] == "Ayse"


@pytest.mark.parametrize("kwargs", [
    {"status": 404, "payload": None},
    {"status": 503, "payload": None},
    {"status": 200, "payload": {"error": "not connected"}},
])
def test_list_channels_returns_none_on_bridge_failure(kwargs):
    """None (not []) so build_channel_directory keeps the session fallback."""
    adapter = _make_adapter()
    _respond(adapter, **kwargs)

    assert asyncio.run(adapter.list_channels()) is None


def test_list_channels_returns_none_when_not_running():
    adapter = _make_adapter()
    adapter._running = False

    assert asyncio.run(adapter.list_channels()) is None


def test_directory_uses_adapter_channels(tmp_path, monkeypatch):
    """End-to-end: a populated list_channels() fills channel_directory.json."""
    from gateway import channel_directory

    monkeypatch.setattr(channel_directory, "DIRECTORY_PATH",
                        tmp_path / "channel_directory.json")

    adapter = _make_adapter()
    _respond(adapter, payload={"chats": [
        {"id": "120363000000000000@g.us", "name": "Aile", "type": "group"},
    ]})

    directory = asyncio.run(
        channel_directory.build_channel_directory({Platform.WHATSAPP: adapter})
    )

    assert directory["platforms"]["whatsapp"] == [{
        "id": "120363000000000000@g.us",
        "name": "Aile",
        "type": "group",
    }]
