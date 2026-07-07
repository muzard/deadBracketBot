# CLAUDE.md — Dead-Bracket NO Capture bot

Standalone Python bot for Polymarket daily **"highest temperature in <city>"**
markets. Each integer-°C bracket is a YES/NO pair settling to $1. Once the
resolution-grade airport METAR's running max passes a bracket, that bracket can
never be the high, so its NO is certain → buy the NO if offered <98¢ (IOC), then
rest a 99.9¢ maker sell to recycle capital; else hold to $1.

## Read these first
- **[FINDINGS.md](FINDINGS.md)** — the dead-NO edge is **real in thin markets**
  (Moscow, now Shenzhen): a week of daily winners, **+$49.72**, zero losers. It is
  **absent in liquid markets** (Paris: dead NO pre-priced to ~$1 before our METAR
  confirms) — so don't expect it everywhere, and it's a small, concentrated sample.
  In the thin markets we're **bankroll-limited, not liquidity-limited** (Shenzhen:
  ~11× the depth we took sat unlifted at/below CAP, all blocked by `size/caps`).
  Still don't "fix" the bot to chase forward/YES-side mispricings — that's
  directional and a spec non-goal.
- **[README.md](README.md)** — architecture, console policy, how to run.
- **[LIVE.md](LIVE.md)** — live runbook (wallet creds, caps, validation).
- The original spec is `dead_bracket_no_strategy_spec.md` (in ~/Downloads).

## How to run / test
```bash
. .venv/bin/activate                       # use the project venv (macOS: python3)
python -m dead_bracket.main                # paper, ALL 10 cities (the default)
python -m dead_bracket.main --check        # live preflight: creds/balance, no orders
python -m dead_bracket.main --live --bankroll 40   # 40% of wallet balance; needs POLY_PRIVATE_KEY + POLY_FUNDER
TWC_API_KEY=<key> python -m dead_bracket.main --hko --no-trade shenzhen --calibrate  # + Shenzhen calibration collectors
python -m dead_bracket.tools.lfs_calibrate # offline K1/K2/K3/ε report from collected data
python -m pytest                           # 272 tests; NO live network in tests
```
Coverage gate (CI): ≥90% line on strategy/risk/executor/worker. Keep it green.

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
- `strategy.py` — pure: entry sizing + the 99.9¢ exit. `risk.py` — caps + halts.
- `executor.py` — `PaperExecutor` (default sim) / `LiveExecutor`. `worker.py` —
  the per-city cycle. `reporter.py` — stdout=trades, stderr=progress, per-day logs.

## Non-negotiable invariants (each has tests + a memory note — don't regress)
- **METAR-only.** No Météo-France / 6-minute feed anywhere. Decimal temps are
  rejected (non-resolution). Don't import/extend `weatherTrader/multipleCityPoller.py`.
  ONE scoped exception (`shenzhen_calibration_requirements.md`): the HKO 1-min CSV
  + TWC endpoints in `lfs_calibration.py` are the Shenzhen resolution source's own
  upstream/output, collected under `--calibrate` for calibration + the
  WU-prediction shadow ONLY — they never reach a `MetarFeed`, a running max, or
  the order path, and Shenzhen stays `no_trade` until the calibration report's
  acceptance gates pass (a human decision).
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
  every cap (total = per-city = per-bracket) is that % of `initial_balance +
  realized_pnl`, so bet sizes grow as the bot wins (all-in on a single dead
  bracket). Live reads the wallet balance at startup; paper needs `--balance`.
  With `--bankroll 0` the static dollar caps (`MAX_EXPOSURE_*`) apply instead.
- Keep the network layer in `feeds_http.py`; strategy/risk/executor stay pure and
  unit-testable (inject feeds/clients via fixtures).
- The assistant implements/configures live trading but does **not** place orders
  or move funds — the user runs `--live` and does the validation trade.

## Operational gotchas
- macOS: the command is `python3` (or activate `.venv`), not `python`.
- **Keep the Mac awake** (`caffeinate -i …`) — if it sleeps, polling stops and
  report windows are missed; restarts also re-print a stale first METAR reading.
