"""``ingest index-history``: the Total Return Index history the Relative Drawdown Halt's
threshold is computed from (ADR 0020), from niftyindices.com into ``index_values``.

One ingest run (source ``niftyindices``) reads each index of
``tradeline.index_history.INDICES`` over ``HISTORY_START`` to ``HISTORY_END``,
a year a call, about 4 s apart (``tradeline.sources.niftyindices``). A run
that fails stores the indices it finished and none of the one it was reading.
The table is append-only: a value is stored when its (index, day, value) is
new, so a second run writes nothing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from tradeline.index_history import HISTORY_END, HISTORY_START, INDICES
from tradeline.ingest import COMMAND, record_ingest_run
from tradeline.sources import niftyindices
from tradeline.transport import Transport

log = logging.getLogger(__name__)

SOURCE = "niftyindices"


@dataclass(frozen=True)
class IndexHistoryIngestResult:
    rows_seen: int
    rows_written: int

    def summary(self) -> dict[str, Any]:
        return {"rows_seen": self.rows_seen, "rows_written": self.rows_written}


def ingest_index_history(
    conn: psycopg.Connection,
    transport: Transport,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> IndexHistoryIngestResult:
    """Every index's Total Return Index from ``HISTORY_START`` to ``HISTORY_END``."""
    session = niftyindices.NiftyIndicesSession(transport, clock=clock, sleep=sleep)
    with record_ingest_run(
        conn, command=COMMAND, source=SOURCE, range_start=HISTORY_START, range_end=HISTORY_END
    ) as run:
        fetched_at = datetime.now(UTC)
        per_index: dict[str, int] = {}
        run.details = {"values": per_index}
        for name in INDICES:
            values = niftyindices.fetch_total_return_index(
                session, name, HISTORY_START, HISTORY_END
            )
            written = _insert(conn, values, fetched_at)
            conn.commit()
            per_index[name] = len(values)
            run.rows_seen += len(values)
            run.rows_written += written
            log.info(
                "loaded index history",
                extra={"index": name, "rows": len(values), "rows_written": written},
            )
    return IndexHistoryIngestResult(rows_seen=run.rows_seen, rows_written=run.rows_written)


def _insert(
    conn: psycopg.Connection, values: Sequence[niftyindices.IndexValue], fetched_at: datetime
) -> int:
    if not values:
        return 0
    result = conn.execute(
        "WITH inserted AS ("
        "  INSERT INTO index_values (index_name, day, value, fetched_at)"
        "  SELECT v.index_name, v.day, v.value, %s"
        "  FROM unnest(%s::text[], %s::date[], %s::numeric[]) AS v(index_name, day, value)"
        "  ON CONFLICT (index_name, day, value) DO NOTHING RETURNING 1)"
        " SELECT count(*) FROM inserted",
        (
            fetched_at,
            [v.index_name for v in values],
            [v.day for v in values],
            [v.value for v in values],
        ),
    ).fetchone()
    assert result is not None
    return int(result[0])
