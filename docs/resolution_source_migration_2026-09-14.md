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
- `WUNDERGROUND_EXEMPT_ICAOS` (UUWW/LTFM/LLBG) is no longer a special case for
  *source* purposes — every city except Taipei now resolves the way those three
  already did. Their `include_speci=False` setting, however, is **correct and
  should stay** — see §8, which corrects an earlier claim here.

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

## 7. Every tracker now reads its own resolver (done)

Each city's source was re-measured against the table its market actually names,
and the set of resolver-sourced cities **inverted**:

| city | vs NOAA | decision |
|---|---|---|
| seoul (RKSI) | 64/64 **100.0%** | METAR is the resolver -> back to `metar` |
| shenzhen (ZGSZ) | 32/32 **100.0%** | METAR is the resolver -> back to `metar` |
| lucknow (VILK) | 629/633 **99.4%** | genuinely divergent -> reads the resolver's rows |

Seoul and Shenzhen had still been reading TWC/WU closed rows — for Shenzhen a
table that disagrees with the real resolver on 25 of 31 paired observations, on
every one of which **NOAA agrees with us**. Lucknow goes the other way: NOAA
sides with WU against us on all four of its mismatches, and twice ours was the
*higher* value, the direction that manufactures a false dead.

Reading the resolver's rows used to cost the whole speed edge; NOAA does not lag,
so it is now close to free. `TwcResolverFetcher` gained `settle_sec`: TWC
publishes a revising hourly aggregate whose current-hour row must close first
(the 2026-07-08 near-miss was an intra-hour value that later vanished), while
NOAA rows are observations that nothing revises and are servable on arrival.

**Result: 35/35 cities verified, 0 blocked.**

One operational characteristic to know: a resolver-sourced city replays its 26 h
of backfill one row per poll after a restart, so `lucknow` runs a stale (low)
running max for a while before it is current. That is the safe direction — a low
max cannot mark a live bracket dead — but it cannot trade until it catches up.

## 8. Still to do

1. **Re-measure the edge** (`STRATEGY.md` §1 and §4) under the new resolution
   source. Every number there was collected while the market resolved on WU; the
   Shenzhen "opportunities" in particular were false-deads created by the WU/LFS
   mismatch, which no longer exists.
2. **`STRATEGY.md` §5's recycle default** is still open: the data says holding to
   $1 by default is backwards, since every dollar ever realised came from a
   99.9c maker fill and an unfilled one is a free false-dead alarm.
3. **Watch `fractional`** on the Synoptic rows. The feed returns whole °C for
   every station today and the parser counts any exception; a non-zero count
   means the resolving precision changed and the rounding needs revisiting.
4. **Speed up the resolver-sourced backfill replay** if `lucknow`'s post-restart
   catch-up ever gets in the way.

## 9. SPECI carriage is per-station — correcting §3

An earlier version of §3 said the NOAA series "carries SPECIs", inferring a
global property from two stations (RKPK and RCSS). Measured properly across
every city over a 7-day window, that is **not** a global property:

| | result |
|---|---|
| SPECI-counting cities checked | 32 |
| our SPECIs absent from the NOAA table | **0** |
| Moscow (UUWW), `include_speci=False` | **0 of 14** SPECIs present in NOAA |

So both settings are right, for different reasons, and neither is a leftover:

- For the 32 `include_speci=True` cities there are **zero orphan SPECIs** — every
  SPECI we fold into the running max is in the resolver's table too. This is the
  dangerous direction (an orphan SPECI can push our max above the resolver's and
  mark a live bracket dead), and it is clean.
- For **Moscow** the resolver records none of its SPECIs, so `include_speci=False`
  matches the resolver exactly. Flipping it on — as the earlier §3 text invited —
  would have started marking brackets dead on observations the resolver never
  sees. That is a false-dead generator, not a cleanup.
- Istanbul and Tel Aviv issued no SPECIs in the window, so there is no evidence
  either way; the conservative setting stays.

**Rule: `include_speci` is a per-station measurement against the resolver's own
table, never an inference from the resolution source.** Re-measure it when
adding a city, with `scripts` equivalent to the orphan check above.

The same measurement explains why Taipei must stay on the `twc` provider.
Against NOAA its daily max looks 3–8 °C *above* the resolver's on all 7 days —
not a divergence, but an artifact of NOAA carrying only RCSS's SPECIs and none
of its routine cycle. Against the table Taipei actually resolves on (WU) it
agrees on 589 of 589 paired observations. If Taipei were ever switched to
`synoptic` the guard would sticky-block it immediately, which is the correct
fail-closed response.
