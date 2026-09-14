# The resolver moved: Weather Underground → NOAA (2026-09-12)

Measured 2026-09-14, after finding all 35 cities idling with
`no market data (event not resolved or book unavailable)`.

## What happened

Polymarket re-pointed the daily-temperature markets. Every event created from
**2026-09-12** carries new resolution text. Paris, verbatim:

> This market will resolve to the temperature range that contains the highest
> temperature recorded by NOAA at the Paris-Le Bourget Airport Station in degrees
> Celsius on 14 Sep '26.
>
> The resolution source for this market will be information from NOAA,
> specifically the highest reading under the "Temp" column for all times on this
> day, available here: `https://www.weather.gov/wrh/timeseries?site=lfpb`
>
> If NOAA data for the observation date is unavailable by 11:59 PM ET on the day
> following the observation date, the Weather Underground Daily Observations
> table will be used as the resolution source.

Three things changed at once:

1. **The resolution source is NOAA**, named by a per-station URL. Weather
   Underground is demoted to a *fallback* clause.
2. **The station is pinned in the URL** as `site=<ICAO>` — a stronger, machine
   checkable identity claim than the old prose.
3. **Whole degrees Celsius is explicit**: "The resolution source for this market
   measures temperatures to whole degrees Celsius (eg, 9°C). Thus, this is the
   level of precision that will be used when resolving the market."

**Taipei (RCSS) is the lone holdout** — still Weather Underground, no NOAA URL.
See §4 for why.

## 1. Where NOAA actually gets the data

`weather.gov/wrh/timeseries` is a JavaScript page; it holds no data itself. It
loads `https://www.weather.gov/source/wrh/apiKey.js`, which publishes NOAA's own
**Synoptic Data (MesoWest)** token, and renders:

```
https://api.synopticdata.com/v2/stations/timeseries
    ?stid=<ICAO>&recent=<minutes>&units=metric&obtimezone=utc&token=<the weather.gov token>
```

(The request needs a browser `User-Agent` and a `https://www.weather.gov/`
`Referer`; without them the API answers 403.)

The payload is the station's observation series, keyed by ICAO, in whole °C —
`STATION[0].OBSERVATIONS.air_temp_set_1` against `date_time`. For LFPB it
returns `Paris-Le Bourget Airport`, half-hourly, matching our METAR exactly.

## 2. That series IS the METAR (34/35 cities, 100.0%)

Head-to-head over the trailing 48 hours, per station, resolver series vs
`aviationweather.gov` METAR:

| | result |
|---|---|
| cities compared | 35 |
| **cities with any disagreement** | **0** |
| paired observations | ~2,700 |
| agreement | **100.0%** |
| identical observation count / window / daily max | yes, every NOAA city |
| SPECIs | **present in the NOAA series**, same values |

Representative rows (obs count both sides, agreement, both maxima):

```
paris     LFPB  96/96  100.0%  29 / 29      seoul     RKSI  96/96  100.0%  26 / 26
moscow    UUWW  96/96  100.0%  18 / 18      shenzhen  ZGSZ  48/48  100.0%  32 / 32
tel_aviv  LLBG  96/96  100.0%  33 / 33      lucknow   VILK  96/96  100.0%  33 / 33
```

> **Method note.** A *batched* `aviationweather.gov` request (many `ids` in one
> call) silently truncates the record set — 12 stations came back with ~34 obs
> each instead of 96. Per-station requests return the full series. Worth checking
> `SharedMetarFetcher` against this: a truncated batch looks exactly like a feed
> hole, and feed holes are what produced 8 of August's false divergence blocks.

## 3. What this invalidates

- **Seoul is no longer a divergent city.** The 2026-07-19 wipe happened because
  *Weather Underground's* RKSI row held 26 °C where the METAR held 27. WU is now
  only a fallback; NOAA's RKSI series matched our METAR on 96 of 96 obs.
- **Shenzhen is no longer a different instrument.** WU resolved Shenzhen off HKO
  **Lau Fau Shan** (15% agreement). NOAA resolves it off `site=zgsz` —
  Synoptic returns **"Shenzhen Baoan International"**, the airport, which is our
  METAR station. 48 of 48 obs matched.
- Both cities' `resolution_source="resolver"` setting — reading TWC's closed rows
  and eating a 26–564 min publish lag — is now the *wrong* source and pure cost.
- **The WU/TWC resolver cross-check is now checking the fallback table**, not the
  resolver. Its sticky blocks gate trading on a table the market no longer uses.
- `WUNDERGROUND_EXEMPT_ICAOS` (UUWW/LTFM/LLBG) is no longer a special case —
  every city except Taipei now resolves the way those three already did.
- The `include_speci=False` exemption for Moscow/Istanbul/Tel Aviv was justified
  by "weather.gov resolves on the routine cycle". That premise is **false**: the
  NOAA series carries SPECIs with identical values (verified on RKPK and RCSS).
  Leaving the flag off stays *conservative* — it can only lower our running max,
  never raise it — so it is safe to keep, but it is no longer correct as stated.

## 4. Taipei is the exception

Synoptic carries only **6** RCSS observations in 48 hours — and all six are
SPECIs; it does not ingest RCSS's routine cycle at all (our METAR has 90 obs in
the same window). A market resolving on that series would be unresolvable, which
is presumably why Polymarket left Taipei on Weather Underground. Taipei must keep
the WU cross-check; it cannot be migrated with the others.

## 5. Change made

`feeds_http.source_confirmed` required the event text to name the station **and**
the term `wunderground`. The new text says "Weather Underground" only in the
fallback clause (and with a space), so all 34 NOAA cities were rejected and the
bot idled.

It now accepts either source, and is **stricter** than before for NOAA markets:

```python
sites = {m.lower() for m in NOAA_TIMESERIES_RE.findall(text)}
if sites:
    return sites == {icao.lower()}   # resolution URL must be OUR station, only ours
```

Naming our station somewhere in the prose no longer suffices if the resolution
URL points at a different site — the exact failure class that emptied the wallet.

Result: **35/35 cities discover their event; zero "no market data".**

## 6. The cross-check now reads NOAA (done, same day)

`CityConfig.resolver_provider` selects the table the guard compares us against:
**`synoptic`** for 34 cities, **`twc`** for Taipei. The Synoptic fetcher re-emits
the TWC row shape (`parse_synoptic_timeseries`), so the collector, the guard and
the on-disk format are provider-agnostic — swapping the resolver swaps the
fetcher and nothing else. Rows land in `resolver_rows.jsonl` (Taipei keeps
`wu_rows.jsonl`; the two providers' histories must not mix).

Three things fell out of it:

- **No API key.** Synoptic is keyed by `metar_icao` and the token comes from
  weather.gov's own `apiKey.js` at startup. `TWC_API_KEY` is now needed only for
  Taipei and the Shenzhen calibration collectors.
- **Istanbul is verifiable for the first time.** WU served LTFM *zero* rows, so
  the city could never trade by construction. NOAA serves 96 in 48 h.
- **Verification is immediate, not next-day.** One Synoptic call backfills 48 h,
  where the TWC path crawled forward under a 26–564 min publish lag. Our own side
  was then the bottleneck — `metar.jsonl` has holes after any downtime — so
  startup also seeds the guard from the station's trailing METAR
  (`fetch_metar_history`). That feeds the **cross-check only**: never `MetarFeed`,
  never the running max, never `metar.jsonl`.

Measured on restart, 2026-09-14: Paris reached `resolver_ok` **~90 seconds** after
startup with 48 pairs over 23.5 h, and **34 of 35 cities verified**. The only
city still blocked is `lucknow`, whose 4 genuine paired mismatches in 633 obs are
unaffected by any of this.

```
python -m dead_bracket.tools.resolver_status --seed-history
  -> 34/35 cities may trade; 1 blocked for divergence: lucknow
```

## 7. Still to do

1. **Move seoul and shenzhen back to `resolution_source="metar"`.** Their METAR
   is the resolver again (96/96 and 48/48), and reading TWC's closed rows costs
   them the whole speed edge. Wants a soak on the new cross-check first.
2. **Re-check `SharedMetarFetcher` for batch truncation** (the method note in
   §2). A truncated batch is indistinguishable from a feed hole, and feed holes
   caused 8 of August's false divergence blocks.
3. **Re-measure the edge** (`STRATEGY.md` §1 and §4) under the new resolution
   source. Every number there was collected while the market resolved on WU; the
   Shenzhen "opportunities" in particular were false-deads created by the WU/LFS
   mismatch, which no longer exists.
4. **Watch `fractional`** on the Synoptic rows. The feed returns whole °C for
   every station today and the parser counts any exception; a non-zero count
   means the resolving precision changed and the rounding needs revisiting.
