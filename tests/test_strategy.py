"""Strategy: dead detection, entry gate, sizing, exit (spec §3, §4, §12)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dead_bracket.models import Fill
from dead_bracket.strategy import Strategy, is_dead
from conftest import make_bracket, make_snapshot


def make_strategy(config, risk, city="paris"):
    return Strategy(city, config, risk)


# -- dead detection --------------------------------------------------------
def test_is_dead_strictly_below_running_max():
    assert is_dead(22, 24) is True
    assert is_dead(24, 24) is False  # equal is not dead
    assert is_dead(25, 24) is False
    assert is_dead(22, None) is False


def test_upper_open_bracket_is_never_dead():
    # "T°C or higher" wins once the high reaches T, so it is never dead even when
    # an over-the-top reading pushes the running max above T.
    assert is_dead(34, 35, upper_open=True) is False
    assert is_dead(34, 99, upper_open=True) is False
    # ...while an ordinary bracket at the same temp IS dead below that max.
    assert is_dead(34, 35, upper_open=False) is True


def test_no_buy_on_over_the_top_reading_of_open_ended_bracket(config, risk):
    # Regression: Milan 2026-06-24 — the "34°C or higher" top bracket had its NO
    # at ~0.1c (YES≈$1, it was winning) while our METAR ran to 35. Without the
    # upper_open guard the bot would treat 34 as dead and buy a NO that settles $0.
    strat = make_strategy(config, risk)
    snap = make_snapshot("milan", [make_bracket(34, asks=[(0.001, 9999)], upper_open=True)])
    assert strat.evaluate(snap, running_max=35) == []


def test_higher_non_resolution_reading_does_not_mark_dead(config, risk):
    # running_max is always resolution-grade; here it is None (no METAR yet), so
    # even a tempting cheap NO must not be treated as dead.
    strat = make_strategy(config, risk)
    snap = make_snapshot("paris", [make_bracket(20, asks=[(0.90, 100)])])
    assert strat.evaluate(snap, None) == []


# -- entry gate ------------------------------------------------------------
def test_no_buy_when_not_dead(config, risk):
    strat = make_strategy(config, risk)
    snap = make_snapshot("paris", [make_bracket(24, asks=[(0.90, 100)])])
    assert strat.evaluate(snap, running_max=24) == []  # temp == rmax, not dead


def test_no_buy_when_ask_at_or_above_cap(config, risk):
    strat = make_strategy(config, risk)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.98, 100)])])
    assert strat.evaluate(snap, running_max=24) == []  # ask >= CAP (0.98)


def test_no_buy_when_net_below_min(config, risk):
    # Raise MIN_NET so the economic gate bites for an ask just below CAP.
    cfg = replace(config, min_net=0.025)
    strat = Strategy("paris", cfg, risk)
    # net(0.978) ~= 0.022 - fee < 0.025 -> gated out.
    blocked = make_snapshot("paris", [make_bracket(22, asks=[(0.978, 100)], fees_enabled=True)])
    assert strat.evaluate(blocked, running_max=24) == []
    # net(0.95) ~= 0.047 > 0.025 -> passes.
    ok = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)], fees_enabled=True)])
    assert len(strat.evaluate(ok, running_max=24)) == 1


def test_buy_when_all_gates_pass(config, risk):
    strat = make_strategy(config, risk)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])])
    intents = strat.evaluate(snap, running_max=24)
    assert len(intents) == 1
    it = intents[0]
    assert it.side == "buy" and it.tif == "IOC"
    assert it.limit_px == config.cap  # never above CAP
    assert it.reason == "dead<rmax"
    assert it.temp == 22


# -- sizing ----------------------------------------------------------------
def test_size_respects_depth_at_or_below_cap(config, risk):
    strat = make_strategy(config, risk)
    # 60 @ 0.95 (<=cap) and 100 @ 0.99 (>cap, excluded). avail = 60.
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 60), (0.99, 100)])])
    intents = strat.evaluate(snap, running_max=24)
    # avail = 60 (the 0.99 level is above CAP); default frac 1.0 takes it all.
    assert intents[0].size == 60


def test_size_respects_max_frac_of_depth(config, risk):
    # Pin the anti-sweep fraction so this still exercises the knob itself
    # (the default is now 1.0 = take the whole book).
    cfg = replace(config, max_frac_of_depth=0.5)
    strat = Strategy("paris", cfg, risk)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 200)])])
    # avail=200; frac cap int(0.5*200)=100; per-bracket=500; budget large -> 100.
    assert strat.evaluate(snap, running_max=24)[0].size == 100


def test_size_respects_per_bracket_dollar_cap(config):
    from dead_bracket.risk import RiskManager
    # $10 per-bracket cap -> ~10 shares at CAP, even with deep depth and ample
    # city/total room. This is the "half the bankroll per bracket" knob.
    cfg = replace(config, max_exposure_per_bracket=10.0)
    risk = RiskManager(cfg)
    strat = Strategy("paris", cfg, risk)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])])
    assert strat.evaluate(snap, running_max=24)[0].size == 10


def test_size_respects_per_bracket_cap(config, risk):
    strat = make_strategy(config, risk)
    # Pre-load 480 shares of bracket 22 so only 20 remain.
    risk.record_buy_fill(Fill(city="paris", temp=22, side="buy", price=0.95, size=480, fee=0))
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 1000)])])
    assert strat.evaluate(snap, running_max=24)[0].size == 20


def test_size_respects_total_cap_across_brackets(config, risk):
    strat = make_strategy(config, risk)
    # With frac 1.0 each bracket would take its full 500 of depth, but the shared
    # per-city ($500) / total ($1000) caps bind first: the provisional per-pass
    # deduction stops the four brackets from over-committing the budget.
    brackets = [make_bracket(t, asks=[(0.95, 500)]) for t in (20, 21, 22, 23)]
    snap = make_snapshot("paris", brackets)
    intents = strat.evaluate(snap, running_max=24)
    total = sum(i.size for i in intents)
    # Bound by the per-city cap ($500 / 0.98 CAP ~= 510 shares).
    assert total <= config.max_exposure_per_city / config.cap + 1


def test_bankroll_blocked_volume_reported(config):
    from dead_bracket.risk import RiskManager
    # Per-city cap allows ~10 shares ($10/0.98); the window offers far more, so
    # the rest is bankroll-blocked and tallied (valued at the ask).
    cfg = replace(config, max_exposure_per_city=10.0, max_total_exposure=10.0)
    risk = RiskManager(cfg)  # caps must match the strategy's config
    strat = Strategy("paris", cfg, risk)
    # depth 400 @ 0.95 -> willing = min(400, 500, int(1.0*400)=400) = 400 shares.
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 400)])])
    intents = strat.evaluate(snap, running_max=24)
    took = intents[0].size if intents else 0
    blocked_shares = 400 - took
    assert strat.last_blocked_value == pytest.approx(blocked_shares * 0.95)
    assert strat.last_blocked_value > 0  # bankroll was the binding constraint


def test_no_bankroll_block_when_depth_limited(config, risk):
    # Tiny window, ample bankroll -> nothing blocked by the caps.
    strat = make_strategy(config, risk)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 40)])])
    strat.evaluate(snap, running_max=24)
    assert strat.last_blocked_value == 0.0


def test_zero_size_skipped(config, risk):
    # Pin frac so a 1-share book floors the willing size to 0 (int(0.5*1)=0),
    # exercising the willing<=0 skip branch independent of the default.
    cfg = replace(config, max_frac_of_depth=0.5)
    strat = Strategy("paris", cfg, risk)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 1)])])  # int(0.5*1)=0
    assert strat.evaluate(snap, running_max=24) == []


def test_city_block_halts_entries(config, risk):
    strat = make_strategy(config, risk)
    risk.halt_city("paris", "downward revision")
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])])
    assert strat.evaluate(snap, running_max=24) == []


# -- exit ------------------------------------------------------------------
def test_make_exit_intent_is_gtc_maker_sell(config, risk):
    strat = make_strategy(config, risk)
    fill = Fill(city="paris", temp=22, side="buy", price=0.95, size=30, fee=0)
    exit_intent = strat.make_exit_intent(fill, token_id="tok-22")  # default tick 0.001
    assert exit_intent.side == "sell"
    assert exit_intent.tif == "GTC"
    assert exit_intent.is_maker_exit
    assert exit_intent.limit_px == config.sell_px == 0.999
    assert exit_intent.size == 30
    assert exit_intent.token_id == "tok-22"


def test_make_exit_intent_clamps_to_market_max_on_coarse_tick(config, risk):
    strat = make_strategy(config, risk)
    fill = Fill(city="paris", temp=22, side="buy", price=0.95, size=30, fee=0)
    # 0.01-tick market: max placeable price is 0.99, so 0.999 clamps down.
    coarse = strat.make_exit_intent(fill, token_id="t", tick_size=0.01, neg_risk=True)
    assert coarse.limit_px == 0.99
    assert coarse.neg_risk is True
    # 0.001-tick market: the full 0.999 is placeable.
    fine = strat.make_exit_intent(fill, token_id="t", tick_size=0.001)
    assert fine.limit_px == 0.999


def test_entry_respects_min_order_size(config, risk):
    # Pin frac=0.5 so the depths below give the intended sub/over-minimum sizes.
    cfg = replace(config, max_frac_of_depth=0.5)
    strat = Strategy("paris", cfg, risk)
    # depth 6 -> want floor(0.5*6)=3, below min 5 -> skipped.
    small = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 6)], min_order_size=5)])
    assert strat.evaluate(small, running_max=24) == []
    # depth 20 -> want 10 >= 5 -> allowed.
    ok = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 20)], min_order_size=5)])
    assert strat.evaluate(ok, running_max=24)[0].size == 10


def test_entry_carries_neg_risk_and_tick_grid(config, risk):
    strat = make_strategy(config, risk)
    snap = make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)], neg_risk=True, tick_size=0.01)])
    it = strat.evaluate(snap, running_max=24)[0]
    assert it.neg_risk is True
    assert it.tick_size == 0.01
    assert it.limit_px == 0.98  # CAP on the 0.01 grid, <= max 0.99
