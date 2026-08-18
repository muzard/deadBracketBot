"""Risk manager: caps, kill-switch triggers, daily loss (spec §9, §12)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dead_bracket.models import Fill
from dead_bracket.risk import RiskManager
from conftest import make_bracket, make_snapshot


def buy(city, temp, price, size, fee=0.0):
    return Fill(city=city, temp=temp, side="buy", price=price, size=size, fee=fee)


def sell(city, temp, price, size):
    return Fill(city=city, temp=temp, side="sell", price=price, size=size, fee=0.0)


def test_per_bracket_cap_remaining(config):
    r = RiskManager(config)
    assert r.per_bracket_cap_remaining("paris", 22) == 500
    r.record_buy_fill(buy("paris", 22, 0.95, 300))
    assert r.per_bracket_cap_remaining("paris", 22) == 200


def test_per_city_and_total_caps_in_shares(config):
    r = RiskManager(config)
    # per-city dollar cap 500 / cap 0.98 -> ~510 shares.
    assert r.per_city_cap_remaining("paris") == 500.0 / config.cap
    assert r.total_cap_remaining() == 1000.0 / config.cap
    r.record_buy_fill(buy("paris", 22, 0.98, 100, fee=0.0))  # $98 exposure
    assert r.per_city_cap_remaining("paris") == (500.0 - 98.0) / config.cap
    assert r.total_cap_remaining() == (1000.0 - 98.0) / config.cap


def test_bankroll_pct_caps_track_balance(config):
    # User's example: 50% of a $30 balance caps a bet at $15; a $5 win grows the
    # balance to $35 so the next bet may be up to $17.50.
    cfg = replace(config, bankroll_pct=50.0, max_shares_per_bracket=100_000)
    r = RiskManager(cfg, initial_balance=30.0)
    assert r.current_balance == pytest.approx(30.0)
    # City/total track the $15 bankroll budget; the per-bracket cap is
    # additionally held to max_bracket_pct (25%) of equity = $7.50, so one
    # bracket can no longer be the whole account (post-Seoul concentration cap).
    assert r.total_cap_remaining() == pytest.approx(15.0 / cfg.cap)
    assert r.per_city_cap_remaining("paris") == pytest.approx(15.0 / cfg.cap)
    assert r.per_bracket_cap_remaining("paris", 22) == pytest.approx(7.5 / cfg.cap)

    r.record_sell_fill(sell("paris", 22, 1.0, 5), cost_released=0.0)  # realise +$5
    assert r.current_balance == pytest.approx(35.0)
    assert r.total_cap_remaining() == pytest.approx(17.5 / cfg.cap)
    assert r.per_bracket_cap_remaining("paris", 22) == pytest.approx(8.75 / cfg.cap)


def test_bankroll_pct_shrinks_on_loss(config):
    cfg = replace(config, bankroll_pct=50.0, max_shares_per_bracket=100_000)
    r = RiskManager(cfg, initial_balance=30.0)
    # A $10 realised loss (sold below cost) drops the balance to $20 -> cap $10.
    r.record_sell_fill(sell("paris", 22, 0.0, 10), cost_released=10.0)  # -$10
    assert r.current_balance == pytest.approx(20.0)
    assert r.total_cap_remaining() == pytest.approx(10.0 / cfg.cap)


def test_bankroll_pct_subtracts_open_exposure(config):
    cfg = replace(config, bankroll_pct=50.0, max_shares_per_bracket=100_000,
                  max_bracket_pct=100.0)  # isolate from the concentration cap
    r = RiskManager(cfg, initial_balance=100.0)  # $50 budget
    r.record_buy_fill(buy("paris", 22, 0.50, 40))  # $20 open exposure
    # Equity unchanged by an open buy; remaining budget = $50 - $20 = $30.
    assert r.current_balance == pytest.approx(100.0)
    assert r.per_bracket_cap_remaining("paris", 22) == pytest.approx(30.0 / cfg.cap)


def test_equity_source_drives_percentage_sizing(config):
    # Live mode: the sizing base is the ground-truth wallet equity
    # (wallet cash + open exposure), not the synthetic initial + realised PnL.
    cfg = replace(config, bankroll_pct=50.0, max_shares_per_bracket=100_000)
    r = RiskManager(cfg, initial_balance=0.0)  # synthetic base would be $0
    wallet = {"cash": 100.0}
    r.set_equity_source(lambda: wallet["cash"] + r.total_exposure)
    assert r.current_balance == pytest.approx(100.0)  # equity source wins
    assert r.total_cap_remaining() == pytest.approx(50.0 / cfg.cap)

    # A buy moves cash -> exposure; equity (hence the cap) is unchanged.
    r.record_buy_fill(buy("paris", 22, 0.50, 40))  # $20 cost
    wallet["cash"] -= 20.0
    assert r.current_balance == pytest.approx(100.0)
    assert r.per_city_cap_remaining("paris") == pytest.approx((50.0 - 20.0) / cfg.cap)

    # A failing equity source falls back to the synthetic base (never zeroes caps).
    def boom():
        raise RuntimeError("balance read failed")

    r.set_equity_source(boom)
    assert r.current_balance == pytest.approx(0.0 + r.realized_pnl)


def test_sell_frees_exposure(config):
    r = RiskManager(config)
    r.record_buy_fill(buy("paris", 22, 0.95, 100))
    before = r.total_exposure
    r.record_sell_fill(sell("paris", 22, 0.999, 100), cost_released=95.0)
    assert r.total_exposure < before
    assert r.bracket_shares("paris", 22) == 0


def test_crossed_book_halts_only_that_city(config):
    r = RiskManager(config)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.40, 10)], bids=[(0.60, 10)])])
    assert r.check_book(snap) is True
    assert r.is_city_blocked("paris") is True
    assert r.is_city_blocked("london") is False
    assert r.halted is False  # NOT the global kill-switch


def test_invalid_ask_halts_city(config):
    r = RiskManager(config)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(1.5, 10)])])
    assert r.check_book(snap) is True
    assert r.is_city_blocked("paris") is True
    assert r.halted is False


def test_stale_feed_halts_city_and_recovers(config):
    r = RiskManager(config)
    assert r.check_stale(city="paris", is_stale=True) is True
    assert r.is_city_blocked("paris") is True
    assert r.halted is False  # per-city, not global
    # Feed returns -> the stale halt auto-recovers.
    assert r.check_stale(city="paris", is_stale=False) is False
    assert r.is_city_blocked("paris") is False


def test_repeated_order_errors_halt_only_that_city(config):
    r = RiskManager(replace(config, max_order_errors=3))
    assert r.note_order_error("paris") is False
    assert r.note_order_error("paris") is False
    assert r.note_order_error("paris") is True  # third halts paris
    assert r.is_city_blocked("paris") is True
    assert r.is_city_blocked("london") is False
    assert r.halted is False


def test_order_ok_resets_city_counter(config):
    r = RiskManager(replace(config, max_order_errors=2))
    r.note_order_error("paris")
    r.note_order_ok("paris")
    assert r.note_order_error("paris") is False  # counter reset, not yet at 2
    assert r.is_city_blocked("paris") is False


def test_roll_day_rearms_and_is_idempotent(config):
    r = RiskManager(config)
    r.halt_city("paris", "crossed/invalid book", recoverable=True)
    r.trip("daily loss breach")  # global
    r.record_buy_fill(buy("paris", 22, 0.95, 100))
    assert r.roll_day("2026-06-09") is True
    assert r.halted is False and r.halt_reason is None
    assert r.is_city_blocked("paris") is False
    assert r.total_exposure == 0.0 and r.realized_pnl == 0.0
    # All caps free again after the EOD rollover.
    assert r.per_bracket_cap_remaining("paris", 22) == config.max_shares_per_bracket
    assert r.per_city_cap_remaining("paris") == config.max_exposure_per_city / config.cap
    assert r.roll_day("2026-06-09") is False  # idempotent for the same day


def test_pop_global_halt_announces_once(config):
    r = RiskManager(config)
    r.trip("daily loss breach")
    assert r.pop_global_halt() == "daily loss breach"
    assert r.pop_global_halt() is None  # only once


def test_daily_loss_breaker(config):
    r = RiskManager(replace(config, max_daily_loss=10.0))
    r.record_buy_fill(buy("paris", 22, 0.95, 100))  # cost 95
    # Sell far below cost to realise a >$10 loss.
    r.record_sell_fill(sell("paris", 22, 0.80, 100), cost_released=95.0)  # proceeds 80, loss 15
    assert r.realized_pnl < 0
    assert r.halted is True
    assert "daily loss" in (r.halt_reason or "")


def test_settlement_realises_profit(config):
    r = RiskManager(config)
    r.record_buy_fill(buy("paris", 22, 0.95, 100))  # cost 95
    r.record_settlement(city="paris", temp=22, shares=100, cost_basis=95.0)
    assert r.realized_pnl == pytest.approx(5.0)
    assert r.bracket_shares("paris", 22) == 0


def test_city_halt_isolated(config):
    r = RiskManager(config)
    r.halt_city("paris", "revision")
    assert r.is_city_blocked("paris") is True
    assert r.is_city_blocked("london") is False
    assert r.halted is False  # global switch untouched


def test_recoverable_city_halt_can_be_cleared(config):
    r = RiskManager(config)
    r.halt_city("paris", "crossed/invalid book", recoverable=True)
    assert r.is_city_blocked("paris") is True
    assert r.recover_city("paris") is True
    assert r.is_city_blocked("paris") is False
    assert r.recover_city("paris") is False  # nothing left to recover


def test_non_recoverable_city_halt_is_sticky(config):
    # A downward-revision halt must NOT auto-recover (spec §9).
    r = RiskManager(config)
    r.halt_city("paris", "downward revision")  # recoverable defaults False
    assert r.recover_city("paris") is False
    assert r.is_city_blocked("paris") is True


# ---------------------------------------------------------------------------
# Bankroll tracks results (post-Seoul): cumulative equity, solvency,
# concentration, and the sticky resolver block.
# ---------------------------------------------------------------------------
def test_equity_compounds_across_daily_rollovers(config):
    # roll_day used to zero the realised PnL that the sizing base was computed
    # from, so every midnight the bankroll snapped back to the starting balance
    # and results never compounded. The DAILY figure still resets (the loss
    # breaker needs it); the equity base must not.
    cfg = replace(config, bankroll_pct=50.0, max_shares_per_bracket=100_000)
    r = RiskManager(cfg, initial_balance=100.0)
    r.record_sell_fill(sell("paris", 22, 1.0, 20), cost_released=0.0)  # +$20
    assert r.current_balance == pytest.approx(120.0)

    r.roll_day("2026-07-20")
    assert r.realized_pnl == 0.0  # daily figure resets
    assert r.cumulative_pnl == pytest.approx(20.0)
    assert r.current_balance == pytest.approx(120.0)  # equity carried over

    r.record_settlement(city="paris", temp=21, shares=10, cost_basis=10.0, payout=0.0)
    r.roll_day("2026-07-21")
    assert r.cumulative_pnl == pytest.approx(10.0)  # a loss shrinks it too
    assert r.current_balance == pytest.approx(110.0)


def test_ledger_resumes_the_equity_curve_after_a_restart(config, tmp_path):
    from dead_bracket.risk import Ledger

    cfg = replace(config, bankroll_pct=50.0, max_shares_per_bracket=100_000)
    path = str(tmp_path / "paper_ledger.json")
    ledger = Ledger(path, initial_balance=100.0)
    r = RiskManager(cfg, initial_balance=100.0, ledger=ledger)
    r.record_sell_fill(sell("paris", 22, 1.0, 30), cost_released=0.0)  # +$30

    resumed = Ledger(path, initial_balance=999.0)  # a later --balance must not rebase
    assert resumed.initial_balance == pytest.approx(100.0)
    assert resumed.cumulative_pnl == pytest.approx(30.0)
    assert resumed.equity == pytest.approx(130.0)
    r2 = RiskManager(cfg, initial_balance=resumed.initial_balance, ledger=resumed)
    assert r2.current_balance == pytest.approx(130.0)


def test_ledger_write_failure_never_breaks_settlement(config, tmp_path):
    class Boom:
        cumulative_pnl = 0.0

        def save(self, value):
            raise OSError("disk full")

    r = RiskManager(config, initial_balance=100.0, ledger=Boom())
    r.record_sell_fill(sell("paris", 22, 1.0, 10), cost_released=0.0)
    assert r.cumulative_pnl == pytest.approx(10.0)  # accounting still correct


def test_caps_never_exceed_available_capital(config):
    # The paper book used to let a $100 balance "buy" $1000 of NO, because the
    # static dollar caps never consulted the balance at all.
    r = RiskManager(config, initial_balance=100.0)  # static caps, known equity
    assert config.max_exposure_per_bracket == 1000.0  # the old flat cap
    budget = config.max_deployed_pct / 100.0 * 100.0
    assert r.total_cap_remaining() == pytest.approx(budget / config.cap)
    assert r.per_bracket_cap_remaining("paris", 22) == pytest.approx(
        min(budget, config.max_bracket_pct / 100.0 * 100.0) / config.cap)

    r.record_buy_fill(buy("paris", 22, 0.90, 100))  # $90 committed
    assert r.total_cap_remaining() == pytest.approx(0.0)  # nothing left to spend


def test_static_caps_survive_when_equity_is_unknown(config):
    # No wallet source and no starting balance (the historical default): there
    # is nothing to clamp against, so the static caps must apply unchanged
    # rather than being silently zeroed.
    r = RiskManager(config)
    assert r.current_balance == 0.0
    assert r.total_cap_remaining() == pytest.approx(
        config.max_total_exposure / config.cap)


def test_concentration_cap_stops_one_bracket_taking_the_account(config):
    # The Seoul wipe was a single bracket funded with the entire bankroll.
    cfg = replace(config, bankroll_pct=100.0, max_shares_per_bracket=100_000,
                  max_bracket_pct=25.0)
    r = RiskManager(cfg, initial_balance=200.0)
    # Even at bankroll_pct=100 the city cap stops at the 90% solvency ceiling,
    # and one bracket stops at 25% of equity.
    assert r.per_city_cap_remaining("paris") == pytest.approx(180.0 / cfg.cap)
    assert r.per_bracket_cap_remaining("paris", 22) == pytest.approx(50.0 / cfg.cap)


def test_sticky_block_survives_the_daily_rollover(config):
    # Resolver divergence means the source is wrong, which is still true
    # tomorrow — unlike every other per-city halt, roll_day must not clear it.
    r = RiskManager(config)
    assert r.block_city_sticky("seoul", "resolver divergence: 27C vs 26C") is True
    assert r.block_city_sticky("seoul", "again") is False
    assert r.is_city_blocked("seoul") is True

    r.roll_day("2026-07-20")
    assert r.is_city_blocked("seoul") is True
    assert "27C" in r.sticky_block_reason("seoul")
    assert r.recover_city("seoul") is False  # not clearable as a normal halt

    assert r.clear_sticky_block("seoul") is True  # only the manual tool does it
    assert r.is_city_blocked("seoul") is False
    assert r.clear_sticky_block("seoul") is False
