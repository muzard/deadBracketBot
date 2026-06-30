"""Strategy: entry rule (spec §3) and exit rule (spec §4).

Pure decision logic. ``evaluate`` turns a running-max + market snapshot into a
list of taker buy Intents; ``make_exit_intent`` turns a buy fill into the resting
maker sell Intent. No I/O, no network, no threads — all of which keeps this
module (the strategy-critical core) fully unit-testable.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Optional

from .config import Config
from .fees import net
from .models import Fill, Intent, MarketSnapshot, max_price_for_tick, round_to_tick

if TYPE_CHECKING:  # avoid import cost / cycles at runtime
    from .risk import RiskManager


def is_dead(temp: int, running_max: Optional[int], *, upper_open: bool = False) -> bool:
    """A bracket is dead iff its temp is strictly below the running max.

    The running max here is always the resolution-grade METAR max (spec §9): a
    higher non-resolution reading can never reach this function, so it can never
    mark a bracket dead.

    The open-ended top bracket ("T°C or higher", ``upper_open=True``) is exempt:
    it is the WINNER once the high reaches T, so it is never dead regardless of
    the running max. This guards against an over-the-top reading (running_max >
    T) false-flagging the top bracket dead and buying a NO that settles to $0.
    """

    if upper_open:
        return False
    return running_max is not None and temp < running_max


class Strategy:
    def __init__(self, city: str, config: Config, risk: "RiskManager") -> None:
        self.city = city
        self._cfg = config
        self._risk = risk
        # $ of liftable dead-NO blocked by the bankroll caps on the last evaluate.
        self.last_blocked_value = 0.0

    def evaluate(self, snapshot: MarketSnapshot, running_max: Optional[int]) -> list[Intent]:
        """Return taker-buy Intents for every dead bracket whose NO is cheap.

        Implements the authoritative entry rule in spec §3. Caps consumed by
        earlier intents in the same pass are deducted provisionally so a single
        tick cannot over-commit the per-city / total budget.
        """

        cfg = self._cfg
        if running_max is None:
            return []
        # Downward-revision guard / kill-switch: no new entries when blocked.
        if self._risk.is_city_blocked(self.city):
            return []

        city_remaining = self._risk.per_city_cap_remaining(self.city)
        total_remaining = self._risk.total_cap_remaining()

        # $ of dead-NO volume that was liftable (within depth / don't-sweep /
        # per-bracket limits) but blocked purely by the bankroll exposure caps
        # this pass — i.e. how much more we'd deploy with a bigger bankroll.
        self.last_blocked_value = 0.0

        intents: list[Intent] = []
        for b in snapshot.brackets:
            if not is_dead(b.temp, running_max, upper_open=b.upper_open):  # b.temp >= rmax -> not dead
                continue
            if b.is_crossed_or_invalid():  # never act on a garbage book
                continue
            a = b.no_best_ask
            if a is None or a >= cfg.cap:
                continue
            if net(
                a,
                fee_rate=cfg.fee_rate,
                fee_exponent=cfg.fee_exponent,
                fees_enabled=b.fees_enabled,
            ) < cfg.min_net:
                continue

            avail = b.depth_at_or_below(cfg.cap)
            bracket_remaining = self._risk.per_bracket_cap_remaining(self.city, b.temp)
            # What we'd take ignoring the bankroll caps (depth, don't-sweep,
            # per-bracket share cap) vs what the bankroll caps actually allow.
            willing = math.floor(min(avail, bracket_remaining, int(cfg.max_frac_of_depth * avail)))
            if willing <= 0:
                continue
            want = math.floor(min(willing, max(0.0, city_remaining), max(0.0, total_remaining)))
            # Volume the bankroll blocked, valued at the ask.
            self.last_blocked_value += max(0, willing - want) * a
            if want <= 0:
                continue  # fully bankroll-blocked: counted above, no order placed
            # Respect the venue minimum order size — a smaller order is rejected.
            if b.min_order_size and want < b.min_order_size:
                continue

            # Buy limit on the market's tick grid, never above CAP or the tick max.
            limit_px = min(round_to_tick(cfg.cap, b.tick_size), max_price_for_tick(b.tick_size))
            intents.append(
                Intent(
                    city=self.city,
                    temp=b.temp,
                    side="buy",
                    limit_px=limit_px,
                    size=float(want),
                    tif="IOC",
                    reason="dead<rmax",
                    token_id=b.token_id,
                    neg_risk=b.neg_risk,
                    tick_size=b.tick_size,
                )
            )
            # Provisionally consume shared budget for the rest of this pass. We do
            # NOT break when exhausted — remaining dead brackets still tally their
            # bankroll-blocked volume into last_blocked_value.
            city_remaining -= want
            total_remaining -= want

        return intents

    def make_exit_intent(self, fill: Fill, *, token_id: Optional[str] = None,
                         tick_size: float = 0.001, neg_risk: bool = False) -> Intent:
        """Build the resting GTC maker SELL for a buy fill (spec §4).

        One exit per buy fill, for exactly the filled size. The caller always
        passes tick_size=0.001 (the CLOB's fine tick near $1), so the price is
        effectively always SELL_PX (0.999). Because it rests above the bid it
        is a maker order (no taker fee); the executor keeps any unfilled
        remainder resting and never taker-sells.
        """

        limit_px = round_to_tick(min(self._cfg.sell_px, max_price_for_tick(tick_size)), tick_size)
        return Intent(
            city=fill.city,
            temp=fill.temp,
            side="sell",
            limit_px=limit_px,
            size=fill.size,
            tif="GTC",
            reason="recycle@sell_px",
            token_id=token_id,
            neg_risk=neg_risk,
            tick_size=tick_size,
        )
