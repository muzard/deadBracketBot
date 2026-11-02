"""Shenzhen / Lau Fau Shan calibration: collectors + a WU-prediction shadow.

The Shenzhen market resolves on Weather Underground's table for HKO AWS station
45035 (Lau Fau Shan) — NOT a METAR: TWC resamples HKO sub-hourly data into
hourly rows. Three unknowns gate re-enabling the city (see
``docs/shenzhen_calibration_requirements.md``): **K1** — TWC's sample grid (which HKO
minutes end up in a row) + ingestion latency; **K2** — the 0.1 °C → integer
rounding rule (incl. x.5); **K3** — whether the daily "High Temp" is the max
over ALL ingested row versions or only each hour's final value. This module
collects the data that measures them, and runs a best-guess **prediction
shadow** — our estimate of what WU will publish, scored row-by-row against what
WU actually publishes — so the model's accuracy is measurable before any
capital touches it.

Data collection only: Shenzhen stays ``no_trade``; nothing here touches
strategy, risk, or the order path. Parsers / dedupe / prediction are pure and
unit-tested; the network callables are injected from ``feeds_http`` (repo
convention: all network code lives there). Files land under ``data/<city>/``
(NOT per-day — multi-day joins are the whole point):

- ``hko_1min.jsonl``          one row per new HKO observation minute
- ``twc_rows.jsonl``          one row per new TWC row VERSION (intra-hour revisions)
- ``twc_daily.jsonl``         the previous HKT day's final table (00:30 + 09:00 HKT)
- ``predictions.jsonl``       our best-guess WU row value + daily max, as formed
- ``prediction_eval.jsonl``   prediction vs actual, one row per scoreable publish
- ``lock_ahead.jsonl``        L2 shadow: in-window locks + their scores against
  the row the hour finalized as (re-enable plan gate 3)
- ``collector_health.jsonl``  cumulative poll/failure counters (the "<1% missed
  polls" acceptance criterion needs a denominator) + per-collector stall flags

Offline analysis: ``python -m dead_bracket.tools.lfs_calibrate``.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional

HKT = timezone(timedelta(hours=8), name="HKT")

# The TWC observation fields that form a row's VERSION identity. Temperature is
# often flat for hours; pressure (0.1 hPa) and rh are the change detectors that
# reveal a row being re-sampled even when temp doesn't move.
TWC_VERSION_FIELDS = (
    "temp",
    "dewPt",
    "rh",
    "pressure",
    "wdir",
    "wspd",
    "gust",
    "precip_total",
)
# What the daily capture keeps per final row (the full TWC row is ~40 mostly-
# marine fields; keep the version tuple + the summary max/min).
TWC_DAILY_FIELDS = ("valid_time_gmt",) + TWC_VERSION_FIELDS + ("max_temp", "min_temp")

_HKO_DT_RE = re.compile(r"\d{12}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def append_jsonl(path: str, rec: dict) -> None:
    """Append one JSON record to a jsonl file, creating parent dirs as needed."""

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def read_jsonl(path: str) -> list[dict]:
    """Read a jsonl file tolerantly (missing file -> [], bad lines skipped)."""

    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Component 1 — HKO 1-minute feed (pure parser + collector)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Hko1MinReading:
    """One station row of the HKO latest-1-minute CSV.

    ``temp_c`` keeps the full 0.1 °C precision — this feed is allowed ONLY as
    the Shenzhen resolution source's own upstream (HKO), logged for calibration;
    it must never feed a MetarFeed / running max (decimal = non-resolution).
    """

    obs_time_raw: str  # as published, e.g. "202607072150" (HKT, yyyyMMddHHmm)
    obs_time_utc: datetime
    temp_c: float


def parse_hko_1min_csv(text: Optional[str], station: str) -> Optional[Hko1MinReading]:
    """Extract the named station's reading from the latest-1-minute CSV.

    Schema verified live 2026-07-07: header ``Date time,Automatic Weather
    Station,Air Temperature(degree Celsius)``, then one row per station like
    ``202607072150,Lau Fau Shan,24.9``. Datetimes are HKT (UTC+8); temps are
    0.1 °C; a station with no data carries ``N/A``. Handles a UTF-8 BOM, quoted
    fields, and station-name whitespace/case variants. Returns None when the
    text is empty/malformed or the station row is absent or N/A.
    """

    if not text:
        return None
    want = station.strip().casefold()
    try:
        rows = csv.reader(io.StringIO(text.lstrip("\ufeff")))
        for row in rows:
            if len(row) < 3:
                continue
            dt_raw, name, temp_raw = (c.strip() for c in row[:3])
            if not _HKO_DT_RE.fullmatch(dt_raw):
                continue  # header or junk line
            if name.casefold() != want:
                continue
            try:
                temp = float(temp_raw)
            except ValueError:
                return None  # station present but reading is N/A
            local = datetime.strptime(dt_raw, "%Y%m%d%H%M").replace(tzinfo=HKT)
            return Hko1MinReading(
                obs_time_raw=dt_raw,
                obs_time_utc=local.astimezone(timezone.utc),
                temp_c=temp,
            )
    except (csv.Error, ValueError):
        return None
    return None


class Hko1MinCollector:
    """Polls the HKO 1-minute CSV and appends new Lau-Fau-Shan minutes.

    Dedupes on ``obs_time_raw`` (writes only new observation minutes), flags
    gaps (a jump of more than one minute between consecutive stored readings),
    and is restart-safe: the last stored minute is re-seeded from the file so a
    restart never duplicates it. ``fetch`` is injected (returns the CSV text or
    None) so tests run with canned strings and no network.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], Optional[str]],
        station: str,
        path: str,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._fetch = fetch
        self._station = station
        self._path = path
        self._clock = clock
        self.polls = 0
        self.failures = 0  # fetch/parse failures (the missed-poll numerator)
        self.new_records = 0
        self._last_raw: Optional[str] = None
        self._last_utc: Optional[datetime] = None
        for rec in read_jsonl(path):  # restart-safe dedupe seed
            raw = rec.get("obs_time_raw")
            ts = rec.get("obs_time_utc")
            if raw:
                self._last_raw = str(raw)
            if ts:
                try:
                    self._last_utc = datetime.fromisoformat(str(ts))
                except ValueError:
                    pass

    def poll_once(self) -> Optional[Hko1MinReading]:
        """One poll. Returns the reading only when it is a NEW observation minute."""

        self.polls += 1
        text = self._fetch()
        if text is None:
            self.failures += 1
            return None
        reading = parse_hko_1min_csv(text, self._station)
        if reading is None:
            self.failures += 1
            return None
        if reading.obs_time_raw == self._last_raw:
            return None  # same minute re-served -> not a miss, just no news
        rec = {
            "fetch_ts": self._clock().isoformat(),
            "obs_time_utc": reading.obs_time_utc.isoformat(),
            "obs_time_raw": reading.obs_time_raw,
            "temp_c": reading.temp_c,
        }
        # Gap flag: >1 observation minute skipped since the last stored reading
        # (the feed republishes per minute; the analysis measures coverage).
        if self._last_utc is not None:
            gap_min = (reading.obs_time_utc - self._last_utc).total_seconds() / 60.0
            if gap_min > 1.5:
                rec["gap_min"] = round(gap_min, 1)
        append_jsonl(self._path, rec)
        self._last_raw = reading.obs_time_raw
        self._last_utc = reading.obs_time_utc
        self.new_records += 1
        return reading


# ---------------------------------------------------------------------------
# Component 2 — TWC row-version collector
# ---------------------------------------------------------------------------
def twc_version_tuple(ob: dict) -> tuple:
    """A TWC observation's version identity (see ``TWC_VERSION_FIELDS``)."""

    return tuple(ob.get(f) for f in TWC_VERSION_FIELDS)


class TwcRowCollector:
    """Polls the TWC timeseries and appends every new ROW VERSION.

    A "row" is one ``valid_time_gmt`` (the hourly slot WU displays); TWC revises
    it intra-hour as it re-samples HKO. We append a record only when the version
    tuple differs from the last-seen version for that slot — that history is
    exactly what K1/K3 need. Also watches ``metadata.transaction_id`` across
    polls: a repeated id means the response came from a stale cache (the fetch
    is cache-busted, so this counter should stay ~0). Restart-safe: last-seen
    versions are re-seeded from the file.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], Optional[dict]],
        path: str,
        freshness_max_sec: float = 10800.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._fetch = fetch
        self._path = path
        self._freshness_max_sec = freshness_max_sec
        self._clock = clock
        self.polls = 0
        self.failures = 0
        self.new_records = 0
        self.stale_responses = 0  # repeated transaction_id OR rejected-stale payload
        self._last_txn: Optional[str] = None
        self._last: dict[int, tuple] = {}
        for rec in read_jsonl(path):  # restart-safe version seed
            vt = rec.get("valid_time_gmt")
            if vt is None:
                continue
            self._last[int(vt)] = tuple(rec.get(f) for f in TWC_VERSION_FIELDS)

    def poll_once(self) -> list[dict]:
        """One poll. Returns the list of NEW row-version records appended."""

        self.polls += 1
        payload = self._fetch()
        if payload is None:
            self.failures += 1
            return []
        txn = (payload.get("metadata") or {}).get("transaction_id")
        if txn is not None and txn == self._last_txn:
            self.stale_responses += 1  # cache-bust not working; analysis will see it
        self._last_txn = txn
        # Freshness rejection (re-enable plan, L1): the API has served responses
        # DAYS old (observed live 2026-07-08). A payload whose newest row is
        # older than ``freshness_max_sec`` is rejected outright — its rows must
        # never enter twc_rows.jsonl, which downstream (TwcResolverFetcher)
        # treats as resolver truth. The next poll's rotated cache key retries.
        obs_list = payload.get("observations") or []
        vts = []
        for ob in obs_list:
            try:
                vts.append(int(ob.get("valid_time_gmt")))
            except (TypeError, ValueError):
                continue
        if vts and (self._clock().timestamp() - max(vts)) > self._freshness_max_sec:
            self.stale_responses += 1
            return []
        now_iso = self._clock().isoformat()
        new: list[dict] = []
        for ob in payload.get("observations") or []:
            vt = ob.get("valid_time_gmt")
            if vt is None:
                continue
            vt = int(vt)
            tup = twc_version_tuple(ob)
            if self._last.get(vt) == tup:
                continue  # same version -> nothing new for this slot
            self._last[vt] = tup
            rec = {
                "first_seen_ts": now_iso,
                "valid_time_gmt": vt,
                **{f: ob.get(f) for f in TWC_VERSION_FIELDS},
            }
            append_jsonl(self._path, rec)
            new.append(rec)
        self.new_records += len(new)
        return new


# ---------------------------------------------------------------------------
# Component 3 — daily summary capture (K3)
# ---------------------------------------------------------------------------
class TwcDailyCollector:
    """Captures the previous HKT day's final TWC table, twice a day.

    Fires once per configured HKT slot (default 00:30 — first full table — and
    09:00 — revision-window guard) per day: at/after the slot time it fetches
    ``historical.json`` for the previous HKT day and appends one record holding
    every final row (version fields + any ``max_temp``/``min_temp`` summary
    fields — verified live: past days may carry no max_temp at all, so the
    analysis falls back to max(temp)). A failed fetch is retried on the next
    poll; the slot is marked done only on success. Restart-safe via the file.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[str], Optional[dict]],
        path: str,
        capture_hkt: tuple[str, ...] = ("00:30", "09:00"),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._fetch = fetch  # takes the target day as "yyyyMMdd"
        self._path = path
        self._clock = clock
        self._slots: list[tuple[str, dtime]] = []
        for s in capture_hkt:
            hh, mm = s.strip().split(":")
            self._slots.append((s.strip(), dtime(int(hh), int(mm))))
        self.polls = 0
        self.failures = 0
        self.new_records = 0
        self._done: set[tuple[str, str]] = set()  # (hkt_date, slot)
        for rec in read_jsonl(path):  # restart-safe: don't re-capture done slots
            d, s = rec.get("hkt_date"), rec.get("slot")
            if d and s:
                self._done.add((str(d), str(s)))

    def poll_once(self) -> list[dict]:
        """Capture any due-and-not-yet-done slot. Returns new records."""

        now_hkt = self._clock().astimezone(HKT)
        prev_day = now_hkt.date() - timedelta(days=1)
        out: list[dict] = []
        for slot_name, slot_time in self._slots:
            key = (prev_day.isoformat(), slot_name)
            if key in self._done or now_hkt.time() < slot_time:
                continue
            self.polls += 1
            payload = self._fetch(prev_day.strftime("%Y%m%d"))
            if payload is None:
                self.failures += 1
                continue  # retried next poll (slot not marked done)
            obs = payload.get("observations") or []
            rec = {
                "fetch_ts": self._clock().isoformat(),
                "hkt_date": prev_day.isoformat(),
                "slot": slot_name,
                "n_rows": len(obs),
                "observations": [{k: o.get(k) for k in TWC_DAILY_FIELDS} for o in obs],
            }
            append_jsonl(self._path, rec)
            self._done.add(key)
            self.new_records += 1
            out.append(rec)
        return out


# ---------------------------------------------------------------------------
# Prediction shadow — our best guess of what WU will publish, scored later
# ---------------------------------------------------------------------------
def predict_wu_integer(temp_c: float, rounding: str = "half_up") -> int:
    """Best-guess K2: the integer WU will print for a 0.1 °C HKO reading.

    ``half_up`` (default assumption under test), ``half_even`` (banker's), and
    ``floor`` (truncation) are the candidate rules the calibration
    discriminates between; the predictor runs one of them so its hit rate is a
    direct measurement of that rule.
    """

    if rounding == "half_up":
        return math.floor(temp_c + 0.5)
    if rounding == "half_even":
        floor = math.floor(temp_c)
        frac = temp_c - floor
        if abs(frac - 0.5) < 1e-9:
            return floor if floor % 2 == 0 else floor + 1
        return math.floor(temp_c + 0.5)
    if rounding == "floor":
        return math.floor(temp_c)
    raise ValueError(f"unknown rounding rule {rounding!r}")


class WuPredictor:
    """Best-guess model of the WU/TWC publication, scored against reality.

    Assumptions (each IS one of the calibration unknowns, deliberately — the
    scorecard measures how wrong they are):

    - **K2**: WU integer = ``predict_wu_integer(latest 0.1 °C reading)``.
    - **K1**: the row for hour H tracks the most recent 1-min reading (zero
      grid offset, zero latency).
    - **K3**: the daily High counts ALL ingested samples, so our predicted
      daily max is the running max of the rounded 1-min readings (HKT day).

    Two outputs: ``predictions.jsonl`` — the estimate stream, one row whenever
    the predicted current-hour value or daily max changes; and
    ``prediction_eval.jsonl`` — one row per scoreable WU publication (each new
    TWC row version, and each captured daily summary) with predicted vs actual.

    Restart-safe: state is rebuilt by replaying ``hko_1min.jsonl`` (pass
    ``seed_hko_path``), so a restart mid-day neither loses the daily max nor
    re-emits an unchanged prediction row.
    """

    def __init__(
        self,
        *,
        pred_path: str,
        eval_path: str,
        rounding: str = "half_up",
        seed_hko_path: Optional[str] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._pred_path = pred_path
        self._eval_path = eval_path
        self._rounding = rounding
        self._clock = clock
        self._readings: list[Hko1MinReading] = []  # pruned to the last 48 h
        self._daily_max: dict[str, int] = {}  # HKT date -> predicted High
        self._daily_count: dict[str, int] = {}  # HKT date -> readings absorbed
        self._last_pred: Optional[tuple] = None
        self.row_evals = 0
        self.row_hits = 0
        self.daily_evals = 0
        self.daily_hits = 0
        if seed_hko_path:
            for rec in read_jsonl(seed_hko_path):
                try:
                    reading = Hko1MinReading(
                        obs_time_raw=str(rec["obs_time_raw"]),
                        obs_time_utc=datetime.fromisoformat(str(rec["obs_time_utc"])),
                        temp_c=float(rec["temp_c"]),
                    )
                except (KeyError, ValueError):
                    continue
                pred_row, daily, _day, hour_epoch = self._absorb(reading)
                # Seed the change-dedupe too, so a restart doesn't re-emit an
                # unchanged prediction row for the reading it already logged.
                self._last_pred = (hour_epoch, pred_row, daily)

    # -- state ---------------------------------------------------------------
    def _absorb(self, reading: Hko1MinReading) -> tuple[int, int, str, int]:
        """Fold a reading into state; return (pred_row, pred_daily, hkt_date, hour_epoch)."""

        self._readings.append(reading)
        cutoff = reading.obs_time_utc - timedelta(hours=48)
        if self._readings and self._readings[0].obs_time_utc < cutoff:
            self._readings = [r for r in self._readings if r.obs_time_utc >= cutoff]
        day = reading.obs_time_utc.astimezone(HKT).date().isoformat()
        pred_row = predict_wu_integer(reading.temp_c, self._rounding)
        daily = max(self._daily_max.get(day, pred_row), pred_row)
        self._daily_max[day] = daily
        self._daily_count[day] = self._daily_count.get(day, 0) + 1
        hour = reading.obs_time_utc.replace(minute=0, second=0, microsecond=0)
        hour_epoch = int(hour.timestamp())
        return pred_row, daily, day, hour_epoch

    def predicted_daily_max(self, hkt_date: str) -> Optional[int]:
        return self._daily_max.get(hkt_date)

    # -- live hooks ------------------------------------------------------------
    def on_reading(self, reading: Hko1MinReading) -> Optional[dict]:
        """New 1-min reading -> update the estimate; log it when it changes."""

        pred_row, daily, day, hour_epoch = self._absorb(reading)
        key = (hour_epoch, pred_row, daily)
        if key == self._last_pred:
            return None
        self._last_pred = key
        rec = {
            "ts": self._clock().isoformat(),
            "obs_time_utc": reading.obs_time_utc.isoformat(),
            "basis_temp_c": reading.temp_c,
            # our estimate of the WU row this reading belongs to (hour slot,
            # keyed like TWC's valid_time_gmt for trivial offline joins)...
            "valid_time_gmt": hour_epoch,
            "pred_row_temp": pred_row,
            # ...and of the day's WU "High Temp" under the K3 all-samples guess.
            "hkt_date": day,
            "pred_daily_max": daily,
        }
        append_jsonl(self._pred_path, rec)
        return rec

    def on_twc_row(self, row: dict) -> Optional[dict]:
        """Score one observed TWC row version against our estimate.

        The estimate for a row is what we would have predicted from the 1-min
        feed at the moment the version appeared: the newest reading inside the
        row's own hour (fallback: newest reading at/before first-seen, within
        2 h). Unscoreable rows (no temp, no basis reading) are skipped, not
        guessed.
        """

        actual = row.get("temp")
        vt = row.get("valid_time_gmt")
        if actual is None or vt is None:
            return None
        try:
            first_seen = datetime.fromisoformat(str(row.get("first_seen_ts")))
        except (TypeError, ValueError):
            first_seen = self._clock()
        vt = int(vt)
        candidates = [r for r in self._readings if r.obs_time_utc <= first_seen]
        in_hour = [
            r for r in candidates if vt <= r.obs_time_utc.timestamp() < vt + 3600
        ]
        pool = in_hour or [
            r for r in candidates if (first_seen - r.obs_time_utc) <= timedelta(hours=2)
        ]
        if not pool:
            return None
        basis = max(pool, key=lambda r: r.obs_time_utc)
        pred = predict_wu_integer(basis.temp_c, self._rounding)
        hit = pred == int(actual)
        self.row_evals += 1
        self.row_hits += int(hit)
        rec = {
            "ts": self._clock().isoformat(),
            "kind": "row",
            "valid_time_gmt": vt,
            "actual_temp": actual,
            "pred_temp": pred,
            "hit": hit,
            "basis_obs_time_utc": basis.obs_time_utc.isoformat(),
            "basis_temp_c": basis.temp_c,
            "basis_in_hour": bool(in_hour),
        }
        append_jsonl(self._eval_path, rec)
        return rec

    def on_daily(self, daily_rec: dict) -> Optional[dict]:
        """Score the captured WU daily table against our predicted daily max.

        The actual High is the max over any non-null ``max_temp`` summary
        fields, else the max final-row ``temp`` (verified live: past days can
        carry no max_temp at all). ``basis_minutes`` says how many 1-min
        readings backed our estimate — a partial-coverage day (collector
        started mid-day) scores low for a boring reason, so the analysis can
        filter on it.
        """

        day = daily_rec.get("hkt_date")
        pred = self._daily_max.get(str(day))
        obs = daily_rec.get("observations") or []
        max_temps = [o["max_temp"] for o in obs if o.get("max_temp") is not None]
        temps = [o["temp"] for o in obs if o.get("temp") is not None]
        actual = max(max_temps) if max_temps else (max(temps) if temps else None)
        if pred is None or actual is None:
            return None
        hit = pred == int(actual)
        self.daily_evals += 1
        self.daily_hits += int(hit)
        rec = {
            "ts": self._clock().isoformat(),
            "kind": "daily_max",
            "hkt_date": day,
            "actual_temp": actual,
            "pred_temp": pred,
            "hit": hit,
            "actual_from": "max_temp" if max_temps else "max(final rows)",
            "basis_minutes": self._daily_count.get(str(day), 0),
        }
        append_jsonl(self._eval_path, rec)
        return rec


# ---------------------------------------------------------------------------
# L2 lock-ahead shadow — re-enable plan gate 3 (>=50 events, >=98% hold rate)
# ---------------------------------------------------------------------------
class LockAheadShadow:
    """Shadow-scores the L2 hypothesis: the HKO sample landing in [h:35, h:55]
    is (to rounding, with margin) what the TWC row for hour h finalizes as.

    A LOCK fires when an in-window 1-min reading clears an integer with margin:
    ``locked_min_row = floor(temp_c - margin)`` is the highest K for which the
    claim "the final row will print >= K" is made (trading translation: every
    bracket B with B+1 <= K is dead). One lock per slot per K level — a later
    in-window reading that raises K fires again. A SCORE lands once the hour
    has closed plus a grace period, against the latest observed row version for
    that slot: ``hold = actual >= locked_min_row``. Locks with no observed row
    are logged unscoreable (hold=null) — they count against coverage, not the
    hold rate. Restart-safe: pending locks and last-seen row temps are re-seeded
    from the shadow file and ``twc_rows.jsonl``.

    Shadow only: writes ``lock_ahead.jsonl``, touches nothing in the order path.
    The gate ("L2 live after >=50 events shadow-scored at >=98% hold") is a
    human decision on this file's contents.
    """

    def __init__(
        self,
        *,
        path: str,
        margin_c: float = 0.3,
        window_min: tuple[int, int] = (35, 55),
        grace_sec: float = 300.0,
        seed_twc_path: Optional[str] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._path = path
        self._margin = margin_c
        self._window = window_min
        self._grace = grace_sec
        self._clock = clock
        self.locks = 0
        self.scored = 0
        self.holds = 0
        self.unscoreable = 0
        self._best: dict[int, int] = {}  # slot -> highest locked K
        self._pending: dict[int, list[dict]] = {}  # slot -> lock recs awaiting score
        self._row_temp: dict[int, int] = {}  # slot -> latest observed row temp
        if seed_twc_path:
            for rec in read_jsonl(seed_twc_path):
                self.on_twc_row(rec)
        for rec in read_jsonl(path):  # replay own file: locks minus scores
            vt = rec.get("valid_time_gmt")
            k = rec.get("locked_min_row")
            if vt is None or k is None:
                continue
            vt, k = int(vt), int(k)
            if rec.get("kind") == "lock":
                self._best[vt] = max(self._best.get(vt, k), k)
                self._pending.setdefault(vt, []).append(rec)
            elif rec.get("kind") == "score":
                self._pending[vt] = [
                    p
                    for p in self._pending.get(vt, [])
                    if int(p["locked_min_row"]) != k
                ]
                if not self._pending.get(vt):
                    self._pending.pop(vt, None)

    def on_reading(self, reading: Hko1MinReading) -> Optional[dict]:
        """A new 1-min reading -> a lock record when it raises the slot's K."""

        minute = reading.obs_time_utc.minute  # HKT offset is whole hours
        if not (self._window[0] <= minute <= self._window[1]):
            return None
        k = math.floor(reading.temp_c - self._margin)
        hour = reading.obs_time_utc.replace(minute=0, second=0, microsecond=0)
        vt = int(hour.timestamp())
        if k <= self._best.get(vt, -(10**6)):
            return None
        self._best[vt] = k
        rec = {
            "ts": self._clock().isoformat(),
            "kind": "lock",
            "valid_time_gmt": vt,
            "obs_time_utc": reading.obs_time_utc.isoformat(),
            "basis_temp_c": reading.temp_c,
            "locked_min_row": k,
            "pred_row": predict_wu_integer(reading.temp_c),
            "margin_c": self._margin,
        }
        append_jsonl(self._path, rec)
        self.locks += 1
        self._pending.setdefault(vt, []).append(rec)
        return rec

    def on_twc_row(self, rec: dict) -> None:
        """Track the latest observed row temp per slot (the scoring target)."""

        vt, temp = rec.get("valid_time_gmt"), rec.get("temp")
        if vt is None or temp is None:
            return
        try:
            self._row_temp[int(vt)] = int(temp)
        except (TypeError, ValueError):
            return

    def finalize_due(self, now: Optional[datetime] = None) -> list[dict]:
        """Score every pending lock whose hour closed ``grace_sec`` ago."""

        now = now or self._clock()
        out: list[dict] = []
        for vt in sorted(self._pending):
            if now.timestamp() < vt + 3600 + self._grace:
                continue
            actual = self._row_temp.get(vt)
            for lock in self._pending.pop(vt):
                hold = (
                    None
                    if actual is None
                    else bool(actual >= int(lock["locked_min_row"]))
                )
                if hold is None:
                    self.unscoreable += 1
                else:
                    self.scored += 1
                    self.holds += int(hold)
                rec = {
                    "ts": now.isoformat(),
                    "kind": "score",
                    "valid_time_gmt": vt,
                    "locked_min_row": int(lock["locked_min_row"]),
                    "basis_temp_c": lock.get("basis_temp_c"),
                    "actual_row_temp": actual,
                    "hold": hold,
                }
                append_jsonl(self._path, rec)
                out.append(rec)
        return out


# ---------------------------------------------------------------------------
# Worker — one daemon thread per calibrated city, fail-soft, unattended-safe
# ---------------------------------------------------------------------------
class LfsCalibrationWorker:
    """Runs the collectors + prediction shadow on their cadences in one thread.

    Isolated from the trading CityWorker: it must run unattended for days, so
    every poll is wrapped — a failing collector logs a ``calibration_error``
    event and skips the tick, never crashes the thread, and NEVER writes to
    stdout (status goes to the reporter's event/notice streams). Collector
    health counters are flushed to ``collector_health.jsonl`` periodically so
    the missed-poll acceptance criterion is measurable offline.
    """

    def __init__(
        self,
        *,
        city: str,
        reporter,
        hko: Optional[Hko1MinCollector] = None,
        twc_rows: Optional[TwcRowCollector] = None,
        twc_daily: Optional[TwcDailyCollector] = None,
        predictor: Optional[WuPredictor] = None,
        lock_ahead: Optional[LockAheadShadow] = None,
        health_path: Optional[str] = None,
        hko_poll_sec: float = 60.0,
        twc_poll_sec: float = 60.0,
        health_every_sec: float = 600.0,
        hko_stall_sec: float = 1500.0,
        twc_stall_sec: float = 4500.0,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.city = city
        self._reporter = reporter
        self._hko = hko
        self._twc_rows = twc_rows
        self._twc_daily = twc_daily
        self._predictor = predictor
        self._lock_ahead = lock_ahead
        self._health_path = health_path
        self._hko_poll = hko_poll_sec
        self._twc_poll = twc_poll_sec
        self._health_every = health_every_sec
        self._clock = clock
        self._mono = monotonic
        self._last_hko: Optional[float] = None
        self._last_twc: Optional[float] = None
        self._last_health: Optional[float] = None
        # Staleness alarms: health counters alone are blind to SOURCE-side gaps
        # (2026-07-08: a 90-min hko_1min outage with ``failures: 0`` — the CSV
        # kept serving, just nothing new). Track the last time each collector
        # produced a NEW record and alarm when that goes quiet too long. The
        # clocks arm at start() so a fresh boot isn't an instant alarm.
        self._stall_sec = {"hko_1min": hko_stall_sec, "twc_rows": twc_stall_sec}
        self._last_new: dict[str, Optional[float]] = {
            "hko_1min": None,
            "twc_rows": None,
        }
        self._stalled: dict[str, bool] = {"hko_1min": False, "twc_rows": False}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _due(self, last: Optional[float], every: float, now: float) -> bool:
        return last is None or (now - last) >= every

    def _guard(self, collector: str, fn) -> object:
        """Run one poll fail-soft: an exception is an event, never a crash."""

        try:
            return fn()
        except Exception as exc:
            self._reporter.event(
                self.city, "calibration_error", collector=collector, error=repr(exc)
            )
            return None

    def tick(self, now: Optional[float] = None) -> None:
        """One scheduling pass (extracted from ``run`` so tests need no thread)."""

        now = self._mono() if now is None else now
        if self._hko is not None and self._due(self._last_hko, self._hko_poll, now):
            self._last_hko = now
            reading = self._guard("hko_1min", self._hko.poll_once)
            if reading is not None:
                self._mark_alive("hko_1min", now)
                if self._predictor is not None:
                    self._guard(
                        "predictor", lambda: self._predictor.on_reading(reading)
                    )
                if self._lock_ahead is not None:
                    self._guard(
                        "lock_ahead", lambda: self._lock_ahead.on_reading(reading)
                    )
            self._check_stall("hko_1min", now)
        if self._due(self._last_twc, self._twc_poll, now):
            self._last_twc = now
            if self._twc_rows is not None:
                new_rows = self._guard("twc_rows", self._twc_rows.poll_once) or []
                if new_rows:
                    self._mark_alive("twc_rows", now)
                for row in new_rows:
                    if self._predictor is not None:
                        self._guard(
                            "predictor", lambda r=row: self._predictor.on_twc_row(r)
                        )
                    if self._lock_ahead is not None:
                        self._guard(
                            "lock_ahead", lambda r=row: self._lock_ahead.on_twc_row(r)
                        )
                if self._lock_ahead is not None:
                    self._guard("lock_ahead", self._lock_ahead.finalize_due)
                self._check_stall("twc_rows", now)
            if self._twc_daily is not None:
                captured = self._guard("twc_daily", self._twc_daily.poll_once) or []
                for rec in captured:
                    if self._predictor is not None:
                        self._guard(
                            "predictor", lambda r=rec: self._predictor.on_daily(r)
                        )
                    self._reporter.notice(
                        self.city,
                        f"calibration: captured WU daily table "
                        f"{rec['hkt_date']} @{rec['slot']} HKT ({rec['n_rows']} rows)",
                    )
        if self._due(self._last_health, self._health_every, now):
            self._last_health = now
            self._flush_health()

    def _mark_alive(self, collector: str, now: float) -> None:
        self._last_new[collector] = now
        if self._stalled[collector]:
            self._stalled[collector] = False
            self._reporter.event(self.city, "staleness_recovered", collector=collector)
            self._reporter.notice(
                self.city, f"calibration: {collector} producing again"
            )

    def _check_stall(self, collector: str, now: float) -> None:
        last = self._last_new[collector]
        if last is None:  # armed at start(); no baseline yet
            self._last_new[collector] = now
            return
        quiet = now - last
        if quiet > self._stall_sec[collector] and not self._stalled[collector]:
            self._stalled[collector] = True
            self._reporter.event(
                self.city,
                "staleness_alarm",
                collector=collector,
                quiet_sec=round(quiet),
            )
            self._reporter.notice(
                self.city,
                f"calibration ALARM: {collector} has produced no new "
                f"record for {round(quiet / 60)} min (source-side gap? dead key?)",
            )

    def _flush_health(self) -> None:
        """Cumulative counters -> collector_health.jsonl (+ events log)."""

        if not self._health_path:
            return
        ts = self._clock().isoformat()
        rows = []
        if self._hko is not None:
            rows.append(
                {
                    "collector": "hko_1min",
                    "polls": self._hko.polls,
                    "failures": self._hko.failures,
                    "new_records": self._hko.new_records,
                    "stalled": self._stalled["hko_1min"],
                }
            )
        if self._twc_rows is not None:
            rows.append(
                {
                    "collector": "twc_rows",
                    "polls": self._twc_rows.polls,
                    "failures": self._twc_rows.failures,
                    "new_records": self._twc_rows.new_records,
                    "stale_responses": self._twc_rows.stale_responses,
                    "stalled": self._stalled["twc_rows"],
                }
            )
        if self._lock_ahead is not None:
            rows.append(
                {
                    "collector": "lock_ahead",
                    "locks": self._lock_ahead.locks,
                    "scored": self._lock_ahead.scored,
                    "holds": self._lock_ahead.holds,
                    "unscoreable": self._lock_ahead.unscoreable,
                }
            )
        if self._twc_daily is not None:
            rows.append(
                {
                    "collector": "twc_daily",
                    "polls": self._twc_daily.polls,
                    "failures": self._twc_daily.failures,
                    "new_records": self._twc_daily.new_records,
                }
            )
        if self._predictor is not None:
            rows.append(
                {
                    "collector": "predictor",
                    "row_evals": self._predictor.row_evals,
                    "row_hits": self._predictor.row_hits,
                    "daily_evals": self._predictor.daily_evals,
                    "daily_hits": self._predictor.daily_hits,
                }
            )
        for row in rows:
            append_jsonl(self._health_path, {"ts": ts, **row})
        self._reporter.event(self.city, "calibration_health", collectors=rows)

    # -- thread lifecycle ----------------------------------------------------
    def run(self) -> None:  # pragma: no cover - loop; tick() carries the logic
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # belt-and-braces: tick already guards
                self._reporter.event(
                    self.city, "calibration_error", collector="worker", error=repr(exc)
                )
            self._stop.wait(1.0)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, daemon=True, name=f"lfs-cal-{self.city}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
