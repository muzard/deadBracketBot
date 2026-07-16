"""BalanceTracker: adaptive wallet-balance polling (balance.py)."""

from __future__ import annotations

import pytest

from dead_bracket.balance import LiveBalanceTracker, PaperBalanceTracker


class _Clock:
    """Manually-advanced monotonic clock for deterministic cadence tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_paper_tracker_is_fixed_and_never_polls():
    tr = PaperBalanceTracker(123.0)
    assert tr.cash() == 123.0
    assert tr.refresh() == 123.0
    assert tr.refresh(urgent=True) == 123.0
    assert tr.cash() == 123.0


def test_negative_initial_clamped_to_zero():
    assert PaperBalanceTracker(-5.0).cash() == 0.0
    tr = LiveBalanceTracker(fetch=lambda: -1.0, initial=-9.0)
    assert tr.cash() == 0.0


def test_urgent_bypasses_the_interval():
    clock = _Clock()
    values = iter([100.0, 90.0, 80.0])
    tr = LiveBalanceTracker(fetch=lambda: next(values), fast_sec=2.0,
                            monotonic=clock)
    assert tr.refresh(urgent=True) == 100.0
    # No time has passed, but urgent forces a fresh read each call.
    assert tr.refresh(urgent=True) == 90.0
    assert tr.refresh(urgent=True) == 80.0


def test_interval_throttles_non_urgent_reads():
    clock = _Clock()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return 100.0 + calls["n"]  # always changing so cadence stays fast

    tr = LiveBalanceTracker(fetch=fetch, fast_sec=2.0, monotonic=clock)
    assert tr.refresh() == 101.0  # first read (last_poll was None)
    assert tr.refresh() == 101.0  # within the 2s window -> cached, no new fetch
    assert calls["n"] == 1
    clock.advance(2.0)
    assert tr.refresh() == 102.0  # window elapsed -> a real read
    assert calls["n"] == 2


def test_cools_down_after_consecutive_unchanged_reads():
    clock = _Clock()
    tr = LiveBalanceTracker(fetch=lambda: 50.0, initial=50.0, fast_sec=2.0,
                            slow_sec=16.0, cool_after=3, monotonic=clock)
    # Three unchanged reads (each after the interval elapses) trip the cooldown.
    for _ in range(3):
        tr.refresh()
        clock.advance(tr.interval)
    assert tr.interval == pytest.approx(4.0)  # doubled once at cool_after
    # Keep reading unchanged: the interval keeps doubling up to slow_sec.
    for _ in range(3):
        tr.refresh()
        clock.advance(tr.interval)
    assert tr.interval == pytest.approx(16.0)  # capped at slow_sec


def test_balance_change_re_arms_fast_cadence():
    clock = _Clock()
    seq = iter([50.0, 50.0, 50.0, 50.0, 77.0])
    tr = LiveBalanceTracker(fetch=lambda: next(seq), initial=50.0, fast_sec=2.0,
                            slow_sec=64.0, cool_after=3, monotonic=clock)
    for _ in range(4):
        tr.refresh()
        clock.advance(tr.interval)
    assert tr.interval > 2.0  # cooled down after the unchanged run
    tr.refresh()  # balance moved (77.0) -> snap back to fast
    assert tr.cash() == 77.0
    assert tr.interval == pytest.approx(2.0)


def test_fetch_error_keeps_last_value():
    clock = _Clock()
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise RuntimeError("venue blip")
        return 42.0

    tr = LiveBalanceTracker(fetch=fetch, fast_sec=1.0, monotonic=clock)
    assert tr.refresh() == 42.0
    state["fail"] = True
    clock.advance(5.0)
    assert tr.refresh(urgent=True) == 42.0  # transient failure keeps last known


def test_constructor_validates_args():
    with pytest.raises(ValueError):
        LiveBalanceTracker(fetch=lambda: 0.0, fast_sec=0.0)
    with pytest.raises(ValueError):
        LiveBalanceTracker(fetch=lambda: 0.0, fast_sec=10.0, slow_sec=1.0)
    with pytest.raises(ValueError):
        LiveBalanceTracker(fetch=lambda: 0.0, cool_after=0)
