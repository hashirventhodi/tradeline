"""``ingest symbol-changes``: NSE's symbol-change list into ``symbol_changes``.

One ingest run (source ``nse_symbol_changes``) fetches the whole list, one
file NSE republishes as it grows, through the NSE session. Each record is
stored as published (company name, old Symbol, new Symbol, effective date,
with NSE's padding trimmed), inserted the first time it is seen and never
changed, so a second ingest of the same list adds nothing. The records are
evidence for the link builder (``tradeline.instruments``); nothing here
decides what they join.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from tradeline.ingest import COMMAND, record_ingest_run
from tradeline.sources import nse
from tradeline.transport import Transport

log = logging.getLogger(__name__)

SOURCE = "nse_symbol_changes"


@dataclass(frozen=True)
class SymbolChangeIngestResult:
    rows_seen: int
    rows_written: int

    def summary(self) -> dict[str, Any]:
        return {"rows_seen": self.rows_seen, "rows_written": self.rows_written}


def ingest_symbol_changes(
    conn: psycopg.Connection, transport: Transport
) -> SymbolChangeIngestResult:
    session = nse.NseSession(transport)
    with record_ingest_run(conn, command=COMMAND, source=SOURCE) as run:
        records = nse.fetch_symbol_changes(session)
        run.rows_seen = len(records)
        run.rows_written = _insert(conn, records)
        conn.commit()
    return SymbolChangeIngestResult(rows_seen=run.rows_seen, rows_written=run.rows_written)


def _insert(conn: psycopg.Connection, records: list[nse.SymbolChange]) -> int:
    # The first copy of a record NSE lists twice is the one stored.
    unique: dict[tuple[str, str, object], nse.SymbolChange] = {}
    for r in records:
        unique.setdefault((r.old_symbol, r.new_symbol, r.effective_date), r)
    rows = list(unique.values())
    if not rows:
        return 0
    result = conn.execute(
        "WITH inserted AS ("
        "  INSERT INTO symbol_changes (old_symbol, new_symbol, effective_date, company_name)"
        "  SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[])"
        "  ON CONFLICT (old_symbol, new_symbol, effective_date) DO NOTHING RETURNING 1)"
        " SELECT count(*) FROM inserted",
        (
            [r.old_symbol for r in rows],
            [r.new_symbol for r in rows],
            [r.effective_date for r in rows],
            [r.company_name for r in rows],
        ),
    ).fetchone()
    assert result is not None
    return int(result[0])
