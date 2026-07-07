"""HKO station source: parse rhrread, synthesize a METAR, drive a MetarFeed.

WU resolves some markets (Shenzhen) off an HKO station rather than the ICAO's
NOAA METAR. These cover reading HKO's open data directly. No live network: the
session is faked with canned JSON.
"""

from __future__ import annotations

from datetime import datetime, timezone

from dead_bracket.feeds_http import (
    HkoStationFetcher,
    parse_hko_rhrread,
    synth_hko_metar,
)
from dead_bracket.metar import MetarFeed, parse_metar

PAYLOAD = {
    "temperature": {
        "recordTime": "2026-07-03T14:00:00+08:00",  # 06:00Z
        "data": [
            {"place": "King's Park", "value": 31, "unit": "C"},
            {"place": "Lau Fau Shan", "value": 29, "unit": "C"},
            {"place": "Chek Lap Kok", "value": 30, "unit": "C"},
        ],
    }
}
NOW = datetime(2026, 7, 3, 6, 5, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload, *, fail=False):
        self._p = payload
        self.fail = fail
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network down")
        return _Resp(self._p)


# -- pure parser -------------------------------------------------------------
def test_parse_rhrread_picks_the_named_station():
    temp, ts = parse_hko_rhrread(PAYLOAD, "Lau Fau Shan")
    assert temp == 29
    assert ts == datetime.fromisoformat("2026-07-03T14:00:00+08:00")


def test_parse_rhrread_rounds_a_stray_float():
    payload = {"temperature": {"recordTime": "2026-07-03T14:00:00+08:00",
                               "data": [{"place": "Lau Fau Shan", "value": 28.6, "unit": "C"}]}}
    assert parse_hko_rhrread(payload, "Lau Fau Shan")[0] == 29


def test_parse_rhrread_missing_station_or_malformed_is_none():
    assert parse_hko_rhrread(PAYLOAD, "Nowhere") is None
    assert parse_hko_rhrread({}, "Lau Fau Shan") is None
    assert parse_hko_rhrread({"temperature": {"data": []}}, "Lau Fau Shan") is None


# -- synthesized METAR round-trips through the real parser --------------------
def test_synth_metar_parses_back_to_the_reading():
    obs_time = datetime.fromisoformat("2026-07-03T14:00:00+08:00")
    line = synth_hko_metar("ZGSZ", 29, obs_time, "Lau Fau Shan")
    assert "SRC=HKO:Lau Fau Shan" in line  # honest provenance in the logged raw
    obs = parse_metar(line, now=NOW)
    assert obs is not None
    assert obs.icao == "ZGSZ"           # market station -> passes isolation guard
    assert obs.temp_c == 29
    assert obs.resolution_grade is True  # whole-degC, never non-resolution
    assert obs.is_speci is False
    assert obs.obs_time == datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)


def test_synth_metar_handles_subzero():
    obs = parse_metar(synth_hko_metar("ZBAA", -4, NOW, "X"), now=NOW)
    assert obs.temp_c == -4


def test_xx_dewpoint_placeholder_parses_and_real_metars_unaffected():
    # The XX dewpoint slot must not break parsing of the synthesized raw...
    obs = parse_metar("ZGSZ 030600Z 29/XX RMK SRC=HKO:Lau Fau Shan", now=NOW)
    assert obs is not None and obs.temp_c == 29 and obs.resolution_grade
    # ...and a genuine temp/dewpoint group still parses exactly as before.
    obs = parse_metar("LFPB 030600Z 23/10 Q1015", now=NOW)
    assert obs is not None and obs.temp_c == 23


# -- fetcher: caching + fail-soft -------------------------------------------
def test_fetcher_returns_synth_and_caches_within_ttl():
    sess = _Session(PAYLOAD)
    f = HkoStationFetcher(station="Lau Fau Shan", icao="ZGSZ", session=sess,
                          ttl=240.0, clock=lambda: 1000.0)
    raw = f.raw_for("ZGSZ")
    # Dewpoint slot is an explicit XX placeholder (the source has no dewpoint);
    # an earlier mirrored-temp form ("29/29") sent audits chasing a phantom.
    assert raw is not None and "29/XX" in raw and "SRC=HKO:Lau Fau Shan" in raw
    f.raw_for("ZGSZ")           # within ttl (clock frozen) -> no second HTTP call
    assert sess.calls == 1


def test_fetcher_fail_soft_returns_none_so_feed_goes_stale():
    f = HkoStationFetcher(station="Lau Fau Shan", icao="ZGSZ",
                          session=_Session(PAYLOAD, fail=True), clock=lambda: 1000.0)
    assert f.raw_for("ZGSZ") is None


def test_fetcher_missing_station_yields_no_reading():
    f = HkoStationFetcher(station="Nowhere", icao="ZGSZ",
                          session=_Session(PAYLOAD), clock=lambda: 1000.0)
    assert f.raw_for("ZGSZ") is None


# -- end to end: HKO drives a real MetarFeed's running max -------------------
def test_hko_feed_sets_running_max_and_logs(tmp_path):
    log = tmp_path / "metar_hko.jsonl"
    f = HkoStationFetcher(station="Lau Fau Shan", icao="ZGSZ",
                          session=_Session(PAYLOAD), clock=lambda: 1000.0)
    feed = MetarFeed("ZGSZ", fetch_fn=f.raw_for, log_path=str(log), clock=lambda: NOW)
    obs = feed.poll_once()
    assert obs is not None and obs.temp_c == 29
    assert feed.running_max == 29
    assert "SRC=HKO:Lau Fau Shan" in log.read_text()
