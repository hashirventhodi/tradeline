"""``ingest delistings``: NSE's list of delisted securities into ``delistings``.

One ingest run (source ``nse_delistings``) fetches the whole list, one file on
the archives host, through the NSE session. Each record is stored as
published (Symbol, company name, delisting date, kind of delisting, with NSE's
padding trimmed), inserted the first time it is seen and never changed, so a
second ingest of the same list adds nothing. The records are the official
source for the end reason "delisted" (``queries.end_reasons``); the list
names no mergers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from tradeline.ingest import COMMAND, record_ingest_run
from tradeline.sources import nse
from tradeline.transport import Transport

SOURCE = "nse_delistings"


@dataclass(frozen=True)
class DelistingIngestResult:
    rows_seen: int
    rows_written: int

    def summary(self) -> dict[str, Any]:
        return {"rows_seen": self.rows_seen, "rows_written": self.rows_written}


def ingest_delistings(conn: psycopg.Connection, transport: Transport) -> DelistingIngestResult:
    session = nse.NseSession(transport)
    with record_ingest_run(conn, command=COMMAND, source=SOURCE) as run:
        records = nse.fetch_delistings(session)
        run.rows_seen = len(records)
        run.rows_written = _insert(conn, records)
        conn.commit()
    return DelistingIngestResult(rows_seen=run.rows_seen, rows_written=run.rows_written)


def _insert(conn: psycopg.Connection, records: list[nse.Delisting]) -> int:
    # The first copy of a record NSE lists twice is the one stored.
    unique: dict[tuple[str, date], nse.Delisting] = {}
    for r in records:
        unique.setdefault((r.symbol, r.delisted_on), r)
    rows = list(unique.values())
    if not rows:
        return 0
    result = conn.execute(
        "WITH inserted AS ("
        "  INSERT INTO delistings (symbol, delisted_on, company_name, kind)"
        "  SELECT * FROM unnest(%s::text[], %s::date[], %s::text[], %s::text[])"
        "  ON CONFLICT (symbol, delisted_on) DO NOTHING RETURNING 1)"
        " SELECT count(*) FROM inserted",
        (
            [r.symbol for r in rows],
            [r.delisted_on for r in rows],
            [r.company_name for r in rows],
            [r.kind for r in rows],
        ),
    ).fetchone()
    assert result is not None
    return int(result[0])
