"""Resolver cross-check: our source vs the table the market settles on.

The regression suite for the 2026-07-19 Seoul wipe. Every test here answers
"would this have stopped the bot from buying?" — the guard's whole job is to
fail closed, so the negative cases (unverified, stale, sticky) matter as much
as the happy path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dead_bracket.resolver_guard import (
    DIVERGENT,
    OK,
    UNVERIFIED,
    ResolverBlockStore,
    ResolverCrossCheck,
    ResolverPollerWorker,
    ResolverRowCollector,
    seed_records_from_metar_log,
)

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def guard(**kw) -> ResolverCrossCheck:
    kw.setdefault("clock", lambda: NOW)
    return ResolverCrossCheck("seoul", **kw)


def half_hourly(start: datetime, temps, step_min: int = 30):
    """(obs_time, temp) pairs on a :00/:30 grid."""

    return [(start + timedelta(minutes=step_min * i), t) for i, t in enumerate(temps)]


def agreeing_history(n: int = 48, temp: int = 20, end: datetime = NOW):
    """A full window of identical, agreeing observations on both sides."""

    start = end - timedelta(minutes=30 * n)
    ours = half_hourly(start, [temp] * n)
    theirs = [(int(t.timestamp()), v) for t, v in ours]
    return ours, theirs


def seeded(**kw):
    g = guard(**kw)
    ours, theirs = agreeing_history()
    g.seed_ours(ours)
    g.note_theirs(theirs, station="Seoul")
    return g


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_full_agreement_over_the_window_allows_trading():
    v = seeded().evaluate(NOW)
    assert v.state == OK and v.ok
    assert v.pairs == 48
    assert v.span_hours == pytest.approx(23.5)
    assert v.station == "Seoul"


def test_station_name_from_the_resolver_is_reported():
    # Shenzhen's table answers "Lau Fau Shan", not the ZGSZ airport — the
    # resolver naming its own instrument. Surfacing it keeps station identity
    # verifiable at runtime instead of trusted from config.
    g = seeded()
    g.note_theirs([], station="Lau Fau Shan")
    assert g.evaluate(NOW).station == "Lau Fau Shan"


# ---------------------------------------------------------------------------
# Divergence — the failure that actually cost money
# ---------------------------------------------------------------------------
def test_seoul_2026_07_19_false_dead_is_caught():
    """The real numbers: RKSI METAR printed 27C at 02:00Z; WU's row for that
    same instant read 26C (79F = 26.1C). The 26C bracket was NOT dead, and the
    bot spent the wallet on its NO. One mismatched pair must block the city."""

    g = guard()
    ours, theirs = agreeing_history()
    spike = datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc)
    ours = [(t, 27 if t == spike else v) for t, v in ours]
    theirs = [(e, 26 if e == int(spike.timestamp()) else v) for e, v in theirs]
    g.seed_ours(ours)
    g.note_theirs(theirs)

    v = g.evaluate(NOW)
    assert v.state == DIVERGENT and not v.ok
    assert v.mismatches[0].ours == 27 and v.mismatches[0].theirs == 26
    assert "27C" in v.reason and "26C" in v.reason


def test_divergence_is_sticky_even_after_the_data_agrees_again():
    # A resolver revision that "fixes" the row must not silently re-enable
    # trading: the two sources have been shown to be different instruments.
    g = guard()
    ours, theirs = agreeing_history()
    bad = ours[10][0]
    g.seed_ours([(t, 27 if t == bad else v) for t, v in ours])
    g.note_theirs(theirs)
    assert g.evaluate(NOW).state == DIVERGENT

    g.note_theirs([(int(bad.timestamp()), 27)])  # now agrees
    assert g.evaluate(NOW).state == DIVERGENT
    assert g.clear() is True
    assert g.evaluate(NOW).state == OK


def test_running_max_divergence_blocks_even_when_every_pair_matches():
    # A SPECI spike our source counts but the resolver's table never records:
    # no paired observation disagrees, yet our running max is a degree high —
    # exactly enough to mark a live bracket dead.
    g = guard(tz=ZoneInfo("Asia/Seoul"))
    ours, theirs = agreeing_history()
    speci = datetime(2026, 7, 19, 2, 17, tzinfo=timezone.utc)  # off-grid
    g.seed_ours(ours + [(speci, 27)])
    g.note_theirs(theirs)

    v = g.evaluate(NOW)
    assert v.state == DIVERGENT
    assert v.ours_max == 27 and v.theirs_max == 20
    assert "running max" in v.reason


def test_tolerance_allows_configured_slack():
    g = guard(tolerance_c=1)
    ours, theirs = agreeing_history()
    bad = ours[5][0]
    g.seed_ours([(t, 21 if t == bad else v) for t, v in ours])
    g.note_theirs(theirs)
    assert g.evaluate(NOW).state == OK  # 1C inside tolerance


# ---------------------------------------------------------------------------
# Fail closed: everything that is not proven agreement
# ---------------------------------------------------------------------------
def test_no_resolver_data_means_no_trading():
    g = guard()
    g.seed_ours(agreeing_history()[0])
    v = g.evaluate(NOW)
    assert v.state == UNVERIFIED and not v.ok


def test_too_few_matched_obs_means_no_trading():
    g = guard()
    ours, theirs = agreeing_history(n=8)
    g.seed_ours(ours)
    g.note_theirs(theirs)
    v = g.evaluate(NOW)
    assert v.state == UNVERIFIED
    assert "8 matched obs" in v.reason


def test_history_that_does_not_reach_back_far_enough_means_no_trading():
    # 40 obs at 5-minute spacing clears min_pairs but only covers the last ~3h.
    g = guard(min_pairs=20)
    ours = half_hourly(NOW - timedelta(hours=3, minutes=20), [20] * 40, step_min=5)
    g.seed_ours(ours)
    g.note_theirs([(int(t.timestamp()), v) for t, v in ours])
    v = g.evaluate(NOW)
    assert v.state == UNVERIFIED and "reaches back" in v.reason


def test_publish_lag_alone_does_not_block_a_fully_covered_city():
    """REGRESSION: the coverage rule used to demand a 23h SPAN inside a 24h
    window. The resolver publishes 26-96 min late, so the achievable span is
    only ``window - lag`` — 23h was unreachable and every city flapped on how
    fresh the resolver happened to be. Reach-back must be lag-independent."""

    g = guard()
    # A full day of history whose newest ob is 1.6h old: the worst lag measured.
    ours, theirs = agreeing_history(end=NOW - timedelta(hours=1, minutes=36))
    g.seed_ours(ours)
    g.note_theirs(theirs)
    v = g.evaluate(NOW)
    assert v.state == OK, v.reason
    assert v.span_hours < 23.0  # the span rule would have rejected this


def test_a_stale_near_end_still_blocks():
    # Reach-back alone is not enough: if nothing recent pairs, the newest
    # observation we would actually trade on is unverified.
    g = guard(max_lag_hours=3.0)
    ours, theirs = agreeing_history(end=NOW - timedelta(hours=5))
    g.seed_ours(ours)
    g.note_theirs(theirs)
    v = g.evaluate(NOW)
    assert v.state == UNVERIFIED and "stale" in v.reason


def test_hole_in_the_middle_means_no_trading():
    g = guard()
    ours, theirs = agreeing_history()
    keep = lambda t: not (NOW - timedelta(hours=14) < t < NOW - timedelta(hours=6))
    g.seed_ours([(t, v) for t, v in ours if keep(t)])
    g.note_theirs(theirs)
    v = g.evaluate(NOW)
    assert v.state == UNVERIFIED and "hole" in v.reason


def test_stale_resolver_rows_mean_no_trading():
    # A dead API key or stopped poller must not read as agreement: yesterday's
    # match proves nothing about the observation we would trade on now.
    end = NOW - timedelta(hours=6)
    g = guard()
    ours, theirs = agreeing_history(end=end)
    g.seed_ours(ours)
    g.note_theirs(theirs)
    v = g.evaluate(NOW)
    assert v.state == UNVERIFIED and "stale" in v.reason


def test_city_without_a_resolver_table_never_verifies():
    # Istanbul: LTFM serves no rows, so it can never be cross-checked.
    g = guard(configured=False)
    ours, theirs = agreeing_history()
    g.seed_ours(ours)
    g.note_theirs(theirs)
    v = g.evaluate(NOW)
    assert v.state == UNVERIFIED and "no resolver source" in v.reason


def test_mismatch_is_reported_even_when_coverage_is_thin():
    # Proven divergence beats absence of evidence: it must not hide behind an
    # "unverified" verdict that would clear itself once data accumulates.
    g = guard()
    ours, theirs = agreeing_history(n=4)
    g.seed_ours([(ours[0][0], 27)] + ours[1:])
    g.note_theirs(theirs)
    assert g.evaluate(NOW).state == DIVERGENT


def test_adopt_block_restores_stickiness_after_restart():
    g = seeded()
    assert g.evaluate(NOW).state == OK
    g.adopt_block("resolver divergence from a previous run")
    v = g.evaluate(NOW)
    assert v.state == DIVERGENT and "previous run" in v.reason


# ---------------------------------------------------------------------------
# Block store (the manual-resolution record)
# ---------------------------------------------------------------------------
def test_block_store_persists_and_clears(tmp_path):
    path = tmp_path / "_global" / "resolver_blocks.json"
    store = ResolverBlockStore(str(path))
    assert store.block("seoul", reason="resolver divergence", detail="27 vs 26") is True
    assert store.block("seoul", reason="again", detail="x") is False  # idempotent

    reloaded = ResolverBlockStore(str(path))
    assert reloaded.blocked("seoul")["detail"] == "27 vs 26"
    assert reloaded.blocked("paris") is None
    assert reloaded.clear("seoul") is True
    assert ResolverBlockStore(str(path)).blocked("seoul") is None
    assert reloaded.clear("seoul") is False


def test_block_store_tolerates_a_corrupt_file(tmp_path):
    path = tmp_path / "blocks.json"
    path.write_text("{not json", encoding="utf-8")
    assert ResolverBlockStore(str(path)).all_blocks() == {}


# ---------------------------------------------------------------------------
# Row collector
# ---------------------------------------------------------------------------
def payload(rows, station="Seoul"):
    return {"observations": [
        {"valid_time_gmt": e, "temp": t, "obs_name": station} for e, t in rows]}


def test_collector_appends_new_rows_and_feeds_the_guard(tmp_path):
    g = guard()
    rows = [(int((NOW - timedelta(hours=i)).timestamp()), 20 + i) for i in range(3)]
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return payload(rows)

    path = tmp_path / "wu_rows.jsonl"
    c = ResolverRowCollector(city="seoul", guard=g, fetch=fetch, path=str(path),
                             clock=lambda: NOW)
    assert len(c.poll_once()) == 3
    assert c.poll_once() == []  # unchanged rows are not re-appended
    assert c.polls == 2 and c.new_rows == 3
    assert calls[0] == ("20260718", "20260719")  # yesterday..today
    assert g.counts[1] == 3

    written = [json.loads(l) for l in path.read_text().splitlines()]
    assert {r["temp"] for r in written} == {20, 21, 22}
    assert written[0]["obs_name"] == "Seoul"


def test_collector_records_a_revised_row(tmp_path):
    g = guard()
    epoch = int(NOW.timestamp())
    state = {"temp": 20}
    c = ResolverRowCollector(
        city="seoul", guard=g,
        fetch=lambda s, e: payload([(epoch, state["temp"])]),
        path=str(tmp_path / "wu.jsonl"), clock=lambda: NOW)
    c.poll_once()
    state["temp"] = 21  # the resolver revised its own row
    assert len(c.poll_once()) == 1


def test_collector_survives_bad_payloads(tmp_path):
    g = guard()
    bad = [None, {}, {"observations": "nope"}, {"observations": [{"temp": None}]}]
    c = ResolverRowCollector(city="seoul", guard=g, fetch=lambda s, e: bad.pop(0),
                             path=str(tmp_path / "wu.jsonl"), clock=lambda: NOW)
    for _ in range(4):
        assert c.poll_once() == []
    assert c.failures == 3  # the 4th parsed but had no usable temperature


def test_collector_reseeds_from_disk_on_restart(tmp_path):
    path = tmp_path / "wu.jsonl"
    rows = [(int(NOW.timestamp()), 25)]
    ResolverRowCollector(city="seoul", guard=guard(), fetch=lambda s, e: payload(rows),
                         path=str(path), clock=lambda: NOW).poll_once()

    g2 = guard()
    ResolverRowCollector(city="seoul", guard=g2, fetch=lambda s, e: None,
                         path=str(path), clock=lambda: NOW)
    assert g2.counts[1] == 1  # history survived the restart


def test_poller_worker_guards_a_throwing_collector():
    events = []

    class Rep:
        def event(self, city, name, **f):
            events.append(name)

    class Boom:
        polls = failures = new_rows = 0

        def poll_once(self):
            raise RuntimeError("network down")

    w = ResolverPollerWorker(city="seoul", collector=Boom(), reporter=Rep(),
                             poll_sec=1.0, monotonic=lambda: 0.0)
    w.tick()  # must not raise
    assert "resolver_poll_error" in events and "resolver_health" in events


# ---------------------------------------------------------------------------
# Seeding our side from the METAR log
# ---------------------------------------------------------------------------
def test_seed_from_metar_log_respects_window_and_speci(tmp_path):
    path = tmp_path / "metar.jsonl"
    recs = [
        {"obs_time": (NOW - timedelta(hours=1)).isoformat(), "temp_c": 21,
         "raw": "METAR RKSI ..."},
        {"obs_time": (NOW - timedelta(hours=200)).isoformat(), "temp_c": 5,
         "raw": "METAR RKSI ..."},  # outside the window
        {"obs_time": (NOW - timedelta(hours=2)).isoformat(), "temp_c": 27,
         "raw": "SPECI RKSI ..."},
        {"garbage": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")

    kept = list(seed_records_from_metar_log(str(path), hours=48, now=NOW))
    assert [t for _, t in kept] == [21, 27]
    # A routine-only city never let the SPECI into its running max, so it must
    # not enter the comparison either.
    routine = list(seed_records_from_metar_log(str(path), hours=48, now=NOW,
                                               include_speci=False))
    assert [t for _, t in routine] == [21]


def test_seed_from_missing_log_is_empty():
    assert list(seed_records_from_metar_log("/nonexistent/metar.jsonl")) == []


# ---------------------------------------------------------------------------
# Strict mode: the resolver's own table must reach the running max
# ---------------------------------------------------------------------------
def test_confirms_max_waits_for_the_resolver_to_catch_up():
    # The residual window the 24h check cannot cover: our print says 27 but the
    # resolver has not published anything above 26 yet, so "dead below 27" is
    # still our claim alone.
    g = seeded()
    assert g.confirms_max(20) is True   # resolver already reached 20 today
    assert g.confirms_max(27) is False  # nothing that high in its table
    assert g.confirms_max(None) is True  # no running max -> nothing to confirm


def test_confirms_max_is_false_when_the_resolver_has_no_rows_today():
    g = guard(tz=ZoneInfo("Asia/Seoul"))
    g.note_theirs([(int((NOW - timedelta(days=3)).timestamp()), 30)])
    assert g.confirms_max(25) is False


# ---------------------------------------------------------------------------
# Self-sourced cities (seoul/shenzhen read the resolver's table directly)
# ---------------------------------------------------------------------------
def test_self_sourced_city_is_ok_without_any_pairing():
    # The feed labels a closed row at slot+50min while the row is keyed at the
    # slot start, so the two sides NEVER pair. Comparing them would leave the
    # city permanently unverified and silently untradeable; there is also
    # nothing to compare, since both sides are the same data.
    g = guard(self_sourced=True)
    _ours, theirs = agreeing_history()
    g.note_theirs(theirs, station="Seoul")
    g.seed_ours([])  # nothing pairs, deliberately
    v = g.evaluate(NOW)
    assert v.state == OK and v.ok
    assert "nothing to cross-check" in v.reason
    assert v.pairs == 48


def test_self_sourced_city_still_fails_closed_on_a_dead_table():
    _ours, theirs = agreeing_history()

    empty = guard(self_sourced=True)
    assert empty.evaluate(NOW).state == UNVERIFIED

    stale = guard(self_sourced=True)
    stale.note_theirs(agreeing_history(end=NOW - timedelta(hours=6))[1])
    assert "stale" in stale.evaluate(NOW).reason

    thin = guard(self_sourced=True)
    thin.note_theirs(agreeing_history(n=6)[1])
    assert "need" in thin.evaluate(NOW).reason

    holed = guard(self_sourced=True)
    holed.note_theirs([(e, v) for e, v in theirs
                       if not (NOW.timestamp() - 14*3600 < e < NOW.timestamp() - 6*3600)])
    assert "hole" in holed.evaluate(NOW).reason


def test_self_sourced_city_still_honours_an_adopted_block():
    g = guard(self_sourced=True)
    g.note_theirs(agreeing_history()[1])
    g.adopt_block("divergence recorded before the source was switched")
    assert g.evaluate(NOW).state == DIVERGENT
