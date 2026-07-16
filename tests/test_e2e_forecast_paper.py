"""End-to-end forecast paper run: feed reading -> signal -> fill -> outcome.

Deterministic: injected wall clock (12:07Z = 14:07 Paris, midday bucket),
scripted METAR raws and book snapshots, no threads, no network.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from dead_bracket.config import CityConfig, Config
from dead_bracket.forecast import FeedReading, ForecastStrategy
from dead_bracket.risk import RiskManager
from conftest import StubReporter, build_worker, make_bracket, make_snapshot

UTC = timezone.utc
NOW = datetime(2026, 7, 7, 12, 7, tzinfo=UTC)

CITY_CFG = CityConfig(id="paris", metar_icao="LFPB", tz="Europe/Paris",
                      market_terms=("paris",), feed_source="fmi",
                      feed_station_id="100968", feed_cadence_sec=360,
                      metar_minutes=(0, 30))


def setup(config=None, balance=100.0, reading_temp=31.6):
    config = config or Config()
    risk = RiskManager(config, initial_balance=balance)
    rep = StubReporter()
    forecast = ForecastStrategy("paris", config, CITY_CFG, risk)
    reading = FeedReading(src="fmi", station="100968",
                          obs_time=NOW - timedelta(minutes=3),
                          temp_c=reading_temp, ts_recv=NOW - timedelta(minutes=1))
    holder = {"reading": reading}
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, city_cfg=CITY_CFG,
        metar_raw="LFPB 071130Z 30/12 Q1015",  # running max 30
        forecast=forecast, feed_reader=lambda: holder["reading"],
        wall_clock=lambda: NOW,
    )
    metar.poll_once()
    return worker, ex, metar, risk, rep, forecast, holder


def alive_30_snapshot(ask=0.90, depth=500):
    # Bracket 30 is ALIVE (== running max): the dead path cannot touch it,
    # only the forecast (reading 31.6 -> target 31, margin 0.6) can.
    return make_snapshot("paris", [make_bracket(30, asks=[(ask, depth)],
                                                bids=[(0.85, 50)])])


def test_win_path_buy_confirm_recycle():
    worker, ex, metar, risk, rep, forecast, _ = setup()

    # Tick 1: feed says 31.6 -> forecast NO-buy on bracket 30 at the ask.
    worker.process_book(alive_30_snapshot())
    assert len(rep.console) == 1 and rep.console[0].startswith("BOUGHT  paris  30C NO")
    assert "@ 0.900" in rep.console[0]
    pos = ex.positions[("paris", 30)]
    assert pos.shares >= 1
    assert pos.resting_sell == pos.shares  # the 99.9c recycle exit is resting
    assert risk.forecast_exposure > 0

    # The METAR confirms the kill and the market reprices toward $1: a 0.999
    # bid appears and the resting maker sell recycles the position.
    confirm = make_snapshot("paris", [make_bracket(30, bids=[(0.999, 1000)])])
    worker.process_book(confirm)
    assert any(l.startswith("SOLD    paris  30C NO") for l in rep.console)
    assert risk.realized_pnl > 0
    assert risk.forecast_exposure == pytest.approx(0.0)


def test_forecast_exit_kept_by_gated_recycle():
    """A forecast position keeps its immediate 99.9c de-risk exit under the
    default gated policy: a later dead-path tick with no capital block must NOT
    cancel it (only pure dead-path positions are opportunity-gated)."""

    worker, ex, metar, risk, rep, forecast, holder = setup()
    worker.process_book(alive_30_snapshot())
    pos = ex.positions[("paris", 30)]
    assert pos.resting_sell == pos.shares  # forecast de-risk exit is resting

    # A quiet tick: no dead-NO opportunity anywhere -> shortfall is 0. The gated
    # recycle logic must leave the forecast position's resting sell intact.
    holder["reading"] = None  # no fresh feed reading -> no new forecast entry
    worker.process_book(make_snapshot("paris", [make_bracket(30, bids=[(0.5, 10)])]))
    assert pos.resting_sell == pos.shares  # still resting (not cancelled)


def test_loss_path_settles_at_zero_and_pauses_forecast():
    worker, ex, metar, risk, rep, forecast, _ = setup()
    worker.process_book(alive_30_snapshot())
    assert rep.console and rep.console[0].startswith("BOUGHT")
    cost = ex.positions[("paris", 30)].cost_basis
    assert cost > 0

    # The day ends with the max stuck at 30: bracket 30 WAS the high — the
    # forecast bet loses at settlement and the forecast path pauses globally.
    worker.roll_day()
    assert risk.realized_pnl == pytest.approx(-cost)
    assert risk.forecast_paused
    assert any("FORECAST PAUSED" in l for l in rep.progress_lines)

    # No further forecast entries anywhere; the dead-bracket path still runs.
    worker.process_book(alive_30_snapshot())
    assert len([l for l in rep.console if l.startswith("BOUGHT")]) == 1
    assert not risk.is_city_blocked("paris")


def test_no_pause_when_disabled_in_config():
    cfg = replace(Config(), forecast_pause_on_loss=False)
    worker, ex, metar, risk, rep, forecast, _ = setup(config=cfg)
    worker.process_book(alive_30_snapshot())
    worker.roll_day()
    assert risk.realized_pnl < 0
    assert not risk.forecast_paused


def test_live_mode_without_forecast_live_logs_but_never_orders():
    cfg = Config(mode="live")  # forecast_live defaults False
    worker, ex, metar, risk, rep, forecast, _ = setup(config=cfg)
    worker.process_book(alive_30_snapshot())
    assert rep.console == []  # no BOUGHT: signals only
    assert risk.forecast_exposure == 0.0
    # The engine really did evaluate (and would have taken it in paper).
    assert any(r.get("taken") for r in forecast.last_signals)


def test_dead_path_owns_dead_brackets_no_double_buy():
    worker, ex, metar, risk, rep, forecast, holder = setup(reading_temp=33.6)
    # Bracket 29 is DEAD (< rmax 30): dead path buys it; the forecast (which
    # would also cover it via margin) must not add a second order. Bracket 30
    # is alive and belongs to the forecast.
    snap = make_snapshot("paris", [
        make_bracket(29, asks=[(0.96, 100)]),
        make_bracket(30, asks=[(0.90, 100)], bids=[(0.85, 10)]),
    ])
    worker.process_book(snap)
    bought = [l for l in rep.console if l.startswith("BOUGHT")]
    assert len(bought) == 2  # one per bracket, never two on one bracket
    assert any("29C" in l for l in bought) and any("30C" in l for l in bought)


def test_stale_feed_notice_and_no_signals():
    worker, ex, metar, risk, rep, forecast, holder = setup()
    old = NOW - timedelta(minutes=30)
    holder["reading"] = FeedReading(src="fmi", station="100968", obs_time=old,
                                    temp_c=31.6, ts_recv=old)
    worker.process_book(alive_30_snapshot())
    assert rep.console == []
    assert any("stale" in l for l in rep.progress_lines)
