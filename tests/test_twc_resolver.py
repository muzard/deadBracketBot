"""L1 TWC resolver feed + L2 lock-ahead shadow + collector staleness alarms.

The Shenzhen re-enable plan's decision layers, tested with canned files and
fixed clocks — NO live network anywhere. Covers: the closed-row-only rule (the
July 8 near-miss class), backlog replay into a MetarFeed running max, post-close
revisions, staleness halts, TWC payload freshness rejection (the 5-day-old
cache response observed live), lock/score bookkeeping with restart seeding, the
worker's source-side gap alarms (the 90-min outage that showed ``failures: 0``),
the ``.env`` loader, and the ``--twc`` CLI preconditions.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dead_bracket.feeds_http import TwcResolverFetcher, synth_twc_metar
from dead_bracket.lfs_calibration import (
    Hko1MinReading,
    LfsCalibrationWorker,
    LockAheadShadow,
    TwcRowCollector,
    append_jsonl,
    read_jsonl,
)
from dead_bracket.main import build_supervisor, load_dotenv, main
from dead_bracket.metar import MetarFeed, parse_metar

NOW = datetime(2026, 7, 17, 6, 30, tzinfo=timezone.utc)  # 14:30 HKT


def _vt(hour: int, day: int = 17) -> int:
    return int(datetime(2026, 7, day, hour, tzinfo=timezone.utc).timestamp())


def _row(vt: int, temp: int, seen: datetime) -> dict:
    return {"first_seen_ts": seen.isoformat(), "valid_time_gmt": vt, "temp": temp}


def _write_rows(path, rows) -> None:
    for r in rows:
        append_jsonl(str(path), r)


class _Clocks:
    """One knob for both the monotonic and the UTC clock."""

    def __init__(self, utc: datetime):
        self.utc = utc
        self.mono = 0.0

    def advance(self, sec: float) -> None:
        self.mono += sec
        self.utc += timedelta(seconds=sec)


def _fetcher(path, clocks, **kw) -> TwcResolverFetcher:
    return TwcResolverFetcher(path=str(path), icao="ZGSZ",
                              clock=lambda: clocks.mono,
                              utc_clock=lambda: clocks.utc, ttl=0.0, **kw)


# ---------------------------------------------------------------------------
# synth_twc_metar
# ---------------------------------------------------------------------------
def test_synth_twc_metar_parses_with_slot_plus_50_and_integer_temp():
    slot = datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc)
    raw = synth_twc_metar("ZGSZ", 31, slot, "ZGSZ:9:CN")
    obs = parse_metar(raw, now=NOW)
    assert obs is not None and obs.icao == "ZGSZ" and obs.temp_c == 31
    assert obs.obs_time == slot + timedelta(minutes=50)
    assert obs.resolution_grade and "SRC=TWC" in obs.raw


def test_synth_twc_metar_negative_temp():
    slot = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)
    raw = synth_twc_metar("ZGSZ", -3, slot, "ZGSZ:9:CN")
    obs = parse_metar(raw, now=datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc))
    assert obs is not None and obs.temp_c == -3


# ---------------------------------------------------------------------------
# TwcResolverFetcher — closed rows only, backlog replay, revisions, staleness
# ---------------------------------------------------------------------------
def test_serves_only_closed_rows_and_replays_backlog_oldest_first(tmp_path):
    path = tmp_path / "twc_rows.jsonl"
    seen = NOW - timedelta(minutes=5)
    _write_rows(path, [_row(_vt(4), 26, seen), _row(_vt(5), 27, seen),
                       _row(_vt(6), 30, seen)])  # 06:xx is the OPEN hour at 06:30
    clocks = _Clocks(NOW)
    f = _fetcher(path, clocks)
    first, second = f.raw_for("ZGSZ"), f.raw_for("ZGSZ")
    assert "26/XX" in first and "27/XX" in second
    # Caught up -> keeps serving the newest CLOSED row; the open hour never leaks.
    assert "27/XX" in f.raw_for("ZGSZ")
    # The open hour closes -> it gets served.
    clocks.advance(35 * 60)  # 07:05
    assert "30/XX" in f.raw_for("ZGSZ")


def test_folds_into_metar_feed_running_max_excluding_open_hour(tmp_path):
    path = tmp_path / "twc_rows.jsonl"
    seen = NOW - timedelta(minutes=5)
    _write_rows(path, [_row(_vt(4), 26, seen), _row(_vt(5), 31, seen),
                       _row(_vt(6), 33, seen)])
    clocks = _Clocks(NOW)
    f = _fetcher(path, clocks)
    feed = MetarFeed("ZGSZ", fetch_fn=f.raw_for,
                     log_path=str(tmp_path / "metar_twc.jsonl"),
                     tz=ZoneInfo("Asia/Shanghai"), clock=lambda: clocks.utc)
    for _ in range(4):
        feed.poll_once()
    assert feed.running_max == 31          # 33 is the still-open hour: excluded
    clocks.advance(35 * 60)
    feed.poll_once()
    assert feed.running_max == 33          # now closed -> resolver truth


def test_post_close_revision_reserved_and_refolded(tmp_path):
    path = tmp_path / "twc_rows.jsonl"
    seen = NOW - timedelta(minutes=5)
    _write_rows(path, [_row(_vt(4), 26, seen)])
    clocks = _Clocks(NOW)
    f = _fetcher(path, clocks)
    assert "26/XX" in f.raw_for("ZGSZ")
    _write_rows(path, [_row(_vt(4), 27, NOW)])  # post-close upward revision
    clocks.advance(1)
    assert "27/XX" in f.raw_for("ZGSZ")


def test_stale_file_and_missing_file_return_none(tmp_path):
    path = tmp_path / "twc_rows.jsonl"
    clocks = _Clocks(NOW)
    f = _fetcher(path, clocks, stale_sec=5400.0)
    assert f.raw_for("ZGSZ") is None       # no file yet -> no activity
    _write_rows(path, [_row(_vt(4), 26, NOW - timedelta(minutes=5))])
    clocks.advance(1)
    assert f.raw_for("ZGSZ") is not None
    clocks.advance(5400 + 600)             # poller quiet past stale_sec
    assert f.raw_for("ZGSZ") is None       # -> the feed's stale-halt trips


def test_backfill_cutoff_ignores_ancient_rows(tmp_path):
    path = tmp_path / "twc_rows.jsonl"
    old_vt = _vt(4, day=14)                # 3 days old
    _write_rows(path, [_row(old_vt, 40, NOW - timedelta(minutes=5)),
                       _row(_vt(4), 26, NOW - timedelta(minutes=5))])
    f = _fetcher(path, _Clocks(NOW))
    raw = f.raw_for("ZGSZ")
    assert raw is not None and "26/XX" in raw
    assert all("40/XX" not in (f.raw_for("ZGSZ") or "") for _ in range(3))


def test_truncated_file_is_reread_and_bad_lines_skipped(tmp_path):
    path = tmp_path / "twc_rows.jsonl"
    seen = NOW - timedelta(minutes=5)
    _write_rows(path, [_row(_vt(3), 25, seen), _row(_vt(4), 26, seen)])
    clocks = _Clocks(NOW)
    f = _fetcher(path, clocks)
    assert f.raw_for("ZGSZ") is not None
    # Rotation: the file shrinks below the read offset -> re-read from the top,
    # skipping the junk line.
    path.write_text("not json\n" + json.dumps(_row(_vt(5), 28, NOW)) + "\n",
                    encoding="utf-8")
    clocks.advance(1)
    raws = {f.raw_for("ZGSZ") for _ in range(3)}
    assert any(r and "28/XX" in r for r in raws)


# ---------------------------------------------------------------------------
# TwcRowCollector — freshness rejection (the 5-day-old cached response)
# ---------------------------------------------------------------------------
def _payload(*obs, txn="t1"):
    return {"metadata": {"transaction_id": txn}, "observations": list(obs)}


def test_row_collector_rejects_stale_payload_outright(tmp_path):
    stale_vt = int((NOW - timedelta(days=5)).timestamp())
    payloads = [_payload({"valid_time_gmt": stale_vt, "temp": 30}),
                _payload({"valid_time_gmt": _vt(6), "temp": 31}, txn="t2")]
    c = TwcRowCollector(fetch=lambda: payloads.pop(0),
                        path=str(tmp_path / "twc_rows.jsonl"),
                        clock=lambda: NOW)
    assert c.poll_once() == []             # rejected: nothing written
    assert c.stale_responses == 1
    assert read_jsonl(str(tmp_path / "twc_rows.jsonl")) == []
    new = c.poll_once()                    # fresh payload flows normally
    assert len(new) == 1 and new[0]["temp"] == 31


# ---------------------------------------------------------------------------
# LockAheadShadow — locks in [h:35, h:55], scored against the closed row
# ---------------------------------------------------------------------------
def _reading(minute: int, temp: float, hour: int = 4) -> Hko1MinReading:
    t = datetime(2026, 7, 17, hour, minute, tzinfo=timezone.utc)
    return Hko1MinReading(obs_time_raw=t.strftime("%Y%m%d%H%M"),
                          obs_time_utc=t, temp_c=temp)


def test_lock_fires_in_window_with_margin_and_scores_hold(tmp_path):
    path = tmp_path / "lock_ahead.jsonl"
    la = LockAheadShadow(path=str(path), clock=lambda: NOW)
    assert la.on_reading(_reading(20, 31.4)) is None      # outside the window
    rec = la.on_reading(_reading(40, 31.4))
    assert rec is not None and rec["locked_min_row"] == 31  # floor(31.4 - 0.3)
    assert la.on_reading(_reading(45, 31.6)) is None      # K not raised
    rec2 = la.on_reading(_reading(50, 32.4))              # K raised to 32
    assert rec2 is not None and rec2["locked_min_row"] == 32
    la.on_twc_row({"valid_time_gmt": _vt(4), "temp": 31})
    scores = la.finalize_due(datetime(2026, 7, 17, 5, 6, tzinfo=timezone.utc))
    assert [s["hold"] for s in scores] == [True, False]   # 31 held, 32 missed
    assert la.locks == 2 and la.scored == 2 and la.holds == 1
    kinds = [r["kind"] for r in read_jsonl(str(path))]
    assert kinds == ["lock", "lock", "score", "score"]


def test_lock_without_row_is_unscoreable_and_grace_respected(tmp_path):
    la = LockAheadShadow(path=str(tmp_path / "lock_ahead.jsonl"),
                         clock=lambda: NOW)
    la.on_reading(_reading(40, 30.5))
    # Hour closed but inside the grace period -> not scored yet.
    assert la.finalize_due(datetime(2026, 7, 17, 5, 2, tzinfo=timezone.utc)) == []
    scores = la.finalize_due(datetime(2026, 7, 17, 5, 6, tzinfo=timezone.utc))
    assert scores[0]["hold"] is None and la.unscoreable == 1 and la.scored == 0


def test_lock_ahead_restart_seeds_pending_and_row_temps(tmp_path):
    path = tmp_path / "lock_ahead.jsonl"
    twc_path = tmp_path / "twc_rows.jsonl"
    la = LockAheadShadow(path=str(path), clock=lambda: NOW)
    la.on_reading(_reading(40, 31.4))                     # K=31
    la.on_reading(_reading(50, 32.4))                     # K=32
    la.on_twc_row({"valid_time_gmt": _vt(4), "temp": 32})
    # Score only K=31 before the "crash" (simulate by finalizing then rewriting
    # the pending K=32 lock on top — easier: score both, then append a fresh lock).
    la.finalize_due(datetime(2026, 7, 17, 5, 6, tzinfo=timezone.utc))
    append_jsonl(str(path), {"ts": NOW.isoformat(), "kind": "lock",
                             "valid_time_gmt": _vt(5), "locked_min_row": 33,
                             "basis_temp_c": 33.4})
    append_jsonl(str(twc_path), _row(_vt(5), 33, NOW))
    la2 = LockAheadShadow(path=str(path), seed_twc_path=str(twc_path),
                          clock=lambda: NOW)
    # Scored slots were not re-queued; the unscored K=33 lock was.
    scores = la2.finalize_due(datetime(2026, 7, 17, 6, 10, tzinfo=timezone.utc))
    assert len(scores) == 1
    assert scores[0]["locked_min_row"] == 33 and scores[0]["hold"] is True
    # The seeded best-K also blocks a duplicate lock at the same level.
    assert la2.on_reading(_reading(40, 33.4, hour=5)) is None


# ---------------------------------------------------------------------------
# Worker — staleness alarms + lock-ahead wiring + health rows
# ---------------------------------------------------------------------------
class _Events:
    def __init__(self):
        self.events, self.notices = [], []

    def event(self, city, name, **fields):
        self.events.append((city, name, fields))

    def notice(self, city, msg):
        self.notices.append((city, msg))


class _StuckHko:
    """Collector stub: produced one reading once, then nothing new forever."""

    def __init__(self):
        self.polls = self.failures = 0
        self.new_records = 1
        self.served = False

    def poll_once(self):
        self.polls += 1
        if not self.served:
            self.served = True
            return _reading(40, 30.0)
        return None


def test_worker_alarms_on_source_side_gap_and_recovers(tmp_path):
    rep = _Events()
    hko = _StuckHko()
    w = LfsCalibrationWorker(city="shenzhen", reporter=rep, hko=hko,
                             health_path=str(tmp_path / "h.jsonl"),
                             hko_poll_sec=60.0, hko_stall_sec=1500.0,
                             clock=lambda: NOW)
    w.tick(now=0.0)                        # reading arrives -> baseline
    w.tick(now=61.0)                       # nothing new, quiet 61s -> no alarm
    assert not any(n == "staleness_alarm" for _c, n, _f in rep.events)
    w.tick(now=1601.0)                     # quiet 1601s > 1500 -> alarm, once
    w.tick(now=1700.0)
    alarms = [f for _c, n, f in rep.events if n == "staleness_alarm"]
    assert len(alarms) == 1 and alarms[0]["collector"] == "hko_1min"
    assert any("ALARM" in m for _c, m in rep.notices)
    hko.served = False                     # source recovers
    w.tick(now=1800.0)
    assert any(n == "staleness_recovered" for _c, n, _f in rep.events)
    health = read_jsonl(str(tmp_path / "h.jsonl"))
    assert any(r.get("collector") == "hko_1min" and "stalled" in r for r in health)


def test_worker_wires_lock_ahead_and_flushes_its_health(tmp_path):
    rep = _Events()
    hko = _StuckHko()
    row = {"valid_time_gmt": _vt(4), "temp": 30}

    class _Rows:
        polls = failures = new_records = stale_responses = 0

        def poll_once(self):
            return [dict(row, first_seen_ts=NOW.isoformat())]

    la = LockAheadShadow(path=str(tmp_path / "lock_ahead.jsonl"),
                         clock=lambda: NOW)
    w = LfsCalibrationWorker(city="shenzhen", reporter=rep, hko=hko,
                             twc_rows=_Rows(), lock_ahead=la,
                             health_path=str(tmp_path / "h.jsonl"),
                             clock=lambda: NOW)
    w.tick(now=0.0)
    assert la.locks == 1                   # the in-window reading locked
    assert la._row_temp[_vt(4)] == 30      # the row reached the shadow
    health = read_jsonl(str(tmp_path / "h.jsonl"))
    assert any(r.get("collector") == "lock_ahead" for r in health)


# ---------------------------------------------------------------------------
# .env loader + --twc CLI preconditions + supervisor wiring
# ---------------------------------------------------------------------------
def test_load_dotenv_sets_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nTWC_API_KEY='abc123'\nEXISTING=new\n\nBAD LINE\n",
                   encoding="utf-8")
    monkeypatch.delenv("TWC_API_KEY", raising=False)
    monkeypatch.setenv("EXISTING", "old")
    assert load_dotenv(str(env)) == 1
    assert os.environ["TWC_API_KEY"] == "abc123"
    assert os.environ["EXISTING"] == "old"        # real env always wins
    monkeypatch.delenv("TWC_API_KEY", raising=False)
    assert load_dotenv(str(tmp_path / "missing.env")) == 0


def test_main_refuses_twc_without_key_or_calibrate(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)                   # no repo .env in reach
    monkeypatch.delenv("TWC_API_KEY", raising=False)
    monkeypatch.delenv("LFS_CALIBRATION", raising=False)
    assert main(["--twc", "--calibrate"]) == 2
    assert "TWC_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("TWC_API_KEY", "k")
    assert main(["--twc"]) == 2                   # key but no --calibrate
    assert "--calibrate" in capsys.readouterr().err


def test_build_supervisor_twc_resolver_feed_for_shenzhen(tmp_path):
    import io
    from dataclasses import replace

    from dead_bracket.config import Config
    from dead_bracket.reporter import Reporter
    from dead_bracket.risk import RiskManager

    cfg = replace(Config.from_env({"TWC_API_KEY": "k"}), data_dir=str(tmp_path))
    rep = Reporter("unused", stdout=io.StringIO(), stderr=io.StringIO(),
                   write_files=False)
    sup = build_supervisor(cfg, rep, RiskManager(cfg), ["shenzhen"],
                           use_twc=True, use_hko=True,
                           no_trade_cities=["shenzhen"])
    worker = sup._workers[0]
    # --twc wins over --hko: the feed logs to metar_twc.jsonl, and the fetch is
    # the file-driven resolver fetcher (no HKO/ZGSZ in the trading path).
    assert worker._metar._log_path.endswith("metar_twc.jsonl")
    assert isinstance(worker._metar._fetch_fn.__self__, TwcResolverFetcher)


def test_resolver_fetcher_reads_the_general_wu_rows_schema(tmp_path):
    """wu_rows.jsonl (ResolverRowCollector) stamps 'ts'; twc_rows.jsonl
    (calibration TwcRowCollector) stamps 'first_seen_ts'. The fetcher serves
    both, so resolver-sourced cities can run off the general poller."""

    import json
    from datetime import datetime, timedelta, timezone

    from dead_bracket.feeds_http import TwcResolverFetcher

    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    slot = now - timedelta(hours=2)  # closed
    path = tmp_path / "wu_rows.jsonl"
    path.write_text(json.dumps({
        "ts": (now - timedelta(minutes=5)).isoformat(),
        "valid_time_gmt": int(slot.timestamp()),
        "temp": 26,
        "obs_name": "Seoul",
    }) + "\n", encoding="utf-8")

    f = TwcResolverFetcher(path=str(path), icao="RKSI", location="RKSI:9:KR",
                           utc_clock=lambda: now)
    raw = f.raw_for("RKSI")
    assert raw is not None and "RKSI" in raw and "26/" in raw


def test_noaa_rows_are_servable_immediately_but_twc_rows_wait_for_the_hour(tmp_path):
    """settle_sec is the whole difference between the two providers.

    TWC publishes a REVISING hourly aggregate, so its current-hour row must
    close before it can feed a running max (the 2026-07-08 near-miss was an
    intra-hour value that later vanished). NOAA/Synoptic rows are observations —
    nothing revises them — so waiting an hour would throw away precisely the
    window worth trading.
    """

    import json
    from datetime import datetime, timedelta, timezone
    from dead_bracket.feeds_http import TwcResolverFetcher

    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=10)
    path = tmp_path / "rows.jsonl"
    path.write_text(json.dumps({
        "ts": now.isoformat(),
        "valid_time_gmt": int(recent.timestamp()),
        "temp": 31,
    }) + "\n")

    def build(settle):
        return TwcResolverFetcher(
            path=str(path), icao="VILK", location="VILK",
            ttl=0.0, settle_sec=settle,
            clock=lambda: 0.0, utc_clock=lambda: now)

    # The row is 10 minutes old: NOAA serves it, TWC does not.
    assert "31/" in (build(0.0).raw_for() or "")
    assert build(3600.0).raw_for() is None
