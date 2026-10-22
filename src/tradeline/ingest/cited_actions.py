"""``ingest cited-actions``: the reviewed rows citing NSE's circulars into ``corporate_actions``.

The rows are read whole from the checked-in file (``tradeline.cited_actions``)
by the caller, so a malformed file refuses the run before anything is
written. One ingest run (source ``cited_corporate_actions``) checks each
Symbol's rows of one Ex-Date together, against the price and against NSE's
lists (ADR 0006, amended by #37):

- **The price must agree.** The factor of the rows together (a split and a
  bonus on one day compound) may differ by at most ``PRICE_TOLERANCE`` from
  the Ex-Date close over the previous close, both read from the Raw Series
  across NSE's listing series (``LISTING_SERIES``). Rows that differ by more,
  or that lack either close, are refused: not stored, logged, and listed in
  the run's details. A wrong ratio or Ex-Date is out by a factor of two or
  more; a price band or an odd print is not.
- **NSE's list wins.** Rows for which the Instrument their Symbol named on the
  Ex-Date has a listed split or bonus within ``SUPERSEDED_WITHIN`` are
  reported as superseded (``queries.cited_supersession``), and, where the
  listed factor differs from theirs, as an anomaly. They are stored all the
  same: the Adjusted Series never applies a superseded row (``adjust.adjust``),
  whichever arrived first. Every Instruments rebuild reports the same over
  all stored rows, through the same check, so a row NSE lists later is
  reported without this command.

A row passing the price check is stored with its citation and no NSE list,
its kind and factor as ``adjust.interpret`` reads its subject, and the series
of its previous close. It is inserted once per (Symbol, series, Ex-Date,
subject) and never changed, as NSE's listed actions are.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

import psycopg

from tradeline import queries
from tradeline.adjust import CorporateAction
from tradeline.cited_actions import CitedAction
from tradeline.ingest import COMMAND, record_ingest_run
from tradeline.instruments import LISTING_SERIES

log = logging.getLogger(__name__)

SOURCE = "cited_corporate_actions"
# How far the rows' factor may be from the Ex-Date close over the previous close.
PRICE_TOLERANCE = Fraction(1, 5)


@dataclass(frozen=True)
class CitedActionIngestResult:
    rows_seen: int
    rows_written: int
    # Rows refused, and rows NSE's list supersedes.
    refused: int
    superseded: int
    # Ex-Dates on which NSE's list prices the change differently from the rows.
    anomalies: int

    def summary(self) -> dict[str, Any]:
        return {
            "rows_seen": self.rows_seen,
            "rows_written": self.rows_written,
            "refused": self.refused,
            "superseded": self.superseded,
            "anomalies": self.anomalies,
        }


def ingest_cited_actions(
    conn: psycopg.Connection, rows: Sequence[CitedAction]
) -> CitedActionIngestResult:
    """Check each Symbol's rows of one Ex-Date against the price and NSE's lists; store those
    the price agrees with."""
    refused: list[dict[str, Any]] = []
    superseding: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    with record_ingest_run(conn, command=COMMAND, source=SOURCE) as run:
        run.details = {"refused": refused, "superseded": superseding, "anomalies": anomalies}
        by_day: dict[tuple[str, date], list[CitedAction]] = defaultdict(list)
        for row in rows:
            by_day[(row.symbol, row.ex_date)].append(row)
        actions: list[CorporateAction] = []
        for (symbol, ex_date), cited in sorted(by_day.items()):
            checked = _checked(conn, symbol, ex_date, cited)
            if isinstance(checked, str):
                record = _day(symbol, ex_date, cited) | {"reason": checked}
                log.warning("cited corporate action refused", extra=record)
                refused.append(record)
                continue
            found = queries.cited_supersession(conn, symbol, ex_date, checked)
            if found is not None:
                record = _day(symbol, ex_date, cited) | found.listed_record()
                log.warning("cited corporate action superseded by NSE's list", extra=record)
                superseding.append(record)
                if not found.agrees:
                    log.warning("NSE's list prices the change differently", extra=record)
                    anomalies.append(record)
            actions.extend(checked)
        run.rows_seen = len(rows)
        run.rows_written = _insert(conn, actions)
        conn.commit()
    return CitedActionIngestResult(
        rows_seen=len(rows),
        rows_written=run.rows_written,
        refused=sum(len(r["lines"]) for r in refused),
        superseded=sum(len(r["lines"]) for r in superseding),
        anomalies=len(anomalies),
    )


def _day(symbol: str, ex_date: date, cited: list[CitedAction]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "ex_date": ex_date.isoformat(),
        "lines": [row.line for row in cited],
        "subjects": [row.subject for row in cited],
    }


def _checked(
    conn: psycopg.Connection, symbol: str, ex_date: date, cited: list[CitedAction]
) -> list[CorporateAction] | str:
    """The rows as actions, if the price agrees with their factor together; else why not."""
    before = _close(conn, symbol, ex_date, on_ex_date=False)
    on = _close(conn, symbol, ex_date, on_ex_date=True)
    if before is None or on is None:
        missing = "previous close" if before is None else "Ex-Date close"
        return f"no {missing} for {symbol} in {', '.join(LISTING_SERIES)}"
    series, previous_day, previous = before
    _, _, ex_close = on
    factor = Fraction(1)
    for row in cited:
        factor *= row.factor
    moved = Fraction(ex_close) / Fraction(previous)
    if abs(moved / factor - 1) > PRICE_TOLERANCE:
        return (
            f"factor {factor} is more than {PRICE_TOLERANCE} from the close's move,"
            f" {previous} on {previous_day.isoformat()} to {ex_close} on {ex_date.isoformat()}"
        )
    return [
        CorporateAction(
            symbol=symbol,
            series=series,
            ex_date=ex_date,
            subject=row.subject,
            kind=row.kind,
            factor=row.factor,
            nse_list=None,
            citation=row.citation,
        )
        for row in cited
    ]


def _close(
    conn: psycopg.Connection, symbol: str, ex_date: date, *, on_ex_date: bool
) -> tuple[str, date, Decimal] | None:
    """The Symbol's close in a listing series on the Ex-Date, or its latest before it."""
    row = conn.execute(
        "SELECT series, trade_date, close FROM eod_bhavcopy"
        " WHERE symbol = %s AND series = ANY(%s) AND close IS NOT NULL"
        " AND CASE WHEN %s THEN trade_date = %s ELSE trade_date < %s END"
        " ORDER BY trade_date DESC, series LIMIT 1",
        (symbol, list(LISTING_SERIES), on_ex_date, ex_date, ex_date),
    ).fetchone()
    return None if row is None else (row[0], row[1], row[2])


def _insert(conn: psycopg.Connection, actions: list[CorporateAction]) -> int:
    if not actions:
        return 0
    factors = [a.factor for a in actions if a.factor is not None]
    assert len(factors) == len(actions), "a cited action always has a factor"
    result = conn.execute(
        "WITH inserted AS ("
        " INSERT INTO corporate_actions (symbol, series, ex_date, subject, kind,"
        "  factor_numerator, factor_denominator, nse_list, citation)"
        " SELECT symbol, series, ex_date, subject, kind, numerator, denominator, NULL, citation"
        " FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[], %s::text[],"
        "  %s::bigint[], %s::bigint[], %s::text[])"
        "  AS n (symbol, series, ex_date, subject, kind, numerator, denominator, citation)"
        " ON CONFLICT (symbol, series, ex_date, subject) DO NOTHING RETURNING 1)"
        " SELECT count(*) FROM inserted",
        (
            [a.symbol for a in actions],
            [a.series for a in actions],
            [a.ex_date for a in actions],
            [a.subject for a in actions],
            [a.kind for a in actions],
            [f.numerator for f in factors],
            [f.denominator for f in factors],
            [a.citation for a in actions],
        ),
    ).fetchone()
    assert result is not None
    return int(result[0])
