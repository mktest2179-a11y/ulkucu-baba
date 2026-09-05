"""Tests for the optional per-run token spend ceiling
(``agent.token_spend_ceiling`` / env ``HERMES_TOKEN_SPEND_CEILING``).

Covers:

1. Fresh-token accounting in
   ``agent.conversation_loop._session_fresh_tokens``:
   ``session_input_tokens + session_output_tokens`` (input already excludes
   cache reads), floored at 0 — ``session_cache_read_tokens`` is ignored.

2. Ceiling resolution / precedence in
   ``agent.conversation_loop._resolve_token_spend_ceiling``:
   env override wins over config;
   null / non-positive / non-int disables the guard.

3. The gate predicate ``_token_spend_ceiling_exceeded``: dormant with no
   ceiling, fires strictly above the ceiling.

4. One-time-ness of the 80% wrap-up notice injection in
   ``_maybe_inject_token_ceiling_wrapup`` — latched, appended to the
   newest tool message, never before 80% or with no ceiling.

5. Config plumbing: ``agent:\\n  token_spend_ceiling: N`` populates
   ``agent.token_spend_ceiling``; absent key leaves it ``None``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import conversation_loop as CL


class _FakeAgent:
    def __init__(self, **kw):
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.token_spend_ceiling = None
        self._token_ceiling_wrapup_injected = False
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("HERMES_TOKEN_SPEND_CEILING", raising=False)


# ── fresh-token math ──────────────────────────────────────────────────────

@pytest.mark.parametrize("inp,out,cache,expected", [
    (0, 0, 0, 0),
    (1000, 500, 200, 1500),      # cache_read ignored (input already excludes it)
    (1000, 0, 5000, 1000),       # warm cache must NOT drive the figure to 0
    (100, 50, 0, 150),
])
def test_session_fresh_tokens(inp, out, cache, expected):
    a = _FakeAgent(session_input_tokens=inp, session_output_tokens=out,
                   session_cache_read_tokens=cache)
    assert CL._session_fresh_tokens(a) == expected


# ── ceiling resolution / precedence ───────────────────────────────────────

def test_resolve_none_by_default():
    assert CL._resolve_token_spend_ceiling(_FakeAgent()) is None


@pytest.mark.parametrize("raw", [None, 0, -1, True, "abc"])
def test_non_positive_or_bad_config_disables(raw):
    assert CL._resolve_token_spend_ceiling(_FakeAgent(token_spend_ceiling=raw)) is None


def test_config_ceiling_resolves():
    assert CL._resolve_token_spend_ceiling(_FakeAgent(token_spend_ceiling=1000)) == 1000


def test_env_override_wins_over_config(monkeypatch):
    monkeypatch.setenv("HERMES_TOKEN_SPEND_CEILING", "1000")
    a = _FakeAgent(token_spend_ceiling=999_999_999)
    assert CL._resolve_token_spend_ceiling(a) == 1000


@pytest.mark.parametrize("raw", ["0", "-5", "not-a-number"])
def test_env_non_positive_disables(monkeypatch, raw):
    monkeypatch.setenv("HERMES_TOKEN_SPEND_CEILING", raw)
    assert CL._resolve_token_spend_ceiling(_FakeAgent(token_spend_ceiling=1000)) is None


# ── gate predicate ───────────────────────────────────────────────────────

def test_gate_dormant_without_ceiling():
    a = _FakeAgent(session_input_tokens=10**9)
    assert CL._token_spend_ceiling_exceeded(a) is False


def test_gate_under_ceiling():
    a = _FakeAgent(session_input_tokens=800, session_output_tokens=100,
                   token_spend_ceiling=1000)
    assert CL._token_spend_ceiling_exceeded(a) is False


def test_gate_at_ceiling_is_not_exceeded():
    a = _FakeAgent(session_input_tokens=1000, token_spend_ceiling=1000)
    assert CL._token_spend_ceiling_exceeded(a) is False


def test_gate_over_ceiling():
    a = _FakeAgent(session_input_tokens=1200, session_output_tokens=100,
                   token_spend_ceiling=1000)
    assert CL._token_spend_ceiling_exceeded(a) is True


# ── 80% wrap-up notice ───────────────────────────────────────────────────

def test_wrapup_injected_once_on_newest_tool_message():
    a = _FakeAgent(session_input_tokens=850, token_spend_ceiling=1000)
    msgs = [{"role": "user", "content": "hi"},
            {"role": "tool", "content": "result"}]
    assert CL._maybe_inject_token_ceiling_wrapup(a, msgs) is True
    assert CL.TOKEN_CEILING_WRAPUP_NOTICE in msgs[-1]["content"]
    assert msgs[0]["content"] == "hi"          # no synthetic user row
    assert a._token_ceiling_wrapup_injected is True
    # latched: second call is a no-op
    assert CL._maybe_inject_token_ceiling_wrapup(a, msgs) is False


def test_wrapup_dormant_below_threshold():
    a = _FakeAgent(session_input_tokens=500, token_spend_ceiling=1000)
    assert CL._maybe_inject_token_ceiling_wrapup(a, [{"role": "tool", "content": "r"}]) is False


def test_wrapup_dormant_without_ceiling():
    a = _FakeAgent(session_input_tokens=10**9)
    assert CL._maybe_inject_token_ceiling_wrapup(a, [{"role": "tool", "content": "r"}]) is False


# ── config plumbing (full AIAgent construction) ──────────────────────────

def _make_agent(tmp_path, monkeypatch, config_body: str = "", **overrides):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(config_body or "{}\n", encoding="utf-8")
    from run_agent import AIAgent
    kwargs = dict(
        model="gpt-5.5",
        provider="openai",
        api_key="sk-dummy",
        base_url="https://api.openai.com/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


def test_no_ceiling_by_default(monkeypatch, tmp_path):
    agent = _make_agent(tmp_path, monkeypatch)
    assert getattr(agent, "token_spend_ceiling", "missing") is None


def test_config_key_sets_ceiling(monkeypatch, tmp_path):
    agent = _make_agent(
        tmp_path, monkeypatch,
        config_body="agent:\n  token_spend_ceiling: 250000\n",
    )
    assert agent.token_spend_ceiling == 250000


def test_config_non_positive_disables(monkeypatch, tmp_path):
    agent = _make_agent(
        tmp_path, monkeypatch,
        config_body="agent:\n  token_spend_ceiling: 0\n",
    )
    assert agent.token_spend_ceiling is None
