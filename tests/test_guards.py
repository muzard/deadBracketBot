"""Defensive-guard and error-path coverage (spec §9, §12)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dead_bracket.executor import PaperExecutor
from dead_bracket.models import Fill, Intent
from dead_bracket.risk import RiskManager
from dead_bracket.strategy import Strategy
from conftest import StubReporter, build_worker, make_bracket, make_snapshot


# -- executor guards -------------------------------------------------------
def test_execute_entry_rejects_non_buy(config, risk):
    ex = PaperExecutor(config, risk)
    sell = Intent(city="paris", temp=22, side="sell", limit_px=0.999, size=10, tif="GTC", reason="x")
    assert ex.execute_entry(sell, make_bracket(22, asks=[(0.9, 10)])) is None


def test_place_exit_ignores_zero_and_buy(config, risk):
    ex = PaperExecutor(config, risk)
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999, size=0, tif="GTC", reason="x"))
    ex.place_exit(Intent(city="paris", temp=22, side="buy", limit_px=0.98, size=5, tif="IOC", reason="x"))
    assert ("paris", 22) not in ex.positions or ex.positions[("paris", 22)].resting_sell == 0


def test_on_book_no_position_is_noop(config, risk):
    ex = PaperExecutor(config, risk)
    assert ex.on_book(make_snapshot("paris", [make_bracket(22, bids=[(0.999, 10)])])) == []


# -- risk guards -----------------------------------------------------------
def test_record_buy_fill_ignores_sell(config):
    r = RiskManager(config)
    r.record_buy_fill(Fill(city="p", temp=1, side="sell", price=0.9, size=10, fee=0))
    assert r.total_exposure == 0.0


def test_record_sell_fill_ignores_buy(config):
    r = RiskManager(config)
    r.record_sell_fill(Fill(city="p", temp=1, side="buy", price=0.9, size=10, fee=0), cost_released=5)
    assert r.realized_pnl == 0.0


def test_record_settlement_zero_shares_noop(config):
    r = RiskManager(config)
    r.record_settlement(city="p", temp=1, shares=0, cost_basis=0)
    assert r.realized_pnl == 0.0


def test_trip_is_idempotent(config):
    r = RiskManager(config)
    assert r.trip("first") is True
    assert r.trip("second") is False
    assert r.halt_reason == "first"


# -- worker error paths ----------------------------------------------------
def test_order_error_halts_only_that_city(config, risk):
    rep = StubReporter()
    cfg = replace(config, max_order_errors=1)
    risk1 = RiskManager(cfg)
    worker, ex, metar = build_worker(
        config=cfg, risk=risk1, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()

    def boom(intent, bracket):
        raise RuntimeError("kaboom")

    ex.execute_entry = boom  # type: ignore
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    # Per-city pause, NOT the global kill-switch / console HALT.
    assert risk1.is_city_blocked("paris") is True
    assert risk1.halted is False
    assert not any(line.startswith("HALT:") for line in rep.console)
    assert any("repeated order errors" in ln for ln in rep.progress_lines)


def test_order_error_pause_auto_recovers(config, risk):
    """A per-city order-error pause must clear once the venue is reachable again
    (clean maintenance ticks), so a transient blip can't sideline a city all day."""
    rep = StubReporter()
    cfg = replace(config, max_order_errors=1, order_error_recover_ticks=2)
    risk1 = RiskManager(cfg)
    worker, ex, metar = build_worker(
        config=cfg, risk=risk1, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()

    def boom(intent, bracket):
        raise RuntimeError("kaboom")

    ex.execute_entry = boom  # type: ignore
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])])
    worker.process_book(snap)
    assert risk1.is_city_blocked("paris") is True

    # Venue back: clean ticks while paused recover after the configured streak.
    ex.execute_entry = lambda intent, bracket: None  # type: ignore
    worker.process_book(snap)  # streak 1, still paused
    assert risk1.is_city_blocked("paris") is True
    worker.process_book(snap)  # streak 2 -> recovered
    assert risk1.is_city_blocked("paris") is False
    assert any("resumed: order errors cleared" in ln for ln in rep.progress_lines)


def test_metar_fetch_error_is_handled(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(config=config, risk=risk, reporter=rep)

    def boom(_icao):
        raise RuntimeError("network down")

    metar._fetch_fn = boom  # type: ignore
    # Must not raise; running max stays None.
    assert worker.update_running_max() is None


def test_place_exit_error_handled(config, risk):
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()

    def boom(intent):
        raise RuntimeError("exit failed")

    ex.place_exit = boom  # type: ignore
    # Buy succeeds, exit placement raises -> handled as an order error, no crash.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    assert any(line.startswith("BOUGHT") for line in rep.console)
