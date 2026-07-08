# Sub-hourly national feeds not yet wired into the bot — 2026-07-02

Correction to the earlier report: "no 6-min feeds in the resolver chain" was too narrow. National met services publish sub-hourly data from the **same (or co-located) sensors** that produce the resolver METARs. These don't change resolver truth — they let you know what the next METAR will say **6–30 min before it's filed**, which is a bigger lead than the sub-hourly METAR edge (where you only beat hourly-only watchers).

## Tier 1 — verified feeds, same-station, wire these first

| City | Feed | Cadence / latency | Station | Access |
|---|---|---|---|---|
| **paris LFPB** | Météo-France "Paquet Observations" API (RADOME network, ~550 stations) | **6 min**, near-real-time | Le Bourget is a RADOME station — same site as the METAR | Free API key, portail-api.meteofrance.fr; also meteo.data.gouv.fr |
| **amsterdam EHAM** | KNMI Data Platform `Actuele10mindataKNMIstations` | **10 min**, available a few min after interval end | Station 240 = Schiphol, same site | Free API key, dataplatform.knmi.nl |
| **helsinki EFHK** | FMI open data WFS (`fmi::observations::weather`) | **10 min**, ~5 min lag | Helsinki-Vantaa lentoasema (fmisid 100968), same site | No key needed |
| **tokyo RJTT** | JMA AMeDAS JSON (`jma.go.jp/bosai/amedas/data/map/{ts}.json`, `latest_time.txt`) | **10 min** | AMeDAS "Haneda" station on the airport | No key, public JSON |
| **singapore WSSS** | NEA / data.gov.sg air-temperature API | **1 min(!)** | "Changi Climate Station" at the airport — verify sensor vs METAR for a week | No key; note NEA has had recent outages |

## Tier 2 — feeds exist, station identity or latency needs verification

| City | Feed | Notes |
|---|---|---|
| munich EDDM | DWD opendata 10-min air_temperature (`/now/` files) | 10-min values but publication latency can be ~30 min — measure before relying |
| istanbul LTFM, ankara LTAC | MGM (mgm.gov.tr) AWOS obs, ~10-min refresh on `servis.mgm.gov.tr` JSON | Unofficial/scrape; same-station AWOS |
| seoul RKSI, busan RKPK | KMA API Hub (apihub.kma.go.kr) — AWS 1-min & AMOS aviation obs | Free key; confirm the airport AMOS station is exposed |
| taipei RCSS | CWA open data 10-min auto stations (O-A0001-001) | Nearest auto station may not be the Songshan METAR sensor — calibrate |
| tel_aviv LLBG | IMS open 10-min API (ims.gov.il) | Nearest IMS station (Bet Dagan) is ~7 km from LLBG — real mismatch risk, treat as weak predictor only |
| toronto CYYZ | ECCC Datamart SWOB-ML (`dd.weather.gc.ca/observations/swob-ml`) | Not sub-hourly, but delivers METARs/SPECIs faster than aggregators — cuts your 350–900 s ingest latency |
| sao_paulo SBGR | REDEMET API (Brazilian AF aviation met) | Fastest METAR delivery for SB* stations; free key |
| lucknow VILK | IMD AWS network, 15-min | Public pages flaky; low priority |

## No usable open sub-hourly found

China cities (CMA/NMC publish hourly only — beijing, guangzhou, shanghai, chengdu, chongqing, qingdao, wuhan), karachi (PMD), jeddah (NCM), manila (PAGASA), kuala_lumpur (METMalaysia), cape_town (SAWS), panama_city, wellington (MetService closed API), warsaw (IMGW hourly), moscow (no open feed — but 133 SPECIs/25d already make it the best METAR-native early station), madrid (AEMET is hourly).

## How the bet changes (and the new risks)

The feed reading T̃ (tenths, e.g. 32.4) predicts the next METAR's whole-degree temp. Three new risk axes that METAR-based betting didn't have:

1. **Sensor identity** — same site ≠ same instrument. Calibrate per station: log feed + METAR pairs for 1–2 weeks, measure P(next METAR ≥ T | feed ≥ T + m) for margins m.
2. **Rounding** — METAR rounds to whole °C; a feed reading of 31.5–31.9 is not a safe "32". Only fire at margin, e.g. feed ≥ T + 0.4.
3. **Intra-interval drift** — METAR temp is an instant sample, not the interval max. A 6-min spike can retreat before the :00/:30 sampling instant. Mid-afternoon this is mild (temps persist near the peak); at cloud/wind changes it isn't. This is also why the bet is still strong: you don't need the *next* METAR to confirm, any later ob that day ≥ T settles it.

## Kelly with feed-based p

p now factors as: p = P(some METAR today ≥ T | feed ≥ T+m) × P(resolver reflects it) (~0.995+ from the previous report). The first factor is unknown until calibrated — plausibly 0.95–0.99 at m=0.4 in the 13:00–17:00 window, lower elsewhere. Until measured:

- Bet only in the 13:00–17:00 local window at margin ≥ 0.4°C, adjacent bracket or lower.
- Use p = 0.95 in `f* = p − (1−p)·a/(1−a)` and **quarter-Kelly**: at a = 0.83, f*/4 ≈ 18%; at a = 0.90, ≈ 14%. Same-ob correlation cap (~25%) still applies, and feed-triggered and METAR-triggered bets on the same city-day are the same bet.
- After 2 weeks of calibration logs, replace 0.95 with the measured per-station number and re-derive.

The prize for wiring Tier 1: your current pipeline sees a new max ~5–15 min after the METAR files (latency.jsonl: 350–900 s). A 6-min/10-min feed sees it up to 30+ min before the METAR files — you'd act before the information that moves the book even exists publicly.
