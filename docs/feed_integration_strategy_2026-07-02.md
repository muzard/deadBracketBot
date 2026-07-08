# Strategy: plugging early-reading feeds into deadBracketBot

Builds on: `station_audit`, `early_reading_reliability`, `subhourly_feed_openings`, `trust_decay_formula` (all 2026-07-02, this folder). Move these into the repo's `docs/` and link from the README when implementing.

## Architecture (three new layers, one fix)

```
[Feed pollers]──feed.jsonl──▶[Signal engine]──signals.jsonl──▶[Sizer/Gater]──▶ existing order path
[METAR poller] (existing)──────────▲                                              ▲
[Resolver poller] (TWC timeseries)─┴── resolver-truth running_max ────────────────┘
```

Principles: every layer logs before it acts; trading logic consumes *resolver truth*, never raw feed values; feeds only ever *accelerate* a decision the resolver would eventually confirm.

## Phase 0 — Resolver truth (do first, independent value)

1. New poller per city: `api.weather.com /v1/location/{ICAO}:9:{CC}/observations/timeseries.json?hours=2`, every 5 min. Append to `resolver.jsonl` (`obs_id, valid_time, temp`).
2. `running_max_resolver` computed from it; the dead-bracket rule becomes `bracket < running_max_resolver` (METAR rmax stays as a leading indicator only).
3. Weekly audit job: assert `obs_id == ICAO` per city, else auto-add to `no_trade` and alert. This makes Shenzhen-class faults structurally impossible.
4. On `is_correction` or a resolver value revised downward: re-evaluate all open positions for that city-day; if a position's kill condition no longer holds, exit at market and log `event: unwind`.

**Gate to Phase 1:** 7 days of resolver.jsonl across all cities; measured ε (resolver drop rate of METAR obs) replaces the 1–2% guess in the p formula.

## Phase 1 — Feed layer, shadow mode (no trading)

Wire Tier 1 feeds only: paris/Météo-France 6-min, amsterdam/KNMI 10-min, helsinki/FMI 10-min, tokyo/JMA AMeDAS 10-min, singapore/NEA 1-min.

Config `feeds.yaml` per city:
```yaml
paris:
  source: meteofrance_paquet_obs
  station_id: <RADOME id for Le Bourget>   # resolve during implementation
  cadence_s: 360
  auth: MF_API_KEY
  sensor_sigma: null        # unknown until calibrated
  enabled: shadow
```

Schema `feed.jsonl`: `{ts_recv, src, obs_time, temp_c}` (tenths). Log receive latency (`ts_recv − obs_time`) — it is the product.

Calibration jobs (daily, per station):
- **Sensor offset/noise:** pair each feed ob with the nearest METAR instant → `bias`, `sensor_sigma`. Accept station if |bias| ≤ 0.3 °C and sensor_sigma ≤ 0.3 °C; else demote to Tier 2 handling (inflate σ in formula).
- **Trust-parameter refit:** rerun the σ₃₀/H/drift/ρ fit per station-month (3-line job on metar.jsonl); overwrite `PAR` table.
- **End-to-end check:** P(resolver max ≥ T | feed ≥ T + m) counted directly, binned by τ and hour — this is the number the formula predicts; compare (Brier score).

**Gate to Phase 2:** ≥14 days per station, sensor_sigma accepted, formula Brier ≤ 0.02 vs counted outcomes.

## Phase 2 — Paper trading

Signal engine, on every feed ob `x` at local time t:

```
for B in brackets(city, day) with ask a still quoted:
    T = B + 1                        # METAR temp that kills bracket B
    m = x − T                        # tenths margin (can be negative → skip)
    τ = minutes to next METAR sampling instant
    p = p_trust(τ, m, hour=t, sensor_sigma) × (1 − ε_measured)
    if convective_flag(city): p = min(p, 0.95)
    if 17 ≤ t.hour < 21: skip        # evening cutoff (drift −1 °C/h kills edge)
    f = quarter_kelly(p, a)          # f* = p − (1−p)·a/(1−a), /4
    if p ≥ 0.97 and f·bankroll ≥ min_order and edge p − a ≥ 0.03:
        log signal {city, B, p, a, f, depth, τ, m}
```

Paper-fill against logged book depth (dead.jsonl already captures ask/depth). Track: realized hold rate vs p̂ (calibration), paper PnL, how often depth < desired stake, and how often the market repriced before the METAR published (measures whether anyone else has the feed).

**Gate to Phase 3:** ≥100 signals, realized hold rate within CI of p̂, paper EV positive after 5% taker fee (note `feeType: weather_fees`, rate 0.05 taker — this eats ~5¢ per share and must be inside the edge test: require `p·1 − a − fee(a) > 0`).

## Phase 3 — Live, capped

Sizing and hard limits (encode in config, not code):
- Quarter-Kelly on p̂, with p̂ capped at 0.99 (unconfirmed) / 0.995 (ob confirmed by second reading).
- **Same-ob cap:** all brackets triggered by one feed ob = one bet, ≤ 25% bankroll.
- **Same-day cap** across correlated cities (shared synoptic pattern; cluster Europe / China coast / SE-Asia): ≤ 40% bankroll.
- Check balance before sizing (July 1 order_errors were balance rejections).
- Per-city enable list starts: paris, amsterdam, helsinki (best calibration sample) → add tokyo, singapore after their gates pass.

Kill switches (auto `trade_disabled`):
- Any settled loss on a feed-triggered bet → global pause + post-mortem (at p̂ ≥ 0.97 a single loss is a model/mapping bug until proven otherwise).
- Feed-vs-METAR divergence > 1.0 °C on 2 consecutive pairs → disable that feed (sensor/station drift).
- Resolver obs_id changes, or resolver silent > 3 h → city to no_trade.
- NEA-style feed outage: stale feed (> 2× cadence) → signals off for that city, METAR path continues.

## What NOT to build yet

- Tier 2 feeds (MGM, KMA, CWA, IMS, DWD): only after Tier 1 is profitable; each needs its own sensor-identity calibration and some are scrapes that will break.
- China cities: no feed exists; they stay on the METAR path with resolver truth.
- Any YES-side or forecast-based betting — different risk class, out of scope for a dead-bracket bot.
- Shenzhen stays disabled unless rebuilt on the resolver feed (station 45035) — and then its "METAR" path must be removed entirely.

## Timeline sketch

Week 1: Phase 0 + feeds.yaml + Météo-France/KNMI/FMI keys. Week 2–3: shadow + calibration. Week 4: paper. Week 5+: live at quarter-Kelly on 3 cities, review weekly against the gates. Every gate is a measurement, not a date — if a gate fails, the phase repeats with the fix.

## Success metrics

- Latency: feed signal → order submitted < 5 s; information lead vs METAR publication ≥ 10 min median.
- Calibration: |realized hold − p̂| within binomial CI monthly.
- Growth: log-bankroll growth per city-day vs the quarter-Kelly expectation (~0.1–0.6%/bet); if realized < half of expected over 30 days, the market is pricing the feed already — cut stakes, investigate.
