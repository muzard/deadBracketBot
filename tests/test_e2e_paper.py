"""End-to-end paper run over a recorded book sequence (spec §12).

Deterministic BUY/SELL console output and PnL from a fixed METAR + book script.
"""

from __future__ import annotations

import pytest

from dead_bracket.config import Config
from dead_bracket.risk import RiskManager
from conftest import StubReporter, build_worker, make_bracket, make_snapshot


def test_recorded_sequence_deterministic_output_and_pnl():
    config = Config()
    risk = RiskManager(config)
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep,
        metar_raw="LFPB 031530Z 24/10 Q1018",  # running max 24; brackets <24 dead
    )
    metar.poll_once()

    # Recorded book sequence (fees disabled for clean arithmetic).
    sequence = [
        # Tick 1: bracket 22 cheap NO ask -> buy floor(0.5*100)=50 @ 0.96.
        make_snapshot("paris", [make_bracket(22, asks=[(0.96, 100)])]),
        # Tick 2: a 0.999 bid appears -> the resting maker sell fills (50).
        make_snapshot("paris", [make_bracket(22, bids=[(0.999, 50)])]),
        # Tick 3: bracket 23 cheap NO ask -> buy floor(0.5*40)=20 @ 0.97.
        make_snapshot("paris", [make_bracket(23, asks=[(0.97, 40)])]),
    ]
    for snap in sequence:
        worker.process_book(snap)

    # Bracket 23 stays unsold; held to $1 at settlement.
    settlements = ex.settle()
    for s in settlements:
        rep.settlement(s.city, temp=s.temp, shares=s.shares, pnl=s.pnl)

    assert rep.console == [
        "BOUGHT  paris  22C NO  x50 @ 0.960",
        "SOLD    paris  22C NO  x50 @ 0.999",
        "BOUGHT  paris  23C NO  x20 @ 0.970",
    ]

    # PnL: sell recycle 50*(0.999-0.96)=1.95 ; settlement 20*(1-0.97)=0.60.
    assert risk.realized_pnl == pytest.approx(1.95 + 0.60)
    # No exposure left open after settlement.
    assert risk.total_exposure == pytest.approx(0.0)
