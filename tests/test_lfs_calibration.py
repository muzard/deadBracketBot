"""LFS (Shenzhen / Lau Fau Shan) calibration collectors + prediction shadow.

Parsers, dedupe, revision detection, the WU predictor, and the worker's
scheduling — all against canned payloads/fixtures. NO live network anywhere
(fetch callables are plain lambdas/fakes).
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from dead_bracket.config import Config
from dead_bracket.lfs_calibration import (
    HKT,
    Hko1MinCollector,
    Hko1MinReading,
    LfsCalibrationWorker,
    TwcDailyCollector,
    TwcRowCollector,
    WuPredictor,
    append_jsonl,
    parse_hko_1min_csv,
    predict_wu_integer,
    read_jsonl,
    twc_version_tuple,
)
from dead_bracket.main import build_calibration_workers
from dead_bracket.reporter import Reporter

CSV = (
    "﻿Date time,Automatic Weather Station,Air Temperature(degree Celsius)\n"
    "202607072150,Chek Lap Kok,27.0\n"
    "202607072150,Lau Fau Shan,24.9\n"
    "202607072150,Shek Kong,N/A\n"
)
NOW = datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc)


def _clock(t=NOW):
    return lambda: t


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------
def test_parse_csv_extracts_station_and_converts_hkt_to_utc():
    r = parse_hko_1min_csv(CSV, "Lau Fau Shan")
    assert r is not None
    assert r.temp_c == 24.9
    assert r.obs_time_raw == "202607072150"
    # 21:50 HKT == 13:50 UTC
    assert r.obs_time_utc == datetime(2026, 7, 7, 13, 50, tzinfo=timezone.utc)


def test_parse_csv_handles_bom_case_and_whitespace_variants():
    assert parse_hko_1min_csv(CSV, "  lau fau shan ") is not None


def test_parse_csv_na_missing_station_and_junk_are_none():
    assert parse_hko_1min_csv(CSV, "Shek Kong") is None       # N/A reading
    assert parse_hko_1min_csv(CSV, "Nowhere") is None          # absent station
    assert parse_hko_1min_csv("", "Lau Fau Shan") is None
    assert parse_hko_1min_csv(None, "Lau Fau Shan") is None
    assert parse_hko_1min_csv("garbage,x\n1,2\n", "Lau Fau Shan") is None


def test_parse_csv_rejects_malformed_datetime():
    assert parse_hko_1min_csv("202613990000,Lau Fau Shan,24.9\n", "Lau Fau Shan") is None


# ---------------------------------------------------------------------------
# Component 1 — HKO 1-min collector
# ---------------------------------------------------------------------------
def _csv_at(minute_raw: str, temp: str = "24.9") -> str:
    return f"Date time,Automatic Weather Station,Air Temperature(degree Celsius)\n{minute_raw},Lau Fau Shan,{temp}\n"


def test_hko_collector_appends_new_minutes_and_dedupes(tmp_path):
    path = str(tmp_path / "hko_1min.jsonl")
    feed = {"text": _csv_at("202607072150")}
    c = Hko1MinCollector(fetch=lambda: feed["text"], station="Lau Fau Shan",
                         path=path, clock=_clock())
    assert c.poll_once() is not None
    assert c.poll_once() is None          # same minute re-served -> no write
    feed["text"] = _csv_at("202607072151", "25.1")
    assert c.poll_once().temp_c == 25.1
    recs = read_jsonl(path)
    assert [r["obs_time_raw"] for r in recs] == ["202607072150", "202607072151"]
    assert recs[0]["temp_c"] == 24.9 and "gap_min" not in recs[1]
    assert c.polls == 3 and c.failures == 0 and c.new_records == 2


def test_hko_collector_flags_gaps_and_counts_failures(tmp_path):
    path = str(tmp_path / "hko_1min.jsonl")
    feed = {"text": _csv_at("202607072150")}
    c = Hko1MinCollector(fetch=lambda: feed["text"], station="Lau Fau Shan",
                         path=path, clock=_clock())
    c.poll_once()
    feed["text"] = None                    # fetch failure
    assert c.poll_once() is None and c.failures == 1
    feed["text"] = _csv_at("202607072200")  # 10 minutes later -> gap flagged
    c.poll_once()
    assert read_jsonl(path)[-1]["gap_min"] == 10.0


def test_hko_collector_restart_seeds_dedupe_from_file(tmp_path):
    path = str(tmp_path / "hko_1min.jsonl")
    append_jsonl(path, {"fetch_ts": NOW.isoformat(),
                        "obs_time_utc": "2026-07-07T13:50:00+00:00",
                        "obs_time_raw": "202607072150", "temp_c": 24.9})
    c = Hko1MinCollector(fetch=lambda: _csv_at("202607072150"),
                         station="Lau Fau Shan", path=path, clock=_clock())
    assert c.poll_once() is None           # already logged before the restart
    assert len(read_jsonl(path)) == 1


# ---------------------------------------------------------------------------
# Component 2 — TWC row-version collector
# ---------------------------------------------------------------------------
# A recent slot epoch: the collector rejects payloads whose newest row is
# stale (freshness guard), so fixtures must sit near the test clock.
VT = int(NOW.timestamp()) - 600


def _ob(vt, temp=24, pressure=1002.8, **kw):
    ob = {"valid_time_gmt": vt, "temp": temp, "dewPt": 24, "rh": 100,
          "pressure": pressure, "wdir": 80, "wspd": 11, "gust": None,
          "precip_total": None}
    ob.update(kw)
    return ob


def _payload(obs, txn="t1"):
    return {"metadata": {"transaction_id": txn}, "observations": obs}


def test_twc_rows_appends_only_new_versions(tmp_path):
    path = str(tmp_path / "twc_rows.jsonl")
    feed = {"p": _payload([_ob(VT)], "t1")}
    c = TwcRowCollector(fetch=lambda: feed["p"], path=path, clock=_clock())
    assert len(c.poll_once()) == 1
    feed["p"] = _payload([_ob(VT)], "t2")          # identical tuple -> nothing
    assert c.poll_once() == []
    # pressure moves 0.1 hPa with temp flat -> that IS a new version (the
    # change detector the requirements call out).
    feed["p"] = _payload([_ob(VT, pressure=1002.9)], "t3")
    new = c.poll_once()
    assert len(new) == 1 and new[0]["pressure"] == 1002.9
    recs = read_jsonl(path)
    assert len(recs) == 2 and all(r["valid_time_gmt"] == VT for r in recs)
    assert twc_version_tuple(recs[0]) != twc_version_tuple(recs[1])


def test_twc_rows_detects_stale_cache_and_counts_failures(tmp_path):
    path = str(tmp_path / "twc_rows.jsonl")
    feed = {"p": _payload([_ob(VT)], "same-txn")}
    c = TwcRowCollector(fetch=lambda: feed["p"], path=path, clock=_clock())
    c.poll_once()
    c.poll_once()                                    # same transaction_id again
    assert c.stale_responses == 1
    feed["p"] = None
    assert c.poll_once() == [] and c.failures == 1


def test_twc_rows_restart_seeds_versions_from_file(tmp_path):
    path = str(tmp_path / "twc_rows.jsonl")
    c1 = TwcRowCollector(fetch=lambda: _payload([_ob(VT)]), path=path, clock=_clock())
    c1.poll_once()
    c2 = TwcRowCollector(fetch=lambda: _payload([_ob(VT)]), path=path, clock=_clock())
    assert c2.poll_once() == []                      # version known across restart
    assert len(read_jsonl(path)) == 1


# ---------------------------------------------------------------------------
# Component 3 — daily summary capture
# ---------------------------------------------------------------------------
def _daily_payload(n=2):
    return {"observations": [
        {"valid_time_gmt": 1000 + i, "temp": 27 + i, "max_temp": None,
         "min_temp": None, "dewPt": 24, "rh": 100, "pressure": 1002.8,
         "wdir": 80, "wspd": 11, "gust": None, "precip_total": None}
        for i in range(n)]}


def test_daily_collector_fires_per_slot_once_for_prev_hkt_day(tmp_path):
    path = str(tmp_path / "twc_daily.jsonl")
    calls = []

    def fetch(day):
        calls.append(day)
        return _daily_payload()

    # 00:10 HKT on Jul 8 == 16:10 UTC Jul 7: before the 00:30 slot -> nothing.
    t = {"now": datetime(2026, 7, 7, 16, 10, tzinfo=timezone.utc)}
    c = TwcDailyCollector(fetch=fetch, path=path, capture_hkt=("00:30", "09:00"),
                          clock=lambda: t["now"])
    assert c.poll_once() == [] and calls == []
    # 00:35 HKT -> the 00:30 slot fires for the PREVIOUS HKT day (Jul 7)...
    t["now"] = datetime(2026, 7, 7, 16, 35, tzinfo=timezone.utc)
    recs = c.poll_once()
    assert calls == ["20260707"]
    assert recs[0]["hkt_date"] == "2026-07-07" and recs[0]["slot"] == "00:30"
    assert recs[0]["n_rows"] == 2
    assert c.poll_once() == []                       # ...and only once
    # 09:05 HKT -> the revision-guard slot fires for the same day.
    t["now"] = datetime(2026, 7, 8, 1, 5, tzinfo=timezone.utc)
    assert c.poll_once()[0]["slot"] == "09:00"
    assert calls == ["20260707", "20260707"]


def test_daily_collector_retries_failed_fetch_and_seeds_done_from_file(tmp_path):
    path = str(tmp_path / "twc_daily.jsonl")
    feed = {"p": None}
    t = datetime(2026, 7, 7, 16, 35, tzinfo=timezone.utc)  # 00:35 HKT Jul 8
    c = TwcDailyCollector(fetch=lambda d: feed["p"], path=path,
                          capture_hkt=("00:30",), clock=_clock(t))
    assert c.poll_once() == [] and c.failures == 1   # failed -> NOT marked done
    feed["p"] = _daily_payload()
    assert len(c.poll_once()) == 1                    # retried next poll
    # A restarted collector must not re-capture the done slot.
    c2 = TwcDailyCollector(fetch=lambda d: feed["p"], path=path,
                           capture_hkt=("00:30",), clock=_clock(t))
    assert c2.poll_once() == []


# ---------------------------------------------------------------------------
# Prediction shadow
# ---------------------------------------------------------------------------
def test_predict_wu_integer_rules():
    assert predict_wu_integer(24.4) == 24
    assert predict_wu_integer(24.5) == 25             # half_up default
    assert predict_wu_integer(24.6) == 25
    assert predict_wu_integer(24.5, "half_even") == 24
    assert predict_wu_integer(25.5, "half_even") == 26
    assert predict_wu_integer(24.9, "floor") == 24
    with pytest.raises(ValueError):
        predict_wu_integer(24.5, "nope")


def _reading(hhmm: str, temp: float, day: str = "20260707") -> Hko1MinReading:
    local = datetime.strptime(day + hhmm, "%Y%m%d%H%M").replace(tzinfo=HKT)
    return Hko1MinReading(obs_time_raw=day + hhmm,
                          obs_time_utc=local.astimezone(timezone.utc), temp_c=temp)


def _predictor(tmp_path, **kw):
    return WuPredictor(pred_path=str(tmp_path / "predictions.jsonl"),
                       eval_path=str(tmp_path / "prediction_eval.jsonl"),
                       clock=_clock(), **kw)


def test_predictor_logs_estimates_on_change_and_tracks_daily_max(tmp_path):
    p = _predictor(tmp_path)
    assert p.on_reading(_reading("1000", 24.9)) is not None
    assert p.on_reading(_reading("1010", 24.6)) is None      # 25/25 unchanged
    rec = p.on_reading(_reading("1020", 26.5))               # new row temp + max
    assert rec["pred_row_temp"] == 27 and rec["pred_daily_max"] == 27
    # A later cooler reading changes the row estimate but NEVER lowers the max.
    rec = p.on_reading(_reading("1030", 25.0))
    assert rec["pred_row_temp"] == 25 and rec["pred_daily_max"] == 27
    assert p.predicted_daily_max("2026-07-07") == 27
    recs = read_jsonl(str(tmp_path / "predictions.jsonl"))
    assert len(recs) == 3


def test_predictor_scores_twc_rows_against_in_hour_basis(tmp_path):
    p = _predictor(tmp_path)
    p.on_reading(_reading("1000", 24.9))   # 02:00 UTC hour slot
    p.on_reading(_reading("1050", 26.2))   # latest in-hour reading -> basis
    vt = int(datetime(2026, 7, 7, 2, 0, tzinfo=timezone.utc).timestamp())
    row = {"first_seen_ts": "2026-07-07T03:05:00+00:00", "valid_time_gmt": vt,
           "temp": 26}
    rec = p.on_twc_row(row)
    assert rec["pred_temp"] == 26 and rec["hit"] is True and rec["basis_in_hour"]
    assert rec["basis_temp_c"] == 26.2
    # A row WU prints differently is a scored miss, not an error.
    rec = p.on_twc_row({**row, "temp": 27})
    assert rec["hit"] is False
    assert p.row_evals == 2 and p.row_hits == 1
    assert len(read_jsonl(str(tmp_path / "prediction_eval.jsonl"))) == 2


def test_predictor_skips_unscoreable_rows(tmp_path):
    p = _predictor(tmp_path)
    vt = int(datetime(2026, 7, 7, 2, 0, tzinfo=timezone.utc).timestamp())
    # No basis reading at all -> skipped, not guessed.
    assert p.on_twc_row({"first_seen_ts": "2026-07-07T03:05:00+00:00",
                         "valid_time_gmt": vt, "temp": 26}) is None
    assert p.on_twc_row({"valid_time_gmt": vt, "temp": None}) is None
    assert p.row_evals == 0


def test_predictor_scores_daily_max_with_fallback_and_basis_count(tmp_path):
    p = _predictor(tmp_path)
    p.on_reading(_reading("1000", 24.9))
    p.on_reading(_reading("1200", 27.5))              # daily max prediction: 28
    daily = {"hkt_date": "2026-07-07",
             "observations": [{"temp": 27, "max_temp": None},
                              {"temp": 28, "max_temp": None}]}
    rec = p.on_daily(daily)                           # falls back to max(temp)
    assert rec["actual_temp"] == 28 and rec["pred_temp"] == 28 and rec["hit"]
    assert rec["actual_from"] == "max(final rows)" and rec["basis_minutes"] == 2
    # An explicit max_temp summary field wins over the row temps.
    rec = p.on_daily({**daily, "observations": [{"temp": 27, "max_temp": 29}]})
    assert rec["actual_temp"] == 29 and rec["hit"] is False
    # A day we never observed is unscoreable.
    assert p.on_daily({"hkt_date": "2026-01-01", "observations": [{"temp": 20}]}) is None


def test_predictor_seeds_state_from_hko_file_and_dedupes_after_restart(tmp_path):
    hko_path = str(tmp_path / "hko_1min.jsonl")
    for r in (_reading("1000", 24.9), _reading("1200", 27.5)):
        append_jsonl(hko_path, {"fetch_ts": NOW.isoformat(),
                                "obs_time_utc": r.obs_time_utc.isoformat(),
                                "obs_time_raw": r.obs_time_raw, "temp_c": r.temp_c})
    p = _predictor(tmp_path, seed_hko_path=hko_path)
    assert p.predicted_daily_max("2026-07-07") == 28   # replayed, no writes
    assert read_jsonl(str(tmp_path / "predictions.jsonl")) == []
    # Re-serving the last seeded reading must not emit a duplicate estimate.
    assert p.on_reading(_reading("1200", 27.5)) is None


# ---------------------------------------------------------------------------
# Worker scheduling (no threads: tick() is called directly)
# ---------------------------------------------------------------------------
class _Events:
    """Reporter stub capturing events/notices (files off, no console)."""

    def __init__(self):
        self.events = []
        self.notices = []

    def event(self, city, name, **fields):
        self.events.append((city, name, fields))

    def notice(self, city, msg):
        self.notices.append((city, msg))


def test_worker_polls_on_cadence_and_wires_predictor(tmp_path):
    rep = _Events()
    feed = {"text": _csv_at("202607072150")}
    hko = Hko1MinCollector(fetch=lambda: feed["text"], station="Lau Fau Shan",
                           path=str(tmp_path / "hko_1min.jsonl"), clock=_clock())
    predictor = _predictor(tmp_path)
    w = LfsCalibrationWorker(city="shenzhen", reporter=rep, hko=hko,
                             predictor=predictor,
                             health_path=str(tmp_path / "collector_health.jsonl"),
                             hko_poll_sec=60.0, twc_poll_sec=60.0,
                             health_every_sec=600.0, clock=_clock())
    w.tick(now=0.0)
    w.tick(now=30.0)                       # within the poll interval -> no poll
    assert hko.polls == 1
    feed["text"] = _csv_at("202607072151", "25.6")  # rounds 26 -> estimate changes
    w.tick(now=61.0)
    assert hko.polls == 2
    # The predictor saw both new readings -> estimate stream has both rows.
    assert len(read_jsonl(str(tmp_path / "predictions.jsonl"))) == 2
    # Health flush happened on the first tick (counters + reporter event).
    health = read_jsonl(str(tmp_path / "collector_health.jsonl"))
    assert {r["collector"] for r in health} == {"hko_1min", "predictor"}
    assert any(name == "calibration_health" for _c, name, _f in rep.events)


def test_worker_absorbs_collector_exceptions_as_events(tmp_path):
    rep = _Events()

    def boom():
        raise RuntimeError("feed exploded")

    hko = Hko1MinCollector(fetch=boom, station="Lau Fau Shan",
                           path=str(tmp_path / "hko_1min.jsonl"), clock=_clock())
    w = LfsCalibrationWorker(city="shenzhen", reporter=rep, hko=hko,
                             clock=_clock())
    w.tick(now=0.0)                        # must not raise
    assert any(name == "calibration_error" and f.get("collector") == "hko_1min"
               for _c, name, f in rep.events)


def test_worker_captures_daily_and_notices(tmp_path):
    rep = _Events()
    t = datetime(2026, 7, 7, 16, 35, tzinfo=timezone.utc)  # 00:35 HKT Jul 8
    daily = TwcDailyCollector(fetch=lambda d: _daily_payload(),
                              path=str(tmp_path / "twc_daily.jsonl"),
                              capture_hkt=("00:30",), clock=_clock(t))
    predictor = _predictor(tmp_path)
    w = LfsCalibrationWorker(city="shenzhen", reporter=rep, twc_daily=daily,
                             predictor=predictor, clock=_clock(t))
    w.tick(now=0.0)
    assert any("captured WU daily table 2026-07-07" in m for _c, m in rep.notices)


def test_worker_start_stop_thread_lifecycle(tmp_path):
    rep = _Events()
    w = LfsCalibrationWorker(city="shenzhen", reporter=rep, clock=_clock())
    w.start()
    w.start()                              # idempotent while alive
    w.stop()                               # joins cleanly


# ---------------------------------------------------------------------------
# Config knobs + main wiring
# ---------------------------------------------------------------------------
def test_config_lfs_knobs_and_env_overrides():
    cfg = Config()
    assert cfg.lfs_calibration is False and cfg.lfs_predict_rounding == "half_up"
    cfg = Config.from_env({"LFS_CALIBRATION": "1", "TWC_API_KEY": "k123",
                           "HKO_1MIN_POLL_SEC": "30", "TWC_POLL_SEC": "45",
                           "TWC_DAILY_HKT": "01:00", "LFS_PREDICT_ROUNDING": "floor"})
    assert cfg.lfs_calibration is True and cfg.twc_api_key == "k123"
    assert cfg.hko_1min_poll_sec == 30.0 and cfg.twc_poll_sec == 45.0
    assert cfg.twc_daily_hkt == "01:00" and cfg.lfs_predict_rounding == "floor"
    assert Config.from_env({"LFS_CALIBRATION": "0"}).lfs_calibration is False
    with pytest.raises(ValueError):
        Config(lfs_predict_rounding="banker")


def _reporter():
    return Reporter("unused", stdout=io.StringIO(), stderr=io.StringIO(),
                    write_files=False)


def test_build_calibration_workers_only_for_hko_cities(tmp_path):
    from dataclasses import replace
    cfg = replace(Config.from_env({"TWC_API_KEY": "k"}), data_dir=str(tmp_path))
    workers = build_calibration_workers(cfg, _reporter(), ["paris", "shenzhen"])
    assert [w.city for w in workers] == ["shenzhen"]
    # With the key present the TWC collectors are wired in.
    w = workers[0]
    assert w._twc_rows is not None and w._twc_daily is not None
    assert w._hko is not None and w._predictor is not None


def test_build_calibration_workers_without_twc_key_is_hko_only(tmp_path):
    from dataclasses import replace
    cfg = replace(Config(), data_dir=str(tmp_path))
    workers = build_calibration_workers(cfg, _reporter(), ["shenzhen"])
    w = workers[0]
    assert w._twc_rows is None and w._twc_daily is None and w._hko is not None
