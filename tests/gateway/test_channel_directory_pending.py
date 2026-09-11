"""A platform queued for reconnect must keep its published targets.

WhatsApp's Node bridge takes longer to come up than the capped cold-start
connect budget, so on every boot the gateway reaches ``running`` with WhatsApp
queued for the reconnect watcher and rebuilds the channel directory.  That
rebuild used to replace a good list of chats with ``[]`` for the ~minute until
the retry succeeded, leaving ``send_message`` with nothing to route to.
"""

import asyncio

import pytest

from gateway import channel_directory
from gateway.config import Platform


@pytest.fixture
def directory_path(tmp_path, monkeypatch):
    path = tmp_path / "channel_directory.json"
    monkeypatch.setattr(channel_directory, "DIRECTORY_PATH", path)
    monkeypatch.setattr(
        channel_directory, "_build_from_sessions", lambda name: []
    )
    return path


def _build(adapters, pending=None):
    return asyncio.run(
        channel_directory.build_channel_directory(
            adapters, pending_platforms=pending
        )
    )


def _seed(previous):
    _build({}, pending=None)  # create the file
    from utils import atomic_json_write
    atomic_json_write(
        channel_directory.DIRECTORY_PATH,
        {"updated_at": "2026-01-01T00:00:00", "platforms": previous},
    )


def test_pending_platform_keeps_previous_targets(directory_path):
    _seed({"whatsapp": [{"id": "1@g.us", "name": "Aile", "type": "group"}]})

    directory = _build({}, pending={Platform.WHATSAPP})

    assert directory["platforms"]["whatsapp"] == [
        {"id": "1@g.us", "name": "Aile", "type": "group"}
    ]


def test_absent_platform_without_pending_is_dropped(directory_path):
    """The connected-only rule still holds for decommissioned platforms."""
    _seed({"whatsapp": [{"id": "1@g.us", "name": "Aile", "type": "group"}]})

    directory = _build({}, pending=None)

    assert "whatsapp" not in directory["platforms"]


def test_pending_platform_with_no_history_stays_empty(directory_path):
    _seed({})

    directory = _build({}, pending={Platform.WHATSAPP})

    assert directory["platforms"].get("whatsapp") in (None, [])


def test_connected_platform_overrides_carried_entries(directory_path):
    """A live list always wins over the carried-forward one."""
    _seed({"whatsapp": [{"id": "old@g.us", "name": "Eski", "type": "group"}]})

    class _Adapter:
        async def list_channels(self):
            return [{"id": "new@g.us", "name": "Yeni", "type": "group"}]

    directory = _build({Platform.WHATSAPP: _Adapter()}, pending={Platform.WHATSAPP})

    assert [c["id"] for c in directory["platforms"]["whatsapp"]] == ["new@g.us"]
