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
    # Always-rest is the default: the 0.1c given up buys realisation, capital
    # recycling, and a false-dead alarm (STRATEGY.md section 2).
    assert c.recycle_on_opportunity is False
    assert c.recycle_alarm_min == 240.0
    assert c.balance_poll_fast_sec == 2.0
    assert c.balance_poll_slow_sec == 60.0
    assert c.balance_poll_cool_after == 3
    over = Config.from_env({"RECYCLE_ON_OPPORTUNITY": "true",
                            "RECYCLE_ALARM_MIN": "45",
                            "BALANCE_POLL_FAST_SEC": "1.5",
                            "BALANCE_POLL_SLOW_SEC": "90",
                            "BALANCE_POLL_COOL_AFTER": "5"})
    assert over.recycle_on_opportunity is True
    assert over.recycle_alarm_min == 45.0
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


# ---------------------------------------------------------------------------
# Resolution source per city (measured 2026-08-18; see RESOLVER_LOCATIONS)
# ---------------------------------------------------------------------------
def test_resolver_locations_cover_every_city_except_istanbul():
    from dead_bracket.config import RESOLVER_LOCATIONS, Config

    for c in Config().cities:
        assert c.id in RESOLVER_LOCATIONS, f"{c.id} has no resolver entry"
    # Istanbul's LTFM serves no rows, so it can never be verified -> never trades.
    assert RESOLVER_LOCATIONS["istanbul"] is None
    assert next(c for c in Config().cities if c.id == "istanbul").resolver_location is None


def test_only_the_measured_cities_read_from_the_resolver_table():
    # Re-measured 2026-09-14 against the resolver the market now names (NOAA):
    # seoul 64/64 and shenzhen 32/32 agree, so their METAR IS the resolver and
    # they no longer give up the speed edge. lucknow still diverges (629/633,
    # and NOAA sides with WU against us on all four), so it reads the table.
    from dead_bracket.config import Config

    sourced = {c.id for c in Config().cities if c.resolution_source == "resolver"}
    assert sourced == {"lucknow"}
    for c in Config().cities:
        assert c.resolution_source in ("metar", "resolver")
        if c.resolution_source == "resolver":
            # Either provider gives it a table: synoptic is keyed by the ICAO,
            # twc by the WU location id.
            assert (c.metar_icao if c.resolver_provider == "synoptic"
                    else c.resolver_location), f"{c.id} needs a table to read"


def test_every_city_has_a_usable_resolver_provider():
    # "Every tracker works" means no city is structurally unable to verify.
    from dead_bracket.config import Config

    for c in Config().cities:
        assert c.resolver_provider in ("synoptic", "twc")
        if c.resolver_provider == "synoptic":
            assert c.metar_icao, f"{c.id} has no station to key NOAA by"
        else:
            assert c.resolver_location, f"{c.id} has no WU table"


def test_taipei_is_the_only_city_left_on_weather_underground():
    # Synoptic carries only RCSS's SPECIs (6 obs/48h vs our 90), so Taipei could
    # not migrate with the others. If that ever changes, this test should fail
    # and the city should move.
    from dead_bracket.config import Config

    twc = {c.id for c in Config().cities if c.resolver_provider == "twc"}
    assert twc == {"taipei"}
