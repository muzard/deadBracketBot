"""Book every settled position's PnL from how the market ACTUALLY resolved.

**Why this exists.** On 2026-09-20 the bot held 500 NO on Singapore's 31 °C
bracket. Paper settled it as a +$308 win; the market resolved YES on 31 and the
position was a -$191 loss. Settling against the resolver's max (worker.roll_day)
closes most of that gap, but it is still our own reconstruction of the outcome,
made at local midnight — before the market has actually resolved, from a table
that can still change until "the first data point for the following date has
been published". The only ground truth is the market's own resolution.

**How it works.** At each city's local midnight the worker releases the day's
held positions from the caps WITHOUT booking PnL, and enqueues them here with
the event they belong to and the provisional outcome. The reconciler polls the
event until it is fully resolved, then books ``payout * shares - cost`` against
the bracket that actually won — for paper and for live alike, so the daily-loss
breaker and the paper ledger both see what really happened. Any disagreement
with the provisional outcome is reported loudly: it means our reconstruction
of the resolver was wrong, which is the failure that emptied the wallet.

Pending settlements persist in one small JSON file, so a restart between local
midnight and resolution loses nothing. A position whose market cannot be
resolved within ``give_up_hours`` (Polymarket resolves by 11:59 PM ET the day
after, so 48h is generous) is booked at its provisional outcome and flagged.

Pure except for the injected ``resolve(slug) -> Optional[int]`` callable
(``feeds_http.fetch_event_resolution``).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SettlementReconciler:
    """Queue of settled-but-unresolved positions, resolved against the market."""

    def __init__(self, *, path: str, resolve: Callable[[str], Optional[int]],
                 risk, reporter, clock: Callable[[], datetime] = _utcnow,
                 give_up_hours: float = 48.0) -> None:
        self._path = path
        self._resolve = resolve
        self._risk = risk
        self._reporter = reporter
        self._clock = clock
        self._give_up = float(give_up_hours) * 3600.0
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self.resolved = 0
        self.mismatches = 0
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, list):
            self._pending = [d for d in data if isinstance(d, dict)]

    def _save(self) -> None:
        """Caller holds the lock. Atomic replace so a crash cannot truncate it."""

        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._pending, fh, indent=1)
        os.replace(tmp, self._path)

    # -- intake ------------------------------------------------------------
    def enqueue(self, *, city: str, day: str, slug: Optional[str], temp: int,
                shares: float, cost_basis: float, provisional_payout: float) -> None:
        """Hold one settled position until its market resolves."""

        key = (city, day, int(temp))
        with self._lock:
            if any((p["city"], p["day"], p["temp"]) == key for p in self._pending):
                return  # already queued (idempotent across a restart)
            self._pending.append({
                "city": city, "day": day, "slug": slug, "temp": int(temp),
                "shares": float(shares), "cost_basis": float(cost_basis),
                "provisional_payout": float(provisional_payout),
                "queued": self._clock().isoformat(),
            })
            self._save()
        if not slug:
            self._reporter.notice(
                city, f"settlement of {temp}C on {day} has no market to check — it "
                      f"will be booked at its provisional outcome after "
                      f"{self._give_up / 3600:.0f}h")

    @property
    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._pending]

    # -- resolution --------------------------------------------------------
    def poll_once(self) -> list[dict]:
        """Resolve whatever the market has resolved. Returns the items booked."""

        with self._lock:
            items = list(self._pending)
        if not items:
            return []
        winners: dict[str, Optional[int]] = {}
        for slug in {p["slug"] for p in items if p.get("slug")}:
            try:
                winners[slug] = self._resolve(slug)
            except Exception:
                winners[slug] = None
        now = self._clock()
        done: list[dict] = []
        for p in items:
            winner = winners.get(p.get("slug")) if p.get("slug") else None
            if winner is not None:
                payout = 0.0 if p["temp"] == winner else 1.0
                self._book(p, payout, winner=winner)
                done.append(p)
                continue
            age = (now - datetime.fromisoformat(p["queued"])).total_seconds()
            if age >= self._give_up:
                self._book(p, p["provisional_payout"], winner=None)
                done.append(p)
        if done:
            with self._lock:
                keys = {(d["city"], d["day"], d["temp"]) for d in done}
                self._pending = [q for q in self._pending
                                 if (q["city"], q["day"], q["temp"]) not in keys]
                self._save()
        return done

    def _book(self, p: dict, payout: float, *, winner: Optional[int]) -> None:
        city, temp = p["city"], p["temp"]
        pnl = payout * p["shares"] - p["cost_basis"]
        self._risk.record_resolved_pnl(pnl)
        self._reporter.settlement(city, temp=temp, shares=p["shares"], pnl=pnl,
                                  won=payout > 0)
        if winner is None:
            self._reporter.event(city, "settlement_unresolved", day=p["day"],
                                 temp=temp, pnl=pnl, slug=p.get("slug"))
            self._reporter.notice(
                city, f"settled {temp}C on {p['day']} at its PROVISIONAL outcome "
                      f"(pnl {pnl:+.2f}): the market could not be resolved")
            return
        self.resolved += 1
        mismatch = payout != p["provisional_payout"]
        self._reporter.event(city, "settlement_resolved", day=p["day"], temp=temp,
                             winner=winner, pnl=pnl, payout=payout,
                             provisional_payout=p["provisional_payout"],
                             mismatch=mismatch)
        if mismatch:
            self.mismatches += 1
            self._reporter.notice(
                city, f"SETTLEMENT MISMATCH {p['day']}: {temp}C was provisionally "
                      f"settled at ${p['provisional_payout']:.0f}/share but the "
                      f"market resolved YES on {winner}C (pays ${payout:.0f}), pnl "
                      f"{pnl:+.2f}. Our reconstruction of the resolver was wrong.")
        else:
            self._reporter.notice(
                city, f"settled {temp}C on {p['day']}: market resolved YES on "
                      f"{winner}C, pnl {pnl:+.2f}")
        if payout == 0.0 and hasattr(self._risk, "pause_forecast"):
            # Fail closed on ANY actual loss, as the worker does provisionally.
            self._risk.pause_forecast(
                f"resolved loss: {city} {temp}C was the daily high on {p['day']}")


class ReconcilerWorker:
    """Runs the reconciler on a slow cadence in a daemon thread."""

    def __init__(self, reconciler: SettlementReconciler, reporter, *,
                 poll_sec: float = 600.0,
                 monotonic: Callable[[], float] = None) -> None:
        import time as _time

        self.reconciler = reconciler
        self._reporter = reporter
        self._poll_sec = float(poll_sec)
        self._mono = monotonic or _time.monotonic
        self._last: Optional[float] = None
        self._stop = threading.Event()
        self._thread = None

    def tick(self, now: Optional[float] = None) -> None:
        now = self._mono() if now is None else now
        if self._last is not None and (now - self._last) < self._poll_sec:
            return
        self._last = now
        try:
            self.reconciler.poll_once()
        except Exception as exc:
            self._reporter.event("_global", "reconcile_error", error=repr(exc))

    def run(self) -> None:  # pragma: no cover - loop; tick() carries the logic
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(1.0)

    def start(self) -> None:  # pragma: no cover - thread lifecycle
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name="settlement-reconciler")
        self._thread.start()

    def stop(self) -> None:  # pragma: no cover - thread lifecycle
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
