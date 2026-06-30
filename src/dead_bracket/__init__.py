"""Dead-Bracket NO capture bot.

A fresh, self-contained strategy for Polymarket daily "highest temperature"
markets. The day's high is monotonically non-decreasing, so once the confirmed
running-max temperature (from the resolution-grade METAR feed) passes an integer
bracket, that bracket can never be the high and its NO is certain to settle at $1.

This package buys such "dead" NO tokens when offered below a hard ceiling (CAP),
then immediately rests a 99.9c maker sell to recycle capital. Default mode is
paper; live signed execution is gated behind ``--live``.
"""

__version__ = "0.1.0"
