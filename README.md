# Dead-Bracket NO Capture

A fresh, self-contained bot for Polymarket daily **"highest temperature"** markets.

Each integer-°C bracket is a YES/NO pair settling to $1. The day's high is
monotonically non-decreasing, so once the **confirmed running-max temperature**
(from the resolution-grade airport METAR) passes a bracket, that bracket can
never be the high — its NO is certain to settle at $1.

**Strategy:** when a dead bracket's NO is offered below 98¢, buy it (IOC taker);
immediately rest a 99.9¢ maker sell to recycle capital; if it doesn't fill, hold
to resolution ($1). All cities run **concurrently** in one process.

This is *capture-if-it-appears*: sub-98¢ dead-NO asks are expected to be rare, so
the system sits idle cheaply, acts fast when one appears, and logs every
opportunity. **Default mode is paper** — no live orders until explicitly enabled.

> ⚠️ **Empirical status (see [FINDINGS.md](FINDINGS.md)):** across every day
> measured, dead brackets are already priced to ~$0.997–0.999 by the time our
> resolution METAR confirms them, because the market leads the resolution feed by
> minutes (via the 6-min feed / nowcasting). The sub-98¢ capture window the
> strategy assumes has **not** appeared. The bot is correct and safe, but the
> *edge as specified does not exist on the data so far.* Read FINDINGS.md before
> assuming this makes money.

**Cities (concurrent):** Paris, London, Moscow, Milan, Madrid, Ankara, Istanbul,
Helsinki, Amsterdam, Warsaw, Munich. Each has its own resolution station and IANA timezone;
the running max **resets at the city's local midnight**, and a reading from one
station can never fold into another city (hard ICAO isolation). All station/tz
pairs are confirmed against the live markets — see [LIVE.md](LIVE.md).

> METAR/resolver-only **for resolution truth**: only the resolution METAR's
> running max can mark a bracket dead. The **forecast path** (below) adds
> sub-hourly national feeds as *signal-only* inputs — they accelerate NO-side
> entries probabilistically and never touch the running max or discovery.

## Forecast path (probabilistic NO-side, off by default)

`--forecast` enables a second entry path built on the research in
[docs/](docs/): national met services publish sub-hourly readings (6-min
Météo-France, 10-min KNMI/FMI/JMA, 1-min NEA) from the same or co-located
sensors that produce the resolver METARs — a 10–30+ min information lead. The
fitted trust-decay model ([docs/trust_decay_formula_2026-07-02.md](docs/trust_decay_formula_2026-07-02.md))
turns a reading with margin `m` at lead `τ` into a win probability p̂ for
buying NO on brackets below the predicted print:

- **Entry**: only when `p̂ − ask − fee(ask) ≥ forecast_min_edge` (3¢ default);
  the IOC limit is `p̂ − fee − edge` — a hard margin between forecast and price.
- **Sizing**: fractional Kelly (`kelly_fraction`, default **quarter**, hard
  ceiling **half**) of the current balance, capped per feed ob (25% — all
  brackets from one reading are ONE bet), across cities (40%), per bracket/
  city/total as usual.
- **Guards**: ≥0.4 °C margin (METAR rounds), p̂ ≤ 0.99, no entries 17–21h
  local (evening drift kills the edge), stale-feed cutoff, feed-vs-METAR
  divergence trip, and a **sticky global pause on any settled forecast loss**.
- **Rollout**: paper-trades from day one; in live mode signals are logged but
  orders need the explicit `--forecast-live` go/no-go, informed by
  `python -m dead_bracket.tools.forecast_calibrate`.

Feed cities: paris (Météo-France, needs `MF_API_KEY`), amsterdam (KNMI, needs
`KNMI_API_KEY`), helsinki (FMI), tokyo (JMA AMeDAS), singapore (NEA). A missing
key just leaves that city METAR-only (one startup notice).

## Layout

```
src/dead_bracket/
  config.py      # all knobs (spec §10); defaults match the spec
  models.py      # Bracket, MarketSnapshot, Intent, Fill, Position
  fees.py        # fee(a) / net(a) (spec §2)
  metar.py       # resolution feed: parser + monotonic running-max + revision guard (§6, §9)
  market.py      # market feed interface (injected snapshot_fn) (§6)
  feeds_http.py  # the ONLY network code: aviationweather + Gamma + batched CLOB books
  strategy.py    # entry (§3) and exit (§4) — pure logic
  forecast.py    # forecast path: p_trust model, Kelly sizing, ForecastStrategy, divergence guard
  forecast_feeds.py  # FeedCollector (feed.jsonl + latest-reading holder) + FeedPollerWorker threads
  risk.py        # caps (incl. per-bracket $ + forecast caps), kill-switch, daily-loss, per-city halts (§9)
  executor.py    # PaperExecutor (default) + LiveExecutor (polymarket-client SDK) (§7)
  reporter.py    # strict console policy + telemetry files (§8, §11)
  worker.py      # CityWorker: one city's cycle, restart-safe (§5)
  supervisor.py  # runs cities concurrently, isolated (§5)
  lfs_calibration.py  # Shenzhen/Lau Fau Shan calibration collectors + WU-prediction shadow
  tools/lfs_calibrate.py  # offline K1/K2/K3/ε analysis -> calibration report
  tools/forecast_calibrate.py  # offline forecast report: sensor σ, Brier, hold rate, paper PnL
  main.py        # CLI entry point
tests/           # pytest suite; ≥90% on strategy/risk/execution (§12)
```

## Install & run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"        # add ".[live]" for signed execution

# Paper mode (default): ALL configured cities concurrently
python -m dead_bracket.main

# Subset of cities / data dir ("--cities all" is the explicit alias for default)
python -m dead_bracket.main --cities paris,london --data-dir data

# Silence the stderr progress stream (METAR readings, status, notices)
python -m dead_bracket.main --quiet

# Live (Phase 2) — gated on a non-zero bankroll % AND a configured wallet.
# --bankroll is a PERCENT of the wallet balance (here, all-in: 100%).
POLY_PRIVATE_KEY=... python -m dead_bracket.main --live --bankroll 100

# Paper, percentage sizing — supply the starting balance explicitly:
python -m dead_bracket.main --bankroll 50 --balance 30 --cities london

# Forecast path, paper (Kelly sizing needs an explicit balance):
python -m dead_bracket.main --forecast --balance 100 --cities paris,helsinki,tokyo,singapore

# Forecast live orders — HUMAN go/no-go, only after the calibration gates pass:
POLY_PRIVATE_KEY=... python -m dead_bracket.main --live --bankroll 40 --forecast --forecast-live

# Shenzhen calibration run (data collection + prediction shadow, no orders):
TWC_API_KEY=<key> python -m dead_bracket.main --hko --no-trade shenzhen --calibrate

# Shenzhen L1 shadow (re-enable plan): running max from the resolver's own
# CLOSED rows + the L2 lock-ahead shadow. Still no orders (--no-trade).
# TWC_API_KEY can live in a git-ignored .env at the repo root instead:
python -m dead_bracket.main --twc --no-trade shenzhen --calibrate

# Offline analysis of the collected calibration data (run any time):
python -m dead_bracket.tools.lfs_calibrate --data-dir data --city shenzhen
python -m dead_bracket.tools.forecast_calibrate --data-dir data
```

All behaviour is config (spec §10). Scalar knobs can be overridden by an
environment variable of the same upper-case name, e.g. `CAP=0.97`,
`MIN_NET=0.02`, `MAX_TOTAL_EXPOSURE=2000`.

## Console output policy

The console prints **only**:

```
BOUGHT  paris  24C NO  x300 @ 0.973
SOLD    paris  24C NO  x300 @ 0.999
```

…plus a single `HALT: <reason>` line if the kill-switch trips. **stdout stays a
pure trade stream.**

Human-readable **progress** goes to **stderr** (on by default, `--quiet` to
silence): per-METAR readings, a periodic per-city `STATUS` line, discovery, and
`NOTE` lines when a market isn't resolving / a book is unavailable. Example:

```
METAR   paris     17C   high 17C
NOTE    london    no market data (event not resolved or book unavailable)
STATUS  paris     high 24C  obs 15:30Z   open $480/$500 (500sh, 3pos)  avail+$620  tot $480/$1000  ok
```

The `no market data` NOTE now fires **only** for a genuinely vanished market
(undiscovered event / no resolvable brackets). A transient batched-`/books`
failure (a CLOB rate-limit or network blip) no longer flips it — that tick is
skipped silently and recorded as `book_fetch_transient` in `events.jsonl`. Book
reads are **batched into one POST per city per tick** and scoped to the
**frontier band** around the running max (`book_band_below` dead brackets below
+ `book_band_above` live brackets above, default 1/2, widened to cover
`price_sample_band`) plus any bracket still held — so ~33 cities no longer hammer
the per-token CLOB endpoint into the rate limit (which previously flapped the
notice all day).

The `STATUS` line tracks **open volume**: capital currently deployed in held
positions for that city vs the per-city cap (`open $deployed/$cap`), the open
share count and number of positions, the portfolio `tot $deployed/$cap`, and
**`avail+$N`** — the dollar volume of liftable dead-NO that your bankroll caps
*blocked* today (today's peak; omitted when zero). So `avail+$620` means there
was $620 more capturable in the market that the caps stopped you from taking —
i.e. roughly how much raising the bankroll would have let you deploy.

The full structured record of everything (discovery, opportunities not taken,
warnings, per-tick state) goes to per-city, **per-day** append-only logs under
`data/<city>/<YYYY-MM-DD>/*.jsonl` (the date is the city's local day, matching
the running-max reset). Two of these study the "no edge" problem with minimal
disk: `dead.jsonl` records the NO book the instant each bracket first goes dead
(one row per bracket per day), and `prices.jsonl` is a sparse frontier-price time
series taken only inside each publication hot window (`price_sample_sec`, default
15s; `price_sample_band` brackets at/above the running max) — together they show
whether the market reprices a soon-to-be-dead bracket toward $1 *before* our METAR
lands, and how late our feed is. Set `PRICE_SAMPLE_SEC=0` to turn the sampler off.

## Shenzhen (Lau Fau Shan) calibration & the WU-prediction shadow

Shenzhen resolves on WU's table for **HKO AWS 45035 (Lau Fau Shan)** — not a
METAR: TWC resamples HKO sub-hourly data into hourly rows (see
[docs/wu_lfs_discrepancy_2026-07-03.md](docs/wu_lfs_discrepancy_2026-07-03.md)).
Three unknowns gate re-enabling the city
([shenzhen_calibration_requirements.md](shenzhen_calibration_requirements.md)):
**K1** TWC's sample grid + ingestion latency, **K2** the 0.1 °C → integer
rounding rule, **K3** whether the daily High counts all ingested row versions
or only each hour's final value. `--calibrate` (or `LFS_CALIBRATION=1`) starts
three collectors + a prediction shadow for every city with an HKO resolution
station (i.e. Shenzhen). Pure data collection: Shenzhen stays `no_trade`, and
nothing touches strategy/risk/orders. Files land in `data/shenzhen/` (not
per-day):

- `hko_1min.jsonl` — HKO's "latest 1-minute" regional CSV, Lau Fau Shan row
  only, deduped on the observation minute. (**Measured 2026-07-07:** despite
  the name, observations step in ~10-min increments, published ~7–9 min after
  the observation minute — the analysis derives the grid from the data.)
- `twc_rows.jsonl` — every distinct **row version** from TWC's timeseries
  (pressure/rh are the change detectors when temp is flat). The
  api.weather.com cache is `s-maxage=300` and 400s on unknown params, so the
  fetch cache-busts by **rotating `hours=2..7`** (each value is its own cache
  entry; a repeat only comes around after the TTL). `transaction_id` repeats
  are counted as `stale_responses`, and a payload whose newest row is older
  than 3 h is **rejected outright** (the API has served days-old cached
  responses; rejected payloads never reach this file, which `--twc` treats as
  resolver truth).
- `lock_ahead.jsonl` — the **L2 lock-ahead shadow** (re-enable plan gate 3):
  a `lock` row whenever an HKO reading in [h:35, h:55] clears an integer with
  ≥ `LOCK_AHEAD_MARGIN_C` (default 0.3), and a `score` row once the hour
  closes (+5 min grace) — did the row finalize at/above the locked value? The
  gate is ≥50 scored events at ≥98% hold rate.
- `twc_daily.jsonl` — the previous HKT day's final table, captured at 00:30
  and 09:00 HKT (`TWC_DAILY_HKT`), rows + any `max_temp` summary fields.
- `predictions.jsonl` / `prediction_eval.jsonl` — the **prediction shadow**:
  our best-guess of what WU will publish (row value + daily High) from the
  1-min feed under the configured rounding rule (`LFS_PREDICT_ROUNDING`,
  default `half_up`), then one eval row per actual WU publication scoring
  predicted vs printed. The hit rate is direct evidence for/against the
  K1/K2/K3 assumptions before any capital touches them.
- `collector_health.jsonl` — cumulative poll/failure counters (the <1%-missed
  acceptance criterion) + per-collector `stalled` flags. Failure counters are
  blind to **source-side** gaps (a 90-min hko outage once logged `failures: 0`
  — the CSV kept serving, just nothing new), so the worker also raises a
  `staleness_alarm` event/notice when a collector produces no NEW record for
  25 min (hko_1min) / 75 min (twc_rows), and a `staleness_recovered` when it
  resumes.

The TWC endpoints need `TWC_API_KEY` (the public wunderground.com frontend key
works); without it only the HKO collector runs. The key can live in a
git-ignored `.env` at the repo root (`KEY=VALUE` lines, loaded at startup;
real environment variables always win). Collector status goes to
stderr/`events.jsonl` — stdout stays the pure trade stream.

### `--twc`: the L1 resolver feed (Shenzhen re-enable plan)

`--twc` switches a TWC/WU-resolved city's running max onto the **closed rows
of the resolver's own timeseries** — the literal content the market settles
on, so there is no station/sampling/rounding seam left at all. Mechanics: the
calibration worker (required: `--calibrate` + `TWC_API_KEY`) is the process's
single TWC poller and writes `twc_rows.jsonl`; a file-driven
`TwcResolverFetcher` replays each hour's latest version **only after the hour
closes** as a synthesized METAR (`metar_twc.jsonl`, temp = TWC's own integer)
into the standard `MetarFeed` → running max → dead-bracket path. The
still-revising current-hour row can never mark a bracket dead (the July 8
near-miss class). If the poller goes quiet past `TWC_RESOLVER_STALE_SEC`
(default 90 min) the feed reports stale and the city halts. `--twc` takes
precedence over `--hko`; under it, ZGSZ METAR and HKO rhrread are **out of the
trading path entirely** (`metar_hko.jsonl` is a deprecated trading source —
it samples h:00 and disagrees with WU on ~45% of rows; HKO 1-min data remains
as calibration/forecast signal only). Rollout per
`data/shenzhen_reenable_plan_2026-07-08.md`: run `--twc --no-trade shenzhen`
for 3–5 shadow days, check the calibration report's **L1 closed-row
stability** and **L2 lock-ahead** sections, then dropping `--no-trade` is the
operator's call.

`python -m dead_bracket.tools.lfs_calibrate` writes
`data/shenzhen/calibration_report_<date>.md`: K1 join (documented logic +
latency percentiles + sampled-minute histogram), K2 rule inference with x.5
enumeration, K3 per-day comparison of WU High vs max(final rows) / max(all
versions) / max(1-min rounded), the ε spike-never-sampled rate
(numerator/denominator, not estimated), the prediction scorecard, the
acceptance checklist, and a Tier-1 (δ, m) recommendation — or "insufficient
data" with the open gates named. **Re-enabling Shenzhen stays a human
decision.**

## Safety (spec §9)

- Trades a bracket only when `temp < running_max` from the **resolution METAR**.
- **Downward-revision guard:** a `COR` that lowers the max never un-deads an
  acted bracket but halts new entries for that city.
- **Caps:** per-bracket shares **and dollars** (`MAX_EXPOSURE_PER_BRACKET`),
  per-city / total dollar exposure, `MAX_FRAC_OF_DEPTH` (max share of displayed
  depth ≤CAP one take lifts; default **1.0** = take the whole book, lower leaves a buffer),
  `MAX_DAILY_LOSS` → kill-switch. `--bankroll` is a **percent of current balance**
  (`bankroll_pct`): total = per-city = per-bracket = that % of `balance +
  realized_pnl` (all-in on conviction), so bets grow as the bot wins. `--bankroll
  0` falls back to the static `MAX_EXPOSURE_*` dollar caps. Exposure frees on
  every sell fill and resets at the daily rollover.
- **SPECIs:** off-cycle special reports are included by default.
- **Kill-switch** on stale METAR, repeated order errors, crossed/invalid book,
  or a daily-loss breach.
- **Halts are per-city.** Order errors, a stale feed, a crossed/invalid book,
  and a downward revision pause **only that city** — the others keep running.
  Only a **daily-loss breach** trips the one global kill-switch (the single
  `HALT` console line). Per-city pauses go to the stderr progress stream and
  `events.jsonl`, not stdout. In paper, a crossed book must persist
  `CROSSED_BOOK_GRACE_TICKS` (default 3) ticks before pausing the city and
  auto-recovers after `CROSSED_BOOK_RECOVER_TICKS` (default 3) healthy ticks.
- **Runs unattended across days.** Each city discovers the new day's market at
  its local midnight, the running max resets, and at `ROLLOVER_TZ` midnight
  (default UTC) the supervisor settles positions and re-arms risk (zeroing the
  daily-loss budget and clearing per-city pauses). `DAILY_REARM=false` disables.
- **Discovery only ever tracks *today's* event** (matched by local date in the
  slug). It will idle rather than fall back to tomorrow's forecast market — a
  past bug where it tracked the next day's event made dead-detection compare
  today's METAR against tomorrow's prices. **Next-day scanning is per-city:** each
  city re-discovers at *its own* local midnight (timezones differ), so no city's
  00:00 rollover is missed; if the new day's market isn't open yet the city idles
  (drops the stale event) and keeps retrying until it appears.
- **Resolution-source cross-check.** Before trading a city, the discovered
  event's text must name BOTH the station ICAO and **Weather Underground**
  (`wunderground`) — confirmation that the market resolves on *our* station and
  source (a wrong source turns a false dead bracket into a real loss). A market
  that doesn't confirm is idled, not traded. Two stations waive ONLY the Weather
  Underground half because their markets resolve off a non-WU source — **Moscow
  (UUWW)** and **Istanbul (LTFM)** — but they still must name their station ICAO
  (the station match is never waived)
  (`WUNDERGROUND_EXEMPT_ICAOS` in [feeds_http.py](src/dead_bracket/feeds_http.py)).
- **Live gating:** requires `--live` AND a non-zero bankroll; default paper.
- **Secrets:** the wallet key lives only in the executor via the environment;
  never logged, never in config or telemetry.

## Tests

```bash
pytest                                  # full suite
pytest --cov=dead_bracket --cov-report=term-missing
```

No live network in tests (feeds/CLOB are mocked with fixtures). The suite
includes an end-to-end paper run over a recorded book sequence with
deterministic BUY/SELL output and PnL. CI enforces ≥90% line coverage on the
strategy/risk/execution modules.

## Status

Phase 1 (paper) is implemented and green (272 tests). Phase 2 (live) is wired:
`LiveExecutor` places IOC-emulated buys + `post_only` GTC sells via the unified
**`polymarket-client`** SDK (`SecureClient`) and reconciles fills. (The archived
`py-clob-client` no longer works — the venue rejects its orders with "invalid
order version".) Live is gated behind `--live` + a non-zero bankroll; the wallet
needs `POLY_PRIVATE_KEY` and `POLY_FUNDER` — see [LIVE.md](LIVE.md).

**But note the empirical finding above / in [FINDINGS.md](FINDINGS.md):** the
dead-NO capture edge has not materialised on the data so far. Run `--check` and a
long paper stretch, and read FINDINGS.md, before risking real money.
