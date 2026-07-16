"""CityWorker (spec §5).

Ties one city's METAR feed, market feed, strategy, executor, shared risk
manager, and reporter together. Workers are isolated: an exception in one city's
loop is caught, logged, and the loop restarts after a backoff — it must never
stop or corrupt another city (spec §5, §12).

The trading logic is exposed as small, side-effect-explicit methods
(``update_running_max``, ``check_feed_health``, ``process_book``) so it is
fully testable without threads or network; ``run`` just polls on a cadence and
calls them.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import CityConfig, Config
from .executor import Executor
from .feeds_http import TransientBookError
from .fees import net as net_fn
from .forecast import FeedReading, ForecastStrategy
from .market import MarketFeed
from .metar import MetarFeed
from .models import MarketSnapshot
from .reporter import Reporter
from .risk import RiskManager
from .strategy import Strategy, is_dead

# Returned by ``poll_market`` when a transient book-fetch failure should be
# ignored: distinct from ``None`` (a genuinely vanished market) so the caller
# skips the tick without flapping the market-unavailable notice.
_SKIP_TICK = object()


class CityWorker:
    def __init__(
        self,
        city_cfg: CityConfig,
        config: Config,
        *,
        metar: MetarFeed,
        market: MarketFeed,
        executor: Executor,
        strategy: Strategy,
        risk: RiskManager,
        reporter: Reporter,
        forecast: Optional[ForecastStrategy] = None,
        feed_reader=None,  # Callable[[], Optional[FeedReading]] from FeedCollector.latest
        balance=None,  # BalanceTracker (live) / PaperBalanceTracker; refreshed before buys
        monotonic=time.monotonic,
        wall_clock=None,  # () -> aware UTC datetime; injectable for tests
    ) -> None:
        self.city = city_cfg.id
        self._city_cfg = city_cfg
        self._cfg = config
        self._metar = metar
        self._market = market
        self._executor = executor
        self._strategy = strategy
        self._risk = risk
        self._reporter = reporter
        # Wallet-balance tracker: refreshed at high frequency while a buy is being
        # attempted and cooled down when the balance is stable (see balance.py).
        # Defaults to a fixed synthetic tracker so paper/tests need no wallet.
        if balance is None:
            from .balance import PaperBalanceTracker
            balance = PaperBalanceTracker(risk.current_balance)
        self._balance = balance
        # Forecast path (optional): probabilistic entries from a sub-hourly
        # feed reading. Both stay None unless the run passes --forecast and
        # this city has an active feed.
        self._forecast = forecast
        self._feed_reader = feed_reader
        self._feed_stale_notified = False
        self._wall = wall_clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        # Tolerant crossed-book tracking (paper mode, spec §9).
        self._crossed_streak = 0
        self._healthy_streak = 0
        # Consecutive clean ticks while paused for repeated order errors, used to
        # auto-recover the city once the venue is reachable again.
        self._order_ok_streak = 0
        # Market-availability state for change-only health notices.
        self._market_available: Optional[bool] = None
        # Today's sub-98c dead-NO opportunity sightings (rising-edge counted so a
        # persistent offer isn't tallied every tick), reset at local midnight.
        try:
            self._tz = ZoneInfo(city_cfg.tz)
        except Exception:
            self._tz = None
        self._opp_count = 0
        self._opp_day: Optional[str] = None
        self._opp_active: set[int] = set()
        # Minimal price dataset (for studying why the dead-NO edge rarely appears):
        # the NO book the instant each bracket first goes dead, one row per bracket
        # per day. ``_dead_seen`` enforces the once-per-day rising edge; _last_no_ask
        # carries each bracket's prior-tick (still-live) ask so the snapshot can show
        # whether the market had already priced it to ~$1 before our METAR confirmed.
        self._dead_seen: set[int] = set()
        self._last_no_ask: dict[int, Optional[float]] = {}
        # Windowed price sampler: a monotonic clock for the >=price_sample_sec
        # throttle, and the time of the last sample (None = never sampled yet).
        self._mono = monotonic
        self._last_price_sample: Optional[float] = None
        # Peak $ of liftable dead-NO blocked by the bankroll caps today.
        self._capped_peak = 0.0
        # Seed the METAR-output dedup from the feed's log so a restart doesn't
        # re-announce a report already recorded (it appeared as an inflated
        # "+my" latency on every restart).
        self._last_metar_key = metar.last_obs_key

    # -- hot-window timing -------------------------------------------------
    def is_hot(self, now: Optional[datetime] = None) -> bool:
        """True when the next scheduled report is due but not yet seen (poll fast).

        Predictive: stations report half-hourly but at station-specific minutes
        (:00/:30, :20/:50, :25/:55 ...), so the window arms a couple of minutes
        before ``last_obs + 30min`` (``metar_expected_gap_min``, default 28) and
        stays hot until the new report actually lands — covering the 100-400s
        publish delay regardless of the station's reporting offset. No
        observation yet -> hot.
        """

        now = now or datetime.now(timezone.utc)
        last = self._metar.last_obs_time
        if last is None:
            return True
        return (now - last).total_seconds() >= self._cfg.metar_expected_gap_min * 60.0

    def metar_interval(self, now: Optional[datetime] = None) -> float:
        return self._cfg.metar_hot_poll_sec if self.is_hot(now) else self._cfg.metar_poll_sec

    # -- feed handling -----------------------------------------------------
    def update_running_max(self, max_age: Optional[float] = None) -> Optional[int]:
        """Poll METAR; honour the downward-revision guard (spec §9)."""

        try:
            obs = self._metar.poll_once(max_age)
        except Exception as exc:  # feed errors must not crash the worker
            self._reporter.event(self.city, "metar_error", error=repr(exc))
            return self._metar.running_max
        # Only log/print when the reading actually changes — with 1s hot polling
        # the same observation is re-served every tick and would flood the output.
        if obs is not None:
            key = (obs.obs_time, obs.temp_c, obs.is_correction)
            if key != self._last_metar_key:
                self._last_metar_key = key
                rmax = self._metar.running_max
                self._reporter.event(self.city, "metar", temp=obs.temp_c,
                                     obs_time=obs.obs_time.isoformat(),
                                     is_correction=obs.is_correction,
                                     running_max=rmax)
                if self._forecast is not None:
                    # Feed-vs-METAR sensor-identity guard: pair this ob with
                    # the nearest feed reading; divergence disables the feed.
                    was_tripped = self._forecast.guard.tripped
                    self._forecast.note_metar(obs.obs_time, obs.temp_c)
                    if self._forecast.guard.tripped and not was_tripped:
                        self._reporter.event(self.city, "feed_divergence_trip",
                                             pair=self._forecast.guard.last_pair)
                        self._reporter.notice(
                            self.city, "forecast feed disabled for today: "
                            "feed-vs-METAR divergence")
                # Latency split: publish delay (obs -> source receipt) plus our
                # own delay (source receipt -> now). When the receipt time is
                # unknown only the total is shown.
                now_wall = datetime.now(timezone.utc)
                total = max(0.0, (now_wall - obs.obs_time).total_seconds())
                publish = my = None
                # Only split when the receipt is sane (at/after the observation
                # and not in the future); otherwise show the total alone rather
                # than a misleading "0s (+huge)".
                if (obs.receipt_time is not None
                        and obs.obs_time <= obs.receipt_time <= now_wall):
                    publish = (obs.receipt_time - obs.obs_time).total_seconds()
                    my = (now_wall - obs.receipt_time).total_seconds()
                self._reporter.metar_reading(self.city, temp=obs.temp_c, running_max=rmax,
                                             publish_sec=publish, my_sec=my, total_sec=total)
        if self._metar.revision_detected and not self._risk.is_city_blocked(self.city):
            if self._risk.halt_city(self.city, "downward METAR revision"):
                self._reporter.event(self.city, "revision_halt",
                                     running_max=self._metar.running_max)
        return self._metar.running_max

    def check_feed_health(self, now=None) -> None:
        """Trip the kill-switch on a stale resolution feed (spec §9)."""

        stale = self._metar.is_stale(stale_min=self._cfg.stale_feed_min, now=now)
        if self._risk.check_stale(city=self.city, is_stale=stale):
            # Per-city pause (not the global kill); logged + progress, not stdout.
            self._reporter.event(self.city, "stale_feed_halt")
            self._reporter.notice(self.city, "paused: stale METAR feed")

    def _held_temps(self) -> set[int]:
        """Brackets this city still holds, so their books keep being fetched even
        if the running max has climbed past the frontier band (the resting 99.9c
        sell can only be reconciled/filled while its bracket is in the snapshot)."""

        positions = getattr(self._executor, "positions", {})
        return {p.temp for p in positions.values()
                if p.city == self.city and p.shares > 0}

    def poll_market(self):
        """Poll the frontier-band book; absorb transient fetch failures.

        Returns the snapshot (possibly empty), ``None`` for a genuinely vanished
        market (handled by ``process_book`` as the unavailable notice), or
        ``_SKIP_TICK`` when the batched book request just failed this tick — a
        rate-limit/network blip that must not flap the console. The transient case
        is recorded to the events log only and leaves market-availability state
        untouched (so we don't print restored/unavailable on every blip).
        """

        try:
            return self._market.poll_once(running_max=self._metar.running_max,
                                          keep_temps=self._held_temps())
        except TransientBookError:
            self._reporter.event(self.city, "book_fetch_transient")
            return _SKIP_TICK

    # -- core trading cycle ------------------------------------------------
    def process_book(self, snapshot: Optional[MarketSnapshot]) -> None:
        """One market cycle: fill resting sells, validate, then maybe enter."""

        if snapshot is None:
            # Market not resolving / book unavailable. Notice only on change so
            # it isn't spammy; always recorded to the events log.
            if self._market_available is not False:
                self._reporter.event(self.city, "market_unavailable")
                self._reporter.notice(self.city, "no market data (event not resolved or book unavailable)")
            self._market_available = False
            return

        if self._market_available is False:
            self._reporter.event(self.city, "market_restored", event_slug=snapshot.event_slug)
            self._reporter.notice(self.city, f"market data restored ({snapshot.event_slug})")
        self._market_available = True

        rmax = self._metar.running_max

        # 0. Windowed price sampling (pure telemetry, runs even while halted): in a
        #    publication hot window, sparsely record the frontier NO prices so we
        #    can see the market reprice before our METAR lands and gauge our lag.
        self._maybe_sample_prices(snapshot, rmax)

        # 1. Book-validity kill-switch (spec §9). Strict global trip in live;
        #    tolerant per-city pause with auto-recovery in paper.
        self._check_books(snapshot, rmax)

        # 2. Maintain the resting maker exits (spec §4). The executor skips any
        #    crossed/invalid bracket, so a venue glitch can't spoof a fill.
        try:
            for fill in self._executor.on_book(snapshot):
                self._reporter.sell(fill)
        except Exception as exc:  # pragma: no cover - defensive
            self._handle_order_error(exc)
            return

        # The single console HALT line is reserved for the GLOBAL kill-switch
        # (daily-loss); per-city pauses go to the progress/log streams only.
        self._announce_global_halt()

        # Auto-recover a per-city order-error pause: reaching here means step 2
        # maintained the resting sells without an order error this tick, so the
        # venue is reachable again. After enough consecutive clean ticks resume
        # the city — a transient blip (e.g. a connection reset on an order-status
        # poll) must never sideline a city for the rest of the day (24/7 op).
        if self._risk.city_halt_reason(self.city) == "repeated order errors":
            self._order_ok_streak += 1
            if self._order_ok_streak >= self._cfg.order_error_recover_ticks:
                if self._risk.recover_city(self.city, "repeated order errors"):
                    self._risk.note_order_ok(self.city)  # clear the error counter
                    self._reporter.event(self.city, "order_error_recover",
                                         streak=self._order_ok_streak)
                    self._reporter.notice(self.city, "resumed: order errors cleared")
                self._order_ok_streak = 0
        else:
            self._order_ok_streak = 0

        # 3. No new entries while halted (globally or for this city).
        if self._risk.is_city_blocked(self.city):
            return

        # Refresh the wallet balance before sizing: poll fast while a buy is
        # liftable (a dead bracket with a cheap NO in the book), and cool the
        # cadence down otherwise — so percentage sizing bases off the real
        # available capital without hammering the venue's balance API.
        wants_to_buy = any(
            is_dead(b.temp, rmax, upper_open=b.upper_open)
            and b.no_best_ask is not None and b.no_best_ask < self._cfg.cap
            for b in snapshot.brackets)
        self._balance.refresh(urgent=wants_to_buy)

        intents = self._strategy.evaluate(snapshot, rmax)
        acted = {i.temp for i in intents}
        blocked_this_tick = self._strategy.last_blocked_value

        # 3½. Forecast path (optional): probabilistic entries from the freshest
        #     sub-hourly feed reading. Runs AFTER the dead path so a bracket the
        #     certainty path just took is never double-bought; shares the same
        #     halt gate above. In live mode it is signal-logging only unless
        #     forecast_live is explicitly enabled (the human go/no-go).
        intents = intents + self._evaluate_forecast(snapshot, rmax, frozenset(acted))

        # 4. Opportunity telemetry: every dead-NO ask < CAP (spec §11), plus a
        #    one-row-per-bracket-per-day snapshot of the NO book the instant a
        #    bracket goes dead (the minimal price dataset for the no-edge problem).
        opp_now: set[int] = set()
        for b in snapshot.brackets:
            a = b.no_best_ask
            dead = is_dead(b.temp, rmax, upper_open=b.upper_open)
            if dead and b.temp not in self._dead_seen:
                self._dead_seen.add(b.temp)
                self._reporter.dead_snapshot(
                    self.city, temp=b.temp, running_max=rmax, ask=a,
                    bid=b.no_best_bid, depth=b.depth_at_or_below(self._cfg.cap),
                    ask_pre=self._last_no_ask.get(b.temp))
            self._last_no_ask[b.temp] = a
            if not dead or a is None or a >= self._cfg.cap:
                continue
            opp_now.add(b.temp)
            n = net_fn(a, fee_rate=self._cfg.fee_rate,
                       fee_exponent=self._cfg.fee_exponent, fees_enabled=b.fees_enabled)
            why = "" if b.temp in acted else ("net<min_net" if n < self._cfg.min_net else "size/caps")
            self._reporter.opportunity(self.city, temp=b.temp, ask=a,
                                       depth=b.depth_at_or_below(self._cfg.cap),
                                       net=n, acted=b.temp in acted, why_not=why)
        self._count_opportunities(opp_now)  # also performs the daily reset
        # Track today's peak bankroll-blocked volume AFTER the daily reset, so
        # the first tick of a new day doesn't wipe it.
        self._capped_peak = max(self._capped_peak, blocked_this_tick)

        # 5. Execute entries. Whether the freshly-bought NO is held to $1 or
        #    immediately backed by a recycle sell is decided in step 6.
        for intent in intents:
            bracket = snapshot.bracket(intent.temp)
            if bracket is None:
                continue
            try:
                fill = self._executor.execute_entry(intent, bracket)
            except Exception as exc:
                self._handle_order_error(exc)
                continue
            self._risk.note_order_ok(self.city)
            if fill is None:
                continue
            self._reporter.buy(fill)
            if not self._cfg.recycle_on_opportunity or fill.reason.startswith("fcast"):
                # Legacy always-rest, OR a forecast entry: a probabilistic bet
                # gets its immediate 99.9c exit so it's de-risked (locked in) the
                # moment the market confirms. Dead-path entries in gated mode
                # defer to step 6 and are held to $1 unless capital is blocked.
                self._place_exit_for(fill.temp, fill.size, bracket)

        # 6. Capital recycling. Default (opportunity-gated): hold every dead NO to
        #    $1 and post recycle sells only to cover a bankroll-blocked buy this
        #    tick, cancelling them when the block clears. Legacy: guarantee every
        #    held share rests (spec §4 as originally written).
        if self._cfg.recycle_on_opportunity:
            self._maintain_recycle_sells(snapshot, frozenset(acted), blocked_this_tick)
        else:
            self._ensure_exits(snapshot)

    def _evaluate_forecast(self, snapshot: MarketSnapshot, rmax: Optional[int],
                           skip_temps: frozenset[int]) -> list:
        """Run the forecast engine for this tick; log its signals; gate live.

        Returns the Intents to execute (empty when the path is disabled, the
        feed is stale/absent, or live mode without the forecast_live enable).
        """

        if self._forecast is None or self._feed_reader is None:
            return []
        reading: Optional[FeedReading] = self._feed_reader()
        now_utc = self._wall()
        intents = self._forecast.evaluate(snapshot, rmax, reading, now_utc,
                                          skip_temps=skip_temps)
        # Stale-feed transition notices (signals off; METAR path unaffected).
        if reading is not None:
            if self._forecast.stale and not self._feed_stale_notified:
                self._feed_stale_notified = True
                self._reporter.event(self.city, "feed_stale",
                                     obs_time=reading.obs_time.isoformat())
                self._reporter.notice(self.city, "forecast feed stale: signals off")
            elif not self._forecast.stale and self._feed_stale_notified:
                self._feed_stale_notified = False
                self._reporter.event(self.city, "feed_fresh")
                self._reporter.notice(self.city, "forecast feed fresh again")
        live_gated = self._cfg.mode == "live" and not self._cfg.forecast_live
        for row in self._forecast.last_signals:
            self._reporter.signal(self.city, live_gated=live_gated, **row)
        if live_gated and intents:
            # Signals are recorded (the shadow measurement) but no live orders
            # without the explicit --forecast-live go decision.
            return []
        return intents

    def _place_exit_for(self, temp: int, size: float, bracket) -> None:
        """Place a GTC maker sell of ``size`` shares for one bracket."""

        from .models import Fill
        # Dead-NO exits always rest near $1 — Polymarket's CLOB uses 0.001 ticks
        # at price extremes regardless of what orderPriceMinTickSize reports for
        # the mid-range. Historical fills at 0.999 confirm this. Using the
        # bracket's coarse tick (often 0.01) here would incorrectly clamp to 0.99.
        exit_intent = self._strategy.make_exit_intent(
            Fill(city=self.city, temp=temp, side="buy", price=0.0, size=size, fee=0.0),
            token_id=bracket.token_id, tick_size=0.001, neg_risk=bracket.neg_risk)
        try:
            self._executor.place_exit(exit_intent)
        except Exception as exc:
            self._handle_order_error(exc)

    def _ensure_exits(self, snapshot: MarketSnapshot) -> None:
        """Re-post the resting sell for any held shares not yet covered by one."""

        positions = getattr(self._executor, "positions", {})
        for pos in list(positions.values()):
            if pos.city != self.city:
                continue
            uncovered = pos.shares - pos.resting_sell
            if uncovered <= 0:
                continue
            bracket = snapshot.bracket(pos.temp)
            if bracket is None:
                continue  # bracket not in the current book; will retry next tick
            self._place_exit_for(pos.temp, uncovered, bracket)

    def _cancel_exit_for(self, temp: int) -> None:
        """Retract the resting recycle sell for one bracket (revert to hold-$1)."""

        try:
            self._executor.cancel_exit(self.city, temp)
        except Exception as exc:
            self._handle_order_error(exc)

    def _maintain_recycle_sells(self, snapshot: MarketSnapshot,
                                acted: frozenset[int], shortfall: float) -> None:
        """Opportunity-gated capital recycling (the default policy).

        A dead NO is certain to pay $1, so hold it — UNLESS a liftable dead-NO buy
        was blocked purely by the bankroll caps this tick (``shortfall`` > 0). In
        that case post resting 99.9c maker sells on OTHER held positions, enough to
        cover the shortfall, so capital frees as those sells fill against arriving
        99.9c bids and the blocked buy can be taken on a later tick. When there is
        no such block, cancel any still-unfilled recycle sells and revert to
        holding every NO to $1.

        Brackets bought this tick (``acted``) are never recycled — we just paid to
        acquire them; selling them back would be churn, not recycling. Each freed
        share redeems at ~$1, so shares are used directly as the dollar proxy.

        Forecast positions are exempt: they keep their immediate 99.9c de-risk
        exit (posted in step 5) and are re-covered on retry here, never cancelled —
        only the dead-path positions are opportunity-gated.
        """

        positions = getattr(self._executor, "positions", {})
        mine = [p for p in positions.values()
                if p.city == self.city and p.shares > 0]
        forecast_pos, dead_pos = [], []
        for pos in mine:
            (forecast_pos if self._risk.forecast_exposure_for(self.city, pos.temp) > 0
             else dead_pos).append(pos)

        # Forecast positions: guarantee their resting de-risk exit stays in place
        # (re-cover a prior place_exit that failed); never cancel or gate them.
        for pos in forecast_pos:
            uncovered = pos.shares - pos.resting_sell
            if uncovered <= 0:
                continue
            bracket = snapshot.bracket(pos.temp)
            if bracket is None:
                continue
            self._place_exit_for(pos.temp, uncovered, bracket)

        if shortfall <= 0:
            # No unfunded opportunity: hold dead positions to $1, retract any
            # resting recycle sell so nothing sells at 99.9c needlessly.
            for pos in dead_pos:
                if pos.resting_sell > 0:
                    self._cancel_exit_for(pos.temp)
            return

        # Value already-resting recycle sells (on dead brackets we didn't just buy)
        # toward the shortfall, so we don't over-post across ticks.
        recyclable = [p for p in dead_pos if p.temp not in acted]
        covered = sum(p.resting_sell for p in recyclable)
        for pos in recyclable:
            if covered >= shortfall:
                break
            uncovered = pos.shares - pos.resting_sell
            if uncovered <= 0:
                continue
            bracket = snapshot.bracket(pos.temp)
            if bracket is None:
                continue  # bracket not in the current book; retry next tick
            size = min(uncovered, shortfall - covered)
            if size <= 0:
                continue
            self._place_exit_for(pos.temp, size, bracket)
            covered += size

    def _check_books(self, snapshot: MarketSnapshot, rmax: Optional[int]) -> None:
        """Police order-book validity (spec §9).

        Live (strict): any crossed/invalid book trips the global, sticky
        kill-switch and prints the HALT line — you do not trade against a venue
        emitting broken books.

        Paper (tolerant): only brackets at/below the running max (the ones we
        could actually trade) are policed; a crossed book must persist
        ``crossed_book_grace_ticks`` consecutive ticks before the *city* (not the
        whole process) is paused, and the city auto-recovers after
        ``crossed_book_recover_ticks`` healthy ticks. This keeps a transient
        exchange glitch on an irrelevant bracket from stopping data collection.
        """

        if self._cfg.mode == "live":
            # Strict: a crossed/invalid book pauses THIS city (not the process).
            if self._risk.check_book(snapshot):
                self._reporter.event(self.city, "book_halt")
                self._reporter.notice(self.city, "paused: crossed/invalid book")
            return

        if rmax is None:
            return  # nothing is tradeable yet; don't police books

        crossed = [b.temp for b in snapshot.brackets
                   if b.temp <= rmax and b.is_crossed_or_invalid()]
        if crossed:
            self._crossed_streak += 1
            self._healthy_streak = 0
            if self._crossed_streak >= self._cfg.crossed_book_grace_ticks:
                if self._risk.halt_city(self.city, "crossed/invalid book", recoverable=True):
                    self._reporter.event(self.city, "book_halt", brackets=crossed,
                                         streak=self._crossed_streak)
                    self._reporter.notice(self.city, f"paused: crossed/invalid book {crossed}")
        else:
            self._healthy_streak += 1
            self._crossed_streak = 0
            if self._healthy_streak >= self._cfg.crossed_book_recover_ticks:
                if self._risk.recover_city(self.city, "crossed/invalid book"):
                    self._reporter.event(self.city, "book_recover", streak=self._healthy_streak)
                    self._reporter.notice(self.city, "resumed: book healthy")

    def _maybe_sample_prices(self, snapshot: MarketSnapshot, rmax: Optional[int]) -> None:
        """Sparsely sample the frontier NO prices during a publication hot window.

        Gated three ways so it stays minimal: only inside a hot window (the minutes
        a new report is due — :00/:30 plus each station's own offset, via
        ``is_hot``), at most once per ``price_sample_sec``, and only the frontier
        band ``[rmax, rmax+price_sample_band]`` (the brackets about to flip dead).
        Disabled when ``price_sample_sec`` <= 0 or before the first observation.
        """

        if self._cfg.price_sample_sec <= 0 or rmax is None:
            return
        if not self.is_hot():
            return
        now = self._mono()
        if (self._last_price_sample is not None
                and now - self._last_price_sample < self._cfg.price_sample_sec):
            return
        self._last_price_sample = now
        last_obs = self._metar.last_obs_time
        obs_age = None
        if last_obs is not None:
            obs_age = max(0.0, (datetime.now(timezone.utc) - last_obs).total_seconds())
        top = rmax + max(0, self._cfg.price_sample_band)
        for b in snapshot.brackets:
            if b.upper_open or b.temp < rmax or b.temp > top:
                continue
            self._reporter.price_sample(self.city, temp=b.temp, running_max=rmax,
                                        ask=b.no_best_ask, bid=b.no_best_bid,
                                        obs_age_sec=obs_age)

    def _count_opportunities(self, opp_now: set[int]) -> None:
        """Tally newly-appeared sub-98c dead-NO offers (rising edges) for today.

        A bracket that stays cheap across many ticks counts once until it leaves
        the sub-98c state and reappears. Resets at the city's local midnight so
        the count tracks the trading day.
        """

        today = datetime.now(timezone.utc)
        if self._tz is not None:
            today = today.astimezone(self._tz)
        day = today.date().isoformat()
        if day != self._opp_day:
            self._opp_day = day
            self._opp_count = 0
            self._opp_active = set()
            self._capped_peak = 0.0  # reset the daily bankroll-blocked peak
            self._dead_seen = set()  # re-arm the once-per-day dead-snapshot
            self._last_no_ask = {}
        # Rising edges: brackets cheap now that weren't cheap last tick.
        self._opp_count += len(opp_now - self._opp_active)
        self._opp_active = opp_now

    @property
    def opportunities_today(self) -> int:
        return self._opp_count

    def _handle_order_error(self, exc: Exception) -> None:
        self._reporter.event(self.city, "order_error", error=repr(exc))
        # Per-city: repeated errors pause only this city, never the others.
        if self._risk.note_order_error(self.city):
            self._reporter.notice(self.city, "paused: repeated order errors")

    def _announce_global_halt(self) -> None:
        """Print the one console HALT line if the GLOBAL kill-switch newly tripped."""

        reason = self._risk.pop_global_halt()
        if reason:
            self._reporter.halt(reason)

    def roll_day(self) -> None:
        """Settle yesterday's positions and log the daily rollover for this city.

        Called at the trading-day boundary so the bot runs unattended: open
        dead-NO positions from the resolved day are settled and reported. The
        day's final running max marks the WINNING bracket, whose NO pays $0 —
        only reachable by a forecast entry whose predicted kill never confirmed
        (dead-path positions always sit below the final max). Such a loss
        pauses the forecast path globally (config.forecast_pause_on_loss): at
        p-hat >= 0.97 it is a model/mapping bug until a human clears it. The
        shared risk re-arm is done once by the supervisor.
        """

        final_max = self._metar.running_max
        settlements = self._executor.settle(final_max)
        total_pnl = 0.0
        for s in settlements:
            self._reporter.settlement(s.city, temp=s.temp, shares=s.shares, pnl=s.pnl,
                                      won=s.won)
            total_pnl += s.pnl
            if not s.won and self._cfg.forecast_pause_on_loss:
                if self._risk.pause_forecast(
                        f"settled loss: {s.city} {s.temp}C was the daily high"):
                    self._reporter.event(self.city, "forecast_paused",
                                         temp=s.temp, final_max=final_max,
                                         pnl=s.pnl)
                    self._reporter.notice(
                        self.city, "FORECAST PAUSED: settled loss on "
                        f"{s.temp}C (final max {final_max}) — restart to clear")
        if self._forecast is not None:
            self._forecast.roll_day()
        self._reporter.event(self.city, "day_rollover", settled=len(settlements), pnl=total_pnl)
        self._reporter.notice(self.city, f"new day: settled {len(settlements)} position(s), pnl {total_pnl:+.2f}")

    # -- progress ----------------------------------------------------------
    def open_volume(self) -> tuple[int, float, float]:
        """Currently-held positions for this city: (count, shares, cost $)."""

        mine = [p for p in getattr(self._executor, "positions", {}).values()
                if p.city == self.city and p.shares > 0]
        return len(mine), sum(p.shares for p in mine), sum(p.cost_basis for p in mine)

    def status_line(self) -> str:
        """Build a one-line per-city status summary for periodic progress.

        Shows the OPEN VOLUME the bot has actually taken — capital deployed vs the
        per-city cap, plus shares and position count — so you can see the
        remaining headroom to scale into.
        """

        rmax = self._metar.running_max
        hi = "--" if rmax is None else f"{rmax}C"
        last = self._metar.last_obs_time
        obs = last.strftime("%H:%MZ") if last else "no METAR"
        n, shares, cost = self.open_volume()
        city_cap = self._risk.city_cap_dollars()
        total_cap = self._risk.total_cap_dollars()
        state = "HALTED" if self._risk.is_city_blocked(self.city) else (
            "HOT" if self.is_hot() else ("MKT?" if self._market_available is False else "ok"))
        # Per-city deployed/cap; the portfolio total/cap (the real ceiling when
        # scaling); and avail+ = $ of liftable dead-NO the bankroll caps blocked
        # today (peak) — how much more you could deploy with a bigger bankroll.
        avail = f"  avail+${self._capped_peak:.0f}" if self._capped_peak > 0 else ""
        return (f"STATUS  {self.city:<9} high {hi:<4} obs {obs:<9} "
                f"open ${cost:.0f}/${city_cap:.0f} ({int(shares)}sh, {n}pos){avail}  "
                f"tot ${self._risk.total_exposure:.0f}/${total_cap:.0f}  {state}")

    def emit_status(self) -> None:
        self._reporter.progress(self.status_line())

    # -- run loop ----------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - exercised via supervisor/integration
        """Poll feeds on cadence and drive the trading cycle until stopped.

        Restart-safe: any per-iteration exception is caught and logged, then the
        loop continues after a short backoff so one city never takes down the
        process or the other city.
        """

        last_metar = 0.0
        last_status = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                # Hot window after :00/:30 -> poll METAR aggressively until the
                # new report lands; otherwise the slow cadence. The freshness
                # hint drives the shared/batched fetcher at the same rate.
                interval = self.metar_interval()
                if now - last_metar >= interval:
                    self.update_running_max(max_age=interval)
                    last_metar = now
                self.check_feed_health()
                snap = self.poll_market()
                if snap is not _SKIP_TICK:
                    self.process_book(snap)
                if now - last_status >= self._cfg.status_every_sec:
                    self.emit_status()
                    last_status = now
            except Exception as exc:
                self._reporter.event(self.city, "worker_error", error=repr(exc))
                self._stop.wait(1.0)
                continue
            self._stop.wait(self._cfg.market_poll_sec)
