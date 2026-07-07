"""Console policy + telemetry tests (spec §8, §11, §12)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

from dead_bracket.models import Fill
from dead_bracket.reporter import Reporter
from conftest import StubReporter

FIXED = datetime(2026, 6, 8, 15, 30, tzinfo=timezone.utc)


def test_only_buy_sell_halt_reach_console():
    rep = StubReporter()
    rep.buy(Fill(city="paris", temp=24, side="buy", price=0.973, size=300, fee=0))
    rep.sell(Fill(city="paris", temp=24, side="sell", price=0.999, size=300, fee=0))
    # Non-console events must NOT appear on stdout.
    rep.opportunity("paris", temp=23, ask=0.97, depth=100, net=0.03, acted=False)
    rep.event("paris", "heartbeat", n=1)
    rep.settlement("paris", temp=24, shares=300, pnl=8.0)
    rep.halt("stale METAR feed for paris")

    console = rep.console
    assert console == [
        "BOUGHT  paris  24C NO  x300 @ 0.973",
        "SOLD    paris  24C NO  x300 @ 0.999",
        "HALT: stale METAR feed for paris",
    ]


def test_metar_reading_shows_publish_and_my_delay():
    rep = StubReporter()
    rep.metar_reading("paris", temp=22, running_max=22, publish_sec=170.0, my_sec=12.0)
    assert any(ln == "METAR   paris     22C   high 22C   delay 170s (+12s)"
               for ln in rep.progress_lines)


def test_metar_reading_falls_back_to_total_without_receipt():
    rep = StubReporter()
    rep.metar_reading("paris", temp=22, running_max=22, total_sec=183.0)
    assert any("delay 183s" in ln and "(+" not in ln for ln in rep.progress_lines)


def test_metar_reading_persists_latency_split(tmp_path):
    # The controllable "my delay" must be logged to a file (not just stderr) so
    # the fetcher fix has a measurable before/after baseline.
    rep = Reporter(str(tmp_path), progress=False, clock=lambda: FIXED)
    rep.metar_reading("paris", temp=22, running_max=22,
                      publish_sec=170.4, my_sec=12.6, total_sec=183.0)
    path = tmp_path / "paris" / "2026-06-08" / "latency.jsonl"
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln]
    assert len(rows) == 1
    assert rows[0]["my_sec"] == 12.6 and rows[0]["publish_sec"] == 170.4
    assert rows[0]["temp"] == 22 and rows[0]["running_max"] == 22


def test_metar_reading_no_latency_row_without_timing(tmp_path):
    # No receipt and no total -> nothing to measure -> no row (avoid noise).
    rep = Reporter(str(tmp_path), progress=False, clock=lambda: FIXED)
    rep.metar_reading("paris", temp=22, running_max=22)
    assert not (tmp_path / "paris" / "2026-06-08" / "latency.jsonl").exists()


def test_buy_sell_line_format_matches_spec():
    rep = StubReporter()
    rep.buy(Fill(city="london", temp=18, side="buy", price=0.95, size=50, fee=0))
    assert rep.console[0] == "BOUGHT  london  18C NO  x50 @ 0.950"


def test_telemetry_written_to_per_day_files(tmp_path):
    rep = Reporter(str(tmp_path), stdout=io.StringIO(), clock=lambda: FIXED)
    rep.opportunity("paris", temp=23, ask=0.97, depth=120, net=0.03, acted=True)
    rep.settlement("paris", temp=23, shares=100, pnl=3.0)
    rep.event("paris", "discovery", slug="paris-high-temp")

    day = tmp_path / "paris" / "2026-06-08"  # per-day folder
    opp = (day / "opportunities.jsonl").read_text().splitlines()
    assert json.loads(opp[0])["ask"] == 0.97
    settle = (day / "settlement.jsonl").read_text().splitlines()
    assert json.loads(settle[0])["pnl"] == 3.0
    events = (day / "events.jsonl").read_text().splitlines()
    assert json.loads(events[0])["event"] == "discovery"


def test_log_day_uses_city_local_timezone(tmp_path):
    # 23:30 UTC on the 8th is 02:30 on the 9th in Moscow (UTC+3) -> next day folder.
    rep = Reporter(str(tmp_path), stdout=io.StringIO(),
                   clock=lambda: datetime(2026, 6, 8, 23, 30, tzinfo=timezone.utc),
                   city_tz={"moscow": "Europe/Moscow"})
    rep.event("moscow", "discovery", slug="moscow-high-temp")
    assert (tmp_path / "moscow" / "2026-06-09" / "events.jsonl").exists()


def test_dead_snapshot_written_and_console_pure(tmp_path):
    rep = Reporter(str(tmp_path), stdout=io.StringIO(), clock=lambda: FIXED)
    rep.dead_snapshot("paris", temp=23, running_max=25, ask=0.9985,
                      bid=0.997, depth=120.0, ask_pre=0.9123456)
    rec = json.loads((tmp_path / "paris" / "2026-06-08" / "dead.jsonl").read_text())
    # Prices are rounded to 4 dp to keep the file tiny; pre-dead ask is carried.
    assert rec["temp"] == 23 and rec["running_max"] == 25
    assert rec["ask"] == 0.9985 and rec["ask_pre"] == 0.9123 and rec["depth"] == 120.0
    # It is telemetry only — nothing reaches the stdout trade stream.
    assert rep._stdout.getvalue() == ""


def test_dead_snapshot_tolerates_missing_prices(tmp_path):
    rep = Reporter(str(tmp_path), stdout=io.StringIO(), clock=lambda: FIXED)
    rep.dead_snapshot("paris", temp=20, running_max=25, ask=None, bid=None,
                      depth=0.0, ask_pre=None)
    rec = json.loads((tmp_path / "paris" / "2026-06-08" / "dead.jsonl").read_text())
    assert rec["ask"] is None and rec["bid"] is None and rec["ask_pre"] is None


def test_price_sample_written_and_console_pure(tmp_path):
    rep = Reporter(str(tmp_path), stdout=io.StringIO(), clock=lambda: FIXED)
    rep.price_sample("paris", temp=24, running_max=24, ask=0.812345,
                     bid=0.79, obs_age_sec=137.44)
    rec = json.loads((tmp_path / "paris" / "2026-06-08" / "prices.jsonl").read_text())
    assert rec["temp"] == 24 and rec["running_max"] == 24
    assert rec["ask"] == 0.8123 and rec["obs_age_sec"] == 137.4  # rounded to keep it tiny
    assert rep._stdout.getvalue() == ""  # telemetry only, never the trade stream


def test_buy_logs_order_record(tmp_path):
    rep = Reporter(str(tmp_path), stdout=io.StringIO(), clock=lambda: FIXED)
    rep.buy(Fill(city="paris", temp=24, side="buy", price=0.97, size=10, fee=0.1, reason="dead<rmax"))
    orders = (tmp_path / "paris" / "2026-06-08" / "orders.jsonl").read_text().splitlines()
    rec = json.loads(orders[0])
    assert rec["side"] == "buy" and rec["size"] == 10 and rec["reason"] == "dead<rmax"
