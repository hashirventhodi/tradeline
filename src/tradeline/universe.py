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
- its Listing Age, the trading days from its listing date to the day before
  M, is at least ``min_listing_trading_days``. The listing date is the equity
  master's; a symbol the master no longer lists (one since delisted) falls
  back to its first end-of-day row, which is exact once history is loaded
  back to its listing.

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
(``instruments.listed_on``). Under ``listing_age_from = "earliest_evidence"``
(ADR 0017) Listing Age counts from the earlier of that date and the
Instrument's first row in any listing series (the view
``instrument_listing_age``), since NSE's master gives a
security's last admission to the main board, which can follow years of
trading on NSE.
The member's Symbol is that of its most recent row in the window, the Symbol
in force on the window's last day, and the membership row also records the
Instrument's key. The Instruments must be rebuilt over the whole window first:
a month whose window has a row in an equity listing series that no Instrument
holds is refused, never ranked without it. Rows stored under a rule that
ranks Symbols are left as they are; that rule's version is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.instruments import LISTING_SERIES

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
    # Stored members whose Instrument key a later rebuild retired; not a difference
    # when the key now current is the recomputed member's.
    obsolete: tuple[queries.ObsoleteUniverseKey, ...] = ()


@dataclass(frozen=True)
class MembershipCheck:
    """A recomputed month against the stored one, compared by Instrument identity."""

    differs: bool
    # The stored members whose key is obsolete, with the key now current.
    obsolete: tuple[queries.ObsoleteUniverseKey, ...]


def check_membership(
    stored: Sequence[queries.UniverseMember],
    recomputed: Sequence[queries.UniverseMember],
    *,
    obsolete: Sequence[queries.ObsoleteUniverseKey],
) -> MembershipCheck:
    """Whether the recomputed members differ from the stored ones in rank, Symbol, series,
    traded value, or Instrument, reading each obsolete stored key as the key now current.

    A key a later rebuild renamed without regrouping the window (#35) is no
    difference, only reported. An obsolete key no Instrument answers for is
    one, as is any other change.
    """
    current: dict[tuple[str, str | None], str | None] = {
        (o.symbol, o.stored_key): o.current_key for o in obsolete
    }
    canonical = [
        replace(m, instrument=current[(m.symbol, m.instrument)])
        if (m.symbol, m.instrument) in current
        else m
        for m in stored
    ]
    return MembershipCheck(differs=canonical != list(recomputed), obsolete=tuple(obsolete))


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
    obsolete: tuple[queries.ObsoleteUniverseKey, ...] = ()
    if not stored:
        rows_written = _insert_members(conn, members)
    else:
        check = check_membership(
            stored,
            members,
            obsolete=queries.obsolete_universe_keys(conn, month=month, rule_version=rule.version),
        )
        anomaly, obsolete = check.differs, check.obsolete
        if anomaly:
            log.warning(
                "universe recomputed differently from the stored membership; stored rows kept",
                extra={"month": month.isoformat(), "rule_version": rule.version},
            )
        elif obsolete:
            log.warning(
                "stored universe members name Instrument keys a later rebuild retired;"
                " the members are unchanged and the stored rows kept",
                extra={
                    "month": month.isoformat(),
                    "rule_version": rule.version,
                    "obsolete_keys": {o.stored_key: o.current_key for o in obsolete},
                },
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
        obsolete=obsolete,
    )


# The rule's eligibility filters, in the order they are applied.
ExclusionReason = Literal["series", "fund_unit", "listing_age", "presence"]


@dataclass(frozen=True)
class Exclusion:
    """The first of the rule's filters a candidate fails, and how."""

    reason: ExclusionReason
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class Standing:
    """Where the rule places one symbol with a row in its series in the window."""

    symbol: str
    # The rank among every eligible symbol, beyond ``top_n`` too; None if not eligible.
    rank: int | None
    # Traded value in the rule's series over the window.
    traded_value: Decimal
    # Why the symbol is not eligible; None if it is.
    excluded: Exclusion | None
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


@dataclass(frozen=True)
class Eligibility:
    """How the rule judges one Instrument or Symbol for a month."""

    # The Symbol the rule ranks it by: of its latest row in the window.
    symbol: str
    # Why it is not eligible; None if it is.
    excluded: Exclusion | None


def eligibility(
    conn: psycopg.Connection, rule: UniverseRule, *, month: date, keys: Sequence[str]
) -> dict[str, Eligibility]:
    """How the rule judges each of ``keys`` for the month.

    A key is an Instrument's key under a rule that ranks Instruments, else a
    Symbol. The rule's own filters are applied to only these, so the cost is
    theirs and not the whole window's. A key with no row in the rule's series
    in the window is left out.
    """
    window = _window(conn, rule, month)
    if rule.rank_by == "instrument":
        candidates = _instrument_candidates(conn, rule, month, window[0], only=keys)
    else:
        candidates = _candidates(conn, rule, month, window[0], only=keys)
    return {c.instrument or c.symbol: Eligibility(c.symbol, _excluded(rule, c)) for c in candidates}


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
    conn: psycopg.Connection,
    rule: UniverseRule,
    month: date,
    window_start: date,
    *,
    only: Sequence[str] | None = None,
) -> list[_Candidate]:
    """Every symbol with a row in the rule's series in the window, with what eligibility needs;
    only the symbols ``only`` names, if given."""
    rows = conn.execute(
        # Materialized: inlined into the per-symbol count below, the calendar
        # would be rebuilt from every end-of-day row once per candidate.
        "WITH calendar AS MATERIALIZED ("
        "  SELECT DISTINCT trade_date FROM eod_bhavcopy WHERE trade_date < %(month)s),"
        " window_rows AS ("
        "  SELECT * FROM eod_bhavcopy WHERE trade_date >= %(start)s AND trade_date < %(month)s"
        "  AND (%(only)s::text[] IS NULL OR symbol = ANY(%(only)s))),"
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
        {
            "start": window_start,
            "month": month,
            "series": rule.series,
            "only": None if only is None else list(only),
        },
    ).fetchall()
    return [_Candidate(*row) for row in rows]


def _instrument_candidates(
    conn: psycopg.Connection,
    rule: UniverseRule,
    month: date,
    window_start: date,
    *,
    only: Sequence[str] | None = None,
) -> list[_Candidate]:
    """Every Instrument with a row in the rule's series in the window, across its Symbols;
    only the Instruments whose keys ``only`` names, if given.

    A window row belongs to the Instrument whose interval holds it. A row in
    NSE's listing series that no interval holds was loaded after the last
    rebuild and would be left out of the ranking, so the month is refused.
    """
    params = {
        "start": window_start,
        "month": month,
        "series": rule.series,
        "listing_series": list(LISTING_SERIES),
        "listing_age_from": rule.listing_age_from,
        "only": None if only is None else list(only),
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
        "  WHERE e.trade_date >= %(start)s AND e.trade_date < %(month)s"
        "  AND (%(only)s::text[] IS NULL OR i.instrument_key = ANY(%(only)s))),"
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
        "  (SELECT count(*) FROM calendar c WHERE c.trade_date >= CASE %(listing_age_from)s"
        "   WHEN 'earliest_evidence' THEN (SELECT la.listing_age_from"
        "    FROM instrument_listing_age la WHERE la.instrument_key = ins.key)"
        "   ELSE ins.listed_on END),"
        "  st.instrument_key"
        " FROM stats st JOIN latest l USING (instrument_key)"
        " JOIN instruments ins ON ins.key = st.instrument_key"
        " ORDER BY l.symbol, st.instrument_key",
        params,
    ).fetchall()
    return [_Candidate(*row) for row in rows]


def _excluded(rule: UniverseRule, candidate: _Candidate) -> Exclusion | None:
    """Why ``candidate`` is not eligible under ``rule``, or None if it is."""
    if candidate.latest_series != rule.series:
        return Exclusion("series", f"last traded in series {candidate.latest_series} in the window")
    if (candidate.isin or "").startswith(rule.excluded_isin_prefix):
        return Exclusion("fund_unit", f"ISIN {candidate.isin} is a fund unit (ETF)")
    if candidate.listing_trading_days < rule.min_listing_trading_days:
        return Exclusion(
            "listing_age",
            f"listed {candidate.listing_trading_days} trading days,"
            f" fewer than {rule.min_listing_trading_days}",
        )
    if candidate.days * 100 < rule.min_presence_percent * rule.lookback_trading_days:
        return Exclusion(
            "presence",
            f"rows on {candidate.days} of {rule.lookback_trading_days} window days,"
            f" under {rule.min_presence_percent}%",
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
