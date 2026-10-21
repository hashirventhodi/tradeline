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
    symbol: str
    days: int
    traded_value: Decimal
    latest_series: str
    isin: str | None
    listing_trading_days: int


def compute_universe(
    conn: psycopg.Connection, rule: UniverseRule, *, month: date
) -> UniverseResult:
    if month.day != 1:
        raise ValueError(f"a Universe month is given by its first day, not {month}")
    window = _window(conn, rule, month)
    window_start, window_end = window[0], window[-1]
    candidates = _candidates(conn, rule, month, window_start)
    eligible = sorted(
        (c for c in candidates if _eligible(rule, c)),
        key=lambda c: (-c.traded_value, c.symbol),
    )[: rule.top_n]
    members = tuple(
        queries.UniverseMember(
            month=month,
            rule_version=rule.version,
            rank=rank,
            symbol=c.symbol,
            series=rule.series,
            traded_value=c.traded_value,
        )
        for rank, c in enumerate(eligible, start=1)
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
        "WITH calendar AS ("
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


def _eligible(rule: UniverseRule, candidate: _Candidate) -> bool:
    return (
        candidate.latest_series == rule.series
        and not (candidate.isin or "").startswith(rule.excluded_isin_prefix)
        and candidate.listing_trading_days >= rule.min_listing_trading_days
        and candidate.days * 100 >= rule.min_presence_percent * rule.lookback_trading_days
    )


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
        "INSERT INTO universe_membership (month, rule_version, rank, symbol, series, traded_value)"
        " SELECT * FROM unnest(%s::date[], %s::text[], %s::integer[], %s::text[], %s::text[],"
        "  %s::numeric[])",
        (
            [m.month for m in members],
            [m.rule_version for m in members],
            [m.rank for m in members],
            [m.symbol for m in members],
            [m.series for m in members],
            [m.traded_value for m in members],
        ),
    )
    return cursor.rowcount
