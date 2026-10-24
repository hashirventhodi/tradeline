"""NSE's tick sizes: a checked-in, dated table citing NSE's circulars.

The tick an equity's prices move in has changed with NSE's circulars, so it
is stated in a CSV file in the repository, at the location ``risk.tick_sizes``
in ``tradeline.toml`` configures, each row reviewed like code, as the Trading
Calendar is::

    in_force_from,from,to,tick,citation
    2015-01-01,,,0.05,NSE/CMTR/62174
    2024-06-10,,<250,0.01,NSE/CMTR/62174
    2024-06-10,>=250,,0.05,NSE/CMTR/62174

Each date's rows replace every band of the date before. A band's ``from`` is
``>=N`` or ``>N`` (empty from zero) and its ``to`` is ``<N`` or ``<=N`` (empty
without bound), as NSE's circulars write them. The price that picks a band is
the security's reference close, which NSE fixes once a month (the close on the
last trading day of the month before, NSE/CMTR/62174); finding it is the
caller's.

A malformed row, a row without a citation, a tick that is not positive, or a
date whose bands leave a price uncovered or cover one twice refuses the whole
file, naming the line, so no Plan is priced on part of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tradeline.reviewed_csv import Line, parse_date, read_lines, require_header

COLUMNS = ("in_force_from", "from", "to", "tick", "citation")
_FROM = re.compile(r"(>=|>)(\d+(?:\.\d+)?)")
_TO = re.compile(r"(<=|<)(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class TickBand:
    in_force_from: date
    # The lowest price in the band (None: from zero), and whether it is in the band.
    low: Decimal | None
    low_included: bool
    # The highest price (None: no bound), and whether it is in the band.
    high: Decimal | None
    high_included: bool
    tick: Decimal
    citation: str
    line: int

    def covers(self, price: Decimal) -> bool:
        above = self.low is None or price > self.low or (self.low_included and price == self.low)
        below = (
            self.high is None or price < self.high or (self.high_included and price == self.high)
        )
        return above and below


@dataclass(frozen=True)
class TickSizes:
    bands: tuple[TickBand, ...]

    def tick(self, day: date, *, reference_close: Decimal) -> Decimal:
        """The tick in force on ``day`` for a security whose reference close is
        ``reference_close``."""
        dates = [b.in_force_from for b in self.bands if b.in_force_from <= day]
        if not dates:
            raise ValueError(f"{day} is before the tick table's first date")
        in_force = max(dates)
        (band,) = (
            b for b in self.bands if b.in_force_from == in_force and b.covers(reference_close)
        )
        return band.tick


def load_tick_sizes(path: Path) -> TickSizes:
    """The tick table at ``path``; any bad row refuses the file."""
    lines = read_lines(path, "tick sizes")
    require_header(path, lines[0] if lines else None, COLUMNS, "first")
    bands = [_row(line) for line in lines[1:]]
    if not bands:
        raise ValueError(f"{path}: no tick sizes")
    for day in sorted({b.in_force_from for b in bands}):
        _check_tiling([b for b in bands if b.in_force_from == day])
    return TickSizes(tuple(bands))


def _row(line: Line) -> TickBand:
    day_text, low_text, high_text, tick_text, citation = line.columns(COLUMNS)
    day = parse_date(line.where, "in_force_from", day_text)
    where = f"{line.where}: {day}"
    low, low_included = _bound(where, "from", low_text, _FROM, inclusive=">=")
    high, high_included = _bound(where, "to", high_text, _TO, inclusive="<=")
    try:
        tick = Decimal(tick_text)
    except InvalidOperation:
        raise ValueError(f"{where}: tick {tick_text!r} is not a number") from None
    if not tick > 0:
        raise ValueError(f"{where}: tick {tick} is not positive")
    if not citation:
        raise ValueError(f"{where}: no citation; cite NSE's circular by URL or number")
    return TickBand(day, low, low_included, high, high_included, tick, citation, line.number)


def _bound(
    where: str, column: str, text: str, pattern: re.Pattern[str], *, inclusive: str
) -> tuple[Decimal | None, bool]:
    if not text:
        return None, True
    match = pattern.fullmatch(text)
    if match is None:
        raise ValueError(f"{where}: {column} {text!r} is not one of {pattern.pattern}")
    return Decimal(match[2]), match[1] == inclusive


def _check_tiling(bands: list[TickBand]) -> None:
    """Refuse one date's bands unless they cover every price from zero up exactly once."""
    ordered = sorted(bands, key=lambda b: (b.low is not None, b.low or 0, b.low_included is False))
    first = ordered[0]
    where = f"tick_sizes line {first.line}: {first.in_force_from}"
    if first.low is not None:
        raise ValueError(f"{where}: no band covers prices from zero")
    for before, after in zip(ordered, ordered[1:], strict=False):
        meets = (
            before.high is not None
            and before.high == after.low
            and before.high_included != after.low_included
        )
        if not meets:
            raise ValueError(
                f"tick_sizes line {after.line}: {after.in_force_from}: its band does not start"
                f" where line {before.line}'s ends"
            )
    if ordered[-1].high is not None:
        raise ValueError(f"tick_sizes line {ordered[-1].line}: no band covers the highest prices")
