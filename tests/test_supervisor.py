"""Multi-city isolation & supervisor restart (spec §5, §12)."""

from __future__ import annotations

import threading
import time

import pytest

from dead_bracket.supervisor import Supervisor
from conftest import StubReporter, build_worker, make_bracket, make_snapshot


def test_fault_in_one_city_does_not_stop_other(config, risk):
    """An order error in paris is caught; london keeps trading on shared risk."""

    rep = StubReporter()
    paris, paris_ex, paris_metar = build_worker(
        config=config, risk=risk, reporter=rep, city="paris", icao="LFPB",
        metar_raw="LFPB 031530Z 24/10 Q1018",
    )
    london, london_ex, london_metar = build_worker(
        config=config, risk=risk, reporter=rep, city="london", icao="EGLC",
        metar_raw="EGLC 031530Z 24/10 Q1018",
    )
    paris_metar.poll_once()
    london_metar.poll_once()

    # Make paris's executor raise on entry (simulated fault).
    def boom(intent, bracket):
        raise RuntimeError("paris executor exploded")

    paris_ex.execute_entry = boom  # type: ignore

    paris.process_book(make_snapshot("paris", [make_bracket(22, asks=[(0.95, 100)])]))
    # Paris fault was caught, logged as order error, not propagated.
    assert risk.halted is False  # single error doesn't trip the switch

    # London is unaffected and trades normally.
    london.process_book(make_snapshot("london", [make_bracket(22, asks=[(0.95, 100)])]))
    assert any(line.startswith("BOUGHT  london") for line in rep.console)
    assert not any(line.startswith("BOUGHT  paris") for line in rep.console)


class _FakeWorker:
    """A worker whose run() exits immediately, to exercise respawn logic."""

    def __init__(self, name):
        self.city = name
        self.runs = 0
        self._stopped = False

    def run(self):
        self.runs += 1
        # Exit immediately to simulate an unexpected death.

    def stop(self):
        self._stopped = True


def test_supervisor_restarts_dead_worker():
    fake = _FakeWorker("paris")
    sup = Supervisor([fake], restart_backoff=0.0)

    # Continue until the worker has been (re)spawned at least twice.
    def keep_going():
        return fake.runs < 2

    sup.run_forever(poll=0.0, should_continue=keep_going)
    assert fake.runs >= 2  # the dead worker was restarted
    assert fake._stopped is True  # stop() called on teardown


def test_maybe_roll_day_settles_and_rearms(config, risk):
    from datetime import datetime, timezone
    from dead_bracket.models import Intent
    rep = StubReporter()
    worker, ex, metar = build_worker(
        config=config, risk=risk, reporter=rep, metar_raw="LFPB 031530Z 24/10 Q1018")
    metar.poll_once()
    # Open a position and halt the city, then roll into a new day.
    ex.execute_entry(Intent(city="paris", temp=22, side="buy", limit_px=0.98, size=100,
                            tif="IOC", reason="x"), make_bracket(22, asks=[(0.95, 100)]))
    risk.halt_city("paris", "crossed/invalid book", recoverable=True)

    clock = {"t": datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)}
    sup = Supervisor([worker], risk=risk, reporter=rep, clock=lambda: clock["t"])
    assert sup.maybe_roll_day() is False  # first call just records the day
    clock["t"] = datetime(2026, 6, 9, 0, 30, tzinfo=timezone.utc)  # next day
    assert sup.maybe_roll_day() is True
    # Position settled, risk re-armed, city un-halted.
    assert ex.positions[("paris", 22)].shares == 0
    assert risk.is_city_blocked("paris") is False
    assert risk.total_exposure == 0.0
    assert any("NEW TRADING DAY" in ln for ln in rep.progress_lines)


def test_daily_rearm_can_be_disabled(config, risk):
    from datetime import datetime, timezone
    rep = StubReporter()
    worker, _, metar = build_worker(config=config, risk=risk, reporter=rep,
                                    metar_raw="LFPB 031530Z 24/10 Q1018")
    clock = {"t": datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)}
    sup = Supervisor([worker], risk=risk, reporter=rep, clock=lambda: clock["t"],
                     daily_rearm=False)
    sup.maybe_roll_day()
    clock["t"] = datetime(2026, 6, 9, 0, 30, tzinfo=timezone.utc)
    assert sup.maybe_roll_day() is False  # disabled


def test_supervisor_start_and_stop_threads():
    rep = StubReporter()
    cfg_workers = []
    for city, icao in (("paris", "LFPB"), ("london", "EGLC")):
        w, _, _ = build_worker(config=__import__("dead_bracket.config", fromlist=["Config"]).Config(),
                               risk=__import__("dead_bracket.risk", fromlist=["RiskManager"]).RiskManager(
                                   __import__("dead_bracket.config", fromlist=["Config"]).Config()),
                               reporter=rep, city=city, icao=icao)
        cfg_workers.append(w)
    sup = Supervisor(cfg_workers)
    sup.start()
    time.sleep(0.05)
    sup.stop()
    # All threads joined / not alive after stop.
    assert all(not t.is_alive() for t in sup._threads.values())


class _TzWorker:
    """A worker stub with a real timezone that records what it was asked to settle."""

    def __init__(self, city, tz):
        from zoneinfo import ZoneInfo
        self.city = city
        self._tz = ZoneInfo(tz)
        self.settled = []

    def local_date(self, now):
        return now.astimezone(self._tz).date()

    def roll_day(self, ended_day=None):
        self.settled.append(ended_day)


def test_each_city_settles_at_its_own_local_midnight():
    """Regression: every city used to roll at 00:00 UTC.

    Singapore (UTC+8) was then settled 8 hours into its NEXT day, against that
    morning's partial max — which is how a false dead on bracket 31 "won" on
    2026-09-20. Toronto (UTC-4) was settled at 20:00 local, before its own day
    had ended. Each must settle at its own midnight, the day that just ended.
    """

    from datetime import date, datetime, timezone
    sg = _TzWorker("singapore", "Asia/Singapore")
    to = _TzWorker("toronto", "America/Toronto")
    clock = {"t": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)}
    sup = Supervisor([sg, to], clock=lambda: clock["t"])
    sup.maybe_roll_day()                                    # record only

    clock["t"] = datetime(2026, 9, 20, 16, 30, tzinfo=timezone.utc)  # SG 00:30
    sup.maybe_roll_day()
    assert sg.settled == [date(2026, 9, 20)] and to.settled == []

    clock["t"] = datetime(2026, 9, 21, 0, 30, tzinfo=timezone.utc)   # UTC midnight
    sup.maybe_roll_day()
    assert to.settled == []                                 # Toronto is 20:30 still
    assert sg.settled == [date(2026, 9, 20)]                # not settled twice

    clock["t"] = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)   # TO 00:30
    sup.maybe_roll_day()
    assert to.settled == [date(2026, 9, 20)]
