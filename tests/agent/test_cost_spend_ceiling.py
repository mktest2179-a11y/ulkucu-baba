"""Tests for the optional per-run estimated-cost ceiling
(``agent.cost_spend_ceiling_usd`` / env ``HERMES_COST_SPEND_CEILING_USD``)
and the operator ``pricing:`` config bridge that makes it bite on custom
(otherwise-unpriced) models.

Covers:

1. Ceiling resolution / precedence in
   ``agent.conversation_loop._resolve_cost_spend_ceiling``:
   env override wins over config; null / non-positive / non-number
   disables the guard.

2. The gate predicate ``_cost_spend_ceiling_exceeded``: dormant with no
   ceiling, fires strictly above the accumulated
   ``session_estimated_cost_usd``.

3. The ``pricing:`` config table: ``set_config_pricing`` installs operator
   rates so ``estimate_usage_cost`` prices a custom model that ships no
   metadata (instead of silently $0), and clearing it fails open.

4. Config plumbing: ``agent:\\n  cost_spend_ceiling_usd: N`` populates
   ``agent.cost_spend_ceiling_usd``; a root ``pricing:`` section is
   installed at init.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from agent import conversation_loop as CL
from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    has_known_pricing,
    set_config_pricing,
)


class _FakeAgent:
    def __init__(self, **kw):
        self.cost_spend_ceiling_usd = None
        self.session_estimated_cost_usd = 0.0
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("HERMES_COST_SPEND_CEILING_USD", raising=False)


# ── ceiling resolution ───────────────────────────────────────────────────

def test_resolve_none_by_default():
    assert CL._resolve_cost_spend_ceiling(_FakeAgent()) is None


@pytest.mark.parametrize("raw", [None, 0, 0.0, -1, -0.5, True, "abc"])
def test_non_positive_or_bad_config_disables(raw):
    assert CL._resolve_cost_spend_ceiling(_FakeAgent(cost_spend_ceiling_usd=raw)) is None


def test_config_ceiling_resolves():
    assert CL._resolve_cost_spend_ceiling(_FakeAgent(cost_spend_ceiling_usd=0.5)) == 0.5


def test_env_override_wins_over_config(monkeypatch):
    monkeypatch.setenv("HERMES_COST_SPEND_CEILING_USD", "0.01")
    a = _FakeAgent(cost_spend_ceiling_usd=999.0)
    assert CL._resolve_cost_spend_ceiling(a) == 0.01


@pytest.mark.parametrize("raw", ["0", "-2", "not-a-number"])
def test_env_non_positive_disables(monkeypatch, raw):
    monkeypatch.setenv("HERMES_COST_SPEND_CEILING_USD", raw)
    assert CL._resolve_cost_spend_ceiling(_FakeAgent(cost_spend_ceiling_usd=0.5)) is None


# ── gate predicate ───────────────────────────────────────────────────────

def test_gate_dormant_without_ceiling():
    assert CL._cost_spend_ceiling_exceeded(_FakeAgent(session_estimated_cost_usd=9999)) is False


def test_gate_under_ceiling():
    a = _FakeAgent(cost_spend_ceiling_usd=0.5, session_estimated_cost_usd=0.49)
    assert CL._cost_spend_ceiling_exceeded(a) is False


def test_gate_at_ceiling_is_not_exceeded():
    a = _FakeAgent(cost_spend_ceiling_usd=0.5, session_estimated_cost_usd=0.5)
    assert CL._cost_spend_ceiling_exceeded(a) is False


def test_gate_over_ceiling():
    a = _FakeAgent(cost_spend_ceiling_usd=0.5, session_estimated_cost_usd=0.5001)
    assert CL._cost_spend_ceiling_exceeded(a) is True


def test_gate_bad_cost_value_fails_open():
    a = _FakeAgent(cost_spend_ceiling_usd=0.5, session_estimated_cost_usd="oops")
    assert CL._cost_spend_ceiling_exceeded(a) is False


# ── pricing: config bridge ───────────────────────────────────────────────

_MODEL = "acme/tiny-1"
_ONE_M = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)


def test_unpriced_custom_model_is_unknown_without_config():
    set_config_pricing({})
    try:
        assert has_known_pricing(_MODEL, provider="custom",
                                 base_url="https://x.invalid/v1") is False
    finally:
        set_config_pricing({})


def test_config_pricing_makes_custom_model_priced():
    set_config_pricing({
        _MODEL: {"input_cost_per_million": 0.15, "output_cost_per_million": 0.60},
    })
    try:
        assert has_known_pricing(_MODEL, provider="custom",
                                 base_url="https://x.invalid/v1") is True
        c = estimate_usage_cost(_MODEL, _ONE_M, provider="custom",
                                base_url="https://x.invalid/v1")
        assert c.status == "estimated"
        assert c.amount_usd == Decimal("0.75")  # 0.15 + 0.60
    finally:
        set_config_pricing({})


def test_clearing_config_pricing_fails_open():
    set_config_pricing({_MODEL: {"input_cost_per_million": 1}})
    set_config_pricing({})  # cleared
    assert has_known_pricing(_MODEL, provider="custom",
                             base_url="https://x.invalid/v1") is False


def test_invalid_config_pricing_table_is_ignored():
    set_config_pricing("not-a-dict")
    try:
        assert has_known_pricing(_MODEL, provider="custom",
                                 base_url="https://x.invalid/v1") is False
    finally:
        set_config_pricing({})


# ── config plumbing (full AIAgent construction) ─────────────────────────

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
    assert getattr(agent, "cost_spend_ceiling_usd", "missing") is None


def test_config_key_sets_ceiling(monkeypatch, tmp_path):
    agent = _make_agent(
        tmp_path, monkeypatch,
        config_body="agent:\n  cost_spend_ceiling_usd: 0.5\n",
    )
    assert agent.cost_spend_ceiling_usd == 0.5


def test_config_non_positive_disables(monkeypatch, tmp_path):
    agent = _make_agent(
        tmp_path, monkeypatch,
        config_body="agent:\n  cost_spend_ceiling_usd: 0\n",
    )
    assert agent.cost_spend_ceiling_usd is None


def test_root_pricing_section_installed_at_init(monkeypatch, tmp_path):
    set_config_pricing({})
    _make_agent(
        tmp_path, monkeypatch,
        config_body=(
            "pricing:\n"
            "  acme/tiny-1:\n"
            "    input_cost_per_million: 0.15\n"
            "    output_cost_per_million: 0.60\n"
        ),
    )
    try:
        assert has_known_pricing("acme/tiny-1", provider="custom",
                                 base_url="https://x.invalid/v1") is True
    finally:
        set_config_pricing({})
