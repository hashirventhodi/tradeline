"""``universe``: the Universe Rule (ADR 0005) applied to one month.

The Universe for month M is computed from NSE end-of-day rows alone, and only
from rows dated before M's first trading day. No trading day falls between the
first calendar day of M and its first trading day, so "before the first
trading day" is "before the first of the month", and M can be computed on the
morning of its first trading day before that day's file exists.

The trading calendar is the set of days with end-of-day rows. The window is
the ``lookback_trading_days`` trading days before M. A symbol is eligible when:

- it has rows in the rule's series (EQ) on at least ``min_presence_percent`` of
  the window's days, and its most recent row in the window is in that series,
  so a symbol moved to trade-for-trade (BE) or listed on SME is not;
- its ISIN does not begin with ``excluded_isin_prefix`` (INF: ETFs and other
  fund units, which NSE trades in series EQ);
- at least ``min_listing_trading_days`` trading days fall from its listing
  date to the day before M. The listing date is the equity master's; a symbol
  the master no longer lists (one since delisted) falls back to its first
  end-of-day row, which is exact once history is loaded back to its listing.

Eligible symbols are ranked by traded value in the rule's series over the
window, descending, ties broken by symbol ascending, and the top ``top_n`` are
the Universe. The run records the rule version (its parameter hash) and one
membership row per member. Both tables are append-only: a month already
written under a rule version is recomputed and compared, never rewritten, and
a difference (end-of-day rows loaded since that change the window) is
reported as an anomaly with the stored membership kept.

A rule with ``rank_by = "instrument"`` (ADR 0016) ranks Instruments instead,
so a stock renamed inside the window is neither cut in two nor taken for a new
listing. Each end-of-day row counts for the Instrument whose interval holds
it: the window's days and traded value in the rule's series are summed over
every Symbol the Instrument had, its most recent row (under any Symbol) gives
the series and ISIN checks, and its listing date is the Instrument's
(``instruments.listed_on``). The member's Symbol is that of its most recent
row in the window, the Symbol in force on the window's last day, and the
membership row also records the Instrument's key. The Instruments must be
rebuilt over the whole window first: a month whose window has a row in an
equity listing series that no Instrument holds is refused, never ranked
without it. Rows stored under a rule that ranks Symbols are left as they are;
that rule's version is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.rebuild_instruments import SERIES as LISTING_SERIES

log = logging.getLogger(__name__)

# NSE has never been shut for a week, so a month whose latest earlier file is
# older than this has not had its end-of-day files loaded up to its start.
_MAX_GAP_BEFORE_MONTH = timedelta(days=7)


@dataclass(frozen=True)
class UniverseResult:
    month: date
    rule_version: str
    window_start: date
    window_end: date
    members: tuple[queries.UniverseMember, ...]
    rows_written: int
    # True when the month was already stored under this rule version with
    # different members; the stored rows are kept.
    anomaly: bool


@dataclass(frozen=True)
class _Candidate:
    # Ranking Instruments, the Symbol of the Instrument's latest row in the window.
    symbol: str
    days: int
    traded_value: Decimal
    latest_series: str
    isin: str | None
    listing_trading_days: int
    # The Instrument's key when the rule ranks Instruments, else None.
    instrument: str | None = None


def compute_universe(
    conn: psycopg.Connection, rule: UniverseRule, *, month: date
) -> UniverseResult:
    if month.day != 1:
        raise ValueError(f"a Universe month is given by its first day, not {month}")
    window_start, window_end, ranked = _rank(conn, rule, month)
    eligible = [s for s in ranked if s.rank is not None]
    members = tuple(
        queries.UniverseMember(
            month=month,
            rule_version=rule.version,
            rank=s.rank,
            symbol=s.symbol,
            series=rule.series,
            traded_value=s.traded_value,
            instrument=s.instrument,
        )
        for s in eligible
        if s.rank is not None and s.rank <= rule.top_n
    )
    _record_rule_version(conn, rule)
    stored = queries.universe_membership(conn, month, rule_version=rule.version, as_of=month)
    rows_written, anomaly = 0, False
    if not stored:
        rows_written = _insert_members(conn, members)
    elif list(members) != stored:
        anomaly = True
        log.warning(
            "universe recomputed differently from the stored membership; stored rows kept",
            extra={"month": month.isoformat(), "rule_version": rule.version},
        )
    conn.commit()
    log.info(
        "universe computed",
        extra={
            "month": month.isoformat(),
            "rule_version": rule.version,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "eligible": len(eligible),
            "members": len(members),
            "rows_written": rows_written,
        },
    )
    return UniverseResult(
        month=month,
        rule_version=rule.version,
        window_start=window_start,
        window_end=window_end,
        members=members,
        rows_written=rows_written,
        anomaly=anomaly,
    )


@dataclass(frozen=True)
class Standing:
    """Where the rule places one symbol with a row in its series in the window."""

    symbol: str
    # The rank among every eligible symbol, beyond ``top_n`` too; None if not eligible.
    rank: int | None
    # Traded value in the rule's series over the window.
    traded_value: Decimal
    # Why the symbol is not eligible; None if it is.
    excluded: str | None
    # The Instrument ranked when the rule ranks Instruments, else None.
    instrument: str | None = None


def standings(conn: psycopg.Connection, rule: UniverseRule, *, month: date) -> list[Standing]:
    """Every symbol with a row in the rule's series in the month's window, ranked as the rule does.

    Eligible symbols come first, best rank first, then the ineligible ones by
    symbol. The Universe is the eligible symbols ranked ``top_n`` or better.
    """
    if month.day != 1:
        raise ValueError(f"a Universe month is given by its first day, not {month}")
    return _rank(conn, rule, month)[2]


def _rank(
    conn: psycopg.Connection, rule: UniverseRule, month: date
) -> tuple[date, date, list[Standing]]:
    window = _window(conn, rule, month)
    if rule.rank_by == "instrument":
        candidates = _instrument_candidates(conn, rule, month, window[0])
    else:
        candidates = _candidates(conn, rule, month, window[0])
    judged = [(c, _excluded(rule, c)) for c in candidates]
    eligible = sorted(
        (c for c, excluded in judged if excluded is None),
        key=lambda c: (-c.traded_value, c.symbol),
    )
    ranked = [
        Standing(c.symbol, rank, c.traded_value, None, c.instrument)
        for rank, c in enumerate(eligible, start=1)
    ] + [
        Standing(c.symbol, None, c.traded_value, excluded, c.instrument)
        for c, excluded in judged
        if excluded is not None
    ]
    return window[0], window[-1], ranked


def _window(conn: psycopg.Connection, rule: UniverseRule, month: date) -> list[date]:
    days = [
        day
        for (day,) in conn.execute(
            "SELECT DISTINCT trade_date FROM eod_bhavcopy WHERE trade_date < %s"
            " ORDER BY trade_date DESC LIMIT %s",
            (month, rule.lookback_trading_days),
        ).fetchall()
    ]
    if len(days) < rule.lookback_trading_days:
        raise ValueError(
            f"the Universe for {month:%Y-%m} needs {rule.lookback_trading_days} trading days of"
            f" end-of-day files before it and {len(days)} are loaded"
        )
    if month - days[0] > _MAX_GAP_BEFORE_MONTH:
        raise ValueError(
            f"the latest end-of-day file before {month:%Y-%m} is from {days[0]}; load the files"
            " up to the month's start before computing its Universe"
        )
    return sorted(days)


def _candidates(
    conn: psycopg.Connection, rule: UniverseRule, month: date, window_start: date
) -> list[_Candidate]:
    """Every symbol with a row in the rule's series in the window, with what eligibility needs."""
    rows = conn.execute(
        # Materialized: inlined into the per-symbol count below, the calendar
        # would be rebuilt from every end-of-day row once per candidate.
        "WITH calendar AS MATERIALIZED ("
        "  SELECT DISTINCT trade_date FROM eod_bhavcopy WHERE trade_date < %(month)s),"
        " window_rows AS ("
        "  SELECT * FROM eod_bhavcopy WHERE trade_date >= %(start)s AND trade_date < %(month)s),"
        " stats AS ("
        "  SELECT symbol, count(*) AS days, sum(traded_value) AS traded_value"
        "  FROM window_rows WHERE series = %(series)s GROUP BY symbol),"
        " latest AS ("
        "  SELECT DISTINCT ON (symbol) symbol, series, isin FROM window_rows"
        "  WHERE symbol IN (SELECT symbol FROM stats)"
        "  ORDER BY symbol, trade_date DESC, series = %(series)s DESC),"
        " listed AS ("
        "  SELECT st.symbol, COALESCE("
        "   (SELECT listing_date FROM symbols s WHERE s.symbol = st.symbol"
        "    AND s.series = %(series)s),"
        "   (SELECT min(trade_date) FROM eod_bhavcopy e WHERE e.symbol = st.symbol)"
        "  ) AS listing_date FROM stats st)"
        " SELECT st.symbol, st.days, st.traded_value, l.series,"
        "  COALESCE(l.isin, (SELECT isin FROM symbols s WHERE s.symbol = st.symbol"
        "   AND s.series = %(series)s)),"
        "  (SELECT count(*) FROM calendar c WHERE c.trade_date >= ls.listing_date)"
        " FROM stats st JOIN latest l USING (symbol) JOIN listed ls USING (symbol)"
        " ORDER BY st.symbol",
        {"start": window_start, "month": month, "series": rule.series},
    ).fetchall()
    return [_Candidate(*row) for row in rows]


def _instrument_candidates(
    conn: psycopg.Connection, rule: UniverseRule, month: date, window_start: date
) -> list[_Candidate]:
    """Every Instrument with a row in the rule's series in the window, across its Symbols.

    A window row belongs to the Instrument whose interval holds it. A row in
    NSE's listing series that no interval holds was loaded after the last
    rebuild and would be left out of the ranking, so the month is refused.
    """
    params = {
        "start": window_start,
        "month": month,
        "series": rule.series,
        "listing_series": list(LISTING_SERIES),
    }
    row = conn.execute(
        "SELECT count(*) FROM eod_bhavcopy e"
        " WHERE e.trade_date >= %(start)s AND e.trade_date < %(month)s"
        " AND e.series = ANY(%(listing_series)s) AND NOT EXISTS ("
        "  SELECT 1 FROM instrument_intervals i WHERE i.symbol = e.symbol"
        "  AND i.series = e.series AND e.trade_date BETWEEN i.first_day AND i.last_day)",
        params,
    ).fetchone()
    if row is not None and row[0]:
        raise ValueError(
            f"{row[0]} end-of-day rows in the window for {month:%Y-%m} belong to no Instrument;"
            " run `tradeline rebuild-instruments` first"
        )
    rows = conn.execute(
        # Materialized for the same reason as in ``_candidates``.
        "WITH calendar AS MATERIALIZED ("
        "  SELECT DISTINCT trade_date FROM eod_bhavcopy WHERE trade_date < %(month)s),"
        " window_rows AS ("
        "  SELECT i.instrument_key, e.* FROM eod_bhavcopy e JOIN instrument_intervals i"
        "  ON i.symbol = e.symbol AND i.series = e.series"
        "  AND e.trade_date BETWEEN i.first_day AND i.last_day"
        "  WHERE e.trade_date >= %(start)s AND e.trade_date < %(month)s),"
        " stats AS ("
        "  SELECT instrument_key, count(DISTINCT trade_date) AS days,"
        "  sum(traded_value) AS traded_value"
        "  FROM window_rows WHERE series = %(series)s GROUP BY instrument_key),"
        " latest AS ("
        "  SELECT DISTINCT ON (instrument_key) instrument_key, symbol, series, isin"
        "  FROM window_rows WHERE instrument_key IN (SELECT instrument_key FROM stats)"
        "  ORDER BY instrument_key, trade_date DESC, series = %(series)s DESC)"
        " SELECT l.symbol, st.days, st.traded_value, l.series,"
        "  COALESCE(l.isin, (SELECT isin FROM symbols s WHERE s.symbol = l.symbol"
        "   AND s.series = %(series)s)),"
        "  (SELECT count(*) FROM calendar c WHERE c.trade_date >= ins.listed_on),"
        "  st.instrument_key"
        " FROM stats st JOIN latest l USING (instrument_key)"
        " JOIN instruments ins ON ins.key = st.instrument_key"
        " ORDER BY l.symbol, st.instrument_key",
        params,
    ).fetchall()
    return [_Candidate(*row) for row in rows]


def _excluded(rule: UniverseRule, candidate: _Candidate) -> str | None:
    """Why ``candidate`` is not eligible under ``rule``, or None if it is."""
    if candidate.latest_series != rule.series:
        return f"last traded in series {candidate.latest_series} in the window"
    if (candidate.isin or "").startswith(rule.excluded_isin_prefix):
        return f"ISIN {candidate.isin} is a fund unit (ETF)"
    if candidate.listing_trading_days < rule.min_listing_trading_days:
        return (
            f"listed {candidate.listing_trading_days} trading days,"
            f" fewer than {rule.min_listing_trading_days}"
        )
    if candidate.days * 100 < rule.min_presence_percent * rule.lookback_trading_days:
        return (
            f"rows on {candidate.days} of {rule.lookback_trading_days} window days,"
            f" under {rule.min_presence_percent}%"
        )
    return None


def _record_rule_version(conn: psycopg.Connection, rule: UniverseRule) -> None:
    conn.execute(
        "INSERT INTO universe_rule_versions (version, parameters, recorded_at)"
        " VALUES (%s, %s, now()) ON CONFLICT (version) DO NOTHING",
        (rule.version, Jsonb(rule.parameters)),
    )


def _insert_members(conn: psycopg.Connection, members: tuple[queries.UniverseMember, ...]) -> int:
    if not members:
        return 0
    cursor = conn.execute(
        "INSERT INTO universe_membership"
        " (month, rule_version, rank, symbol, series, traded_value, instrument_key)"
        " SELECT * FROM unnest(%s::date[], %s::text[], %s::integer[], %s::text[], %s::text[],"
        "  %s::numeric[], %s::text[])",
        (
            [m.month for m in members],
            [m.rule_version for m in members],
            [m.rank for m in members],
            [m.symbol for m in members],
            [m.series for m in members],
            [m.traded_value for m in members],
            [m.instrument for m in members],
        ),
    )
    return cursor.rowcount
