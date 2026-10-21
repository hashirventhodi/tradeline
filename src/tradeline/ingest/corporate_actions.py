"""``ingest corporate-actions``: NSE's corporate-action list into ``corporate_actions``.

One ingest run (source ``nse_corporate_actions``) covers the ex-date range, one
request per calendar year (and per symbol when symbols are named) so that a
ten-year load is ten bounded responses rather than one. Each record's subject
is read by ``tradeline.adjust.interpret`` into a kind and, for splits and
bonuses, the price factor, which are stored beside NSE's facts.

A record is inserted once per (symbol, series, ex-date, subject) and never
changed. A record already stored for which this run derives a different kind
or factor (as after a parser fix) is an anomaly: logged, recorded in the run's
details with both derivations, and the stored row kept, as for a resent
candle. A record that mentions a bonus, split, or consolidation but could not
be given a factor is stored as unadjusted, logged, and listed in the run's
details, since that is the one mistake the adjusted series could not reveal
on its own.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any

import psycopg

from tradeline.adjust import CorporateAction, interpret, looks_adjusting
from tradeline.ingest import IngestRunCounters, record_ingest_run
from tradeline.sources import nse
from tradeline.transport import Transport

log = logging.getLogger(__name__)

COMMAND = "ingest"
SOURCE = "nse_corporate_actions"
# Unparsed subjects and anomalies are counted in full; this many of each are recorded in detail.
MAX_RECORDED = 100


@dataclass(frozen=True)
class CorporateActionIngestResult:
    rows_seen: int
    rows_written: int
    unparsed: int
    anomalies: int
    requests: int


def ingest_corporate_actions(
    conn: psycopg.Connection,
    transport: Transport,
    *,
    start: date,
    end: date,
    symbols: Sequence[str] | None = None,
) -> CorporateActionIngestResult:
    """Load every action with an ex-date in the range (one symbol's at a time when named)."""
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    session = nse.NseSession(transport)
    with record_ingest_run(
        conn, command=COMMAND, source=SOURCE, range_start=start, range_end=end
    ) as run:
        _load(conn, session, run, start, end, symbols)
    return CorporateActionIngestResult(
        rows_seen=run.rows_seen,
        rows_written=run.rows_written,
        unparsed=run.details["unparsed"],
        anomalies=run.details["anomalies"],
        requests=run.details["requests"],
    )


def _load(
    conn: psycopg.Connection,
    session: nse.NseSession,
    run: IngestRunCounters,
    start: date,
    end: date,
    symbols: Sequence[str] | None,
) -> None:
    by_kind: Counter[str] = Counter()
    unparsed_subjects: list[dict[str, Any]] = []
    anomaly_actions: list[dict[str, Any]] = []
    # Filled in as the run goes, so a failed run's record still shows what it got to.
    run.details = {
        "requests": 0,
        "by_kind": {},
        "unparsed": 0,
        "unparsed_subjects": unparsed_subjects,
        "anomalies": 0,
        "anomaly_actions": anomaly_actions,
    }
    for chunk_start, chunk_end in year_chunks(start, end):
        for symbol in [None] if symbols is None else list(symbols):
            notices = nse.fetch_corporate_actions(
                session, start=chunk_start, end=chunk_end, symbol=symbol
            )
            run.details["requests"] += 1
            actions = [_derive(notice) for notice in notices]
            for action in actions:
                by_kind[action.kind] += 1
                if action.factor is None and looks_adjusting(action.subject):
                    run.details["unparsed"] += 1
                    record = {
                        "symbol": action.symbol,
                        "ex_date": action.ex_date.isoformat(),
                        "subject": action.subject,
                    }
                    log.warning("corporate action looks adjusting but has no factor", extra=record)
                    if len(unparsed_subjects) < MAX_RECORDED:
                        unparsed_subjects.append(record)
            written, anomalies = _insert(conn, actions)
            conn.commit()
            run.rows_seen += len(notices)
            run.rows_written += written
            run.details["anomalies"] += len(anomalies)
            for record in anomalies:
                log.warning("corporate action derived differently; stored row kept", extra=record)
                if len(anomaly_actions) < MAX_RECORDED:
                    anomaly_actions.append(record)
            log.info(
                "loaded corporate actions",
                extra={
                    "chunk_start": chunk_start.isoformat(),
                    "chunk_end": chunk_end.isoformat(),
                    "symbol": symbol,
                    "rows": len(notices),
                    "rows_written": written,
                },
            )
    run.details["by_kind"] = dict(sorted(by_kind.items()))


def year_chunks(start: date, end: date) -> Iterator[tuple[date, date]]:
    """The range cut at calendar-year boundaries, inclusive at both ends."""
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, date(chunk_start.year, 12, 31))
        yield chunk_start, chunk_end
        chunk_start = date(chunk_start.year + 1, 1, 1)


def _derive(notice: nse.CorporateActionNotice) -> CorporateAction:
    kind, factor = interpret(notice.subject)
    return CorporateAction(
        symbol=notice.symbol,
        series=notice.series,
        ex_date=notice.ex_date,
        subject=notice.subject,
        kind=kind,
        factor=factor,
        isin=notice.isin,
        record_date=notice.record_date,
        face_value=notice.face_value,
    )


_KEY = ("symbol", "series", "ex_date", "subject")
_DERIVED = ("kind", "factor_numerator", "factor_denominator")
_COLUMNS = (*_KEY, "isin", "record_date", "face_value", *_DERIVED)
_NAMES = ", ".join(_COLUMNS)


def _insert(
    conn: psycopg.Connection, actions: list[CorporateAction]
) -> tuple[int, list[dict[str, Any]]]:
    """Insert what is new; return the count and an anomaly per stored row derived differently."""
    # NSE's list occasionally repeats a record; the first copy is the one offered.
    unique: dict[tuple[str, str, date, str], CorporateAction] = {}
    for a in actions:
        unique.setdefault((a.symbol, a.series, a.ex_date, a.subject), a)
    if not unique:
        return 0, []
    rows = list(unique.values())
    stored = ", ".join(f"c.{name}" for name in _DERIVED)
    incoming = ", ".join(f"n.{name}" for name in _DERIVED)
    result = conn.execute(
        "WITH incoming AS ("
        "  SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[], %s::text[],"
        "   %s::date[], %s::numeric[], %s::text[], %s::bigint[], %s::bigint[])"
        f"  AS n({_NAMES})),"
        " inserted AS ("
        f"  INSERT INTO corporate_actions ({_NAMES}) SELECT {_NAMES} FROM incoming"
        "  ON CONFLICT (symbol, series, ex_date, subject) DO NOTHING RETURNING 1)"
        " SELECT (SELECT count(*) FROM inserted),"
        "  coalesce(array_agg(ARRAY[n.symbol, n.series, n.ex_date::text, n.subject,"
        "   c.kind, c.factor_numerator::text, c.factor_denominator::text,"
        "   n.kind, n.factor_numerator::text, n.factor_denominator::text]"
        "   ORDER BY n.ex_date, n.symbol, n.series, n.subject)"
        "   FILTER (WHERE c.symbol IS NOT NULL), '{}')"
        " FROM incoming n LEFT JOIN corporate_actions c"
        "  ON (c.symbol, c.series, c.ex_date, c.subject)"
        "   = (n.symbol, n.series, n.ex_date, n.subject)"
        f"  AND ({stored}) IS DISTINCT FROM ({incoming})",
        (
            [a.symbol for a in rows],
            [a.series for a in rows],
            [a.ex_date for a in rows],
            [a.subject for a in rows],
            [a.isin for a in rows],
            [a.record_date for a in rows],
            [a.face_value for a in rows],
            [a.kind for a in rows],
            [None if a.factor is None else a.factor.numerator for a in rows],
            [None if a.factor is None else a.factor.denominator for a in rows],
        ),
    ).fetchone()
    assert result is not None
    written, changed = result
    return int(written), [_anomaly(values) for values in changed]


def _anomaly(values: list[str | None]) -> dict[str, Any]:
    symbol, series, ex_date, subject = values[:4]
    return {
        "symbol": symbol,
        "series": series,
        "ex_date": ex_date,
        "subject": subject,
        "stored": _derivation(values[4:7]),
        "derived": _derivation(values[7:10]),
    }


def _derivation(values: list[str | None]) -> dict[str, str | None]:
    kind, numerator, denominator = values
    factor = None if numerator is None else str(Fraction(int(numerator), int(str(denominator))))
    return {"kind": kind, "factor": factor}
