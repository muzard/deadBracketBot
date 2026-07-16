"""End-to-end paper run over a recorded book sequence (spec §12).

Deterministic BUY/SELL console output and PnL from a fixed METAR + book script.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dead_bracket.config import Config
from dead_bracket.risk import RiskManager
from conftest import StubReporter, build_worker, make_bracket, make_snapshot


def test_recorded_sequence_deterministic_output_and_pnl():
    # Legacy always-rest mode: every buy is immediately backed by a 99.9c sell, so
    # the recycle-on-bid PnL is deterministic (the default gated policy holds to $1
    # unless capital is blocked — covered by the capped-out replay test below).
    config = replace(Config(), recycle_on_opportunity=False)
    risk = RiskManager(config)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",  # running max 24; brackets <24 dead
    )
    metar.poll_once()

    # Recorded book sequence (fees disabled for clean arithmetic).
    sequence = [
        # Tick 1: bracket 22 cheap NO ask -> buy the full depth 100 @ 0.96 (frac 1.0).
        make_snapshot("paris", [make_bracket(22, asks=[(0.96, 100)])]),
        # Tick 2: a 0.999 bid appears -> the resting maker sell fills (100).
        make_snapshot("paris", [make_bracket(22, bids=[(0.999, 100)])]),
        # Tick 3: bracket 23 cheap NO ask -> buy the full depth 40 @ 0.97.
        make_snapshot("paris", [make_bracket(23, asks=[(0.97, 40)])]),
    ]
    for snap in sequence:
        worker.process_book(snap)

    # Bracket 23 stays unsold; held to $1 at settlement.
    settlements = ex.settle()
    for s in settlements:
        rep.settlement(s.city, temp=s.temp, shares=s.shares, pnl=s.pnl)

    assert rep.console == [
        "BOUGHT  paris  22C NO  x100 @ 0.960",
        "SOLD    paris  22C NO  x100 @ 0.999",
        "BOUGHT  paris  23C NO  x40 @ 0.970",
    ]

    # PnL: sell recycle 100*(0.999-0.96)=3.90 ; settlement 40*(1-0.97)=1.20.
    assert risk.realized_pnl == pytest.approx(3.90 + 1.20)
    # No exposure left open after settlement.
    assert risk.total_exposure == pytest.approx(0.0)


def test_later_bracket_plays_while_earlier_still_held():
    """A city's later bracket is bought even though an earlier bracket is still
    open (no recycle in between): with the static caps leaving headroom, two
    freshly-dead brackets in the same city are held concurrently.
    """

    config = Config()
    risk = RiskManager(config)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",  # running max 24; brackets <24 dead
    )
    metar.poll_once()

    # Tick 1: bracket 22 goes dead and cheap -> buy the full depth 100 @ 0.96.
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.96, 100)])]))
    # Tick 2: the high has climbed, bracket 23 now freshly dead and cheap. 22 is
    # still held (its resting sell never filled) yet 23 must still be bought.
    worker.process_book(make_snapshot("paris", [make_bracket(23, asks=[(0.97, 40)])]))

    assert rep.console == [
        "BOUGHT  paris  22C NO  x100 @ 0.960",
        "BOUGHT  paris  23C NO  x40 @ 0.970",
    ]
    # Both positions are open at once.
    assert ex.positions[("paris", 22)].shares == 100
    assert ex.positions[("paris", 23)].shares == 40


def test_capped_out_city_replays_after_funds_freed():
    """Gated recycling (default): an earlier bracket consumes the whole budget and
    is held to $1; when a later dead bracket appears but is blocked by the caps,
    the held bracket is recycled (99.9c sell posted) to free capital; once a 0.999
    bid lifts that sell the freed funds buy the later bracket.
    """

    # Tight caps so a single bracket exhausts the budget; full-depth taking and a
    # venue minimum on the later bracket so the conservative residual can't trade.
    config = replace(Config(), max_total_exposure=48.0, max_exposure_per_city=48.0,
                     max_frac_of_depth=1.0)
    risk = RiskManager(config)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",  # running max 24
    )
    metar.poll_once()

    # Tick 1: bracket 22 dead+cheap -> consumes essentially the whole budget. No
    # competing opportunity yet, so it is held to $1 (no recycle sell posted).
    worker.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.96, 50)])]))
    pos22 = ex.positions[("paris", 22)]
    bought22 = pos22.shares
    assert bought22 > 0
    assert pos22.resting_sell == 0  # held to $1 by default
    assert risk.total_cap_remaining() < 10  # capped out (small conservative slack)

    # Tick 2: bracket 23 is dead+cheap but funds are locked in 22 -> blocked. 22 is
    # still in the book (held positions are always fetched), so it is recycled: a
    # 99.9c sell is posted to free capital. Nothing new is bought yet.
    snap_both = make_snapshot("paris", [
        make_bracket(22), make_bracket(23, asks=[(0.97, 50)], min_order_size=10)])
    worker.process_book(snap_both)
    assert pos22.resting_sell > 0  # recycle sell posted to cover the shortfall
    assert ("paris", 23) not in ex.positions or ex.positions[("paris", 23)].shares == 0

    # Tick 3: a 0.999 bid lifts 22's resting maker sell -> exposure frees IN the
    # same tick, and the freed funds immediately buy the still-blocked bracket 23.
    snap_fill = make_snapshot("paris", [
        make_bracket(22, bids=[(0.999, bought22)]),
        make_bracket(23, asks=[(0.97, 50)], min_order_size=10)])
    worker.process_book(snap_fill)
    assert ex.positions[("paris", 23)].shares > 0

    bought = [ln for ln in rep.console if ln.startswith("BOUGHT")]
    sold = [ln for ln in rep.console if ln.startswith("SOLD")]
    assert any("22C" in ln for ln in bought)
    assert any("22C" in ln for ln in sold)
    assert any("23C" in ln for ln in bought)
    # 23 is only ever bought after the sell that freed the funds.
    assert rep.console.index(next(ln for ln in rep.console if "SOLD" in ln)) < \
        rep.console.index(next(ln for ln in rep.console if "23C" in ln and "BOUGHT" in ln))
