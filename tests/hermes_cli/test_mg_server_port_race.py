"""``hermes mg`` must survive losing a start race on its port.

At logon two things start mg: the Startup-folder script and the HermesMG
scheduled task (``mg_baslat.bat``).  Both check whether 9140 answers before
starting, but neither has bound yet when the other looks, so both start.  The
loser used to surface uvicorn's raw ``[Errno 10048]`` line and exit 1, even
though the outcome the launcher wanted — an mg serving on 9140 — had been
achieved by the winner.

Losing the race to another mg is now a quiet exit 0.  A port held by
something that is *not* mg is still a real failure, reported through the same
sentinel ``serve``/``dashboard`` use and exit code 75.

uvicorn never raises for a bind failure (``Config.bind_socket`` logs the
OSError and calls ``sys.exit(1)``), so the decision is made by a preflight
probe, with the exception paths kept for the narrow TOCTOU window.
"""

import errno
from unittest.mock import MagicMock

import pytest

from hermes_cli import mg_server, port_probe


@pytest.fixture
def uvicorn_run(monkeypatch):
    """Replace ``uvicorn.run``; assume a free port unless a test says otherwise."""
    uvicorn = pytest.importorskip("uvicorn")
    runner = MagicMock()
    monkeypatch.setattr(uvicorn, "run", runner)
    monkeypatch.setattr(mg_server, "port_bind_conflict", lambda h, p: False)
    return runner


@pytest.fixture
def port_busy(monkeypatch):
    monkeypatch.setattr(mg_server, "port_bind_conflict", lambda h, p: True)


def _addr_in_use() -> OSError:
    exc = OSError(errno.EADDRINUSE, "address in use")
    exc.winerror = 10048
    return exc


# --- preflight: the ordinary logon race -----------------------------------

def test_preflight_quiet_exit_when_another_mg_holds_the_port(
    uvicorn_run, port_busy, monkeypatch, capsys
):
    monkeypatch.setattr(mg_server, "_mg_already_serving", lambda *a, **k: True)

    rc = mg_server.run_server("127.0.0.1", 9140, open_browser=False)

    assert rc == 0
    assert "zaten çalışıyor" in capsys.readouterr().out
    assert not uvicorn_run.called, "must not try to bind a port it knows is taken"


def test_preflight_stranger_on_the_port_is_a_real_failure(
    uvicorn_run, port_busy, monkeypatch
):
    monkeypatch.setattr(mg_server, "_mg_already_serving", lambda *a, **k: False)
    reported = []
    monkeypatch.setattr(
        mg_server, "_report_port_conflict",
        lambda host, port: reported.append((host, port)),
    )

    rc = mg_server.run_server("127.0.0.1", 9140, open_browser=False)

    assert rc == port_probe.PORT_IN_USE_EXIT_CODE == 75
    assert reported == [("127.0.0.1", 9140)]


# --- TOCTOU: bound between the probe and uvicorn's own bind ---------------

def test_systemexit_from_uvicorn_bind_is_reclassified(uvicorn_run, monkeypatch):
    """uvicorn exits 1 rather than raising; a real conflict becomes exit 0."""
    uvicorn_run.side_effect = SystemExit(1)
    monkeypatch.setattr(mg_server, "port_bind_conflict", lambda h, p: True)
    monkeypatch.setattr(mg_server, "_mg_already_serving", lambda *a, **k: True)

    assert mg_server.run_server("127.0.0.1", 9140, open_browser=False) == 0


def test_systemexit_that_is_not_a_port_conflict_propagates(uvicorn_run):
    """Exit 1 with a free port is a genuine startup failure, not our case."""
    uvicorn_run.side_effect = SystemExit(1)

    with pytest.raises(SystemExit):
        mg_server.run_server("127.0.0.1", 9140, open_browser=False)


def test_oserror_addr_in_use_is_handled(uvicorn_run, monkeypatch):
    uvicorn_run.side_effect = _addr_in_use()
    monkeypatch.setattr(mg_server, "_mg_already_serving", lambda *a, **k: True)

    assert mg_server.run_server("127.0.0.1", 9140, open_browser=False) == 0


def test_unrelated_oserror_still_propagates(uvicorn_run):
    """EACCES, bad host, … must keep uvicorn's own diagnostics."""
    uvicorn_run.side_effect = OSError(errno.EACCES, "permission denied")

    with pytest.raises(OSError):
        mg_server.run_server("127.0.0.1", 9140, open_browser=False)


# --- ordinary paths -------------------------------------------------------

def test_clean_start_returns_zero(uvicorn_run):
    assert mg_server.run_server("127.0.0.1", 9140, open_browser=False) == 0
    assert uvicorn_run.called


def test_keyboard_interrupt_is_clean(uvicorn_run):
    uvicorn_run.side_effect = KeyboardInterrupt()

    assert mg_server.run_server("127.0.0.1", 9140, open_browser=False) == 0


# --- shared probe ---------------------------------------------------------

@pytest.mark.parametrize("exc,expected", [
    (_addr_in_use(), True),
    (OSError(errno.EADDRINUSE, "in use"), True),
    (OSError(errno.EACCES, "denied"), False),
    (OSError(errno.ECONNREFUSED, "refused"), False),
])
def test_is_addr_in_use_error(exc, expected):
    assert port_probe.is_addr_in_use_error(exc) is expected


def test_ephemeral_port_never_conflicts():
    assert port_probe.port_bind_conflict("127.0.0.1", 0) is False


def test_probe_detects_a_real_listener():
    """The probe must conflict with a live LISTEN socket on every platform."""
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        bound_port = srv.getsockname()[1]

        assert port_probe.port_bind_conflict("127.0.0.1", bound_port) is True
    finally:
        srv.close()


def test_probe_accepts_a_free_port():
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    assert port_probe.port_bind_conflict("127.0.0.1", free_port) is False


# --- "is it actually mg?" -------------------------------------------------

def _stub_response(body: bytes, status: int = 200):
    class _Resp:
        def __init__(self):
            self.status = status

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp


def test_mg_already_serving_rejects_a_non_mg_server(monkeypatch):
    """A different service on the port must not be mistaken for mg."""
    import urllib.request

    resp = _stub_response(b'{"something": "else"}')
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp())

    assert mg_server._mg_already_serving("127.0.0.1", 9140) is False


def test_mg_already_serving_accepts_an_mg_response(monkeypatch):
    import urllib.request

    resp = _stub_response(b'{"ok": true, "chat": [], "hist": []}')
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp())

    assert mg_server._mg_already_serving("127.0.0.1", 9140) is True


def test_mg_already_serving_rejects_non_200(monkeypatch):
    import urllib.request

    resp = _stub_response(b'{"chat": []}', status=503)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp())

    assert mg_server._mg_already_serving("127.0.0.1", 9140) is False


def test_mg_already_serving_rejects_unparseable_body(monkeypatch):
    import urllib.request

    resp = _stub_response(b"<html>not json</html>")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp())

    assert mg_server._mg_already_serving("127.0.0.1", 9140) is False


def test_mg_already_serving_survives_a_dead_port(monkeypatch):
    import urllib.request

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    assert mg_server._mg_already_serving("127.0.0.1", 9140) is False
