"""Manually clear a sticky resolver-divergence block (the human in the loop).

A divergence block means our resolution source and the table the market settles
on disagreed — the exact condition that emptied the wallet on 2026-07-19. It is
deliberately NOT self-clearing: someone has to look at the mismatch and decide
what it means (wrong station, wrong pipeline, tenths-vs-whole-degrees, a
one-off resolver revision) before the city trades again.

    python -m dead_bracket.tools.resolver_status --city seoul -v   # look first
    python -m dead_bracket.tools.resolver_clear seoul --reason "..."

The running process picks the change up on its next tick only if it is
restarted — the in-memory guard keeps its sticky verdict for the life of the
process, by design.
"""

from __future__ import annotations

import argparse
import os
import sys

from ..config import Config
from ..resolver_guard import ResolverBlockStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("city", nargs="?", help="city id to unblock")
    ap.add_argument("--all", action="store_true", help="clear every block")
    ap.add_argument("--list", action="store_true", help="list blocks and exit")
    ap.add_argument("--reason", default="", help="note why it is safe to clear")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args(argv)

    config = Config.from_env()
    data_dir = args.data_dir or config.data_dir
    store = ResolverBlockStore(os.path.join(data_dir, "_global", "resolver_blocks.json"))
    blocks = store.all_blocks()

    if args.list or not (args.city or args.all):
        if not blocks:
            print("No resolver divergence blocks.")
            return 0
        print(f"{len(blocks)} blocked cit{'y' if len(blocks) == 1 else 'ies'} "
              f"({store.path}):\n")
        for city, rec in sorted(blocks.items()):
            print(f"  {city}")
            print(f"    since : {rec.get('since', '?')}")
            print(f"    reason: {rec.get('detail') or rec.get('reason', '?')}")
        print("\nClear one with: python -m dead_bracket.tools.resolver_clear <city>")
        return 0

    targets = sorted(blocks) if args.all else [args.city]
    cleared = []
    for city in targets:
        if store.clear(city):
            cleared.append(city)
            print(f"cleared: {city}")
        else:
            print(f"not blocked: {city}", file=sys.stderr)
    if not cleared:
        return 1
    if args.reason:
        print(f"reason recorded: {args.reason}")
    print("\nRestart the bot for the running process to pick this up.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
