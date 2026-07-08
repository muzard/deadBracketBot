"""tools/forecast_calibrate.py: pairing, sigma, scoring, PnL, report build."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dead_bracket.tools.forecast_calibrate import (
    bias_sigma,
    build_city_report,
    build_report,
    day_target_hits,
    forecast_pnl,
    main,
    pair_feed_metar,
    score_signals,
    wilson_ci,
)

UTC = timezone.utc
T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def feed_row(minutes: float, temp: float) -> dict:
    obs = T0 + timedelta(minutes=minutes)
    return {"obs_time": obs.isoformat(), "temp_c": temp,
            "ts_recv": (obs + timedelta(minutes=2)).isoformat(),
            "latency_sec": 120.0, "src": "fmi", "station": "100968"}


def metar_row(minutes: float, temp: int) -> dict:
    return {"obs_time": (T0 + timedelta(minutes=minutes)).isoformat(), "temp_c": temp}


def test_pair_feed_metar_nearest_within_window():
    feed = [feed_row(-2, 30.4), feed_row(8, 30.9), feed_row(120, 25.0)]
    metar = [metar_row(0, 30), metar_row(30, 31), metar_row(240, 20)]
    pairs = pair_feed_metar(feed, metar)
    # METAR@0 pairs with feed@-2; METAR@30 doesn't reach feed@8 within 6 min...
    assert (30.4, 30) in pairs
    assert all(m != 20 for _, m in pairs)  # METAR@240 has nothing nearby


def test_bias_sigma_subtracts_rounding_variance():
    # Feed == METAR + 0.2 exactly: bias 0.2 and raw variance 0 -> sigma 0.
    pairs = [(20.2, 20)] * 10
    bias, sigma = bias_sigma(pairs)
    assert bias == pytest.approx(0.2)
    assert sigma == 0.0
    assert bias_sigma(pairs[:5]) is None  # < 8 pairs -> not gated


def test_day_target_hits_uses_local_day():
    tz = ZoneInfo("Asia/Tokyo")
    # 23:30 UTC Jul 7 = 08:30 JST Jul 8.
    rows = [metar_row(0, 30), {"obs_time": "2026-07-07T23:30:00+00:00", "temp_c": 26}]
    hits = day_target_hits(rows, tz)
    assert hits["2026-07-07"] == 30
    assert hits["2026-07-08"] == 26


def test_wilson_ci_sane():
    lo, hi = wilson_ci(98, 100)
    assert 0.9 < lo < 0.98 < hi <= 1.0
    assert wilson_ci(0, 0) == (0.0, 1.0)


def test_score_signals_and_brier():
    tz = ZoneInfo("UTC")
    day_max = {"2026-07-07": 31}
    signals = [
        {"p": 0.98, "temp": 30, "obs_time": T0.isoformat()},   # target 31 -> hit
        {"p": 0.98, "temp": 31, "obs_time": T0.isoformat()},   # target 32 -> miss
        {"p": 0.98, "temp": 30, "obs_time": "2026-07-09T00:00:00+00:00"},  # no data
        {"temp": 30},  # malformed
    ]
    scored, brier = score_signals(signals, day_max, tz)
    assert [h for _, h in scored] == [True, False]
    assert brier == pytest.approx(((0.98 - 1) ** 2 + (0.98 - 0) ** 2) / 2)


def test_forecast_pnl_isolates_fcast_fills():
    orders = [
        {"side": "buy", "temp": 30, "price": 0.9, "size": 10, "fee": 0.1,
         "reason": "fcast p=0.98 m=0.6 tau=20"},
        {"side": "buy", "temp": 22, "price": 0.96, "size": 100, "fee": 0,
         "reason": "dead<rmax"},  # dead path: excluded
        {"side": "sell", "temp": 30, "price": 0.999, "size": 10, "fee": 0},
    ]
    settles = [{"temp": 30, "pnl": 0.0, "won": True},
               {"temp": 22, "pnl": 4.0, "won": True}]
    pnl = forecast_pnl(orders, settles)
    assert pnl["n_buys"] == 1
    assert pnl["bought_$"] == pytest.approx(9.1)
    assert pnl["sold_$"] == pytest.approx(9.99)
    assert pnl["losing_settlements"] == 0


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def make_city_data(root: str, city: str = "helsinki") -> str:
    cd = os.path.join(root, city)
    write_jsonl(os.path.join(cd, "feed.jsonl"),
                [feed_row(i * 10, 29.5 + i * 0.2) for i in range(12)])
    write_jsonl(os.path.join(cd, "metar.jsonl"),
                [metar_row(i * 30, 30 + (1 if i else 0)) for i in range(4)])
    day = T0.astimezone(ZoneInfo("Europe/Helsinki")).date().isoformat()
    write_jsonl(os.path.join(cd, day, "signals.jsonl"),
                [{"p": 0.98, "temp": 30, "obs_time": T0.isoformat(), "taken": True}])
    write_jsonl(os.path.join(cd, day, "orders.jsonl"),
                [{"side": "buy", "temp": 30, "price": 0.9, "size": 10, "fee": 0,
                  "reason": "fcast p=0.98"}])
    write_jsonl(os.path.join(cd, day, "settlement.jsonl"),
                [{"temp": 30, "pnl": 1.0, "won": True}])
    return cd


def test_build_city_report_full(tmp_path):
    make_city_data(str(tmp_path))
    rep = build_city_report("helsinki", "Europe/Helsinki", str(tmp_path))
    assert rep is not None
    assert "sensor identity" in rep
    assert "Brier" in rep
    assert "paper PnL" in rep
    assert build_city_report("nofeed", "UTC", str(tmp_path)) is None


def test_build_report_and_main(tmp_path, capsys):
    make_city_data(str(tmp_path))
    out = str(tmp_path / "report.md")
    assert main(["--data-dir", str(tmp_path), "--out", out]) == 0
    text = open(out).read()
    assert "# Forecast calibration report" in text
    assert "helsinki" in text
    # Empty data dir -> the no-data note, not a crash.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--data-dir", str(empty), "--out", str(empty / "r.md")]) == 0
    assert "No feed.jsonl found" in open(empty / "r.md").read()
