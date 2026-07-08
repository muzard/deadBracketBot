"""Sub-hourly feed collection for the forecast path (forecast.py).

One :class:`FeedCollector` + :class:`FeedPollerWorker` per forecast-enabled
city. The collector polls its injected fetch callable (built by
``feeds_http.build_feed_fetcher``), appends every NEW observation to the
city's un-dated ``data/<city>/feed.jsonl`` (multi-day joins are the point —
the calibration tool pairs these with metar.jsonl), and holds the freshest
reading behind a lock for the CityWorker to pull each tick.

SIGNAL-ONLY, like the LFS calibration collectors this mirrors: nothing here
reaches a MetarFeed, a running max, or the dead-bracket logic — readings go
exclusively to ForecastStrategy. Never writes stdout.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .forecast import FeedReading
from .lfs_calibration import append_jsonl, read_jsonl, utcnow

# The fetch contract from feeds_http.build_feed_fetcher:
# None = fetch failure; [] = fetched fine, nothing parsed.
FetchReadings = Callable[[], Optional[list[tuple[datetime, float]]]]


class FeedCollector:
    """Polls one city's sub-hourly feed; logs new obs; serves the freshest.

    Dedupes on ``obs_time`` (feeds re-serve the same interval until the next
    one publishes), is restart-safe (re-seeds seen obs times and the latest
    reading from its own jsonl), and records receive latency
    (``ts_recv - obs_time``) per row — the latency IS the product: it is the
    information lead the forecast path trades on.
    """

    def __init__(self, *, city: str, src: str, station: str, path: str,
                 fetch: FetchReadings, clock: Callable[[], datetime] = utcnow) -> None:
        self.city = city
        self.src = src
        self.station = station
        self._path = path
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self.polls = 0
        self.failures = 0
        self.new_records = 0
        self._seen: set[str] = set()
        self._latest: Optional[FeedReading] = None
        for rec in read_jsonl(path):  # restart-safe seed
            obs_raw = rec.get("obs_time")
            temp = rec.get("temp_c")
            if not obs_raw or temp is None:
                continue
            self._seen.add(str(obs_raw))
            try:
                obs_dt = datetime.fromisoformat(str(obs_raw))
                recv = datetime.fromisoformat(str(rec.get("ts_recv")))
            except (TypeError, ValueError):
                continue
            if self._latest is None or obs_dt > self._latest.obs_time:
                self._latest = FeedReading(src=src, station=station, obs_time=obs_dt,
                                           temp_c=float(temp), ts_recv=recv)

    def latest(self) -> Optional[FeedReading]:
        """The freshest reading seen so far (thread-safe; None before first)."""

        with self._lock:
            return self._latest

    def poll_once(self) -> list[FeedReading]:
        """One poll. Returns only NEW readings (deduped on obs_time)."""

        self.polls += 1
        rows = self._fetch()
        if rows is None:
            self.failures += 1
            return []
        now = self._clock()
        fresh: list[FeedReading] = []
        for obs_time, temp_c in sorted(rows):
            obs_key = obs_time.isoformat()
            if obs_key in self._seen:
                continue
            self._seen.add(obs_key)
            reading = FeedReading(src=self.src, station=self.station,
                                  obs_time=obs_time, temp_c=temp_c, ts_recv=now)
            append_jsonl(self._path, {
                "ts_recv": now.isoformat(),
                "src": self.src,
                "station": self.station,
                "obs_time": obs_key,
                "temp_c": temp_c,
                "latency_sec": round((now - obs_time).total_seconds(), 1),
            })
            self.new_records += 1
            fresh.append(reading)
        if fresh:
            with self._lock:
                if self._latest is None or fresh[-1].obs_time > self._latest.obs_time:
                    self._latest = fresh[-1]
        if len(self._seen) > 20000:  # bound across long runs; dedupe only needs recents
            keep = sorted(self._seen)[-5000:]
            self._seen = set(keep)
        return fresh


class FeedPollerWorker:
    """Runs one city's FeedCollector on its cadence in a daemon thread.

    Same discipline as LfsCalibrationWorker: every poll is guarded (a failing
    fetch/parse logs a ``feed_error`` event and skips the tick, never crashes
    the thread), health counters flush periodically to the reporter's event
    stream, and nothing is ever written to stdout.
    """

    def __init__(self, *, city: str, collector: FeedCollector, reporter,
                 poll_sec: float, health_every_sec: float = 600.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.city = city
        self.collector = collector
        self._reporter = reporter
        self._poll_sec = poll_sec
        self._health_every = health_every_sec
        self._mono = monotonic
        self._last_poll: Optional[float] = None
        self._last_health: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _due(self, last: Optional[float], every: float, now: float) -> bool:
        return last is None or (now - last) >= every

    def tick(self, now: Optional[float] = None) -> None:
        """One scheduling pass (extracted from ``run`` so tests need no thread)."""

        now = self._mono() if now is None else now
        if self._due(self._last_poll, self._poll_sec, now):
            self._last_poll = now
            try:
                self.collector.poll_once()
            except Exception as exc:
                self._reporter.event(self.city, "feed_error",
                                     src=self.collector.src, error=repr(exc))
        if self._due(self._last_health, self._health_every, now):
            self._last_health = now
            self._reporter.event(self.city, "feed_health", src=self.collector.src,
                                 polls=self.collector.polls,
                                 failures=self.collector.failures,
                                 new_records=self.collector.new_records)

    # -- thread lifecycle ----------------------------------------------------
    def run(self) -> None:  # pragma: no cover - loop; tick() carries the logic
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # belt-and-braces: tick already guards
                self._reporter.event(self.city, "feed_error",
                                     src=self.collector.src, error=repr(exc))
            self._stop.wait(1.0)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name=f"feed-{self.city}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
