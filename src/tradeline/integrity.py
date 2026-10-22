"""``integrity``: the Integrity Check of NSE's data as we hold it, against NSE alone (ADR 0018).

It never reads a broker's data: a broker's conventions can neither fail nor
pass the soundness of our own.

**The file check.** Every day the Trading Calendar (``tradeline.trading_calendar``)
says NSE was open must have its end-of-day file, and no other day may have
one. A day fails the check when:

- **missing**: a weekday that is not a listed holiday has no file;
- **special_session_missing**: a listed Special Session has no file;
- **holiday_with_file**: a listed holiday has a file, so the calendar's row is wrong;
- **unlisted_weekend_file**: a weekend day has a file and is not a listed
  Special Session, so the calendar is incomplete.

The checked range must lie inside the dates the calendar covers. Whatever
part of it runs outside is a failure of its own (**uncovered**), and only the
covered part is checked day by day, so a check never passes on a calendar
that is silently short.

The report lists each failure, as text and as a machine-readable summary with
the same data. Nothing is written: the check is a read.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, get_args

import psycopg

from tradeline import queries
from tradeline.config import Config
from tradeline.trading_calendar import TradingCalendar, is_weekday, load_trading_calendar

DayFailureKind = Literal[
    "missing", "special_session_missing", "holiday_with_file", "unlisted_weekend_file"
]
DAY_FAILURE_KINDS: tuple[DayFailureKind, ...] = get_args(DayFailureKind)
# Every failure is counted; this many are listed in a summary.
MAX_LISTED = 100


@dataclass(frozen=True)
class DayFailure:
    """A day whose end-of-day file is missing where NSE was open, or present where it was not."""

    kind: DayFailureKind
    trade_date: date
    # The calendar row's citation, for a listed holiday or Special Session.
    citation: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "trade_date": self.trade_date.isoformat(),
            "citation": self.citation,
        }

    def text(self) -> str:
        cited = "" if self.citation is None else f" (cited {self.citation})"
        return f"  {self.kind:<24} {self.trade_date} {self.trade_date:%a}{cited}"


@dataclass(frozen=True)
class Span:
    """Days ``start`` to ``end`` inclusive."""

    start: date
    end: date

    def summary(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class IntegrityReport:
    start: date
    end: date
    # The dates the Trading Calendar covers.
    calendar_first: date
    calendar_last: date
    # Days the calendar says NSE was open, in the covered part of the range.
    trading_days: int
    day_failures: tuple[DayFailure, ...]
    # The parts of the range outside the calendar's coverage: each a failure.
    uncovered: tuple[Span, ...]

    @property
    def passed(self) -> bool:
        return not self.day_failures and not self.uncovered

    def summary(self) -> dict[str, Any]:
        """The machine-readable report: the same data as the text."""
        counts = Counter(f.kind for f in self.day_failures)
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "calendar_first": self.calendar_first.isoformat(),
            "calendar_last": self.calendar_last.isoformat(),
            "trading_days": self.trading_days,
            "uncovered": [s.summary() for s in self.uncovered],
            "day_failures": len(self.day_failures),
            **{kind: counts[kind] for kind in DAY_FAILURE_KINDS},
            "listed_day_failures": [f.summary() for f in self.day_failures[:MAX_LISTED]],
            "passed": self.passed,
        }

    def text(self) -> str:
        """The human-readable report."""
        counts = Counter(f.kind for f in self.day_failures)
        lines = [
            f"integrity {self.start} to {self.end}: {'PASS' if self.passed else 'FAIL'}",
            f"Trading Calendar {self.calendar_first} to {self.calendar_last}:"
            f" {self.trading_days} trading days checked, {len(self.day_failures)} day failures"
            f" ({', '.join(f'{counts[kind]} {kind}' for kind in DAY_FAILURE_KINDS)})",
        ]
        lines += [
            f"  uncovered {s.start} to {s.end}: outside the Trading Calendar"
            for s in self.uncovered
        ]
        lines += [f.text() for f in self.day_failures[:MAX_LISTED]]
        if len(self.day_failures) > MAX_LISTED:
            lines.append(f"  ... and {len(self.day_failures) - MAX_LISTED} more")
        return "\n".join(lines) + "\n"


def integrity(
    conn: psycopg.Connection, settings: Config.Integrity, *, start: date, end: date
) -> IntegrityReport:
    """Run the Integrity Check from ``start`` to ``end`` inclusive; a read."""
    if end < start:
        raise ValueError(f"the range ends ({end}) before it starts ({start})")
    calendar = load_trading_calendar(Path(settings.trading_calendar))
    uncovered = _uncovered(calendar, start, end)
    covered = Span(max(start, calendar.first), min(end, calendar.last))
    day_failures: tuple[DayFailure, ...] = ()
    trading_days = 0
    if covered.start <= covered.end:
        open_days = list(calendar.trading_days(covered.start, covered.end))
        trading_days = len(open_days)
        with_file = queries.end_of_day_dates(conn, start=covered.start, end=covered.end)
        day_failures = _day_failures(calendar, open_days, with_file)
    return IntegrityReport(
        start=start,
        end=end,
        calendar_first=calendar.first,
        calendar_last=calendar.last,
        trading_days=trading_days,
        day_failures=day_failures,
        uncovered=uncovered,
    )


def _uncovered(calendar: TradingCalendar, start: date, end: date) -> tuple[Span, ...]:
    spans = []
    if start < calendar.first:
        spans.append(Span(start, min(end, calendar.first - timedelta(days=1))))
    if end > calendar.last:
        spans.append(Span(max(start, calendar.last + timedelta(days=1)), end))
    return tuple(spans)


def _day_failures(
    calendar: TradingCalendar, open_days: list[date], with_file: list[date]
) -> tuple[DayFailure, ...]:
    cited = {d.trade_date: d.citation for d in calendar.days}
    sessions, holidays = calendar.special_sessions, calendar.holidays
    has_file, is_open = set(with_file), set(open_days)
    failures = [
        DayFailure("special_session_missing", day, cited[day])
        if day in sessions
        else DayFailure("missing", day)
        for day in open_days
        if day not in has_file
    ]
    for day in with_file:
        if day in is_open:
            continue
        if day in holidays:
            failures.append(DayFailure("holiday_with_file", day, cited[day]))
        elif not is_weekday(day):
            failures.append(DayFailure("unlisted_weekend_file", day))
    return tuple(sorted(failures, key=lambda f: f.trade_date))
