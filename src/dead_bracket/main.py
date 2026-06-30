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
from .feeds_http import GammaMarketProvider, SharedMetarFetcher, make_metar_fetch, make_tgftp_fetch
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


def build_supervisor(config: Config, reporter: Reporter, risk: RiskManager,
                     city_ids, *, live_client=None) -> Supervisor:
    selected = select_cities(config, city_ids)
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
        executor = make_executor(config, risk, client=live_client)
        strategy = Strategy(city_cfg.id, config, risk)
        workers.append(CityWorker(
            city_cfg, config, metar=metar, market=market, executor=executor,
            strategy=strategy, risk=risk, reporter=reporter,
        ))
    return Supervisor(workers, risk=risk, reporter=reporter,
                      rollover_tz=config.rollover_tz, daily_rearm=config.daily_rearm)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.from_env()
    if args.live:
        config = config.with_mode("live")
    if args.data_dir:
        from dataclasses import replace
        config = replace(config, data_dir=args.data_dir)

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

    initial_balance = (resolve_initial_balance(config, args.balance, live_client)
                       if config.bankroll_pct > 0 else 0.0)

    city_tz = {c.id: c.tz for c in config.cities}
    reporter = Reporter(config.data_dir, city_tz=city_tz, progress=not args.quiet)
    risk = RiskManager(config, initial_balance=initial_balance)
    # Default: all configured cities. "--cities all" is an explicit alias.
    if not args.cities or args.cities.strip().lower() == "all":
        city_ids = [c.id for c in config.cities]
    else:
        city_ids = [c.strip() for c in args.cities.split(",") if c.strip()]
    supervisor = build_supervisor(config, reporter, risk, city_ids, live_client=live_client)

    reporter.event("_global", "startup", mode=config.mode, cities=list(city_ids))
    reporter.progress(f"START   mode={config.mode}  cities={','.join(city_ids)}  "
                      f"(stdout=trades only; this stream=progress, --quiet to silence)")
    try:
        supervisor.run_forever()
    except KeyboardInterrupt:
        supervisor.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
