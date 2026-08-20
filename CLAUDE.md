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

## STOP — read this before touching live trading (2026-08-18)
The bot **wiped the live wallet on 2026-07-19** (Seoul, ~$170 -> $0.90). RKSI's
METAR printed `27/22`; the resolver's row for that same instant held 26 °C. Our
running max went to 27, the 26 °C bracket was marked dead, and the whole
bankroll bought a NO that settled to $0 because WU's daily High never left 26.

**Which cities this actually affects — measured, not assumed** (2 months of
resolver rows vs our METAR logs, 2026-08-18; the numbers live in
`config.RESOLVER_LOCATIONS`):
- **31 of 34 checkable cities agree 100.0%** with zero false-dead days. For
  them the METAR *is* the resolver's data; nothing needs to change.
- **seoul 99.1%** — 1 false-dead day in 21 (the wipe). Same station, aligned
  timestamps, and every resolver row is an exact whole-°C→°F conversion, so it
  is neither a lag nor a tenths artifact: the table just holds a different
  whole degree ~0.9% of the time. Irreducible from the METAR.
- **shenzhen 15.0%** — 5 false-dead days in 14; a different station entirely
  (Lau Fau Shan). No transform recovers it.

Both now read the resolver's own closed rows instead of their METAR
(`resolution_source="resolver"`); verified live to reproduce the resolver's
daily max exactly.

Consequences now in the code:
- **Live mode is LOCKED.** `--live` refuses unless `DEAD_BRACKET_ALLOW_LIVE=1`.
  Paper until the resolver cross-check has a clean soak behind it.
- **`resolver_guard.py` gates every entry** (see the invariants below).
- **No more all-in.** `max_bracket_pct` (25%) caps one bracket's share of
  equity; `max_deployed_pct` (90%) caps total exposure and every cap is clamped
  to capital actually available. One false-dead can no longer be a total loss.
- **The bankroll compounds.** Cumulative realised PnL survives daily rollovers
  and restarts (`risk.py::Ledger` -> `data/_global/paper_ledger.json`); the
  daily figure still resets for the loss breaker.

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
python -m dead_bracket.main --balance 100          # paper with a real bankroll (equity compounds via the ledger)
DEAD_BRACKET_ALLOW_LIVE=1 python -m dead_bracket.main --live --bankroll 40  # LOCKED by default — see the STOP section
python -m dead_bracket.main --forecast --balance 100   # + forecast path, paper (Kelly needs a balance)
python -m dead_bracket.main --live --bankroll 40 --forecast                 # live dead path; forecast SIGNALS ONLY
python -m dead_bracket.main --live --bankroll 40 --forecast --forecast-live # forecast live orders (human go/no-go!)
TWC_API_KEY=<key> python -m dead_bracket.main --hko --no-trade shenzhen --calibrate  # + Shenzhen calibration collectors
python -m dead_bracket.main --twc --no-trade shenzhen --calibrate  # Shenzhen L1 shadow: running max from the resolver's CLOSED rows + L2 lock-ahead shadow (key via .env)
python -m dead_bracket.tools.resolver_status    # per-city: may it trade, and why not?
python -m dead_bracket.tools.resolver_clear <city>  # lift a sticky divergence block (human decision)
python -m dead_bracket.tools.lfs_calibrate      # offline K1/K2/K3/ε report + L1 stability + L2 hold rate
python -m dead_bracket.tools.forecast_calibrate # offline sensor-σ/Brier/hold-rate report (the go/no-go input)
python -m pytest                           # 372 tests; NO live network in tests
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
  (+ the forecast exposure cap, the sticky forecast pause, sticky resolver
  blocks, and the `Ledger` that persists cumulative equity).
- `resolver_guard.py` — the post-Seoul failsafe: `ResolverCrossCheck` (pure
  24 h agreement logic), `ResolverRowCollector` + `ResolverPollerWorker` (one
  daemon thread per city), `ResolverBlockStore` (sticky divergence on disk).
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
- **The running max must come from the source the market resolves on.**
  `CityConfig.resolution_source` is `metar` for the 31 cities measured to agree
  with the resolver 100%, and `resolver` for **seoul + shenzhen**
  (`config.RESOLVER_SOURCED_CITIES`), whose METAR provably does not track it —
  they read the resolver's OWN closed rows (`wu_rows.jsonl` ->
  `TwcResolverFetcher` -> `metar_resolver.jsonl`). A resolver-sourced city with
  no key/table has NO valid source and idles rather than falling back to the
  METAR we know is wrong. Cost is the 26-66 min publish lag, affordable because
  sub-CAP dead-NO offers persist much longer in the thin markets (shenzhen
  median 108 min liftable, 57% >= 60 min).
- **No city trades without 24 h of resolver agreement** (`resolver_guard.py`).
  A per-city poller reads the resolver's OWN published table (TWC/WU location
  id in `CityConfig.resolver_location`, one verified entry per city in
  `config.RESOLVER_LOCATIONS`) into `data/<city>/wu_rows.jsonl`, pairs it with
  our observations by timestamp, and allows entries only in state `ok`: every
  paired ob in the trailing 24 h matched exactly, the matched history REACHES
  BACK `resolver_min_reach_hours` (23 h) and is fresh within
  `resolver_max_lag_hours` with no hole over `resolver_max_gap_hours`, and the
  local-day running maxima agree. Coverage is reach-back + freshness, never a
  *span*: the resolver publishes 26-96 min late, so a 23 h span inside a 24 h
  window is arithmetically unreachable and made every city flap. `unverified` (thin/stale data, or a city with no resolver table
  — Istanbul's LTFM serves none) withholds entries and self-clears.
  `divergent` is **sticky**: it survives ticks, rollovers and restarts
  (`data/_global/resolver_blocks.json`) and only
  `python -m dead_bracket.tools.resolver_clear <city>` lifts it. Fail closed is
  the point — no key, no rows, no trading. `resolver_confirm_dead` (off) adds
  the strict rule that the resolver's table must itself reach the running max,
  closing the 25–65 min publish-lag window at the cost of the speed edge.
- **METAR/resolver-only for RESOLUTION TRUTH.** Only the resolution-grade METAR
  running max may mark a bracket dead; decimal temps are rejected from
  `MetarFeed` (non-resolution). The sub-hourly national feeds (forecast path)
  and the HKO 1-min calibration feed are **signal/data-collection only**:
  they must NEVER reach a `MetarFeed`, a running max, the dead-bracket logic,
  or discovery. **One deliberate exception (re-enable plan, 2026-07-08):**
  under `--twc`, the CLOSED rows of the TWC timeseries — the literal table the
  market resolves on — feed the running max via `TwcResolverFetcher` →
  `metar_twc.jsonl`. That is not a sensor anticipating the resolver; it IS the
  resolver, which is the whole point (L1). The still-open current-hour row and
  every HKO/ZGSZ reading stay out of the trading path. A forecast entry may
  only ever *anticipate* what the resolver would confirm (NO-side below a
  predicted print) — never contradict it, and never bet YES/day-max (spec
  non-goal). Don't import/extend `weatherTrader/multipleCityPoller.py`.
  Shenzhen stays `no_trade` until the calibration acceptance gates AND the
  re-enable plan's shadow gates (L1 closed-row stability, end-of-day
  running-max-vs-WU-High agreement) pass — a human decision, see
  `shenzhen_calibration_requirements.md` and
  `data/shenzhen_reenable_plan_2026-07-08.md`.
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
  the total and per-city caps are that % of the sizing base, so bet sizes grow as
  the bot wins. The per-bracket cap is additionally held to `max_bracket_pct`
  (25%) of equity and every cap is clamped to available capital — the old
  all-in-on-one-bracket sizing is what made the Seoul false-dead total. The base is the
  **live wallet equity** (`balance.py::LiveBalanceTracker` — real USDC cash + open
  exposure, polled with an adaptive cadence: fast while a buy is being attempted,
  cooling down when consecutive reads are unchanged) in live mode, and the
  synthetic `initial_balance + CUMULATIVE realized_pnl` in paper
  (`RiskManager.set_equity_source` installs the live source; `BALANCE_POLL_*`
  knobs tune the cadence). Cumulative — not daily — is the fix for the bankroll
  resetting to its starting value at every rollover; `roll_day` still zeroes the
  daily PnL the loss breaker reads. Paper needs `--balance`. With `--bankroll 0`
  the static dollar caps (`MAX_EXPOSURE_*`) apply, but once a balance is known
  they are still clamped to it — a $100 paper balance can no longer "buy" $1000.
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
