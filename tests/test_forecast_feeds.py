"""Feed parsers (against frozen fixture payloads), collector, poller worker."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from dead_bracket.config import CityConfig, Config
from dead_bracket.feeds_http import (
    build_feed_fetcher,
    parse_fmi_wfs,
    parse_jma_amedas,
    parse_knmi_edr,
    parse_mf_obs,
    parse_nea_air_temp,
)
from dead_bracket.forecast_feeds import FeedCollector, FeedPollerWorker

from conftest import StubReporter

UTC = timezone.utc
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def fixture_json(name: str):
    return json.loads(fixture_text(name))


# ---------------------------------------------------------------------------
# Parsers — one per national source, all pure
# ---------------------------------------------------------------------------
def test_parse_fmi_wfs_fixture():
    rows = parse_fmi_wfs(fixture_text("fmi_simple.xml"))
    assert len(rows) >= 3
    dt, temp = rows[0]
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0
    assert isinstance(temp, float)
    assert rows == sorted(rows)  # chronological


def test_parse_fmi_wfs_garbage():
    assert parse_fmi_wfs("<not-xml") == []
    assert parse_fmi_wfs("<a/>") == []


def test_parse_jma_amedas_fixture():
    payload = fixture_json("jma_map.json")
    obs = datetime(2026, 7, 7, 20, 0, tzinfo=UTC)
    rows = parse_jma_amedas(payload, "44166", obs)
    assert rows == [(obs, 21.8)]
    assert parse_jma_amedas(payload, "99999", obs) == []  # unknown station


def test_parse_jma_amedas_rejects_bad_qc():
    obs = datetime(2026, 7, 7, 20, 0, tzinfo=UTC)
    assert parse_jma_amedas({"44166": {"temp": [21.8, 1]}}, "44166", obs) == []
    assert parse_jma_amedas({"44166": {"temp": [None, 0]}}, "44166", obs) == []
    assert parse_jma_amedas({"44166": {}}, "44166", obs) == []


def test_parse_nea_air_temp_fixture():
    payload = fixture_json("nea_v2.json")
    rows = parse_nea_air_temp(payload, "S24")
    assert len(rows) == 1
    dt, temp = rows[0]
    assert dt.utcoffset().total_seconds() == 0  # +08:00 converted to UTC
    assert isinstance(temp, float)
    assert parse_nea_air_temp(payload, "S999") == []
    assert parse_nea_air_temp({}, "S24") == []


def test_parse_mf_obs_fixture_kelvin_to_celsius():
    payload = fixture_json("mf_infrahoraire.json")
    rows = parse_mf_obs(payload, "95088001")
    assert [t for _, t in rows] == [32.4, 32.6, 32.3]  # 305.55K etc -> degC tenths
    assert rows[0][0] == datetime(2026, 7, 7, 14, 6, tzinfo=UTC)
    assert parse_mf_obs(payload, "12345678") == []
    assert parse_mf_obs({"not": "a list"}, "95088001") == []


def test_parse_knmi_edr_fixture():
    payload = fixture_json("knmi_edr.json")
    rows = parse_knmi_edr(payload)
    assert [t for _, t in rows] == [24.1, 24.4, 24.2]
    assert rows[0][0] == datetime(2026, 7, 7, 13, 30, tzinfo=UTC)
    assert parse_knmi_edr({}) == []
    assert parse_knmi_edr({"domain": {}, "ranges": {}}) == []


# ---------------------------------------------------------------------------
# build_feed_fetcher — key gating and composition (no network in tests)
# ---------------------------------------------------------------------------
def cc(src, station="X", **kw):
    return CityConfig(id="c", metar_icao="XXXX", tz="UTC", market_terms=("c",),
                      feed_source=src, feed_station_id=station, **kw)


def test_build_feed_fetcher_key_gating():
    cfg = Config()  # no keys
    assert build_feed_fetcher(cc("meteofrance"), cfg) is None
    assert build_feed_fetcher(cc("knmi"), cfg) is None
    assert build_feed_fetcher(cc(None, station=None), cfg) is None
    assert build_feed_fetcher(cc("nosuch"), cfg) is None
    # Keyless sources compose; keyed ones appear once the key is present.
    assert build_feed_fetcher(cc("fmi"), cfg) is not None
    assert build_feed_fetcher(cc("jma"), cfg) is not None
    assert build_feed_fetcher(cc("nea"), cfg) is not None
    cfg2 = Config(mf_api_key="k", knmi_api_key="k")
    assert build_feed_fetcher(cc("meteofrance"), cfg2) is not None
    assert build_feed_fetcher(cc("knmi"), cfg2) is not None


# ---------------------------------------------------------------------------
# FeedCollector — dedup, latest, restart safety, failure counting
# ---------------------------------------------------------------------------
T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def seq_fetch(batches):
    """A fetch that returns each batch once, then repeats the last."""

    state = {"i": 0}

    def fetch():
        i = min(state["i"], len(batches) - 1)
        state["i"] += 1
        return batches[i]

    return fetch


def test_collector_dedup_and_latest(tmp_path):
    path = str(tmp_path / "feed.jsonl")
    clock = lambda: T0 + timedelta(minutes=10)
    col = FeedCollector(city="paris", src="fmi", station="100968", path=path,
                        fetch=seq_fetch([
                            [(T0, 20.0), (T0 + timedelta(minutes=10), 20.4)],
                            [(T0 + timedelta(minutes=10), 20.4)],  # re-served
                            None,  # outage
                        ]), clock=clock)
    fresh = col.poll_once()
    assert [r.temp_c for r in fresh] == [20.0, 20.4]
    assert col.latest().temp_c == 20.4
    assert col.poll_once() == []          # dedup: nothing new
    assert col.poll_once() == [] and col.failures == 1
    assert col.polls == 3 and col.new_records == 2
    rows = [json.loads(l) for l in open(path)]
    assert len(rows) == 2
    assert rows[0]["latency_sec"] == pytest.approx(600.0)


def test_collector_restart_seeds_from_file(tmp_path):
    path = str(tmp_path / "feed.jsonl")
    col = FeedCollector(city="paris", src="fmi", station="100968", path=path,
                        fetch=seq_fetch([[(T0, 20.0)]]))
    col.poll_once()
    # Restart: a new collector re-serves the same ob -> no duplicate row.
    col2 = FeedCollector(city="paris", src="fmi", station="100968", path=path,
                         fetch=seq_fetch([[(T0, 20.0)]]))
    assert col2.latest() is not None and col2.latest().temp_c == 20.0
    assert col2.poll_once() == []
    assert len(open(path).readlines()) == 1


def test_collector_latest_none_before_first_poll(tmp_path):
    col = FeedCollector(city="paris", src="fmi", station="100968",
                        path=str(tmp_path / "feed.jsonl"),
                        fetch=lambda: None)
    assert col.latest() is None


# ---------------------------------------------------------------------------
# FeedPollerWorker — cadence scheduling + fail-soft guard (no threads)
# ---------------------------------------------------------------------------
def test_poller_tick_cadence(tmp_path):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return []

    col = FeedCollector(city="paris", src="fmi", station="100968",
                        path=str(tmp_path / "feed.jsonl"), fetch=fetch)
    rep = StubReporter()
    w = FeedPollerWorker(city="paris", collector=col, reporter=rep, poll_sec=60.0)
    w.tick(now=0.0)
    w.tick(now=30.0)   # not due
    w.tick(now=61.0)   # due
    assert calls["n"] == 2
    assert rep.console == []  # never stdout


def test_poller_guard_swallows_exceptions(tmp_path):
    def boom():
        raise RuntimeError("kaput")

    col = FeedCollector(city="paris", src="fmi", station="100968",
                        path=str(tmp_path / "feed.jsonl"), fetch=boom)
    rep = StubReporter()
    w = FeedPollerWorker(city="paris", collector=col, reporter=rep, poll_sec=60.0)
    w.tick(now=0.0)  # must not raise
    assert rep.console == []


def test_collector_seed_tolerates_corrupt_rows(tmp_path):
    path = tmp_path / "feed.jsonl"
    path.write_text(
        '{"obs_time": "2026-07-07T12:00:00+00:00", "temp_c": 20.0, "ts_recv": "garbage"}\n'
        '{"temp_c": 21.0, "ts_recv": "2026-07-07T12:01:00+00:00"}\n'
        '{"obs_time": "2026-07-07T12:10:00+00:00"}\n'
        'not json at all\n'
        '{"obs_time": "2026-07-07T12:20:00+00:00", "temp_c": 20.4, '
        '"ts_recv": "2026-07-07T12:21:00+00:00"}\n')
    col = FeedCollector(city="paris", src="fmi", station="100968", path=str(path),
                        fetch=lambda: [])
    # Only the fully-valid row seeds latest; the corrupt ones are skipped.
    assert col.latest() is not None and col.latest().temp_c == 20.4
    # But all parseable obs_times still count as seen (no duplicate rows).
    dup = (datetime(2026, 7, 7, 12, 0, tzinfo=UTC), 20.0)
    col._fetch = lambda: [dup]  # type: ignore[attr-defined]
    assert col.poll_once() == []


def test_collector_older_reading_does_not_regress_latest(tmp_path):
    col = FeedCollector(city="paris", src="fmi", station="100968",
                        path=str(tmp_path / "feed.jsonl"),
                        fetch=seq_fetch([
                            [(T0 + timedelta(minutes=10), 21.0)],
                            [(T0, 20.0)],  # late-arriving OLDER ob
                        ]))
    col.poll_once()
    col.poll_once()
    assert col.latest().temp_c == 21.0  # freshest by obs_time, not arrival


def test_collector_seen_set_is_bounded(tmp_path):
    big = [(T0 + timedelta(seconds=i), 20.0) for i in range(20001)]
    col = FeedCollector(city="paris", src="fmi", station="100968",
                        path=str(tmp_path / "feed.jsonl"),
                        fetch=seq_fetch([big]))
    col.poll_once()
    assert len(col._seen) <= 5000  # trimmed to the most recent obs times


def test_poller_thread_lifecycle(tmp_path):
    col = FeedCollector(city="paris", src="fmi", station="100968",
                        path=str(tmp_path / "feed.jsonl"), fetch=lambda: [])
    rep = StubReporter()
    w = FeedPollerWorker(city="paris", collector=col, reporter=rep, poll_sec=0.01)
    w.start()
    w.start()  # idempotent while alive
    import time as _t
    deadline = _t.monotonic() + 2.0
    while col.polls == 0 and _t.monotonic() < deadline:
        _t.sleep(0.01)
    w.stop()
    assert col.polls >= 1
    assert rep.console == []
