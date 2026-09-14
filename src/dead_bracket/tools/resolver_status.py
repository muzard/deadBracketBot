"""Offline report: does each city's source agree with the market's resolver?

Reads what the running process has already written to disk — the city's METAR
log and ``wu_rows.jsonl`` — and re-runs the same comparison
``resolver_guard.ResolverCrossCheck`` runs in-process, so it answers the
operator question "which cities are actually allowed to trade right now, and
why not?" without touching the network or the bot.

    python -m dead_bracket.tools.resolver_status
    python -m dead_bracket.tools.resolver_status --city seoul --verbose
    python -m dead_bracket.tools.resolver_status --at 2026-08-22T12:02Z

Exit code is 1 if any city is in sticky DIVERGENT state, so it can gate a
deploy/enable script.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..config import Config, select_cities
from ..resolver_guard import (
    DIVERGENT,
    OK,
    ResolverBlockStore,
    ResolverCrossCheck,
    seed_records_from_metar_log,
)

_METAR_LOGS = ("metar_resolver.jsonl", "metar_twc.jsonl", "metar_hko.jsonl",
               "metar.jsonl")


def _parse_at(raw: str) -> datetime:
    """Parse a --at instant, defaulting a bare timestamp to UTC."""

    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_wu_rows(path: str, as_of: datetime | None = None
                  ) -> tuple[list[tuple[int, int]], str | None]:
    """Resolver rows from disk, optionally only those already *seen* by ``as_of``.

    Each record carries ``ts``, the moment the poller first read it. Honouring
    that is what makes ``--at`` a faithful replay rather than hindsight: the
    resolver publishes up to hours late, so a row keyed inside the window may
    not have existed yet at the instant being replayed — which is precisely the
    confusion that produced August's false divergence blocks.
    """

    import json

    rows: list[tuple[int, int]] = []
    station = None
    cutoff = as_of.timestamp() if as_of else None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if cutoff is not None:
                        seen = _parse_at(str(rec["ts"])).timestamp()
                        if seen > cutoff:
                            continue
                    rows.append((int(rec["valid_time_gmt"]), int(rec["temp"])))
                    station = rec.get("obs_name") or station
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return rows, station


def _configured(city_cfg) -> bool:
    """Can this city be cross-checked at all? Synoptic needs only the station."""

    if city_cfg.resolver_provider == "synoptic":
        return bool(city_cfg.metar_icao)
    return bool(city_cfg.resolver_location)


def resolver_rows_file(city_cfg) -> str:
    """Row log for this city's resolver provider (they must not be mixed)."""

    return ("resolver_rows.jsonl" if city_cfg.resolver_provider == "synoptic"
            else "wu_rows.jsonl")


def build_guard(config: Config, city_cfg, *, now: datetime,
                as_of: datetime | None = None,
                seed_history: bool = False) -> ResolverCrossCheck:
    """Rebuild one city's guard from the on-disk logs."""

    try:
        tz = ZoneInfo(city_cfg.tz)
    except Exception:
        tz = None
    guard = ResolverCrossCheck(
        city_cfg.id,
        tz=tz,
        window_hours=config.resolver_window_hours,
        min_pairs=config.resolver_min_pairs,
        min_reach_hours=config.resolver_min_reach_hours,
        max_gap_hours=config.resolver_max_gap_hours,
        max_lag_hours=config.resolver_max_lag_hours,
        settle_lag_min=config.resolver_settle_lag_min,
        min_day_obs=config.resolver_min_day_obs,
        tolerance_c=config.resolver_tolerance_c,
        configured=_configured(city_cfg),
        self_sourced=(city_cfg.resolution_source == "resolver"
                      and _configured(city_cfg)),
        clock=lambda: now,
    )
    city_dir = os.path.join(config.data_dir, city_cfg.id)
    for name in _METAR_LOGS:
        path = os.path.join(city_dir, name)
        if os.path.exists(path):
            guard.seed_ours(seed_records_from_metar_log(
                path, hours=config.resolver_window_hours * 2, now=now,
                include_speci=city_cfg.include_speci))
            break
    if seed_history and city_cfg.resolution_source == "metar":
        # Match what the running process does at startup, so this report and the
        # bot cannot disagree about whether a city is verified. Off by default:
        # the tool is otherwise offline, and a replay (--at) must not be given
        # observations from after the instant being replayed.
        from ..feeds_http import fetch_metar_history

        try:
            guard.seed_ours(fetch_metar_history(
                city_cfg.metar_icao,
                hours=int(config.resolver_window_hours * 2),
                include_speci=city_cfg.include_speci))
        except Exception:
            pass
    rows, station = _load_wu_rows(os.path.join(city_dir, resolver_rows_file(city_cfg)),
                                  as_of=as_of)
    if rows:
        guard.note_theirs(rows, station=station)
    return guard


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", help="only this city")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="list every mismatched observation")
    ap.add_argument("--seed-history", action="store_true",
                    help="also pull the station's trailing METAR from the "
                         "network, as the running bot does at startup, so this "
                         "report matches the live guard instead of only what "
                         "has been written to disk. Ignored with --at.")
    ap.add_argument("--at", metavar="ISO8601",
                    help="replay the guard as of a past instant "
                         "(e.g. 2026-08-22T12:02Z) instead of now — the only "
                         "way to ask why a sticky block actually fired")
    args = ap.parse_args(argv)

    config = Config.from_env()
    if args.data_dir:
        from dataclasses import replace

        config = replace(config, data_dir=args.data_dir)
    now = _parse_at(args.at) if args.at else datetime.now(timezone.utc)
    blocks = ResolverBlockStore(
        os.path.join(config.data_dir, "_global", "resolver_blocks.json"))

    cities = select_cities(
        config, [args.city] if args.city else [c.id for c in config.cities])
    print(f"Resolver cross-check — {now.isoformat(timespec='seconds')}")
    print(f"window {config.resolver_window_hours:.0f}h, need "
          f"{config.resolver_min_pairs} matched obs reaching back "
          f"{config.resolver_min_reach_hours:.0f}h and fresh within "
          f"{config.resolver_max_lag_hours:.0f}h, tolerance "
          f"{config.resolver_tolerance_c}C")
    print("(the resolver publishes 26-96 min late, so the newest ~1h is "
          "legitimately unverifiable)\n")
    print(f"{'city':<14} {'state':<11} {'pairs':>5} {'span':>6}  detail")
    print("-" * 100)

    diverged = []
    tradeable = 0
    for city_cfg in cities:
        guard = build_guard(config, city_cfg, now=now,
                            as_of=now if args.at else None,
                            seed_history=args.seed_history and not args.at)
        stored = blocks.blocked(city_cfg.id)
        if stored:
            guard.adopt_block(stored.get("detail") or stored.get("reason", ""))
        verdict = guard.evaluate(now)
        if verdict.state == OK:
            tradeable += 1
        if verdict.state == DIVERGENT:
            diverged.append(city_cfg.id)
        span = f"{verdict.span_hours:.1f}h" if verdict.span_hours else "--"
        print(f"{city_cfg.id:<14} {verdict.state:<11} {verdict.pairs:>5} {span:>6}  "
              f"{verdict.reason[:60]}")
        if args.verbose and verdict.mismatches:
            for m in verdict.mismatches:
                print(f"{'':<14} {'':<11} {'':>5} {'':>6}    {m.describe()}")
        if args.verbose and verdict.station:
            print(f"{'':<14} {'':<11} {'':>5} {'':>6}    resolver station: "
                  f"{verdict.station}")

    print("-" * 100)
    print(f"{tradeable}/{len(cities)} cities may trade; "
          f"{len(diverged)} blocked for divergence"
          + (f": {', '.join(diverged)}" if diverged else ""))
    if diverged:
        print("\nInvestigate before clearing. To clear one city:")
        print(f"  python -m dead_bracket.tools.resolver_clear {diverged[0]}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
