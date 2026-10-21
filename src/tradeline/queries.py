"""The public read module.

Every later phase and every test reads the data layer through the functions
here and never from the tables. Each function takes an application connection
and returns frozen rows. Queries grow here as phases add data: adjusted
candles, Universe membership, sectors, announcements.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg


@dataclass(frozen=True)
class EndOfDayRow:
    trade_date: date
    symbol: str
    series: str
    isin: str | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    last: Decimal | None
    prev_close: Decimal | None
    traded_qty: int
    traded_value: Decimal
    trades: int


@dataclass(frozen=True)
class Symbol:
    symbol: str
    series: str
    isin: str | None
    name: str | None
    listing_date: date | None
    kite_instrument_token: int | None


@dataclass(frozen=True)
class IngestRun:
    id: uuid.UUID
    command: str
    source: str
    range_start: date | None
    range_end: date | None
    started_at: datetime
    finished_at: datetime
    rows_seen: int
    rows_written: int
    error: str | None
    details: dict[str, Any]


def end_of_day(
    conn: psycopg.Connection,
    symbol: str,
    *,
    start: date,
    end: date,
    series: str | None = None,
) -> list[EndOfDayRow]:
    """NSE end-of-day rows for ``symbol`` from ``start`` to ``end`` inclusive, oldest first.

    All series are returned unless one is named; a symbol that moved between
    series inside the range has rows under each.
    """
    rows = conn.execute(
        "SELECT trade_date, symbol, series, isin, open, high, low, close, last, prev_close,"
        " traded_qty, traded_value, trades"
        " FROM eod_bhavcopy"
        " WHERE symbol = %s AND trade_date BETWEEN %s AND %s"
        " AND (%s::text IS NULL OR series = %s)"
        " ORDER BY trade_date, series",
        (symbol, start, end, series, series),
    ).fetchall()
    return [EndOfDayRow(*row) for row in rows]


def symbols(conn: psycopg.Connection, *, symbol: str | None = None) -> list[Symbol]:
    """The instrument master, or the rows (one per series) for one symbol."""
    rows = conn.execute(
        "SELECT symbol, series, isin, name, listing_date, kite_instrument_token"
        " FROM symbols WHERE (%s::text IS NULL OR symbol = %s) ORDER BY symbol, series",
        (symbol, symbol),
    ).fetchall()
    return [Symbol(*row) for row in rows]


def ingest_runs(conn: psycopg.Connection, *, source: str | None = None) -> list[IngestRun]:
    """Run history, newest first."""
    rows = conn.execute(
        "SELECT id, command, source, range_start, range_end, started_at, finished_at,"
        " rows_seen, rows_written, error, details"
        " FROM ingest_runs WHERE (%s::text IS NULL OR source = %s)"
        " ORDER BY started_at DESC, id",
        (source, source),
    ).fetchall()
    return [IngestRun(*row) for row in rows]


@dataclass(frozen=True)
class DailyCandle:
    trade_date: date
    symbol: str
    series: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def daily_candles(
    conn: psycopg.Connection,
    symbol: str,
    *,
    start: date,
    end: date,
    series: str | None = None,
) -> list[DailyCandle]:
    """Raw daily candles for ``symbol`` from ``start`` to ``end`` inclusive, oldest first.

    Exactly as Kite returned them: unadjusted. The adjusted series arrives
    with corporate actions and is computed on read, never stored. All series
    are returned unless one is named, as for ``end_of_day``.
    """
    rows = conn.execute(
        "SELECT trade_date, symbol, series, open, high, low, close, volume"
        " FROM candles_daily"
        " WHERE symbol = %s AND trade_date BETWEEN %s AND %s"
        " AND (%s::text IS NULL OR series = %s)"
        " ORDER BY trade_date, series",
        (symbol, start, end, series, series),
    ).fetchall()
    return [DailyCandle(*row) for row in rows]
