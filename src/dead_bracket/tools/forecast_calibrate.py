"""Offline forecast-calibration report (the live go/no-go input).

Reads what the forecast path logs while running (no network):

- ``data/<city>/feed.jsonl``   — sub-hourly feed readings (un-dated)
- ``data/<city>/metar.jsonl``  — resolution METAR obs (un-dated)
- ``data/<city>/<date>/signals.jsonl``    — every signal decision + p-hat
- ``data/<city>/<date>/orders.jsonl``     — fills (fcast reason = forecast)
- ``data/<city>/<date>/settlement.jsonl`` — day-end outcomes

and reports, per city:

1. **Sensor identity** — feed readings paired with METAR obs (nearest within
   the pair window): bias + residual sigma. Acceptance: |bias| <= 0.3 degC and
   sigma <= 0.3 degC (docs/feed_integration_strategy Phase-1 gate); the
   measured sigma is the value to copy into ``CityConfig.sensor_sigma``.
2. **Latency** — receive-latency percentiles (the information lead).
3. **Calibration** — realized hit rate vs p-hat (binned, binomial CI) and the
   Brier score. Gate: Brier <= 0.02. A "hit" is the modelled event: some
   later METAR that local day printed >= the signal's target integer.
4. **Paper PnL** — forecast fills' entries vs recycle sells + settlements.

Usage:
    python -m dead_bracket.tools.forecast_calibrate [--data-dir data] [--out report.md]

Zero capital involved; run after >= a few days of ``--forecast`` paper soak.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..config import Config
from ..lfs_calibration import read_jsonl

PAIR_WINDOW_SEC = 360.0  # feed reading <-> METAR ob max separation for pairing
P_BINS = ((0.0, 0.9), (0.9, 0.95), (0.95, 0.97), (0.97, 0.98), (0.98, 0.99), (0.99, 1.01))
BIAS_GATE = 0.3
SIGMA_GATE = 0.3
BRIER_GATE = 0.02


def _dt(raw: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def pair_feed_metar(feed_rows: list[dict], metar_rows: list[dict],
                    window_sec: float = PAIR_WINDOW_SEC) -> list[tuple[float, int]]:
    """(feed temp, METAR temp) pairs: each METAR ob vs its nearest reading."""

    readings: list[tuple[datetime, float]] = []
    for r in feed_rows:
        dt = _dt(r.get("obs_time"))
        if dt is not None and isinstance(r.get("temp_c"), (int, float)):
            readings.append((dt, float(r["temp_c"])))
    readings.sort()
    pairs: list[tuple[float, int]] = []
    for m in metar_rows:
        mdt = _dt(m.get("obs_time"))
        mtemp = m.get("temp_c")
        if mdt is None or not isinstance(mtemp, (int, float)):
            continue
        best: Optional[tuple[float, float]] = None  # (gap, feed temp)
        for rdt, rtemp in readings:
            gap = abs((rdt - mdt).total_seconds())
            if gap <= window_sec and (best is None or gap < best[0]):
                best = (gap, rtemp)
        if best is not None:
            pairs.append((best[1], int(mtemp)))
    return pairs


def bias_sigma(pairs: list[tuple[float, int]]) -> Optional[tuple[float, float]]:
    """Mean and residual std-dev of (feed - METAR), or None below n=8.

    METAR integers carry rounding noise (uniform +-0.5, variance 1/12); that
    variance is subtracted so sigma estimates the SENSOR component the
    p_trust formula wants, not sensor + rounding.
    """

    if len(pairs) < 8:
        return None
    diffs = [f - m for (f, m) in pairs]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    sensor_var = max(0.0, var - 1.0 / 12.0)
    return (mean, math.sqrt(sensor_var))


def day_target_hits(metar_rows: list[dict], tz: ZoneInfo) -> dict[str, int]:
    """Local-date -> the day's max METAR integer (the resolution of 'hit')."""

    day_max: dict[str, int] = {}
    for m in metar_rows:
        mdt = _dt(m.get("obs_time"))
        temp = m.get("temp_c")
        if mdt is None or not isinstance(temp, (int, float)):
            continue
        day = mdt.astimezone(tz).date().isoformat()
        t = int(temp)
        if day not in day_max or t > day_max[day]:
            day_max[day] = t
    return day_max


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    phat = hits / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    spread = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return ((centre - spread) / denom, (centre + spread) / denom)


def score_signals(signal_rows: list[dict], day_max: dict[str, int], tz: ZoneInfo
                  ) -> tuple[list[tuple[float, bool]], float]:
    """[(p-hat, hit)] per signal + the Brier score. Hit = the day's final METAR
    max reached the signal's target (bracket temp + 1)."""

    scored: list[tuple[float, bool]] = []
    for s in signal_rows:
        p = s.get("p")
        temp = s.get("temp")
        odt = _dt(s.get("obs_time"))
        if not isinstance(p, (int, float)) or not isinstance(temp, int) or odt is None:
            continue
        day = odt.astimezone(tz).date().isoformat()
        if day not in day_max:
            continue  # no METAR record for that day -> can't score
        scored.append((float(p), day_max[day] >= temp + 1))
    brier = (sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in scored) / len(scored)
             if scored else float("nan"))
    return scored, brier


def collect_dated(city_dir: str, category: str) -> list[dict]:
    """All rows of a dated per-day category across every day dir of a city."""

    rows: list[dict] = []
    if not os.path.isdir(city_dir):
        return rows
    for day in sorted(os.listdir(city_dir)):
        path = os.path.join(city_dir, day, f"{category}.jsonl")
        if os.path.isfile(path):
            rows.extend(read_jsonl(path))
    return rows


def forecast_pnl(order_rows: list[dict], settle_rows: list[dict]) -> dict:
    """Realized paper PnL attributable to forecast fills (reason 'fcast...')."""

    buys = [o for o in order_rows
            if o.get("side") == "buy" and str(o.get("reason", "")).startswith("fcast")]
    fcast_temps = {(o.get("temp")) for o in buys}
    bought = sum(float(o.get("price", 0)) * float(o.get("size", 0)) + float(o.get("fee", 0))
                 for o in buys)
    sells = [o for o in order_rows
             if o.get("side") == "sell" and o.get("temp") in fcast_temps]
    sold = sum(float(o.get("price", 0)) * float(o.get("size", 0)) for o in sells)
    settles = [s for s in settle_rows if s.get("temp") in fcast_temps]
    settled = sum(float(s.get("pnl", 0)) for s in settles)
    losses = [s for s in settles if s.get("won") is False]
    return {"n_buys": len(buys), "bought_$": round(bought, 2),
            "sold_$": round(sold, 2), "settled_pnl_$": round(settled, 2),
            "losing_settlements": len(losses)}


def build_city_report(city: str, tz_name: str, data_dir: str) -> Optional[str]:
    city_dir = os.path.join(data_dir, city)
    feed_rows = read_jsonl(os.path.join(city_dir, "feed.jsonl"))
    if not feed_rows:
        return None
    metar_rows = read_jsonl(os.path.join(city_dir, "metar.jsonl"))
    tz = ZoneInfo(tz_name)
    lines = [f"## {city}", ""]
    lines.append(f"- feed rows: **{len(feed_rows)}**, METAR obs: **{len(metar_rows)}**")

    lat = sorted(float(r.get("latency_sec", 0)) for r in feed_rows
                 if isinstance(r.get("latency_sec"), (int, float)))
    if lat:
        def pct(q: float) -> float:
            return lat[min(len(lat) - 1, int(q * len(lat)))]
        lines.append(f"- receive latency (obs -> us): p50 **{pct(0.5):.0f}s**, "
                     f"p90 {pct(0.9):.0f}s, max {lat[-1]:.0f}s")

    pairs = pair_feed_metar(feed_rows, metar_rows)
    bs = bias_sigma(pairs)
    if bs is None:
        lines.append(f"- sensor identity: only {len(pairs)} pairs — need >= 8. **NOT GATED**")
    else:
        bias, sigma = bs
        ok = abs(bias) <= BIAS_GATE and sigma <= SIGMA_GATE
        lines.append(f"- sensor identity ({len(pairs)} pairs): bias **{bias:+.2f}** degC, "
                     f"sensor sigma **{sigma:.2f}** degC -> "
                     f"{'ACCEPT' if ok else 'REJECT (keep inflated default)'}"
                     f" [gates |bias|<={BIAS_GATE}, sigma<={SIGMA_GATE}]")
        if ok:
            lines.append(f"  - set `sensor_sigma={max(0.05, round(sigma, 2))}` for {city} in config.py")

    signal_rows = collect_dated(city_dir, "signals")
    day_max = day_target_hits(metar_rows, tz)
    scored, brier = score_signals(signal_rows, day_max, tz)
    lines.append(f"- signals: **{len(signal_rows)}** rows, {len(scored)} scoreable")
    if scored:
        ok = brier <= BRIER_GATE
        lines.append(f"- Brier score: **{brier:.4f}** -> {'PASS' if ok else 'FAIL'} "
                     f"[gate <= {BRIER_GATE}]")
        lines.append("")
        lines.append("| p-hat bin | n | hit rate | 95% CI | mean p-hat |")
        lines.append("|---|---|---|---|---|")
        for lo, hi in P_BINS:
            in_bin = [(p, h) for p, h in scored if lo <= p < hi]
            if not in_bin:
                continue
            n = len(in_bin)
            hits = sum(1 for _, h in in_bin if h)
            ci = wilson_ci(hits, n)
            mean_p = sum(p for p, _ in in_bin) / n
            flag = "" if ci[0] <= mean_p <= ci[1] else "  <-- OUT OF CI"
            lines.append(f"| {lo:.2f}-{min(hi, 1.0):.2f} | {n} | {hits / n:.3f} | "
                         f"{ci[0]:.3f}-{ci[1]:.3f} | {mean_p:.3f}{flag} |")

    pnl = forecast_pnl(collect_dated(city_dir, "orders"), collect_dated(city_dir, "settlement"))
    lines.append("")
    lines.append(f"- paper PnL: {pnl['n_buys']} forecast buys, ${pnl['bought_$']} deployed, "
                 f"${pnl['sold_$']} recycled, settled pnl ${pnl['settled_pnl_$']}, "
                 f"**{pnl['losing_settlements']} losing settlement(s)**")
    lines.append("")
    return "\n".join(lines)


def build_report(data_dir: str, cities: list[tuple[str, str]]) -> str:
    now = datetime.now(timezone.utc)
    parts = [f"# Forecast calibration report — {now.date().isoformat()}",
             "",
             "Go/no-go gates per docs/feed_integration_strategy_2026-07-02.md: "
             f"sensor |bias| <= {BIAS_GATE} & sigma <= {SIGMA_GATE}; Brier <= {BRIER_GATE}; "
             ">= 100 signals with hold rate inside the CI; positive paper EV after fees. "
             "Going live (--forecast-live) is a HUMAN decision.",
             ""]
    any_city = False
    for city, tz_name in cities:
        section = build_city_report(city, tz_name, data_dir)
        if section:
            any_city = True
            parts.append(section)
    if not any_city:
        parts.append("_No feed.jsonl found for any city — run with --forecast first._")
    return "\n".join(parts) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="forecast-calibrate", description=__doc__)
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--out", default=None, help="report path (default: data/_global/...)")
    args = ap.parse_args(argv)

    config = Config.from_env()
    cities = [(c.id, c.tz) for c in config.cities if c.feed_source]
    report = build_report(args.data_dir, cities)
    out = args.out or os.path.join(
        args.data_dir, "_global",
        f"forecast_calibration_{datetime.now(timezone.utc).date().isoformat()}.md")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"wrote {out}", file=sys.stderr)
    print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
