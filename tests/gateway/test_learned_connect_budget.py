"""Connect budgets are learned from measurement, not tuned constants.

A per-platform constant is guesswork that rots: WhatsApp spawns a Node bridge
whose cold start depends on the machine, so a number tuned on one box is wrong
on the next. Each successful connect records its duration and later attempts
budget from the slowest observed time.
"""

import pytest

from gateway import run as gateway_run
from gateway.config import Platform


class _Runner:
    _platform_connect_timeout_secs = gateway_run.GatewayRunner._platform_connect_timeout_secs
    _learned_connect_timeout_secs = gateway_run.GatewayRunner._learned_connect_timeout_secs
    _record_connect_duration = gateway_run.GatewayRunner._record_connect_duration

    def __init__(self):
        self._connect_durations = {}


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", raising=False)
    return _Runner()


def test_unmeasured_platform_uses_the_shared_default(runner):
    assert runner._platform_connect_timeout_secs(Platform.WHATSAPP) == (
        gateway_run._PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT
    )


def test_budget_grows_to_cover_a_slow_cold_start(runner):
    runner._record_connect_duration(Platform.WHATSAPP, 55.0)

    budget = runner._platform_connect_timeout_secs(Platform.WHATSAPP, initial=True)

    assert budget > 55.0, "must leave room for a slower boot than the one measured"
    assert budget > gateway_run._PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT


def test_budget_follows_the_slowest_sample(runner):
    for d in (2.0, 40.0, 3.0):
        runner._record_connect_duration(Platform.WHATSAPP, d)

    assert runner._platform_connect_timeout_secs(Platform.WHATSAPP) == pytest.approx(
        40.0 * gateway_run._CONNECT_TIMEOUT_HEADROOM
    )


def test_a_fast_platform_never_drops_below_the_default(runner):
    runner._record_connect_duration(Platform.DISCORD, 0.2)

    assert runner._platform_connect_timeout_secs(Platform.DISCORD) == (
        gateway_run._PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT
    )


def test_cold_start_stays_bounded_so_one_platform_cannot_block_the_rest(runner):
    runner._record_connect_duration(Platform.WHATSAPP, 600.0)

    initial = runner._platform_connect_timeout_secs(Platform.WHATSAPP, initial=True)
    retry = runner._platform_connect_timeout_secs(Platform.WHATSAPP, initial=False)

    assert initial == gateway_run._CONNECT_TIMEOUT_INITIAL_CEILING_SECS
    assert retry == gateway_run._CONNECT_TIMEOUT_RETRY_CEILING_SECS
    assert initial < retry


def test_old_samples_age_out(runner):
    runner._record_connect_duration(Platform.WHATSAPP, 80.0)
    for _ in range(gateway_run._CONNECT_DURATION_SAMPLES):
        runner._record_connect_duration(Platform.WHATSAPP, 5.0)

    assert runner._connect_durations["whatsapp"] == [5.0] * gateway_run._CONNECT_DURATION_SAMPLES


def test_failed_connects_are_not_recorded(runner):
    runner._record_connect_duration(Platform.WHATSAPP, 0)
    runner._record_connect_duration(Platform.WHATSAPP, -1)

    assert runner._learned_connect_timeout_secs(Platform.WHATSAPP) is None


def test_env_override_still_wins(runner, monkeypatch):
    runner._record_connect_duration(Platform.WHATSAPP, 55.0)
    monkeypatch.setenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "7")

    assert runner._platform_connect_timeout_secs(Platform.WHATSAPP) == 7.0


def test_telegram_keeps_its_explicit_budget(runner):
    assert runner._platform_connect_timeout_secs(
        Platform.TELEGRAM, initial=True
    ) == gateway_run._TELEGRAM_INITIAL_CONNECT_TIMEOUT_SECS_DEFAULT
