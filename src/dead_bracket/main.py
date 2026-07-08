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

from .config import Config, select_cities
from .executor import PaperExecutor, make_executor
from .feeds_http import (
    GammaMarketProvider,
    HkoStationFetcher,
    SharedMetarFetcher,
    build_feed_fetcher,
    make_hko_1min_fetch,
    make_metar_fetch,
    make_tgftp_fetch,
    make_twc_historical_fetch,
    make_twc_timeseries_fetch,
)
from .forecast import ForecastStrategy
from .forecast_feeds import FeedCollector, FeedPollerWorker
from .lfs_calibration import (
    Hko1MinCollector,
    LfsCalibrationWorker,
    TwcDailyCollector,
    TwcRowCollector,
    WuPredictor,
)
from .market import MarketFeed
from .metar import MetarFeed
from .reporter import Reporter
from .risk import RiskManager
from .strategy import Strategy
from .supervisor import Supervisor
from .worker import CityWorker


def _new_session():  # pragma: no cover - trivial network helper
    import requests
    return requests.Session()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dead-bracket-bot", description=__doc__)
    p.add_argument("--live", action="store_true", help="enable live signed execution (default: paper)")
    p.add_argument("--bankroll", type=float, default=float(os.environ.get("BANKROLL", "0") or 0),
                   help="bet size as a PERCENT of current balance (0-100); e.g. 50 with a "
                        "$30 balance caps a bet at $15, growing as wins grow the balance. "
                        "Must be > 0 for --live.")
    p.add_argument("--balance", type=float,
                   default=(float(os.environ["BALANCE"]) if os.environ.get("BALANCE") else None),
                   help="starting balance for percentage sizing. Required in paper mode; "
                        "in live mode it overrides the wallet balance fetched at startup.")
    p.add_argument("--cities", default=None,
                   help="comma-separated city ids, or 'all' (default: all configured)")
    p.add_argument("--no-trade", default=None,
                   help="comma-separated city ids to fence out of trading: they still "
                        "poll METAR, discover markets, and log prices/dead/opportunities, "
                        "but place NO orders (live or paper). Use to observe a city safely.")
    p.add_argument("--hko", action="store_true",
                   help="read the HKO station directly for cities that resolve off one "
                        "(e.g. Shenzhen -> Lau Fau Shan/WMO 45035) instead of the NOAA "
                        "METAR. Logs to metar_hko.jsonl. Off by default. Pair with "
                        "--no-trade to shadow-validate a city before trading off HKO.")
    p.add_argument("--calibrate", action="store_true",
                   help="start the LFS calibration collectors for cities that resolve "
                        "off an HKO station (Shenzhen): HKO 1-min CSV + TWC row-version "
                        "+ daily-table capture + the WU-prediction shadow. Data "
                        "collection only — no strategy or order-path effect. Files land "
                        "in data/<city>/. Also enabled by LFS_CALIBRATION=1; TWC capture "
                        "needs TWC_API_KEY (else HKO-only).")
    p.add_argument("--forecast", action="store_true",
                   help="enable the forecast path: sub-hourly national feed pollers "
                        "(Météo-France/KNMI/FMI/JMA/NEA, signal-only) + the trust-decay "
                        "signal engine with fractional-Kelly sizing. Trades in PAPER "
                        "mode; in live mode it is signal-LOGGING only unless "
                        "--forecast-live is also given. Needs --balance (or --bankroll "
                        "in live) for Kelly sizing. Also enabled by FORECAST=1.")
    p.add_argument("--forecast-live", action="store_true",
                   help="allow REAL orders from forecast signals (requires --live and "
                        "--forecast). This is the human go/no-go: only flip it after "
                        "the paper soak + tools/forecast_calibrate.py gates pass.")
    p.add_argument("--data-dir", default=None, help="override DATA_DIR")
    p.add_argument("--check", action="store_true",
                   help="preflight: verify wallet credentials, balance, and allowances, then exit without trading")
    p.add_argument("--quiet", action="store_true",
                   help="silence stderr progress (METAR readings, status, market-health notices)")
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
    allowances = {k: _parse_usdc(v) for k, v in (getattr(ba, "allowances", {}) or {}).items()}
    min_allowance = min(allowances.values()) if allowances else 0.0
    print("CHECK OK — credentials valid, no orders placed")
    print(f"  funder (wallet) : {os.environ.get('POLY_FUNDER') or '(signer deposit wallet)'}")
    print(f"  USDC balance    : {balance:.2f}")
    for spender, amt in allowances.items():
        print(f"  allowance       : {spender[:10]}…  {amt:.2f}")
    if balance <= 0:
        print("  WARNING: zero balance — deposit USDC to the funder before --live")
    if min_allowance <= 0:
        print("  WARNING: zero allowance — place one manual trade in the Polymarket UI "
              "to set allowances before --live")
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


def resolve_initial_balance(config: Config, balance_arg: Optional[float],
                            live_client=None) -> float:
    """The starting balance for percentage sizing: an explicit ``--balance``
    wins; otherwise live mode reads the wallet, and paper mode has no default."""

    if balance_arg is not None:
        return max(0.0, balance_arg)
    if config.mode == "live" and live_client is not None:
        return _fetch_wallet_balance(live_client)
    return 0.0


def build_forecast_workers(config: Config, reporter: Reporter, city_ids
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
                city_cfg.feed_source, "?")
            reporter.event(city_cfg.id, "feed_skipped", src=city_cfg.feed_source,
                           missing_key=key_env)
            reporter.notice(city_cfg.id,
                            f"forecast: {city_cfg.feed_source} feed OFF — set {key_env} "
                            "to enable (city stays METAR-only)")
            continue
        collector = FeedCollector(
            city=city_cfg.id, src=city_cfg.feed_source,
            station=city_cfg.feed_station_id or "",
            path=os.path.join(config.data_dir, city_cfg.id, "feed.jsonl"),
            fetch=fetch)
        collectors[city_cfg.id] = collector
        # Poll at most every 60 s regardless of cadence: publication latency is
        # the product, and dedup makes early polls free (JMA's big map file is
        # additionally guarded by its latest_time stamp check).
        poll_sec = float(min(city_cfg.feed_cadence_sec, 60))
        workers.append(FeedPollerWorker(city=city_cfg.id, collector=collector,
                                        reporter=reporter, poll_sec=poll_sec))
        reporter.event(city_cfg.id, "feed_start", src=city_cfg.feed_source,
                       station=city_cfg.feed_station_id,
                       cadence_sec=city_cfg.feed_cadence_sec, poll_sec=poll_sec)
        reporter.notice(city_cfg.id,
                        f"forecast feed ON: {city_cfg.feed_source} station "
                        f"{city_cfg.feed_station_id} (cadence {city_cfg.feed_cadence_sec}s) "
                        f"-> feed.jsonl + signals.jsonl")
    return workers, collectors


def build_supervisor(config: Config, reporter: Reporter, risk: RiskManager,
                     city_ids, *, live_client=None, no_trade_cities=(),
                     use_hko=False, feed_collectors=None) -> Supervisor:
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
        # A city that resolves off an HKO station reads it directly when --hko is
        # set (else the NOAA METAR as normal, so default behaviour is unchanged).
        # HKO readings land in a SEPARATE log so they never mix with the NOAA
        # history — the running max seeds only from the source in use.
        hko_place = city_cfg.resolution_hko_place if use_hko else None
        if hko_place:
            hko = HkoStationFetcher(station=hko_place, icao=city_cfg.metar_icao,
                                    session=_new_session(), ttl=config.metar_poll_sec)
            metar = MetarFeed(
                city_cfg.metar_icao,
                fetch_fn=hko.raw_for,
                log_path=os.path.join(config.data_dir, city_cfg.id, "metar_hko.jsonl"),
                tz=ZoneInfo(city_cfg.tz),
                include_speci=city_cfg.include_speci,
            )
            reporter.event(city_cfg.id, "hko_source", station=hko_place)
            reporter.notice(city_cfg.id, f"resolution source: HKO {hko_place} (not {city_cfg.metar_icao} METAR)")
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
            on_event=_on_event, tz=ZoneInfo(city_cfg.tz),
            book_band_below=config.book_band_below,
            # Widen the read band so the price sampler's frontier band still has
            # rows (it samples [rmax, rmax+price_sample_band]).
            book_band_above=max(config.book_band_above, config.price_sample_band),
        )
        market = MarketFeed(city_cfg.id, snapshot_fn=provider.snapshot)
        trade_disabled = city_cfg.id in no_trade
        if trade_disabled:
            reporter.event(city_cfg.id, "trade_disabled")
            reporter.notice(city_cfg.id, "trading DISABLED (data collection only; no orders)")
        executor = make_executor(config, risk, client=live_client,
                                 trade_disabled=trade_disabled)
        strategy = Strategy(city_cfg.id, config, risk)
        # Forecast path: only cities with an ACTIVE feed collector get the
        # probabilistic engine; everyone else runs exactly as before.
        collector = (feed_collectors or {}).get(city_cfg.id)
        forecast = feed_reader = None
        if collector is not None:
            forecast = ForecastStrategy(city_cfg.id, config, city_cfg, risk)
            feed_reader = collector.latest
        workers.append(CityWorker(
            city_cfg, config, metar=metar, market=market, executor=executor,
            strategy=strategy, risk=risk, reporter=reporter,
            forecast=forecast, feed_reader=feed_reader,
        ))
    return Supervisor(workers, risk=risk, reporter=reporter,
                      rollover_tz=config.rollover_tz, daily_rearm=config.daily_rearm)


def build_calibration_workers(config: Config, reporter: Reporter,
                              city_ids) -> list[LfsCalibrationWorker]:
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
            station=city_cfg.resolution_hko_place, path=hko_path)
        predictor = WuPredictor(
            pred_path=os.path.join(city_dir, "predictions.jsonl"),
            eval_path=os.path.join(city_dir, "prediction_eval.jsonl"),
            rounding=config.lfs_predict_rounding,
            seed_hko_path=hko_path)
        twc_rows = twc_daily = None
        if config.twc_api_key:
            sess = _new_session()
            twc_rows = TwcRowCollector(
                fetch=make_twc_timeseries_fetch(api_key=config.twc_api_key,
                                                location=config.twc_location,
                                                session=sess),
                path=os.path.join(city_dir, "twc_rows.jsonl"))
            twc_daily = TwcDailyCollector(
                fetch=make_twc_historical_fetch(api_key=config.twc_api_key,
                                                location=config.twc_location,
                                                session=sess),
                path=os.path.join(city_dir, "twc_daily.jsonl"),
                capture_hkt=tuple(s for s in config.twc_daily_hkt.split(",") if s.strip()))
        else:
            reporter.notice(city_cfg.id,
                            "calibration: TWC_API_KEY missing — HKO 1-min only "
                            "(no TWC row/daily capture, K1/K2/K3 cannot close)")
        workers.append(LfsCalibrationWorker(
            city=city_cfg.id, reporter=reporter, hko=hko, twc_rows=twc_rows,
            twc_daily=twc_daily, predictor=predictor,
            health_path=os.path.join(city_dir, "collector_health.jsonl"),
            hko_poll_sec=config.hko_1min_poll_sec,
            twc_poll_sec=config.twc_poll_sec))
        reporter.event(city_cfg.id, "calibration_start",
                       station=city_cfg.resolution_hko_place,
                       twc=bool(config.twc_api_key),
                       rounding=config.lfs_predict_rounding)
        reporter.notice(city_cfg.id,
                        f"calibration collectors ON (HKO 1-min"
                        f"{' + TWC rows + daily' if config.twc_api_key else ''}"
                        f", predict={config.lfs_predict_rounding}) -> {city_dir}/")
    return workers


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.forecast_live and not (args.live and args.forecast):
        print("HALT: --forecast-live requires both --live and --forecast", file=sys.stderr)
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
            print("HALT: live mode requires a non-zero bankroll percentage", file=sys.stderr)
            return 2
        live_client = _build_live_client(config)
    if config.bankroll_pct > 0 and config.mode != "live" and args.balance is None:
        print("HALT: percentage sizing in paper mode requires --balance", file=sys.stderr)
        return 2
    # The forecast path sizes with fractional Kelly on the CURRENT BALANCE, so
    # it needs one even when the dead path runs on static caps (bankroll 0).
    if config.forecast and config.mode != "live" and args.balance is None:
        print("HALT: --forecast needs --balance for Kelly sizing in paper mode",
              file=sys.stderr)
        return 2

    initial_balance = (resolve_initial_balance(config, args.balance, live_client)
                       if (config.bankroll_pct > 0 or config.forecast) else 0.0)

    city_tz = {c.id: c.tz for c in config.cities}
    reporter = Reporter(config.data_dir, city_tz=city_tz, progress=not args.quiet)
    risk = RiskManager(config, initial_balance=initial_balance)
    # Default: all configured cities. "--cities all" is an explicit alias.
    if not args.cities or args.cities.strip().lower() == "all":
        city_ids = [c.id for c in config.cities]
    else:
        city_ids = [c.strip() for c in args.cities.split(",") if c.strip()]
    no_trade_cities = ([c.strip() for c in args.no_trade.split(",") if c.strip()]
                       if args.no_trade else [])
    # Forecast feed pollers first, so the supervisor can inject each city's
    # collector into its worker.
    feed_workers, feed_collectors = build_forecast_workers(config, reporter, city_ids)
    supervisor = build_supervisor(config, reporter, risk, city_ids,
                                  live_client=live_client, no_trade_cities=no_trade_cities,
                                  use_hko=args.hko, feed_collectors=feed_collectors)
    # LFS calibration collectors (Shenzhen): --calibrate or LFS_CALIBRATION=1.
    calibrate = bool(args.calibrate) or config.lfs_calibration
    cal_workers = (build_calibration_workers(config, reporter, city_ids)
                   if calibrate else [])

    reporter.event("_global", "startup", mode=config.mode, cities=list(city_ids),
                   no_trade=list(no_trade_cities), hko=bool(args.hko),
                   calibrate=[w.city for w in cal_workers],
                   forecast=sorted(feed_collectors), forecast_live=config.forecast_live)
    if config.forecast:
        gate = ("LIVE ORDERS ENABLED" if config.forecast_live
                else ("live mode: signal-logging only (no --forecast-live)"
                      if config.mode == "live" else "paper trading"))
        reporter.progress(f"FORECAST  cities={','.join(sorted(feed_collectors)) or '(none)'}  "
                          f"kelly={config.kelly_fraction}  min_edge={config.forecast_min_edge}  "
                          f"[{gate}]")
    reporter.progress(f"START   mode={config.mode}  cities={','.join(city_ids)}  "
                      + (f"no-trade={','.join(no_trade_cities)}  " if no_trade_cities else "")
                      + "(stdout=trades only; this stream=progress, --quiet to silence)")
    for w in feed_workers:
        w.start()
    for w in cal_workers:
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
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
