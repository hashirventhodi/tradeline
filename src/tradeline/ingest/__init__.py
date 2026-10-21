"""Ingestion services and the record every one of them writes.

An ingest run is one source over one range inside one command (distinct from
the Glossary's Run, a decision cycle). Its row in ``ingest_runs`` is written
when the run finishes, success, failure, or interruption, so the history shows
what each run saw, wrote, and reported. The id exists from the start so every
log line inside the run carries it, with the source and range.
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
class IngestRunCounters:
    """What the run tallies as it goes; copied into its ``ingest_runs`` row at the end."""

    id: uuid.UUID
    rows_seen: int = 0
    rows_written: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@contextmanager
def record_ingest_run(
    conn: psycopg.Connection,
    *,
    command: str,
    source: str,
    range_start: date | None = None,
    range_end: date | None = None,
) -> Iterator[IngestRunCounters]:
    """Run a source, then record it: counts on success; the error and counts so far otherwise.

    The caller commits its own data as it goes; the record is committed here in
    its own transaction so that a failure never loses what was already loaded.
    An interruption (Ctrl-C) is recorded as an error too, then re-raised.
    """
    counters = IngestRunCounters(id=uuid.uuid4())
    started_at = datetime.now(UTC)
    error: str | None = None
    with log_context(
        ingest_run_id=str(counters.id),
        source=source,
        range_start=None if range_start is None else range_start.isoformat(),
        range_end=None if range_end is None else range_end.isoformat(),
    ):
        log.info("ingest run started")
        try:
            yield counters
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _write_record(
                conn, counters, command, source, range_start, range_end, started_at, error
            )
            log.log(
                logging.ERROR if error else logging.INFO,
                "ingest run finished",
                extra={
                    "rows_seen": counters.rows_seen,
                    "rows_written": counters.rows_written,
                    "error": error,
                    **counters.details,
                },
            )


def _write_record(
    conn: psycopg.Connection,
    counters: IngestRunCounters,
    command: str,
    source: str,
    range_start: date | None,
    range_end: date | None,
    started_at: datetime,
    error: str | None,
) -> None:
    try:
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
    except psycopg.Error:
        # The database itself is the problem: do not let the record's failure
        # mask the error the run is reporting (or become one where there was none).
        if error is None:
            raise
        log.exception("could not record the ingest run")
