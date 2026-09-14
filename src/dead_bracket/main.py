"""CLI entry point (spec §5, §7, §9).

Builds the shared RiskManager + Reporter, wires per-city feeds / strategy /
executor / worker, and runs them concurrently under the Supervisor. Default mode
is paper; ``--live`` is gated on a non-zero bankroll percentage and a wallet
(spec §9). The wallet key is read only here->executor via the environment and is
never logged.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional
from zoneinfo import ZoneInfo

from .balance import LiveBalanceTracker, PaperBalanceTracker
from .config import Config, select_cities
from .executor import PaperExecutor, make_executor
from .feeds_http import (
    GammaMarketProvider,
    HkoStationFetcher,
    SharedMetarFetcher,
    TwcResolverFetcher,
    build_feed_fetcher,
    make_hko_1min_fetch,
    fetch_metar_history,
    make_metar_fetch,
    make_tgftp_fetch,
    make_twc_historical_fetch,
    make_synoptic_range_fetch,
    make_twc_range_fetch,
    make_twc_timeseries_fetch,
)
from .forecast import ForecastStrategy
from .forecast_feeds import FeedCollector, FeedPollerWorker
from .lfs_calibration import (
    Hko1MinCollector,
    LfsCalibrationWorker,
    LockAheadShadow,
    TwcDailyCollector,
    TwcRowCollector,
    WuPredictor,
)
from .market import MarketFeed
from .metar import MetarFeed
from .reporter import Reporter
from .resolver_guard import (
    ResolverBlockStore,
    ResolverCrossCheck,
    ResolverPollerWorker,
    ResolverRowCollector,
    seed_records_from_metar_log,
)
from .risk import Ledger, RiskManager
from .strategy import Strategy
from .supervisor import Supervisor
from .worker import CityWorker


_SYNOPTIC_TOKEN: list = []


def _synoptic_token() -> str:  # pragma: no cover - trivial network helper
    """weather.gov's published Synoptic token, fetched once per process."""

    if not _SYNOPTIC_TOKEN:
        from .feeds_http import fetch_synoptic_token

        _SYNOPTIC_TOKEN.append(fetch_synoptic_token(session=_new_session()))
    return _SYNOPTIC_TOKEN[0]


def _new_session():  # pragma: no cover - trivial network helper
    import requests

    return requests.Session()


def load_dotenv(path: str = ".env") -> int:
    """Load ``KEY=VALUE`` lines from a git-ignored ``.env`` into the process env.

    Real environment variables always win (no override). Comments and blank
    lines are skipped; values may be quoted. Returns the number of keys set —
    purely stdlib, so keys like ``TWC_API_KEY`` survive restarts without
    living in shell history or the repo.
    """

    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    n = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            n += 1
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dead-bracket-bot", description=__doc__)
    p.add_argument(
        "--live",
        action="store_true",
        help="enable live signed execution (default: paper)",
    )
    p.add_argument(
        "--bankroll",
        type=float,
        default=float(os.environ.get("BANKROLL", "0") or 0),
        help="bet size as a PERCENT of current balance (0-100); e.g. 50 with a "
        "$30 balance caps a bet at $15, growing as wins grow the balance. "
        "Must be > 0 for --live.",
    )
    p.add_argument(
        "--balance",
        type=float,
        default=(float(os.environ["BALANCE"]) if os.environ.get("BALANCE") else None),
        help="starting balance for percentage sizing. Required in paper mode; "
        "in live mode it overrides the wallet balance fetched at startup.",
    )
    p.add_argument(
        "--cities",
        default=None,
        help="comma-separated city ids, or 'all' (default: all configured)",
    )
    p.add_argument(
        "--no-trade",
        default=None,
        help="comma-separated city ids to fence out of trading: they still "
        "poll METAR, discover markets, and log prices/dead/opportunities, "
        "but place NO orders (live or paper). Use to observe a city safely.",
    )
    p.add_argument(
        "--hko",
        action="store_true",
        help="read the HKO station directly for cities that resolve off one "
        "(e.g. Shenzhen -> Lau Fau Shan/WMO 45035) instead of the NOAA "
        "METAR. Logs to metar_hko.jsonl. Off by default. Pair with "
        "--no-trade to shadow-validate a city before trading off HKO.",
    )
    p.add_argument(
        "--twc",
        action="store_true",
        help="L1 resolver feed: for cities that resolve off a TWC/WU "
        "station table (Shenzhen), compute the running max from "
        "the CLOSED rows of the resolver's own timeseries "
        "(twc_rows.jsonl) — the literal content the market "
        "settles on. Requires --calibrate (whose worker is the "
        "process's single TWC poller) and TWC_API_KEY. Takes "
        "precedence over --hko for those cities. Pair with "
        "--no-trade <city> for the 3-5 day shadow phase before "
        "any re-enable decision.",
    )
    p.add_argument(
        "--calibrate",
        action="store_true",
        help="start the LFS calibration collectors for cities that resolve "
        "off an HKO station (Shenzhen): HKO 1-min CSV + TWC row-version "
        "+ daily-table capture + the WU-prediction shadow. Data "
        "collection only — no strategy or order-path effect. Files land "
        "in data/<city>/. Also enabled by LFS_CALIBRATION=1; TWC capture "
        "needs TWC_API_KEY (else HKO-only).",
    )
    p.add_argument(
        "--forecast",
        action="store_true",
        help="enable the forecast path: sub-hourly national feed pollers "
        "(Météo-France/KNMI/FMI/JMA/NEA, signal-only) + the trust-decay "
        "signal engine with fractional-Kelly sizing. Trades in PAPER "
        "mode; in live mode it is signal-LOGGING only unless "
        "--forecast-live is also given. Needs --balance (or --bankroll "
        "in live) for Kelly sizing. Also enabled by FORECAST=1.",
    )
    p.add_argument(
        "--forecast-live",
        action="store_true",
        help="allow REAL orders from forecast signals (requires --live and "
        "--forecast). This is the human go/no-go: only flip it after "
        "the paper soak + tools/forecast_calibrate.py gates pass.",
    )
    p.add_argument("--data-dir", default=None, help="override DATA_DIR")
    p.add_argument(
        "--check",
        action="store_true",
        help="preflight: verify wallet credentials, balance, and allowances, then exit without trading",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="silence stderr progress (METAR readings, status, market-health notices)",
    )
    return p


def _build_live_client(config: Config):  # pragma: no cover - live path
    """Construct a Polymarket SecureClient from env secrets. Key never leaves here.

    Uses the unified ``polymarket-client`` SDK (the archived ``py-clob-client``
    is rejected by the venue with "invalid order version"). Set:
        POLY_PRIVATE_KEY   the signing key (e.g. Magic-exported)
        POLY_FUNDER        the wallet/proxy address holding the USDC (optional;
                           defaults to the signer's deposit wallet)
    Wallet type detection and API-credential derivation are automatic —
    POLY_SIGNATURE_TYPE is no longer needed.
    """

    from polymarket import SecureClient  # type: ignore

    key = os.environ.get("POLY_PRIVATE_KEY")
    if not key:
        raise RuntimeError("live mode requires POLY_PRIVATE_KEY in the environment")
    funder = os.environ.get("POLY_FUNDER")
    return SecureClient.create(private_key=key, wallet=funder)


def _parse_usdc(raw) -> float:
    """USDC balances/allowances come back in 6-decimal base units as strings."""

    try:
        return float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def run_check(config: Config) -> int:  # pragma: no cover - live path
    """Preflight: build the live client, report address/balance/allowance, exit.

    Places no orders and moves no funds — it only reads. Use it to confirm
    credentials and on-chain readiness before a live run.
    """

    try:
        client = _build_live_client(config)
    except Exception as exc:
        print(f"CHECK FAIL: cannot build client: {exc}", file=sys.stderr)
        return 2
    try:
        ba = client.get_balance_allowance(asset_type="COLLATERAL")
    except Exception as exc:
        print(f"CHECK FAIL: API call failed: {exc}", file=sys.stderr)
        return 2

    balance = _parse_usdc(getattr(ba, "balance", 0))
    allowances = {
        k: _parse_usdc(v) for k, v in (getattr(ba, "allowances", {}) or {}).items()
    }
    min_allowance = min(allowances.values()) if allowances else 0.0
    print("CHECK OK — credentials valid, no orders placed")
    print(
        f"  funder (wallet) : {os.environ.get('POLY_FUNDER') or '(signer deposit wallet)'}"
    )
    print(f"  USDC balance    : {balance:.2f}")
    for spender, amt in allowances.items():
        print(f"  allowance       : {spender[:10]}…  {amt:.2f}")
    if balance <= 0:
        print("  WARNING: zero balance — deposit USDC to the funder before --live")
    if min_allowance <= 0:
        print(
            "  WARNING: zero allowance — place one manual trade in the Polymarket UI "
            "to set allowances before --live"
        )
    return 0


def apply_bankroll_pct(config: Config, bankroll_pct: float) -> Config:
    """Switch sizing to a percentage of the current balance.

    ``--bankroll`` is now a percentage (0-100), not a dollar amount: it caps
    every bet at that fraction of the live balance (starting balance + realised
    PnL), all-in on a single high-conviction dead bracket, so bet sizes grow as
    the bot wins. Leaves sizing on the static dollar caps when bankroll_pct<=0.
    """

    if bankroll_pct <= 0:
        return config
    from dataclasses import replace

    return replace(config, bankroll_pct=bankroll_pct)


def _fetch_wallet_balance(client) -> float:  # pragma: no cover - live path
    """Read the funder wallet's USDC balance once, for percentage sizing."""

    ba = client.get_balance_allowance(asset_type="COLLATERAL")
    return _parse_usdc(getattr(ba, "balance", 0))


def resolve_initial_balance(
    config: Config, balance_arg: Optional[float], live_client=None
) -> float:
    """The starting balance for percentage sizing: an explicit ``--balance``
    wins; otherwise live mode reads the wallet, and paper mode has no default."""

    if balance_arg is not None:
        return max(0.0, balance_arg)
    if config.mode == "live" and live_client is not None:
        return _fetch_wallet_balance(live_client)
    return 0.0


def build_balance_tracker(
    config: Config, risk: RiskManager, initial_balance: float, live_client=None
):
    """Build the wallet-balance tracker and wire live equity into the risk caps.

    Live: an adaptive-cadence tracker over the real USDC balance, and the risk
    manager's percentage-sizing base becomes ``wallet_cash + open exposure`` so
    the caps self-correct against the wallet. Paper: a fixed synthetic tracker
    (no wallet to poll) — the risk manager keeps its exact synthetic accounting.
    """

    if config.mode == "live" and live_client is not None:
        tracker = LiveBalanceTracker(
            fetch=lambda: _fetch_wallet_balance(live_client),
            initial=initial_balance,
            fast_sec=config.balance_poll_fast_sec,
            slow_sec=config.balance_poll_slow_sec,
            cool_after=config.balance_poll_cool_after,
        )
        risk.set_equity_source(lambda: tracker.cash() + risk.total_exposure)
        return tracker
    return PaperBalanceTracker(initial_balance)


def build_forecast_workers(
    config: Config, reporter: Reporter, city_ids
) -> tuple[list[FeedPollerWorker], dict[str, FeedCollector]]:
    """Wire the sub-hourly feed pollers for the forecast path.

    One collector + daemon poller per selected city with a ``feed_source``
    whose auth (if any) is satisfied. A city whose key is missing gets one
    startup notice and stays METAR-only. Pure construction — no network until
    the worker thread polls. Returns (pollers, city_id -> collector) so the
    supervisor can hand each CityWorker its collector's ``latest``.
    """

    workers: list[FeedPollerWorker] = []
    collectors: dict[str, FeedCollector] = {}
    if not config.forecast:
        return workers, collectors
    for city_cfg in select_cities(config, city_ids):
        if not city_cfg.feed_source:
            continue
        fetch = build_feed_fetcher(city_cfg, config, session=_new_session())
        if fetch is None:
            key_env = {"meteofrance": "MF_API_KEY", "knmi": "KNMI_API_KEY"}.get(
                city_cfg.feed_source, "?"
            )
            reporter.event(
                city_cfg.id,
                "feed_skipped",
                src=city_cfg.feed_source,
                missing_key=key_env,
            )
            reporter.notice(
                city_cfg.id,
                f"forecast: {city_cfg.feed_source} feed OFF — set {key_env} "
                "to enable (city stays METAR-only)",
            )
            continue
        collector = FeedCollector(
            city=city_cfg.id,
            src=city_cfg.feed_source,
            station=city_cfg.feed_station_id or "",
            path=os.path.join(config.data_dir, city_cfg.id, "feed.jsonl"),
            fetch=fetch,
        )
        collectors[city_cfg.id] = collector
        # Poll at most every 60 s regardless of cadence: publication latency is
        # the product, and dedup makes early polls free (JMA's big map file is
        # additionally guarded by its latest_time stamp check).
        poll_sec = float(min(city_cfg.feed_cadence_sec, 60))
        workers.append(
            FeedPollerWorker(
                city=city_cfg.id,
                collector=collector,
                reporter=reporter,
                poll_sec=poll_sec,
            )
        )
        reporter.event(
            city_cfg.id,
            "feed_start",
            src=city_cfg.feed_source,
            station=city_cfg.feed_station_id,
            cadence_sec=city_cfg.feed_cadence_sec,
            poll_sec=poll_sec,
        )
        reporter.notice(
            city_cfg.id,
            f"forecast feed ON: {city_cfg.feed_source} station "
            f"{city_cfg.feed_station_id} (cadence {city_cfg.feed_cadence_sec}s) "
            f"-> feed.jsonl + signals.jsonl",
        )
    return workers, collectors


def build_resolver_guards(
    config: Config, reporter: Reporter, risk: RiskManager, city_ids
) -> tuple[dict[str, ResolverCrossCheck], list[ResolverPollerWorker], ResolverBlockStore]:
    """Wire the resolver cross-check: one guard + poller per selected city.

    Each guard's own side is seeded from the city's existing ``metar.jsonl`` so
    a restart has the trailing history the 24 h coverage rule needs instead of
    idling for a day; the resolver side is seeded from ``wu_rows.jsonl`` by the
    collector. Sticky divergence blocks are re-adopted from disk here, so a
    restart comes back blocked rather than forgetting and re-buying.

    Without a TWC API key nothing can be verified, so every city stays
    ``unverified`` and therefore untradeable — deliberately fail-closed, with
    one loud startup notice rather than a silent return to the old behaviour.
    """

    guards: dict[str, ResolverCrossCheck] = {}
    pollers: list[ResolverPollerWorker] = []
    blocks = ResolverBlockStore(
        os.path.join(config.data_dir, "_global", "resolver_blocks.json")
    )
    if not config.resolver_check:
        return guards, pollers, blocks

    if not config.twc_api_key:
        reporter.notice(
            "_global",
            "resolver cross-check has NO TWC_API_KEY — every city is unverified "
            "and will NOT trade. Set TWC_API_KEY (.env) to enable trading.",
        )
        reporter.event("_global", "resolver_no_key")

    for city_cfg in select_cities(config, city_ids):
        tz = None
        try:
            tz = ZoneInfo(city_cfg.tz)
        except Exception:
            pass
        # Synoptic needs no API key — only the station the market names. That
        # also makes Istanbul verifiable for the first time: WU served LTFM no
        # rows at all, NOAA serves 96 in 48h.
        if city_cfg.resolver_provider == "synoptic":
            configured = bool(city_cfg.metar_icao)
        else:
            configured = bool(city_cfg.resolver_location and config.twc_api_key)
        guard = ResolverCrossCheck(
            city_cfg.id,
            tz=tz,
            window_hours=config.resolver_window_hours,
            min_pairs=config.resolver_min_pairs,
            min_reach_hours=config.resolver_min_reach_hours,
            max_gap_hours=config.resolver_max_gap_hours,
            max_lag_hours=config.resolver_max_lag_hours,
            settle_lag_min=config.resolver_settle_lag_min,
            min_day_obs=config.resolver_min_day_obs,
            tolerance_c=config.resolver_tolerance_c,
            configured=configured,
            # seoul/shenzhen read the resolver's table AS their running max, so
            # there are no two sides to compare — gate on the table's health.
            self_sourced=(city_cfg.resolution_source == "resolver" and configured),
        )
        city_dir = os.path.join(config.data_dir, city_cfg.id)
        # Seed our side from whichever log this city's running max is fed by
        # (resolver-sourced cities first — that IS their trading path).
        for log_name in ("metar_resolver.jsonl", "metar_twc.jsonl",
                         "metar_hko.jsonl", "metar.jsonl"):
            path = os.path.join(city_dir, log_name)
            if os.path.exists(path):
                guard.seed_ours(seed_records_from_metar_log(
                    path,
                    hours=config.resolver_window_hours * 2,
                    include_speci=city_cfg.include_speci,
                ))
                break
        # Our log can have holes the resolver's table does not (a restart after
        # downtime, a new city, a fetcher outage). The Synoptic side backfills
        # 48h in its first poll, so without this the city sits unverified for a
        # day waiting on reach-back while the comparison itself is ready. Seeds
        # the CROSS-CHECK only — never the running max.
        if configured and city_cfg.resolution_source == "metar":
            try:
                history = fetch_metar_history(
                    city_cfg.metar_icao,
                    hours=int(config.resolver_window_hours * 2),
                    session=_new_session(),
                    include_speci=city_cfg.include_speci,
                )
            except Exception:
                history = []
            if history:
                guard.seed_ours(history)
                reporter.event(city_cfg.id, "resolver_seed_history",
                               obs=len(history))
        # Re-arm a divergence recorded by an earlier run.
        prior = blocks.blocked(city_cfg.id)
        if prior:
            detail = prior.get("detail") or prior.get("reason") or "resolver divergence"
            guard.adopt_block(detail)
            risk.block_city_sticky(city_cfg.id, detail)
            reporter.notice(
                city_cfg.id,
                f"TRADING BLOCKED since {prior.get('since', '?')} — {detail}. "
                f"Clear with: python -m dead_bracket.tools.resolver_clear {city_cfg.id}",
            )
        guards[city_cfg.id] = guard

        if not configured:
            if not city_cfg.resolver_location:
                reporter.notice(
                    city_cfg.id,
                    "no resolver table configured — city can never be verified "
                    "and will NOT trade (data collection continues)",
                )
            continue
        if city_cfg.resolver_provider == "synoptic":
            fetch = make_synoptic_range_fetch(
                station=city_cfg.metar_icao,
                token=_synoptic_token(),
                session=_new_session(),
                recent_minutes=config.resolver_recent_minutes,
            )
            rows_path = os.path.join(city_dir, "resolver_rows.jsonl")
            source_label = f"noaa/synoptic {city_cfg.metar_icao}"
        else:
            fetch = make_twc_range_fetch(
                api_key=config.twc_api_key,
                location=city_cfg.resolver_location,
                session=_new_session(),
            )
            rows_path = os.path.join(city_dir, "wu_rows.jsonl")
            source_label = f"twc/wu {city_cfg.resolver_location}"
        collector = ResolverRowCollector(
            city=city_cfg.id,
            guard=guard,
            fetch=fetch,
            path=rows_path,
            tz=tz,
        )
        pollers.append(
            ResolverPollerWorker(
                city=city_cfg.id,
                collector=collector,
                reporter=reporter,
                poll_sec=config.resolver_poll_sec,
            )
        )
        reporter.event(city_cfg.id, "resolver_start", source=source_label,
                       location=city_cfg.resolver_location)
    return guards, pollers, blocks


def build_supervisor(
    config: Config,
    reporter: Reporter,
    risk: RiskManager,
    city_ids,
    *,
    live_client=None,
    no_trade_cities=(),
    use_hko=False,
    use_twc=False,
    feed_collectors=None,
    balance=None,
    resolver_guards=None,
    resolver_blocks=None,
) -> Supervisor:
    selected = select_cities(config, city_ids)
    no_trade = set(no_trade_cities)
    # One batched METAR request for ALL stations, instead of N per cycle —
    # gentler on the API and far fewer independent timeout points.
    shared_metar = SharedMetarFetcher(
        [c.metar_icao for c in selected],
        session=_new_session(),
        ttl=config.metar_poll_sec,
        # Hot-window fast path: per-station NOAA tgftp races the (CDN-cached)
        # batched fetch so a fresh report is seen within ~the hot cadence.
        station_fetch=make_tgftp_fetch(),
        # Stations whose market resolves on the routine cycle only (Moscow):
        # exclude SPECIs from the JSON selection and skip their tgftp fast path.
        exclude_speci_icaos={c.metar_icao for c in selected if not c.include_speci},
    )
    workers: list[CityWorker] = []
    for city_cfg in selected:
        # Resolution-source selection for HKO/TWC-resolved cities (Shenzhen).
        # --twc (L1): running max from the CLOSED rows of the resolver's own
        # timeseries — no sensor of ours in the loop at all. Takes precedence
        # over --hko. Each source logs to its OWN file so histories never mix —
        # the running max seeds only from the source in use.
        twc_resolver = bool(
            use_twc and city_cfg.resolution_hko_place and config.twc_api_key
        )
        hko_place = (
            city_cfg.resolution_hko_place if (use_hko and not twc_resolver) else None
        )
        # Cities whose METAR provably does not track the resolver (seoul,
        # shenzhen — see config.RESOLVER_LOCATIONS for the measurements) read
        # their running max from the resolver's OWN closed rows, collected by
        # the resolver poller into wu_rows.jsonl. --twc keeps precedence so the
        # Shenzhen calibration workflow is unchanged.
        use_resolver_src = (
            not twc_resolver
            and city_cfg.resolution_source == "resolver"
            and config.resolver_check
            and bool(city_cfg.resolver_location and config.twc_api_key)
        )
        if city_cfg.resolution_source == "resolver" and not (
            twc_resolver or use_resolver_src
        ):
            # No key / no table: this city has NO valid source at all. Idle it
            # rather than silently falling back to the METAR we know is wrong.
            reporter.event(city_cfg.id, "resolver_source_unavailable")
            reporter.notice(
                city_cfg.id,
                "resolution source is the resolver table but it is unavailable "
                "(needs TWC_API_KEY + resolver_check) — city will NOT trade; "
                "its METAR is known not to track the resolver",
            )
        if use_resolver_src:
            fetcher = TwcResolverFetcher(
                path=os.path.join(config.data_dir, city_cfg.id, "wu_rows.jsonl"),
                icao=city_cfg.metar_icao,
                location=city_cfg.resolver_location,
                stale_sec=config.twc_resolver_stale_sec,
            )
            metar = MetarFeed(
                city_cfg.metar_icao,
                fetch_fn=fetcher.raw_for,
                log_path=os.path.join(
                    config.data_dir, city_cfg.id, "metar_resolver.jsonl"
                ),
                tz=ZoneInfo(city_cfg.tz),
                include_speci=city_cfg.include_speci,
            )
            reporter.event(city_cfg.id, "resolver_source",
                           location=city_cfg.resolver_location)
            reporter.notice(
                city_cfg.id,
                f"resolution source: the RESOLVER's own closed rows "
                f"({city_cfg.resolver_location} -> metar_resolver.jsonl); "
                f"{city_cfg.metar_icao} METAR is OUT of the trading path "
                "(it does not track the resolver)",
            )
        elif twc_resolver:
            fetcher = TwcResolverFetcher(
                path=os.path.join(config.data_dir, city_cfg.id, "twc_rows.jsonl"),
                icao=city_cfg.metar_icao,
                location=config.twc_location,
                stale_sec=config.twc_resolver_stale_sec,
            )
            metar = MetarFeed(
                city_cfg.metar_icao,
                fetch_fn=fetcher.raw_for,
                log_path=os.path.join(config.data_dir, city_cfg.id, "metar_twc.jsonl"),
                tz=ZoneInfo(city_cfg.tz),
                include_speci=city_cfg.include_speci,
            )
            reporter.event(
                city_cfg.id, "twc_resolver_source", location=config.twc_location
            )
            reporter.notice(
                city_cfg.id,
                f"resolution source: TWC resolver rows, closed hours "
                f"({config.twc_location} -> metar_twc.jsonl); ZGSZ METAR "
                "and HKO rhrread are OUT of the trading path",
            )
        elif hko_place:
            hko = HkoStationFetcher(
                station=hko_place,
                icao=city_cfg.metar_icao,
                session=_new_session(),
                ttl=config.metar_poll_sec,
            )
            metar = MetarFeed(
                city_cfg.metar_icao,
                fetch_fn=hko.raw_for,
                log_path=os.path.join(config.data_dir, city_cfg.id, "metar_hko.jsonl"),
                tz=ZoneInfo(city_cfg.tz),
                include_speci=city_cfg.include_speci,
            )
            reporter.event(city_cfg.id, "hko_source", station=hko_place)
            reporter.notice(
                city_cfg.id,
                f"resolution source: HKO {hko_place} (not {city_cfg.metar_icao} METAR)",
            )
        else:
            metar = MetarFeed(
                city_cfg.metar_icao,
                fetch_fn=shared_metar.raw_for,
                receipt_fn=shared_metar.receipt_for,  # AWC receipt time -> publish/my split
                log_path=os.path.join(config.data_dir, city_cfg.id, "metar.jsonl"),
                tz=ZoneInfo(city_cfg.tz),
                include_speci=city_cfg.include_speci,
            )

        def _on_event(slug, cid=city_cfg.id):
            reporter.event(cid, "discovery", slug=slug)
            reporter.notice(cid, f"discovered event {slug}")

        provider = GammaMarketProvider(
            market_terms=city_cfg.market_terms,
            metar_icao=city_cfg.metar_icao,
            on_event=_on_event,
            tz=ZoneInfo(city_cfg.tz),
            book_band_below=config.book_band_below,
            # Widen the read band so the price sampler's frontier band still has
            # rows (it samples [rmax, rmax+price_sample_band]).
            book_band_above=max(config.book_band_above, config.price_sample_band),
        )
        market = MarketFeed(city_cfg.id, snapshot_fn=provider.snapshot)
        trade_disabled = city_cfg.id in no_trade
        if trade_disabled:
            reporter.event(city_cfg.id, "trade_disabled")
            reporter.notice(
                city_cfg.id, "trading DISABLED (data collection only; no orders)"
            )
        executor = make_executor(
            config, risk, client=live_client, trade_disabled=trade_disabled
        )
        strategy = Strategy(city_cfg.id, config, risk)
        # Forecast path: only cities with an ACTIVE feed collector get the
        # probabilistic engine; everyone else runs exactly as before.
        collector = (feed_collectors or {}).get(city_cfg.id)
        forecast = feed_reader = None
        if collector is not None:
            forecast = ForecastStrategy(city_cfg.id, config, city_cfg, risk)
            feed_reader = collector.latest
        workers.append(
            CityWorker(
                city_cfg,
                config,
                metar=metar,
                market=market,
                executor=executor,
                strategy=strategy,
                risk=risk,
                reporter=reporter,
                forecast=forecast,
                feed_reader=feed_reader,
                balance=balance,
                resolver=(resolver_guards or {}).get(city_cfg.id),
                resolver_blocks=resolver_blocks,
            )
        )
    return Supervisor(
        workers,
        risk=risk,
        reporter=reporter,
        rollover_tz=config.rollover_tz,
        daily_rearm=config.daily_rearm,
    )


def build_calibration_workers(
    config: Config, reporter: Reporter, city_ids
) -> list[LfsCalibrationWorker]:
    """Wire the LFS calibration collectors + WU-prediction shadow.

    One worker per selected city that resolves off an HKO station (Shenzhen).
    Pure construction — no network happens until the worker thread polls.
    Without a TWC API key only the HKO 1-min collector runs (the K1/K2/K3
    joins then have no right-hand side, so the key is strongly recommended).
    """

    workers: list[LfsCalibrationWorker] = []
    for city_cfg in select_cities(config, city_ids):
        if not city_cfg.resolution_hko_place:
            continue
        city_dir = os.path.join(config.data_dir, city_cfg.id)
        hko_path = os.path.join(city_dir, "hko_1min.jsonl")
        hko = Hko1MinCollector(
            fetch=make_hko_1min_fetch(session=_new_session()),
            station=city_cfg.resolution_hko_place,
            path=hko_path,
        )
        predictor = WuPredictor(
            pred_path=os.path.join(city_dir, "predictions.jsonl"),
            eval_path=os.path.join(city_dir, "prediction_eval.jsonl"),
            rounding=config.lfs_predict_rounding,
            seed_hko_path=hko_path,
        )
        twc_rows = twc_daily = lock_ahead = None
        if config.twc_api_key:
            sess = _new_session()
            twc_rows_path = os.path.join(city_dir, "twc_rows.jsonl")
            twc_rows = TwcRowCollector(
                fetch=make_twc_timeseries_fetch(
                    api_key=config.twc_api_key,
                    location=config.twc_location,
                    session=sess,
                ),
                path=twc_rows_path,
            )
            twc_daily = TwcDailyCollector(
                fetch=make_twc_historical_fetch(
                    api_key=config.twc_api_key,
                    location=config.twc_location,
                    session=sess,
                ),
                path=os.path.join(city_dir, "twc_daily.jsonl"),
                capture_hkt=tuple(
                    s for s in config.twc_daily_hkt.split(",") if s.strip()
                ),
            )
            # L2 shadow needs both sides (HKO locks, TWC rows to score them),
            # so it only runs when the TWC collectors do.
            lock_ahead = LockAheadShadow(
                path=os.path.join(city_dir, "lock_ahead.jsonl"),
                margin_c=config.lock_ahead_margin_c,
                seed_twc_path=twc_rows_path,
            )
        else:
            reporter.notice(
                city_cfg.id,
                "calibration: TWC_API_KEY missing — HKO 1-min only "
                "(no TWC row/daily capture, K1/K2/K3 cannot close)",
            )
        workers.append(
            LfsCalibrationWorker(
                city=city_cfg.id,
                reporter=reporter,
                hko=hko,
                twc_rows=twc_rows,
                twc_daily=twc_daily,
                predictor=predictor,
                lock_ahead=lock_ahead,
                health_path=os.path.join(city_dir, "collector_health.jsonl"),
                hko_poll_sec=config.hko_1min_poll_sec,
                twc_poll_sec=config.twc_poll_sec,
            )
        )
        reporter.event(
            city_cfg.id,
            "calibration_start",
            station=city_cfg.resolution_hko_place,
            twc=bool(config.twc_api_key),
            rounding=config.lfs_predict_rounding,
        )
        reporter.notice(
            city_cfg.id,
            f"calibration collectors ON (HKO 1-min"
            f"{' + TWC rows + daily' if config.twc_api_key else ''}"
            f", predict={config.lfs_predict_rounding}) -> {city_dir}/",
        )
    return workers


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()  # .env in CWD; real env vars always win
    args = build_parser().parse_args(argv)
    if args.forecast_live and not (args.live and args.forecast):
        print(
            "HALT: --forecast-live requires both --live and --forecast", file=sys.stderr
        )
        return 2
    config = Config.from_env()
    if args.live:
        config = config.with_mode("live")
    if args.data_dir or args.forecast or args.forecast_live:
        from dataclasses import replace

        overrides = {}
        if args.data_dir:
            overrides["data_dir"] = args.data_dir
        if args.forecast:
            overrides["forecast"] = True
        if args.forecast_live:
            overrides["forecast_live"] = True
        config = replace(config, **overrides)

    # Preflight credential/balance/allowance check — reads only, never trades.
    if args.check:
        return run_check(config)

    # Sizing: --bankroll is a percentage of the current balance (default static
    # caps when unset). Live mode requires it; paper needs an explicit --balance.
    live_client = None
    config = apply_bankroll_pct(config, args.bankroll)
    if config.mode == "live":
        if args.bankroll <= 0:
            print(
                "HALT: live mode requires a non-zero bankroll percentage",
                file=sys.stderr,
            )
            return 2
        # Live lock (2026-08-18). The 2026-07-19 Seoul false-dead emptied the
        # wallet because nothing cross-checked our METAR against the table the
        # market settles on. That guard now exists but has no soak behind it,
        # so live trading is opt-in through the environment rather than a bare
        # --live: a flag alone is too easy to reach for out of habit.
        if os.environ.get("DEAD_BRACKET_ALLOW_LIVE", "").strip() not in ("1", "true", "yes", "on"):
            print(
                "HALT: live trading is locked. The 2026-07-19 Seoul false-dead "
                "wiped the wallet (our METAR 27C vs the resolver's 26C for the "
                "same observation).\n"
                "      The resolver cross-check that would have caught it is "
                "new and unsoaked. Run paper until it has a clean record:\n"
                "        python -m dead_bracket.main --balance <amount>\n"
                "        python -m dead_bracket.tools.resolver_status\n"
                "      To deliberately override: DEAD_BRACKET_ALLOW_LIVE=1",
                file=sys.stderr,
            )
            return 2
        live_client = _build_live_client(config)
    if config.bankroll_pct > 0 and config.mode != "live" and args.balance is None:
        print(
            "HALT: percentage sizing in paper mode requires --balance", file=sys.stderr
        )
        return 2
    # The forecast path sizes with fractional Kelly on the CURRENT BALANCE, so
    # it needs one even when the dead path runs on static caps (bankroll 0).
    if config.forecast and config.mode != "live" and args.balance is None:
        print(
            "HALT: --forecast needs --balance for Kelly sizing in paper mode",
            file=sys.stderr,
        )
        return 2

    initial_balance = (
        resolve_initial_balance(config, args.balance, live_client)
        if (config.bankroll_pct > 0 or config.forecast)
        else 0.0
    )

    city_tz = {c.id: c.tz for c in config.cities}
    reporter = Reporter(config.data_dir, city_tz=city_tz, progress=not args.quiet)
    # Paper equity ledger: cumulative realised PnL persisted across restarts so
    # the bankroll actually tracks results instead of resetting to the starting
    # balance every run (and every daily rollover). Live sizes off the wallet.
    ledger = None
    if config.mode != "live" and initial_balance > 0:
        ledger = Ledger(
            os.path.join(config.data_dir, "_global", "paper_ledger.json"),
            initial_balance=initial_balance,
        )
        if abs(ledger.cumulative_pnl) > 1e-9:
            reporter.progress(
                f"LEDGER  resuming paper equity ${ledger.equity:.2f} "
                f"(start ${ledger.initial_balance:.2f}, "
                f"cumulative PnL {ledger.cumulative_pnl:+.2f}) "
                f"-> {ledger.path}"
            )
        initial_balance = ledger.initial_balance
    risk = RiskManager(config, initial_balance=initial_balance, ledger=ledger)
    # Wallet-balance tracker: live polls the real USDC balance (adaptive cadence)
    # and drives percentage sizing off actual capital; paper stays synthetic.
    balance = build_balance_tracker(config, risk, initial_balance, live_client)
    # Default: all configured cities. "--cities all" is an explicit alias.
    if not args.cities or args.cities.strip().lower() == "all":
        city_ids = [c.id for c in config.cities]
    else:
        city_ids = [c.strip() for c in args.cities.split(",") if c.strip()]
    no_trade_cities = (
        [c.strip() for c in args.no_trade.split(",") if c.strip()]
        if args.no_trade
        else []
    )
    # --twc preconditions: the calibration worker is the process's single TWC
    # poller (it writes the twc_rows.jsonl the resolver feed reads), and no key
    # means no rows at all — refuse loudly rather than run a silently-stale feed.
    calibrate = bool(args.calibrate) or config.lfs_calibration
    if args.twc:
        if not config.twc_api_key:
            print(
                "HALT: --twc requires TWC_API_KEY (set it in the environment "
                "or a .env file)",
                file=sys.stderr,
            )
            return 2
        if not calibrate:
            print(
                "HALT: --twc requires --calibrate (its worker is the TWC "
                "poller that feeds twc_rows.jsonl)",
                file=sys.stderr,
            )
            return 2
    # Forecast feed pollers first, so the supervisor can inject each city's
    # collector into its worker.
    feed_workers, feed_collectors = build_forecast_workers(config, reporter, city_ids)
    # Resolver cross-check: guards gate entries, pollers keep their side fresh.
    resolver_guards, resolver_workers, resolver_blocks = build_resolver_guards(
        config, reporter, risk, city_ids
    )
    supervisor = build_supervisor(
        config,
        reporter,
        risk,
        city_ids,
        live_client=live_client,
        no_trade_cities=no_trade_cities,
        use_hko=args.hko,
        use_twc=args.twc,
        feed_collectors=feed_collectors,
        balance=balance,
        resolver_guards=resolver_guards,
        resolver_blocks=resolver_blocks,
    )
    # LFS calibration collectors (Shenzhen): --calibrate or LFS_CALIBRATION=1.
    cal_workers = (
        build_calibration_workers(config, reporter, city_ids) if calibrate else []
    )

    reporter.event(
        "_global",
        "startup",
        mode=config.mode,
        cities=list(city_ids),
        no_trade=list(no_trade_cities),
        hko=bool(args.hko),
        twc=bool(args.twc),
        calibrate=[w.city for w in cal_workers],
        forecast=sorted(feed_collectors),
        forecast_live=config.forecast_live,
    )
    if config.forecast:
        gate = (
            "LIVE ORDERS ENABLED"
            if config.forecast_live
            else (
                "live mode: signal-logging only (no --forecast-live)"
                if config.mode == "live"
                else "paper trading"
            )
        )
        reporter.progress(
            f"FORECAST  cities={','.join(sorted(feed_collectors)) or '(none)'}  "
            f"kelly={config.kelly_fraction}  min_edge={config.forecast_min_edge}  "
            f"[{gate}]"
        )
    if config.resolver_check:
        reporter.progress(
            f"RESOLVER cross-check ON: {len(resolver_workers)}/{len(city_ids)} "
            f"cities have a resolver table; a city trades only after "
            f"{config.resolver_window_hours:.0f}h of agreement. "
            f"Blocked now: {','.join(sorted(resolver_blocks.all_blocks())) or '(none)'}"
        )
    else:
        reporter.progress(
            "RESOLVER cross-check DISABLED (resolver_check=0) — the Seoul "
            "false-dead guard is OFF"
        )
    reporter.progress(
        f"START   mode={config.mode}  cities={','.join(city_ids)}  "
        + (f"no-trade={','.join(no_trade_cities)}  " if no_trade_cities else "")
        + "(stdout=trades only; this stream=progress, --quiet to silence)"
    )
    for w in feed_workers:
        w.start()
    for w in cal_workers:
        w.start()
    for w in resolver_workers:
        w.start()
    try:
        supervisor.run_forever()
    except KeyboardInterrupt:
        supervisor.stop()
    finally:
        for w in feed_workers:
            w.stop()
        for w in cal_workers:
            w.stop()
        for w in resolver_workers:
            w.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
