"""The Trading Calendar: a checked-in file of the days NSE was closed and the sessions it added.

NSE's end-of-day files cannot tell a holiday from a lost day, so the days NSE
was open for equity trading are stated in a CSV file in the repository, at the
location ``integrity.trading_calendar`` in ``tradeline.toml`` configures, each
row reviewed like code (ADR 0018). Every weekday is a trading day except a
listed holiday, and every listed Special Session is one too::

    covers,2015-01-01,2026-12-31
    date,kind,citation
    2024-08-15,holiday,NSE/CMTR/60310
    2024-11-01,special_session,https://nsearchives.nseindia.com/content/circulars/CMTR64476.pdf

The first line states the first and last dates the calendar covers: a check
of any day outside them fails rather than trusting a calendar that is short.
Then the header, then one row per day. ``date`` is ISO (``YYYY-MM-DD``),
``kind`` is ``holiday`` or ``special_session``, and ``citation`` is NSE's
circular announcing it, by URL or number. Lines starting with ``#`` are
comments, and blank lines are skipped.

A malformed row (the wrong number of columns, an unparseable date), an unknown
kind, a row without a citation, a holiday on a weekend, a day listed twice,
or a day outside the stated coverage refuses the whole file, naming the line,
so a check never runs on part of it, as with the manual links. One loader
serves the Integrity Check and Kite Parity, which reads the Special Sessions.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, get_args

from tradeline.dates import calendar_days
from tradeline.reviewed_csv import FirstLines, Line, parse_date, read_lines, require_header

CalendarKind = Literal["holiday", "special_session"]
KINDS: tuple[CalendarKind, ...] = get_args(CalendarKind)
COLUMNS = ("date", "kind", "citation")
COVERS = "covers"
SATURDAY = 5


@dataclass(frozen=True)
class CalendarDay:
    """A reviewed row: a weekday NSE was closed, or a session it held outside its normal days."""

    trade_date: date
    kind: CalendarKind
    citation: str
    # The row's line in the file, for naming it in a report.
    line: int


@dataclass(frozen=True)
class TradingCalendar:
    # The first and last dates the calendar covers, inclusive.
    first: date
    last: date
    days: tuple[CalendarDay, ...]

    def covers(self, day: date) -> bool:
        return self.first <= day <= self.last

    @property
    def holidays(self) -> frozenset[date]:
        return frozenset(d.trade_date for d in self.days if d.kind == "holiday")

    @property
    def special_sessions(self) -> frozenset[date]:
        return frozenset(d.trade_date for d in self.days if d.kind == "special_session")

    def trading_days(self, start: date, end: date) -> Iterator[date]:
        """The days NSE was open from ``start`` to ``end``, both inside the coverage."""
        if not (self.covers(start) and self.covers(end)):
            raise ValueError(
                f"{start} to {end} runs outside the Trading Calendar's {self.first} to {self.last}"
            )
        holidays, sessions = self.holidays, self.special_sessions
        for day in calendar_days(start, end):
            if day in sessions or (is_weekday(day) and day not in holidays):
                yield day


def is_weekday(day: date) -> bool:
    return day.weekday() < SATURDAY


def load_trading_calendar(path: Path) -> TradingCalendar:
    """The Trading Calendar file at ``path``; any bad row refuses the file."""
    lines = read_lines(path, "Trading Calendar")
    if not lines:
        raise ValueError(f"{path}: the first row must be {COVERS},FIRST_DATE,LAST_DATE")
    first, last = _covers(lines[0])
    require_header(path, lines[1] if len(lines) > 1 else None, COLUMNS, "second")
    days: list[CalendarDay] = []
    listed = FirstLines[date]()
    for line in lines[2:]:
        day = _row(line)
        where = f"{line.where}: {day.trade_date}"
        if not first <= day.trade_date <= last:
            raise ValueError(f"{where} is outside the calendar's coverage, {first} to {last}")
        listed.add(day.trade_date, line, f"{day.trade_date} is already listed")
        days.append(day)
    return TradingCalendar(first, last, tuple(days))


def _covers(line: Line) -> tuple[date, date]:
    fields = line.fields()
    if len(fields) != 3 or fields[0] != COVERS:
        raise ValueError(
            f"{line.where}: the first row must be {COVERS},FIRST_DATE,LAST_DATE: {line.text}"
        )
    first, last = (parse_date(line.where, "covers", text) for text in fields[1:])
    if first > last:
        raise ValueError(
            f"{line.where}: the calendar's first date {first} is after its last {last}"
        )
    return first, last


def _row(line: Line) -> CalendarDay:
    text, kind, citation = line.columns(COLUMNS)
    trade_date = parse_date(line.where, "date", text)
    where = f"{line.where}: {trade_date}"
    if kind not in KINDS:
        raise ValueError(f"{where}: kind {kind!r} is not one of {', '.join(KINDS)}")
    if not citation:
        raise ValueError(f"{where}: no citation; cite NSE's circular by URL or number")
    if kind == "holiday" and not is_weekday(trade_date):
        raise ValueError(f"{where}: a holiday on a {trade_date:%A}; only weekdays are listed")
    return CalendarDay(trade_date, kind, citation, line.number)
