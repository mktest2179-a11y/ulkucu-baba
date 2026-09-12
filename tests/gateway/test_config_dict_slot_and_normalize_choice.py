"""gateway.config_loader imports _dict_slot and _normalize_choice from
gateway.config — both were missing after a merge, breaking that import
entirely. Confirmed live (2026-09-12): the standalone gateway logged
"relay adapter registration failed at gateway startup" with
"ImportError: cannot import name '_dict_slot' from 'gateway.config'" on
every single startup. The relay-registration call site swallows the
exception (non-fatal there), but the same broken import would hard-fail
any OTHER caller of gateway.config_loader — and it silently disabled
relay adapter self-provisioning on every boot.
"""

import pytest

from gateway.config import _dict_slot, _normalize_choice


def test_config_loader_imports_cleanly():
    """The exact import that failed live — this alone is the regression guard."""
    from gateway.config_loader import (  # noqa: F401
        bridge_platform_shared_keys,
        merge_platform_sections,
        read_yaml_layers,
    )


def test_relay_module_imports_cleanly():
    """relay/__init__.py's self_provision_relay chain hits config_loader."""
    from gateway.relay import (  # noqa: F401
        relay_explicitly_disabled,
        relay_url,
        self_provision_relay,
    )


class TestDictSlot:
    def test_creates_a_missing_key_as_an_empty_dict(self):
        d = {}
        assert _dict_slot(d, "platforms") == {}
        assert d == {"platforms": {}}

    def test_returns_the_existing_dict_for_in_place_mutation(self):
        d = {"platforms": {"telegram": {}}}
        slot = _dict_slot(d, "platforms")
        slot["discord"] = {}
        assert d["platforms"] == {"telegram": {}, "discord": {}}

    def test_coerces_a_non_dict_value_instead_of_raising(self):
        d = {"platforms": "not-a-dict"}
        slot = _dict_slot(d, "platforms")
        assert slot == {}
        assert d["platforms"] == {}

    def test_chains_for_nested_slots(self):
        d = {}
        inner = _dict_slot(_dict_slot(d, "platforms"), "telegram")
        inner["token"] = "x"
        assert d == {"platforms": {"telegram": {"token": "x"}}}


class TestNormalizeChoice:
    @pytest.mark.parametrize("raw,expected", [
        ("pair", "pair"), ("PAIR", "pair"), ("  Pair  ", "pair"), ("ignore", "ignore"),
    ])
    def test_accepts_case_and_whitespace_insensitive_matches(self, raw, expected):
        assert _normalize_choice(raw, {"pair", "ignore"}, "pair") == expected

    @pytest.mark.parametrize("raw", ["bogus", "", None, 42, ["pair"]])
    def test_falls_back_to_default_for_anything_unrecognized(self, raw):
        assert _normalize_choice(raw, {"pair", "ignore"}, "ignore") == "ignore"

    def test_matches_the_behavior_of_the_sibling_normalizers(self):
        """Same allowed-set/default shape config.py's own
        _normalize_unauthorized_dm_behavior / _normalize_notice_delivery
        already use inline — this is that logic, generalized."""
        from gateway.config import (
            _normalize_notice_delivery,
            _normalize_unauthorized_dm_behavior,
        )

        for raw in ("PAIR", "ignore", "garbage", None):
            assert (
                _normalize_choice(raw, {"pair", "ignore"}, "pair")
                == _normalize_unauthorized_dm_behavior(raw, "pair")
            )
        for raw in ("PUBLIC", "private", "garbage", None):
            assert (
                _normalize_choice(raw, {"public", "private"}, "public")
                == _normalize_notice_delivery(raw, "public")
            )
