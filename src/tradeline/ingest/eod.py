"""``ingest eod``: NSE end-of-day files for a date range, and the instrument master.

Three sources, each recorded as its own ingest run:

1. ``nse_equity_master``: NSE's list of listed equities gives every current
   (symbol, series) its company name, ISIN, and listing date.
2. ``nse_eod``: one file per calendar day in the range into ``eod_bhavcopy``,
   so that a special weekend session is loaded like any other day. The
   (symbol, series, ISIN) of every row also goes into ``symbols``, which is how
   a symbol that has since delisted gets a row at all. A weekday with no file
   is reported in the run's details and in a warning; the exchange holiday
   calendar is not known here, so holidays appear there too. A day already
   loaded is left exactly as it was: NSE publishes each file once as final,
   so a resend whose values differ is an anomaly, logged and counted.
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
from typing import Any

import psycopg

from tradeline.dates import calendar_days
from tradeline.ingest import COMMAND, IngestRunCounters, record_ingest_run
from tradeline.sources import kite, nse
from tradeline.transport import Transport

log = logging.getLogger(__name__)

SOURCE_EQUITY_MASTER = "nse_equity_master"
SOURCE_EOD = "nse_eod"
SOURCE_KITE_INSTRUMENTS = "kite_instruments"


@dataclass(frozen=True)
class EodIngestResult:
    days_loaded: int
    missing_days: tuple[date, ...]
    eod_rows_written: int
    anomalies: int
    # ``symbols`` rows created or changed, summed over the three sources: a
    # symbol first seen by the equity master and then given a token counts twice.
    symbol_updates: int

    def summary(self) -> dict[str, Any]:
        return {
            "days_loaded": self.days_loaded,
            "missing_days": [day.isoformat() for day in self.missing_days],
            "eod_rows_written": self.eod_rows_written,
            "anomalies": self.anomalies,
            "symbol_updates": self.symbol_updates,
        }


@dataclass(frozen=True)
class SymbolFacts:
    """What one source knows about a (symbol, series); None means it does not say."""

    symbol: str
    series: str
    isin: str | None = None
    name: str | None = None
    listing_date: date | None = None


def ingest_eod(
    conn: psycopg.Connection, transport: Transport, *, start: date, end: date
) -> EodIngestResult:
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    session = nse.NseSession(transport)

    with record_ingest_run(conn, command=COMMAND, source=SOURCE_EQUITY_MASTER) as master_run:
        listed = nse.fetch_equity_master(session)
        master_run.rows_seen = len(listed)
        master_run.rows_written = _upsert_symbols(
            conn,
            (
                SymbolFacts(
                    s.symbol, s.series, isin=s.isin, name=s.name, listing_date=s.listing_date
                )
                for s in listed
            ),
        )
        conn.commit()

    with record_ingest_run(
        conn, command=COMMAND, source=SOURCE_EOD, range_start=start, range_end=end
    ) as eod_run:
        _load_days(conn, session, eod_run, start, end)

    with record_ingest_run(conn, command=COMMAND, source=SOURCE_KITE_INSTRUMENTS) as kite_run:
        instruments = kite.fetch_instruments(transport)
        kite_run.rows_seen = len(instruments)
        kite_run.rows_written = _set_instrument_tokens(conn, instruments)
        conn.commit()

    return EodIngestResult(
        days_loaded=eod_run.details["days_loaded"],
        missing_days=tuple(date.fromisoformat(d) for d in eod_run.details["missing_days"]),
        eod_rows_written=eod_run.rows_written,
        anomalies=eod_run.details["anomalies"],
        symbol_updates=master_run.rows_written
        + eod_run.details["symbol_updates"]
        + kite_run.rows_written,
    )


def _load_days(
    conn: psycopg.Connection,
    session: nse.NseSession,
    run: IngestRunCounters,
    start: date,
    end: date,
) -> None:
    days_loaded = 0
    missing: list[str] = []
    anomalies = 0
    symbol_updates = 0
    for day in calendar_days(start, end):
        rows = nse.fetch_bhavcopy(session, day)
        if rows is None:
            if day.weekday() < 5:
                log.warning("no end-of-day file", extra={"trade_date": day.isoformat()})
                missing.append(day.isoformat())
            continue
        written, changed = _insert_eod_rows(conn, rows)
        symbol_updates += _upsert_symbols(
            conn, {SymbolFacts(r.symbol, r.series, isin=r.isin) for r in rows}
        )
        conn.commit()
        days_loaded += 1
        anomalies += len(changed)
        run.rows_seen += len(rows)
        run.rows_written += written
        for key in changed:
            log.warning(
                "end-of-day row resent with different values; stored row kept",
                extra={"trade_date": day.isoformat(), "symbol": key[0], "series": key[1]},
            )
        log.info(
            "loaded end-of-day file",
            extra={"trade_date": day.isoformat(), "rows": len(rows), "rows_written": written},
        )
    run.details = {
        "days_loaded": days_loaded,
        "missing_days": missing,
        "anomalies": anomalies,
        "symbol_updates": symbol_updates,
    }
    if missing:
        log.warning("trading days without an end-of-day file", extra={"missing_days": missing})


# Every column of eod_bhavcopy with its SQL type, in the order the row type holds them.
_EOD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trade_date", "date"),
    ("symbol", "text"),
    ("series", "text"),
    ("isin", "text"),
    ("open", "numeric"),
    ("high", "numeric"),
    ("low", "numeric"),
    ("close", "numeric"),
    ("last", "numeric"),
    ("prev_close", "numeric"),
    ("traded_qty", "bigint"),
    ("traded_value", "numeric"),
    ("trades", "integer"),
)
_EOD_NAMES = ", ".join(name for name, _ in _EOD_COLUMNS)
_EOD_ARRAYS = ", ".join(f"%s::{sql_type}[]" for _, sql_type in _EOD_COLUMNS)
_EOD_VALUES = [name for name, _ in _EOD_COLUMNS[3:]]  # everything but the key


def _insert_eod_rows(
    conn: psycopg.Connection, rows: list[nse.EndOfDayRow]
) -> tuple[int, list[tuple[str, str]]]:
    """Insert what is new; return the count and the keys of present rows whose values differ."""
    existing = ", ".join(f"e.{name}" for name in _EOD_VALUES)
    incoming = ", ".join(f"n.{name}" for name in _EOD_VALUES)
    row = conn.execute(
        f"WITH incoming AS (SELECT * FROM unnest({_EOD_ARRAYS}) AS n({_EOD_NAMES})),"
        f" inserted AS (INSERT INTO eod_bhavcopy ({_EOD_NAMES}) SELECT {_EOD_NAMES} FROM incoming"
        "  ON CONFLICT (trade_date, symbol, series) DO NOTHING RETURNING 1)"
        " SELECT (SELECT count(*) FROM inserted),"
        "  coalesce(array_agg(ARRAY[e.symbol, e.series]) FILTER (WHERE e.symbol IS NOT NULL), '{}')"
        " FROM incoming n LEFT JOIN eod_bhavcopy e"
        "  ON (e.trade_date, e.symbol, e.series) = (n.trade_date, n.symbol, n.series)"
        f"  AND ({existing}) IS DISTINCT FROM ({incoming})",
        tuple([getattr(r, name) for r in rows] for name, _ in _EOD_COLUMNS),
    ).fetchone()
    assert row is not None
    written, changed = row
    return int(written), [(symbol, series) for symbol, series in changed]


def _upsert_symbols(conn: psycopg.Connection, facts: Iterable[SymbolFacts]) -> int:
    """Insert or fill in what a source knows; a known value is never blanked.

    Returns how many rows were created or changed: an identical re-run returns 0.
    """
    facts = list(facts)
    if not facts:
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
        (
            [f.symbol for f in facts],
            [f.series for f in facts],
            [f.isin for f in facts],
            [f.name for f in facts],
            [f.listing_date for f in facts],
        ),
    )
    return cursor.rowcount


def _set_instrument_tokens(conn: psycopg.Connection, instruments: list[kite.KiteInstrument]) -> int:
    """Give every ``symbols`` row the token of its expected Kite trading symbol."""
    tokens = {i.trading_symbol: i.instrument_token for i in instruments}
    known = conn.execute("SELECT symbol, series FROM symbols").fetchall()
    matched = [
        (symbol, series, tokens[kite.trading_symbol(symbol, series)])
        for symbol, series in known
        if kite.trading_symbol(symbol, series) in tokens
    ]
    if not matched:
        return 0
    cursor = conn.execute(
        "UPDATE symbols SET kite_instrument_token = k.token"
        " FROM unnest(%s::text[], %s::text[], %s::bigint[]) AS k(symbol, series, token)"
        " WHERE (symbols.symbol, symbols.series) = (k.symbol, k.series)"
        " AND symbols.kite_instrument_token IS DISTINCT FROM k.token",
        (
            [symbol for symbol, _, _ in matched],
            [series for _, series, _ in matched],
            [token for _, _, token in matched],
        ),
    )
    return cursor.rowcount
