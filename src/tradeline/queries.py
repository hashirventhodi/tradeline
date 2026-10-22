"""The public read module.

Every later phase and every test reads the data layer through the functions
here and never from the tables. Each function takes an application connection
and returns frozen rows. Queries grow here as phases add data: adjusted
candles, Universe membership, sectors, announcements.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal, cast

import psycopg

from tradeline.adjust import KINDS, AdjustedBar, CorporateAction, Kind, RawBar, adjust
from tradeline.dates import IST
from tradeline.instruments import Anomaly, Interval, KeyFrom, Link, LinkCandidate
from tradeline.sources.nse import Delisting, SymbolChange


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
    # The ``collect-daily`` collection the run was part of; None when run on its own.
    collection_id: uuid.UUID | None

    @property
    def status(self) -> str:
        """``failed`` when the run ended in an error (or was interrupted), else ``succeeded``."""
        return "failed" if self.error is not None else "succeeded"

    @property
    def duration(self) -> timedelta:
        return self.finished_at - self.started_at


def end_of_day(
    conn: psycopg.Connection,
    instrument: str,
    *,
    start: date,
    end: date,
    series: str | None = None,
    as_of: date | None = None,
) -> list[EndOfDayRow]:
    """NSE end-of-day rows of an Instrument from ``start`` to ``end`` inclusive, oldest first.

    ``instrument`` is an Instrument key, or a Symbol read as the Instrument it
    named on ``as_of`` (``end`` when not given; see ``_owned``). The rows are
    those of every Symbol the Instrument had, each under the Symbol it was
    published under. All series are returned unless one is named; an
    Instrument that moved between series inside the range has rows under each.
    """
    owned = _owned(conn, instrument, as_of=end if as_of is None else as_of)
    return _end_of_day(conn, owned, start=start, end=end, series=series)


def _end_of_day(
    conn: psycopg.Connection, owned: _Owned, *, start: date, end: date, series: str | None
) -> list[EndOfDayRow]:
    rows = conn.execute(
        f"WITH {_OWNED} SELECT e.trade_date, e.symbol, e.series, e.isin, e.open, e.high, e.low,"
        " e.close, e.last, e.prev_close, e.traded_qty, e.traded_value, e.trades"
        " FROM eod_bhavcopy e JOIN owned o ON o.symbol = e.symbol"
        " AND (o.since IS NULL OR e.trade_date >= o.since)"
        " AND (o.until IS NULL OR e.trade_date < o.until)"
        " WHERE e.trade_date BETWEEN %(start)s AND %(end)s"
        " AND (%(series)s::text IS NULL OR e.series = %(series)s)"
        " ORDER BY e.trade_date, e.series, e.symbol",
        {**owned.params(), "start": start, "end": end, "series": series},
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


def ingest_runs(
    conn: psycopg.Connection,
    *,
    source: str | None = None,
    collection_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> list[IngestRun]:
    """Run history, newest first: every run, or one source's, one collection's, or since a time.

    Each run carries its status, row counts, start and finish (so its
    duration), its error, and its source's details.
    """
    if since is not None and since.tzinfo is None:
        raise ValueError(f"since must carry a time zone, not {since}")
    rows = conn.execute(
        "SELECT id, command, source, range_start, range_end, started_at, finished_at,"
        " rows_seen, rows_written, error, details, collection_id"
        " FROM ingest_runs WHERE (%s::text IS NULL OR source = %s)"
        " AND (%s::uuid IS NULL OR collection_id = %s)"
        " AND (%s::timestamptz IS NULL OR started_at >= %s)"
        " ORDER BY started_at DESC, id",
        (source, source, collection_id, collection_id, since, since),
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
    """Kite's daily candles for ``symbol`` from ``start`` to ``end`` inclusive, oldest first.

    Exactly as Kite returned them, which is adjusted by Kite's own rules as of
    the day they were fetched (docs/research.md). Our adjusted series is
    ``adjusted_daily_candles``, computed from NSE's end-of-day rows. All
    series are returned unless one is named, as for ``end_of_day``.
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


def corporate_actions(
    conn: psycopg.Connection,
    instrument: str,
    *,
    start: date | None = None,
    end: date | None = None,
    as_of: date | None = None,
) -> list[CorporateAction]:
    """NSE's Corporate Actions of an Instrument with our factors, oldest Ex-Date first.

    ``instrument`` is an Instrument key, or a Symbol read as the Instrument it
    named on ``as_of`` (``end`` when not given, else the Instrument it last
    named; see ``_owned``). The actions are those filed under any of the
    Instrument's Symbols, each under the Symbol it was filed under, and one
    action filed under two Symbols is returned under each (the Adjusted Series
    applies it once). Every action is returned unless the Ex-Date range is
    narrowed at either end.
    """
    if as_of is None:
        as_of = date.max if end is None else end
    owned = _owned(conn, instrument, as_of=as_of)
    return _corporate_actions(conn, owned, start=start, end=end)


def _corporate_actions(
    conn: psycopg.Connection, owned: _Owned, *, start: date | None, end: date | None
) -> list[CorporateAction]:
    rows = conn.execute(
        f"WITH {_OWNED} SELECT c.symbol, c.series, c.ex_date, c.subject, c.kind,"
        " c.factor_numerator, c.factor_denominator, c.isin, c.record_date, c.face_value, c.nse_list"
        " FROM corporate_actions c JOIN owned o ON o.symbol = c.symbol"
        " AND (o.since IS NULL OR c.ex_date >= o.since)"
        " AND (o.until IS NULL OR c.ex_date < o.until)"
        " WHERE (%(start)s::date IS NULL OR c.ex_date >= %(start)s)"
        " AND (%(end)s::date IS NULL OR c.ex_date <= %(end)s)"
        " ORDER BY c.ex_date, c.series, c.subject, c.symbol",
        {**owned.params(), "start": start, "end": end},
    ).fetchall()
    return [_corporate_action(row) for row in rows]


def adjusting_corporate_actions(conn: psycopg.Connection) -> list[CorporateAction]:
    """Every stored split and bonus (a consolidation is a split), under whatever Symbol NSE
    filed it, by Symbol, then Ex-Date: the actions that can explain an ISIN change."""
    rows = conn.execute(
        "SELECT symbol, series, ex_date, subject, kind, factor_numerator, factor_denominator,"
        " isin, record_date, face_value, nse_list FROM corporate_actions"
        " WHERE kind IN ('split', 'bonus')"
        " ORDER BY symbol, ex_date, series, subject"
    ).fetchall()
    return [_corporate_action(row) for row in rows]


def _corporate_action(row: tuple[Any, ...]) -> CorporateAction:
    (
        symbol,
        series,
        ex_date,
        subject,
        kind,
        numerator,
        denominator,
        isin,
        record_date,
        fv,
        nse_list,
    ) = row
    if kind not in KINDS:
        raise ValueError(f"unknown corporate action kind {kind!r} stored for {symbol}")
    return CorporateAction(
        symbol=symbol,
        series=series,
        ex_date=ex_date,
        subject=subject,
        kind=cast(Kind, kind),
        factor=None if numerator is None else Fraction(int(numerator), int(denominator)),
        isin=isin,
        record_date=record_date,
        face_value=fv,
        nse_list=nse_list,
    )


def adjusted_daily_candles(
    conn: psycopg.Connection,
    instrument: str,
    *,
    start: date,
    end: date,
    as_of: date,
    series: str | None = None,
) -> list[AdjustedBar]:
    """The Adjusted Series of an Instrument from ``start`` to ``end`` as it stood on ``as_of``.

    ``instrument`` is an Instrument key, or a Symbol read as the Instrument it
    named on ``as_of``. The Raw Series is NSE's end-of-day rows of every Symbol
    the Instrument had, each bar under the Symbol it was published under: the
    official unadjusted prices (``end_of_day``). Kite's candles are already
    adjusted by Kite's rules and are not the input here. The adjustment is
    computed on every read from the raw rows and the stored factors
    (``tradeline.adjust``) and is never written back. Actions are those filed
    under any of the Instrument's Symbols, in every series, applied once each.

    Point in time. ``as_of`` is the adjustment reference date: an action is
    applied, or flagged, only if its ex-date is on or before ``as_of``, the
    ex-date being the day the market price itself changed. An action announced
    before ``as_of`` but ex-dated after it is correctly not applied, because
    the prices before its ex-date are still the ones the market quoted. Bars
    after ``as_of`` are refused rather than returned, so a decision replayed
    on ``as_of`` cannot see a later bar. What this does not model: NSE's list
    is read as it stands at ingestion, so an action NSE later revised or
    withdrew is known here in its final form on every ``as_of``.
    """
    if end > as_of:
        raise ValueError(
            f"range end {end} is after the as-of date {as_of}: bars after the as-of date"
            " are not known on it"
        )
    owned = _owned(conn, instrument, as_of=as_of)
    rows = _end_of_day(conn, owned, start=start, end=end, series=series)
    bars = [
        RawBar(r.symbol, r.series, r.trade_date, r.open, r.high, r.low, r.close, r.traded_qty)
        for r in rows
    ]
    return adjust(bars, _corporate_actions(conn, owned, start=None, end=None), as_of=as_of)


@dataclass(frozen=True)
class _Owned:
    """Whose rows an Instrument read returns: an Instrument's, or one Symbol's alone."""

    key: str | None
    # Set only when ``key`` is None: a Symbol no Instrument has had.
    symbol: str | None = None

    def params(self) -> dict[str, str | None]:
        return {"key": self.key, "symbol": self.symbol}


# The (Symbol, days) whose rows are the Instrument's, as ``owned``. A row of a
# Symbol on a day belongs to the Instrument the Symbol named that day, or last
# named before it, as ``resolve_symbol`` reads a Symbol: so rows that arrived
# after the last rebuild are read with the Instrument their Symbol last named.
# A row dated before its Symbol's first interval belongs to the first
# Instrument the Symbol named (``since`` is NULL), because NSE files an
# Instrument's past Corporate Actions under its Symbol of today (Gujarat Gas's
# 2019 split is listed under GUJENERGY, the name it took in 2026). A Symbol no
# Instrument has had is read alone, every day. Unlike the ``symbol_instruments``
# view (migration 0015, for Kite), a day between two runs of a Symbol is the
# earlier Instrument's: NSE dates a row by the name it used that day, Kite by its
# name when fetched.
_OWNED = (
    "starts AS ("
    "  SELECT DISTINCT symbol, first_day, instrument_key FROM instrument_intervals"
    "  WHERE symbol IN (SELECT symbol FROM instrument_intervals WHERE instrument_key = %(key)s)),"
    " spans AS ("
    "  SELECT symbol, instrument_key,"
    "   CASE WHEN first_day > min(first_day) OVER (PARTITION BY symbol) THEN first_day END"
    "    AS since,"
    "   lead(first_day) OVER (PARTITION BY symbol ORDER BY first_day, instrument_key) AS until"
    "  FROM starts),"
    " owned AS ("
    "  SELECT symbol, since, until FROM spans WHERE instrument_key = %(key)s"
    "  UNION ALL SELECT %(symbol)s::text, NULL::date, NULL::date WHERE %(key)s::text IS NULL)"
)


def _owned(conn: psycopg.Connection, instrument: str, *, as_of: date) -> _Owned:
    """An Instrument key (``SYMBOL@YYYY-MM-DD``; no Symbol has an ``@``), or a Symbol resolved
    as of ``as_of``, as ``resolve_symbol`` does.

    An unknown key, and a Symbol that named an Instrument only after
    ``as_of``, are refused with ``NoInstrument``. A Symbol no Instrument has
    had (Instruments not rebuilt since its rows arrived) is read alone.
    """
    if "@" in instrument:
        if conn.execute("SELECT 1 FROM instruments WHERE key = %s", (instrument,)).fetchone():
            return _Owned(instrument)
        raise NoInstrument(_unknown_key(instrument))
    try:
        return _Owned(resolve_symbol(conn, instrument, as_of=as_of).key)
    except NoInstrument:
        had = conn.execute(
            "SELECT 1 FROM instrument_intervals WHERE symbol = %s LIMIT 1", (instrument,)
        ).fetchone()
        if had is not None:
            raise
        return _Owned(None, instrument)


@dataclass(frozen=True)
class Announcement:
    source: str  # 'nse' or 'bse'
    source_id: str
    # The official timestamp: when the exchange made the filing public.
    disseminated_at: datetime
    received_at: datetime | None
    exchange_code: str  # NSE symbol or BSE scrip code, as the exchange gives it
    company_name: str | None
    symbol: str | None  # the matched NSE symbol; None when the match was not certain
    subject: str
    body: str | None
    attachment_url: str | None


_ANNOUNCEMENT_COLUMNS = (
    "source, source_id, disseminated_at, received_at, exchange_code, company_name, symbol,"
    " subject, body, attachment_url"
)


def announcements(
    conn: psycopg.Connection,
    symbol: str | None,
    *,
    start: date,
    end: date,
    as_of: datetime,
) -> list[Announcement]:
    """NSE and BSE announcements for ``symbol`` from ``start`` to ``end``, as known at ``as_of``.

    ``symbol=None`` returns the filings matched to no symbol. Days are Indian
    calendar days of the official timestamp, the moment the exchange
    disseminated the filing; an announcement disseminated after ``as_of`` is
    left out, so a decision replayed at ``as_of`` sees only what was public
    then. Oldest first.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware: announcements are timestamped in IST")
    after = datetime.combine(start, time(), IST)
    before = datetime.combine(end + timedelta(days=1), time(), IST)
    rows = conn.execute(
        f"SELECT {_ANNOUNCEMENT_COLUMNS} FROM announcements"
        f" WHERE {'symbol IS NULL' if symbol is None else 'symbol = %(symbol)s'}"
        " AND disseminated_at >= %(after)s AND disseminated_at < %(before)s"
        " AND disseminated_at <= %(as_of)s"
        " ORDER BY disseminated_at, source, source_id",
        {"symbol": symbol, "after": after, "before": before, "as_of": as_of},
    ).fetchall()
    return [Announcement(*row) for row in rows]


@dataclass(frozen=True)
class UniverseMember:
    month: date
    rule_version: str
    rank: int
    symbol: str
    series: str
    traded_value: Decimal
    # The Instrument ranked, under a rule that ranks Instruments; None under
    # one that ranks Symbols.
    instrument: str | None = None


@dataclass(frozen=True)
class UniverseRuleVersion:
    version: str
    parameters: dict[str, Any]
    recorded_at: datetime


def universe_membership(
    conn: psycopg.Connection,
    month: date,
    *,
    as_of: date,
    rule_version: str | None = None,
) -> list[UniverseMember]:
    """The Universe for the month starting ``month`` (a first day), best rank first.

    ``rule_version`` is a Universe Rule's parameter hash
    (``tradeline.config.UniverseRule.version``); without one, the membership
    is the one stored under the latest rule version recorded on or before
    ``as_of`` (an Indian calendar day) that has any for the month, so a
    version recorded later is not seen. A month never computed under the
    version is empty.

    Point in time. The Universe for a month is computed only from end-of-day
    rows dated before its first trading day, so it is known from that day on.
    An ``as_of`` before the month's first calendar day is refused.
    """
    if month.day != 1:
        raise ValueError(f"a Universe month is given by its first day, not {month}")
    if as_of < month:
        raise ValueError(
            f"the Universe for {month:%Y-%m} is not known on {as_of}, before the month starts"
        )
    if rule_version is None:
        latest = conn.execute(
            "SELECT v.version FROM universe_rule_versions v"
            " WHERE EXISTS (SELECT 1 FROM universe_membership m"
            "  WHERE m.month = %s AND m.rule_version = v.version)"
            " AND v.recorded_at < %s"
            " ORDER BY v.recorded_at DESC, v.version LIMIT 1",
            (month, datetime.combine(as_of + timedelta(days=1), time(), IST)),
        ).fetchone()
        if latest is None:
            return []
        rule_version = latest[0]
    rows = conn.execute(
        "SELECT month, rule_version, rank, symbol, series, traded_value, instrument_key"
        " FROM universe_membership WHERE month = %s AND rule_version = %s ORDER BY rank",
        (month, rule_version),
    ).fetchall()
    return [UniverseMember(*row) for row in rows]


def universe_rule_versions(conn: psycopg.Connection) -> list[UniverseRuleVersion]:
    """Every Universe Rule version recorded, oldest first."""
    rows = conn.execute(
        "SELECT version, parameters, recorded_at FROM universe_rule_versions"
        " ORDER BY recorded_at, version"
    ).fetchall()
    return [UniverseRuleVersion(*row) for row in rows]


@dataclass(frozen=True)
class MinuteCandle:
    ts: datetime  # the minute the bar starts
    symbol: str
    series: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def minute_candles(
    conn: psycopg.Connection, symbol: str, *, day: date, series: str | None = None
) -> list[MinuteCandle]:
    """Kite's minute candles for ``symbol`` on the trading day ``day``, in time order.

    Exactly as Kite returned them, adjusted by Kite's rules as of the day they
    were fetched, like ``daily_candles``. ``ts`` is timezone-aware. All series
    are returned unless one is named.
    """
    opens = datetime.combine(day, time(), IST)
    rows = conn.execute(
        "SELECT ts, symbol, series, open, high, low, close, volume"
        " FROM candles_minute"
        " WHERE symbol = %s AND ts >= %s AND ts < %s"
        " AND (%s::text IS NULL OR series = %s)"
        " ORDER BY ts, series",
        (symbol, opens, opens + timedelta(days=1), series, series),
    ).fetchall()
    return [MinuteCandle(*row) for row in rows]


@dataclass(frozen=True)
class MinutePartition:
    month: date  # the first day of the Indian calendar month it holds
    name: str
    rows: int


def minute_candle_partitions(conn: psycopg.Connection) -> list[MinutePartition]:
    """Every monthly partition of the minute candles with its row count, oldest first.

    The counts are exact and so read every stored bar; this is a report for
    the Operator, not something to call in a loop.
    """
    rows = conn.execute(
        "WITH counts AS ("
        "  SELECT tableoid AS oid, count(*) AS n FROM candles_minute GROUP BY tableoid)"
        " SELECT c.relname, coalesce(counts.n, 0)"
        " FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " LEFT JOIN counts ON counts.oid = i.inhrelid"
        " WHERE i.inhparent = 'candles_minute'::regclass"
        " ORDER BY c.relname"
    ).fetchall()
    return [
        MinutePartition(datetime.strptime(name[-7:], "%Y_%m").date(), name, int(count))
        for name, count in rows
    ]


@dataclass(frozen=True)
class NewsItem:
    feed: str
    title: str
    link: str
    summary: str | None
    published_at: datetime | None
    fetched_at: datetime
    symbols: tuple[str, ...]


def news(
    conn: psycopg.Connection,
    symbol: str | None,
    *,
    start: datetime,
    end: datetime,
    as_of: datetime,
) -> list[NewsItem]:
    """RSS news items dated from ``start`` to ``end`` inclusive that were knowable at ``as_of``.

    With a symbol, only items matched to it; with ``None``, every item. An
    item is dated by its feed's publication timestamp, or, when the feed gave
    none, by when this collector first saw it; it is returned only if that
    date is on or before ``as_of``, so a decision replayed at ``as_of`` sees
    no later story. Oldest first. All three times must carry a zone.

    What this does not model: an item backfilled from a feed after the fact
    is dated by its publication timestamp, not by when it was collected.
    """
    for name, value in (("start", start), ("end", end), ("as_of", as_of)):
        if value.tzinfo is None:
            raise ValueError(f"{name} {value} has no time zone")
    rows = conn.execute(
        "SELECT feed, title, link, summary, published_at, fetched_at, symbols"
        " FROM news_items"
        " WHERE coalesce(published_at, fetched_at) BETWEEN %s AND %s"
        " AND coalesce(published_at, fetched_at) <= %s"
        " AND (%s::text IS NULL OR %s = ANY(symbols))"
        " ORDER BY coalesce(published_at, fetched_at), content_hash",
        (start, end, as_of, symbol, symbol),
    ).fetchall()
    return [
        NewsItem(feed, title, link, summary, published, fetched, tuple(symbols))
        for feed, title, link, summary, published, fetched, symbols in rows
    ]


def symbol_changes(conn: psycopg.Connection, *, symbol: str | None = None) -> list[SymbolChange]:
    """NSE's symbol-change records as published, oldest effective date first: every record,
    or those naming ``symbol`` as the old or the new Symbol."""
    rows = conn.execute(
        "SELECT company_name, old_symbol, new_symbol, effective_date FROM symbol_changes"
        " WHERE %s::text IS NULL OR %s IN (old_symbol, new_symbol)"
        " ORDER BY effective_date, old_symbol, new_symbol",
        (symbol, symbol),
    ).fetchall()
    return [SymbolChange(*row) for row in rows]


def delistings(conn: psycopg.Connection, *, symbol: str | None = None) -> list[Delisting]:
    """NSE's delisted-securities records as published, oldest first: every record, or
    ``symbol``'s."""
    rows = conn.execute(
        "SELECT symbol, company_name, delisted_on, kind FROM delistings"
        " WHERE %s::text IS NULL OR symbol = %s"
        " ORDER BY delisted_on, symbol",
        (symbol, symbol),
    ).fetchall()
    return [Delisting(*row) for row in rows]


class NoInstrument(ValueError):
    """An Instrument key the last rebuild did not make, or a Symbol that named no Instrument
    by the date asked: a refusal, as for any read asked of what is not there."""


def _unknown_key(key: str) -> str:
    return f"no Instrument {key}; run `tradeline rebuild-instruments` if its rows have arrived"


@dataclass(frozen=True)
class Instrument:
    """An Instrument as last rebuilt (``tradeline.rebuild_instruments``)."""

    key: str
    first_symbol: str
    listed_on: date
    # Whether ``listed_on`` is NSE's listing date or the first end-of-day row.
    key_from: KeyFrom


@dataclass(frozen=True)
class InstrumentHistory:
    key: str
    first_symbol: str
    listed_on: date
    key_from: KeyFrom
    # Oldest first: each a run of end-of-day rows under one Symbol, series, and ISIN.
    intervals: tuple[Interval, ...]
    # The Instrument Links that joined them, oldest first, each with its source.
    links: tuple[Link, ...]


def _named_by(symbol: str, day: str) -> str:
    """SQL for the key of the Instrument the Symbol ``symbol`` (an SQL expression) named on
    ``day``, or last named before it; NULL if none. ``resolve_symbol``'s rule."""
    return (
        "SELECT v.instrument_key FROM instrument_intervals v"
        f" WHERE v.symbol = {symbol} AND v.first_day <= {day}"
        " ORDER BY v.first_day DESC, v.series LIMIT 1"
    )


def resolve_symbol(conn: psycopg.Connection, symbol: str, *, as_of: date) -> Instrument:
    """The Instrument ``symbol`` named on ``as_of``, or the one it last named before then.

    A Symbol names at most one Instrument on any day, so this is the
    Instrument of the latest interval under ``symbol`` starting on or before
    ``as_of``: ZOMATO as of today is the Instrument now trading as ETERNAL. A
    Symbol that named no Instrument on or before ``as_of`` is refused with
    ``NoInstrument``, so a Symbol is never read into a past before it existed.
    """
    row = conn.execute(
        "SELECT key, first_symbol, listed_on, key_from FROM instruments"
        f" WHERE key = ({_named_by('%(symbol)s', '%(as_of)s')})",
        {"symbol": symbol, "as_of": as_of},
    ).fetchone()
    if row is None:
        raise NoInstrument(
            f"{symbol} named no Instrument on or before {as_of}; ask as of a later date,"
            " or run `tradeline rebuild-instruments` if its rows arrived since the last rebuild"
        )
    return Instrument(*row)


def instrument_history(conn: psycopg.Connection, key: str) -> InstrumentHistory:
    """An Instrument's Symbol history: its intervals and the links that joined them.

    An unknown key is refused with ``NoInstrument``.
    """
    found = conn.execute(
        "SELECT key, first_symbol, listed_on, key_from FROM instruments WHERE key = %s", (key,)
    ).fetchone()
    if found is None:
        raise NoInstrument(_unknown_key(key))
    links = conn.execute(
        "SELECT kind, on_date, from_symbol, from_isin, to_symbol, to_isin, source"
        " FROM instrument_links WHERE instrument_key = %s"
        " ORDER BY on_date, kind, from_symbol, from_isin, to_symbol, to_isin",
        (key,),
    ).fetchall()
    found_key, first_symbol, listed_on, key_from = found
    return InstrumentHistory(
        key=found_key,
        first_symbol=first_symbol,
        listed_on=listed_on,
        key_from=key_from,
        intervals=_intervals(conn, key),
        links=tuple(Link(*row) for row in links),
    )


def _intervals(conn: psycopg.Connection, key: str) -> tuple[Interval, ...]:
    """An Instrument's intervals, oldest first; none for an unknown key."""
    rows = conn.execute(
        "SELECT symbol, series, isin, first_day, last_day FROM instrument_intervals"
        " WHERE instrument_key = %s ORDER BY first_day, last_day, symbol, series",
        (key,),
    ).fetchall()
    return tuple(Interval(*row) for row in rows)


def instrument_anomalies(conn: psycopg.Connection) -> list[Anomaly]:
    """What the last rebuild would not join, oldest first: unexplained ISIN changes and
    refused links."""
    rows = conn.execute(
        "SELECT kind, symbol, on_date, detail FROM instrument_anomalies"
        " ORDER BY on_date, symbol, kind, detail"
    ).fetchall()
    return [Anomaly(*row) for row in rows]


def link_candidates(conn: psycopg.Connection) -> list[LinkCandidate]:
    """The last rebuild's Link Candidates, oldest new Symbol first."""
    rows = conn.execute(
        "SELECT old_symbol, new_symbol, isin, old_last, new_first FROM instrument_link_candidates"
        " ORDER BY new_first, new_symbol, old_symbol, isin"
    ).fetchall()
    return [LinkCandidate(*row) for row in rows]


EndReasonKind = Literal[
    "series moved", "merged", "delisted", "ended, reason not stated", "still trading"
]
SERIES_MOVED: EndReasonKind = "series moved"
MERGED: EndReasonKind = "merged"
DELISTED: EndReasonKind = "delisted"
ENDED: EndReasonKind = "ended, reason not stated"
STILL_TRADING: EndReasonKind = "still trading"
# A Symbol in no Instrument: none rebuilt yet, so no reason can be read.
WithoutCandlesReason = EndReasonKind | Literal["no Instrument"]
NO_INSTRUMENT: WithoutCandlesReason = "no Instrument"


@dataclass(frozen=True)
class EndReason:
    """Why an Instrument's rows in a series stop, as the stored rows and NSE's records say.

    ``reason`` is one of: ``series moved`` (the Instrument's rows continue in
    another series), ``merged`` or ``delisted`` (it ended, and an official
    record says so: a scheme of amalgamation NSE lists for its last Symbol, or
    NSE's delisted list), ``ended, reason not stated`` (no later rows and no
    such record), or ``still trading`` (rows in the series on the Raw Series'
    last day). Never inferred from a name or a price.
    """

    instrument: str
    series: str
    reason: EndReasonKind
    # The last row in the series (series moved, still trading), or the Instrument's last row.
    last_day: date
    # The Symbol of that last row.
    last_symbol: str
    # Where the rows continue, or the official record; empty where there is none.
    detail: str


# The earliest official record of an ended Instrument's end, under its last
# Symbol, after its last row and before any later Instrument took the Symbol.
_NOT_REUSED = (
    "NOT EXISTS (SELECT 1 FROM instrument_intervals r WHERE r.symbol = %(symbol)s"
    " AND r.first_day > %(last_day)s AND r.first_day <= {on})"
)
_SCHEME = (
    "SELECT ex_date, subject FROM corporate_actions"
    " WHERE symbol = %(symbol)s AND ex_date > %(last_day)s AND subject ILIKE '%%amalgamation%%'"
    " AND (isin IS NULL OR %(isin)s::text IS NULL OR isin = %(isin)s)"
    f" AND {_NOT_REUSED.format(on='ex_date')}"
    " ORDER BY ex_date, subject LIMIT 1"
)
_DELISTING = (
    "SELECT delisted_on, kind FROM delistings"
    " WHERE symbol = %(symbol)s AND delisted_on > %(last_day)s"
    f" AND {_NOT_REUSED.format(on='delisted_on')}"
    " ORDER BY delisted_on LIMIT 1"
)


def end_reasons(conn: psycopg.Connection, wanted: Iterable[tuple[str, str]]) -> list[EndReason]:
    """The reason each (Instrument key, series) in ``wanted`` stops, in the order given.

    An unknown key is refused with ``NoInstrument``.
    """
    return _end_reasons(conn, wanted, instruments_last_day(conn))


def _end_reasons(
    conn: psycopg.Connection, wanted: Iterable[tuple[str, str]], raw_last: date | None
) -> list[EndReason]:
    return [_end_reason(conn, key, series, raw_last) for key, series in wanted]


def _end_reason(
    conn: psycopg.Connection, key: str, series: str, raw_last: date | None
) -> EndReason:
    # Latest first, then by series and Symbol (a stable sort keeps the second order).
    by_name = sorted(_intervals(conn, key), key=lambda i: (i.series, i.symbol))
    intervals = sorted(by_name, key=lambda i: i.last, reverse=True)
    if not intervals:
        raise NoInstrument(_unknown_key(key))
    last = intervals[0]
    if last.last != raw_last:
        return _ended(conn, key, series, last)
    in_series = [i for i in intervals if i.series == series]
    if in_series and in_series[0].last == raw_last:
        return EndReason(key, series, STILL_TRADING, raw_last, in_series[0].symbol, "")
    moved_from = in_series[0] if in_series else last
    after = [i for i in intervals if i.series != series and i.first > moved_from.last]
    onward = min(after, key=lambda i: (i.first, i.series)) if after else last
    return EndReason(
        key,
        series,
        SERIES_MOVED,
        moved_from.last,
        moved_from.symbol,
        f"continues in {onward.series} from {onward.first}",
    )


def _ended(conn: psycopg.Connection, key: str, series: str, last: Interval) -> EndReason:
    params = {"symbol": last.symbol, "last_day": last.last, "isin": last.isin}
    scheme = conn.execute(_SCHEME, params).fetchone()
    if scheme is not None:
        ex_date, subject = scheme
        detail = f"NSE corporate action ex {ex_date}: {subject}"
        return EndReason(key, series, MERGED, last.last, last.symbol, detail)
    delisting = conn.execute(_DELISTING, params).fetchone()
    if delisting is not None:
        delisted_on, kind = delisting
        detail = f"NSE delisted list: {kind} on {delisted_on}"
        return EndReason(key, series, DELISTED, last.last, last.symbol, detail)
    return EndReason(key, series, ENDED, last.last, last.symbol, "")


@dataclass(frozen=True)
class EndedInstrument:
    key: str
    # The Instrument's last end-of-day row, and the Symbol it was under.
    last_day: date
    last_symbol: str
    # ``merged``, ``delisted``, or ``ended, reason not stated`` (``EndReason``), and its record.
    reason: EndReasonKind
    detail: str


def instruments_last_day(conn: psycopg.Connection) -> date | None:
    """The last day of any Instrument's history: where the Raw Series ends."""
    row = conn.execute("SELECT max(last_day) FROM instrument_intervals").fetchone()
    return None if row is None else row[0]


def ended_instruments(conn: psycopg.Connection) -> list[EndedInstrument]:
    """The Instruments whose history stops before the Raw Series does, by key, each with
    the reason an official record gives (``end_reasons``).

    An Instrument with no row on the last day of any Instrument's history has
    ended, as far as the stored rows show: delisted, suspended, absorbed, or
    renamed with no link to its new Symbol. Only NSE's records say which.
    """
    raw_last = instruments_last_day(conn)
    rows = conn.execute(
        "SELECT DISTINCT ON (instrument_key) instrument_key, series"
        " FROM instrument_intervals"
        " WHERE instrument_key NOT IN (SELECT instrument_key FROM instrument_intervals"
        "  WHERE last_day = %s)"
        " ORDER BY instrument_key, last_day DESC, series",
        (raw_last,),
    ).fetchall()
    return [
        EndedInstrument(r.instrument, r.last_day, r.last_symbol, r.reason, r.detail)
        for r in _end_reasons(conn, [(key, series) for key, series in rows], raw_last)
    ]


def members_without_candles(conn: psycopg.Connection) -> list[EndReason]:
    """Every Instrument ever in the Universe, in the series it was ranked in, with no Kite
    daily candle stored for it, each with its end reason; by reason, then key.

    A member is the Instrument it was ranked as, if the last rebuild still has
    it; otherwise (a rule that ranks Symbols, or a stale month whose Instrument
    a later rebuild regrouped) the Instrument its Symbol names in its month. A
    member whose Symbol is in no Instrument is left out: the report reads a
    rebuild.
    """
    rows = conn.execute(
        # One pass over the candles, not one per member.
        "WITH fetched AS MATERIALIZED (SELECT DISTINCT instrument FROM candles_daily_instruments),"
        " members AS (SELECT DISTINCT coalesce(i.key, s.instrument_key) AS key, m.series"
        "  FROM universe_membership m"
        "  LEFT JOIN instruments i ON i.key = m.instrument_key"
        "  LEFT JOIN symbol_instruments s ON i.key IS NULL AND s.symbol = m.symbol"
        "   AND names_on(s.after, s.through, m.month))"
        " SELECT key, series FROM members"
        " WHERE key IS NOT NULL AND key NOT IN (SELECT instrument FROM fetched)"
        " ORDER BY key, series"
    ).fetchall()
    reasons = end_reasons(conn, [(key, series) for key, series in rows])
    return sorted(reasons, key=lambda r: (r.reason, r.instrument, r.series))


@dataclass(frozen=True)
class CandleDisagreement:
    """Two stored copies of one Instrument-day under different Kite names that differ."""

    instrument: str
    trade_date: date
    series: str
    kept: DailyCandle  # under Kite's more recent name, the one read
    other: DailyCandle


@dataclass(frozen=True)
class InstrumentDailyCandles:
    candles: list[DailyCandle]
    disagreements: list[CandleDisagreement]


def instrument_daily_candles(
    conn: psycopg.Connection,
    instrument: str,
    *,
    start: date,
    end: date,
    series: str | None = None,
    as_of: date | None = None,
) -> InstrumentDailyCandles:
    """Kite's daily candles for an Instrument, one per day and series, oldest first.

    ``instrument`` is an Instrument key, or a Symbol read as the Instrument it
    named on ``as_of`` (``end`` when not given), as for ``end_of_day``.

    Kite files an Instrument's history under its name of the day it is
    fetched, so one Instrument-day can be stored under an old name and a new
    one. The copy under Kite's more recent name is read, each stored as Kite
    returned it (``daily_candles``), and any other copy whose values differ
    from it is reported. A Symbol in no Instrument (none rebuilt yet) is read
    as its own key.
    """
    owned = _owned(conn, instrument, as_of=end if as_of is None else as_of)
    key = owned.key or owned.symbol or instrument
    rows = conn.execute(
        "SELECT trade_date, symbol, series, open, high, low, close, volume"
        " FROM candles_daily_instruments"
        " WHERE instrument = %s AND trade_date BETWEEN %s AND %s"
        " AND (%s::text IS NULL OR series = %s)"
        " ORDER BY trade_date, series, named_until DESC NULLS LAST, symbol",
        (key, start, end, series, series),
    ).fetchall()
    candles: list[DailyCandle] = []
    disagreements: list[CandleDisagreement] = []
    for row in rows:
        candle = DailyCandle(*row)
        kept = candles[-1] if candles else None
        if kept is None or (kept.trade_date, kept.series) != (candle.trade_date, candle.series):
            candles.append(candle)
        elif _bar_values(candle) != _bar_values(kept):
            disagreements.append(
                CandleDisagreement(key, candle.trade_date, candle.series, kept, candle)
            )
    return InstrumentDailyCandles(candles, disagreements)


def _bar_values(candle: DailyCandle) -> tuple[Decimal, Decimal, Decimal, Decimal, int]:
    return candle.open, candle.high, candle.low, candle.close, candle.volume


@dataclass(frozen=True)
class StaleUniverseMonth:
    """A stored Universe month a later rebuild of the Instruments regrouped; kept as stored."""

    rule_version: str
    month: date


def stale_universe_months(conn: psycopg.Connection) -> list[StaleUniverseMonth]:
    """Every stale Universe month, by rule version, then month."""
    rows = conn.execute(
        "SELECT rule_version, month FROM universe_stale_months ORDER BY rule_version, month"
    ).fetchall()
    return [StaleUniverseMonth(*row) for row in rows]


@dataclass(frozen=True)
class ObsoleteUniverseKey:
    """A stored Universe member whose Instrument key a later rebuild retired (#35).

    The stored row is kept as it was. ``current_key`` is the member's Instrument
    now: the one its Symbol named on the day before the month, the Symbol in force
    on the window's last day (``resolve_symbol``'s rule). None if no Instrument
    answers for it.
    """

    month: date
    rule_version: str
    symbol: str
    stored_key: str
    current_key: str | None


def obsolete_universe_keys(
    conn: psycopg.Connection, *, month: date | None = None, rule_version: str | None = None
) -> list[ObsoleteUniverseKey]:
    """Every stored member whose key names no Instrument of the last rebuild, by rule
    version, month, then rank; narrowed to one month or rule version if given."""
    rows = conn.execute(
        "SELECT m.month, m.rule_version, m.symbol, m.instrument_key,"
        f" ({_named_by('m.symbol', 'm.month - 1')})"
        " FROM universe_membership m"
        " WHERE m.instrument_key IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM instruments i WHERE i.key = m.instrument_key)"
        " AND (%(month)s::date IS NULL OR m.month = %(month)s)"
        " AND (%(rule_version)s::text IS NULL OR m.rule_version = %(rule_version)s)"
        " ORDER BY m.rule_version, m.month, m.rank",
        {"month": month, "rule_version": rule_version},
    ).fetchall()
    return [ObsoleteUniverseKey(*row) for row in rows]


@dataclass(frozen=True)
class Sector:
    # The Symbol asked about.
    symbol: str
    sector: str
    effective_from: date
    # The Symbol NSE published the sector under: an earlier Symbol of the same
    # Instrument when the load was taken before a rename.
    recorded_under: str


def sector(conn: psycopg.Connection, symbol: str, *, as_of: date) -> Sector | None:
    """NSE's sector for ``symbol`` in force on ``as_of``, or None if none was recorded by then.

    Point in time. NSE publishes only its current classification, so each
    load is in force from the day it was taken (``tradeline.sectors``): a
    date before the first load has no sector, and a date between two loads
    that disagree reads the earlier one.

    Through the Instrument: ``symbol`` resolves to the Instrument it named on
    ``as_of`` (as ``resolve_symbol``), and a sector recorded under any of that
    Instrument's Symbols counts, so a renamed stock keeps the sector NSE gave
    its earlier Symbol. A recorded row belongs to the Instrument its Symbol
    named on the day the row came into force, so a Symbol later reused by
    another company does not lend it the first company's sector. A Symbol
    that names no Instrument on ``as_of`` (none built yet, or a Symbol not yet
    traded) reads the rows recorded under itself alone.
    """
    found = sectors(conn, as_of=as_of, symbols=[symbol])
    return found[0] if found else None


def sectors(
    conn: psycopg.Connection, *, as_of: date, symbols: list[str] | None = None
) -> list[Sector]:
    """The sector in force on ``as_of`` for each Symbol named that has one, found through its
    Instrument as for ``sector``; with none named, for every Symbol a sector is recorded under."""
    rows = conn.execute(
        "WITH asked AS ("
        "  SELECT DISTINCT unnest(coalesce(%(symbols)s::text[], ARRAY("
        "    SELECT symbol FROM sectors WHERE effective_from <= %(as_of)s))) AS symbol"
        "), target AS ("
        f"  SELECT a.symbol, ({_named_by('a.symbol', '%(as_of)s')}) AS instrument_key"
        "  FROM asked a"
        "), recorded AS ("
        "  SELECT s.symbol, s.sector, s.effective_from,"
        f"  ({_named_by('s.symbol', 's.effective_from')}) AS instrument_key"
        "  FROM sectors s WHERE s.effective_from <= %(as_of)s"
        ")"
        " SELECT DISTINCT ON (t.symbol) t.symbol, r.sector, r.effective_from, r.symbol"
        " FROM target t JOIN recorded r ON r.instrument_key = t.instrument_key"
        "  OR (r.symbol = t.symbol AND (t.instrument_key IS NULL OR r.instrument_key IS NULL))"
        " ORDER BY t.symbol, r.effective_from DESC, r.symbol <> t.symbol, r.symbol",
        {"as_of": as_of, "symbols": symbols},
    ).fetchall()
    return [Sector(*row) for row in rows]
