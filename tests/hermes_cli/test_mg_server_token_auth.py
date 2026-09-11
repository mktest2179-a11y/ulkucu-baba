"""``hermes mg`` (port 9140) never validated the token it embedded in its own
page — confirmed live (2026-09-11 security audit): a POST /gorev with no
token at all still returned 200 and started a real agent task. The Host
check (see test_mg_server_host_check.py) closes DNS rebinding; this closes
the separate, broader gap it doesn't cover — any local caller, rebinding or
not, that never had a legitimate token in the first place.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.mg_server as mg_server
import hermes_cli.web_routers.model_groups as model_groups


@pytest.fixture(autouse=True)
def _fixed_token(monkeypatch):
    monkeypatch.setattr(mg_server, "_SESSION_TOKEN", "the-real-token")
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.middleware("http")(mg_server._token_auth_middleware)

    @app.api_route("/api/model-groups/gorev", methods=["GET", "POST"])
    async def api_ok():
        return {"ok": True}

    @app.get("/mg")
    async def page_ok():
        return {"page": True}

    return TestClient(app)


def test_api_call_without_a_token_is_rejected(client):
    r = client.post("/api/model-groups/gorev")
    assert r.status_code == 401


def test_api_call_with_the_wrong_token_is_rejected(client):
    r = client.get("/api/model-groups/gorev", headers={"X-Hermes-Session-Token": "guess"})
    assert r.status_code == 401


def test_api_call_with_the_real_token_succeeds(client):
    r = client.get("/api/model-groups/gorev", headers={"X-Hermes-Session-Token": "the-real-token"})
    assert r.status_code == 200


def test_the_write_endpoint_named_in_the_finding_is_protected(client):
    """The finding was specifically an unauthenticated POST /gorev."""
    r = client.post("/api/model-groups/gorev")
    assert r.status_code == 401
    r = client.post("/api/model-groups/gorev", headers={"X-Hermes-Session-Token": "the-real-token"})
    assert r.status_code == 200


def test_the_page_itself_stays_reachable_without_a_token(client):
    """A browser has to be able to load /mg with no credential to ever get one."""
    r = client.get("/mg")
    assert r.status_code == 200


def test_token_comparison_is_timing_safe(monkeypatch):
    """Must go through hmac.compare_digest, not ==, on the request path."""
    calls = []
    import hmac as hmac_module

    real_compare = hmac_module.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(hmac_module, "compare_digest", _spy)

    class _FakeRequest:
        headers = {"X-Hermes-Session-Token": "the-real-token"}

    assert mg_server._has_valid_session_token(_FakeRequest()) is True
    assert calls, "must have gone through hmac.compare_digest, not a bare == "


def test_missing_header_short_circuits_before_comparing(monkeypatch):
    """An absent header must not even reach compare_digest (nothing to time)."""
    import hmac as hmac_module

    called = []
    monkeypatch.setattr(hmac_module, "compare_digest", lambda a, b: called.append(1) or False)

    class _FakeRequest:
        headers = {}

    assert mg_server._has_valid_session_token(_FakeRequest()) is False
    assert not called


def test_run_server_publishes_its_token_onto_the_shared_router(monkeypatch):
    """The page (model_groups._mg_page_html) reads STANDALONE_SESSION_TOKEN —
    run_server must set it before the app can ever serve a request."""
    monkeypatch.setattr(model_groups, "STANDALONE_SESSION_TOKEN", None)
    monkeypatch.setattr(mg_server, "port_bind_conflict", lambda h, p: True)
    monkeypatch.setattr(mg_server, "_mg_already_serving", lambda h, p: True)

    mg_server.run_server("127.0.0.1", 9140, open_browser=False)

    assert model_groups.STANDALONE_SESSION_TOKEN == mg_server._SESSION_TOKEN
    assert model_groups.STANDALONE_SESSION_TOKEN is not None


def test_page_html_embeds_the_standalone_token(monkeypatch):
    monkeypatch.setattr(model_groups, "STANDALONE_SESSION_TOKEN", "a-fresh-token")

    html = model_groups._mg_page_html()

    assert 'window.__HERMES_SESSION_TOKEN__="a-fresh-token"' in html


def test_page_html_falls_back_to_dashboard_token_when_not_standalone(monkeypatch):
    """Under the dashboard mount, STANDALONE_SESSION_TOKEN stays None and the
    existing web_server._SESSION_TOKEN path (already covered elsewhere) is
    used instead — this must not regress that mount."""
    monkeypatch.setattr(model_groups, "STANDALONE_SESSION_TOKEN", None)

    html = model_groups._mg_page_html()

    assert "__HERMES_SESSION_TOKEN__" in html  # dashboard token still gets embedded
