"""``instruments rebuild``: the link builder run over the stored evidence, its output stored.

The evidence is read from the stored sources (ADR 0016), handed to the pure
builder (``tradeline.instruments.build_instruments``), and its output replaces
the derived tables (``instruments``, ``instrument_intervals``,
``instrument_links``, ``instrument_anomalies``, ``instrument_link_candidates``)
in one transaction, so a
reader sees the old Instruments or the new ones, never a mixture. Nothing
else is written: the end-of-day rows, Kite's candles, the Corporate Actions,
the symbol master, sectors, and the symbol-change records are read only.

The Raw Series is read as spans in NSE's equity listing series (EQ; BE and BZ,
trade-for-trade; SM and ST, SME). The block-deal window (BL), same-day
settlement (T0), and debt and fund series are left out: they trade beside a
Symbol's EQ rows or carry their own ISINs, and would read as ISIN changes.

The manual Instrument Links are the one piece of evidence not in the
database: the caller reads them from the checked-in file
(``tradeline.manual_links``) and passes them in, so a malformed file refuses
the rebuild before anything is read or replaced.

A rebuild never changes the stored Universe. Before replacing the
Instruments it compares them with the new build over the window of every
month stored under a rule that ranks Instruments: where any (Symbol, day) in
the window falls in a different Instrument, which is to say the window's rows
are grouped differently (a new key for the same grouping is not a change),
or where an in-window Instrument's listing date moves across the rule's
``min_listing_trading_days`` (the equity master corrected, or a link joined
a listed Symbol), so its listing-age eligibility for the month changes, the
month is recorded as stale (``universe_stale_months``) and its membership
kept. Recomputing it is the Operator's deliberate act. A rule that ranks
Symbols never reads the Instruments, so its months are never stale. A link
dated after a window leaves the rows in it grouped as they were, so an
ordinary rename makes no earlier month stale.

Each rebuild is recorded in the run history (command ``instruments``, source
``instrument_links``) with its counts: Instruments, links by source,
anomalies by kind, Link Candidates, and the Universe months it found stale.
"""

from __future__ import annotations

import logging
import uuid
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from tradeline import queries
from tradeline.ingest import record_ingest_run
from tradeline.instruments import (
    LISTING_SERIES,
    Evidence,
    InstrumentBuild,
    Interval,
    Listing,
    ManualLink,
    Span,
    build_instruments,
)

log = logging.getLogger(__name__)

COMMAND = "instruments"
SOURCE = "instrument_links"


@dataclass(frozen=True)
class InstrumentRebuildResult:
    spans: int
    instruments: int
    links: int
    anomalies: int
    link_candidates: int

    def summary(self) -> dict[str, Any]:
        return {
            "spans": self.spans,
            "instruments": self.instruments,
            "links": self.links,
            "anomalies": self.anomalies,
            "link_candidates": self.link_candidates,
        }


def rebuild_instruments(
    conn: psycopg.Connection, *, manual_links: Sequence[ManualLink] = ()
) -> InstrumentRebuildResult:
    with record_ingest_run(conn, command=COMMAND, source=SOURCE) as run:
        evidence = read_evidence(conn, manual_links=manual_links)
        built = build_instruments(evidence)
        stale = _record_stale_months(conn, built, found_by=run.id)
        _replace(conn, built)
        conn.commit()
        links = Counter(link.kind for i in built.instruments for link in i.links)
        run.rows_seen = len(evidence.spans)
        run.rows_written = len(built.instruments)
        run.details = {
            "spans": len(evidence.spans),
            "symbol_changes": len(evidence.symbol_changes),
            "manual_links": len(evidence.manual_links),
            "instruments": len(built.instruments),
            "links": dict(sorted(links.items())),
            "keys_from": dict(sorted(Counter(i.key_from for i in built.instruments).items())),
            "anomalies": dict(sorted(Counter(a.kind for a in built.anomalies).items())),
            "link_candidates": len(built.link_candidates),
            "stale_months": stale,
            "obsolete_universe_keys": len(queries.obsolete_universe_keys(conn)),
        }
        for anomaly in built.anomalies:
            log.warning(
                "instrument history split",
                extra={"kind": anomaly.kind, "symbol": anomaly.symbol, "detail": anomaly.detail},
            )
    return InstrumentRebuildResult(
        spans=len(evidence.spans),
        instruments=len(built.instruments),
        links=sum(links.values()),
        anomalies=len(built.anomalies),
        link_candidates=len(built.link_candidates),
    )


def read_evidence(conn: psycopg.Connection, *, manual_links: Sequence[ManualLink] = ()) -> Evidence:
    """Everything the builder reads: the stored sources, and the manual links given."""
    return Evidence(
        spans=_spans(conn),
        symbol_changes=queries.symbol_changes(conn),
        corporate_actions=queries.adjusting_corporate_actions(conn),
        listings=_listings(conn),
        manual_links=tuple(manual_links),
    )


def _spans(conn: psycopg.Connection) -> list[Span]:
    # A new span starts wherever a Symbol's (series, ISIN) differs from its previous row's.
    rows = conn.execute(
        "WITH marked AS ("
        "  SELECT symbol, series, isin, trade_date,"
        "   CASE WHEN (series, isin) IS NOT DISTINCT FROM (lag(series) OVER w, lag(isin) OVER w)"
        "    THEN 0 ELSE 1 END AS starts"
        "  FROM eod_bhavcopy WHERE series = ANY(%s)"
        "  WINDOW w AS (PARTITION BY symbol ORDER BY trade_date, series)),"
        " numbered AS ("
        "  SELECT *, sum(starts) OVER (PARTITION BY symbol ORDER BY trade_date, series) AS run"
        "  FROM marked)"
        " SELECT symbol, series, isin, min(trade_date), max(trade_date) FROM numbered"
        " GROUP BY symbol, run, series, isin ORDER BY symbol, min(trade_date), series",
        (list(LISTING_SERIES),),
    ).fetchall()
    return [Span(*row) for row in rows]


def _listings(conn: psycopg.Connection) -> list[Listing]:
    rows = conn.execute(
        "SELECT DISTINCT symbol, isin, listing_date FROM symbols"
        " WHERE listing_date IS NOT NULL ORDER BY symbol, isin, listing_date"
    ).fetchall()
    return [Listing(*row) for row in rows]


def _record_stale_months(
    conn: psycopg.Connection, built: InstrumentBuild, *, found_by: uuid.UUID
) -> int:
    """Record each stored Universe month the build regroups or changes the listing age of;
    returns how many were new.

    Only months under a rule that ranks Instruments and not already stale are
    compared. Each is compared over its window as the rule defines it: the
    ``lookback_trading_days`` days with end-of-day rows before the month.
    """
    months = conn.execute(
        "SELECT DISTINCT m.month, m.rule_version, (v.parameters->>'lookback_trading_days')::int,"
        " (v.parameters->>'min_listing_trading_days')::int,"
        " v.parameters->>'listing_age_from' IS NOT DISTINCT FROM 'earliest_evidence'"
        " FROM universe_membership m JOIN universe_rule_versions v ON v.version = m.rule_version"
        " WHERE v.parameters->>'rank_by' = 'instrument' AND NOT EXISTS ("
        "  SELECT 1 FROM universe_stale_months s"
        "  WHERE s.month = m.month AND s.rule_version = m.rule_version)"
        " ORDER BY m.month, m.rule_version"
    ).fetchall()
    if not months:
        return 0
    calendar = [
        day
        for (day,) in conn.execute(
            "SELECT DISTINCT trade_date FROM eod_bhavcopy ORDER BY trade_date"
        ).fetchall()
    ]
    old = [
        (key, (listed_on, first_row), Interval(symbol, series, isin, first, last))
        for key, listed_on, first_row, symbol, series, isin, first, last in conn.execute(
            "SELECT v.instrument_key, i.listed_on,"
            " min(v.first_day) OVER (PARTITION BY v.instrument_key),"
            " v.symbol, v.series, v.isin, v.first_day,"
            " v.last_day FROM instrument_intervals v JOIN instruments i ON i.key = v.instrument_key"
        ).fetchall()
    ]
    new = [
        (i.key, (i.listed_on, min(v.first for v in i.intervals)), v)
        for i in built.instruments
        for v in i.intervals
    ]
    stale = []
    for month, version, lookback, min_listing, earliest in months:
        end = bisect_left(calendar, month)
        if end == 0:
            continue
        first, last = calendar[max(0, end - lookback)], calendar[end - 1]
        # The latest listing date with ``min_listing`` trading days to the month, as the
        # Universe counts them; None when the calendar holds too few.
        listed_by = calendar[end - min_listing] if end >= min_listing else None
        if _grouping(old, first, last, listed_by, earliest) != _grouping(
            new, first, last, listed_by, earliest
        ):
            stale.append((month, version))
    for month, version in stale:
        log.warning(
            "universe month stale: the rebuild regroups its window or changes a listing age;"
            " stored membership kept",
            extra={"month": month.isoformat(), "rule_version": version},
        )
    conn.execute(
        "INSERT INTO universe_stale_months (month, rule_version, found_by)"
        " SELECT m, v, %s FROM unnest(%s::date[], %s::text[]) AS t (m, v)",
        (found_by, [m for m, _ in stale], [v for _, v in stale]),
    )
    return len(stale)


def _grouping(
    intervals: Sequence[tuple[str, tuple[date, date], Interval]],
    first: date,
    last: date,
    listed_by: date | None,
    earliest: bool,
) -> frozenset[tuple[frozenset[tuple[str, str, date, date]], bool]]:
    """How the rows from ``first`` to ``last`` are grouped into Instruments, keys aside, each
    group with whether its Instrument was listed by ``listed_by``, long enough for the month.

    Each interval carries its Instrument's listing date and first row; under a rule
    counting Listing Age from the earliest evidence (ADR 0017) the earlier of the two
    is the Instrument's listing: the rule of the view ``instrument_listing_age``
    (migration 0021), restated here because the rebuild's Instruments are not stored
    yet. Change the two together."""
    groups: defaultdict[str, set[tuple[str, str, date, date]]] = defaultdict(set)
    eligible: dict[str, bool] = {}
    for key, (listed_on, first_row), interval in intervals:
        if interval.first <= last and interval.last >= first:
            listed = min(listed_on, first_row) if earliest else listed_on
            eligible[key] = listed_by is not None and listed <= listed_by
            groups[key].add(
                (
                    interval.symbol,
                    interval.series,
                    max(interval.first, first),
                    min(interval.last, last),
                )
            )
    return frozenset((frozenset(group), eligible[key]) for key, group in groups.items())


def _replace(conn: psycopg.Connection, built: InstrumentBuild) -> None:
    conn.execute("DELETE FROM instrument_anomalies")
    conn.execute("DELETE FROM instrument_link_candidates")
    conn.execute("DELETE FROM instruments")  # intervals and links go with them
    instruments = built.instruments
    conn.execute(
        "INSERT INTO instruments (key, first_symbol, listed_on, key_from)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[])",
        (
            [i.key for i in instruments],
            [i.first_symbol for i in instruments],
            [i.listed_on for i in instruments],
            [i.key_from for i in instruments],
        ),
    )
    intervals = [(i.key, v) for i in instruments for v in i.intervals]
    conn.execute(
        "INSERT INTO instrument_intervals"
        " (instrument_key, symbol, series, isin, first_day, last_day)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[],"
        "  %s::date[], %s::date[])",
        (
            [key for key, _ in intervals],
            [v.symbol for _, v in intervals],
            [v.series for _, v in intervals],
            [v.isin for _, v in intervals],
            [v.first for _, v in intervals],
            [v.last for _, v in intervals],
        ),
    )
    links = [(i.key, link) for i in instruments for link in i.links]
    conn.execute(
        "INSERT INTO instrument_links"
        " (instrument_key, kind, on_date, from_symbol, from_isin, to_symbol, to_isin, source)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[], %s::text[],"
        "  %s::text[], %s::text[], %s::text[])",
        (
            [key for key, _ in links],
            [x.kind for _, x in links],
            [x.on for _, x in links],
            [x.from_symbol for _, x in links],
            [x.from_isin for _, x in links],
            [x.to_symbol for _, x in links],
            [x.to_isin for _, x in links],
            [x.source for _, x in links],
        ),
    )
    anomalies = built.anomalies
    conn.execute(
        "INSERT INTO instrument_anomalies (kind, symbol, on_date, detail)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[])",
        (
            [a.kind for a in anomalies],
            [a.symbol for a in anomalies],
            [a.on for a in anomalies],
            [a.detail for a in anomalies],
        ),
    )
    candidates = built.link_candidates
    conn.execute(
        "INSERT INTO instrument_link_candidates"
        " (old_symbol, new_symbol, isin, old_last, new_first)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::date[], %s::date[])",
        (
            [c.old_symbol for c in candidates],
            [c.new_symbol for c in candidates],
            [c.isin for c in candidates],
            [c.old_last for c in candidates],
            [c.new_first for c in candidates],
        ),
    )
