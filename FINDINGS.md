# Findings — does the dead-bracket edge exist?

Updated **2026-06-29**. Short answer: **yes, but only in thin / less-efficient
markets.** The original single-city (Paris) study found no edge and is still
correct *for liquid markets* (§A below). The multi-city expansion then surfaced a
real, repeatable sub-98¢ dead-NO capture in a handful of thin markets — **Moscow
first, now Shenzhen** — which has produced a winning trade on (almost) every day
for the past two weeks with **zero losers**.

## 0. The live result (Moscow + Shenzhen only)
Realized from the actual `orders.jsonl` fills, held positions settled to $1
(deadness confirmed in every case — see below), recycled positions valued at
their 99.9¢ sell:

| day        | city     | bracket | bought | avg buy | exit        | PnL     |
|------------|----------|---------|--------|---------|-------------|---------|
| 2026-06-16 | moscow   | 17      | 10     | 0.970   | settle $1   | +0.26   |
| 2026-06-18 | moscow   | 15      | 10     | 0.970   | settle $1   | +0.26   |
| 2026-06-20 | moscow   | 19      | 10     | 0.950   | settle $1   | +0.45   |
| 2026-06-22 | moscow   | 23      | 10     | 0.840   | settle $1   | +1.52   |
| 2026-06-24 | moscow   | 20      | 10     | 0.946   | settle $1   | +0.49   |
| 2026-06-26 | moscow   | 19      | 44     | 0.972   | settle $1   | +1.07   |
| 2026-06-27 | moscow   | 20      | 121    | 0.713   | recycle .999| +33.68  |
| 2026-06-28 | moscow   | 22      | 122    | 0.955   | recycle .99 | +3.65   |
| 2026-06-29 | shenzhen | 29      | 132    | 0.930   | recycle .999| +8.35   |
| **TOTAL**  |          |         |        |         |             | **+49.72** |

Every trade is a NO bought **below CAP** (0.84–0.97, two deep ones at 0.71/0.84)
that resolved to $1 or was recycled at ~0.999. The deep-discount fills are *not*
the wrong-day/mismatch artifacts §C warns about: each one later found a **0.999
NO bid** to recycle into, which a false-dead NO never would — the bid existing is
itself proof the NO was genuinely worth ~$1 and the offer was a real mispricing.

> **Excluded as bugs:** the 2026-06-10 London (NO @ 0.015) and Paris (NO @ 0.90)
> fills were a since-fixed discovery bug, not edge. Do not count them.

## A. Why there is still no edge in *liquid* markets (the original Paris study)
Analysing a full day of Paris snapshots:
- Over **3,481** snapshots with a dead-NO ask quoted: `min 0.997`, `median
  0.999`. Zero below 0.98. At the instant each bracket newly died the freshest
  dead bracket's NO ask was already **0.998–0.999**.
- Our resolution METAR is the *slowest* signal: tracing bracket 21's death, the
  6-minute feed read 22.0 at 11:56 (market knows 21 is dead) but our METAR only
  confirmed 22 at 12:03 (+167s publish delay) — a ~7-minute head start for the
  market. Even at +0s pipeline latency we trail by minutes.

So in a deep, well-arbitraged book the dead NO is pre-repriced to ~$1 and there is
nothing to lift. That conclusion stands.

## B. Why the thin markets are different
The capturing cities (Moscow=UUWW, now Shenzhen) are **thin and less efficient**:
the arbitrage that pins Paris's dead NO to $1 within seconds is slow or absent, so
a freshly-dead NO sits **offered at 0.84–0.97 for minutes** — long enough for even
our last-to-know METAR to lift it. Moscow is also one of the
`WUNDERGROUND_EXEMPT_ICAOS` (it resolves off a non-WU source), i.e. exactly the
kind of market fewer participants price tightly. The edge is small per share
(net 2–7¢) but near-certain once deadness is confirmed.

## C. The liquidity is far larger than we captured — we are bankroll-limited
Shenzhen 2026-06-29, bracket 29 (`opportunities.jsonl`, 253 opportunity ticks):
- We deployed **132 shares (~$123)**. Depth at/below CAP was a **median of ~981
  shares and peaked at ~1,495** — roughly **11× what we took**.
- **251 of 253** ticks logged `acted=False, why=size/caps` — i.e. blocked by the
  per-bracket / per-city / bankroll exposure caps, **never** by missing liquidity
  and **never** by the net-of-fees threshold. The NO sat at 0.93–0.962 (net
  3.3–6.5¢) with four-figure depth the whole time.

Implication: in these thin markets the constraint is **capital, not the market**.
A larger bankroll (or higher per-bracket caps) would have scaled the Shenzhen
capture by ~10× at the same prices. This is what the `avail+$N` figure on the
STATUS line measures — track it to size the bankroll against the real opportunity.

## D. Risks / open items (don't get complacent)
- **Small, concentrated sample.** Nine trades, two cities, two weeks. This is an
  inefficiency in specific thin books, not a broad validated edge. Watch for it
  disappearing as/if those markets get arbitraged.
- **Deadness-corroboration guard still worth adding.** A "dead" NO offered at a
  *deep* discount is genuine edge in a thin market **but** is also the exact
  signature of a wrong-day / lowest-market / station-mismatch bug (the excluded
  06-10 London 0.015 fill). The recycle-bid check confirms deadness only *after*
  the fact. A pre-trade guard — corroborate deadness before lifting a NO far below
  ~0.95 — would block the bug class without giving up the real captures (which
  also clear the bar once corroborated). One config knob.
- **Resolution-source remains the sharp edge.** The whole strategy still hinges on
  the running max matching *that market's* resolution source. A thin market is
  precisely where a station/source mismatch would go unnoticed and turn a "free"
  capture into a $0 settlement. Keep the discovery cross-check strict.

## E. It is not a latency race — a faster connection wins nothing
Asked whether a faster connection would have opened opportunities on 2026-06-29
(the first day with the new frontier price-tracking): **no.** Decomposing that
day across 34 cities (`prices.jsonl` + `opportunities.jsonl`):

- **Only Shenzhen** had a single post-death tick where the dead-NO was still
  < 0.98 and liftable. We hit it **0.3s** after it appeared, at the floor price
  (0.929), and it stayed open **12 minutes** — ~3,000× any plausible connection
  latency. Nothing about our pipe limited that capture (bankroll did — see §C).
- In **24 of 34 cities** the market priced the dead-NO up to ≥ 0.98 a **median of
  ~1,005s (~17 min)** *before our running-max even reached that bracket* — i.e.
  before we were permitted to treat it as dead at all.
- Our own METAR detection lag (obs-time → we log the new reading) was **median
  277s, p90 438s**. We hot-poll at 1s, so almost all of that is the METAR's own
  ~160–490s **publish** delay — structural, untouchable by a faster connection.

Two independent reasons connection speed cannot help: (1) we may only lift a
dead-NO once `temp < running_max`, and the running-max only moves when the METAR
*publishes* — a faster pipe shaves seconds off a ~270s structural wait; (2) the
market does not use the METAR at all — it nowcasts and prices the NO dead ~17 min
ahead of our confirmation. We are structurally last to know. **Lower latency buys
nothing here; a bigger bankroll (§C) does.**

## F. Working hypothesis — the edge is a *forecast-surprise* in a thin book
Proposed by the operator, and consistent with the 06-29 data: the markets price a
bracket's NO toward $1 early when the **forecast** says higher readings are
near-certain. That yields two distinct claims.

1. **Captures come from "surprise" overshoots of the forecast peak** (e.g. forecast
   high 25, an actual 26 lands). The dead-NO edge concentrates on the bracket(s)
   *near the forecast peak* — the ones that still carry real YES probability (NO
   priced well under ~0.97) right up until a higher reading passes them. A bracket
   the forecast already knew was sub-peak has its NO pinned at ~0.99 long before
   (the §E "priced-out ~17 min early" cohort) — no YES value to unwind, no window.
   The overshoot is what flips a *cheap-NO, near-peak* bracket into a dead one, and
   thin liquidity is what makes the repricing slow enough to lift.

   *Supporting evidence (Shenzhen 2026-06-29):* the running max climbed
   27→28→29→**30** (30 landed 03:05:44); we bought bracket 29 the same second with
   its NO still at **0.93** (≈7% residual YES) — a recent plausible-peak bracket
   the thin book had not yet repriced — not a long-dead 0.99 bracket. The sampled
   frontier band (30/31/32) still carried up to ~10% YES on 30. This predicts the
   edge should correlate with **actual-high-minus-forecast-high > 0** and with
   bracket distance below the forecast peak; worth logging the forecast peak per
   city to test directly.

2. **The early-priced NO may be *over*-valued (a longshot, and a different game).**
   If the market pins a near-peak bracket's NO to ~$1 *before* the day's high is
   actually in, it can be wrong — the high may never reach the next bracket, so the
   "dead" NO was really still live and the YES was underpriced. Capturing that
   means **buying the forward YES / selling the over-pinned NO ahead of the METAR**
   — a *predictive, directional* bet with real downside and the official-vs-METAR
   resolution risk. That is explicitly the `weatherTrader` game and a **spec
   non-goal** (§14) for this bot. Recorded as a hypothesis only; do not build it
   into the dead-bracket bot.
