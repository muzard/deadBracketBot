"""CityWorker integration: cycle, telemetry, halts, isolation (spec §3-§9, §12)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from dead_bracket.config import Config
from dead_bracket.models import Intent
from dead_bracket.reporter import Reporter
from dead_bracket.risk import RiskManager
from conftest import StubReporter, build_worker, make_bracket, make_snapshot

NOW = datetime(2026, 6, 3, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def risk_capped():
    """Tight caps so a single bracket exhausts the budget — the setup where a
    second dead bracket is capital-blocked and recycling is triggered.

    Opts INTO the hold-to-$1 policy, which is no longer the default (see
    ``test_dead_no_rests_a_recycle_sell_by_default``)."""

    from dataclasses import replace
    cfg = replace(Config(), max_total_exposure=48.0, max_exposure_per_city=48.0,
                  max_frac_of_depth=1.0, recycle_on_opportunity=True)
    return RiskManager(cfg), cfg


def test_dead_no_rests_a_recycle_sell_by_default(config, risk):
    """Default policy since 2026-09-14: every dead-NO buy is immediately backed by
    a resting 99.9c maker sell.

    Holding to $1 earns 0.1c more per share and risks 100c. Every dollar the
    project ever REALISED came from a 99.9c fill; every dollar it ever lost came
    from a position held to settlement in a city whose source was not the
    resolver's. See STRATEGY.md section 2."""

    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    pos = ex.positions[("paris", 22)]
    assert pos.shares == 100
    assert pos.resting_sell == 100  # covered on open, not held to $1


def test_dead_no_held_to_dollar_when_opportunity_gated(risk_capped):
    """The opt-in policy: a lone dead-NO buy is NOT recycled — it is held to $1,
    so no resting 99.9c sell is posted with no capital-blocked opportunity."""

    risk, config = risk_capped
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    pos = ex.positions[("paris", 22)]
    assert pos.shares > 0
    assert pos.resting_sell == 0  # held to $1, no recycle sell posted

    # A later tick with no new opportunity keeps it held (still no sell).
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert pos.resting_sell == 0
    assert not any(line.startswith("SOLD") for line in rep.console)


def test_legacy_mode_rests_and_retries():
    """recycle_on_opportunity=False restores the always-rest behaviour: every buy
    is immediately backed by a resting sell, and a lost exit is re-covered."""

    from dataclasses import replace
    cfg = replace(Config(), recycle_on_opportunity=False)
    risk = RiskManager(cfg)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    pos = ex.positions[("paris", 22)]
    assert pos.shares == 100 and pos.resting_sell == 100  # fully covered on open

    pos.resting_sell = 0.0  # simulate the resting sell being lost
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert pos.resting_sell == pos.shares  # exit guaranteed back in place


def test_full_buy_then_sell_cycle_legacy():
    from dataclasses import replace
    cfg = replace(Config(), recycle_on_opportunity=False)
    risk = RiskManager(cfg)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",  # running max 24
    )
    metar.poll_once()
    # Dead bracket 22 with a cheap NO ask.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    assert any(line.startswith("BOUGHT") for line in rep.console)
    # Next book shows a high bid -> resting maker sell fills.
    worker.process_book(make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])]))
    assert any(line.startswith("SOLD") for line in rep.console)


def test_recycle_sell_posted_only_when_capital_blocked(risk_capped):
    """Default policy: a held dead NO is recycled (99.9c sell posted) only when a
    NEW dead-NO buy is blocked by the bankroll caps this tick; when the block
    clears the unfilled recycle sell is cancelled and the NO reverts to hold-$1."""

    risk, cfg = risk_capped
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()

    # Tick 1: bracket 22 dead+cheap consumes essentially the whole budget; nothing
    # else is competing for capital, so it is held to $1 (no recycle sell).
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.96, 50)])]))
    pos22 = ex.positions[("paris", 22)]
    assert pos22.shares > 0 and pos22.resting_sell == 0

    # Tick 2: bracket 23 dead+cheap but funds are locked in 22 -> a real buy is
    # blocked by the caps, so 22 is recycled to free capital (both in the book).
    snap_both = make_snapshot("paris", [
        make_bracket(22), make_bracket(23, asks=[(0.97, 50)], min_order_size=10)])
    worker.process_book(snap_both)
    assert pos22.resting_sell > 0  # recycle sell now posted on the held 22

    # Tick 3: the blocking opportunity disappears (23 gone) -> cancel the unfilled
    # recycle sell and revert to holding 22 to $1.
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert pos22.resting_sell == 0


def test_maintain_recycle_covers_shortfall_and_stops(config, risk):
    """Unit: the dead-path recycle loop covers only up to the shortfall — skipping
    a bracket absent from the book and one already fully resting, then stopping
    once enough value is posted (leaving later positions untouched)."""

    from dead_bracket.models import Position
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Four held dead positions (insertion order fixes iteration order).
    ex.positions[("paris", 19)] = Position("paris", 19, shares=100, cost_basis=95)
    ex.positions[("paris", 20)] = Position("paris", 20, shares=10, cost_basis=9,
                                           resting_sell=10, resting_px=0.999)
    ex.positions[("paris", 21)] = Position("paris", 21, shares=100, cost_basis=95)
    ex.positions[("paris", 22)] = Position("paris", 22, shares=100, cost_basis=95)
    # Bracket 19 is absent from the book (skipped); 20/21/22 present.
    snap = make_snapshot("paris", [make_bracket(20), make_bracket(21), make_bracket(22)])
    worker._maintain_recycle_sells(snap, frozenset(), shortfall=30.0)
    assert ex.positions[("paris", 19)].resting_sell == 0    # absent -> skipped
    assert ex.positions[("paris", 20)].resting_sell == 10   # already covered -> unchanged
    assert ex.positions[("paris", 21)].resting_sell == 20   # posts the 20-share gap (10 pre + 20)
    assert ex.positions[("paris", 22)].resting_sell == 0    # shortfall met -> not reached


def test_maintain_recovers_lost_forecast_exit(config, risk):
    """Unit: a forecast position is exempt from gating — its de-risk exit is
    re-covered here even when there is no capital block (shortfall == 0)."""

    from dead_bracket.models import Fill, Position
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Mark bracket 30 as a forecast position (risk tracks the forecast exposure).
    risk.record_buy_fill(Fill(city="paris", temp=30, side="buy", price=0.9,
                              size=50, fee=0.0, reason="fcast p=0.98"))
    ex.positions[("paris", 30)] = Position("paris", 30, shares=50, cost_basis=45)
    snap = make_snapshot("paris", [make_bracket(30)])
    worker._maintain_recycle_sells(snap, frozenset(), shortfall=0.0)
    assert ex.positions[("paris", 30)].resting_sell == 50  # de-risk exit re-covered


def test_cancel_exit_error_trips_per_city_pause(config, risk):
    """A failing cancel routes through the per-city order-error path."""

    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()

    def boom(city, temp):
        raise RuntimeError("venue down")

    ex.cancel_exit = boom
    for _ in range(config.max_order_errors):
        worker._cancel_exit_for(22)
    assert risk.is_city_blocked("paris")


def test_opportunity_logged_even_when_not_acted(config, risk, tmp_path):
    rep = Reporter(str(tmp_path), stdout=io.StringIO())
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",
    )
    metar.poll_once()
    # Depth 1 but a venue minimum of 5 -> want 1 < 5, not acted, yet ask < CAP so
    # it's still logged as an opportunity (size/caps).
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 1)], min_order_size=5)]))
    # Per-day folder: glob across whatever date the file landed in.
    opp_files = list((tmp_path / "paris").glob("*/opportunities.jsonl"))
    assert opp_files, "expected an opportunities log"
    rec = json.loads(opp_files[0].read_text().splitlines()[0])
    assert rec["acted"] is False
    assert rec["why_not"] == "size/caps"
    assert not list((tmp_path / "paris").glob("*/orders.jsonl"))  # no order placed


def test_dead_snapshot_once_per_day_and_carries_prior_ask(config, risk, tmp_path):
    # Build a worker whose METAR max we can raise between ticks, so a bracket can
    # be live first and dead later (the live -> dead transition we want to capture).
    from dead_bracket.config import CityConfig
    from dead_bracket.executor import PaperExecutor
    from dead_bracket.market import MarketFeed
    from dead_bracket.metar import MetarFeed
    from dead_bracket.strategy import Strategy
    from dead_bracket.worker import CityWorker

    raw = {"v": "LFPB 031530Z 24/10 Q1018"}  # running max 24
    metar = MetarFeed("LFPB", fetch_fn=lambda _i: raw["v"], log_path=None)
    rep = Reporter(str(tmp_path), stdout=io.StringIO())
    worker = CityWorker(
        CityConfig(id="paris", metar_icao="LFPB", tz="Europe/Paris", market_terms=("paris",)),
        config, metar=metar, market=MarketFeed("paris", snapshot_fn=lambda: None),
        executor=PaperExecutor(config, risk), strategy=Strategy("paris", config, risk),
        risk=risk, reporter=rep)
    metar.poll_once()

    # Warm-up tick initialises the trading day (the first-ever tick is treated as a
    # rollover, which re-arms the per-day snapshot state at end of cycle).
    worker.process_book(make_snapshot("paris", []))
    # Tick 1: bracket 25 is live (above the max of 24) with a cheap NO ask.
    worker.process_book(make_snapshot("paris", [make_bracket(25, asks=[(0.40, 100)])]))
    # Max climbs to 26 -> bracket 25 is now dead, and its NO has repriced to ~$1.
    raw["v"] = "LFPB 031600Z 26/10 Q1018"
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(25, asks=[(0.999, 100)])]))
    # Re-observing the same dead bracket must NOT write a second row (once/day).
    worker.process_book(make_snapshot("paris", [make_bracket(25, asks=[(0.998, 100)])]))

    files = list((tmp_path / "paris").glob("*/dead.jsonl"))
    assert files, "expected a dead-snapshot log"
    rows = files[0].read_text().splitlines()
    assert len(rows) == 1  # exactly one row for the bracket, despite three ticks
    rec = json.loads(rows[0])
    assert rec["temp"] == 25 and rec["running_max"] == 26
    assert rec["ask"] == 0.999          # already at ~$1 the instant it went dead
    assert rec["ask_pre"] == 0.4        # the last live ask, carried from the prior tick


def test_price_sampling_hot_window_throttled_and_banded(config, risk, tmp_path):
    # Inject a controllable monotonic clock so the >=price_sample_sec throttle is
    # deterministic. The ancient METAR obs keeps the hot window open in tests.
    from dead_bracket.config import CityConfig
    from dead_bracket.executor import PaperExecutor
    from dead_bracket.market import MarketFeed
    from dead_bracket.metar import MetarFeed
    from dead_bracket.strategy import Strategy
    from dead_bracket.worker import CityWorker

    clock = {"t": 1000.0}
    metar = MetarFeed("LFPB", fetch_fn=lambda _i: "LFPB 031530Z 24/10 Q1018", log_path=None)
    rep = Reporter(str(tmp_path), stdout=io.StringIO())
    worker = CityWorker(
        CityConfig(id="paris", metar_icao="LFPB", tz="Europe/Paris", market_terms=("paris",)),
        config, metar=metar, market=MarketFeed("paris", snapshot_fn=lambda: None),
        executor=PaperExecutor(config, risk), strategy=Strategy("paris", config, risk),
        risk=risk, reporter=rep, monotonic=lambda: clock["t"])
    metar.poll_once()  # running max 24

    def book():
        return make_snapshot("paris", [
            make_bracket(23, asks=[(0.999, 50)]),  # dead, below the band -> excluded
            make_bracket(24, asks=[(0.80, 50)]),   # frontier (== running max)
            make_bracket(25, asks=[(0.50, 50)]),   # band+1
            make_bracket(26, asks=[(0.30, 50)]),   # band+2 (default band=2 -> included)
            make_bracket(27, asks=[(0.20, 50)]),   # band+3 -> excluded
        ])

    worker.process_book(book())   # t=1000: first sample fires
    clock["t"] = 1005.0
    worker.process_book(book())   # +5s: throttled (< 15s) -> no new rows
    clock["t"] = 1020.0
    worker.process_book(book())   # +20s: second sample fires

    rows = [json.loads(ln) for ln in
            next((tmp_path / "paris").glob("*/prices.jsonl")).read_text().splitlines()]
    # Two sampling instants × the frontier band 24..26 (3 brackets) = 6 rows.
    assert len(rows) == 6
    assert sorted({r["temp"] for r in rows}) == [24, 25, 26]  # 23 and 27 never sampled
    assert all(r["running_max"] == 24 and r["obs_age_sec"] is not None for r in rows)


def test_price_sampling_disabled_when_interval_zero(config, risk, tmp_path):
    from dataclasses import replace
    cfg = replace(config, price_sample_sec=0.0)
    rep = Reporter(str(tmp_path), stdout=io.StringIO())
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(24, asks=[(0.80, 50)])]))
    assert not list((tmp_path / "paris").glob("*/prices.jsonl"))  # nothing sampled


def test_crossed_book_ignored_when_bracket_above_running_max(config, risk):
    # Reproduces the real incident: running max 17, the glitchy book is 18C
    # (>= rmax, not a bracket we'd ever trade). Tolerant paper mode must ignore
    # it entirely -> no halt, no console noise.
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 17/10 Q1018",
    )
    metar.poll_once()
    crossed_18 = make_bracket(18, asks=[(0.40, 10)], bids=[(0.60, 10)])
    for _ in range(5):
        worker.process_book(make_snapshot("paris", [crossed_18]))
    assert risk.halted is False
    assert risk.is_city_blocked("paris") is False
    assert rep.console == []


def test_crossed_dead_bracket_halts_city_after_grace_then_recovers(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",  # rmax 24; 22C is dead/policed
    )
    metar.poll_once()
    crossed = make_snapshot("paris", [make_bracket(22, asks=[(0.40, 10)], bids=[(0.60, 10)])])
    # grace_ticks=3: first two crossed ticks do NOT halt.
    worker.process_book(crossed)
    worker.process_book(crossed)
    assert risk.is_city_blocked("paris") is False
    # Third consecutive crossed tick pauses the city (per-city, not global).
    worker.process_book(crossed)
    assert risk.is_city_blocked("paris") is True
    assert risk.halted is False  # NOT the global kill-switch
    assert not any(line.startswith("HALT:") for line in rep.console)  # no console noise

    # Healthy books return: after recover_ticks=3 the city auto-recovers.
    healthy = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])])
    worker.process_book(healthy)
    worker.process_book(healthy)
    assert risk.is_city_blocked("paris") is True
    worker.process_book(healthy)
    assert risk.is_city_blocked("paris") is False  # recovered, trading resumes


def test_crossed_book_pause_is_per_city(config, risk):
    rep = StubReporter()
    paris, _, paris_metar = build_worker(
        config=config, risk=risk, reporter=rep, city="paris", icao="LFPB",
        metar_raw="LFPB 031530Z 24/10 Q1018")
    london, london_ex, london_metar = build_worker(
        config=config, risk=risk, reporter=rep, city="london", icao="EGLC",
        metar_raw="EGLC 031530Z 24/10 Q1018")
    paris_metar.poll_once()
    london_metar.poll_once()

    crossed = make_snapshot("paris", [make_bracket(22, asks=[(0.40, 10)], bids=[(0.60, 10)])])
    for _ in range(3):
        paris.process_book(crossed)
    assert risk.is_city_blocked("paris") is True
    # London is unaffected and keeps trading on shared risk.
    london.process_book(make_snapshot("london", [make_bracket(22, asks=[(0.95, 100)])]))
    assert any(line.startswith("BOUGHT  london") for line in rep.console)


def test_live_mode_crossed_book_halts_only_that_city(config, risk):
    from dataclasses import replace
    rep = StubReporter()
    live_cfg = replace(config, mode="live")
    worker, ex, metar = build_worker(
        config=live_cfg, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Strict (live): a single crossed book pauses THIS city, not the process.
    worker.process_book(make_snapshot("paris", [make_bracket(18, asks=[(0.40, 10)], bids=[(0.60, 10)])]))
    assert risk.is_city_blocked("paris") is True
    assert risk.halted is False
    assert not any(line.startswith("HALT:") for line in rep.console)


def test_daily_loss_global_halt_prints_one_console_line(config):
    from dataclasses import replace
    from dead_bracket.risk import RiskManager
    rep = StubReporter()
    cfg = replace(config, max_daily_loss=10.0)
    risk = RiskManager(cfg)
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Force a >$10 realised loss -> GLOBAL kill-switch.
    risk.record_buy_fill(__import__("dead_bracket.models", fromlist=["Fill"]).Fill(
        city="paris", temp=22, side="buy", price=0.95, size=100, fee=0))
    risk.record_sell_fill(__import__("dead_bracket.models", fromlist=["Fill"]).Fill(
        city="paris", temp=22, side="sell", price=0.80, size=100, fee=0), cost_released=95.0)
    assert risk.halted is True
    worker.process_book(make_snapshot("paris", [make_bracket(22, bids=[(0.5, 1)])]))
    assert any(line.startswith("HALT:") and "daily loss" in line for line in rep.console)


def test_downward_revision_halts_city_entries(config, risk):
    rep = StubReporter()
    raws = iter([
        "LFPB 031200Z 24/10 Q1018",
        "LFPB 031200Z COR 21/10 Q1018",  # downward COR for same obs time
    ])
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw=None,
    )
    # Re-wire fetch to the sequence.
    metar._fetch_fn = lambda _i: next(raws, None)
    worker.update_running_max()  # max 24
    worker.update_running_max()  # downward revision detected -> city halted
    assert risk.is_city_blocked("paris") is True
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    assert not any(line.startswith("BOUGHT") for line in rep.console)


def test_stale_feed_halts_city_not_global(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(config=config, risk=risk, reporter=rep)
    # Never polled -> stale.
    worker.check_feed_health(now=NOW)
    assert risk.is_city_blocked("paris") is True
    assert risk.halted is False  # per-city pause, not the global kill-switch
    assert not any(line.startswith("HALT:") for line in rep.console)
    assert any("stale METAR" in ln for ln in rep.progress_lines)


def test_no_running_max_no_entries(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(config=config, risk=risk, reporter=rep)
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    assert rep.console == []


def test_process_none_snapshot_is_noop(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(config=config, risk=risk, reporter=rep)
    worker.process_book(None)  # should not raise
    assert rep.console == []


def test_metar_reading_progress_emitted(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 17/10 Q1018")
    worker.update_running_max()
    # Progress goes to stderr stream, never to the trade stdout stream.
    assert any(ln.startswith("METAR") and "paris" in ln and "17C" in ln
               for ln in rep.progress_lines)
    assert rep.console == []


def test_metar_reading_printed_only_on_change(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 17/10 Q1018")
    # Same observation re-served on every (1s) poll must print exactly once.
    worker.update_running_max()
    worker.update_running_max()
    worker.update_running_max()
    metar_lines = [ln for ln in rep.progress_lines if ln.startswith("METAR")]
    assert len(metar_lines) == 1
    # A genuinely new report prints again.
    metar._fetch_fn = lambda _i: "LFPB 031600Z 19/10 Q1018"
    worker.update_running_max()
    assert len([ln for ln in rep.progress_lines if ln.startswith("METAR")]) == 2


def test_market_unavailable_notice_only_on_change(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(config=config, risk=risk, reporter=rep)
    worker.process_book(None)
    worker.process_book(None)  # still unavailable -> no duplicate notice
    notes = [ln for ln in rep.progress_lines if ln.startswith("NOTE")]
    assert len(notes) == 1
    assert "no market data" in notes[0]


def test_market_restored_notice(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    worker.process_book(None)  # unavailable
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))  # back
    assert any("restored" in ln for ln in rep.progress_lines)


def test_transient_book_failure_does_not_flap_availability(config, risk):
    """A batched /books blip (TransientBookError) must skip the tick quietly — no
    'no market data' / 'restored' flap, just an events-log entry."""

    from dead_bracket.feeds_http import TransientBookError
    from dead_bracket.worker import _SKIP_TICK

    def boom(**_kw):
        raise TransientBookError("paris")

    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018", snapshot_fn=boom)
    metar.poll_once()
    # Establish "available" via a real snapshot first.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    before = worker._market_available
    # Now the book fetch fails transiently several ticks running.
    for _ in range(3):
        assert worker.poll_market() is _SKIP_TICK
    assert worker._market_available is before  # unchanged -> no flap
    notes = [ln for ln in rep.progress_lines if ln.startswith("NOTE")]
    assert not any("no market data" in n or "restored" in n for n in notes)


def test_poll_market_passes_running_max_and_held_temps(config, risk):
    """poll_market forwards the running max and held-position temps to the feed."""

    seen = {}

    def capture(running_max=None, keep_temps=()):
        seen["running_max"] = running_max
        seen["keep_temps"] = set(keep_temps)
        return make_snapshot("paris", [make_bracket(24, asks=[(0.95, 100)])])

    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018", snapshot_fn=capture)
    metar.poll_once()  # running max 24
    # Open a position so a held temp exists.
    worker.process_book(make_snapshot("paris", [make_bracket(21, asks=[(0.40, 100)])]))
    worker.poll_market()
    assert seen["running_max"] == 24
    assert 21 in seen["keep_temps"]


def test_quiet_reporter_suppresses_progress(config, risk):
    rep = StubReporter(progress=False)
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 17/10 Q1018")
    worker.update_running_max()
    worker.process_book(None)
    assert rep.progress_lines == []  # silenced


def test_status_line_shows_open_volume(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # No positions yet -> $0 deployed against the per-city cap.
    worker.emit_status()
    line0 = rep.progress_lines[-1]
    assert line0.startswith("STATUS") and "paris" in line0 and "24C" in line0
    assert f"open $0/${config.max_exposure_per_city:.0f} (0sh, 0pos)" in line0
    assert "opps" not in line0  # replaced by open volume

    # Take a position; the status now reflects deployed capital + shares.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    n, shares, cost = worker.open_volume()
    assert n == 1 and shares == 100 and cost == pytest.approx(95.0)
    worker.emit_status()
    line1 = rep.progress_lines[-1]
    assert "open $95/" in line1 and "(100sh, 1pos)" in line1


def test_status_line_shows_bankroll_blocked_volume(config, risk):
    from dataclasses import replace
    from dead_bracket.risk import RiskManager
    rep = StubReporter()
    cfg = replace(config, max_exposure_per_city=10.0, max_total_exposure=20.0)
    risk = RiskManager(cfg)
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Big window, tiny bankroll -> caps block most of it; avail+ shows the rest.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 600)])]))
    worker.emit_status()
    assert any("avail+$" in ln for ln in rep.progress_lines)


def test_opportunity_counter_counts_rising_edges(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Bracket 22 cheap NO (depth 1 -> not acted, but a sighting). Persisting
    # across two ticks counts once (rising edge).
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 1)])])
    worker.process_book(snap)
    worker.process_book(snap)
    assert worker.opportunities_today == 1
    # It disappears, then reappears -> a second rising edge.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.99, 1)])]))  # >= cap, gone
    worker.process_book(snap)
    assert worker.opportunities_today == 2


def test_opportunity_counter_distinct_brackets(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 25/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [
        make_bracket(22, asks=[(0.95, 1)]), make_bracket(23, asks=[(0.96, 1)])]))
    assert worker.opportunities_today == 2  # two distinct cheap dead brackets


# ---------------------------------------------------------------------------
# Resolver cross-check gating (the Seoul false-dead failsafe)
# ---------------------------------------------------------------------------
def _dead_no_snapshot(city="paris", temp=21):
    """A dead bracket with a cheap NO — the setup that always buys."""

    return make_snapshot(city, [make_bracket(temp, asks=[(0.90, 500)])])


class _StubGuard:
    """Minimal ResolverCrossCheck stand-in returning a fixed verdict."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.noted = []

    def note_ours(self, obs_time, temp_c):
        self.noted.append((obs_time, temp_c))

    def evaluate(self, now=None):
        return self.verdict


def test_resolver_divergence_blocks_buys_and_sticks(config, risk, reporter):
    from dead_bracket.resolver_guard import DIVERGENT, Mismatch, Verdict

    verdict = Verdict(state=DIVERGENT, reason="ours=27C resolver=26C", pairs=40,
                      mismatches=(Mismatch(1_752_890_400, 27, 26),))
    worker, executor, metar = build_worker(
        config=config, risk=risk, reporter=reporter,
        resolver=_StubGuard(verdict))
    metar._tracker._running_max = 22  # 21C bracket is dead

    worker.process_book(_dead_no_snapshot())

    assert ('paris', 21) not in executor.positions  # nothing bought
    assert risk.is_city_blocked("paris") is True
    assert "27C" in risk.sticky_block_reason("paris")
    assert any("TRADING BLOCKED" in ln for ln in reporter.progress_lines)
    # Sticky: it must still be blocked on the next trading day.
    risk.roll_day("2026-07-20")
    assert risk.is_city_blocked("paris") is True


def test_resolver_divergence_is_persisted_for_the_next_process(config, risk,
                                                              reporter, tmp_path):
    from dead_bracket.resolver_guard import DIVERGENT, ResolverBlockStore, Verdict

    store = ResolverBlockStore(str(tmp_path / "blocks.json"))
    worker, _executor, metar = build_worker(
        config=config, risk=risk, reporter=reporter,
        resolver=_StubGuard(Verdict(state=DIVERGENT, reason="ours=27C resolver=26C")),
        resolver_blocks=store)
    metar._tracker._running_max = 22
    worker.process_book(_dead_no_snapshot())

    assert ResolverBlockStore(str(tmp_path / "blocks.json")).blocked("paris")


def test_unverified_resolver_withholds_buys_without_a_sticky_block(config, risk,
                                                                   reporter):
    from dead_bracket.resolver_guard import UNVERIFIED, Verdict

    worker, executor, metar = build_worker(
        config=config, risk=risk, reporter=reporter,
        resolver=_StubGuard(Verdict(state=UNVERIFIED, reason="no resolver rows yet")))
    metar._tracker._running_max = 22
    worker.process_book(_dead_no_snapshot())

    assert ('paris', 21) not in executor.positions
    # Not sticky: coverage arriving later must re-enable the city by itself.
    assert risk.sticky_block_reason("paris") is None
    assert risk.is_city_blocked("paris") is False


def test_verified_resolver_allows_the_buy(config, risk, reporter):
    from dead_bracket.resolver_guard import OK, Verdict

    worker, executor, metar = build_worker(
        config=config, risk=risk, reporter=reporter,
        resolver=_StubGuard(Verdict(state=OK, reason="48 obs matched over 23.5h",
                                    pairs=48, span_hours=23.5)))
    metar._tracker._running_max = 22
    worker.process_book(_dead_no_snapshot())

    assert executor.positions[("paris", 21)].shares > 0


def test_worker_without_a_resolver_is_unchanged(config, risk, reporter):
    # The guard is opt-in at construction; existing wiring must not change.
    worker, executor, metar = build_worker(config=config, risk=risk, reporter=reporter)
    metar._tracker._running_max = 22
    worker.process_book(_dead_no_snapshot())
    assert executor.positions[("paris", 21)].shares > 0


def test_strict_mode_waits_for_resolver_to_confirm_the_running_max(config, risk,
                                                                   reporter):
    from dataclasses import replace as _replace

    from dead_bracket.resolver_guard import OK, Verdict

    class _Guard(_StubGuard):
        def __init__(self, verdict, confirms):
            super().__init__(verdict)
            self._confirms = confirms

        def confirms_max(self, rmax, now=None):
            return self._confirms

    strict = _replace(config, resolver_confirm_dead=True)
    ok = Verdict(state=OK, reason="matched", pairs=48, span_hours=23.5)

    # Resolver has not caught up: agreement alone must not authorise the buy.
    worker, executor, metar = build_worker(
        config=strict, risk=risk, reporter=reporter,
        resolver=_Guard(ok, confirms=False))
    metar._tracker._running_max = 22
    worker.process_book(_dead_no_snapshot())
    assert ("paris", 21) not in executor.positions

    # Once its own table reaches the running max, the entry proceeds.
    risk2 = RiskManager(strict)
    worker2, executor2, metar2 = build_worker(
        config=strict, risk=risk2, reporter=reporter,
        resolver=_Guard(ok, confirms=True))
    metar2._tracker._running_max = 22
    worker2.process_book(_dead_no_snapshot())
    assert executor2.positions[("paris", 21)].shares > 0


# ---------------------------------------------------------------------------
# False-dead alarm: a 99.9c recycle sell that never fills


def _alarm_worker(minutes, clock):
    """A worker whose recycle alarm fires after ``minutes``, on a fake clock."""

    from dataclasses import replace
    cfg = replace(Config(), recycle_alarm_min=minutes)
    risk = RiskManager(cfg)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018", monotonic=clock)
    metar.poll_once()
    return worker, ex, metar, risk, rep


def test_unfilled_recycle_sell_halts_the_city():
    """The one independent check on a live position: nobody bids ~$1 for a NO
    that is not actually certain, so a sell that never fills is evidence our
    deadness call — and therefore our source — is wrong."""

    t = [0.0]
    worker, ex, metar, risk, rep = _alarm_worker(90.0, lambda: t[0])

    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    assert ex.positions[("paris", 22)].resting_sell == 100  # resting, unfilled
    assert risk.city_halt_reason("paris") is None

    t[0] = 89 * 60                       # just inside the limit
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert risk.city_halt_reason("paris") is None

    t[0] = 91 * 60                       # past it
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert risk.city_halt_reason("paris") == "unfilled recycle sell"
    assert any("bracket 22" in n and "unfilled" in n for n in rep.progress_lines)


def test_recycle_alarm_clock_resets_when_the_sell_fills():
    """A fill is the good outcome — it must clear the clock, not leave a city
    one slow fill away from a spurious halt later in the day."""

    t = [0.0]
    worker, ex, metar, risk, rep = _alarm_worker(90.0, lambda: t[0])
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))

    t[0] = 60 * 60
    # A 0.999 bid lifts the resting sell.
    worker.process_book(make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])]))
    assert ex.positions[("paris", 22)].resting_sell == 0
    assert any(line.startswith("SOLD") for line in rep.console)

    t[0] = 200 * 60                      # long past the limit, but nothing rests
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert risk.city_halt_reason("paris") is None


def test_recycle_alarm_can_be_disabled():
    t = [0.0]
    worker, ex, metar, risk, rep = _alarm_worker(0.0, lambda: t[0])
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    t[0] = 10_000 * 60
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert risk.city_halt_reason("paris") is None


class _SwitchableResolver:
    """A resolver stub whose verdict the test flips mid-scenario."""

    def __init__(self):
        from dead_bracket.resolver_guard import OK, Verdict
        self.verdict = Verdict(state=OK, reason="ok", pairs=48, span_hours=23.5)

    def evaluate(self, now=None):
        return self.verdict

    def confirms_max(self, running_max, now=None):
        return True

    def go_stale(self):
        from dead_bracket.resolver_guard import UNVERIFIED, Verdict
        self.verdict = Verdict(state=UNVERIFIED, reason="resolver rows stale (3.2h old)")


def test_alarm_still_fires_after_the_resolver_goes_unverified():
    """Regression for 2026-09-20, Singapore.

    The bot bought 500 NO on a bracket that was not actually dead. Later that
    day NOAA's rows for the station went stale, the guard turned UNVERIFIED, and
    process_book started returning before the alarm check — so the one check
    built for exactly this situation went silent with the position on the book.
    """

    from dataclasses import replace
    t = [0.0]
    cfg = replace(Config(), recycle_alarm_min=90.0)
    risk = RiskManager(cfg)
    rep = StubReporter()
    resolver = _SwitchableResolver()
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep, resolver=resolver,
        metar_raw="LFPB 031530Z 24/10 Q1018", monotonic=lambda: t[0])
    metar.poll_once()

    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    assert ex.positions[("paris", 22)].resting_sell == 100

    resolver.go_stale()                 # entry gate now closes every tick
    t[0] = 91 * 60
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))

    assert risk.city_halt_reason("paris") == "unfilled recycle sell"
    assert any("bracket 22" in n and "unfilled" in n for n in rep.progress_lines)


def test_alarm_reports_even_when_the_city_was_already_halted():
    """The report is the point: a city already paused for another reason (a
    stale feed, say) must not swallow the warning about a held position."""

    from dataclasses import replace
    t = [0.0]
    cfg = replace(Config(), recycle_alarm_min=90.0)
    risk = RiskManager(cfg)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=cfg, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018", monotonic=lambda: t[0])
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))

    risk.halt_city("paris", "stale feed", recoverable=True)
    t[0] = 91 * 60
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))  # once only

    alarms = [n for n in rep.progress_lines if "unfilled" in n]
    assert len(alarms) == 1 and "bracket 22" in alarms[0]


def test_exits_are_reposted_while_entries_are_gated():
    """A blocked city must still be able to get OUT: a lost exit is re-posted
    even when the resolver has closed the entry gate."""

    resolver = _SwitchableResolver()
    risk = RiskManager(Config())
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=Config(), risk=risk, reporter=rep, resolver=resolver,
        metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    pos = ex.positions[("paris", 22)]

    resolver.go_stale()
    pos.resting_sell = 0.0              # simulate the resting sell being lost
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert pos.resting_sell == pos.shares


# ---------------------------------------------------------------------------
# Settlement against the resolver's table, per city


class _ResolverWithDayMax(_SwitchableResolver):
    """Stub resolver that also reports (ours, resolver's) maxima for a day."""

    def __init__(self, ours, theirs):
        super().__init__()
        self._maxima = (ours, theirs)
        self.asked = []

    def day_maxima(self, day):
        self.asked.append(day)
        return self._maxima


def test_false_dead_settles_as_a_loss_against_the_resolvers_table():
    """Regression for 2026-09-20, Singapore: our METAR peaked at 32, NOAA's
    table never ingested those readings and topped out at 31, and the market
    resolved YES on 31. Paper used to settle against OUR max, so the NO on 31 —
    a real -$191 — was booked as a +$308 win."""

    from datetime import date
    risk = RiskManager(Config())
    rep = StubReporter()
    resolver = _ResolverWithDayMax(ours=32, theirs=31)
    worker, ex, metar = build_worker(
        config=Config(), risk=risk, reporter=rep, resolver=resolver,
        metar_raw="LFPB 031530Z 32/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(31, asks=[(0.38, 100)])]))
    assert ex.positions[("paris", 31)].shares == 100

    day = date(2026, 9, 20)
    worker.roll_day(ended_day=day)

    assert resolver.asked == [day]                 # the ENDED day, not today
    assert ex.positions[("paris", 31)].shares == 0
    assert risk.realized_pnl == pytest.approx(0.0 * 100 - 38.0)   # NO paid $0
    assert any("FALSE DEAD" in n and "31C" in n for n in rep.progress_lines)


def test_dead_path_position_still_wins_when_the_resolver_agrees():
    from datetime import date
    risk = RiskManager(Config())
    rep = StubReporter()
    resolver = _ResolverWithDayMax(ours=32, theirs=32)
    worker, ex, metar = build_worker(
        config=Config(), risk=risk, reporter=rep, resolver=resolver,
        metar_raw="LFPB 031530Z 32/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(31, asks=[(0.95, 100)])]))
    worker.roll_day(ended_day=date(2026, 9, 20))
    assert risk.realized_pnl == pytest.approx(100 - 95.0)
    assert not any("FALSE DEAD" in n for n in rep.progress_lines)


def test_settlement_says_so_when_it_cannot_consult_the_resolver():
    """No resolver rows for the day: settle against our own max, but LOUDLY —
    a false dead there would not show up as a loss."""

    from datetime import date
    risk = RiskManager(Config())
    rep = StubReporter()
    resolver = _ResolverWithDayMax(ours=None, theirs=None)
    worker, ex, metar = build_worker(
        config=Config(), risk=risk, reporter=rep, resolver=resolver,
        metar_raw="LFPB 031530Z 32/10 Q1018")
    metar.poll_once()
    worker.process_book(make_snapshot("paris", [make_bracket(31, asks=[(0.95, 100)])]))
    worker.roll_day(ended_day=date(2026, 9, 20))
    assert any("against OUR max" in n for n in rep.progress_lines)
