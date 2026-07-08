"""HTTP wiring for the live feeds (spec §6).

Isolated from the strategy-critical modules: this is the only place that touches
the network, so the rest of the system stays mockable. ``make_metar_fetch``
produces the fetch callable for :class:`~dead_bracket.metar.MetarFeed`, and
``GammaMarketProvider`` produces the ``snapshot_fn`` for
:class:`~dead_bracket.market.MarketFeed` (Gamma discovery + CLOB book/depth).

Discovery uses Gamma's ``daily-temperature`` tag (reliable; the unordered
``/events`` scan misses these) and picks the event whose slug matches the city
and resolves soonest. The discovered event is re-resolved each new day and when
it expires, so the bot never keeps hammering a closed event's tokens (the cause
of the CLOB 404 storm). Per-token book failures are tolerated — a single
bracket's missing book skips that bracket instead of failing the whole snapshot.

The only weather input that can mark a bracket dead is the resolution METAR.
The sub-hourly national feeds at the bottom of this module (Météo-France /
KNMI / FMI / JMA / NEA, added for the forecast path) are SIGNAL-ONLY: they
feed forecast.py's probabilistic entries and never reach a MetarFeed, a
running max, or the dead-bracket logic.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

try:  # requests is a runtime dep; tests never import this module's network path
    import requests
except Exception:  # pragma: no cover - import guard
    requests = None  # type: ignore

try:  # connection pooling + 429/5xx retry for the hammered CLOB book endpoint
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - import guard
    HTTPAdapter = Retry = None  # type: ignore

from .models import Bracket, MarketSnapshot


class TransientBookError(Exception):
    """A discovered market whose batched book request failed this tick.

    Raised by :meth:`GammaMarketProvider.snapshot` when the event and its
    brackets resolve fine but the single batched ``/books`` call fails (network
    blip / CLOB rate limit). It is distinct from "no market" (snapshot -> None):
    the caller skips the tick instead of flapping the market-unavailable notice.
    """

_DEFAULT_UA = "dead-bracket-bot/0.1 (METAR resolution feed; contact: ops@example.com)"
AVIATIONWEATHER_URL = "https://aviationweather.gov/api/data/metar"
TAF_URL = "https://aviationweather.gov/api/data/taf"
TGFTP_URL = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
# Batched book endpoint: ONE POST returns the books for several tokens at once.
# We deliberately do NOT use the per-token GET ``/book`` — fetching every
# bracket of every city each second tripped the CLOB rate limit (all books then
# 404/429'd at once, flapping "market unavailable" -> "restored" all day).
CLOB_BOOKS_URL = "https://clob.polymarket.com/books"
DAILY_TEMP_TAG = "daily-temperature"
# The strategy is the daily HIGH (spec §1): once the running max passes a
# bracket it can never be the high. In a LOWEST-temperature market the same
# logic inverts into a near-certain loss (low brackets look "dead" vs the
# daytime max while the overnight low can still land on them). So discovery
# must only ever accept "highest" events and must hard-reject "lowest".
REQUIRED_EVENT_TERM = "highest"
FORBIDDEN_EVENT_TERMS = ("lowest", "minimum")

# Resolution-source cross-check (CLAUDE.md invariant: a bracket is "dead" only if
# the running max matches *that market's* resolution source — a wrong station or
# data source turns a false dead into a real loss). Before trading a city we
# require its event text to name the station ICAO; by default we ALSO require it
# to name Weather Underground.
# A few ICAOs are exempt from the Weather-Underground half only: their daily markets
# resolve off a non-WU source (Moscow=UUWW on the routine cycle / weather.gov,
# Istanbul=LTFM likewise; Tel Aviv=LLBG resolves off NOAA's weather.gov timeseries
# for the station), so demanding the "wunderground" string would idle them forever.
# They still MUST name their station ICAO — the station is the part that guarantees
# the running max matches the market, so it is never waived.
WUNDERGROUND_TERM = "wunderground"
WUNDERGROUND_EXEMPT_ICAOS = frozenset({"UUWW", "LTFM", "LLBG"})

_TEMP_IN_QUESTION = re.compile(r"(-?\d+)\s*°?\s*[cC]\b")
# Open-ended top bracket: "... or higher/above/more/over/greater", or a trailing
# "+" (e.g. "30°C+" / "30+"). Matched on the bracket title to exempt it from the
# dead check (see ``bracket_is_upper_open``).
_UPPER_OPEN = re.compile(r"\bor\s+(?:higher|above|more|over|greater)\b|\+\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Pure helpers (no network) — these carry the token-resolution logic and are
# unit-tested against fixture dicts.
# --------------------------------------------------------------------------
def _loads_maybe(value):
    """Gamma returns ``outcomes``/``clobTokenIds`` as JSON-encoded strings."""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def resolve_no_token(market: dict) -> Optional[str]:
    """Return the CLOB token id for the NO outcome of a market, or None.

    Pairs ``outcomes`` with ``clobTokenIds`` positionally and returns the id
    whose outcome is "No" (case-insensitive). Returns None if the market has no
    order book, the arrays are missing/mismatched, or there is no NO outcome.
    """

    if market.get("enableOrderBook") is False:
        return None
    outcomes = _loads_maybe(market.get("outcomes"))
    tokens = _loads_maybe(market.get("clobTokenIds"))
    if not outcomes or not tokens or len(outcomes) != len(tokens):
        return None
    for name, tok in zip(outcomes, tokens):
        if str(name).strip().lower() == "no" and tok:
            return str(tok)
    return None


def parse_bracket_temp(market: dict) -> Optional[int]:
    """Extract the integer °C threshold from a bracket market.

    Handles "24°C", "24°C or below", "28°C or higher", etc. by taking the first
    integer followed by a degree/C marker.
    """

    text = market.get("groupItemTitle") or market.get("question") or ""
    m = _TEMP_IN_QUESTION.search(text)
    return int(m.group(1)) if m else None


def bracket_is_upper_open(market: dict) -> bool:
    """True if this is the open-ended top bracket ("28°C or higher", "30°C+").

    Such a bracket WINS once the day's high reaches its threshold, so it must
    never be flagged dead by the running max (an over-the-top reading would
    otherwise buy a NO that settles to $0). The "or below" bottom bracket is
    deliberately NOT matched — it dies correctly once the max passes it.
    """

    text = market.get("groupItemTitle") or market.get("question") or ""
    return bool(_UPPER_OPEN.search(text))


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sorted_levels(raw_levels, *, reverse: bool):
    """Normalise a CLOB book side into sorted ``(price, size)`` tuples."""

    out = []
    for lvl in raw_levels or []:
        try:
            px = float(lvl["price"])
            sz = float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((px, sz))
    out.sort(key=lambda x: x[0], reverse=reverse)
    return tuple(out)


def _event_end(ev: dict) -> Optional[datetime]:
    raw = ev.get("endDate") or ev.get("endDateIso")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def date_token(d: date) -> str:
    """The unambiguous date marker in a daily-temperature slug, e.g. 'june-8-2026'.

    Includes the year so it can't partial-match (``june-1-2026`` is not a
    substring of ``june-18-2026``).
    """

    return f"{d.strftime('%B').lower()}-{d.day}-{d.year}"


def is_highest_event(hay: str) -> bool:
    """True iff the slug/title text identifies a HIGHEST-temperature market.

    Requires the word "highest" and rejects any forbidden term — both checks, so
    a hypothetical "highest vs lowest" combo market is also excluded.
    """

    hay = hay.lower()
    if REQUIRED_EVENT_TERM not in hay:
        return False
    return not any(t in hay for t in FORBIDDEN_EVENT_TERMS)


def event_text(ev: dict) -> str:
    """All resolution-relevant text of an event + its markets, lowercased.

    The resolution criteria ("...the highest temperature ... as reported by
    Weather Underground for station XXXX ...") live in the event/market
    descriptions, so the source check searches those plus the title, slug, and
    each bracket's question.
    """

    parts = [
        str(ev.get("description") or ""),
        str(ev.get("title") or ""),
        str(ev.get("slug") or ""),
    ]
    for mkt in ev.get("markets") or []:
        parts.append(str(mkt.get("description") or ""))
        parts.append(str(mkt.get("question") or ""))
    return " ".join(parts).lower()


def source_confirmed(ev: dict, icao: Optional[str]) -> bool:
    """True iff ``ev`` is confirmed to resolve on ``icao`` (and, by default, WU).

    The resolution-source cross-check: the event text MUST name the station code
    — that is what guarantees the running max matches the market, so it is never
    waived. By default it must ALSO name "wunderground". A WU-exempt ICAO
    (UUWW/LTFM) skips ONLY the Weather-Underground half (its market resolves off a
    non-WU source) but still has to name its station. An unset ``icao`` skips the
    check entirely (keeps the pure picker usable). See ``WUNDERGROUND_EXEMPT_ICAOS``.
    """

    if not icao:
        return True
    text = event_text(ev)
    if icao.lower() not in text:
        return False  # wrong/unnamed station -> never trade (false-dead risk)
    if icao.upper() in WUNDERGROUND_EXEMPT_ICAOS:
        return True  # station confirmed; this market resolves off Weather Underground
    return WUNDERGROUND_TERM in text


def choose_event(events: list, terms: tuple[str, ...], *, now: Optional[datetime] = None,
                 require_date: Optional[date] = None, icao: Optional[str] = None) -> Optional[dict]:
    """Pick the city's daily-temperature event from candidates.

    Matches all city terms against the slug/title and drops closed events. When
    ``require_date`` is given (always, in normal operation) ONLY an event whose
    slug carries that exact date is eligible — so the bot can never track
    tomorrow's (forecast) market against today's METAR. If today's event is
    absent/closed it returns None (idle) rather than falling back to another day.

    When ``icao`` is given, the event must also pass the resolution-source
    cross-check (:func:`source_confirmed`): its text must name the station and
    Weather Underground, unless the ICAO is exempt. A market that doesn't confirm
    our station/source is dropped (idle) rather than traded on a wrong source.
    """

    now = now or datetime.now(timezone.utc)
    want = date_token(require_date) if require_date else None
    matches = []
    for ev in events:
        hay = f"{ev.get('slug') or ''} {ev.get('title') or ''}".lower()
        if ev.get("closed"):
            continue
        if not is_highest_event(hay):
            continue  # never a lowest-temperature (or other non-high) market
        if not all(term in hay for term in terms):
            continue
        if want is not None and want not in hay:
            continue  # not today's event -> never eligible
        if not source_confirmed(ev, icao):
            continue  # resolution source not confirmed (wrong station / not WU)
        matches.append(ev)
    if not matches:
        return None

    def sort_key(ev):
        end = _event_end(ev)
        if end is None:
            return (2, 0.0)
        secs = (end - now).total_seconds()
        return (0, secs) if secs >= 0 else (1, -secs)

    return sorted(matches, key=sort_key)[0]


def _pooled_session():  # pragma: no cover - exercised only with the real network
    """A requests session with connection pooling + a small 429/5xx retry.

    Used for the CLOB book reads (one batched POST per city per tick across all
    cities still adds up). The retry honours ``Retry-After`` and never raises on
    a bad status — the caller inspects ``status_code`` itself.
    """

    if requests is None:
        return None
    sess = requests.Session()
    if HTTPAdapter is not None and Retry is not None:
        retry = Retry(
            total=1, backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True, raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
    return sess


def make_metar_fetch(
    *, user_agent: str = _DEFAULT_UA, timeout: float = 10.0
) -> Callable[[str], Optional[str]]:
    """Return a fetch callable that yields the raw METAR for an ICAO."""

    def fetch(icao: str, max_age: Optional[float] = None) -> Optional[str]:  # pragma: no cover - network
        if requests is None:
            return None
        resp = requests.get(
            AVIATIONWEATHER_URL,
            params={"ids": icao, "format": "raw", "hours": 2},
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.text.strip()
        if not text:
            return None
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1] if lines else None

    return fetch


_METAR_TIME = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")

# HKO official open data ("regional weather" real-time report). Free, no API key,
# whole-degC, updated in real time — and it is UPSTREAM of what The Weather
# Company / Wunderground ingest for their HK-region stations, so reading it
# directly matches WU's resolution source and front-runs WU's publish lag.
HKO_RHRREAD_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"


def parse_hko_rhrread(payload, place: str):
    """Extract ``(temp_c, obs_time)`` for one named HKO station from an rhrread
    response, or ``None`` if absent/malformed.

    Temperature is whole-degC at the source (matches the integer-bracket
    resolution); a stray float is rounded defensively. ``obs_time`` is the feed's
    ``recordTime`` (tz-aware). Pure/deterministic — no network, unit-testable.
    """

    try:
        block = payload["temperature"]
        obs_time = datetime.fromisoformat(block["recordTime"])
        for row in block["data"]:
            if row.get("place") == place and row.get("unit") == "C":
                return int(round(float(row["value"]))), obs_time
    except (KeyError, TypeError, ValueError):
        return None
    return None


def synth_hko_metar(icao: str, temp_c: int, obs_time: datetime, place: str) -> str:
    """Render an HKO reading as a minimal, parseable METAR line so it drives an
    existing ``MetarFeed`` with no downstream changes (running max, dedup, log,
    staleness, day rollover all unchanged).

    Only temperature matters for the dead-NO edge; the source has no dewpoint,
    so the dewpoint slot carries an explicit ``XX`` placeholder (an earlier
    version mirrored the temp — ``26/26`` — which sent audits chasing a
    phantom dewpoint). A trailing ``RMK SRC=HKO:<place>`` keeps the logged raw
    honest (clearly not a spoofed ZGSZ airport METAR) while leaving ``icao`` =
    the market station so the feed's station-isolation guard still passes.
    """

    z = obs_time.astimezone(timezone.utc).strftime("%d%H%MZ")
    tt = f"M{abs(temp_c):02d}" if temp_c < 0 else f"{temp_c:02d}"
    return f"{icao} {z} {tt}/XX RMK SRC=HKO:{place}"


class HkoStationFetcher:
    """MetarFeed-compatible fetch for a market that resolves off an HKO station.

    Reads ONE station's temperature from HKO's rhrread open data and returns it as
    a synthesized METAR line (:func:`synth_hko_metar`). ttl-throttled and
    fail-soft in the same shape as :class:`SharedMetarFetcher`: a transient failure
    keeps serving the last-good reading for up to ``max_age`` seconds; a prolonged
    outage returns ``None`` so the feed's stale-halt still trips. Thread-safe. The
    ``session`` is injected so tests exercise it with canned JSON (no live network).
    """

    def __init__(self, *, station: str, icao: str, session,
                 user_agent: str = _DEFAULT_UA, timeout: float = 15.0,
                 ttl: float = 240.0, max_age: float = 1200.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._station = station
        self._icao = icao
        self._session = session
        self._ua = user_agent
        self._timeout = timeout
        self._ttl = ttl
        self._max_age = max_age
        self._clock = clock
        self._raw: Optional[str] = None
        self._last_success: Optional[float] = None
        self._last_attempt: Optional[float] = None
        self._lock = threading.Lock()

    def raw_for(self, icao: Optional[str] = None, max_age: Optional[float] = None) -> Optional[str]:
        interval = max_age if max_age is not None else self._ttl
        with self._lock:
            now = self._clock()
            if self._last_attempt is None or (now - self._last_attempt) >= interval:
                self._last_attempt = now
                self._refresh(now)
            if self._last_success is None or (now - self._last_success) > self._max_age:
                return None  # no recent success -> let the feed go stale
            return self._raw

    def _refresh(self, now: float) -> None:  # pragma: no cover - network
        if self._session is None:
            return
        try:
            resp = self._session.get(
                HKO_RHRREAD_URL,
                params={"dataType": "rhrread", "lang": "en"},
                headers={"User-Agent": self._ua},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            parsed = parse_hko_rhrread(resp.json(), self._station)
        except Exception:
            return  # keep last-good; raw_for ages it out via max_age
        if parsed is None:
            return
        temp_c, obs_time = parsed
        self._raw = synth_hko_metar(self._icao, temp_c, obs_time, self._station)
        self._last_success = now


# --------------------------------------------------------------------------
# Shenzhen / Lau Fau Shan calibration sources (see lfs_calibration.py). These
# are DATA-COLLECTION feeds only: nothing here reaches a MetarFeed or the
# strategy. The 1-min CSV is HKO's own upstream of the market's resolution
# source (the one permitted non-METAR feed, per the calibration requirements);
# the api.weather.com endpoints are the resolution source itself (what WU
# renders). Fetchers return None on any failure — the collectors count it and
# skip the tick, so an outage never crashes the calibration worker.
# --------------------------------------------------------------------------
HKO_1MIN_URL = ("https://data.weather.gov.hk/weatherAPI/hko_data/"
                "regional-weather/latest_1min_temperature.csv")
TWC_TIMESERIES_URL = ("https://api.weather.com/v1/location/{location}/"
                      "observations/timeseries.json")
TWC_HISTORICAL_URL = ("https://api.weather.com/v1/location/{location}/"
                      "observations/historical.json")


def _cache_bust() -> str:
    return str(int(time.time() * 1000))


def make_hko_1min_fetch(*, session=None, user_agent: str = _DEFAULT_UA,
                        timeout: float = 15.0) -> Callable[[], Optional[str]]:
    """Fetch callable for the HKO latest-1-minute regional temperature CSV.

    Returns the CSV text or None. Served as ``application/octet-stream`` with a
    possible BOM, so it is decoded from bytes as utf-8-sig rather than trusting
    the content-type. Cache-busted per request (the file sits behind a cache;
    a stale snapshot would just re-serve the last observation minute).
    """

    sess = session or (requests.Session() if requests else None)

    def fetch() -> Optional[str]:  # pragma: no cover - network
        if sess is None:
            return None
        try:
            resp = sess.get(HKO_1MIN_URL, params={"_": _cache_bust()},
                            headers={"User-Agent": user_agent,
                                     "Cache-Control": "no-cache"},
                            timeout=timeout)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            return resp.content.decode("utf-8-sig", errors="replace")
        except Exception:
            return None

    return fetch


# api.weather.com REJECTS unknown query params with a 400 (verified live
# 2026-07-07), so the usual "_=<millis>" cache-bust cannot be used, and its
# cache ignores Cache-Control/Pragma request headers (s-maxage=300 — the SAME
# transaction_id is re-served for up to 5 minutes). The cache key does include
# the query string, though, so rotating the ``hours`` parameter through
# distinct values gives every poll its own cache entry; with >= 6 values at a
# 60 s cadence each entry has expired before its value comes around again ->
# origin-fresh data every poll. Larger ``hours`` merely returns more history
# rows, which the row-version collector dedupes for free.
TWC_HOURS_ROTATION = (2, 3, 4, 5, 6, 7)


def make_twc_timeseries_fetch(*, api_key: str, location: str = "ZGSZ:9:CN",
                              hours_rotation: tuple[int, ...] = TWC_HOURS_ROTATION,
                              session=None, user_agent: str = _DEFAULT_UA,
                              timeout: float = 15.0) -> Callable[[], Optional[dict]]:
    """Fetch callable for TWC's observation timeseries (the WU row source).

    Returns the parsed JSON payload or None. Cache-busted by rotating the
    ``hours`` parameter (see ``TWC_HOURS_ROTATION`` above) so 60 s polling sees
    fresh row versions, not the 5-minute CDN copy; the collector additionally
    watches ``metadata.transaction_id`` to detect a stale cache slipping
    through anyway.
    """

    sess = session or (requests.Session() if requests else None)
    state = {"i": 0}

    def fetch() -> Optional[dict]:  # pragma: no cover - network
        if sess is None:
            return None
        hours = hours_rotation[state["i"] % len(hours_rotation)]
        state["i"] += 1
        try:
            resp = sess.get(TWC_TIMESERIES_URL.format(location=location),
                            params={"apiKey": api_key, "units": "m",
                                    "hours": hours},
                            headers={"User-Agent": user_agent},
                            timeout=timeout)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    return fetch


def make_twc_historical_fetch(*, api_key: str, location: str = "ZGSZ:9:CN",
                              session=None, user_agent: str = _DEFAULT_UA,
                              timeout: float = 20.0) -> Callable[[str], Optional[dict]]:
    """Fetch callable for TWC's historical daily table (the K3 capture).

    Takes the target day as ``yyyyMMdd`` (start=end, i.e. one HKT day of final
    rows + any max_temp/min_temp summary fields) and returns the parsed JSON
    payload or None.
    """

    sess = session or (requests.Session() if requests else None)

    def fetch(day_yyyymmdd: str) -> Optional[dict]:  # pragma: no cover - network
        if sess is None:
            return None
        try:
            # No cache-bust param (api.weather.com 400s on unknown params);
            # the per-day startDate/endDate already vary the cache key, and
            # the two capture slots are hours apart (>> the 5-min cache TTL).
            resp = sess.get(TWC_HISTORICAL_URL.format(location=location),
                            params={"apiKey": api_key, "units": "m",
                                    "startDate": day_yyyymmdd,
                                    "endDate": day_yyyymmdd},
                            headers={"User-Agent": user_agent},
                            timeout=timeout)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    return fetch


def make_tgftp_fetch(*, user_agent: str = _DEFAULT_UA, timeout: float = 5.0,
                     session=None) -> Callable[[str], Optional[str]]:
    """Per-station fetch from NOAA's tgftp file server (hot-window fast path).

    One tiny text file per station, updated as reports arrive — the canonical
    "poll me" METAR distribution. Used during hot windows to race the batched
    aviationweather fetch, whose CDN caches responses for up to 60s.
    """

    sess = session or (requests.Session() if requests else None)

    def fetch(icao: str) -> Optional[str]:  # pragma: no cover - network
        if sess is None:
            return None
        resp = sess.get(TGFTP_URL.format(icao=icao),
                        headers={"User-Agent": user_agent, "Cache-Control": "no-cache"},
                        timeout=timeout)
        if resp.status_code != 200:
            return None
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        return lines[-1] if lines else None

    return fetch


def _obs_key(line: str) -> tuple:
    m = _METAR_TIME.search(line)
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)


def latest_by_icao(text: str) -> dict[str, str]:
    """Group a multi-station raw METAR response to the latest line per station.

    The data API returns several hours of observations for each requested id;
    we keep, per ICAO, the line with the most recent ddhhmmZ timestamp.
    """

    best: dict[str, str] = {}
    best_t: dict[str, tuple] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        toks = line.split()
        icao = toks[1] if toks and toks[0] in ("METAR", "SPECI") and len(toks) > 1 else toks[0]
        if not re.fullmatch(r"[A-Z]{4}", icao):
            continue
        t = _obs_key(line)
        if icao not in best_t or t >= best_t[icao]:
            best[icao] = line
            best_t[icao] = t
    return best


def latest_json_by_icao(records, exclude_speci_icaos=()) -> dict[str, tuple]:
    """Group an aviationweather JSON response to the latest record per station.

    Returns ``{ICAO: (rawOb, obs_key, receipt_datetime|None)}`` keyed by the most
    recent ``obsTime``. ``receiptTime`` is when AWC ingested the report (used to
    split publish delay from receipt delay); ``rawOb`` is the raw METAR we parse.

    ``exclude_speci_icaos`` drops off-cycle SPECI records for the listed ICAOs
    (via the ``metarType`` field / ``SPECI`` prefix) so their newest *routine*
    METAR wins. Production no longer uses this (routine-only cities now surface
    SPECIs so the feed can log-but-not-fold them); it is kept for callers/tests
    that want the pure routine picker.
    """

    exclude = set(exclude_speci_icaos)
    out: dict[str, tuple] = {}
    best_obs: dict[str, int] = {}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        icao = rec.get("icaoId") or rec.get("station_id")
        raw = rec.get("rawOb") or rec.get("rawText")
        obs_time = rec.get("obsTime")
        if not icao or not raw or obs_time is None:
            continue
        if icao in exclude and (rec.get("metarType") == "SPECI"
                                or str(raw).strip().startswith("SPECI")):
            continue
        try:
            obs_time = int(obs_time)
        except (TypeError, ValueError):
            continue
        if icao in best_obs and obs_time <= best_obs[icao]:
            continue
        best_obs[icao] = obs_time
        receipt = SharedMetarFetcher._parse_receipt(rec.get("receiptTime"))
        out[icao] = (str(raw).strip(), _obs_key(str(raw)), receipt)
    return out


class SharedMetarFetcher:
    """One batched METAR fetch shared by every city (spec §6).

    Collapses the per-city fan-out (N requests per cycle) into a single request
    for all stations, throttled to one network call per ``ttl`` seconds. A
    transient failure (timeout) keeps serving the last-good reading for up to
    ``max_age`` seconds so a brief blip doesn't trip the stale-feed pause; a
    prolonged outage stops serving (returns None) so genuine staleness is still
    detected. Network call is retried ``retries`` times. Thread-safe.

    Hot windows (a small ``max_age`` hint from a worker expecting a report):
    the aviationweather CDN caches our batched URL for up to 60s (measured
    ``Cache-control: max-age=60`` / ``X-Cache: TCP_HIT``), which alone can add a
    minute of latency right when it matters. So while hot we (a) cache-bust the
    batched request (forces an origin fetch) at a slightly throttled cadence,
    and (b) race a per-station ``station_fetch`` (NOAA tgftp) at the full hot
    cadence — whichever source carries the newer observation wins.
    """

    def __init__(
        self,
        icaos,
        *,
        session,
        user_agent: str = _DEFAULT_UA,
        timeout: float = 15.0,
        ttl: float = 240.0,
        max_age: float = 1200.0,
        retries: int = 1,
        station_fetch: Optional[Callable[[str], Optional[str]]] = None,
        exclude_speci_icaos=(),
        hot_origin_min_sec: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._icaos = list(dict.fromkeys(icaos))  # de-dup, preserve order
        self._session = session
        self._ua = user_agent
        self._timeout = timeout
        self._ttl = ttl
        self._max_age = max_age
        self._retries = retries
        # ICAOs whose market resolves on the routine cycle only. SPECIs still
        # arrive (and get logged) via the metarType-marked JSON batch; this set
        # only disables the *unmarked* tgftp fast path for them, so an off-cycle
        # SPECI can never slip in looking like a routine METAR and set the max.
        self._exclude_speci = set(exclude_speci_icaos)
        self._station_fetch = station_fetch
        self._hot_origin_min = hot_origin_min_sec
        self._clock = clock
        # Per-station cache: {raw, obs(tuple), receipt(datetime|None)}.
        self._cache: dict[str, dict] = {}
        # Snapshot of what raw_for last handed out per icao, so a paired
        # receipt_for can't be desynced by another city's concurrent refresh.
        self._served: dict[str, Optional[dict]] = {}
        self._last_success: Optional[float] = None
        self._last_attempt: Optional[float] = None
        self._station_last: dict[str, float] = {}
        self._lock = threading.Lock()

    def raw_for(self, icao: str, max_age: Optional[float] = None) -> Optional[str]:
        """Return the latest raw METAR for ``icao``.

        Refreshes the (shared, batched) data when the cache is older than
        ``max_age`` if given, else the default ttl. A hot-window worker passes a
        small ``max_age``: the batched fetch is then cache-busted (origin-fresh,
        min ``hot_origin_min_sec`` apart) and the per-station fast path polls at
        the full hot cadence.
        """

        interval = max_age if max_age is not None else self._ttl
        hot = max_age is not None and max_age < self._ttl
        with self._lock:
            now = self._clock()
            # Batched fetch: while hot, bust the CDN but go no faster than the
            # origin-friendly floor; the per-station path covers the gap.
            batch_interval = max(interval, self._hot_origin_min) if hot else interval
            if self._last_attempt is None or (now - self._last_attempt) >= batch_interval:
                self._last_attempt = now
                self._refresh(now, bust=hot)
            # Per-station fast path at the full hot cadence (tiny tgftp file).
            # Skipped for routine-only icaos: tgftp lines are unmarked, so we
            # can't tell a routine METAR from a SPECI there and an off-cycle SPECI
            # would fold into the max looking routine. Their SPECIs still arrive
            # (marked) via the JSON batch, where the feed logs-but-doesn't-fold.
            if hot and self._station_fetch is not None and icao not in self._exclude_speci:
                last = self._station_last.get(icao)
                if last is None or (now - last) >= interval:
                    self._station_last[icao] = now
                    self._merge_station(icao, now)
            if self._last_success is None or (now - self._last_success) > self._max_age:
                self._served[icao] = None
                return None  # no successful fetch recently -> let the feed go stale
            entry = self._cache.get(icao)
            # Snapshot raw+receipt together so the paired receipt_for matches
            # exactly this reading (a frozen copy; never a live cache reference).
            self._served[icao] = dict(entry) if entry else None
            return entry["raw"] if entry else None

    def receipt_for(self, icao: str) -> Optional[datetime]:
        """AWC receipt time of the reading raw_for last returned for ``icao``.

        ``None`` if unknown (e.g. the reading came in via the per-station fast
        path before the JSON batch carried its receipt metadata). Lets a caller
        split publish delay (obs->receipt) from its own delay (receipt->seen).
        """

        with self._lock:
            served = self._served.get(icao)
            return served.get("receipt") if served else None

    def _store(self, icao: str, raw: str, obs: tuple, receipt: Optional[datetime],
               now: float) -> None:
        """Keep the newest observation per station (and its receipt time)."""

        cur = self._cache.get(icao)
        if cur is not None and obs < cur["obs"]:
            return  # older observation -> ignore
        if cur is not None and obs == cur["obs"]:
            # Same observation re-seen: keep it, but adopt a receipt if we only
            # now learned one (e.g. JSON catching up after a tgftp fast-path hit).
            # NEVER carry an old receipt onto a *newer* obs (that produced the
            # bogus "0s (+1700s)" split).
            if receipt is not None and cur.get("receipt") is None:
                cur["receipt"] = receipt
            self._last_success = now
            return
        # Strictly newer (or first) observation -> use ITS own receipt only
        # (None when it arrived via the per-station fast path).
        self._cache[icao] = {"raw": raw, "obs": obs, "receipt": receipt}
        self._last_success = now

    def _merge_station(self, icao: str, now: float) -> None:
        """Fold a per-station (tgftp) fetch into the cache; newer obs wins.

        tgftp has no receipt-time field, so a fast-path reading carries
        ``receipt=None`` until the JSON batch catches up with the same obs."""

        try:
            raw = self._station_fetch(icao)
        except Exception:
            return  # fast path is best-effort; the batched fetch still runs
        if raw:
            self._store(icao, raw.strip(), _obs_key(raw), None, now)

    @staticmethod
    def _parse_receipt(value) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _refresh(self, now: float, bust: bool = False) -> None:  # pragma: no cover - network
        # JSON carries obsTime + receiptTime + rawOb per record (raw format does
        # not), so we can report the publish/receipt split.
        params = {"ids": ",".join(self._icaos), "format": "json", "hours": 2}
        if bust:
            # Unique param -> CDN cache miss -> origin-fresh data. Only while
            # hot; the cached (max-age=60) response is fine at the slow cadence.
            params["_"] = str(int(time.time() * 1000))
        for _ in range(self._retries + 1):
            try:
                resp = self._session.get(
                    AVIATIONWEATHER_URL,
                    params=params,
                    headers={"User-Agent": self._ua},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                got = False
                # Surface SPECIs from the JSON too (they carry the SPECI keyword,
                # so the FEED can tell them apart and log-but-not-fold them on
                # routine-only cities). We do NOT drop them here — only the
                # unmarked tgftp fast path stays disabled for those stations.
                for ic, rec in latest_json_by_icao(resp.json()).items():
                    raw, obs, receipt = rec
                    # Never let a (cache-stale) batch overwrite a newer reading.
                    self._store(ic, raw, obs, receipt, now)
                    got = True
                if got:
                    self._last_success = now
                return
            except Exception:
                continue  # keep last-good cache; raw_for ages it out via max_age


class GammaMarketProvider:  # pragma: no cover - network orchestration
    """Discovers the city's daily-temperature event and reads CLOB book/depth."""

    def __init__(
        self,
        *,
        market_terms: tuple[str, ...],
        metar_icao: Optional[str] = None,
        event_slug: Optional[str] = None,
        timeout: float = 10.0,
        session=None,
        on_event=None,
        tz: Optional[ZoneInfo] = None,
        book_band_below: int = 1,
        book_band_above: int = 2,
    ) -> None:
        self._terms = tuple(t.lower() for t in market_terms)
        # Resolution station this city trades on; used to cross-check the
        # discovered event's description names our station + Weather Underground.
        self._icao = metar_icao
        self._event_slug = event_slug
        self._timeout = timeout
        self._session = session or _pooled_session()
        # We only read books for the frontier band around the running max — the
        # freshly-dead bracket just below it (the only one the dead-NO edge ever
        # lives on) plus a couple above to watch it approach. Everything else is
        # either already $1 (deep-dead) or far from resolving, so fetching it
        # every second only burned the CLOB rate budget. ``keep_temps`` (held
        # positions) are always added so a resting sell keeps reconciling.
        self._book_band_below = max(0, int(book_band_below))
        self._book_band_above = max(0, int(book_band_above))
        self._on_event = on_event  # optional callback(slug) for telemetry
        self._tz = tz  # city timezone -> which local date's event to track
        self._event: Optional[dict] = None
        self._discovered_date: Optional[str] = None

    def _local_date(self, now: datetime) -> date:
        return now.astimezone(self._tz).date() if self._tz else now.date()

    def _get(self, url: str, **params):
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_books(self, token_ids: list[str]) -> Optional[dict[str, dict]]:
        """Batch-fetch several tokens' books in ONE POST.

        Returns ``{token_id: book}`` on success — a token legitimately without a
        live book is simply absent from the map (caller skips that bracket, as
        the old per-token 404 path did). Returns ``None`` when the *whole*
        request fails (network / non-200, e.g. a 429): the caller turns that into
        a transient skip rather than a vanished market, so a rate-limit blip no
        longer flaps the console.
        """

        if not token_ids:
            return {}
        try:
            resp = self._session.post(
                CLOB_BOOKS_URL,
                json=[{"token_id": t} for t in token_ids],
                timeout=self._timeout,
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        out: dict[str, dict] = {}
        for book in payload or []:
            if not isinstance(book, dict):
                continue
            tok = book.get("asset_id") or book.get("token_id") or book.get("market")
            if tok:
                out[str(tok)] = book
        return out

    # -- discovery ---------------------------------------------------------
    def _needs_rediscover(self, now: datetime) -> bool:
        if self._event is None:
            return True
        # New local day -> re-resolve to today's event (auto daily rollover, per
        # city local midnight — so every timezone rolls on its own 00:00, not a
        # single global one). We deliberately do NOT re-discover merely because
        # the cached event's endDate passed: a daily market sits "resolving" with
        # a past endDate for hours, and re-resolving every poll spammed discovery.
        # The local-date change below is what rolls us forward.
        if self._discovered_date != self._local_date(now).isoformat():
            return True
        # Safety: cached event must be today's and must be a HIGHEST market.
        hay = f"{self._event.get('slug') or ''} {self._event.get('title') or ''}".lower()
        if not is_highest_event(hay):
            self._event = None  # poison: drop a non-high event immediately
            return True
        # Safety: cached event must still confirm our resolution source.
        if not source_confirmed(self._event, self._icao):
            self._event = None  # source no longer confirmed -> drop and re-resolve
            return True
        if not self._event_slug and date_token(self._local_date(now)) not in hay:
            return True
        return False

    def discover(self, *, now: Optional[datetime] = None) -> Optional[dict]:
        if self._session is None:
            return None
        now = now or datetime.now(timezone.utc)
        if self._event_slug:
            events = self._get(GAMMA_EVENTS_URL, slug=self._event_slug)
        else:
            events = []
            offset = 0
            while True:
                page = self._get(GAMMA_EVENTS_URL, closed="false",
                                 tag_slug=DAILY_TEMP_TAG, limit=100, offset=offset)
                if not page:
                    break
                events.extend(page)
                if len(page) < 100:
                    break
                offset += 100
        if self._event_slug:
            ev = events[0] if events else None
            # Even an explicit slug override must be a HIGHEST market — the
            # strategy is incoherent (and loss-making) on a lowest market —
            # and must confirm our resolution station + source.
            if ev is not None and not is_highest_event(
                    f"{ev.get('slug') or ''} {ev.get('title') or ''}"):
                ev = None
            if ev is not None and not source_confirmed(ev, self._icao):
                ev = None
        else:
            # Require today's local-date event — never tomorrow's forecast market
            # — and confirm its resolution station + source (icao cross-check).
            ev = choose_event(events, self._terms, now=now,
                              require_date=self._local_date(now), icao=self._icao)
        today = self._local_date(now).isoformat()
        if ev is not None:
            changed = ev.get("slug") != (self._event.get("slug") if self._event else None)
            self._event = ev
            self._discovered_date = today
            # Announce only on an actual event change, not every re-resolve.
            if changed and self._on_event:
                self._on_event(ev.get("slug"))
        elif self._discovered_date is not None and self._discovered_date != today:
            # The local day rolled but the new day's market isn't open yet: drop
            # yesterday's cached event so we idle cleanly (never serve stale
            # brackets) and keep retrying each poll until today's market appears.
            self._event = None
            self._discovered_date = None
        return ev

    def snapshot(self, running_max: Optional[int] = None,
                 keep_temps=()) -> Optional[MarketSnapshot]:
        """Build the frontier-band book snapshot for today's event.

        Only the brackets near ``running_max`` (the band ``[max-below, max+above]``)
        plus any ``keep_temps`` we still hold are fetched — see the class doc and
        ``_get_books``. ``None`` means "no market" (undiscovered / no resolvable
        brackets); a :class:`TransientBookError` means "books failed this tick"
        (skip, don't flap). An empty-but-valid snapshot means the market is up but
        nothing is near the frontier yet (e.g. early day, max far below brackets).
        """

        now = datetime.now(timezone.utc)
        if self._needs_rediscover(now) and self.discover(now=now) is None:
            return None
        ev = self._event
        assert ev is not None
        # Resolvable bracket metadata (no network): one (temp, NO token, market)
        # per bracket. The structural safeguard's max_temp is taken from this full
        # set, so the open-ended "or higher" top bracket is still flagged
        # upper_open even when the fetched band is only a few brackets wide.
        resolvable: list[tuple[int, str, dict]] = []
        for mkt in ev.get("markets", []):
            temp = parse_bracket_temp(mkt)
            no_token = resolve_no_token(mkt)
            if temp is None or no_token is None:
                continue
            resolvable.append((temp, no_token, mkt))
        if not resolvable:
            return None  # genuinely nothing to trade -> "market unavailable"
        max_temp = max(t for (t, _tok, _m) in resolvable)

        # Select the frontier band (+ held positions). Before the first METAR the
        # frontier is unknown, so fall back to the full set (brief, day-start only).
        keep = {int(t) for t in keep_temps}
        if running_max is None:
            wanted = resolvable
        else:
            low = running_max - self._book_band_below
            high = running_max + self._book_band_above
            wanted = [(t, tok, m) for (t, tok, m) in resolvable
                      if (low <= t <= high) or t in keep]
        if not wanted:
            # Market is up but nothing is near the frontier yet — available, just
            # empty. NOT "unavailable" (that is reserved for a vanished market).
            return MarketSnapshot(city=self._terms[0], brackets=(),
                                  event_slug=ev.get("slug"))

        books = self._get_books([tok for (_t, tok, _m) in wanted])
        if books is None:
            # The whole batched request failed (network / rate limit). Transient:
            # signal the caller to skip the tick instead of flapping the notice.
            raise TransientBookError(self._terms[0])

        brackets: list[Bracket] = []
        for temp, no_token, mkt in wanted:
            book = books.get(no_token)
            if book is None:
                continue  # this token had no live book on a successful request
            brackets.append(
                Bracket(
                    temp=temp,
                    no_asks=sorted_levels(book.get("asks"), reverse=False),
                    no_bids=sorted_levels(book.get("bids"), reverse=True),
                    fees_enabled=bool(mkt.get("feesEnabled", True)),
                    token_id=no_token,
                    neg_risk=bool(mkt.get("negRisk", ev.get("negRisk", False))),
                    tick_size=_as_float(mkt.get("orderPriceMinTickSize"), 0.001),
                    min_order_size=_as_float(mkt.get("orderMinSize"), 0.0),
                    upper_open=bracket_is_upper_open(mkt) or temp == max_temp,
                )
            )
        return MarketSnapshot(
            city=self._terms[0],
            brackets=tuple(sorted(brackets, key=lambda b: b.temp)),
            event_slug=ev.get("slug"),
        )


# --------------------------------------------------------------------------
# Forecast-path sub-hourly feeds (forecast.py / forecast_feeds.py). SIGNAL
# ONLY: these readings drive probabilistic entries, never a MetarFeed or a
# running max. One fetch factory + one PURE parser per national source, so the
# parsers are unit-testable against canned payloads (tests/fixtures/). All
# fetchers return None on any failure — the collector counts it and skips the
# tick, mirroring the calibration-collector convention above.
# --------------------------------------------------------------------------
FMI_WFS_URL = "https://opendata.fmi.fi/wfs"
JMA_LATEST_TIME_URL = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
JMA_MAP_URL = "https://www.jma.go.jp/bosai/amedas/data/map/{stamp}.json"
NEA_AIR_TEMP_URL = "https://api-open.data.gov.sg/v2/real-time/api/air-temperature"
MF_INFRAHORAIRE_URL = ("https://public-api.meteofrance.fr/public/DPObs/v1/"
                       "station/infrahoraire-6m")
KNMI_EDR_URL = ("https://api.dataplatform.knmi.nl/edr/v1/collections/"
                "observations/locations/{location}")

# (obs_time UTC, temp degC in tenths) — the parsers' common output row.
FeedObs = tuple[datetime, float]


def _parse_iso_utc(raw: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (Z or offset) to an aware UTC datetime."""

    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_fmi_wfs(xml_text: str) -> list[FeedObs]:
    """FMI open-data WFS 'simple' response (10-min t2m rows, tenths, UTC)."""

    import xml.etree.ElementTree as ET

    ns = {"BsWfs": "http://xml.fmi.fi/schema/wfs/2.0"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[FeedObs] = []
    for el in root.iter("{http://xml.fmi.fi/schema/wfs/2.0}BsWfsElement"):
        name = el.findtext("BsWfs:ParameterName", default="", namespaces=ns)
        if name != "t2m":
            continue
        t_raw = el.findtext("BsWfs:Time", default="", namespaces=ns)
        v_raw = el.findtext("BsWfs:ParameterValue", default="", namespaces=ns)
        dt = _parse_iso_utc(t_raw)
        try:
            val = float(v_raw)
        except (TypeError, ValueError):
            continue
        if dt is None or val != val:  # NaN guard
            continue
        out.append((dt, val))
    return out


def parse_jma_amedas(payload: dict, station: str, obs_time: datetime) -> list[FeedObs]:
    """One JMA AMeDAS map/{ts}.json snapshot: the station's temp if QC-clean.

    The map file carries every AMeDAS station for a single timestamp;
    ``temp`` is ``[value_degC, qc_flag]`` and only qc 0 is a clean ob.
    """

    entry = payload.get(station)
    if not isinstance(entry, dict):
        return []
    temp = entry.get("temp")
    if (not isinstance(temp, (list, tuple)) or len(temp) < 2
            or temp[0] is None or temp[1] != 0):
        return []
    try:
        return [(obs_time.astimezone(timezone.utc), float(temp[0]))]
    except (TypeError, ValueError):
        return []


def parse_nea_air_temp(payload: dict, station: str) -> list[FeedObs]:
    """NEA data.gov.sg v2 real-time air-temperature payload (1-min cadence)."""

    data = payload.get("data") or {}
    out: list[FeedObs] = []
    for block in data.get("readings") or []:
        dt = _parse_iso_utc(str(block.get("timestamp", "")))
        if dt is None:
            continue
        for row in block.get("data") or []:
            if row.get("stationId") != station:
                continue
            try:
                out.append((dt, float(row["value"])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def parse_mf_obs(payload: list, station: str) -> list[FeedObs]:
    """Météo-France DPObs infrahoraire-6m rows for one station (t in KELVIN)."""

    out: list[FeedObs] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict) or str(row.get("geo_id_insee")) != station:
            continue
        dt = _parse_iso_utc(str(row.get("validity_time", "")))
        t_k = row.get("t")
        if dt is None or t_k is None:
            continue
        try:
            out.append((dt, round(float(t_k) - 273.15, 1)))
        except (TypeError, ValueError):
            continue
    return out


def parse_knmi_edr(payload: dict, parameter: str = "ta") -> list[FeedObs]:
    """KNMI EDR CoverageJSON PointSeries: the ``ta`` (air temp degC) range.

    UNVERIFIED against a live payload until a KNMI_API_KEY is available — the
    shape follows the EDR/CoverageJSON spec and the KNMI Data Platform docs.
    """

    domain = payload.get("domain") or {}
    times = ((domain.get("axes") or {}).get("t") or {}).get("values") or []
    rng = (payload.get("ranges") or {}).get(parameter) or {}
    values = rng.get("values") or []
    out: list[FeedObs] = []
    for t_raw, val in zip(times, values):
        dt = _parse_iso_utc(str(t_raw))
        if dt is None or val is None:
            continue
        try:
            out.append((dt, float(val)))
        except (TypeError, ValueError):
            continue
    return out


def make_fmi_fetch(*, station_id: str, session=None, user_agent: str = _DEFAULT_UA,
                   timeout: float = 15.0, window_min: int = 40) -> Callable[[], Optional[str]]:
    """Fetch callable for FMI's 10-min observations (no key needed)."""

    sess = session or (requests.Session() if requests else None)

    def fetch() -> Optional[str]:  # pragma: no cover - network
        if sess is None:
            return None
        start = (datetime.now(timezone.utc) - timedelta(minutes=window_min))
        try:
            resp = sess.get(FMI_WFS_URL, params={
                "service": "WFS", "version": "2.0.0", "request": "getFeature",
                "storedquery_id": "fmi::observations::weather::simple",
                "fmisid": station_id, "parameters": "t2m",
                "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ")},
                headers={"User-Agent": user_agent}, timeout=timeout)
        except Exception:
            return None
        return resp.text if resp.status_code == 200 else None

    return fetch


def make_jma_fetch(*, session=None, user_agent: str = _DEFAULT_UA,
                   timeout: float = 15.0
                   ) -> Callable[[], Optional[tuple[datetime, Optional[dict]]]]:
    """Fetch callable for JMA AMeDAS (no key): latest_time.txt -> map JSON.

    Returns ``(obs_time_utc, map_payload)`` or None on failure. The map file
    (~1-2 MB, every AMeDAS station) is only downloaded when latest_time.txt
    ADVANCES; an unchanged stamp returns ``(obs_time, None)`` so a 60 s poll
    cadence costs one tiny text request, not gigabytes/day. The filename is
    stamped in JST exactly as latest_time.txt names it.
    """

    sess = session or (requests.Session() if requests else None)
    state: dict[str, Optional[str]] = {"stamp": None}

    def fetch() -> Optional[tuple[datetime, Optional[dict]]]:  # pragma: no cover - network
        if sess is None:
            return None
        try:
            r1 = sess.get(JMA_LATEST_TIME_URL, headers={"User-Agent": user_agent},
                          timeout=timeout)
            if r1.status_code != 200:
                return None
            latest = _parse_iso_utc(r1.text)
            if latest is None:
                return None
            # The filename uses the timestamp's own (JST) wall clock.
            local = datetime.fromisoformat(r1.text.strip())
            stamp = local.strftime("%Y%m%d%H%M%S")
            if stamp == state["stamp"]:
                return (latest, None)  # nothing new published yet
            r2 = sess.get(JMA_MAP_URL.format(stamp=stamp),
                          headers={"User-Agent": user_agent}, timeout=timeout)
            if r2.status_code != 200:
                return None
            payload = r2.json()
            if not isinstance(payload, dict):
                return None
            state["stamp"] = stamp
        except Exception:
            return None
        return (latest, payload)

    return fetch


def make_nea_fetch(*, session=None, user_agent: str = _DEFAULT_UA,
                   timeout: float = 15.0) -> Callable[[], Optional[dict]]:
    """Fetch callable for NEA/data.gov.sg v2 air temperature (no key)."""

    sess = session or (requests.Session() if requests else None)

    def fetch() -> Optional[dict]:  # pragma: no cover - network
        if sess is None:
            return None
        try:
            resp = sess.get(NEA_AIR_TEMP_URL, headers={"User-Agent": user_agent},
                            timeout=timeout)
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    return fetch


def make_meteofrance_fetch(*, api_key: str, station_id: str, session=None,
                           user_agent: str = _DEFAULT_UA,
                           timeout: float = 15.0) -> Callable[[], Optional[list]]:
    """Fetch callable for Météo-France's 6-min station observations (apikey)."""

    sess = session or (requests.Session() if requests else None)

    def fetch() -> Optional[list]:  # pragma: no cover - network
        if sess is None:
            return None
        try:
            resp = sess.get(MF_INFRAHORAIRE_URL,
                            params={"id_station": station_id, "format": "json"},
                            headers={"User-Agent": user_agent, "apikey": api_key},
                            timeout=timeout)
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except Exception:
            return None
        return payload if isinstance(payload, list) else None

    return fetch


def make_knmi_fetch(*, api_key: str, station_id: str, parameter: str = "ta",
                    session=None, user_agent: str = _DEFAULT_UA,
                    timeout: float = 15.0) -> Callable[[], Optional[dict]]:
    """Fetch callable for KNMI EDR 10-min observations (Authorization key).

    ``station_id`` may be the bare synop number ("06240"); it is expanded to
    the WIGOS location id the EDR collection uses. UNVERIFIED live until a
    KNMI_API_KEY is available.
    """

    sess = session or (requests.Session() if requests else None)
    location = station_id if "-" in station_id else f"0-20000-0-{station_id.lstrip('0') or '0'}"

    def fetch() -> Optional[dict]:  # pragma: no cover - network
        if sess is None:
            return None
        start = datetime.now(timezone.utc) - timedelta(minutes=60)
        try:
            resp = sess.get(KNMI_EDR_URL.format(location=location),
                            params={"parameter-name": parameter,
                                    "datetime": start.strftime("%Y-%m-%dT%H:%M:%SZ") + "/.."},
                            headers={"User-Agent": user_agent, "Authorization": api_key},
                            timeout=timeout)
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    return fetch


def build_feed_fetcher(city_cfg, config, *, session=None
                       ) -> Optional[Callable[[], Optional[list[FeedObs]]]]:
    """Compose fetch + parse for a city's forecast feed.

    Returns a callable yielding ``list[(obs_time_utc, temp_c)]`` (None on
    fetch failure), or None when the city has no feed / its API key is
    missing — the caller logs that once and the city stays METAR-only.
    """

    src = city_cfg.feed_source
    station = city_cfg.feed_station_id or ""
    if not src or not station:
        return None
    if src == "fmi":
        fetch = make_fmi_fetch(station_id=station, session=session)
        return lambda: (lambda t: None if t is None else parse_fmi_wfs(t))(fetch())
    if src == "jma":
        fetch = make_jma_fetch(session=session)

        def run_jma() -> Optional[list[FeedObs]]:
            got = fetch()
            if got is None:
                return None
            obs_time, payload = got
            if payload is None:
                return []  # stamp unchanged: fetched fine, nothing new
            return parse_jma_amedas(payload, station, obs_time)

        return run_jma
    if src == "nea":
        fetch = make_nea_fetch(session=session)
        return lambda: (lambda p: None if p is None else parse_nea_air_temp(p, station))(fetch())
    if src == "meteofrance":
        if not config.mf_api_key:
            return None
        fetch = make_meteofrance_fetch(api_key=config.mf_api_key, station_id=station,
                                       session=session)
        return lambda: (lambda p: None if p is None else parse_mf_obs(p, station))(fetch())
    if src == "knmi":
        if not config.knmi_api_key:
            return None
        fetch = make_knmi_fetch(api_key=config.knmi_api_key, station_id=station,
                                session=session)
        return lambda: (lambda p: None if p is None else parse_knmi_edr(p))(fetch())
    return None
