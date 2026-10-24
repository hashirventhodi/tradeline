"""The public read module.

Every later phase and every test reads the data layer through the functions
here and never from the tables. Each function takes an application connection
and returns frozen rows. Queries grow here as phases add data: adjusted
candles, Universe membership, sectors, announcements.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal, TypedDict, cast

import psycopg

from tradeline.adjust import (
    KINDS,
    AdjustedBar,
    CorporateAction,
    Kind,
    RawBar,
    Supersession,
    adjust,
    superseded,
)
from tradeline.costs import Charges
from tradeline.dates import IST, month_end
from tradeline.instruments import Anomaly, Interval, KeyFrom, Link, LinkCandidate
from tradeline.paper_broker import ExitReason, OrderPurpose
from tradeline.protocol import Protocol
from tradeline.sources.nse import Delisting, SymbolChange
from tradeline.trading_calendar import TradingCalendar


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


def end_of_day_dates(conn: psycopg.Connection, *, start: date, end: date) -> list[date]:
    """The days from ``start`` to ``end`` inclusive that have an end-of-day file, oldest first.

    A day has its file when any row of it is stored: ``ingest eod`` loads a
    file whole or not at all.
    """
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM eod_bhavcopy WHERE trade_date BETWEEN %s AND %s"
        " ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return [day for (day,) in rows]


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


@dataclass(frozen=True)
class CandleRequest:
    """One range candle ingest asked Kite for, under the Symbol it fetched."""

    ingest_run_id: uuid.UUID
    symbol: str
    series: str
    interval: str
    start: date
    end: date


@dataclass(frozen=True)
class RequestedRange:
    """Days, ``start`` to ``end`` inclusive, that candle ingest has asked Kite for."""

    start: date
    end: date

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


def candle_requests(conn: psycopg.Connection, symbol: str, *, series: str) -> list[CandleRequest]:
    """Every range of daily candles asked of Kite for ``symbol`` in ``series``, in the order
    asked; Kite Parity reads only the daily ones.

    One per request, so a range longer than Kite serves at once is several,
    and a re-run asks, and is recorded, again."""
    rows = conn.execute(
        "SELECT ingest_run_id, symbol, series, interval, range_start, range_end"
        " FROM candle_requests WHERE (symbol, series, interval) = (%s, %s, 'day')"
        " ORDER BY requested_at, range_start",
        (symbol, series),
    ).fetchall()
    return [CandleRequest(*row) for row in rows]


def requested_candle_ranges(
    conn: psycopg.Connection, symbol: str, *, series: str
) -> list[RequestedRange]:
    """The days ever asked of Kite for daily candles of ``symbol`` in ``series``, merged,
    oldest first.

    Overlapping and adjoining requests read as one range; whether Kite
    returned bars for them does not matter."""
    merged: list[RequestedRange] = []
    for request in sorted(
        candle_requests(conn, symbol, series=series),
        key=lambda r: (r.start, r.end),
    ):
        if merged and request.start <= merged[-1].end + timedelta(days=1):
            last = merged[-1]
            merged[-1] = RequestedRange(last.start, max(last.end, request.end))
        else:
            merged.append(RequestedRange(request.start, request.end))
    return merged


def corporate_actions(
    conn: psycopg.Connection,
    instrument: str,
    *,
    start: date | None = None,
    end: date | None = None,
    as_of: date | None = None,
) -> list[CorporateAction]:
    """The Corporate Actions of an Instrument with our factors, oldest Ex-Date first: NSE's
    listed ones, and those cited from NSE's circulars (``tradeline.cited_actions``).

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
        " c.factor_numerator, c.factor_denominator, c.isin, c.record_date, c.face_value,"
        " c.nse_list, c.citation"
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
    """Every stored split and bonus (a consolidation is a split), listed or cited, under
    whatever Symbol it is filed, by Symbol, then Ex-Date: the actions that can explain an
    ISIN change."""
    rows = conn.execute(
        "SELECT symbol, series, ex_date, subject, kind, factor_numerator, factor_denominator,"
        " isin, record_date, face_value, nse_list, citation FROM corporate_actions"
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
        citation,
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
        citation=citation,
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
    under any of the Instrument's Symbols, in every series, applied once each;
    a cited action that NSE's list has within a week is not applied at all.

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


class AdjustedSeriesSpan:
    """An Instrument's Raw Series from ``start`` to ``end``, loaded once, read as the Adjusted
    Series as it stood on any day in the span (``adjusted_series_span``).

    A read is what ``adjusted_daily_candles`` returns for the same range and As-Of
    Date, and is refused the same way when the range ends after the As-Of Date: the
    rows past it are held, never returned. The Adjusted Series changes only on an
    Ex-Date, so it is adjusted once for each set of actions known, not on every read.
    """

    def __init__(
        self,
        instrument: str,
        *,
        start: date,
        end: date,
        bars: Sequence[RawBar],
        actions: Sequence[CorporateAction],
    ) -> None:
        self.instrument = instrument
        self.start, self.end = start, end
        self._raw = list(bars)
        self._actions = sorted(actions, key=lambda a: a.ex_date)
        self._ex_dates = [a.ex_date for a in self._actions]
        self._known = -1
        self._adjusted: list[AdjustedBar] = []
        self._dates: list[date] = []

    def bars(self, *, start: date, end: date, as_of: date) -> list[AdjustedBar]:
        if end > as_of:
            raise ValueError(
                f"range end {end} is after the as-of date {as_of}: bars after the as-of date"
                " are not known on it"
            )
        if start < self.start or as_of > self.end:
            raise ValueError(
                f"{start} to {end} as of {as_of} is outside {self.instrument}'s span"
                f" {self.start} to {self.end}"
            )
        known = bisect_right(self._ex_dates, as_of)
        if known != self._known:
            self._adjusted = adjust(self._raw, self._actions, as_of=as_of)
            self._dates = [b.trade_date for b in self._adjusted]
            self._known = known
        return self._adjusted[bisect_left(self._dates, start) : bisect_right(self._dates, end)]


def adjusted_series_span(
    conn: psycopg.Connection, owner: str, *, start: date, end: date
) -> AdjustedSeriesSpan:
    """The Raw Series from ``start`` to ``end``, and every action filed under its Symbols, of
    what ``instrument_key`` reads an Instrument as on a day: an Instrument key, or a Symbol no
    Instrument has had. For reading as of many days (a backtest's); a Symbol that names an
    Instrument does so only as of a day, so it is refused."""
    if "@" in owner:
        owned = _owned(conn, owner, as_of=end)
    elif conn.execute(
        "SELECT 1 FROM instrument_intervals WHERE symbol = %s LIMIT 1", (owner,)
    ).fetchone():
        raise ValueError(f"{owner} names an Instrument; read it by its key (instrument_key)")
    else:
        owned = _Owned(None, owner)
    rows = _end_of_day(conn, owned, start=start, end=end, series=None)
    bars = [
        RawBar(r.symbol, r.series, r.trade_date, r.open, r.high, r.low, r.close, r.traded_qty)
        for r in rows
    ]
    actions = _corporate_actions(conn, owned, start=None, end=None)
    return AdjustedSeriesSpan(owner, start=start, end=end, bars=bars, actions=actions)


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


def instrument_key(conn: psycopg.Connection, instrument: str, *, as_of: date) -> str:
    """What an Instrument read reads ``instrument`` as on ``as_of`` (``_owned``): its
    Instrument's key, or a Symbol no Instrument has had, standing for its own."""
    owned = _owned(conn, instrument, as_of=as_of)
    if owned.key is not None:
        return owned.key
    if owned.symbol is None:
        raise ValueError("an owner names an Instrument key or a Symbol")
    return owned.symbol


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


@dataclass(frozen=True)
class EverMember:
    """A Symbol stored in some Universe month, under any rule version, and its Instrument now.

    ``instrument`` is the stored key when the last rebuild still has it, else the
    Instrument the Symbol named on the day before the month (``resolve_symbol``'s
    rule): a rule ranking Symbols stores no key, and a later rebuild can retire
    one (``obsolete_universe_keys``). None when no Instrument answers for it.
    """

    symbol: str
    instrument: str | None


def universe_ever_members(conn: psycopg.Connection) -> list[EverMember]:
    """Every distinct (Symbol, Instrument) of every stored Universe month, by Instrument key
    (those with none last), then Symbol."""
    rows = conn.execute(
        "SELECT DISTINCT m.symbol, CASE WHEN EXISTS"
        "  (SELECT 1 FROM instruments i WHERE i.key = m.instrument_key) THEN m.instrument_key"
        f"  ELSE ({_named_by('m.symbol', 'm.month - 1')}) END AS instrument"
        " FROM universe_membership m ORDER BY instrument, m.symbol"
    ).fetchall()
    return [EverMember(*row) for row in rows]


def universe_rule_versions(conn: psycopg.Connection) -> list[UniverseRuleVersion]:
    """Every Universe Rule version recorded, oldest first."""
    rows = conn.execute(
        "SELECT version, parameters, recorded_at FROM universe_rule_versions"
        " ORDER BY recorded_at, version"
    ).fetchall()
    return [UniverseRuleVersion(*row) for row in rows]


@dataclass(frozen=True)
class RuleValidity:
    """The Operator's declaration that a rule version is the rule in force from a date."""

    rule_version: str
    in_force_from: date
    declared_at: datetime


def universe_rule_validity(conn: psycopg.Connection) -> list[RuleValidity]:
    """Every Rule Validity declaration, in the order declared."""
    rows = conn.execute(
        "SELECT rule_version, in_force_from, declared_at FROM universe_rule_validity"
        " ORDER BY declared_at, in_force_from, rule_version"
    ).fetchall()
    return [RuleValidity(*row) for row in rows]


UniverseMode = Literal["recorded", "reconstructed"]


def universe_month_mode(
    conn: psycopg.Connection, month: date, *, rule_version: str, calendar: TradingCalendar
) -> UniverseMode:
    """Whether the month's Universe under ``rule_version`` is a Recorded or a Reconstructed one.

    Recorded when, by what was declared before the month's first trading day
    (on ``calendar``) began in India, the version was the one in force on that
    day (the declaration with the latest ``in_force_from`` on or before it), and
    the version was recorded before then too; reconstructed otherwise.
    """
    if month.day != 1:
        raise ValueError(f"a Universe month is given by its first day, not {month}")
    first = next(iter(calendar.trading_days(month, month_end(month))), None)
    if first is None:
        raise ValueError(f"the Trading Calendar has no trading day in {month:%Y-%m}")
    began = datetime.combine(first, time(), IST)
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM universe_rule_versions"
        "  WHERE version = %(version)s AND recorded_at < %(began)s)"
        " AND %(version)s IS NOT DISTINCT FROM (SELECT rule_version FROM universe_rule_validity"
        "  WHERE declared_at < %(began)s AND in_force_from <= %(first)s"
        "  ORDER BY in_force_from DESC, declared_at DESC LIMIT 1)",
        {"version": rule_version, "began": began, "first": first},
    ).fetchone()
    return "recorded" if row is not None and row[0] else "reconstructed"


@dataclass(frozen=True)
class UniverseMonthBuild:
    """What one month's Universe under one rule version was built from (ADR 0005)."""

    month: date
    rule_version: str
    window_start: date
    # The Data Cutoff: the last end-of-day date in the window.
    data_cutoff: date
    # The nse_eod ingest runs that wrote rows in the window, oldest first.
    eod_run_ids: tuple[uuid.UUID, ...]
    # The last Instruments rebuild before the build; None if none had run.
    instruments_run_id: uuid.UUID | None
    eod_hash: str
    links_hash: str
    # True when no recorded ingest run covers the window, so a backfill read the
    # cutoff from the rows there at the time.
    cutoff_inferred: bool
    built_at: datetime


def universe_month_builds(
    conn: psycopg.Connection, *, month: date | None = None, rule_version: str | None = None
) -> list[UniverseMonthBuild]:
    """Every recorded month build, or one month's, or one version's, by month then version."""
    rows = conn.execute(
        "SELECT month, rule_version, window_start, data_cutoff, eod_run_ids,"
        " instruments_run_id, eod_hash, links_hash, cutoff_inferred, built_at"
        " FROM universe_month_builds"
        " WHERE (%(month)s::date IS NULL OR month = %(month)s)"
        " AND (%(version)s::text IS NULL OR rule_version = %(version)s)"
        " ORDER BY month, rule_version",
        {"month": month, "version": rule_version},
    ).fetchall()
    return [
        UniverseMonthBuild(
            month=row[0],
            rule_version=row[1],
            window_start=row[2],
            data_cutoff=row[3],
            eod_run_ids=tuple(row[4]),
            instruments_run_id=row[5],
            eod_hash=row[6],
            links_hash=row[7],
            cutoff_inferred=row[8],
            built_at=row[9],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class UniverseMonthHash:
    """A month window's hashes over the rows there now, and whether its build recorded them."""

    eod_hash: str
    links_hash: str
    matches: bool


def universe_month_hash(conn: psycopg.Connection, build: UniverseMonthBuild) -> UniverseMonthHash:
    """Recompute the hashes of ``build``'s window from the rows stored now.

    A mismatch means an input changed after the build: an end-of-day row in
    the window, or an Instrument interval holding one of its days.
    """
    eod, links = universe_window_hashes(conn, start=build.window_start, cutoff=build.data_cutoff)
    return UniverseMonthHash(
        eod_hash=eod,
        links_hash=links,
        matches=(eod, links) == (build.eod_hash, build.links_hash),
    )


def _row_text(columns: Sequence[str]) -> str:
    """SQL for one row as text in a hash: every column, NULL as ``\\N``, ``|`` between."""
    return "concat_ws('|', " + ", ".join(f"coalesce(({c})::text, '\\N')" for c in columns) + ")"


def _sha256_of(row_text: str, order: str) -> str:
    return (
        "encode(sha256(convert_to(coalesce("
        f"string_agg({row_text}, E'\\n' ORDER BY {order}), ''), 'UTF8')), 'hex')"
    )


_EOD_HASH = _sha256_of(
    _row_text(
        (
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
    ),
    'trade_date, symbol COLLATE "C", series COLLATE "C"',
)
_LINKS_HASH = _sha256_of(
    _row_text(
        (
            "i.instrument_key",
            "i.symbol",
            "i.series",
            "i.isin",
            "i.first_day",
            "i.last_day",
            "n.listed_on",
            "n.key_from",
        )
    ),
    'i.symbol COLLATE "C", i.series COLLATE "C", i.first_day',
)


def universe_window_hashes(
    conn: psycopg.Connection, *, start: date, cutoff: date
) -> tuple[str, str]:
    """SHA-256 of the end-of-day rows dated ``start`` to ``cutoff``, and of the Instrument
    intervals overlapping those days with their Instruments' listing dates.

    Each is over every column of the rows as text, in byte order, so two
    databases holding the same rows give the same hashes.
    """
    row = conn.execute(
        f"SELECT (SELECT {_EOD_HASH} FROM eod_bhavcopy"
        "  WHERE trade_date BETWEEN %(start)s AND %(cutoff)s),"
        f" (SELECT {_LINKS_HASH} FROM instrument_intervals i"
        "  JOIN instruments n ON n.key = i.instrument_key"
        "  WHERE i.first_day <= %(cutoff)s AND i.last_day >= %(start)s)",
        {"start": start, "cutoff": cutoff},
    ).fetchone()
    if row is None:
        raise AssertionError("an aggregate query returns a row")
    return row[0], row[1]


def adjusted_series_hash(
    conn: psycopg.Connection, instrument: str, *, start: date, end: date
) -> str:
    """SHA-256 of an Instrument's Adjusted Series from ``start`` to ``end`` as it stood on
    ``end``: each bar's day, Symbol, series, prices, volume, cumulative factor and later
    Flagged Actions, oldest first.

    Every Run as of a day in the range reads bars derived from these raw rows
    and a subset of these actions, so an end-of-day row or a split or bonus
    that changes in the range changes the hash.
    """
    digest = hashlib.sha256()
    for bar in adjusted_daily_candles(conn, instrument, start=start, end=end, as_of=end):
        flagged = ",".join(f"{a.ex_date}:{a.kind}" for a in bar.unadjusted)
        line = "|".join(
            str(value)
            for value in (
                bar.trade_date,
                bar.symbol,
                bar.series,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.factor,
                flagged,
            )
        )
        digest.update(line.encode() + b"\n")
    return digest.hexdigest()


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


@dataclass(frozen=True)
class ListedAction:
    """One of NSE's listed splits and bonuses, as a superseded Cited Action names it."""

    ex_date: date
    subject: str


@dataclass(frozen=True)
class SupersededCitedAction:
    """The Cited Actions of one Symbol and Ex-Date that NSE's listed splits and bonuses
    within seven days supersede (``adjust.superseded``)."""

    symbol: str
    ex_date: date
    subjects: tuple[str, ...]
    # By Ex-Date.
    listed: tuple[ListedAction, ...]
    cited_factor: Fraction
    listed_factor: Fraction

    @property
    def agrees(self) -> bool:
        """Whether NSE's list prices the change as the Cited Actions do; if not, an anomaly."""
        return self.cited_factor == self.listed_factor

    def record(self) -> dict[str, Any]:
        """As a run's details and the log state it."""
        return {
            "symbol": self.symbol,
            "ex_date": self.ex_date.isoformat(),
            "subjects": list(self.subjects),
        } | self.listed_record()

    def listed_record(self) -> dict[str, Any]:
        """The listed actions and both factors, as ``record`` states them."""
        return {
            "listed": [
                {"ex_date": a.ex_date.isoformat(), "subject": a.subject} for a in self.listed
            ],
            "cited_factor": str(self.cited_factor),
            "listed_factor": str(self.listed_factor),
        }


def agree_and_anomalies(superseded: Iterable[SupersededCitedAction]) -> dict[str, int]:
    """Superseded Cited Actions counted as every report states them: those NSE's list prices
    as they do, and the anomalies."""
    agrees = [s.agrees for s in superseded]
    return {"agree": sum(agrees), "anomalies": len(agrees) - sum(agrees)}


def superseded_cited_actions(conn: psycopg.Connection) -> list[SupersededCitedAction]:
    """The stored Cited Actions NSE's list superseded at the last rebuild, oldest Ex-Date
    first."""
    rows = conn.execute(
        "SELECT symbol, ex_date, subjects, listed_ex_dates, listed_subjects,"
        " cited_numerator, cited_denominator, listed_numerator, listed_denominator"
        " FROM cited_action_supersessions ORDER BY ex_date, symbol"
    ).fetchall()
    return [
        SupersededCitedAction(
            symbol=symbol,
            ex_date=ex_date,
            subjects=tuple(subjects),
            listed=tuple(ListedAction(*a) for a in zip(listed_days, listed_subjects, strict=True)),
            cited_factor=Fraction(cited_n, cited_d),
            listed_factor=Fraction(listed_n, listed_d),
        )
        for (
            symbol,
            ex_date,
            subjects,
            listed_days,
            listed_subjects,
            cited_n,
            cited_d,
            listed_n,
            listed_d,
        ) in rows
    ]


def cited_supersession(
    conn: psycopg.Connection,
    symbol: str,
    ex_date: date,
    cited: Iterable[CorporateAction] = (),
) -> SupersededCitedAction | None:
    """How NSE's list supersedes the Cited Actions of ``symbol`` on ``ex_date``, if it does
    (``adjust.superseded``), as the Instruments stand now.

    The actions read are those of the Instrument the Symbol named on the
    Ex-Date, the reach the Adjusted Series uses, with ``cited``, any not stored
    yet, beside them. A Symbol that named no Instrument by the Ex-Date is
    refused with ``NoInstrument``. A stored Cited Action's has, since its price
    check read the Symbol's close that day, unless the row was stored past the
    check.
    """
    for found in superseded([*corporate_actions(conn, symbol, as_of=ex_date), *cited]):
        if found.ex_date == ex_date:
            return _superseded_cited_action(symbol, found)
    return None


def _superseded_cited_action(symbol: str, found: Supersession) -> SupersededCitedAction:
    return SupersededCitedAction(
        symbol=symbol,
        ex_date=found.ex_date,
        subjects=tuple(a.subject for a in found.cited),
        listed=tuple(ListedAction(a.ex_date, a.subject) for a in found.listed),
        cited_factor=found.cited_factor,
        listed_factor=found.listed_factor,
    )


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
    conn: psycopg.Connection,
    *,
    as_of: date,
    symbols: list[str] | None = None,
    classified_on: date | None = None,
) -> list[Sector]:
    """The sector in force on ``as_of`` for each Symbol named that has one, found through its
    Instrument as for ``sector``; with none named, for every Symbol a sector is recorded under.

    ``classified_on`` reads the classification in force on that day instead, the Symbols
    still resolved to their Instruments on ``as_of``: the Risk Engine reads the first load
    for a day before it (``sectors_for_run``)."""
    rows = conn.execute(
        "WITH asked AS ("
        "  SELECT DISTINCT unnest(coalesce(%(symbols)s::text[], ARRAY("
        "    SELECT symbol FROM sectors WHERE effective_from <= %(classified_on)s))) AS symbol"
        "), target AS ("
        f"  SELECT a.symbol, ({_named_by('a.symbol', '%(as_of)s')}) AS instrument_key"
        "  FROM asked a"
        "), recorded AS ("
        "  SELECT s.symbol, s.sector, s.effective_from,"
        f"  ({_named_by('s.symbol', 's.effective_from')}) AS instrument_key"
        "  FROM sectors s WHERE s.effective_from <= %(classified_on)s"
        # Only rows that can match: under an asked Symbol, or a Symbol of an asked Instrument.
        "  AND (s.symbol IN (SELECT symbol FROM asked) OR s.symbol IN ("
        "   SELECT i.symbol FROM instrument_intervals i"
        "   WHERE i.instrument_key IN (SELECT instrument_key FROM target)))"
        ")"
        " SELECT DISTINCT ON (t.symbol) t.symbol, r.sector, r.effective_from, r.symbol"
        " FROM target t JOIN recorded r ON r.instrument_key = t.instrument_key"
        "  OR (r.symbol = t.symbol AND (t.instrument_key IS NULL OR r.instrument_key IS NULL))"
        " ORDER BY t.symbol, r.effective_from DESC, r.symbol <> t.symbol, r.symbol",
        {
            "as_of": as_of,
            "symbols": symbols,
            "classified_on": as_of if classified_on is None else classified_on,
        },
    ).fetchall()
    return [Sector(*row) for row in rows]


def sectors_for_run(conn: psycopg.Connection, symbols: list[str], *, as_of: date) -> list[Sector]:
    """The sector the Risk Engine reads for each Symbol named, in a Run as of ``as_of``.

    NSE publishes only its current classification, so a Run on a day before the
    first load reads the earliest load (#72; a backtest borrows it, and the report
    says so); on or after it, the load in force on ``as_of``, as ``sectors``. A Symbol
    without one is Unclassified, which the caller decides."""
    row = conn.execute("SELECT min(effective_from) FROM sectors").fetchone()
    first: date | None = None if row is None else row[0]
    classified_on = as_of if first is None or first <= as_of else first
    return sectors(conn, as_of=as_of, symbols=symbols, classified_on=classified_on)


# --- The Ledger (ADR 0019) --------------------------------------------------

EngineMode = Literal["backtest", "paper", "live"]


@dataclass(frozen=True)
class LedgerStrategy:
    """A Strategy version as the Ledger records it for one mode (and backtest)."""

    id: uuid.UUID
    mode: EngineMode
    backtest_id: uuid.UUID | None
    family: str
    name: str
    code_revision: str
    # The hash of the Protocol it ran under (every backtest has one); None for a Run
    # outside one, such as a paper Run.
    protocol: str | None
    ranking: str


@dataclass(frozen=True)
class LedgerRun:
    id: uuid.UUID
    mode: EngineMode
    backtest_id: uuid.UUID | None
    strategy: LedgerStrategy
    as_of: date
    universe_month: date
    universe_rule_version: str
    universe_size: int
    parameters: dict[str, str]
    recorded_at: datetime
    # The account after the day's session: cash, and Equity (cash plus the open Positions
    # at the day's adjusted close), what the Run sized against. None before #78.
    cash: Decimal | None = None
    equity: Decimal | None = None
    # A paper dry-run's Run (#80), which the Paper Gate never counts.
    dry_run: bool = False
    # The Slippage impact c it was priced with in place of its Protocol's (a report's c
    # sensitivity, #79); None for the Protocol's own.
    impact: Decimal | None = None


@dataclass(frozen=True)
class LedgerCandidate:
    run_id: uuid.UUID
    rank: int
    instrument: str
    symbol: str
    # What the Strategy ranked by (its ``ranking`` says how).
    score: Decimal
    facts: dict[str, Decimal]


@dataclass(frozen=True)
class LedgerPlan:
    run_id: uuid.UUID
    rank: int
    instrument: str
    symbol: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    time_stop_days: int
    facts: dict[str, Decimal]


def runs(
    conn: psycopg.Connection,
    *,
    mode: EngineMode | None = None,
    backtest_id: uuid.UUID | None = None,
    as_of: date | None = None,
    dry_run: bool | None = None,
) -> list[LedgerRun]:
    """Runs in the Ledger, filtered by whichever of mode, backtest, day and dry-run marker
    are given; in day order, then in the order they were recorded."""
    rows = conn.execute(
        "SELECT r.id, r.mode, r.backtest_id, s.id, s.mode, s.backtest_id, s.family, s.name,"
        " s.code_revision, s.protocol, s.ranking, r.as_of, r.universe_month,"
        " r.universe_rule_version, r.universe_size, r.parameters, r.recorded_at, r.cash,"
        " r.equity, r.dry_run, r.impact"
        " FROM runs r JOIN strategies s ON (s.mode, s.id) = (r.mode, r.strategy_id)"
        " WHERE (%(mode)s::text IS NULL OR r.mode = %(mode)s)"
        " AND (%(backtest_id)s::uuid IS NULL OR r.backtest_id = %(backtest_id)s)"
        " AND (%(as_of)s::date IS NULL OR r.as_of = %(as_of)s)"
        " AND (%(dry_run)s::boolean IS NULL OR r.dry_run = %(dry_run)s)"
        " ORDER BY r.as_of, r.recorded_at, r.id",
        {"mode": mode, "backtest_id": backtest_id, "as_of": as_of, "dry_run": dry_run},
    ).fetchall()
    return [
        LedgerRun(
            id=row[0],
            mode=row[1],
            backtest_id=row[2],
            strategy=LedgerStrategy(*row[3:11]),
            as_of=row[11],
            universe_month=row[12],
            universe_rule_version=row[13],
            universe_size=row[14],
            parameters=row[15],
            recorded_at=row[16],
            cash=row[17],
            equity=row[18],
            dry_run=row[19],
            impact=row[20],
        )
        for row in rows
    ]


def _decimals(facts: dict[str, str]) -> dict[str, Decimal]:
    return {name: Decimal(value) for name, value in facts.items()}


def candidates(conn: psycopg.Connection, run_id: uuid.UUID) -> list[LedgerCandidate]:
    """A Run's Candidates, best rank first."""
    rows = conn.execute(
        "SELECT run_id, rank, instrument, symbol, score, facts FROM candidates"
        " WHERE run_id = %s ORDER BY rank",
        (run_id,),
    ).fetchall()
    return [
        LedgerCandidate(run, rank, instrument, symbol, score, _decimals(facts))
        for run, rank, instrument, symbol, score, facts in rows
    ]


def plans(conn: psycopg.Connection, run_id: uuid.UUID) -> list[LedgerPlan]:
    """A Run's Plans, in its Candidates' rank order."""
    rows = conn.execute(
        "SELECT run_id, rank, instrument, symbol, entry, stop, target, time_stop_days, facts"
        " FROM plans WHERE run_id = %s ORDER BY rank",
        (run_id,),
    ).fetchall()
    return [
        LedgerPlan(run, rank, instrument, symbol, entry, stop, target, days, _decimals(facts))
        for run, rank, instrument, symbol, entry, stop, target, days, facts in rows
    ]


@dataclass(frozen=True)
class LedgerPass:
    """The Risk Engine's Pass for one Plan: the Plan priced on the tick and sized, and the
    hash of that Plan the Pass is bound to (``tradeline.risk``)."""

    run_id: uuid.UUID
    rank: int
    instrument: str
    symbol: str
    plan_hash: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    time_stop_days: int
    shares: int
    # The sector the sector Limit read; ``unclassified`` when none was recorded.
    sector: str
    # Equity, the tick, the 20-day average traded value, and the shares each sizing
    # Limit allowed.
    facts: dict[str, Decimal]


@dataclass(frozen=True)
class LedgerBlock:
    """The Risk Engine's Block of one Plan, naming the Limit, with the Plan as far as it
    was priced and sized."""

    run_id: uuid.UUID
    rank: int
    instrument: str
    symbol: str
    limit: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    time_stop_days: int
    shares: int
    sector: str
    facts: dict[str, Decimal]


@dataclass(frozen=True)
class LedgerVerdict:
    run_id: uuid.UUID
    rank: int
    instrument: str
    symbol: str
    # BUY or SKIP (WAIT and EXIT are later phases').
    verdict: str
    # ``pass``, or the Limit that Blocked the Plan.
    reason: str


def passes(conn: psycopg.Connection, run_id: uuid.UUID) -> list[LedgerPass]:
    """A Run's Passes, in Plan order."""
    rows = conn.execute(
        "SELECT x.run_id, x.rank, p.instrument, p.symbol, x.plan_hash, x.entry, x.stop,"
        " x.target, x.time_stop_days, x.shares, x.sector, x.facts"
        " FROM passes x JOIN plans p USING (mode, run_id, rank)"
        " WHERE x.run_id = %s ORDER BY x.rank",
        (run_id,),
    ).fetchall()
    return [LedgerPass(*_with_facts(row)) for row in rows]


def blocks(conn: psycopg.Connection, run_id: uuid.UUID) -> list[LedgerBlock]:
    """A Run's Blocks, in Plan order."""
    rows = conn.execute(
        "SELECT x.run_id, x.rank, p.instrument, p.symbol, x.limit_name, x.entry, x.stop,"
        " x.target, x.time_stop_days, x.shares, x.sector, x.facts"
        " FROM blocks x JOIN plans p USING (mode, run_id, rank)"
        " WHERE x.run_id = %s ORDER BY x.rank",
        (run_id,),
    ).fetchall()
    return [LedgerBlock(*_with_facts(row)) for row in rows]


def _with_facts(row: tuple[Any, ...]) -> list[Any]:
    """A row whose last column is a jsonb of Decimal strings, read as Decimals."""
    return [*row[:-1], _decimals(row[-1])]


def verdicts(conn: psycopg.Connection, run_id: uuid.UUID) -> list[LedgerVerdict]:
    """A Run's Verdicts, one per Candidate, best rank first."""
    rows = conn.execute(
        "SELECT v.run_id, v.rank, c.instrument, c.symbol, v.verdict, v.reason"
        " FROM verdicts v JOIN candidates c USING (mode, run_id, rank)"
        " WHERE v.run_id = %s ORDER BY v.rank",
        (run_id,),
    ).fetchall()
    return [LedgerVerdict(*row) for row in rows]


# An account is one mode's Orders, Fills and Positions: a backtest's (mode backtest and its
# id), the paper or the live account (no backtest id), or the paper dry-run's (paper, marked
# ``dry_run``, #80). ``day`` and ``seq`` order every event of an account across the three
# tables.


class Account(TypedDict):
    """What names an account, as the account reads take it."""

    mode: EngineMode
    backtest_id: uuid.UUID | None
    dry_run: bool


@dataclass(frozen=True)
class LedgerOrder:
    """One Order: its terms, when it was placed, and how it ended: a Fill, a cancellation,
    or neither yet (open)."""

    id: uuid.UUID
    run_id: uuid.UUID
    # The Pass's rank, for an entry.
    rank: int | None
    position_id: uuid.UUID
    instrument: str
    symbol: str
    side: str
    # ``entry``, ``target``, ``stop``, ``time`` or ``boundary``.
    purpose: OrderPurpose
    limit_price: Decimal
    shares: int
    # The trading day it can fill on.
    session: date
    placed: tuple[date, int]
    cancelled: tuple[date, int] | None
    cancel_reason: str | None
    filled: tuple[date, int] | None
    fill_price: Decimal | None

    @property
    def status(self) -> str:
        if self.filled is not None:
            return "filled"
        return "open" if self.cancelled is None else "cancelled"


@dataclass(frozen=True)
class LedgerFill:
    order_id: uuid.UUID
    day: date
    seq: int
    position_id: uuid.UUID
    instrument: str
    symbol: str
    side: str
    price: Decimal
    shares: int
    # Its costs beyond the price, itemised; None for a Fill written before #78.
    charges: Charges | None = None

    @property
    def cash_flow(self) -> Decimal:
        """What the Fill moved into the account's cash: the sale's value less its charges,
        or less the purchase's value and its charges."""
        value = self.price * self.shares
        costs = Decimal(0) if self.charges is None else self.charges.total
        return value - costs if self.side == "sell" else -value - costs


@dataclass(frozen=True)
class LedgerPosition:
    """A Position as it stands: open, or closed with its exit's price and reason."""

    id: uuid.UUID
    run_id: uuid.UUID
    rank: int
    instrument: str
    symbol: str
    opened_on: date
    shares: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    time_stop_days: int
    # The day of its latest event: the day it closed, once closed.
    as_of: date
    # ``stop``, ``target``, ``time`` or ``boundary``; None while open.
    exit_reason: ExitReason | None
    exit_price: Decimal | None

    @property
    def closed_on(self) -> date | None:
        return None if self.exit_reason is None else self.as_of


def orders(
    conn: psycopg.Connection,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool = False,
    open_only: bool = False,
) -> list[LedgerOrder]:
    """An account's Orders in the order they were placed; only those neither filled nor
    cancelled when ``open_only``."""
    rows = conn.execute(
        "SELECT p.id, p.run_id, p.rank, p.position_id, p.instrument, p.symbol, p.side,"
        " p.purpose, p.limit_price, p.shares, p.session, p.day, p.seq, c.day, c.seq, c.reason,"
        " f.day, f.seq, f.price"
        " FROM orders p"
        " LEFT JOIN orders c ON (c.mode, c.id, c.event) = (p.mode, p.id, 'cancelled')"
        " LEFT JOIN fills f ON (f.mode, f.order_id) = (p.mode, p.id)"
        " WHERE p.event = 'placed' AND p.mode = %(mode)s"
        " AND p.backtest_id IS NOT DISTINCT FROM %(backtest_id)s AND p.dry_run = %(dry_run)s"
        " AND (NOT %(open_only)s OR (c.id IS NULL AND f.order_id IS NULL))"
        " ORDER BY p.day, p.seq",
        {"mode": mode, "backtest_id": backtest_id, "dry_run": dry_run, "open_only": open_only},
    ).fetchall()
    return [
        LedgerOrder(
            id=row[0],
            run_id=row[1],
            rank=row[2],
            position_id=row[3],
            instrument=row[4],
            symbol=row[5],
            side=row[6],
            purpose=row[7],
            limit_price=row[8],
            shares=row[9],
            session=row[10],
            placed=(row[11], row[12]),
            cancelled=None if row[13] is None else (row[13], row[14]),
            cancel_reason=row[15],
            filled=None if row[16] is None else (row[16], row[17]),
            fill_price=row[18],
        )
        for row in rows
    ]


def fills(
    conn: psycopg.Connection,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool = False,
) -> list[LedgerFill]:
    """An account's Fills in the order they happened."""
    rows = conn.execute(
        "SELECT order_id, day, seq, position_id, instrument, symbol, side, price, shares, "
        + ", ".join(_CHARGE_COLUMNS)
        + " FROM fills WHERE mode = %(mode)s"
        " AND backtest_id IS NOT DISTINCT FROM %(backtest_id)s AND dry_run = %(dry_run)s"
        " ORDER BY day, seq",
        {"mode": mode, "backtest_id": backtest_id, "dry_run": dry_run},
    ).fetchall()
    return [_ledger_fill(row) for row in rows]


def _ledger_fill(row: tuple[Any, ...]) -> LedgerFill:
    order_id, day, seq, position_id, instrument, symbol, side, price, shares = row[:9]
    charges = None if row[9] is None else Charges(*row[9:])
    return LedgerFill(
        order_id, day, seq, position_id, instrument, symbol, side, price, shares, charges
    )


_CHARGE_COLUMNS = tuple(f.name for f in dataclasses.fields(Charges))


def positions(
    conn: psycopg.Connection,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool = False,
    open_only: bool = False,
) -> list[LedgerPosition]:
    """An account's Positions as they stand (the ``current_positions`` view), in the order
    they opened; only the open ones when ``open_only``."""
    # The ``current_positions`` view, read for the one account: filtered before DISTINCT ON,
    # so a query reads that account's events and not every account's.
    rows = conn.execute(
        "SELECT position_id, run_id, rank, instrument, symbol, opened_on, shares, entry, stop,"
        " target, time_stop_days, day, exit_reason, exit_price FROM ("
        "  SELECT DISTINCT ON (position_id) * FROM positions"
        "  WHERE mode = %(mode)s AND backtest_id IS NOT DISTINCT FROM %(backtest_id)s"
        "  AND dry_run = %(dry_run)s"
        "  ORDER BY position_id, day DESC, seq DESC) p"
        " WHERE NOT %(open_only)s OR event <> 'closed'"
        " ORDER BY opened_on, symbol, position_id",
        {"mode": mode, "backtest_id": backtest_id, "dry_run": dry_run, "open_only": open_only},
    ).fetchall()
    return [LedgerPosition(*row) for row in rows]


@dataclass(frozen=True)
class LedgerPositionEvent:
    """One event of a Position, with its state after it: ``opened``, ``restated`` on a split
    or bonus Ex-Date, or ``closed``."""

    position_id: uuid.UUID
    event: str
    day: date
    seq: int
    instrument: str
    symbol: str
    shares: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    # A restatement's Adjustment Factor and the cash paid for a fractional share.
    factor: Fraction | None
    cash_in_lieu: Decimal | None
    exit_price: Decimal | None
    exit_reason: ExitReason | None


def position_events(
    conn: psycopg.Connection,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool = False,
) -> list[LedgerPositionEvent]:
    """Every event of an account's Positions, in the order they happened."""
    rows = conn.execute(
        "SELECT position_id, event, day, seq, instrument, symbol, shares, entry, stop, target,"
        " factor, cash_in_lieu, exit_price, exit_reason FROM positions"
        " WHERE mode = %(mode)s AND backtest_id IS NOT DISTINCT FROM %(backtest_id)s"
        " AND dry_run = %(dry_run)s ORDER BY day, seq",
        {"mode": mode, "backtest_id": backtest_id, "dry_run": dry_run},
    ).fetchall()
    return [
        LedgerPositionEvent(
            position_id=row[0],
            event=row[1],
            day=row[2],
            seq=row[3],
            instrument=row[4],
            symbol=row[5],
            shares=row[6],
            entry=row[7],
            stop=row[8],
            target=row[9],
            factor=None if row[10] is None else Fraction(row[10]),
            cash_in_lieu=row[11],
            exit_price=row[12],
            exit_reason=row[13],
        )
        for row in rows
    ]


def account_cash_flows(
    conn: psycopg.Connection,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool = False,
) -> Decimal:
    """The net cash an account's Fills and restatements moved: every sale's value less its
    charges, every purchase's value and its charges taken away, plus cash paid in lieu of
    fractional shares. An account's cash is its starting Equity plus this."""
    charges = " + ".join(f"coalesce({c}, 0)" for c in _CHARGE_COLUMNS)
    row = conn.execute(
        "SELECT coalesce((SELECT sum(CASE side WHEN 'sell' THEN price * shares"
        " ELSE -price * shares END - (" + charges + ")) FROM fills"
        " WHERE mode = %(mode)s AND backtest_id IS NOT DISTINCT FROM %(backtest_id)s"
        " AND dry_run = %(dry_run)s), 0)"
        " + coalesce((SELECT sum(cash_in_lieu) FROM positions"
        " WHERE mode = %(mode)s AND backtest_id IS NOT DISTINCT FROM %(backtest_id)s"
        " AND dry_run = %(dry_run)s), 0)",
        {"mode": mode, "backtest_id": backtest_id, "dry_run": dry_run},
    ).fetchone()
    assert row is not None
    found: Decimal = row[0]
    return found


def paper_gate_trades(conn: psycopg.Connection) -> list[LedgerPosition]:
    """The paper trades the Paper Gate counts (ADR 0015): the paper account's closed
    Positions, in the order they opened. A paper dry-run's (#80) are never among them."""
    return [
        p
        for p in positions(conn, mode="paper", backtest_id=None, dry_run=False)
        if p.closed_on is not None
    ]


@dataclass(frozen=True)
class FrozenProtocol:
    """A Protocol in the Ledger: its key, when it was frozen, and what it states."""

    hash: str
    frozen_at: datetime
    protocol: Protocol


# The shortest prefix of a Protocol's hash that names it.
PROTOCOL_PREFIX = 12


def protocols(conn: psycopg.Connection) -> list[FrozenProtocol]:
    """Every frozen Protocol, in the order frozen."""
    rows = conn.execute(
        "SELECT hash, frozen_at, content FROM protocols ORDER BY frozen_at, hash"
    ).fetchall()
    return [FrozenProtocol(h, at, Protocol.from_content(json.loads(c))) for h, at, c in rows]


def protocol(conn: psycopg.Connection, name: str) -> FrozenProtocol | None:
    """The frozen Protocol whose hash is ``name``, or starts with it (at least 12 hex
    characters); None if no Protocol has it. A prefix two Protocols share is refused."""
    if len(name) < PROTOCOL_PREFIX:
        raise ValueError(
            f"a Protocol is named by at least {PROTOCOL_PREFIX} characters of its hash,"
            f" not {name!r}"
        )
    rows = conn.execute(
        "SELECT hash, frozen_at, content FROM protocols WHERE starts_with(hash, %s)"
        " ORDER BY hash LIMIT 2",
        (name.lower(),),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError(f"{name} names more than one Protocol; give more of its hash")
    if not rows:
        return None
    ((found, frozen_at, content),) = rows
    return FrozenProtocol(found, frozen_at, Protocol.from_content(json.loads(content)))


# --- Walk-Forward (#82) -------------------------------------------------------


@dataclass(frozen=True)
class WalkForward:
    """A Protocol's Walk-Forward: the backtest id its Stitched Curve is written under."""

    protocol: str
    backtest_id: uuid.UUID
    started_at: datetime


@dataclass(frozen=True)
class WalkForwardCell:
    """One grid cell's In-Sample run in a Window, kept as a summary only."""

    protocol: str
    window: int
    parameters: dict[str, str]
    runs: int
    trades: int
    sharpe: Decimal | None
    max_drawdown: Decimal
    ending_equity: Decimal


@dataclass(frozen=True)
class WalkForwardWindow:
    """One Window's selection and its out-of-sample result on the Stitched Curve.
    ``parameters`` and the in-sample figures are None when no cell was eligible."""

    protocol: str
    window: int
    cells: int
    eligible_cells: int
    parameters: dict[str, str] | None
    objective: Decimal | None
    in_sample_sharpe: Decimal | None
    in_sample_trades: int | None
    out_of_sample_runs: int
    out_of_sample_trades: int
    start_equity: Decimal | None
    end_equity: Decimal | None
    # When the Window was recorded: the end of its Out-of-Sample stretch's writing.
    recorded_at: datetime | None = None


def walk_forward(conn: psycopg.Connection, protocol: str) -> WalkForward | None:
    """The Walk-Forward under the Protocol whose full hash is ``protocol``, if one started."""
    row = conn.execute(
        "SELECT protocol, backtest_id, started_at FROM walk_forwards WHERE protocol = %s",
        (protocol,),
    ).fetchone()
    return None if row is None else WalkForward(*row)


def walk_forward_cells(
    conn: psycopg.Connection, protocol: str, *, window: int | None = None
) -> list[WalkForwardCell]:
    """The grid cells run under the Protocol, in a Window if one is named, in the order
    recorded."""
    rows = conn.execute(
        "SELECT protocol, window_number, parameters, runs, trades, sharpe, max_drawdown,"
        " ending_equity FROM walk_forward_cells WHERE protocol = %s"
        " AND (%s::integer IS NULL OR window_number = %s)"
        " ORDER BY window_number, recorded_at, parameters",
        (protocol, window, window),
    ).fetchall()
    return [WalkForwardCell(*row) for row in rows]


def walk_forward_windows(conn: psycopg.Connection, protocol: str) -> list[WalkForwardWindow]:
    """The Windows the Walk-Forward under the Protocol has finished, in order."""
    rows = conn.execute(
        "SELECT protocol, window_number, cells, eligible_cells, parameters, objective,"
        " in_sample_sharpe, in_sample_trades, out_of_sample_runs, out_of_sample_trades,"
        " start_equity, end_equity, recorded_at FROM walk_forward_windows WHERE protocol = %s"
        " ORDER BY window_number",
        (protocol,),
    ).fetchall()
    return [WalkForwardWindow(*row) for row in rows]


# --- The Holdout (#83) --------------------------------------------------------


@dataclass(frozen=True)
class HoldoutAttempt:
    """One request by a Strategy version (its Protocol) to run the Holdout: ``ran``, with
    the account it ran in, its parameters, whether it consumed the family's Holdout and the
    earlier runs whose data has been corrected since; or ``refused``, with the reason.
    ``consumes`` and ``independent`` are None for a refusal."""

    id: uuid.UUID
    protocol: str
    family: str
    attempted_at: datetime
    outcome: str
    backtest_id: uuid.UUID | None
    parameters: dict[str, str] | None
    consumes: bool | None
    corrected_since: tuple[uuid.UUID, ...]
    independent: bool | None
    reason: str | None


def holdout_attempts(
    conn: psycopg.Connection, *, protocol: str | None = None, family: str | None = None
) -> list[HoldoutAttempt]:
    """Holdout attempts, under the Protocol whose full hash is ``protocol`` or in the Strategy
    Family ``family`` when named, in the order made."""
    rows = conn.execute(
        "SELECT id, protocol, family, attempted_at, outcome, backtest_id, parameters, consumes,"
        " corrected_since, independent, reason FROM holdout_attempts"
        " WHERE (%(protocol)s::text IS NULL OR protocol = %(protocol)s)"
        " AND (%(family)s::text IS NULL OR family = %(family)s)"
        " ORDER BY attempted_at, id",
        {"protocol": protocol, "family": family},
    ).fetchall()
    return [
        HoldoutAttempt(
            id=row[0],
            protocol=row[1],
            family=row[2],
            attempted_at=row[3],
            outcome=row[4],
            backtest_id=row[5],
            parameters=row[6],
            consumes=row[7],
            corrected_since=tuple(row[8] or ()),
            independent=row[9],
            reason=row[10],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class LineageVersion:
    """A Strategy version in a lineage: its Protocol, Strategy and code revision, when it was
    frozen, and its Holdout run, if it has one."""

    protocol: str
    name: str
    code_revision: str
    frozen_at: datetime
    holdout: HoldoutAttempt | None


@dataclass(frozen=True)
class FamilyLineage:
    """A Strategy Family's versions, in the order frozen, and the Protocol whose Holdout run
    consumed the family's Holdout (None while none has run)."""

    family: str
    versions: tuple[LineageVersion, ...]
    consumed_by: str | None


@dataclass(frozen=True)
class Lineage:
    """A version's lineage (ADR 0007): its own family's versions, and every other family with
    a version frozen before it, the ones it was frozen beside, each with those versions. The
    family is the one its Protocol states, recorded when it was frozen, so a version
    relabelled as a new family shows the family it was frozen beside."""

    protocol: str
    frozen_at: datetime
    family: FamilyLineage
    beside: tuple[FamilyLineage, ...]


def lineage(conn: psycopg.Connection, protocol: str) -> Lineage:
    """The lineage of the version under the Protocol whose full hash is ``protocol``."""
    frozen = protocols(conn)
    own = next((p for p in frozen if p.hash == protocol), None)
    if own is None:
        raise ValueError(f"no Protocol {protocol} is frozen")
    attempts = [a for a in holdout_attempts(conn) if a.outcome == "ran"]
    ran = {a.protocol: a for a in attempts}

    def family(name: str, versions: list[FrozenProtocol]) -> FamilyLineage:
        consumer = [a.protocol for a in attempts if a.family == name and a.consumes]
        return FamilyLineage(
            name,
            tuple(
                LineageVersion(
                    v.hash, v.protocol.name, v.protocol.code_revision, v.frozen_at, ran.get(v.hash)
                )
                for v in versions
            ),
            consumer[0] if consumer else None,
        )

    name = own.protocol.family
    earlier = [p for p in frozen if (p.frozen_at, p.hash) < (own.frozen_at, own.hash)]
    beside = sorted({p.protocol.family for p in earlier} - {name})
    return Lineage(
        protocol,
        own.frozen_at,
        family(name, [p for p in frozen if p.protocol.family == name]),
        tuple(family(f, [p for p in earlier if p.protocol.family == f]) for f in beside),
    )


# --- The Backtest Gate (#84) --------------------------------------------------


@dataclass(frozen=True)
class GateEvaluation:
    """One evaluation of the Backtest Gate on a Protocol's Stitched Curve and Holdout run,
    with what it read: the Protocol's thresholds, each curve's metrics (exact decimal
    strings, None where the curve gives none), the Holdout run's standing, every check, and
    how many real contract notes the cost model was checked against."""

    id: uuid.UUID
    gate: str
    protocol: str
    evaluated_at: datetime
    stitched_backtest_id: uuid.UUID
    holdout_attempt: uuid.UUID
    thresholds: dict[str, Any]
    inputs: dict[str, Any]
    standing: dict[str, Any]
    checks: list[dict[str, Any]]
    contract_notes: int
    outcome: str
    reason: str


def gate_evaluations(conn: psycopg.Connection, *, protocol: str) -> list[GateEvaluation]:
    """The Backtest Gate's evaluations under the Protocol whose full hash is ``protocol``, in
    the order made."""
    rows = conn.execute(
        "SELECT id, gate, protocol, evaluated_at, stitched_backtest_id, holdout_attempt,"
        " thresholds, inputs, standing, checks, contract_notes, outcome, reason"
        " FROM gate_evaluations WHERE protocol = %s ORDER BY evaluated_at, id",
        (protocol,),
    ).fetchall()
    return [GateEvaluation(*row) for row in rows]


@dataclass(frozen=True)
class CostSensitivity:
    """A Protocol's Stitched Curve or Holdout run again with the Slippage impact c at
    ``impact``, in the account ``backtest_id``: information, never a Protocol result."""

    protocol: str
    curve: str
    impact: Decimal
    backtest_id: uuid.UUID
    started_at: datetime


def cost_sensitivities(conn: psycopg.Connection, protocol: str) -> list[CostSensitivity]:
    """The c sensitivity accounts under the Protocol whose full hash is ``protocol``, by
    impact, the Stitched Curve before the Holdout."""
    rows = conn.execute(
        "SELECT protocol, curve, impact, backtest_id, started_at FROM cost_sensitivities"
        " WHERE protocol = %s ORDER BY impact, curve DESC",
        (protocol,),
    ).fetchall()
    return [CostSensitivity(*row) for row in rows]
