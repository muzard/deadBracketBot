"""Market feed (spec §6).

Resolves today's event via Gamma, then reads the CLOB book + depth for each
bracket's NO side (and the NO bid, for the exit), capturing ``feesEnabled`` and
fee params per market. The heavy network code lives in :mod:`dead_bracket.feeds_http`;
``MarketFeed`` itself only orchestrates an injected ``snapshot_fn`` so the
strategy sees a clean ``MarketSnapshot`` interface and tests need no network.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .models import MarketSnapshot


class MarketFeed:
    """Produces point-in-time :class:`MarketSnapshot` objects for one city.

    ``snapshot_fn`` performs discovery + book/depth reads and returns a snapshot
    (or None if discovery has not yet succeeded). It is injected to keep the
    network layer out of the strategy-critical path and fully mockable in tests.
    """

    def __init__(
        self,
        city: str,
        *,
        snapshot_fn: Callable[[], Optional[MarketSnapshot]],
    ) -> None:
        self.city = city
        self._snapshot_fn = snapshot_fn
        self._lock = threading.Lock()
        self._last: Optional[MarketSnapshot] = None

    def poll_once(self, running_max: Optional[int] = None,
                  keep_temps=()) -> Optional[MarketSnapshot]:
        """Fetch the current book; cache and return it (None if undiscovered).

        ``running_max``/``keep_temps`` are forwarded to the snapshot fn so it can
        fetch only the frontier band (plus held positions). May raise
        ``TransientBookError`` when the batched book request fails this tick — the
        caller decides whether to skip (it does, to avoid console flap).
        """

        snap = self._snapshot_fn(running_max=running_max, keep_temps=keep_temps)
        if snap is not None:
            with self._lock:
                self._last = snap
        return snap

    @property
    def last(self) -> Optional[MarketSnapshot]:
        with self._lock:
            return self._last
