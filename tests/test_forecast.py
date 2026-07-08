"""forecast.py: the trust-decay model, Kelly sizing, and ForecastStrategy."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from dead_bracket.config import CityConfig, Config
from dead_bracket.forecast import (
    FeedMetarGuard,
    FeedReading,
    ForecastStrategy,
    bucket_for_hour,
    floor_to_tick,
    kelly_stake,
    minutes_to_next_metar,
    p_trust,
)
from dead_bracket.models import Fill
from dead_bracket.risk import RiskManager

from conftest import make_bracket, make_snapshot

UTC = timezone.utc
# 12:07Z = 14:07 in Paris (midday bucket, outside the evening cutoff).
NOW = datetime(2026, 7, 7, 12, 7, tzinfo=UTC)


def city_cfg(**kw) -> CityConfig:
    defaults = dict(id="paris", metar_icao="LFPB", tz="Europe/Paris",
                    market_terms=("paris",), feed_source="fmi",
                    feed_station_id="100968", feed_cadence_sec=360,
                    metar_minutes=(0, 30))
    defaults.update(kw)
    return CityConfig(**defaults)


def reading(temp: float, *, obs=None, recv=None) -> FeedReading:
    obs = obs or NOW - timedelta(minutes=3)
    return FeedReading(src="fmi", station="100968", obs_time=obs,
                       temp_c=temp, ts_recv=recv or NOW - timedelta(minutes=1))


def make_strategy(config=None, balance=100.0, cc=None):
    cfg = config or Config()
    risk = RiskManager(cfg, initial_balance=balance)
    return ForecastStrategy("paris", cfg, cc or city_cfg(), risk), risk


# ---------------------------------------------------------------------------
# p_trust — the fitted trust-decay model
# ---------------------------------------------------------------------------
def test_p_trust_matches_doc_table_midday():
    # docs/trust_decay_formula: m=0.4 midday -> p_trust ~0.99 @ tau=6,
    # ~0.985 @ 10, ~0.98 @ 20, ~0.97 @ 30 (resolver 0.995 included).
    assert p_trust(6, 0.4, 14) == pytest.approx(0.99, abs=0.01)
    assert p_trust(10, 0.4, 14) == pytest.approx(0.985, abs=0.01)
    assert p_trust(20, 0.4, 14) == pytest.approx(0.98, abs=0.01)
    assert p_trust(30, 0.4, 14) == pytest.approx(0.97, abs=0.01)


def test_p_trust_evening_is_much_weaker():
    # Evening drift (-1 degC/h) + dead rescue: doc says ~0.81 at tau=30.
    assert p_trust(30, 0.4, 18) < 0.85
    assert p_trust(30, 0.4, 18) < p_trust(30, 0.4, 14) - 0.1


def test_p_trust_monotonics():
    assert p_trust(6, 0.4, 14) > p_trust(30, 0.4, 14)          # shorter lead better
    assert p_trust(10, 1.2, 14) > p_trust(10, 0.4, 14)         # bigger margin better
    assert p_trust(10, 0.4, 14, sensor_sigma=0.5) < p_trust(10, 0.4, 14)
    assert p_trust(10, 0.4, 14, resolver=0.9) == pytest.approx(
        p_trust(10, 0.4, 14, resolver=1.0) * 0.9)


def test_p_trust_tau_zero_is_clamped():
    assert 0.0 < p_trust(0, 0.4, 14) <= 1.0  # no div-by-zero at sigma(0)


def test_bucket_boundaries():
    assert bucket_for_hour(8) == "morn"
    assert bucket_for_hour(12) == "mid"
    assert bucket_for_hour(17) == "eve"
    assert bucket_for_hour(21) == "night"
    assert bucket_for_hour(3) == "night"


# ---------------------------------------------------------------------------
# tau / kelly / tick helpers
# ---------------------------------------------------------------------------
def test_minutes_to_next_metar():
    t = datetime(2026, 7, 7, 12, 7, tzinfo=UTC)
    assert minutes_to_next_metar(t, (0, 30)) == pytest.approx(23.0)
    assert minutes_to_next_metar(t, (25, 55)) == pytest.approx(18.0)
    # Wraparound: :58 with a (0, 30) grid -> 2 minutes.
    t2 = datetime(2026, 7, 7, 12, 58, tzinfo=UTC)
    assert minutes_to_next_metar(t2, (0, 30)) == pytest.approx(2.0)
    # Exactly on a sampling instant -> the NEXT one (30), never 0.
    t3 = datetime(2026, 7, 7, 12, 30, tzinfo=UTC)
    assert minutes_to_next_metar(t3, (0, 30)) == pytest.approx(30.0)
    assert minutes_to_next_metar(t, ()) == 30.0  # degenerate fallback


def test_kelly_stake_doc_examples():
    # docs/early_reading_reliability table: p=0.99 @ a=0.83 -> 0.94.
    assert kelly_stake(0.99, 0.83) == pytest.approx(0.94, abs=0.005)
    assert kelly_stake(0.98, 0.70) == pytest.approx(0.93, abs=0.005)
    assert kelly_stake(0.5, 0.9) < 0  # no edge -> negative
    assert kelly_stake(0.99, 1.0) == 0.0


def test_floor_to_tick_rounds_down():
    assert floor_to_tick(0.949, 0.001) == 0.949
    assert floor_to_tick(0.9499, 0.001) == 0.949
    assert floor_to_tick(0.949, 0.01) == 0.94
    assert floor_to_tick(0.5, 0) == 0.5


# ---------------------------------------------------------------------------
# FeedMetarGuard — sensor-identity kill switch
# ---------------------------------------------------------------------------
def test_guard_trips_on_consecutive_divergence():
    g = FeedMetarGuard(divergence_c=1.0, needed_n=2)
    g.on_reading(reading(28.0, obs=NOW - timedelta(minutes=2)))
    g.on_metar(NOW, 31)  # |28 - 31| = 3 > 1 -> streak 1
    assert not g.tripped
    g.on_reading(reading(28.2, obs=NOW + timedelta(minutes=28)))
    g.on_metar(NOW + timedelta(minutes=30), 31)  # streak 2 -> trip
    assert g.tripped
    g.roll_day()
    assert not g.tripped


def test_guard_good_pair_resets_streak_and_far_reading_ignored():
    g = FeedMetarGuard(divergence_c=1.0, needed_n=2)
    g.on_reading(reading(28.0, obs=NOW - timedelta(minutes=2)))
    g.on_metar(NOW, 31)
    g.on_reading(reading(30.8, obs=NOW + timedelta(minutes=28)))
    g.on_metar(NOW + timedelta(minutes=30), 31)  # good pair -> reset
    g.on_reading(reading(28.0, obs=NOW + timedelta(minutes=58)))
    g.on_metar(NOW + timedelta(minutes=60), 31)  # streak 1 again
    assert not g.tripped
    # A METAR with no reading within the window is not evidence.
    g2 = FeedMetarGuard(divergence_c=1.0, needed_n=1)
    g2.on_reading(reading(20.0, obs=NOW - timedelta(hours=2)))
    g2.on_metar(NOW, 31)
    assert not g2.tripped


# ---------------------------------------------------------------------------
# ForecastStrategy.evaluate
# ---------------------------------------------------------------------------
def snap_alive_30(ask=0.90, depth=500, temp=30):
    return make_snapshot("paris", [make_bracket(temp, asks=[(ask, depth)],
                                                bids=[(ask - 0.05, depth)])])


def test_take_emits_ioc_intent_with_fcast_reason():
    fs, risk = make_strategy()
    intents = fs.evaluate(snap_alive_30(), 30, reading(31.6), NOW)
    assert len(intents) == 1
    it = intents[0]
    assert it.side == "buy" and it.tif == "IOC" and it.temp == 30
    assert it.reason.startswith("fcast p=")
    assert it.limit_px >= 0.90            # lifts the ask
    assert it.limit_px < 0.97             # p cap 0.99 - edge 0.03 keeps a margin
    assert it.size >= 1
    rows = [r for r in fs.last_signals if r.get("taken")]
    assert len(rows) == 1 and rows[0]["temp"] == 30 and rows[0]["p"] <= 0.99


def test_same_reading_fires_once():
    fs, _ = make_strategy()
    r = reading(31.6)
    assert fs.evaluate(snap_alive_30(), 30, r, NOW)
    assert fs.evaluate(snap_alive_30(), 30, r, NOW) == []
    fs.roll_day()  # new day re-arms
    assert fs.evaluate(snap_alive_30(), 30, r, NOW)


def test_price_gate_skips_and_logs_once():
    fs, _ = make_strategy()
    r = reading(31.6)
    assert fs.evaluate(snap_alive_30(ask=0.985), 30, r, NOW) == []
    rows = [x for x in fs.last_signals if x.get("why") == "price"]
    assert len(rows) == 1
    # Second tick, same ob: the skip is not logged again (1 Hz market cadence).
    fs.evaluate(snap_alive_30(ask=0.985), 30, r, NOW)
    assert fs.last_signals == []


def test_margin_gate():
    fs, _ = make_strategy()
    # reading 31.2 vs target 31 -> m = 0.2 < 0.4 -> not a signal at all.
    assert fs.evaluate(snap_alive_30(), 30, reading(31.2), NOW) == []
    assert fs.last_signals == []


def test_evening_cutoff():
    fs, _ = make_strategy()
    evening = datetime(2026, 7, 7, 16, 7, tzinfo=UTC)  # 18:07 Paris
    assert fs.evaluate(snap_alive_30(), 30, reading(31.6, obs=evening, recv=evening),
                       evening) == []


def test_stale_reading_disables_signals():
    fs, _ = make_strategy()
    old = NOW - timedelta(minutes=30)  # 2x cadence(360s) = 12 min << 30
    assert fs.evaluate(snap_alive_30(), 30, reading(31.6, obs=old, recv=old), NOW) == []
    assert fs.stale


def test_dead_upper_open_and_acted_brackets_skipped():
    fs, _ = make_strategy()
    dead = make_bracket(29, asks=[(0.5, 100)])
    top = make_bracket(30, asks=[(0.5, 100)], upper_open=True)
    snap = make_snapshot("paris", [dead, top])
    assert fs.evaluate(snap, 30, reading(33.6), NOW) == []  # dead + upper_open
    # skip_temps (the dead path acted this very tick) wins over the forecast.
    assert fs.evaluate(snap_alive_30(), 30, reading(31.6), NOW,
                       skip_temps=frozenset({30})) == []


def test_no_running_max_yet_still_fires():
    fs, _ = make_strategy()
    assert fs.evaluate(snap_alive_30(), None, reading(31.6), NOW)


def test_same_ob_cap_limits_total_stake():
    cfg = replace(Config(), forecast_same_ob_cap_pct=10.0)  # $10 of $100
    fs, _ = make_strategy(config=cfg)
    snap = make_snapshot("paris", [
        make_bracket(29, asks=[(0.85, 500)], bids=[(0.80, 10)]),
        make_bracket(30, asks=[(0.85, 500)], bids=[(0.80, 10)]),
    ])
    intents = fs.evaluate(snap, 29, reading(32.6), NOW)
    assert intents
    assert sum(i.size * i.limit_px for i in intents) <= 10.0 + 1e-6


def test_forecast_daily_cap_blocks_when_consumed():
    cfg = replace(Config(), forecast_daily_cap_pct=5.0)  # $5 of $100
    fs, risk = make_strategy(config=cfg)
    risk.record_buy_fill(Fill(city="tokyo", temp=33, side="buy", price=0.9,
                              size=6, fee=0.0, reason="fcast p=0.99"))
    assert risk.forecast_cap_remaining() == 0.0
    assert fs.evaluate(snap_alive_30(), 30, reading(31.6), NOW) == []


def test_paused_guard_or_blocked_city_stops_entries():
    fs, risk = make_strategy()
    risk.pause_forecast("test")
    assert fs.evaluate(snap_alive_30(), 30, reading(31.6), NOW) == []
    fs2, risk2 = make_strategy()
    fs2.guard.tripped = True
    assert fs2.evaluate(snap_alive_30(), 30, reading(31.6), NOW) == []
    fs3, risk3 = make_strategy()
    risk3.halt_city("paris", "test halt")
    assert fs3.evaluate(snap_alive_30(), 30, reading(31.6), NOW) == []


def test_kelly_sizing_scales_with_balance_and_fraction():
    intents_small = make_strategy(balance=50.0)[0].evaluate(
        snap_alive_30(), 30, reading(31.6), NOW)
    intents_big = make_strategy(balance=200.0)[0].evaluate(
        snap_alive_30(), 30, reading(31.6), NOW)
    assert intents_big[0].size > intents_small[0].size
    half = replace(Config(), kelly_fraction=0.5)
    intents_half = make_strategy(config=half, balance=50.0)[0].evaluate(
        snap_alive_30(), 30, reading(31.6), NOW)
    assert intents_half[0].size == pytest.approx(2 * intents_small[0].size, abs=1)


def test_zero_balance_means_no_forecast_trades():
    fs, _ = make_strategy(balance=0.0)
    assert fs.evaluate(snap_alive_30(), 30, reading(31.6), NOW) == []


def test_min_order_size_respected():
    fs, _ = make_strategy(balance=100.0)
    b = make_bracket(30, asks=[(0.90, 500)], min_order_size=1000)
    assert fs.evaluate(make_snapshot("paris", [b]), 30, reading(31.6), NOW) == []
    assert any(r.get("why") == "size/caps" for r in fs.last_signals)


def test_sensor_sigma_override_lowers_p_vs_calibrated():
    # A city with a CALIBRATED (small) sigma gets higher p than the inflated default.
    fs_default, _ = make_strategy()
    fs_cal, _ = make_strategy(cc=city_cfg(sensor_sigma=0.05))
    r = reading(31.6)
    fs_default.evaluate(snap_alive_30(), 30, r, NOW)
    fs_cal.evaluate(snap_alive_30(), 30, r, NOW)
    p_def = [x for x in fs_default.last_signals if x.get("taken")][0]["p"]
    p_cal = [x for x in fs_cal.last_signals if x.get("taken")][0]["p"]
    assert p_cal >= p_def


# ---------------------------------------------------------------------------
# RiskManager forecast bookkeeping
# ---------------------------------------------------------------------------
def fcast_fill(city="paris", temp=30, price=0.9, size=10.0):
    return Fill(city=city, temp=temp, side="buy", price=price, size=size,
                fee=0.0, reason="fcast p=0.985 m=0.6 tau=23")


def test_forecast_exposure_tracked_and_released():
    cfg = Config()
    risk = RiskManager(cfg, initial_balance=100.0)
    risk.record_buy_fill(fcast_fill())  # $9
    assert risk.forecast_exposure == pytest.approx(9.0)
    # Dead-path fill on the same bracket does NOT count as forecast exposure.
    risk.record_buy_fill(Fill(city="paris", temp=30, side="buy", price=0.9,
                              size=10, fee=0.0, reason="dead<rmax"))
    assert risk.forecast_exposure == pytest.approx(9.0)
    # Selling half the bracket releases forecast exposure pro-rata (9/18 -> half).
    risk.record_sell_fill(Fill(city="paris", temp=30, side="sell", price=0.999,
                               size=10, fee=0.0, reason="recycle@sell_px"),
                          cost_released=9.0)
    assert risk.forecast_exposure == pytest.approx(4.5)
    risk.record_settlement(city="paris", temp=30, shares=10, cost_basis=9.0)
    assert risk.forecast_exposure == pytest.approx(0.0)


def test_forecast_cap_remaining_and_roll_day():
    cfg = replace(Config(), forecast_daily_cap_pct=40.0)
    risk = RiskManager(cfg, initial_balance=100.0)
    assert risk.forecast_cap_remaining() == pytest.approx(40.0)
    risk.record_buy_fill(fcast_fill(size=20.0))  # $18
    assert risk.forecast_cap_remaining() == pytest.approx(22.0)
    risk.roll_day("2026-07-08")
    assert risk.forecast_cap_remaining() == pytest.approx(40.0)


def test_pause_forecast_is_sticky_across_roll_day():
    risk = RiskManager(Config(), initial_balance=100.0)
    assert risk.pause_forecast("settled loss")
    assert not risk.pause_forecast("again")  # already paused
    risk.roll_day("2026-07-08")
    assert risk.forecast_paused
    assert risk.forecast_pause_reason == "settled loss"


def test_settlement_payout_zero_realises_loss():
    risk = RiskManager(Config(), initial_balance=100.0)
    risk.record_buy_fill(fcast_fill(size=10.0))  # cost $9
    risk.record_settlement(city="paris", temp=30, shares=10, cost_basis=9.0, payout=0.0)
    assert risk.realized_pnl == pytest.approx(-9.0)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def test_kelly_fraction_ceiling_is_half():
    with pytest.raises(ValueError):
        Config(kelly_fraction=0.6)
    with pytest.raises(ValueError):
        Config(kelly_fraction=0.0)
    assert Config(kelly_fraction=0.5).kelly_fraction == 0.5


def test_forecast_live_requires_live_mode():
    with pytest.raises(ValueError):
        Config(forecast_live=True)  # paper mode
    assert Config(mode="live", forecast_live=True).forecast_live


def test_forecast_env_overrides():
    cfg = Config.from_env({"FORECAST": "1", "KELLY_FRACTION": "0.4",
                           "FORECAST_MIN_EDGE": "0.05", "MF_API_KEY": "k"})
    assert cfg.forecast and cfg.kelly_fraction == 0.4
    assert cfg.forecast_min_edge == 0.05 and cfg.mf_api_key == "k"
