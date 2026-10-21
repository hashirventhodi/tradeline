"""``ingest candles --interval day``: raw daily candles from Kite into ``candles_daily``.

One ingest run (source ``kite_candles_daily``) covers every requested symbol
over the range. Each symbol is looked up in the instrument master for its
Kite instrument token, fetched in chunks the historical API accepts at no
more than 3 requests per second (``KiteSession``), and committed before the
next symbol starts, so an interrupted run keeps what it loaded. Without a
symbol list, every ``EQ`` series symbol with a token is loaded: Phase 1 is
scoped to series ``EQ``. A named symbol is loaded under every series it has
in the master, so a symbol that moved between series (``EQ`` to ``BE`` and
back) can be given a continuous history on purpose. A symbol in the master
without a token is skipped and reported; a symbol not in the master is
refused up front (``ingest eod`` loads the master).

Bars are stored exactly as Kite returned them. A bar already stored is left
as it is: Kite can resend a bar, and a resend whose values differ is an
anomaly, logged and recorded in the run's details with both versions, never
applied silently.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from tradeline.ingest import IngestRunCounters, record_ingest_run
from tradeline.logging import log_context
from tradeline.sources import kite

log = logging.getLogger(__name__)

COMMAND = "ingest"
SOURCE_DAILY = "kite_candles_daily"
EQUITY_SERIES = "EQ"
# Anomalies are counted in full; this many are recorded in detail per run.
MAX_RECORDED_ANOMALIES = 100


@dataclass(frozen=True)
class CandleIngestResult:
    symbols_loaded: int
    symbols_without_token: tuple[str, ...]
    rows_written: int
    anomalies: int
    requests: int


@dataclass(frozen=True)
class _Instrument:
    symbol: str
    series: str
    token: int | None


def ingest_candles(
    conn: psycopg.Connection,
    session: kite.KiteSession,
    *,
    start: date,
    end: date,
    symbols: Sequence[str] | None = None,
) -> CandleIngestResult:
    """Load daily candles for ``symbols`` (every ``EQ`` symbol with a token when None)."""
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    instruments = _instruments(conn, symbols)

    with record_ingest_run(
        conn, command=COMMAND, source=SOURCE_DAILY, range_start=start, range_end=end
    ) as run:
        loaded = _load_symbols(conn, session, run, instruments, start, end)

    return CandleIngestResult(
        symbols_loaded=loaded,
        symbols_without_token=tuple(run.details["symbols_without_token"]),
        rows_written=run.rows_written,
        anomalies=run.details["anomalies"],
        requests=run.details["requests"],
    )


def _instruments(conn: psycopg.Connection, symbols: Sequence[str] | None) -> list[_Instrument]:
    wanted = None if symbols is None else list(symbols)
    rows = conn.execute(
        "SELECT symbol, series, kite_instrument_token FROM symbols"
        " WHERE CASE WHEN %s::text[] IS NULL THEN series = %s ELSE symbol = ANY(%s::text[]) END"
        " ORDER BY symbol, series",
        (wanted, EQUITY_SERIES, wanted),
    ).fetchall()
    instruments = [_Instrument(symbol, series, token) for symbol, series, token in rows]
    if wanted is not None:
        unknown = sorted(set(wanted) - {i.symbol for i in instruments})
        if unknown:
            raise ValueError(
                f"symbols not in the instrument master: {', '.join(unknown)};"
                " run `ingest eod` first"
            )
    return instruments


def _load_symbols(
    conn: psycopg.Connection,
    session: kite.KiteSession,
    run: IngestRunCounters,
    instruments: list[_Instrument],
    start: date,
    end: date,
) -> int:
    loaded = 0
    anomalies = 0
    anomaly_bars: list[dict[str, Any]] = []
    without_token: list[str] = []
    run.details = {
        "interval": kite.INTERVAL_DAY,
        "symbols_requested": len(instruments),
        "symbols_without_token": without_token,
        "anomalies": 0,
        "anomaly_bars": anomaly_bars,
        "requests": 0,
    }
    for instrument in instruments:
        with log_context(symbol=instrument.symbol, series=instrument.series):
            if instrument.token is None:
                log.warning("symbol has no kite instrument token; skipped")
                without_token.append(instrument.symbol)
                continue
            before = session.requests_sent
            candles = kite.fetch_daily_candles(session, instrument.token, start=start, end=end)
            written, changed = _insert_candles(conn, instrument, candles)
            conn.commit()
            loaded += 1
            run.rows_seen += len(candles)
            run.rows_written += written
            run.details["requests"] += session.requests_sent - before
            anomalies += len(changed)
            for record in changed:
                log.warning("candle resent with different values; stored bar kept", extra=record)
                if len(anomaly_bars) < MAX_RECORDED_ANOMALIES:
                    anomaly_bars.append(record)
            log.info(
                "loaded daily candles",
                extra={
                    "rows": len(candles),
                    "rows_written": written,
                    "requests": session.requests_sent - before,
                },
            )
    run.details["anomalies"] = anomalies
    if without_token:
        log.warning("symbols without a kite instrument token", extra={"symbols": without_token})
    return loaded


_CANDLE_VALUES = ("open", "high", "low", "close", "volume")  # everything but the key


def _insert_candles(
    conn: psycopg.Connection, instrument: _Instrument, candles: list[kite.Candle]
) -> tuple[int, list[dict[str, Any]]]:
    """Insert what is new; return the count and an anomaly record per present bar that differs."""
    if not candles:
        return 0, []
    stored = ", ".join(f"c.{name}" for name in _CANDLE_VALUES)
    incoming = ", ".join(f"n.{name}" for name in _CANDLE_VALUES)
    row = conn.execute(
        "WITH incoming AS ("
        "  SELECT * FROM unnest(%s::date[], %s::numeric[], %s::numeric[], %s::numeric[],"
        "   %s::numeric[], %s::bigint[]) AS n(trade_date, open, high, low, close, volume)),"
        " inserted AS ("
        "  INSERT INTO candles_daily (symbol, series, trade_date, open, high, low, close, volume)"
        "  SELECT %s, %s, trade_date, open, high, low, close, volume FROM incoming"
        "  ON CONFLICT (symbol, series, trade_date) DO NOTHING RETURNING 1)"
        " SELECT (SELECT count(*) FROM inserted),"
        "  coalesce(array_agg(ARRAY[n.trade_date::text, n.open::text, n.high::text, n.low::text,"
        "   n.close::text, n.volume::text, c.open::text, c.high::text, c.low::text,"
        "   c.close::text, c.volume::text] ORDER BY n.trade_date)"
        "   FILTER (WHERE c.trade_date IS NOT NULL), '{}')"
        " FROM incoming n LEFT JOIN candles_daily c"
        "  ON (c.symbol, c.series, c.trade_date) = (%s, %s, n.trade_date)"
        f"  AND ({stored}) IS DISTINCT FROM ({incoming})",
        (
            [c.trade_date for c in candles],
            [c.open for c in candles],
            [c.high for c in candles],
            [c.low for c in candles],
            [c.close for c in candles],
            [c.volume for c in candles],
            instrument.symbol,
            instrument.series,
            instrument.symbol,
            instrument.series,
        ),
    ).fetchone()
    assert row is not None
    written, changed = row
    return int(written), [_anomaly(instrument, values) for values in changed]


def _anomaly(instrument: _Instrument, values: list[str]) -> dict[str, Any]:
    trade_date, received, stored = values[0], values[1:6], values[6:11]
    return {
        "symbol": instrument.symbol,
        "series": instrument.series,
        "trade_date": trade_date,
        "stored": _bar(stored),
        "received": _bar(received),
    }


def _bar(values: list[str]) -> dict[str, Any]:
    open_, high, low, close, volume = values
    return {
        "open": str(Decimal(open_)),
        "high": str(Decimal(high)),
        "low": str(Decimal(low)),
        "close": str(Decimal(close)),
        "volume": int(volume),
    }
