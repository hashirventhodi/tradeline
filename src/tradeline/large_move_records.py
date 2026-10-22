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
Instrument that day, or that matches no Large Move; neither fails it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, get_args

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


def load_large_move_records(path: Path) -> list[LargeMoveRecord]:
    """Every row of the Large Move records file at ``path``; any bad row refuses the file."""
    if not path.exists():
        raise ValueError(f"Large Move records file {path} does not exist")
    lines = [
        (number, line)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or next(csv.reader([lines[0][1]])) != list(COLUMNS):
        raise ValueError(f"{path}: the first row must be the header {','.join(COLUMNS)}")
    records: list[LargeMoveRecord] = []
    seen: dict[tuple[str, date], int] = {}
    for number, line in lines[1:]:
        record = _row(path, number, line)
        key = (record.symbol, record.trade_date)
        if key in seen:
            raise ValueError(
                f"{path.name} line {number}: {record.symbol}: {record.trade_date} is already"
                f" recorded on line {seen[key]}"
            )
        seen[key] = number
        records.append(record)
    return records


def _row(path: Path, number: int, line: str) -> LargeMoveRecord:
    fields = [f.strip() for f in next(csv.reader([line]))]
    where = f"{path.name} line {number}"
    if len(fields) != len(COLUMNS):
        raise ValueError(f"{where}: expected {len(COLUMNS)} columns ({','.join(COLUMNS)}): {line}")
    symbol, day, reason, citation = fields
    if not symbol:
        raise ValueError(f"{where}: the Symbol is required: {line}")
    where = f"{where}: {symbol}"
    try:
        trade_date = date.fromisoformat(day)
    except ValueError:
        raise ValueError(f"{where}: trade_date {day!r} is not a date (YYYY-MM-DD)") from None
    if reason not in REASONS:
        raise ValueError(f"{where}: reason {reason!r} is not one of {', '.join(REASONS)}")
    if not citation:
        raise ValueError(
            f"{where}: no citation; cite the official source, or note that none was found"
        )
    return LargeMoveRecord(symbol, trade_date, reason, citation, number)
