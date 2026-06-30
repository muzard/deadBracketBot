"""Reporting & telemetry (spec §8, §11).

Strict console policy: stdout gets ONLY a BUY line per entry fill and a SELL
line per exit fill, plus the single allowed ``HALT: <reason>`` line if the
kill-switch trips. Everything else — discovery, running-max updates,
opportunities not taken, warnings, per-tick state, heartbeats — goes to
append-only per-city log files and never to the console.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from typing import IO, Optional
from zoneinfo import ZoneInfo

from .models import Fill


def _fmt_size(size: float) -> str:
    # Shares are whole units; show as integers when they are.
    return str(int(size)) if float(size).is_integer() else f"{size:g}"


class Reporter:
    def __init__(
        self,
        data_dir: str,
        *,
        stdout: Optional[IO[str]] = None,
        stderr: Optional[IO[str]] = None,
        clock=lambda: datetime.now(timezone.utc),
        write_files: bool = True,
        city_tz: Optional[dict[str, str]] = None,
        progress: bool = False,
    ) -> None:
        self._data_dir = data_dir
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr
        self._clock = clock
        self._write_files = write_files
        self._progress = progress  # human-readable diagnostics -> stderr
        self._lock = threading.Lock()
        # Per-city tz so each day's log folder rolls at the same local-midnight
        # boundary as the running-max reset (spec §6, §11).
        self._tz: dict[str, ZoneInfo] = {}
        for cid, name in (city_tz or {}).items():
            try:
                self._tz[cid] = ZoneInfo(name)
            except Exception:
                pass

    def _log_date(self, city: str) -> str:
        now = self._clock()
        tz = self._tz.get(city)
        return (now.astimezone(tz).date() if tz else now.date()).isoformat()

    # -- console (the only things allowed on stdout) -----------------------
    def buy(self, fill: Fill) -> None:
        line = f"BOUGHT  {fill.city}  {fill.temp}C NO  x{_fmt_size(fill.size)} @ {fill.price:.3f}"
        self._console(line)
        self.log(fill.city, "orders", event="fill", side="buy", temp=fill.temp,
                  size=fill.size, price=fill.price, fee=fill.fee, reason=fill.reason)

    def sell(self, fill: Fill) -> None:
        line = f"SOLD    {fill.city}  {fill.temp}C NO  x{_fmt_size(fill.size)} @ {fill.price:.3f}"
        self._console(line)
        self.log(fill.city, "orders", event="fill", side="sell", temp=fill.temp,
                  size=fill.size, price=fill.price, fee=fill.fee, reason=fill.reason)

    def halt(self, reason: str) -> None:
        # The single allowed non-BUY/SELL console line (spec §8), safety-critical.
        self._console(f"HALT: {reason}")
        self.log("_global", "kill", event="halt", reason=reason)

    def _console(self, line: str) -> None:
        with self._lock:
            self._stdout.write(line + "\n")
            self._stdout.flush()

    # -- progress (stderr; stdout stays pure BUY/SELL/HALT) ----------------
    def progress(self, line: str) -> None:
        """Human-readable diagnostic to stderr, only when progress is enabled.

        stdout remains the pure trade stream (BUY/SELL/HALT); progress, METAR
        readings, and market-health notices go here so operators see signs of
        life without polluting the machine-readable output.
        """

        if not self._progress:
            return
        with self._lock:
            self._stderr.write(line + "\n")
            self._stderr.flush()

    def metar_reading(self, city: str, *, temp: int, running_max: Optional[int],
                      publish_sec: Optional[float] = None, my_sec: Optional[float] = None,
                      total_sec: Optional[float] = None) -> None:
        """One-line METAR progress: city, observed temp, running high, latency.

        Latency is shown as the publish delay (observation -> source receipt)
        with our own receipt delay in brackets: ``delay 170s (+12s)``. When the
        source receipt time is unknown, only the total ``delay Ns`` is shown.
        """

        hi = "--" if running_max is None else f"{running_max}C"
        if publish_sec is not None and my_sec is not None:
            lat = f"   delay {publish_sec:.0f}s (+{my_sec:.0f}s)"
        elif total_sec is not None:
            lat = f"   delay {total_sec:.0f}s"
        else:
            lat = ""
        self.progress(f"METAR   {city:<9} {temp}C   high {hi}{lat}")

    def notice(self, city: str, message: str) -> None:
        """Surface a market/feed problem (also logged as an event by the caller)."""

        self.progress(f"NOTE    {city:<9} {message}")

    # -- telemetry (files only) -------------------------------------------
    def opportunity(self, city: str, *, temp: int, ask: float, depth: float,
                    net: float, acted: bool, why_not: str = "") -> None:
        """Every dead-NO ask < CAP (spec §11): the dataset proving frequency."""

        self.log(city, "opportunities", temp=temp, ask=ask, depth=depth,
                 net=net, acted=acted, why_not=why_not)

    def dead_snapshot(self, city: str, *, temp: int, running_max: Optional[int],
                      ask: Optional[float], bid: Optional[float], depth: float,
                      ask_pre: Optional[float] = None) -> None:
        """The NO book the instant a bracket first goes dead (one row/bracket/day).

        The minimal price dataset for the "no edge elsewhere" question: if the
        market has already priced a bracket to ~$1 by the time our METAR confirms
        it dead, ``ask`` is >= CAP and there is nothing to capture. ``ask_pre`` is
        the last NO ask seen while the bracket was still live (the prior tick), so
        you can see whether the repricing happened before or after confirmation.
        Prices are rounded to keep the file tiny; at most ~one row per bracket per
        day, so it cannot run away with disk.
        """

        def r(x: Optional[float]) -> Optional[float]:
            return None if x is None else round(x, 4)

        self.log(city, "dead", temp=temp, running_max=running_max, ask=r(ask),
                 bid=r(bid), depth=round(depth, 1), ask_pre=r(ask_pre))

    def price_sample(self, city: str, *, temp: int, running_max: Optional[int],
                     ask: Optional[float], bid: Optional[float],
                     obs_age_sec: Optional[float] = None) -> None:
        """One frontier-bracket NO price during a publication window (-> prices.jsonl).

        A sparse time series (>= ``price_sample_sec`` apart, hot window only) for
        measuring how early the market reprices a soon-to-be-dead bracket relative
        to when our METAR lands. ``obs_age_sec`` is how stale the obs backing the
        running max is at sample time; the row ``ts`` plus the separately-logged
        report receipt give the lag. Rounded + throttled to keep the file tiny.
        """

        def r(x: Optional[float]) -> Optional[float]:
            return None if x is None else round(x, 4)

        self.log(city, "prices", temp=temp, running_max=running_max, ask=r(ask),
                 bid=r(bid), obs_age_sec=None if obs_age_sec is None else round(obs_age_sec, 1))

    def settlement(self, city: str, *, temp: int, shares: float, pnl: float) -> None:
        self.log(city, "settlement", temp=temp, shares=shares, pnl=pnl)

    def position(self, city: str, *, temp: int, shares: float, avg_cost: float,
                 resting_sell: float) -> None:
        self.log(city, "positions", temp=temp, shares=shares, avg_cost=avg_cost,
                 resting_sell=resting_sell)

    def event(self, city: str, name: str, **fields) -> None:
        """Generic non-console event (discovery, heartbeat, warnings, errors)."""

        self.log(city, "events", event=name, **fields)

    def log(self, city: str, category: str, **fields) -> None:
        """Append one JSON record to ``data_dir/<city>/<YYYY-MM-DD>/<category>.jsonl``.

        Files are rotated per day (the city's local day) so each trading day's
        telemetry is self-contained.
        """

        if not self._write_files:
            return
        rec = {"ts": self._clock().isoformat(), **fields}
        path = os.path.join(self._data_dir, city, self._log_date(city), f"{category}.jsonl")
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
