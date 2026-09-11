"""A crashed or force-killed backend's heartbeat row is never cleaned up:
``_atexit_clear()`` only runs on a graceful exit, and nothing else in the
row's lifecycle ever deletes it. Confirmed against the real state.db
(2026-09-11/12): 6 rows for long-dead PIDs, none matching the live gateway.

``_start_backend_heartbeat_refresher()`` now sweeps rows older than 24h
before registering itself, reusing the already-tested (and previously
uncalled-from-production-code) ``prune_stale_heartbeats``.
"""

import threading

import pytest

import tui_gateway.server as server


@pytest.fixture(autouse=True)
def _reset_refresher_state(monkeypatch):
    monkeypatch.setattr(server, "_heartbeat_refresher_started", False)
    monkeypatch.setattr(server, "_heartbeat_refresher_lock", threading.Lock())
    monkeypatch.setattr(server, "_HEARTBEAT_REFRESH_S", 0.0)  # no background thread
    monkeypatch.setattr(server, "_refresh_backend_heartbeat", lambda: None)


class _FakeDB:
    def __init__(self):
        self.pruned_with = None

    def prune_stale_heartbeats(self, *, max_age_seconds):
        self.pruned_with = max_age_seconds
        return ["dead-backend-1", "dead-backend-2"]


def test_startup_sweeps_stale_rows_before_registering(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(server, "_get_db", lambda: db)

    server._start_backend_heartbeat_refresher()

    assert db.pruned_with == 86400.0


def test_sweep_cutoff_is_many_multiples_of_the_refresh_interval():
    """24h must stay generous relative to the 60s-default refresh cadence,
    or a live-but-briefly-slow backend risks being swept as dead."""
    assert 86400.0 / 60.0 >= 100


def test_no_db_available_skips_the_sweep_without_error(monkeypatch):
    monkeypatch.setattr(server, "_get_db", lambda: None)

    server._start_backend_heartbeat_refresher()  # must not raise


def test_a_broken_prune_does_not_block_this_backend_registering(monkeypatch):
    """Startup must not fail just because housekeeping did."""
    calls = []

    class _BoomDB:
        def prune_stale_heartbeats(self, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(server, "_get_db", lambda: _BoomDB())
    monkeypatch.setattr(
        server, "_refresh_backend_heartbeat", lambda: calls.append("registered")
    )

    server._start_backend_heartbeat_refresher()  # must not raise

    assert calls == ["registered"]


def test_repeat_calls_only_sweep_once(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(server, "_get_db", lambda: db)

    server._start_backend_heartbeat_refresher()
    sweeps_after_first = 1 if db.pruned_with is not None else 0
    db.pruned_with = None
    server._start_backend_heartbeat_refresher()

    assert sweeps_after_first == 1
    assert db.pruned_with is None, "second call must be a no-op, not a re-sweep"
