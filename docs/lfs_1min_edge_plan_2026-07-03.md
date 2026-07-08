# Lau Fau Shan: dewpoint rule-out + 1-min-feed edge plan (2026-07-03)

Companion to `wu_lfs_discrepancy_2026-07-03.md`.

## 1. Dewpoint misread: ruled out

Four independent checks, all negative:

1. **Range.** Bot temps peaked at 32 (05Z). WU's dewPt for LFS stayed 24–27 all day. A dewpoint-as-temp bug can't produce 30/31/32.
2. **Series match.** Bot series matches WU's `temp` series 20/20 hours under the late-in-hour alignment. Against WU's `dewPt` series it fails immediately (bot 00Z=30 vs dewPt 27; 04Z=31 vs 26; 05Z=32 vs 27).
3. **Schema.** HKO `rhrread` has no per-station dewpoint at all — `temperature.data[]` carries temp only (humidity exists only for HKO HQ). There is no dewpoint field to misread.
4. **Live spot-check.** HKO's 20:00 HKT reading for LFS = 27 = bot's 12Z entry.

The `"26/26"` in the synthesized raw is the bot writing temp into both METAR slots. Cosmetic; worth fixing so future audits don't chase it (this prompt included).

## 2. What we know about WU's measurement times (and what we don't)

Established today:

- WU row HH:00 **appears ≤ ~13 min after HH:00** (12Z row live at 12:13Z, holding the ~12:00 reading).
- The row's **final** value ≈ a reading **late in hour HH** (~HH:50): every transition hour lands between the HKO HH:00 and (HH+1):00 instantaneous values (e.g. WU 23Z=29 between 27 and 30).
- ⇒ rows are (almost certainly) **revised intra-hour** as TWC re-samples HKO sub-hourly data. Direct confirmation failed today — temps were flat 27, two polls 6 min apart showed identical rows. Needs the calibration below.

Unknowns — these three gate any live trading:

- **K1 — sample grid + latency.** Which minutes does TWC sample (every 10 min? :00/:30?), and how long from HKO reading → WU row update?
- **K2 — rounding.** HKO AWS is 0.1 °C; WU prints integers. Half-up? Truncation? A 29.5 decided differently is exactly the 1° peak-day divergence that loses a "dead" NO.
- **K3 — daily-max computation (the critical one).** Is WU's "High Temp" the max of *final* hourly rows (≈ HH:50 samples only), or the max over *all* ingested samples including transient row versions? If final-rows-only, a 13:20 spike you traded on can **vanish from the resolution table**. Until K3 is answered, no 1-min-based entry is safe.

## 3. Edge formulation

Signal source: `data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_1min_temperature.csv` — 1-minute LFS temps at 0.1 °C (confirmed live; I couldn't render the CSV body in-session — verify columns locally with `curl`, documented as datetime / station / air temp).

Structure: this is the same instrument the resolver reads, sampled ~10–50× finer. Two distinct leads:

- **Publication lead** — you hold the reading TWC will print, ~5–15+ min before the WU row updates.
- **Grid lead** — you see intra-hour values TWC may *never* sample (10-min spikes). Only monetizable if K3 says all samples count; otherwise it's noise to ignore.

### Tiered entry rules

| Tier | Trigger | p | Status |
|---|---|---|---|
| 0 | WU row itself prints ≥ B+1 | ~1 | Spec-compliant today. Replace hourly-HKO running_max with a 1–5-min poll of the TWC timeseries as `rmax_resolver`. Zero informational edge, but the Moscow/Shenzhen precedent says thin books stay mispriced anyway. |
| 1 | 1-min feed ≥ (B+1) + δ, sustained ≥ m min (start δ=0.4 °C, m=10; recalibrate from K1/K2) | ≥0.99 target | The real edge: fire up to ~50 min before top-of-hour watchers, ~10–20 min before continuous WU pollers, while asks sit at 0.70–0.96. Gate on K3. |
| 2 | Rising 1-min path, forecast of the HH:50 print | quarter-Kelly, explicit p | **Flagging per spec: this is forecasting, not a certainty trade** — it edges into the "directional" non-goal. Park it unless Tiers 0–1 prove out and you consciously widen the mandate. |

Risk rails (keep regardless of tier):

- Never buy NO on the bracket adjacent to the candidate daily max off the 1-min feed alone — that's where K2 rounding kills you. Adjacent bracket: require the WU print. Margin-≥2 brackets: 1-min trigger allowed (mirrors the existing adjacent/margin-≥2 framework, p≈0.999).
- Two maxima in the worker: `rmax_resolver` (TWC prints — authoritative, drives Tier 0 and settlement expectations) and `rmax_signal` (1-min — drives Tier 1 with δ/m). Keep them separate; never let signal-max mark a bracket dead in the resolver's books.
- Temp crashes (today: 32→26 squall under TS Maysak) don't resurrect dead brackets — max is monotone. The only downward risk is K3-style row revision, which is why K3 gates everything.

### Calibration protocol (3–5 days, zero capital, shenzhen stays no_trade)

1. Log the 1-min CSV every 60 s (LFS row: timestamp, 0.1° temp).
2. Poll TWC `timeseries.json?hours=2` every 60 s; store every distinct row version. Use the full field tuple (pressure 0.1 hPa, rh, wdir, wspd) as the change detector — temp alone is too flat.
3. Join row versions to 1-min readings on matching fields → recovers K1 exactly.
4. Collect (0.1° matched reading, WU integer) pairs across days → K2, with attention to x.5 cases.
5. Next day, compare WU page "High Temp" / `historical.json` `max_temp` vs max(final rows) vs max(all row versions) → K3.
6. Acceptance to go live (Tier 1): K1 latency stable, K2 rule exact on ≥30 pairs with no contradiction, K3 answered "all samples count" (or Tier 1 restricted to sustained-≥-full-grid-interval signals if "final rows only").

### Implementation notes

- Both new pollers live in `feeds_http.py` (network-layer convention); strategy/risk stay pure.
- The CSV covers HKO AWS stations only → this whole apparatus is Shenzhen-specific. No carryover to the METAR cities.
- Honest ceiling: the edge survives only while the Shenzhen book is thin and nobody else polls TWC sub-minute. Bankroll-limited per FINDINGS, so even Tier 0 alone may capture most of the available PnL.
