# Requirements: Shenzhen calibration data collection

Goal: collect the data needed to re-enable Shenzhen trading. The market resolves on WU's
table for HKO AWS station 45035 (Lau Fau Shan), which is NOT a METAR feed — TWC resamples
HKO sub-hourly data into hourly rows (see `data/wu_lfs_discrepancy_2026-07-03.md`,
`data/lfs_1min_edge_plan_2026-07-03.md`, `data/wu_metar_fidelity_2026-07-04.md`).
Three unknowns gate trading; this work measures them:

- **K1** — TWC's sample grid (which HKO minutes end up in a row) + ingestion latency.
- **K2** — TWC's rounding rule from HKO 0.1 °C to WU integer °C (incl. x.5 behavior).
- **K3** — whether WU's daily "High Temp" = max over ALL ingested row versions, or only
  each hour's FINAL row value. **This decides if intra-hour spikes count for resolution.**

## Scope

Data collection + offline analysis only.

- Shenzhen stays in `no_trade`. No strategy, risk, executor, or order-path changes.
- Existing feeds for all other cities untouched.
- One small fix allowed: the synthesized raw in `metar_hko.jsonl` writes temp into the
  dewpoint slot (`"26/26"`); write a proper placeholder (e.g. `26/XX`) so audits stop
  chasing it.

## Component 1 — HKO 1-minute collector

- Source: `https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_1min_temperature.csv`
  (confirmed live 2026-07-03; served as `application/octet-stream, text/csv`).
- **First task: verify the actual schema and update cadence** — expected columns are
  datetime (`yyyyMMddHHmm`, HKT), station name, air temp at 0.1 °C, but this was not
  render-verified. Handle BOM, odd content-type, and station-name variants
  (`Lau Fau Shan`).
- Poll every 60 s (configurable). Extract the Lau Fau Shan row only.
- Append to `data/shenzhen/hko_1min.jsonl`: `{fetch_ts, obs_time_utc, obs_time_raw, temp_c}`.
  Dedupe on `obs_time_raw` — write only new observation minutes.
- HKO datetimes are HKT (UTC+8). Store UTC ISO-8601 AND the raw string.

## Component 2 — TWC row-version collector

- Source: `GET https://api.weather.com/v1/location/ZGSZ:9:CN/observations/timeseries.json?apiKey=<KEY>&units=m&hours=2`,
  poll every 60 s. Key via env `TWC_API_KEY` (the public wunderground.com frontend key
  works). Verify responses aren't served from a stale cache (compare
  `metadata.transaction_id` across polls; add a cache-bust query param if needed —
  the batched METAR fetcher already does this pattern).
- For each observation, form the version tuple
  `(valid_time_gmt, temp, dewPt, rh, pressure, wdir, wspd, gust, precip_total)`.
- Append to `data/shenzhen/twc_rows.jsonl` **only when the tuple differs** from the
  last-seen version for that `valid_time_gmt`: `{first_seen_ts, valid_time_gmt, ...tuple}`.
  This captures intra-hour row revisions; pressure (0.1 hPa) and rh are the change
  detectors when temp is flat.

## Component 3 — daily summary capture (K3)

- Once daily at 00:30 HKT and again at 09:00 HKT (revision window guard), fetch
  `historical.json?startDate=<prev HKT day>&endDate=<same>` for `ZGSZ:9:CN`.
- Append to `data/shenzhen/twc_daily.jsonl`: all final rows + any `max_temp`/`min_temp`
  summary fields, with fetch ts.

## Component 4 — offline analysis script

Run on demand (`python -m dead_bracket.tools.lfs_calibrate` or similar), reads the three
jsonl files, writes `data/shenzhen/calibration_report_<date>.md` containing:

1. **K1**: for each TWC row version, the matching 1-min reading(s) (match on rounded temp
   + nearest time; document join logic and ambiguity handling) → histogram of sampled
   minutes, latency p50/p95, % of versions matched.
2. **K2**: table of (0.1 °C matched reading → WU integer) pairs; inferred rounding rule;
   list every x.5 case observed.
3. **K3**: per HKT day: WU daily max per `historical.json` vs `max(final rows)` vs
   `max(all row versions)` vs `max(1-min readings, rounded per K2)`. State which matches.
4. **ε**: fraction of 1-min integer-boundary crossings that never appear in any row
   version (the "spike never sampled" rate).
5. Recommended Tier-1 parameters δ (margin) and m (sustain minutes) per the edge plan,
   or "insufficient data".

## Repo constraints (non-negotiable, from CLAUDE.md)

- All network code lives in `feeds_http.py` (collectors injectable; parsers pure).
- NO live network in tests. Unit-test parsers, dedupe, revision detection, and the
  analysis join against fixture files. Keep the ≥90% coverage gate green.
- stdout stays a pure trade stream — collector status goes to stderr / per-day logs.
- Collectors must run unattended for days: retry with backoff, log gaps, never crash the
  city worker; a failed poll skips the tick.
- Config: poll intervals and an enable flag (e.g. `LFS_CALIBRATION=1`) in `config.py`
  with UPPERCASE env overrides. Default OFF for other cities, ON only for shenzhen.
- Update README.md (and CLAUDE.md if behavior notes change) to document the new
  collectors, files, and how to run the analysis.

## Acceptance criteria (go/no-go for re-enabling Shenzhen)

- ≥3 (target 5) full HKT days logged, <1% missed polls per collector per day.
- K1: ≥95% of row versions matched; sample grid + latency distribution stated.
- K2: rounding rule exact on ≥30 pairs, zero contradictions; x.5 cases enumerated (or
  explicitly "none observed → rule undetermined at boundary, keep δ ≥ 0.5").
- K3: answered consistently across all logged days.
- ε: measured with count numerator/denominator, not estimated.
- Report written; re-enable decision itself is the user's, not the bot's.

## Non-goals

- No trading on 1-min data yet (Tier-1 gated on this calibration; Tier-2 forecasting is
  a spec non-goal — do not build it).
- No changes to METAR cities: WU is a verbatim METAR copy there (44/44 exact,
  `wu_metar_fidelity_2026-07-04.md`); this apparatus is Shenzhen-specific.
- No Météo-France-style non-resolution feeds; the 1-min CSV is allowed ONLY as the
  Shenzhen resolution-source's own upstream (HKO), logged for calibration.

## Verify-first list (cheap checks before building)

1. Exact 1-min CSV schema, cadence, and publication latency (curl it, watch 5 min).
2. TWC timeseries cache behavior at 60 s polling.
3. Whether hourly `rhrread` values coincide with the 1-min CSV at :00 (sanity link
   between existing `metar_hko.jsonl` and the new feed).
