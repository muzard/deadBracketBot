# CLAUDE.md — Dead-Bracket NO Capture bot (+ forecast path)

Standalone Python bot for Polymarket daily **"highest temperature in <city>"**
markets. Each integer-°C bracket is a YES/NO pair settling to $1. Two entry
paths, one order pipeline:

1. **Dead path (certainty).** Once the resolution-grade METAR's running max
   passes a bracket, that bracket can never be the high, so its NO is certain
   → buy the NO if offered <98¢ (IOC). By default the NO is then **held to $1**;
   the 99.9¢ maker recycle sell is posted only when a *new* dead-NO buy this tick
   is blocked purely by the bankroll caps (`strategy.last_blocked_value > 0`),
   sized to cover just that shortfall, and cancelled when the block clears
   (`recycle_on_opportunity`, default on; set False for the legacy always-rest).
   Forecast entries keep their immediate 99.9¢ de-risk exit (see path 2).
2. **Forecast path (probabilistic, 2026-07-08 — a deliberate mandate widening,
   NO-side only).** Sub-hourly national-met feeds (same/co-located sensors as
   the resolver METAR) predict the next METAR print 6–30+ min early. The
   fitted trust-decay model (`forecast.py::p_trust`) turns a reading with
   margin `m` at lead `τ` into a win probability p̂; entries are fractional-
   Kelly-sized (`kelly_fraction`, default 0.25, HARD CEILING 0.5) and require
   `p̂ − ask − fee ≥ forecast_min_edge` (default 3¢) with the limit price at
   `p̂ − fee − edge`. These bets CAN lose (settlement pays $0 if the bracket
   wins the day) → any settled loss pauses the whole forecast path (sticky).
   YES-side / day-max betting remains a **non-goal**.

## Read these first
- **[FINDINGS.md](FINDINGS.md)** — the dead-NO edge is **real in thin markets**
  (Moscow, now Shenzhen): a week of daily winners, **+$49.72**, zero losers. It is
  **absent in liquid markets** (Paris: dead NO pre-priced to ~$1 before our METAR
  confirms) — so don't expect it everywhere, and it's a small, concentrated sample.
  In the thin markets we're **bankroll-limited, not liquidity-limited** (Shenzhen:
  ~11× the depth we took sat unlifted at/below CAP, all blocked by `size/caps`).
- **[docs/](docs/)** — the forecast path's research: `trust_decay_formula_…md`
  (the p̂ model + fitted PAR table), `subhourly_feed_openings_…md` (the Tier-1
  feeds), `feed_integration_strategy_…md` (phases/gates/kill switches),
  `early_reading_reliability_…md` (Kelly + ε), plus the Shenzhen/LFS audits.
- **[README.md](README.md)** — architecture, console policy, how to run.
- **[LIVE.md](LIVE.md)** — live runbook (wallet creds, caps, validation,
  forecast go/no-go).
- The original spec is `dead_bracket_no_strategy_spec.md` (in ~/Downloads).

## How to run / test
```bash
. .venv/bin/activate                       # use the project venv (macOS: python3)
python -m dead_bracket.main                # paper, ALL cities (the default)
python -m dead_bracket.main --check        # live preflight: creds/balance, no orders
python -m dead_bracket.main --live --bankroll 40   # 40% of wallet balance; needs POLY_PRIVATE_KEY + POLY_FUNDER
python -m dead_bracket.main --forecast --balance 100   # + forecast path, paper (Kelly needs a balance)
python -m dead_bracket.main --live --bankroll 40 --forecast                 # live dead path; forecast SIGNALS ONLY
python -m dead_bracket.main --live --bankroll 40 --forecast --forecast-live # forecast live orders (human go/no-go!)
TWC_API_KEY=<key> python -m dead_bracket.main --hko --no-trade shenzhen --calibrate  # + Shenzhen calibration collectors
python -m dead_bracket.tools.lfs_calibrate      # offline K1/K2/K3/ε report
python -m dead_bracket.tools.forecast_calibrate # offline sensor-σ/Brier/hold-rate report (the go/no-go input)
python -m pytest                           # 355 tests; NO live network in tests
```
Coverage gate (CI): ≥90% line on strategy/risk/executor/forecast/forecast_feeds
(+ keep worker there too). Keep it green.

## Architecture (one process, one thread per city, shared risk + reporter)
`Supervisor → CityWorker(metar, market, strategy, executor, risk, reporter)`.
- `metar.py` — `MetarFeed` + `RunningMax`. Monotonic running max, resets at the
  city's **local** midnight, downward-COR guard, rejects foreign-station and
  **decimal (6-min/non-resolution)** readings, and **SPECIs** per-city.
- `feeds_http.py` — the ONLY network code. `SharedMetarFetcher` (one batched
  aviationweather JSON request for all stations; cache-busts + tgftp fast path in
  hot windows). `GammaMarketProvider` (discovery + CLOB books: ONE batched
  `/books` POST per city per tick, scoped to the frontier band around the running
  max — `book_band_below`/`book_band_above`, default 1/2 — plus held positions, so
  ~33 cities don't trip the CLOB rate limit. A failed batch raises
  `TransientBookError` → the tick is skipped, NOT reported as market-unavailable).
- `strategy.py` — pure: entry sizing + the 99.9¢ exit. `risk.py` — caps + halts
  (+ the forecast exposure cap and the sticky forecast pause).
- `executor.py` — `PaperExecutor` (default sim) / `LiveExecutor`. `worker.py` —
  the per-city cycle. `reporter.py` — stdout=trades, stderr=progress, per-day logs.
- **Forecast path**: `forecast.py` (pure — `p_trust` model, Kelly sizing,
  `ForecastStrategy`, `FeedMetarGuard`), `forecast_feeds.py` (`FeedCollector` →
  `data/<city>/feed.jsonl` + a thread-safe latest-reading holder;
  `FeedPollerWorker` daemon thread per city), fetchers/parsers in
  `feeds_http.py` (`build_feed_fetcher`). Tier-1 feeds: paris=Météo-France 6-min
  (`MF_API_KEY`), amsterdam=KNMI 10-min (`KNMI_API_KEY`, parser unverified until
  a key exists), helsinki=FMI, tokyo=JMA AMeDAS, singapore=NEA (keyless).
  Missing key → one startup notice, city stays METAR-only.
  `tools/forecast_calibrate.py` = the offline gate report.

## Non-negotiable invariants (each has tests + a memory note — don't regress)
- **METAR/resolver-only for RESOLUTION TRUTH.** Only the resolution-grade METAR
  running max may mark a bracket dead; decimal temps are rejected from
  `MetarFeed` (non-resolution). The sub-hourly national feeds (forecast path)
  and the HKO/TWC calibration collectors are **signal/data-collection only**:
  they must NEVER reach a `MetarFeed`, a running max, the dead-bracket logic,
  or discovery. A forecast entry may only ever *anticipate* what the resolver
  would confirm (NO-side below a predicted print) — never contradict it, and
  never bet YES/day-max (spec non-goal). Don't import/extend
  `weatherTrader/multipleCityPoller.py`. Shenzhen stays `no_trade` until the
  LFS calibration acceptance gates pass (a human decision, see
  `shenzhen_calibration_requirements.md`).
- **Forecast live orders are human-gated.** `--forecast` alone trades only in
  paper; in live mode it logs signals. Real forecast orders require the
  explicit `--forecast-live` flag, to be flipped only after the paper soak +
  `tools/forecast_calibrate.py` gates (sensor |bias|≤0.3 & σ≤0.3, Brier≤0.02,
  ≥100 signals with hold rate in CI, positive EV after fees). A settled
  forecast loss pauses the path process-wide and stays paused across rollovers
  (restart to clear — it's a model/mapping bug until proven otherwise).
- **Discovery only ever tracks today's local-date HIGHEST event.** Never
  tomorrow's, never a `lowest-temperature` market. Idle if today's isn't open.
  Next-day rollover is **per-city at each city's own local midnight** (timezones
  differ); if the new day's market isn't open yet the city drops the stale event
  and idles, retrying until it appears (`GammaMarketProvider._needs_rediscover`).
- **Resolution-source matters.** A bracket is "dead" only if `temp < running_max`
  where the running max matches *that market's* resolution source. Wrong station
  / wrong day → false dead → real loss. Discovery enforces a **resolution-source
  cross-check** (`source_confirmed` in `feeds_http.py`): the event text must name
  the station ICAO **and** `wunderground`, else the city idles. **UUWW (Moscow)**,
  **LTFM (Istanbul)** and **LLBG (Tel Aviv)** are in `WUNDERGROUND_EXEMPT_ICAOS` —
  they waive ONLY the `wunderground` half (they resolve off a non-WU source — Tel
  Aviv off NOAA's `weather.gov` timeseries for the station) but **still must name
  their station ICAO** (the station is never waived). These three resolve on
  `weather.gov`'s **routine** cycle, so they carry `include_speci=False`: SPECIs
  are still fetched and **logged** (data collection is unaffected) but never fold
  into the trade-triggering running max — a SPECI spike must not mark a still-live
  bracket dead. Every other city counts SPECIs (`include_speci=True`).
- **stdout = pure trade stream** (`BOUGHT`/`SOLD` + one `HALT` for the global
  daily-loss kill only). Everything else → stderr / per-day `data/<city>/<date>/`.
- **Halts are per-city** (order errors, stale feed, crossed book, downward
  revision). Only daily-loss is global. Runs unattended across days (daily
  rollover settles + re-arms; caps free on sell fills and at EOD).
- **Live SDK is `polymarket-client`** (`from polymarket import SecureClient`),
  NOT the archived `py-clob-client`. Markets are negRisk; tick size is dynamic
  (clamp the 99.9¢ sell to `1-tick`). Wallet key only in the executor via env.

## Conventions
- All behaviour is config (`config.py`); scalar knobs override via UPPERCASE env
  vars. `--bankroll` is a **percentage of current balance** (`config.bankroll_pct`):
  every cap (total = per-city = per-bracket) is that % of the sizing base, so bet
  sizes grow as the bot wins (all-in on a single dead bracket). The base is the
  **live wallet equity** (`balance.py::LiveBalanceTracker` — real USDC cash + open
  exposure, polled with an adaptive cadence: fast while a buy is being attempted,
  cooling down when consecutive reads are unchanged) in live mode, and the
  synthetic `initial_balance + realized_pnl` in paper (`RiskManager.set_equity_source`
  installs the live source; `BALANCE_POLL_*` knobs tune the cadence). Paper needs
  `--balance`. With `--bankroll 0` the static dollar caps (`MAX_EXPOSURE_*`) apply.
- Forecast sizing/caps (all config): `kelly_fraction` (≤0.5 enforced),
  `forecast_min_edge`, `forecast_min_margin_c` (≥0.4 °C — METAR rounds),
  `forecast_p_cap` 0.99, the 17–21h local evening cutoff, same-ob cap 25% of
  balance (all brackets from one reading = ONE bet), cross-city forecast cap
  40% of balance, stale-feed cutoff, and the ±1 °C×2 feed-vs-METAR divergence
  guard. Forecast Kelly needs a balance even when the dead path runs static
  caps (`--forecast` without `--balance` in paper is refused).
- Keep the network layer in `feeds_http.py`; strategy/risk/executor stay pure and
  unit-testable (inject feeds/clients via fixtures).
- The assistant implements/configures live trading but does **not** place orders
  or move funds — the user runs `--live` and does the validation trade.

## Operational gotchas
- macOS: the command is `python3` (or activate `.venv`), not `python`.
- **Keep the Mac awake** (`caffeinate -i …`) — if it sleeps, polling stops and
  report windows are missed; restarts also re-print a stale first METAR reading.
