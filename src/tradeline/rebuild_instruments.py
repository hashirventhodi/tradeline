"""``instruments rebuild``: the link builder run over the stored evidence, its output stored.

The evidence is read from the stored sources (ADR 0016), handed to the pure
builder (``tradeline.instruments.build_instruments``), and its output replaces
the derived tables (``instruments``, ``instrument_intervals``,
``instrument_links``, ``instrument_anomalies``, ``instrument_link_candidates``)
in one transaction, so a
reader sees the old Instruments or the new ones, never a mixture. Nothing
else is written: the end-of-day rows, Kite's candles, the Corporate Actions,
the symbol master, sectors, and the symbol-change records are read only.

The Raw Series is read as spans in NSE's equity listing series (EQ; BE and BZ,
trade-for-trade; SM and ST, SME). The block-deal window (BL), same-day
settlement (T0), and debt and fund series are left out: they trade beside a
Symbol's EQ rows or carry their own ISINs, and would read as ISIN changes.

The manual Instrument Links are the one piece of evidence not in the
database: the caller reads them from the checked-in file
(``tradeline.manual_links``) and passes them in, so a malformed file refuses
the rebuild before anything is read or replaced.

Each rebuild is recorded in the run history (command ``instruments``, source
``instrument_links``) with its counts: Instruments, links by source,
anomalies by kind, and link candidates.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import psycopg

from tradeline import queries
from tradeline.adjust import CorporateAction
from tradeline.ingest import record_ingest_run
from tradeline.instruments import (
    Evidence,
    InstrumentBuild,
    Listing,
    ManualLink,
    Span,
    build_instruments,
)

log = logging.getLogger(__name__)

COMMAND = "instruments"
SOURCE = "instrument_links"
# NSE's equity listing series; an Instrument spans moves between them.
SERIES = ("BE", "BZ", "EQ", "SM", "ST")


@dataclass(frozen=True)
class InstrumentRebuildResult:
    spans: int
    instruments: int
    links: int
    anomalies: int
    candidates: int

    def summary(self) -> dict[str, Any]:
        return {
            "spans": self.spans,
            "instruments": self.instruments,
            "links": self.links,
            "anomalies": self.anomalies,
            "candidates": self.candidates,
        }


def rebuild_instruments(
    conn: psycopg.Connection, *, manual_links: Sequence[ManualLink] = ()
) -> InstrumentRebuildResult:
    with record_ingest_run(conn, command=COMMAND, source=SOURCE) as run:
        evidence = read_evidence(conn, manual_links=manual_links)
        built = build_instruments(evidence)
        _replace(conn, built)
        conn.commit()
        links = Counter(link.kind for i in built.instruments for link in i.links)
        run.rows_seen = len(evidence.spans)
        run.rows_written = len(built.instruments)
        run.details = {
            "spans": len(evidence.spans),
            "symbol_changes": len(evidence.symbol_changes),
            "manual_links": len(evidence.manual_links),
            "instruments": len(built.instruments),
            "links": dict(sorted(links.items())),
            "keys_from": dict(sorted(Counter(i.key_from for i in built.instruments).items())),
            "anomalies": dict(sorted(Counter(a.kind for a in built.anomalies).items())),
            "candidates": len(built.candidates),
        }
        for anomaly in built.anomalies:
            log.warning(
                "instrument history split",
                extra={"kind": anomaly.kind, "symbol": anomaly.symbol, "detail": anomaly.detail},
            )
    return InstrumentRebuildResult(
        spans=len(evidence.spans),
        instruments=len(built.instruments),
        links=sum(links.values()),
        anomalies=len(built.anomalies),
        candidates=len(built.candidates),
    )


def read_evidence(conn: psycopg.Connection, *, manual_links: Sequence[ManualLink] = ()) -> Evidence:
    """Everything the builder reads: the stored sources, and the manual links given."""
    return Evidence(
        spans=_spans(conn),
        symbol_changes=queries.symbol_changes(conn),
        corporate_actions=_adjusting_actions(conn),
        listings=_listings(conn),
        manual_links=tuple(manual_links),
    )


def _spans(conn: psycopg.Connection) -> list[Span]:
    # A new span starts wherever a Symbol's (series, ISIN) differs from its previous row's.
    rows = conn.execute(
        "WITH marked AS ("
        "  SELECT symbol, series, isin, trade_date,"
        "   CASE WHEN (series, isin) IS NOT DISTINCT FROM (lag(series) OVER w, lag(isin) OVER w)"
        "    THEN 0 ELSE 1 END AS starts"
        "  FROM eod_bhavcopy WHERE series = ANY(%s)"
        "  WINDOW w AS (PARTITION BY symbol ORDER BY trade_date, series)),"
        " numbered AS ("
        "  SELECT *, sum(starts) OVER (PARTITION BY symbol ORDER BY trade_date, series) AS run"
        "  FROM marked)"
        " SELECT symbol, series, isin, min(trade_date), max(trade_date) FROM numbered"
        " GROUP BY symbol, run, series, isin ORDER BY symbol, min(trade_date), series",
        (list(SERIES),),
    ).fetchall()
    return [Span(*row) for row in rows]


def _adjusting_actions(conn: psycopg.Connection) -> list[CorporateAction]:
    rows = conn.execute(
        "SELECT symbol, series, ex_date, subject, kind, factor_numerator, factor_denominator,"
        " isin FROM corporate_actions WHERE kind IN ('split', 'bonus')"
        " ORDER BY symbol, ex_date, series, subject"
    ).fetchall()
    return [
        CorporateAction(
            symbol=symbol,
            series=series,
            ex_date=ex_date,
            subject=subject,
            kind=kind,
            factor=Fraction(int(numerator), int(denominator)),
            isin=isin,
        )
        for symbol, series, ex_date, subject, kind, numerator, denominator, isin in rows
    ]


def _listings(conn: psycopg.Connection) -> list[Listing]:
    rows = conn.execute(
        "SELECT DISTINCT symbol, isin, listing_date FROM symbols"
        " WHERE listing_date IS NOT NULL ORDER BY symbol, isin, listing_date"
    ).fetchall()
    return [Listing(*row) for row in rows]


def _replace(conn: psycopg.Connection, built: InstrumentBuild) -> None:
    conn.execute("DELETE FROM instrument_anomalies")
    conn.execute("DELETE FROM instrument_link_candidates")
    conn.execute("DELETE FROM instruments")  # intervals and links go with them
    instruments = built.instruments
    conn.execute(
        "INSERT INTO instruments (key, first_symbol, listed_on, key_from)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[])",
        (
            [i.key for i in instruments],
            [i.first_symbol for i in instruments],
            [i.listed_on for i in instruments],
            [i.key_from for i in instruments],
        ),
    )
    intervals = [(i.key, v) for i in instruments for v in i.intervals]
    conn.execute(
        "INSERT INTO instrument_intervals"
        " (instrument_key, symbol, series, isin, first_day, last_day)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[],"
        "  %s::date[], %s::date[])",
        (
            [key for key, _ in intervals],
            [v.symbol for _, v in intervals],
            [v.series for _, v in intervals],
            [v.isin for _, v in intervals],
            [v.first for _, v in intervals],
            [v.last for _, v in intervals],
        ),
    )
    links = [(i.key, link) for i in instruments for link in i.links]
    conn.execute(
        "INSERT INTO instrument_links"
        " (instrument_key, kind, on_date, from_symbol, from_isin, to_symbol, to_isin, source)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[], %s::text[],"
        "  %s::text[], %s::text[], %s::text[])",
        (
            [key for key, _ in links],
            [x.kind for _, x in links],
            [x.on for _, x in links],
            [x.from_symbol for _, x in links],
            [x.from_isin for _, x in links],
            [x.to_symbol for _, x in links],
            [x.to_isin for _, x in links],
            [x.source for _, x in links],
        ),
    )
    anomalies = built.anomalies
    conn.execute(
        "INSERT INTO instrument_anomalies (kind, symbol, on_date, detail)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[])",
        (
            [a.kind for a in anomalies],
            [a.symbol for a in anomalies],
            [a.on for a in anomalies],
            [a.detail for a in anomalies],
        ),
    )
    candidates = built.candidates
    conn.execute(
        "INSERT INTO instrument_link_candidates"
        " (old_symbol, new_symbol, isin, old_last, new_first)"
        " SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::date[], %s::date[])",
        (
            [c.old_symbol for c in candidates],
            [c.new_symbol for c in candidates],
            [c.isin for c in candidates],
            [c.old_last for c in candidates],
            [c.new_first for c in candidates],
        ),
    )
