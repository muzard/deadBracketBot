"""Tick-grid helpers (spec §7 live order constraints)."""

from __future__ import annotations

import pytest

from dead_bracket.models import max_price_for_tick, round_to_tick


def test_max_price_for_tick():
    assert max_price_for_tick(0.01) == 0.99
    assert max_price_for_tick(0.001) == 0.999
    assert max_price_for_tick(0.0001) == 0.9999


def test_round_to_tick():
    assert round_to_tick(0.98, 0.01) == 0.98
    assert round_to_tick(0.999, 0.001) == 0.999
    assert round_to_tick(0.983, 0.01) == 0.98  # nearest 0.01
    assert round_to_tick(0.987, 0.01) == 0.99
    assert round_to_tick(0.5, 0.0) == 0.5  # degenerate tick -> unchanged
