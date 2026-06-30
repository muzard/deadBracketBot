"""METAR parse / running-max / revision-guard / restart tests (spec §6, §9, §12)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dead_bracket.metar import MetarFeed, RunningMax, parse_metar

NOW = datetime(2026, 6, 3, 16, 0, tzinfo=timezone.utc)
PARIS = ZoneInfo("Europe/Paris")


def test_parse_basic_metar():
    raw = "LFPB 031530Z 24015KT 9999 FEW040 23/10 Q1018 NOSIG"
    obs = parse_metar(raw, now=NOW)
    assert obs is not None
    assert obs.icao == "LFPB"
    assert obs.temp_c == 23
    assert obs.is_correction is False
    assert obs.obs_time == datetime(2026, 6, 3, 15, 30, tzinfo=timezone.utc)


def test_parse_negative_temp_and_cor():
    raw = "EGLC 031530Z COR 02010KT 9999 OVC020 M03/M05 Q1000"
    obs = parse_metar(raw, now=NOW)
    assert obs is not None
    assert obs.temp_c == -3
    assert obs.is_correction is True


def test_parse_handles_month_rollover():
    raw = "LFPB 311530Z 24015KT 9999 FEW040 18/10 Q1018"
    obs = parse_metar(raw, now=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc))
    assert obs is not None
    assert obs.obs_time.month == 5
    assert obs.obs_time.day == 31


def test_parse_unparseable_returns_none():
    assert parse_metar("") is None
    assert parse_metar("LFPB 031530Z NIL") is None  # no temp group
    assert parse_metar("23/10 no time here") is None  # no time group


def test_running_max_monotonic():
    rm = RunningMax()
    rm.update(parse_metar("LFPB 031200Z 20/10 Q1018", now=NOW))
    assert rm.value == 20
    rm.update(parse_metar("LFPB 031230Z 23/10 Q1018", now=NOW))
    assert rm.value == 23
    # A later, lower (non-corrected) reading must not lower the max.
    rm.update(parse_metar("LFPB 031300Z 21/10 Q1018", now=NOW))
    assert rm.value == 23
    assert rm.revision_detected is False


def test_downward_revision_guard_flags_but_keeps_max():
    rm = RunningMax()
    rm.update(parse_metar("LFPB 031200Z 20/10 Q1018", now=NOW))
    rm.update(parse_metar("LFPB 031230Z 24/10 Q1018", now=NOW))
    assert rm.value == 24
    # A COR for the 12:30 observation revises it down to 21; supported max -> 21.
    rm.update(parse_metar("LFPB 031230Z COR 21/10 Q1018", now=NOW))
    assert rm.value == 24  # running max not lowered (do not un-dead)
    assert rm.revision_detected is True


def test_feed_running_max_and_dedup(tmp_path):
    raws = iter([
        "LFPB 031200Z 20/10 Q1018",
        "LFPB 031200Z 20/10 Q1018",  # duplicate -> deduped
        "LFPB 031230Z 23/10 Q1018",
    ])
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: next(raws, None),
                     log_path=str(tmp_path / "metar.jsonl"), clock=lambda: NOW)
    feed.poll_once()
    feed.poll_once()  # dedup
    feed.poll_once()
    assert feed.running_max == 23


def test_feed_stale_detection():
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: None, clock=lambda: NOW)
    assert feed.is_stale(stale_min=45) is True  # never updated
    feed2 = MetarFeed("LFPB", fetch_fn=lambda _i: "LFPB 031530Z 23/10 Q1018",
                      clock=lambda: NOW)
    feed2.poll_once()
    assert feed2.is_stale(stale_min=45, now=NOW) is False
    later = datetime(2026, 6, 3, 17, 0, tzinfo=timezone.utc)  # +60 min
    assert feed2.is_stale(stale_min=45, now=later) is True


def test_restart_safety_seed_from_log(tmp_path):
    log = str(tmp_path / "metar.jsonl")
    raws = iter(["LFPB 031200Z 20/10 Q1018", "LFPB 031230Z 23/10 Q1018"])
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: next(raws, None), log_path=log,
                     clock=lambda: NOW)
    feed.poll_once()
    feed.poll_once()
    assert feed.running_max == 23

    # New process: seed from the log; running max survives the restart.
    feed2 = MetarFeed("LFPB", fetch_fn=lambda _i: "LFPB 031230Z 23/10 Q1018",
                      log_path=log, clock=lambda: NOW)
    assert feed2.running_max == 23
    # The already-seen observation is deduped, so no duplicate fold-in.
    feed2.poll_once()
    assert feed2.running_max == 23


def test_running_max_resets_at_local_midnight():
    rm = RunningMax(PARIS)
    # 2026-06-03 12:00 UTC == 14:00 Paris -> trading day 2026-06-03.
    d1 = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    rm.update(parse_metar("LFPB 031200Z 24/10 Q1018", now=d1), now=d1)
    assert rm.value == 24
    assert rm.trading_day.isoformat() == "2026-06-03"

    # Next day 06:00 UTC == 08:00 Paris -> day rolled, max reset, new reading 12.
    d2 = datetime(2026, 6, 4, 6, 0, tzinfo=timezone.utc)
    rm.update(parse_metar("LFPB 040600Z 12/08 Q1018", now=d2), now=d2)
    assert rm.value == 12
    assert rm.trading_day.isoformat() == "2026-06-04"


def test_stale_prior_day_observation_ignored():
    rm = RunningMax(PARIS)
    today = datetime(2026, 6, 4, 6, 0, tzinfo=timezone.utc)
    # Establish today's day first.
    rm.roll(today)
    # A late-arriving yesterday observation (12:00 UTC on the 3rd == 14:00 Paris,
    # local day 06-03) must not fold into today's (06-04) running max.
    yest = parse_metar("LFPB 031200Z 30/10 Q1018", now=today)
    assert yest.obs_time == datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    rm.update(yest, now=today)
    assert rm.value is None  # ignored: different local day


def test_roll_with_no_observation_resets_at_midnight():
    rm = RunningMax(PARIS)
    d1 = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    rm.update(parse_metar("LFPB 031200Z 24/10 Q1018", now=d1), now=d1)
    assert rm.value == 24
    rm.roll(datetime(2026, 6, 4, 6, 0, tzinfo=timezone.utc))  # midnight passed
    assert rm.value is None  # reset even though no new observation arrived


def test_running_max_read_rolls_at_midnight_without_poll():
    # Even with no new poll, reading running_max after local midnight reflects
    # the reset (guards the "didn't reset at midnight" failure mode).
    clock = {"t": datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)}
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: "LFPB 031200Z 24/10 Q1018",
                     tz=PARIS, clock=lambda: clock["t"])
    feed.poll_once()
    assert feed.running_max == 24
    clock["t"] = datetime(2026, 6, 4, 6, 0, tzinfo=timezone.utc)  # next local day
    assert feed.running_max is None  # reset on read, no poll needed


def test_feed_resets_across_midnight():
    clock = {"t": datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)}
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: "LFPB 031200Z 24/10 Q1018",
                     tz=PARIS, clock=lambda: clock["t"])
    feed.poll_once()
    assert feed.running_max == 24
    # Advance past local midnight; the next poll returns no fresh data but rolls.
    clock["t"] = datetime(2026, 6, 4, 6, 0, tzinfo=timezone.utc)
    feed._fetch_fn = lambda _i: None
    feed.poll_once()
    assert feed.running_max is None


def test_station_isolation_rejects_foreign_reading():
    # An EGLC (London) reading must never fold into an LFPB (Paris) feed.
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: "EGLC 031200Z 30/10 Q1018",
                     tz=PARIS, clock=lambda: datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc))
    assert feed.poll_once() is None
    assert feed.running_max is None
    assert feed.foreign_rejected == 1


def test_restart_does_not_carry_yesterdays_high(tmp_path):
    log = str(tmp_path / "metar.jsonl")
    d1 = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: "LFPB 031200Z 28/10 Q1018",
                     tz=PARIS, log_path=log, clock=lambda: d1)
    feed.poll_once()
    assert feed.running_max == 28

    # Restart the next day: seeding must NOT bring yesterday's 28 into today.
    d2 = datetime(2026, 6, 4, 8, 0, tzinfo=timezone.utc)
    feed2 = MetarFeed("LFPB", fetch_fn=lambda _i: None, tz=PARIS, log_path=log,
                      clock=lambda: d2)
    assert feed2.running_max is None


def test_parse_flags_speci():
    routine = parse_metar("METAR LFPB 081130Z 22/11 Q1015", now=NOW)
    assert routine.is_speci is False and routine.temp_c == 22
    speci = parse_metar("SPECI LFPB 081145Z 24/11 Q1015", now=NOW)
    assert speci is not None and speci.is_speci is True and speci.temp_c == 24


def test_feed_excludes_speci_from_running_max():
    # A SPECI spike (24) must NOT lift the running max above the routine high
    # (22) — the market resolves on the routine cycle. This is the critical
    # guard against buying a still-live bracket's NO.
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: "SPECI LFPB 081145Z 24/11 Q1015",
                     include_speci=False, clock=lambda: NOW)
    assert feed.poll_once() is None
    assert feed.running_max is None
    assert feed.speci_rejected == 1


def test_feed_can_include_speci_when_configured():
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: "SPECI LFPB 081145Z 24/11 Q1015",
                     include_speci=True, clock=lambda: NOW)
    feed.poll_once()
    assert feed.running_max == 24  # opt-in: SPECIs counted


def test_routine_metar_still_sets_running_max_when_excluding_speci():
    raws = iter(["METAR LFPB 081130Z 22/11 Q1015",
                 "SPECI LFPB 081145Z 24/11 Q1015"])  # spike ignored
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: next(raws, None),
                     include_speci=False, clock=lambda: NOW)
    feed.poll_once()  # routine 22 -> folds
    feed.poll_once()  # SPECI 24 -> rejected
    assert feed.running_max == 22


def test_parse_flags_decimal_reading_non_resolution():
    # The 6-minute / non-resolution feed reports tenths; the resolving METAR does
    # not. A decimal temperature must be flagged non-resolution-grade.
    whole = parse_metar("LFPB 081130Z 22/11 Q1015", now=NOW)
    assert whole.resolution_grade is True and whole.temp_c == 22
    dec = parse_metar("LFPB 081130Z 22.4/10.6 Q1015", now=NOW)
    assert dec is not None and dec.resolution_grade is False


def test_feed_rejects_non_resolution_reading():
    # A decimal reading must not move the running max, must not refresh freshness
    # (so we go stale rather than trade on a non-resolution source), and counts.
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: "LFPB 081130Z 22.4/10.6 Q1015",
                     clock=lambda: NOW)
    assert feed.poll_once() is None
    assert feed.running_max is None
    assert feed.non_resolution_rejected == 1
    assert feed.is_stale(stale_min=45, now=NOW) is True  # no resolution data -> stale


def test_hot_window_predictive_after_last_obs():
    from dead_bracket.config import Config
    from dead_bracket.risk import RiskManager
    from conftest import build_worker, StubReporter
    cfg = Config()  # metar_expected_gap_min = 28
    worker, _ex, m = build_worker(config=cfg, risk=RiskManager(cfg), reporter=StubReporter())
    t = datetime(2026, 6, 8, 11, 32, tzinfo=timezone.utc)
    # No obs yet -> hot, poll aggressively.
    assert worker.is_hot(t) is True
    assert worker.metar_interval(t) == cfg.metar_hot_poll_sec
    # Feed the 11:30 report -> the window closes immediately.
    m._fetch_fn = lambda _i: "LFPB 081130Z 22/11 Q1015"
    m._clock = lambda: t
    m.poll_once()
    assert worker.is_hot(t) is False
    assert worker.metar_interval(t) == cfg.metar_poll_sec
    # Still cool within the expected gap...
    assert worker.is_hot(datetime(2026, 6, 8, 11, 57, tzinfo=timezone.utc)) is False
    # ...armed ~2 min before the next half-hourly report (11:30 + 28 min).
    assert worker.is_hot(datetime(2026, 6, 8, 11, 58, 30, tzinfo=timezone.utc)) is True
    # And it stays hot until the next report lands (covers late publishes).
    assert worker.is_hot(datetime(2026, 6, 8, 12, 10, tzinfo=timezone.utc)) is True


def test_hot_window_adapts_to_offset_stations():
    # A :20/:50 station (e.g. EGLC): obs at 11:20 -> hot from 11:48, in time
    # for the 11:50 report. The old :00/:30 slot logic mistimed these.
    from dead_bracket.config import Config
    from dead_bracket.risk import RiskManager
    from conftest import build_worker, StubReporter
    cfg = Config()
    worker, _ex, m = build_worker(config=cfg, risk=RiskManager(cfg), reporter=StubReporter(),
                                  city="london", icao="EGLC", tz="Europe/London")
    t = datetime(2026, 6, 8, 11, 22, tzinfo=timezone.utc)
    m._fetch_fn = lambda _i: "EGLC 081120Z 19/10 Q1014"
    m._clock = lambda: t
    m.poll_once()
    assert worker.is_hot(datetime(2026, 6, 8, 11, 45, tzinfo=timezone.utc)) is False
    assert worker.is_hot(datetime(2026, 6, 8, 11, 48, 30, tzinfo=timezone.utc)) is True


def test_feed_corrupt_log_lines_ignored(tmp_path):
    log = tmp_path / "metar.jsonl"
    log.write_text('not json\n{"missing":"fields"}\n{"obs_time":"2026-06-03T12:00:00+00:00","temp_c":22}\n')
    feed = MetarFeed("LFPB", fetch_fn=lambda _i: None, log_path=str(log), clock=lambda: NOW)
    assert feed.running_max == 22
