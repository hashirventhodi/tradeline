"""``reconcile``: Kite's daily candles against NSE's end-of-day files.

For every Instrument and series that has candles stored, each trading day in
the range is compared on presence and close, an end-of-day row and a candle
each belonging to the Instrument its Symbol named that day (ADR 0016). Kite
files an Instrument's whole history under its current name, so before a
rename its candles are under a Symbol NSE had not used yet; they belong to the
Instrument all the same. Of two candles of one Instrument-day under different
Kite names, the one under the more recent name is compared
(``queries.instrument_daily_candles``), and where the two differ the day is
reported as a **candle disagreement**, listed but not counted as a
discrepancy: the compared candle is the one Kite serves today. A Symbol in no Instrument (none
rebuilt yet) stands for its own. Series moves are compared as before, per
series.

- **missing**: NSE has an end-of-day row and there is no candle;
- **extra**: there is a candle and NSE has no end-of-day row;
- **close**: both exist and the closes disagree beyond the tolerance.

A candle dated before its Instrument's Listing Age starts (the view
``instrument_listing_age``, ADR 0017) is a **Pre-listing Candle**: the
Instrument was not trading on NSE that day, so the candle cannot be its own.
Kite files history by instrument token, and a token can carry an earlier
security's bars (MAZDOCK's carries Farmax India's, docs/research.md, #34).
Such a candle is listed, with its count, but neither compared nor counted as
a row, and never attributed to another Instrument. A Symbol in no Instrument
has no Listing Age, so none of its candles is one.

Kite's candles are not raw prices: Kite adjusts its whole history, as of the
day it is fetched, for splits, bonuses, and large dividends (docs/research.md,
ADR 0006). Between two Ex-Dates that factor is one constant, so the closes
are compared after taking it out: the factor of a stretch of days is the
median of Kite's close over NSE's across it, the stretches being cut at every
Ex-Date NSE lists for any of its Symbols (any kind, since Kite's rules for which
actions it adjusts are its own). The median is robust to a few bad closes;
a stretch of one or two days cannot show a bad close at all. A close
mismatches when it is further from NSE's close times the factor than
``close_tolerance`` of that price plus half Kite's 5 paisa tick, since Kite
rounds its adjusted prices to the tick. Bars of one stretch fetched on
different days, either side of a new action, carry different factors and are
reported as mismatches, which they are.

The run passes when all discrepancies together, close mismatches as well as
missing and extra rows, are under ``max_discrepancy_pct`` of the rows
compared, a row being one (Instrument, series, day) present on either side; a
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
class PreListingCandle:
    """A candle dated before its Instrument's Listing Age starts: never compared."""

    instrument: str
    symbol: str
    series: str
    trade_date: date
    close: Decimal
    # The day the Instrument's Listing Age starts (ADR 0017).
    listing_age_from: date

    def summary(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "symbol": self.symbol,
            "series": self.series,
            "trade_date": self.trade_date.isoformat(),
            "close": str(self.close),
            "listing_age_from": self.listing_age_from.isoformat(),
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
    # Instrument-days stored under two Kite names with different values; not discrepancies.
    candle_disagreements: tuple[queries.CandleDisagreement, ...] = ()
    # Candles from before their Instrument's Listing Age starts; not discrepancies.
    pre_listing_candles: tuple[PreListingCandle, ...] = ()

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
            "candle_disagreements": len(self.candle_disagreements),
            "listed_candle_disagreements": [
                _disagreement_summary(d) for d in self.candle_disagreements[:MAX_LISTED]
            ],
            "pre_listing_candles": len(self.pre_listing_candles),
            "listed_pre_listing_candles": [
                d.summary() for d in self.pre_listing_candles[:MAX_LISTED]
            ],
        }

    def text(self) -> str:
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
        if self.candle_disagreements:
            count = len(self.candle_disagreements)
            lines.append("")
            lines.append(
                f"{count} candle disagreement{'s' if count != 1 else ''}"
                " (one Instrument-day under two Kite names; the newer is compared):"
            )
            lines.extend(
                f"  {d.instrument} {d.series} {d.trade_date}: {d.kept.symbol} {d.kept.close},"
                f" {d.other.symbol} {d.other.close}"
                for d in self.candle_disagreements[:MAX_LISTED]
            )
            if count > MAX_LISTED:
                lines.append(f"... {count - MAX_LISTED} more not listed")
        if self.pre_listing_candles:
            count = len(self.pre_listing_candles)
            lines.append("")
            lines.append(
                f"{count} pre-listing candle{'s' if count != 1 else ''}"
                " (dated before its Instrument's Listing Age starts; not compared):"
            )
            lines.extend(
                f"  {c.instrument} {c.series} {c.trade_date}: {c.symbol} {c.close},"
                f" Listing Age from {c.listing_age_from}"
                for c in self.pre_listing_candles[:MAX_LISTED]
            )
            if count > MAX_LISTED:
                lines.append(f"... {count - MAX_LISTED} more not listed")
        return "\n".join(lines) + "\n"


def _disagreement_summary(d: queries.CandleDisagreement) -> dict[str, Any]:
    return {
        "instrument": d.instrument,
        "series": d.series,
        "trade_date": d.trade_date.isoformat(),
        "kept_symbol": d.kept.symbol,
        "kept_close": str(d.kept.close),
        "other_symbol": d.other.symbol,
        "other_close": str(d.other.close),
    }


@dataclass(frozen=True)
class _Row:
    # The Instrument's key, or the Symbol standing for one where it is in none.
    instrument: str
    # NSE's Symbol of the day, or Kite's where NSE has no row.
    symbol: str
    series: str
    trade_date: date
    candle_close: Decimal | None
    eod_close: Decimal | None
    has_candle: bool
    has_eod: bool
    # The day the Instrument's Listing Age starts; None for a Symbol in no Instrument.
    listing_age_from: date | None

    @property
    def pre_listing(self) -> bool:
        """A candle from before the Instrument was trading on NSE, with no row to meet.

        A day NSE has a row for is compared, whatever the last rebuild made of it."""
        return (
            self.has_candle
            and not self.has_eod
            and self.listing_age_from is not None
            and self.trade_date < self.listing_age_from
        )


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
    found = _rows(conn, start, end, names)
    if not found:
        raise ValueError(f"no candles or end-of-day rows to reconcile from {start} to {end}")
    rows = [r for r in found if not r.pre_listing]
    pre_listing = tuple(_pre_listing(r) for r in found if r.pre_listing)
    if not rows:
        count = len(pre_listing)
        raise ValueError(
            f"nothing to reconcile from {start} to {end}: only {count}"
            f" pre-listing candle{'s' if count != 1 else ''}"
        )

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
        candle_disagreements=_candle_disagreements(conn, rows, start, end),
        pre_listing_candles=pre_listing,
    )


def _pre_listing(row: _Row) -> PreListingCandle:
    return PreListingCandle(
        instrument=row.instrument,
        symbol=row.symbol,
        series=row.series,
        trade_date=row.trade_date,
        close=_of(row.candle_close),
        listing_age_from=_of(row.listing_age_from),
    )


def _candle_disagreements(
    conn: psycopg.Connection, rows: list[_Row], start: date, end: date
) -> tuple[queries.CandleDisagreement, ...]:
    """Each compared Instrument's days stored under two Kite names that differ, from
    the day its Listing Age starts: a Pre-listing Candle's day is not the Instrument's."""
    listing = {r.instrument: r.listing_age_from or start for r in rows if r.has_candle}
    found = [
        d
        for instrument in sorted(listing)
        for d in queries.instrument_daily_candles(
            conn, instrument, start=max(start, listing[instrument]), end=end, as_of=date.max
        ).disagreements
    ]
    return tuple(found)


# The Instruments a name stands for: every one the Symbol has named, or the
# Symbol itself where it is in no Instrument (``candles_daily_instruments``).
_NAMED = (
    "(SELECT instrument_key FROM symbol_instruments WHERE symbol = ANY(%(names)s)"
    " UNION SELECT unnest(%(names)s::text[]))"
)


def _rows(conn: psycopg.Connection, start: date, end: date, names: list[str] | None) -> list[_Row]:
    # Keyed by (Instrument, series, day): an end-of-day row belongs to the
    # Instrument its Symbol named that day, like a candle (migration 0015). Of
    # two candles of one Instrument-day, the one under Kite's more recent name.
    found = conn.execute(
        "WITH instruments AS ("
        "  SELECT DISTINCT instrument, series FROM candles_daily_instruments"
        f" WHERE (%(names)s::text[] IS NULL OR instrument IN {_NAMED})"
        "), k AS ("
        "  SELECT DISTINCT ON (instrument, series, trade_date)"
        "   instrument, symbol, series, trade_date, close"
        "  FROM candles_daily_instruments"
        "  JOIN instruments USING (instrument, series)"
        "  WHERE trade_date BETWEEN %(start)s AND %(end)s"
        "  ORDER BY instrument, series, trade_date, named_until DESC NULLS LAST, symbol"
        "), e AS ("
        "  SELECT coalesce(m.instrument_key, b.symbol) AS instrument,"
        "   b.symbol, b.series, b.trade_date, b.close"
        "  FROM eod_bhavcopy b"
        "  LEFT JOIN symbol_instruments m ON m.symbol = b.symbol"
        "   AND names_on(m.after, m.through, b.trade_date)"
        "  JOIN instruments i"
        "   ON (i.instrument, i.series) = (coalesce(m.instrument_key, b.symbol), b.series)"
        "  WHERE b.trade_date BETWEEN %(start)s AND %(end)s"
        ")"
        " SELECT instrument, coalesce(e.symbol, k.symbol), series, trade_date, k.close, e.close,"
        "  k.instrument IS NOT NULL, e.instrument IS NOT NULL, la.listing_age_from"
        " FROM k FULL JOIN e USING (instrument, series, trade_date)"
        " LEFT JOIN instrument_listing_age la ON la.instrument_key = instrument"
        " ORDER BY instrument, series, trade_date",
        {"names": names, "start": start, "end": end},
    ).fetchall()
    return [_Row(*row) for row in found]


def _refuse_symbols_without_candles(conn: psycopg.Connection, names: list[str]) -> None:
    unknown = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM unnest(%(names)s::text[]) AS name"
            " WHERE NOT EXISTS (SELECT FROM candles_daily_instruments c"
            "  WHERE c.instrument = name OR c.instrument IN"
            "   (SELECT instrument_key FROM symbol_instruments WHERE symbol = name))"
            " ORDER BY name",
            {"names": names},
        ).fetchall()
    ]
    if unknown:
        raise ValueError(f"no candles stored for {', '.join(unknown)}")


def _close_mismatches(
    conn: psycopg.Connection, rows: list[_Row], tolerance: Decimal
) -> tuple[Discrepancy, ...]:
    # Rows with both closes, grouped into the stretches between Ex-Dates.
    # An Instrument's Ex-Dates are those filed under any of its Symbols.
    stretches: dict[tuple[str, str, int], list[_Row]] = defaultdict(list)
    ex_dates: dict[str, list[date]] = {}
    for row in rows:
        if row.candle_close is None or not row.eod_close:  # NSE can state no close
            continue
        if row.instrument not in ex_dates:
            ex_dates[row.instrument] = sorted(
                {action.ex_date for action in queries.corporate_actions(conn, row.instrument)}
            )
        stretch = bisect.bisect_right(ex_dates[row.instrument], row.trade_date)
        stretches[(row.instrument, row.series, stretch)].append(row)

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


def _of[T](value: T | None) -> T:
    if value is None:
        raise ValueError("a value was expected")
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
