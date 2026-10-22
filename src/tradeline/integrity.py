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

**Large Moves.** Every Instrument ever in a stored Universe month, under any
rule version, is an ever-member. Its Adjusted Series in the range, as of the
range's end, is read in its listing series (EQ, BE, BZ, SM, ST) together, by
day: a close more than ``large_move_ratio`` times the previous trading day's
close in any of them, either way, is a Large Move, so a series move neither
hides a move nor starts a new one. A day with rows in two listing series is
read in the first of ``MOVE_SERIES`` it has (EQ, then BE, BZ, SM, ST), so a
day's rows are never compared with each other. The first day in the range
has no previous day.
A split or bonus of the Instrument with that Ex-Date explains it, and so does
a row of the Large Move records (``tradeline.large_move_records``) whose
Symbol named the Instrument that day. Any other fails the check (an
unexplained Large Move may be a split or bonus the Adjusted Series lacks).
A record in the range whose Symbol named no Instrument that day, or that
matches no Large Move, is reported and does not fail. So is a second record
whose Symbol named the same Instrument on the same day as an earlier one
(the file refuses one Symbol and day twice, but two Symbols can name one
Instrument after a rename): only the first is read, since which names it
depends on the Instruments' last rebuild, not on the file alone. A member whose Symbol
names no Instrument (the Instruments were not rebuilt) fails the check, since
its moves cannot be checked.

**Gaps.** Days the Trading Calendar says NSE was open, between an
ever-member's first and last row in the range, on which it has no row in any
listing series. NSE's file is whole on those days, so a gap is NSE's own
record (a suspension, mostly): counted and listed, never a failure.

The report lists each failure, as text and as a machine-readable summary with
the same data. Nothing is written: the check is a read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, get_args

import psycopg

from tradeline import queries
from tradeline.adjust import ADJUSTED_KINDS, AdjustedBar
from tradeline.config import Config
from tradeline.instruments import LISTING_SERIES
from tradeline.large_move_records import LargeMoveRecord, load_large_move_records
from tradeline.trading_calendar import TradingCalendar, is_weekday, load_trading_calendar

DayFailureKind = Literal[
    "missing", "special_session_missing", "holiday_with_file", "unlisted_weekend_file"
]
DAY_FAILURE_KINDS: tuple[DayFailureKind, ...] = get_args(DayFailureKind)
# Every failure is counted; this many are listed in a summary.
MAX_LISTED = 100
# The listing series (``LISTING_SERIES``), in the order a day's close is read for Large
# Moves: of a day's priced rows, the first of these it has.
MOVE_SERIES = ("EQ", "BE", "BZ", "SM", "ST")


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
class LargeMove:
    """A close more than the Large Move ratio from the previous row's close, either way."""

    instrument: str
    # The Symbol and series of the row that moved, as NSE published it.
    symbol: str
    series: str
    trade_date: date
    # Both closes in the Adjusted Series as of the check's end date.
    previous_close: Decimal
    close: Decimal
    # What explains it: ``split`` or ``bonus`` (an action with that Ex-Date),
    # or a Large Move record's reason. None when nothing does: a failure.
    explained_by: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "symbol": self.symbol,
            "series": self.series,
            "trade_date": self.trade_date.isoformat(),
            "previous_close": str(self.previous_close),
            "close": str(self.close),
            "explained_by": self.explained_by,
        }

    def text(self) -> str:
        return (
            f"  large move {self.trade_date} {self.instrument} ({self.symbol} {self.series}):"
            f" {self.previous_close} to {self.close}"
        )


@dataclass(frozen=True)
class Gap:
    """A day NSE was open, inside an ever-member's rows in the range, with no row of it."""

    instrument: str
    trade_date: date

    def summary(self) -> dict[str, Any]:
        return {"instrument": self.instrument, "trade_date": self.trade_date.isoformat()}


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
    # The ever-members' Instruments checked for Large Moves and gaps.
    ever_members: int
    # Ever-member Symbols no Instrument answers for: each a failure.
    unresolved_members: tuple[str, ...]
    # Every Large Move of an ever-member in the range, explained or not, by day.
    large_moves: tuple[LargeMove, ...]
    # Records in the range whose Symbol named no Instrument that day, and
    # records that match no Large Move: reported, never failures.
    unresolved_records: tuple[LargeMoveRecord, ...]
    unmatched_records: tuple[LargeMoveRecord, ...]
    # Records naming the Instrument and day of an earlier one: reported, never read.
    duplicate_records: tuple[LargeMoveRecord, ...]
    # Never failures.
    gaps: tuple[Gap, ...]

    @property
    def unexplained_large_moves(self) -> tuple[LargeMove, ...]:
        return tuple(m for m in self.large_moves if m.explained_by is None)

    @property
    def passed(self) -> bool:
        return (
            not self.day_failures
            and not self.uncovered
            and not self.unresolved_members
            and not self.unexplained_large_moves
        )

    def summary(self) -> dict[str, Any]:
        """The machine-readable report: the same data as the text."""
        counts = Counter(f.kind for f in self.day_failures)
        unexplained = self.unexplained_large_moves
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
            "ever_members": self.ever_members,
            "unresolved_members": list(self.unresolved_members),
            "large_moves": len(self.large_moves),
            "large_moves_explained": dict(
                sorted(Counter(m.explained_by for m in self.large_moves if m.explained_by).items())
            ),
            "unexplained_large_moves": len(unexplained),
            "listed_unexplained_large_moves": [m.summary() for m in unexplained[:MAX_LISTED]],
            "unresolved_records": [_record_summary(r) for r in self.unresolved_records],
            "unmatched_records": [_record_summary(r) for r in self.unmatched_records],
            "duplicate_records": [_record_summary(r) for r in self.duplicate_records],
            "gaps": len(self.gaps),
            "gaps_by_instrument": dict(sorted(Counter(g.instrument for g in self.gaps).items())),
            "listed_gaps": [g.summary() for g in self.gaps[:MAX_LISTED]],
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
        lines += _more(self.day_failures)
        unexplained = self.unexplained_large_moves
        explained = len(self.large_moves) - len(unexplained)
        lines.append(
            f"Large Moves of {self.ever_members} ever-members: {len(self.large_moves)} found,"
            f" {explained} explained, {len(unexplained)} unexplained"
        )
        lines += [
            f"  unresolved member {symbol}: names no Instrument; run `tradeline"
            " rebuild-instruments`"
            for symbol in self.unresolved_members
        ]
        lines += [m.text() for m in unexplained[:MAX_LISTED]]
        lines += _more(unexplained)
        lines += [
            f"  record line {r.line} ({r.symbol} {r.trade_date}): {r.symbol} named no Instrument"
            for r in self.unresolved_records
        ]
        lines += [
            f"  record line {r.line} ({r.symbol} {r.trade_date}): matches no Large Move"
            for r in self.unmatched_records
        ]
        lines += [
            f"  record line {r.line} ({r.symbol} {r.trade_date}): names the Instrument and day"
            " of an earlier record; not read"
            for r in self.duplicate_records
        ]
        lines.append(f"Gaps (never failures): {len(self.gaps)}")
        lines += [f"  gap {g.trade_date} {g.instrument}" for g in self.gaps[:MAX_LISTED]]
        lines += _more(self.gaps)
        return "\n".join(lines) + "\n"


def _more(listed: tuple[object, ...]) -> list[str]:
    return [f"  ... and {len(listed) - MAX_LISTED} more"] if len(listed) > MAX_LISTED else []


def _record_summary(record: LargeMoveRecord) -> dict[str, Any]:
    return {
        "line": record.line,
        "symbol": record.symbol,
        "trade_date": record.trade_date.isoformat(),
        "reason": record.reason,
    }


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
    records = load_large_move_records(Path(settings.large_moves))
    members = queries.universe_ever_members(conn)
    instruments = sorted({m.instrument for m in members if m.instrument is not None})
    unresolved_members = tuple(sorted({m.symbol for m in members if m.instrument is None}))
    ratio = Decimal(str(settings.large_move_ratio))
    moves: list[LargeMove] = []
    gaps: list[Gap] = []
    for instrument in instruments:
        bars = [
            bar
            for bar in queries.adjusted_daily_candles(
                conn, instrument, start=start, end=end, as_of=end
            )
            if bar.series in LISTING_SERIES
        ]
        moves += _large_moves(conn, instrument, bars, ratio=ratio, end=end)
        gaps += _gaps(calendar, instrument, bars, covered)
    resolved, unresolved_records, duplicate_records = _resolve(conn, records, start=start, end=end)
    explained = _explain(moves, resolved)
    matched = {(m.instrument, m.trade_date) for m in moves}
    return IntegrityReport(
        start=start,
        end=end,
        calendar_first=calendar.first,
        calendar_last=calendar.last,
        trading_days=trading_days,
        day_failures=day_failures,
        uncovered=uncovered,
        ever_members=len(instruments),
        unresolved_members=unresolved_members,
        large_moves=tuple(sorted(explained, key=lambda m: (m.trade_date, m.instrument))),
        unresolved_records=unresolved_records,
        unmatched_records=tuple(r for key, r in resolved.items() if key not in matched),
        duplicate_records=duplicate_records,
        gaps=tuple(sorted(gaps, key=lambda g: (g.trade_date, g.instrument))),
    )


def _large_moves(
    conn: psycopg.Connection,
    instrument: str,
    bars: list[AdjustedBar],
    *,
    ratio: Decimal,
    end: date,
) -> list[LargeMove]:
    """The Instrument's Large Moves, each explained by a split or bonus on its day if one is."""
    actions = {
        a.ex_date: a.kind
        for a in queries.corporate_actions(conn, instrument, end=end)
        if a.kind in ADJUSTED_KINDS
    }
    # A row with no close, or a zero one, has no move.
    priced = _one_per_day([b for b in bars if b.close])
    moves = []
    for previous, bar in zip(priced, priced[1:], strict=False):
        assert previous.close is not None and bar.close is not None
        if bar.close > previous.close * ratio or previous.close > bar.close * ratio:
            moves.append(
                LargeMove(
                    instrument,
                    bar.symbol,
                    bar.series,
                    bar.trade_date,
                    previous.close,
                    bar.close,
                    actions.get(bar.trade_date),
                )
            )
    return moves


def _one_per_day(bars: list[AdjustedBar]) -> list[AdjustedBar]:
    """Each day's row in the first of ``MOVE_SERIES`` the day has, oldest first."""
    by_day: dict[date, AdjustedBar] = {}
    for bar in bars:
        kept = by_day.get(bar.trade_date)
        if kept is None or MOVE_SERIES.index(bar.series) < MOVE_SERIES.index(kept.series):
            by_day[bar.trade_date] = bar
    return [by_day[day] for day in sorted(by_day)]


def _gaps(
    calendar: TradingCalendar, instrument: str, bars: list[AdjustedBar], covered: Span
) -> list[Gap]:
    if not bars:
        return []
    first, last = max(bars[0].trade_date, covered.start), min(bars[-1].trade_date, covered.end)
    if first > last:
        return []
    held = {b.trade_date for b in bars}
    return [Gap(instrument, day) for day in calendar.trading_days(first, last) if day not in held]


def _resolve(
    conn: psycopg.Connection, records: Iterable[LargeMoveRecord], *, start: date, end: date
) -> tuple[
    dict[tuple[str, date], LargeMoveRecord],
    tuple[LargeMoveRecord, ...],
    tuple[LargeMoveRecord, ...],
]:
    """The records in the range by (the Instrument their Symbol named that day, day);
    those whose Symbol named none; and those keyed like an earlier one, which is kept."""
    resolved: dict[tuple[str, date], LargeMoveRecord] = {}
    unresolved = []
    duplicates = []
    for record in records:
        if not start <= record.trade_date <= end:
            continue
        try:
            named = queries.resolve_symbol(conn, record.symbol, as_of=record.trade_date)
        except queries.NoInstrument:
            unresolved.append(record)
            continue
        key = (named.key, record.trade_date)
        if key in resolved:
            duplicates.append(record)
            continue
        resolved[key] = record
    return resolved, tuple(unresolved), tuple(duplicates)


def _explain(
    moves: Iterable[LargeMove], resolved: dict[tuple[str, date], LargeMoveRecord]
) -> list[LargeMove]:
    """The moves no split or bonus explains, explained by their record where there is one."""
    explained = []
    for move in moves:
        record = resolved.get((move.instrument, move.trade_date))
        if move.explained_by is None and record is not None:
            move = replace(move, explained_by=record.reason)
        explained.append(move)
    return explained


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
