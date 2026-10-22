"""The Large Move records: a checked-in file explaining the Large Moves no split or bonus does.

A Large Move (ADR 0018) is a close more than ``integrity.large_move_ratio``
times the previous close, either way, in an ever-member's Adjusted Series. One
on a split's or bonus's Ex-Date is explained by it. Any other must be
explained here, in a CSV file at the location ``integrity.large_moves`` in
``tradeline.toml`` configures, each row reviewed like code::

    symbol,trade_date,reason,citation
    ABC,2024-07-01,demerger,<the exchange circular or company filing, by number or URL>
    XYZ,2020-03-23,market,"<the reviewer's note: no filing found, and why>"

``symbol`` is the Symbol of the day, read as the Instrument it named on
``trade_date`` (ISO, ``YYYY-MM-DD``), the day of the move. ``reason`` is
one of ``demerger`` (a demerger or other scheme), ``rights``, ``dividend``,
``resumed`` (trading resumed after a suspension) or ``market`` (a market
move). ``citation`` is an official source where one exists (an exchange
circular or a company filing, by number or URL), or the reviewer's note that
none was found for a market move; it is required, and its form is not
checked. Lines starting with ``#`` are comments, and blank lines are skipped.

A malformed row, an unknown reason, a missing citation, or a second row for
one Symbol and day refuses the whole file, naming the line. The Integrity
Check (``tradeline.integrity``) reports a record whose Symbol named no
Instrument that day, one that matches no Large Move, and one whose Symbol named
the Instrument of an earlier record on its day; none fails it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, get_args

from tradeline.reviewed_csv import FirstLines, Line, parse_date, read_lines, require_header

COLUMNS = ("symbol", "trade_date", "reason", "citation")
Reason = Literal["demerger", "rights", "dividend", "resumed", "market"]
REASONS: tuple[Reason, ...] = get_args(Reason)


@dataclass(frozen=True)
class LargeMoveRecord:
    """A reviewed row: why the Symbol's Instrument moved on ``trade_date``, and the source."""

    symbol: str
    trade_date: date
    reason: Reason
    citation: str
    # The row's line in the file, for naming it in a report.
    line: int

    def summary(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "reason": self.reason,
        }

    def text(self, problem: str) -> str:
        """The record named in a report, with what is wrong with it."""
        return f"  record line {self.line} ({self.symbol} {self.trade_date}): {problem}"


def load_large_move_records(path: Path) -> list[LargeMoveRecord]:
    """Every row of the Large Move records file at ``path``; any bad row refuses the file."""
    lines = read_lines(path, "Large Move records")
    require_header(path, next(iter(lines), None), COLUMNS, "first")
    records: list[LargeMoveRecord] = []
    first = FirstLines[tuple[str, date]]()
    for line in lines[1:]:
        record = _row(line)
        first.add(
            (record.symbol, record.trade_date),
            line,
            f"{record.symbol}: {record.trade_date} is already recorded",
        )
        records.append(record)
    return records


def _row(line: Line) -> LargeMoveRecord:
    symbol, day, reason, citation = line.columns(COLUMNS)
    if not symbol:
        raise ValueError(f"{line.where}: the Symbol is required: {line.text}")
    where = f"{line.where}: {symbol}"
    trade_date = parse_date(where, "trade_date", day)
    if reason not in REASONS:
        raise ValueError(f"{where}: reason {reason!r} is not one of {', '.join(REASONS)}")
    if not citation:
        raise ValueError(
            f"{where}: no citation; cite the official source, or note that none was found"
        )
    return LargeMoveRecord(symbol, trade_date, reason, citation, line.number)
