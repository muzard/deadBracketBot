"""Offline K1/K2/K3/ε calibration analysis for Shenzhen / Lau Fau Shan.

Reads the collector files under ``data/<city>/`` (see ``lfs_calibration.py``)
and writes ``data/<city>/calibration_report_<date>.md`` answering the three
unknowns that gate re-enabling Shenzhen, plus the spike-never-sampled rate ε,
the WU-prediction scorecard, an acceptance-criteria checklist, and a Tier-1
(δ, m) recommendation — or "insufficient data" with the failing gates named.

Run: ``python -m dead_bracket.tools.lfs_calibrate [--data-dir data] [--city shenzhen]``

Everything here is pure post-processing: no network, no trading. The join
logic (and its ambiguity handling) is documented in the report itself so the
numbers can be audited without reading this source.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from ..lfs_calibration import HKT, predict_wu_integer, read_jsonl

# K2 candidate rules the pair table is tested against.
RULES = ("half_up", "half_even", "floor")


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------
def _parse_ts(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def load_readings(recs: list[dict]) -> list[dict]:
    """hko_1min.jsonl -> [{t: datetime, temp: float, day: 'YYYY-MM-DD' (HKT)}]."""

    out = []
    for rec in recs:
        t = _parse_ts(rec.get("obs_time_utc"))
        temp = rec.get("temp_c")
        if t is None or temp is None:
            continue
        out.append({"t": t, "temp": float(temp),
                    "day": t.astimezone(HKT).date().isoformat()})
    out.sort(key=lambda r: r["t"])
    return out


def load_rows(recs: list[dict]) -> list[dict]:
    """twc_rows.jsonl -> [{vt, temp, first_seen, day (HKT of the row slot)}]."""

    out = []
    for rec in recs:
        vt = rec.get("valid_time_gmt")
        fs = _parse_ts(rec.get("first_seen_ts"))
        if vt is None or fs is None:
            continue
        vt = int(vt)
        out.append({
            "vt": vt,
            "temp": rec.get("temp"),
            "first_seen": fs,
            "day": datetime.fromtimestamp(vt, HKT).date().isoformat(),
        })
    out.sort(key=lambda r: (r["vt"], r["first_seen"]))
    return out


# ---------------------------------------------------------------------------
# K1 — sample grid + ingestion latency (the row-version <-> 1-min join)
# ---------------------------------------------------------------------------
JOIN_DOC = (
    "Join logic: for each TWC row version with integer temp T (slot start "
    "`valid_time_gmt`, first seen F by the 60s poller), candidate 1-min "
    "readings are those inside the row's own hour, observed at/before F, with "
    "T-1 < reading < T+1 (i.e. T is the reading's floor or ceil — plausible "
    "under any rounding rule). The MATCH is the latest candidate (TWC holds "
    "the most recent sample it ingested); `latency` = F - match time, an UPPER "
    "bound on ingestion latency (F includes up to one poll interval of our own "
    "delay). A match is AMBIGUOUS — excluded from the K2 pair table — when "
    "candidates in the 15 minutes up to the match disagree on the 0.1 °C "
    "value, since we then cannot know which reading produced T."
)


def match_versions(rows: list[dict], readings: list[dict]) -> list[dict]:
    """K1 join per the documented logic. One result dict per scoreable version."""

    out = []
    for row in rows:
        temp = row.get("temp")
        if temp is None:
            continue
        temp = int(temp)
        vt, first_seen = row["vt"], row["first_seen"]
        fs_epoch = first_seen.timestamp()
        cands = [r for r in readings
                 if vt <= r["t"].timestamp() <= fs_epoch
                 and r["t"].timestamp() < vt + 3600
                 and temp - 1 < r["temp"] < temp + 1]
        res = {"vt": vt, "temp": temp, "first_seen": first_seen, "day": row["day"],
               "matched": bool(cands)}
        if cands:
            match = max(cands, key=lambda r: r["t"])
            near = {r["temp"] for r in cands
                    if (match["t"] - r["t"]).total_seconds() <= 15 * 60}
            res.update({
                "basis_t": match["t"],
                "basis_temp": match["temp"],
                "minute": match["t"].astimezone(HKT).minute,
                "latency_sec": (first_seen - match["t"]).total_seconds(),
                "ambiguous": len(near) > 1,
            })
        out.append(res)
    return out


def k1_summary(matches: list[dict]) -> dict:
    matched = [m for m in matches if m["matched"]]
    lat = sorted(m["latency_sec"] for m in matched)
    minutes = Counter(m["minute"] for m in matched)

    def pct(p: float) -> Optional[float]:
        if not lat:
            return None
        return lat[min(len(lat) - 1, int(math.ceil(p * len(lat))) - 1)]

    return {
        "versions": len(matches),
        "matched": len(matched),
        "matched_pct": 100.0 * len(matched) / len(matches) if matches else 0.0,
        "latency_p50_sec": pct(0.50),
        "latency_p95_sec": pct(0.95),
        "minute_histogram": dict(sorted(minutes.items())),
        "ambiguous": sum(1 for m in matched if m.get("ambiguous")),
    }


# ---------------------------------------------------------------------------
# K2 — the 0.1 °C -> integer rounding rule
# ---------------------------------------------------------------------------
def k2_pairs(matches: list[dict]) -> list[tuple[float, int]]:
    """Clean (0.1 °C matched reading, WU integer) pairs (unambiguous matches)."""

    return [(m["basis_temp"], m["temp"]) for m in matches
            if m["matched"] and not m.get("ambiguous")]


def infer_rounding(pairs: list[tuple[float, int]]) -> dict:
    """Test every candidate rule against the pair table.

    A rule "fits" when it maps every matched 0.1 °C reading to the printed
    integer. x.5 readings are the only ones that discriminate half_up from
    half_even from floor-vs-round, so they are enumerated explicitly; with none
    observed the rule is undetermined at the boundary (keep δ ≥ 0.5).
    """

    contradictions = {rule: [] for rule in RULES}
    for temp, wu in pairs:
        for rule in RULES:
            if predict_wu_integer(temp, rule) != wu:
                contradictions[rule].append((temp, wu))
    x5 = [(t, w) for (t, w) in pairs if round(t * 10) % 10 == 5]
    fits = [r for r in RULES if not contradictions[r]]
    if not pairs:
        verdict = "no pairs"
    elif not fits:
        verdict = "NO candidate rule fits — join or feed problem, investigate"
    elif len(fits) == 1 and x5:
        verdict = f"exact: {fits[0]}"
    else:
        verdict = (f"consistent with {'/'.join(fits)}; "
                   + ("boundary decided by x.5 cases" if x5 else
                      "no x.5 case observed -> rule undetermined at the boundary"))
    return {"pairs": len(pairs), "contradictions": {r: len(c) for r, c in contradictions.items()},
            "contradiction_examples": {r: c[:5] for r, c in contradictions.items() if c},
            "fits": fits, "x5_cases": x5, "verdict": verdict}


# ---------------------------------------------------------------------------
# K3 — what WU's daily "High Temp" is the max OF
# ---------------------------------------------------------------------------
def k3_compare(daily_recs: list[dict], rows: list[dict], readings: list[dict],
               rule: str) -> list[dict]:
    """Per captured HKT day: WU High vs the three candidate maxima.

    ``wu_high`` prefers the LAST capture of the day (the 09:00 revision-window
    guard beats the 00:30 first pass) and, within it, any non-null ``max_temp``
    summary field, else max(final-row temp) — verified live that past days can
    carry no max_temp at all, in which case the historical table IS the final
    rows and those two candidates coincide by construction.
    """

    by_day: dict[str, dict] = {}
    for rec in daily_recs:  # file order == capture order; later capture wins
        day = str(rec.get("hkt_date"))
        by_day[day] = rec

    out = []
    for day, rec in sorted(by_day.items()):
        obs = rec.get("observations") or []
        max_temps = [o["max_temp"] for o in obs if o.get("max_temp") is not None]
        temps = [o["temp"] for o in obs if o.get("temp") is not None]
        wu_high = max(max_temps) if max_temps else (max(temps) if temps else None)

        day_rows = [r for r in rows if r["day"] == day and r["temp"] is not None]
        final_by_vt: dict[int, int] = {}
        for r in day_rows:  # sorted by (vt, first_seen) -> last version wins
            final_by_vt[r["vt"]] = int(r["temp"])
        max_final = max(final_by_vt.values()) if final_by_vt else None
        max_all = max(int(r["temp"]) for r in day_rows) if day_rows else None
        day_readings = [r for r in readings if r["day"] == day]
        max_1min = (max(predict_wu_integer(r["temp"], rule) for r in day_readings)
                    if day_readings else None)

        matches = [name for name, v in (("final_rows", max_final),
                                        ("all_versions", max_all),
                                        ("1min_rounded", max_1min))
                   if v is not None and wu_high is not None and v == wu_high]
        out.append({"day": day, "wu_high": wu_high,
                    "wu_high_from": "max_temp" if max_temps else "max(final rows)",
                    "max_final_rows": max_final, "max_all_versions": max_all,
                    "max_1min_rounded": max_1min, "matches": matches})
    return out


def k3_verdict(days: list[dict]) -> str:
    """One consistent answer across all logged days, or say it isn't."""

    scored = [d for d in days if d["wu_high"] is not None]
    if not scored:
        return "no daily captures yet"
    # The discriminating days are those where the candidates DISAGREE.
    discriminating = [d for d in scored
                      if d["max_final_rows"] != d["max_all_versions"]
                      or d["max_all_versions"] != d["max_1min_rounded"]]
    if not discriminating:
        return (f"{len(scored)} day(s) logged but candidates never diverged — "
                "K3 not yet discriminated (needs an intra-hour spike day)")
    verdicts = []
    for d in discriminating:
        if "all_versions" in d["matches"] and "final_rows" not in d["matches"]:
            verdicts.append("all_versions")
        elif "final_rows" in d["matches"] and "all_versions" not in d["matches"]:
            verdicts.append("final_rows")
        else:
            verdicts.append("inconclusive")
    if all(v == "all_versions" for v in verdicts):
        return f"all row versions count ({len(verdicts)} discriminating day(s), consistent)"
    if all(v == "final_rows" for v in verdicts):
        return f"final rows only ({len(verdicts)} discriminating day(s), consistent)"
    return f"INCONSISTENT across days: {verdicts} — do not trade on 1-min data"


# ---------------------------------------------------------------------------
# ε — the "spike never sampled" rate
# ---------------------------------------------------------------------------
def spike_sample_rate(readings: list[dict], rows: list[dict], rule: str) -> dict:
    """Fraction of 1-min upward integer crossings absent from every row version.

    A crossing is a 1-min reading whose rounded integer exceeds the day's prior
    rounded max (the exact event a Tier-1 entry would fire on). It "appeared"
    if any row version that HKT day carries temp >= that integer. Counted, not
    estimated: numerator = crossings never reaching any version, denominator =
    all crossings.
    """

    day_version_max: dict[str, int] = {}
    for r in rows:
        if r["temp"] is None:
            continue
        d = r["day"]
        day_version_max[d] = max(day_version_max.get(d, -999), int(r["temp"]))

    crossings = 0
    missed = 0
    detail: list[dict] = []
    run: dict[str, int] = {}
    for r in readings:
        n = predict_wu_integer(r["temp"], rule)
        d = r["day"]
        prior = run.get(d)
        run[d] = n if prior is None else max(prior, n)
        if prior is None or n <= prior:
            continue
        crossings += 1
        sampled = day_version_max.get(d, -999) >= n
        if not sampled:
            missed += 1
        detail.append({"day": d, "t": r["t"].isoformat(), "temp": r["temp"],
                       "integer": n, "sampled": sampled})
    rate = (missed / crossings) if crossings else None
    return {"crossings": crossings, "missed": missed, "rate": rate, "detail": detail}


# ---------------------------------------------------------------------------
# Coverage, health, prediction scorecard
# ---------------------------------------------------------------------------
def coverage_by_day(readings: list[dict]) -> list[dict]:
    """Per HKT day: observation count, modal grid interval, coverage %.

    The feed's grid is measured, not assumed (live probe 2026-07-07 saw ~10-min
    observation steps despite the "1-min" name): the expected count for a full
    day is 1440 / modal-interval-minutes.
    """

    by_day: dict[str, list[datetime]] = {}
    for r in readings:
        by_day.setdefault(r["day"], []).append(r["t"])
    out = []
    for day, ts in sorted(by_day.items()):
        ts.sort()
        gaps = [round((b - a).total_seconds() / 60.0)
                for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() > 0]
        modal = Counter(gaps).most_common(1)[0][0] if gaps else None
        expected = (1440 / modal) if modal else None
        cov = (100.0 * len(ts) / expected) if expected else None
        out.append({"day": day, "count": len(ts), "modal_interval_min": modal,
                    "coverage_pct": cov,
                    "full": cov is not None and cov >= 99.0})
    return out


def health_summary(health_recs: list[dict]) -> dict:
    """Last (cumulative) counter row per collector -> failure rates."""

    last: dict[str, dict] = {}
    for rec in health_recs:
        c = rec.get("collector")
        if c:
            last[str(c)] = rec
    out = {}
    for name, rec in sorted(last.items()):
        polls, fails = rec.get("polls"), rec.get("failures")
        rate = (100.0 * fails / polls) if polls else None
        out[name] = {**{k: v for k, v in rec.items() if k not in ("ts", "collector")},
                     "failure_pct": rate}
    return out


def prediction_scorecard(eval_recs: list[dict]) -> dict:
    rows = [r for r in eval_recs if r.get("kind") == "row"]
    daily = [r for r in eval_recs if r.get("kind") == "daily_max"]

    def score(recs: list[dict]) -> dict:
        scored = [r for r in recs
                  if r.get("pred_temp") is not None and r.get("actual_temp") is not None]
        hits = sum(1 for r in scored if r.get("hit"))
        mae = (statistics.mean(abs(float(r["pred_temp"]) - float(r["actual_temp"]))
                               for r in scored) if scored else None)
        return {"n": len(scored), "hits": hits,
                "hit_pct": 100.0 * hits / len(scored) if scored else None,
                "mae": mae}

    return {"rows": score(rows), "daily": score(daily),
            "daily_detail": [{k: r.get(k) for k in
                              ("hkt_date", "pred_temp", "actual_temp", "hit",
                               "basis_minutes")} for r in daily]}


# ---------------------------------------------------------------------------
# Tier-1 recommendation (δ margin, m sustain minutes) — or insufficient data
# ---------------------------------------------------------------------------
def recommend(k1: dict, k2: dict, k3verdict: str, eps: dict,
              coverage: list[dict]) -> dict:
    full_days = sum(1 for d in coverage if d["full"])
    gates: list[str] = []
    if full_days < 3:
        gates.append(f"only {full_days} full HKT day(s) logged (need >= 3)")
    if k1["versions"] == 0 or k1["matched_pct"] < 95.0:
        gates.append(f"K1 match rate {k1['matched_pct']:.1f}% (need >= 95%)")
    if k2["pairs"] < 30 or not k2["fits"]:
        gates.append(f"K2: {k2['pairs']} pairs, fits={k2['fits']} "
                     "(need >= 30 pairs with a zero-contradiction rule)")
    if "consistent" not in k3verdict and "count" not in k3verdict and "only" not in k3verdict:
        gates.append(f"K3 unanswered: {k3verdict}")
    if eps["crossings"] == 0:
        gates.append("epsilon: no integer crossings observed yet")
    if gates:
        return {"status": "insufficient data", "gates": gates,
                "delta": None, "m": None}

    # δ: the K2 boundary decides it. With the rule exact incl. x.5 the only
    # margin needed is instrument noise (0.2-0.3 °C); undetermined boundary
    # keeps the spec floor of 0.5.
    delta = 0.3 if (len(k2["fits"]) == 1 and k2["x5_cases"]) else 0.5
    # m: under "all versions count", sustain just needs to outlive ingestion
    # latency (p95, rounded up, +1 safety); under "final rows only" the signal
    # must survive to the hour's last sample, so m spans the sampled grid.
    p95_min = math.ceil((k1["latency_p95_sec"] or 0) / 60.0)
    if "all row versions" in k3verdict:
        m = max(3, p95_min + 1)
        basis = "all-versions: m outlives p95 ingestion latency"
    else:
        minutes = sorted(k1["minute_histogram"])
        grid = (max(minutes) - min(minutes)) // max(1, len(minutes) - 1) if len(minutes) > 1 else 10
        m = max(10, grid, p95_min + 1)
        basis = "final-rows-only: m spans the sample grid to the hour's last sample"
    return {"status": "ok", "gates": [], "delta": delta, "m": m, "basis": basis}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _fmt(v, nd: int = 1) -> str:
    if v is None:
        return "--"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def build_report(*, city: str, readings, rows, daily_recs, eval_recs,
                 health_recs, rule: str, generated: Optional[datetime] = None) -> str:
    generated = generated or datetime.now(timezone.utc)
    matches = match_versions(rows, readings)
    k1 = k1_summary(matches)
    k2 = infer_rounding(k2_pairs(matches))
    k3days = k3_compare(daily_recs, rows, readings, rule)
    k3v = k3_verdict(k3days)
    eps = spike_sample_rate(readings, rows, rule)
    cov = coverage_by_day(readings)
    health = health_summary(health_recs)
    pred = prediction_scorecard(eval_recs)
    rec = recommend(k1, k2, k3v, eps, cov)

    L: list[str] = []
    L.append(f"# LFS calibration report — {city} — {generated.date().isoformat()}")
    L.append("")
    L.append(f"Generated {generated.isoformat(timespec='seconds')} from "
             f"{len(readings)} 1-min readings, {len(rows)} TWC row versions, "
             f"{len(daily_recs)} daily captures, {len(eval_recs)} prediction evals. "
             f"Predictor rounding rule under test: `{rule}`.")
    L.append("")

    L.append("## Data coverage")
    L.append("")
    L.append("| HKT day | readings | modal grid (min) | coverage | full? |")
    L.append("|---|---|---|---|---|")
    for d in cov:
        L.append(f"| {d['day']} | {d['count']} | {_fmt(d['modal_interval_min'])} | "
                 f"{_fmt(d['coverage_pct'])}% | {'yes' if d['full'] else 'no'} |")
    if not cov:
        L.append("| (none) | | | | |")
    L.append("")
    if health:
        L.append("Collector health (cumulative):")
        L.append("")
        for name, h in health.items():
            L.append(f"- `{name}`: " + ", ".join(f"{k}={_fmt(v)}" for k, v in h.items()))
        L.append("")

    L.append("## K1 — sample grid + ingestion latency")
    L.append("")
    L.append(f"- Row versions scored: {k1['versions']}, matched: {k1['matched']} "
             f"({_fmt(k1['matched_pct'])}%), ambiguous (K2-excluded): {k1['ambiguous']}")
    L.append(f"- Latency (first-seen - matched reading, upper bound): "
             f"p50 {_fmt(k1['latency_p50_sec'], 0)}s, p95 {_fmt(k1['latency_p95_sec'], 0)}s")
    L.append(f"- Sampled minute-of-hour histogram: {k1['minute_histogram'] or '(empty)'}")
    L.append("")
    L.append(f"> {JOIN_DOC}")
    L.append("")

    L.append("## K2 — rounding rule (0.1 °C -> WU integer)")
    L.append("")
    L.append(f"- Clean pairs: {k2['pairs']}; contradictions per rule: {k2['contradictions']}")
    if k2["contradiction_examples"]:
        L.append(f"- Contradiction examples: {k2['contradiction_examples']}")
    L.append(f"- x.5 cases observed: {k2['x5_cases'] or 'none'}")
    L.append(f"- **Verdict: {k2['verdict']}**")
    L.append("")

    L.append("## K3 — what the daily High is the max of")
    L.append("")
    L.append("| HKT day | WU High (from) | max(final rows) | max(all versions) | max(1-min rounded) | High matches |")
    L.append("|---|---|---|---|---|---|")
    for d in k3days:
        L.append(f"| {d['day']} | {_fmt(d['wu_high'])} ({d['wu_high_from']}) | "
                 f"{_fmt(d['max_final_rows'])} | {_fmt(d['max_all_versions'])} | "
                 f"{_fmt(d['max_1min_rounded'])} | {', '.join(d['matches']) or '--'} |")
    if not k3days:
        L.append("| (no daily captures yet) | | | | | |")
    L.append("")
    L.append(f"**Verdict: {k3v}**")
    L.append("")

    L.append("## ε — spike-never-sampled rate")
    L.append("")
    L.append(f"- Upward integer crossings on the 1-min feed: {eps['crossings']}; "
             f"never reached any row version: {eps['missed']} "
             f"-> ε = {_fmt(eps['rate'], 3) if eps['rate'] is not None else '--'} "
             f"({eps['missed']}/{eps['crossings']})")
    misses = [d for d in eps["detail"] if not d["sampled"]]
    if misses:
        L.append(f"- Missed crossings: {misses}")
    L.append("")

    L.append("## Prediction scorecard (our WU estimate vs WU's publication)")
    L.append("")
    r, d = pred["rows"], pred["daily"]
    L.append(f"- Hourly rows: {r['n']} scored, {r['hits']} hits "
             f"({_fmt(r['hit_pct'])}%), MAE {_fmt(r['mae'], 2)} °C")
    L.append(f"- Daily max: {d['n']} scored, {d['hits']} hits "
             f"({_fmt(d['hit_pct'])}%), MAE {_fmt(d['mae'], 2)} °C")
    for row in pred["daily_detail"]:
        L.append(f"  - {row['hkt_date']}: predicted {row['pred_temp']} vs actual "
                 f"{row['actual_temp']} ({'HIT' if row['hit'] else 'MISS'}; "
                 f"basis {row['basis_minutes']} readings)")
    L.append("")

    L.append("## Acceptance checklist")
    L.append("")
    full_days = sum(1 for c in cov if c["full"])
    checks = [
        (full_days >= 3, f">= 3 full HKT days logged (have {full_days}, target 5)"),
        (all((h.get('failure_pct') or 0) < 1.0 for h in health.values()) if health else False,
         "< 1% missed polls per collector"),
        (k1["matched_pct"] >= 95.0 and k1["versions"] > 0,
         f"K1 >= 95% versions matched (have {_fmt(k1['matched_pct'])}%)"),
        (k2["pairs"] >= 30 and len(k2["fits"]) >= 1
         and min(k2["contradictions"].values() or [1]) == 0,
         f"K2 rule exact on >= 30 pairs, zero contradictions (have {k2['pairs']} pairs)"),
        ("consistent" in k3v or "count" in k3v or "only" in k3v,
         f"K3 answered consistently ({k3v})"),
        (eps["crossings"] > 0, "epsilon measured with a real numerator/denominator"),
    ]
    for ok, label in checks:
        L.append(f"- [{'x' if ok else ' '}] {label}")
    L.append("")

    L.append("## Tier-1 recommendation")
    L.append("")
    if rec["status"] == "ok":
        L.append(f"- δ (margin) = **{rec['delta']} °C**, m (sustain) = "
                 f"**{rec['m']} min** — {rec['basis']}")
        L.append("- Re-enabling Shenzhen is the operator's decision, not the "
                 "bot's: review the checklist above first.")
    else:
        L.append("**Insufficient data.** Open gates:")
        for g in rec["gates"]:
            L.append(f"- {g}")
    L.append("")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="lfs_calibrate",
        description="Offline K1/K2/K3/epsilon analysis of the LFS calibration logs.")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--city", default="shenzhen")
    ap.add_argument("--rounding", default="half_up",
                    choices=("half_up", "half_even", "floor"),
                    help="K2 rule used for the 1-min-derived maxima/crossings")
    ap.add_argument("--out", default=None,
                    help="report path (default data/<city>/calibration_report_<date>.md)")
    args = ap.parse_args(argv)

    city_dir = os.path.join(args.data_dir, args.city)
    readings = load_readings(read_jsonl(os.path.join(city_dir, "hko_1min.jsonl")))
    rows = load_rows(read_jsonl(os.path.join(city_dir, "twc_rows.jsonl")))
    daily_recs = read_jsonl(os.path.join(city_dir, "twc_daily.jsonl"))
    eval_recs = read_jsonl(os.path.join(city_dir, "prediction_eval.jsonl"))
    health_recs = read_jsonl(os.path.join(city_dir, "collector_health.jsonl"))

    report = build_report(city=args.city, readings=readings, rows=rows,
                          daily_recs=daily_recs, eval_recs=eval_recs,
                          health_recs=health_recs, rule=args.rounding)
    out = args.out or os.path.join(
        city_dir, f"calibration_report_{datetime.now(timezone.utc).date().isoformat()}.md")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
