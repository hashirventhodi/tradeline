"""``ingest eod``: NSE end-of-day files for a date range, and the instrument master.

Three sources, each recorded as its own run:

1. ``nse_equity_master``: NSE's list of listed equities gives every current
   (symbol, series) its company name, ISIN, and listing date.
2. ``nse_eod``: one file per weekday in the range into ``eod_bhavcopy``. The
   (symbol, series, ISIN) of every row also goes into ``symbols``, which is how
   a symbol that has since delisted gets a row at all. A weekday with no file
   is reported in the run's details and in a warning; the exchange holiday
   calendar is not known here, so holidays appear there too.
3. ``kite_instruments``: Kite's instrument dump supplies the instrument token
   for every ``symbols`` row whose expected Kite trading symbol it lists.

Everything is keyed naturally, so a second run over a loaded range writes
nothing and the history shows a run with zero new rows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import psycopg

from tradeline.ingest import RunCounters, record_run
from tradeline.sources import kite, nse
from tradeline.transport import Transport

log = logging.getLogger(__name__)

COMMAND = "ingest"
SOURCE_EQUITY_MASTER = "nse_equity_master"
SOURCE_EOD = "nse_eod"
SOURCE_KITE_INSTRUMENTS = "kite_instruments"


@dataclass(frozen=True)
class EodIngestResult:
    days_loaded: int
    missing_days: tuple[date, ...]
    rows_written: int
    symbols_written: int


def ingest_eod(
    conn: psycopg.Connection, transport: Transport, *, start: date, end: date
) -> EodIngestResult:
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    session = nse.NseSession(transport)

    with record_run(conn, command=COMMAND, source=SOURCE_EQUITY_MASTER) as run:
        listed = nse.fetch_equity_master(session)
        run.rows_seen = len(listed)
        run.rows_written = _upsert_symbols(
            conn, ((s.symbol, s.series, s.isin, s.name, s.listing_date) for s in listed)
        )
        conn.commit()
    symbols_written = run.rows_written

    with record_run(
        conn, command=COMMAND, source=SOURCE_EOD, range_start=start, range_end=end
    ) as run:
        days_loaded, missing = _load_days(conn, session, run, start, end)
        symbols_written += run.details["symbols_written"]
    rows_written = run.rows_written

    with record_run(conn, command=COMMAND, source=SOURCE_KITE_INSTRUMENTS) as run:
        instruments = kite.fetch_instruments(transport)
        run.rows_seen = len(instruments)
        run.rows_written = _set_instrument_tokens(conn, instruments)
        conn.commit()
    symbols_written += run.rows_written

    return EodIngestResult(
        days_loaded=days_loaded,
        missing_days=tuple(missing),
        rows_written=rows_written,
        symbols_written=symbols_written,
    )


def _load_days(
    conn: psycopg.Connection,
    session: nse.NseSession,
    run: RunCounters,
    start: date,
    end: date,
) -> tuple[int, list[date]]:
    days_loaded = 0
    missing: list[date] = []
    symbols_written = 0
    for day in nse.weekdays(start, end):
        rows = nse.fetch_bhavcopy(session, day)
        if rows is None:
            log.warning("no end-of-day file", extra={"trade_date": day.isoformat()})
            missing.append(day)
            continue
        written = _insert_eod_rows(conn, rows)
        symbols_written += _upsert_symbols(
            conn, {(r.symbol, r.series, r.isin, None, None) for r in rows}
        )
        conn.commit()
        days_loaded += 1
        run.rows_seen += len(rows)
        run.rows_written += written
        log.info(
            "loaded end-of-day file",
            extra={"trade_date": day.isoformat(), "rows": len(rows), "rows_written": written},
        )
    run.details = {
        "days_loaded": days_loaded,
        "missing_days": [day.isoformat() for day in missing],
        "symbols_written": symbols_written,
    }
    if missing:
        log.warning(
            "trading days without an end-of-day file",
            extra={"missing_days": run.details["missing_days"]},
        )
    return days_loaded, missing


def _insert_eod_rows(conn: psycopg.Connection, rows: list[nse.EodRow]) -> int:
    """Insert what is new; a row already present is left exactly as it was."""
    columns = (
        "trade_date",
        "symbol",
        "series",
        "isin",
        "open",
        "high",
        "low",
        "close",
        "last",
        "prev_close",
        "traded_qty",
        "traded_value",
        "trades",
    )
    cursor = conn.execute(
        "INSERT INTO eod_bhavcopy (trade_date, symbol, series, isin, open, high, low, close,"
        " last, prev_close, traded_qty, traded_value, trades)"
        " SELECT * FROM unnest(%s::date[], %s::text[], %s::text[], %s::text[],"
        " %s::numeric[], %s::numeric[], %s::numeric[], %s::numeric[], %s::numeric[],"
        " %s::numeric[], %s::bigint[], %s::numeric[], %s::integer[])"
        " ON CONFLICT (trade_date, symbol, series) DO NOTHING",
        tuple([getattr(row, column) for row in rows] for column in columns),
    )
    return cursor.rowcount


SymbolFields = tuple[str, str, str | None, str | None, date | None]


def _upsert_symbols(conn: psycopg.Connection, rows: Iterable[SymbolFields]) -> int:
    """Insert or fill in (symbol, series, isin, name, listing_date); a known value is never blanked.

    Returns how many rows were created or changed: an identical re-run returns 0.
    """
    rows = list(rows)
    if not rows:
        return 0
    cursor = conn.execute(
        "INSERT INTO symbols (symbol, series, isin, name, listing_date)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::date[])"
        " ON CONFLICT (symbol, series) DO UPDATE SET"
        "  isin = COALESCE(EXCLUDED.isin, symbols.isin),"
        "  name = COALESCE(EXCLUDED.name, symbols.name),"
        "  listing_date = COALESCE(EXCLUDED.listing_date, symbols.listing_date)"
        " WHERE (symbols.isin, symbols.name, symbols.listing_date) IS DISTINCT FROM"
        "  (COALESCE(EXCLUDED.isin, symbols.isin), COALESCE(EXCLUDED.name, symbols.name),"
        "   COALESCE(EXCLUDED.listing_date, symbols.listing_date))",
        tuple([row[i] for row in rows] for i in range(5)),
    )
    return cursor.rowcount


def _set_instrument_tokens(conn: psycopg.Connection, instruments: list[kite.KiteInstrument]) -> int:
    """Give every ``symbols`` row the token of its expected Kite trading symbol."""
    cursor = conn.execute(
        "UPDATE symbols SET kite_instrument_token = k.token"
        " FROM unnest(%s::text[], %s::bigint[]) AS k(trading_symbol, token)"
        " WHERE k.trading_symbol = CASE WHEN symbols.series = 'EQ' THEN symbols.symbol"
        "  ELSE symbols.symbol || '-' || symbols.series END"
        " AND symbols.kite_instrument_token IS DISTINCT FROM k.token",
        (
            [i.trading_symbol for i in instruments],
            [i.instrument_token for i in instruments],
        ),
    )
    return cursor.rowcount
