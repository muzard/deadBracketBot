# Lau Fau Shan: bot HKO feed vs WU — discrepancy explained (2026-07-03)

Same station (HKO 45035 Lau Fau Shan), **different sampling**. Not a bug in either value.

- **Bot feed** (`metar_hko.jsonl`): HKO `rhrread` top-of-hour instantaneous reading, stamped with HKO's `recordTime`. Verified live: HKO published the 20:00 HKT (12Z) reading at 20:02, value 27 — bot logged 27 at 12Z. Labels and values are HKO-correct.
- **WU / TWC** (`api.weather.com` timeseries, obs_id 45035): the row labeled HH:00 is filled from HKO's **sub-hourly AWS data** and its final value reflects a reading **late in hour HH** (~HH:50). The row appears ~13 min after the hour and evidently updates within the hour.

## Evidence (Jul 2 17Z – Jul 3 12Z, all 20 overlapping hours)

| Hr (Z) | bot/HKO @HH:00 | WU row HH | fits "WU = late-in-hour"? |
|---|---|---|---|
| 17–21 | 26 | 26 | ✓ flat |
| 22 | 26 | 27 | ✓ (next hr 27) |
| 23 | 27 | **29** | ✓ (between 27→30) |
| 00 | 30 | 31 | ✓ (next hr 31) |
| 01 | 31 | 30 | ✓ |
| 02 | 30 | 30 | ✓ |
| 03 | 30 | 31 | ✓ |
| 04 | 31 | **32** | ✓ (bot saw 32 only at 05Z) |
| 05 | 32 | 30 | ✓ |
| 06 | 30 | **26** | ✓ squall crash (gust 47 km/h, 1.5 mm) — WU showed it a full hour before the bot feed |
| 07–12 | 26–27 | 26–27 | ✓ flat |

20/20 fit. Naive same-hour comparison shows gaps up to 4°C; there is no data error.

## Trading implications (Shenzhen market resolves on WU's 45035 table)

1. **The bot feed LAGS the resolver by up to ~1 h at every temperature change.** WU displayed the day's max (32) in its 04Z row; bot running_max hit 32 at 05:03Z. Hourly HKO gives no early edge here — the opposite.
2. **WU's daily max can exceed the hourly-HKO max.** A 10-minute spike between top-of-hours (or a different rounding of the 0.1° AWS value) shows up on WU but never in `rhrread` hourly. A dead-bracket call from hourly-HKO running_max can be wrong in the losing direction: bot kills bracket B because HKO max ≥ B+1, WU resolves max = B → NO on B loses. Two same-alignment ±1° candidates were visible today even outside spikes (WU 29 vs HKO 27→30; WU 26 vs 26→27).
3. Keep `no_trade: [shenzhen]` until running_max is computed from resolver truth.

## Fix

- **Resolver truth:** poll `api.weather.com/v1/location/ZGSZ:9:CN/observations/timeseries.json` (rows appear ≤ ~15 min past the hour and update intra-hour). Compute running_max from `temp` there, nothing else.
- **Early signal (optional):** HKO publishes **1-minute** regional temps incl. Lau Fau Shan: `data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_1min_temperature.csv` (confirmed live). That's faster than TWC ingestion and would restore an early-reading edge — but treat it as signal only; TWC's rounding of the 0.1° value decides resolution.
- Cosmetic: the synthesized raw in `metar_hko.jsonl` writes temp into the dewpoint slot ("26/26" all day). Harmless, but confusing in audits.

## Correction to station_audit_2026-07-02.md

The audit's "Divergence Jul 2: METAR max 35 vs WU 33" conflates two effects: ZGSZ-vs-LFS station distance (real, large) **and** this sampling offset (up to 1 h / ±1°). With the bot now on the HKO LFS feed, only the sampling offset remains.
