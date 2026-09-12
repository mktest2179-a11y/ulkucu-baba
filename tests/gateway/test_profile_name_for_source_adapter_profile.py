"""Live-confirmed (2026-09-12, real WhatsApp round-trip): ``gateway.platforms.base.build_source``
calls ``GatewayRunner._profile_name_for_source(source, adapter_profile=owner_profile)`` — but our
fork's ``_profile_name_for_source`` only ever accepted ``(self, source)``, so every inbound
message on every platform raised
``TypeError: _profile_name_for_source() got an unexpected keyword argument 'adapter_profile'``.
It was caught by ``build_source``'s broad ``except Exception`` and silently degraded to the
active profile, so single-profile setups never noticed — but any ``multiplex_profiles`` setup
with a secondary bot (``adapter_profile`` scopes route matching to routes declaring that bot's
``bot_profile``, #104933) would silently ignore the adapter's owning profile.

Root cause: an incomplete merge. ``gateway/platforms/base.py`` had already picked up the
newer calling convention, but ``gateway/run.py``'s ``_profile_name_for_source`` was still the
pre-``adapter_profile`` version. ``gateway/profile_routing.py``'s ``match_profile_route`` and
``gateway/authz_mixin.py``'s ``_transport_owner`` — both dependencies of the newer
implementation — were already present and correct; only this one method was stale.

Fix: ported the current upstream (origin/main) implementation of ``_profile_name_for_source``
verbatim, since every symbol it needs already exists correctly in this fork.
"""

import inspect

from gateway.run import GatewayRunner


def test_profile_name_for_source_accepts_adapter_profile_kwarg():
    """The exact call shape that crashed live: gateway/platforms/base.py's build_source
    passes adapter_profile as a keyword argument."""
    sig = inspect.signature(GatewayRunner._profile_name_for_source)
    assert "adapter_profile" in sig.parameters
    assert sig.parameters["adapter_profile"].default is None


def test_profile_name_for_source_ignores_adapter_profile_when_multiplex_off():
    """Single-profile setups (multiplex_profiles unset/False, the common case that masked
    this bug) must keep returning None regardless of adapter_profile."""

    class _Config:
        multiplex_profiles = False

    class _Runner:
        config = _Config()
        _profile_name_for_source = GatewayRunner._profile_name_for_source

    runner = _Runner()
    result = runner._profile_name_for_source(source=object(), adapter_profile="some-bot-profile")
    assert result is None
