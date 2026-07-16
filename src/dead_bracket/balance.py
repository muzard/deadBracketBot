"""Wallet-balance tracking with adaptive polling.

The bankroll used for percentage sizing must reflect *actual available capital*,
not a synthetic running total that drifts from the wallet. In live mode the real
USDC balance is polled from the venue, but polling has a cost (API rate limit),
so the cadence adapts:

* while a buy is being attempted the tracker is asked to ``refresh(urgent=True)``
  and reads the wallet at the fast cadence, and
* once several consecutive reads come back unchanged (nothing settled) the
  cadence cools down — doubling up to a slow ceiling — until the balance moves
  again, which snaps it back to fast.

Paper mode has no wallet, so :class:`PaperBalanceTracker` holds a fixed synthetic
value and never polls; the :class:`RiskManager` keeps its exact accounting there.
Both implementations share the tiny surface the rest of the system uses:
``cash()`` (last known wallet USDC) and ``refresh(urgent=...)``.
"""

from __future__ import annotations

import time
from typing import Callable, Optional


class PaperBalanceTracker:
    """No wallet: a fixed synthetic balance. ``refresh`` is a no-op.

    Used in paper mode (and as the default in tests) so the balance-refresh call
    in the worker is always safe. The RiskManager keeps its own synthetic
    ``initial_balance + realized_pnl`` accounting in paper, so this value is only
    a display convenience.
    """

    def __init__(self, initial: float = 0.0) -> None:
        self._value = max(0.0, float(initial))

    def cash(self) -> float:
        return self._value

    def refresh(self, *, urgent: bool = False) -> float:  # noqa: ARG002 - interface parity
        return self._value


class LiveBalanceTracker:
    """Polls the real wallet USDC balance with an adaptive cadence.

    ``fetch`` returns the current wallet cash (e.g. ``main._fetch_wallet_balance``
    bound to the live client). Reads are throttled to at most one per ``interval``
    seconds, where ``interval`` starts at ``fast_sec`` and doubles (capped at
    ``slow_sec``) after ``cool_after`` consecutive unchanged reads, snapping back
    to ``fast_sec`` the moment the balance changes. ``refresh(urgent=True)``
    bypasses the throttle so a buy attempt always sizes off a fresh read.

    A ``fetch`` that raises keeps the last known value (a transient venue blip
    must never zero the bankroll); the cadence is left untouched so the next call
    retries promptly.
    """

    def __init__(
        self,
        fetch: Callable[[], float],
        *,
        initial: float = 0.0,
        fast_sec: float = 2.0,
        slow_sec: float = 60.0,
        cool_after: int = 3,
        eps: float = 1e-6,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if fast_sec <= 0:
            raise ValueError("fast_sec must be > 0")
        if slow_sec < fast_sec:
            raise ValueError("slow_sec must be >= fast_sec")
        if cool_after < 1:
            raise ValueError("cool_after must be >= 1")
        self._fetch = fetch
        self._value = max(0.0, float(initial))
        self._fast = float(fast_sec)
        self._slow = float(slow_sec)
        self._cool_after = int(cool_after)
        self._eps = float(eps)
        self._mono = monotonic
        self._interval = self._fast
        self._same_streak = 0
        self._last_poll: Optional[float] = None  # monotonic time of last real read

    @property
    def interval(self) -> float:
        """Current cool-down cadence (seconds) — exposed for telemetry/tests."""

        return self._interval

    def cash(self) -> float:
        return self._value

    def refresh(self, *, urgent: bool = False) -> float:
        """Read the wallet if due (or ``urgent``); adapt the cadence. Returns cash."""

        now = self._mono()
        if (not urgent and self._last_poll is not None
                and now - self._last_poll < self._interval):
            return self._value
        self._last_poll = now
        try:
            new_value = max(0.0, float(self._fetch()))
        except Exception:
            # Transient failure: keep the last known balance, retry next call.
            return self._value
        if abs(new_value - self._value) <= self._eps:
            self._same_streak += 1
            if self._same_streak >= self._cool_after:
                # Cool down: nothing is settling, poll less often.
                self._interval = min(self._slow, self._interval * 2.0)
        else:
            # Balance moved: something settled — re-arm the fast cadence.
            self._same_streak = 0
            self._interval = self._fast
        self._value = new_value
        return self._value
