# Live trading runbook (Phase 2)

> **LIVE IS LOCKED (2026-08-18).** `--live` refuses to start unless
> `DEAD_BRACKET_ALLOW_LIVE=1` is set. On 2026-07-19 the bot emptied the wallet
> on Seoul: RKSI's METAR printed 27 °C, Weather Underground's row for the same
> instant read 26 °C (tenths pipeline: 79 °F = 26.1 °C), the 26 °C bracket was
> marked dead, and the entire bankroll bought a NO that settled to $0. Nothing
> was cross-checking our source against the table the market settles on.
>
> The failsafe now exists (`resolver_guard.py`, §0.0 below) but has **no soak
> behind it**. Before unlocking, run paper until
> `python -m dead_bracket.tools.resolver_status` shows the cities you intend to
> trade sitting in `ok` for several consecutive days with zero divergences.

> Live places **real signed orders** with **real funds**. Default mode is paper;
> nothing below happens unless you pass `--live` with a non-zero `--bankroll`.
> Validate with tiny caps on one market before scaling.

## 0.0 The resolver cross-check (the gate that replaces the old assumption)

Every city polls the resolver's **own** published table (the TWC/WU location id
in `config.RESOLVER_LOCATIONS`) into `data/<city>/wu_rows.jsonl`, and trades only
when our source has matched it on **every paired observation across the trailing
24 h**. States:

| state | meaning | trades? | clears |
|---|---|---|---|
| `ok` | full 24 h agreement, fresh rows, running maxima agree | yes | — |
| `unverified` | thin/stale data, or no resolver table (Istanbul's LTFM serves none) | no | itself, once coverage arrives |
| `divergent` | a paired ob or the day's running max disagreed | no | **only** `tools/resolver_clear <city>` |

Divergence is sticky across ticks, daily rollovers and restarts
(`data/_global/resolver_blocks.json`). Investigate before clearing — it means
our source and the resolver are different instruments, so every "certain" dead
bracket in that city is a guess.

```bash
python -m dead_bracket.tools.resolver_status -v      # who may trade, and why not
python -m dead_bracket.tools.resolver_clear --list   # what is blocked
python -m dead_bracket.tools.resolver_clear <city> [<city> ...]
```

**Coverage is measured as reach-back, not span.** The resolver publishes 26–96
min behind, so the newest ~1 h of our observations legitimately has nothing to
compare against. A city verifies when its matched history *reaches back*
`RESOLVER_MIN_REACH_HOURS` (23 h), is *fresh* within `RESOLVER_MAX_LAG_HOURS`
(3 h), and has no hole over `RESOLVER_MAX_GAP_HOURS` (3 h). Requiring a 23 h
*span* inside a 24 h window — the original rule — is arithmetically unreachable
once you subtract the publish lag, and made every city flap on how fresh the
resolver happened to be at that instant.

No `TWC_API_KEY` means nothing can be verified, so **no city trades**. That is
deliberate: fail closed.

**Two cities read the resolver instead of their METAR.** Seoul and Shenzhen
carry `resolution_source="resolver"` (`config.RESOLVER_SOURCED_CITIES`): their
running max comes from the resolver's own closed rows, because their METAR
provably does not track it — Seoul disagrees on ~0.9 % of observations (one
false-dead day in 21, the wipe), Shenzhen is a different station entirely
(Lau Fau Shan, 15 % agreement). For these the cross-check is a coverage/
freshness gate rather than a comparison: there is only one source, so there is
nothing to diverge. The other 31 measured cities agree with the resolver 100 %
and stay on METAR. A resolver-sourced city with no key or table has **no valid
source** and idles rather than falling back to a METAR known to be wrong.

Optional strict mode `RESOLVER_CONFIRM_DEAD=1` also requires the resolver's own
table to have reached the running max before any bracket below it counts as
dead. That closes the residual window (the resolver publishes late, so the
freshest print — the only one worth trading — has nothing to disagree with yet),
at the cost of the speed edge the strategy is built on. Measure it in paper
before deciding.

## 0. Prerequisites — verify before any real order

1. **Resolution stations — confirmed.** The METAR ICAOs in
   [`config.py`](src/dead_bracket/config.py) `DEFAULT_CITIES` (Paris=LFPB,
   London=EGLC, Moscow=UUWW, Milan=LIMC, Madrid=LEMD, Ankara=LTAC, Istanbul=LTFM,
   Helsinki=EFHK, Amsterdam=EHAM, Warsaw=EPWA, Munich=EDDM) have been confirmed
   against the markets. All cities include off-cycle SPECIs. If a market's
   resolution source ever changes, update the ICAO there.
2. **Daily-high timezone.** The running max resets at each city's local midnight
   (the `tz` field) — the local-day boundary the daily-high market resolves on.
   Discovery rolls to the next day's market at *each city's own* local midnight,
   so the differing timezones each get their 00:00 rollover (no global cutover).
2b. **Resolution-source cross-check (automatic).** Discovery only trades an event
   whose description names both the station ICAO and **Weather Underground**, so a
   market that silently changes resolution source is idled rather than traded.
   **Moscow (UUWW)** and **Istanbul (LTFM)** are in `WUNDERGROUND_EXEMPT_ICAOS`:
   they waive only the **Weather Underground** string (they resolve off a non-WU
   source) but still must name their station ICAO. If either market's source ever
   becomes WU-confirmable, drop it from that set in `feeds_http.py`; to add another
   non-WU city, add its ICAO to the set (the station match stays required).
2c. **Shenzhen trades only via `--twc`** (running max = the CLOSED rows of the
   WU/TWC resolver table — see README "`--twc`: the L1 resolver feed"). Never
   trade it off ZGSZ METAR or HKO rhrread: both diverge from the resolver
   (station_audit_2026-07-02, wu_lfs_discrepancy_2026-07-03). Re-enable order:
   3–5 days `--twc --no-trade shenzhen --calibrate`, then check the calibration
   report's L1 closed-row-stability section (downward post-close revisions ~0)
   and that each day's `metar_twc` running max equals WU's printed High, then
   drop `--no-trade` (operator decision). L2 lock-ahead and L3 forecast stay
   shadow/paper until their own gates pass
   (`data/shenzhen_reenable_plan_2026-07-08.md`).
3. **Run paper first** and check `data/<city>/opportunities.jsonl` actually shows
   sub-98¢ dead-NO offers with usable size. Per spec §13, only deploy live once
   the opportunity log proves real occurrences.

## 1. Install the live dependency

```bash
. .venv/bin/activate
pip install -e ".[live]"     # polymarket-client (unified SDK, beta)
```

> ⚠️ `py-clob-client` is **archived and non-functional** — the venue rejects its
> orders with `invalid order version`. The unified `polymarket-client` SDK is
> required.

## 1b. Order constraints (handled automatically)

These temperature markets are **negRisk** and use a **dynamic tick size** (0.01
early — max price 0.99 — refining to 0.001 near the extremes, where 0.999
becomes placeable). The executor handles this per order via the unified
`polymarket-client` SDK, which resolves the negRisk exchange and tick internally;
we additionally clamp the resting sell to `min(SELL_PX, 1 - tick)` and respect
the venue minimum order size. If you ever see `invalid order version`, you are on
the **archived** `py-clob-client` — uninstall it and `pip install -e ".[live]"`
to get `polymarket-client`.

## 2. Fund the wallet

Your Polymarket **proxy wallet** (the address shown in the UI under Deposit)
must hold USDC.e on Polygon and have set the standard CLOB allowances (made once
via the Polymarket UI or `client.update_balance_allowance`). Start with ~$20.

## 3. Credentials

The signing key lives **only** in the environment and is read once by the
executor; it is never logged, never written to config or telemetry.

```bash
export POLY_PRIVATE_KEY=<your Magic-exported signing key>
export POLY_FUNDER=<your Polymarket proxy wallet address>   # holds the USDC
# (optional — defaults to the signer's deposit wallet)
```

The unified SDK detects the wallet type (Magic proxy / Safe / EOA) and derives
API credentials automatically — `POLY_SIGNATURE_TYPE` is no longer needed.

## 4. Caps (`--bankroll` = % of current balance)

In live mode, [`apply_bankroll_pct`](src/dead_bracket/main.py) makes `--bankroll`
a **percentage (0-100) of your current balance**, not a dollar amount. The
balance starts from the wallet's USDC (read once at startup) and grows by
realized PnL, so the caps grow as the bot wins and shrink as it loses.

Sizing is **no longer all-in on one bracket** — that is precisely what turned
the 2026-07-19 Seoul false-dead into a total loss rather than a bad day.

| Cap | Live value (for `--bankroll P`) | Env to override |
|-----|-------------:|-----------------|
| Total exposure | `min(P%, 90%) × balance` | `MAX_TOTAL_EXPOSURE` (only when `--bankroll 0`) |
| Per-city exposure | `min(P%, 90%) × balance` | `MAX_EXPOSURE_PER_CITY` (only when `--bankroll 0`) |
| **Per-bracket exposure** | **`min(P%, 25%) × balance`** | `MAX_BRACKET_PCT` / `MAX_EXPOSURE_PER_BRACKET` |
| Deployable ceiling | `90% × balance`, minus open exposure | `MAX_DEPLOYED_PCT` |
| Shares per bracket | 500 (hard ceiling) | `MAX_SHARES_PER_BRACKET` |

e.g. with a $30 wallet, `--bankroll 50` → $15 across the book but at most $7.50
on any one bracket; after a $5 win the balance is $35 so those become $17.50 and
$8.75. Use `--balance` to override the starting balance (required in paper mode,
where there is no wallet to read). When `--bankroll 0`, sizing falls back to the
static `MAX_EXPOSURE_*` dollar caps — but once a balance is known those are
still clamped to it, so a $100 balance can never authorise a $1000 bet.

**Paper equity compounds and persists.** Cumulative realised PnL is written to
`data/_global/paper_ledger.json` and reloaded on restart, so a long soak shows a
real equity curve; `roll_day` resets only the *daily* PnL the loss breaker
reads. Delete that file to start a fresh soak.

The kill-switch is **strict** in live: any crossed/invalid book trips a global,
sticky `HALT` (you don't trade against a venue emitting broken books).

## 4b. Preflight check (reads only, never trades)

Before the first live run, verify credentials and on-chain readiness:

```bash
python -m dead_bracket.main --check
```

It builds the client, derives your API creds, and prints your signing address,
funder, USDC balance, and CLOB allowance — then exits. It places no orders. Fix
any `WARNING` (zero balance → deposit; zero allowance → do one manual UI trade)
before going live.

## 5. First validation run

Trade a single city to keep it simple, with a small bankroll percentage:

```bash
python -m dead_bracket.main --live --bankroll 20 --cities london   # risk 20% of the wallet
```

You should see only `BOUGHT` / `SOLD` / `HALT` on the console. Watch
`data/london/orders.jsonl` for the entry + the resting 99.9¢ exit.

### Confirm the fill parsing on this first trade

The post-order/`get_order` response field semantics (amount scaling,
`size_matched`) are interpreted defensively in
[`LiveExecutor`](src/dead_bracket/executor.py) (`_parse_buy_match`,
`_matched_size`) and **must be verified once against a real fill** — compare the
`BOUGHT`/`SOLD` size and price against the Polymarket UI / your order history.
The parser rejects implausible matches (avg price outside (0,1.05]) as "no fill"
rather than guessing, so it can't over-credit a position — but confirm it isn't
*under*-reporting before you scale.

## 6. Scale only after one clean entry→exit/settlement

Once a real entry, its resting exit (filled or held to $1 settlement), and the
accounting all reconcile, raise the caps via the env vars above and add cities.

## 7. Forecast path go/no-go (`--forecast-live`)

The forecast path (probabilistic NO-buys off sub-hourly feeds, `--forecast`)
never places live orders on its own: in live mode it only logs signals. Flip
`--forecast-live` ONLY after all of these hold, from a multi-day paper soak:

1. `python -m dead_bracket.tools.forecast_calibrate` reports, per city you
   intend to enable: sensor **|bias| ≤ 0.3 °C and σ ≤ 0.3 °C** (then copy the
   measured σ into that city's `sensor_sigma` in `config.py`), **Brier ≤ 0.02**,
   and ≥ 100 signals with the realized hold rate inside the binomial CI of p̂.
2. Paper PnL is positive **after fees**.
3. You have decided the Kelly fraction (`KELLY_FRACTION`, default 0.25 — the
   0.5 ceiling is enforced in config) and checked the same-ob / cross-city caps.
4. You understand the kill switch: any settled loss on a forecast bet pauses
   the whole forecast path (sticky across rollovers; restart to clear) and is a
   post-mortem, not a resume.

Start with the calibrated keyless cities (helsinki/tokyo/singapore) or paris
once `MF_API_KEY` exists; amsterdam requires a `KNMI_API_KEY` plus a one-time
live verification of the KNMI parser (it was written against the documented
EDR format only).

## What I (the assistant) will not do

I implemented and configured the signed-execution code, but I do not fund
wallets or place orders. You run `--live`, fund the wallet, and perform the
validation trade.
