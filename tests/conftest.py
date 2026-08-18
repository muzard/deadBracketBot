"""Shared test fixtures and builders. No live network anywhere (spec §12)."""

from __future__ import annotations

import io
from typing import Optional

import pytest

from dead_bracket.config import CityConfig, Config
from dead_bracket.executor import PaperExecutor
from dead_bracket.market import MarketFeed
from dead_bracket.metar import MetarFeed
from dead_bracket.models import Bracket, MarketSnapshot
from dead_bracket.reporter import Reporter
from dead_bracket.risk import RiskManager
from dead_bracket.strategy import Strategy
from dead_bracket.worker import CityWorker


@pytest.fixture
def config() -> Config:
    return Config()


def make_bracket(temp: int, *, asks=(), bids=(), fees_enabled: bool = False,
                 neg_risk: bool = False, tick_size: float = 0.001,
                 min_order_size: float = 0.0, upper_open: bool = False) -> Bracket:
    """Default fees_enabled=False to keep arithmetic simple unless a test opts in."""

    return Bracket(
        temp=temp,
        no_asks=tuple((float(p), float(s)) for p, s in asks),
        no_bids=tuple((float(p), float(s)) for p, s in bids),
        fees_enabled=fees_enabled,
        token_id=f"tok-{temp}",
        neg_risk=neg_risk,
        tick_size=tick_size,
        min_order_size=min_order_size,
        upper_open=upper_open,
    )


def make_snapshot(city: str, brackets) -> MarketSnapshot:
    return MarketSnapshot(city=city, brackets=tuple(brackets), event_slug=f"{city}-high-temp")


@pytest.fixture
def risk(config) -> RiskManager:
    return RiskManager(config)


class StubReporter(Reporter):
    """Reporter that captures stdout (trades) and stderr (progress) separately.

    Progress is enabled so tests can assert on it; it writes to the captured
    stderr stream, leaving the stdout trade stream pure.
    """

    def __init__(self, progress: bool = True):
        self.stream = io.StringIO()
        self.err = io.StringIO()
        super().__init__("/dev/null", stdout=self.stream, stderr=self.err,
                         write_files=False, progress=progress)

    @property
    def console(self) -> list[str]:
        return [ln for ln in self.stream.getvalue().splitlines() if ln]

    @property
    def progress_lines(self) -> list[str]:
        return [ln for ln in self.err.getvalue().splitlines() if ln]


@pytest.fixture
def reporter() -> StubReporter:
    return StubReporter()


def build_worker(
    *,
    config: Config,
    risk: RiskManager,
    reporter: Reporter,
    city: str = "paris",
    icao: str = "LFPB",
    tz: str = "Europe/Paris",
    metar_raw: Optional[str] = None,
    snapshot_fn=None,
    city_cfg: Optional[CityConfig] = None,
    forecast=None,
    feed_reader=None,
    wall_clock=None,
    resolver=None,
    resolver_blocks=None,
) -> tuple[CityWorker, PaperExecutor, MetarFeed]:
    city_cfg = city_cfg or CityConfig(id=city, metar_icao=icao, tz=tz, market_terms=(city,))
    metar = MetarFeed(icao, fetch_fn=lambda _i: metar_raw, log_path=None)
    market = MarketFeed(city, snapshot_fn=snapshot_fn or (lambda **_kw: None))
    executor = PaperExecutor(config, risk)
    strategy = Strategy(city, config, risk)
    worker = CityWorker(
        city_cfg, config, metar=metar, market=market, executor=executor,
        strategy=strategy, risk=risk, reporter=reporter,
        forecast=forecast, feed_reader=feed_reader, wall_clock=wall_clock,
        resolver=resolver, resolver_blocks=resolver_blocks,
    )
    return worker, executor, metar
