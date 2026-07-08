"""Risk manager (spec §9).

Shared across all city workers. Owns the authoritative exposure / share state
used for entry sizing, the kill-switch, the daily-loss breaker, and per-city
halts (e.g. downward METAR revisions). Thread-safe: every worker thread queries
and updates it concurrently.

Caps mix units in the spec: per-bracket is a share count, while per-city/total
are dollar exposures. Entry sizing works in shares, so the dollar caps are
converted to a conservative share budget at the CAP price (the most expensive a
NO buy can be), guaranteeing the dollar limit is never exceeded.
"""

from __future__ import annotations

import threading
from typing import Optional

from .config import Config
from .models import Fill, MarketSnapshot


class RiskManager:
    def __init__(self, config: Config, *, initial_balance: float = 0.0) -> None:
        self._cfg = config
        self._lock = threading.RLock()
        # Starting account balance, used only in percentage-sizing mode
        # (config.bankroll_pct > 0). Current balance = this + realised PnL, so
        # the exposure caps grow as the bot wins and shrink as it loses.
        self._initial_balance = initial_balance

        self._bracket_shares: dict[tuple[str, int], float] = {}
        self._bracket_exposure: dict[tuple[str, int], float] = {}  # $ per bracket
        self._city_exposure: dict[str, float] = {}
        self._total_exposure: float = 0.0
        self._realized_pnl: float = 0.0

        # Forecast-path bookkeeping (probabilistic entries, reason "fcast ...").
        # Exposure is tracked per bracket so sells/settlements release the right
        # share of it; the sum is capped at forecast_daily_cap_pct of balance.
        self._forecast_exposure: dict[tuple[str, int], float] = {}
        self._forecast_paused: bool = False  # sticky: NOT cleared by roll_day
        self._forecast_pause_reason: Optional[str] = None

        self._halted: bool = False  # GLOBAL kill-switch (daily-loss only)
        self._halt_reason: Optional[str] = None
        self._halt_announced: bool = False
        self._city_halted: dict[str, str] = {}
        self._city_recoverable: set[str] = set()
        self._order_errors: dict[str, int] = {}  # per-city consecutive errors
        self._rolled_day: Optional[str] = None

    # -- state -------------------------------------------------------------
    @property
    def halted(self) -> bool:
        with self._lock:
            return self._halted

    @property
    def halt_reason(self) -> Optional[str]:
        with self._lock:
            return self._halt_reason

    @property
    def realized_pnl(self) -> float:
        with self._lock:
            return self._realized_pnl

    @property
    def total_exposure(self) -> float:
        with self._lock:
            return self._total_exposure

    @property
    def current_balance(self) -> float:
        """Current account equity = starting balance + realised PnL. This is the
        base for percentage sizing (config.bankroll_pct)."""

        with self._lock:
            return self._initial_balance + self._realized_pnl

    def _cap_dollars(self, static_cap: float) -> float:
        """The active dollar exposure cap. In percentage-sizing mode it's
        ``bankroll_pct`` of the current balance (so all three caps track equity,
        all-in on a single dead bracket); otherwise the static config cap.
        Caller must hold the lock."""

        if self._cfg.bankroll_pct > 0:
            balance = self._initial_balance + self._realized_pnl
            return max(0.0, self._cfg.bankroll_pct / 100.0 * balance)
        return static_cap

    def city_exposure(self, city: str) -> float:
        with self._lock:
            return self._city_exposure.get(city, 0.0)

    def bracket_shares(self, city: str, temp: int) -> float:
        with self._lock:
            return self._bracket_shares.get((city, temp), 0.0)

    def is_city_blocked(self, city: str) -> bool:
        """True if new entries for ``city`` must not be placed."""

        with self._lock:
            return self._halted or city in self._city_halted

    def city_cap_dollars(self) -> float:
        """Effective per-city dollar cap: bankroll_pct × balance, or the static
        max_exposure_per_city when bankroll_pct is 0."""

        with self._lock:
            return self._cap_dollars(self._cfg.max_exposure_per_city)

    def total_cap_dollars(self) -> float:
        """Effective total dollar cap: bankroll_pct × balance, or the static
        max_total_exposure when bankroll_pct is 0."""

        with self._lock:
            return self._cap_dollars(self._cfg.max_total_exposure)

    def city_halt_reason(self, city: str) -> Optional[str]:
        """The active per-city pause reason for ``city``, or None if not paused."""

        with self._lock:
            return self._city_halted.get(city)

    # -- forecast path (probabilistic entries, reason prefix "fcast") -------
    @property
    def forecast_paused(self) -> bool:
        with self._lock:
            return self._forecast_paused

    @property
    def forecast_pause_reason(self) -> Optional[str]:
        with self._lock:
            return self._forecast_pause_reason

    @property
    def forecast_exposure(self) -> float:
        """Total open $ exposure from forecast entries, across all cities."""

        with self._lock:
            return sum(self._forecast_exposure.values())

    def pause_forecast(self, reason: str) -> bool:
        """Stop ALL new forecast entries (sticky across rollovers — a settled
        loss at p-hat >= 0.97 is a model/mapping bug until a human clears it by
        restarting). The dead-bracket path is unaffected. Returns newly paused."""

        with self._lock:
            if self._forecast_paused:
                return False
            self._forecast_paused = True
            self._forecast_pause_reason = reason
            return True

    def forecast_cap_remaining(self) -> float:
        """Remaining $ the forecast path may deploy across ALL cities — the
        cross-city correlation cap (one synoptic pattern drives many cities):
        forecast_daily_cap_pct of the current balance, minus open forecast
        exposure."""

        with self._lock:
            cap = self._cfg.forecast_daily_cap_pct / 100.0 * (
                self._initial_balance + self._realized_pnl)
            return max(0.0, cap - sum(self._forecast_exposure.values()))

    def _release_forecast(self, key: tuple[str, int], cost_released: float) -> None:
        """Release forecast exposure pro-rata when shares leave a bracket that
        holds a mix of forecast and dead-path cost. Caller must hold the lock,
        and must call this BEFORE reducing the bracket exposure."""

        fc = self._forecast_exposure.get(key, 0.0)
        if fc <= 0.0:
            return
        total = self._bracket_exposure.get(key, 0.0)
        frac = 1.0 if total <= 0 else min(1.0, fc / total)
        remaining = fc - cost_released * frac
        if remaining <= 1e-9:
            self._forecast_exposure.pop(key, None)
        else:
            self._forecast_exposure[key] = remaining

    # -- remaining caps (in shares) ---------------------------------------
    def per_bracket_cap_remaining(self, city: str, temp: int) -> float:
        """Remaining shares for a bracket: the lesser of the share cap and the
        per-bracket DOLLAR cap (``max_exposure_per_bracket``, or a percentage of
        the current balance when ``bankroll_pct`` is set), converted to shares at
        the CAP price (conservative)."""

        with self._lock:
            key = (city, temp)
            cap_dollars = self._cap_dollars(self._cfg.max_exposure_per_bracket)
            share_rem = max(0.0, self._cfg.max_shares_per_bracket - self._bracket_shares.get(key, 0.0))
            dollars_rem = max(0.0, cap_dollars - self._bracket_exposure.get(key, 0.0))
            return min(share_rem, dollars_rem / self._cfg.cap)

    def per_city_cap_remaining(self, city: str) -> float:
        with self._lock:
            cap_dollars = self._cap_dollars(self._cfg.max_exposure_per_city)
            used = self._city_exposure.get(city, 0.0)
            remaining_dollars = max(0.0, cap_dollars - used)
            return remaining_dollars / self._cfg.cap

    def total_cap_remaining(self) -> float:
        with self._lock:
            cap_dollars = self._cap_dollars(self._cfg.max_total_exposure)
            remaining_dollars = max(0.0, cap_dollars - self._total_exposure)
            return remaining_dollars / self._cfg.cap

    # -- fill bookkeeping --------------------------------------------------
    def record_buy_fill(self, fill: Fill) -> None:
        """A taker entry filled: add open shares and dollar exposure (incl. fee)."""

        if fill.side != "buy" or fill.size <= 0:
            return
        with self._lock:
            key = (fill.city, fill.temp)
            self._bracket_shares[key] = self._bracket_shares.get(key, 0.0) + fill.size
            cost = fill.notional + fill.fee
            self._bracket_exposure[key] = self._bracket_exposure.get(key, 0.0) + cost
            self._city_exposure[fill.city] = self._city_exposure.get(fill.city, 0.0) + cost
            self._total_exposure += cost
            if fill.reason.startswith("fcast"):
                self._forecast_exposure[key] = self._forecast_exposure.get(key, 0.0) + cost

    def record_sell_fill(self, fill: Fill, *, cost_released: float) -> None:
        """A maker exit filled: free exposure, reduce shares, realise PnL.

        ``cost_released`` is the cost basis of the shares being sold; proceeds
        minus that basis is the realised PnL contribution.
        """

        if fill.side != "sell" or fill.size <= 0:
            return
        with self._lock:
            key = (fill.city, fill.temp)
            self._release_forecast(key, cost_released)
            self._bracket_shares[key] = max(0.0, self._bracket_shares.get(key, 0.0) - fill.size)
            self._bracket_exposure[key] = max(0.0, self._bracket_exposure.get(key, 0.0) - cost_released)
            self._city_exposure[fill.city] = max(0.0, self._city_exposure.get(fill.city, 0.0) - cost_released)
            self._total_exposure = max(0.0, self._total_exposure - cost_released)
            self._realized_pnl += (fill.notional - cost_released)
        self._check_daily_loss()

    def record_settlement(self, *, city: str, temp: int, shares: float, cost_basis: float,
                          payout: float = 1.0) -> None:
        """A NO settled: free exposure and realise PnL (payout*shares - cost).

        ``payout`` is the per-share redemption: $1 for every bracket that was
        NOT the day's high (all dead-path positions), $0 when the bracket WON
        the day — only possible for forecast entries, whose predicted kill
        never confirmed."""

        if shares <= 0:
            return
        with self._lock:
            key = (city, temp)
            self._release_forecast(key, cost_basis)
            self._bracket_shares[key] = max(0.0, self._bracket_shares.get(key, 0.0) - shares)
            self._bracket_exposure[key] = max(0.0, self._bracket_exposure.get(key, 0.0) - cost_basis)
            self._city_exposure[city] = max(0.0, self._city_exposure.get(city, 0.0) - cost_basis)
            self._total_exposure = max(0.0, self._total_exposure - cost_basis)
            self._realized_pnl += (payout * shares - cost_basis)
        self._check_daily_loss()

    # -- kill-switch / halts ----------------------------------------------
    # Only a portfolio-wide condition (daily-loss) trips the GLOBAL kill-switch.
    # City-specific problems (order errors, stale feed, crossed book, downward
    # revision) halt ONLY that city, so one city never takes down the others.
    def trip(self, reason: str) -> bool:
        """Trip the GLOBAL kill-switch (daily-loss). Returns True if newly tripped."""

        with self._lock:
            if self._halted:
                return False
            self._halted = True
            self._halt_reason = reason
            return True

    def pop_global_halt(self) -> Optional[str]:
        """Return the global halt reason once, for a single console HALT line."""

        with self._lock:
            if self._halted and not self._halt_announced:
                self._halt_announced = True
                return self._halt_reason
            return None

    def halt_city(self, city: str, reason: str, *, recoverable: bool = False) -> bool:
        """Halt new entries for one city only. Returns True if newly halted.

        ``recoverable=True`` marks it auto-clearable via :meth:`recover_city`
        (crossed book, stale feed, order errors). A non-recoverable halt (a
        downward METAR revision, spec §9) persists until the daily rollover.
        """

        with self._lock:
            if city in self._city_halted:
                return False
            self._city_halted[city] = reason
            if recoverable:
                self._city_recoverable.add(city)
            return True

    def recover_city(self, city: str, reason: Optional[str] = None) -> bool:
        """Clear a recoverable per-city halt. If ``reason`` is given, only clear
        when the current halt matches it (so e.g. a fresh feed doesn't clear a
        crossed-book pause). Returns True if a halt was cleared."""

        with self._lock:
            if city not in self._city_recoverable or city not in self._city_halted:
                return False
            if reason is not None and self._city_halted[city] != reason:
                return False
            del self._city_halted[city]
            self._city_recoverable.discard(city)
            return True

    def note_order_error(self, city: str) -> bool:
        """Record an order error for ``city``; halt THAT city after the configured
        run of consecutive errors. Returns True if this error halted the city."""

        with self._lock:
            self._order_errors[city] = self._order_errors.get(city, 0) + 1
            if self._order_errors[city] >= self._cfg.max_order_errors:
                return self.halt_city(city, "repeated order errors", recoverable=True)
            return False

    def note_order_ok(self, city: str) -> None:
        """Reset ``city``'s consecutive order-error counter after a clean order."""

        with self._lock:
            self._order_errors[city] = 0

    def check_book(self, snapshot: MarketSnapshot) -> bool:
        """Halt the city if any bracket book is crossed/invalid. Returns halted."""

        for b in snapshot.brackets:
            if b.is_crossed_or_invalid():
                return self.halt_city(snapshot.city, "crossed/invalid book", recoverable=True)
        return False

    def check_stale(self, *, city: str, is_stale: bool) -> bool:
        """Halt the city on a stale feed; auto-recover when it returns. Returns halted."""

        if is_stale:
            return self.halt_city(city, "stale METAR feed", recoverable=True)
        self.recover_city(city, "stale METAR feed")
        return False

    def roll_day(self, day_key: str) -> bool:
        """Re-arm for a new trading day (idempotent per ``day_key``).

        Clears the global kill-switch, all per-city halts, error counters, and
        resets daily PnL and exposures so the bot runs unattended across days.
        Returns True only on the first call for a given day. Positions should be
        settled by the caller *before* this resets exposure.
        """

        with self._lock:
            if day_key == self._rolled_day:
                return False
            self._rolled_day = day_key
            self._halted = False
            self._halt_reason = None
            self._halt_announced = False
            self._city_halted.clear()
            self._city_recoverable.clear()
            self._order_errors.clear()
            self._bracket_shares.clear()
            self._bracket_exposure.clear()
            self._city_exposure.clear()
            self._forecast_exposure.clear()  # pause stays sticky (human decision)
            self._total_exposure = 0.0
            self._realized_pnl = 0.0
            return True

    def _check_daily_loss(self) -> None:
        with self._lock:
            if -self._realized_pnl >= self._cfg.max_daily_loss:
                # _check_daily_loss is always called outside the trip's own lock
                # need; RLock makes the re-entrant trip() call safe.
                self.trip(f"daily loss breach ({self._realized_pnl:.2f})")
