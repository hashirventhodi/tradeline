"""``collect-daily``: one trading day's collection, safe to run from cron after the close.

Four steps, in order, each one the existing ingest service for the day:

1. ``eod``: NSE's end-of-day file and the instrument master (``ingest_eod``).
2. ``candles``: Kite's daily candle for every member of the current Universe,
   the day's month under the configured rule (``ingest_candles``). The
   month's membership is computed first if it has not been (``compute_universe``),
   so the first collection of a month needs no separate ``universe`` run.
3. ``announcements``: NSE's and BSE's filings of the day (``ingest_announcements``).
4. ``news``: what the configured RSS feeds carry now (``ingest_news``).

Every ingest run made inside a collection carries the collection's id, so
``queries.ingest_runs(collection_id=...)`` shows the day source by source. A
step that fails is recorded and the next step still runs. A step that fails
before its service records anything (no Kite access token today, a Universe
that cannot be computed) is recorded here, as a failed run of the source it
was about to load. A source its step never reached, because an earlier source
of the same step failed, is recorded as failed too, with a ``NotRun`` error
naming that source. So every source has a row whatever happened.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.dates import month_of
from tradeline.ingest import in_collection, record_ingest_run
from tradeline.ingest.announcements import SOURCE_BSE, SOURCE_NSE, ingest_announcements
from tradeline.ingest.candles import SOURCE_DAILY, CurrentUniverse, ingest_candles
from tradeline.ingest.eod import (
    SOURCE_EOD,
    SOURCE_EQUITY_MASTER,
    SOURCE_KITE_INSTRUMENTS,
    ingest_eod,
)
from tradeline.ingest.news import SOURCE as SOURCE_RSS
from tradeline.ingest.news import Feed, ingest_news
from tradeline.sources.kite import KiteSession
from tradeline.transport import Transport
from tradeline.universe import compute_universe

log = logging.getLogger(__name__)

COMMAND = "collect-daily"


class NotRun(Exception):
    """A source was not run because an earlier source of its step failed."""


@dataclass(frozen=True)
class CollectResult:
    collection_id: uuid.UUID
    day: date
    runs: tuple[queries.IngestRun, ...]  # oldest first

    @property
    def failed_sources(self) -> tuple[str, ...]:
        return tuple(r.source for r in self.runs if r.status == "failed")

    @property
    def failed_feeds(self) -> tuple[str, ...]:
        """RSS feeds that failed inside a news run that otherwise loaded."""
        return tuple(
            feed
            for r in self.runs
            if r.source == SOURCE_RSS
            for feed in r.details.get("failed_feeds", [])
        )

    @property
    def succeeded(self) -> bool:
        return not self.failed_sources and not self.failed_feeds

    def summary(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "collection_id": str(self.collection_id),
            "runs": {
                r.source: {
                    "status": r.status,
                    "rows_seen": r.rows_seen,
                    "rows_written": r.rows_written,
                    "seconds": round(r.duration.total_seconds(), 3),
                }
                for r in self.runs
            },
            "failed_sources": list(self.failed_sources),
            "failed_feeds": list(self.failed_feeds),
        }


def collect_daily(
    conn: psycopg.Connection,
    *,
    day: date,
    rule: UniverseRule,
    feeds: Sequence[Feed],
    transport: Transport,
    browser_transport: Transport,
    kite: Callable[[], KiteSession],
) -> CollectResult:
    """Collect every source for ``day``; never raises for a failed source.

    ``browser_transport`` is the one BSE needs (a browser's TLS handshake);
    ``kite`` builds the Kite session when the candle step starts, so a missing
    access token fails that step alone.
    """
    collection_id = uuid.uuid4()

    def candles() -> None:
        universe = _current_universe(conn, rule, day)
        ingest_candles(conn, kite(), start=day, end=day, universe=universe)

    steps: list[tuple[str, tuple[str, ...], Callable[[], object]]] = [
        (
            "eod",
            (SOURCE_EQUITY_MASTER, SOURCE_EOD, SOURCE_KITE_INSTRUMENTS),
            lambda: ingest_eod(conn, transport, start=day, end=day),
        ),
        ("candles", (SOURCE_DAILY,), candles),
        (
            "announcements",
            (SOURCE_NSE, SOURCE_BSE),
            lambda: ingest_announcements(conn, browser_transport, start=day, end=day),
        ),
        ("news", (SOURCE_RSS,), lambda: ingest_news(conn, transport, feeds=feeds)),
    ]
    with in_collection(collection_id):
        log.info("collection started", extra={"day": day.isoformat()})
        for step, sources, run in steps:
            try:
                run()
            except Exception as error:
                log.exception("collection step failed", extra={"step": step})
                conn.rollback()
                _record_unrecorded_sources(conn, collection_id, step, sources, day, error)
        runs = tuple(reversed(queries.ingest_runs(conn, collection_id=collection_id)))
        result = CollectResult(collection_id, day, runs)
        log.log(
            logging.INFO if result.succeeded else logging.ERROR,
            "collection finished",
            extra={
                "day": day.isoformat(),
                "failed_sources": list(result.failed_sources),
                "failed_feeds": list(result.failed_feeds),
            },
        )
    return result


def _current_universe(conn: psycopg.Connection, rule: UniverseRule, day: date) -> CurrentUniverse:
    """``day``'s Universe under ``rule``, computing the month's membership if it is not stored."""
    month = month_of(day)
    if not queries.universe_membership(conn, month, as_of=day, rule_version=rule.version):
        compute_universe(conn, rule, month=month)
    return CurrentUniverse(month=month, rule_version=rule.version)


def _record_unrecorded_sources(
    conn: psycopg.Connection,
    collection_id: uuid.UUID,
    step: str,
    sources: tuple[str, ...],
    day: date,
    error: Exception,
) -> None:
    """Give every source of a failed step that has no run a failed one.

    The first source without a row gets ``error`` when no source of the step
    recorded anything (the step failed before its service started); every
    other source without a row was never reached, and says which source failed.
    """
    runs = queries.ingest_runs(conn, collection_id=collection_id)
    recorded = {r.source for r in runs}
    failed = next((r.source for r in runs if r.source in sources and r.status == "failed"), None)
    for source in sources:
        if source in recorded:
            continue
        if failed is None:
            reason: Exception = error
            failed = source
        else:
            reason = NotRun(f"not run because {failed} failed in the {step} step")
        try:
            with record_ingest_run(
                conn, command=COMMAND, source=source, range_start=day, range_end=day
            ):
                raise reason
        except Exception:
            pass
