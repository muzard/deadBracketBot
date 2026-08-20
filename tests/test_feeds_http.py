"""Token resolver & event-discovery picker (pure, no network) — spec §6.

These cover the logic behind the CLOB token resolution and the daily-event
selection that previously caused 404s when a stale event was kept.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from dead_bracket.feeds_http import (
    SharedMetarFetcher,
    bracket_is_upper_open,
    choose_event,
    latest_by_icao,
    parse_bracket_temp,
    resolve_no_token,
    sorted_levels,
)

NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def market(*, title="24°C", outcomes=("Yes", "No"), tokens=("tok-yes", "tok-no"),
           order_book=True):
    return {
        "groupItemTitle": title,
        "question": f"Will the highest temperature be {title} on June 8?",
        "outcomes": json.dumps(list(outcomes)),
        "clobTokenIds": json.dumps(list(tokens)),
        "enableOrderBook": order_book,
    }


# -- token resolver --------------------------------------------------------
def test_resolve_no_token_picks_no_outcome():
    assert resolve_no_token(market(tokens=("yId", "nId"))) == "nId"


def test_resolve_no_token_handles_native_lists():
    m = {"outcomes": ["Yes", "No"], "clobTokenIds": ["a", "b"], "enableOrderBook": True}
    assert resolve_no_token(m) == "b"


def test_resolve_no_token_none_when_order_book_disabled():
    assert resolve_no_token(market(order_book=False)) is None


def test_resolve_no_token_none_on_mismatched_arrays():
    m = {"outcomes": json.dumps(["Yes", "No"]), "clobTokenIds": json.dumps(["only-one"]),
         "enableOrderBook": True}
    assert resolve_no_token(m) is None


def test_resolve_no_token_none_when_no_outcome_absent():
    assert resolve_no_token(market(outcomes=("Yes", "Maybe"))) is None


# -- bracket temperature ---------------------------------------------------
def test_parse_bracket_temp_variants():
    assert parse_bracket_temp(market(title="24°C")) == 24
    assert parse_bracket_temp(market(title="18°C or below")) == 18
    assert parse_bracket_temp(market(title="28°C or higher")) == 28
    assert parse_bracket_temp({"groupItemTitle": "no number here"}) is None


def test_bracket_is_upper_open_matches_only_open_ended_top():
    assert bracket_is_upper_open(market(title="28°C or higher")) is True
    assert bracket_is_upper_open(market(title="28°C or above")) is True
    assert bracket_is_upper_open(market(title="28°C or more")) is True
    assert bracket_is_upper_open(market(title="30°C+")) is True
    # Exact and bottom-open brackets must NOT match (they die correctly).
    assert bracket_is_upper_open(market(title="24°C")) is False
    assert bracket_is_upper_open(market(title="18°C or below")) is False


# -- level sorting ---------------------------------------------------------
def test_sorted_levels_orders_and_skips_bad():
    asks = [{"price": "0.97", "size": "10"}, {"price": "0.95", "size": "5"}, {"bad": 1}]
    assert sorted_levels(asks, reverse=False) == ((0.95, 5.0), (0.97, 10.0))
    bids = [{"price": "0.40", "size": "3"}, {"price": "0.60", "size": "7"}]
    assert sorted_levels(bids, reverse=True) == ((0.60, 7.0), (0.40, 3.0))


# -- event discovery picker ------------------------------------------------
def _ev(slug, *, closed=False, end="2026-06-08T22:00:00Z", title=None):
    return {"slug": slug, "title": title or slug, "closed": closed, "endDate": end}


def test_choose_event_matches_city_term():
    events = [
        _ev("highest-temperature-in-london-on-june-8-2026"),
        _ev("highest-temperature-in-paris-on-june-8-2026"),
    ]
    ev = choose_event(events, ("paris",), now=NOW)
    assert ev["slug"] == "highest-temperature-in-paris-on-june-8-2026"


def test_choose_event_skips_closed():
    events = [
        _ev("highest-temperature-in-paris-on-june-7-2026", closed=True, end="2026-06-07T22:00:00Z"),
        _ev("highest-temperature-in-paris-on-june-8-2026"),
    ]
    assert choose_event(events, ("paris",), now=NOW)["slug"].endswith("june-8-2026")


def test_choose_event_prefers_soonest_open_day():
    events = [
        _ev("highest-temperature-in-paris-on-june-9-2026", end="2026-06-09T22:00:00Z"),
        _ev("highest-temperature-in-paris-on-june-8-2026", end="2026-06-08T22:00:00Z"),
    ]
    # Today's (sooner future end) event is chosen over tomorrow's.
    assert choose_event(events, ("paris",), now=NOW)["slug"].endswith("june-8-2026")


def test_choose_event_none_when_no_match():
    assert choose_event([_ev("highest-temperature-in-berlin-on-june-8-2026")],
                        ("paris",), now=NOW) is None


# -- HIGHEST-only: a lowest-temperature market must never be selected ---------
def test_choose_event_never_selects_lowest_market():
    from datetime import date
    events = [
        # The lowest event resolves sooner (overnight low) — the old sort
        # preferred it. It must now be excluded outright.
        _ev("lowest-temperature-in-paris-on-june-10-2026", end="2026-06-10T10:00:00Z"),
        _ev("highest-temperature-in-paris-on-june-10-2026", end="2026-06-10T22:00:00Z"),
    ]
    now = datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
    ev = choose_event(events, ("paris",), now=now, require_date=date(2026, 6, 10))
    assert ev["slug"].startswith("highest-")


def test_choose_event_idles_when_only_lowest_exists():
    from datetime import date
    events = [_ev("lowest-temperature-in-paris-on-june-10-2026", end="2026-06-10T10:00:00Z")]
    now = datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
    assert choose_event(events, ("paris",), now=now, require_date=date(2026, 6, 10)) is None


def test_is_highest_event_guard():
    from dead_bracket.feeds_http import is_highest_event
    assert is_highest_event("highest-temperature-in-paris-on-june-10-2026") is True
    assert is_highest_event("lowest-temperature-in-paris-on-june-10-2026") is False
    assert is_highest_event("Highest temperature in Paris on June 10?") is True
    assert is_highest_event("daily-temperature-paris") is False  # no 'highest' -> reject
    assert is_highest_event("highest-vs-lowest-temperature-paris") is False  # forbidden term


# -- the today-vs-tomorrow bug: require_date must pin today's event -----------
def test_choose_event_requires_todays_date():
    from datetime import date
    events = [
        _ev("highest-temperature-in-paris-on-june-8-2026", end="2026-06-08T22:00:00Z"),
        _ev("highest-temperature-in-paris-on-june-9-2026", end="2026-06-09T22:00:00Z"),
    ]
    ev = choose_event(events, ("paris",), now=NOW, require_date=date(2026, 6, 8))
    assert ev["slug"].endswith("june-8-2026")


def test_choose_event_never_falls_back_to_other_day():
    # Only tomorrow's event exists; with require_date=today we must idle (None),
    # never trade tomorrow's forecast market against today's METAR.
    from datetime import date
    events = [_ev("highest-temperature-in-paris-on-june-9-2026", end="2026-06-09T22:00:00Z")]
    assert choose_event(events, ("paris",), now=NOW, require_date=date(2026, 6, 8)) is None


# -- resolution-source cross-check (ICAO + Weather Underground) ---------------
def _src_ev(slug, *, icao=None, wu=True, **kw):
    """A today-dated highest event whose description optionally names the station
    + Weather Underground (the resolution-source text we cross-check)."""

    desc_bits = ["Resolves to the highest temperature"]
    if icao:
        desc_bits.append(f"at station {icao}")
    if wu:
        desc_bits.append("as reported by Weather Underground (wunderground.com)")
    ev = _ev(slug, **kw)
    ev["description"] = " ".join(desc_bits)
    ev["markets"] = []
    return ev


def test_source_confirmed_requires_icao_and_wunderground():
    from dead_bracket.feeds_http import source_confirmed
    ev = _src_ev("highest-temperature-in-london-on-june-8-2026", icao="EGLC", wu=True)
    assert source_confirmed(ev, "EGLC") is True
    # Missing the station code -> not confirmed.
    assert source_confirmed(_src_ev("x", icao=None, wu=True), "EGLC") is False
    # Missing the Weather Underground source -> not confirmed.
    assert source_confirmed(_src_ev("x", icao="EGLC", wu=False), "EGLC") is False


def test_wunderground_exempt_icaos_still_require_station():
    from dead_bracket.feeds_http import source_confirmed
    # UUWW (Moscow) and LTFM (Istanbul) waive ONLY the "wunderground" string:
    # an event that names the station but no WU source is confirmed.
    assert source_confirmed(_src_ev("x", icao="UUWW", wu=False), "UUWW") is True
    assert source_confirmed(_src_ev("x", icao="LTFM", wu=False), "LTFM") is True
    # ...but the station ICAO is never waived: no station text -> not confirmed,
    # even for a WU-exempt ICAO.
    bare = _src_ev("x", icao=None, wu=False)
    assert source_confirmed(bare, "UUWW") is False
    assert source_confirmed(bare, "LTFM") is False
    # Unset icao still skips entirely (keeps the pure picker usable).
    assert source_confirmed(bare, None) is True


def test_choose_event_filters_on_unconfirmed_source():
    from datetime import date
    good = _src_ev("highest-temperature-in-london-on-june-8-2026", icao="EGLC", wu=True)
    bad = _src_ev("highest-temperature-in-paris-on-june-8-2026", icao=None, wu=False)
    req = date(2026, 6, 8)
    # London confirms its source -> chosen; Paris (no source text) -> idled.
    assert choose_event([good], ("london",), now=NOW, require_date=req,
                        icao="EGLC")["slug"].endswith("london-on-june-8-2026")
    assert choose_event([bad], ("paris",), now=NOW, require_date=req, icao="LFPB") is None
    # WU-exempt ICAO trades on station+slug/date alone (no Weather Underground text)...
    moscow = _src_ev("highest-temperature-in-moscow-on-june-8-2026", icao="UUWW", wu=False)
    assert choose_event([moscow], ("moscow",), now=NOW, require_date=req,
                        icao="UUWW") is not None
    # ...but even a WU-exempt ICAO is idled when its event never names the station.
    assert choose_event([bad], ("paris",), now=NOW, require_date=req, icao="UUWW") is None


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Returns a single today-dated event and an empty book; counts calls."""

    def __init__(self, slug):
        self.slug = slug
        self.event_calls = 0

    def get(self, url, params=None, timeout=None):
        if "events" in url:
            self.event_calls += 1
            # First page has the event; pagination stops (len < 100).
            if (params or {}).get("offset", 0) == 0:
                return _FakeResp([{"slug": self.slug, "title": self.slug, "closed": False,
                                   "endDate": "2026-06-08T22:00:00Z",
                                   "markets": []}])
            return _FakeResp([])
        return _FakeResp({"bids": [], "asks": []})


def test_on_event_fires_once_and_no_rediscovery_spam():
    from datetime import date, datetime, timezone
    from zoneinfo import ZoneInfo
    from dead_bracket.feeds_http import GammaMarketProvider
    slug = "highest-temperature-in-paris-on-june-8-2026"
    sess = _FakeSession(slug)
    announced = []
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    p = GammaMarketProvider(market_terms=("paris",), session=sess, tz=ZoneInfo("Europe/Paris"),
                            on_event=announced.append)
    p.discover(now=now)
    # Repeated polls on the same day must not re-discover or re-announce.
    assert p._needs_rediscover(now) is False
    p.discover(now=now)
    assert announced == [slug]  # announced exactly once despite two discovers


class _TwoDaySession:
    """Returns two consecutive days' events (both source-confirmed) + empty books.

    Lets a test advance ``now`` across a city's *local* midnight and assert the
    provider rolls to the new day's market on its own timezone boundary.
    """

    def __init__(self, city, icao):
        self.city = city
        self.icao = icao
        self.event_calls = 0

    def _ev(self, day_slug):
        return {
            "slug": f"highest-temperature-in-{self.city}-on-{day_slug}",
            "title": f"Highest temperature in {self.city} on {day_slug}",
            "closed": False,
            "endDate": "2026-06-30T22:00:00Z",
            "description": f"Highest temperature at {self.icao} per Weather "
                           f"Underground (wunderground.com).",
            "markets": [],
        }

    def get(self, url, params=None, timeout=None):
        if "events" in url:
            self.event_calls += 1
            if (params or {}).get("offset", 0) == 0:
                return _FakeResp([self._ev("june-8-2026"), self._ev("june-9-2026")])
            return _FakeResp([])
        return _FakeResp({"bids": [], "asks": []})


def test_next_day_discovery_rolls_at_local_midnight():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from dead_bracket.feeds_http import GammaMarketProvider
    # Helsinki is UTC+3 in summer: local midnight June 9 == 21:00Z June 8.
    sess = _TwoDaySession("helsinki", "EFHK")
    p = GammaMarketProvider(market_terms=("helsinki",), metar_icao="EFHK",
                            session=sess, tz=ZoneInfo("Europe/Helsinki"))
    before = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)   # local June 8
    ev = p.discover(now=before)
    assert ev["slug"].endswith("june-8-2026")
    assert p._needs_rediscover(before) is False  # same local day, no churn
    # Cross Helsinki's local midnight (21:30Z -> local June 9): must roll forward.
    after = datetime(2026, 6, 8, 21, 30, tzinfo=timezone.utc)
    assert p._needs_rediscover(after) is True
    ev2 = p.discover(now=after)
    assert ev2["slug"].endswith("june-9-2026")


def test_day_rollover_drops_stale_event_when_new_market_absent():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from dead_bracket.feeds_http import GammaMarketProvider

    class _OnlyDay8(_TwoDaySession):
        def get(self, url, params=None, timeout=None):
            if "events" in url:
                self.event_calls += 1
                if (params or {}).get("offset", 0) == 0:
                    return _FakeResp([self._ev("june-8-2026")])  # day 9 not open yet
                return _FakeResp([])
            return _FakeResp({"bids": [], "asks": []})

    sess = _OnlyDay8("helsinki", "EFHK")
    p = GammaMarketProvider(market_terms=("helsinki",), metar_icao="EFHK",
                            session=sess, tz=ZoneInfo("Europe/Helsinki"))
    p.discover(now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc))
    assert p._event is not None
    # Day rolled (local June 9) but tomorrow's market isn't open -> idle cleanly.
    after = datetime(2026, 6, 8, 21, 30, tzinfo=timezone.utc)
    assert p.discover(now=after) is None
    assert p._event is None              # stale June-8 event dropped, not served
    assert p._needs_rediscover(after) is True  # keeps retrying for today's market


def test_discovery_idles_when_source_not_confirmed():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from dead_bracket.feeds_http import GammaMarketProvider

    class _NoSource(_TwoDaySession):
        def _ev(self, day_slug):
            ev = super()._ev(day_slug)
            ev["description"] = "Highest temperature (no source named)."
            return ev

    sess = _NoSource("london", "EGLC")
    p = GammaMarketProvider(market_terms=("london",), metar_icao="EGLC",
                            session=sess, tz=ZoneInfo("Europe/London"))
    # Non-exempt station with no WU/ICAO text in the description -> never traded.
    assert p.discover(now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)) is None


def test_snapshot_flags_top_bracket_upper_open_even_without_qualifier():
    """Structural safeguard: the highest bracket of a HIGHEST event is always the
    open-ended 'or higher' one, so snapshot() must flag it upper_open even when
    the title carries no qualifier — otherwise an over-the-top METAR reading would
    mark it dead (the Milan 2026-06-24 false-dead)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from dead_bracket.feeds_http import GammaMarketProvider

    def _mkt(title, no_tok):
        return {
            "groupItemTitle": title,
            "question": f"Will the highest temperature be {title}?",
            "outcomes": json.dumps(["Yes", "No"]),
            "clobTokenIds": json.dumps([f"yes-{no_tok}", no_tok]),
            "enableOrderBook": True,
        }

    class _BracketSession:
        def get(self, url, params=None, timeout=None):
            if "events" in url:
                if (params or {}).get("offset", 0) == 0:
                    return _FakeResp([{
                        "slug": "highest-temperature-in-london-on-june-8-2026",
                        "title": "Highest temperature in London on June 8 2026",
                        "closed": False,
                        "endDate": "2026-06-30T22:00:00Z",
                        "description": "Highest temperature at EGLC per Weather "
                                       "Underground (wunderground.com).",
                        # Top bracket (35) deliberately carries NO 'or higher' text.
                        "markets": [_mkt("33°C", "no-33"), _mkt("34°C", "no-34"),
                                    _mkt("35°C", "no-35")],
                    }])
                return _FakeResp([])
            return _FakeResp({"bids": [], "asks": []})

        def post(self, url, json=None, timeout=None):
            # Batched /books: echo an empty book per requested token id.
            return _FakeResp([{"asset_id": item["token_id"], "bids": [], "asks": []}
                              for item in (json or [])])

    p = GammaMarketProvider(market_terms=("london",), metar_icao="EGLC",
                            session=_BracketSession(), tz=ZoneInfo("Europe/London"))
    p.discover(now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc))
    p._needs_rediscover = lambda now: False  # pin the discovered event (snapshot() uses wall-clock now)
    snap = p.snapshot()
    flags = {b.temp: b.upper_open for b in snap.brackets}
    assert flags == {33: False, 34: False, 35: True}


# -- frontier-band book reads + transient-failure handling ------------------
def _band_market(title, no_tok):
    return {
        "groupItemTitle": title,
        "question": f"Will the highest temperature be {title}?",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"yes-{no_tok}", no_tok]),
        "enableOrderBook": True,
    }


class _BandSession:
    """Event with brackets 30..35 (35 = open top). Records every /books POST so a
    test can assert which token ids were actually fetched. ``books_status`` /
    ``books_raise`` simulate a CLOB rate-limit / network blip."""

    def __init__(self, *, books_status=200, books_raise=False):
        self.posted_tokens = []   # list of token-id lists, one per POST
        self.books_status = books_status
        self.books_raise = books_raise

    def get(self, url, params=None, timeout=None):
        if "events" in url and (params or {}).get("offset", 0) == 0:
            return _FakeResp([{
                "slug": "highest-temperature-in-london-on-june-8-2026",
                "title": "Highest temperature in London on June 8 2026",
                "closed": False,
                "endDate": "2026-06-30T22:00:00Z",
                "description": "Highest temperature at EGLC per Weather "
                               "Underground (wunderground.com).",
                "markets": [_band_market(f"{t}°C", f"no-{t}") for t in range(30, 35)]
                            + [_band_market("35°C or higher", "no-35")],
            }])
        return _FakeResp([])

    def post(self, url, json=None, timeout=None):
        toks = [item["token_id"] for item in (json or [])]
        self.posted_tokens.append(toks)
        if self.books_raise:
            raise TimeoutError("read timed out")
        return _FakeResp([{"asset_id": t, "bids": [], "asks": []} for t in toks],
                         status=self.books_status)


def _band_provider(sess):
    from zoneinfo import ZoneInfo
    from dead_bracket.feeds_http import GammaMarketProvider
    p = GammaMarketProvider(market_terms=("london",), metar_icao="EGLC",
                            session=sess, tz=ZoneInfo("Europe/London"),
                            book_band_below=1, book_band_above=2)
    p.discover(now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc))
    p._needs_rediscover = lambda now: False  # pin the discovered event
    return p


def test_snapshot_fetches_only_frontier_band():
    sess = _BandSession()
    p = _band_provider(sess)
    snap = p.snapshot(running_max=32)
    # band = [rmax-1, rmax+2] = [31, 34]; nothing else is fetched.
    assert sorted(b.temp for b in snap.brackets) == [31, 32, 33, 34]
    assert sess.posted_tokens[-1] == ["no-31", "no-32", "no-33", "no-34"]


def test_snapshot_includes_held_positions_outside_band():
    sess = _BandSession()
    p = _band_provider(sess)
    # Held bracket 30 sits below the band (max 33 -> band [32, 35]) but must still
    # be fetched so its resting sell keeps reconciling.
    snap = p.snapshot(running_max=33, keep_temps={30})
    temps = sorted(b.temp for b in snap.brackets)
    assert 30 in temps and temps == [30, 32, 33, 34, 35]


def test_snapshot_empty_but_valid_when_nothing_near_frontier():
    sess = _BandSession()
    p = _band_provider(sess)
    # Running max far below every bracket: market is up but nothing is tradeable
    # yet -> an empty (not None) snapshot, and no book request at all.
    snap = p.snapshot(running_max=10)
    assert snap is not None and snap.brackets == ()
    assert snap.event_slug.endswith("june-8-2026")
    assert sess.posted_tokens == []  # no wasted /books call


def test_snapshot_raises_transient_on_book_request_failure():
    from dead_bracket.feeds_http import TransientBookError
    import pytest
    for sess in (_BandSession(books_raise=True), _BandSession(books_status=429)):
        p = _band_provider(sess)
        with pytest.raises(TransientBookError):
            p.snapshot(running_max=32)


def test_snapshot_top_bracket_upper_open_even_when_outside_fetched_band():
    sess = _BandSession()
    p = _band_provider(sess)
    # Max 33 -> band [32, 35] includes the top bracket 35; it must be upper_open.
    snap = p.snapshot(running_max=33)
    assert snap.bracket(35).upper_open is True
    assert snap.bracket(34).upper_open is False


def test_date_token_is_unambiguous():
    from datetime import date
    from dead_bracket.feeds_http import date_token
    assert date_token(date(2026, 6, 8)) == "june-8-2026"
    # day-1 token must not be a substring of a day-18 slug
    assert date_token(date(2026, 6, 1)) not in "highest-temperature-in-x-on-june-18-2026"


# -- USDC parsing ----------------------------------------------------------
def test_parse_usdc_base_units():
    from dead_bracket.main import _parse_usdc
    assert _parse_usdc("20000000") == 20.0
    assert _parse_usdc(None) == 0.0
    assert _parse_usdc("bad") == 0.0


# -- batched shared METAR fetcher --------------------------------------------
def test_latest_by_icao_keeps_most_recent_per_station():
    text = (
        "METAR LFPB 081100Z 21/11 Q1015\n"
        "METAR LFPB 081130Z 22/11 Q1015\n"  # newer -> wins
        "METAR EGLC 081120Z 19/10 Q1014\n"
    )
    out = latest_by_icao(text)
    assert "22/11" in out["LFPB"] and out["EGLC"].endswith("Q1014")


def _json_records(text, receipts=None):
    """Turn raw METAR lines into aviationweather JSON records (the fetcher now
    consumes ``format=json``). obsTime is synthesised from ddhhmmZ so recency
    ordering matches the raw timestamp."""

    import re
    recs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        toks = line.split()
        icao = toks[1] if toks[0] in ("METAR", "SPECI") and len(toks) > 1 else toks[0]
        m = re.search(r"(\d{2})(\d{2})(\d{2})Z", line)
        dd, hh, mm = (int(g) for g in m.groups()) if m else (0, 0, 0)
        rec = {"icaoId": icao, "rawOb": line, "obsTime": dd * 86400 + hh * 3600 + mm * 60}
        if receipts and icao in receipts:
            rec["receiptTime"] = receipts[icao]
        recs.append(rec)
    return recs


class _OneShotSession:
    """Counts GETs; returns a fixed batched body (as JSON), or raises to timeout."""

    def __init__(self, body, fail=False, receipts=None):
        self.records = _json_records(body, receipts)
        self.fail = fail
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        if self.fail:
            raise TimeoutError("read timed out")
        return _FakeResp(self.records)


def test_shared_fetcher_batches_and_throttles_by_ttl():
    body = "METAR LFPB 081130Z 22/11 Q1015\nMETAR EGLC 081120Z 19/10 Q1014\n"
    sess = _OneShotSession(body)
    clock = {"t": 1000.0}
    f = SharedMetarFetcher(["LFPB", "EGLC"], session=sess, ttl=240.0, clock=lambda: clock["t"])
    assert "22/11" in f.raw_for("LFPB")
    assert "19/10" in f.raw_for("EGLC")  # served from same fetch
    assert sess.calls == 1  # ONE batched request for both stations
    clock["t"] += 100  # within ttl
    f.raw_for("LFPB")
    assert sess.calls == 1  # throttled
    clock["t"] += 200  # now past ttl
    f.raw_for("LFPB")
    assert sess.calls == 2


def test_shared_fetcher_max_age_drives_aggressive_polling():
    body = "METAR LFPB 081130Z 22/11 Q1015\n"
    sess = _OneShotSession(body)
    clock = {"t": 0.0}
    # Normal ttl is 60s, but a hot worker passes a small max_age to poll fast.
    # Batched origin hits are floored at hot_origin_min_sec while hot.
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0, hot_origin_min_sec=5.0,
                           clock=lambda: clock["t"])
    f.raw_for("LFPB", max_age=3.0)
    assert sess.calls == 1
    clock["t"] += 4  # past the 3s hint but inside the 5s origin floor
    f.raw_for("LFPB", max_age=3.0)
    assert sess.calls == 1  # floored: cache-busted origin hits are throttled
    clock["t"] += 2  # now past the 5s floor
    f.raw_for("LFPB", max_age=3.0)
    assert sess.calls == 2


def test_shared_fetcher_busts_cache_only_when_hot():
    body = "METAR LFPB 081130Z 22/11 Q1015\n"

    class ParamSession(_OneShotSession):
        def __init__(self, body):
            super().__init__(body)
            self.params_seen = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.params_seen.append(dict(params or {}))
            return super().get(url, params=params, headers=headers, timeout=timeout)

    sess = ParamSession(body)
    clock = {"t": 0.0}
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0, clock=lambda: clock["t"])
    f.raw_for("LFPB")  # normal cadence -> plain URL, CDN cache is fine
    assert sess.params_seen[0]["hours"] == 2
    clock["t"] += 61
    f.raw_for("LFPB", max_age=1.0)  # hot -> cache-busted
    # Busting must rotate the LEGAL `hours` param, never add an unknown one:
    # the API 400s on unrecognised parameters (see the regression test below).
    assert sess.params_seen[1]["hours"] != 2
    assert "_" not in sess.params_seen[1]


def test_shared_fetcher_station_fast_path_newer_wins():
    # Batched cache holds the 11:30 obs; while hot, the per-station fast path
    # returns the fresh 12:00 report and must replace it.
    batched = "METAR LFPB 081130Z 22/11 Q1015\n"
    sess = _OneShotSession(batched)
    clock = {"t": 0.0}
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0,
                           station_fetch=lambda icao: "METAR LFPB 081200Z 24/11 Q1015",
                           clock=lambda: clock["t"])
    assert "081130Z" in f.raw_for("LFPB")  # cold fill from the batched fetch
    clock["t"] += 1
    out = f.raw_for("LFPB", max_age=1.0)  # hot -> station fast path merges
    assert "081200Z" in out and "24/11" in out


def test_shared_fetcher_stale_batch_never_overwrites_newer_station():
    # The CDN can serve a stale batched body; a newer per-station reading
    # already in the cache must survive the next batched refresh.
    stale_batch = "METAR LFPB 081130Z 22/11 Q1015\n"
    sess = _OneShotSession(stale_batch)
    clock = {"t": 0.0}
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0, hot_origin_min_sec=5.0,
                           station_fetch=lambda icao: "METAR LFPB 081200Z 24/11 Q1015",
                           clock=lambda: clock["t"])
    f.raw_for("LFPB")                      # cold fill (11:30)
    clock["t"] += 1
    assert "081200Z" in f.raw_for("LFPB", max_age=1.0)  # station upgrade to 12:00
    clock["t"] += 6                        # past the origin floor -> batched refresh
    out = f.raw_for("LFPB", max_age=1.0)   # batch still serves stale 11:30
    assert "081200Z" in out                # newer station reading preserved


def test_latest_json_by_icao_skips_speci_only_for_excluded_icaos():
    from dead_bracket.feeds_http import latest_json_by_icao
    recs = [
        {"icaoId": "UUWW", "rawOb": "METAR UUWW 081130Z 22/11 Q1015",
         "obsTime": 100, "metarType": "METAR"},
        {"icaoId": "UUWW", "rawOb": "SPECI UUWW 081145Z 24/11 Q1015",
         "obsTime": 200, "metarType": "SPECI"},  # newer but a SPECI
    ]
    # No exclusion -> newest (the SPECI) wins.
    assert "24/11" in latest_json_by_icao(recs)["UUWW"][0]
    # UUWW excluded (Moscow) -> the routine 22 is kept despite the newer SPECI.
    assert "22/11" in latest_json_by_icao(recs, exclude_speci_icaos={"UUWW"})["UUWW"][0]


def test_shared_fetcher_skips_tgftp_for_speci_excluded_icao():
    # tgftp lines are unmarked, so for a SPECI-excluded icao the fast path can't
    # be trusted -> it is skipped, and the (filtered) JSON routine is served.
    sess = _OneShotSession("METAR UUWW 081130Z 22/11 Q1015\n")
    f = SharedMetarFetcher(["UUWW"], session=sess, ttl=60.0,
                           station_fetch=lambda icao: "UUWW 081145Z 24/11 Q1015",
                           exclude_speci_icaos={"UUWW"}, clock=lambda: 0.0)
    out = f.raw_for("UUWW", max_age=1.0)  # hot: would normally race tgftp
    assert "22/11" in out and "24/11" not in out  # tgftp not consulted for UUWW


def test_latest_json_by_icao_parses_receipt():
    from dead_bracket.feeds_http import latest_json_by_icao
    recs = [
        {"icaoId": "LFPB", "rawOb": "METAR LFPB 081130Z 22/11 Q1015",
         "obsTime": 100, "receiptTime": "2026-06-08T11:32:49Z"},
        {"icaoId": "LFPB", "rawOb": "METAR LFPB 081100Z 21/11 Q1015", "obsTime": 50},  # older
    ]
    out = latest_json_by_icao(recs)
    raw, obs, receipt = out["LFPB"]
    assert "22/11" in raw  # newest obs kept
    assert receipt.hour == 11 and receipt.minute == 32


def test_shared_fetcher_exposes_receipt_time():
    body = "METAR LFPB 081130Z 22/11 Q1015\n"
    sess = _OneShotSession(body, receipts={"LFPB": "2026-06-08T11:32:49Z"})
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0, clock=lambda: 0.0)
    assert "22/11" in f.raw_for("LFPB")
    rcpt = f.receipt_for("LFPB")
    assert rcpt is not None and rcpt.minute == 32


def test_station_newer_obs_does_not_inherit_old_receipt():
    # Repro of the bogus "0s (+1700s)": JSON has the 11:30 report (receipt 11:32);
    # the per-station fast path then brings the fresh 12:00 report (no receipt).
    # The 12:00 reading must NOT inherit 11:30's receipt.
    sess = _OneShotSession("METAR LFPB 081130Z 22/11 Q1015\n",
                           receipts={"LFPB": "2026-06-08T11:32:00Z"})
    clock = {"t": 0.0}
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0,
                           station_fetch=lambda icao: "METAR LFPB 081200Z 24/11 Q1015",
                           clock=lambda: clock["t"])
    f.raw_for("LFPB")                                  # serve the 11:30 (has receipt)
    assert f.receipt_for("LFPB").minute == 32
    clock["t"] += 1
    out = f.raw_for("LFPB", max_age=1.0)               # fast path -> 12:00, no receipt
    assert "081200Z" in out
    assert f.receipt_for("LFPB") is None               # NOT the stale 11:30 receipt


def test_receipt_for_matches_served_raw_under_concurrent_refresh():
    # receipt_for must reflect what raw_for just returned, even if the cache is
    # advanced (by another city's refresh) between the two calls.
    sess = _OneShotSession("METAR LFPB 081130Z 22/11 Q1015\n",
                           receipts={"LFPB": "2026-06-08T11:32:00Z"})
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0, clock=lambda: 0.0)
    raw = f.raw_for("LFPB")
    # Simulate a concurrent refresh advancing the cache to a newer obs.
    f._store("LFPB", "METAR LFPB 081200Z 24/11 Q1015", (8, 12, 0), None, 0.0)
    assert "081130Z" in raw
    assert f.receipt_for("LFPB").minute == 32  # still the 11:30 receipt we served


def test_shared_fetcher_station_failure_is_best_effort():
    def boom(icao):
        raise TimeoutError("tgftp down")

    body = "METAR LFPB 081130Z 22/11 Q1015\n"
    sess = _OneShotSession(body)
    clock = {"t": 0.0}
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0, station_fetch=boom,
                           clock=lambda: clock["t"])
    assert f.raw_for("LFPB", max_age=1.0) is not None  # batched data still served  # refreshed aggressively because of the hint


def test_shared_fetcher_serves_last_good_through_blip_then_ages_out():
    body = "METAR LFPB 081130Z 22/11 Q1015\n"
    sess = _OneShotSession(body)
    clock = {"t": 0.0}
    f = SharedMetarFetcher(["LFPB"], session=sess, ttl=60.0, max_age=600.0,
                           retries=0, clock=lambda: clock["t"])
    assert f.raw_for("LFPB") is not None  # initial success
    sess.fail = True  # API starts timing out
    clock["t"] = 120
    assert f.raw_for("LFPB") is not None  # transient blip -> last-good served
    clock["t"] = 700  # beyond max_age since last success
    assert f.raw_for("LFPB") is None  # ages out -> feed correctly goes stale


# ---------------------------------------------------------------------------
# Batched METAR cache-busting (2026-08-20 regression)
# ---------------------------------------------------------------------------
def test_hot_window_bust_never_adds_an_unknown_query_param():
    """aviationweather.gov answers HTTP 400 "Unexpected query parameter
    provided" for ANY parameter it does not recognise, so the old `_=<millis>`
    cache-bust made every hot-window batch fail — silently, because _refresh
    swallows exceptions. Stations with the per-station tgftp fast path masked
    it; the routine-only ICAOs that deliberately skip tgftp (Moscow, Istanbul,
    Tel Aviv) were left with no source and collected ~2 obs/day.
    """

    from dead_bracket.feeds_http import AWC_HOURS_ROTATION, SharedMetarFetcher

    seen = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return []

    class Session:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.append(dict(params or {}))
            return Resp()

    f = SharedMetarFetcher(["UUWW"], session=Session(), ttl=60.0)
    for _ in range(4):
        f._refresh(0.0, bust=True)

    allowed = {"ids", "format", "hours"}
    for params in seen:
        assert set(params) <= allowed, f"unknown param would 400: {set(params) - allowed}"
    # And the bust must actually vary the cache key.
    assert len({p["hours"] for p in seen}) > 1
    assert {p["hours"] for p in seen} <= set(AWC_HOURS_ROTATION)
