# Live trading runbook (Phase 2)

> Live places **real signed orders** with **real funds**. Default mode is paper;
> nothing below happens unless you pass `--live` with a non-zero `--bankroll`.
> Validate with tiny caps on one market before scaling.

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
realized PnL, so every cap — all three are equal, all-in on a single
high-conviction dead bracket — grows as the bot wins and shrinks as it loses.

| Cap | Live value (for `--bankroll P`) | Env to override |
|-----|-------------:|-----------------|
| Total exposure | `P% × balance` | `MAX_TOTAL_EXPOSURE` (only when `--bankroll 0`) |
| Per-city exposure | `P% × balance` | `MAX_EXPOSURE_PER_CITY` (only when `--bankroll 0`) |
| **Per-bracket exposure** | **`P% × balance`** (all-in) | `MAX_EXPOSURE_PER_BRACKET` (only when `--bankroll 0`) |
| Shares per bracket | 500 (hard ceiling) | `MAX_SHARES_PER_BRACKET` |

e.g. with a $30 wallet, `--bankroll 50` → max bet $15; after a $5 win the
balance is $35 so the next bet may be up to $17.50. Use `--balance` to override
the starting balance (required in paper mode, where there is no wallet to read).
When `--bankroll 0`, sizing falls back to the static `MAX_EXPOSURE_*` dollar caps.

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

## What I (the assistant) will not do

I implemented and configured the signed-execution code, but I do not fund
wallets or place orders. You run `--live`, fund the wallet, and perform the
validation trade.
