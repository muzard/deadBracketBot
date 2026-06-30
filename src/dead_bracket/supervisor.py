"""Supervisor (spec §5).

Runs every selected CityWorker concurrently in its own thread, sharing one
RiskManager (global caps / kill-switch) and one Reporter (console policy). I/O
bound, so threads are sufficient. Worker isolation is enforced here: a thread
that dies is restarted, and one city's failure never stops the other.

It also drives the daily rollover: at the configured rollover timezone's
midnight it settles each worker's positions and re-arms the shared risk manager,
so the bot runs unattended across days (new-day markets are discovered by each
city's feed automatically).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .reporter import Reporter
from .risk import RiskManager
from .worker import CityWorker


class Supervisor:
    def __init__(
        self,
        workers: list[CityWorker],
        *,
        risk: Optional[RiskManager] = None,
        reporter: Optional[Reporter] = None,
        rollover_tz: str = "UTC",
        daily_rearm: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        restart_backoff: float = 1.0,
    ) -> None:
        self._workers = workers
        self._risk = risk
        self._reporter = reporter
        self._daily_rearm = daily_rearm
        self._clock = clock
        try:
            self._tz = ZoneInfo(rollover_tz)
        except Exception:
            self._tz = ZoneInfo("UTC")
        self._current_day: Optional[str] = None
        self._threads: dict[str, threading.Thread] = {}
        self._restart_backoff = restart_backoff
        self._stop = threading.Event()

    def _spawn(self, worker: CityWorker) -> threading.Thread:
        t = threading.Thread(target=worker.run, name=f"worker-{worker.city}", daemon=True)
        t.start()
        return t

    def start(self) -> None:
        for w in self._workers:
            self._threads[w.city] = self._spawn(w)

    # -- daily rollover ----------------------------------------------------
    def maybe_roll_day(self) -> bool:
        """Settle + re-arm at the rollover-tz midnight. Returns True if it rolled.

        The first call only records the current day (no roll). Subsequent calls
        on a new day settle every worker's positions and re-arm shared risk once.
        """

        day = self._clock().astimezone(self._tz).date().isoformat()
        if self._current_day is None:
            self._current_day = day
            return False
        if day == self._current_day or not self._daily_rearm:
            self._current_day = day
            return False
        self._current_day = day
        for w in self._workers:
            try:
                w.roll_day()  # settle yesterday's positions, log per city
            except Exception:  # pragma: no cover - defensive
                pass
        if self._risk is not None:
            self._risk.roll_day(day)  # re-arm kill-switch, reset daily PnL/exposure
        if self._reporter is not None:
            self._reporter.progress(f"===== NEW TRADING DAY {day} (risk re-armed) =====")
        return True

    def run_forever(self, *, poll: float = 0.5, should_continue: Callable[[], bool] = lambda: True) -> None:
        """Start workers and supervise them until stopped.

        If a worker thread exits unexpectedly (and we have not been asked to
        stop), it is restarted after a backoff — isolating each city's faults.
        The daily rollover is checked each pass.
        """

        self.start()
        try:
            while not self._stop.is_set() and should_continue():
                self.maybe_roll_day()
                for w in self._workers:
                    t = self._threads.get(w.city)
                    if t is not None and not t.is_alive() and not self._stop.is_set():
                        time.sleep(self._restart_backoff)
                        self._threads[w.city] = self._spawn(w)
                time.sleep(poll)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        for w in self._workers:
            w.stop()
        for t in self._threads.values():
            t.join(timeout=2.0)
