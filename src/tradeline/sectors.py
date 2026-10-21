"""``sectors``: NSE's sector classification into ``sectors``, versioned by effective date.

The classification comes from NSE's NIFTY Total Market constituent list, the
widest list NSE publishes with a sector for each symbol (its ``Industry``
column; the equity master carries none). NSE publishes only the current
classification, so a load is dated by the day it was taken: its rows are in
force from that date.

A load compares each symbol's sector with the one in force on the load's date
and writes a row only where it is new or has changed, so the table holds one
row per change and an earlier date still reads the earlier sector. The table
is append-only. Loading twice on one day with a different sector the second
time is an anomaly (logged, counted, the stored row kept), as for a resent
candle; a load dated before the latest one stored is refused, since it would
insert a classification into history that was not known then.

A symbol the list no longer carries keeps the sector last seen for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from tradeline import queries
from tradeline.ingest import record_ingest_run
from tradeline.sources import nse
from tradeline.transport import Transport

log = logging.getLogger(__name__)

COMMAND = "sectors"
SOURCE = "nse_sectors"


@dataclass(frozen=True)
class SectorChange:
    symbol: str
    before: str
    after: str


@dataclass(frozen=True)
class SectorLoadResult:
    effective_date: date
    rows_seen: int
    rows_written: int
    # Symbols whose sector differs from the one in force before this load.
    changes: tuple[SectorChange, ...]
    anomalies: int

    def summary(self) -> dict[str, Any]:
        return {
            "effective_date": self.effective_date.isoformat(),
            "rows_seen": self.rows_seen,
            "rows_written": self.rows_written,
            "changes": [
                {"symbol": c.symbol, "before": c.before, "after": c.after} for c in self.changes
            ],
            "anomalies": self.anomalies,
        }


def load_sectors(
    conn: psycopg.Connection, transport: Transport, *, effective_date: date
) -> SectorLoadResult:
    latest = conn.execute("SELECT max(effective_from) FROM sectors").fetchone()
    if latest is not None and latest[0] is not None and latest[0] > effective_date:
        raise ValueError(
            f"sectors are recorded from {latest[0]}; a load dated {effective_date} would insert"
            " a classification into history before it"
        )
    session = nse.NseSession(transport)
    changes: list[SectorChange] = []
    anomalies: list[dict[str, Any]] = []
    with record_ingest_run(
        conn,
        command=COMMAND,
        source=SOURCE,
        range_start=effective_date,
        range_end=effective_date,
    ) as run:
        listed = nse.fetch_index_constituents(session, nse.TOTAL_MARKET_LIST)
        run.rows_seen = len(listed)
        incoming = _one_per_symbol(listed)
        in_force = {s.symbol: s for s in queries.sectors(conn, as_of=effective_date)}
        new_rows: list[nse.IndexConstituent] = []
        for row in incoming:
            current = in_force.get(row.symbol)
            if current is None:
                new_rows.append(row)
            elif current.sector == row.sector:
                continue
            elif current.effective_from == effective_date:
                anomalies.append(
                    {"symbol": row.symbol, "stored": current.sector, "loaded": row.sector}
                )
            else:
                new_rows.append(row)
                changes.append(SectorChange(row.symbol, current.sector, row.sector))
        run.rows_written = _insert(conn, new_rows, effective_date)
        conn.commit()
        for anomaly in anomalies:
            log.warning(
                "sector differs from one loaded the same day; stored row kept", extra=anomaly
            )
        for change in changes:
            log.info(
                "sector changed",
                extra={"symbol": change.symbol, "before": change.before, "after": change.after},
            )
        run.details = {
            "effective_date": effective_date.isoformat(),
            "changes": len(changes),
            "anomalies": len(anomalies),
        }
    return SectorLoadResult(
        effective_date=effective_date,
        rows_seen=run.rows_seen,
        rows_written=run.rows_written,
        changes=tuple(changes),
        anomalies=len(anomalies),
    )


def _one_per_symbol(listed: list[nse.IndexConstituent]) -> list[nse.IndexConstituent]:
    """The first row for each symbol; a symbol listed twice with two sectors is refused."""
    unique: dict[str, nse.IndexConstituent] = {}
    for row in listed:
        first = unique.setdefault(row.symbol, row)
        if first.sector != row.sector:
            raise nse.NseError(
                f"{row.symbol} is listed under two sectors: {first.sector!r} and {row.sector!r}"
            )
    return sorted(unique.values(), key=lambda row: row.symbol)


def _insert(
    conn: psycopg.Connection, rows: list[nse.IndexConstituent], effective_date: date
) -> int:
    if not rows:
        return 0
    cursor = conn.execute(
        "INSERT INTO sectors (symbol, effective_from, sector, isin)"
        " SELECT symbol, %s, sector, isin FROM unnest(%s::text[], %s::text[], %s::text[])"
        "  AS n(symbol, sector, isin)",
        (
            effective_date,
            [r.symbol for r in rows],
            [r.sector for r in rows],
            [r.isin or None for r in rows],
        ),
    )
    return cursor.rowcount
