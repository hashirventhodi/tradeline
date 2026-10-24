"""Indicators over the Adjusted Series: simple averages, true range, trailing return.

Each takes bars oldest first and computes on the last bars given, in
``Decimal`` so a value is the same on every machine.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from tradeline.adjust import AdjustedBar


def _close(bar: AdjustedBar) -> Decimal:
    if bar.close is None:
        raise ValueError(f"{bar.symbol} has no close on {bar.trade_date}")
    return bar.close


def sma(bars: Sequence[AdjustedBar], length: int) -> Decimal:
    """The simple average of the last ``length`` closes."""
    if len(bars) < length:
        raise ValueError(f"SMA{length} needs {length} bars, given {len(bars)}")
    return sum((_close(b) for b in bars[-length:]), Decimal(0)) / length


def true_range(bar: AdjustedBar, previous_close: Decimal) -> Decimal:
    if bar.high is None or bar.low is None:
        raise ValueError(f"{bar.symbol} has no high or low on {bar.trade_date}")
    return max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close))


def atr(bars: Sequence[AdjustedBar], length: int) -> Decimal:
    """The simple average of the last ``length`` true ranges (each needs the close before)."""
    if len(bars) < length + 1:
        raise ValueError(f"ATR({length}) needs {length + 1} bars, given {len(bars)}")
    window = bars[-(length + 1) :]
    ranges = (
        true_range(bar, _close(before)) for before, bar in zip(window[:-1], window[1:], strict=True)
    )
    return sum(ranges, Decimal(0)) / length


def trailing_return(bars: Sequence[AdjustedBar], length: int) -> Decimal:
    """The return over the last ``length`` trading days: the last close over the close
    ``length`` bars before it, less one."""
    if len(bars) < length + 1:
        raise ValueError(f"a {length}-day return needs {length + 1} bars, given {len(bars)}")
    return _close(bars[-1]) / _close(bars[-(length + 1)]) - 1
