# Station / resolver audit — 2026-07-02

Question: does each city's METAR feed (bot) match the market's resolution source?
Method: (1) probed TWC/WU obs product (`api.weather.com .../observations/timeseries`) for every ICAO the bot reads — WU's history table serves this product; (2) pulled Polymarket market descriptions (`gamma-api`) to confirm the resolver URL + station per city.

## Verdict

**34 of 35 cities are clean. Shenzhen is the only broken one — keep it on no_trade.**

## Broken

| City | Bot station | Resolver | Problem |
|---|---|---|---|
| shenzhen | ZGSZ METAR | WU history page for ZGSZ | WU has no METAR-keyed obs station for ZGSZ. Its history table serves **obs_id 45035 "Lau Fau Shan"** — an HKO station ~25 km away across Deep Bay. Divergence Jul 1: METAR max 32 vs WU 33. Jul 2: METAR max 35 vs WU 33 (2°C gap at the same hour). "Dead by METAR" ≠ dead by resolver. Fix: for Shenzhen, resolve running_max from `api.weather.com` location `ZGSZ:9:CN` (station 45035) instead of METARs, or keep disabled. |

## Special case (OK, but different source type)

| City | Bot station | Resolver | Note |
|---|---|---|---|
| istanbul | LTFM METAR | **NOAA** `weather.gov/wrh/timeseries?site=LTFM` (not WU) | Station matches, and weather.gov's table is the LTFM METAR feed → values match. WU has NO data for LTFM at all (its obs product returns empty), so anyone checking WU for Istanbul sees garbage — but the market doesn't use WU here. FYI: WU's LTBA (Atatürk) read 32°C at 11:10Z today while LTFM read 27°C — irrelevant to resolution, but don't mix them up. |

## Verified station + resolver (market description checked, obs_id == ICAO, temps match METAR)

amsterdam EHAM (WU "schiphol/EHAM") · ankara LTAC · beijing ZBAA · busan RKPK (Gimhae) ·
chengdu ZUUU (Shuangliu, not Tianfu) · chongqing ZUCK · guangzhou ZGGG · kuala_lumpur WMKK ·
london **EGLC** (City, not Heathrow) · milan **LIMC** (Malpensa, not Linate) · panama_city **MPMG** (Albrook, not Tocumen) ·
paris **LFPB** (Le Bourget, not CDG) · qingdao ZSQD (Jiaodong) · sao_paulo SBGR (Guarulhos) ·
seoul **RKSI** (Incheon, not Gimpo) · shanghai **ZSPD** (Pudong, not Hongqiao) · taipei **RCSS** (Songshan, not Taoyuan) ·
tokyo **RJTT** (Haneda, not Narita) · toronto CYYZ (Pearson) · wuhan ZHHH · moscow UUWW (user-confirmed)

## Verified at data layer only (obs_id == ICAO, temps match METAR; market description not fetched)

cape_town FACT · helsinki EFHK · jeddah OEJN · karachi OPKC · lucknow VILK · madrid LEMD ·
manila RPLL · munich EDDM · singapore WSSS · tel_aviv LLBG · warsaw EPWA · wellington NZWN

All are single-airport cities with no plausible alternate WU station; residual risk is negligible, but if you want 100 % coverage, spot-check these 12 descriptions the same way (`gamma-api.polymarket.com/events?slug=highest-temperature-in-<city>-on-<date>`, read `resolutionSource`).

## Residual risks even with matching stations (not "risk-free", but close)

1. **METAR corrections (COR).** A temp revised downward after the bot buys can resurrect a "dead" bracket. Market rules accept revisions until the next day's first datapoint. The bot already logs `is_correction` — make sure it re-evaluates positions on corrections, not just running_max.
2. **Collection gaps.** metar.jsonl is missing the 2026-07-01 00:00Z ZGSZ ob; a missed peak ob understates running_max (safe direction — missed edge, not loss) but a missed *correction* is the dangerous direction.
3. **WU feed hiccups.** WU's table occasionally drops/lags an ob; if WU never displays the peak METAR, the resolver max can be lower than METAR max. Same failure class as Shenzhen but transient. Sizing should assume dead-bracket bets are ~99 % not 100 %.
4. **Istanbul SPECIs.** weather.gov's table can include intermediate/special obs; ensure the LTFM parser ingests SPECI reports, not just hourly METARs.

## July 1 Shenzhen incident (context)

Bot bought ~651 NO-shares on the 31°C bracket (reason `dead<rmax`, METAR rmax 32) at 03:48–05:17Z. At purchase time the **resolver's** running max (Lau Fau Shan) was only 31 — the bracket was alive per the resolution source, and the ~9 ¢ YES price was rational. LFS later hit 33, so the bet won on weather, not edge. `trade_disabled` at 06:15Z and the current `no_trade: [shenzhen]` are correct.
