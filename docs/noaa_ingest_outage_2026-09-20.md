# Root cause: the Singapore false dead of 2026-09-20

Measured 2026-09-23 from the soak's own logs, NOAA's table fetched fresh, and
the markets' actual resolutions.

## What happened

The bot bought **500 NO on Singapore's 31 °C bracket at an average 0.378**
(10:25Z). The market resolved **YES on 31**, so the NO paid $0: a real loss
of **−$191.48**. Paper booked it as **+$308.52**.

## The cause: a worldwide NOAA ingest outage

The market resolves on `weather.gov/wrh/timeseries?site=<ICAO>`, which reads
Synoptic/MesoWest. Comparing every METAR we received over the soak
(2026-09-14 18:00Z → 09-23) against that table:

| | |
|---|---|
| METARs received | 11,523 |
| absent from NOAA's table | **250 (2.17%)** |
| seen by our poller and later deleted | **0** — they were never ingested |

The absences are not scattered. They are two **feed-wide outages**, every
station at once:

| window (UTC) | stations missing in the same 30 min | effect |
|---|---|---|
| **09-20 02:30 → 07:30** (~5 h) | up to **31 of 34** | the afternoon peak across East Asia |
| **09-22 10:30 → 11:30** (~1 h) | up to **28 of 34** | midday in Europe |

plus a chronic station-level trickle at **Manila** (25 of 220, 11%).

Because a missing reading can only *lower* the resolver's daily max, the 09-20
outage changed the final high of **five markets — and all five resolved on
NOAA's lower value, not the METAR's**:

| city | METAR max | NOAA max | resolved YES on |
|---|---|---|---|
| Singapore | 32 | 31 | **31** |
| Beijing | 30 | 28 | **28** |
| Guangzhou | 37 | 36 | **36** |
| Qingdao | 30 | 29 | **29** |
| Manila | 33 | 32 | **32** |

On both outage days **every one of the 34 markets resolved exactly on NOAA's
table.** The 09-22 outage changed no final high: the European peaks came
after it. But we traded Ankara, London and Munich on the missing 10:50Z/11:20Z
readings — they paid only because the 11:50Z reading happened to go higher.

## Why nothing stopped it

1. **The running max came from the METAR.** Singapore's 32 °C at 03:30Z–04:30Z
   was a valid METAR that NOAA never ingested. The bot called bracket 31 dead.
2. **The resolver guard could not see it.** It pairs observations both sides
   hold; a reading NOAA never ingested has nothing to pair against. Singapore's
   hole happened to be patchy (03:30–05:00 and 06:30 missing, 05:30/06:00
   present), so the largest gap in matched history was 2.5 h — under the 3 h
   limit — and the guard stayed `ok`. Paired agreement over the whole soak was
   4,193 / 4,193; the check that matters here is a different one.
3. **`resolver_confirm_dead` was off** — the one rule that requires the
   resolver's own table to reach the running max before a bracket is dead.
4. **The market was telling us.** The NO was offered at 0.149 at entry and fell
   to 0.09 through the day: the market, reading NOAA, priced 31 at ~85–91% to
   win. The fill walked the book to an average of 0.378.
5. **Size was unclamped.** `--balance 100` was discarded when percentage sizing
   was off, so a "$100" account put $189 into one bracket.
6. **Paper could not show the loss.** It settled against our own running max —
   and at 00:00 UTC, which for Singapore is 08:00 the next day, against that
   morning's partial max. A dead-path position could not lose on paper.
7. **The recycle alarm went silent.** Singapore's resolver rows later went
   stale, entries were gated, and `process_book` returned before the alarm
   check.

## What changed (branch `fix/resolver-confirmed-dead`, on PR #4)

| cause | fix |
|---|---|
| 1–3 | `resolver_confirm_dead` **on**: a bracket is dead only once NOAA's own table reaches our max; the resolver is polled every 60 s while it is behind; every wait is logged with its length |
| 5 | the balance is resolved whenever given; $100 caps one bracket at 25/0.98 |
| 6 | each city settles at its **own** midnight against the resolver's max, and the PnL is then booked from the market's **actual resolution** (`reconcile.py`) — paper and live |
| 7 | position maintenance and the alarm run before the entry gates |

## The correction this forces

`resolution_source_migration_2026-09-14.md` §2 says NOAA's series "IS the
METAR". That holds observation-for-observation *when NOAA has the
observation*. It does not always: it drops readings, in bulk, and a dropped
reading is invisible to any pairwise comparison. **The METAR is not a safe
proxy for the resolver; only the resolver's own table is.**
