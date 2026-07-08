# Do other cities show the Shenzhen pattern? No — WU is a verbatim METAR copy there (2026-07-04)

Question: is WU/TWC resampling or time-shifting obs anywhere else, like it does for Lau Fau Shan?
Method: fetched TWC timeseries (`api.weather.com`, same product the WU history table renders) for four stations covering every cadence class, joined **programmatically** against local `metar.jsonl` on exact UTC timestamp (window: Jul 3 ~18Z – Jul 4 ~02Z).

## Result: 44/44 rows match exactly — timestamp AND value

| City | Station | Cadence class | WU rows | Exact | Notes |
|---|---|---|---|---|---|
| london | EGLC | :20/:50 half-hourly | 16 | 16 | Rows sit at :20/:50 — WU keeps true ob times, no top-of-hour relabeling |
| paris | LFPB | :00/:30 half-hourly | 10 | 10 | Both half-hourly obs present as separate rows |
| moscow | UUWW | :00/:30 + SPECI-active | 11 | 11 | Incl. the 3° rain-shower drop at 21:30Z, matched exactly |
| toronto | CYYZ | hourly + SPECI | 7 | 7 | **The 20:10Z SPECI (34→31) appears as its own WU row**, exact time and value |

Zero value mismatches, zero time mismatches, zero WU rows without a corresponding local METAR. Dewpoints also match the METAR DP group everywhere spot-checked (EGLC 19/13, LFPB 16/09, UUWW 18/15, CYYZ 29/20).

## Interpretation

- **For METAR-keyed obs stations (obs_id == ICAO), the WU history table IS the METAR stream.** Verbatim: exact ob timestamps (:20/:50, :00/:30, hourly), exact integer temps, SPECIs ingested as their own rows. No late-in-hour resampling, no top-of-hour relabeling, no independent rounding.
- **Shenzhen/Lau Fau Shan is the exception, not an instance of a general pattern.** Its obs_id 45035 is an HKO AWS station with no METARs; TWC fills those rows from HKO sub-hourly data with top-of-hour labels — that's what produced the shift/rounding mess. The failure mode is "non-METAR AWS-keyed obs_id", which per the Jul 2 audit exists in exactly one of our 35 cities.
- Consequences for the 34 clean cities:
  - The bot's running_max is computed from the resolver's literal data. No LFS-style ±1° rounding divergence or 1-h sampling offset exists.
  - The early-reading edge analysis (sub-hourly METARs + SPECIs, 68% of maxima first seen sub-hourly) stands as written — WU displays those same obs, so the bot's lead is TWC's ingestion latency only.
  - Residual risks are unchanged and small: TWC ingest drops/lag (ε ≈ 1–2%, still worth measuring per the reliability memo) and CORs.

## Caveats (honest limits of this check)

- One ~8-h window, 4 stations, mild overnight weather in 3 of 4. The Toronto SPECI and Moscow shower drop give real transition coverage, but a multi-day diff (the ε-backtest already recommended) is the definitive version.
- This verifies the *timeseries product*. The WU page's daily "High Temp" summary was not independently reconciled against max-of-rows here — same K3-class question as Shenzhen, one page-check per city-day would close it.
- Not checked: the 12 "data-layer-only" cities from the audit — but they're METAR-keyed obs_ids too, so the same conclusion should hold; spot-check if paranoid.
