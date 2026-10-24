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

Each month stored is recorded with its build (ADR 0005, 2026-10-24
amendment): the window's first day and Data Cutoff, the end-of-day ingest runs
and Instruments rebuild it read, and SHA-256 hashes of the window's rows and
Instrument intervals, so a later re-ingest that changed an input is detected.
A rerun compares the inputs it reads now with the build and reports those that
differ, keeping the build as it keeps the membership. Months stored before
builds were recorded get theirs from ``backfill_universe_builds``. Whether a
month is a Recorded or a Reconstructed Universe follows from the Operator's
Rule Validity declarations (``declare_rule_validity``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.ingest.eod import SOURCE_EOD
from tradeline.instruments import LISTING_SERIES
from tradeline.rebuild_instruments import SOURCE as SOURCE_INSTRUMENTS

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
    # What the stored membership was built from (ADR 0005); None for a month
    # stored before builds were recorded and not yet backfilled.
    build: queries.UniverseMonthBuild | None = None
    # True when this run stored the membership and so recorded its build.
    build_written: bool = False
    # The inputs (``_BUILD_INPUTS``) read now that differ from the recorded build's.
    build_differences: tuple[str, ...] = ()


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
    build, build_written, build_differences = _record_build(
        conn,
        month=month,
        rule_version=rule.version,
        window_start=window_start,
        data_cutoff=window_end,
        fresh=not stored,
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
            "build_written": build_written,
            "build_differences": list(build_differences),
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
        build=build,
        build_written=build_written,
        build_differences=build_differences,
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
    window = _window(conn, rule.lookback_trading_days, month)
    if rule.rank_by == "instrument":
        candidates = _instrument_candidates(conn, rule, month, window[0], only=keys)
    else:
        candidates = _candidates(conn, rule, month, window[0], only=keys)
    return {c.instrument or c.symbol: Eligibility(c.symbol, _excluded(rule, c)) for c in candidates}


def _rank(
    conn: psycopg.Connection, rule: UniverseRule, month: date
) -> tuple[date, date, list[Standing]]:
    window = _window(conn, rule.lookback_trading_days, month)
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


def _window(conn: psycopg.Connection, lookback_trading_days: int, month: date) -> list[date]:
    days = [
        day
        for (day,) in conn.execute(
            "SELECT DISTINCT trade_date FROM eod_bhavcopy WHERE trade_date < %s"
            " ORDER BY trade_date DESC LIMIT %s",
            (month, lookback_trading_days),
        ).fetchall()
    ]
    if len(days) < lookback_trading_days:
        raise ValueError(
            f"the Universe for {month:%Y-%m} needs {lookback_trading_days} trading days of"
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


def _record_rule_version(
    conn: psycopg.Connection, rule: UniverseRule, *, recorded_at: datetime | None = None
) -> None:
    conn.execute(
        "INSERT INTO universe_rule_versions (version, parameters, recorded_at)"
        " VALUES (%s, %s, coalesce(%s, now())) ON CONFLICT (version) DO NOTHING",
        (rule.version, Jsonb(rule.parameters), recorded_at),
    )


def declare_rule_validity(
    conn: psycopg.Connection,
    rule_version: str,
    *,
    in_force_from: date,
    declared_at: datetime,
    rule: UniverseRule | None = None,
) -> queries.RuleValidity:
    """Declare ``rule_version`` (or a unique prefix of it) the Universe Rule in force from
    ``in_force_from``, as of ``declared_at`` (ADR 0005's Rule Validity).

    A version must be recorded to be declared. ``rule``, the configured one, is
    recorded first, at ``declared_at``, when it is the version named, so a rule
    can be declared in force before any month is computed under it.
    """
    if declared_at.tzinfo is None:
        raise ValueError(f"declared_at must carry a time zone, not {declared_at}")
    if rule is not None and rule.version == rule_version:
        _record_rule_version(conn, rule, recorded_at=declared_at)
    versions = [
        version
        for (version,) in conn.execute(
            "SELECT version FROM universe_rule_versions WHERE starts_with(version, %s)",
            (rule_version,),
        ).fetchall()
    ]
    if len(versions) != 1:
        conn.rollback()
        raise ValueError(
            f"rule version {rule_version} is not recorded; compute a month under it first"
            if not versions
            else f"rule version {rule_version} names {len(versions)} versions; give more of it"
        )
    validity = queries.RuleValidity(versions[0], in_force_from, declared_at)
    conn.execute(
        "INSERT INTO universe_rule_validity (rule_version, in_force_from, declared_at)"
        " VALUES (%s, %s, %s)",
        (validity.rule_version, validity.in_force_from, validity.declared_at),
    )
    conn.commit()
    log.info(
        "rule validity declared",
        extra={
            "rule_version": validity.rule_version,
            "in_force_from": in_force_from.isoformat(),
            "declared_at": declared_at.isoformat(),
        },
    )
    return validity


# The fields of a build that say what it read; two builds of a month agreeing on
# them read the same inputs.
_BUILD_INPUTS = (
    "window_start",
    "data_cutoff",
    "eod_run_ids",
    "instruments_run_id",
    "eod_hash",
    "links_hash",
)


def _record_build(
    conn: psycopg.Connection,
    *,
    month: date,
    rule_version: str,
    window_start: date,
    data_cutoff: date,
    fresh: bool,
) -> tuple[queries.UniverseMonthBuild | None, bool, tuple[str, ...]]:
    """The month's build, whether this run wrote it, and the inputs that differ from it now.

    A build is written only with the membership it describes (``fresh``); one
    already recorded is compared and kept. A month stored without one, before
    builds were recorded, is left to ``backfill_universe_builds``.
    """
    current = _build_from(
        conn,
        month=month,
        rule_version=rule_version,
        window_start=window_start,
        data_cutoff=data_cutoff,
        cutoff_inferred=False,
    )
    stored = queries.universe_month_builds(conn, month=month, rule_version=rule_version)
    if stored:
        differences = tuple(
            name for name in _BUILD_INPUTS if getattr(stored[0], name) != getattr(current, name)
        )
        if differences:
            log.warning(
                "universe month inputs differ from those its build recorded; the build is kept",
                extra={
                    "month": month.isoformat(),
                    "rule_version": rule_version,
                    "differences": list(differences),
                },
            )
        return stored[0], False, differences
    if not fresh:
        log.warning(
            "universe month was stored without a build; run `tradeline backfill-universe-builds`",
            extra={"month": month.isoformat(), "rule_version": rule_version},
        )
        return None, False, ()
    _insert_build(conn, current)
    return current, True, ()


def _build_from(
    conn: psycopg.Connection,
    *,
    month: date,
    rule_version: str,
    window_start: date,
    data_cutoff: date,
    cutoff_inferred: bool,
) -> queries.UniverseMonthBuild:
    """The build of a month whose window is ``window_start`` to ``data_cutoff``, read now.

    End-of-day rows carry no ingest run id, so the runs a window read are the
    nse_eod runs that wrote rows over a range overlapping it.
    """
    run_ids = tuple(
        run_id
        for (run_id,) in conn.execute(
            "SELECT id FROM ingest_runs WHERE source = %s AND rows_written > 0"
            " AND range_start <= %s AND range_end >= %s ORDER BY started_at, id",
            (SOURCE_EOD, data_cutoff, window_start),
        ).fetchall()
    )
    rebuild = conn.execute(
        "SELECT id FROM ingest_runs WHERE source = %s AND error IS NULL"
        " ORDER BY started_at DESC, id LIMIT 1",
        (SOURCE_INSTRUMENTS,),
    ).fetchone()
    eod_hash, links_hash = queries.universe_window_hashes(
        conn, start=window_start, cutoff=data_cutoff
    )
    return queries.UniverseMonthBuild(
        month=month,
        rule_version=rule_version,
        window_start=window_start,
        data_cutoff=data_cutoff,
        eod_run_ids=run_ids,
        instruments_run_id=None if rebuild is None else rebuild[0],
        eod_hash=eod_hash,
        links_hash=links_hash,
        cutoff_inferred=cutoff_inferred,
        built_at=datetime.now(UTC),
    )


def _insert_build(conn: psycopg.Connection, build: queries.UniverseMonthBuild) -> None:
    conn.execute(
        "INSERT INTO universe_month_builds (month, rule_version, window_start, data_cutoff,"
        " eod_run_ids, instruments_run_id, eod_hash, links_hash, cutoff_inferred, built_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            build.month,
            build.rule_version,
            build.window_start,
            build.data_cutoff,
            list(build.eod_run_ids),
            build.instruments_run_id,
            build.eod_hash,
            build.links_hash,
            build.cutoff_inferred,
            build.built_at,
        ),
    )


@dataclass(frozen=True)
class BuildBackfill:
    """The (month, rule version) pairs given a build, by where their cutoff came from."""

    # Every day of the window lies in the range of a recorded nse_eod run.
    from_history: tuple[tuple[date, str], ...]
    # Some day of it does not, so the cutoff is read from the rows there now.
    cutoff_inferred: tuple[tuple[date, str], ...]

    def summary(self) -> dict[str, Any]:
        return {
            "from_history": [f"{m:%Y-%m} {v}" for m, v in self.from_history],
            "cutoff_inferred": [f"{m:%Y-%m} {v}" for m, v in self.cutoff_inferred],
        }


def backfill_universe_builds(conn: psycopg.Connection) -> BuildBackfill:
    """Give every stored month without a build one, from ``ingest_runs`` history.

    The window is the version's lookback over the end-of-day rows there now,
    as ``compute_universe`` reads it, and the hashes are of those rows. Each
    build is committed as it is written, so an interrupted backfill resumes.
    """
    pending = conn.execute(
        "SELECT DISTINCT m.month, m.rule_version, v.parameters FROM universe_membership m"
        " JOIN universe_rule_versions v ON v.version = m.rule_version"
        " WHERE NOT EXISTS (SELECT 1 FROM universe_month_builds b"
        "  WHERE b.month = m.month AND b.rule_version = m.rule_version)"
        " ORDER BY m.month, m.rule_version"
    ).fetchall()
    from_history: list[tuple[date, str]] = []
    inferred: list[tuple[date, str]] = []
    for month, rule_version, parameters in pending:
        days = _window(conn, int(parameters["lookback_trading_days"]), month)
        row = conn.execute(
            "SELECT count(*) FROM unnest(%s::date[]) AS d (day) WHERE NOT EXISTS ("
            " SELECT 1 FROM ingest_runs r WHERE r.source = %s AND r.rows_written > 0"
            " AND d.day BETWEEN r.range_start AND r.range_end)",
            (days, SOURCE_EOD),
        ).fetchone()
        uncovered = row is not None and row[0] > 0
        _insert_build(
            conn,
            _build_from(
                conn,
                month=month,
                rule_version=rule_version,
                window_start=days[0],
                data_cutoff=days[-1],
                cutoff_inferred=uncovered,
            ),
        )
        conn.commit()
        (inferred if uncovered else from_history).append((month, rule_version))
    result = BuildBackfill(tuple(from_history), tuple(inferred))
    log.info("universe builds backfilled", extra=result.summary())
    return result


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
