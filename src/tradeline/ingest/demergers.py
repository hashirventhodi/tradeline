"""``ingest demergers``: the reviewed demerger table into ``demergers``, whole.

The rows are read whole from the checked-in file (``tradeline.demergers``) by
the caller, so a malformed table refuses the run before anything is written.
One ingest run (source ``demergers``) stores every row under its id; the
table in force is the latest successful run's (``queries.demergers``), so an
edit is a new run and the old rows stay as they were.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from tradeline.demergers import DemergerTable
from tradeline.ingest import COMMAND, record_ingest_run

SOURCE = "demergers"


@dataclass(frozen=True)
class DemergerIngestResult:
    rows_written: int
    # The stored table's hash, as a Protocol crediting demergers freezes it.
    hash: str

    def summary(self) -> dict[str, Any]:
        return {"rows_written": self.rows_written, "hash": self.hash}


def ingest_demergers(conn: psycopg.Connection, table: DemergerTable) -> DemergerIngestResult:
    """Store every row of ``table`` as the demerger table in force from now."""
    with record_ingest_run(conn, command=COMMAND, source=SOURCE) as run:
        run.details = {"hash": table.hash}
        for row in table.rows:
            run.rows_seen += 1
            conn.execute(
                "INSERT INTO demergers (ingest_run_id, line, parent, child, ratio_numerator,"
                " ratio_denominator, ex_date, listed_on, parent_basis, child_basis, citation)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run.id,
                    row.line,
                    row.parent,
                    row.child,
                    row.ratio.numerator,
                    row.ratio.denominator,
                    row.ex_date,
                    row.listed_on,
                    row.parent_basis,
                    row.child_basis,
                    row.citation,
                ),
            )
            run.rows_written += 1
        conn.commit()
    return DemergerIngestResult(len(table.rows), table.hash)
