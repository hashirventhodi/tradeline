"""Indicator arithmetic on fixed, hand-worked series."""

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from tradeline.adjust import AdjustedBar
from tradeline.strategy.indicators import atr, sma, trailing_return


def bars(*ohlc: tuple[str, str, str, str]) -> list[AdjustedBar]:
    start = date(2024, 1, 1)
    return [
        AdjustedBar(
            "X",
            "EQ",
            start + timedelta(days=i),
            Decimal(o),
            Decimal(h),
            Decimal(low),
            Decimal(c),
            100,
            Fraction(1),
            (),
        )
        for i, (o, h, low, c) in enumerate(ohlc)
    ]


def closes(*values: str) -> list[AdjustedBar]:
    return bars(*((v, v, v, v) for v in values))


def test_sma_averages_the_last_closes_only() -> None:
    assert sma(closes("1", "10", "20", "30"), 3) == Decimal(20)


def test_sma_refuses_fewer_bars_than_its_length() -> None:
    with pytest.raises(ValueError, match="SMA3 needs 3 bars"):
        sma(closes("1", "2"), 3)


def test_atr_takes_the_gap_from_the_previous_close_into_the_true_range() -> None:
    series = bars(
        ("100", "101", "99", "100"),
        ("100", "102", "99", "101"),  # range 3
        ("95", "96", "94", "95"),  # gapped down: 101 - 94 = 7 beats the range of 2
        ("95", "97", "96", "96"),  # range 1, but 97 - 95 = 2
    )

    assert atr(series, 3) == Decimal(12) / 3


def test_trailing_return_compares_the_last_close_with_the_one_length_bars_before() -> None:
    assert trailing_return(closes("50", "80", "100", "120"), 2) == Decimal("0.5")
