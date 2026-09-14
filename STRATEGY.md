# STRATEGY — what actually works, measured

> **Superseded in part, same day.** Hours after this was written, the cause of
> the bot's silence turned out to be bigger than the guard bug in §3: on
> **2026-09-12 Polymarket moved the markets from Weather Underground to NOAA**
> (`weather.gov/wrh/timeseries?site=<ICAO>`), whose series is the station METAR
> itself — see
> [docs/resolution_source_migration_2026-09-14.md](docs/resolution_source_migration_2026-09-14.md).
> Every measurement below was collected while the market still resolved on WU.
> §1–§2 remain valid as history and the §3 guard bug is real and fixed, but the
> Shenzhen and Seoul conclusions are now *about the old source*: their METARs
> match the new resolver exactly. **Re-measure §1 and §4 before acting on §5.**

Written **2026-09-14** from the full recorded history (`data/`), not from the
spec. It supersedes the optimistic reading of [FINDINGS.md](FINDINGS.md) §B/§C
without contradicting its raw numbers: those trades were real, the
generalisation drawn from them was not.

Three sentences up front:

1. **The dead-NO capture does not exist in the cities we are allowed to trade.**
   Over 7,014 bracket deaths the NO was already at **0.999 (p1 = 0.996)** the
   instant our source confirmed death; only **2 of 1,913** quoted deaths in
   source-verified cities were liftable at all, and only **one** of those had
   tradeable depth — worth **$22.50**.
2. **Nearly every "edge" we ever logged was a wrong resolution source.** 41 of
   the 43 liftable deaths were Shenzhen, 1 was Seoul — the two cities whose
   running max provably is not the resolver's. The cheap NO was the market
   correctly pricing a bracket we had wrongly declared dead. Taken to size once,
   that cost the whole wallet.
3. **The bot has nevertheless been dead since 2026-08-20 for an unrelated
   reason: the resolver guard is sticky-blocking 26 of 34 cities on false
   evidence.** All 25 day-max blocks are artifacts. That bug must be fixed
   regardless of which strategy runs on top.

What remains after that is small, rare, and genuinely positive-expectancy. It is
written up as **the operating strategy** in §5.

---

## 1. The measurement that settles the dead-NO question

`data/<city>/<date>/dead.jsonl` records the NO book **the instant a bracket
first goes dead**, one row per bracket per day. Sample: 2026-06-29 → 2026-09-01,
**35 cities, 861 city-days, 7,014 bracket deaths** (2,102 with a NO ask quoted).

| population | n | ask p1 | ask p25 | ask p50 | liftable (≤0.98 & net≥1.2¢) |
|---|---:|---:|---:|---:|---|
| all cities | 2,102 | 0.891 | 0.999 | 0.999 | 44 (2.1%) |
| **source-verified only** (excl. seoul, shenzhen) | **1,913** | **0.996** | **0.999** | **0.999** | **2 (0.1%)** |

Liftable deaths *with real depth* (≥50 sh), by city: **shenzhen 41, seoul 1,
moscow 1.** That single Moscow event is the entire verified-city opportunity in
the death-instant dataset: **$22.50 of net available, across 861 city-days.**

The mechanism is visible in the same file. `ask_pre` is the last NO ask seen
while the bracket was still live to us:

- **99%** (1,584/1,601) of deaths had the NO **already ≥ 0.98 while we still
  considered the bracket live**.
- **78%** (1,256/1,601) show *no price change at all* across the death instant.

The market prices the bracket dead before our confirmation and does not move
when we confirm. There is no repricing event to capture. This is FINDINGS §A
(Paris) generalised to 35 cities: it was never a Paris-specific liquidity story.

### Why Shenzhen looked like a goldmine

Shenzhen's resolver table is **Lau Fau Shan (HKO)**, not the ZGSZ airport METAR;
paired agreement is **19.4%** (25 mismatches in 31 pairs, measured again here).
Every deep-discount Shenzhen "opportunity" — NO offered at 0.25, 0.42, 0.55 for
hours — was the market pricing a bracket that was **not dead**. FINDINGS §C's
"we are bankroll-limited, 11× the depth sat unlifted" is therefore a measurement
of how much money we were *prevented* from losing.

The same applies to Seoul 2026-07-19: the opportunity log shows the NO on
bracket 26 trading down to **0.001**, i.e. the market screaming that the NO was
worthless. We bought 806 shares at 0.979.

### Corollary: the price-based "deadness corroboration" guard proposed in FINDINGS §D would not work

It fails in both directions on our own data:

- It would **not** have stopped the wipe — Seoul was bought at **0.977**, above
  any plausible suspicion threshold.
- It **would** have blocked our single best genuine trade — Moscow 2026-06-27,
  bought at **0.713**, settled to $1, **+$33.68**.

Cheapness is not the signal. Source identity is. Do not build that knob.

---

## 2. What the genuine trades were, and why they won

Splitting every fill in the repo's history by whether the city's source is
proven identical to the resolver:

| | trades | realized by selling | still riding on source correctness | losers |
|---|---:|---:|---:|---|
| **source-verified** (moscow, munich, helsinki) | 12* | **$59.59** | $89.51 (all settled to $1) | **0** |
| **unverified source** (seoul, shenzhen) | 6 | $37.68 | **$1,518.80** | 1 catastrophic |

\* 11 dead-path trades plus the single forecast-path trade ever placed
(helsinki 2026-07-19, 19 sh, +$0.83). The two 2026-06-10 fills excluded by
FINDINGS as a discovery bug are excluded here too.

Verified-city total, after the small held positions settled: **+$63.64, 12 for
12.** Every dollar ever lost came from a position **held to settlement** in a
city whose source was not the resolver's.

The winning mechanism is not "thin books reprice slowly". It is that **in an
unwatched book the market's own running max lags ours**. Moscow 2026-06-30:
bracket 25 died with its NO at 0.98, then *fell* to 0.87–0.94 over the next hour
— the market was still assigning ~13% to a bracket that could no longer win,
because nobody there had ingested the new high. We were **first** to know, not
last. That only happens where no automated participant is watching.

### The exit that has never failed

All **9** recycle sells at $0.999 filled — 0.7 to 234 minutes after the buy,
median ≈ 30 min. That matters for two reasons:

- It is **source-independent income**: a fill converts an uncertain settlement
  claim into cash at 99.9¢. Holding to $1 earns 0.1¢ more and risks 100¢.
- **A resting 0.999 sell that does not fill is a free false-dead alarm.** A NO
  that is genuinely certain attracts a 0.999 bid; a false-dead NO never will.
  FINDINGS §B already observed this after the fact — it should be a live monitor.

The current default (`recycle_on_opportunity=True` → hold to $1 unless the
bankroll caps block a new buy) gives up both benefits to earn 0.1¢. That default
is backwards.

---

## 3. The bug that is keeping the bot at zero

`data/_global/resolver_blocks.json` holds **26 sticky blocks**; the soak log
shows every city HALTED and $0 deployed since 2026-08-20. Replaying each block
against the recorded data at the exact instant it fired: **all 25 day-max blocks
are false positives**, in two flavours.

**Flavour A — the resolver simply had not published yet (17 cities).** Our
day-max observation *is* in the resolver's table with the *same value*; the row
was first seen **after** the block fired. `resolver_settle_lag_min = 90` assumes
a 90-minute publish lag. Measured lag over 20,883 rows: **p50 51 min, p75 73
min, 18% exceed 90 min**, and individual rows arrived +114, +141, +474, +504,
**+564 min** late. Examples:

```
london     ourMaxEpoch 14:50Z IS in WU as 24C  (first seen +91 min, AFTER the block)
tokyo      ourMaxEpoch 02:00Z IS in WU as 31C  (first seen +564 min, AFTER the block)
singapore  ourMaxEpoch 03:30Z IS in WU as 32C  (first seen +474 min, AFTER the block)
```

**Flavour B — our own collection had a hole (8 cities).** The resolver's day-max
came from a timestamp we have **no METAR for**. Paris 2026-08-22: our log is
missing 09:00, 10:00 and **10:30Z (=21 °C)**; WU has all three. Our max stalled
at 20, WU's was 21 → "divergence". Six of these eight cities blocked within the
same two minutes (11:24 and 12:02 on 08-22) — one shared fetcher outage, not
eight independent divergences.

Meanwhile the check that actually matters passes cleanly:

- **Paired-observation agreement: 100.0%** for 31 of 34 cities (20,000+ pairs).
  Only lucknow (99.4%), seoul (98.4%) and shenzhen (19.4%) disagree.
- **Closed-day max audit over 417 complete city-days: our METAR max exceeded the
  resolver's final daily high ZERO times.** Three days it was *lower* (safe).

The live day-max rule compares a complete source against an incomplete one and
calls the difference divergence. It is unsound by construction.

### The fix, simulated

1. **Keep** rule 1 — exact agreement on every *paired* observation in the
   trailing 24 h, sticky. This is what catches Seoul, Shenzhen and Lucknow.
2. **Delete** the live day-max rule. Over paired epochs it is tautological once
   rule 1 passes; over unpaired epochs it is noise.
3. **Add a retrospective closed-day audit.** Once the local day has closed *and*
   the resolver's newest row is past local midnight, compare final day maxima:
   - `ours > theirs` → **sticky divergence** (this, and only this, is the
     false-dead condition that wiped the wallet).
   - `ours < theirs` → **coverage warning**: we missed observations. Withhold
     entries until coverage is restored; never sticky.
4. Raise `resolver_max_lag_hours` above the measured p95, or drop the freshness
   rule in favour of the reach-back rule that already exists.

Simulated over the same 417 city-days:

| | current guard | corrected guard |
|---|---|---|
| sticky blocks | 26 (25 false) | **3** — lucknow, seoul, shenzhen |
| false positives | 25 | **0** |
| cities tradeable | 8 / 34 | **32 / 35** |

It still blocks Seoul on 2026-08-19 (`ours=28 resolver=29`), i.e. it still
catches the wipe class.

---

## 4. Frequency and size of what is left

Restricting to the clean window (2026-06-29 → 07-20, discovery bug fixed, all 34
cities running, **748 city-days**) and to source-verified cities,
`opportunities.jsonl` — written for *every* dead-NO offered below CAP — exists on
exactly **three city-days**:

| day | city | bracket | window | median ask | median depth | net $ available | we captured | blocked by |
|---|---|---|---|---|---|---|---|---|
| 06-30 | moscow | 25 | 61 min | 0.940 | 776 sh | $42 | $3.43 | 1,759 / 1,760 ticks `size/caps` |
| 07-01 | moscow | 28 | 23 min | 0.910 | 1,558 sh | $130 | $8.25 | 680 / 682 ticks `size/caps` |
| 07-12 | munich | 27 | 6 min | 0.961 | 682 sh | $23 | $9.75 | 54 / 86 ticks `size/caps` |

So, honestly:

- **Frequency: ~1 tradeable event per 7 days across all 32 verified cities.**
- **Size: $23–$130 of net available per event**, at full depth.
- **We are genuinely bankroll-limited when an event happens** — the caps, not
  the book, blocked ≥ 63% of ticks in every case. FINDINGS §C's conclusion
  survives even though its Shenzhen evidence does not.
- Fully capturing the largest event (1,558 sh @ 0.91 ≈ $1,418 notional) under
  `max_bracket_pct = 25` needs **≈ $5.7 k of equity** — and also a lift of
  `max_shares_per_bracket` (500), which binds first and would have capped that
  event at a third of the book. Beyond roughly $6 k the strategy becomes
  depth-limited and stops scaling.

Ceiling estimate: ~$195 of net available per 22 days at full capture ⇒
**order of $250/month gross at ~$6 k equity**, before any missed event. Not a
business; a small, near-riskless harvest with a hard capacity limit.

### The forecast path is signal-starved, not model-starved

`forecast_calibration_2026-07-13.md`: sensor identity is excellent (bias −0.05
to +0.15 °C, σ 0.00–0.14 °C, three of four feeds pass) — but across two months
it produced **5 scoreable signals** against a go/no-go gate of **≥100**. Only one
forecast trade was ever placed (helsinki 2026-07-19, 19 sh, +$0.83). At the
observed rate the gate needs years. Keep it collecting; do not wait on it.

---

## 5. The operating strategy

**Thesis.** The only repeatable, non-negative-expectancy trade in this market is
a *rare* dead-NO in a book nobody is watching, entered only where our source is
provably the resolver's, exited as a maker at 99.9¢, and sized so that being
wrong is survivable. Everything else measured in this repo is either pre-priced
away or a false-dead in disguise.

**The rules.**

1. **Source gate is the whole strategy.** Trade only cities that pass paired-ob
   exact agreement over 24 h *and* the closed-day max audit. Fail closed. This
   is worth more than every other rule combined: it is the difference between
   12-for-12 and a wiped wallet.
2. **Fix the guard before anything else** (§3). Until then the bot is a data
   collector with 26 cities switched off.
3. **Always rest the 99.9¢ maker sell. Make holding-to-$1 the exception.**
   Set `recycle_on_opportunity=False`. Cost 0.1¢/share; buys source-independent
   realisation, capital recycling, and a false-dead alarm.
4. **Alarm on an unfilled recycle.** A resting 0.999 sell with no fill after
   ~90 minutes (3× the observed median) means no one will pay ~$1 for a NO we
   believe is certain. Halt the city and page a human. Cheap, and it is the only
   independent check we have on a live position.
5. **Keep the concentration caps exactly as they are** (`max_bracket_pct 25`,
   `max_deployed_pct 90`, clamped to available capital). At ~1 event/week they
   cost little; they are what makes rule 1's residual failure survivable.
6. **Narrow the city list to where events actually occur** — moscow, munich,
   helsinki, amsterdam, milan, ankara, london, paris — and keep the rest in
   observe-only mode. Fewer books, less discovery surface, less rate-limit
   pressure, same event count. Re-derive the list from `dead.jsonl` monthly.
7. **Fund it to ~$6 k and no further.** Below that we leave most of each event
   on the table; above it the books cannot absorb the size.
8. **Do not trade seoul or shenzhen**, and do not restore them by reading the
   resolver's own rows: the measured publish lag (p50 51 min, p75 73 min, tail
   to 9 h) means the running max arrives long after the market has repriced —
   the CLAUDE.md estimate of 26–66 min is optimistic by the whole tail.

**What success looks like:** a quiet process that trades ~4 times a month, wins
nearly every time, earns tens of dollars per event, and *never* takes a position
in a city it cannot prove. Judge it on hit rate and on zero source violations,
not on PnL per week.

---

## 6. Open items, in priority order

1. **Guard fix + retrospective audit** (§3). Blocks everything.
2. **Recycle default flip + unfilled-recycle alarm** (§5.3, §5.4).
3. **`book_band_below = 1` is a blind spot.** We only ever price the single
   newest dead bracket, so the entire dataset above is silent about brackets
   2–5 below the running max. Widening the band for a month in observe-only mode
   is cheap and is the one place an unmeasured edge could still be hiding.
4. **Re-measure quarterly.** The Moscow window (June) produced 10 of the 12
   genuine trades; in July it produced one. Whatever inefficiency existed is
   decaying. Re-run §1 monthly; if verified-city liftable deaths stay at ~0.1%
   and the Moscow class disappears, the honest conclusion is to stop trading and
   keep only the collectors.
5. **Do not build the YES/day-max longshot** (FINDINGS §F.2). It remains a spec
   non-goal, it is directionally risky, and nothing measured here makes it look
   better than the market.

---

### Reproducing these numbers

All figures come from the repo's own logs; the scripts are throwaway but the
inputs are permanent:

- `data/*/<date>/dead.jsonl` — §1 (death-instant book, `ask_pre` vs `ask`)
- `data/*/<date>/opportunities.jsonl` — §4 (every sub-CAP dead NO, `why_not`)
- `data/*/<date>/orders.jsonl` — §2 (fills, recycle latency)
- `data/*/metar.jsonl` vs `data/*/wu_rows.jsonl` — §3 (pairing, publish lag,
  closed-day audit, corrected-guard simulation)
- `data/_global/resolver_blocks.json` — §3 (the 26 blocks, replayed at their
  `since` timestamps)
