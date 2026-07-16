"""LiveExecutor order plumbing & reconciliation, against a fake SecureClient.

No network: a fake of the unified ``polymarket-client`` SDK returns real
response models so the live orchestration (limit placement, IOC-emulation
cancel, post-only exit, fill parsing, resting-sell reconciliation, risk updates)
is exercised deterministically.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("polymarket")  # live dep; skip cleanly if absent

from polymarket.models.clob.order_response import AcceptedOrder, RejectedOrder

from dead_bracket.executor import LiveExecutor, OrderError, make_executor
from dead_bracket.models import Bracket, Intent
from dead_bracket.risk import RiskManager
from conftest import make_bracket, make_snapshot


def accepted(order_id="o1", status="matched", making="0", taking="0"):
    return AcceptedOrder(ok=True, order_id=order_id, status=status,
                         making_amount=Decimal(making), taking_amount=Decimal(taking),
                         trade_ids=(), transactions_hashes=())


def rejected(code="not_enough_balance", message="not enough balance"):
    return RejectedOrder(ok=False, code=code, message=message)


class FakeClient:
    """Minimal stand-in for polymarket.SecureClient."""

    def __init__(self, *, responses=None, order_status=None):
        self.responses = list(responses or [])
        self.order_status = order_status or {}  # order_id -> [size_matched, ...]
        self.placed = []  # kwargs of each place_limit_order call
        self.cancelled = []

    def place_limit_order(self, **kwargs):
        self.placed.append(kwargs)
        return self.responses.pop(0)

    def cancel_order(self, *, order_id):
        self.cancelled.append(order_id)
        return SimpleNamespace(canceled=(order_id,), not_canceled={})

    def get_order(self, *, order_id):
        seq = self.order_status.get(order_id, [0.0])
        matched = seq.pop(0) if len(seq) > 1 else seq[0]
        return SimpleNamespace(size_matched=Decimal(str(matched)))


def live_cfg(config):
    return config.with_mode("live")


def buy_intent(temp=22, size=50, px=0.98, token="tok-22"):
    return Intent(city="paris", temp=temp, side="buy", limit_px=px, size=size,
                  tif="IOC", reason="dead<rmax", token_id=token)


def sell_intent(size=100, px=0.999, token="tok-22"):
    return Intent(city="paris", temp=22, side="sell", limit_px=px, size=size,
                  tif="GTC", reason="recycle", token_id=token)


def test_entry_parses_fill_and_updates_risk(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[accepted(status="matched", making="48.0", taking="50")])
    ex = LiveExecutor(live_cfg(config), risk, client)
    fill = ex.execute_entry(buy_intent(), make_bracket(22))
    assert fill is not None
    assert fill.size == 50
    assert fill.price == pytest.approx(0.96)
    assert risk.bracket_shares("paris", 22) == 50
    assert client.placed[0]["side"] == "BUY"
    assert client.placed[0]["post_only"] is False
    assert client.cancelled == []  # fully matched -> nothing to cancel


def test_entry_partial_match_cancels_remainder(config):
    # 30 of 50 crossed immediately; the resting remainder must be cancelled
    # (IOC emulation: a buy never rests).
    risk = RiskManager(config)
    client = FakeClient(responses=[accepted(order_id="o9", status="live",
                                            making="28.5", taking="30")])
    ex = LiveExecutor(live_cfg(config), risk, client)
    fill = ex.execute_entry(buy_intent(size=50), make_bracket(22))
    assert fill.size == 30
    assert client.cancelled == ["o9"]


def test_entry_no_fill_cancels_and_returns_none(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[accepted(order_id="o2", status="live")])
    ex = LiveExecutor(live_cfg(config), risk, client)
    assert ex.execute_entry(buy_intent(), make_bracket(22)) is None
    assert client.cancelled == ["o2"]


def test_entry_implausible_price_rejected(config):
    # making/taking implies avg 2.0 -> implausible -> treated as no fill.
    risk = RiskManager(config)
    client = FakeClient(responses=[accepted(status="matched", making="20", taking="10")])
    ex = LiveExecutor(live_cfg(config), risk, client)
    assert ex.execute_entry(buy_intent(size=10), make_bracket(22)) is None


def test_rejected_order_raises(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[rejected()])
    ex = LiveExecutor(live_cfg(config), risk, client)
    with pytest.raises(OrderError):
        ex.execute_entry(buy_intent(), make_bracket(22))


def test_entry_without_token_raises(config):
    risk = RiskManager(config)
    ex = LiveExecutor(live_cfg(config), risk, FakeClient())
    with pytest.raises(OrderError):
        ex.execute_entry(buy_intent(token=None), Bracket(temp=22))  # token_id defaults None


def test_entry_price_clamped_to_tick(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[accepted(status="matched", making="49", taking="50")])
    ex = LiveExecutor(live_cfg(config), risk, client)
    intent = Intent(city="paris", temp=22, side="buy", limit_px=0.98, size=50,
                    tif="IOC", reason="x", token_id="tok-22", tick_size=0.01)
    ex.execute_entry(intent, make_bracket(22, tick_size=0.01))
    assert client.placed[0]["price"] == 0.98  # on the 0.01 grid, <= 0.99 max


def test_exit_is_post_only_and_clamped(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[
        accepted(status="matched", making="95", taking="100"),  # entry
        accepted(order_id="s1", status="live"),                 # exit
    ])
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    # Coarse 0.01-tick market: 0.999 clamps to 0.99; post_only guarantees maker.
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999, size=100,
                         tif="GTC", reason="recycle", token_id="tok-22", tick_size=0.01))
    exit_call = client.placed[1]
    assert exit_call["side"] == "SELL"
    assert exit_call["post_only"] is True
    assert exit_call["price"] == 0.99


def test_exit_fill_price_at_fine_tick(config):
    """Exits always use 0.001-tick (the CLOB fine tick near $1), so the fill is
    recorded at 0.999 regardless of what the bracket's orderPriceMinTickSize says."""
    risk = RiskManager(config)
    client = FakeClient(
        responses=[
            accepted(status="matched", making="95.0", taking="100"),  # entry @0.95
            accepted(order_id="s1", status="live"),                   # the GTC sell
        ],
        order_status={"s1": [100.0]},
    )
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22, tick_size=0.01))
    # Worker passes tick_size=0.001 for exits; limit_px stays 0.999.
    ex.place_exit(Intent(city="paris", temp=22, side="sell", limit_px=0.999,
                         size=100, tif="GTC", reason="recycle",
                         token_id="tok-22", tick_size=0.001))
    fills = ex.on_book(make_snapshot("paris", [make_bracket(22, tick_size=0.001)]))
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(0.999)
    assert risk.realized_pnl == pytest.approx(100 * 0.999 - 95.0)


def test_exit_then_reconcile_partial_and_complete(config):
    risk = RiskManager(config)
    client = FakeClient(
        responses=[
            accepted(status="matched", making="95.0", taking="100"),  # entry
            accepted(order_id="s1", status="live"),                   # the GTC sell
        ],
        order_status={"s1": [30.0, 100.0]},  # first poll 30 matched, then 100
    )
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    ex.place_exit(sell_intent(size=100))
    assert ex.positions[("paris", 22)].resting_sell == 100

    snap = make_snapshot("paris", [make_bracket(22)])
    first = ex.on_book(snap)
    assert len(first) == 1 and first[0].size == 30 and first[0].price == 0.999
    second = ex.on_book(snap)
    assert len(second) == 1 and second[0].size == 70  # 100 - 30 already booked
    assert ex.positions[("paris", 22)].shares == pytest.approx(0.0)


def test_cancel_exit_cancels_order_and_frees_resting(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[
        accepted(status="matched", making="95.0", taking="100"),  # entry
        accepted(order_id="s1", status="live"),                   # the GTC sell
    ])
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    ex.place_exit(sell_intent(size=100))
    assert ex.positions[("paris", 22)].resting_sell == 100

    ex.cancel_exit("paris", 22)
    assert client.cancelled == ["s1"]  # the resting order was cancelled
    assert ex.positions[("paris", 22)].resting_sell == 0.0  # freed
    assert ex.positions[("paris", 22)].shares == 100  # shares held to $1
    # No resting orders remain; a second cancel is a no-op (no extra API calls).
    ex.cancel_exit("paris", 22)
    assert client.cancelled == ["s1"]


def test_cancel_exit_only_frees_unmatched_size(config):
    # 30 of 100 already matched via on_book; cancelling frees only the open 70.
    risk = RiskManager(config)
    client = FakeClient(
        responses=[
            accepted(status="matched", making="95.0", taking="100"),  # entry
            accepted(order_id="s1", status="live"),                   # the GTC sell
        ],
        order_status={"s1": [30.0]},
    )
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    ex.place_exit(sell_intent(size=100))
    ex.on_book(make_snapshot("paris", [make_bracket(22)]))  # books 30 matched
    assert ex.positions[("paris", 22)].resting_sell == 70

    ex.cancel_exit("paris", 22)
    assert client.cancelled == ["s1"]
    assert ex.positions[("paris", 22)].resting_sell == 0.0
    assert ex.positions[("paris", 22)].shares == pytest.approx(70.0)


def test_cancel_exit_failure_raises_order_error(config):
    class UncancelableClient(FakeClient):
        def cancel_order(self, *, order_id):
            raise RuntimeError("venue down")

    risk = RiskManager(config)
    client = UncancelableClient(responses=[
        accepted(status="matched", making="95.0", taking="100"),
        accepted(order_id="s1", status="live"),
    ])
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    ex.place_exit(sell_intent(size=100))
    with pytest.raises(OrderError):
        ex.cancel_exit("paris", 22)


def test_exit_post_only_would_cross_raises(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[
        accepted(status="matched", making="95", taking="100"),
        rejected(code="post_only_would_cross", message="would cross"),
    ])
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    with pytest.raises(OrderError):
        ex.place_exit(sell_intent(size=100))


def test_settle_reports_then_clears_state_without_realising_pnl(config):
    risk = RiskManager(config)
    client = FakeClient(responses=[
        accepted(status="matched", making="95.0", taking="100"),
        accepted(order_id="s1", status="live")])
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    ex.place_exit(sell_intent(size=100))
    pnl_before = risk.realized_pnl
    results = ex.settle()
    assert len(results) == 1 and results[0].shares == 100
    assert risk.realized_pnl == pnl_before  # settlement is on-chain; not synthesised
    # EOD must clear the executor's local state so the new day starts flat.
    assert ex.positions[("paris", 22)].shares == 0
    assert ex._resting == {}


def test_make_executor_live_returns_live(config):
    risk = RiskManager(config)
    ex = make_executor(live_cfg(config), risk, client=FakeClient())
    assert isinstance(ex, LiveExecutor)


def test_reconcile_status_query_failure_raises(config):
    class FlakyClient(FakeClient):
        def get_order(self, *, order_id):
            raise ConnectionError("clob down")

    risk = RiskManager(config)
    client = FlakyClient(responses=[
        accepted(status="matched", making="95.0", taking="100"),
        accepted(order_id="s1", status="live"),
    ])
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    ex.place_exit(sell_intent(size=100))
    with pytest.raises(OrderError):
        ex.on_book(make_snapshot("paris", [make_bracket(22)]))


def test_reconcile_malformed_status_is_no_progress(config):
    class WeirdClient(FakeClient):
        def get_order(self, *, order_id):
            return SimpleNamespace()  # no size_matched attribute

    risk = RiskManager(config)
    client = WeirdClient(responses=[
        accepted(status="matched", making="95.0", taking="100"),
        accepted(order_id="s1", status="live"),
    ])
    ex = LiveExecutor(live_cfg(config), risk, client)
    ex.execute_entry(buy_intent(size=100), make_bracket(22))
    ex.place_exit(sell_intent(size=100))
    assert ex.on_book(make_snapshot("paris", [make_bracket(22)])) == []
