"""Configuration objects.

All behaviour is config, not hard-coded (spec §10). Defaults below match the
spec exactly. ``Config.from_env`` lets any field be overridden by an environment
variable of the same (upper-case) name, which keeps deployment simple without a
config file format dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Iterable


@dataclass(frozen=True)
class CityConfig:
    """A single tradeable city.

    Cities are pure config (spec §5): an id, the resolution-grade METAR station,
    the IANA timezone the daily high is measured in (the running max resets at
    local midnight), and the terms used to discover the daily event on Gamma. No
    Meteo-France / 6-minute fields exist anywhere.

    NOTE: ``metar_icao`` must match the station the *market actually resolves on*.
    The stations below have been confirmed against the markets; if a market's
    resolution source ever changes, update the ICAO here (it is pure config).
    """

    id: str
    metar_icao: str
    tz: str  # IANA timezone name, e.g. "Europe/Paris" — daily-high reset boundary
    market_terms: tuple[str, ...]
    # Whether this market's resolution counts off-cycle SPECIs. Most do (default
    # True); Moscow resolves on the routine METAR cycle only, so it must exclude
    # SPECIs (a SPECI spike there could mark a still-live bracket dead).
    include_speci: bool = True


# Paris -> LFPB, London -> EGLC (spec §10), plus additional European cities.
# All (icao, tz) pairs are confirmed against the live markets; all of it is
# config and trivially overridable if a market's resolution source changes.
DEFAULT_CITIES: tuple[CityConfig, ...] = (
    CityConfig(id="paris", metar_icao="LFPB", tz="Europe/Paris", market_terms=("paris",)),
    CityConfig(id="london", metar_icao="EGLC", tz="Europe/London", market_terms=("london",)),
    CityConfig(id="moscow", metar_icao="UUWW", tz="Europe/Moscow", market_terms=("moscow",)),
    CityConfig(id="milan", metar_icao="LIMC", tz="Europe/Rome", market_terms=("milan",)),
    CityConfig(id="madrid", metar_icao="LEMD", tz="Europe/Madrid", market_terms=("madrid",)),
    CityConfig(id="ankara", metar_icao="LTAC", tz="Europe/Istanbul", market_terms=("ankara",)),
    CityConfig(id="istanbul", metar_icao="LTFM", tz="Europe/Istanbul", market_terms=("istanbul",)),
    CityConfig(id="helsinki", metar_icao="EFHK", tz="Europe/Helsinki", market_terms=("helsinki",)),
    CityConfig(id="amsterdam", metar_icao="EHAM", tz="Europe/Amsterdam", market_terms=("amsterdam",)),
    CityConfig(id="warsaw", metar_icao="EPWA", tz="Europe/Warsaw", market_terms=("warsaw",)),
    CityConfig(id="munich", metar_icao="EDDM", tz="Europe/Berlin", market_terms=("munich",)),
    # Additional cities (Asia / Mideast / Africa / Americas). Each (icao, tz) pair
    # was verified against authoritative ICAO references so the running max comes
    # from the airport the market actually resolves on — a wrong station here is a
    # false-dead/real-loss risk. None are SOURCE_CHECK_EXEMPT, so discovery still
    # requires the event text to name the ICAO + Weather Underground before trading.
    # market_terms are tokens that appear in the hyphenated ASCII slug/title; for
    # accented names (são paulo) the term avoids the accent so it still matches.
    CityConfig(id="seoul", metar_icao="RKSI", tz="Asia/Seoul", market_terms=("seoul",)),
    CityConfig(id="tokyo", metar_icao="RJTT", tz="Asia/Tokyo", market_terms=("tokyo",)),
    CityConfig(id="shanghai", metar_icao="ZSPD", tz="Asia/Shanghai", market_terms=("shanghai",)),
    CityConfig(id="beijing", metar_icao="ZBAA", tz="Asia/Shanghai", market_terms=("beijing",)),
    CityConfig(id="taipei", metar_icao="RCSS", tz="Asia/Taipei", market_terms=("taipei",)),
    CityConfig(id="guangzhou", metar_icao="ZGGG", tz="Asia/Shanghai", market_terms=("guangzhou",)),
    CityConfig(id="busan", metar_icao="RKPK", tz="Asia/Seoul", market_terms=("busan",)),
    CityConfig(id="singapore", metar_icao="WSSS", tz="Asia/Singapore", market_terms=("singapore",)),
    CityConfig(id="chongqing", metar_icao="ZUCK", tz="Asia/Shanghai", market_terms=("chongqing",)),
    CityConfig(id="qingdao", metar_icao="ZSQD", tz="Asia/Shanghai", market_terms=("qingdao",)),
    CityConfig(id="chengdu", metar_icao="ZUUU", tz="Asia/Shanghai", market_terms=("chengdu",)),
    CityConfig(id="wellington", metar_icao="NZWN", tz="Pacific/Auckland", market_terms=("wellington",)),
    CityConfig(id="kuala_lumpur", metar_icao="WMKK", tz="Asia/Kuala_Lumpur", market_terms=("kuala",)),
    CityConfig(id="lucknow", metar_icao="VILK", tz="Asia/Kolkata", market_terms=("lucknow",)),
    CityConfig(id="shenzhen", metar_icao="ZGSZ", tz="Asia/Shanghai", market_terms=("shenzhen",)),
    CityConfig(id="manila", metar_icao="RPLL", tz="Asia/Manila", market_terms=("manila",)),
    CityConfig(id="wuhan", metar_icao="ZHHH", tz="Asia/Shanghai", market_terms=("wuhan",)),
    CityConfig(id="karachi", metar_icao="OPKC", tz="Asia/Karachi", market_terms=("karachi",)),
    CityConfig(id="jeddah", metar_icao="OEJN", tz="Asia/Riyadh", market_terms=("jeddah",)),
    CityConfig(id="cape_town", metar_icao="FACT", tz="Africa/Johannesburg", market_terms=("cape town",)),
    CityConfig(id="tel_aviv", metar_icao="LLBG", tz="Asia/Jerusalem", market_terms=("tel aviv",)),
    CityConfig(id="panama_city", metar_icao="MPMG", tz="America/Panama", market_terms=("panama",)),
    CityConfig(id="sao_paulo", metar_icao="SBGR", tz="America/Sao_Paulo", market_terms=("paulo",)),
    CityConfig(id="toronto", metar_icao="CYYZ", tz="America/Toronto", market_terms=("toronto",)),
)


@dataclass(frozen=True)
class Config:
    """Global strategy / risk / execution configuration (spec §10)."""

    cities: tuple[CityConfig, ...] = DEFAULT_CITIES
    mode: str = "paper"  # "paper" | "live"

    cap: float = 0.98  # hard NO-buy ceiling
    min_net: float = 0.012  # min net margin after fee
    sell_px: float = 0.999  # resting maker exit

    fee_rate: float = 0.025
    fee_exponent: float = 0.5

    max_shares_per_bracket: int = 500
    max_exposure_per_bracket: float = 1000.0  # $ cap on a single bracket
    max_exposure_per_city: float = 500.0
    max_total_exposure: float = 1000.0
    # When > 0, the dollar exposure caps above are ignored and sizing is a
    # percentage of the *current* balance (starting balance + realised PnL)
    # instead: e.g. bankroll_pct=50 with a $30 balance caps a bet at $15, and as
    # wins grow the balance the cap grows with it. 0 keeps the static caps.
    bankroll_pct: float = 0.0
    max_frac_of_depth: float = 0.5
    max_daily_loss: float = 50.0

    stale_feed_min: int = 45
    max_order_errors: int = 3  # consecutive order errors before per-city pause
    # A per-city order-error pause is recoverable: once the venue is reachable
    # again (this many consecutive ticks maintain the resting sells without an
    # order error) the city resumes. Keeps a transient network blip — e.g. a
    # connection reset on an order-status poll — from sidelining a city all day.
    order_error_recover_ticks: int = 3
    # Crossed/invalid book handling (spec §9). In paper mode this is tolerant:
    # only brackets at/below the running max are policed, a crossed book must
    # persist this many consecutive ticks before the city is paused, and the
    # city auto-recovers after this many consecutive healthy ticks. In live mode
    # the check is strict (any crossed book trips the global sticky kill-switch).
    crossed_book_grace_ticks: int = 3
    crossed_book_recover_ticks: int = 3
    data_dir: str = "data"  # per-city subdir created at runtime

    # METAR polling cadence (seconds). Fetches are batched into ONE request for
    # all stations (SharedMetarFetcher), so frequent polling is cheap — kept low
    # to catch off-cycle SPECI reports (which publish at irregular times) within
    # ~a minute, while the shared fetcher only hits the network once per cycle.
    metar_poll_sec: float = 60.0
    # Aggressive cadence inside a "hot window" — from a :00/:30 slot until that
    # slot's report actually lands (empirically 100-400s later) — to grab a new
    # high (and the brief opportunity it opens) as fast as the API allows.
    metar_hot_poll_sec: float = 1.0
    # The hot window arms this many minutes after the last observation (reports
    # are half-hourly at station-specific minutes; 28 = a 2-min early lead).
    metar_expected_gap_min: float = 28.0
    # Market book polling cadence (seconds).
    market_poll_sec: float = 1.0
    # Frontier band for book reads: only fetch the brackets near the running max,
    # not every bracket. ``book_band_below`` dead brackets below the max (1 = just
    # the freshly-dead one, the only place the dead-NO edge ever appears) and
    # ``book_band_above`` live brackets above it (to watch it approach). This is
    # what stops ~33 cities x ~9 brackets/sec from tripping the CLOB rate limit;
    # the effective above-band is widened to cover ``price_sample_band`` so the
    # price sampler still sees its frontier rows. Held positions are always read.
    book_band_below: int = 1
    book_band_above: int = 2
    # Windowed price sampling. Inside a publication hot window ONLY (the minutes
    # before each station's :00/:30-style report lands), record the frontier
    # brackets' NO prices at most once per this many seconds — sparse vs the 1s
    # market cadence so the file stays small. Lets you watch the market reprice a
    # soon-to-be-dead bracket toward $1 BEFORE our METAR confirms the new high and
    # measure how late our feed is. 0 disables it.
    price_sample_sec: float = 15.0
    # How many brackets at/above the running max to sample each time — the frontier
    # band most likely to flip dead next (running_max .. running_max+band). Bigger
    # = more rows on disk; 0 samples only the single frontier bracket.
    price_sample_band: int = 2
    # Periodic per-city status line cadence on stderr (seconds).
    status_every_sec: float = 300.0
    # Daily rollover: re-arm risk and settle positions at this tz's midnight so
    # the bot runs unattended across days. daily_rearm=False disables it.
    rollover_tz: str = "UTC"
    daily_rearm: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {self.mode!r}")
        if not (0.0 < self.cap < 1.0):
            raise ValueError("cap must be in (0, 1)")
        if not (0.0 < self.sell_px <= 1.0):
            raise ValueError("sell_px must be in (0, 1]")
        if self.sell_px <= self.cap:
            raise ValueError("sell_px must be above cap to be a maker exit")
        if not (0.0 < self.max_frac_of_depth <= 1.0):
            raise ValueError("max_frac_of_depth must be in (0, 1]")
        if not (0.0 <= self.bankroll_pct <= 100.0):
            raise ValueError("bankroll_pct must be in [0, 100]")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        """Build a config, overriding scalar fields from environment variables.

        Only the simple scalar fields are overridable from env (the common
        operational knobs). ``cities`` is left at the default unless overridden
        programmatically.
        """

        src = os.environ if env is None else env
        overrides: dict[str, object] = {}
        casters = {
            "mode": str,
            "cap": float,
            "min_net": float,
            "sell_px": float,
            "fee_rate": float,
            "fee_exponent": float,
            "max_shares_per_bracket": int,
            "max_exposure_per_bracket": float,
            "max_exposure_per_city": float,
            "max_total_exposure": float,
            "bankroll_pct": float,
            "max_frac_of_depth": float,
            "max_daily_loss": float,
            "stale_feed_min": int,
            "max_order_errors": int,
            "order_error_recover_ticks": int,
            "crossed_book_grace_ticks": int,
            "crossed_book_recover_ticks": int,
            "data_dir": str,
            "metar_poll_sec": float,
            "metar_hot_poll_sec": float,
            "metar_expected_gap_min": float,
            "market_poll_sec": float,
            "book_band_below": int,
            "book_band_above": int,
            "price_sample_sec": float,
            "price_sample_band": int,
            "status_every_sec": float,
            "rollover_tz": str,
        }
        for name, cast in casters.items():
            raw = src.get(name.upper())
            if raw is not None and raw != "":
                overrides[name] = cast(raw)
        return cls(**overrides)  # type: ignore[arg-type]

    def with_mode(self, mode: str) -> "Config":
        return replace(self, mode=mode)


def select_cities(config: Config, ids: Iterable[str]) -> tuple[CityConfig, ...]:
    """Return the CityConfig objects whose id is in ``ids`` (order preserved)."""

    wanted = list(ids)
    by_id = {c.id: c for c in config.cities}
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise KeyError(f"unknown city ids: {missing}")
    return tuple(by_id[i] for i in wanted)
