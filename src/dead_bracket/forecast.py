"""Forecast layer: sub-hourly feed readings -> probabilistic NO-side entries.

A national-met-service reading T-tilde (tenths of a degC, from the same or a
co-located sensor as the resolution METAR) taken tau minutes before the next
METAR sampling instant predicts that METAR will print >= T. Every bracket
strictly below T is then *about to be* dead. This module turns such a reading
into fractional-Kelly-sized IOC NO-buy Intents with an explicit win
probability p-hat < 1 — unlike the dead-bracket path, these bets can lose.

The probability model is the trust-decay formula fitted on 13,300 obs pairs
(docs/trust_decay_formula_2026-07-02.md): sigma scales as tau^H (temperature
structure function), a diurnal drift term, and a "rescue" factor for later
same-day obs. Feeds are SIGNAL-ONLY: nothing here ever touches a MetarFeed,
a RunningMax, dead-bracket logic, or market discovery.

Pure decision logic: no I/O, no network, no threads (mirrors strategy.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from .config import CityConfig, Config
from .fees import fee as fee_fn
from .models import Intent, MarketSnapshot, max_price_for_tick

if TYPE_CHECKING:  # avoid import cost / cycles at runtime
    from .risk import RiskManager


@dataclass(frozen=True)
class FeedReading:
    """One sub-hourly feed observation (tenths of a degC, UTC-timestamped)."""

    src: str  # feed source id, e.g. "fmi" / "jma" / "nea" / "meteofrance" / "knmi"
    station: str  # source-native station id, e.g. "100968" / "44166" / "S24"
    obs_time: datetime  # observation validity time (aware, UTC)
    temp_c: float  # air temperature in degC, tenths precision
    ts_recv: datetime  # when we received it (aware, UTC) — latency = ts_recv - obs_time


# Fitted trust-decay parameters per local-hour bucket (docs/trust_decay_formula):
# bucket -> (sigma30 degC, Hurst H, diurnal drift degC/h, rescue rho).
PAR: dict[str, tuple[float, float, float, float]] = {
    "morn": (0.61, 0.60, 1.15, 0.80),
    "mid": (0.73, 0.46, 0.33, 0.63),
    "eve": (0.72, 0.66, -0.97, 0.20),
    "night": (0.76, 0.75, -0.22, 0.30),
}


def bucket_for_hour(hour_local: int) -> str:
    if 8 <= hour_local < 12:
        return "morn"
    if 12 <= hour_local < 17:
        return "mid"
    if 17 <= hour_local < 21:
        return "eve"
    return "night"


def p_trust(tau_min: float, margin_c: float, hour_local: int, *,
            sensor_sigma: float = 0.0, resolver: float = 0.995) -> float:
    """P(the market's resolution source records >= T today | reading = T + margin).

    ``tau_min`` is the lead to the next METAR sampling instant, ``margin_c`` the
    tenths margin above the integer target T. The +0.5 is the METAR
    round-to-nearest buffer; ``sensor_sigma`` widens sigma for co-located but
    not identical sensors; ``resolver`` is the resolver-ingestion factor
    (P(the resolver actually displays the ob), ~0.995).
    """

    tau = max(1.0, tau_min)  # clamp: tau -> 0 would collapse sigma to 0
    sigma30, hurst, drift, rescue = PAR[bucket_for_hour(hour_local)]
    sig = math.sqrt((sigma30 * (tau / 30.0) ** hurst) ** 2 + sensor_sigma ** 2)
    z = (margin_c + 0.5 + drift * tau / 60.0) / sig
    p_next = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    p_hold = 1.0 - (1.0 - p_next) * (1.0 - rescue)
    return p_hold * resolver


def minutes_to_next_metar(now_utc: datetime, metar_minutes: tuple[int, ...]) -> float:
    """Minutes until the next scheduled METAR sampling instant (UTC grid).

    METAR observation minutes are UTC-based station constants (e.g. :00/:30 at
    LFPB, :25/:55 at EHAM). Returns a value in (0, 60].
    """

    best: Optional[float] = None
    for m in metar_minutes:
        candidate = now_utc.replace(minute=m, second=0, microsecond=0)
        if candidate <= now_utc:
            candidate += timedelta(hours=1)
        delta = (candidate - now_utc).total_seconds() / 60.0
        if best is None or delta < best:
            best = delta
    return best if best is not None else 30.0


def kelly_stake(p: float, price: float) -> float:
    """Full-Kelly bankroll fraction for buying at ``price`` with win prob ``p``:
    f* = p - (1-p) * price/(1-price). Negative when there is no edge."""

    if price >= 1.0:
        return 0.0
    return p - (1.0 - p) * price / (1.0 - price)


def floor_to_tick(price: float, tick: float) -> float:
    """Round DOWN to the market's tick grid (entry prices must never round up
    past the model's limit)."""

    if tick <= 0:
        return price
    steps = math.floor(price / tick + 1e-9)
    decimals = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[-1]))
    return round(steps * tick, decimals)


class FeedMetarGuard:
    """Sensor-identity kill switch: feed and METAR must agree.

    Pairs each new METAR ob with the closest-in-time feed reading (within
    ``pair_window_min``); ``needed_n`` consecutive pairs diverging by more than
    ``divergence_c`` degC disable the feed for the rest of the day (a drifting /
    misidentified sensor would otherwise feed the model garbage margins).
    """

    def __init__(self, *, divergence_c: float, needed_n: int,
                 pair_window_min: float = 12.0) -> None:
        self._divergence_c = divergence_c
        self._needed_n = needed_n
        self._pair_window = timedelta(minutes=pair_window_min)
        self._readings: list[FeedReading] = []  # recent, bounded
        self._streak = 0
        self.tripped = False
        self.last_pair: Optional[tuple[float, int]] = None  # (feed degC, metar degC)

    def on_reading(self, reading: FeedReading) -> None:
        self._readings.append(reading)
        if len(self._readings) > 240:  # bound memory (~4h of 1-min readings)
            del self._readings[: len(self._readings) - 240]

    def on_metar(self, obs_time: datetime, temp: int) -> None:
        best: Optional[FeedReading] = None
        best_gap: Optional[timedelta] = None
        for r in self._readings:
            gap = abs(r.obs_time - obs_time)
            if gap <= self._pair_window and (best_gap is None or gap < best_gap):
                best, best_gap = r, gap
        if best is None:
            return  # no comparable reading — not evidence either way
        self.last_pair = (best.temp_c, temp)
        if abs(best.temp_c - temp) > self._divergence_c:
            self._streak += 1
            if self._streak >= self._needed_n:
                self.tripped = True
        else:
            self._streak = 0

    def roll_day(self) -> None:
        self._readings.clear()
        self._streak = 0
        self.tripped = False
        self.last_pair = None


class ForecastStrategy:
    """Turns the freshest feed reading into Kelly-sized NO-buy Intents.

    Sibling of :class:`strategy.Strategy` with the same injection style; the
    worker runs the dead-bracket path FIRST and passes its acted temps in, so
    a forecast entry can never double-buy a bracket the certainty path just
    took. Emits ``reason="fcast ..."`` — the risk manager keys its forecast
    exposure cap off that prefix.
    """

    def __init__(self, city: str, config: Config, city_cfg: CityConfig,
                 risk: "RiskManager") -> None:
        self.city = city
        self._cfg = config
        self._city_cfg = city_cfg
        self._risk = risk
        self._tz = ZoneInfo(city_cfg.tz)
        self.guard = FeedMetarGuard(divergence_c=config.forecast_divergence_c,
                                    needed_n=config.forecast_divergence_n)
        self._fired: set[tuple[datetime, int]] = set()  # (feed ob, bracket) one-shot
        self._logged: set[tuple[datetime, int]] = set()  # signal rows logged once
        self._seen_obs: set[tuple[datetime, float]] = set()  # readings fed to guard
        self._ob_spent: dict[datetime, float] = {}  # $ staked per feed ob (same-ob cap)
        self.stale = False  # worker logs the stale/fresh transition
        # Signal rows produced by the last evaluate() for signals.jsonl.
        self.last_signals: list[dict] = []

    # -- hooks from the worker ----------------------------------------------
    def note_metar(self, obs_time: datetime, temp: int) -> None:
        """Feed a new (deduped) resolution METAR ob to the divergence guard."""

        self.guard.on_metar(obs_time, temp)

    def roll_day(self) -> None:
        """New local trading day: clear one-shot memory and re-arm the guard."""

        self._fired.clear()
        self._logged.clear()
        self._seen_obs.clear()
        self._ob_spent.clear()
        self.guard.roll_day()

    # -- helpers -------------------------------------------------------------
    def _sensor_sigma(self) -> float:
        if self._city_cfg.sensor_sigma is not None:
            return self._city_cfg.sensor_sigma
        return self._cfg.forecast_sensor_sigma_default

    def _is_stale(self, reading: FeedReading, now_utc: datetime) -> bool:
        age = (now_utc - reading.ts_recv).total_seconds()
        return age > self._cfg.forecast_stale_factor * self._city_cfg.feed_cadence_sec

    def _same_ob_remaining(self, obs_time: datetime) -> float:
        cap = self._cfg.forecast_same_ob_cap_pct / 100.0 * self._risk.current_balance
        return max(0.0, cap - self._ob_spent.get(obs_time, 0.0))

    # -- core ----------------------------------------------------------------
    def evaluate(self, snapshot: MarketSnapshot, running_max: Optional[int],
                 reading: Optional[FeedReading], now_utc: datetime,
                 *, skip_temps: frozenset[int] = frozenset()) -> list[Intent]:
        """Return IOC NO-buy Intents for brackets the reading predicts dead.

        ``skip_temps`` are brackets the dead-bracket path already acted on this
        tick. Populates ``last_signals`` with one row per (feed ob, bracket)
        decision for signals.jsonl (skips logged once, takes always).
        """

        cfg = self._cfg
        self.last_signals = []
        if reading is None:
            return []

        # Track staleness on every call so the worker can log transitions.
        self.stale = self._is_stale(reading, now_utc)
        if self.stale:
            return []
        # Feed each distinct reading to the divergence guard exactly once.
        rkey = (reading.obs_time, reading.temp_c)
        if rkey not in self._seen_obs:
            self._seen_obs.add(rkey)
            if len(self._seen_obs) > 4096:  # bound (cleared daily anyway)
                self._seen_obs.clear()
                self._seen_obs.add(rkey)
            self.guard.on_reading(reading)

        if self.guard.tripped or self._risk.forecast_paused:
            return []
        if self._risk.is_city_blocked(self.city):
            return []
        hour_local = now_utc.astimezone(self._tz).hour
        if cfg.forecast_evening_start <= hour_local < cfg.forecast_evening_end:
            return []  # evening drift (-1 degC/h) kills the edge; hard cutoff

        tau = minutes_to_next_metar(now_utc, self._city_cfg.metar_minutes)
        sensor_sigma = self._sensor_sigma()
        city_remaining = self._risk.per_city_cap_remaining(self.city)
        total_remaining = self._risk.total_cap_remaining()
        forecast_remaining = self._risk.forecast_cap_remaining()  # $ across cities
        same_ob_remaining = self._same_ob_remaining(reading.obs_time)  # $

        intents: list[Intent] = []
        for b in snapshot.brackets:
            if b.upper_open:
                continue  # the top bracket is never dead — no NO-buy, ever
            if running_max is not None and b.temp < running_max:
                continue  # already dead: the certainty path owns it
            if b.temp in skip_temps:
                continue  # dead path acted on it this very tick
            key = (reading.obs_time, b.temp)
            if key in self._fired:
                continue  # one shot per (feed ob, bracket)
            if b.is_crossed_or_invalid():
                continue
            ask = b.no_best_ask
            if ask is None:
                continue

            target = b.temp + 1  # the METAR integer that kills bracket b
            margin = reading.temp_c - target
            if margin < cfg.forecast_min_margin_c:
                continue  # METAR rounding / retreat risk: not a signal

            p = p_trust(tau, margin, hour_local, sensor_sigma=sensor_sigma,
                        resolver=cfg.forecast_resolver_factor)
            p = min(p, cfg.forecast_p_cap)

            entry_fee = fee_fn(ask, fee_rate=cfg.fee_rate, fee_exponent=cfg.fee_exponent,
                               fees_enabled=b.fees_enabled)
            # The user-required margin between forecast and limit price: never
            # bid above fair value minus fees minus the configured edge.
            raw_limit = p - entry_fee - cfg.forecast_min_edge
            limit_px = floor_to_tick(min(raw_limit, cfg.cap, max_price_for_tick(b.tick_size)),
                                     b.tick_size)

            sig_row = {"temp": b.temp, "reading": reading.temp_c,
                       "obs_time": reading.obs_time.isoformat(), "src": reading.src,
                       "margin": round(margin, 2), "tau_min": round(tau, 1),
                       "hour_local": hour_local, "p": round(p, 4), "ask": ask,
                       "limit_px": limit_px, "fee": round(entry_fee, 4)}

            if limit_px <= 0 or ask > limit_px:
                self._log_once(key, {**sig_row, "taken": False, "why": "price"})
                continue

            f_star = kelly_stake(p, limit_px)  # sized at the worst price we pay
            if f_star <= 0:
                self._log_once(key, {**sig_row, "taken": False, "why": "no_edge"})
                continue
            stake = cfg.kelly_fraction * f_star * self._risk.current_balance
            stake = min(stake, same_ob_remaining, forecast_remaining)

            avail = b.depth_at_or_below(limit_px)
            bracket_remaining = self._risk.per_bracket_cap_remaining(self.city, b.temp)
            want = math.floor(min(stake / limit_px, avail * cfg.max_frac_of_depth,
                                  bracket_remaining, max(0.0, city_remaining),
                                  max(0.0, total_remaining)))
            if want <= 0 or (b.min_order_size and want < b.min_order_size):
                self._log_once(key, {**sig_row, "taken": False, "why": "size/caps",
                                     "kelly_f": round(f_star, 4)})
                continue

            intents.append(Intent(
                city=self.city,
                temp=b.temp,
                side="buy",
                limit_px=limit_px,
                size=float(want),
                tif="IOC",
                reason=f"fcast p={p:.3f} m={margin:.1f} tau={tau:.0f}",
                token_id=b.token_id,
                neg_risk=b.neg_risk,
                tick_size=b.tick_size,
            ))
            self._fired.add(key)
            self.last_signals.append({**sig_row, "taken": True,
                                      "kelly_f": round(f_star, 4), "shares": want})
            # Provisional in-pass deduction so one reading can't over-commit.
            spent = want * limit_px
            self._ob_spent[reading.obs_time] = self._ob_spent.get(reading.obs_time, 0.0) + spent
            same_ob_remaining -= spent
            forecast_remaining -= spent
            city_remaining -= want
            total_remaining -= want

        return intents

    def _log_once(self, key: tuple[datetime, int], row: dict) -> None:
        """Queue a skipped-signal row the first time this (ob, bracket) skips —
        asks move every tick and we must not spam signals.jsonl at 1 Hz."""

        if key in self._logged:
            return
        self._logged.add(key)
        self.last_signals.append(row)
