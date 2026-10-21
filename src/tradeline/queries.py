"""The public read module.

Every later phase and every test reads the data layer through the functions
here and never from the tables. Each function takes an application connection
and returns frozen rows. Queries grow here as phases add data: adjusted
candles, Universe membership, sectors, announcements.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast
from zoneinfo import ZoneInfo

import psycopg

from tradeline.adjust import KINDS, AdjustedBar, CorporateAction, Kind, RawBar, adjust

# Exchange days are Indian calendar days.
IST = ZoneInfo("Asia/Kolkata")


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
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[CorporateAction]:
    """NSE's corporate actions for ``symbol`` with our factors, oldest ex-date first.

    Every action is returned unless the ex-date range is narrowed at either end.
    """
    rows = conn.execute(
        "SELECT symbol, series, ex_date, subject, kind, factor_numerator, factor_denominator,"
        " isin, record_date, face_value"
        " FROM corporate_actions"
        " WHERE symbol = %s"
        " AND (%s::date IS NULL OR ex_date >= %s) AND (%s::date IS NULL OR ex_date <= %s)"
        " ORDER BY ex_date, series, subject",
        (symbol, start, start, end, end),
    ).fetchall()
    return [_corporate_action(row) for row in rows]


def _corporate_action(row: tuple[Any, ...]) -> CorporateAction:
    symbol, series, ex_date, subject, kind, numerator, denominator, isin, record_date, fv = row
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
    )


def adjusted_daily_candles(
    conn: psycopg.Connection,
    symbol: str,
    *,
    start: date,
    end: date,
    as_of: date,
    series: str | None = None,
) -> list[AdjustedBar]:
    """The Adjusted Series of ``symbol`` from ``start`` to ``end`` as it stood on ``as_of``.

    The Raw Series is NSE's end-of-day rows, the official unadjusted prices
    (``end_of_day``); Kite's candles are already adjusted by Kite's rules and
    are not the input here. The adjustment is computed on every read from the
    raw rows and the stored factors (``tradeline.adjust``) and is never written
    back. Actions are the symbol's under every series, applied once each.

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
    rows = end_of_day(conn, symbol, start=start, end=end, series=series)
    bars = [
        RawBar(r.symbol, r.series, r.trade_date, r.open, r.high, r.low, r.close, r.traded_qty)
        for r in rows
    ]
    return adjust(bars, corporate_actions(conn, symbol), as_of=as_of)


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
    is the one stored under the most recently recorded rule version that has
    any for the month. A month never computed under the version is empty.

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
            " ORDER BY v.recorded_at DESC, v.version LIMIT 1",
            (month,),
        ).fetchone()
        if latest is None:
            return []
        rule_version = latest[0]
    rows = conn.execute(
        "SELECT month, rule_version, rank, symbol, series, traded_value"
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


# Minute bars are stamped in Indian time; a trading day is an Indian calendar day.
IST = ZoneInfo("Asia/Kolkata")


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


@dataclass(frozen=True)
class Sector:
    symbol: str
    sector: str
    effective_from: date


def sector(conn: psycopg.Connection, symbol: str, *, as_of: date) -> Sector | None:
    """NSE's sector for ``symbol`` in force on ``as_of``, or None if none was recorded by then.

    Point in time. NSE publishes only its current classification, so each
    load is in force from the day it was taken (``tradeline.sectors``): a
    date before the first load has no sector, and a date between two loads
    that disagree reads the earlier one.
    """
    found = sectors(conn, as_of=as_of, symbols=[symbol])
    return found[0] if found else None


def sectors(
    conn: psycopg.Connection, *, as_of: date, symbols: list[str] | None = None
) -> list[Sector]:
    """The sector in force on ``as_of`` for every symbol that has one (or for those named)."""
    rows = conn.execute(
        "SELECT DISTINCT ON (symbol) symbol, sector, effective_from FROM sectors"
        " WHERE effective_from <= %s AND (%s::text[] IS NULL OR symbol = ANY(%s))"
        " ORDER BY symbol, effective_from DESC",
        (as_of, symbols, symbols),
    ).fetchall()
    return [Sector(*row) for row in rows]
