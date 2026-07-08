# Early-reading reliability & Kelly sizing — 2026-07-02

Data: local metar.jsonl (Europe: 24–29 days; Asia/other: 3–4 days; ~13,000 obs, 342 complete market days).
Resolver-side ingestion verified via TWC obs feed (`api.weather.com timeseries` — the same product WU's history table renders).

## 1. Who publishes early readings

| Cadence | Stations | Early data |
|---|---|---|
| **:20/:50 half-hourly** (no :00 ob at all) | london EGLC, amsterdam EHAM (:25/:55), helsinki, milan, munich, ankara, istanbul, tel_aviv | 2 obs/hr; the "hourly" row everyone waits for doesn't exist — :50 ob is effectively the hourly |
| **:00/:30 half-hourly** | paris, madrid, warsaw, moscow, beijing, guangzhou, shanghai, seoul, tokyo, karachi, kuala_lumpur, lucknow, singapore, taipei, wellington | :30 ob gives a 30-min lead on the top-of-hour |
| **Hourly only** | busan, cape_town, chengdu, chongqing, jeddah, manila, panama_city, qingdao, sao_paulo, toronto, wuhan, (shenzhen) | Only SPECIs give leads |
| **SPECI-active** (extra unscheduled obs) | **moscow (133 in 25d!)**, guangzhou 12, taipei 11, shanghai 9, ankara 8, madrid 7, beijing 6, busan/cape_town 5, toronto/manila 3–4 | Sub-30-min leads; fired on weather changes (often when temp jumps) |

No true 6-min/1-min feeds exist in the resolver chain for these cities (HKO 10-min data exists for Lau Fau Shan but Shenzhen is disabled; NWS 1-min ASOS is US-only). **The early data that counts is half-hourly METARs + SPECIs — and TWC ingests both** (e.g., ZGGG's :17 SPECI appeared in the WU obs feed).

## 2. How reliable are early readings

- **Corrections: ~zero risk.** 0 temperature-changing corrections in ~13,000 obs. 95% upper bound (rule of three): <0.9% per day.
- **68% of daily maxima are first seen on a sub-hourly/SPECI ob** (avg lead 26 min before the next top-of-hour). For :20/:50 stations it's 100% by construction.
- **27% of daily maxima are fragile** — the max temp appears exactly once all day. For the other 73%, a second ob confirms after a median 30 min (p90: 90 min).
- A fragile max only hurts you if the resolver *also* drops that single ob. Not measurable from local data alone; TWC feed drops are rare and revisions are accepted until the next day's first datapoint (time for backfill). Estimate P(single ob missing from resolver) = ε ≈ 1–2% conservatively.
- **Win probability for a NO-buy on bracket B when a new reading T arrives:**
  - Adjacent bracket (B = T−1): loses only if the ob vanishes AND no later ob reaches T AND final max lands exactly on B. p ≈ 1 − 0.27·ε ≈ **0.995–0.997**
  - Margin ≥2 (B ≤ T−2): needs the above plus final max ≤ B. p ≈ **0.999+**
  - After a second ob confirms T: p ≈ **~1** (double feed failure required) — but the price edge is usually gone by then.

## 3. When (local time)

Final daily maxima are first reached overwhelmingly **12:00–17:00 local** (78% of days; peak 15:00–16:00). That's your betting window: each new-max ob that crosses a bracket boundary kills a bracket ~26 min before hourly-only watchers see it. Morning new-max obs are equally *valid* but already priced. The 15:00–17:00 readings are the money ones: they're most likely to be the day's final max (i.e., adjacent brackets stay contested and priced 0.70–0.96 — exactly the asks seen in today's Shenzhen dead.jsonl: 0.70, 0.83, 0.96).

Caveat: watch for late-day COR risk on days with a fragile 15–16h max — the exposure gap until confirmation is median 30 min; if no confirmation comes by the next scheduled ob, the max is fragile for the rest of the day (27% of days).

## 4. Kelly sizing

Buying NO at ask `a`, true win prob `p`:

```
f* = p − (1−p) · a/(1−a)      (fraction of bankroll, full Kelly)
```

| ask a \ p | 0.98 | 0.99 | 0.995 |
|---|---|---|---|
| 0.70 | 0.93 | 0.97 | 0.98 |
| 0.83 | 0.88 | 0.94 | 0.97 |
| 0.90 | 0.80 | 0.90 | 0.95 |
| 0.96 | 0.50 | 0.75 | 0.88 |

Full Kelly at these numbers says "bet most of the bankroll", which is exactly why you shouldn't use it raw:

1. **p is estimated, and the loss tail is fat.** If you size for p=0.995 at a=0.96 (f=0.88) and true p is 0.97, you lose ~88% of bankroll 3% of the time. Kelly's optimality assumes p is known.
2. **Bets are correlated.** All NO-buys triggered by the same ob (multiple brackets, and often multiple cities under the same synoptic pattern) share one failure mode. Treat the sum of same-ob stakes as one bet.
3. **Practical rule: quarter-Kelly with a p floor.** Use p=0.99 for unconfirmed adjacent-bracket bets, p=0.995 for margin-≥2 or confirmed ones. Quarter-Kelly examples: a=0.83, p=0.99 → stake ~23% of bankroll; a=0.96, p=0.99 → ~19%. Cap total same-ob exposure at ~25%.
4. Expected growth per bet is small (~0.1–0.6% of bankroll at quarter-Kelly) but it compounds across ~35 cities × daily; the constraint is bankroll, correlation, and book depth (today's depths were only 400–900 shares).

## 5. Recommended follow-ups

- Backtest ε directly: diff the 25-day European metar.jsonl against WU history pages for the same days (counts of obs present/missing). That turns the 1–2% ε guess into a measured number and tightens p.
- Log the TWC timeseries feed alongside METARs going forward — it's the resolver's actual content, so `running_max` can be computed on resolver truth instead of METAR truth (also fixes Shenzhen-class faults permanently).
- Moscow deserves priority: half-hourly + 133 SPECIs = the most early-reading opportunities of any station, and it's user-confirmed to match the resolver.
