"""CityWorker integration: cycle, telemetry, halts, isolation (spec §3-§9, §12)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from dead_bracket.models import Intent
from dead_bracket.reporter import Reporter
from conftest import StubReporter, build_worker, make_bracket, make_snapshot

NOW = datetime(2026, 6, 3, 16, 0, tzinfo=timezone.utc)


def test_exit_placed_on_open_and_retried_if_lost(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Open a position: the resting 99.9c sell is placed immediately on the fill.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    pos = ex.positions[("paris", 22)]
    assert pos.shares == 50 and pos.resting_sell == 50  # fully covered on open

    # Simulate the resting sell being lost (e.g. an exit placement that failed).
    pos.resting_sell = 0.0
    # Next tick (bracket present, no asks -> no new buy) re-covers the position.
    worker.process_book(make_snapshot("paris", [make_bracket(22)]))
    assert pos.resting_sell == pos.shares  # exit guaranteed back in place


def test_full_buy_then_sell_cycle(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",  # running max 24
    )
    metar.poll_once()
    # Dead bracket 22 with a cheap NO ask.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    assert any(line.startswith("BOUGHT") for line in rep.console)
    # Next book shows a high bid -> resting maker sell fills.
    worker.process_book(make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])]))
    assert any(line.startswith("SOLD") for line in rep.console)


def test_opportunity_logged_even_when_not_acted(config, risk, tmp_path):
    rep = Reporter(str(tmp_path), stdout=io.StringIO())
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",
    )
    metar.poll_once()
    # Depth of 1 -> int(0.5*1)=0 -> not acted, but ask < CAP so it's an opportunity.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 1)])]))
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
    assert n == 1 and shares == 50 and cost == pytest.approx(47.5)
    worker.emit_status()
    line1 = rep.progress_lines[-1]
    assert "open $48/" in line1 and "(50sh, 1pos)" in line1


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
