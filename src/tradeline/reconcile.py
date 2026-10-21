"""``reconcile``: Kite's daily candles against NSE's end-of-day files.

For every instrument (symbol, series) that has candles stored, each trading
day in the range is compared on presence and close:

- **missing**: NSE has an end-of-day row and there is no candle;
- **extra**: there is a candle and NSE has no end-of-day row;
- **close**: both exist and the closes disagree beyond the tolerance.

Kite's candles are not raw prices: Kite adjusts its whole history, as of the
day it is fetched, for splits, bonuses, and large dividends (docs/research.md,
ADR 0006). Between two Ex-Dates that factor is one constant, so the closes
are compared after taking it out: the factor of a stretch of days is the
median of Kite's close over NSE's across it, the stretches being cut at every
Ex-Date NSE lists for the symbol (any kind, since Kite's rules for which
actions it adjusts are its own). The median is robust to a few bad closes;
a stretch of one or two days cannot show a bad close at all. A close
mismatches when it is further from NSE's close times the factor than
``close_tolerance`` of that price plus half Kite's 5 paisa tick, since Kite
rounds its adjusted prices to the tick. Bars of one stretch fetched on
different days, either side of a new action, carry different factors and are
reported as mismatches, which they are.

The run passes when all discrepancies together, close mismatches as well as
missing and extra rows, are under ``max_discrepancy_pct`` of the rows
compared, a row being one (symbol, series, day) present on either side; a
run exactly at the threshold fails. Nothing is written: reconcile is a read.
"""

from __future__ import annotations

import bisect
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import psycopg

from tradeline import queries
from tradeline.config import Config

Kind = Literal["missing", "extra", "close"]
HALF_TICK = Decimal("0.025")
HUNDRED = Decimal(100)
# Every discrepancy is counted; this many of each kind are listed in a summary.
MAX_LISTED = 100


@dataclass(frozen=True)
class Discrepancy:
    kind: Kind
    symbol: str
    series: str
    trade_date: date
    candle_close: Decimal | None
    eod_close: Decimal | None
    # NSE's close times Kite's factor for the stretch; only for a close mismatch.
    expected_close: Decimal | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "series": self.series,
            "trade_date": self.trade_date.isoformat(),
            "candle_close": _text(self.candle_close),
            "eod_close": _text(self.eod_close),
            "expected_close": _text(self.expected_close),
        }


@dataclass(frozen=True)
class ReconcileReport:
    start: date
    end: date
    close_tolerance: Decimal
    max_discrepancy_pct: Decimal
    rows: int
    missing: tuple[Discrepancy, ...]
    extra: tuple[Discrepancy, ...]
    close_mismatches: tuple[Discrepancy, ...]

    @property
    def discrepancies(self) -> int:
        """Every discrepancy of the three kinds: missing, extra, and close mismatches."""
        return len(self.missing) + len(self.extra) + len(self.close_mismatches)

    @property
    def discrepancy_pct(self) -> Decimal:
        return HUNDRED * self.discrepancies / self.rows

    @property
    def reconciliation_pct(self) -> Decimal:
        return HUNDRED - self.discrepancy_pct

    @property
    def passed(self) -> bool:
        """Discrepancies strictly under the threshold."""
        return self.discrepancy_pct < self.max_discrepancy_pct

    def summary(self) -> dict[str, Any]:
        """The machine-readable report: same data, same output, key for key."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "close_tolerance": str(self.close_tolerance),
            "max_discrepancy_pct": str(self.max_discrepancy_pct),
            "rows": self.rows,
            "discrepancies": self.discrepancies,
            "missing": len(self.missing),
            "extra": len(self.extra),
            "close_mismatches": len(self.close_mismatches),
            "discrepancy_pct": _pct(self.discrepancy_pct),
            "reconciliation_pct": _pct(self.reconciliation_pct),
            "passed": self.passed,
            "listed": [
                d.summary()
                for kind in (self.missing, self.extra, self.close_mismatches)
                for d in kind[:MAX_LISTED]
            ],
        }

    def table(self) -> str:
        """The human-readable report."""
        lines = [
            f"reconcile {self.start} to {self.end}: {self.rows} rows,"
            f" {self.discrepancies} discrepancies ({len(self.missing)} missing,"
            f" {len(self.extra)} extra, {len(self.close_mismatches)} close),"
            f" {_pct(self.reconciliation_pct)}% reconciled,"
            f" must be under {self.max_discrepancy_pct}%: {'PASS' if self.passed else 'FAIL'}",
        ]
        listed = [
            d
            for kind in (self.missing, self.extra, self.close_mismatches)
            for d in kind[:MAX_LISTED]
        ]
        if listed:
            header = ("kind", "symbol", "series", "date", "candle", "eod", "expected")
            cells = [header] + [
                (
                    d.kind,
                    d.symbol,
                    d.series,
                    d.trade_date.isoformat(),
                    _text(d.candle_close) or "-",
                    _text(d.eod_close) or "-",
                    _text(d.expected_close) or "-",
                )
                for d in listed
            ]
            widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
            lines.append("")
            lines.extend(
                "  ".join(
                    cell.ljust(width) for cell, width in zip(row, widths, strict=True)
                ).rstrip()
                for row in cells
            )
        if listed and len(listed) < self.discrepancies:
            lines.append(f"... {self.discrepancies - len(listed)} more not listed")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _Row:
    symbol: str
    series: str
    trade_date: date
    candle_close: Decimal | None
    eod_close: Decimal | None
    has_candle: bool
    has_eod: bool


def reconcile(
    conn: psycopg.Connection,
    settings: Config.Reconcile,
    *,
    start: date,
    end: date,
    symbols: Sequence[str] | None = None,
) -> ReconcileReport:
    """Compare stored candles with end-of-day rows from ``start`` to ``end`` inclusive.

    Every instrument with candles is compared unless ``symbols`` narrows it; a
    named symbol with no candles at all is refused, as is a range with nothing
    to compare, so an empty database cannot pass.
    """
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    names = None if symbols is None else sorted(set(symbols))
    if names is not None:
        _refuse_symbols_without_candles(conn, names)
    rows = _rows(conn, start, end, names)
    if not rows:
        raise ValueError(f"no candles or end-of-day rows to reconcile from {start} to {end}")

    tolerance = Decimal(str(settings.close_tolerance))
    missing = tuple(_discrepancy("missing", r) for r in rows if not r.has_candle)
    extra = tuple(_discrepancy("extra", r) for r in rows if not r.has_eod)
    return ReconcileReport(
        start=start,
        end=end,
        close_tolerance=tolerance,
        max_discrepancy_pct=Decimal(str(settings.max_discrepancy_pct)),
        rows=len(rows),
        missing=missing,
        extra=extra,
        close_mismatches=_close_mismatches(conn, rows, tolerance),
    )


def _rows(conn: psycopg.Connection, start: date, end: date, names: list[str] | None) -> list[_Row]:
    found = conn.execute(
        "WITH instruments AS ("
        "  SELECT DISTINCT symbol, series FROM candles_daily"
        "  WHERE (%(names)s::text[] IS NULL OR symbol = ANY(%(names)s))"
        "), k AS ("
        "  SELECT symbol, series, trade_date, close FROM candles_daily"
        "  WHERE trade_date BETWEEN %(start)s AND %(end)s"
        "  AND (%(names)s::text[] IS NULL OR symbol = ANY(%(names)s))"
        "), e AS ("
        "  SELECT symbol, series, trade_date, close FROM eod_bhavcopy"
        "  JOIN instruments USING (symbol, series)"
        "  WHERE trade_date BETWEEN %(start)s AND %(end)s"
        ")"
        " SELECT symbol, series, trade_date, k.close, e.close,"
        "  k.symbol IS NOT NULL, e.symbol IS NOT NULL"
        " FROM k FULL JOIN e USING (symbol, series, trade_date)"
        " ORDER BY symbol, series, trade_date",
        {"names": names, "start": start, "end": end},
    ).fetchall()
    return [_Row(*row) for row in found]


def _refuse_symbols_without_candles(conn: psycopg.Connection, names: list[str]) -> None:
    with_candles = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM candles_daily WHERE symbol = ANY(%s)", (names,)
        ).fetchall()
    }
    unknown = [name for name in names if name not in with_candles]
    if unknown:
        raise ValueError(f"no candles stored for {', '.join(unknown)}")


def _close_mismatches(
    conn: psycopg.Connection, rows: list[_Row], tolerance: Decimal
) -> tuple[Discrepancy, ...]:
    # Rows with both closes, grouped into the stretches between Ex-Dates.
    stretches: dict[tuple[str, str, int], list[_Row]] = defaultdict(list)
    ex_dates: dict[str, list[date]] = {}
    for row in rows:
        if row.candle_close is None or not row.eod_close:  # NSE can state no close
            continue
        if row.symbol not in ex_dates:
            ex_dates[row.symbol] = sorted(
                {action.ex_date for action in queries.corporate_actions(conn, row.symbol)}
            )
        stretch = bisect.bisect_right(ex_dates[row.symbol], row.trade_date)
        stretches[(row.symbol, row.series, stretch)].append(row)

    mismatches: list[Discrepancy] = []
    for stretch_rows in stretches.values():
        factor = statistics.median(_ratio(r) for r in stretch_rows)
        for r in stretch_rows:
            expected = _of(r.eod_close) * factor
            if abs(_of(r.candle_close) - expected) > tolerance * expected + HALF_TICK:
                mismatches.append(
                    _discrepancy("close", r, expected=expected.quantize(Decimal("0.01")))
                )
    mismatches.sort(key=lambda d: (d.symbol, d.series, d.trade_date))
    return tuple(mismatches)


def _ratio(row: _Row) -> Decimal:
    return _of(row.candle_close) / _of(row.eod_close)


def _of(value: Decimal | None) -> Decimal:
    if value is None:
        raise ValueError("a close was expected")
    return value


def _discrepancy(kind: Kind, row: _Row, *, expected: Decimal | None = None) -> Discrepancy:
    return Discrepancy(
        kind=kind,
        symbol=row.symbol,
        series=row.series,
        trade_date=row.trade_date,
        candle_close=row.candle_close,
        eod_close=row.eod_close,
        expected_close=expected,
    )


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _pct(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))
