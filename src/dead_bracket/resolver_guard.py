"""Resolver cross-check: our source vs the table the market actually resolves on.

**Why this module exists.** On 2026-07-19 the live wallet was wiped on Seoul.
RKSI's routine METAR printed ``27/22``; Weather Underground's row for that same
instant read 26 °C (its RKSI pipeline carries tenths — 79 °F = 26.1 °C — where
the METAR encodes whole degrees). Our running max went to 27, the 26 °C bracket
was marked dead, and the bot spent the whole bankroll on a NO that settled to
$0 because WU's daily High never left 26. Station identity was verified;
*pipeline* identity was not. The same failure class had already been found on
Shenzhen (WU resolves it off HKO Lau Fau Shan, not the ZGSZ METAR) — it was
simply never generalised to the other WU cities.

**The rule this module enforces.** A city may not trade unless our resolution
source and the resolver's own published table have agreed on **every paired
observation across the trailing 24 hours**. Anything else fails closed:

``ok``          every pair in the window matched, coverage is sufficient, and
                both sides are fresh -> entries allowed.
``unverified``  not enough (or not fresh enough) resolver data to prove
                agreement — no entries, but collection continues and the state
                clears itself as soon as coverage is met. This is also the
                state of a city with no resolver location configured at all.
``divergent``   at least one paired observation (or the day's running max)
                disagreed. **Sticky**: it survives ticks, daily rollovers and
                restarts, and only a human clears it
                (``python -m dead_bracket.tools.resolver_clear <city>``).

Divergence is not a glitch to ride out — it means our source and the resolver
are different instruments, so every "certain" dead bracket is a coin flip. The
only safe response is to stop trading that city and keep recording, which is
exactly what the sticky state does.

Layout mirrors the rest of the repo: everything here is pure and unit-testable
(the fetch callable is injected), and the HTTP lives in ``feeds_http.py``. Rows
land in ``data/<city>/wu_rows.jsonl`` and the sticky blocks in
``data/_global/resolver_blocks.json``.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

# Guard states (see the module docstring).
OK = "ok"
UNVERIFIED = "unverified"
DIVERGENT = "divergent"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Mismatch:
    """One paired observation where our source and the resolver disagreed."""

    epoch: int
    ours: int
    theirs: int

    @property
    def iso(self) -> str:
        return datetime.fromtimestamp(self.epoch, timezone.utc).isoformat()

    def describe(self) -> str:
        return f"{self.iso[:16]}Z ours={self.ours}C resolver={self.theirs}C"


@dataclass(frozen=True)
class Verdict:
    """The guard's answer for one city at one instant."""

    state: str
    reason: str
    pairs: int = 0
    span_hours: float = 0.0
    mismatches: tuple[Mismatch, ...] = ()
    station: Optional[str] = None
    ours_max: Optional[int] = None
    theirs_max: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.state == OK

    @property
    def sticky(self) -> bool:
        return self.state == DIVERGENT


class ResolverBlockStore:
    """Disk-backed record of sticky divergence blocks (survives restarts).

    One small JSON file for the whole process:
    ``{"<city>": {"reason": ..., "since": ..., "detail": ...}}``. A block is
    written the moment divergence is detected and is only removed by
    :meth:`clear` (the manual-resolution step). Reads tolerate a missing or
    corrupt file by treating it as empty — a broken file must not silently
    *unblock* anything, so callers re-detect and re-write divergence on the
    next evaluation anyway.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._blocks: dict[str, dict] = {}
        self._load()

    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._blocks = {
                str(k): v for k, v in data.items() if isinstance(v, dict)
            }

    def _save(self) -> None:
        tmp = f"{self._path}.tmp"
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._blocks, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self._path)

    def blocked(self, city: str) -> Optional[dict]:
        with self._lock:
            rec = self._blocks.get(city)
            return dict(rec) if rec else None

    def all_blocks(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._blocks.items()}

    def block(self, city: str, *, reason: str, detail: str = "",
              now: Optional[datetime] = None) -> bool:
        """Record a sticky block. Returns True if it is newly blocked."""

        with self._lock:
            if city in self._blocks:
                return False
            self._blocks[city] = {
                "reason": reason,
                "detail": detail,
                "since": (now or _utcnow()).isoformat(),
            }
            self._save()
            return True

    def clear(self, city: str) -> bool:
        """Manual resolution: drop the block. Returns True if one was removed."""

        with self._lock:
            if city not in self._blocks:
                return False
            del self._blocks[city]
            self._save()
            return True


class ResolverCrossCheck:
    """Per-city 24-hour agreement check between our source and the resolver.

    ``note_ours`` receives every observation that drives this city's running max
    (the METAR, or whatever resolution feed the city is wired to); ``note_theirs``
    receives the resolver's published rows from the poller. ``evaluate`` pairs
    them by exact observation timestamp over the trailing ``window_hours`` and
    returns a :class:`Verdict`.

    Both sides arrive from different threads (worker vs poller), so all state is
    behind a lock. Memory is bounded by pruning anything older than twice the
    window.

    Pairing is by exact epoch because both sides publish on the same
    :00/:30-style grid. Off-grid observations (SPECIs) simply never pair, so
    they cannot create phantom mismatches — but they *do* feed the running-max
    comparison, which is deliberate: a SPECI spike that the resolver's table
    never records is precisely the kind of false-dead this guard exists to catch.
    """

    def __init__(
        self,
        city: str,
        *,
        tz: Optional[ZoneInfo] = None,
        window_hours: float = 24.0,
        min_pairs: int = 20,
        min_span_hours: float = 23.0,
        max_gap_hours: float = 3.0,
        max_lag_hours: float = 3.0,
        settle_lag_min: float = 90.0,
        tolerance_c: int = 0,
        configured: bool = True,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.city = city
        self._tz = tz
        self._window = float(window_hours) * 3600.0
        self._min_pairs = int(min_pairs)
        self._min_span = float(min_span_hours) * 3600.0
        self._max_gap = float(max_gap_hours) * 3600.0
        self._max_lag = float(max_lag_hours) * 3600.0
        self._settle_lag = float(settle_lag_min) * 60.0
        self._tol = int(tolerance_c)
        # A city with no resolver location configured (e.g. Istanbul's LTFM,
        # which the resolver API serves no rows for) can never be verified, so
        # it stays permanently ``unverified`` -> never trades. Fail closed.
        self._configured = bool(configured)
        self._clock = clock
        self._lock = threading.Lock()
        self._ours: dict[int, int] = {}
        self._theirs: dict[int, int] = {}
        self._station: Optional[str] = None
        self._sticky: Optional[Verdict] = None

    # -- ingestion ---------------------------------------------------------
    def note_ours(self, obs_time: datetime, temp_c: int) -> None:
        """Record one observation from our resolution source (idempotent)."""

        with self._lock:
            self._ours[int(obs_time.timestamp())] = int(temp_c)
            self._prune()

    def seed_ours(self, records: Iterable[tuple[datetime, int]]) -> int:
        """Bulk-load our side (a restart seeding from ``metar.jsonl``).

        Without this a restart would start from zero history and the city would
        sit ``unverified`` for a full day; the log already holds the trailing
        observations, so coverage is available immediately.
        """

        n = 0
        with self._lock:
            for obs_time, temp_c in records:
                self._ours[int(obs_time.timestamp())] = int(temp_c)
                n += 1
            self._prune()
        return n

    def note_theirs(self, rows: Iterable[tuple[int, int]],
                    *, station: Optional[str] = None) -> None:
        """Record resolver rows as ``(epoch_seconds, temp_c)`` pairs."""

        with self._lock:
            for epoch, temp_c in rows:
                self._theirs[int(epoch)] = int(temp_c)
            if station:
                self._station = station
            self._prune()

    def _prune(self) -> None:
        """Caller holds the lock. Bound memory to twice the comparison window."""

        cutoff = self._clock().timestamp() - 2 * self._window
        for store in (self._ours, self._theirs):
            for epoch in [e for e in store if e < cutoff]:
                del store[epoch]

    # -- state -------------------------------------------------------------
    @property
    def station(self) -> Optional[str]:
        """The station name the resolver's own rows carry (identity evidence).

        For Shenzhen this reads "Lau Fau Shan" rather than the ZGSZ airport —
        the resolver telling us, in its own payload, which instrument it uses.
        """

        with self._lock:
            return self._station

    @property
    def counts(self) -> tuple[int, int]:
        with self._lock:
            return len(self._ours), len(self._theirs)

    def adopt_block(self, reason: str) -> None:
        """Re-arm a sticky divergence loaded from the block store at startup.

        Divergence outlives the process: a restart must come back blocked, not
        forget and re-buy. ``main`` calls this for every city in the store.
        """

        with self._lock:
            self._sticky = Verdict(state=DIVERGENT, reason=reason,
                                   station=self._station)

    def clear(self) -> bool:
        """Drop the in-memory sticky state (manual resolution). Returns cleared."""

        with self._lock:
            had = self._sticky is not None
            self._sticky = None
            return had

    # -- the check ---------------------------------------------------------
    def evaluate(self, now: Optional[datetime] = None) -> Verdict:
        """Compare the two sources over the trailing window. Never raises."""

        now = now or self._clock()
        with self._lock:
            if self._sticky is not None:
                return self._sticky
            if not self._configured:
                return Verdict(
                    state=UNVERIFIED,
                    reason="no resolver source configured for this city",
                    station=self._station,
                )

            now_ts = now.timestamp()
            cutoff = now_ts - self._window
            pairs: list[tuple[int, int, int]] = []
            for epoch in sorted(self._ours):
                if epoch < cutoff or epoch > now_ts:
                    continue
                theirs = self._theirs.get(epoch)
                if theirs is not None:
                    pairs.append((epoch, self._ours[epoch], theirs))

            # 1. Any disagreement at all -> sticky divergence. Checked FIRST so a
            #    mismatch is reported even when coverage is otherwise thin: a
            #    proven divergence is information, thin coverage is only absence
            #    of it.
            mismatches = tuple(
                Mismatch(e, o, t) for e, o, t in pairs if abs(o - t) > self._tol
            )
            if mismatches:
                shown = "; ".join(m.describe() for m in mismatches[:3])
                extra = "" if len(mismatches) <= 3 else f" (+{len(mismatches) - 3} more)"
                self._sticky = Verdict(
                    state=DIVERGENT,
                    reason=f"source disagrees with resolver on {len(mismatches)} "
                           f"of {len(pairs)} paired obs in {self._window / 3600:.0f}h: "
                           f"{shown}{extra}",
                    pairs=len(pairs),
                    mismatches=mismatches,
                    station=self._station,
                )
                return self._sticky

            # 2. Resolver freshness: agreement proven only on stale history is
            #    not proof about now (a dead poller/API key must not read as OK).
            if not self._theirs:
                return Verdict(state=UNVERIFIED, reason="no resolver rows yet",
                               pairs=len(pairs), station=self._station)
            newest_theirs = max(self._theirs)
            lag = now_ts - newest_theirs
            if lag > self._max_lag:
                return Verdict(
                    state=UNVERIFIED,
                    reason=f"resolver rows stale ({lag / 3600:.1f}h old)",
                    pairs=len(pairs),
                    station=self._station,
                )

            # 3. Coverage: enough pairs, spanning the required window, with no
            #    long unpaired hole in the middle.
            if len(pairs) < self._min_pairs:
                return Verdict(
                    state=UNVERIFIED,
                    reason=f"only {len(pairs)} matched obs in the last "
                           f"{self._window / 3600:.0f}h (need {self._min_pairs})",
                    pairs=len(pairs),
                    station=self._station,
                )
            span = pairs[-1][0] - pairs[0][0]
            if span < self._min_span:
                return Verdict(
                    state=UNVERIFIED,
                    reason=f"matched obs span only {span / 3600:.1f}h "
                           f"(need {self._min_span / 3600:.0f}h)",
                    pairs=len(pairs),
                    span_hours=span / 3600.0,
                    station=self._station,
                )
            gap = max(
                (b[0] - a[0] for a, b in zip(pairs, pairs[1:])), default=0
            )
            if gap > self._max_gap:
                return Verdict(
                    state=UNVERIFIED,
                    reason=f"{gap / 3600:.1f}h hole in the matched history "
                           f"(max {self._max_gap / 3600:.0f}h)",
                    pairs=len(pairs),
                    span_hours=span / 3600.0,
                    station=self._station,
                )

            # 4. Running-max agreement for the current local day, over the
            #    settled portion only (the resolver's table publishes ~an hour
            #    behind, so the newest obs are legitimately absent from it).
            #    This is the check that speaks directly to the trade: the
            #    running max is what marks brackets dead.
            settled = now_ts - self._settle_lag
            ours_max = self._day_max(self._ours, now, settled)
            theirs_max = self._day_max(self._theirs, now, settled)
            if ours_max is not None and theirs_max is not None and ours_max != theirs_max:
                self._sticky = Verdict(
                    state=DIVERGENT,
                    reason=f"running max disagrees for the local day: "
                           f"ours={ours_max}C resolver={theirs_max}C",
                    pairs=len(pairs),
                    span_hours=span / 3600.0,
                    station=self._station,
                    ours_max=ours_max,
                    theirs_max=theirs_max,
                )
                return self._sticky

            return Verdict(
                state=OK,
                reason=f"{len(pairs)} obs matched over {span / 3600:.1f}h",
                pairs=len(pairs),
                span_hours=span / 3600.0,
                station=self._station,
                ours_max=ours_max,
                theirs_max=theirs_max,
            )

    def confirms_max(self, running_max: Optional[int],
                     now: Optional[datetime] = None) -> bool:
        """Has the resolver's OWN table reached ``running_max`` today?

        The 24-hour agreement check can only compare observations the resolver
        has already published, and it publishes 25-65 minutes late (measured
        2026-08-18). That leaves a residual window: the freshest print — the one
        that marks a new bracket dead, and the only one worth trading — has no
        resolver row to disagree with yet. On 2026-07-19 the window was ~11
        minutes wide and the guard would still have blocked in time, but that is
        margin, not safety.

        This closes it completely: a bracket is only treated as dead when the
        resolver's own rows for the local day have themselves reached the
        running max. Costs the whole speed edge (the dead-NO opportunity is
        usually gone an hour later), so it is opt-in via
        ``config.resolver_confirm_dead`` — run it in paper to measure what the
        edge is actually worth against what it risks.
        """

        if running_max is None:
            return True
        now = now or self._clock()
        with self._lock:
            today = self._local_date(now)
            vals = [
                t for e, t in self._theirs.items()
                if self._local_date(datetime.fromtimestamp(e, timezone.utc)) == today
            ]
            return bool(vals) and max(vals) >= running_max

    def _day_max(self, store: dict[int, int], now: datetime,
                 settled_ts: float) -> Optional[int]:
        """Max temp in ``store`` for the city's current local day, counting only
        observations old enough for the resolver to have published. Caller holds
        the lock."""

        today = self._local_date(now)
        vals = [
            t for e, t in store.items()
            if e <= settled_ts and self._local_date(
                datetime.fromtimestamp(e, timezone.utc)) == today
        ]
        return max(vals) if vals else None

    def _local_date(self, dt: datetime) -> date:
        return (dt.astimezone(self._tz) if self._tz else dt).date()


class ResolverRowCollector:
    """Polls the resolver's published table and feeds the guard.

    ``fetch`` is injected (``feeds_http.make_twc_range_fetch``) and takes the
    two ``yyyyMMdd`` day tokens bounding the window; the resolver interprets
    them in the *station's* local frame, which is the same day boundary the
    market resolves on. New rows are appended to ``wu_rows.jsonl`` (one record
    per new row version) and pushed into ``ResolverCrossCheck.note_theirs``.

    Restart-safe: previously seen rows are re-read from the file and replayed
    into the guard, so a restart does not lose the 24 h of resolver history the
    coverage rule needs.
    """

    def __init__(
        self,
        *,
        city: str,
        guard: ResolverCrossCheck,
        fetch: Callable[[str, str], Optional[dict]],
        path: str,
        tz: Optional[ZoneInfo] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.city = city
        self._guard = guard
        self._fetch = fetch
        self._path = path
        self._tz = tz
        self._clock = clock
        self.polls = 0
        self.failures = 0
        self.new_rows = 0
        self._seen: dict[int, int] = {}
        self._seed()

    def _seed(self) -> None:
        rows: list[tuple[int, int]] = []
        station = None
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        epoch = int(rec["valid_time_gmt"])
                        temp = int(rec["temp"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    self._seen[epoch] = temp
                    rows.append((epoch, temp))
                    station = rec.get("obs_name") or station
        except OSError:
            return
        if rows:
            self._guard.note_theirs(rows, station=station)

    def _day_tokens(self, now: datetime) -> tuple[str, str]:
        local = now.astimezone(self._tz) if self._tz else now
        start = (local - timedelta(days=1)).strftime("%Y%m%d")
        return start, local.strftime("%Y%m%d")

    def poll_once(self) -> list[dict]:
        """One poll. Returns the NEW row records appended (may be empty)."""

        self.polls += 1
        now = self._clock()
        start, end = self._day_tokens(now)
        try:
            payload = self._fetch(start, end)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            self.failures += 1
            return []
        observations = payload.get("observations") or []
        # An empty (but well-formed) response counts as a failure: a location id
        # the resolver serves no rows for — Istanbul's LTFM — would otherwise
        # look perfectly healthy in the counters while never verifying anything.
        if not isinstance(observations, list) or not observations:
            self.failures += 1
            return []

        fresh: list[dict] = []
        pushed: list[tuple[int, int]] = []
        station = None
        for ob in observations:
            if not isinstance(ob, dict):
                continue
            try:
                epoch = int(ob["valid_time_gmt"])
                temp = int(ob["temp"])
            except (KeyError, TypeError, ValueError):
                continue  # a row with no temperature carries no information
            station = ob.get("obs_name") or station
            pushed.append((epoch, temp))
            if self._seen.get(epoch) == temp:
                continue
            self._seen[epoch] = temp
            rec = {
                "ts": now.isoformat(),
                "valid_time_gmt": epoch,
                "temp": temp,
                "obs_name": ob.get("obs_name"),
                "obs_id": ob.get("obs_id"),
                "max_temp": ob.get("max_temp"),
            }
            fresh.append(rec)

        if pushed:
            self._guard.note_theirs(pushed, station=station)
        if fresh:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                for rec in fresh:
                    fh.write(json.dumps(rec) + "\n")
            self.new_rows += len(fresh)
        return fresh


class ResolverPollerWorker:
    """Runs one city's ResolverRowCollector on its cadence in a daemon thread.

    Same discipline as the other pollers: every poll is guarded (a failing
    fetch/parse logs an event and skips the tick, never crashing the thread),
    health counters flush periodically to the event stream, and nothing ever
    reaches stdout. The cadence is slow by design (default 10 min) — the check
    is a 24 h agreement, and the resolver's own CDN caches for 5 minutes.
    """

    def __init__(self, *, city: str, collector: ResolverRowCollector, reporter,
                 poll_sec: float = 600.0, health_every_sec: float = 3600.0,
                 monotonic: Callable[[], float] = None) -> None:
        import time as _time

        self.city = city
        self.collector = collector
        self._reporter = reporter
        self._poll_sec = float(poll_sec)
        self._health_every = float(health_every_sec)
        self._mono = monotonic or _time.monotonic
        self._last_poll: Optional[float] = None
        self._last_health: Optional[float] = None
        self._stop = threading.Event()
        self._thread = None

    def _due(self, last: Optional[float], every: float, now: float) -> bool:
        return last is None or (now - last) >= every

    def tick(self, now: Optional[float] = None) -> None:
        """One scheduling pass (extracted from ``run`` so tests need no thread)."""

        now = self._mono() if now is None else now
        if self._due(self._last_poll, self._poll_sec, now):
            self._last_poll = now
            try:
                self.collector.poll_once()
            except Exception as exc:
                self._reporter.event(self.city, "resolver_poll_error", error=repr(exc))
        if self._due(self._last_health, self._health_every, now):
            self._last_health = now
            self._reporter.event(self.city, "resolver_health",
                                 polls=self.collector.polls,
                                 failures=self.collector.failures,
                                 new_rows=self.collector.new_rows)

    def run(self) -> None:  # pragma: no cover - loop; tick() carries the logic
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # belt-and-braces: tick already guards
                self._reporter.event(self.city, "resolver_poll_error", error=repr(exc))
            self._stop.wait(1.0)

    def start(self) -> None:  # pragma: no cover - thread lifecycle
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name=f"resolver-{self.city}")
        self._thread.start()

    def stop(self) -> None:  # pragma: no cover - thread lifecycle
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def seed_records_from_metar_log(path: str, *, hours: float = 48.0,
                                now: Optional[datetime] = None,
                                include_speci: bool = True):
    """Yield ``(obs_time, temp_c)`` from a ``metar.jsonl`` for the last ``hours``.

    Used to seed :meth:`ResolverCrossCheck.seed_ours` on startup. Mirrors
    ``MetarFeed._seed_from_log``'s SPECI policy: on routine-only cities a logged
    SPECI never fed the running max, so it must not feed the comparison either.
    """

    now = now or _utcnow()
    cutoff = now.timestamp() - hours * 3600.0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    obs_time = datetime.fromisoformat(rec["obs_time"])
                    temp_c = int(rec["temp_c"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not include_speci and str(rec.get("raw", "")).strip().startswith("SPECI"):
                    continue
                if obs_time.timestamp() >= cutoff:
                    yield obs_time, temp_c
    except OSError:
        return
