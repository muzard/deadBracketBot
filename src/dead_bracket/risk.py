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

import json
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import Config
from .models import Fill, MarketSnapshot


class Ledger:
    """Disk-backed cumulative PnL, so the bankroll survives a restart.

    Paper mode has no wallet to read the truth back from, so without this every
    restart would reset the sizing base to the starting balance and a long soak
    would never show a compounding (or shrinking) bankroll. Live mode reads the
    real wallet instead and uses this only as a record.

    The file is tiny and rewritten atomically on each realised PnL change:
    ``{"initial_balance": ..., "cumulative_pnl": ..., "updated": ...}``.
    """

    def __init__(self, path: str, *, initial_balance: float = 0.0) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.initial_balance = float(initial_balance)
        self.cumulative_pnl = 0.0
        self._load()

    @property
    def path(self) -> str:
        return self._path

    @property
    def equity(self) -> float:
        return max(0.0, self.initial_balance + self.cumulative_pnl)

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            self.cumulative_pnl = float(data.get("cumulative_pnl", 0.0))
        except (TypeError, ValueError):
            self.cumulative_pnl = 0.0
        # The starting balance is whatever the FIRST run recorded; a later
        # --balance must not silently rebase the equity curve mid-soak.
        try:
            stored = data.get("initial_balance")
            if stored is not None:
                self.initial_balance = float(stored)
        except (TypeError, ValueError):
            pass

    def save(self, cumulative_pnl: float) -> None:
        with self._lock:
            self.cumulative_pnl = float(cumulative_pnl)
            rec = {
                "initial_balance": self.initial_balance,
                "cumulative_pnl": self.cumulative_pnl,
                "equity": self.equity,
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self._path)


class RiskManager:
    def __init__(self, config: Config, *, initial_balance: float = 0.0,
                 ledger: Optional["Ledger"] = None) -> None:
        self._cfg = config
        self._lock = threading.RLock()
        # Starting account balance. Current equity = this + CUMULATIVE realised
        # PnL, so the exposure caps grow as the bot wins and shrink as it loses.
        self._initial_balance = initial_balance
        # Cumulative realised PnL since inception. Distinct from _realized_pnl,
        # which is the DAILY figure the loss breaker reads and roll_day resets:
        # zeroing the sizing base every midnight made the paper bankroll snap
        # back to its starting value forever, so results never compounded.
        # Persisted via ``ledger`` so a restart resumes the equity curve too.
        self._ledger = ledger
        self._cumulative_pnl = ledger.cumulative_pnl if ledger is not None else 0.0
        # Optional ground-truth equity source (live mode): when set, the
        # percentage-sizing base is this callable's value (real wallet cash +
        # open exposure) instead of the synthetic initial_balance + realised PnL,
        # so the caps self-correct against the actual wallet. See
        # set_equity_source / _balance_base.
        self._equity_source: Optional[Callable[[], float]] = None

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
        # Sticky per-city blocks (resolver divergence). Unlike _city_halted these
        # survive roll_day: a source that disagrees with the resolver is still
        # the wrong source tomorrow. Cleared only by clear_sticky_block, i.e. by
        # a human running tools/resolver_clear.
        self._sticky_blocked: dict[str, str] = {}
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

    def set_equity_source(self, source: Optional[Callable[[], float]]) -> None:
        """Install (or clear) the ground-truth equity source for percentage
        sizing. Live mode wires ``wallet_cash + total_exposure`` here so the caps
        track real capital; paper leaves it None and uses the synthetic base."""

        with self._lock:
            self._equity_source = source

    def _balance_base(self) -> float:
        """The equity used as the percentage-sizing base. Prefers the injected
        ground-truth source (real wallet cash + open exposure, live) and falls
        back to the synthetic ``initial_balance + CUMULATIVE realised PnL``
        (paper). Caller must hold the lock; the source itself is read outside no
        extra lock — the RLock is re-entrant so a source reading
        ``total_exposure`` is safe."""

        if self._equity_source is not None:
            try:
                return max(0.0, float(self._equity_source()))
            except Exception:
                # A transient balance-read failure must not zero the caps; fall
                # back to the synthetic base rather than halting sizing.
                pass
        return max(0.0, self._initial_balance + self._cumulative_pnl)

    @property
    def cumulative_pnl(self) -> float:
        """Realised PnL since inception (survives daily rollovers/restarts)."""

        with self._lock:
            return self._cumulative_pnl

    def _record_pnl(self, delta: float) -> None:
        """Book realised PnL into both the daily and the cumulative figure.
        Caller must hold the lock."""

        self._realized_pnl += delta
        self._cumulative_pnl += delta
        if self._ledger is not None:
            try:
                self._ledger.save(self._cumulative_pnl)
            except Exception:
                pass  # a ledger write must never break trading/settlement

    @property
    def current_balance(self) -> float:
        """Current account equity, the base for percentage sizing
        (config.bankroll_pct): the ground-truth wallet equity in live mode, else
        starting balance + realised PnL."""

        with self._lock:
            return self._balance_base()

    def _equity_known(self) -> bool:
        """Whether the account size is actually known to us.

        The solvency and concentration clamps below are only meaningful against
        a real equity figure. A run with no wallet source and no starting
        balance (static dollar caps, the historical default and most unit
        tests) has nothing to clamp against, so those runs keep the plain
        static caps rather than being silently zeroed. Caller holds the lock."""

        return self._equity_source is not None or self._initial_balance > 0

    def _cap_dollars(self, static_cap: float) -> float:
        """The active dollar exposure cap. In percentage-sizing mode it's
        ``bankroll_pct`` of the current equity; otherwise the static config cap.
        When equity is known the result is clamped to ``max_deployed_pct`` of
        it, so the caps can never authorise money the account does not have —
        the paper book used to let a $100 balance "buy" $1000 of NO because the
        static caps never consulted the balance at all. Caller holds the lock."""

        if self._cfg.bankroll_pct > 0:
            cap = self._cfg.bankroll_pct / 100.0 * self._balance_base()
        else:
            cap = static_cap
        if self._equity_known():
            cap = min(cap, self._cfg.max_deployed_pct / 100.0 * self._balance_base())
        return max(0.0, cap)

    def _available_dollars(self) -> Optional[float]:
        """Capital not already committed: the deployable ceiling minus open
        exposure, or None when equity is unknown (no clamp). Every remaining-cap
        query is clamped by this so concurrent brackets cannot collectively
        overspend the account. Caller holds the lock."""

        if not self._equity_known():
            return None
        ceiling = self._cfg.max_deployed_pct / 100.0 * self._balance_base()
        return max(0.0, ceiling - self._total_exposure)

    def _clamp_available(self, dollars: float) -> float:
        """Apply the available-capital clamp when equity is known."""

        avail = self._available_dollars()
        return dollars if avail is None else min(dollars, avail)

    def city_exposure(self, city: str) -> float:
        with self._lock:
            return self._city_exposure.get(city, 0.0)

    def bracket_shares(self, city: str, temp: int) -> float:
        with self._lock:
            return self._bracket_shares.get((city, temp), 0.0)

    def is_city_blocked(self, city: str) -> bool:
        """True if new entries for ``city`` must not be placed."""

        with self._lock:
            return (self._halted or city in self._city_halted
                    or city in self._sticky_blocked)

    def block_city_sticky(self, city: str, reason: str) -> bool:
        """Block a city until a human clears it — survives the daily rollover.

        Used for resolver divergence: our source and the resolver's published
        table disagreed, so every "certain" dead bracket in this city is
        actually a guess. Returns True if newly blocked.
        """

        with self._lock:
            if city in self._sticky_blocked:
                return False
            self._sticky_blocked[city] = reason
            return True

    def clear_sticky_block(self, city: str) -> bool:
        """Manual resolution: lift a sticky block. Returns True if one existed."""

        with self._lock:
            return self._sticky_blocked.pop(city, None) is not None

    def sticky_block_reason(self, city: str) -> Optional[str]:
        with self._lock:
            return self._sticky_blocked.get(city)

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

    def forecast_exposure_for(self, city: str, temp: int) -> float:
        """Open $ forecast exposure on one bracket (0 if it's a pure dead-path
        position). Lets the worker keep forecast entries on their immediate
        de-risk exit while opportunity-gating only the dead-path positions."""

        with self._lock:
            return self._forecast_exposure.get((city, temp), 0.0)

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
            cap = self._cfg.forecast_daily_cap_pct / 100.0 * self._balance_base()
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
        """Remaining shares for a bracket: the lesser of the share cap, the
        per-bracket DOLLAR cap (``max_exposure_per_bracket``, or a percentage of
        current equity when ``bankroll_pct`` is set), the CONCENTRATION limit
        (``max_bracket_pct`` of equity — no single bracket may be the whole
        account), and the capital actually available. Converted to shares at the
        CAP price (conservative)."""

        with self._lock:
            key = (city, temp)
            cap_dollars = self._cap_dollars(self._cfg.max_exposure_per_bracket)
            if self._equity_known():
                cap_dollars = min(
                    cap_dollars,
                    self._cfg.max_bracket_pct / 100.0 * self._balance_base(),
                )
            share_rem = max(0.0, self._cfg.max_shares_per_bracket - self._bracket_shares.get(key, 0.0))
            dollars_rem = max(0.0, cap_dollars - self._bracket_exposure.get(key, 0.0))
            dollars_rem = self._clamp_available(dollars_rem)
            return min(share_rem, dollars_rem / self._cfg.cap)

    def per_city_cap_remaining(self, city: str) -> float:
        with self._lock:
            cap_dollars = self._cap_dollars(self._cfg.max_exposure_per_city)
            used = self._city_exposure.get(city, 0.0)
            remaining_dollars = self._clamp_available(max(0.0, cap_dollars - used))
            return remaining_dollars / self._cfg.cap

    def total_cap_remaining(self) -> float:
        with self._lock:
            cap_dollars = self._cap_dollars(self._cfg.max_total_exposure)
            remaining_dollars = self._clamp_available(
                max(0.0, cap_dollars - self._total_exposure))
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
            self._record_pnl(fill.notional - cost_released)
        self._check_daily_loss()

    def record_settlement(self, *, city: str, temp: int, shares: float, cost_basis: float,
                          payout: float = 1.0, realise: bool = True) -> None:
        """A NO settled: free exposure and (by default) realise PnL.

        ``payout`` is the per-share redemption: $1 for every bracket that was
        NOT the day's high, $0 for the bracket that WON it — a forecast entry
        whose predicted kill never confirmed, or a dead-path false dead where
        our running max ran ahead of the resolver's table.

        ``realise=False`` frees the exposure without booking PnL. Live uses it:
        redemption happens on-chain and the wallet is the equity source there,
        so the executor releases the caps but does not synthesise a result."""

        if shares <= 0:
            return
        with self._lock:
            key = (city, temp)
            self._release_forecast(key, cost_basis)
            self._bracket_shares[key] = max(0.0, self._bracket_shares.get(key, 0.0) - shares)
            self._bracket_exposure[key] = max(0.0, self._bracket_exposure.get(key, 0.0) - cost_basis)
            self._city_exposure[city] = max(0.0, self._city_exposure.get(city, 0.0) - cost_basis)
            self._total_exposure = max(0.0, self._total_exposure - cost_basis)
            if realise:
                self._record_pnl(payout * shares - cost_basis)
        if realise:
            self._check_daily_loss()

    def record_resolved_pnl(self, pnl: float) -> None:
        """Book a settlement's PnL once the market has ACTUALLY resolved it.

        The exposure was already released at the city's local midnight
        (``record_settlement(..., realise=False)``); this is the result, taken
        from the market's own resolution rather than our reconstruction of it
        (see reconcile.py). Feeds the paper ledger and the daily-loss breaker in
        both modes — so in live a false-dead loss now reaches the breaker.
        """

        with self._lock:
            self._record_pnl(pnl)
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

        Clears the global kill-switch, all per-city halts and error counters,
        and resets the DAILY PnL the loss breaker reads, so the bot runs
        unattended across days. Returns True only on the first call for a given
        day.

        Deliberately NOT cleared:

        * **Exposure and share counts.** They are released exactly by the
          events that end a position — a sell fill (``record_sell_fill``) or a
          settlement (``record_settlement``) — and positions are now settled at
          each city's OWN local midnight, not at this shared boundary. Wiping
          exposure here would drop every still-open position from the caps: a
          city west of this boundary is mid-afternoon at it, and a city east of
          it may already hold positions in its new day.
        * Cumulative PnL (the sizing base must compound across days), the
          sticky forecast pause, and sticky resolver blocks.
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
            self._realized_pnl = 0.0
            return True

    def _check_daily_loss(self) -> None:
        with self._lock:
            if -self._realized_pnl >= self._cfg.max_daily_loss:
                # _check_daily_loss is always called outside the trip's own lock
                # need; RLock makes the re-entrant trip() call safe.
                self.trip(f"daily loss breach ({self._realized_pnl:.2f})")
