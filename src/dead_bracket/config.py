"""Configuration objects.

All behaviour is config, not hard-coded (spec §10). Defaults below match the
spec exactly. ``Config.from_env`` lets any field be overridden by an environment
variable of the same (upper-case) name, which keeps deployment simple without a
config file format dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional


def _as_bool(raw: str) -> bool:
    """Env-var boolean: '1'/'true'/'yes'/'on' (any case) are True."""

    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class CityConfig:
    """A single tradeable city.

    Cities are pure config (spec §5): an id, the resolution-grade METAR station,
    the IANA timezone the daily high is measured in (the running max resets at
    local midnight), and the terms used to discover the daily event on Gamma.

    The optional ``feed_*`` fields describe a national-met-service sub-hourly
    feed for the *forecast* signal path (forecast.py) — signal-only data that
    never reaches a MetarFeed, a running max, or dead-bracket logic.

    NOTE: ``metar_icao`` must match the station the *market actually resolves on*.
    The stations below have been confirmed against the markets; if a market's
    resolution source ever changes, update the ICAO here (it is pure config).
    """

    id: str
    metar_icao: str
    tz: str  # IANA timezone name, e.g. "Europe/Paris" — daily-high reset boundary
    market_terms: tuple[str, ...]
    # Whether off-cycle SPECIs may set the running max that TRIGGERS trades. Most
    # markets resolve on a source that counts SPECIs (default True). The
    # weather.gov-resolving cities (Moscow/UUWW, Istanbul/LTFM, Tel Aviv/LLBG —
    # the WUNDERGROUND_EXEMPT set) resolve on the routine cycle only, so a SPECI
    # spike there could mark a still-live bracket dead -> a losing buy. For them
    # set this False: SPECIs are still fetched and LOGGED (data collection is
    # unaffected), they just never fold into the trade-triggering running max.
    include_speci: bool = True
    # When set, this market resolves off a Hong Kong Observatory automatic weather
    # station rather than the ICAO's NOAA METAR — e.g. Wunderground resolves
    # "Shenzhen" off HKO Lau Fau Shan (WMO 45035), a Deep-Bay coastal site that
    # runs 1-2C cooler at peak than the ZGSZ airport METAR. Reading HKO's official
    # open data directly matches that resolution source AND front-runs WU's publish
    # lag. Only USED when the run enables HKO sources (``--hko``); otherwise the
    # NOAA METAR is read as normal, so default behaviour is unchanged.
    resolution_hko_place: Optional[str] = None
    # --- forecast feed (signal-only; used only when the run passes --forecast).
    # Which sub-hourly source serves this city ("meteofrance" | "knmi" | "fmi" |
    # "jma" | "nea"), the source-native station id at/next to the METAR sensor,
    # and the publication cadence. Sources requiring an API key activate only
    # when the key is present (MF_API_KEY / KNMI_API_KEY).
    feed_source: Optional[str] = None
    feed_station_id: Optional[str] = None
    feed_cadence_sec: int = 600
    # UTC minutes-of-hour at which this station's routine METARs sample (drives
    # tau, the model's lead time to the next print). E.g. LFPB :00/:30 but
    # EHAM :25/:55 — the ":50 ob" IS the hourly for those stations.
    metar_minutes: tuple[int, ...] = (0, 30)
    # Calibrated feed-vs-METAR sensor noise (degC). None -> the inflated
    # config default until tools/forecast_calibrate.py measures it.
    sensor_sigma: Optional[float] = None


# Paris -> LFPB, London -> EGLC (spec §10), plus additional European cities.
# All (icao, tz) pairs are confirmed against the live markets; all of it is
# config and trivially overridable if a market's resolution source changes.
DEFAULT_CITIES: tuple[CityConfig, ...] = (
    # Forecast feeds (feed_* fields): Tier-1 sub-hourly sources verified
    # 2026-07-08 (docs/subhourly_feed_openings_2026-07-02.md + live probes).
    # Station ids: MF 95088001 = "LE BOURGET AEROPORT" (RADOME, is_minutely,
    # coords match LFPB); KNMI 06240 = Schiphol (UNVERIFIED until a
    # KNMI_API_KEY is available); FMI fmisid 100968 = Helsinki-Vantaa;
    # JMA AMeDAS 44166 = Haneda; NEA S24 sits at the Changi climate station
    # coordinates (sensor identity provisional until calibration accepts it).
    CityConfig(
        id="paris",
        metar_icao="LFPB",
        tz="Europe/Paris",
        market_terms=("paris",),
        feed_source="meteofrance",
        feed_station_id="95088001",
        feed_cadence_sec=360,
        metar_minutes=(0, 30),
    ),
    CityConfig(
        id="london", metar_icao="EGLC", tz="Europe/London", market_terms=("london",)
    ),
    # weather.gov-resolving (WUNDERGROUND_EXEMPT) cities resolve on the routine
    # cycle only -> SPECIs are collected but must not trigger trades.
    CityConfig(
        id="moscow",
        metar_icao="UUWW",
        tz="Europe/Moscow",
        market_terms=("moscow",),
        include_speci=False,
    ),
    CityConfig(
        id="milan", metar_icao="LIMC", tz="Europe/Rome", market_terms=("milan",)
    ),
    CityConfig(
        id="madrid", metar_icao="LEMD", tz="Europe/Madrid", market_terms=("madrid",)
    ),
    CityConfig(
        id="ankara", metar_icao="LTAC", tz="Europe/Istanbul", market_terms=("ankara",)
    ),
    CityConfig(
        id="istanbul",
        metar_icao="LTFM",
        tz="Europe/Istanbul",
        market_terms=("istanbul",),
        include_speci=False,
    ),  # weather.gov, routine-only (see moscow)
    CityConfig(
        id="helsinki",
        metar_icao="EFHK",
        tz="Europe/Helsinki",
        market_terms=("helsinki",),
        feed_source="fmi",
        feed_station_id="100968",
        feed_cadence_sec=600,
        metar_minutes=(20, 50),
    ),
    CityConfig(
        id="amsterdam",
        metar_icao="EHAM",
        tz="Europe/Amsterdam",
        market_terms=("amsterdam",),
        feed_source="knmi",
        feed_station_id="06240",
        feed_cadence_sec=600,
        metar_minutes=(25, 55),
    ),
    CityConfig(
        id="warsaw", metar_icao="EPWA", tz="Europe/Warsaw", market_terms=("warsaw",)
    ),
    CityConfig(
        id="munich", metar_icao="EDDM", tz="Europe/Berlin", market_terms=("munich",)
    ),
    # Additional cities (Asia / Mideast / Africa / Americas). Each (icao, tz) pair
    # was verified against authoritative ICAO references so the running max comes
    # from the airport the market actually resolves on — a wrong station here is a
    # false-dead/real-loss risk. None are SOURCE_CHECK_EXEMPT, so discovery still
    # requires the event text to name the ICAO + Weather Underground before trading.
    # market_terms are tokens that appear in the hyphenated ASCII slug/title; for
    # accented names (são paulo) the term avoids the accent so it still matches.
    CityConfig(id="seoul", metar_icao="RKSI", tz="Asia/Seoul", market_terms=("seoul",)),
    CityConfig(
        id="tokyo",
        metar_icao="RJTT",
        tz="Asia/Tokyo",
        market_terms=("tokyo",),
        feed_source="jma",
        feed_station_id="44166",
        feed_cadence_sec=600,
        metar_minutes=(0, 30),
    ),
    CityConfig(
        id="shanghai", metar_icao="ZSPD", tz="Asia/Shanghai", market_terms=("shanghai",)
    ),
    CityConfig(
        id="beijing", metar_icao="ZBAA", tz="Asia/Shanghai", market_terms=("beijing",)
    ),
    CityConfig(
        id="taipei", metar_icao="RCSS", tz="Asia/Taipei", market_terms=("taipei",)
    ),
    CityConfig(
        id="guangzhou",
        metar_icao="ZGGG",
        tz="Asia/Shanghai",
        market_terms=("guangzhou",),
    ),
    CityConfig(id="busan", metar_icao="RKPK", tz="Asia/Seoul", market_terms=("busan",)),
    CityConfig(
        id="singapore",
        metar_icao="WSSS",
        tz="Asia/Singapore",
        market_terms=("singapore",),
        feed_source="nea",
        feed_station_id="S24",
        feed_cadence_sec=60,
        metar_minutes=(0, 30),
    ),
    CityConfig(
        id="chongqing",
        metar_icao="ZUCK",
        tz="Asia/Shanghai",
        market_terms=("chongqing",),
    ),
    CityConfig(
        id="qingdao", metar_icao="ZSQD", tz="Asia/Shanghai", market_terms=("qingdao",)
    ),
    CityConfig(
        id="chengdu", metar_icao="ZUUU", tz="Asia/Shanghai", market_terms=("chengdu",)
    ),
    CityConfig(
        id="wellington",
        metar_icao="NZWN",
        tz="Pacific/Auckland",
        market_terms=("wellington",),
    ),
    CityConfig(
        id="kuala_lumpur",
        metar_icao="WMKK",
        tz="Asia/Kuala_Lumpur",
        market_terms=("kuala",),
    ),
    CityConfig(
        id="lucknow", metar_icao="VILK", tz="Asia/Kolkata", market_terms=("lucknow",)
    ),
    # WU resolves Shenzhen off HKO Lau Fau Shan (WMO 45035), not the ZGSZ airport
    # METAR — only read via HKO when the run passes --hko (else NOAA as normal).
    CityConfig(
        id="shenzhen",
        metar_icao="ZGSZ",
        tz="Asia/Shanghai",
        market_terms=("shenzhen",),
        resolution_hko_place="Lau Fau Shan",
    ),
    CityConfig(
        id="manila", metar_icao="RPLL", tz="Asia/Manila", market_terms=("manila",)
    ),
    CityConfig(
        id="wuhan", metar_icao="ZHHH", tz="Asia/Shanghai", market_terms=("wuhan",)
    ),
    CityConfig(
        id="karachi", metar_icao="OPKC", tz="Asia/Karachi", market_terms=("karachi",)
    ),
    CityConfig(
        id="jeddah", metar_icao="OEJN", tz="Asia/Riyadh", market_terms=("jeddah",)
    ),
    CityConfig(
        id="cape_town",
        metar_icao="FACT",
        tz="Africa/Johannesburg",
        market_terms=("cape town",),
    ),
    CityConfig(
        id="tel_aviv",
        metar_icao="LLBG",
        tz="Asia/Jerusalem",
        market_terms=("tel aviv",),
        include_speci=False,
    ),  # weather.gov, routine-only (see moscow)
    CityConfig(
        id="panama_city",
        metar_icao="MPMG",
        tz="America/Panama",
        market_terms=("panama",),
    ),
    CityConfig(
        id="sao_paulo",
        metar_icao="SBGR",
        tz="America/Sao_Paulo",
        market_terms=("paulo",),
    ),
    CityConfig(
        id="toronto", metar_icao="CYYZ", tz="America/Toronto", market_terms=("toronto",)
    ),
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
    # Anti-sweep cap: the most of a bracket's displayed depth (<= CAP) a single
    # take may lift. 1.0 = take the whole displayed book; lower leaves a buffer
    # so we never sweep the entire offer at once. Thin markets (Shenzhen) are
    # bankroll-limited, not liquidity-limited, so 1.0 deploys the available edge.
    max_frac_of_depth: float = 1.0
    max_daily_loss: float = 50.0
    # Capital recycling policy. A dead NO is certain to pay $1, so resting a
    # 99.9c maker sell only pays off when the 0.1c given up frees capital for a
    # NEW <=CAP opportunity worth more. When True (default), the bot HOLDS every
    # dead NO to $1 and posts a recycle sell only when a would-be buy this tick
    # was blocked purely by the bankroll caps (strategy.last_blocked_value > 0),
    # sized to cover just that shortfall; when the block clears the unfilled
    # recycle sells are cancelled (revert to hold). False restores the old
    # always-rest-after-every-buy behaviour.
    recycle_on_opportunity: bool = True
    # Live wallet-balance polling cadence (seconds). The balance tracker reads
    # the real USDC balance fast while a buy is being attempted, and cools the
    # cadence (doubling up to slow) once ``balance_poll_cool_after`` consecutive
    # reads are unchanged — no point hammering the API when nothing settles.
    # Paper mode uses exact synthetic accounting and never polls.
    balance_poll_fast_sec: float = 2.0
    balance_poll_slow_sec: float = 60.0
    balance_poll_cool_after: int = 3

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
    # --- Shenzhen / Lau Fau Shan calibration collectors (lfs_calibration.py).
    # Data collection + prediction shadow ONLY (no strategy/order-path effect);
    # they run for cities with ``resolution_hko_place`` set, i.e. Shenzhen, and
    # are off by default (enable with --calibrate or LFS_CALIBRATION=1).
    lfs_calibration: bool = False
    hko_1min_poll_sec: float = 60.0  # HKO latest-1-min CSV poll cadence
    twc_poll_sec: float = 60.0  # TWC timeseries row-version poll cadence
    twc_api_key: str = ""  # TWC_API_KEY (the public wunderground.com
    # frontend key works); empty -> HKO-only
    twc_location: str = "ZGSZ:9:CN"  # TWC location id resolving to obs 45035
    twc_daily_hkt: str = "00:30,09:00"  # daily-table capture times (HKT):
    # first full table + revision-window guard
    # --twc (L1 resolver feed): the CityWorker's running max reads CLOSED rows
    # from twc_rows.jsonl. If the TWC poller produces nothing new for this many
    # seconds the feed reports stale and the city halts (dead key / outage).
    twc_resolver_stale_sec: float = 5400.0
    # L2 lock-ahead shadow margin: an in-window HKO sample locks integer K only
    # when temp_c - K >= this (covers rounding/conversion noise; tighten after
    # the K2 rounding map is measured).
    lock_ahead_margin_c: float = 0.3
    # Best-guess K2 rule the prediction shadow runs (half_up | half_even |
    # floor). Its measured hit rate is direct evidence for/against the rule.
    lfs_predict_rounding: str = "half_up"
    # --- Forecast path (forecast.py): sub-hourly feed -> probabilistic NO-buys.
    # Off by default; --forecast (or FORECAST=1) enables the feed pollers +
    # signal engine. In PAPER mode forecast entries trade (simulated) from day
    # one; in LIVE mode they are logged-only unless forecast_live is ALSO set
    # (--forecast-live) — the go/no-go is a human decision informed by
    # tools/forecast_calibrate.py.
    forecast: bool = False
    forecast_live: bool = False
    # Fractional Kelly on the model's p-hat. Default quarter-Kelly (p is
    # estimated and the loss tail is fat); hard ceiling half-Kelly.
    kelly_fraction: float = 0.25
    # Required edge after fees: only buy when p - ask - fee(ask) >= this, and
    # never bid above p - fee - this (the margin between forecast and limit).
    forecast_min_edge: float = 0.03
    # Minimum tenths margin (reading - target integer) to count as a signal —
    # METAR rounds to whole degC, so sub-0.4 margins are coin flips.
    forecast_min_margin_c: float = 0.4
    forecast_p_cap: float = 0.99  # never trust the model above this (unconfirmed obs)
    forecast_resolver_factor: float = 0.995  # P(resolver ingests/keeps the ob)
    # Local-hour window with NO forecast entries: evening drift (-1 degC/h)
    # makes feed spikes the weakest signal exactly when asks look juiciest.
    forecast_evening_start: int = 17
    forecast_evening_end: int = 21
    # All brackets triggered by ONE feed ob share a failure mode = one bet:
    # cap their combined stake at this % of the current balance.
    forecast_same_ob_cap_pct: float = 25.0
    # Cross-city correlation cap: total open forecast exposure across all
    # cities (shared synoptic pattern) at this % of the current balance.
    forecast_daily_cap_pct: float = 40.0
    # A reading older than this multiple of the feed cadence is stale -> no
    # signals (the METAR/dead path continues unaffected).
    forecast_stale_factor: float = 2.0
    # Sensor-identity guard: this degC divergence on this many consecutive
    # feed-vs-METAR pairs disables the feed for the day.
    forecast_divergence_c: float = 1.0
    forecast_divergence_n: int = 2
    # Any settled LOSS on a forecast bet pauses the forecast path globally
    # (at p-hat >= 0.97 a loss is a model/mapping bug until proven otherwise).
    forecast_pause_on_loss: bool = True
    # Feed-vs-METAR sensor noise (degC) used until calibration measures a
    # per-city value (CityConfig.sensor_sigma). Deliberately inflated.
    forecast_sensor_sigma_default: float = 0.3
    mf_api_key: str = ""  # MF_API_KEY — Météo-France portail-api (paris)
    knmi_api_key: str = ""  # KNMI_API_KEY — KNMI Data Platform (amsterdam)
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
        if self.lfs_predict_rounding not in ("half_up", "half_even", "floor"):
            raise ValueError("lfs_predict_rounding must be half_up, half_even or floor")
        if not (0.0 < self.kelly_fraction <= 0.5):
            raise ValueError(
                "kelly_fraction must be in (0, 0.5] — half-Kelly is the ceiling"
            )
        if not (0.0 <= self.forecast_min_edge < 1.0):
            raise ValueError("forecast_min_edge must be in [0, 1)")
        if not (0.0 < self.forecast_p_cap <= 1.0):
            raise ValueError("forecast_p_cap must be in (0, 1]")
        if not (0 <= self.forecast_evening_start <= self.forecast_evening_end <= 24):
            raise ValueError(
                "forecast evening window must satisfy 0 <= start <= end <= 24"
            )
        if self.forecast_live and self.mode != "live":
            raise ValueError("forecast_live requires live mode")

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
            "recycle_on_opportunity": _as_bool,
            "balance_poll_fast_sec": float,
            "balance_poll_slow_sec": float,
            "balance_poll_cool_after": int,
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
            "lfs_calibration": _as_bool,
            "hko_1min_poll_sec": float,
            "twc_poll_sec": float,
            "twc_api_key": str,
            "twc_location": str,
            "twc_daily_hkt": str,
            "twc_resolver_stale_sec": float,
            "lock_ahead_margin_c": float,
            "lfs_predict_rounding": str,
            "forecast": _as_bool,
            "forecast_live": _as_bool,
            "kelly_fraction": float,
            "forecast_min_edge": float,
            "forecast_min_margin_c": float,
            "forecast_p_cap": float,
            "forecast_resolver_factor": float,
            "forecast_evening_start": int,
            "forecast_evening_end": int,
            "forecast_same_ob_cap_pct": float,
            "forecast_daily_cap_pct": float,
            "forecast_stale_factor": float,
            "forecast_divergence_c": float,
            "forecast_divergence_n": int,
            "forecast_pause_on_loss": _as_bool,
            "forecast_sensor_sigma_default": float,
            "mf_api_key": str,
            "knmi_api_key": str,
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
