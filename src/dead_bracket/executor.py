"""Execution layer (spec §7).

Two implementations behind one interface:

* :class:`PaperExecutor` (default) — simulates entry fills against displayed
  depth at <= CAP and the resting 99.9c maker sell filling when a later book
  shows a bid >= SELL_PX, else holds to $1 settlement. Tracks positions and,
  via the shared :class:`RiskManager`, exposure and PnL.
* :class:`LiveExecutor` (``--live``) — submits EIP-712-signed orders through a
  maintained CLOB client. Gated and never exercised by the test suite.

Both own positions and the resting-sell state so the rest of the system only
sees Fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .config import Config
from .fees import fee as fee_fn
from .models import Bracket, Fill, Intent, MarketSnapshot, Position, max_price_for_tick, round_to_tick
from .risk import RiskManager


@dataclass(frozen=True)
class Settlement:
    city: str
    temp: int
    shares: float
    pnl: float
    won: bool = True  # False iff this bracket WAS the day's high (NO paid $0)


class Executor(Protocol):
    def execute_entry(self, intent: Intent, bracket: Bracket) -> Optional[Fill]: ...
    def place_exit(self, intent: Intent) -> None: ...
    def cancel_exit(self, city: str, temp: int) -> None: ...
    def on_book(self, snapshot: MarketSnapshot) -> list[Fill]: ...
    def settle(self, final_max: Optional[int] = None) -> list[Settlement]: ...


class PaperExecutor:
    """Simulated executor. No wallet, no signing (spec §7)."""

    def __init__(self, config: Config, risk: RiskManager) -> None:
        self._cfg = config
        self._risk = risk
        self._positions: dict[tuple[str, int], Position] = {}

    # -- helpers -----------------------------------------------------------
    def _pos(self, city: str, temp: int) -> Position:
        key = (city, temp)
        pos = self._positions.get(key)
        if pos is None:
            pos = Position(city=city, temp=temp)
            self._positions[key] = pos
        return pos

    @property
    def positions(self) -> dict[tuple[str, int], Position]:
        return self._positions

    # -- entry -------------------------------------------------------------
    def execute_entry(self, intent: Intent, bracket: Bracket) -> Optional[Fill]:
        """Fill an IOC taker buy against book depth at price <= limit_px.

        Walks ask levels best-first up to the requested size, never paying above
        ``limit_px`` (== CAP). Returns a single aggregated Fill, or None if
        nothing was available (IOC: any unfilled remainder is discarded).
        """

        if intent.side != "buy" or intent.size <= 0:
            return None
        remaining = intent.size
        filled = 0.0
        notional = 0.0
        fee_total = 0.0
        for px, sz in bracket.no_asks:
            if px > intent.limit_px:
                break
            take = min(remaining, sz)
            if take <= 0:
                continue
            filled += take
            notional += px * take
            if bracket.fees_enabled:
                fee_total += fee_fn(
                    px,
                    fee_rate=self._cfg.fee_rate,
                    fee_exponent=self._cfg.fee_exponent,
                    fees_enabled=True,
                ) * take
            remaining -= take
            if remaining <= 0:
                break

        if filled <= 0:
            return None

        avg_px = notional / filled
        fill = Fill(
            city=intent.city,
            temp=intent.temp,
            side="buy",
            price=avg_px,
            size=filled,
            fee=fee_total,
            reason=intent.reason,
        )
        pos = self._pos(intent.city, intent.temp)
        pos.shares += filled
        pos.cost_basis += notional + fee_total
        self._risk.record_buy_fill(fill)
        return fill

    # -- exit --------------------------------------------------------------
    def place_exit(self, intent: Intent) -> None:
        """Register a resting GTC maker sell (spec §4). Never crosses as taker."""

        if intent.side != "sell" or intent.size <= 0:
            return
        pos = self._pos(intent.city, intent.temp)
        # Cap resting size at held shares; keep remainder resting across ticks.
        pos.resting_sell = min(pos.shares, pos.resting_sell + intent.size)
        # Record the actual grid-clamped resting price (e.g. 0.99 on a 0.01-tick
        # market) so fills/PnL reflect the real exit price, not the raw SELL_PX.
        pos.resting_px = intent.limit_px

    def cancel_exit(self, city: str, temp: int) -> None:
        """Withdraw the resting maker sell for one bracket (revert to hold-to-$1).

        Shares are untouched — only the pending exit is retracted, so the NO is
        held to $1 settlement instead of being recycled at 99.9c.
        """

        pos = self._positions.get((city, temp))
        if pos is None or pos.resting_sell <= 0:
            return
        pos.resting_sell = 0.0
        pos.resting_px = 0.0

    def on_book(self, snapshot: MarketSnapshot) -> list[Fill]:
        """Fill resting maker sells when the book shows a bid >= SELL_PX.

        A maker sell at 0.999 only fills if someone bids up to it. Fill size is
        the lesser of the resting size and the available bid depth at >= SELL_PX.
        """

        fills: list[Fill] = []
        for b in snapshot.brackets:
            key = (snapshot.city, b.temp)
            pos = self._positions.get(key)
            if pos is None or pos.resting_sell <= 0:
                continue
            if b.is_crossed_or_invalid():
                continue  # never fill against a garbage book (e.g. a venue glitch)
            rest_px = pos.resting_px or self._cfg.sell_px
            bid_depth = sum(sz for (px, sz) in b.no_bids if px >= rest_px)
            if bid_depth <= 0:
                continue
            qty = min(pos.resting_sell, bid_depth, pos.shares)
            if qty <= 0:
                continue
            avg_cost = pos.avg_cost
            cost_released = avg_cost * qty
            fill = Fill(
                city=snapshot.city,
                temp=b.temp,
                side="sell",
                price=rest_px,
                size=qty,
                fee=0.0,  # maker: no taker fee
                reason="recycle@sell_px",
            )
            pos.shares -= qty
            pos.cost_basis -= cost_released
            pos.resting_sell -= qty
            self._risk.record_sell_fill(fill, cost_released=cost_released)
            fills.append(fill)
        return fills

    # -- settlement --------------------------------------------------------
    def settle(self, final_max: Optional[int] = None) -> list[Settlement]:
        """Settle all remaining open shares (spec §4, held to resolution).

        ``final_max`` is the day's final running max (the winning bracket): a
        held NO redeems at $1 for every bracket EXCEPT that one, whose NO pays
        $0. Dead-path positions always sit below the final max, so only a
        forecast entry (predicted kill that never confirmed) can hit the $0
        leg. ``final_max=None`` (no METAR data all day) falls back to $1.
        """

        results: list[Settlement] = []
        for (city, temp), pos in self._positions.items():
            if pos.shares <= 0:
                continue
            shares = pos.shares
            cost = pos.cost_basis
            won = final_max is None or temp != final_max
            payout = 1.0 if won else 0.0
            pnl = payout * shares - cost
            self._risk.record_settlement(city=city, temp=temp, shares=shares,
                                         cost_basis=cost, payout=payout)
            pos.shares = 0.0
            pos.cost_basis = 0.0
            pos.resting_sell = 0.0
            results.append(Settlement(city=city, temp=temp, shares=shares, pnl=pnl, won=won))
        return results


@dataclass
class RestingOrder:
    """A live GTC maker sell that we track for fill reconciliation."""

    order_id: str
    size: float
    price: float = 0.0  # grid-clamped resting price (e.g. 0.99 on a 0.01 tick)
    matched: float = 0.0


class OrderError(RuntimeError):
    """A live order was rejected or failed; surfaced to the worker's error path."""


class LiveExecutor:
    """Live signed executor (spec §7, §9). Gated behind ``--live``.

    Uses Polymarket's unified ``polymarket-client`` SDK (``SecureClient``) — the
    archived ``py-clob-client`` is rejected by the venue with "invalid order
    version". The SDK resolves the negRisk exchange and the live tick size per
    token internally; we still clamp/round prices with the Gamma-known tick as a
    belt-and-braces measure. The wallet key lives only in the injected client,
    sourced from the environment / secret store — never logged, never in config
    or telemetry.

    Entry is an IOC-emulated limit buy: a GTC limit at CAP crosses immediately
    for whatever depth is at or below CAP (the exchange enforces the per-share
    limit), and any unfilled remainder is cancelled at once so a buy never rests
    unintentionally. Exit is a ``post_only`` GTC sell — the venue itself
    guarantees it can never cross as a taker (spec §4).
    """

    def __init__(self, config: Config, risk: RiskManager, client: object) -> None:
        if client is None:
            raise ValueError("LiveExecutor requires a configured CLOB client")
        self._cfg = config
        self._risk = risk
        self._client = client
        self._positions: dict[tuple[str, int], Position] = {}
        self._resting: dict[tuple[str, int], list[RestingOrder]] = {}

    @property
    def positions(self) -> dict[tuple[str, int], Position]:
        return self._positions

    def _pos(self, city: str, temp: int) -> Position:
        key = (city, temp)
        pos = self._positions.get(key)
        if pos is None:
            pos = Position(city=city, temp=temp)
            self._positions[key] = pos
        return pos

    # -- order plumbing ----------------------------------------------------
    @staticmethod
    def _grid_price(price: float, tick: float) -> float:
        """Round/clamp a price onto the market's tick grid (Gamma-known tick)."""

        lo, hi = tick, max_price_for_tick(tick)
        return round_to_tick(min(max(price, lo), hi), tick)

    def _place_limit(self, *, token_id: str, price: float, size: float,
                     side: str, post_only: bool):
        """Place one limit order; return the AcceptedOrder, raise on rejection."""

        resp = self._client.place_limit_order(
            token_id=token_id, price=price, size=size, side=side, post_only=post_only)
        if not getattr(resp, "ok", False):
            code = getattr(resp, "code", "?")
            msg = getattr(resp, "message", resp)
            raise OrderError(f"order rejected [{code}]: {msg}")
        return resp

    @staticmethod
    def _parse_buy_match(resp, *, limit_px: float) -> tuple[float, float]:
        """Return (filled_shares, avg_price) from an accepted BUY order.

        For a BUY, ``taking_amount`` is outcome tokens received and
        ``making_amount`` is collateral paid (both Decimals). An implausible
        average price (outside (0, 1.05]) is treated as no fill, so a parsing
        surprise can never over-credit a position.
        """

        try:
            taking = float(getattr(resp, "taking_amount", 0) or 0)
            making = float(getattr(resp, "making_amount", 0) or 0)
        except (TypeError, ValueError):
            return 0.0, 0.0
        if taking <= 0:
            return 0.0, 0.0
        avg_px = making / taking
        if not (0.0 < avg_px <= 1.05):
            return 0.0, 0.0
        return taking, min(avg_px, limit_px)

    # -- entry -------------------------------------------------------------
    def execute_entry(self, intent: Intent, bracket: Bracket) -> Optional[Fill]:
        token = intent.token_id or bracket.token_id
        if not token:
            raise OrderError(f"no NO token id for {intent.city} {intent.temp}C")
        limit_px = self._grid_price(intent.limit_px, intent.tick_size)
        resp = self._place_limit(token_id=token, price=limit_px,
                                 size=intent.size, side="BUY", post_only=False)
        filled, avg_px = self._parse_buy_match(resp, limit_px=limit_px)
        # IOC emulation: whatever did not cross immediately must not rest as a
        # stray bid — cancel the remainder right away (best-effort; if the
        # cancel itself fails we surface it as an order error).
        if filled < intent.size and getattr(resp, "status", "") != "matched":
            try:
                self._client.cancel_order(order_id=resp.order_id)
            except Exception as exc:
                raise OrderError(f"failed to cancel IOC remainder {resp.order_id}: {exc!r}")
        if filled <= 0:
            return None
        # Taker entry fee, estimated with the spec fee model (venue charges its
        # own; this keeps PnL/exposure accounting consistent with paper).
        fee_total = 0.0
        if bracket.fees_enabled:
            fee_total = fee_fn(avg_px, fee_rate=self._cfg.fee_rate,
                               fee_exponent=self._cfg.fee_exponent, fees_enabled=True) * filled
        fill = Fill(city=intent.city, temp=intent.temp, side="buy", price=avg_px,
                    size=filled, fee=fee_total, reason=intent.reason)
        pos = self._pos(intent.city, intent.temp)
        pos.shares += filled
        pos.cost_basis += avg_px * filled + fee_total
        self._risk.record_buy_fill(fill)
        return fill

    # -- exit --------------------------------------------------------------
    def place_exit(self, intent: Intent) -> None:
        token = intent.token_id
        if not token:
            raise OrderError(f"no NO token id for exit {intent.city} {intent.temp}C")
        limit_px = self._grid_price(intent.limit_px, intent.tick_size)
        # post_only: the venue guarantees this can never cross as a taker
        # (spec §4 — the exit is maker-only, no taker fee, ever).
        resp = self._place_limit(token_id=token, price=limit_px,
                                 size=intent.size, side="SELL", post_only=True)
        order_id = getattr(resp, "order_id", None)
        if not order_id:
            raise OrderError("exit accepted but no order id returned")
        key = (intent.city, intent.temp)
        self._resting.setdefault(key, []).append(
            RestingOrder(order_id=str(order_id), size=intent.size, price=limit_px))
        self._pos(intent.city, intent.temp).resting_sell += intent.size

    def cancel_exit(self, city: str, temp: int) -> None:
        """Cancel every resting maker sell for one bracket (revert to hold-to-$1).

        Only the still-open (unmatched) size is retracted from ``resting_sell`` —
        anything already matched has left through ``on_book`` and reduced shares.
        A failed cancel raises :class:`OrderError` so the worker's per-city error
        path handles it (a canceled-order-not-found is treated as already gone).
        """

        key = (city, temp)
        orders = self._resting.get(key)
        if not orders:
            return
        pos = self._pos(city, temp)
        for ro in list(orders):
            open_size = max(0.0, ro.size - ro.matched)
            try:
                self._client.cancel_order(order_id=ro.order_id)
            except Exception as exc:
                raise OrderError(f"failed to cancel exit {ro.order_id}: {exc!r}")
            pos.resting_sell = max(0.0, pos.resting_sell - open_size)
            orders.remove(ro)
        if not orders:
            self._resting.pop(key, None)

    def on_book(self, snapshot: MarketSnapshot) -> list[Fill]:
        """Reconcile resting maker sells against the venue's order status."""

        fills: list[Fill] = []
        for b in snapshot.brackets:
            key = (snapshot.city, b.temp)
            orders = self._resting.get(key)
            if not orders:
                continue
            pos = self._pos(snapshot.city, b.temp)
            for ro in list(orders):
                matched = self._matched_size(ro.order_id)
                delta = matched - ro.matched
                if delta <= 0:
                    continue
                ro.matched = matched
                avg_cost = pos.avg_cost
                cost_released = avg_cost * delta
                fill = Fill(city=snapshot.city, temp=b.temp, side="sell",
                            price=ro.price, size=delta, fee=0.0,
                            reason="recycle@sell_px")
                pos.shares = max(0.0, pos.shares - delta)
                pos.cost_basis = max(0.0, pos.cost_basis - cost_released)
                pos.resting_sell = max(0.0, pos.resting_sell - delta)
                self._risk.record_sell_fill(fill, cost_released=cost_released)
                fills.append(fill)
                if ro.matched >= ro.size:
                    orders.remove(ro)
        return fills

    def _matched_size(self, order_id: str) -> float:
        try:
            o = self._client.get_order(order_id=order_id)
        except Exception as exc:  # status query failed; treat as no progress
            raise OrderError(f"get_order failed for {order_id}: {exc!r}")
        try:
            return float(getattr(o, "size_matched", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def settle(self, final_max: Optional[int] = None) -> list[Settlement]:
        """Day's end: the resolved market's positions redeem on-chain to USDC.

        Realised PnL is the venue/chain's job (not synthesised here), but the
        executor's local position + resting-order state MUST be cleared so the
        new day starts flat — otherwise yesterday's redeemed positions would
        linger (mis-reporting open volume and prompting stray exit re-posts).
        ``final_max`` marks the winning bracket so the reported (informational)
        PnL and ``won`` flag are honest for forecast positions; the actual
        redemption amount is decided on-chain either way.

        The position's exposure is released HERE. It used to be left for
        ``RiskManager.roll_day`` to wipe at one shared midnight, which dropped
        still-open positions in cities whose own day had not ended; settlement
        now happens at each city's own midnight and releases exactly what it
        settles. PnL is still NOT synthesised (``realise=False``) — redemption
        is on-chain and the wallet is the equity source in live.
        """

        results: list[Settlement] = []
        for (city, temp), pos in self._positions.items():
            if pos.shares > 0:
                won = final_max is None or temp != final_max
                payout = 1.0 if won else 0.0
                self._risk.record_settlement(city=city, temp=temp, shares=pos.shares,
                                             cost_basis=pos.cost_basis, payout=payout,
                                             realise=False)
                results.append(Settlement(city=city, temp=temp, shares=pos.shares,
                                          pnl=payout * pos.shares - pos.cost_basis,
                                          won=won))
            pos.shares = 0.0
            pos.cost_basis = 0.0
            pos.resting_sell = 0.0
        self._resting.clear()
        return results


class DisabledExecutor:
    """Data-collection-only executor: places no orders and holds no positions.

    Used to fence a single city out of trading (``--no-trade``) while the rest of
    the process trades live/paper. The worker still polls METAR, discovers the
    market, samples prices, and logs dead-snapshots/opportunities — none of that
    touches the executor — so full telemetry keeps flowing for the fenced city.
    Every order path is a no-op and it never calls into the shared ``RiskManager``,
    so it cannot consume the total-exposure cap or move realized PnL (i.e. it
    can't skew bankroll-pct sizing for the cities that *are* trading).
    """

    positions: dict[tuple[str, int], Position] = {}

    def execute_entry(self, intent: Intent, bracket: Bracket) -> Optional[Fill]:
        return None

    def place_exit(self, intent: Intent) -> None:
        return None

    def cancel_exit(self, city: str, temp: int) -> None:
        return None

    def on_book(self, snapshot: MarketSnapshot) -> list[Fill]:
        return []

    def settle(self, final_max: Optional[int] = None) -> list[Settlement]:
        return []


def make_executor(config: Config, risk: RiskManager, *, client: object = None,
                  trade_disabled: bool = False) -> Executor:
    """Factory: paper by default, live only when explicitly configured.

    ``trade_disabled`` fences the city out of trading entirely (data collection
    only), regardless of paper/live mode.
    """

    if trade_disabled:
        return DisabledExecutor()
    if config.mode == "live":
        return LiveExecutor(config, risk, client)
    return PaperExecutor(config, risk)
