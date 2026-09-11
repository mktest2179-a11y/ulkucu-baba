"""Pricing must resolve for whatever model is chosen, not just configured ones.

Two gaps made cache writes bill at zero and relay-served models price at
nothing at all:

1. A hand-written ``pricing:`` config row wins the lookup, but such rows
   usually list input/output/cache_read and omit cache_write.  The row was
   treated as the whole answer, so every cached write cost $0.
2. Models reached through a relay carry the route provider of the relay
   ("custom"), while the model slug names its real vendor
   ("anthropic/claude-sonnet-4-5").  The bundled rate table is keyed by
   vendor, so nothing matched and the model stayed unpriced.
"""

from decimal import Decimal

import pytest

import agent.usage_pricing as u


@pytest.fixture(autouse=True)
def _clear_config_pricing():
    u.set_config_pricing(None)
    yield
    u.set_config_pricing(None)


def _entry(model, provider="custom"):
    return u.get_pricing_entry(model, provider=provider, base_url=None)


# --- 1. field-level merge ------------------------------------------------

def test_config_row_missing_cache_write_is_filled_from_bundled_table():
    u.set_config_pricing({
        "anthropic/claude-sonnet-4-5": {
            "input_cost_per_million": 3.0,
            "output_cost_per_million": 15.0,
            "cache_read_cost_per_million": 0.3,
        }
    })

    e = _entry("anthropic/claude-sonnet-4-5")

    assert e.cache_write_cost_per_million == Decimal("3.75")
    # The operator's own rates must survive untouched.
    assert e.input_cost_per_million == Decimal("3.0")
    assert e.cache_read_cost_per_million == Decimal("0.3")
    assert e.source == "config"


def test_operator_rate_always_beats_the_bundled_one():
    u.set_config_pricing({
        "anthropic/claude-sonnet-4-5": {"input_cost_per_million": 0.5}
    })

    e = _entry("anthropic/claude-sonnet-4-5")

    assert e.input_cost_per_million == Decimal("0.5")   # not the bundled 3.00
    assert e.cache_write_cost_per_million == Decimal("3.75")


def test_merge_is_a_no_op_when_config_is_complete():
    u.set_config_pricing({
        "anthropic/claude-sonnet-4-5": {
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "cache_read_cost_per_million": 3.0,
            "cache_write_cost_per_million": 4.0,
        }
    })

    e = _entry("anthropic/claude-sonnet-4-5")

    assert e.cache_write_cost_per_million == Decimal("4.0")


# --- 2. vendor-prefixed slugs through a relay ----------------------------

@pytest.mark.parametrize("model,expect_write", [
    ("anthropic/claude-sonnet-4-5", Decimal("3.75")),
    ("anthropic/claude-sonnet-5", Decimal("2.50")),
    ("openai/gpt-5.6-sol", Decimal("6.25")),
])
def test_relay_served_models_resolve_by_vendor_prefix(model, expect_write):
    e = _entry(model, provider="custom")

    assert e is not None, f"{model} priced at nothing through a relay"
    assert e.cache_write_cost_per_million == expect_write
    assert e.input_cost_per_million is not None


def test_direct_provider_lookup_still_wins():
    """A real anthropic route must not be changed by the prefix fallback."""
    e = u.get_pricing_entry("claude-sonnet-4-5", provider="anthropic", base_url=None)

    assert e.cache_write_cost_per_million == Decimal("3.75")


def test_unknown_model_stays_unpriced():
    """No invented numbers — an unknown model must fail honestly."""
    assert _entry("acme/not-a-real-model") is None


def test_vendor_prefix_does_not_invent_a_cache_write_rate():
    """DeepSeek does not charge for cache writes; None must stay None."""
    e = _entry("deepseek/deepseek-chat")

    assert e is not None
    assert e.input_cost_per_million == Decimal("0.14")
    assert e.cache_write_cost_per_million is None


# --- 3. live models.dev catalog ------------------------------------------

class _CatalogInfo:
    """Stand-in for a models.dev catalog row."""

    def __init__(self, inp, out, read=None, write=None):
        self.cost_input = inp
        self.cost_output = out
        self.cost_cache_read = read
        self.cost_cache_write = write


def test_catalog_prices_a_model_no_table_knows(monkeypatch):
    """Any model in the refreshed catalog must price, not just table entries."""
    monkeypatch.setattr(
        "agent.models_dev.get_model_info",
        lambda prov, mod, **kw: _CatalogInfo(2.5, 7.5, 0.25, 3.125),
    )

    e = _entry("alibaba/qwen3.7-max")

    assert e is not None
    assert e.source == "models_dev_catalog"
    assert e.input_cost_per_million == Decimal("2.5")
    assert e.cache_write_cost_per_million == Decimal("3.125")


def test_catalog_lookup_uses_the_vendor_prefix(monkeypatch):
    """A relay route must query the catalog by vendor, not by the relay name."""
    seen = {}

    def _capture(prov, mod, **kw):
        seen["provider"], seen["model"] = prov, mod
        return _CatalogInfo(1.0, 2.0)

    monkeypatch.setattr("agent.models_dev.get_model_info", _capture)

    _entry("alibaba/qwen3.7-max")

    assert seen == {"provider": "alibaba", "model": "qwen3.7-max"}


def test_catalog_fills_cache_write_left_blank_by_config():
    u.set_config_pricing({
        "anthropic/claude-sonnet-4-5": {"input_cost_per_million": 3.0}
    })

    e = _entry("anthropic/claude-sonnet-4-5")

    assert e.input_cost_per_million == Decimal("3.0")
    assert e.cache_write_cost_per_million == Decimal("3.75")


def test_catalog_ranks_below_the_hand_checked_table(monkeypatch):
    """Vendor snapshot wins over the community catalog when both know it."""
    e = _entry("anthropic/claude-sonnet-4-5")

    assert e.source == "official_docs_snapshot"


def test_catalog_zero_is_not_read_as_free():
    """A catalog 0 means "unfilled"; it must not silently price writes at 0."""
    from decimal import Decimal as D

    class _Info:
        cost_input = 1.0
        cost_output = 2.0
        cost_cache_read = 0.0
        cost_cache_write = 0.0

    monkeypatch_target = "agent.models_dev.get_model_info"
    import unittest.mock as m
    with m.patch(monkeypatch_target, return_value=_Info()):
        route = u.resolve_billing_route("acme/x", provider="custom", base_url=None)
        entry = u._models_dev_pricing_entry(route)

    assert entry.input_cost_per_million == D("1.0")
    assert entry.cache_write_cost_per_million is None
    assert entry.cache_read_cost_per_million is None


def test_catalog_lookup_never_hits_the_network():
    import unittest.mock as m

    with m.patch("agent.models_dev.get_model_info") as gmi:
        gmi.return_value = None
        route = u.resolve_billing_route("acme/x", provider="custom", base_url=None)
        u._models_dev_pricing_entry(route)

    assert gmi.call_args.kwargs.get("allow_network") is False
