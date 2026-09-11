"""``hermes mg`` (port 9140) has no token gate by design (a single trusted
local operator), but that made it wide open to DNS rebinding — confirmed
live (2026-09-11 security audit): a request carrying ``Host: evil.test``
got 200, meaning any external page whose DNS TTL-flips to 127.0.0.1 could
POST /gorev and run an agent task, unauthenticated, in a caller-chosen
directory.

``_loopback_host_middleware`` closes that the same way
``hermes_cli.web_server.host_header_middleware`` already protects the
dashboard's own ``/mg`` mount: reject any request whose Host header isn't
a loopback alias, before it reaches a route.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.mg_server import _host_header_hostname, _loopback_host_middleware


@pytest.fixture
def client():
    app = FastAPI()
    app.middleware("http")(_loopback_host_middleware)

    @app.api_route("/api/model-groups/gorev", methods=["GET", "POST"])
    async def ok():
        return {"ok": True}

    return TestClient(app)


@pytest.mark.parametrize("host_header", ["127.0.0.1", "127.0.0.1:9140", "localhost",
                                          "localhost:9140", "[::1]", "[::1]:9140"])
def test_loopback_hosts_are_accepted(client, host_header):
    r = client.get("/api/model-groups/gorev", headers={"Host": host_header})
    assert r.status_code == 200


@pytest.mark.parametrize("host_header", ["evil.test", "evil.test:9140",
                                          "attacker.example", "127.0.0.1.evil.test"])
def test_rebinding_hosts_are_rejected(client, host_header):
    r = client.get("/api/model-groups/gorev", headers={"Host": host_header})
    assert r.status_code == 400


def test_write_endpoint_is_protected_too(client):
    """The finding was specifically about POST /gorev — confirm it, not just GET."""
    r = client.post("/api/model-groups/gorev", headers={"Host": "evil.test"})
    assert r.status_code == 400


@pytest.mark.parametrize("value,expected", [
    ("127.0.0.1", "127.0.0.1"),
    ("127.0.0.1:9140", "127.0.0.1"),
    ("LOCALHOST", "localhost"),
    ("[::1]", "::1"),
    ("[::1]:9140", "::1"),
    ("evil.test", "evil.test"),
    ("", ""),
    ("127.0.0.1:abc", ""),          # non-numeric port
    ("a:b:c", ""),                   # ambiguous multi-colon, not bracketed
    ("127.0.0.1/../x", ""),          # path-like, rejected
    ("http://127.0.0.1", ""),        # full URL, not a bare authority
])
def test_host_header_hostname_parsing(value, expected):
    assert _host_header_hostname(value) == expected
