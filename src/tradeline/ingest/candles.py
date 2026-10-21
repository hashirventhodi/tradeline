"""``ingest candles``: Kite's candles into ``candles_daily`` or ``candles_minute``.

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

Which token. Kite's dump lists an instrument under its current name only, so
a symbol is fetched as the Instrument it names (on the range's end, or in the
Universe month) through the token of that Instrument's current Symbol, the
one it was named last, and its bars are stored under that Symbol: ZOMATO is
fetched as ETERNAL, and ``fetched_through`` in the run's details records it.
There is no fallback to an old Symbol's token. A member without a token is
reported with the reason its Instrument has none (``queries.end_reasons``:
its series moved, it merged or was delisted on NSE's record, it ended with no
reason stated, or it still trades and Kite's dump lacks it), in
``without_token_by_reason`` with the name requested and the name looked up.
An Instrument asked for under two of its Symbols is fetched once. A symbol in
no Instrument (none rebuilt yet) is fetched under itself.

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

from tradeline import queries
from tradeline.dates import month_of
from tradeline.ingest import COMMAND, IngestRunCounters, record_ingest_run
from tradeline.logging import log_context
from tradeline.sources import kite

log = logging.getLogger(__name__)

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
    # Each reason's members without a token: the Kite name looked up, the one requested,
    # the Instrument, and the reason's detail (``queries.EndReason``).
    without_token_by_reason: dict[str, list[dict[str, str | None]]]

    def summary(self) -> dict[str, Any]:
        return {
            "symbols_loaded": self.symbols_loaded,
            "symbols_without_token": list(self.symbols_without_token),
            "without_token_by_reason": self.without_token_by_reason,
            "rows_written": self.rows_written,
            "anomalies": self.anomalies,
            "requests": self.requests,
        }


@dataclass(frozen=True)
class _Instrument:
    """What is fetched for a requested (symbol, series): the token of the Instrument's
    current Symbol, the name its bars are stored under."""

    requested: str
    symbol: str
    series: str
    token: int | None
    # The Instrument's key; None for a symbol in no Instrument.
    instrument: str | None


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
        requested, as_of = _universe_members(conn, universe), universe.month
    else:
        requested, as_of = _requested(conn, symbols), end
    instruments = _through_current_symbols(conn, requested, as_of)

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
        without_token_by_reason=run.details["without_token_by_reason"],
    )


def _requested(conn: psycopg.Connection, symbols: Sequence[str] | None) -> list[tuple[str, str]]:
    wanted = None if symbols is None else list(symbols)
    rows = conn.execute(
        "SELECT symbol, series FROM symbols"
        " WHERE CASE WHEN %s::text[] IS NULL THEN series = %s ELSE symbol = ANY(%s::text[]) END"
        " ORDER BY symbol, series",
        (wanted, EQUITY_SERIES, wanted),
    ).fetchall()
    requested = [(symbol, series) for symbol, series in rows]
    if wanted is not None:
        unknown = sorted(set(wanted) - {symbol for symbol, _ in requested})
        if unknown:
            raise ValueError(
                f"symbols not in the instrument master: {', '.join(unknown)};"
                " run `ingest eod` first"
            )
    return requested


def _universe_members(conn: psycopg.Connection, universe: CurrentUniverse) -> list[tuple[str, str]]:
    if universe.month.day != 1:
        raise ValueError(f"a Universe month is given by its first day, not {universe.month}")
    rows = conn.execute(
        "SELECT symbol, series FROM universe_membership"
        " WHERE month = %s AND rule_version = %s"
        " ORDER BY symbol, series",
        (universe.month, universe.rule_version),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"no Universe for {universe.month:%Y-%m} under rule version"
            f" {universe.rule_version[:12]}; run `tradeline universe --month"
            f" {universe.month:%Y-%m}` first"
        )
    return [(symbol, series) for symbol, series in rows]


def _through_current_symbols(
    conn: psycopg.Connection, requested: list[tuple[str, str]], as_of: date
) -> list[_Instrument]:
    """Each requested (symbol, series) as the Instrument the symbol names on ``as_of``,
    fetched under the Symbol that Instrument was named last, with that Symbol's token,
    once each.

    Kite's dump lists an instrument under its current name only, so an old
    Symbol has no token of its own. A symbol in no Instrument (none rebuilt
    yet, or a series the Instruments leave out) is fetched under itself.
    """
    rows = conn.execute(
        "SELECT r.symbol, coalesce(cur.symbol, r.symbol), r.series, s.kite_instrument_token,"
        " m.instrument_key"
        " FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY AS r(symbol, series, n)"
        " LEFT JOIN symbol_instruments m ON m.symbol = r.symbol"
        "  AND (m.after IS NULL OR %s::date > m.after)"
        "  AND (m.through IS NULL OR %s::date <= m.through)"
        " LEFT JOIN LATERAL ("
        "  SELECT c.symbol FROM symbol_instruments c WHERE c.instrument_key = m.instrument_key"
        "  ORDER BY c.named_until DESC, c.symbol LIMIT 1"
        " ) cur ON true"
        " LEFT JOIN symbols s"
        "  ON (s.symbol, s.series) = (coalesce(cur.symbol, r.symbol), r.series)"
        " ORDER BY r.n",
        ([symbol for symbol, _ in requested], [series for _, series in requested], as_of, as_of),
    ).fetchall()
    instruments: dict[tuple[str, str], _Instrument] = {}
    for requested_symbol, symbol, series, token, key in rows:
        instruments.setdefault(
            (symbol, series), _Instrument(requested_symbol, symbol, series, token, key)
        )
    return list(instruments.values())


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
    without_token_by_reason: dict[str, list[dict[str, str | None]]] = {}
    run.details.update(
        {
            "symbols_requested": len(instruments),
            "symbols_without_token": without_token,
            "without_token_by_reason": without_token_by_reason,
            # An old Symbol fetched through its Instrument's current one, by Kite trading symbol.
            "fetched_through": {
                kite.trading_symbol(i.requested, i.series): kite.trading_symbol(i.symbol, i.series)
                for i in instruments
                if i.requested != i.symbol
            },
            "anomalies": 0,
            "anomaly_bars": anomaly_bars,
            "requests": 0,
        }
    )
    tokenless: list[_Instrument] = []
    for instrument in instruments:
        with log_context(symbol=instrument.symbol, series=instrument.series):
            if instrument.token is None:
                log.warning("symbol has no kite instrument token; skipped")
                without_token.append(kite.trading_symbol(instrument.symbol, instrument.series))
                tokenless.append(instrument)
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
    without_token_by_reason.update(_by_reason(conn, tokenless))
    if without_token:
        log.warning(
            "symbols without a kite instrument token",
            extra={"symbols": without_token, "by_reason": without_token_by_reason},
        )
    return loaded


def _by_reason(
    conn: psycopg.Connection, tokenless: list[_Instrument]
) -> dict[str, list[dict[str, str | None]]]:
    """Each Instrument without a token under the reason it has none, in the order fetched."""
    keyed = [(i.instrument, i.series) for i in tokenless if i.instrument is not None]
    found = dict(zip(keyed, queries.end_reasons(conn, keyed), strict=True))
    by_reason: dict[str, list[dict[str, str | None]]] = {}
    for i in tokenless:
        reason = found.get((i.instrument, i.series)) if i.instrument is not None else None
        by_reason.setdefault(queries.NO_INSTRUMENT if reason is None else reason.reason, []).append(
            {
                "symbol": kite.trading_symbol(i.symbol, i.series),
                "requested": kite.trading_symbol(i.requested, i.series),
                "instrument": i.instrument,
                "detail": "" if reason is None else reason.detail,
            }
        )
    return by_reason


_CANDLE_VALUES = ("open", "high", "low", "close", "volume")  # everything but the key


def _insert_candles(
    conn: psycopg.Connection, target: _Target, instrument: _Instrument, candles: list[kite.Candle]
) -> tuple[int, list[dict[str, Any]]]:
    """Insert what is new; return the count and an anomaly record per present bar that differs."""
    if not candles:
        return 0, []
    if target.partitioned_by_month:
        months = sorted({month_of(candle.trade_date) for candle in candles})
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
