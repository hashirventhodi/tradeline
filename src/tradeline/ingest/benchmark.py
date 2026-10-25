"""``ingest benchmark``: AMFI's scheme master and NAV history for the Benchmark and the
factor fund (ADR 0015 amendment) into ``amfi_schemes`` and ``amfi_navs``.

One ingest run (source ``amfi``) reads the scheme master once, then every
scheme's NAV history in four-year windows, about 4 s apart
(``tradeline.sources.amfi``). Before any NAV is fetched, every Benchmark scheme
the master lists (``tradeline.benchmark``'s funds file) must be named with both
"Direct" and "Growth", and the scheme in force on ``end`` must be listed:
otherwise the run fails, ``NotDirectGrowth`` or ``LookupError``, and stores
nothing. A scheme the master no longer lists (one closed since) keeps its stored
line.

Each Benchmark scheme's history is read from the first day it is the Benchmark
to the last (``end`` for the current one); the factor fund's, reported only, from
the Benchmark's first date. Both tables are append-only: a master line is stored
when it differs from the code's latest, a NAV when its (scheme, date, value) is
new, so a second run over the same range writes nothing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from tradeline.benchmark import BenchmarkFunds, require_direct_growth
from tradeline.ingest import COMMAND, IngestRunCounters, record_ingest_run
from tradeline.sources import amfi
from tradeline.transport import Transport

log = logging.getLogger(__name__)

SOURCE = "amfi"


@dataclass(frozen=True)
class BenchmarkIngestResult:
    rows_seen: int
    rows_written: int

    def summary(self) -> dict[str, Any]:
        return {"rows_seen": self.rows_seen, "rows_written": self.rows_written}


def ingest_benchmark(
    conn: psycopg.Connection,
    transport: Transport,
    *,
    funds: BenchmarkFunds,
    factor_fund: int,
    end: date,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BenchmarkIngestResult:
    """The master lines and NAVs of every Benchmark scheme and the factor fund to ``end``."""
    session = amfi.AmfiSession(transport, clock=clock, sleep=sleep)
    with record_ingest_run(
        conn, command=COMMAND, source=SOURCE, range_start=funds.first, range_end=end
    ) as run:
        _load(conn, session, run, funds, factor_fund, end)
    return BenchmarkIngestResult(rows_seen=run.rows_seen, rows_written=run.rows_written)


def _load(
    conn: psycopg.Connection,
    session: amfi.AmfiSession,
    run: IngestRunCounters,
    funds: BenchmarkFunds,
    factor_fund: int,
    end: date,
) -> None:
    fetched_at = datetime.now(UTC)
    listed = {r.scheme_code: r for r in amfi.fetch_scheme_master(session)}
    current = funds.scheme_on(end)
    if current not in listed:
        raise LookupError(f"AMFI's scheme master does not list the Benchmark's scheme {current}")
    for code in funds.schemes:
        if code in listed:
            require_direct_growth(code, listed[code].full_name)
    ranges = [r for code in funds.schemes if (r := _benchmark_range(funds, code, end)) is not None]
    ranges.append((factor_fund, funds.first, end))
    wanted = [listed[code] for code in dict.fromkeys(c for c, _, _ in ranges) if code in listed]
    run.rows_seen += len(wanted)
    run.rows_written += _insert_schemes(conn, wanted, fetched_at)
    conn.commit()
    per_scheme: dict[str, int] = {}
    run.details = {"navs": per_scheme}
    for code, start, last in ranges:
        records = amfi.fetch_nav_history(session, code, start, last)
        written = _insert_navs(conn, records, fetched_at)
        conn.commit()
        per_scheme[str(code)] = len(records)
        run.rows_seen += len(records)
        run.rows_written += written
        log.info(
            "loaded NAV history",
            extra={"scheme_code": code, "rows": len(records), "rows_written": written},
        )


def _benchmark_range(funds: BenchmarkFunds, code: int, end: date) -> tuple[int, date, date] | None:
    first, last = funds.span(code)
    last = end if last is None else min(last, end)
    return None if first > last else (code, first, last)


def _insert_schemes(
    conn: psycopg.Connection, records: Sequence[amfi.SchemeRecord], fetched_at: datetime
) -> int:
    written = 0
    for r in records:
        row = conn.execute(
            "INSERT INTO amfi_schemes (scheme_code, scheme_name, plan, option, isin_growth,"
            "  isin_reinvestment, nav_date, fetched_at)"
            " SELECT %(code)s, %(name)s, %(plan)s, %(option)s, %(growth)s, %(reinvestment)s,"
            "  %(nav_date)s, %(fetched_at)s"
            " WHERE NOT EXISTS ("
            "  SELECT 1 FROM (SELECT * FROM amfi_schemes WHERE scheme_code = %(code)s"
            "   ORDER BY fetched_at DESC LIMIT 1) latest"
            "  WHERE (latest.scheme_name, latest.plan, latest.option) ="
            "   (%(name)s, %(plan)s, %(option)s)"
            "  AND latest.isin_growth IS NOT DISTINCT FROM %(growth)s"
            "  AND latest.isin_reinvestment IS NOT DISTINCT FROM %(reinvestment)s)"
            " RETURNING 1",
            {
                "code": r.scheme_code,
                "name": r.scheme_name,
                "plan": r.plan,
                "option": r.option,
                "growth": r.isin_growth,
                "reinvestment": r.isin_reinvestment,
                "nav_date": r.nav_date,
                "fetched_at": fetched_at,
            },
        ).fetchone()
        written += row is not None
    return written


def _insert_navs(
    conn: psycopg.Connection, records: Sequence[amfi.NavRecord], fetched_at: datetime
) -> int:
    if not records:
        return 0
    result = conn.execute(
        "WITH inserted AS ("
        "  INSERT INTO amfi_navs (scheme_code, nav_date, nav, uploaded_at, fetched_at)"
        "  SELECT n.scheme_code, n.nav_date, n.nav, n.uploaded_at, %s"
        "  FROM unnest(%s::integer[], %s::date[], %s::numeric[], %s::timestamp[])"
        "   AS n(scheme_code, nav_date, nav, uploaded_at)"
        "  ON CONFLICT (scheme_code, nav_date, nav) DO NOTHING RETURNING 1)"
        " SELECT count(*) FROM inserted",
        (
            fetched_at,
            [r.scheme_code for r in records],
            [r.nav_date for r in records],
            [r.nav for r in records],
            [r.uploaded_at for r in records],
        ),
    ).fetchone()
    assert result is not None
    return int(result[0])
