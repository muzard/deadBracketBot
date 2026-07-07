"""Paper executor: entry/exit fills, partials, settlement, PnL (spec §7, §12)."""

from __future__ import annotations

import pytest

from dead_bracket.executor import (
    DisabledExecutor,
    LiveExecutor,
    PaperExecutor,
    make_executor,
)
from dead_bracket.models import Intent
from dead_bracket.risk import RiskManager
from conftest import make_bracket, make_snapshot


def buy_intent(temp, size, px=0.98, city="paris"):
    return Intent(city=city, temp=temp, side="buy", limit_px=px, size=size, tif="IOC", reason="dead<rmax")


def test_entry_fills_against_depth_at_or_below_cap(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 20), (0.97, 30), (0.99, 100)])
    fill = ex.execute_entry(buy_intent(22, 100, px=0.98), bracket)
    # Fills 20@0.95 + 30@0.97 = 50 shares; 0.99 level is above CAP, excluded.
    assert fill is not None
    assert fill.size == 50
    assert fill.price == pytest.approx((20 * 0.95 + 30 * 0.97) / 50)
    assert risk.bracket_shares("paris", 22) == 50


def test_entry_never_pays_above_limit(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.99, 100)])  # all above CAP
    assert ex.execute_entry(buy_intent(22, 50, px=0.98), bracket) is None


def test_entry_respects_requested_size(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 1000)])
    fill = ex.execute_entry(buy_intent(22, 40), bracket)
    assert fill.size == 40


def test_entry_fee_charged_when_enabled(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 10)], fees_enabled=True)
    fill = ex.execute_entry(buy_intent(22, 10), bracket)
    assert fill.fee > 0


def test_exit_resting_sell_created_and_fills_on_high_bid(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 100)])
    fill = ex.execute_entry(buy_intent(22, 100), bracket)
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999,
                         size=fill.size, tif="GTC", reason="recycle"))
    # No bid >= 0.999: nothing fills.
    snap = make_snapshot("paris", [make_bracket(22, bids=[(0.50, 100)])])
    assert ex.on_book(snap) == []
    # A bid >= SELL_PX appears: the maker sell fills (no fee).
    snap2 = make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])])
    sells = ex.on_book(snap2)
    assert len(sells) == 1
    assert sells[0].side == "sell" and sells[0].price == 0.999 and sells[0].fee == 0.0
    assert sells[0].size == 100


def test_exit_uses_fine_tick_price(config, risk):
    """Exits always use 0.001-tick so the fill records at 0.999, even if the
    bracket's orderPriceMinTickSize was 0.01 (coarse mid-range tick)."""
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 100)], tick_size=0.01)
    fill = ex.execute_entry(buy_intent(22, 100), bracket)
    # Worker always passes tick_size=0.001 for exits (fine CLOB tick near $1).
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999,
                         size=fill.size, tif="GTC", reason="recycle", tick_size=0.001))
    snap = make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)], tick_size=0.001)])
    sells = ex.on_book(snap)
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(0.999)
    assert risk.realized_pnl == pytest.approx(100 * 0.999 - 100 * 0.95)


def test_exit_partial_fill_keeps_remainder_resting(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 100)])
    fill = ex.execute_entry(buy_intent(22, 100), bracket)
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999,
                         size=fill.size, tif="GTC", reason="recycle"))
    # Only 40 of bid depth at >= SELL_PX -> partial fill of 40.
    snap = make_snapshot("paris", [make_bracket(22, bids=[(0.999, 40)])])
    sells = ex.on_book(snap)
    assert sells[0].size == 40
    pos = ex.positions[("paris", 22)]
    assert pos.resting_sell == 60  # remainder still resting
    assert pos.shares == 60


def test_exit_never_crosses_as_taker(config, risk):
    # The exit only fills on a bid >= SELL_PX; a bid below it never crosses.
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 100)])
    fill = ex.execute_entry(buy_intent(22, 100), bracket)
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999,
                         size=fill.size, tif="GTC", reason="recycle"))
    snap = make_snapshot("paris", [make_bracket(22, bids=[(0.998, 100)])])
    assert ex.on_book(snap) == []


def test_settlement_holds_unfilled_to_one(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 100)])
    ex.execute_entry(buy_intent(22, 100), bracket)
    results = ex.settle()
    assert len(results) == 1
    # Held to $1 from cost 95 -> PnL 5.
    assert results[0].shares == 100
    assert results[0].pnl == pytest.approx(5.0)


def test_limit_sell_fill_frees_all_caps(config, risk):
    # Buying consumes per-bracket/city/total exposure; when the resting 99.9c
    # limit sell fills, every cap must free back so capital can redeploy.
    ex = PaperExecutor(config, risk)
    ex.execute_entry(buy_intent(22, 100), make_bracket(22, asks=[(0.95, 100)]))
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999,
                         size=100, tif="GTC", reason="recycle"))
    assert risk.total_exposure > 0
    assert risk.per_bracket_cap_remaining("paris", 22) < config.max_shares_per_bracket
    # The limit order fills.
    ex.on_book(make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])]))
    assert risk.bracket_shares("paris", 22) == 0
    assert risk.city_exposure("paris") == pytest.approx(0.0)
    assert risk.total_exposure == pytest.approx(0.0)
    assert risk.per_bracket_cap_remaining("paris", 22) == config.max_shares_per_bracket


def test_eod_settlement_frees_all_caps(config, risk):
    # Position held to EOD (no sell): settle() must free every cap too.
    ex = PaperExecutor(config, risk)
    ex.execute_entry(buy_intent(22, 100), make_bracket(22, asks=[(0.95, 100)]))
    assert risk.total_exposure > 0
    ex.settle()
    assert risk.bracket_shares("paris", 22) == 0
    assert risk.city_exposure("paris") == pytest.approx(0.0)
    assert risk.total_exposure == pytest.approx(0.0)
    assert risk.per_bracket_cap_remaining("paris", 22) == config.max_shares_per_bracket


def test_pnl_via_sell_recycle(config, risk):
    ex = PaperExecutor(config, risk)
    bracket = make_bracket(22, asks=[(0.95, 100)])
    ex.execute_entry(buy_intent(22, 100), bracket)
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999,
                         size=100, tif="GTC", reason="recycle"))
    ex.on_book(make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])]))
    # Sold 100 @ 0.999, cost 95 -> realised 4.9.
    assert risk.realized_pnl == pytest.approx(100 * 0.999 - 95.0)


def test_make_executor_paper_by_default(config, risk):
    assert isinstance(make_executor(config, risk), PaperExecutor)


def test_make_executor_live_requires_client(config, risk):
    live_cfg = config.with_mode("live")
    with pytest.raises(ValueError):
        make_executor(live_cfg, risk, client=None)
    assert isinstance(make_executor(live_cfg, risk, client=object()), LiveExecutor)


def test_make_executor_trade_disabled_beats_live(config, risk):
    # trade_disabled fences the city out of trading even in live mode, and never
    # needs a signing client.
    live_cfg = config.with_mode("live")
    ex = make_executor(live_cfg, risk, client=None, trade_disabled=True)
    assert isinstance(ex, DisabledExecutor)


def test_disabled_executor_places_nothing_and_spares_risk(config, risk):
    ex = DisabledExecutor()
    bracket = make_bracket(22, asks=[(0.95, 100)])
    snap = make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])])

    assert ex.execute_entry(buy_intent(22, 100), bracket) is None
    assert ex.place_exit(buy_intent(22, 100)) is None
    assert ex.on_book(snap) == []
    assert ex.settle() == []
    assert ex.positions == {}
    # No order path touched the shared risk manager.
    assert risk.total_exposure == 0.0
    assert risk.realized_pnl == 0.0
