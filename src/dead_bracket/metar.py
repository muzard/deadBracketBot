"""METAR resolution feed (spec §6, §9).

This is the *only* weather input. We tap the airport METAR directly, parse the
whole-degC temperature and observation time, and maintain a monotonic
non-decreasing running max. There is no Meteo-France / 6-minute feed anywhere.

The feed is split into a pure parser (``parse_metar``) and a stateful tracker
(``RunningMax``) so the strategy-critical logic is testable with no network, and
a thin ``MetarFeed`` that wires a fetch callable to the tracker with restart-safe
seeding from an append-only log.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

# Day-of-month + HHMM + Z, e.g. "031530Z".
_TIME_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
# Temp/dewpoint group, e.g. "23/10", "M03/M05", "07/M01". Temp is whole degC,
# "M" prefix means negative. The resolving METAR is ALWAYS whole integers.
_TEMP_RE = re.compile(r"\b(M?\d{2})/(M?\d{2})\b")
# Intra-Celsius (decimal) temperature, e.g. "21.3" or "M03.4". The market does
# NOT resolve on the decimal/6-minute feed; a decimal reading is therefore a
# non-resolution source and must never set the running max (it could mark a
# bracket dead that the resolving whole-degree report does not). We deliberately
# do NOT parse the precise "Tnnnn" remark group for the same reason — that is
# tenths-precision and not resolution-grade.
_DECIMAL_TEMP_RE = re.compile(r"\bM?\d{1,2}\.\d")


def _signed(token: str) -> int:
    return -int(token[1:]) if token.startswith("M") else int(token)


def _round_decimal(token: str) -> int:
    neg = token.startswith("M")
    value = float(token[1:] if neg else token)
    rounded = int(round(value))
    return -rounded if neg else rounded


@dataclass(frozen=True)
class MetarObservation:
    """A parsed METAR: station, observation time, whole-degC temp, COR flag.

    ``resolution_grade`` is False when the reading carries intra-Celsius (decimal)
    precision — i.e. it came from a non-resolution source (the 6-minute feed) and
    must not be folded into the running max.
    """

    icao: str
    obs_time: datetime
    temp_c: int
    is_correction: bool
    raw: str
    resolution_grade: bool = True
    # When the source (AWC) ingested the report; None if unknown. obs->receipt is
    # the publish delay; receipt->now is our own (receipt) delay.
    receipt_time: Optional[datetime] = None
    # True for a SPECI (off-cycle special report). The market resolves on the
    # routine METAR cycle, so SPECIs must not set the running max by default.
    is_speci: bool = False


def parse_metar(raw: str, *, now: Optional[datetime] = None) -> Optional[MetarObservation]:
    """Parse a raw METAR string into an observation, or None if unparseable.

    ``now`` (UTC) is used only to resolve the day-of-month timestamp into a full
    datetime; it defaults to the current UTC time. Temperature resolution is
    whole degC per the resolution feed.
    """

    if not raw:
        return None
    text = raw.strip()
    tokens = text.split()
    if not tokens:
        return None

    is_speci = tokens[0] == "SPECI"
    icao = tokens[0] if re.fullmatch(r"[A-Z]{4}", tokens[0]) else ""
    is_cor = "COR" in tokens

    time_match = _TIME_RE.search(text)
    if not time_match:
        return None
    day, hour, minute = (int(g) for g in time_match.groups())

    # A decimal temperature => non-resolution (6-minute) source. Flag it so the
    # feed can reject it; if a whole-degree group is *also* present we still flag
    # non-resolution to be safe (resolving reports never carry decimals).
    has_decimal = bool(_DECIMAL_TEMP_RE.search(text))
    temp_match = _TEMP_RE.search(text)
    if temp_match:
        temp_c = _signed(temp_match.group(1))
        resolution_grade = not has_decimal
    elif has_decimal:
        temp_c = _round_decimal(_DECIMAL_TEMP_RE.search(text).group(0))
        resolution_grade = False
    else:
        return None

    ref = now or datetime.now(timezone.utc)
    year, month = ref.year, ref.month
    # Handle month rollover: an observation day greater than today's day refers
    # to the previous month.
    if day > ref.day:
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
    try:
        obs_time = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None

    return MetarObservation(icao=icao, obs_time=obs_time, temp_c=temp_c,
                            is_correction=is_cor, raw=text, resolution_grade=resolution_grade,
                            is_speci=is_speci)


class RunningMax:
    """Monotonic running-max tracker with a downward-revision guard (spec §9).

    Running max never decreases *within a day*. If a corrected (COR) observation
    would lower the max basis (i.e. the highest reading was revised down), we do
    NOT lower the running max and do NOT un-dead any acted bracket, but we set
    ``revision_detected`` so the worker can halt new entries and flag it.

    The "day" is the local calendar day in ``tz`` (the daily-high market resolves
    in the city's local time). At local midnight the running max, the supported
    set, and the revision flag all reset, and observations from a different local
    day are ignored — so yesterday's high can never carry into today. When ``tz``
    is None, day handling is disabled (used by unit tests that don't need it).
    """

    def __init__(self, tz: Optional[ZoneInfo] = None) -> None:
        self._tz = tz
        self._by_time: dict[datetime, int] = {}
        self._running_max: Optional[int] = None
        self._last_obs_time: Optional[datetime] = None
        self.revision_detected: bool = False
        self._trading_day: Optional[date] = None

    @property
    def value(self) -> Optional[int]:
        return self._running_max

    @property
    def last_obs_time(self) -> Optional[datetime]:
        return self._last_obs_time

    @property
    def trading_day(self) -> Optional[date]:
        return self._trading_day

    def _supported_max(self) -> Optional[int]:
        return max(self._by_time.values()) if self._by_time else None

    def _local_date(self, dt: datetime) -> date:
        assert self._tz is not None
        return dt.astimezone(self._tz).date()

    def _reset(self) -> None:
        self._by_time.clear()
        self._running_max = None
        self._last_obs_time = None
        self.revision_detected = False

    def roll(self, now: datetime) -> None:
        """Reset state if the local calendar day has advanced (midnight reset)."""

        if self._tz is None:
            return
        day = self._local_date(now)
        if self._trading_day is None:
            self._trading_day = day
        elif day != self._trading_day:
            self._reset()
            self._trading_day = day

    def update(self, obs: MetarObservation, *, now: Optional[datetime] = None) -> Optional[int]:
        """Incorporate an observation; return the (possibly unchanged) max.

        ``now`` (wall clock, UTC) drives the local-midnight reset; pass it
        whenever day handling matters. An observation whose local day differs
        from the current trading day is ignored.
        """

        if self._tz is not None and now is not None:
            self.roll(now)
            if self._trading_day is not None and self._local_date(obs.obs_time) != self._trading_day:
                return self._running_max

        prior = self._by_time.get(obs.obs_time)
        self._by_time[obs.obs_time] = obs.temp_c
        if self._last_obs_time is None or obs.obs_time >= self._last_obs_time:
            self._last_obs_time = obs.obs_time

        supported = self._supported_max()
        if self._running_max is None:
            self._running_max = supported
            return self._running_max

        # New high: bump the monotonic max.
        if supported is not None and supported > self._running_max:
            self._running_max = supported

        # Downward-revision guard: a correction that lowers a prior reading such
        # that the supported max drops below the running max we already acted on.
        if (
            obs.is_correction
            and prior is not None
            and obs.temp_c < prior
            and supported is not None
            and supported < self._running_max
        ):
            self.revision_detected = True

        return self._running_max


class MetarFeed:
    """Thread-safe METAR feed: a fetch callable + RunningMax + restart-safe log.

    ``fetch_fn`` takes an ICAO and returns the raw METAR string (or None). It is
    injected so tests can supply fixtures and so the network layer stays out of
    the strategy-critical path.
    """

    def __init__(
        self,
        icao: str,
        *,
        fetch_fn: Callable[[str], Optional[str]],
        log_path: Optional[str] = None,
        tz: Optional[ZoneInfo] = None,
        receipt_fn: Optional[Callable[[str], Optional[datetime]]] = None,
        include_speci: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.icao = icao
        self._fetch_fn = fetch_fn
        self._receipt_fn = receipt_fn
        self._log_path = log_path
        self._tz = tz
        self._include_speci = include_speci
        self._clock = clock
        self._tracker = RunningMax(tz)
        self._lock = threading.Lock()
        self._last_update_wall: Optional[datetime] = None
        self._seen_raw: set[str] = set()
        self.speci_rejected: int = 0  # off-cycle SPECIs not folded (non-resolution)
        # Identity (obs_time, temp, cor) of the last folded observation; lets a
        # restarted worker skip re-announcing a report already in the log.
        self._last_obs_key: Optional[tuple] = None
        self.foreign_rejected: int = 0  # readings from another station (isolation)
        self.non_resolution_rejected: int = 0  # decimal/6-min readings (spec §6)
        # A batched fetcher accepts a freshness hint as a 2nd arg; a plain
        # single-station fetch (and test fixtures) takes only the ICAO.
        self._fetch_takes_max_age = self._detect_max_age_arg(fetch_fn)
        if log_path:
            self._seed_from_log(log_path)

    @property
    def last_obs_key(self) -> Optional[tuple]:
        with self._lock:
            return self._last_obs_key

    @staticmethod
    def _norm_raw(raw: str) -> str:
        """Normalise a raw line for dedup: drop a leading METAR/SPECI keyword so
        the same observation from tgftp (``LFPB ...``) and the JSON feed
        (``METAR LFPB ...``) is recognised as one."""

        s = raw.strip()
        for kw in ("METAR ", "SPECI "):
            if s.startswith(kw):
                return s[len(kw):]
        return s

    @staticmethod
    def _detect_max_age_arg(fetch_fn) -> bool:
        try:
            params = inspect.signature(fetch_fn).parameters
        except (ValueError, TypeError):
            return False
        if any(p.kind == p.VAR_POSITIONAL for p in params.values()):
            return True
        return len([p for p in params.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 2

    # -- state accessors ---------------------------------------------------
    @property
    def running_max(self) -> Optional[int]:
        with self._lock:
            # Roll on read so a query after local midnight reflects the reset
            # even if polling stalled around the boundary.
            self._tracker.roll(self._clock())
            return self._tracker.value

    @property
    def revision_detected(self) -> bool:
        with self._lock:
            self._tracker.roll(self._clock())
            return self._tracker.revision_detected

    @property
    def last_obs_time(self) -> Optional[datetime]:
        with self._lock:
            return self._tracker.last_obs_time

    def is_stale(self, *, stale_min: int, now: Optional[datetime] = None) -> bool:
        """True if no fresh observation arrived within ``stale_min`` minutes."""

        ref = now or self._clock()
        with self._lock:
            last = self._last_update_wall
        if last is None:
            return True
        return (ref - last).total_seconds() > stale_min * 60

    # -- polling -----------------------------------------------------------
    def poll_once(self, max_age: Optional[float] = None) -> Optional[MetarObservation]:
        """Fetch once, parse, and update state. Returns the parsed obs or None.

        ``max_age`` is a freshness hint (seconds) for a shared/batched fetcher:
        during a hot window the worker passes a small value so the network is
        polled aggressively; otherwise the normal cadence applies.
        """

        raw = self._fetch_fn(self.icao, max_age) if self._fetch_takes_max_age else self._fetch_fn(self.icao)
        now = self._clock()
        if not raw:
            with self._lock:
                self._tracker.roll(now)  # honour midnight reset even with no data
            return None
        obs = parse_metar(raw, now=now)
        if obs is None:
            return None
        if self._receipt_fn is not None:
            obs = replace(obs, receipt_time=self._receipt_fn(self.icao))
        # Station isolation: a reading whose ICAO is not this feed's station must
        # never be folded in, so one city's METAR can never cross into another.
        if obs.icao and obs.icao != self.icao:
            with self._lock:
                self.foreign_rejected += 1
            return None
        # Resolution-grade only: a decimal (intra-Celsius) reading is the 6-minute
        # / non-resolution feed and must never set the running max (spec §6). It
        # also does NOT refresh freshness — if only such data arrives we go stale.
        if not obs.resolution_grade:
            with self._lock:
                self.non_resolution_rejected += 1
                self._tracker.roll(now)
            return None
        # SPECIs are off-cycle special reports; the market resolves on the routine
        # METAR cycle, so a SPECI must not set the running max (a SPECI spike
        # could otherwise mark a still-live bracket dead -> a losing buy).
        if obs.is_speci and not self._include_speci:
            with self._lock:
                self.speci_rejected += 1
                self._tracker.roll(now)
            return None
        with self._lock:
            self._last_update_wall = now
            self._tracker.roll(now)
            # Always record wall-clock freshness; only fold a new observation
            # into the tracker and log. Dedup on the normalised raw so the same
            # report from tgftp and the JSON feed counts once.
            norm = self._norm_raw(raw)
            if norm in self._seen_raw:
                return obs
            self._seen_raw.add(norm)
            self._tracker.update(obs, now=now)
            self._last_obs_key = (obs.obs_time, obs.temp_c, obs.is_correction)
        self._append_log(obs)
        return obs

    # -- restart safety ----------------------------------------------------
    def _append_log(self, obs: MetarObservation) -> None:
        if not self._log_path:
            return
        os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
        rec = {
            "obs_time": obs.obs_time.isoformat(),
            "temp_c": obs.temp_c,
            "is_correction": obs.is_correction,
            "raw": obs.raw,
        }
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _seed_from_log(self, log_path: str) -> None:
        """Rebuild running max + dedup set from a prior run's log (spec §6)."""

        if not os.path.exists(log_path):
            return
        now = self._clock()
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    obs = MetarObservation(
                        icao=self.icao,
                        obs_time=datetime.fromisoformat(rec["obs_time"]),
                        temp_c=int(rec["temp_c"]),
                        is_correction=bool(rec.get("is_correction", False)),
                        raw=rec.get("raw", ""),
                    )
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                # Pass ``now`` so only the current local day's observations seed
                # the running max — a restart never carries yesterday's high in.
                self._tracker.update(obs, now=now)
                if obs.raw:
                    self._seen_raw.add(self._norm_raw(obs.raw))
                # Remember the last logged observation so a restarted worker does
                # not re-announce a report it already recorded (which showed up
                # as an inflated "+my" latency on every restart).
                self._last_obs_key = (obs.obs_time, obs.temp_c, obs.is_correction)
