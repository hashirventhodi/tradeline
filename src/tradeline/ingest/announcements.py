"""``ingest announcements``: NSE and BSE corporate announcements into ``announcements``.

Two sources, each recorded as its own ingest run over the date range:
``nse_announcements`` and ``bse_announcements``; a failure of the first does
not stop the second. Each calendar day in the
range is fetched from each exchange (BSE a page at a time), so a weekend
filing is loaded like any other.

Every filing is matched to an NSE symbol against the instrument master as it
stands when the run starts (``tradeline.matching``): an NSE filing by its NSE
symbol, else its company name; a BSE filing, which carries no NSE symbol, by
its company name only. A filing with no certain match, an ambiguous name
among them, is stored with no symbol and counted in the run's details.

A filing is inserted once per (exchange, exchange id) and never changed, so a
second run over a loaded day writes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import psycopg

from tradeline.ingest import IngestRunCounters, record_ingest_run
from tradeline.matching import SymbolMatcher
from tradeline.sources import bse, nse
from tradeline.transport import Transport

log = logging.getLogger(__name__)

COMMAND = "ingest"
SOURCE_NSE = "nse_announcements"
SOURCE_BSE = "bse_announcements"


@dataclass(frozen=True)
class Filing:
    """One exchange's announcement, ready to store."""

    source: str  # 'nse' or 'bse'
    source_id: str
    disseminated_at: datetime
    received_at: datetime | None
    exchange_code: str  # NSE symbol or BSE scrip code, as the exchange gives it
    company_name: str | None
    symbol: str | None
    subject: str
    body: str | None
    attachment_url: str | None


# One day's filings from one exchange, and the requests it took.
_DayFetcher = Callable[[date], tuple[list[Filing], int]]


@dataclass(frozen=True)
class SourceResult:
    rows_seen: int
    rows_written: int
    matched: int
    unmatched: int
    requests: int


@dataclass(frozen=True)
class AnnouncementIngestResult:
    nse: SourceResult
    bse: SourceResult


def ingest_announcements(
    conn: psycopg.Connection, transport: Transport, *, start: date, end: date
) -> AnnouncementIngestResult:
    """Load every NSE and BSE announcement of each day from ``start`` to ``end`` inclusive."""
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")
    matcher = SymbolMatcher.load(conn)
    nse_session = nse.NseSession(transport)
    bse_session = bse.BseSession(transport)

    def nse_day(day: date) -> tuple[list[Filing], int]:
        return [_nse_filing(a, matcher) for a in nse.fetch_announcements(nse_session, day)], 1

    def bse_day(day: date) -> tuple[list[Filing], int]:
        fetched = bse.fetch_announcements(bse_session, day)
        return [_bse_filing(a, matcher) for a in fetched.announcements], fetched.requests

    # One exchange's outage does not cost the other's window: BSE is loaded
    # even when NSE fails, and NSE's error is raised once BSE is done.
    try:
        nse_result = _run(conn, SOURCE_NSE, start, end, nse_day)
    except Exception:
        _run(conn, SOURCE_BSE, start, end, bse_day)
        raise
    return AnnouncementIngestResult(
        nse=nse_result,
        bse=_run(conn, SOURCE_BSE, start, end, bse_day),
    )


def _run(
    conn: psycopg.Connection,
    source: str,
    start: date,
    end: date,
    fetch_day: _DayFetcher,
) -> SourceResult:
    with record_ingest_run(
        conn, command=COMMAND, source=source, range_start=start, range_end=end
    ) as run:
        _load(conn, run, start, end, fetch_day)
    return SourceResult(
        rows_seen=run.rows_seen,
        rows_written=run.rows_written,
        matched=run.details["matched"],
        unmatched=run.details["unmatched"],
        requests=run.details["requests"],
    )


def _load(
    conn: psycopg.Connection,
    run: IngestRunCounters,
    start: date,
    end: date,
    fetch_day: _DayFetcher,
) -> None:
    # Filled in as the run goes, so a failed run's record still shows what it got to.
    run.details = {"requests": 0, "matched": 0, "unmatched": 0}
    for day in _calendar_days(start, end):
        filings, requests = fetch_day(day)
        written = _insert(conn, filings)
        conn.commit()
        matched = sum(1 for f in filings if f.symbol is not None)
        run.rows_seen += len(filings)
        run.rows_written += written
        run.details["requests"] += requests
        run.details["matched"] += matched
        run.details["unmatched"] += len(filings) - matched
        log.info(
            "loaded announcements",
            extra={
                "day": day.isoformat(),
                "rows": len(filings),
                "rows_written": written,
                "matched": matched,
            },
        )


def _calendar_days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _nse_filing(a: nse.NseAnnouncement, matcher: SymbolMatcher) -> Filing:
    return Filing(
        source="nse",
        source_id=a.seq_id,
        disseminated_at=a.disseminated_at,
        received_at=a.received_at,
        exchange_code=a.symbol,
        company_name=a.company_name,
        symbol=matcher.match(symbol=a.symbol, name=a.company_name),
        subject=a.subject,
        body=a.body,
        attachment_url=a.attachment_url,
    )


def _bse_filing(a: bse.BseAnnouncement, matcher: SymbolMatcher) -> Filing:
    return Filing(
        source="bse",
        source_id=a.news_id,
        disseminated_at=a.disseminated_at,
        received_at=a.received_at,
        exchange_code=a.scrip_code,
        company_name=a.company_name,
        symbol=matcher.match(name=a.company_name),
        subject=a.subject,
        body=a.body,
        attachment_url=a.attachment_url,
    )


_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source", "text"),
    ("source_id", "text"),
    ("disseminated_at", "timestamptz"),
    ("received_at", "timestamptz"),
    ("exchange_code", "text"),
    ("company_name", "text"),
    ("symbol", "text"),
    ("subject", "text"),
    ("body", "text"),
    ("attachment_url", "text"),
)
_NAMES = ", ".join(name for name, _ in _COLUMNS)
_ARRAYS = ", ".join(f"%s::{sql_type}[]" for _, sql_type in _COLUMNS)


def _insert(conn: psycopg.Connection, filings: list[Filing]) -> int:
    """Insert the filings not already stored; return how many were."""
    # An exchange may list one filing twice in a day; the first copy is the one kept.
    unique: dict[tuple[str, str], Filing] = {}
    for f in filings:
        unique.setdefault((f.source, f.source_id), f)
    if not unique:
        return 0
    rows = list(unique.values())
    result = conn.execute(
        f"WITH inserted AS (INSERT INTO announcements ({_NAMES})"
        f" SELECT * FROM unnest({_ARRAYS})"
        " ON CONFLICT (source, source_id) DO NOTHING RETURNING 1)"
        " SELECT count(*) FROM inserted",
        tuple([getattr(f, name) for f in rows] for name, _ in _COLUMNS),
    ).fetchone()
    assert result is not None
    return int(result[0])
