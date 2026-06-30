"""Fee / net-margin tests (spec §2, §12)."""

from __future__ import annotations

import math

import pytest

from dead_bracket.fees import fee, net

RATE = 0.025
EXP = 0.5


def test_fee_matches_formula():
    a = 0.97
    expected = a * RATE * (a * (1 - a)) ** EXP
    assert fee(a, fee_rate=RATE, fee_exponent=EXP) == pytest.approx(expected)


def test_net_matches_formula():
    a = 0.97
    expected = (1 - a) - a * RATE * (a * (1 - a)) ** EXP
    assert net(a, fee_rate=RATE, fee_exponent=EXP) == pytest.approx(expected)


def test_fees_disabled_is_zero_fee():
    a = 0.95
    assert fee(a, fee_rate=RATE, fee_exponent=EXP, fees_enabled=False) == 0.0
    assert net(a, fee_rate=RATE, fee_exponent=EXP, fees_enabled=False) == pytest.approx(1 - a)


def test_gate_around_min_net():
    # With fees disabled, net(a) = 1 - a, so MIN_NET=0.012 gates at a = 0.988.
    min_net = 0.012
    assert net(0.985, fee_rate=RATE, fee_exponent=EXP, fees_enabled=False) >= min_net
    assert net(0.99, fee_rate=RATE, fee_exponent=EXP, fees_enabled=False) < min_net


def test_fee_reduces_net_below_face():
    a = 0.97
    assert net(a, fee_rate=RATE, fee_exponent=EXP, fees_enabled=True) < (1 - a)
