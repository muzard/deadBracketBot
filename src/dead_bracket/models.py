"""Plain data models shared across feeds, strategy, risk, and execution.

These are deliberately simple, hashable-where-useful dataclasses with no
behaviour beyond light derived properties, so they are trivial to construct in
tests and fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# An order-book level: (price, size). Price in dollars (0..1), size in shares.
Level = tuple[float, float]


def max_price_for_tick(tick: float) -> float:
    """Highest placeable price for a given tick size (Polymarket: 1 - tick).

    Polymarket bounds order prices to [tick, 1 - tick]. Tick is dynamic per
    market (0.01 early, refining to 0.001 near the extremes), so the maximum a
    maker sell can rest at moves with it: 0.99 at 0.01-tick, 0.999 at 0.001-tick.
    """

    return round(1.0 - tick, 6)


def round_to_tick(price: float, tick: float) -> float:
    """Round a price to the market's tick grid (nearest valid increment)."""

    if tick <= 0:
        return price
    steps = round(price / tick)
    decimals = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[-1]))
    return round(steps * tick, decimals)


@dataclass(frozen=True)
class Bracket:
    """A single integer-degC bracket's NO side of the book (spec §6).

    Only the NO token matters to this strategy: we buy NO asks and rest a NO
    sell. ``no_asks`` / ``no_bids`` are full depth, sorted best-first by the
    feed (asks ascending, bids descending). ``no_best_ask`` / ``no_best_bid``
    are conveniences derived from depth.
    """

    temp: int
    no_asks: tuple[Level, ...] = ()
    no_bids: tuple[Level, ...] = ()
    fees_enabled: bool = True
    token_id: Optional[str] = None  # CLOB token id for the NO side (live mode)
    neg_risk: bool = False  # negRisk market -> orders sign for the negRisk exchange
    tick_size: float = 0.001  # current price tick (dynamic per market)
    min_order_size: float = 0.0  # venue minimum order size in shares
    # True for the open-ended top bracket ("T°C or higher"): it WINS once the
    # day's high reaches T, so an over-the-top reading must never flag it dead.
    # The symmetric "or below" bottom bracket needs no flag — it dies correctly
    # once the running max passes it.
    upper_open: bool = False

    @property
    def no_best_ask(self) -> Optional[float]:
        return self.no_asks[0][0] if self.no_asks else None

    @property
    def no_best_bid(self) -> Optional[float]:
        return self.no_bids[0][0] if self.no_bids else None

    def depth_at_or_below(self, cap: float) -> float:
        """Total ask size available at price <= cap (spec §3 sizing)."""

        return sum(sz for (px, sz) in self.no_asks if px <= cap)

    def is_crossed_or_invalid(self) -> bool:
        """True if the NO book is crossed or out of the [0, 1] range.

        Used by the kill-switch (spec §9): NO ask > 1, or ask < bid.
        """

        ask = self.no_best_ask
        bid = self.no_best_bid
        if ask is not None and (ask > 1.0 or ask < 0.0):
            return True
        if bid is not None and (bid > 1.0 or bid < 0.0):
            return True
        if ask is not None and bid is not None and ask < bid:
            return True
        return False


@dataclass(frozen=True)
class MarketSnapshot:
    """A point-in-time view of one city's market: its brackets (spec §6)."""

    city: str
    brackets: tuple[Bracket, ...]
    event_slug: Optional[str] = None

    def bracket(self, temp: int) -> Optional[Bracket]:
        for b in self.brackets:
            if b.temp == temp:
                return b
        return None


@dataclass(frozen=True)
class Intent:
    """An order the strategy wants placed.

    ``side`` is "buy" or "sell" of the NO token. Entry is an IOC taker buy at
    CAP; exit is a GTC maker sell at SELL_PX.
    """

    city: str
    temp: int
    side: str  # "buy" | "sell"
    limit_px: float
    size: float
    tif: str  # "IOC" | "GTC"
    reason: str
    token_id: Optional[str] = None
    neg_risk: bool = False  # carried to the live order so it signs for the right exchange
    tick_size: float = 0.001

    @property
    def is_maker_exit(self) -> bool:
        return self.side == "sell" and self.tif == "GTC"


@dataclass(frozen=True)
class Fill:
    """A (possibly partial) fill of an intent."""

    city: str
    temp: int
    side: str  # "buy" | "sell"
    price: float  # average fill price
    size: float
    fee: float  # total fee paid on this fill (0 for makers)
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class Position:
    """Net long-NO position for one bracket, with its resting exit.

    ``shares`` is the quantity still held (not yet sold). ``cost_basis`` is the
    total dollars paid for the currently-held shares including entry fees, used
    for settlement PnL. ``resting_sell`` is the size of the live GTC maker sell;
    ``resting_px`` is the price it actually rests at (the configured SELL_PX
    clamped to the market's tick grid — e.g. 0.99 on a 0.01-tick market).
    """

    city: str
    temp: int
    shares: float = 0.0
    cost_basis: float = 0.0
    resting_sell: float = 0.0
    resting_px: float = 0.0

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares else 0.0
