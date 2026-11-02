"""Settlement reconciliation against the market's ACTUAL resolution.

The regression suite for 2026-09-20: paper booked Singapore's 500-share NO on
bracket 31 as a +$308 win; the market resolved YES on 31 and it was a -$191
loss. Every test here asks "does the booked result match what the market paid?"
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dead_bracket.config import Config
from dead_bracket.feeds_http import resolved_bracket
from dead_bracket.reconcile import ReconcilerWorker, SettlementReconciler
from dead_bracket.risk import RiskManager
from conftest import StubReporter, build_worker, make_bracket, make_snapshot

T0 = datetime(2026, 9, 21, 0, 5, tzinfo=timezone.utc)
SLUG = "highest-temperature-in-singapore-on-september-20-2026"


def _market(title, yes, status="resolved"):
    return {"groupItemTitle": title, "umaResolutionStatus": status,
            "outcomePrices": json.dumps([str(yes), str(1 - yes)])}


def _event(winner_title, titles=("30°C", "31°C", "32°C"), status="resolved"):
    return {"markets": [_market(t, 1 if t == winner_title else 0, status)
                        for t in titles]}


# ---------------------------------------------------------------------------
# resolved_bracket: only a genuine resolution counts


def test_resolved_bracket_reads_the_winning_market():
    assert resolved_bracket(_event("31°C")) == 31


def test_an_open_market_at_0999_is_a_price_not_a_resolution():
    # Paris on 2026-09-23 sat at 27°C 0.999 for hours before resolving.
    ev = {"markets": [
        {"groupItemTitle": "27°C", "umaResolutionStatus": None,
         "outcomePrices": json.dumps(["0.999", "0.001"])},
        {"groupItemTitle": "28°C", "umaResolutionStatus": None,
         "outcomePrices": json.dumps(["0.0005", "0.9995"])},
    ]}
    assert resolved_bracket(ev) is None


def test_partial_or_ambiguous_resolution_is_not_trusted():
    ev = _event("31°C")
    ev["markets"][0]["umaResolutionStatus"] = "proposed"      # one not final
    assert resolved_bracket(ev) is None
    two = _event("31°C")
    two["markets"][2]["outcomePrices"] = json.dumps(["1", "0"])   # two winners
    assert resolved_bracket(two) is None
    assert resolved_bracket({"markets": []}) is None
    assert resolved_bracket(None) is None


# ---------------------------------------------------------------------------
# the reconciler


def _rec(tmp_path, resolve, *, clock=lambda: T0, risk=None, rep=None, give_up=48.0):
    risk = risk or RiskManager(Config())
    rep = rep or StubReporter()
    r = SettlementReconciler(path=str(tmp_path / "pending.json"), resolve=resolve,
                             risk=risk, reporter=rep, clock=clock,
                             give_up_hours=give_up)
    return r, risk, rep


def _singapore(r, provisional=1.0):
    r.enqueue(city="singapore", day="2026-09-20", slug=SLUG, temp=31,
              shares=500.0, cost_basis=191.48, provisional_payout=provisional)


def test_singapore_false_dead_is_booked_as_the_loss_it_was(tmp_path):
    """The exact 2026-09-20 case, settled provisionally as a win."""

    r, risk, rep = _rec(tmp_path, resolve=lambda slug: 31)
    _singapore(r, provisional=1.0)
    booked = r.poll_once()

    assert len(booked) == 1 and r.pending == []
    assert risk.realized_pnl == pytest.approx(-191.48)      # not +308.52
    assert r.mismatches == 1
    assert any("SETTLEMENT MISMATCH" in n and "31C" in n for n in rep.progress_lines)


def test_an_agreeing_resolution_books_quietly(tmp_path):
    r, risk, rep = _rec(tmp_path, resolve=lambda slug: 33)
    r.enqueue(city="madrid", day="2026-09-21", slug="madrid-slug", temp=32,
              shares=100.0, cost_basis=96.5, provisional_payout=1.0)
    r.poll_once()
    assert risk.realized_pnl == pytest.approx(3.5)
    assert r.mismatches == 0
    assert not any("MISMATCH" in n for n in rep.progress_lines)


def test_unresolved_stays_pending_then_books_provisionally(tmp_path):
    t = [T0]
    r, risk, rep = _rec(tmp_path, resolve=lambda slug: None, clock=lambda: t[0])
    _singapore(r, provisional=0.0)
    assert r.poll_once() == [] and len(r.pending) == 1      # market still open

    t[0] = T0 + timedelta(hours=49)                          # past give-up
    r.poll_once()
    assert r.pending == []
    assert risk.realized_pnl == pytest.approx(-191.48)
    assert any("PROVISIONAL" in n for n in rep.progress_lines)


def test_pending_settlements_survive_a_restart(tmp_path):
    r, _, _ = _rec(tmp_path, resolve=lambda slug: None)
    _singapore(r)
    _singapore(r)                                            # idempotent
    assert len(r.pending) == 1

    again, risk, _ = _rec(tmp_path, resolve=lambda slug: 31)  # new process
    assert len(again.pending) == 1
    again.poll_once()
    assert risk.realized_pnl == pytest.approx(-191.48)
    assert _rec(tmp_path, resolve=lambda slug: 31)[0].pending == []


def test_a_resolver_error_never_loses_a_settlement(tmp_path):
    def boom(slug):
        raise RuntimeError("gamma 429")
    r, risk, _ = _rec(tmp_path, resolve=boom)
    _singapore(r)
    assert r.poll_once() == [] and len(r.pending) == 1
    assert risk.realized_pnl == 0.0


def test_a_resolved_loss_reaches_the_daily_loss_breaker(tmp_path):
    """The point of connecting settlement to the real outcome in live too: a
    false dead now counts toward the daily-loss kill-switch."""

    from dataclasses import replace
    cfg = replace(Config(), max_daily_loss=50.0)
    risk = RiskManager(cfg)
    r, _, _ = _rec(tmp_path, resolve=lambda slug: 31, risk=risk)
    _singapore(r)
    r.poll_once()
    assert risk.halted is True


def test_worker_defers_pnl_to_the_market_and_frees_the_caps(tmp_path):
    """End to end: at local midnight the worker frees the caps but books
    nothing; the market's resolution books the real result."""

    from datetime import date
    risk = RiskManager(Config())
    rep = StubReporter()
    winner = [None]
    seen = []
    rec, _, _ = _rec(tmp_path, resolve=lambda slug: seen.append(slug) or winner[0],
                     risk=risk, rep=rep)
    noon = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)   # Paris afternoon
    worker, ex, metar = build_worker(
        config=Config(), risk=risk, reporter=rep, reconciler=rec,
        metar_raw="LFPB 031530Z 32/10 Q1018", wall_clock=lambda: noon)
    metar.poll_once()
    # (No resolver guard here, so confirm-dead does not apply.)
    snap = make_snapshot("paris", [make_bracket(31, asks=[(0.38, 100)])])
    worker.process_book(snap)
    assert risk.total_exposure > 0

    worker.roll_day(ended_day=date(2026, 9, 20))
    assert risk.total_exposure == 0.0                # caps freed at midnight
    assert risk.realized_pnl == 0.0                  # ...but nothing booked yet
    assert len(rec.pending) == 1

    assert rec.pending[0]["slug"] == snap.event_slug  # checked against THIS market
    winner[0] = 31                                   # market resolves YES on 31
    rec.poll_once()
    assert seen[-1] == snap.event_slug
    assert risk.realized_pnl == pytest.approx(0.0 * 100 - 38.0)
    assert rec.pending == []


def test_reconciler_worker_polls_on_its_cadence(tmp_path):
    calls = []
    r, _, _ = _rec(tmp_path, resolve=lambda slug: calls.append(slug) or None)
    _singapore(r)
    w = ReconcilerWorker(r, StubReporter(), poll_sec=600.0)
    w.tick(now=0.0)
    w.tick(now=300.0)
    assert len(calls) == 1
    w.tick(now=601.0)
    assert len(calls) == 2
