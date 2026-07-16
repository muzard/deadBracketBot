"""Config validation and env overrides (spec §10)."""

from __future__ import annotations

import pytest

from dead_bracket.config import Config, select_cities


def test_defaults_match_spec():
    c = Config()
    assert c.cap == 0.98
    assert c.min_net == 0.012
    assert c.sell_px == 0.999
    assert c.fee_rate == 0.025 and c.fee_exponent == 0.5
    assert c.max_shares_per_bracket == 500
    assert c.max_exposure_per_city == 500.0
    assert c.max_total_exposure == 1000.0
    assert c.max_frac_of_depth == 1.0
    assert c.max_daily_loss == 50.0
    assert c.stale_feed_min == 45
    ids = [city.id for city in c.cities]
    assert ids[:2] == ["paris", "london"]
    assert set(ids) == {
        "paris", "london", "moscow", "milan", "madrid",
        "ankara", "istanbul", "helsinki", "amsterdam", "warsaw", "munich",
        "seoul", "tokyo", "shanghai", "beijing", "taipei", "guangzhou",
        "busan", "singapore", "chongqing", "qingdao", "chengdu", "wellington",
        "kuala_lumpur", "lucknow", "shenzhen", "manila", "wuhan", "karachi",
        "jeddah", "cape_town", "tel_aviv", "panama_city", "sao_paulo", "toronto",
    }
    # ids are unique (no duplicate city slugs) and every station is a distinct ICAO.
    assert len(ids) == len(set(ids))
    icaos = {city.id: city.metar_icao for city in c.cities}
    assert len(set(icaos.values())) == len(icaos)
    assert icaos["paris"] == "LFPB" and icaos["london"] == "EGLC"
    assert icaos["seoul"] == "RKSI" and icaos["sao_paulo"] == "SBGR"
    # Every city carries a valid IANA timezone (the daily-high reset boundary).
    from zoneinfo import ZoneInfo
    for city in c.cities:
        assert ZoneInfo(city.tz) is not None


def test_weather_gov_cities_exclude_speci_from_trigger():
    # The weather.gov-resolving (Wunderground-exempt) cities resolve on the
    # routine cycle only, so SPECIs must not fold into the trade-triggering
    # running max. Every other city keeps SPECIs (default True).
    from dead_bracket.feeds_http import WUNDERGROUND_EXEMPT_ICAOS
    routine_only = {"moscow", "istanbul", "tel_aviv"}
    for city in Config().cities:
        expected = city.metar_icao not in WUNDERGROUND_EXEMPT_ICAOS
        assert city.include_speci is expected, city.id
        assert (city.id in routine_only) is (not city.include_speci)


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        Config(mode="bogus")


def test_sell_px_must_exceed_cap():
    with pytest.raises(ValueError):
        Config(cap=0.999, sell_px=0.999)


def test_invalid_cap_rejected():
    with pytest.raises(ValueError):
        Config(cap=1.5)


def test_from_env_overrides():
    env = {"CAP": "0.97", "MIN_NET": "0.02", "MODE": "paper", "STALE_FEED_MIN": "30"}
    c = Config.from_env(env)
    assert c.cap == 0.97
    assert c.min_net == 0.02
    assert c.stale_feed_min == 30


def test_recycle_and_balance_defaults_and_env():
    c = Config()
    assert c.recycle_on_opportunity is True  # hold-to-$1 gated recycling by default
    assert c.balance_poll_fast_sec == 2.0
    assert c.balance_poll_slow_sec == 60.0
    assert c.balance_poll_cool_after == 3
    over = Config.from_env({"RECYCLE_ON_OPPORTUNITY": "false",
                            "BALANCE_POLL_FAST_SEC": "1.5",
                            "BALANCE_POLL_SLOW_SEC": "90",
                            "BALANCE_POLL_COOL_AFTER": "5"})
    assert over.recycle_on_opportunity is False
    assert over.balance_poll_fast_sec == 1.5
    assert over.balance_poll_slow_sec == 90.0
    assert over.balance_poll_cool_after == 5


def test_select_cities():
    c = Config()
    selected = select_cities(c, ["london"])
    assert len(selected) == 1 and selected[0].id == "london"
    with pytest.raises(KeyError):
        select_cities(c, ["berlin"])


def test_with_mode():
    assert Config().with_mode("live").mode == "live"


def test_apply_bankroll_pct_sets_percentage():
    from dead_bracket.main import apply_bankroll_pct
    c = apply_bankroll_pct(Config().with_mode("live"), 50.0)
    assert c.bankroll_pct == 50.0


def test_apply_bankroll_pct_zero_is_noop():
    from dead_bracket.main import apply_bankroll_pct
    base = Config().with_mode("live")
    assert apply_bankroll_pct(base, 0.0) is base  # stays on the static caps


def test_resolve_initial_balance_prefers_explicit():
    from dead_bracket.main import resolve_initial_balance
    assert resolve_initial_balance(Config(), 30.0) == 30.0
    # Paper mode without --balance has no source for a starting balance.
    assert resolve_initial_balance(Config(), None) == 0.0


def test_bankroll_pct_out_of_range_rejected():
    import pytest
    from dataclasses import replace
    with pytest.raises(ValueError):
        replace(Config(), bankroll_pct=150.0)
