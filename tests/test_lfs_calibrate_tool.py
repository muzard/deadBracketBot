"""Offline calibration analysis (dead_bracket.tools.lfs_calibrate).

The K1 join, K2 rule inference, K3 comparison, ε and the report writer, all
against small synthetic fixtures — no network, no real data files needed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from dead_bracket.lfs_calibration import HKT, append_jsonl
from dead_bracket.tools.lfs_calibrate import (
    build_report,
    coverage_by_day,
    health_summary,
    infer_rounding,
    k1_summary,
    k2_pairs,
    k3_compare,
    k3_verdict,
    load_readings,
    load_rows,
    main,
    match_versions,
    prediction_scorecard,
    recommend,
    spike_sample_rate,
)


def _t(hh, mm, day=7):
    return datetime(2026, 7, day, hh, mm, tzinfo=timezone.utc)


def _reading(hh, mm, temp, day=7):
    t = _t(hh, mm, day)
    return {"t": t, "temp": temp, "day": t.astimezone(HKT).date().isoformat()}


def _row(hh, temp, seen_hh, seen_mm, day=7):
    vt = int(_t(hh, 0, day).timestamp())
    return {"vt": vt, "temp": temp, "first_seen": _t(seen_hh, seen_mm, day),
            "day": datetime.fromtimestamp(vt, HKT).date().isoformat()}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def test_loaders_normalise_and_skip_malformed():
    readings = load_readings([
        {"obs_time_utc": "2026-07-07T02:50:00+00:00", "temp_c": 24.9},
        {"obs_time_utc": "bad", "temp_c": 1.0},
        {"temp_c": 2.0},
    ])
    assert len(readings) == 1 and readings[0]["day"] == "2026-07-07"
    rows = load_rows([
        {"valid_time_gmt": 1783476000, "temp": 26,
         "first_seen_ts": "2026-07-08T02:05:00+00:00"},
        {"valid_time_gmt": None, "temp": 26, "first_seen_ts": "2026-07-08T02:05:00+00:00"},
    ])
    assert len(rows) == 1 and rows[0]["vt"] == 1783476000


# ---------------------------------------------------------------------------
# K1
# ---------------------------------------------------------------------------
def test_k1_matches_latest_in_hour_candidate_and_measures_latency():
    readings = [_reading(2, 0, 24.9), _reading(2, 50, 26.2), _reading(3, 0, 25.0)]
    rows = [_row(2, 26, 3, 5)]  # hour-02 row, integer 26, first seen 03:05
    m = match_versions(rows, readings)[0]
    assert m["matched"] and m["basis_temp"] == 26.2
    assert m["latency_sec"] == 15 * 60           # 02:50 -> 03:05
    s = k1_summary(match_versions(rows, readings))
    assert s["matched"] == 1 and s["matched_pct"] == 100.0
    assert s["latency_p50_sec"] == 900.0
    # 02:50 UTC == 10:50 HKT -> sampled minute-of-hour 50 (the ~HH:50 grid).
    assert s["minute_histogram"] == {50: 1}


def test_k1_unmatched_and_ambiguous_versions():
    # No reading anywhere near integer 30 -> unmatched.
    m = match_versions([_row(2, 30, 3, 5)], [_reading(2, 10, 24.9)])[0]
    assert not m["matched"]
    # Two different 0.1° values both plausible for 25 within 15 min -> ambiguous.
    readings = [_reading(2, 40, 24.8), _reading(2, 50, 25.2)]
    m = match_versions([_row(2, 25, 3, 5)], readings)[0]
    assert m["matched"] and m["ambiguous"]
    assert k2_pairs([m]) == []                    # excluded from the K2 table


def test_k1_candidates_restricted_to_the_rows_own_hour():
    # The only plausible reading is from the PREVIOUS hour -> no match.
    m = match_versions([_row(3, 26, 3, 20)], [_reading(2, 50, 26.2)])[0]
    assert not m["matched"]


# ---------------------------------------------------------------------------
# K2
# ---------------------------------------------------------------------------
def test_k2_exact_half_up_needs_an_x5_case():
    # 24.5 (even floor) is the discriminator: half_up says 25, half_even says
    # 24. An odd-floor x.5 like 25.5 -> 26 satisfies BOTH rules and decides
    # nothing — that asymmetry is exactly why x.5 cases must be enumerated.
    pairs = [(24.4, 24), (24.6, 25), (24.5, 25)]
    r = infer_rounding(pairs)
    assert r["verdict"] == "exact: half_up"
    assert r["contradictions"]["floor"] > 0 and r["contradictions"]["half_even"] > 0
    assert r["x5_cases"] == [(24.5, 25)]


def test_k2_without_x5_is_undetermined_at_boundary():
    r = infer_rounding([(24.4, 24), (24.6, 25)])
    assert "undetermined at the boundary" in r["verdict"]
    assert set(r["fits"]) == {"half_up", "half_even"}


def test_k2_no_fitting_rule_is_flagged():
    r = infer_rounding([(24.1, 25)])
    assert "NO candidate rule fits" in r["verdict"]
    assert infer_rounding([])["verdict"] == "no pairs"


# ---------------------------------------------------------------------------
# K3 + ε
# ---------------------------------------------------------------------------
def _daily(day, temps, max_temp=None):
    return {"hkt_date": day, "slot": "09:00",
            "observations": [{"valid_time_gmt": 0, "temp": t, "max_temp": max_temp,
                              "min_temp": None} for t in temps]}


def test_k3_all_versions_scenario():
    # Hour 02 spiked to 27 in an early version, final settled at 26; WU says 27.
    rows = [_row(2, 27, 2, 20), _row(2, 26, 2, 55)]
    readings = [_reading(2, 15, 26.8), _reading(2, 50, 25.8)]
    days = k3_compare([_daily("2026-07-07", [26], max_temp=27)], rows, readings, "half_up")
    d = days[0]
    assert d["wu_high"] == 27 and d["wu_high_from"] == "max_temp"
    assert d["max_final_rows"] == 26 and d["max_all_versions"] == 27
    assert d["max_1min_rounded"] == 27
    assert "all_versions" in d["matches"] and "final_rows" not in d["matches"]
    assert "all row versions count" in k3_verdict(days)


def test_k3_final_rows_scenario_and_fallback_high():
    rows = [_row(2, 27, 2, 20), _row(2, 26, 2, 55)]
    days = k3_compare([_daily("2026-07-07", [26, 25])], rows, [], "half_up")
    d = days[0]
    assert d["wu_high"] == 26 and d["wu_high_from"] == "max(final rows)"
    assert "final rows only" in k3_verdict(days)


def test_k3_non_discriminating_days_stay_open():
    rows = [_row(2, 26, 2, 20)]
    days = k3_compare([_daily("2026-07-07", [26])], rows,
                      [_reading(2, 15, 25.8)], "half_up")
    assert "not yet discriminated" in k3_verdict(days)
    assert k3_verdict([]) == "no daily captures yet"


def test_epsilon_counts_missed_crossings():
    # 25 -> 26 -> 27 on the 1-min feed: two upward crossings. Versions only
    # ever reached 26, so the 27 spike was never sampled -> eps = 1/2.
    readings = [_reading(2, 0, 24.9), _reading(2, 10, 25.7), _reading(2, 20, 26.8),
                _reading(2, 30, 25.0)]  # cooling never counts as a crossing
    rows = [_row(2, 26, 2, 25)]
    eps = spike_sample_rate(readings, rows, "half_up")
    assert eps["crossings"] == 2 and eps["missed"] == 1 and eps["rate"] == 0.5
    assert [d["integer"] for d in eps["detail"]] == [26, 27]


# ---------------------------------------------------------------------------
# Coverage / health / scorecard / recommendation
# ---------------------------------------------------------------------------
def test_coverage_measures_the_grid_not_assumes_it():
    # 10-min grid (as observed live 2026-07-07), 6 readings over 50 min.
    readings = [_reading(2, m, 25.0) for m in range(0, 60, 10)]
    cov = coverage_by_day(readings)[0]
    assert cov["modal_interval_min"] == 10
    assert cov["count"] == 6 and not cov["full"]     # 6 of 144 expected


def test_health_summary_takes_last_cumulative_row():
    h = health_summary([
        {"ts": "t1", "collector": "hko_1min", "polls": 10, "failures": 5},
        {"ts": "t2", "collector": "hko_1min", "polls": 1000, "failures": 5},
    ])
    assert h["hko_1min"]["failure_pct"] == 0.5


def test_prediction_scorecard():
    s = prediction_scorecard([
        {"kind": "row", "pred_temp": 26, "actual_temp": 26, "hit": True},
        {"kind": "row", "pred_temp": 27, "actual_temp": 26, "hit": False},
        {"kind": "daily_max", "hkt_date": "2026-07-07", "pred_temp": 28,
         "actual_temp": 28, "hit": True, "basis_minutes": 100},
    ])
    assert s["rows"]["n"] == 2 and s["rows"]["hit_pct"] == 50.0 and s["rows"]["mae"] == 0.5
    assert s["daily"]["hits"] == 1 and s["daily_detail"][0]["basis_minutes"] == 100


def _good_inputs():
    k1 = {"versions": 100, "matched": 98, "matched_pct": 98.0, "ambiguous": 0,
          "latency_p50_sec": 300.0, "latency_p95_sec": 700.0,
          "minute_histogram": {50: 98}}
    k2 = {"pairs": 40, "contradictions": {"half_up": 0, "half_even": 1, "floor": 40},
          "contradiction_examples": {}, "fits": ["half_up"],
          "x5_cases": [(25.5, 26)], "verdict": "exact: half_up"}
    eps = {"crossings": 12, "missed": 1, "rate": 1 / 12, "detail": []}
    cov = [{"day": f"2026-07-0{i}", "count": 144, "modal_interval_min": 10,
            "coverage_pct": 100.0, "full": True} for i in range(1, 5)]
    return k1, k2, eps, cov


def test_recommend_ok_path_all_versions():
    k1, k2, eps, cov = _good_inputs()
    rec = recommend(k1, k2, "all row versions count (2 discriminating day(s), consistent)",
                    eps, cov)
    assert rec["status"] == "ok"
    assert rec["delta"] == 0.3                      # rule exact incl. x.5
    assert rec["m"] == 13                           # p95 12 min -> +1 safety


def test_recommend_insufficient_names_the_gates():
    k1, k2, eps, cov = _good_inputs()
    rec = recommend({**k1, "matched_pct": 80.0}, {**k2, "pairs": 5},
                    "no daily captures yet", {**eps, "crossings": 0}, cov[:1])
    assert rec["status"] == "insufficient data"
    joined = " ".join(rec["gates"])
    assert "full HKT day" in joined and "K1" in joined and "K2" in joined
    assert "K3" in joined and "epsilon" in joined


# ---------------------------------------------------------------------------
# Report + CLI end-to-end on fixture files
# ---------------------------------------------------------------------------
def test_build_report_contains_all_sections():
    readings = [_reading(2, 0, 24.9), _reading(2, 50, 26.2)]
    rows = [_row(2, 26, 3, 5)]
    report = build_report(city="shenzhen", readings=readings, rows=rows,
                          daily_recs=[_daily("2026-07-07", [26])],
                          eval_recs=[{"kind": "row", "pred_temp": 26,
                                      "actual_temp": 26, "hit": True}],
                          health_recs=[{"ts": "t", "collector": "hko_1min",
                                        "polls": 100, "failures": 0}],
                          rule="half_up")
    for section in ("## Data coverage", "## K1", "## K2", "## K3",
                    "## ε — spike-never-sampled rate", "## Prediction scorecard",
                    "## Acceptance checklist", "## Tier-1 recommendation"):
        assert section in report
    assert "Insufficient data" in report            # tiny fixture can't pass gates


def test_cli_reads_city_dir_and_writes_report(tmp_path, capsys):
    city_dir = tmp_path / "shenzhen"
    append_jsonl(str(city_dir / "hko_1min.jsonl"),
                 {"obs_time_utc": "2026-07-07T02:50:00+00:00",
                  "obs_time_raw": "202607071050", "temp_c": 26.2})
    append_jsonl(str(city_dir / "twc_rows.jsonl"),
                 {"first_seen_ts": "2026-07-07T03:05:00+00:00",
                  "valid_time_gmt": int(_t(2, 0).timestamp()), "temp": 26})
    out = str(tmp_path / "report.md")
    assert main(["--data-dir", str(tmp_path), "--city", "shenzhen",
                 "--out", out]) == 0
    text = open(out).read()
    assert "# LFS calibration report — shenzhen" in text
    assert "1 TWC row versions" in text
    # Nothing on stdout (console policy); the path notice goes to stderr.
    captured = capsys.readouterr()
    assert captured.out == ""
