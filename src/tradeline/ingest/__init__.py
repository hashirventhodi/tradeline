"""Ingestion services and the run record every one of them writes.

A run is one source over one range inside one command. Its row in
``ingest_runs`` is written when the run finishes, success or failure, so the
history shows what each run saw, wrote, and reported. The run id exists from
the start so every log line inside the run carries it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from tradeline.logging import log_context

log = logging.getLogger(__name__)


@dataclass
class RunCounters:
    """What the run tallies as it goes; copied into its ``ingest_runs`` row at the end."""

    id: uuid.UUID
    rows_seen: int = 0
    rows_written: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@contextmanager
def record_run(
    conn: psycopg.Connection,
    *,
    command: str,
    source: str,
    range_start: date | None = None,
    range_end: date | None = None,
) -> Iterator[RunCounters]:
    """Run a source, then record it: counts on success, the error (and counts so far) on failure.

    The caller commits its own data as it goes; the record is committed here in
    its own transaction so that a failure never loses what was already loaded.
    """
    counters = RunCounters(id=uuid.uuid4())
    started_at = datetime.now(UTC)
    error: str | None = None
    with log_context(run_id=str(counters.id), source=source):
        log.info("run started", extra={"range_start": range_start, "range_end": range_end})
        try:
            yield counters
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            conn.rollback()  # anything half-done by a failed step is not part of the record
            conn.execute(
                "INSERT INTO ingest_runs"
                " (id, command, source, range_start, range_end, started_at, finished_at,"
                "  rows_seen, rows_written, error, details)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    counters.id,
                    command,
                    source,
                    range_start,
                    range_end,
                    started_at,
                    datetime.now(UTC),
                    counters.rows_seen,
                    counters.rows_written,
                    error,
                    Jsonb(counters.details),
                ),
            )
            conn.commit()
            log.log(
                logging.ERROR if error else logging.INFO,
                "run finished",
                extra={
                    "rows_seen": counters.rows_seen,
                    "rows_written": counters.rows_written,
                    "error": error,
                    **counters.details,
                },
            )
