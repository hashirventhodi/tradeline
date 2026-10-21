"""``ingest candles``: raw Kite candles into ``candles_daily`` or ``candles_minute``.

One ingest run (source ``kite_candles_daily`` or ``kite_candles_minute``)
covers every requested symbol over the range. Each symbol is looked up in the
instrument master for its Kite instrument token, fetched in chunks the
historical API accepts for the interval (2000 days of daily bars, 60 of minute
bars) at no more than 3 requests per second (``KiteSession``), and committed
before the next symbol starts, so an interrupted run keeps what it loaded.

Which symbols. Named symbols are loaded under every series they have in the
master, so a symbol that moved between series (``EQ`` to ``BE`` and back) can
be given a continuous history on purpose. A ``CurrentUniverse`` loads the
members of a recorded Universe month under one rule version, each under the
series it was ranked in; minute candles are loaded this way (Phase 1 keeps
minute history for the current Universe only). With neither, every ``EQ``
series symbol with a token is loaded: Phase 1 is scoped to series ``EQ``. A
master row without a token is skipped and reported by its Kite trading symbol
(``BAFNAPH-BE``), the name Kite's instrument dump would have had to list; a
symbol not in the master is refused up front (``ingest eod`` loads the master).

Bars are stored exactly as Kite returned them. A bar already stored is left
as it is: Kite can resend a bar, and a resend whose values differ is an
anomaly, logged and recorded in the run's details with both versions, never
applied silently. ``candles_minute`` is partitioned by month; the month's
partition is created on the first insert into it.
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
SOURCE_MINUTE = "kite_candles_minute"
EQUITY_SERIES = "EQ"
# Anomalies are counted in full; this many are recorded in detail per run.
MAX_RECORDED_ANOMALIES = 100


@dataclass(frozen=True)
class CurrentUniverse:
    """The Universe recorded for ``month`` (its first day) under ``rule_version``."""

    month: date
    rule_version: str


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


@dataclass(frozen=True)
class _Target:
    """Where an interval's bars go and how a bar is keyed there."""

    source: str
    table: str
    key: str  # the column that, with symbol and series, identifies a bar
    key_type: str
    # The key as text in an anomaly record: a date, or a time in Indian time.
    key_text: str
    partitioned_by_month: bool


_TARGETS: dict[str, _Target] = {
    kite.INTERVAL_DAY: _Target(
        source=SOURCE_DAILY,
        table="candles_daily",
        key="trade_date",
        key_type="date",
        key_text="n.trade_date::text",
        partitioned_by_month=False,
    ),
    kite.INTERVAL_MINUTE: _Target(
        source=SOURCE_MINUTE,
        table="candles_minute",
        key="ts",
        key_type="timestamptz",
        key_text="to_char(n.ts AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+05:30\"')",
        partitioned_by_month=True,
    ),
}


def ingest_candles(
    conn: psycopg.Connection,
    session: kite.KiteSession,
    *,
    start: date,
    end: date,
    interval: str = kite.INTERVAL_DAY,
    symbols: Sequence[str] | None = None,
    universe: CurrentUniverse | None = None,
) -> CandleIngestResult:
    """Load ``interval`` candles for ``symbols``, or ``universe``'s members, or every ``EQ``."""
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    if symbols is not None and universe is not None:
        raise ValueError("give symbols or a Universe, not both")
    target = _TARGETS.get(interval)
    if target is None:
        raise ValueError(f"unknown candle interval {interval!r}")
    if universe is not None:
        instruments = _universe_instruments(conn, universe)
    else:
        instruments = _instruments(conn, symbols)

    with record_ingest_run(
        conn, command=COMMAND, source=target.source, range_start=start, range_end=end
    ) as run:
        run.details = {
            "interval": interval,
            "universe": None
            if universe is None
            else {"month": universe.month.isoformat(), "rule_version": universe.rule_version},
        }
        loaded = _load_symbols(conn, session, run, interval, target, instruments, start, end)

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


def _universe_instruments(conn: psycopg.Connection, universe: CurrentUniverse) -> list[_Instrument]:
    if universe.month.day != 1:
        raise ValueError(f"a Universe month is given by its first day, not {universe.month}")
    rows = conn.execute(
        "SELECT m.symbol, m.series, s.kite_instrument_token"
        " FROM universe_membership m LEFT JOIN symbols s USING (symbol, series)"
        " WHERE m.month = %s AND m.rule_version = %s"
        " ORDER BY m.symbol, m.series",
        (universe.month, universe.rule_version),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"no Universe for {universe.month:%Y-%m} under rule version"
            f" {universe.rule_version[:12]}; run `tradeline universe --month"
            f" {universe.month:%Y-%m}` first"
        )
    return [_Instrument(symbol, series, token) for symbol, series, token in rows]


def _load_symbols(
    conn: psycopg.Connection,
    session: kite.KiteSession,
    run: IngestRunCounters,
    interval: str,
    target: _Target,
    instruments: list[_Instrument],
    start: date,
    end: date,
) -> int:
    loaded = 0
    anomalies = 0
    anomaly_bars: list[dict[str, Any]] = []
    without_token: list[str] = []
    run.details.update(
        {
            "symbols_requested": len(instruments),
            "symbols_without_token": without_token,
            "anomalies": 0,
            "anomaly_bars": anomaly_bars,
            "requests": 0,
        }
    )
    for instrument in instruments:
        with log_context(symbol=instrument.symbol, series=instrument.series):
            if instrument.token is None:
                log.warning("symbol has no kite instrument token; skipped")
                without_token.append(kite.trading_symbol(instrument.symbol, instrument.series))
                continue
            before = session.requests_sent
            candles = kite.fetch_candles(
                session, instrument.token, interval=interval, start=start, end=end
            )
            written, changed = _insert_candles(conn, target, instrument, candles)
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
                "loaded candles",
                extra={
                    "interval": interval,
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
    conn: psycopg.Connection, target: _Target, instrument: _Instrument, candles: list[kite.Candle]
) -> tuple[int, list[dict[str, Any]]]:
    """Insert what is new; return the count and an anomaly record per present bar that differs."""
    if not candles:
        return 0, []
    if target.partitioned_by_month:
        months = sorted({candle.trade_date.replace(day=1) for candle in candles})
        conn.execute(
            "SELECT candles_minute_partition(month) FROM unnest(%s::date[]) AS month", (months,)
        )
    keys = [candle.trade_date if target.key == "trade_date" else candle.ts for candle in candles]
    key, table = target.key, target.table
    stored = ", ".join(f"c.{name}" for name in _CANDLE_VALUES)
    incoming = ", ".join(f"n.{name}" for name in _CANDLE_VALUES)
    row = conn.execute(
        "WITH incoming AS ("
        f"  SELECT * FROM unnest(%s::{target.key_type}[], %s::numeric[], %s::numeric[],"
        "   %s::numeric[], %s::numeric[], %s::bigint[])"
        f"   AS n({key}, open, high, low, close, volume)),"
        " inserted AS ("
        f"  INSERT INTO {table} (symbol, series, {key}, open, high, low, close, volume)"
        f"  SELECT %s, %s, {key}, open, high, low, close, volume FROM incoming"
        f"  ON CONFLICT (symbol, series, {key}) DO NOTHING RETURNING 1)"
        " SELECT (SELECT count(*) FROM inserted),"
        f"  coalesce(array_agg(ARRAY[{target.key_text}, n.open::text, n.high::text,"
        "   n.low::text, n.close::text, n.volume::text, c.open::text, c.high::text,"
        f"   c.low::text, c.close::text, c.volume::text] ORDER BY n.{key})"
        f"   FILTER (WHERE c.{key} IS NOT NULL), '{{}}')"
        f" FROM incoming n LEFT JOIN {table} c"
        f"  ON (c.symbol, c.series, c.{key}) = (%s, %s, n.{key})"
        f"  AND ({stored}) IS DISTINCT FROM ({incoming})",
        (
            keys,
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
    return int(written), [_anomaly(target, instrument, values) for values in changed]


def _anomaly(target: _Target, instrument: _Instrument, values: list[str]) -> dict[str, Any]:
    key, received, stored = values[0], values[1:6], values[6:11]
    return {
        "symbol": instrument.symbol,
        "series": instrument.series,
        target.key: key,
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
