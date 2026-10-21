"""Indian time, calendar days, and months, as every module counts them.

NSE, BSE, and Kite stamp everything in Indian time, and a trading day is an
Indian calendar day. A month is given by its first day throughout (a Universe
month, a minute-candle partition, a ``--month`` argument).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def calendar_days(start: date, end: date) -> Iterator[date]:
    """Every calendar day from ``start`` to ``end`` inclusive."""
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def month_of(day: date) -> date:
    """The month ``day`` falls in, as its first day."""
    return day.replace(day=1)


def next_month(month: date) -> date:
    """The month after the one ``month`` falls in, as its first day."""
    return month_of(month.replace(day=28) + timedelta(days=4))


def month_end(month: date) -> date:
    """The last day of the month ``month`` falls in."""
    return next_month(month) - timedelta(days=1)


def parse_month(text: str) -> date:
    """A month written ``YYYY-MM``, as its first day."""
    return datetime.strptime(text, "%Y-%m").date()
