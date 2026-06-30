"""Fee and net-margin math (spec §2).

    net(a) = (1 - a) - fee(a)
    fee(a) = a * FEE_RATE * (a * (1 - a)) ** FEE_EXPONENT   # weather params

``fee(a) = 0`` if the market's ``feesEnabled`` is false. Makers pay no fee, so
the resting 99.9c exit is always fee-free; only the taker entry buy incurs a fee.
"""

from __future__ import annotations


def fee(
    a: float,
    *,
    fee_rate: float,
    fee_exponent: float,
    fees_enabled: bool = True,
) -> float:
    """Taker fee per share for buying NO at ask ``a``.

    Returns 0.0 when the market has fees disabled. The formula is only
    meaningful for ``a`` in [0, 1]; callers gate prices before this point.
    """

    if not fees_enabled:
        return 0.0
    return a * fee_rate * (a * (1.0 - a)) ** fee_exponent


def net(
    a: float,
    *,
    fee_rate: float,
    fee_exponent: float,
    fees_enabled: bool = True,
) -> float:
    """Net margin per share from buying NO at ask ``a`` and holding to $1."""

    return (1.0 - a) - fee(a, fee_rate=fee_rate, fee_exponent=fee_exponent, fees_enabled=fees_enabled)
